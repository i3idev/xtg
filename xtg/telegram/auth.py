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
    """Coarse authentication state derived from local files.

    The states are deliberately distinct. "There is no session file" is a very
    different fact from "Telegram has this device authorized": collapsing them
    makes a fresh install look like a failed login, and hides a corrupt or
    hand-edited session behind the same message as an ordinary one.
    """

    #: No API credentials configured, so nothing can be checked.
    UNCONFIGURED = "unconfigured"
    #: No session file at all - never logged in, or it was deleted.
    MISSING = "missing"
    #: A file exists but is not a usable Telethon session.
    INVALID = "invalid"
    #: A valid Telethon session that holds no authorization yet.
    NOT_AUTHORIZED = "not_authorized"
    #: A valid Telethon session holding an auth key.
    AUTHORIZED = "authorized"
    #: A rollback journal is present, so another process has it open.
    LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Where the session lives and what it actually is.

    ``exists`` only says a file is there. ``valid`` says it is a Telethon
    session database and ``authorized`` says it also holds an auth key, which
    is what :meth:`state` and the ``xtg auth status`` report rely on: a leftover
    or truncated file is present but useless, and reporting it as "authorized"
    sends the user looking for the wrong problem.
    """

    path: Path
    exists: bool
    locked: bool
    size_bytes: int
    mode: str = "-rw-r--r--"
    valid: bool = False
    authorized: bool = False
    problem: str = ""

    @property
    def state(self) -> AuthState:
        """The most specific state the file on disk supports.

        Each branch answers a different question, and keeping them apart is what
        stops "no session file" from being reported as "Telegram is not
        authorized".
        """
        if self.locked:
            return AuthState.LOCKED
        if not self.exists:
            return AuthState.MISSING
        if not self.valid:
            return AuthState.INVALID
        return AuthState.AUTHORIZED if self.authorized else AuthState.NOT_AUTHORIZED


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
    """Absolute path of the Telethon session file (``<home>/data/sessions/<name>.session``).

    This is the single resolver behind ``xtg auth login``, ``status`` and
    ``logout``: they all go through :attr:`Settings.session_file`, so they can
    never disagree about which file the session lives in.
    """
    settings = settings or get_settings()
    return settings.session_file


def session_files(settings: Settings | None = None) -> list[Path]:
    """Every file Telethon created for the configured session.

    SQLite companions use both separators: ``xtg.session-journal`` and, in older
    layouts, ``xtg.session.db``.
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
    probe = probe_session(path)
    return SessionInfo(
        path=path,
        exists=exists,
        locked=journal.exists(),
        size_bytes=path.stat().st_size if exists else 0,
        mode=_mode_string(path) if exists else "-",
        valid=probe.valid,
        authorized=probe.authorized,
        problem=probe.problem,
    )


@dataclass(frozen=True, slots=True)
class SessionProbe:
    """What a local file on disk actually is.

    Telethon 1.45 stores a session in a SQLite database carrying a ``version``
    table and a ``sessions`` table holding ``auth_key``; being authorized means
    that row exists with a key in it. The three flags separate "not there",
    "there but not a session" and "a session, but nobody has logged in yet", so
    ``xtg auth status`` can say which of those is true instead of collapsing
    them all into "not authorized".

    The auth key is never read into this object, so it cannot leak through a
    repr, a log or a JSON payload.
    """

    valid: bool = False
    authorized: bool = False
    problem: str = ""


def probe_session(path: Path) -> SessionProbe:
    """Classify a file as a Telethon session, offline.

    Handles every case that matters: missing, zero-byte, a random file, a
    truncated or corrupted SQLite database, a SQLite file that is not a
    Telethon session, a valid session with no auth key, and a valid authorized
    session. Never raises - an unreadable file is simply not a session.
    """
    if not path.exists():
        return SessionProbe(problem="no session file")
    if not path.is_file():
        return SessionProbe(problem="not a regular file")
    if path.stat().st_size == 0:
        return SessionProbe(problem="session file is empty")

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            # Opening is not enough: sqlite happily treats an arbitrary file as
            # a database and only fails on the first real statement.
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not tables >= _REQUIRED_TABLES:
                return SessionProbe(problem="not a Telegram session database")
            authorized = (
                connection.execute(
                    "SELECT count(*) FROM sessions WHERE auth_key IS NOT NULL"
                ).fetchone()[0]
                > 0
            )
    except sqlite3.DatabaseError:
        return SessionProbe(problem="not a readable SQLite database")
    except OSError:
        return SessionProbe(problem="session file is not readable")

    return SessionProbe(valid=True, authorized=authorized)


#: Both tables exist in every SQLite session Telethon has written, and neither
#: is optional: ``version`` carries the schema revision and ``sessions`` the
#: auth key. Requiring both rejects a valid-but-unrelated SQLite file.
_REQUIRED_TABLES = frozenset({"version", "sessions"})


def is_session_database(path: Path) -> bool:
    """True when ``path`` really is a Telethon session file."""
    return probe_session(path).valid


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
