"""Tests for proxy scoring, cooldown and failover selection."""

from __future__ import annotations

import time

import pytest
from xtg.proxy.checker import ProxyCheckResult
from xtg.proxy.manager import (
    ProxyHealth,
    ProxyManager,
    ProxyState,
    ProxyStats,
)
from xtg.proxy.parser import ProxyEntry


def entry(name: str, scheme: str = "mtproto") -> ProxyEntry:
    return ProxyEntry(name=name, scheme=scheme, host="10.0.0.1", port=443, secret="a" * 32)


# --- scoring -----------------------------------------------------------------


def test_untried_proxy_is_neither_trusted_nor_distrusted() -> None:
    assert ProxyHealth().score == 0.5


def test_more_evidence_beats_a_lucky_start() -> None:
    lucky = ProxyHealth(successes=1)
    steady = ProxyHealth(successes=900, failures=100)

    assert lucky.score < steady.score
    assert steady.score > 0.8


def test_a_failed_proxy_scores_low() -> None:
    health = ProxyHealth(successes=1, failures=99)

    assert health.score < 0.2


def test_latency_lowers_the_score() -> None:
    fast = ProxyHealth(successes=100, latency_ms=10.0)
    slow = ProxyHealth(successes=100, latency_ms=1000.0)

    assert slow.score < fast.score


def test_score_is_bounded() -> None:
    health = ProxyHealth(successes=10_000, latency_ms=0.001)
    assert 0.0 <= health.score <= 1.0


def test_latency_is_an_average() -> None:
    health = ProxyHealth()
    health.record_success(10.0)
    health.record_success(20.0)
    health.record_success(30.0)

    assert 10.0 < health.latency_ms < 30.0


def test_success_clears_the_cooldown() -> None:
    health = ProxyHealth()
    health.record_failure()

    assert health.on_cooldown is True
    health.record_success(1.0)
    assert health.on_cooldown is False
    assert health.consecutive_failures == 0


def test_cooldown_doubles_per_failure() -> None:
    first = ProxyHealth()
    second = ProxyHealth()
    third = ProxyHealth()

    first.record_failure()
    for _ in range(2):
        second.record_failure()
    for _ in range(3):
        third.record_failure()

    assert first.cooldown_until > time.monotonic()
    assert second.cooldown_until > first.cooldown_until
    assert third.cooldown_until > second.cooldown_until


def test_cooldown_is_capped() -> None:
    health = ProxyHealth()
    for _ in range(20):
        health.record_failure()

    assert health.cooldown_until - time.monotonic() <= 300.0 + 1.0


def test_states_are_distinguished() -> None:
    assert ProxyHealth().state is ProxyState.UNKNOWN
    assert ProxyHealth(successes=10).state is ProxyState.HEALTHY
    assert ProxyHealth(successes=1, failures=9).state is ProxyState.DEGRADED

    health = ProxyHealth()
    health.record_failure()
    assert health.state is ProxyState.COOLDOWN


# --- selection ----------------------------------------------------------------


def test_next_proxy_returns_none_when_disabled() -> None:
    assert ProxyManager([entry("P1")], enabled=False).next_proxy() is None


def test_next_proxy_returns_none_when_empty() -> None:
    assert ProxyManager().next_proxy() is None


def test_untried_proxies_rotate() -> None:
    manager = ProxyManager([entry("P1"), entry("P2"), entry("P3")])

    assert [manager.next_proxy().name for _ in range(4)] == ["P1", "P2", "P3", "P1"]


def test_a_failed_proxy_is_skipped() -> None:
    manager = ProxyManager([entry("P1"), entry("P2")])
    manager.mark_failure(entry("P1"))

    assert manager.next_proxy().name == "P2"
    assert [item.name for item in manager.usable] == ["P2"]


def test_every_proxy_dead_raises() -> None:
    from xtg.core.errors import ProxyError

    manager = ProxyManager([entry("P1")])
    manager.mark_failure(entry("P1"))

    with pytest.raises(ProxyError):
        manager.next_proxy()


def test_a_healthy_proxy_beats_a_degraded_one() -> None:
    manager = ProxyManager([entry("weak"), entry("good")])
    weak, good = manager._entries

    for _ in range(10):
        manager.mark_success(good, latency_ms=1.0)
    for _ in range(10):
        manager.mark_failure(weak)
        manager.mark_success(weak, latency_ms=1.0)  # keeps it off cooldown

    assert manager.next_proxy().name == "good"


def test_cooled_down_proxies_are_recovered_from_in_order() -> None:
    manager = ProxyManager([entry("P1"), entry("P2")])
    first, second = manager._entries

    # A cooldown keeps the entries usable, so selection must still work
    # rather than raising.
    now = time.monotonic()
    manager._health_of(first).cooldown_until = now + 30.0
    manager._health_of(second).cooldown_until = now + 60.0

    # The one whose cooldown expires first wins.
    assert manager.next_proxy().name == "P1"


def test_failover_is_counted() -> None:
    manager = ProxyManager([entry("P1"), entry("P2")])
    first, second = manager._entries

    # P1 only cools down, P2 stays eligible: skipping it is a failover.
    manager.mark_failure(first)
    manager.mark_success(second, latency_ms=1.0)

    assert manager.next_proxy().name == "P2"
    assert manager.stats().failovers >= 1


def test_scoring_survives_a_long_run() -> None:
    manager = ProxyManager([entry(f"P{index}") for index in range(5)])
    weak = manager._entries[0]

    for index in range(100):
        proxy = manager.next_proxy()
        assert proxy is not None
        if proxy is weak and index % 2 == 0:
            manager.mark_failure(proxy)
        else:
            manager.mark_success(proxy, latency_ms=float(index % 50))

    ranking = manager.ranking()
    assert len(ranking) == 5
    scores = [score for _, score in ranking]
    assert scores == sorted(scores, reverse=True)
    # The proxy that kept failing ends up last, not first.
    assert manager.score_of(weak) == min(scores)


def test_states_expose_the_whole_list() -> None:
    manager = ProxyManager([entry("P1"), entry("P2")])
    manager.mark_failure(entry("P1"))

    states = manager.states()

    assert states["P1"] in {ProxyState.COOLDOWN.value, ProxyState.EXCLUDED.value}
    assert states["P2"] == ProxyState.UNKNOWN.value


# --- bookkeeping ---------------------------------------------------------------


def test_mark_success_returns_the_proxy_to_the_pool() -> None:
    manager = ProxyManager([entry("P1")])
    proxy = manager._entries[0]

    manager.mark_failure(proxy)
    assert proxy.name in manager.health

    # Fast forward past the cooldown.
    manager._health[proxy.name].cooldown_until = time.monotonic() - 1.0
    manager.mark_success(proxy)

    assert [item.name for item in manager.usable] == ["P1"]


def test_stats_carry_the_counters() -> None:
    manager = ProxyManager([entry("P1")])
    proxy = manager._entries[0]

    manager.mark_success(proxy, latency_ms=2.0)
    manager.mark_failure(proxy)

    stats = manager.stats()
    assert isinstance(stats, ProxyStats)
    assert stats.successes["P1"] == 1
    assert stats.failures["P1"] == 1
    assert stats.alive + stats.dead >= 1


def test_reset_health_forgets_everything() -> None:
    manager = ProxyManager([entry("P1")])
    proxy = manager._entries[0]

    manager.mark_success(proxy)
    manager.reset_health()

    assert manager.stats().successes == {}
    assert manager.score_of(proxy) == 0.5


def test_get_unknown_proxy_raises() -> None:
    from xtg.core.errors import ProxyError

    manager = ProxyManager([entry("P1")])

    with pytest.raises(ProxyError):
        manager.get("missing")


def test_apply_check_results_sets_the_state() -> None:
    manager = ProxyManager([entry("P1"), entry("P2")])
    first, second = manager._entries

    manager.apply_check_results(
        [
            ProxyCheckResult(entry=first, status="alive", latency_ms=1.0),
            ProxyCheckResult(entry=second, status="dead"),
        ]
    )

    assert manager.state_of(first) is ProxyState.HEALTHY
    # A failed check only takes the entry out of the rotation for a while;
    # it is not dead forever.
    assert manager.state_of(second) is ProxyState.EXCLUDED
    assert [proxy.name for proxy in manager.usable] == ["P1"]


def test_excluded_entries_come_back_after_the_cooldown() -> None:
    manager = ProxyManager([entry("P1")])
    only = manager._entries[0]

    manager.mark_failure(only)
    assert [proxy.name for proxy in manager.usable] == []

    manager._health_of(only).cooldown_until = 0.0
    assert [proxy.name for proxy in manager.usable] == ["P1"]
    assert manager.next_proxy().name == "P1"
