"""Fake Telegram API objects.

Telethon is never imported by the test suite: the collector and the login flow
only use the async surface of the client, so these doubles are enough.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xtg.telegram.auth import _NamedSessionPasswordNeeded


@dataclass
class FakeMedia:
    file_name: str | None = None
    size: int | None = None
    mime_type: str | None = None
    duration: int | None = None
    w: int | None = None
    h: int | None = None


@dataclass
class FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    about: str | None = None
    phone: str | None = None
    bot: bool = False
    premium: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "about": self.about,
            "phone": self.phone,
            "bot": self.bot,
        }


@dataclass
class FakeChat:
    id: int
    title: str | None = None
    username: str | None = None
    channel: bool = False
    megagroup: bool = False
    participants_count: int | None = None
    verified: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "username": self.username,
            "participants_count": self.participants_count,
        }


@dataclass
class FakeMessage:
    id: int
    text: str | None = None
    sender: FakeUser | None = None
    date: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    reply_to: Any | None = None
    views: int | None = None
    forwards: Any | None = None
    photo: FakeMedia | None = None
    document: FakeMedia | None = None
    video: FakeMedia | None = None
    voice: FakeMedia | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "date": self.date.isoformat()}


@dataclass
class FakeParticipant:
    user: FakeUser
    admin_rights: Any | None = None
    banned_rights: Any | None = None
    creator: bool = False


class FakeClient:
    """Minimal stand-in for :class:`telethon.TelegramClient`."""

    def __init__(
        self,
        *,
        entities: dict[str, Any] | None = None,
        messages: list[FakeMessage] | None = None,
        participants: list[FakeParticipant] | None = None,
        me: FakeUser | None = None,
        authorized: bool = True,
    ) -> None:
        self.entities = entities or {}
        self.messages = messages or []
        self.participants = participants or []
        self.me = me
        self.authorized = authorized
        self.connected = False
        self.disconnected = False
        self.calls: list[tuple[str, Any]] = []
        self.message_kwargs: list[dict[str, Any]] = []
        self.participant_kwargs: list[dict[str, Any]] = []
        self.code_requested_for: str | None = None
        self.signed_in_with: dict[str, Any] | None = None
        self.code_error: Exception | None = None
        self.password_error: Exception | None = None
        self.needs_password: bool = False

    # --- connection --------------------------------------------------------
    async def connect(self) -> FakeClient:
        """Mirrors Telethon: ``connect()`` is a coroutine."""
        self.connected = True
        return self

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> Any:
        return self.me

    # --- auth --------------------------------------------------------------
    async def send_code_request(self, phone: str) -> None:
        self.calls.append(("send_code_request", phone))
        self.code_requested_for = phone

    async def sign_in(self, **kwargs: Any) -> Any:
        self.calls.append(("sign_in", kwargs))
        if "password" in kwargs:
            if self.password_error is not None:
                raise self.password_error
            self.signed_in_with = kwargs
            self.authorized = True
            return self.me or FakeUser(id=1, username="authorized")
        if self.code_error is not None:
            raise self.code_error
        if self.needs_password:
            raise _NamedSessionPasswordNeeded("Two-factor required")
        self.signed_in_with = kwargs
        self.authorized = True
        return self.me or FakeUser(id=1, username="authorized")

    # --- api ---------------------------------------------------------------
    async def get_entity(self, value: Any) -> Any:
        self.calls.append(("get_entity", value))
        entity = self.entities.get(str(value))
        if entity is None:
            raise _UsernameNotOccupied(str(value))
        return entity

    def iter_messages(self, entity: Any, **kwargs: Any) -> Any:
        self.calls.append(("iter_messages", entity))
        self.message_kwargs.append(kwargs)
        return _aiter(self._filtered_messages(kwargs))

    def _filtered_messages(self, kwargs: dict[str, Any]) -> list[FakeMessage]:
        """Mimic Telethon: newest first, ``offset_id`` is an exclusive upper bound."""
        offset = kwargs.get("offset_id")
        selected = [message for message in self.messages if not offset or message.id < offset]
        limit = kwargs.get("limit")
        return selected[:limit] if limit else selected

    def iter_participants(self, entity: Any, **kwargs: Any) -> Any:
        self.calls.append(("iter_participants", entity))
        self.participant_kwargs.append(kwargs)
        return _aiter(self.participants[: kwargs.get("limit", len(self.participants))])


class _UsernameNotOccupied(Exception):
    """Mirrors ``telethon.errors.UsernameNotOccupiedError`` by name."""


async def _aiter(items: list[Any]) -> Any:
    for item in items:
        yield item


class StaticPrompter:
    """Prompter returning canned answers."""

    def __init__(
        self, *, phone: str = "+10000000000", code: str = "12345", password: str = "s3cret"
    ):
        self.answers = {"phone": phone, "code": code, "password": password}
        self.asked: list[str] = []

    def phone(self) -> str:
        self.asked.append("phone")
        return self.answers["phone"]

    def code(self) -> str:
        self.asked.append("code")
        return self.answers["code"]

    def password(self) -> str:
        self.asked.append("password")
        return self.answers["password"]


def make_messages(count: int, *, chat_id: int = 1) -> list[FakeMessage]:
    """Build ``count`` messages with one shared sender."""
    sender = FakeUser(id=7, username="alice", first_name="Alice", about="hello")
    return [
        FakeMessage(
            id=index,
            text=f"message {index}",
            sender=sender,
            photo=FakeMedia(file_name=f"photo-{index}.jpg", size=1000, mime_type="image/jpeg")
            if index % 3 == 0
            else None,
        )
        for index in range(1, count + 1)
    ]


def make_chat() -> FakeChat:
    return FakeChat(
        id=-1001234567890,
        title="Example Channel",
        username="example",
        channel=True,
        participants_count=3,
    )


#: The schema Telethon 1.45 creates, verbatim. Kept here so tests build files
#: the validator has to agree with, instead of a convenient approximation.
TELETHON_SCHEMA = (
    "CREATE TABLE version (version integer primary key)",
    """CREATE TABLE sessions (
        dc_id integer primary key,
        server_address text,
        port integer,
        auth_key blob,
        takeout_id integer,
        tmp_auth_key blob
    )""",
    "CREATE TABLE entities (id integer primary key, hash integer not null,"
    " username text, phone integer, name text, date integer)",
    "CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer,"
    " id integer, hash integer, primary key(md5_digest, file_size, type))",
    "CREATE TABLE update_state (id integer primary key, pts integer, qts integer,"
    " date integer, seq integer)",
)

#: ``SQLiteSession.CURRENT_VERSION`` in Telethon 1.45.
TELETHON_SCHEMA_VERSION = 8

#: A structurally valid key that carries no meaning. Never a real auth key.
FAKE_AUTH_KEY = bytes(range(256))


def write_session_file(
    path: Path,
    *,
    authorized: bool = True,
    dc_id: int = 2,
    port: int = 443,
    version: int = TELETHON_SCHEMA_VERSION,
) -> Path:
    """Create a file shaped exactly like a real Telethon session database.

    Telethon appends ``.session`` to the session id, so tests must pass the
    same path the application resolves (``<home>/data/sessions/<name>.session``).

    ``authorized=False`` writes the tables but no auth key, which is what a
    freshly created, never-logged-in session looks like on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        for statement in TELETHON_SCHEMA:
            connection.execute(statement)
        connection.execute("INSERT INTO version VALUES (?)", (version,))
        if authorized:
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (dc_id, "149.154.167.92", port, FAKE_AUTH_KEY, None, None),
            )
        connection.commit()
    return path


def write_corrupt_sqlite(path: Path) -> Path:
    """A file that starts like SQLite but is truncated mid-page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    good = write_session_file(path)
    blob = good.read_bytes()
    good.write_bytes(blob[: len(blob) // 3])
    return path


def write_unrelated_sqlite(path: Path) -> Path:
    """A perfectly valid SQLite database that is not a Telegram session."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes (id integer primary key, body text)")
        connection.execute("INSERT INTO notes (body) VALUES ('hello')")
        connection.commit()
    return path
