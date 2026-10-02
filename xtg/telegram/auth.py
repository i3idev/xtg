"""Authentication: local session state and the interactive login flow.

The login flow itself is pure asyncio and only talks to the Telethon *client*
object, never to Telethon directly. That keeps it fully testable with a fake
client and keeps the interactive prompts (phone, code, 2FA password) in the CLI
layer, injected through the :class:`Prompter` protocol.

Security notes:

* the 2FA password is used exactly once, in memory, and never stored
  (not in the database, not in the session file, not in the logs)
* the session file is chmod'ed to ``0600`` after a successful login
"""

from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from xtg.core.config import Settings, get_settings
from xtg.core.errors import AuthError
from xtg.core.logger import logger


class AuthState(StrEnum):
    """Coarse authentication state derived from local files."""

    UNCONFIGURED = "unconfigured"
    NOT_AUTHORIZED = "not_authorized"
    AUTHORIZED = "authorized"
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Where the session lives and whether it looks usable.

    ``exists`` only says a file is there. ``valid`` says it is a Telethon
    session database, which is what :meth:`state` and the ``xtg auth status``
    report rely on: a leftover or truncated file is present but useless, and
    reporting it as "authorized" sends the user looking for the wrong problem.
    """

    path: Path
    exists: bool
    locked: bool
    size_bytes: int
    mode: str = "-rw-r--r--"
    valid: bool = False

    @property
    def state(self) -> AuthState:
        if self.locked:
            return AuthState.LOCKED
        return AuthState.AUTHORIZED if self.valid else AuthState.NOT_AUTHORIZED


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of a login attempt (no secret is ever kept here)."""

    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    phone: str | None = None
    already_authorized: bool = False
    used_password: bool = False

    @property
    def authorized(self) -> bool:
        return self.already_authorized or self.user_id is not None

    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return " ".join(filter(None, (self.first_name, self.phone))) or str(self.user_id or "?")


@runtime_checkable
class Prompter(Protocol):
    """Interactive input provider. Implemented by the CLI layer."""

    def phone(self) -> str: ...

    def code(self) -> str: ...

    def password(self) -> str: ...


# --- local session inspection ------------------------------------------------


def session_path(settings: Settings | None = None) -> Path:
    """Absolute path of the Telethon session file (without suffix)."""
    settings = settings or get_settings()
    return settings.session_file


def session_files(settings: Settings | None = None) -> list[Path]:
    """Every file Telethon created for the configured session.

    SQLite companions use both separators: ``xtg.db`` and ``xtg-journal``.
    """
    settings = settings or get_settings()
    path = settings.session_file
    if not path.parent.exists():
        return []
    stem = path.name
    return sorted(
        item
        for item in path.parent.iterdir()
        if item.is_file() and (item.name == stem or item.name.startswith((f"{stem}.", f"{stem}-")))
    )


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
        mode=_mode_string(path) if exists else "-",
        valid=is_session_database(path) if exists else False,
    )


def is_session_database(path: Path) -> bool:
    """True when ``path`` really is a Telethon session file.

    Telethon stores the session in a SQLite database with a ``sessions`` table
    holding the auth key. Checking that locally is cheap and keeps
    ``xtg auth status`` honest: any other file - a stale export, a truncated
    write, a directory - is reported as "no session" instead of pretending the
    account is logged in.

    Never raises: a file that cannot be inspected is simply not a session.
    """
    if not path.is_file():
        return False
    try:
        if path.stat().st_size == 0:
            return False
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
    except (sqlite3.Error, OSError):
        return False
    return bool(row and row[0])


def auth_state(settings: Settings | None = None) -> AuthState:
    """Full local authentication state, credentials included."""
    settings = settings or get_settings()
    if not settings.has_api_credentials:
        return AuthState.UNCONFIGURED
    return describe_session(settings).state


# --- login flow --------------------------------------------------------------


async def login(client: Any, prompter: Prompter) -> LoginResult:
    """Run the interactive Telethon login flow.

    Handles the three cases Telegram uses:

    1. the session is already authorized -> nothing to do
    2. phone number + login code
    3. phone number + login code + 2FA password

    Args:
        client: a connected Telethon client (``connect()`` already called).
        prompter: source of the interactive answers.

    Raises:
        AuthError: when Telegram rejects the code, the password or the phone.
    """
    if await client.is_user_authorized():
        me = await _safe_get_me(client)
        logger.info("Session already authorized")
        return LoginResult(
            user_id=getattr(me, "id", None),
            username=getattr(me, "username", None),
            first_name=getattr(me, "first_name", None),
            phone=getattr(me, "phone", None),
            already_authorized=True,
        )

    phone = normalize_phone(prompter.phone())
    await client.send_code_request(phone)
    logger.info("Login code requested for {}", phone)

    code = prompter.code().strip()
    try:
        user = await client.sign_in(phone=phone, code=code)
        used_password = False
    except _password_needed_errors() as exc:
        logger.info("Two-factor authentication required")
        password = prompter.password().strip()
        if not password:
            raise AuthError(
                "The 2FA password is required",
                hint="Re-run `xtg auth login` and enter your cloud password.",
            ) from exc
        try:
            user = await client.sign_in(password=password)
        except Exception as sign_in_error:
            raise AuthError(
                f"Telegram rejected the 2FA password: {_error_text(sign_in_error)}",
                hint="Check the password and try again.",
            ) from sign_in_error
        used_password = True
    except Exception as sign_in_error:
        raise AuthError(
            f"Telegram rejected the login code: {_error_text(sign_in_error)}",
            hint="Request a new code and run `xtg auth login` again.",
        ) from sign_in_error

    result = LoginResult(
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        phone=getattr(user, "phone", None),
        used_password=used_password,
    )
    logger.info("Authorized as {}", result.display_name())
    return result


def normalize_phone(value: str) -> str:
    """Strip formatting from a phone number, keeping the ``+`` prefix."""
    cleaned = "".join(char for char in value.strip() if char.isdigit() or char == "+")
    if not cleaned.lstrip("+").isdigit():
        raise AuthError(f"{value!r} is not a valid phone number")
    return cleaned


def harden_session_files(settings: Settings | None = None) -> list[Path]:
    """Restrict the session files to the owner (best effort).

    The session file is the account: anyone who can read it can use it. Telethon
    does not set restrictive permissions on every platform, so we do it here.
    """
    hardened: list[Path] = []
    for path in session_files(settings):
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            logger.warning("Could not restrict permissions on {}: {}", path.name, exc)
            continue
        hardened.append(path)
    return hardened


# --- helpers -----------------------------------------------------------------


async def _safe_get_me(client: Any) -> Any:
    try:
        return await client.get_me()
    except Exception as exc:  # noqa: BLE001 - informational only
        logger.debug("get_me() failed: {}", exc)
        return None


def _password_needed_errors() -> tuple[type[BaseException], ...]:
    """Exception types signalling that 2FA is required.

    Telethon is imported lazily; the local marker keeps the flow testable with a
    fake client and usable when the dependency is not installed.
    """
    try:
        from telethon.errors import SessionPasswordNeededError

        return (SessionPasswordNeededError, _NamedSessionPasswordNeeded)
    except ImportError:
        return (_NamedSessionPasswordNeeded,)


class _NamedSessionPasswordNeeded(Exception):
    """Fallback marker matching Telethon's exception name."""


def _error_text(error: Exception) -> str:
    text = str(error).strip()
    return text or type(error).__name__


def _mode_string(path: Path) -> str:
    try:
        return stat.filemode(path.stat().st_mode)
    except OSError:  # pragma: no cover - race with deletion
        return "?"
