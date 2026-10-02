"""Tests for the database layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from xtg.database import queries
from xtg.database.db import TABLE_NAMES, Database
from xtg.database.models import Chat, Entity, Message, Relation, User


def test_schema_creation_is_idempotent(database: Database) -> None:
    database.create_all()
    assert set(TABLE_NAMES).issubset(set(database.table_names()))


def test_counts_start_at_zero(database: Database) -> None:
    counts = database.counts()
    assert set(counts) == set(TABLE_NAMES)
    assert all(value == 0 for value in counts.values())
    assert database.total_rows() == 0


def test_upsert_user_is_idempotent(database: Database) -> None:
    with database.session() as session:
        first = queries.upsert_user(session, 42, username="durov", first_name="Pavel")
        second = queries.upsert_user(session, 42, username="durov", last_name="Durov")
    assert first.id == second.id
    with database.session() as session:
        assert queries.count_rows(session, User) == 1
        stored = queries.get_user_by_telegram_id(session, 42)
        assert stored.last_name == "Durov"
        assert stored.first_name == "Pavel"


def test_upsert_chat_and_resolve(database: Database) -> None:
    with database.session() as session:
        queries.upsert_chat(session, -1001, title="News", username="news", type="channel")
    with database.session() as session:
        assert queries.resolve_chat(session, "@news").title == "News"
        assert queries.resolve_chat(session, "-1001").username == "news"


def test_resolve_unknown_chat_raises(database: Database) -> None:
    from xtg.core.errors import NotFoundError

    with database.session() as session, pytest.raises(NotFoundError):
        queries.resolve_chat(session, "@nothing")


def test_upsert_message_does_not_duplicate(database: Database) -> None:
    with database.session() as session:
        chat = queries.upsert_chat(session, -1001, title="News")
        sender = queries.upsert_user(session, 7, username="alice")
        queries.upsert_message(
            session,
            chat,
            100,
            sender=sender,
            text="hello",
            date=datetime.now(UTC),
        )
        queries.upsert_message(session, chat, 100, sender=sender, text="hello world")
        assert queries.count_rows(session, Message) == 1
        assert queries.count_chat_messages(session, chat) == 1
        assert queries.count_rows(session, User) == 1


def test_membership_upsert(database: Database) -> None:
    with database.session() as session:
        chat = queries.upsert_chat(session, -1001, title="Group")
        user = queries.upsert_user(session, 7, username="alice")
        queries.upsert_membership(session, user, chat, "member")
        queries.upsert_membership(session, user, chat, "admin")
    with database.session() as session:
        memberships = queries.list_chat_members(session, queries.resolve_chat(session, "-1001"))
        assert len(memberships) == 1
        assert memberships[0].role == "admin"


def test_message_search(database: Database) -> None:
    with database.session() as session:
        chat = queries.upsert_chat(session, -1001, title="News")
        queries.upsert_message(session, chat, 1, text="Bitcoin is up")
        queries.upsert_message(session, chat, 2, text="Nothing here")
    with database.session() as session:
        results = queries.search_messages(session, queries.build_message_search("bitcoin"))
        assert [row.telegram_message_id for row in results] == [1]


def test_message_search_by_id_and_filter(database: Database) -> None:
    with database.session() as session:
        chat = queries.upsert_chat(session, -1001, title="News")
        queries.upsert_message(session, chat, 55, text="x")
    with database.session() as session:
        assert len(queries.search_messages(session, queries.build_message_search("55"))) == 1
        assert not queries.search_messages(session, queries.build_message_search("999"))
        stored = queries.resolve_chat(session, "-1001")
        assert len(queries.search_messages(session, queries.build_message_search("x", chat=stored)))


def test_entity_and_relation_upsert(database: Database) -> None:
    with database.session() as session:
        entity = queries.upsert_entity(session, "email", "a@example.org")
        first_occurrences = entity.occurrences
        queries.upsert_entity(session, "email", "a@example.org")
        queries.add_relation(session, "chat:-1001", "entity:email:a@example.org", "mentions")
        queries.add_relation(session, "chat:-1001", "entity:email:a@example.org", "mentions")
    assert first_occurrences == 1
    with database.session() as session:
        entities = queries.search_entities(session, "email", "example")
        assert len(entities) == 1
        assert entities[0].occurrences == 2
        assert queries.count_rows(session, Entity) == 1
        assert queries.count_rows(session, Relation) == 1


def test_reset_clears_rows(database: Database) -> None:
    with database.session() as session:
        queries.upsert_chat(session, -1001, title="News")
    assert database.total_rows() > 0
    database.reset()
    assert database.total_rows() == 0
    assert set(TABLE_NAMES).issubset(set(database.table_names()))


def test_to_dict_roundtrip(database: Database) -> None:
    with database.session() as session:
        chat = queries.upsert_chat(session, -1001, title="News")
        payload = chat.to_dict()
    assert payload["telegram_id"] == -1001
    assert isinstance(chat, Chat)


def test_session_rollback_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError), database.session() as session:
        queries.upsert_chat(session, -1001, title="News")
        session.add(Chat(telegram_id=-1002, title=None, type=None, raw_data=None))
        raise RuntimeError("boom")
    assert database.total_rows() == 0
