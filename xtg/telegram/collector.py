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
* rows are written in batches through :class:`~xtg.database.writer.BatchWriter`,
  which stores the resume cursor in the same transaction as the rows it covers.
  An interrupted run therefore resumes from the last *committed* position instead
  of re-walking the whole history.

The streaming side is a producer/consumer pair: one task reads the API while a
bounded queue buffers parsed rows. The queue is what stops a fast producer from
buffering an entire channel in memory, and it lets the two sides overlap so a
database commit does not stall the network read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from xtg.core.errors import CollectionError, NotFoundError
from xtg.core.logger import logger
from xtg.core.metrics import Metrics
from xtg.core.retry import RetryPolicy, classify, retry_after_of
from xtg.core.utils import normalize_entity_ref
from xtg.database import queries
from xtg.database.db import Database
from xtg.database.models import Chat, Message, User
from xtg.database.writer import BatchWriter, WriteResult, load_cursor
from xtg.pipeline import BoundedQueue
from xtg.telegram import serializers

#: Messages written per transaction while streaming the history.
BATCH_SIZE = 500

#: Parsed rows buffered between the reader and the writer.
QUEUE_SIZE = 1000

#: Depth at which the producer counts as being throttled by the writer.
QUEUE_HIGH_WATER = 0.8

ProgressHook = Callable[[str, int, int], Awaitable[None] | None]

#: Marks the end of the producer's stream.
_SENTINEL = object()


def _message_id(message: Any) -> int | None:
    """The upstream id of a raw message, without full serialization."""
    value = getattr(message, "id", None)
    return int(value) if value is not None else None


def _parsed_row(
    message: Any,
) -> tuple[int, dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """Serialize one API message into what the writer needs.

    Serialization happens on the producer side, which keeps the consumer's
    critical path down to a buffer append. It also means the raw Telegram object
    never crosses the queue, so a large ``raw_data`` payload is released as soon
    as the row is built.
    """
    fields = serializers.message_fields(message)
    telegram_id = int(fields.get("telegram_message_id") or 0)
    entity = getattr(message, "sender", None)
    sender = (
        serializers.user_fields(entity)
        if entity is not None and getattr(entity, "id", None) is not None
        else None
    )
    return telegram_id, fields, sender, serializers.media_entries(message)


@dataclass(slots=True)
class CollectionReport:
    """Counters returned by every collection run."""

    chat: str = ""
    messages: int = 0
    duplicated: int = 0
    members: int = 0
    media: int = 0
    #: How far the run got, stored as the resume cursor.
    cursor: str | None = None
    #: Batches committed during the run.
    batches: int = 0
    #: Set when rows were dropped because the producer outran the consumer.
    backpressure_drops: int = 0
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
        self.batches += other.batches
        self.backpressure_drops += other.backpressure_drops
        self.cursor = other.cursor or self.cursor
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
        queue_size: int = QUEUE_SIZE,
        progress: ProgressHook | None = None,
        metrics: Metrics | None = None,
        retry_policy: RetryPolicy | None = None,
        resume: bool = False,
    ) -> None:
        self.client = client
        self.database = database
        self.delay = max(delay, 0.0)
        self.batch_size = max(batch_size, 1)
        self.queue_size = max(queue_size, 1)
        self.progress = progress
        self.metrics = metrics or Metrics()
        self.retry_policy = retry_policy or RetryPolicy()
        #: When enabled a run continues after the stored cursor instead of
        #: re-walking the whole history. Off by default so a repeated collection
        #: still refreshes views and edits, as it always has.
        self.resume = resume

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

        The history is read by one task and written by another, connected by a
        bounded queue: the network read and the database commit overlap, and
        neither side can run away with the other's memory.

        By default the walk still starts at the newest message, so edits and view
        counts on already stored messages are refreshed. With ``resume=True`` the
        run continues after the stored cursor instead, which is what makes a
        repeat run cheap on a large channel.
        """
        entity = await self.resolve(target)
        label = ""
        with self.database.session() as session:
            chat = store_chat(session, **serializers.chat_fields(entity))
            label = _chat_label(chat)
            known_max = queries.latest_message_id(session, chat)
            cursor = load_cursor(session, "telegram", target, "messages") if self.resume else None
        start_after = self._resume_point(known_max, cursor) if self.resume else None
        if start_after:
            logger.debug("Chat {} already holds messages up to {}", label, start_after)

        report = CollectionReport(chat=label)
        writer = BatchWriter(
            self.database,
            batch_size=self.batch_size,
            source="telegram",
            target=target,
            section="messages",
        )
        writer.set_target(chat.id, target=target, section="messages")

        queue: BoundedQueue[Any] = BoundedQueue(self.queue_size)
        failure: list[BaseException] = []

        async def produce() -> None:
            """Read the history and enqueue parsed rows, resuming on error.

            When the queue is full the producer *waits*: that is the
            backpressure, and it keeps memory bounded without ever discarding a
            message. A transient failure (a dropped connection, a flood wait)
            restarts the backwards walk from the last id actually enqueued, so a
            long collection does not restart from the top of the channel.
            """
            try:
                consumed = 0
                offset = start_after
                attempt = 0
                while True:
                    try:
                        async for message in self._message_stream(
                            entity, limit=limit, offset_id=offset
                        ):
                            if limit > 0 and consumed >= limit:
                                return
                            consumed += 1
                            offset = _message_id(message) or offset
                            self.metrics.incr("messages_read")
                            if queue.depth >= self.queue_size * QUEUE_HIGH_WATER:
                                self.metrics.incr("backpressure_waits")
                            # Blocks while the writer catches up: bounded memory,
                            # no dropped rows.
                            await queue.put(_parsed_row(message))
                            await self._sleep()
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - classified below
                        kind = classify(exc)
                        if not self.retry_policy.allows(attempt, kind):
                            raise
                        delay = self.retry_policy.delay_for(
                            attempt, server_delay=retry_after_of(exc)
                        )
                        attempt += 1
                        self.metrics.incr("retries")
                        logger.warning(
                            "Message stream failed ({}), retry {} in {:.1f}s from offset {}",
                            kind,
                            attempt,
                            delay,
                            offset,
                        )
                        await asyncio.sleep(delay)
            except BaseException as exc:  # noqa: BLE001 - handed to the consumer
                failure.append(exc)
            finally:
                await queue.put(_SENTINEL)

        async def consume() -> None:
            """Drain the queue into batches, committing as it goes."""
            newest = 0
            processed = 0
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    queue.task_done()
                    break
                telegram_id, fields, sender, media = item
                try:
                    writer.add_message(fields, sender=sender, media=media)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
                    report.errors.append(f"message {telegram_id}: {exc}")
                    logger.warning("Skipping message {}: {}", telegram_id, exc)
                    queue.task_done()
                    continue
                newest = max(newest, telegram_id)
                processed += 1
                if writer.full:
                    # The cursor rides along with the rows it covers, so a crash
                    # can never claim progress that was rolled back.
                    writer.set_cursor(str(newest))
                    result = writer.flush()
                    self._account(result, report)
                    await self._progress("messages", processed, limit)
                queue.task_done()
            writer.set_cursor(str(newest) if newest else None)
            result = writer.close()
            self._account(result, report)
            report.cursor = newest or None
            await self._progress("messages", processed, limit)

        await asyncio.gather(produce(), consume())
        if failure:
            raise failure[0]
        logger.info(
            "Collected {} new messages ({} already known) in {} batches",
            report.messages,
            report.duplicated,
            report.batches,
        )
        return report

    @staticmethod
    def _resume_point(known_max: int | None, cursor: str | None) -> int | None:
        """Where to restart the backwards walk.

        The cursor is authoritative when it is at least as new as what the
        messages table holds; otherwise fall back to the stored maximum.
        """
        points = [int(value) for value in (known_max, cursor) if value is not None]
        return max(points) if points else None

    def _account(self, result: WriteResult, report: CollectionReport) -> None:
        """Fold one flushed batch into the report and the metrics."""
        report.messages += result.inserted
        report.duplicated += result.updated
        report.media += result.media
        report.batches += 1
        self.metrics.incr("messages_written", result.inserted)
        self.metrics.incr("messages_updated", result.updated)
        self.metrics.incr("media_written", result.media)
        self.metrics.observe("batch_rows", report.duplicated + report.messages)

    # --- members -----------------------------------------------------------
    async def collect_members(self, target: str, limit: int = 500) -> CollectionReport:
        """Store the participant list with their role in the chat.

        Memberships are buffered and written in batches: the old path opened a
        transaction per member, which for a large group meant thousands of
        commits for the same handful of rows.
        """
        entity = await self.resolve(target)
        with self.database.session() as session:
            chat = store_chat(session, **serializers.chat_fields(entity))

        report = CollectionReport(chat=_chat_label(chat))
        writer = BatchWriter(
            self.database,
            batch_size=self.batch_size,
            source="telegram",
            target=target,
            section="members",
        )
        writer.set_target(chat.id, target=target, section="members")

        processed = 0
        async for participant in self._participant_stream(entity, limit=limit):
            user = getattr(participant, "user", participant)
            if getattr(user, "id", None) is not None:
                try:
                    writer.add_membership(
                        serializers.user_fields(user), chat.id, _role_name(participant)
                    )
                except Exception as exc:  # noqa: BLE001 - one bad member must not stop the run
                    report.errors.append(f"member {getattr(user, 'id', '?')}: {exc}")
                    logger.warning("Skipping member: {}", exc)
            processed += 1
            if writer.full:
                writer.set_cursor(str(processed))
                report.members += writer.flush().inserted
                report.batches += 1
            await self._progress("members", processed, limit)
            await self._sleep()
        writer.set_cursor(str(processed))
        report.members += writer.close().inserted
        report.batches += 1
        report.cursor = processed or None
        self.metrics.incr("members_written", report.members)
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
