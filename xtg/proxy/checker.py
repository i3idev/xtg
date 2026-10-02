"""Proxy availability checks.

The check is deliberately protocol agnostic: it opens a TCP connection to the
proxy endpoint with a timeout and reports the latency. A full SOCKS5/HTTP
handshake is a Phase 2 concern and lives behind the same
:func:`check_proxy` interface (``full=True``) so the CLI does not change.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass
from typing import Literal

from xtg.proxy.parser import ProxyEntry

CheckStatus = Literal["alive", "dead", "error"]


@dataclass(slots=True)
class ProxyCheckResult:
    """Outcome of a single availability check."""

    entry: ProxyEntry
    status: CheckStatus
    latency_ms: float | None = None
    error: str | None = None

    @property
    def alive(self) -> bool:
        return self.status == "alive"


async def check_proxy(entry: ProxyEntry, timeout: float = 5.0) -> ProxyCheckResult:
    """Try to establish a TCP connection to ``entry`` within ``timeout``."""
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
    """Small helper for callers that only need a boolean."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((entry.host, entry.port)) == 0
