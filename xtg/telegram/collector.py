"""Collection orchestration: Telegram objects -> database rows.

The collector is the only place that knows how to walk the Telegram API. It
talks to a client object (Telethon's async API surface) and to the database
layer, and it never imports Telethon itself - which is what makes it testable
with a fake client.

Rules enforced here:

* every write goes through the idempotent ``upsert_*`` helpers, so re-running a
  collection never duplicates rows
* collections are restart safe: every run starts at the newest message and pages
  backwards, so new messages are never missed and an interrupted run can simply
  be repeated
* media is stored as metadata only (no downloads in Phase 2)
* messages are written in batches, so an interrupted run keeps what it already
  collected
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from xtg.core.errors import CollectionError, NotFoundError
from xtg.core.logger import logger
from xtg.core.utils import normalize_entity_ref
from xtg.database import queries
from xtg.database.db import Database
from xtg.database.models import Chat, Message, User
from xtg.telegram import serializers

#: Messages written per transaction while streaming the history.
BATCH_SIZE = 500

ProgressHook = Callable[[str, int, int], Awaitable[None] | None]


@dataclass(slots=True)
class CollectionReport:
    """Counters returned by every collection run."""

    chat: str = ""
    messages: int = 0
    duplicated: int = 0
    members: int = 0
    media: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.messages + self.members + self.media

    def merge(self, other: CollectionReport) -> CollectionReport:
        self.chat = self.chat or other.chat
        self.messages += other.messages
        self.duplicated += other.duplicated
        self.members += other.members
        self.media += other.media
        self.errors.extend(other.errors)
        return self


class Collector(Protocol):
    """Contract implemented by the Phase 2 collectors."""

    async def collect_messages(self, target: str, limit: int) -> CollectionReport: ...

    async def collect_members(self, target: str, limit: int) -> CollectionReport: ...

    async def collect_media(self, target: str, limit: int) -> CollectionReport: ...


class TelegramCollector:
    """Collect chats, messages, members and media metadata into SQLite."""

    def __init__(
        self,
        client: Any,
        database: Database,
        *,
        delay: float = 0.0,
        batch_size: int = BATCH_SIZE,
        progress: ProgressHook | None = None,
    ) -> None:
        self.client = client
        self.database = database
        self.delay = max(delay, 0.0)
        self.batch_size = max(batch_size, 1)
        self.progress = progress

    # --- entity resolution -------------------------------------------------
    async def resolve(self, target: str) -> Any:
        """Resolve ``@username``, a numeric id or a ``t.me`` link.

        Raises:
            NotFoundError: when Telegram does not know the entity.
            CollectionError: when Telegram refuses the request.
        """
        kind, value = normalize_entity_ref(target)
        if kind == "unknown":
            raise NotFoundError(
                f"{target!r} is not a valid Telegram reference",
                hint="Use @username, a numeric id or a t.me link.",
            )
        entity = await self._get_entity(value)
        if entity is None:
            raise NotFoundError(
                f"Telegram returned no entity for {target!r}",
                hint="Check the spelling, or make sure the account can see it.",
            )
        return entity

    async def _get_entity(self, value: str) -> Any:
        try:
            return await self.client.get_entity(value)
        except Exception as exc:  # noqa: BLE001 - mapped to a clean CLI error
            name = type(exc).__name__
            if "UsernameNotOccupied" in name or "UsernameInvalid" in name:
                raise NotFoundError(f"Telegram does not know {value!r}") from exc
            raise CollectionError(
                f"Cannot resolve {value!r}: {exc}",
                hint="Check the network/proxy and that the session is authorized.",
            ) from exc

    # --- chat --------------------------------------------------------------
    async def collect_chat(self, target: str) -> CollectionReport:
        """Store the chat metadata (id, title, username, type)."""
        entity = await self.resolve(target)
        with self.database.session() as session:
            chat = store_chat(session, **serializers.chat_fields(entity))
        report = CollectionReport(chat=_chat_label(chat))
        logger.info("Collected chat {} ({})", chat.telegram_id, report.chat)
        return report

    # --- messages ----------------------------------------------------------
    async def collect_messages(self, target: str, limit: int = 500) -> CollectionReport:
        """Store the message history of a chat.

        Every run starts at the newest message and pages backwards, so new
        messages are never missed; already stored rows are skipped, which makes
        an interrupted run safe to repeat.
        """
        entity = await self.resolve(target)
        with self.database.session() as session:
            chat = store_chat(session, **serializers.chat_fields(entity))
            known_max = queries.latest_message_id(session, chat)
        if known_max:
            logger.debug("Chat {} already holds messages up to {}", _chat_label(chat), known_max)

        report = CollectionReport(chat=_chat_label(chat))
        processed = 0
        consumed = 0
        batch: list[Any] = []
        async for message in self._message_stream(entity, limit=limit):
            batch.append(message)
            consumed += 1
            if len(batch) >= self.batch_size:
                processed += self._flush_messages(batch, chat, report)
                batch.clear()
                await self._progress("messages", processed, limit)
                await self._sleep()
            if limit > 0 and consumed >= limit:
                break
        if batch:
            processed += self._flush_messages(batch, chat, report)
        await self._progress("messages", processed, limit)
        logger.info(
            "Collected {} new messages ({} already known)", report.messages, report.duplicated
        )
        return report

    def _flush_messages(self, messages: Sequence[Any], chat: Chat, report: CollectionReport) -> int:
        """Write one batch of messages in a single transaction.

        Returns:
            The number of messages processed (new + already known).
        """
        stored = 0
        with self.database.session() as session:
            for message in messages:
                fields = serializers.message_fields(message)
                telegram_id = fields["telegram_message_id"]
                if not telegram_id:
                    continue
                try:
                    known = queries.get_message(session, chat, telegram_id)
                    sender = self._store_sender(session, message)
                    row = store_message(session, chat, **fields, sender=sender)
                    self._store_media(session, row, serializers.media_entries(message), report)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
                    report.errors.append(f"message {telegram_id}: {exc}")
                    logger.warning("Skipping message {}: {}", telegram_id, exc)
                    continue
                if known is None:
                    report.messages += 1
                else:
                    report.duplicated += 1
                stored += 1
        return stored

    def _store_sender(self, session: Any, message: Any) -> User | None:
        entity = getattr(message, "sender", None)
        if entity is None or getattr(entity, "id", None) is None:
            return None
        return store_user(session, **serializers.user_fields(entity))

    def _store_media(
        self,
        session: Any,
        message: Message,
        attachments: Sequence[dict[str, Any]],
        report: CollectionReport,
    ) -> None:
        """Add media metadata once per message (metadata only, no download)."""
        if not attachments or queries.has_media(session, message):
            return
        for attachment in attachments:
            fields = dict(attachment)
            media_type = str(fields.pop("type", "unknown") or "unknown")
            queries.add_media(session, message, media_type, **fields)
            report.media += 1

    # --- members -----------------------------------------------------------
    async def collect_members(self, target: str, limit: int = 500) -> CollectionReport:
        """Store the participant list with their role in the chat."""
        entity = await self.resolve(target)
        with self.database.session() as session:
            chat = store_chat(session, **serializers.chat_fields(entity))

        report = CollectionReport(chat=_chat_label(chat))
        processed = 0
        async for participant in self._participant_stream(entity, limit=limit):
            user = getattr(participant, "user", participant)
            if getattr(user, "id", None) is None:
                continue
            try:
                with self.database.session() as session:
                    stored = store_user(session, **serializers.user_fields(user))
                    queries.upsert_membership(session, stored, chat, _role_name(participant))
                report.members += 1
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"member {getattr(user, 'id', '?')}: {exc}")
                logger.warning("Skipping member: {}", exc)
            processed += 1
            await self._progress("members", processed, limit)
            await self._sleep()
        await self._progress("members", processed, limit)
        logger.info("Collected {} members", report.members)
        return report

    # --- media -------------------------------------------------------------
    async def collect_media(self, target: str, limit: int = 500) -> CollectionReport:
        """Media metadata is captured together with the messages (Phase 2).

        File downloads are intentionally not implemented yet, so this is the
        message walk with the media counter instead of a separate pass.
        """
        return await self.collect_messages(target, limit=limit)

    # --- orchestration -----------------------------------------------------
    async def run(
        self,
        target: str,
        *,
        sections: Sequence[str],
        limit: int = 500,
    ) -> CollectionReport:
        """Collect the requested sections and merge their reports."""
        report = CollectionReport()
        requested = set(sections)
        if requested & {"messages", "media"}:
            report.merge(await self.collect_messages(target, limit=limit))
        if "members" in requested:
            report.merge(await self.collect_members(target, limit=limit))
        return report

    # --- Telegram API helpers ---------------------------------------------
    def _message_stream(self, entity: Any, *, limit: int, offset_id: int | None = None) -> Any:
        """Async iterator over the message history, newest messages first."""
        kwargs: dict[str, Any] = {"limit": limit}
        if offset_id:
            kwargs["offset_id"] = offset_id
        return self._stream(self.client.iter_messages, entity, "message history", **kwargs)

    def _participant_stream(self, entity: Any, *, limit: int) -> Any:
        """Async iterator over the participant list."""
        return self._stream(self.client.iter_participants, entity, "member list", limit=limit)

    def _stream(self, method: Any, entity: Any, what: str, **kwargs: Any) -> Any:
        try:
            stream = method(entity, **kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped to a clean CLI error
            raise CollectionError(f"Cannot read the {what}: {exc}") from exc
        return stream if hasattr(stream, "__aiter__") else _as_async(stream)

    async def _sleep(self) -> None:
        """Pace the API calls to stay below the flood limits."""
        if self.delay:
            await asyncio.sleep(self.delay)

    async def _progress(self, section: str, processed: int, limit: int) -> None:
        if self.progress is None:
            return
        result = self.progress(section, processed, limit)
        if asyncio.iscoroutine(result):
            await result


async def _as_async(items: Any) -> Any:
    """Wrap a synchronous iterable in an async iterator (test doubles)."""
    for item in items or ():
        yield item


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


def _chat_label(chat: Chat) -> str:
    return chat.username or chat.title or str(chat.telegram_id)


def _role_name(participant: Any) -> str | None:
    """Normalise the participant role to a short lowercase name."""
    if getattr(participant, "banned_rights", None) is not None:
        return "banned"
    if getattr(participant, "admin_rights", None) is not None or getattr(
        participant, "is_admin", False
    ):
        return "admin"
    for attribute in ("creator", "owner", "bot"):
        if getattr(participant, attribute, False):
            return attribute
    return "member"
