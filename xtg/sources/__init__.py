"""Source layer: platform independent collection contracts.

Importing this package never imports a platform client. Concrete sources live
in :mod:`xtg.sources.telegram` and :mod:`xtg.sources.reddit` and are loaded on
demand through :func:`get_source`, so a missing optional dependency (Telethon,
PRAW) never breaks ``xtg --help`` or the offline commands.
"""

from __future__ import annotations

from typing import Any

from xtg.sources.base import (
    BaseCollector,
    BaseSource,
    SavedItem,
    SourceConnector,
    SourceReport,
)

__all__ = [
    "BaseCollector",
    "BaseSource",
    "SavedItem",
    "SourceConnector",
    "SourceReport",
    "SOURCE_DOCS",
    "available_sources",
    "get_source",
    "get_source_docs",
]


def __getattr__(name: str) -> Any:
    """Expose the registry lazily (see :mod:`xtg.sources.registry`)."""
    if name in {"get_source", "available_sources", "get_source_docs", "SOURCE_DOCS"}:
        from xtg.sources import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
