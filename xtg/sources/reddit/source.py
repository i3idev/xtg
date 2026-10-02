"""Reddit source: the :class:`~xtg.sources.base.BaseSource` implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from xtg.sources.base import BaseCollector, BaseSource, SourceReport
from xtg.sources.reddit.client import create_reddit
from xtg.sources.reddit.collector import RedditCollector


class RedditSource(BaseSource):
    """Reddit as a source: connect, parse, save, collect."""

    name = "reddit"

    def __init__(
        self,
        *args: Any,
        factory: Any | None = None,
        listing: str | None = None,
        resume: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._factory = factory
        self._listing = listing
        self._resume = resume

    def create_client(self) -> Any:
        """Build the PRAW client (no request is made until the first call)."""
        return create_reddit(self.settings, factory=self._factory)

    def create_collector(self, client: Any) -> BaseCollector:
        return RedditCollector(
            client,
            self.database,
            delay=self.settings.request_delay,
            listing=self._listing or self.settings.reddit_listing,
            resume=self._resume,
        )

    def collect(
        self,
        target: str,
        *,
        sections: Sequence[str] = (),
        limit: int = 100,
    ) -> SourceReport:
        """Connect, walk the requested sections and report the result."""
        self.connect()
        return self.collector.collect(target, sections=sections, limit=limit)

    def sections_for(self, requested: Sequence[str]) -> tuple[str, ...]:
        """Default to posts when the caller did not ask for anything specific."""
        return tuple(requested) or ("posts",)
