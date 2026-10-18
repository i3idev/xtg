"""Subscription source manager.

Persists named subscription URLs next to the global proxy state so that
``xtg proxy global source`` can list, add, remove and refresh from them.
Sources are stored in ``sources.json`` under the proxy state directory as
``{name: url}``.
"""

from __future__ import annotations

import json
from pathlib import Path

from xtg.core.errors import ProxyError


class SourcesManager:
    """Manage named subscription sources for the global proxy pool."""

    SOURCES_FILE_NAME = "sources.json"

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.sources_file = self.state_dir / self.SOURCES_FILE_NAME
        self._sources: dict[str, str] = self._load_sources()

    # --- persistence -------------------------------------------------------

    def _load_sources(self) -> dict[str, str]:
        if not self.sources_file.exists():
            return {}
        try:
            data = json.loads(self.sources_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v}

    def _save_sources(self) -> None:
        self.sources_file.write_text(json.dumps(self._sources, indent=2))

    # --- accessors ---------------------------------------------------------

    @property
    def sources(self) -> dict[str, str]:
        """Return a copy of the registered sources."""
        return dict(self._sources)

    def get(self, name: str) -> str | None:
        return self._sources.get(name)

    def names(self) -> list[str]:
        return sorted(self._sources)

    # --- mutations ---------------------------------------------------------

    def add(self, name: str, url: str) -> None:
        """Add or overwrite a subscription source."""
        if not name:
            raise ProxyError("Source name must not be empty")
        if not url:
            raise ProxyError("Subscription URL must not be empty")
        self._sources[name] = url
        self._save_sources()

    def remove(self, name: str) -> bool:
        """Remove a subscription source. Returns True if it existed."""
        if name not in self._sources:
            return False
        del self._sources[name]
        self._save_sources()
        return True

    def clear(self) -> int:
        """Remove every subscription source. Returns the count removed."""
        count = len(self._sources)
        self._sources.clear()
        self._save_sources()
        return count
