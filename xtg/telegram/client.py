"""Telethon client factory (Phase 2 boundary).

The import of :mod:`telethon` happens inside :func:`get_client` so the CLI keeps
working when the optional dependency is not installed yet.
"""

from __future__ import annotations

from typing import Any

from xtg.core.config import Settings, get_settings
from xtg.core.errors import AuthError, ConfigError
from xtg.core.logger import logger
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyEntry, ProxyType


def telethon_available() -> bool:
    """True when the optional Telethon dependency is importable."""
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


def to_telethon_proxy(entry: ProxyEntry | None) -> dict[str, Any] | None:
    """Translate a :class:`ProxyEntry` into Telethon's ``proxy=`` argument.

    Telethon expects a mapping with ``proxy_type`` / ``addr`` and optionally
    ``username`` / ``password``. This is the *only* place where the proxy
    subsystem and the Telegram subsystem meet.
    """
    if entry is None:
        return None
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
            hint="Use http, https, socks4 or socks5 proxies.",
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
    """Pick the next proxy for a Telegram connection."""
    settings = settings or get_settings()
    if not settings.proxy_enabled:
        return None
    manager = ProxyManager.from_settings(settings)
    return manager.next_proxy()


def get_client(settings: Settings | None = None, *, proxies: ProxyManager | None = None) -> Any:
    """Create a connected :class:`telethon.TelegramClient`.

    Raises:
        ConfigError: when Telethon is missing or the credentials are incomplete.
    """
    settings = settings or get_settings()
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConfigError(
            "Telethon is not installed",
            hint='Install the MTProto client with `uv add "telethon>=1.34"`.',
        ) from exc

    settings.require_api_credentials()
    settings.ensure_directories()

    entry = None
    if settings.proxy_enabled:
        entry = (proxies or ProxyManager.from_settings(settings)).next_proxy()
        if entry is not None:
            logger.info("Using proxy {} ({})", entry.name, entry.masked_url)

    client = TelegramClient(
        str(settings.session_file),
        settings.api_id,
        settings.api_hash,
        proxy=to_telethon_proxy(entry),
        connection_retries=settings.max_retries,
        request_retries=settings.max_retries,
        connect_timeout=settings.connect_timeout,
    )
    if not client.is_authorized():
        raise AuthError(
            "The Telegram session is not authorized",
            hint="Run `xtg auth login` first.",
        )
    return client
