"""Tests for the pipeline primitives: bounded queue, adaptive pool, retries."""

from __future__ import annotations

import asyncio

import pytest
from xtg.core.metrics import Metrics
from xtg.core.retry import (
    ErrorKind,
    RetryPolicy,
    classify,
    retry_after_of,
    retry_async,
    retry_sync,
)
from xtg.pipeline import AdaptivePool, BoundedQueue, PoolLimits, QueueFull

# --- bounded queue -----------------------------------------------------------


def test_queue_blocks_the_producer_when_full() -> None:
    async def run() -> tuple[int, float]:
        queue: BoundedQueue[int] = BoundedQueue(2)
        await queue.put(1)
        await queue.put(2)
        # The third item cannot fit; the producer must wait for a consumer.
        started = asyncio.get_running_loop().time()
        put = asyncio.create_task(queue.put(3))
        await asyncio.sleep(0.01)
        waited = put.done() is False
        assert waited, "a full queue must not accept another item"
        await queue.get()
        await put
        return queue.depth, asyncio.get_running_loop().time() - started

    depth, _ = asyncio.run(run())
    assert depth == 2
    assert waited_or_not()


def waited_or_not() -> bool:
    return True


def test_queue_reports_backpressure() -> None:
    async def run() -> BoundedQueue[int]:
        queue: BoundedQueue[int] = BoundedQueue(1)
        await queue.put(1)
        put = asyncio.create_task(queue.put(2))
        await asyncio.sleep(0.01)
        await queue.get()
        await put
        return queue

    queue = asyncio.run(run())
    assert queue.stats.blocked_puts == 1
    assert queue.stats.blocked_seconds > 0


def test_queue_put_timeout_reports_failure_without_blocking() -> None:
    async def run() -> bool:
        queue: BoundedQueue[int] = BoundedQueue(1)
        await queue.put(1)
        return await queue.put(2, timeout=0.01)

    assert asyncio.run(run()) is False


def test_queue_raises_when_asked_to_never_wait() -> None:
    async def run() -> None:
        queue: BoundedQueue[int] = BoundedQueue(1)
        queue.put_nowait_or_raise(1)
        with pytest.raises(QueueFull):
            queue.put_nowait_or_raise(2)

    asyncio.run(run())


def test_queue_tracks_max_depth_and_saturation() -> None:
    async def run() -> BoundedQueue[int]:
        queue: BoundedQueue[int] = BoundedQueue(4, high_water=0.5)
        for index in range(3):
            queue.put_nowait_or_raise(index)
        return queue

    queue = asyncio.run(run())
    assert queue.stats.max_depth == 3
    assert queue.saturated is True
    assert queue.depth == 3


def test_queue_rejects_a_zero_capacity() -> None:
    with pytest.raises(ValueError):
        BoundedQueue(0)


# --- adaptive pool -----------------------------------------------------------


def test_pool_grows_under_success() -> None:
    async def run() -> AdaptivePool:
        pool = AdaptivePool(limits=PoolLimits(max_workers=8, min_workers=1, scale_up_after=1))
        for _ in range(20):
            pool.record_success(5.0)
            pool.evaluate()
        return pool

    pool = asyncio.run(run())
    assert pool.workers > 1


def test_pool_shrinks_under_failure() -> None:
    async def run() -> AdaptivePool:
        pool = AdaptivePool(limits=PoolLimits(max_workers=8, min_workers=1))
        for _ in range(20):
            pool.record_success(1.0)
            pool.evaluate()
        for _ in range(20):
            pool.record_failure(ErrorKind.TRANSIENT)
            pool.evaluate()
        return pool

    pool = asyncio.run(run())
    assert pool.workers < 8


def test_pool_never_drops_below_the_minimum() -> None:
    async def run() -> AdaptivePool:
        pool = AdaptivePool(limits=PoolLimits(max_workers=4, min_workers=2))
        for _ in range(50):
            pool.record_failure(ErrorKind.TRANSIENT)
            pool.evaluate()
        return pool

    pool = asyncio.run(run())
    assert pool.workers >= 2


def test_pool_limits_are_clamped_to_the_minimum() -> None:
    limits = PoolLimits(max_workers=1, min_workers=4).clamped()
    assert limits.max_workers >= limits.min_workers


def test_pool_maps_every_item() -> None:
    async def double(value: int) -> int:
        await asyncio.sleep(0)
        return value * 2

    async def run() -> list[int]:
        pool = AdaptivePool(limits=PoolLimits(max_workers=4, min_workers=1))
        return await pool.map(list(range(20)), double)

    assert sorted(asyncio.run(run())) == [value * 2 for value in range(20)]


def test_pool_reports_a_failing_item_without_killing_the_batch() -> None:
    async def sometimes_fail(value: int) -> int:
        await asyncio.sleep(0)
        if value == 3:
            raise ValueError("boom")
        return value

    async def run() -> list[int]:
        pool = AdaptivePool(limits=PoolLimits(max_workers=4, min_workers=1))
        return await pool.map(list(range(6)), sometimes_fail)

    assert sorted(asyncio.run(run())) == [0, 1, 2, 4, 5]


def test_pool_flags_rate_limiting() -> None:
    async def run() -> AdaptivePool:
        pool = AdaptivePool(limits=PoolLimits(max_workers=4, min_workers=1))
        for _ in range(6):
            pool.record_failure(ErrorKind.RATE_LIMIT)
            pool.evaluate()
        return pool

    assert asyncio.run(run()).rate_limited is True


# --- retry classification ----------------------------------------------------


def test_flood_wait_is_a_rate_limit() -> None:
    error = type("FloodWaitError", (Exception,), {})("wait")
    assert classify(error) is ErrorKind.RATE_LIMIT


def test_connection_errors_are_transient() -> None:
    error = type("ConnectionResetError", (ConnectionError,), {})("reset")
    assert classify(error) is ErrorKind.TRANSIENT


def test_value_errors_are_permanent() -> None:
    assert classify(ValueError("bad")) is ErrorKind.PERMANENT


def test_auth_errors_are_distinguished() -> None:
    error = type("AuthKeyUnregisteredError", (Exception,), {})("nope")
    assert classify(error) is ErrorKind.AUTH


def test_retry_after_is_read_from_the_error() -> None:
    error = type("FloodWaitError", (Exception,), {})("wait")
    error.seconds = 12  # type: ignore[attr-defined]
    assert retry_after_of(error) == 12.0


def test_server_delay_is_capped() -> None:
    error = type("FloodWaitError", (Exception,), {})("wait")
    error.seconds = 99_999  # type: ignore[attr-defined]
    assert RetryPolicy(max_server_delay=5.0).delay_for(0, server_delay=retry_after_of(error)) == 5.0


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=8.0, jitter=False)
    delays = [policy.delay_for(attempt) for attempt in range(5)]

    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_permanent_errors_are_not_retried() -> None:
    policy = RetryPolicy()
    assert policy.allows(0, ErrorKind.PERMANENT) is False
    assert policy.allows(0, ErrorKind.TRANSIENT) is True


def test_retry_async_retries_then_succeeds() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        return "ok"

    async def run() -> str:
        policy = RetryPolicy(base_delay=0.0, jitter=False)
        return await retry_async(flaky, policy)

    assert asyncio.run(run()) == "ok"
    assert calls["n"] == 3


def test_retry_async_gives_up_after_the_budget() -> None:
    async def always_fails() -> None:
        raise ConnectionError("reset")

    async def run() -> str:
        policy = RetryPolicy(attempts=2, base_delay=0.0, jitter=False)
        try:
            await retry_async(always_fails, policy)
        except ConnectionError:
            return "raised"
        return "no error"

    assert asyncio.run(run()) == "raised"


def test_retry_sync_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("slow")
        return "ok"

    policy = RetryPolicy(base_delay=0.0, jitter=False)
    assert retry_sync(flaky, policy) == "ok"


# --- metrics -----------------------------------------------------------------


def test_metrics_counters_and_rates() -> None:
    metrics = Metrics()
    for _ in range(10):
        metrics.incr("items")
    metrics.incr("items", 5)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["items"] == 15
    assert snapshot["rates"]["items"] > 0


def test_metrics_histogram_percentiles() -> None:
    metrics = Metrics()
    for value in range(1, 101):
        metrics.observe("latency", value)

    histogram = metrics.histogram("latency")
    assert histogram.count == 100
    # Nearest-rank on a 100 sample window.
    assert histogram.percentile(0.50) in (50, 51)
    assert histogram.percentile(0.95) in (95, 96)


def test_metrics_window_stays_bounded() -> None:
    metrics = Metrics()
    for value in range(5000):
        metrics.observe("latency", value)

    assert len(metrics.histogram("latency").recent()) <= 1024


def test_metrics_gauges_and_reset() -> None:
    metrics = Metrics()
    metrics.gauge("queue_depth", 12)
    assert metrics.snapshot()["gauges"]["queue_depth"] == 12

    metrics.reset()
    assert metrics.snapshot()["counters"] == {}


def test_metrics_merge_combines_counters() -> None:
    first, second = Metrics(), Metrics()
    first.incr("a", 2)
    second.incr("b", 3)

    merged = first.merge(second)
    assert merged.get("a") == 2
    assert merged.get("b") == 3
