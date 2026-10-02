"""Deterministic performance scenarios A-G.

Every scenario runs entirely offline against :mod:`xtg.bench.fakes` and a
throwaway SQLite file, so a run is reproducible and a regression shows up as a
number instead of a hunch. Each scenario reports a small set of metrics that are
comparable across runs:

======================  ==================================================
``throughput``          items per second (the headline number)
``latency_ms``          wall time of the whole scenario
``peak_rss_mb``         process RSS at the end of the scenario
``detail``              per-scenario extras (batch counts, retries, ...)
======================  ==================================================

Scenarios A and D carry the before/after comparison: they run the legacy
per-row ORM path and the batched path over identical input, so the speedup is
measured rather than estimated.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xtg.bench.fakes import (
    BenchClient,
    BenchRedditClient,
    bench_messages,
    bench_participants,
)
from xtg.core.config import Settings
from xtg.core.metrics import cpu_seconds, rss_mb
from xtg.database import queries
from xtg.database.db import Database
from xtg.database.writer import BatchWriter
from xtg.telegram import serializers
from xtg.telegram.collector import TelegramCollector

#: Rows per scenario. Small enough to run in CI, large enough to be meaningful.
DEFAULT_ROWS = 2000


@dataclass(slots=True)
class BenchResult:
    """One scenario's measurement."""

    scenario: str
    label: str
    throughput: float
    latency_ms: float
    peak_rss_mb: float
    units: str = "rows/s"
    cpu_seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "label": self.label,
            "throughput": round(self.throughput, 1),
            "latency_ms": round(self.latency_ms, 1),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "units": self.units,
            "detail": self.detail,
        }


class _Timer:
    """Time a block and sample RSS on both sides."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.cpu_started = cpu_seconds()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def cpu(self) -> float:
        return cpu_seconds() - self.cpu_started

    def sample(self) -> float:
        return rss_mb()


def _temp_database() -> Database:
    """A throwaway on-disk database in a temp directory."""
    handle, path = tempfile.mkstemp(prefix="xtg-bench-", suffix=".db")
    os.close(handle)
    settings = Settings(database_path=Path(path))
    database = Database(settings)
    database.create_all()
    return database


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


# --- A: Telegram message extraction -----------------------------------------


async def scenario_a(rows: int = DEFAULT_ROWS, *, batch_size: int = 500) -> BenchResult:
    """Telegram messages end to end: fake API -> queue -> batched writes.

    Drives the real :meth:`TelegramCollector.collect_messages`, so the producer
    and consumer halves, the bounded queue and the batch writer are all included
    in the number - this is the figure that predicts a real collection run.
    """
    database = _temp_database()
    messages = bench_messages(rows)
    client = BenchClient(messages)
    collector = TelegramCollector(client, database, batch_size=batch_size)

    timer = _Timer()
    report = await collector.collect_messages("@bench", limit=rows)
    elapsed = timer.elapsed
    database.dispose()

    return BenchResult(
        scenario="A",
        label="telegram messages (end to end)",
        throughput=rows / elapsed,
        latency_ms=elapsed * 1000,
        peak_rss_mb=timer.sample(),
        cpu_seconds=timer.cpu,
        detail={
            "rows": rows,
            "batch_size": batch_size,
            "stored": report.messages,
            "batches": report.batches,
            "dropped": report.backpressure_drops,
            "cursor": report.cursor,
            "errors": len(report.errors),
        },
    )


# --- B: Telegram member collection ------------------------------------------


async def scenario_b(rows: int = DEFAULT_ROWS, *, batch_size: int = 500) -> BenchResult:
    """Telegram member collection, batched instead of one transaction per member."""
    database = _temp_database()
    participants = bench_participants(rows)
    client = BenchClient([], participants=participants)
    collector = TelegramCollector(client, database, batch_size=batch_size)

    timer = _Timer()
    report = await collector.collect_members("@bench", limit=rows)
    elapsed = timer.elapsed
    database.dispose()

    return BenchResult(
        scenario="B",
        label="telegram members (batched)",
        throughput=rows / elapsed,
        latency_ms=elapsed * 1000,
        peak_rss_mb=timer.sample(),
        cpu_seconds=timer.cpu,
        detail={
            "rows": rows,
            "batch_size": batch_size,
            "stored": report.members,
            "batches": report.batches,
            "errors": len(report.errors),
        },
    )


# --- C: Reddit extraction ----------------------------------------------------


def scenario_c(rows: int = DEFAULT_ROWS, *, batch_size: int = 200) -> BenchResult:
    """Reddit collection: the real collector against the fake PRAW client.

    This walks the whole production path (parse -> author -> post -> graph
    edges -> pivot entities) so the number reflects what a ``xtg reddit
    collect`` run actually costs.
    """
    from xtg.sources.reddit.collector import RedditCollector

    database = _temp_database()
    client = BenchRedditClient(posts=rows)
    collector = RedditCollector(client, database, batch_size=batch_size)

    timer = _Timer()
    report = collector.collect("bench", sections=("subreddit", "posts"), limit=rows)
    elapsed = timer.elapsed
    database.dispose()

    return BenchResult(
        scenario="C",
        label="reddit posts (real collector)",
        throughput=rows / elapsed,
        latency_ms=elapsed * 1000,
        peak_rss_mb=timer.sample(),
        cpu_seconds=timer.cpu,
        detail={
            "rows": rows,
            "batch_size": batch_size,
            "posts": report.counts.get("posts", 0),
            "duplicated": report.duplicated,
            "errors": len(report.errors),
            "api_requests": client.requests,
        },
    )


# --- D: write path in isolation (before/after) -------------------------------


async def scenario_d(rows: int = DEFAULT_ROWS, *, batch_size: int = 500) -> BenchResult:
    """Legacy per-row writes vs bulk upserts on identical rows.

    This is the clearest measure of what batching bought: same data, same
    database, two write strategies.
    """
    messages = bench_messages(rows)
    payload = [
        {
            **serializers.message_fields(message),
            "raw_data": {"id": message.id},
            "has_media": bool(serializers.media_entries(message)),
        }
        for message in messages
    ]
    senders = [
        serializers.user_fields(message.sender)
        for message in messages
        if message.sender is not None
    ]
    attachments = [
        {**serializers.media_entries(message)[0], "telegram_message_id": message.id}
        for message in messages
        if serializers.media_entries(message)
    ]

    # --- legacy: one session, ORM upsert per row --------------------------
    from xtg.telegram.collector import store_chat

    legacy_db = _temp_database()
    with legacy_db.session() as session:
        chat = store_chat(session, telegram_id=client_chat_id(), title="Bench")
    legacy_timer = _Timer()
    with legacy_db.session() as session:
        for row, message in zip(payload, messages, strict=True):
            sender = (
                queries.upsert_user(session, **serializers.user_fields(message.sender))
                if message.sender is not None
                else None
            )
            fields = {key: value for key, value in row.items() if key != "telegram_message_id"}
            stored = queries.upsert_message(
                session, chat, row["telegram_message_id"], sender=sender, **fields
            )
            if row["telegram_message_id"] % 4 == 0:
                queries.add_media(session, stored, "photo", file_name="p.jpg", file_size=1024)
    legacy_elapsed = legacy_timer.elapsed
    legacy_db.dispose()

    # --- batched: bulk upserts --------------------------------------------
    bulk_db = _temp_database()
    with bulk_db.session() as session:
        chat = store_chat(session, telegram_id=client_chat_id(), title="Bench")
    bulk_timer = _Timer()
    writer = BatchWriter(bulk_db, batch_size=batch_size, source="telegram")
    writer.set_target(chat.id, target="@bench")
    for row, message in zip(payload, messages, strict=True):
        sender = serializers.user_fields(message.sender) if message.sender is not None else None
        writer.add_message(row, sender=sender, media=serializers.media_entries(message))
    writer.close()
    bulk_elapsed = bulk_timer.elapsed
    bulk_db.dispose()

    return BenchResult(
        scenario="D",
        label="write path: legacy vs bulk",
        throughput=rows / bulk_elapsed,
        latency_ms=bulk_elapsed * 1000,
        peak_rss_mb=rss_mb(),
        detail={
            "legacy_ms": round(legacy_elapsed * 1000, 1),
            "bulk_ms": round(bulk_elapsed * 1000, 1),
            "speedup": round(legacy_elapsed / bulk_elapsed, 2),
            "rows": rows,
            "batch_size": batch_size,
            "senders": len(senders),
            "attachments": len(attachments),
        },
    )


# --- E: serialization and memory --------------------------------------------


def scenario_e(rows: int = DEFAULT_ROWS) -> BenchResult:
    """Serializer throughput and the memory footprint of the parsed payload."""
    messages = bench_messages(rows)
    timer = _Timer()
    for message in messages:
        serializers.message_fields(message)
        serializers.media_entries(message)
        if message.sender is not None:
            serializers.user_fields(message.sender)
    elapsed = timer.elapsed
    payload = [serializers.message_fields(message) for message in messages]
    size_kb = sum(len(str(row)) for row in payload) / 1024

    return BenchResult(
        scenario="E",
        label="serialization throughput",
        throughput=rows / elapsed,
        latency_ms=elapsed * 1000,
        peak_rss_mb=rss_mb(),
        cpu_seconds=timer.cpu,
        detail={
            "rows": rows,
            "payload_kb": round(size_kb, 1),
            "rows_per_ms": round(rows / elapsed / 1000, 1),
        },
    )


# --- F: concurrency and backpressure -----------------------------------------


async def scenario_f(rows: int = DEFAULT_ROWS, *, workers: int = 8) -> BenchResult:
    """Sequential vs adaptive-pool throughput with a bounded queue.

    Simulates per-item API latency so the difference between one request at a
    time and a small pool is visible without any network.
    """
    from xtg.pipeline import AdaptivePool, PoolLimits

    async def _one(index: int) -> int:
        await asyncio.sleep(0.0005)
        return index

    items = list(range(rows))

    sequential_timer = _Timer()
    for index in items:
        await _one(index)
    sequential = sequential_timer.elapsed

    pool = AdaptivePool(limits=PoolLimits(max_workers=workers, min_workers=1))
    pool_timer = _Timer()
    await pool.map(items, _one)
    pooled = pool_timer.elapsed

    return BenchResult(
        scenario="F",
        label=f"concurrency: 1 vs {workers} workers",
        throughput=rows / pooled,
        latency_ms=pooled * 1000,
        peak_rss_mb=rss_mb(),
        detail={
            "sequential_ms": round(sequential * 1000, 1),
            "pooled_ms": round(pooled * 1000, 1),
            "speedup": round(sequential / pooled, 2),
            "workers": pool.workers,
            "workers_grown": pool.workers > 1,
        },
    )


# --- G: proxy failover -------------------------------------------------------


async def scenario_g(rows: int = DEFAULT_ROWS) -> BenchResult:
    """Failover latency: how fast the manager escapes a run of dead proxies."""
    from xtg.proxy.manager import ProxyManager
    from xtg.proxy.parser import ProxyEntry

    proxies = max(4, min(rows, 32))
    dead = proxies - 1
    entries = [
        ProxyEntry(
            name=f"proxy{index}",
            scheme="mtproto",
            host=f"10.0.0.{index + 1}",
            port=443,
            secret=os.urandom(16).hex(),
        )
        for index in range(proxies)
    ]
    manager = ProxyManager(entries, enabled=True)
    for entry in entries[:dead]:
        manager.mark_failure(entry)

    timer = _Timer()
    hops = 0
    chosen = manager.next_proxy()
    while chosen is not None and chosen.name in {entry.name for entry in entries[:dead]}:
        hops += 1
        if hops > proxies * 4:
            break
        manager.mark_failure(chosen)
        chosen = manager.next_proxy()
    elapsed = timer.elapsed

    return BenchResult(
        scenario="G",
        label=f"proxy failover ({dead}/{proxies} dead)",
        throughput=float(proxies - dead) if elapsed else 0.0,
        latency_ms=elapsed * 1000,
        peak_rss_mb=rss_mb(),
        units="hops/s",
        detail={
            "proxies": proxies,
            "dead": dead,
            "hops_to_healthy": hops,
            "selected": chosen.name if chosen else None,
            "within_one_ms": elapsed < 0.001,
        },
    )


# --- harness -----------------------------------------------------------------


SCENARIOS: dict[str, tuple[str, Callable[..., Awaitable[BenchResult] | BenchResult]]] = {
    "A": ("telegram messages", scenario_a),
    "B": ("telegram members", scenario_b),
    "C": ("reddit posts", scenario_c),
    "D": ("write path", scenario_d),
    "E": ("serialization", scenario_e),
    "F": ("concurrency", scenario_f),
    "G": ("proxy failover", scenario_g),
}


async def run_all(rows: int = DEFAULT_ROWS) -> list[BenchResult]:
    """Run every scenario in order and return the results."""
    results: list[BenchResult] = []
    for key in "ABCDEFG":
        _, factory = SCENARIOS[key]
        results.append(await factory(rows))
    return results


def format_table(results: list[BenchResult]) -> str:
    """Render results as a fixed-width table."""
    header = f"{'ID':<3} {'scenario':<28} {'throughput':>13} {'latency ms':>11} {'rss mb':>8}"
    lines = [header, "-" * len(header)]
    for result in results:
        lines.append(
            f"{result.scenario:<3} {result.label:<28} "
            f"{result.throughput:>13,.1f} {result.latency_ms:>11,.1f} "
            f"{result.peak_rss_mb:>8,.1f}"
        )
    return "\n".join(lines)


# --- legacy write helpers (the "before" side of scenario D) ------------------


def client_chat_id() -> int:
    """The chat id both write strategies of scenario D store into."""
    return -1001234567890
