"""Proxy selection and health bookkeeping.

Responsibilities kept deliberately narrow: load a list, remember which entries
failed, hand out the next usable proxy round-robin. Nothing here knows about
Telegram - see :mod:`xtg.telegram.client` for that adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from xtg.core.config import Settings, get_settings
from xtg.core.errors import ProxyError
from xtg.proxy.checker import ProxyCheckResult, check_many
from xtg.proxy.loader import InvalidProxy, ProxyLoadResult, load_proxy_list
from xtg.proxy.parser import ProxyEntry


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


class ProxyManager:
    """Owns the proxy list and its health state."""

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
        self._failures: dict[str, int] = {}
        self._successes: dict[str, int] = {}
        self._alive: set[str] = set()
        self._dead: set[str] = set()

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

    @property
    def usable(self) -> list[ProxyEntry]:
        """Entries that are not known to be dead."""
        if not self._dead:
            return list(self._entries)
        return [entry for entry in self._entries if entry.name not in self._dead]

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
        )

    # --- Selection ---------------------------------------------------------
    def next_proxy(self) -> ProxyEntry | None:
        """Return the next proxy in round-robin order, or ``None`` if disabled."""
        if not self._enabled or not self._entries:
            return None
        candidates = self.usable
        if not candidates:
            raise ProxyError(
                "Every proxy in the list is marked as dead",
                hint="Run `xtg proxy check --all` to re-test the list.",
            )
        for offset in range(len(candidates)):
            candidate = candidates[(self._index + offset) % len(candidates)]
            if candidate.name in self._dead:
                continue
            self._index = (self._index + offset + 1) % len(candidates)
            return candidate
        raise ProxyError(  # pragma: no cover - unreachable while `usable` is non-empty
            "No usable proxy available"
        )

    # --- Health ------------------------------------------------------------
    def mark_success(self, entry: ProxyEntry) -> None:
        self._successes[entry.name] = self._successes.get(entry.name, 0) + 1
        self._alive.add(entry.name)
        self._dead.discard(entry.name)

    def mark_failure(self, entry: ProxyEntry) -> None:
        self._failures[entry.name] = self._failures.get(entry.name, 0) + 1
        self._dead.add(entry.name)
        self._alive.discard(entry.name)

    def apply_check_results(self, results: list[ProxyCheckResult]) -> None:
        """Fold the outcome of ``xtg proxy check`` into the manager state."""
        self._alive.clear()
        self._dead.clear()
        for result in results:
            if result.alive:
                self._alive.add(result.entry.name)
            else:
                self._dead.add(result.entry.name)

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
