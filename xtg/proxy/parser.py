"""Proxy URL parsing.

The proxy subsystem is completely independent from the Telegram subsystem: it
only knows about URLs, availability and rotation. Converting an entry into a
Telethon connection tuple happens in :mod:`xtg.telegram.client`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import ParseResult, urlparse

from xtg.core.errors import ProxyError

SUPPORTED_SCHEMES: dict[str, ProxyType] = {}


class ProxyType(StrEnum):
    """Supported proxy protocols."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"
    UNKNOWN = "unknown"


SUPPORTED_SCHEMES.update(
    {
        "http": ProxyType.HTTP,
        "https": ProxyType.HTTPS,
        "socks4": ProxyType.SOCKS4,
        "socks4a": ProxyType.SOCKS4,
        "socks5": ProxyType.SOCKS5,
        "socks5h": ProxyType.SOCKS5,
    }
)


@dataclass(frozen=True, slots=True)
class ProxyEntry:
    """A single, validated proxy."""

    name: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    @property
    def type(self) -> ProxyType:
        return SUPPORTED_SCHEMES.get(self.scheme, ProxyType.UNKNOWN)

    @property
    def has_auth(self) -> bool:
        return bool(self.username and self.password)

    @property
    def endpoint(self) -> str:
        """``host:port`` without credentials."""
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """Canonical URL form, safe to write back to the list file."""
        auth = f"{self.username}:{self.password}@" if self.has_auth else ""
        return f"{self.scheme}://{auth}{self.endpoint}"

    @property
    def masked_url(self) -> str:
        """URL with the password hidden, for logs and tables."""
        auth = f"{self.username}:***@" if self.has_auth else ""
        return f"{self.scheme}://{auth}{self.endpoint}"

    def with_credentials(self, username: str | None, password: str | None) -> ProxyEntry:
        """Return a copy with (possibly) different credentials."""
        return ProxyEntry(
            name=self.name,
            scheme=self.scheme,
            host=self.host,
            port=self.port,
            username=username,
            password=password,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "scheme": self.scheme,
            "type": str(self.type),
            "host": self.host,
            "port": self.port,
            "endpoint": self.endpoint,
            "username": self.username,
            "has_auth": self.has_auth,
        }


def detect_scheme(raw: str) -> str | None:
    """Return the scheme of ``raw`` when it looks like ``scheme://...``."""
    candidate = raw.strip()
    if "://" not in candidate:
        return None
    scheme = candidate.split("://", 1)[0].strip().lower()
    if not scheme or ":" in scheme or "/" in scheme:
        return None
    return scheme


def parse_proxy_url(name: str, raw: str) -> ProxyEntry:
    """Parse ``raw`` into a :class:`ProxyEntry`.

    Supported inputs::

        host:port
        host:port:user:pass
        http://host:port
        socks5://user:pass@host:port

    Entries without a scheme are assumed to be ``socks5`` proxies, which is the
    most common case for Telegram clients.

    Raises:
        ProxyError: when the URL is empty, has an unknown scheme or lacks a
            valid host/port pair.
    """
    if raw is None or not str(raw).strip():
        raise ProxyError(f"Proxy {name!r} is empty")

    value = str(raw).strip()
    scheme = detect_scheme(value)
    if scheme is None:
        return parse_host_port_user_pass(name, value)
    if scheme not in SUPPORTED_SCHEMES:
        raise ProxyError(
            f"Proxy {name!r} uses the unsupported scheme {scheme!r}",
            hint=f"Supported schemes: {', '.join(sorted(SUPPORTED_SCHEMES))}",
        )
    try:
        return _build_entry(name, str(SUPPORTED_SCHEMES[scheme]), urlparse(value), raw=value)
    except ProxyError:
        # "socks5://host:port:user:pass" is not valid URL syntax but is a
        # widespread shorthand: fall back to the positional parser.
        return parse_host_port_user_pass(name, value, scheme=str(SUPPORTED_SCHEMES[scheme]))


def parse_host_port_user_pass(name: str, raw: str, *, scheme: str = "socks5") -> ProxyEntry:
    """Parse the ``host:port`` and ``host:port:user:pass`` shorthand."""
    value = raw.split("://", 1)[-1] if "://" in raw else raw
    parts = [part.strip() for part in value.split(":")]
    if len(parts) == 2:
        host, port = parts
        username = password = None
    elif len(parts) == 4:
        host, port, username, password = parts
    else:
        raise ProxyError(
            f"Proxy {name!r} is not a valid host:port[:user:pass] value: {raw!r}",
            hint="Example: 127.0.0.1:1080 or 127.0.0.1:1080:user:secret",
        )
    if not host:
        raise ProxyError(f"Proxy {name!r} has no host: {raw!r}")
    try:
        port_number = int(port)
    except ValueError as exc:
        raise ProxyError(f"Proxy {name!r} has an invalid port: {raw!r}") from exc
    if not 1 <= port_number <= 65535:
        raise ProxyError(f"Proxy {name!r} has an out-of-range port {port_number}: {raw!r}")
    return ProxyEntry(
        name=name,
        scheme=scheme,
        host=host,
        port=port_number,
        username=username or None,
        password=password or None,
    )


def _build_entry(name: str, scheme: str, parsed: ParseResult, *, raw: str) -> ProxyEntry:
    host = (parsed.hostname or "").strip()
    if not host:
        raise ProxyError(f"Proxy {name!r} has no host: {raw!r}")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ProxyError(f"Proxy {name!r} has an invalid port: {raw!r}") from exc
    if port is None:
        raise ProxyError(
            f"Proxy {name!r} has no port: {raw!r}",
            hint="Ports are mandatory, e.g. socks5://host:1080",
        )
    if not 1 <= port <= 65535:
        raise ProxyError(f"Proxy {name!r} has an out-of-range port {port}: {raw!r}")

    return ProxyEntry(
        name=name,
        scheme=scheme,
        host=host,
        port=port,
        username=parsed.username or None,
        password=parsed.password or None,
    )
