"""Proxy availability checks.

For classical proxies the check is protocol agnostic: it opens a TCP connection
to the endpoint with a timeout and reports the latency.

MTProto proxies need more than that. A successful TCP connect only proves that
*something* listens on the endpoint - a web server, a port scanner or a proxy
that instantly drops our traffic would all look "reachable" - and an MTProto
proxy never answers a handshake, so waiting for a reply proves nothing either.
This check therefore speaks the proxy's transport (see :mod:`xtg.proxy.mtproto`):
the obfuscated2 initiation is keyed with the proxy secret, a real MTProto
message follows it, and the verdict is whether the proxy hangs up on us or keeps
the connection open waiting for a message it can forward.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass
from typing import Literal

from xtg.core.errors import ProxyError
from xtg.proxy import mtproto
from xtg.proxy.parser import ProxyEntry, ProxyType

CheckStatus = Literal["alive", "dead", "invalid", "unsupported", "error"]


@dataclass(slots=True)
class ProxyCheckResult:
    """Outcome of a single availability check.

    ``alive`` means the proxy can serve Telegram traffic, not merely that a
    TCP connection succeeded:

    * ``http``/``https``/``socks4``/``socks5``: the TCP endpoint accepted a
      connection.
    * ``mtproto``: the endpoint accepted an obfuscated2 handshake built from the
      proxy secret and did not drop us afterwards.

    ``invalid`` means the entry cannot work as configured (for example an MTProto
    entry without a secret), ``unsupported`` means the protocol is not checkable
    here (unknown scheme, fake-TLS secret, missing crypto backend) and ``error``
    marks an unexpected probe failure.

    ``detail`` carries the protocol level explanation - what the proxy actually
    did - for both healthy and failed entries. It never contains the secret.
    """

    entry: ProxyEntry
    status: CheckStatus
    latency_ms: float | None = None
    error: str | None = None
    detail: str | None = None

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
    if entry.type is ProxyType.MTPROTO:
        return await _check_mtproto(entry, timeout)

    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(entry.host, entry.port), timeout=timeout
        )
    except TimeoutError:
        return ProxyCheckResult(entry=entry, status="dead", error=f"timeout after {timeout:g}s")
    except OSError as exc:
        return ProxyCheckResult(entry=entry, status="dead", error=str(exc))
    else:
        with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
            writer.close()
        latency = (time.perf_counter() - started) * 1000
        return ProxyCheckResult(entry=entry, status="alive", latency_ms=latency)


async def _check_mtproto(entry: ProxyEntry, timeout: float) -> ProxyCheckResult:
    """Probe an MTProto entry by speaking obfuscated2 to it.

    ``alive`` here means the endpoint accepted a secret-keyed obfuscated2
    handshake. It is deliberately not reachable from a plain TCP connect, and a
    proxy that closes on us is reported ``dead`` rather than silently accepted.
    """
    if not entry.has_secret:
        return ProxyCheckResult(
            entry=entry,
            status="invalid",
            error="MTProto proxy has no secret",
        )

    started = time.perf_counter()
    try:
        result = await mtproto.probe(entry.host, entry.port, entry.secret or "", timeout=timeout)
    except mtproto.MissingCryptoError as exc:
        return ProxyCheckResult(entry=entry, status="unsupported", error=str(exc))
    except ProxyError as exc:
        # Unusable secret (wrong size, not hex/base64, fake-TLS transport).
        return ProxyCheckResult(entry=entry, status="unsupported", error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return ProxyCheckResult(entry=entry, status="error", error=str(exc))

    latency = result.latency_ms
    if latency is None:  # pragma: no cover - probe always reports it when alive
        latency = (time.perf_counter() - started) * 1000
    return ProxyCheckResult(
        entry=entry,
        status=result.status,
        latency_ms=latency if result.alive else None,
        error=None if result.alive else result.detail,
        detail=result.detail,
    )


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
    speaks obfuscated2.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.settimeout(timeout)
        return probe_socket.connect_ex((entry.host, entry.port)) == 0
