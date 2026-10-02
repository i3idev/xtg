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

    Raises:
        ConfigError: when the entry cannot be used as a Telethon proxy.
    """
    if entry is None:
        return None
    if entry.type is ProxyType.MTPROTO:
        return {
            "proxy_type": "mtproto",
            "addr": (entry.host, entry.port),
            "password": decode_proxy_secret(entry).key,
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


def decode_proxy_secret(entry: ProxyEntry) -> mtproto.SecretInfo:
    """Decode an MTProto entry's secret, or explain why it cannot be used.

    Telethon speaks obfuscated2 and the ``dd`` randomized-intermediate
    transport, so both are accepted here. Fake-TLS (``ee``) is a different
    handshake that Telethon cannot perform, and is rejected with a clear reason
    rather than a confusing connection failure.

    Raises:
        ConfigError: when the entry has no secret, or one Telethon cannot use.
    """
    if not entry.has_secret:
        raise ConfigError(
            f"MTProto proxy {entry.name} has no secret",
            hint="Use tg://proxy?server=...&port=...&secret=...",
        )
    try:
        info = mtproto.decode_secret(entry.secret or "")
    except ProxyError as exc:
        raise ConfigError(f"Proxy {entry.name} has an unusable secret: {exc}") from exc
    if not info.telethon_usable:
        raise ConfigError(
            f"Proxy {entry.name} uses a {info.kind} secret, which Telethon cannot use",
            hint="Only 16-byte obfuscated2 and dd randomized-intermediate secrets are supported.",
        )
    return info


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
        """Keyword arguments for :class:`telethon.TelegramClient`.

        MTProto proxies are special in Telethon: they need a dedicated
        connection class and a ``(host, port, secret)`` tuple instead of the
        generic mapping, because the secret has to be fed to the obfuscated2
        handshake. The encoded secret is passed through untouched - Telethon
        decodes it itself, in the same order we do.
        """
        kwargs: dict[str, Any] = {
            "session": str(self.session),
            "api_id": self.api_id,
            "api_hash": self.api_hash,
            "proxy": to_telethon_proxy(self.proxy),
            "connection_retries": self.connection_retries,
            "request_retries": self.request_retries,
            "timeout": self.connect_timeout,
        }

        if self.proxy is not None and self.proxy.type is ProxyType.MTPROTO:
            info = decode_proxy_secret(self.proxy)
            kwargs["connection"] = _mtproxy_connection(info.kind)
            kwargs["proxy"] = (self.proxy.host, self.proxy.port, self.proxy.secret or "")
        return kwargs


def _mtproxy_connection(kind: str) -> Any:
    """The Telethon connection class matching an MTProto secret kind.

    ``dd`` secrets are only accepted by the randomized intermediate codec, so
    the transport is chosen from the secret rather than hardcoded.

    Raises:
        ConfigError: when Telethon is not installed.
    """
    try:
        from telethon.network import connection as telethon_connection
    except ImportError as exc:  # pragma: no cover - guarded earlier by create_client
        raise ConfigError(
            "Telethon is not installed",
            hint='Install the MTProto client with `uv add "telethon>=1.34"` followed by `uv sync`.',
        ) from exc

    if kind == "randomized-intermediate":
        return telethon_connection.ConnectionTcpMTProxyRandomizedIntermediate
    return telethon_connection.ConnectionTcpMTProxyIntermediate


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
        entry, rejected = _select_usable(manager)
        logger.info(
            "Proxy selection: {}",
            entry.name if entry else "none available",
        )
        for name in rejected:
            # A malformed entry is a configuration problem worth surfacing once,
            # but it must not stop us from using the rest of the list.
            logger.warning("Skipping proxy {}: unusable MTProto secret", name)
    return ClientOptions(
        session=settings.session_file,
        api_id=settings.api_id or 0,
        api_hash=settings.api_hash or "",
        proxy=entry,
        connection_retries=settings.max_retries,
        request_retries=settings.max_retries,
        connect_timeout=settings.connect_timeout,
    )


def _select_usable(manager: ProxyManager) -> tuple[ProxyEntry | None, list[str]]:
    """Pick a proxy that can actually be used, skipping the ones that cannot.

    A single malformed entry in the list (an MTProto secret that does not
    decode, a fake-TLS secret Telethon cannot speak) used to abort the whole
    connection, including ``xtg auth login``. Each rejected entry is remembered
    as unhealthy so later selections prefer working proxies, and the returned
    names let the caller report what was skipped.

    Returns:
        The chosen entry (or ``None`` when nothing is usable) and the names that
        were rejected.
    """
    rejected: list[str] = []
    for _ in range(len(manager)):
        try:
            entry = manager.next_proxy()
        except ProxyError:
            # Everything is excluded now; report it as a list problem below.
            break
        if entry is None:
            break
        try:
            to_telethon_proxy(entry)
        except ConfigError as exc:
            logger.debug("Proxy {} is unusable: {}", entry.name, exc)
            rejected.append(entry.name)
            # Remember it so the pool drains away from the broken entries.
            manager.mark_failure(entry)
            continue
        return entry, rejected
    if rejected and not [e for e in manager.usable if e.name not in rejected]:
        raise ConfigError(
            f"No usable proxy in the list ({len(rejected)} rejected)",
            hint="Fix or remove the rejected entries in the proxy list file.",
        )
    return None, rejected


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
