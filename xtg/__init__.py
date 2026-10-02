"""eXtractorT - Social Media Intelligence & Extraction Toolkit."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:  # pragma: no cover - depends on installation method
    __version__ = _version("xtg")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.1.0"

__all__ = ["__version__"]
