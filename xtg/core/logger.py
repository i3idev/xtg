"""Loguru based logging setup.

Logs always go to stderr (so ``--json`` output stays pipeable) and, optionally,
to a rotating file under ``data/logs/xtg.log``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from loguru import logger

from xtg.core.config import Settings

Verbosity = Literal["quiet", "normal", "verbose", "debug"]

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
)
_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"

_LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 3


def configure_logging(
    settings: Settings,
    *,
    verbosity: Verbosity = "normal",
) -> None:
    """(Re)configure the global logger. Idempotent - safe to call per command."""
    logger.remove()
    logger.configure(extra={"xtg": True})

    if verbosity != "quiet":
        logger.add(
            sys.stderr,
            level=_console_level(settings.log_level, verbosity),
            format=_CONSOLE_FORMAT,
            colorize=sys.stderr.isatty(),
            backtrace=False,
            diagnose=False,
        )

    if settings.log_to_file:
        _add_file_sink(settings.logs_path)


def _console_level(configured: str, verbosity: Verbosity) -> str:
    if verbosity == "debug":
        return "DEBUG"
    if verbosity == "verbose":
        return "INFO"
    if verbosity == "normal":
        return configured.upper()
    return "ERROR"


def _add_file_sink(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "xtg.log",
        level="DEBUG",
        format=_FILE_FORMAT,
        rotation=_LOG_FILE_MAX_BYTES,
        retention=_LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
        enqueue=False,
        backtrace=True,
        diagnose=False,
    )


__all__ = ["Verbosity", "configure_logging", "logger"]
