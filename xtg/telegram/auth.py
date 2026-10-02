"""Session state inspection.

Only local, dependency-free checks live here (does a session file exist, is it
locked by another process). The interactive login flow needs Telethon and is
implemented in Phase 2 inside :mod:`xtg.telegram.client`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from xtg.core.config import Settings, get_settings


class AuthState(StrEnum):
    """Coarse authentication state derived from local files."""

    UNCONFIGURED = "unconfigured"
    NOT_AUTHORIZED = "not_authorized"
    AUTHORIZED = "authorized"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Where the session lives and whether it looks usable."""

    path: Path
    exists: bool
    locked: bool
    size_bytes: int

    @property
    def state(self) -> AuthState:
        if self.locked:
            return AuthState.LOCKED
        return AuthState.AUTHORIZED if self.exists else AuthState.NOT_AUTHORIZED


def session_path(settings: Settings | None = None) -> Path:
    """Absolute path of the Telethon session file (without suffix)."""
    settings = settings or get_settings()
    return settings.session_file


def describe_session(settings: Settings | None = None) -> SessionInfo:
    """Inspect the session file without touching the network."""
    settings = settings or get_settings()
    path = session_path(settings)
    exists = path.exists()
    journal = path.with_name(f"{path.name}-journal")
    return SessionInfo(
        path=path,
        exists=exists,
        locked=journal.exists(),
        size_bytes=path.stat().st_size if exists else 0,
    )


def auth_state(settings: Settings | None = None) -> AuthState:
    """Full local authentication state, credentials included."""
    settings = settings or get_settings()
    if not settings.has_api_credentials:
        return AuthState.UNCONFIGURED
    return describe_session(settings).state
