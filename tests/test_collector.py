"""Tests for the collector: database writes, idempotency and resumability."""

from __future__ import annotations

import asyncio

import pytest
from tests.fakes import (
    FakeClient,
    FakeMedia,
    FakeMessage,
    FakeParticipant,
    FakeUser,
    make_chat,
    make_messages,
)
from xtg.core.errors import CollectionError, NotFoundError
from xtg.database import queries
from xtg.database.db import Database
from xtg.database.models import Media, User
from xtg.telegram.collector import CollectionReport, TelegramCollector


@pytest.fixture
def chat():
    return make_chat()


@pytest.fixture
def client(chat):
    return FakeClient(entities={"example": chat}, messages=make_messages(6))


def collector_for(client, database: Database, **kwargs) -> TelegramCollector:
    return TelegramCollector(client, database, batch_size=kwargs.pop("batch_size", 3), **kwargs)


# --- chat -------------------------------------------------------------------


def test_collect_chat_stores_metadata(client, database: Database, chat) -> None:
    report = asyncio.run(collector_for(client, database).collect_chat("@example"))

    assert report.chat == "example"
    with database.session() as session:
        stored = queries.resolve_chat(session, "@example")
        assert stored.telegram_id == chat.id
        assert stored.title == "Example Channel"
        assert stored.username == "example"
        assert stored.type == "channel"
        assert stored.members_count == 3
        assert stored.raw_data["title"] == "Example Channel"


def test_collect_chat_is_idempotent(client, database: Database) -> None:
    collector = collector_for(client, database)
    asyncio.run(collector.collect_chat("@example"))
    asyncio.run(collector.collect_chat("@example"))
    assert database.counts()["chats"] == 1


# --- resolution -------------------------------------------------------------


def test_resolve_unknown_username(client, database: Database) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(collector_for(client, database).resolve("@ghost"))


def test_resolve_invalid_reference(client, database: Database) -> None:
    with pytest.raises(NotFoundError):
        asyncio.run(collector_for(client, database).resolve("not a reference"))


def test_resolve_accepts_t_me_link(client, database: Database) -> None:
    entity = asyncio.run(collector_for(client, database).resolve("https://t.me/example"))
    assert entity.id == -1001234567890


def test_broken_client_maps_to_collection_error(database: Database) -> None:
    class Broken:
        def get_entity(self, *_args: object, **_kwargs: object) -> None:
            raise ConnectionError("no route to host")

    with pytest.raises(CollectionError):
        asyncio.run(collector_for(Broken(), database).resolve("@example"))


# --- messages ---------------------------------------------------------------


def test_collect_messages(client, database: Database) -> None:
    report = asyncio.run(collector_for(client, database).collect_messages("@example", limit=100))

    assert report.messages == 6
    assert report.duplicated == 0
    assert report.media == 2  # every third fake message has a photo
    counts = database.counts()
    assert counts["messages"] == 6
    assert counts["users"] == 1
    assert counts["chats"] == 1


def test_message_columns_are_mapped(client, database: Database) -> None:
    reply = type("Reply", (), {"reply_to_msg_id": 1})()
    client.messages = [
        FakeMessage(
            id=42,
            text="hello world",
            sender=FakeUser(id=7, username="alice", first_name="Alice", about="hi"),
            reply_to=reply,
            views=10,
        )
    ]
    asyncio.run(collector_for(client, database).collect_messages("@example", limit=10))

    with database.session() as session:
        chat = queries.resolve_chat(session, "@example")
        message = queries.get_message(session, chat, 42)
        assert message is not None
        assert message.text == "hello world"
        assert message.reply_to_id == 1
        assert message.views == 10
        assert message.has_media is False
        with database.session() as session:
            assert message.sender_id is not None
            sender = session.get(User, message.sender_id)
            assert sender is not None
            assert sender.telegram_id == 7
            assert sender.username == "alice"
            assert sender.bio == "hi"
        assert message.raw_data["id"] == 42


def test_recollect_does_not_duplicate(client, database: Database) -> None:
    collector = collector_for(client, database)
    asyncio.run(collector.collect_messages("@example", limit=100))

    second = asyncio.run(collector.collect_messages("@example", limit=100))

    assert second.messages == 0
    assert second.duplicated == 6
    assert database.counts()["messages"] == 6
    assert database.counts()["users"] == 1


def test_collection_is_resumable(client, database: Database) -> None:
    """An interrupted run is continued without gaps and without duplicates."""
    first = asyncio.run(collector_for(client, database).collect_messages("@example", limit=2))
    assert first.messages == 2  # the two newest messages

    second = asyncio.run(collector_for(client, database).collect_messages("@example", limit=100))

    assert second.messages == 4
    assert second.duplicated == 2
    assert database.counts()["messages"] == 6
    with database.session() as session:
        chat = queries.resolve_chat(session, "@example")
        stored = [queries.get_message(session, chat, index) for index in range(1, 7)]
        assert all(row is not None for row in stored)


def test_media_metadata_is_stored_without_path(client, database: Database) -> None:
    client.messages = [
        FakeMessage(
            id=5,
            text="with photo",
            photo=FakeMedia(file_name="a.jpg", size=2048, mime_type="image/jpeg"),
        )
    ]
    report = asyncio.run(collector_for(client, database).collect_messages("@example", limit=10))

    assert report.media == 1
    with database.session() as session:
        chat = queries.resolve_chat(session, "@example")
        message = queries.get_message(session, chat, 5)
        assert message is not None
        assert message.has_media is True
    from xtg.database.models import Media

    with database.session() as session:
        media = session.query(Media).all()  # noqa: S608 - simple ORM read
        assert len(media) == 1
        assert media[0].type == "photo"
        assert media[0].file_name == "a.jpg"
        assert media[0].file_size == 2048
        assert media[0].path is None
        assert media[0].metadata_["mime_type"] == "image/jpeg"


def test_media_not_duplicated_on_recollect(client, database: Database) -> None:
    collector = collector_for(client, database)
    asyncio.run(collector.collect_messages("@example", limit=100))
    asyncio.run(collector.collect_messages("@example", limit=100))

    with database.session() as session:
        assert session.query(Media).count() == 2  # noqa: S608 - simple ORM read


def test_limit_is_forwarded(client, database: Database) -> None:
    asyncio.run(collector_for(client, database).collect_messages("@example", limit=2))
    assert client.message_kwargs[-1]["limit"] == 2


def test_missing_stream_permission_maps_to_error(database: Database, chat) -> None:
    class NoAccess(FakeClient):
        def iter_messages(self, *_args, **_kwargs):
            raise RuntimeError("ChatAdminRequiredError")

    client = NoAccess(entities={"example": chat})
    with pytest.raises(CollectionError):
        asyncio.run(collector_for(client, database).collect_messages("@example", limit=10))


# --- members ----------------------------------------------------------------


def test_collect_members(client, database: Database, chat) -> None:
    client.participants = [
        FakeParticipant(FakeUser(id=1, username="alice"), admin_rights=object()),
        FakeParticipant(FakeUser(id=2, username="bob")),
        FakeParticipant(FakeUser(id=3, username="carol", bot=True), banned_rights=object()),
    ]

    report = asyncio.run(collector_for(client, database).collect_members("@example", limit=50))

    assert report.members == 3
    counts = database.counts()
    assert counts["users"] == 3
    assert counts["memberships"] == 3
    with database.session() as session:
        stored = queries.resolve_chat(session, "@example")
        roles = sorted(item.role for item in queries.list_chat_members(session, stored))
        assert roles == ["admin", "banned", "member"]


def test_collect_members_is_idempotent(client, database: Database) -> None:
    client.participants = [FakeParticipant(FakeUser(id=1, username="alice"))]
    collector = collector_for(client, database)
    asyncio.run(collector.collect_members("@example", limit=50))
    asyncio.run(collector.collect_members("@example", limit=50))

    counts = database.counts()
    assert counts["users"] == 1
    assert counts["memberships"] == 1


def test_collect_members_limit(client, database: Database) -> None:
    client.participants = [FakeParticipant(FakeUser(id=index)) for index in range(10)]
    report = asyncio.run(collector_for(client, database).collect_members("@example", limit=4))
    assert report.members == 4


# --- orchestration ----------------------------------------------------------


def test_run_collects_all_sections(client, database: Database) -> None:
    client.participants = [FakeParticipant(FakeUser(id=1, username="alice"))]
    report = asyncio.run(
        collector_for(client, database).run("@example", sections=["messages", "members"], limit=100)
    )

    assert report.messages == 6
    assert report.members == 1
    assert report.chat == "example"


def test_run_with_media_section_records_media(client, database: Database) -> None:
    report = asyncio.run(
        collector_for(client, database).run("@example", sections=["media"], limit=100)
    )
    assert report.media == 2
    assert report.messages == 6


def test_run_without_sections_is_a_noop(client, database: Database) -> None:
    report = asyncio.run(collector_for(client, database).run("@example", sections=[]))
    assert report.total == 0
    assert database.total_rows() == 0


def test_progress_hook_is_called(client, database: Database) -> None:
    seen: list[tuple[str, int, int]] = []
    collector = collector_for(
        client, database, progress=lambda section, done, limit: seen.append((section, done, limit))
    )
    asyncio.run(collector.collect_messages("@example", limit=10))
    assert seen
    assert all(section == "messages" for section, _, _ in seen)


def test_progress_hook_may_be_async(client, database: Database) -> None:
    seen: list[int] = []

    async def hook(section: str, done: int, limit: int) -> None:
        seen.append(done)

    collector = collector_for(client, database, progress=hook)
    asyncio.run(collector.collect_messages("@example", limit=10))
    assert seen


def test_report_merge() -> None:
    first = CollectionReport(chat="a", messages=2, media=1)
    second = CollectionReport(messages=3, members=4, errors=["boom"])
    merged = first.merge(second)

    assert merged.chat == "a"
    assert merged.messages == 5
    assert merged.members == 4
    assert merged.media == 1
    assert merged.total == 10
    assert merged.errors == ["boom"]


def test_delay_is_applied(client, database: Database) -> None:
    collector = collector_for(client, database, delay=0.001)
    report = asyncio.run(collector.collect_messages("@example", limit=3))
    assert report.messages == 3
