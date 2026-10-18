"""Tests for the proxy manager and availability checker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from xtg.proxy.checker import check_proxy
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyEntry


def entry(name: str, port: int = 1080) -> ProxyEntry:
    return ProxyEntry(name=name, scheme="socks5", host="127.0.0.1", port=port)


def test_round_robin_selection() -> None:
    manager = ProxyManager([entry("P1"), entry("P2"), entry("P3")])
    assert [manager.next_proxy().name for _ in range(4)] == ["P1", "P2", "P3", "P1"]


def test_disabled_manager_returns_none() -> None:
    assert ProxyManager([entry("P1")], enabled=False).next_proxy() is None
    assert ProxyManager([]).next_proxy() is None


def test_dead_proxies_are_skipped() -> None:
    manager = ProxyManager([entry("P1"), entry("P2")])
    manager.mark_failure(entry("P1"))
    assert manager.next_proxy().name == "P2"
    assert [item.name for item in manager.usable] == ["P2"]


def test_all_dead_raises_proxy_error() -> None:
    from xtg.core.errors import ProxyError

    manager = ProxyManager([entry("P1")])
    manager.mark_failure(entry("P1"))
    with pytest.raises(ProxyError):
        manager.next_proxy()


def test_get_unknown_proxy_raises() -> None:
    from xtg.core.errors import ProxyError

    with pytest.raises(ProxyError):
        ProxyManager([entry("P1")]).get("P9")


def test_mark_success_clears_dead_state() -> None:
    manager = ProxyManager([entry("P1")])
    manager.mark_failure(entry("P1"))
    manager.mark_success(entry("P1"))
    assert manager.health["P1"] == "alive"


def test_stats_counts_invalid_entries(proxy_file: Path) -> None:
    from xtg.proxy.loader import load_proxy_list

    manager = ProxyManager.from_result(load_proxy_list(proxy_file))
    stats = manager.stats()
    assert stats.valid == 4
    assert stats.invalid == 0
    assert stats.total == 4


def test_apply_check_results() -> None:
    from xtg.proxy.checker import ProxyCheckResult

    manager = ProxyManager([entry("P1"), entry("P2")])
    manager.apply_check_results(
        [
            ProxyCheckResult(entry=entry("P1"), status="alive", latency_ms=1.0),
            ProxyCheckResult(entry=entry("P2"), status="dead", error="refused"),
        ]
    )
    assert manager.health == {"P1": "alive", "P2": "dead"}
    assert manager.stats().alive == 1


def test_check_dead_port_is_reported() -> None:
    """Port 1 is closed on every sane host: the checker must return dead."""
    result = asyncio.run(check_proxy(entry("P1", port=1), timeout=0.5))
    assert result.status in {"dead", "error"}
    assert result.alive is False


def test_check_many_preserves_order() -> None:
    entries = [entry("P1", port=1), entry("P2", port=2)]
    results = asyncio.run(ProxyManager(entries).check(timeout=0.5))
    assert [result.entry.name for result in results] == ["P1", "P2"]


def test_from_settings_disabled(settings) -> None:
    assert ProxyManager.from_settings(settings).enabled is False
    assert len(ProxyManager.from_settings(settings)) == 0


def test_remove_range(tmp_path: Path) -> None:
    from xtg.proxy.core.manager import GlobalProxyManager

    manager = GlobalProxyManager(state_dir=tmp_path / "proxy", pool={})
    for pid, uri in [
        ("P1", "socks5://1.1.1.1:1080"),
        ("P10", "socks5://1.1.1.1:1081"),
        ("P100", "socks5://1.1.1.1:1082"),
        ("P456", "socks5://1.1.1.1:1083"),
        ("P999", "socks5://1.1.1.1:1084"),
    ]:
        manager.add_proxy(pid, uri)

    removed = manager.remove_range("P1", "P456")
    assert removed == ["P1", "P10", "P100", "P456"]
    assert set(manager.get_pool()) == {"P999"}

    # An empty range is a no-op, not an error.
    assert manager.remove_range("P999", "P1") == []


def test_sources_manager_add_list_remove(tmp_path: Path) -> None:
    from xtg.proxy.sources.manager import SourcesManager

    mgr = SourcesManager(tmp_path / "proxy")
    assert mgr.sources == {}
    assert mgr.names() == []

    mgr.add("a", "https://x.com")
    mgr.add("b", "https://y.com")
    assert mgr.sources == {"a": "https://x.com", "b": "https://y.com"}
    assert mgr.names() == ["a", "b"]
    assert mgr.get("a") == "https://x.com"
    assert mgr.get("missing") is None

    # Overwriting an existing name is allowed.
    mgr.add("a", "https://z.com")
    assert mgr.get("a") == "https://z.com"

    # Persistence: a second manager reading the same dir sees the sources.
    reloaded = SourcesManager(tmp_path / "proxy")
    assert reloaded.sources == {"a": "https://z.com", "b": "https://y.com"}

    assert mgr.remove("a") is True
    assert mgr.get("a") is None
    assert mgr.remove("a") is False  # already gone

    assert mgr.clear() == 1
    assert mgr.sources == {}


def test_sources_manager_rejects_empty_values(tmp_path: Path) -> None:
    from xtg.core.errors import ProxyError
    from xtg.proxy.sources.manager import SourcesManager

    mgr = SourcesManager(tmp_path / "proxy")
    with pytest.raises(ProxyError):
        mgr.add("", "https://x.com")
    with pytest.raises(ProxyError):
        mgr.add("x", "")
    assert mgr.sources == {}


def test_sources_manager_survives_corrupt_file(tmp_path: Path) -> None:
    from xtg.proxy.sources.manager import SourcesManager

    state_dir = tmp_path / "proxy"
    state_dir.mkdir()
    (state_dir / SourcesManager.SOURCES_FILE_NAME).write_text("not json")
    mgr = SourcesManager(state_dir)
    assert mgr.sources == {}
    # And it can be written again after the corruption.
    mgr.add("a", "https://x.com")
    assert SourcesManager(state_dir).sources == {"a": "https://x.com"}
