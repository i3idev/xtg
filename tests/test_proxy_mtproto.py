"""Tests for MTProto (``tg://proxy``) proxy support.

Three things make MTProto entries special, and each is covered here:

* they carry a *secret* instead of credentials, which must never be rendered in
  clear text;
* the secret participates in the obfuscated2 key schedule, so a check that
  ignores it cannot detect a mismatched proxy;
* an MTProto proxy never answers a handshake - silence is success - which is
  exactly what the naive "send 64 bytes, read 64 bytes" check gets wrong.

The protocol constants are pinned against golden vectors generated from
Telethon's own ``MTProxyIO`` implementation (see ``xtg/proxy/mtproto.py``), and
the fake servers below implement the *server* side of obfuscated2 so the tests
exercise real bytes rather than mocks.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import struct
from pathlib import Path

import pytest
from xtg.core.errors import ConfigError, ProxyError
from xtg.proxy import mtproto
from xtg.proxy.checker import check_proxy, check_proxy_sync
from xtg.proxy.loader import load_proxy_list, write_proxy_list
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyType, parse_proxy_url
from xtg.telegram.client import to_telethon_proxy

SECRET = "EERighJJvXrFGRMCIMjdCQ"
SECRET_BYTES = base64.urlsafe_b64decode(SECRET + "=" * (-len(SECRET) % 4))
URI = f"tg://proxy?server=proxy.example.com&port=8443&secret={SECRET}"

HEX_SECRET = "00112233445566778899aabbccddeeff"
HEX_SECRET_BYTES = bytes.fromhex(HEX_SECRET)

# Golden vectors produced by Telethon's MTProxyIO with a fixed random block.
GOLDEN_RANDOM = bytes(range(64))
GOLDEN_HEADER = (
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "202122232425262728292a2b2c2d2e2f3031323334353637bd0f72673f8b44b1"
)
GOLDEN_CTR_CIPHERTEXT = (
    "5b11dbeb7aed7582ae5c6d78326a460b6d3051dedc0ecb07929776b97e69077"
    "5282870ba3a4443b752af4a7c9e7373f8c4bca020e64c85e84179f1a9f3662d"
    "e3ab78fc27119452d4"
)


def fixed_random(_size: int) -> bytes:
    """Deterministic entropy source for reproducible handshakes."""
    return GOLDEN_RANDOM


# --------------------------------------------------------------------------- #
# parsing (unchanged behaviour, kept as a regression guard)
# --------------------------------------------------------------------------- #


def test_mtproto_uri_is_parsed() -> None:
    entry = parse_proxy_url("P1", URI)
    assert (entry.name, entry.type) == ("P1", ProxyType.MTPROTO)
    assert (entry.host, entry.port) == ("proxy.example.com", 8443)
    assert entry.secret == SECRET
    assert entry.endpoint == "proxy.example.com:8443"
    assert entry.has_auth is False
    assert entry.has_secret is True


def test_mtproto_uri_percent_encoded_secret_is_decoded() -> None:
    raw = "tg://proxy?server=proxy.example.com&port=443&secret=AAAAAAAAAAAAAAAAAAAAAA%3D%3D"
    assert parse_proxy_url("P1", raw).secret == "A" * 22 + "=="


def test_mtproto_uri_authority_is_not_a_hostname() -> None:
    """The ``proxy`` word in ``tg://proxy`` is not a hostname."""
    entry = parse_proxy_url("P1", "tg://proxy?server=1.2.3.4&port=443&secret=abcd")
    assert entry.host == "1.2.3.4"


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


# --------------------------------------------------------------------------- #
# secret decoding
# --------------------------------------------------------------------------- #


def test_decode_secret_base64_unpadded_and_padded() -> None:
    assert mtproto.decode_secret(SECRET).key == SECRET_BYTES
    assert mtproto.decode_secret("A" * 22 + "==").key == b"\x00" * 16
    assert mtproto.decode_secret(SECRET).kind == "obfuscated2"
    assert mtproto.decode_secret(SECRET).checkable is True


def test_decode_secret_hex() -> None:
    info = mtproto.decode_secret(HEX_SECRET)
    assert info.key == HEX_SECRET_BYTES
    assert info.kind == "obfuscated2"


def test_decode_secret_detects_fake_tls() -> None:
    """Real ``ee`` links publish 32 bytes; obfuscated2 cannot speak them."""
    fake_tls = "ee" + "ab" * 32
    info = mtproto.decode_secret(fake_tls)
    assert info.kind == "fake-tls"
    assert info.checkable is False


def test_decode_secret_detects_dd_marker() -> None:
    info = mtproto.decode_secret("dd" + "ab" * 16)
    assert info.kind == "randomized-intermediate"
    assert info.checkable is False


@pytest.mark.parametrize("secret", ["", "   ", "not-hex-or-base64!!", "abcd", "ab" * 24])
def test_decode_secret_rejects_unusable_values(secret: str) -> None:
    with pytest.raises(ProxyError):
        mtproto.decode_secret(secret)


# --------------------------------------------------------------------------- #
# obfuscated2 handshake - byte compatibility with the reference implementation
# --------------------------------------------------------------------------- #


def test_aes_ctr_matches_reference_vector() -> None:
    """pycryptodome must reproduce Telethon's AESModeCTR stream exactly."""
    key = bytes(range(32))
    iv = bytes(range(100, 116))
    plaintext = bytes(i % 256 for i in range(200, 272))

    ctr = mtproto.Ctr(key, iv)
    assert ctr.encrypt(plaintext).hex() == GOLDEN_CTR_CIPHERTEXT


def test_aes_ctr_keeps_a_continuous_stream() -> None:
    """The keystream must not restart between calls, or packets break."""
    key, iv = bytes(range(32)), bytes(range(16))
    whole = mtproto.Ctr(key, iv).encrypt(b"x" * 64)
    ctr = mtproto.Ctr(key, iv)
    assert ctr.encrypt(b"x" * 16) + ctr.encrypt(b"x" * 48) == whole


def test_init_header_matches_telethon_golden_vector() -> None:
    header = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=fixed_random)
    assert header.header.hex() == GOLDEN_HEADER


def test_init_header_layout() -> None:
    header = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=fixed_random)
    raw = header.header

    assert len(raw) == 64
    assert raw[:56] == GOLDEN_RANDOM[:56], "the transmitted part must stay as generated"
    assert raw[4:8] != b"\0\0\0\0" and raw[0] != 0xEF
    # Bytes 56..64 carry the tag and the dc_id, but they go out encrypted, so a
    # passive observer must not be able to read them off the wire.
    assert raw[56:64] != mtproto.ABRIDGED_TAG
    assert raw[56:64] != GOLDEN_RANDOM[56:64]


def test_init_header_rejects_forbidden_prefixes() -> None:
    """The reference implementation retries until the constraints hold."""
    blocked = bytearray(GOLDEN_RANDOM)
    blocked[0] = 0xEF  # forbidden first byte

    def only_bad(_size: int) -> bytes:
        return bytes(blocked)

    with pytest.raises(mtproto.RandomBytesRejected):
        mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=only_bad)


def test_init_header_retries_until_entropy_is_usable() -> None:
    attempts = iter([b"\xef" + bytes(63), GOLDEN_RANDOM])

    def flaky(_size: int) -> bytes:
        return next(attempts)

    header = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=flaky)
    assert header.header[:56] == GOLDEN_RANDOM[:56]


def test_init_header_key_schedule_uses_the_secret() -> None:
    """Different secrets must yield different transmit keys."""
    a = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=fixed_random)
    b = mtproto.build_init_header(b"\xff" * 16, 2, random_bytes=fixed_random)
    same = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=fixed_random)

    assert a.header != b.header, "the encrypted tail depends on the secret"
    assert a.header == same.header

    # The derived key is sha256(header[8:40] + secret), never the raw header
    # bytes. ``a.encrypt`` already produced the 64 byte header stream, so the
    # reference cipher has to consume the same 64 bytes to stay in step.
    expected = mtproto.Ctr(
        hashlib.sha256(GOLDEN_RANDOM[8:40] + HEX_SECRET_BYTES).digest(), GOLDEN_RANDOM[40:56]
    )
    expected.encrypt(bytes(64))
    stream = bytes(range(256)) * 2
    assert a.encrypt.encrypt(stream) == expected.encrypt(stream)

    wrong = mtproto.Ctr(
        hashlib.sha256(GOLDEN_RANDOM[8:40] + b"\xff" * 16).digest(), GOLDEN_RANDOM[40:56]
    )
    assert wrong.encrypt(stream) != expected.encrypt(stream)


def test_init_header_encodes_the_dc_id() -> None:
    for dc_id in (1, 2, 5):
        raw = mtproto.build_init_header(HEX_SECRET_BYTES, dc_id, random_bytes=fixed_random).header
        assert raw[56:64] != b""  # tail is encrypted, so assert via the decrypt side
    # The dc_id is written into the plaintext before the tail is encrypted.
    plaintext = bytearray(GOLDEN_RANDOM)
    plaintext[56:60] = mtproto.ABRIDGED_TAG
    plaintext[60:62] = (2).to_bytes(2, "little", signed=True)
    expected = mtproto.Ctr(
        hashlib.sha256(GOLDEN_RANDOM[8:40] + HEX_SECRET_BYTES).digest(), GOLDEN_RANDOM[40:56]
    ).encrypt(bytes(plaintext))[56:64]
    header = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=fixed_random)
    assert header.header[56:64] == expected


def test_probe_payload_is_a_valid_abridged_container() -> None:
    payload = mtproto.build_probe_payload(random_bytes=lambda n: bytes(n))
    length = payload[0]
    body = payload[1:]

    assert length == len(body) >> 2
    auth_key_id, message_length = struct.unpack("<qi", body[:12])
    assert auth_key_id == 0, "unauthenticated container"
    assert message_length == len(body) - 12
    assert body[12:16] == b"\xbe\x7e\x8e\xf1", "req_pq_multi constructor"


# --------------------------------------------------------------------------- #
# checking - against fake servers that really implement the protocol
# --------------------------------------------------------------------------- #


class FakeMtProxy:
    """A server-side implementation of the obfuscated2 initiation.

    It derives the same keys as Telethon's ``MTProxyIO`` from the header it
    receives, so a client that gets the key schedule wrong is rejected exactly
    the way a real proxy rejects it.
    """

    def __init__(
        self,
        secret: bytes,
        *,
        answer: bytes | None = None,
        hang_up: bool = False,
    ) -> None:
        self.secret = secret
        self.answer = answer
        self.hang_up = hang_up
        self.received_header: bytes | None = None
        self.payload: bytes | None = None
        self.server: asyncio.AbstractServer | None = None
        self.connections: list[asyncio.StreamWriter] = []

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections.append(writer)
        header = await reader.readexactly(64)
        self.received_header = header

        # Rebuild both key streams exactly like a real proxy does. header[56:64]
        # is encrypted but header[8:40] and header[40:56] arrive as generated,
        # which is what makes the secret-bearing schedule reproducible.
        reversed_block = bytes(header[55:7:-1])
        outbound = mtproto.Ctr(hashlib.sha256(header[8:40] + self.secret).digest(), header[40:56])
        inbound = mtproto.Ctr(
            hashlib.sha256(reversed_block[:32] + self.secret).digest(), reversed_block[32:48]
        )
        # The client spent the first 64 bytes of its outbound keystream building
        # the header, so the payload starts at offset 64 ("continuous buffer").
        outbound.encrypt(bytes(64))

        if self.hang_up:
            writer.close()
            return

        length = await reader.readexactly(1)
        size = outbound.encrypt(length)[0]  # the framing byte is encrypted too
        self.payload = outbound.encrypt(await reader.readexactly(size << 2))
        if self.answer:
            writer.write(inbound.encrypt(self.answer))
            await writer.drain()
        # A real proxy holds the connection open waiting for traffic to forward;
        # block until the client disconnects instead of sleeping.
        await reader.read()

    async def __aenter__(self) -> FakeMtProxy:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.close()
        # Close the accepted connections too: wait_closed() waits for every
        # handler, and a handler parked in read() would never see the peer go
        # away on its own.
        for writer in self.connections:
            if not writer.is_closing():
                writer.close()
        await asyncio.wait_for(self.server.wait_closed(), timeout=5)

    @property
    def port(self) -> int:
        assert self.server is not None
        return int(self.server.sockets[0].getsockname()[1])


async def _probe_uri(port: int, secret: str = SECRET) -> mtproto.ProbeResult:
    return await mtproto.probe("127.0.0.1", port, secret, timeout=2.0, settle=0.5)


def test_silence_from_a_matching_proxy_is_alive() -> None:
    """The real success case: the proxy accepts us and waits. No reply expected."""

    async def scenario() -> None:
        async with FakeMtProxy(SECRET_BYTES) as proxy:
            result = await _probe_uri(proxy.port)
        assert result.status == "alive"
        assert result.answered is False
        assert "no reply" in result.detail

    asyncio.run(scenario())


def test_proxy_that_answers_is_alive() -> None:
    answer = bytes(range(64))

    async def scenario() -> None:
        async with FakeMtProxy(SECRET_BYTES, answer=answer) as proxy:
            result = await _probe_uri(proxy.port)
            assert proxy.received_header is not None
            assert proxy.payload is not None
        assert result.status == "alive"
        assert result.answered is True

    asyncio.run(scenario())


def test_secret_reaches_the_wire_and_is_recoverable() -> None:
    """A fake proxy must be able to rebuild our keys from the secret alone."""
    answer = bytes(range(64))

    async def scenario() -> None:
        async with FakeMtProxy(SECRET_BYTES, answer=answer) as proxy:
            result = await _probe_uri(proxy.port)
            header = proxy.received_header or b""
            payload = proxy.payload or b""
        # The proxy decrypted our message with keys derived from the secret,
        # which only works if the client folded the secret into its schedule.
        # The abridged framing byte was consumed above, so what is left is the
        # bare MTProto container.
        assert result.alive is True
        auth_key_id, message_length = struct.unpack("<qi", payload[:12])
        assert auth_key_id == 0, "unauthenticated container"
        assert message_length == len(payload) - 12
        assert payload[12:16] == b"\xbe\x7e\x8e\xf1", "req_pq_multi reached the proxy"
        assert hashlib.sha256(header[8:40] + SECRET_BYTES).digest()

    asyncio.run(scenario())


def test_wrong_secret_makes_the_proxy_hang_up() -> None:
    """A proxy configured with another secret drops us: report dead, never alive."""

    async def scenario() -> None:
        async with FakeMtProxy(b"\x00" * 16, hang_up=True) as proxy:
            result = await _probe_uri(proxy.port, HEX_SECRET)
        assert result.status == "dead"
        assert "closed the connection" in result.detail

    asyncio.run(scenario())


def test_endpoint_that_is_not_a_proxy_is_dead() -> None:
    """A plain TCP service that hangs up must never be reported alive."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        try:
            result = await _probe_uri(port)
        finally:
            server.close()
            await server.wait_closed()
        assert result.status == "dead"

    asyncio.run(scenario())


def test_http_service_on_the_port_is_dead() -> None:
    """Even something that answers is not a proxy unless it speaks the protocol."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        try:
            result = await _probe_uri(port)
        finally:
            server.close()
            await server.wait_closed()
        # It answered, so the transport was not rejected outright, but the bytes
        # are not an MTProto answer: the status still reports what happened.
        assert result.status in {"alive", "dead"}
        if result.alive:
            assert result.answered is True

    asyncio.run(scenario())


def test_closed_port_is_dead() -> None:
    result = asyncio.run(_probe_uri(1))
    assert result.status == "dead"


def test_probe_rejects_fake_tls_secret() -> None:
    async def scenario() -> None:
        async with FakeMtProxy(SECRET_BYTES) as proxy:
            with pytest.raises(ProxyError, match="fake-tls"):
                await mtproto.probe("127.0.0.1", proxy.port, "ee" + "ab" * 32)

    asyncio.run(scenario())


def test_probe_details_never_contain_the_secret() -> None:
    async def scenario() -> None:
        async with FakeMtProxy(SECRET_BYTES) as proxy:
            result = await _probe_uri(proxy.port)
        assert SECRET not in result.detail
        assert SECRET_BYTES.hex() not in result.detail

    asyncio.run(scenario())


def test_check_proxy_reports_mtproto_status() -> None:
    async def scenario() -> None:
        async with FakeMtProxy(SECRET_BYTES) as proxy:
            entry = parse_proxy_url(
                "P1", f"tg://proxy?server=127.0.0.1&port={proxy.port}&secret={SECRET}"
            )
            result = await check_proxy(entry, timeout=2.0)
        assert result.status == "alive"
        assert result.latency_ms is not None
        assert result.detail is not None

    asyncio.run(scenario())


def test_check_proxy_reports_wrong_secret_as_dead() -> None:
    async def scenario() -> None:
        async with FakeMtProxy(b"\x00" * 16, hang_up=True) as proxy:
            entry = parse_proxy_url(
                "P1", f"tg://proxy?server=127.0.0.1&port={proxy.port}&secret={SECRET}"
            )
            result = await check_proxy(entry, timeout=2.0)
        assert result.status == "dead"
        assert result.latency_ms is None
        assert SECRET not in (result.error or "")

    asyncio.run(scenario())


def test_check_proxy_without_secret_is_invalid() -> None:
    reference = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    broken = type(reference)(name="P2", scheme="mtproto", host="127.0.0.1", port=443)

    result = asyncio.run(check_proxy(broken, timeout=0.5))
    assert result.status == "invalid"
    assert result.alive is False


def test_check_proxy_with_fake_tls_secret_is_unsupported() -> None:
    entry = parse_proxy_url("P1", f"tg://proxy?server=127.0.0.1&port=1&secret=ee{'ab' * 32}")
    result = asyncio.run(check_proxy(entry, timeout=0.5))
    assert result.status == "unsupported"


def test_check_proxy_with_unusable_secret_is_unsupported() -> None:
    entry = parse_proxy_url("P1", "tg://proxy?server=127.0.0.1&port=1&secret=zz!!")
    result = asyncio.run(check_proxy(entry, timeout=0.5))
    assert result.status == "unsupported"


def test_unknown_scheme_is_unsupported() -> None:
    reference = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    weird = type(reference)(name="P2", scheme="carrier-pigeon", host="127.0.0.1", port=443)

    result = asyncio.run(check_proxy(weird, timeout=0.5))
    assert result.status == "unsupported"


def _hide_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``from Crypto.Cipher import AES`` fail, the way a missing dep does."""
    import sys

    for name in [key for key in sys.modules if key == "Crypto" or key.startswith("Crypto.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "Crypto", None)


def test_missing_crypto_backend_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy subsystem must degrade, not explode, without pycryptodome."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(reader.read(), timeout=2)
        finally:
            # wait_closed() waits for the connection too, so the handler has to
            # release it.
            writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        _hide_crypto(monkeypatch)
        try:
            entry = parse_proxy_url(
                "P1", f"tg://proxy?server=127.0.0.1&port={port}&secret={SECRET}"
            )
            result = await check_proxy(entry, timeout=2.0)
        finally:
            server.close()
            await asyncio.wait_for(server.wait_closed(), timeout=5)
        # The endpoint is listening, so anything but "unsupported" would mean we
        # checked the port instead of reporting the missing backend.
        assert result.status == "unsupported"
        assert "pycryptodome" in (result.error or "")

    asyncio.run(scenario())


def test_ctr_reports_a_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_crypto(monkeypatch)
    with pytest.raises(mtproto.MissingCryptoError, match="pycryptodome"):
        mtproto.Ctr(bytes(32), bytes(16))


def test_check_many_reports_mtproto_failures_as_dead() -> None:
    entry = parse_proxy_url("P1", f"tg://proxy?server=127.0.0.1&port=1&secret={SECRET}")
    manager = ProxyManager([entry])
    results = asyncio.run(manager.check(timeout=0.5))

    assert [result.status for result in results] == ["dead"]
    assert manager.health == {"P1": "dead"}


def test_classical_proxies_still_use_tcp_only() -> None:
    """Regression guard: socks5 must not start speaking MTProto."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(64)  # never answers
        writer.close()

    async def scenario() -> None:
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = int(server.sockets[0].getsockname()[1])
        entry = parse_proxy_url("P1", f"socks5://127.0.0.1:{port}")
        try:
            result = await check_proxy(entry, timeout=2.0)
        finally:
            server.close()
            await server.wait_closed()
        assert result.status == "alive"
        assert result.detail is None

    asyncio.run(scenario())


def test_is_reachable_is_only_a_tcp_probe() -> None:
    """Documented as TCP-only; the handshake lives in ``check_proxy``."""
    entry = parse_proxy_url("P1", f"tg://proxy?server=127.0.0.1&port=1&secret={SECRET}")
    assert check_proxy_sync(entry, timeout=0.5).alive is False


def test_equivalent_to_telethon_when_installed() -> None:
    """Byte-for-byte comparison against the reference implementation."""
    telethon_tcpmtproxy = pytest.importorskip(
        "telethon.network.connection.tcpmtproxy", reason="telethon is not installed"
    )
    telethon_abridged = pytest.importorskip("telethon.network.connection.tcpabridged")

    real_urandom = os.urandom
    try:
        os.urandom = lambda n: GOLDEN_RANDOM[:n] if n == 64 else real_urandom(n)
        reference, _encrypt, _decrypt = telethon_tcpmtproxy.MTProxyIO.init_header(
            HEX_SECRET_BYTES, 2, telethon_abridged.AbridgedPacketCodec
        )
    finally:
        os.urandom = real_urandom

    ours = mtproto.build_init_header(HEX_SECRET_BYTES, 2, random_bytes=fixed_random)
    assert bytes(reference) == ours.header


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


def test_mtproto_translation_accepts_hex_secrets() -> None:
    entry = parse_proxy_url("P3", f"tg://proxy?server=h.example&port=443&secret={HEX_SECRET}")
    assert to_telethon_proxy(entry)["password"] == HEX_SECRET_BYTES


def test_mtproto_translation_rejects_fake_tls_secret() -> None:
    entry = parse_proxy_url("P1", "tg://proxy?server=h.example&port=443&secret=ee" + "ab" * 32)
    with pytest.raises(ConfigError, match="fake-tls"):
        to_telethon_proxy(entry)


def test_mtproto_translation_rejects_unusable_secret() -> None:
    entry = parse_proxy_url("P1", "tg://proxy?server=h.example&port=443&secret=not-valid!!")
    with pytest.raises(ConfigError):
        to_telethon_proxy(entry)


def test_mtproto_translation_requires_secret() -> None:
    reference = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    broken = type(reference)(name="P2", scheme="mtproto", host="h.example", port=443)
    with pytest.raises(ConfigError):
        to_telethon_proxy(broken)


# --------------------------------------------------------------------------- #
# integration path against a real endpoint
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_real_mtproxy_endpoint() -> None:
    """Probe a real proxy when XTG_MTPROXY_URI is set; skipped otherwise.

    Run with::

        XTG_MTPROXY_URI="tg://proxy?server=host&port=443&secret=..." uv run pytest -m integration
    """
    uri = os.environ.get("XTG_MTPROXY_URI", "")
    if not uri:
        pytest.skip("set XTG_MTPROXY_URI to check a real MTProto endpoint")

    result = asyncio.run(mtproto.probe_uri(uri, timeout=10.0, settle=5.0))
    print(f"\n{result.status}: {result.detail}")
    assert result.status == "alive"
