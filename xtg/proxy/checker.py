"""Proxy availability checks.

For classical proxies the check is protocol agnostic: it opens a TCP connection
to the endpoint with a timeout and reports the latency.

MTProto proxies need more than that. A successful TCP connect only proves that
*something* listens on the endpoint - Telegram proxies routinely accept a
connection and then close it or answer with an unrelated protocol, which would
surface much later as an opaque "connection closed" authentication failure.
For MTProto the checker therefore completes the obfuscated2 handshake: it
sends the 64-byte obfuscated initiation a real client sends and requires the
proxy to answer with its own 64 bytes before reporting ``alive``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from dataclasses import dataclass
from typing import Literal

from xtg.proxy.parser import ProxyEntry, ProxyType

CheckStatus = Literal["alive", "dead", "invalid", "unsupported", "error"]

#: Size of the obfuscated2 handshake payloads (64 random bytes + response).
_HANDSHAKE_SIZE = 64


@dataclass(slots=True)
class ProxyCheckResult:
    """Outcome of a single availability check.

    ``alive`` means the proxy is usable for Telegram traffic, not merely that a
    TCP connection succeeded:

    * ``http``/``https``/``socks4``/``socks5``: the TCP endpoint accepted a
      connection.
    * ``mtproto``: the endpoint additionally answered an MTProto handshake.

    ``invalid`` means the entry cannot work as configured (for example an MTProto
    entry without a secret), ``unsupported`` means the protocol is not checkable
    here, and ``error`` marks an unexpected probe failure.
    """

    entry: ProxyEntry
    status: CheckStatus
    latency_ms: float | None = None
    error: str | None = None

    @property
    def alive(self) -> bool:
        return self.status == "alive"


async def check_proxy(entry: ProxyEntry, timeout: float = 5.0) -> ProxyCheckResult:
    """Check whether ``entry`` can serve Telegram traffic within ``timeout``.

    Unusable entries never raise: they are reported as ``invalid`` or
    ``unsupported`` results so a single bad proxy cannot abort a whole run.
    """
    if entry.type is ProxyType.UNKNOWN:
        return ProxyCheckResult(
            entry=entry,
            status="unsupported",
            error=f"unknown proxy scheme {entry.scheme!r}",
        )
    if entry.type is ProxyType.MTPROTO and not entry.has_secret:
        return ProxyCheckResult(
            entry=entry,
            status="invalid",
            error="MTProto proxy has no secret",
        )

    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(entry.host, entry.port), timeout=timeout
        )
    except TimeoutError:
        return ProxyCheckResult(entry=entry, status="dead", error=f"timeout after {timeout:g}s")
    except OSError as exc:
        return ProxyCheckResult(entry=entry, status="dead", error=str(exc))

    try:
        if entry.type is ProxyType.MTPROTO:
            failure = await _mtproto_handshake(reader, writer, timeout=timeout)
            if failure is not None:
                return ProxyCheckResult(entry=entry, status="dead", error=failure)
    except TimeoutError:
        return ProxyCheckResult(
            entry=entry, status="dead", error=f"MTProto handshake timeout after {timeout:g}s"
        )
    except (OSError, EOFError) as exc:
        # Includes ConnectionResetError and asyncio.IncompleteReadError: the
        # endpoint accepted the connection and then dropped us, which is exactly
        # what a non-MTProto service (or a dead proxy) looks like.
        return ProxyCheckResult(
            entry=entry, status="dead", error=f"MTProto handshake failed: {exc or 'closed'}"
        )
    finally:
        with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
            writer.close()

    latency = (time.perf_counter() - started) * 1000
    return ProxyCheckResult(entry=entry, status="alive", latency_ms=latency)


async def _mtproto_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    timeout: float,
) -> str | None:
    """Send an obfuscated2 initiation and wait for the proxy's response.

    Returns:
        ``None`` when the proxy answered the handshake, otherwise a human
        readable reason why the endpoint cannot be used as an MTProto proxy.
    """
    writer.write(os.urandom(_HANDSHAKE_SIZE))
    await asyncio.wait_for(writer.drain(), timeout=timeout)
    try:
        response = await asyncio.wait_for(reader.readexactly(_HANDSHAKE_SIZE), timeout=timeout)
    except asyncio.IncompleteReadError as exc:
        return (
            f"connection closed during the MTProto handshake "
            f"({exc.partial} of {_HANDSHAKE_SIZE} bytes)"
        )
    if all(byte == 0 for byte in response):
        return "endpoint answered the MTProto handshake with silence"
    return None


def check_proxy_sync(entry: ProxyEntry, timeout: float = 5.0) -> ProxyCheckResult:
    """Blocking wrapper around :func:`check_proxy`."""
    return asyncio.run(check_proxy(entry, timeout=timeout))


async def check_many(
    entries: list[ProxyEntry],
    *,
    timeout: float = 5.0,
    concurrency: int = 20,
) -> list[ProxyCheckResult]:
    """Check every proxy concurrently, preserving input order."""
    if not entries:
        return []
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def worker(entry: ProxyEntry) -> ProxyCheckResult:
        async with semaphore:
            return await check_proxy(entry, timeout=timeout)

    return list(await asyncio.gather(*(worker(entry) for entry in entries)))


def is_reachable(entry: ProxyEntry, timeout: float = 3.0) -> bool:
    """Blocking TCP-only reachability probe.

    Kept for callers that just need a cheap liveness signal. It is *not* a
    health verdict: for MTProto entries use :func:`check_proxy_sync`, which also
    performs the handshake.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((entry.host, entry.port)) == 0
