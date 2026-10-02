"""MTProto ``obfuscated2`` transport - the minimum needed to health-check a proxy.

Why this module exists
----------------------
A TCP connect to an MTProto proxy proves nothing: the port can be open while the
endpoint is an unrelated service, and a proxy with the wrong secret happily
accepts the connection. Checking a proxy *as a proxy* means speaking its
transport, so this module implements the obfuscated2 initiation.

Provenance
----------
This is a byte-compatible port of the maintained reference implementation,
Telethon's :mod:`telethon.network.connection.tcpmtproxy` (``MTProxyIO``,
Telethon >= 1.34), with the AES-CTR primitive delegated to :mod:`pycryptodome`
(Telethon's own crypto backend) instead of :mod:`pyaes`. Byte compatibility is
pinned by golden vectors in ``tests/test_proxy_mtproto.py``, generated from
Telethon's code, plus an equivalence test that runs whenever Telethon happens to
be installed.

What the protocol actually requires
-----------------------------------
Three facts drive the implementation, and all three contradict the naive
"connect, send 64 random bytes, read 64 bytes" approach:

1. **The proxy never answers the handshake.** It waits for a full MTProto
   message. A proxy that has accepted us is therefore *silent*; waiting for a
   reply always times out. Telethon treats this as success: after connecting it
   waits for EOF and only fails if the peer closes
   (``TcpMTProxy._connect``, the workaround for Telethon#1134).

2. **The 64-byte header is not random bytes.** Its layout is:

   ============  =====================================================
   ``0..8``      random, but not starting with ``\\xef``, ``PVrG``,
                 ``GET ``, ``POST`` or ``\\xee\\xee\\xee\\xee``; bytes
                 ``4..8`` must not be zero
   ``8..40``     AES key material
   ``40..56``    AES IV (the initial CTR counter block)
   ``56..60``    obfuscated transport tag (``\\xef\\xef\\xef\\xef`` for
                 the abridged codec)
   ``60..62``    DC id, little endian
   ``62..64``    random
   ============  =====================================================

   The trailing 8 bytes are transmitted *encrypted*: the sender overwrites them
   with the AES-256-CTR output for that offset, so a passive observer cannot
   forge the tag.

3. **The secret is part of the key schedule.** The transmit and receive keys are
   ``sha256(header[8:40] + secret)`` and ``sha256(reversed[8:56] + secret)``, not
   the raw header bytes. That is what makes the check meaningful: a proxy
   configured with a different secret derives a different key, fails to parse the
   message that follows and closes the connection - so silence-after-handshake
   really does indicate a usable proxy with a matching secret.

No Telegram account, session or authorization key is involved: this is transport
level only, which is exactly why it is safe to run against a third-party host.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from xtg.core.errors import ProxyError

#: Obfuscated messages cannot start with these four bytes (Telethon: init_header).
FORBIDDEN_PREFIXES: tuple[bytes, ...] = (b"PVrG", b"GET ", b"POST", b"\xee\xee\xee\xee")

#: Transport tag for the abridged codec, written at header[56:60].
ABRIDGED_TAG = b"\xef\xef\xef\xef"

HEADER_SIZE = 64
DC_ID_OFFSET = 60

#: Telegram production DC 2. The proxy uses it to pick an upstream; a health
#: check does not need the DC to answer, only the proxy to accept the transport.
DEFAULT_DC_ID = 2

#: Secret kinds Telegram uses in ``tg://proxy`` links.
SECRET_SIZE = 16
FAKE_TLS_SECRET_SIZE = 32

#: ``req_pq_multi#be7e8ef1 nonce:int128 rpc_calls:int128`` - the unauthenticated
#: "give me a prime" request every MTProto client sends first.
_REQ_PQ_MULTI = b"\xbe\x7e\x8e\xf1"


class Ctr:
    """AES-256-CTR with a 16 byte IV used as the initial counter block.

    Equivalent to Telethon's ``AESModeCTR`` (which wraps ``pyaes``): the counter
    is the big endian 128 bit block held in ``iv`` and it keeps running across
    calls, so the stream stays continuous.
    """

    __slots__ = ("_cipher",)

    def __init__(self, key: bytes, iv: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"AES-256 needs a 32 byte key, got {len(key)}")
        if len(iv) != 16:
            raise ValueError(f"CTR needs a 16 byte iv, got {len(iv)}")
        try:
            from Crypto.Cipher import AES
        except ImportError as exc:  # pragma: no cover - exercised via monkeypatching
            raise MissingCryptoError("pycryptodome is required for the MTProto handshake") from exc
        self._cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv)

    def encrypt(self, data: bytes) -> bytes:
        return self._cipher.encrypt(data)


class MissingCryptoError(ProxyError):
    """Raised when the AES backend is not installed."""


class RandomBytesRejected(ProxyError):
    """Raised when entropy never satisfies the obfuscated2 header constraints."""


@dataclass(frozen=True, slots=True)
class SecretInfo:
    """A decoded MTProto proxy secret.

    ``kind`` distinguishes the transports Telegram ships:

    ``"obfuscated2"``
        16 raw bytes. This is what ``tg://proxy`` links normally carry and the
        only kind this module can health-check.
    ``"fake-tls"``
        32 raw bytes (``ee`` secrets). A completely different, TLS shaped
        handshake that ``obfuscated2`` does not speak.
    ``"randomized-intermediate"``
        ``dd`` secrets, which require the randomized intermediate codec.
    """

    key: bytes
    kind: Literal["obfuscated2", "fake-tls", "randomized-intermediate"]

    @property
    def checkable(self) -> bool:
        return self.kind == "obfuscated2"


def decode_secret(secret: str) -> SecretInfo:
    """Decode a ``tg://proxy`` secret into raw bytes.

    Telegram publishes secrets in two shapes:

    * a bare 16 byte key, hex or (unpadded) base64url encoded;
    * the same thing behind an ``ee`` (fake-TLS) or ``dd`` (randomized
      intermediate) marker.

    The marker is *not* stripped blindly: a perfectly ordinary base64url secret
    can start with ``ee`` by chance - the shipped ``EERighJJvXrFGRMCIMjdCQ``
    entries do - and cutting two characters off it yields a different key. The
    string is therefore decoded as-is first, and only if that yields no usable
    16 byte key is the marker considered.

    Raises:
        ProxyError: when the secret is empty, undecodable, or of a size that
            matches no known MTProto transport.
    """
    raw = (secret or "").strip()
    if not raw:
        raise ProxyError("MTProto proxy has no secret")

    candidates = _decodings(raw)
    for candidate in candidates:
        if len(candidate) == SECRET_SIZE:
            return SecretInfo(key=candidate, kind="obfuscated2")

    marker = raw[:2].lower()
    if marker in ("ee", "dd"):
        kind: Literal["fake-tls", "randomized-intermediate"] = (
            "fake-tls" if marker == "ee" else "randomized-intermediate"
        )
        for candidate in _decodings(raw[2:]):
            if len(candidate) in (SECRET_SIZE, FAKE_TLS_SECRET_SIZE):
                return SecretInfo(key=candidate, kind=kind)

    if candidates:
        size = len(candidates[0])
        if size == FAKE_TLS_SECRET_SIZE:
            # A 32 byte key+iv pair published without its ``ee`` marker.
            return SecretInfo(key=candidates[0], kind="fake-tls")
        raise ProxyError(
            f"MTProto secret decodes to {size} bytes, expected {SECRET_SIZE}",
            hint="Telegram secrets are 16 bytes, base64 or hex encoded.",
        )
    raise ProxyError(f"MTProto secret is neither hex nor base64: {raw[:4]}…")


def _decodings(raw: str) -> list[bytes]:
    """Every valid decoding of ``raw``: hex first, then base64 (Telethon's order).

    Both encodings can succeed for the same string - a 32 character hex secret is
    also valid base64 - so the caller selects by length.
    """
    candidates: list[bytes] = []
    with contextlib.suppress(ValueError):
        candidates.append(bytes.fromhex(raw))  # hex first, like Telethon
    padded = raw + "=" * (-len(raw) % 4)
    for value in (padded.translate(bytes.maketrans(b"-_", b"+/")), padded):
        with contextlib.suppress(binascii.Error, ValueError):
            candidates.append(base64.b64decode(value, validate=True))
    return candidates


@dataclass(frozen=True, slots=True)
class InitHeader:
    """The 64 byte obfuscated2 initiation plus its derived key streams."""

    header: bytes
    encrypt: Ctr
    decrypt: Ctr


def _init_random(random_bytes: Callable[[int], bytes]) -> bytes:
    """64 random bytes that satisfy the obfuscated2 initiation constraints.

    Telethon retries until the block is usable. The retry count is bounded here
    so a broken or deterministic entropy source fails loudly instead of hanging
    the caller.
    """
    for _ in range(100):
        candidate = random_bytes(HEADER_SIZE)
        if (
            candidate[0] != 0xEF
            and candidate[:4] not in FORBIDDEN_PREFIXES
            and candidate[4:8] != b"\0\0\0\0"
        ):
            return candidate
    raise RandomBytesRejected("entropy source never produced a valid obfuscated2 header")


def build_init_header(
    secret: bytes,
    dc_id: int = DEFAULT_DC_ID,
    *,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> InitHeader:
    """Build the obfuscated2 initiation for ``secret`` (Telethon's init_header).

    Args:
        secret: the 16 raw secret bytes.
        dc_id: destination DC, written at ``header[60:62]``.
        random_bytes: entropy source, injectable for deterministic tests.
    """
    header = bytearray(_init_random(random_bytes))
    reversed_block = bytes(header[55:7:-1])  # bytes 55..8, reversed (48 bytes)

    # The secret is folded into both key schedules: that is what makes a wrong
    # secret detectable (the proxy derives different keys and hangs up).
    encryptor = Ctr(hashlib.sha256(bytes(header[8:40]) + secret).digest(), bytes(header[40:56]))
    decryptor = Ctr(hashlib.sha256(reversed_block[:32] + secret).digest(), reversed_block[32:48])

    header[56:60] = ABRIDGED_TAG
    header[DC_ID_OFFSET : DC_ID_OFFSET + 2] = dc_id.to_bytes(2, "little", signed=True)
    # The last 8 bytes go out encrypted (Telethon: random[56:64] = encryptor.encrypt(random)[56:64]).
    header[56:64] = encryptor.encrypt(bytes(header))[56:64]
    return InitHeader(header=bytes(header), encrypt=encryptor, decrypt=decryptor)


def build_probe_payload(*, random_bytes: Callable[[int], bytes] = os.urandom) -> bytes:
    """The smallest valid MTProto message, framed for the abridged codec.

    ``auth_key_id = 0`` marks an unauthenticated message; ``req_pq_multi`` is
    what a real client opens with, and it needs no authorization key - only the
    proxy secret negotiated above.
    """
    message = _REQ_PQ_MULTI + random_bytes(16) + random_bytes(16)
    container = struct.pack("<qi", 0, len(message)) + message
    length = len(container) >> 2
    if length < 0x7F:
        return struct.pack("B", length) + container
    return b"\x7f" + length.to_bytes(3, "little") + container


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of an obfuscated2 probe."""

    status: Literal["alive", "dead"]
    detail: str
    answered: bool = False
    latency_ms: float | None = None

    @property
    def alive(self) -> bool:
        return self.status == "alive"


async def probe(
    host: str,
    port: int,
    secret: str,
    *,
    timeout: float = 5.0,
    settle: float = 2.0,
    dc_id: int = DEFAULT_DC_ID,
) -> ProbeResult:
    """Speak obfuscated2 to ``host:port`` and report whether the proxy accepts us.

    The sequence mirrors Telethon's ``TcpMTProxy``: send the initiation followed
    by a real message, then give the proxy ``settle`` seconds. Closing during that
    window means the proxy rejected the transport - a mismatched secret, a proxy
    that does not speak obfuscated2, or an endpoint that is not a proxy at all.
    Staying open is acceptance: MTProto proxies answer nothing until they can
    forward a real message, and the reference implementation treats that silence
    as a successful connection.

    Args:
        settle: how long to watch for a hang-up or a reply after the handshake.
            Telethon uses 2 seconds for exactly this step.
    """
    info = decode_secret(secret)
    if not info.checkable:
        raise ProxyError(
            f"secret is a {info.kind} secret, which does not speak obfuscated2",
            hint="Only 16-byte secrets can be checked this way.",
        )

    started = asyncio.get_running_loop().time()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except TimeoutError:
        return ProbeResult(status="dead", detail=f"timeout after {timeout:g}s")
    except OSError as exc:
        return ProbeResult(status="dead", detail=str(exc))

    try:
        try:
            header = build_init_header(info.key, dc_id)
            writer.write(header.header)
            writer.write(header.encrypt.encrypt(build_probe_payload()))
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except TimeoutError:
            return ProbeResult(status="dead", detail="timeout while sending the handshake")
        except OSError as exc:
            return ProbeResult(status="dead", detail=f"connection failed while sending: {exc}")

        try:
            data = await asyncio.wait_for(reader.read(HEADER_SIZE), timeout=settle)
        except TimeoutError:
            # Silence is the documented success case: the proxy is waiting for a
            # message it can forward, exactly like Telethon after connecting.
            return ProbeResult(
                status="alive",
                detail=(
                    f"obfuscated2 accepted, no reply within {settle:g}s "
                    f"(proxy is waiting for a routable message)"
                ),
                answered=False,
                latency_ms=_elapsed_ms(started),
            )
        except (OSError, EOFError) as exc:
            return ProbeResult(
                status="dead",
                detail=(
                    f"proxy closed the connection after the obfuscated2 handshake "
                    f"({exc or 'EOF'}): wrong secret, or not an MTProto proxy"
                ),
            )

        if not data:
            return ProbeResult(
                status="dead",
                detail="proxy closed the connection immediately after the handshake",
            )
        return ProbeResult(
            status="alive",
            detail=f"proxy answered with {len(data)} bytes",
            answered=True,
            latency_ms=_elapsed_ms(started),
        )
    finally:
        # Every exit path closes the socket, including a crypto backend that
        # turns out to be missing after the connection was already open.
        with contextlib.suppress(OSError):
            writer.close()


def _elapsed_ms(started: float) -> float:
    return (asyncio.get_running_loop().time() - started) * 1000


async def probe_uri(uri: str, **kwargs: float | int) -> ProbeResult:
    """Parse a ``tg://proxy`` URI and probe it. Used by the manual entry point."""
    from xtg.proxy.parser import parse_proxy_url

    entry = parse_proxy_url("probe", uri)
    return await probe(entry.host, entry.port, entry.secret or "", **kwargs)  # type: ignore[arg-type]
