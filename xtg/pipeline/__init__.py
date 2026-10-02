"""Pipeline primitives: bounded queues and adaptive worker pools.

The collection pipeline is deliberately split into small, independently usable
pieces:

:class:`BoundedQueue`
    Backpressure. A producer that outruns the database blocks on ``put`` instead
    of growing an unbounded buffer, so RAM stays flat no matter how fast the
    network is.

:class:`AdaptivePool`
    Concurrency control. Starts at ``min_workers`` and grows while the
    workload is healthy (low latency, no errors, no rate limiting), then shrinks
    fast when something goes wrong. Growth is additive and shrink is
    multiplicative - the AIMD pattern - which is stable compared to reacting
    proportionally to every sample.

Neither piece knows about Telegram, Reddit, proxies or the database: they take
callables and metrics, so they can be tested and reused on their own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from xtg.core.metrics import Metrics
from xtg.core.retry import ErrorKind, RetryPolicy, retry_async

TaskFactory = Callable[[], Awaitable[Any]]


class QueueFull(RuntimeError):
    """Raised by :meth:`BoundedQueue.put_nowait_or_raise` when full."""


@dataclass(slots=True)
class QueueStats:
    """Backpressure accounting for one queue."""

    max_depth: int = 0
    blocked_puts: int = 0
    blocked_seconds: float = 0.0
    dropped: int = 0


class BoundedQueue:
    """An ``asyncio.Queue`` that makes backpressure visible.

    ``put`` blocks when full - that *is* the backpressure - but the wait is
    measured so the CLI and the benchmarks can prove the producer was actually
    throttled by a slow consumer rather than buffering without limit.
    """

    __slots__ = ("_queue", "stats", "high_water")

    def __init__(self, maxsize: int = 1000, *, high_water: float = 0.8) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.stats = QueueStats()
        self.high_water = high_water

    # --- producer side ------------------------------------------------------
    async def put(self, item: Any, *, timeout: float | None = None) -> bool:
        """Enqueue ``item``, waiting for room.

        Returns:
            ``True`` when the item was enqueued, ``False`` when ``timeout``
            expired first (the item is *not* enqueued).
        """
        if not self._queue.full():
            self._queue.put_nowait(item)
            self._track_depth()
            return True
        self.stats.blocked_puts += 1
        started = asyncio.get_running_loop().time()
        try:
            if timeout is None:
                await self._queue.put(item)
                self.stats.blocked_seconds += asyncio.get_running_loop().time() - started
                self._track_depth()
                return True
            await asyncio.wait_for(self._queue.put(item), timeout=timeout)
        except TimeoutError:
            self.stats.blocked_seconds += asyncio.get_running_loop().time() - started
            return False
        self.stats.blocked_seconds += asyncio.get_running_loop().time() - started
        self._track_depth()
        return True

    def put_nowait_or_raise(self, item: Any) -> None:
        """Non blocking insert that reports backpressure instead of hiding it."""
        if self._queue.full():
            raise QueueFull("pipeline queue is full")
        self._queue.put_nowait(item)
        self._track_depth()

    # --- consumer side ------------------------------------------------------
    async def get(self) -> Any:
        item = await self._queue.get()
        self._track_depth()
        return item

    def get_nowait(self) -> Any:
        item = self._queue.get_nowait()
        self._track_depth()
        return item

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until every enqueued item was acknowledged by a consumer."""
        await self._queue.join()

    # --- inspection ---------------------------------------------------------
    def __len__(self) -> int:
        return self._queue.qsize()

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def full(self) -> bool:
        return self._queue.full()

    @property
    def saturated(self) -> bool:
        """True once the queue is at its high-water mark (producer is throttled)."""
        return self.depth >= max(1, int(self.maxsize * self.high_water))

    def _track_depth(self) -> None:
        depth = self._queue.qsize()
        if depth > self.stats.max_depth:
            self.stats.max_depth = depth


@dataclass(slots=True)
class PoolStats:
    """Concurrency accounting for one pool."""

    started: int = 0
    completed: int = 0
    failed: int = 0
    retried: int = 0
    rate_limited: int = 0
    scaled_up: int = 0
    scaled_down: int = 0
    peak_workers: int = 0
    worker_errors: list[str] = field(default_factory=list)

    @property
    def utilization(self) -> float:
        """Completed share of started work (0..1)."""
        total = self.completed + self.failed
        return self.completed / total if total else 0.0


@dataclass(frozen=True, slots=True)
class PoolLimits:
    """Safety bounds for :class:`AdaptivePool`."""

    min_workers: int = 2
    max_workers: int = 8
    #: Latency above this (ms) counts as "slow" for one evaluation window.
    latency_target_ms: float = 2000.0
    #: Consecutive healthy windows required before adding a worker.
    scale_up_after: int = 2
    #: Error ratio that forces a shrink (0..1).
    error_threshold: float = 0.2
    #: How many workers to remove at once on a shrink.
    shrink_step: int = 1

    def clamped(self) -> PoolLimits:
        low = max(1, self.min_workers)
        high = max(low, self.max_workers)
        return PoolLimits(
            min_workers=low,
            max_workers=high,
            latency_target_ms=self.latency_target_ms,
            scale_up_after=max(1, self.scale_up_after),
            error_threshold=self.error_threshold,
            shrink_step=max(1, self.shrink_step),
        )


class AdaptivePool:
    """Run ``operation`` over items with a concurrency level that adapts.

    The controller makes one decision per :meth:`evaluate` call (or every
    ``window`` items when driven from :meth:`map`), using only the counters it
    observed itself:

    * rate limits or too many errors -> shrink immediately (multiplicative);
    * latency above target -> shrink one step;
    * everything healthy -> grow one step, but only after
      ``scale_up_after`` consecutive healthy windows.

    Growth never exceeds ``max_workers`` and never drops below ``min_workers``,
    so a flapping API cannot cause a connection storm in either direction.
    """

    __slots__ = (
        "limits",
        "metrics",
        "stats",
        "_workers",
        "_healthy",
        "_window_failures",
        "_window_successes",
        "_window_latency_ms",
        "_rate_limited",
    )

    def __init__(
        self,
        limits: PoolLimits | None = None,
        *,
        metrics: Metrics | None = None,
    ) -> None:
        self.limits = (limits or PoolLimits()).clamped()
        self.metrics = metrics
        self.stats = PoolStats()
        self._workers = self.limits.min_workers
        self._healthy = 0
        self._window_successes = 0
        self._window_failures = 0
        self._window_latency_ms = 0.0
        self._rate_limited = False

    # --- state --------------------------------------------------------------
    @property
    def workers(self) -> int:
        """Current concurrency level."""
        return self._workers

    @property
    def rate_limited(self) -> bool:
        """True while the API is asking us to slow down."""
        return self._rate_limited

    # --- feedback -----------------------------------------------------------
    def record_success(self, latency_ms: float = 0.0) -> None:
        self._window_successes += 1
        self._window_latency_ms += max(0.0, latency_ms)
        self.stats.completed += 1
        self.stats.peak_workers = max(self.stats.peak_workers, self._workers)

    def record_failure(self, kind: ErrorKind = ErrorKind.TRANSIENT) -> None:
        self._window_failures += 1
        self.stats.failed += 1
        if kind is ErrorKind.RATE_LIMIT:
            self.stats.rate_limited += 1
            self._rate_limited = True

    def record_retry(self) -> None:
        self.stats.retried += 1

    def evaluate(self) -> int:
        """Decide the next concurrency level from the window just observed.

        Returns:
            The level after the decision (unchanged when the window is empty).
        """
        total = self._window_successes + self._window_failures
        if not total:
            return self._workers

        error_ratio = self._window_failures / total
        avg_latency = self._window_latency_ms / max(1, self._window_successes)
        rate_limited = self._rate_limited or self._window_failures > self._window_successes

        if rate_limited or error_ratio >= self.limits.error_threshold:
            self._healthy = 0
            # Multiplicative decrease: rate limiting must bite immediately.
            self._set_workers(
                self.limits.min_workers
                if rate_limited
                else max(self.limits.min_workers, self._workers - self.limits.shrink_step)
            )
        elif avg_latency > self.limits.latency_target_ms:
            self._healthy = 0
            self._set_workers(max(self.limits.min_workers, self._workers - self.limits.shrink_step))
        else:
            self._healthy += 1
            if self._healthy >= self.limits.scale_up_after:
                self._healthy = 0
                self._set_workers(min(self.limits.max_workers, self._workers + 1))

        self._window_successes = 0
        self._window_failures = 0
        self._window_latency_ms = 0.0
        # The flag survives this window so callers can react to it
        # (a rate limit is not something to forget on the next item).
        self._rate_limited = rate_limited
        return self._workers

    def _set_workers(self, target: int) -> None:
        if target == self._workers:
            return
        if target > self._workers:
            self.stats.scaled_up += 1
        else:
            self.stats.scaled_down += 1
        self._workers = target
        if self.metrics is not None:
            self.metrics.gauge("workers", self._workers)

    # --- execution ----------------------------------------------------------
    async def run_one(
        self,
        operation: TaskFactory,
        *,
        policy: RetryPolicy | None = None,
        window: int = 25,
    ) -> Any:
        """Run one item with retries, then update the controller state.

        ``evaluate`` is called every ``window`` items so the concurrency level
        follows the conditions the pool actually observes.
        """
        import time

        started = time.perf_counter()
        self.stats.started += 1
        try:
            result = await retry_async(
                operation,
                policy,
                on_retry=lambda kind, _attempt, _delay: self._on_retry(kind),
            )
        except Exception as exc:  # noqa: BLE001 - one item must not kill the pool
            self.record_failure(ErrorKind.PERMANENT)
            self.stats.worker_errors.append(str(exc))
            if len(self.stats.worker_errors) > 100:  # bounded diagnostics
                del self.stats.worker_errors[:-100]
            self.evaluate()
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.record_success(elapsed_ms)
        if self.metrics is not None:
            self.metrics.observe("request_ms", elapsed_ms)
        if self.stats.completed % window == 0:
            self.evaluate()
        return result

    def _on_retry(self, kind: ErrorKind) -> None:
        self.record_retry()
        self.record_failure(kind)
        if kind is ErrorKind.RATE_LIMIT:
            self.evaluate()

    async def map(
        self,
        items: AsyncIterator[Any] | Any,
        operation: TaskFactory,
        *,
        policy: RetryPolicy | None = None,
        window: int = 25,
        inflight_slack: int = 4,
    ) -> list[Any]:
        """Consume ``items`` with a bounded number of in-flight operations.

        The set of in-flight tasks never exceeds ``workers + slack``, which is
        what keeps task creation under control: items are pulled lazily and no
        unbounded task list is ever materialised.

        Returns:
            The successful results, in completion order.
        """
        iterator = items.__aiter__() if hasattr(items, "__aiter__") else _aiter(items)
        results: list[Any] = []
        in_flight: set[asyncio.Task[Any]] = set()
        budget = self._workers + max(0, inflight_slack)

        def reap(task: asyncio.Task[Any]) -> None:
            """Collect one finished task, dropping the ones that failed."""
            if task.cancelled():
                return
            error = task.exception()
            if error is None:
                results.append(task.result())
            # A failing item was already recorded by run_one; it must not
            # abort the batch.

        try:
            while True:
                while len(in_flight) < budget:
                    try:
                        item = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    task = asyncio.create_task(
                        self.run_one(
                            lambda bound=item: operation(bound),
                            policy=policy,
                            window=window,
                        ),
                        name="xtg-pool-worker",
                    )
                    in_flight.add(task)
                    # `reap` collects the value, `discard` keeps the
                    # in-flight set (and therefore the budget) accurate.
                    task.add_done_callback(reap)
                    task.add_done_callback(in_flight.discard)
                if not in_flight:
                    break
                await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in list(in_flight):
                task.cancel()
            if in_flight:
                with contextlib.suppress(Exception):
                    await asyncio.gather(*in_flight, return_exceptions=True)
            with contextlib.suppress(Exception):
                await iterator.aclose()  # type: ignore[attr-defined]
        return results


async def _aiter(items: Any) -> AsyncIterator[Any]:
    for item in items or ():
        yield item
