"""Export subsystem.

Phase 1 defines the format registry and the writer contract; the concrete
writers (json / csv / markdown / obsidian) land together with the collection
feature, because exporting an empty dataset is pointless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from xtg.core.errors import FeatureNotAvailableError


class ExportFormat(StrEnum):
    """Supported export formats."""

    JSON = "json"
    CSV = "csv"
    MARKDOWN = "markdown"
    OBSIDIAN = "obsidian"


@dataclass(slots=True)
class ExportResult:
    """What a writer produced."""

    path: Path
    format: ExportFormat
    records: int = 0
    extra_files: list[Path] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.exists()


@runtime_checkable
class Writer(Protocol):
    """Contract every export format must implement."""

    format: ExportFormat

    def write(self, rows: list[dict[str, Any]], destination: Path) -> ExportResult:
        """Serialise ``rows`` to ``destination``."""


_WRITERS: dict[ExportFormat, type[Writer]] = {}


def register_writer(writer: type[Writer]) -> type[Writer]:
    """Class decorator that adds a writer to the registry."""
    _WRITERS[writer.format] = writer
    return writer


def get_writer(export_format: ExportFormat) -> type[Writer]:
    """Return the writer class for ``export_format``.

    Raises:
        FeatureNotAvailableError: when the writer is not implemented yet.
    """
    try:
        return _WRITERS[ExportFormat(export_format)]
    except (ValueError, KeyError) as exc:
        name = getattr(export_format, "value", str(export_format))
        raise FeatureNotAvailableError(
            f"The {name} writer",
            phase="Phase 3",
        ) from exc


def available_formats() -> list[str]:
    """Names of the formats that can be written right now."""
    return sorted(fmt.value for fmt in _WRITERS)


__all__ = [
    "ExportFormat",
    "ExportResult",
    "Writer",
    "available_formats",
    "get_writer",
    "register_writer",
]
