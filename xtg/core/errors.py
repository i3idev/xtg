"""Process exit codes and the exception hierarchy shared by every subsystem.

Exit codes are stable and documented in the README so that `xtg` can be used
inside shell scripts (``xtg status || echo "broken"``).
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable process exit codes."""

    OK = 0
    ERROR = 1
    USAGE = 2
    CONFIG = 3
    DATABASE = 4
    AUTH = 5
    NETWORK = 6
    PROXY = 7
    NOT_FOUND = 8
    NOT_IMPLEMENTED = 9
    CANCELLED = 130


class XTGError(Exception):
    """Base class for every expected eXtractorT failure.

    Subclasses only need to declare ``exit_code``; the CLI top level turns any
    ``XTGError`` into a clean Rich message instead of a traceback.
    """

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class ConfigError(XTGError):
    """Invalid, missing or contradictory configuration."""

    exit_code = ExitCode.CONFIG


class DatabaseError(XTGError):
    """The local database could not be opened, created or queried."""

    exit_code = ExitCode.DATABASE


class AuthError(XTGError):
    """Telegram credentials or the interactive login flow failed."""

    exit_code = ExitCode.AUTH


class NetworkError(XTGError):
    """A network operation failed."""

    exit_code = ExitCode.NETWORK


class ProxyError(XTGError):
    """The proxy list is unusable or a proxy could not be selected."""

    exit_code = ExitCode.PROXY


class NotFoundError(XTGError):
    """A requested entity does not exist in the local dataset."""

    exit_code = ExitCode.NOT_FOUND


class FeatureNotAvailableError(XTGError):
    """A command belongs to a later development phase and is not built yet."""

    exit_code = ExitCode.NOT_IMPLEMENTED

    def __init__(
        self,
        feature: str,
        *,
        phase: str = "Phase 2",
        hint: str | None = None,
    ) -> None:
        default_hint = (
            f"{feature} is scheduled for {phase} and is not implemented yet. "
            "Run `xtg status` for an overview of what is currently available."
        )
        super().__init__(f"{feature} is not available yet.", hint=hint or default_hint)
