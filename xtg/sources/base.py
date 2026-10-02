"""Generic source layer.

A *source* is an upstream platform the toolkit collects from (Telegram,
Reddit, ...). Every source exposes the same four verbs so the CLI, the export
writers and the future analysis commands never need to know which platform a
row came from:

``connect()``
    Build the platform client. Lazy: nothing connects until it is needed.
``parse()``
    Turn a raw platform object into a plain column mapping.
``save()``
    Persist one parsed item, reporting whether it was new.
``collect()``
    Run a whole section (or several) and return a :class:`SourceReport`.

The split between :class:`BaseSource` and :class:`BaseCollector` keeps platform
life cycle (``connect`` / ``close``) separate from the walk
(``collect`` / ``parse`` / ``save``):

* :class:`BaseSource` - one instance per CLI command. Owns the client, delegates
  everything else to a collector and is usable as a context manager.
* :class:`BaseCollector` - the shared engine: resolve the target, stream the
  sections, pace the API calls, count duplicates and collect per-item errors
  without aborting the run.

Both are synchronous on purpose: PRAW is blocking, and the CLI is a synchronous
program. Sources whose client is asynchronous (Telethon) drive their own engine
inside :meth:`BaseSource.collect` - see :mod:`xtg.sources.telegram`.

Adding a source therefore means: create ``xtg/sources/<name>/`` with a
:class:`BaseCollector` subclass, register it in
:mod:`xtg.sources.registry` and (optionally) add a CLI command.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from sqlalchemy.orm import Session

from xtg.core.config import Settings, get_settings
from xtg.core.errors import CollectionError, NotFoundError, XTGError
from xtg.core.logger import logger
from xtg.database.db import Database

ProgressHook = Callable[[str, int, int], Any]
"""``(section, processed, limit)``; may return an awaitable."""


@dataclass
class SourceReport:
    """Result of a collection run, in a platform independent shape.

    ``counts`` holds one entry per collected kind (``messages``, ``posts``,
    ``comments``, ...) so the CLI can render any source with the same code.
    ``details`` carries source specific extras (client mode, effective proxy).
    """

    source: str
    target: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    duplicated: int = 0
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def add(self, kind: str, *, created: bool) -> None:
        """Register one stored item and whether it was already known."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if not created:
            self.duplicated += 1

    def merge(self, other: SourceReport) -> SourceReport:
        """Fold another report into this one and return it."""
        for kind, value in other.counts.items():
            self.counts[kind] = self.counts.get(kind, 0) + value
        self.duplicated += other.duplicated
        self.errors.extend(other.errors)
        self.details.update(other.details)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "counts": dict(self.counts),
            "duplicated": self.duplicated,
            "total": self.total,
            "errors": list(self.errors),
            "details": dict(self.details),
        }


@dataclass
class SavedItem:
    """Outcome of :meth:`BaseCollector.save`.

    ``ref`` is the knowledge-graph node reference, ``created`` tells the engine
    whether the row was new (used for duplicate reporting).
    """

    kind: str
    ref: str
    row: Any
    created: bool


class BaseCollector(ABC):
    """Shared collection engine.

    Subclasses declare their sections and implement the three hooks
    (:meth:`resolve`, :meth:`parse`, :meth:`save`) plus one method per section.
    Everything else - pacing, progress, duplicate counting, per-item error
    isolation - is inherited.
    """

    #: Source name used in reports and logs.
    source: ClassVar[str] = "source"
    #: Section name -> method name on this class.
    sections: ClassVar[Mapping[str, str]] = {}

    def __init__(
        self,
        client: Any,
        database: Database,
        *,
        delay: float = 0.0,
        batch_size: int = 50,
        progress: ProgressHook | None = None,
    ) -> None:
        self.client = client
        self.database = database
        self.delay = max(0.0, delay)
        self.batch_size = max(1, batch_size)
        self.progress = progress

    # --- hooks -------------------------------------------------------------
    @abstractmethod
    def resolve(self, session: Session, target: str) -> Any:
        """Resolve a user supplied reference into a platform object."""

    @abstractmethod
    def parse(self, raw: Any) -> dict[str, Any]:
        """Turn a raw platform object into a column mapping (``kind`` included)."""

    @abstractmethod
    def save(self, session: Session, parsed: Mapping[str, Any]) -> SavedItem:
        """Persist one parsed item."""

    # --- engine ------------------------------------------------------------
    def collect(
        self,
        target: str,
        *,
        sections: Sequence[str] = (),
        limit: int = 100,
    ) -> SourceReport:
        """Run the requested sections and return the merged report."""
        report = SourceReport(source=self.source, target=target)
        requested = tuple(sections) or tuple(self.sections)
        with self.database.session() as session:
            context = self.resolve(session, target)
        report.target = getattr(context, "label", None) or report.target

        for section in requested:
            method = self.section_method(section)
            if method is None:
                raise CollectionError(
                    f"Unknown section {section!r}",
                    hint=f"Available sections: {', '.join(self.sections) or 'none'}.",
                )
            logger.info("Collecting {} from {}", section, report.target)
            try:
                method(context, limit=limit, report=report)
            except NotFoundError:
                raise
            except XTGError as exc:
                report.errors.append(f"{section}: {exc}")
                logger.warning("Section {} failed: {}", section, exc)
            except Exception as exc:  # noqa: BLE001 - one section must not kill the run
                report.errors.append(f"{section}: {exc}")
                logger.warning("Section {} failed: {}", section, exc)
        return report

    def section_method(self, section: str) -> Callable[..., None] | None:
        """Bound method implementing ``section``, or ``None`` when unknown."""
        name = self.sections.get(section)
        return getattr(self, name, None) if name else None

    # --- helpers for the section implementations ---------------------------
    def store(self, session: Session, raw: Any, report: SourceReport) -> SavedItem | None:
        """Parse, save and account one item. Returns ``None`` when it failed."""
        try:
            parsed = self.parse(raw)
        except Exception as exc:  # noqa: BLE001 - malformed items are reported, not fatal
            report.errors.append(f"parse: {exc}")
            logger.debug("Could not parse item: {}", exc)
            return None
        try:
            item = self.save(session, parsed)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
            report.errors.append(f"{parsed.get('kind', 'item')}: {exc}")
            logger.debug("Could not save item: {}", exc)
            return None
        report.add(item.kind, created=item.created)
        return item

    def pace(self) -> None:
        """Wait between API calls to stay below the rate limits."""
        if self.delay:
            import time

            time.sleep(self.delay)

    def notify(self, section: str, processed: int, limit: int) -> None:
        """Report progress; ignored when no hook was given."""
        if self.progress is None:
            return
        self.progress(section, processed, limit)

    def iter_listing(self, listing: Any, *, section: str, limit: int) -> list[Any]:
        """Materialise a platform listing, bounded by ``limit``.

        Platform listing helpers are lazy generators that page internally; the
        collector pulls at most ``limit`` items so a generous ``limit`` can
        never turn into an unbounded crawl.
        """
        items: list[Any] = []
        for item in listing:
            if limit > 0 and len(items) >= limit:
                break
            items.append(item)
            self.notify(section, len(items), limit)
            self.pace()
        return items


class BaseSource(ABC):
    """One upstream platform, used as a context manager by the CLI."""

    #: Registry key (``telegram``, ``reddit``, ...).
    name: ClassVar[str] = "source"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        database: Database | None = None,
        client: Any | None = None,
        require_authorized: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.database = database or Database(self.settings)
        self.require_authorized = require_authorized
        self._client = client
        self._collector: BaseCollector | None = None

    # --- factory hooks -----------------------------------------------------
    @abstractmethod
    def create_client(self) -> Any:
        """Build the platform client (no network call is made here)."""

    @abstractmethod
    def create_collector(self, client: Any) -> BaseCollector:
        """Build the collector that walks the platform for this client."""

    # --- life cycle --------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            self.connect()
        return self._client

    @property
    def collector(self) -> BaseCollector:
        if self._collector is None:
            self.connect()
        assert self._collector is not None  # noqa: S101 - narrowed by connect()
        return self._collector

    def connect(self) -> Any:
        """Create the client and collector. Idempotent."""
        if self._client is None:
            self._client = self.create_client()
        if self._collector is None:
            self._collector = self.create_collector(self._client)
        return self._client

    def close(self) -> None:
        """Release the client. Never raises; subclasses override when needed."""
        self._client = None
        self._collector = None

    # --- source contract ---------------------------------------------------
    def parse(self, raw: Any) -> dict[str, Any]:
        """Delegate to the collector: raw platform object -> column mapping."""
        return self.collector.parse(raw)

    def save(self, session: Session, parsed: Mapping[str, Any]) -> SavedItem:
        """Delegate to the collector: column mapping -> stored row."""
        return self.collector.save(session, parsed)

    def collect(
        self,
        target: str,
        *,
        sections: Sequence[str] = (),
        limit: int = 100,
    ) -> SourceReport:
        """Connect, collect the requested sections and report the result."""
        self.connect()
        return self.collector.collect(target, sections=sections, limit=limit)

    # --- convenience -------------------------------------------------------
    def __enter__(self) -> BaseSource:
        self.connect()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class SourceConnector(Protocol):
    """Structural type of a source, used by the registry and the CLI."""

    name: ClassVar[str]

    def connect(self) -> Any: ...
    def close(self) -> None: ...
    def parse(self, raw: Any) -> dict[str, Any]: ...
    def save(self, session: Session, parsed: Mapping[str, Any]) -> SavedItem: ...
    def collect(
        self, target: str, *, sections: Sequence[str] = (), limit: int = 100
    ) -> SourceReport: ...
