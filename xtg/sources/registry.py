"""Lazy registry of the available sources.

Sources are imported on first use, so ``xtg status`` never pays for PRAW or
Telethon and a missing dependency only affects the command that needs it.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from xtg.core.config import Settings
from xtg.core.errors import ConfigError
from xtg.database.db import Database
from xtg.sources.base import BaseSource

#: Registry key -> ``(module, class)``. Keeping the module path as a string is
#: what makes the import lazy.
_SOURCES: dict[str, tuple[str, str]] = {
    "telegram": ("xtg.sources.telegram", "TelegramSource"),
    "reddit": ("xtg.sources.reddit", "RedditSource"),
}

# Official documentation links for the supported platform families and ancillary
# OSINT sources that are represented in the project but not necessarily backed by
# first-class collectors yet.
SOURCE_DOCS: dict[str, str] = {
    "telegram": "https://core.telegram.org/api",
    "reddit": "https://praw.readthedocs.io/en/stable/",
    "x.com": "https://docs.x.com/x-api/getting-started/about-x-api",
}


def normalize_source_name(name: str) -> str:
    """Return a stable normalized source key from a name or URL."""
    normalized = name.strip().lower()
    normalized = normalized.removeprefix("https://")
    normalized = normalized.removeprefix("http://")
    normalized = normalized.removeprefix("www.")
    return normalized.rstrip("/")


def get_source_docs(name: str) -> str | None:
    """Return the official docs URL for a source name or hostname."""
    return SOURCE_DOCS.get(normalize_source_name(name))


def available_sources() -> tuple[str, ...]:
    """Registry keys, sorted."""
    return tuple(sorted(_SOURCES))


def get_source_class(name: str) -> type[BaseSource]:
    """Import and return the source class registered under ``name``."""
    try:
        module_name, class_name = _SOURCES[name.strip().lower()]
    except KeyError as exc:
        raise ConfigError(
            f"Unknown source {name!r}",
            hint=f"Available sources: {', '.join(available_sources())}.",
        ) from exc
    module = import_module(module_name)
    return getattr(module, class_name)


def get_source(
    name: str,
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    client: Any | None = None,
    require_authorized: bool = True,
) -> BaseSource:
    """Build a source instance by registry key."""
    source_class = get_source_class(name)
    return source_class(
        settings, database=database, client=client, require_authorized=require_authorized
    )
