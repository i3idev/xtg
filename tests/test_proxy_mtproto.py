"""Tests for MTProto (``tg://proxy``) proxy support.

MTProto entries are special in three ways that the classical proxy tests never
exercise: they carry a *secret* instead of credentials, the secret must never be
rendered in clear text, and a successful TCP connect is not proof that the proxy
works.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from xtg.core.errors import ConfigError, ProxyError
from xtg.proxy.checker import check_proxy, check_proxy_sync
from xtg.proxy.loader import load_proxy_list, write_proxy_list
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyType, parse_proxy_url
from xtg.telegram.client import to_telethon_proxy

SECRET = "EERighJJvXrFGRMCIMjdCQ"
SECRET_BYTES = base64.urlsafe_b64decode(SECRET + "=" * (-len(SECRET) % 4))
URI = f"tg://proxy?server=proxy.example.com&port=8443&secret={SECRET}"


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_mtproto_uri_is_parsed() -> None:
    entry = parse_proxy_url("P1", URI)
    assert (entry.name, entry.type) == ("P1", ProxyType.MTPROTO)
    assert (entry.host, entry.port) == ("proxy.example.com", 8443)
    assert entry.secret == SECRET
    assert entry.endpoint == "proxy.example.com:8443"
    # A secret is not username/password authentication.
    assert entry.has_auth is False
    assert entry.has_secret is True


def test_mtproto_uri_percent_encoded_secret_is_decoded() -> None:
    raw = "tg://proxy?server=proxy.example.com&port=443&secret=AAAAAAAAAAAAAAAAAAAAAA%3D%3D"
    assert parse_proxy_url("P1", raw).secret == "A" * 22 + "=="


def test_mtproto_uri_without_schemeless_authority() -> None:
    """The ``proxy`` word in ``tg://proxy`` is not a hostname."""
    entry = parse_proxy_url("P1", "tg://proxy?server=1.2.3.4&port=443&secret=abcd")
    assert entry.host == "1.2.3.4"
    assert entry.port == 443


@pytest.mark.parametrize(
    "raw",
    [
        "tg://proxy?port=443&secret=abcd",  # no server
        "tg://proxy?server=proxy.example.com&secret=abcd",  # no port
        "tg://proxy?server=proxy.example.com&port=443",  # no secret
        "tg://proxy?server=proxy.example.com&port=&secret=abcd",  # empty port
        "tg://proxy?server=proxy.example.com&port=http&secret=abcd",  # non numeric
        "tg://proxy?server=proxy.example.com&port=0&secret=abcd",  # below range
        "tg://proxy?server=proxy.example.com&port=65536&secret=abcd",  # above range
    ],
)
def test_mtproto_uri_validation(raw: str) -> None:
    with pytest.raises(ProxyError):
        parse_proxy_url("P1", raw)


def test_mtproto_url_is_canonical() -> None:
    assert parse_proxy_url("P1", URI).url == URI


# --------------------------------------------------------------------------- #
# secret handling - never leak
# --------------------------------------------------------------------------- #


def test_secret_is_masked_everywhere() -> None:
    entry = parse_proxy_url("P1", URI)
    assert SECRET not in entry.masked_url
    assert entry.masked_url.endswith("secret=EERi**************jdCQ")
    assert entry.to_dict()["secret"] != SECRET
    assert SECRET not in json.dumps(entry.to_dict())
    assert SECRET not in repr(entry)


def test_url_keeps_the_real_secret_for_round_trips() -> None:
    """``masked_url`` is for display; ``url`` stays usable for the list file."""
    entry = parse_proxy_url("P1", URI)
    assert entry.url == URI
    assert parse_proxy_url("P1", entry.url).secret == entry.secret


def test_write_list_round_trip(tmp_path: Path) -> None:
    entry = parse_proxy_url("P1", URI)
    path = write_proxy_list(tmp_path / "list_proxy.json", [entry])
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload == {"proxys": {"P1": URI}}
    reloaded = load_proxy_list(path)
    assert reloaded.invalid == []
    assert reloaded.entries[0].secret == SECRET


def test_loader_accepts_mtproto_entries(tmp_path: Path) -> None:
    path = tmp_path / "list_proxy.json"
    path.write_text(
        json.dumps(
            {
                "proxys": {
                    "P1": URI,
                    "P2": "tg://proxy?server=1.2.3.4&port=443",  # invalid: no secret
                    "P3": "socks5://127.0.0.1:1080",
                }
            }
        ),
        encoding="utf-8",
    )
    result = load_proxy_list(path)

    assert [entry.name for entry in result.entries] == ["P1", "P3"]
    assert [item.name for item in result.invalid] == ["P2"]
    assert "secret" in str(result.invalid[0].reason)


# --------------------------------------------------------------------------- #
# checking - a TCP connect is not enough
# --------------------------------------------------------------------------- #


def test_mtproto_without_secret_is_invalid() -> None:
    entry = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    broken = type(entry)(name="P2", scheme="mtproto", host="127.0.0.1", port=443)

    result = asyncio.run(check_proxy(broken, timeout=0.5))
    assert result.status == "invalid"
    assert result.alive is False


def test_unknown_scheme_is_unsupported() -> None:
    entry = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    weird = type(entry)(name="P2", scheme="carrier-pigeon", host="127.0.0.1", port=443)

    result = asyncio.run(check_proxy(weird, timeout=0.5))
    assert result.status == "unsupported"


def test_mtproto_endpoint_that_hangs_up_is_dead() -> None:
    """Port open + connection closed = not a working MTProto proxy."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        entry = parse_proxy_url("P1", f"tg://proxy?server=127.0.0.1&port={port}&secret=abcd")
        try:
            result = await check_proxy(entry, timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()
        assert result.status == "dead"
        assert result.alive is False
        assert "handshake" in (result.error or "").lower()

    asyncio.run(scenario())


def test_mtproto_endpoint_that_answers_handshake_is_alive() -> None:
    """A real MTProto proxy replies with its own 64 obfuscated bytes."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(64)
        writer.write(b"\xab" * 64)
        await writer.drain()
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        entry = parse_proxy_url("P1", f"tg://proxy?server=127.0.0.1&port={port}&secret=abcd")
        try:
            result = await check_proxy(entry, timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()
        assert result.status == "alive"
        assert result.latency_ms is not None

    asyncio.run(scenario())


def test_check_many_reports_mtproto_failures_as_dead() -> None:
    entry = parse_proxy_url("P1", "tg://proxy?server=127.0.0.1&port=1&secret=abcd")
    manager = ProxyManager([entry])
    results = asyncio.run(manager.check(timeout=0.5))

    assert [result.status for result in results] == ["dead"]
    assert manager.health == {"P1": "dead"}


def test_classical_proxies_still_use_tcp_only(tmp_path: Path) -> None:
    """Regression guard: socks5 checking must not start speaking MTProto."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(64)  # never answers
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        entry = parse_proxy_url("P1", f"socks5://127.0.0.1:{port}")
        try:
            result = await check_proxy(entry, timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()
        assert result.status == "alive"

    asyncio.run(scenario())


def test_is_reachable_is_only_a_tcp_probe() -> None:
    """Documented as TCP-only; the handshake lives in ``check_proxy``."""
    entry = parse_proxy_url("P1", "tg://proxy?server=127.0.0.1&port=1&secret=abcd")
    assert check_proxy_sync(entry, timeout=0.5).alive is False


# --------------------------------------------------------------------------- #
# telethon translation
# --------------------------------------------------------------------------- #


def test_mtproto_translation_decodes_secret_to_bytes() -> None:
    assert to_telethon_proxy(parse_proxy_url("P1", URI)) == {
        "proxy_type": "mtproto",
        "addr": ("proxy.example.com", 8443),
        "password": SECRET_BYTES,
    }
    assert len(SECRET_BYTES) == 16


def test_mtproto_translation_accepts_padded_and_hex_secrets() -> None:
    # 22 base64 data characters + "==" == 16 bytes, as in the shipped list.
    padded = parse_proxy_url(
        "P2", "tg://proxy?server=h.example&port=443&secret=AAAAAAAAAAAAAAAAAAAAAA%3D%3D"
    )
    hexed = parse_proxy_url("P3", "tg://proxy?server=h.example&port=443&secret=" + "ab" * 16)

    assert padded.secret == "A" * 22 + "=="
    assert to_telethon_proxy(padded)["password"] == b"\x00" * 16
    assert to_telethon_proxy(hexed)["password"] == b"\xab" * 16


def test_mtproto_translation_rejects_bad_secret() -> None:
    entry = parse_proxy_url("P1", "tg://proxy?server=h.example&port=443&secret=not-hex!!")
    with pytest.raises(ConfigError):
        to_telethon_proxy(entry)


def test_mtproto_translation_rejects_wrong_length_secret() -> None:
    """Some providers publish 32-byte fake-TLS secrets; Telethon needs 16."""
    entry = parse_proxy_url("P1", "tg://proxy?server=h.example&port=443&secret=" + "ab" * 32)
    with pytest.raises(ConfigError):
        to_telethon_proxy(entry)


def test_mtproto_translation_requires_secret() -> None:
    entry = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    broken = type(entry)(name="P2", scheme="mtproto", host="h.example", port=443)
    with pytest.raises(ConfigError):
        to_telethon_proxy(broken)
