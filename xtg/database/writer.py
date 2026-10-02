"""Batch persistence: bulk upserts and checkpoints.

The collectors originally wrote one row at a time and each row cost several
``SELECT``s plus a ``flush()``: four statement executions per message before the
commit even started. On SQLite that is pure overhead, and it is what capped
extraction throughput.

This module replaces that with two ideas:

**Bulk upserts.** :func:`upsert_messages`, :func:`upsert_media`,
:func:`upsert_relations`, :func:`upsert_entities` and
:func:`bulk_upsert_memberships` issue a single ``INSERT ... ON CONFLICT DO
UPDATE`` per batch, so one statement moves hundreds of rows. Rows that already
existed are reported back, which is what keeps duplicate counting - and
therefore resume - correct without a lookup per row.

**Batch scoped lookups.** Existing keys are fetched once per batch with a
single ``IN`` query, and parent ids (users, chats) are cached for the duration
of the batch instead of being re-selected for every message. A channel's 500
messages usually come from a handful of senders, so the old path re-queried the
same user hundreds of times.

:class:`BatchWriter` owns buffering and the transaction. It stores the cursor
(resume checkpoint) inside the same transaction as the rows it covers, so a
checkpoint can never claim progress that a rollback undid.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from xtg.core.errors import DatabaseError
from xtg.core.utils import to_utc
from xtg.database.models import (
    Chat,
    CollectionCursor,
    Entity,
    Media,
    Membership,
    Message,
    Relation,
    User,
)

#: Columns never copied from a parsed payload onto an existing row.
_SKIP_FIELDS = frozenset({"id", "created_at", "updated_at"})

#: SQLite's default limit is 999 bound parameters per statement; stay well under.
_MAX_BIND_PARAMS = 400


@dataclass(slots=True)
class WriteResult:
    """What one batch flush achieved."""

    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    media: int = 0
    relations: int = 0
    entities: int = 0

    @property
    def created(self) -> int:
        return self.inserted

    def merge(self, other: WriteResult) -> WriteResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.duplicates += other.duplicates
        self.media += other.media
        self.relations += other.relations
        self.entities += other.entities
        return self

    def to_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "media": self.media,
            "relations": self.relations,
            "entities": self.entities,
        }


# --- helpers ----------------------------------------------------------------


def clean_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Drop unset values so a bulk upsert never blanks a filled column.

    This mirrors the ORM helpers, which only assign non-``None`` fields, so
    moving to bulk writes cannot clear data an earlier run had stored.
    """
    return {
        key: value for key, value in fields.items() if key not in _SKIP_FIELDS and value is not None
    }


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """The free-form payload of a row.

    The ORM attribute is ``metadata_`` (``metadata`` is reserved by
    SQLAlchemy's declarative base) while the column key the serializers and bulk
    statements use is ``metadata``; accept either.
    """
    value = row.get("metadata")
    if value is None:
        value = row.get("metadata_")
    return dict(value) if isinstance(value, Mapping) else {}


def conflict_columns(
    table: Any, payloads: Sequence[Mapping[str, Any]], keys: Sequence[str]
) -> list[str]:
    """Columns a conflict should overwrite: every one any payload provides.

    Returns the *names*; pair them with :func:`update_set` to build the
    ``DO UPDATE SET`` clause.
    """
    present = {
        name
        for payload in payloads
        for name in payload
        if name not in keys and name not in _SKIP_FIELDS
    }
    return [name for name in sorted(present) if name in table.c]


def uniform(payloads: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Give every payload the same key set, filling the gaps with ``None``.

    A multi-row ``INSERT ... VALUES`` cannot leave a column out of one row: SQLAlchemy
    renders the omission as a post-compile bind parameter and the statement fails
    to compile. Since rows in a batch routinely differ (some messages have a
    sender, some do not), the columns are padded here.

    Only columns that accept ``NULL`` may be filled this way; callers set
    ``NOT NULL`` columns explicitly.
    """
    keys: dict[str, None] = {}
    for payload in payloads:
        keys.update(dict.fromkeys(payload))
    return [{key: payload.get(key) for key in keys} for payload in payloads]


def update_set(statement: Any, columns: Sequence[str]) -> dict[str, Any]:
    """``{column: excluded.column}`` for an ``ON CONFLICT DO UPDATE`` statement.

    The values *must* come from ``statement.excluded``: assigning a table column
    instead compiles to ``SET score = score``, which silently updates nothing.
    """
    excluded = statement.excluded
    return {name: getattr(excluded, name) for name in columns}


def chunked(values: Sequence[Any], size: int = _MAX_BIND_PARAMS) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def existing_keys(
    session: Session, table: Any, keys: Sequence[str], values: Iterable[Sequence[Any]]
) -> set[tuple[Any, ...]]:
    """Which of ``values`` already exist in ``table``? One query per chunk."""
    rows = {tuple(value) for value in values if value is not None and None not in value}
    if not rows:
        return set()
    if len(keys) == 1:
        single = keys[0]
        found: set[tuple[Any, ...]] = set()
        unique = sorted({row[0] for row in rows})
        for chunk in chunked(unique):
            statement = select(getattr(table.c, single)).where(getattr(table.c, single).in_(chunk))
            found.update((value,) for (value,) in session.execute(statement))
        return found
    found = set()
    unique = sorted(rows)
    for chunk in chunked(unique):
        columns = tuple(table.c[key] for key in keys)
        statement = select(*columns).where(tuple_(*columns).in_(chunk))
        found.update(tuple(row) for row in session.execute(statement))
    return found


def primary_keys(
    session: Session, table: Any, keys: Sequence[str], values: Iterable[Any]
) -> dict[Any, int]:
    """``key value -> id`` for the given key values, fetched in one query."""
    unique = sorted({value for value in values if value is not None})
    if not unique:
        return {}
    found: dict[Any, int] = {}
    if len(keys) == 1:
        column = getattr(table.c, keys[0])
        for chunk in chunked(unique):
            for value, primary in session.execute(
                select(column, table.c.id).where(column.in_(chunk))
            ):
                found[value] = primary
        return found
    for chunk in chunked(unique):
        columns = tuple(table.c[key] for key in keys)
        predicate = tuple_(*columns).in_([(value,) for value in chunk])
        for row in session.execute(select(*columns, table.c.id).where(predicate)):
            found[row[0]] = row[-1]
    return found


# --- bulk upserts ------------------------------------------------------------


def upsert_users(session: Session, rows: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """Bulk create/update users.

    Returns:
        ``telegram_id -> primary key`` for every row written.
    """
    payloads = [
        {
            **clean_fields(row),
            "telegram_id": int(row["telegram_id"]),
            "raw_data": row.get("raw_data") or {},
            "is_bot": bool(row.get("is_bot", False)),
        }
        for row in rows
        if row.get("telegram_id") is not None
    ]
    if not payloads:
        return {}
    table = User.__table__
    payloads = uniform(payloads)
    statement = sqlite_insert(table).values(payloads)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.telegram_id],
        set_=update_set(statement, conflict_columns(table, payloads, ("telegram_id",))),
    )
    session.execute(statement)
    return primary_keys(
        session, table, ("telegram_id",), [payload["telegram_id"] for payload in payloads]
    )


def upsert_chats(session: Session, rows: Sequence[Mapping[str, Any]]) -> dict[int, int]:
    """Bulk create/update chats. Returns ``telegram_id -> id``."""
    payloads = [
        {
            **clean_fields(row),
            "telegram_id": int(row["telegram_id"]),
            "raw_data": row.get("raw_data") or {},
        }
        for row in rows
        if row.get("telegram_id") is not None
    ]
    if not payloads:
        return {}
    table = Chat.__table__
    payloads = uniform(payloads)
    statement = sqlite_insert(table).values(payloads)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.telegram_id],
        set_=update_set(statement, conflict_columns(table, payloads, ("telegram_id",))),
    )
    session.execute(statement)
    return primary_keys(
        session, table, ("telegram_id",), [payload["telegram_id"] for payload in payloads]
    )


def upsert_messages(
    session: Session, chat_id: int, rows: Sequence[Mapping[str, Any]]
) -> WriteResult:
    """Bulk create/update the messages of one chat in a single statement.

    Rows are identified by ``(chat_id, telegram_message_id)`` - the same unique
    constraint the ORM path relied on - so repeating a collection updates rows
    instead of duplicating them, and :attr:`WriteResult.inserted` tells the
    collector how many were genuinely new.
    """
    payloads: list[dict[str, Any]] = []
    for row in rows:
        telegram_id = row.get("telegram_message_id")
        if telegram_id is None:
            continue
        payload = clean_fields(row)
        date = payload.get("date")
        payload["chat_id"] = chat_id
        payload["telegram_message_id"] = telegram_id
        payload.setdefault("has_media", False)
        payload.setdefault("raw_data", {})
        if isinstance(date, datetime):
            payload["date"] = to_utc(date)
        payloads.append(payload)
    if not payloads:
        return WriteResult()

    table = Message.__table__
    keys = ("chat_id", "telegram_message_id")
    existing = existing_keys(
        session, table, keys, [(p["chat_id"], p["telegram_message_id"]) for p in payloads]
    )
    payloads = uniform(payloads)
    statement = sqlite_insert(table).values(payloads)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.chat_id, table.c.telegram_message_id],
        set_=update_set(statement, conflict_columns(table, payloads, keys)),
    )
    session.execute(statement)

    created = sum(1 for p in payloads if (p["chat_id"], p["telegram_message_id"]) not in existing)
    return WriteResult(
        inserted=created, updated=len(payloads) - created, duplicates=len(payloads) - created
    )


def upsert_media(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert media rows for messages that do not have any yet."""
    payloads = [
        {
            "message_id": int(row["message_id"]),
            "type": row.get("type") or "unknown",
            "path": row.get("path"),
            "file_name": row.get("file_name"),
            "file_size": row.get("file_size"),
            "metadata": _metadata(row),
        }
        for row in rows
        if row.get("message_id") is not None
    ]
    if not payloads:
        return 0
    table = Media.__table__
    known = {
        value
        for (value,) in session.execute(
            select(table.c.message_id).where(
                table.c.message_id.in_(sorted({p["message_id"] for p in payloads}))
            )
        )
    }
    fresh = [payload for payload in payloads if payload["message_id"] not in known]
    if not fresh:
        return 0
    session.execute(insert(table).values(uniform(fresh)))
    return len(fresh)


def upsert_relations(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert graph edges, ignoring exact duplicates.

    Duplicates *within* the batch are collapsed before the insert - one batched
    ``INSERT`` cannot carry the same key twice - and the last weight wins, which
    is what writing the rows one by one produced.

    Returns:
        The number of distinct edges that did not exist yet.
    """
    payloads: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        if not row.get("source") or not row.get("target") or not row.get("relation_type"):
            continue
        edge = (row["source"], row["target"], row["relation_type"])
        payloads[edge] = {
            "source": row["source"],
            "target": row["target"],
            "relation_type": row["relation_type"],
            "weight": float(row.get("weight", 1.0) or 1.0),
            "metadata": _metadata(row),
        }
    if not payloads:
        return 0
    table = Relation.__table__
    keys = ("source", "target", "relation_type")
    existing = existing_keys(session, table, keys, payloads)
    fresh = [payload for edge, payload in payloads.items() if edge not in existing]
    if not fresh:
        return 0
    statement = sqlite_insert(table).values(uniform(fresh))
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.source, table.c.target, table.c.relation_type],
        set_={"weight": statement.excluded.weight},
    )
    session.execute(statement)
    return len(fresh)


def upsert_entities(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """Register pivots and count every occurrence of them.

    A pivot seen several times inside one batch is written once, and an already
    stored pivot has its counter bumped once per distinct pivot - the totals
    match the per-item path, in two statements instead of a lookup and an update
    per row.

    Returns:
        The number of pivots that were newly registered.
    """
    payloads: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        if not row.get("type") or not row.get("value"):
            continue
        pivot = (row["type"], row["value"])
        if pivot not in payloads:
            payloads[pivot] = {
                "type": row["type"],
                "value": row["value"],
                "source": row.get("source") or "telegram",
                "first_seen_id": row.get("first_seen_id"),
                "first_seen_ref": row.get("first_seen_ref"),
                "metadata": _metadata(row),
            }
    if not payloads:
        return 0
    table = Entity.__table__
    keys = ("type", "value")
    existing = existing_keys(session, table, keys, payloads)
    fresh = [payload for pivot, payload in payloads.items() if pivot not in existing]
    if fresh:
        session.execute(insert(table).values(uniform(fresh)))
    if existing:
        # One UPDATE for the whole batch instead of a SELECT plus an UPDATE per
        # pivot.
        predicate = tuple_(table.c.type, table.c.value).in_(sorted(existing))
        session.execute(update(table).where(predicate).values(occurrences=table.c.occurrences + 1))
    return len(fresh)


def upsert_memberships(session: Session, rows: Sequence[Mapping[str, Any]]) -> int:
    """Bulk create/update ``(user, chat)`` memberships. Returns new rows."""
    payloads = [
        {
            "user_id": row["user_id"],
            "chat_id": row["chat_id"],
            "role": row.get("role"),
            "raw_data": {},
        }
        for row in rows
        if row.get("user_id") is not None and row.get("chat_id") is not None
    ]
    if not payloads:
        return 0
    table = Membership.__table__
    keys = ("user_id", "chat_id")
    existing = existing_keys(session, table, keys, [(p["user_id"], p["chat_id"]) for p in payloads])
    payloads = uniform(payloads)
    statement = sqlite_insert(table).values(payloads)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.user_id, table.c.chat_id],
        set_={"role": statement.excluded.role},
    )
    session.execute(statement)
    return len(payloads) - len(existing)


# --- checkpoints -------------------------------------------------------------


def load_cursor(session: Session, source: str, target: str, section: str = "") -> str | None:
    """Last persisted cursor for a ``(source, target, section)`` triple."""
    return session.scalar(
        select(CollectionCursor.cursor).where(
            CollectionCursor.source == source,
            CollectionCursor.target == target,
            CollectionCursor.section == section,
        )
    )


def save_cursor(
    session: Session, source: str, target: str, cursor: str, *, section: str = ""
) -> None:
    """Persist a cursor inside the caller's transaction."""
    table = CollectionCursor.__table__
    statement = sqlite_insert(table).values(
        {
            "source": source,
            "target": target,
            "section": section,
            "cursor": str(cursor),
            "updated_at": datetime.now(UTC),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.source, table.c.target, table.c.section],
        set_={"cursor": statement.excluded.cursor, "updated_at": statement.excluded.updated_at},
    )
    session.execute(statement)


# --- batch writer ------------------------------------------------------------


@dataclass(slots=True)
class BatchWriter:
    """Accumulate rows, then write them in one transaction.

    The writer is synchronous on purpose: it is driven by a single consumer
    task, so keeping the database work inline makes ordering, checkpointing and
    error handling obvious instead of hiding them behind another queue.

    Usage::

        writer = BatchWriter(database, batch_size=500, source="telegram")
        writer.set_target(chat.id, target="@channel", section="messages")
        writer.add_message(fields, sender=user, media=[...])
        writer.set_cursor(str(last_id))
        if writer.full:
            writer.flush()
    """

    database: Any
    batch_size: int = 500
    source: str = ""
    target: str = ""
    section: str = ""
    totals: WriteResult = field(default_factory=WriteResult)
    flushes: int = 0
    rows_in: int = 0
    #: Wall time spent inside :meth:`flush`, for the "database is the
    #: bottleneck" report.
    write_seconds: float = 0.0

    _chat_id: int | None = None
    _messages: list[dict[str, Any]] = field(default_factory=list)
    _senders: dict[int, dict[str, Any]] = field(default_factory=dict)
    _sender_of: dict[Any, int] = field(default_factory=dict)
    _media: list[dict[str, Any]] = field(default_factory=list)
    _relations: list[dict[str, Any]] = field(default_factory=list)
    _entities: list[dict[str, Any]] = field(default_factory=list)
    _memberships: list[dict[str, Any]] = field(default_factory=list)
    _pending_cursor: str | None = None

    def __post_init__(self) -> None:
        self.batch_size = max(1, self.batch_size)

    # --- accumulation -------------------------------------------------------
    def set_target(self, chat_pk: int, *, target: str = "", section: str = "") -> None:
        """Bind the writer to one chat before rows arrive.

        Args:
            chat_pk: the ``chats.id`` primary key, *not* the Telegram id -
                ``messages.chat_id`` is a foreign key.
        """
        self._chat_id = chat_pk
        if target:
            self.target = target
        if section:
            self.section = section

    def add_message(
        self,
        fields: Mapping[str, Any],
        *,
        sender: Mapping[str, Any] | None = None,
        media: Sequence[Mapping[str, Any]] = (),
        relations: Sequence[Mapping[str, Any]] = (),
        entities: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Queue one message plus its sender, media, edges and pivots."""
        row = dict(fields)
        self._messages.append(row)
        if sender and sender.get("telegram_id") is not None:
            telegram_id = int(sender["telegram_id"])
            self._senders[telegram_id] = dict(sender)
            row["_sender_telegram_id"] = telegram_id
        for attachment in media:
            entry = dict(attachment)
            entry["telegram_message_id"] = row.get("telegram_message_id")
            self._media.append(entry)
        for relation in relations:
            self._relations.append(dict(relation))
        for entity in entities:
            self._entities.append(dict(entity))

    def add_user(self, user: Mapping[str, Any]) -> None:
        if user.get("telegram_id") is not None:
            self._senders[int(user["telegram_id"])] = dict(user)

    def add_membership(
        self, user: Mapping[str, Any], chat_id: int, role: str | None = None
    ) -> None:
        """Queue a membership; the user row is queued with it."""
        telegram_id = user.get("telegram_id")
        if telegram_id is not None:
            self._senders[int(telegram_id)] = dict(user)
        self._memberships.append(
            {
                "_telegram_id": None if telegram_id is None else int(telegram_id),
                "chat_id": chat_id,
                "role": role,
            }
        )

    def set_cursor(self, cursor: Any | None) -> None:
        """Record the progress marker to store with the next flush."""
        if cursor is not None:
            self._pending_cursor = str(cursor)

    # --- inspection ---------------------------------------------------------
    @property
    def pending(self) -> int:
        """Primary rows buffered, i.e. the rows that drive the batch size.

        Memberships count here too: a member-only collection never queues
        messages, and without this it would grow one unbounded transaction
        instead of flushing every ``batch_size`` members.
        """
        return len(self._messages) + len(self._memberships)

    @property
    def depth(self) -> int:
        """Every buffered row, including senders, media and graph rows."""
        return (
            len(self._messages)
            + len(self._memberships)
            + len(self._senders)
            + len(self._media)
            + len(self._relations)
            + len(self._entities)
        )

    @property
    def full(self) -> bool:
        return self.pending >= self.batch_size

    @property
    def empty(self) -> bool:
        return self.depth == 0 and self._pending_cursor is None

    # --- flushing -----------------------------------------------------------
    def flush(self) -> WriteResult:
        """Write everything buffered in one transaction.

        Raises:
            DatabaseError: when the transaction fails; the buffer is dropped so
                a poisoned batch cannot be retried forever.
        """
        if self.empty:
            return WriteResult()
        import time

        started = time.perf_counter()
        result = WriteResult()
        try:
            with self.database.session() as session:
                user_ids = upsert_users(session, list(self._senders.values()))
                if self._messages and self._chat_id is not None:
                    for row in self._messages:
                        sender_id = user_ids.get(row.pop("_sender_telegram_id", None))
                        if sender_id is not None:
                            row["sender_id"] = sender_id
                    result.merge(upsert_messages(session, self._chat_id, self._messages))
                    if self._media:
                        result.media += self._attach_media(session)
                if self._memberships:
                    resolved = [
                        {**row, "user_id": user_ids.get(row.get("_telegram_id"))}
                        for row in self._memberships
                    ]
                    result.inserted += upsert_memberships(session, resolved)
                result.relations += upsert_relations(session, self._relations)
                result.entities += upsert_entities(session, self._entities)
                if self._pending_cursor is not None:
                    save_cursor(
                        session,
                        self.source,
                        self.target,
                        self._pending_cursor,
                        section=self.section,
                    )
                    self._pending_cursor = None
        except DatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise DatabaseError(f"Batch write failed: {exc}") from exc
        finally:
            self.rows_in += self.depth
            self.write_seconds += time.perf_counter() - started
            self._reset()
        self.flushes += 1
        self.totals.merge(result)
        return result

    def _attach_media(self, session: Session) -> int:
        """Resolve message primary keys and insert the media rows."""
        if self._chat_id is None:
            return 0
        table = Message.__table__
        telegram_ids = sorted(
            {row["telegram_message_id"] for row in self._messages if row.get("telegram_message_id")}
        )
        if not telegram_ids:
            return 0
        mapping: dict[Any, int] = {}
        for chunk in chunked(telegram_ids):
            statement = select(table.c.telegram_message_id, table.c.id).where(
                table.c.chat_id == self._chat_id,
                table.c.telegram_message_id.in_(chunk),
            )
            for telegram_id, primary in session.execute(statement):
                mapping[telegram_id] = primary
        rows = [
            {**attachment, "message_id": mapping[attachment["telegram_message_id"]]}
            for attachment in self._media
            if attachment.get("telegram_message_id") in mapping
        ]
        return upsert_media(session, rows)

    def _reset(self) -> None:
        self._messages.clear()
        self._senders.clear()
        self._sender_of.clear()
        self._media.clear()
        self._relations.clear()
        self._entities.clear()
        self._memberships.clear()

    def close(self) -> WriteResult:
        """Flush what is left; safe to call twice."""
        return self.flush()


def message_count(session: Session, chat_id: int) -> int:
    """Messages stored for one chat (used by benchmarks and the CLI)."""
    return session.scalar(select(func.count(Message.id)).where(Message.chat_id == chat_id)) or 0
