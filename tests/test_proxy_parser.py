"""Tests for the proxy URL parser."""

from __future__ import annotations

import pytest
from xtg.core.errors import ProxyError
from xtg.proxy.parser import (
    ProxyType,
    detect_scheme,
    parse_host_port_user_pass,
    parse_proxy_url,
)


@pytest.mark.parametrize(
    ("raw", "scheme", "host", "port"),
    [
        ("socks5://1.2.3.4:1080", "socks5", "1.2.3.4", 1080),
        ("http://proxy.local:8080", "http", "proxy.local", 8080),
        ("https://proxy.local:8443", "https", "proxy.local", 8443),
        ("socks4://10.0.0.1:9050", "socks4", "10.0.0.1", 9050),
        ("socks5h://10.0.0.1:1080", "socks5", "10.0.0.1", 1080),
    ],
)
def test_parse_url_without_credentials(raw: str, scheme: str, host: str, port: int) -> None:
    entry = parse_proxy_url("PX", raw)
    assert (entry.scheme, entry.host, entry.port) == (scheme, host, port)
    assert entry.has_auth is False
    assert entry.endpoint == f"{host}:{port}"


def test_parse_url_with_credentials() -> None:
    entry = parse_proxy_url("P1", "socks5://user:secret@1.2.3.4:1080")
    assert entry.username == "user"
    assert entry.password == "secret"
    assert entry.has_auth is True
    assert entry.url == "socks5://user:secret@1.2.3.4:1080"


def test_password_is_masked() -> None:
    entry = parse_proxy_url("P1", "socks5://user:secret@1.2.3.4:1080")
    assert "secret" not in entry.masked_url
    assert "secret" not in repr(entry)


def test_bare_host_port_defaults_to_socks5() -> None:
    entry = parse_proxy_url("P1", "1.2.3.4:1080")
    assert entry.type is ProxyType.SOCKS5
    assert entry.endpoint == "1.2.3.4:1080"


def test_shorthand_with_credentials() -> None:
    entry = parse_proxy_url("P1", "1.2.3.4:1080:user:pass")
    assert (entry.username, entry.password) == ("user", "pass")


def test_type_detection() -> None:
    assert parse_proxy_url("P1", "socks5://h:1").type is ProxyType.SOCKS5
    assert parse_proxy_url("P1", "http://h:1").type is ProxyType.HTTP


def test_detect_scheme() -> None:
    assert detect_scheme("socks5://host:1080") == "socks5"
    assert detect_scheme("host:1080") is None
    assert detect_scheme("host:1080:user:pass") is None


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "socks5://host", "ftp://host:21", "socks5://host:99999", "host:abc"],
)
def test_invalid_urls_raise(raw: str) -> None:
    with pytest.raises(ProxyError):
        parse_proxy_url("P1", raw)


def test_host_port_user_pass_validation() -> None:
    with pytest.raises(ProxyError):
        parse_host_port_user_pass("P1", "host:port:user")
