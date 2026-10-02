"""Batch persistence for the Reddit source.

The Reddit collector stored one submission at a time, and every submission cost
a handful of round trips: look up the subreddit, look up the author, insert or
update the author, insert or update the post, then two relation edges and one
pivot entity per hashtag or domain. Benchmarked offline that path sustains only
a few hundred rows per second - by far the slowest part of a collection.

This module collapses the same work into a handful of statements per batch:

* authors are deduplicated and upserted once (a listing of 200 submissions
  usually has far fewer distinct authors),
* subreddit, posts and comments go in through bulk upserts,
* relations and pivots are resolved batch-wide and inserted once,
* the ``after`` cursor is stored in the same transaction as the rows it covers.

Semantics are unchanged: rows stay idempotent, already-known submissions are
reported as duplicates rather than inserted again, and counters such as a post's
score are refreshed on a re-run.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from xtg.core.errors import DatabaseError
from xtg.core.utils import to_utc
from xtg.database.db import Database
from xtg.database.models import (
    RedditComment,
    RedditPost,
    RedditSubreddit,
    RedditUser,
    Relation,
)
from xtg.database.writer import (
    WriteResult,
    clean_fields,
    conflict_columns,
    existing_keys,
    load_cursor,
    primary_keys,
    save_cursor,
    uniform,
    update_set,
    upsert_entities,
    upsert_relations,
)


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    return None


def _upsert(session: Session, table: Any, payloads: Sequence[Mapping[str, Any]], key: str) -> None:
    """One ``INSERT ... ON CONFLICT DO UPDATE`` for the whole batch."""
    statement = sqlite_insert(table).values(uniform(payloads))
    statement = statement.on_conflict_do_update(
        index_elements=[table.c[key]],
        set_=update_set(statement, conflict_columns(table, payloads, (key,))),
    )
    session.execute(statement)


@dataclass(slots=True)
class RedditBatchWriter:
    """Buffer parsed Reddit items and write them in one transaction.

    Mirrors :class:`~xtg.database.writer.BatchWriter` for the Reddit tables::

        writer = RedditBatchWriter(database, batch_size=200, target="linux")
        writer.add_post(parsed)  # parsed dict with kind="post"
        if writer.full:
            writer.flush()
        writer.close()
    """

    database: Any
    batch_size: int = 200
    target: str = ""
    section: str = "posts"
    totals: WriteResult = field(default_factory=WriteResult)
    flushes: int = 0
    rows_in: int = 0
    write_seconds: float = 0.0

    _posts: list[dict[str, Any]] = field(default_factory=list)
    _comments: list[dict[str, Any]] = field(default_factory=list)
    _users: dict[str, dict[str, Any]] = field(default_factory=dict)
    _relations: list[dict[str, Any]] = field(default_factory=list)
    _entities: list[dict[str, Any]] = field(default_factory=list)
    _pending_cursor: str | None = None

    def __post_init__(self) -> None:
        self.batch_size = max(1, self.batch_size)

    # --- accumulation -------------------------------------------------------
    def add_post(
        self,
        fields: Mapping[str, Any],
        *,
        author: Mapping[str, Any] | None = None,
        relations: Sequence[Mapping[str, Any]] = (),
        entities: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Queue a submission plus its author, edges and pivots.

        ``subreddit`` is *not* stored on the row: the writer resolves the
        subreddit's primary key once per batch and writes ``subreddit_id``.
        """
        row = dict(fields)
        if author:
            name = author.get("name")
            if name:
                self._users[str(name)] = dict(author)
                row["author"] = str(name)
        self._posts.append(row)
        self._extend(relations, entities)

    def add_comment(
        self,
        fields: Mapping[str, Any],
        *,
        author: Mapping[str, Any] | None = None,
        relations: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Queue a comment plus its author and edges."""
        row = dict(fields)
        if author:
            name = author.get("name")
            if name:
                self._users[str(name)] = dict(author)
                row["author"] = str(name)
        self._comments.append(row)
        self._extend(relations)

    def add_user(self, user: Mapping[str, Any]) -> None:
        name = user.get("name")
        if name:
            self._users[str(name)] = dict(user)

    def add_relations(self, relations: Sequence[Mapping[str, Any]]) -> None:
        self._extend(relations)

    def add_entities(self, entities: Sequence[Mapping[str, Any]]) -> None:
        self._extend(entities=entities)

    def _extend(
        self,
        relations: Sequence[Mapping[str, Any]] = (),
        entities: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        for relation in relations:
            self._relations.append(dict(relation))
        for entity in entities:
            self._entities.append(dict(entity))

    def set_cursor(self, cursor: Any | None) -> None:
        if cursor is not None:
            self._pending_cursor = str(cursor)

    # --- inspection ---------------------------------------------------------
    @property
    def depth(self) -> int:
        return len(self._posts) + len(self._comments) + len(self._users)

    @property
    def full(self) -> bool:
        return len(self._posts) + len(self._comments) >= self.batch_size

    @property
    def empty(self) -> bool:
        return self.depth == 0 and self._pending_cursor is None

    # --- flushing -----------------------------------------------------------
    def flush(self) -> WriteResult:
        """Write the buffered batch in one transaction."""
        if self.empty:
            return WriteResult()
        started = time.perf_counter()
        result = WriteResult()
        try:
            with self.database.session() as session:
                user_ids = self._flush_users(session)
                subreddit_id = self._flush_subreddit(session)
                post_ids = self._flush_posts(session, subreddit_id, user_ids, result)
                self._flush_comments(session, post_ids, user_ids, result)
                result.relations += upsert_relations(session, self._relations)
                result.entities += upsert_entities(session, self._entities)
                if self._pending_cursor is not None:
                    save_cursor(
                        session, "reddit", self.target, self._pending_cursor, section=self.section
                    )
                    self._pending_cursor = None
        except DatabaseError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise DatabaseError(f"Reddit batch write failed: {exc}") from exc
        finally:
            self.rows_in += self.depth
            self.write_seconds += time.perf_counter() - started
            self._reset()
        self.flushes += 1
        self.totals.merge(result)
        return result

    def _flush_users(self, session: Session) -> dict[str, int]:
        """Upsert the batch's authors once; returns ``user name -> id``.

        Keyed by ``name`` because that is how the parsed payloads reference an
        author (``t2_`` fullnames are not always exposed).
        """
        if not self._users:
            return {}
        payloads = [
            {
                **clean_fields(user),
                "reddit_id": str(user.get("reddit_id") or f"t2_{name}"),
                "name": name,
                "created_utc": _utc(user.get("created_utc")),
            }
            for name, user in self._users.items()
        ]
        table = RedditUser.__table__
        _upsert(session, table, payloads, "reddit_id")
        return primary_keys(session, table, ("name",), [p["name"] for p in payloads])

    def _flush_subreddit(self, session: Session) -> int | None:
        """Make sure the subreddit row exists; returns its primary key."""
        if not self.target:
            return None
        name = self.target
        table = RedditSubreddit.__table__
        existing = session.scalar(select(table.c.id).where(table.c.name == name).limit(1))
        if existing is not None:
            return existing
        session.execute(
            insert(table).values(
                {
                    "reddit_id": f"t5_{name}",
                    "name": name,
                    "title": f"r/{name}",
                    "raw_data": {},
                }
            )
        )
        return session.scalar(select(table.c.id).where(table.c.name == name).limit(1))

    def _flush_posts(
        self,
        session: Session,
        subreddit_id: int | None,
        user_ids: dict[str, int],
        result: WriteResult,
    ) -> dict[str, int]:
        if not self._posts:
            return {}
        table = RedditPost.__table__
        payloads = []
        for row in self._posts:
            payload = clean_fields(row)
            payload["reddit_id"] = str(row["reddit_id"])
            payload["subreddit_id"] = subreddit_id
            payload["title"] = str(row.get("title") or "")[:512]
            payload["created_utc"] = _utc(row.get("created_utc"))
            author = payload.pop("author", None)
            if author:
                payload["author_id"] = user_ids.get(str(author))
            payloads.append(payload)
        keys = ("reddit_id",)
        existing = existing_keys(session, table, keys, [(p["reddit_id"],) for p in payloads])
        _upsert(session, table, payloads, "reddit_id")
        created = sum(1 for p in payloads if (p["reddit_id"],) not in existing)
        result.inserted += created
        result.updated += len(payloads) - created
        return primary_keys(session, table, ("reddit_id",), [p["reddit_id"] for p in payloads])

    def _flush_comments(
        self,
        session: Session,
        post_ids: dict[str, int],
        user_ids: dict[str, int],
        result: WriteResult,
    ) -> None:
        if not self._comments:
            return
        table = RedditComment.__table__
        payloads = []
        for row in self._comments:
            payload = clean_fields(row)
            payload["reddit_id"] = str(row["reddit_id"])
            payload["created_utc"] = _utc(row.get("created_utc"))
            payload["post_id"] = post_ids.get(str(payload.pop("post", "")))
            author = payload.pop("author", None)
            if author:
                payload["author_id"] = user_ids.get(str(author))
            payloads.append(payload)
        existing = existing_keys(
            session, table, ("reddit_id",), [(p["reddit_id"],) for p in payloads]
        )
        _upsert(session, table, payloads, "reddit_id")
        created = sum(1 for p in payloads if (p["reddit_id"],) not in existing)
        result.inserted += created
        result.updated += len(payloads) - created

    def _reset(self) -> None:
        self._posts.clear()
        self._comments.clear()
        self._users.clear()
        self._relations.clear()
        self._entities.clear()

    def close(self) -> WriteResult:
        return self.flush()


def load_reddit_cursor(database: Database, target: str, section: str = "posts") -> str | None:
    """Stored ``after`` token of a subreddit listing."""
    with database.session() as session:
        return load_cursor(session, "reddit", target, section)


__all__ = [
    "RedditBatchWriter",
    "Relation",
    "WriteResult",
    "load_reddit_cursor",
]
