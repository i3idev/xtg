"""Collection orchestration (Phase 2 boundary).

The collector is responsible for turning Telegram objects into database rows.
Phase 1 ships only the shape of the collector so that the CLI, the models and
the proxy/database subsystems can be developed and tested independently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from xtg.database import queries
from xtg.database.models import Chat, Message, User

ProgressHook = Callable[[str, int, int], Awaitable[None] | None]


@dataclass(slots=True)
class CollectionReport:
    """Counters returned by every collection run."""

    chat: str = ""
    messages: int = 0
    members: int = 0
    media: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.messages + self.members + self.media

    def merge(self, other: CollectionReport) -> CollectionReport:
        self.messages += other.messages
        self.members += other.members
        self.media += other.media
        self.errors.extend(other.errors)
        return self


class Collector(Protocol):
    """Contract implemented by the Phase 2 collectors."""

    async def collect_messages(self, target: str, limit: int) -> CollectionReport: ...

    async def collect_members(self, target: str, limit: int) -> CollectionReport: ...

    async def collect_media(self, target: str, limit: int, download: bool) -> CollectionReport: ...


def store_message(
    session: Any,
    chat: Chat,
    telegram_message_id: int,
    *,
    sender: User | None = None,
    **fields: Any,
) -> Message:
    """Persist a message. Thin wrapper so collectors stay readable."""
    return queries.upsert_message(session, chat, telegram_message_id, sender=sender, **fields)


def store_chat(session: Any, telegram_id: int, **fields: Any) -> Chat:
    """Persist a chat entity."""
    return queries.upsert_chat(session, telegram_id, **fields)


def store_user(session: Any, telegram_id: int, **fields: Any) -> User:
    """Persist a user entity."""
    return queries.upsert_user(session, telegram_id, **fields)
