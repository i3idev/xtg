"""Telethon client construction and connection lifecycle.

Responsibilities:

* resolve the connection options from :class:`~xtg.core.config.Settings`
  (credentials, session path, retries, proxy)
* build the Telethon client (the import is lazy, see :func:`telethon_available`)
* own the connection lifecycle so callers get a reusable client instance

This module knows *nothing* about collection: turning Telegram objects into
database rows lives in :mod:`xtg.telegram.collector`.

Telethon's ``proxy`` argument is a mapping::

    {"proxy_type": "socks5", "addr": (host, port), "username": ..., "password": ...}

:func:`to_telethon_proxy` is the single place where the proxy subsystem and the
Telegram subsystem meet - no proxy parsing is duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xtg.core.config import Settings, get_settings
from xtg.core.errors import AuthError, ConfigError, NetworkError, ProxyError
from xtg.core.logger import logger
from xtg.proxy import mtproto
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyEntry, ProxyType


def telethon_available() -> bool:
    """True when the Telethon dependency is importable."""
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def to_telethon_proxy(entry: ProxyEntry | None) -> dict[str, Any] | None:
    """Translate a :class:`ProxyEntry` into Telethon's ``proxy=`` argument.

    Returns ``None`` when no proxy should be used (direct connection).

    Telethon expects the MTProto secret as 16 *raw* bytes under ``password``.
    Decoding follows the reference implementation (hex first, then base64, with
    ``ee``/``dd`` markers stripped) and lives in :mod:`xtg.proxy.mtproto` so the
    checker and the client can never disagree about what a secret means.
    """
    if entry is None:
        return None
    if entry.type is ProxyType.MTPROTO:
        if not entry.has_secret:
            raise ConfigError(
                f"MTProto proxy {entry.name} has no secret",
                hint="Use tg://proxy?server=...&port=...&secret=...",
            )
        try:
            info = mtproto.decode_secret(entry.secret or "")
        except ProxyError as exc:
            raise ConfigError(f"Proxy {entry.name} has an unusable secret: {exc}") from exc
        if not info.checkable:
            raise ConfigError(
                f"Proxy {entry.name} uses a {info.kind} secret, which Telethon's "
                f"plain MTProxy connection cannot use",
                hint="Only 16-byte obfuscated2 secrets are supported.",
            )
        return {
            "proxy_type": "mtproto",
            "addr": (entry.host, entry.port),
            "password": info.key,
        }
    types = {
        ProxyType.HTTP: "http",
        ProxyType.HTTPS: "https",
        ProxyType.SOCKS4: "socks4",
        ProxyType.SOCKS5: "socks5",
    }
    proxy_type = types.get(entry.type)
    if proxy_type is None:
        raise ConfigError(
            f"Proxy {entry.name} has an unsupported type: {entry.type}",
            hint="Use http, https, socks4, socks5 or tg://proxy MTProto proxies.",
        )
    payload: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": (entry.host, entry.port),
    }
    if entry.has_auth:
        payload["username"] = entry.username or ""
        payload["password"] = entry.password or ""
    return payload


def select_proxy(settings: Settings | None = None) -> ProxyEntry | None:
    """Pick the next proxy from the configured list (round-robin).

    Returns ``None`` when proxying is disabled or the list is empty, which
    means a direct connection.
    """
    settings = settings or get_settings()
    if not settings.proxy_enabled:
        return None
    return ProxyManager.from_settings(settings).next_proxy()


@dataclass(frozen=True, slots=True)
class ClientOptions:
    """Everything needed to build a Telethon client."""

    session: Path
    api_id: int
    api_hash: str
    proxy: ProxyEntry | None = None
    connection_retries: int = 3
    request_retries: int = 3
    connect_timeout: float = 10.0

    @property
    def proxy_url(self) -> str:
        """Human readable proxy description (password masked)."""
        return self.proxy.masked_url if self.proxy else "direct connection"

    def as_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for :class:`telethon.TelegramClient`."""
        return {
            "session": str(self.session),
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "proxy": to_telethon_proxy(self.proxy),
            "connection_retries": self.connection_retries,
            "request_retries": self.request_retries,
            "connect_timeout": self.connect_timeout,
        }


def resolve_options(
    settings: Settings | None = None, *, proxies: ProxyManager | None = None
) -> ClientOptions:
    """Build :class:`ClientOptions` from the configuration.

    Raises:
        ConfigError: when the API credentials are missing or the proxy list is
            unusable.
    """
    settings = settings or get_settings()
    settings.require_api_credentials()
    settings.ensure_directories()

    entry: ProxyEntry | None = None
    if settings.proxy_enabled:
        manager = proxies or ProxyManager.from_settings(settings)
        entry = manager.next_proxy()
        logger.info("Proxy selection: {}", entry.name if entry else "none available")
    return ClientOptions(
        session=settings.session_file,
        api_id=settings.api_id or 0,
        api_hash=settings.api_hash or "",
        proxy=entry,
        connection_retries=settings.max_retries,
        request_retries=settings.max_retries,
        connect_timeout=settings.connect_timeout,
    )


def create_client(options: ClientOptions) -> Any:
    """Instantiate (but do not connect) a :class:`telethon.TelegramClient`.

    Raises:
        ConfigError: when Telethon is not installed.
    """
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise ConfigError(
            "Telethon is not installed",
            hint='Install the MTProto client with `uv add "telethon>=1.34"` followed by `uv sync`.',
        ) from exc

    logger.info(
        "Creating Telegram client (session={}, proxy={})", options.session, options.proxy_url
    )
    return TelegramClient(**options.as_kwargs())


class TelegramConnection:
    """Reusable, lazily connected Telethon client.

    Use it as an async context manager::

        async with TelegramConnection(settings) as client:
            ...

    The same instance can be reused by several collectors in the same process;
    :meth:`open` is idempotent and :meth:`close` never raises.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        proxies: ProxyManager | None = None,
        require_authorized: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.proxies = proxies
        self.require_authorized = require_authorized
        self.options = resolve_options(self.settings, proxies=proxies)
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        """The connected client instance (created on first access)."""
        if self._client is None:
            raise RuntimeError("Use `await connection.open()` before touching the client.")
        return self._client

    async def open(self) -> Any:
        """Create the client, connect it and verify the session.

        Telethon's ``is_user_authorized()`` is a coroutine, hence the async
        signature.
        """
        if self._client is not None:
            return self._client
        client = create_client(self.options)
        try:
            await client.connect()
        except NetworkError:
            raise
        except (OSError, TimeoutError) as exc:
            raise NetworkError(
                f"Could not reach Telegram: {exc}",
                hint=(
                    "Check the network or the configured proxy "
                    f"({self.options.proxy_url or 'direct connection'})."
                ),
            ) from exc
        if self.require_authorized and not await client.is_user_authorized():
            await client.disconnect()
            raise AuthError(
                "The Telegram session is not authorized",
                hint="Run `xtg auth login` first.",
            )
        self._client = client
        logger.debug("Telegram client connected via {}", self.options.proxy_url)
        return client

    async def close(self) -> None:
        """Disconnect and forget the client. Never raises."""
        if self._client is None:
            return
        client, self._client = self._client, None
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001 - shutdown must not fail the command
            logger.debug("Ignoring error while disconnecting: {}", exc)

    async def __aenter__(self) -> Any:
        return await self.open()

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<TelegramConnection session={self.options.session.name} proxy={self.options.proxy_url}>"
