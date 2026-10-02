"""Lightweight metrics collection.

Collection must be cheap enough to sit in the hot path: every method here is a
dictionary increment or an append to a bounded list, with no locks, no timers
and no allocation beyond the value being stored. Percentiles come from a
fixed-size window, so memory is bounded regardless of run length.

The three pieces mirror what a collection run needs to answer:

counters
    ``items``, ``requests``, ``errors``, ``retries``, ``failovers``.
histograms
    latency distributions (``p50``/``p95``/``p99``) for requests, connections
    and proxies.
gauges
    instantaneous values such as queue depth or active workers.

Rates are derived on demand from :meth:`Metrics.snapshot` rather than maintained
incrementally, so idle metrics cost nothing.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Latency samples kept per histogram. Large enough for stable p99 on a
#: collection run, small enough to stay in cache and bounded in memory.
DEFAULT_WINDOW = 512


@dataclass(slots=True)
class Histogram:
    """Bounded latency window with percentile reporting.

    Samples are kept in a plain list that is compacted once it is twice
    ``window``. That keeps the common case (a list append) as cheap as
    possible while bounding memory and percentile error over long runs.
    """

    window: int = DEFAULT_WINDOW
    _samples: list[float] = field(default_factory=list, repr=False)
    count: int = 0
    total_ms: float = 0.0

    def observe(self, value_ms: float) -> None:
        self._samples.append(value_ms)
        self.count += 1
        self.total_ms += value_ms
        if len(self._samples) > self.window * 2:
            del self._samples[: self.window]

    @property
    def mean(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    @property
    def last(self) -> float | None:
        return self._samples[-1] if self._samples else None

    def percentile(self, fraction: float) -> float:
        """Nearest-rank percentile; ``0.0`` when nothing was observed."""
        if not self._samples:
            return 0.0
        ordered = sorted(self._samples)
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[index]

    def snapshot(self) -> dict[str, float]:
        return {
            "count": self.count,
            "mean_ms": round(self.mean, 3),
            "p50_ms": round(self.percentile(0.50), 3),
            "p95_ms": round(self.percentile(0.95), 3),
            "p99_ms": round(self.percentile(0.99), 3),
        }

    def recent(self, limit: int = 0) -> list[float]:
        return self._samples[-limit:] if limit else list(self._samples)

    def reset(self) -> None:
        self._samples.clear()
        self.count = 0
        self.total_ms = 0.0


class Metrics:
    """Counters, latency histograms and gauges for one collection run.

    Usage::

        metrics = Metrics()
        metrics.incr("items")
        metrics.observe("request_ms", 12.5)
        metrics.gauge("queue", len(queue))
        snapshot = metrics.snapshot()  # rates included
    """

    __slots__ = ("counters", "gauges", "histograms", "started_at")

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.histograms: dict[str, Histogram] = {}
        self.gauges: dict[str, float] = {}
        self.started_at = time.perf_counter()

    # --- recording ----------------------------------------------------------
    def incr(self, name: str, amount: int = 1) -> None:
        """Add ``amount`` to a counter (default 1)."""
        self.counters[name] = self.counters.get(name, 0) + amount

    def observe(self, name: str, value_ms: float) -> None:
        """Record a latency sample in milliseconds."""
        histogram = self.histograms.get(name)
        if histogram is None:
            histogram = self.histograms[name] = Histogram()
        histogram.observe(value_ms)

    def gauge(self, name: str, value: float) -> None:
        """Publish an instantaneous value."""
        self.gauges[name] = value

    # --- reading ------------------------------------------------------------
    def get(self, name: str, default: float = 0.0) -> float:
        return self.counters.get(name, default)

    def histogram(self, name: str) -> Histogram:
        return self.histograms.get(name) or Histogram()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def rate(self, name: str) -> float:
        """Items per second for a counter over the whole run."""
        elapsed = self.elapsed
        return (self.counters.get(name, 0) / elapsed) if elapsed > 0 else 0.0

    def snapshot(self) -> dict[str, Any]:
        """Everything measured so far, including derived per-second rates."""
        elapsed = self.elapsed
        payload: dict[str, Any] = {
            "elapsed_s": round(elapsed, 4),
            "counters": dict(self.counters),
            "rates": {
                name: round(value / elapsed, 3) if elapsed > 0 else 0.0
                for name, value in self.counters.items()
            },
            "gauges": dict(self.gauges),
            "histograms": {name: hist.snapshot() for name, hist in self.histograms.items()},
        }
        return payload

    def merge(self, other: Metrics) -> Metrics:
        """Fold another collector's numbers into this one.

        Only the samples still inside ``other``'s bounded windows survive, so the
        merged percentiles describe recent behaviour rather than the full run.
        """
        for name, value in other.counters.items():
            self.incr(name, value)
        for name, hist in other.histograms.items():
            target = self.histograms.setdefault(name, Histogram(window=hist.window))
            for sample in hist.recent():
                target.observe(sample)
        self.gauges.update(other.gauges)
        return self

    def reset(self) -> None:
        self.counters.clear()
        self.histograms.clear()
        self.gauges.clear()
        self.started_at = time.perf_counter()

    # --- formatting ---------------------------------------------------------
    def render(self, *, title: str = "", extra: Iterable[tuple[str, Any]] = ()) -> str:
        """Compact human readable summary (used by the CLI and the logs)."""
        lines: list[str] = []
        if title:
            lines.append(title)
            lines.append("─" * max(len(title), 28))
        for label, value in extra:
            lines.append(f"{label:<12} {value}")
        for name in sorted(self.counters):
            lines.append(f"{name:<12} {self.counters[name]}")
        request = self.histogram("request_ms")
        if request.count:
            snap = request.snapshot()
            lines.append(
                f"{'latency':<12} p50={snap['p50_ms']}ms "
                f"p95={snap['p95_ms']}ms p99={snap['p99_ms']}ms"
            )
        for name in sorted(self.gauges):
            lines.append(f"{name:<12} {self.gauges[name]:g}")
        return "\n".join(lines)


class Timer:
    """Context manager feeding a :class:`Histogram`.

    ``with Timer(metrics, "request_ms"): ...`` measures the block. Kept as a
    class (not a decorator) so the hot path is a plain ``__exit__``.
    """

    __slots__ = ("histogram_name", "metrics", "started_at")

    def __init__(self, metrics: Metrics, name: str = "request_ms") -> None:
        self.metrics = metrics
        self.histogram_name = name
        self.started_at = 0.0

    def __enter__(self) -> Timer:
        self.started_at = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.metrics.observe(self.histogram_name, (time.perf_counter() - self.started_at) * 1000)


class RateWindow:
    """Sliding window of events, for throughput over the recent past.

    Cheaper than a full histogram when all that is needed is "items in the last
    ``seconds``": a deque of timestamps and a pointer past expired entries.
    """

    __slots__ = ("_events", "window_s")

    def __init__(self, window_s: float = 10.0, *, maxlen: int = 100_000) -> None:
        from collections import deque

        self._events: deque[float] = deque(maxlen=maxlen)
        self.window_s = window_s

    def record(self, amount: int = 1, now: float | None = None) -> None:
        stamp = time.perf_counter() if now is None else now
        for _ in range(amount):
            self._events.append(stamp)

    def count(self, now: float | None = None) -> int:
        stamp = time.perf_counter() if now is None else now
        cutoff = stamp - self.window_s
        events = self._events
        while events and events[0] < cutoff:
            events.popleft()
        return len(events)

    def rate(self, now: float | None = None) -> float:
        """Events per second over the part of the window that is covered."""
        stamp = time.perf_counter() if now is None else now
        events = self._events
        while events and events[0] < stamp - self.window_s:
            events.popleft()
        if not events:
            return 0.0
        span = min(self.window_s, max(stamp - events[0], 1e-9))
        return len(events) / span

    def __iter__(self) -> Iterator[float]:
        return iter(self._events)


@dataclass(slots=True)
class Sample:
    """One benchmark observation, comparable before and after."""

    scenario: str
    label: str
    metrics: dict[str, Any]

    def value(self, key: str, default: float = 0.0) -> float:
        return float(self.metrics.get(key, default) or default)


def rss_mb() -> float:
    """Resident set size in MiB, or ``0.0`` when it cannot be read.

    Read from ``/proc`` when available so the benchmark has no hard dependency;
    the resource module is the portable fallback.
    """
    try:
        with Path("/proc/self/statm").open(encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return pages * 4096 / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:  # pragma: no cover - platform dependent
            return 0.0


def cpu_seconds() -> float:
    """Process CPU time (user + system) in seconds."""
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime
    except Exception:  # pragma: no cover - platform dependent
        return 0.0
