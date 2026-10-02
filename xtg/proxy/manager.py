"""Proxy selection, scoring and health bookkeeping.

Responsibilities kept deliberately narrow: load a list, remember how each entry
behaved, and hand out the next proxy worth using. Nothing here knows about
Telegram - see :mod:`xtg.telegram.client` for that adapter.

Selection is *score based* rather than plain round-robin. A round-robin hands out
a proxy that failed ten requests ago exactly as readily as one that answered a
thousand, so a run that starts on a bad proxy keeps paying for it. Instead each
entry carries:

``successes`` / ``failures``
    Lifetime counters.
``score``
    Success ratio, damped by volume so one lucky request does not outrank a
    long healthy streak, with a latency penalty for slow proxies.
``cooldown_until``
    A monotonic deadline after a failure. The entry is skipped until then
    instead of being written off permanently, so a proxy that recovers comes back
    on its own.

Failover is therefore a rotation through the *highest scoring available* entries,
which is what turns a dead first choice into one skipped entry rather than a
failed run.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from xtg.core.config import Settings, get_settings
from xtg.core.errors import ProxyError
from xtg.proxy.checker import ProxyCheckResult, check_many
from xtg.proxy.loader import InvalidProxy, ProxyLoadResult, load_proxy_list
from xtg.proxy.parser import ProxyEntry

#: Consecutive failures after which a proxy is put on cooldown. One failure is
#: enough: a proxy that drops a connection is a proxy to leave immediately, and
#: the cooldown is what makes coming back possible.
COOLDOWN_THRESHOLD = 1

#: Base cooldown, doubled per consecutive failure up to ``MAX_COOLDOWN``.
COOLDOWN_BASE = 5.0
MAX_COOLDOWN = 300.0

#: Success ratio below which an entry is considered degraded even if reachable.
DEGRADED_SCORE = 0.5


class ProxyState(StrEnum):
    """Where an entry stands right now."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    EXCLUDED = "excluded"


@dataclass(slots=True)
class ProxyHealth:
    """Per-entry runtime health: counters, score and cooldown."""

    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    latency_ms: float = 0.0
    last_used: float = 0.0
    cooldown_until: float = 0.0

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def score(self) -> float:
        """Ranking in ``[0, 1]``: reliability first, volume and latency after.

        The volume damping means a proxy with 1 success out of 1 (1.00) scores
        below one with 900 out of 1000 (0.90) - a single success is not evidence
        of a healthy proxy.
        """
        if self.total == 0:
            return 0.5  # untried: neither trusted nor distrusted
        reliability = self.successes / self.total
        confidence = self.total / (self.total + 10.0)
        base = 0.5 + (reliability - 0.5) * confidence
        if self.latency_ms:
            # Halve the score at a one-second round trip.
            base /= 1.0 + min(self.latency_ms / 1000.0, 1.0)
        return max(0.0, min(1.0, base))

    @property
    def on_cooldown(self) -> bool:
        return self.cooldown_until > time.monotonic()

    @property
    def state(self) -> ProxyState:
        if self.on_cooldown:
            return ProxyState.COOLDOWN
        if self.total == 0:
            return ProxyState.UNKNOWN
        return ProxyState.HEALTHY if self.score >= DEGRADED_SCORE else ProxyState.DEGRADED

    def record_success(self, latency_ms: float = 0.0) -> None:
        self.successes += 1
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.last_used = time.monotonic()
        if latency_ms:
            # Exponential moving average, so one slow request cannot pin the
            # proxy down but a consistently slow one still gets ranked lower.
            self.latency_ms = (
                latency_ms if not self.latency_ms else (self.latency_ms * 0.7 + latency_ms * 0.3)
            )

    def record_failure(self) -> float:
        """Register a failure; returns the cooldown in seconds (0 if none)."""
        self.failures += 1
        self.consecutive_failures += 1
        self.last_used = time.monotonic()
        if self.consecutive_failures < COOLDOWN_THRESHOLD:
            return 0.0
        cooldown = min(
            COOLDOWN_BASE * (2 ** (self.consecutive_failures - COOLDOWN_THRESHOLD)),
            MAX_COOLDOWN,
        )
        self.cooldown_until = time.monotonic() + cooldown
        return cooldown


@dataclass(slots=True)
class ProxyStats:
    """Runtime counters exposed by ``xtg proxy status``."""

    total: int = 0
    valid: int = 0
    invalid: int = 0
    alive: int = 0
    dead: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    successes: dict[str, int] = field(default_factory=dict)
    #: How many times selection had to skip an entry (failover events).
    failovers: int = 0


class ProxyManager:
    """Owns the proxy list, its health state and the selection order."""

    def __init__(
        self,
        entries: Iterable[ProxyEntry] | None = None,
        *,
        source: Path | None = None,
        enabled: bool = True,
        invalid: list[InvalidProxy] | None = None,
    ) -> None:
        self._entries: list[ProxyEntry] = list(entries or [])
        self._invalid: list[InvalidProxy] = list(invalid or [])
        self._index = 0
        self._enabled = enabled
        self.source = source
        self._health: dict[str, ProxyHealth] = {}
        self._failures: dict[str, int] = {}
        self._successes: dict[str, int] = {}
        self._alive: set[str] = set()
        self._dead: set[str] = set()
        self._failovers = 0
        for entry in self._entries:
            self._health.setdefault(entry.name, ProxyHealth())

    # --- Construction ------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ProxyManager:
        """Build a manager from the configured proxy list file."""
        settings = settings or get_settings()
        if not settings.proxy_enabled:
            return cls(entries=[], enabled=False)
        result = load_proxy_list(settings.proxy_path)
        return cls.from_result(result, enabled=True)

    @classmethod
    def from_result(cls, result: ProxyLoadResult, *, enabled: bool = True) -> ProxyManager:
        return cls(
            entries=result.entries,
            invalid=result.invalid,
            source=result.path,
            enabled=enabled,
        )

    # --- Inspection --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def entries(self) -> list[ProxyEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[ProxyEntry]:
        return iter(self._entries)

    @property
    def invalid(self) -> list[InvalidProxy]:
        """Entries that failed to parse when the list was loaded."""
        return list(self._invalid)

    def get(self, name: str) -> ProxyEntry:
        for entry in self._entries:
            if entry.name == name:
                return entry
        raise ProxyError(
            f"Unknown proxy {name!r}",
            hint="Run `xtg proxy list` to see the available names.",
        )

    def _is_excluded(self, entry: ProxyEntry) -> bool:
        """True while the entry is out of the rotation because of a cooldown.

        Excluding is temporary. Once the cooldown expires the entry becomes
        usable again, so a long run that fails every proxy from time to time
        cannot permanently drain the list.
        """
        return entry.name in self._dead and self._health_of(entry).on_cooldown

    @property
    def usable(self) -> list[ProxyEntry]:
        """Entries that are not known to be dead."""
        if not self._dead:
            return list(self._entries)
        return [entry for entry in self._entries if not self._is_excluded(entry)]

    @property
    def health(self) -> dict[str, str]:
        """Last known state per proxy name (``alive`` / ``dead``)."""
        states = dict.fromkeys(self._alive, "alive")
        states.update(dict.fromkeys(self._dead, "dead"))
        return states

    def stats(self) -> ProxyStats:
        return ProxyStats(
            total=len(self._entries) + len(self.invalid),
            valid=len(self._entries),
            invalid=len(self.invalid),
            alive=len(self._alive),
            dead=len(self._dead),
            failures=dict(self._failures),
            successes=dict(self._successes),
            failovers=self._failovers,
        )

    # --- Selection ---------------------------------------------------------
    def score_of(self, entry: ProxyEntry) -> float:
        """Current ranking of one entry in ``[0, 1]``."""
        return self._health_of(entry).score

    def ranking(self) -> list[tuple[ProxyEntry, float]]:
        """Every entry with its score, best first.

        Ties keep list order, so an untried list behaves exactly like the old
        round-robin.
        """
        return sorted(
            ((entry, self.score_of(entry)) for entry in self._entries),
            key=lambda pair: -pair[1],
        )

    def next_proxy(self) -> ProxyEntry | None:
        """Return the best available proxy, or ``None`` when disabled.

        Entries on cooldown are skipped. Among the entries that tie for the best
        score the selection rotates, so a fresh list behaves exactly like the
        round-robin it replaces.

        When every entry is cooling down the one whose cooldown expires first is
        used anyway - a stale proxy beats no connection at all, and the failure
        that follows only costs one cooldown.

        Raises:
            ProxyError: when the list is enabled but every entry is dead.
        """
        if not self._enabled or not self._entries:
            return None
        candidates = self.usable
        if not candidates:
            raise ProxyError(
                "Every proxy in the list is marked as dead",
                hint="Run `xtg proxy check --all` to re-test the list.",
            )
        available = [entry for entry in candidates if not self._health_of(entry).on_cooldown]
        if not available:
            # Everything is cooling down: take the one that recovers first.
            available = sorted(candidates, key=lambda e: self._health_of(e).cooldown_until)
        else:
            best = max(self.score_of(entry) for entry in available)
            # Keep list order inside the tie group so rotation is predictable.
            available = [entry for entry in available if self.score_of(entry) >= best]
        chosen = available[self._index % len(available)]
        self._index = (self._index + 1) % len(available)
        # Count the entries we had to pass over because they are dead or
        # cooling down - not the ones we simply ranked below, or every pick
        # on a long list would look like a failover.
        self._failovers += len(self._entries) - len(candidates)
        return chosen

    # --- Health ------------------------------------------------------------
    def _health_of(self, entry: ProxyEntry) -> ProxyHealth:
        return self._health.setdefault(entry.name, ProxyHealth())

    def mark_success(self, entry: ProxyEntry, *, latency_ms: float = 0.0) -> None:
        self._successes[entry.name] = self._successes.get(entry.name, 0) + 1
        self._alive.add(entry.name)
        self._dead.discard(entry.name)
        self._health_of(entry).record_success(latency_ms)

    def mark_failure(self, entry: ProxyEntry) -> None:
        self._failures[entry.name] = self._failures.get(entry.name, 0) + 1
        cooldown = self._health_of(entry).record_failure()
        if cooldown:
            self._dead.add(entry.name)
            self._alive.discard(entry.name)
        else:
            # One failure is not proof; keep it eligible but ranked lower.
            self._alive.add(entry.name)

    def state_of(self, entry: ProxyEntry) -> ProxyState:
        """Current state of one entry."""
        if self._is_excluded(entry):
            return ProxyState.EXCLUDED
        return self._health_of(entry).state

    def states(self) -> dict[str, str]:
        """State per proxy name, for ``xtg proxy status``."""
        return {entry.name: self.state_of(entry).value for entry in self._entries}

    def apply_check_results(self, results: list[ProxyCheckResult]) -> None:
        """Fold the outcome of ``xtg proxy check`` into the manager state."""
        self._alive.clear()
        self._dead.clear()
        for result in results:
            health = self._health_of(result.entry)
            if result.alive:
                self._alive.add(result.entry.name)
                health.record_success()
            else:
                self._dead.add(result.entry.name)
                health.record_failure()

    async def check(self, *, timeout: float = 5.0, concurrency: int = 20) -> list[ProxyCheckResult]:
        """Check every entry and update the health state."""
        results = await check_many(self._entries, timeout=timeout, concurrency=concurrency)
        self.apply_check_results(results)
        return results

    def reset_health(self) -> None:
        self._alive.clear()
        self._dead.clear()
        self._failures.clear()
        self._successes.clear()
        self._failovers = 0
        self._health = {name: ProxyHealth() for name in self._health}
