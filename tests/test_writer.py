"""Tests for the batch writers, checkpoints and the bulk upserts.

These cover the behaviour the per-row ORM path used to provide: idempotency,
duplicate counting, media stored once, cursor durability and the fact that
re-running updates changed values instead of silently doing nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from xtg.database import queries
from xtg.database.db import Database
from xtg.database.models import (
    CollectionCursor,
    Entity,
    Media,
    Membership,
    Message,
    RedditPost,
    RedditUser,
    User,
)
from xtg.database.reddit_writer import RedditBatchWriter
from xtg.database.writer import (
    BatchWriter,
    clean_fields,
    existing_keys,
    load_cursor,
    primary_keys,
    upsert_entities,
    upsert_relations,
)


@pytest.fixture
def chat(database: Database):
    with database.session() as session:
        return queries.upsert_chat(session, telegram_id=-1001, title="Bench", username="bench")


def _writer(database: Database, chat_id: int, **kwargs) -> BatchWriter:
    writer = BatchWriter(database, batch_size=kwargs.pop("batch_size", 100), source="telegram")
    writer.set_target(chat_id, target="@bench", **kwargs)
    return writer


# --- helpers -----------------------------------------------------------------


def test_clean_fields_drops_none_and_identity_columns() -> None:
    cleaned = clean_fields({"text": "hi", "views": None, "id": 3, "created_at": "now"})

    assert cleaned == {"text": "hi"}


def test_existing_keys_finds_stored_rows(database: Database) -> None:
    with database.session() as session:
        queries.upsert_user(session, telegram_id=7, username="alice")
        found = existing_keys(session, User.__table__, ("telegram_id",), [(7,), (8,)])

    assert found == {(7,)}


def test_existing_keys_handles_composite_keys(database: Database) -> None:
    with database.session() as session:
        chat = queries.upsert_chat(session, telegram_id=-1001)
        queries.upsert_message(session, chat, 1, text="a")

        found = existing_keys(
            session,
            Message.__table__,
            ("chat_id", "telegram_message_id"),
            [(chat.id, 1), (chat.id, 2)],
        )

    assert len(found) == 1


def test_update_set_points_at_excluded(database: Database) -> None:
    """Regression: assigning the table column compiles to ``SET x = x``.

    Two messages in different chats must each keep their own text; a no-op
    ``DO UPDATE`` would leave the second one unchanged.
    """
    with database.session() as session:
        first_chat = queries.upsert_chat(session, telegram_id=-1001)
        second_chat = queries.upsert_chat(session, telegram_id=-1002)
        queries.upsert_message(session, first_chat, 1, text="first")

    writer = _writer(database, second_chat.id, batch_size=10)
    writer.add_message({"telegram_message_id": 1, "text": "changed", "date": datetime.now(UTC)})
    writer.close()

    with database.session() as session:
        rows = set(session.execute(select(Message.text)).scalars())

    assert rows == {"first", "changed"}


def test_primary_keys_returns_ids(database: Database) -> None:
    with database.session() as session:
        queries.upsert_user(session, telegram_id=7, username="alice")
        mapping = primary_keys(session, User.__table__, ("telegram_id",), [7, 8])

    assert list(mapping) == [7]
    assert mapping[7] > 0


# --- messages ----------------------------------------------------------------


def test_batch_writer_inserts_and_counts(database: Database, chat) -> None:
    writer = _writer(database, chat.id)
    for index in range(1, 4):
        writer.add_message({"telegram_message_id": index, "text": f"m{index}"})
    result = writer.close()

    assert result.inserted == 3
    assert result.updated == 0
    assert database.counts()["messages"] == 3


def test_batch_writer_counts_duplicates_on_a_second_run(database: Database, chat) -> None:
    writer = _writer(database, chat.id)
    for index in range(1, 4):
        writer.add_message({"telegram_message_id": index, "text": f"m{index}"})
    writer.close()

    again = _writer(database, chat.id)
    for index in range(1, 4):
        again.add_message({"telegram_message_id": index, "text": f"m{index}"})
    result = again.close()

    assert result.inserted == 0
    assert result.updated == 3
    assert database.counts()["messages"] == 3


def test_batch_writer_updates_changed_values(database: Database, chat) -> None:
    first = _writer(database, chat.id)
    first.add_message({"telegram_message_id": 1, "text": "before", "views": 1})
    first.close()

    second = _writer(database, chat.id)
    second.add_message({"telegram_message_id": 1, "text": "after", "views": 99})
    second.close()

    with database.session() as session:
        row = queries.get_message(session, chat, 1)

    assert row.text == "after"
    assert row.views == 99


def test_batch_writer_resolves_senders(database: Database, chat) -> None:
    writer = _writer(database, chat.id)
    writer.add_message(
        {"telegram_message_id": 1, "text": "hi"},
        sender={"telegram_id": 7, "username": "alice", "raw_data": {}},
    )
    writer.add_message({"telegram_message_id": 2, "text": "again"})
    writer.close()

    with database.session() as session:
        assert database.counts()["users"] == 1
        row = queries.get_message(session, chat, 1)

    assert row.sender_id is not None


def test_batch_writer_stores_media_once(database: Database, chat) -> None:
    media = [{"type": "photo", "file_name": "p.jpg", "file_size": 10, "metadata": {"w": 1}}]
    for _ in range(2):
        writer = _writer(database, chat.id)
        writer.add_message({"telegram_message_id": 1, "text": "hi"}, media=media)
        writer.close()

    with database.session() as session:
        rows = list(session.execute(select(Media)))

    assert len(rows) == 1
    assert rows[0][0].file_name == "p.jpg"


def test_batch_writer_bulk_upserts_repeated_senders(database: Database, chat) -> None:
    writer = _writer(database, chat.id)
    for index in range(1, 21):
        writer.add_message(
            {"telegram_message_id": index, "text": "x"},
            sender={"telegram_id": 7, "username": "alice", "raw_data": {}},
        )
    writer.close()

    assert database.counts()["users"] == 1
    assert database.counts()["messages"] == 20


def test_full_flushes_by_batch_size(database: Database, chat) -> None:
    writer = _writer(database, chat.id, batch_size=3)
    seen = []
    for index in range(1, 8):
        writer.add_message({"telegram_message_id": index, "text": "x"})
        if writer.full:
            seen.append(writer.flush().inserted)

    assert seen == [3, 3]
    assert writer.close().inserted == 1


def test_memberships_trigger_flushes(database: Database, chat) -> None:
    """A member-only run must still batch, not accumulate one huge transaction."""
    writer = _writer(database, chat.id, batch_size=2)
    for index in range(5):
        writer.add_membership({"telegram_id": 100 + index, "username": f"u{index}"}, chat.id)

    assert writer.full
    result = writer.close()

    assert result.inserted == 5
    assert database.counts()["memberships"] == 5


def test_membership_roles_are_upserted(database: Database, chat) -> None:
    writer = _writer(database, chat.id)
    writer.add_membership({"telegram_id": 7, "username": "alice"}, chat.id, "admin")
    writer.close()

    with database.session() as session:
        row = session.scalar(select(Membership))

    assert row.role == "admin"


# --- checkpoints -------------------------------------------------------------


def test_cursor_is_written_with_the_batch(database: Database, chat) -> None:
    writer = _writer(database, chat.id, section="messages")
    writer.add_message({"telegram_message_id": 5, "text": "x"})
    writer.set_cursor("5")
    writer.close()

    with database.session() as session:
        assert load_cursor(session, "telegram", "@bench", "messages") == "5"


def test_cursor_advances_with_every_flush(database: Database, chat) -> None:
    writer = _writer(database, chat.id, batch_size=2, section="messages")
    for index in (1, 2):
        writer.add_message({"telegram_message_id": index, "text": "x"})
    writer.set_cursor("2")
    writer.flush()

    with database.session() as session:
        assert load_cursor(session, "telegram", "@bench", "messages") == "2"

    writer.add_message({"telegram_message_id": 3, "text": "x"})
    writer.set_cursor("3")
    writer.close()

    with database.session() as session:
        assert load_cursor(session, "telegram", "@bench", "messages") == "3"
        assert (
            session.scalar(select(CollectionCursor).where(CollectionCursor.source == "telegram"))
            is not None
        )


def test_cursor_is_not_written_without_one(database: Database, chat) -> None:
    writer = _writer(database, chat.id, section="messages")
    writer.add_message({"telegram_message_id": 1, "text": "x"})
    writer.close()

    with database.session() as session:
        assert load_cursor(session, "telegram", "@bench", "messages") is None


def test_cursor_rolls_back_with_a_failed_batch(database: Database, chat) -> None:
    """A checkpoint must never outlive the rows it claims."""
    writer = _writer(database, chat.id, section="messages")
    writer.add_message({"telegram_message_id": 1, "text": "x"})
    writer._messages.append({"telegram_message_id": 2, "text": "y", "bogus_column": 1})
    writer.set_cursor("2")

    with pytest.raises(Exception):  # noqa: B017 - any database error proves the point
        writer.flush()

    with database.session() as session:
        assert load_cursor(session, "telegram", "@bench", "messages") is None
    assert database.counts()["messages"] == 0


# --- relations and pivots ----------------------------------------------------


def test_relations_dedupe_within_a_batch(database: Database) -> None:
    rows = [{"source": "a", "target": "b", "relation_type": "knows"}] * 5

    with database.session() as session:
        assert upsert_relations(session, rows) == 1

    assert database.counts()["relations"] == 1


def test_relations_report_only_new_edges(database: Database) -> None:
    with database.session() as session:
        upsert_relations(session, [{"source": "a", "target": "b", "relation_type": "knows"}])
        assert (
            upsert_relations(session, [{"source": "a", "target": "b", "relation_type": "knows"}])
            == 0
        )


def test_entities_dedupe_within_a_batch(database: Database) -> None:
    rows = [{"type": "domain", "value": "example.org"}] * 3

    with database.session() as session:
        assert upsert_entities(session, rows) == 1

    assert database.counts()["entities"] == 1


def test_entity_occurrences_increase_on_a_second_pass(database: Database) -> None:
    with database.session() as session:
        upsert_entities(session, [{"type": "domain", "value": "example.org"}])
        assert upsert_entities(session, [{"type": "domain", "value": "example.org"}]) == 0

    with database.session() as session:
        row = session.scalar(select(Entity).where(Entity.value == "example.org"))

    assert row.occurrences == 2


def test_relation_and_entity_are_written_by_the_writer(database: Database, chat) -> None:
    writer = _writer(database, chat.id)
    writer.add_message(
        {"telegram_message_id": 1, "text": "x"},
        relations=[{"source": "chat:@bench", "target": "message:1", "relation_type": "contains"}],
        entities=[{"type": "domain", "value": "example.org", "first_seen_ref": "message:1"}],
    )
    writer.close()

    assert database.counts()["relations"] == 1
    assert database.counts()["entities"] == 1


# --- Reddit ------------------------------------------------------------------


def _post(reddit_id: str, **overrides):
    row = {
        "reddit_id": reddit_id,
        "title": f"title {reddit_id}",
        "body": "body",
        "url": "https://example.org/x",
        "permalink": "/r/bench/x",
        "domain": "example.org",
        "score": 1,
        "num_comments": 0,
        "created_utc": None,
        "raw_data": {},
    }
    row.update(overrides)
    return row


def test_reddit_writer_inserts_posts_and_authors(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=10, target="bench")
    for index in range(1, 4):
        writer.add_post(_post(f"t3_p{index}"), author={"name": "alice", "reddit_id": "t2_alice"})
    result = writer.close()

    assert result.inserted == 3
    assert database.counts()["reddit_posts"] == 3
    assert database.counts()["reddit_users"] == 1
    assert database.counts()["reddit_subreddits"] == 1


def test_reddit_writer_links_the_author(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=10, target="bench")
    writer.add_post(_post("t3_p1"), author={"name": "alice", "reddit_id": "t2_alice"})
    writer.close()

    with database.session() as session:
        post = session.scalar(select(RedditPost))
        user = session.scalar(select(RedditUser))

    assert post.author_id == user.id
    assert post.subreddit_id is not None


def test_reddit_writer_reuses_one_subreddit_row(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=10, target="bench")
    writer.add_post(_post("t3_p1"))
    writer.add_post(_post("t3_p2"))
    writer.close()

    assert database.counts()["reddit_subreddits"] == 1


def test_reddit_writer_updates_changed_values(database: Database) -> None:
    first = RedditBatchWriter(database, batch_size=10, target="bench")
    first.add_post(_post("t3_p1", score=1))
    first.close()

    second = RedditBatchWriter(database, batch_size=10, target="bench")
    second.add_post(_post("t3_p1", score=999))
    result = second.close()

    assert result.inserted == 0
    assert result.updated == 1
    with database.session() as session:
        assert session.scalar(select(RedditPost)).score == 999


def test_reddit_writer_deduplicates_authors(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=50, target="bench")
    for index in range(20):
        writer.add_post(_post(f"t3_p{index}"), author={"name": "alice", "reddit_id": "t2_alice"})
    writer.close()

    assert database.counts()["reddit_users"] == 1


def test_reddit_writer_flushes_by_batch_size(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=2, target="bench")
    for index in range(5):
        writer.add_post(_post(f"t3_p{index}"))
        if writer.full:
            writer.flush()
    writer.close()

    assert database.counts()["reddit_posts"] == 5
    assert writer.flushes >= 2


def test_reddit_writer_stores_the_cursor(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=10, target="bench", section="posts")
    writer.add_post(_post("t3_p1"))
    writer.set_cursor("t3_p1")
    writer.close()

    with database.session() as session:
        assert load_cursor(session, "reddit", "bench", "posts") == "t3_p1"


def test_reddit_writer_writes_graph_edges(database: Database) -> None:
    writer = RedditBatchWriter(database, batch_size=10, target="bench")
    writer.add_post(
        _post("t3_p1", title="about #security"),
        author={"name": "alice", "reddit_id": "t2_alice"},
        relations=[
            {
                "source": "reddit:user:alice",
                "target": "reddit:post:t3_p1",
                "relation_type": "authored",
            }
        ],
        entities=[{"type": "hashtag", "value": "security", "first_seen_ref": "reddit:post:t3_p1"}],
    )
    writer.close()

    assert database.counts()["relations"] == 1
    assert database.counts()["entities"] == 1
