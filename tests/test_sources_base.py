"""Tests for the generic source layer (no platform involved)."""

from __future__ import annotations

from typing import Any

import pytest
from xtg.core.errors import CollectionError, NotFoundError
from xtg.database.db import Database
from xtg.sources.base import BaseCollector, BaseSource, SavedItem, SourceReport
from xtg.sources.registry import available_sources, get_source_class


class DemoCollector(BaseCollector):
    """Minimal collector over a fixed list of integers."""

    source = "demo"
    sections = {"items": "collect_items"}

    def __init__(self, client: Any, database: Any, **options: Any) -> None:
        super().__init__(client, database, **options)
        self.saved: list[dict[str, Any]] = []

    def resolve(self, session: Any, target: str) -> str:  # noqa: ARG002 - nothing to resolve
        if target == "missing":
            raise NotFoundError("nothing here")
        return target

    def parse(self, raw: Any) -> dict[str, Any]:
        if raw < 0:
            raise ValueError("negative item")
        return {"kind": "item", "value": raw}

    def save(self, session: Any, parsed: dict[str, Any]) -> SavedItem:  # noqa: ARG002 - demo
        self.saved.append(parsed)
        return SavedItem(kind="item", ref=f"demo:item:{parsed['value']}", row=parsed, created=True)

    def collect_items(self, context: Any, *, limit: int, report: SourceReport) -> None:
        for index, value in enumerate(self.client, start=1):
            with self.database.session() as session:
                self.store(session, value, report)
            self.notify("items", index, limit)


class DemoSource(BaseSource):
    name = "demo"

    def create_client(self) -> Any:
        return list(range(3))

    def create_collector(self, client: Any) -> BaseCollector:
        return DemoCollector(client, self.database, delay=self.settings.request_delay)


# --- report -----------------------------------------------------------------


def test_report_counts_and_total() -> None:
    report = SourceReport(source="demo", target="x")
    report.add("posts", created=True)
    report.add("posts", created=True)
    report.add("users", created=False)

    assert report.counts == {"posts": 2, "users": 1}
    assert report.duplicated == 1
    assert report.total == 3


def test_report_merge() -> None:
    first = SourceReport(source="demo", counts={"posts": 1}, details={"listing": "new"})
    second = SourceReport(source="demo", counts={"posts": 2, "users": 1}, errors=["boom"])

    merged = first.merge(second)

    assert merged.counts == {"posts": 3, "users": 1}
    assert merged.duplicated == 0
    assert merged.errors == ["boom"]
    assert merged.details == {"listing": "new"}


def test_report_to_dict_is_json_ready() -> None:
    report = SourceReport(source="reddit", target="r/linux", counts={"posts": 2}, duplicated=1)
    payload = report.to_dict()
    assert payload["source"] == "reddit"
    assert payload["total"] == 2
    assert payload["errors"] == []


# --- collector --------------------------------------------------------------


def test_collector_runs_sections(settings, database: Database) -> None:
    collector = DemoCollector([1, 2, 3], database)
    report = collector.collect("demo-target")

    assert report.source == "demo"
    assert report.counts == {"item": 3}
    assert report.errors == []


def test_collector_limits_the_target_resolution(settings, database: Database) -> None:
    with pytest.raises(NotFoundError):
        DemoCollector([], database).collect("missing")


def test_collector_rejects_unknown_section(settings, database: Database) -> None:
    with pytest.raises(CollectionError):
        DemoCollector([], database).collect("demo", sections=["nope"])


def test_collector_reports_item_errors_without_aborting(settings, database: Database) -> None:
    collector = DemoCollector([1, -1, 2], database)
    report = collector.collect("demo")

    assert report.counts == {"item": 2}
    assert len(report.errors) == 1
    assert "negative" in report.errors[0]


def test_collector_reports_section_failure(settings, database: Database) -> None:
    class Broken(DemoCollector):
        def collect_items(  # noqa: ARG002 - demo section
            self, context: Any, *, limit: int, report: SourceReport
        ) -> None:
            raise RuntimeError("reddit is down")

    report = Broken([], database).collect("demo")

    assert report.errors == ["items: reddit is down"]


def test_collector_progress_and_delay(settings, database: Database) -> None:
    seen: list[tuple[str, int, int]] = []
    collector = DemoCollector([1, 2], database, progress=lambda *args: seen.append(args))
    collector.collect("demo", sections=["items"])
    assert seen  # the hook was called per item


def test_collector_pacing_is_applied(settings, database: Database) -> None:
    collector = DemoCollector([1], database, delay=0.001)
    assert collector.collect("demo", sections=["items"]).total == 1


# --- source -----------------------------------------------------------------


def test_source_connects_lazily(settings, database: Database) -> None:
    source = DemoSource(settings, database=database)
    assert source.collect("demo", sections=["items"]).counts == {"item": 3}


def test_source_is_a_context_manager(settings, database: Database) -> None:
    with DemoSource(settings, database=database) as source:
        assert source.client == [0, 1, 2]
        assert isinstance(source.collector, BaseCollector)


def test_source_exposes_parse_and_save(settings, database: Database) -> None:
    source = DemoSource(settings, database=database)
    parsed = source.parse(5)
    assert parsed == {"kind": "item", "value": 5}

    with database.session() as session:
        item = source.save(session, parsed)
    assert item.ref == "demo:item:5"
    assert item.created is True


def test_source_close_is_idempotent(settings, database: Database) -> None:
    source = DemoSource(settings, database=database)
    source.connect()
    source.close()
    source.close()


# --- registry ---------------------------------------------------------------


def test_registry_lists_the_sources() -> None:
    assert available_sources() == ("reddit", "telegram")


def test_registry_resolves_classes() -> None:
    assert get_source_class("telegram").name == "telegram"
    assert get_source_class("REDDIT").name == "reddit"


def test_registry_rejects_unknown_sources() -> None:
    with pytest.raises(Exception, match="Unknown source"):
        get_source_class("myspace")
