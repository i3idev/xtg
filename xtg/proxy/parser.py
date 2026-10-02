"""Proxy URL parsing.

The proxy subsystem is completely independent from the Telegram subsystem: it
only knows about URLs, availability and rotation. Converting an entry into a
Telethon connection tuple happens in :mod:`xtg.telegram.client`.

Five protocols are supported:

``http`` / ``https`` / ``socks4`` / ``socks5``
    Classical URL proxies, optionally with ``user:pass`` credentials. A bare
    ``host:port`` is treated as ``socks5``.
``mtproto``
    Telegram MTProto proxies (``tg://proxy?server=…&port=…&secret=…``). These
    carry a *secret*, not a username/password pair, so they are stored in
    :attr:`ProxyEntry.secret` and must never be rendered in clear text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import ParseResult, parse_qs, urlparse

from xtg.core.config import mask_secret
from xtg.core.errors import ProxyError

SUPPORTED_SCHEMES: dict[str, ProxyType] = {}

#: Schemes that introduce a Telegram MTProto proxy URI instead of a network URL.
MTPROTO_SCHEMES: frozenset[str] = frozenset({"tg", "mtproto"})


class ProxyType(StrEnum):
    """Supported proxy protocols."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"
    MTPROTO = "mtproto"
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
    secret: str | None = field(default=None, repr=False)

    @property
    def type(self) -> ProxyType:
        return (
            ProxyType.MTPROTO
            if self.scheme in MTPROTO_SCHEMES
            else SUPPORTED_SCHEMES.get(self.scheme, ProxyType.UNKNOWN)
        )

    @property
    def has_auth(self) -> bool:
        return bool(self.username and self.password)

    @property
    def has_secret(self) -> bool:
        """True for MTProto proxies, which authenticate with a secret."""
        return bool(self.secret)

    @property
    def endpoint(self) -> str:
        """``host:port`` without credentials."""
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """Canonical URL form, safe to write back to the list file."""
        if self.type is ProxyType.MTPROTO:
            return f"tg://proxy?server={self.host}&port={self.port}&secret={self.secret or ''}"
        auth = f"{self.username}:{self.password}@" if self.has_auth else ""
        return f"{self.scheme}://{auth}{self.endpoint}"

    @property
    def masked_url(self) -> str:
        """URL with every secret hidden, for logs, tables and error messages."""
        if self.type is ProxyType.MTPROTO:
            secret = mask_secret(self.secret) if self.secret else ""
            return f"tg://proxy?server={self.host}&port={self.port}&secret={secret}"
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
        """Serialisable view. Secrets are masked, never included verbatim."""
        payload: dict[str, object] = {
            "name": self.name,
            "scheme": self.scheme,
            "type": str(self.type),
            "host": self.host,
            "port": self.port,
            "endpoint": self.endpoint,
            "username": self.username,
            "has_auth": self.has_auth,
            "has_secret": self.has_secret,
        }
        if self.has_secret:
            payload["secret"] = mask_secret(self.secret)
        return payload


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
        tg://proxy?server=host&port=443&secret=abcdef0123456789

    Entries without a scheme are assumed to be ``socks5`` proxies, which is the
    most common case for Telegram clients. Telegram MTProto URIs carry a secret
    instead of credentials and are parsed by :func:`_build_mtproto_entry`.

    Raises:
        ProxyError: when the URL is empty, has an unknown scheme, lacks a valid
            host/port pair or is a malformed MTProto URI.
    """
    if raw is None or not str(raw).strip():
        raise ProxyError(f"Proxy {name!r} is empty")

    value = str(raw).strip()
    scheme = detect_scheme(value)
    if scheme is None:
        return parse_host_port_user_pass(name, value)
    if scheme in MTPROTO_SCHEMES:
        return _build_mtproto_entry(name, value)
    if scheme not in SUPPORTED_SCHEMES:
        raise ProxyError(
            f"Proxy {name!r} uses the unsupported scheme {scheme!r}",
            hint=(
                f"Supported schemes: {', '.join(sorted(SUPPORTED_SCHEMES))} "
                f"and tg://proxy for MTProto"
            ),
        )
    try:
        return _build_entry(name, str(SUPPORTED_SCHEMES[scheme]), urlparse(value), raw=value)
    except ProxyError:
        # "socks5://host:port:user:pass" is not valid URL syntax but is a
        # widespread shorthand: fall back to the positional parser.
        return parse_host_port_user_pass(name, value, scheme=str(SUPPORTED_SCHEMES[scheme]))


def _build_mtproto_entry(name: str, raw: str) -> ProxyEntry:
    """Parse a Telegram MTProto proxy URI.

    The canonical form is ``tg://proxy?server=HOST&port=PORT&secret=SECRET``;
    all three parameters are required. The authority of the URI is the literal
    word ``proxy``, so the host is only read from the ``server`` parameter.
    """
    params = {key: values[0] for key, values in parse_qs(urlparse(raw).query).items() if values}

    host = (params.get("server") or "").strip()
    if not host:
        raise ProxyError(
            f"MTProto proxy {name!r} has no server: {raw!r}",
            hint="Example: tg://proxy?server=host.tld&port=443&secret=abcdef...",
        )

    raw_port = (params.get("port") or "").strip()
    if not raw_port:
        raise ProxyError(
            f"MTProto proxy {name!r} has no port: {raw!r}",
            hint="The 'port' query parameter is mandatory, e.g. ...&port=443",
        )
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ProxyError(f"MTProto proxy {name!r} has an invalid port {raw_port!r}") from exc
    if not 1 <= port <= 65535:
        raise ProxyError(f"MTProto proxy {name!r} has an out-of-range port {port}")

    # ``parse_qs`` already percent-decodes the value, so ``%3D%3D`` -> ``==``.
    secret = (params.get("secret") or "").strip()
    if not secret:
        raise ProxyError(
            f"MTProto proxy {name!r} has no secret: {raw!r}",
            hint="The 'secret' query parameter is mandatory for MTProto proxies.",
        )
    return ProxyEntry(
        name=name,
        scheme=str(ProxyType.MTPROTO),
        host=host,
        port=port,
        secret=secret,
    )


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
