"""Tests for the Telegram source facade, above all its event loop ownership.

Telethon binds a client to the loop it connected on and raises
``the asyncio event loop must not change after connection`` otherwise. The
adapter used to drive ``open()``, the walk and ``close()`` through three
separate :func:`asyncio.run` calls - three fresh, torn-down loops - so every
``xtg collect`` died on the first API call. The doubles below reproduce that
constraint so a regression fails here instead of against the real network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.fakes import (
    FakeParticipant,
    FakeUser,
    LoopBoundClient,
    make_chat,
    make_messages,
)
from xtg.core.config import Settings
from xtg.core.errors import AuthError, CollectionError
from xtg.database.db import Database
from xtg.sources.telegram import TelegramCollectorAdapter, TelegramSource


class FakeConnection:
    """Stand-in for :class:`TelegramConnection` that reports the loop it ran on."""

    instances: list[FakeConnection] = []

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        require_authorized: bool = True,
        authorized: bool = True,
        **options: Any,
    ) -> None:
        self.settings = settings
        self.require_authorized = require_authorized
        self.client = LoopBoundClient(
            authorized=authorized,
            entities={"example": make_chat()},
            messages=make_messages(3),
            participants=[FakeParticipant(user=FakeUser(id=8, username="bob"))],
        )
        self.opened_on: asyncio.AbstractEventLoop | None = None
        self.closed_on: asyncio.AbstractEventLoop | None = None
        self.close_error: Exception | None = None
        self.instances.append(self)

    async def open(self) -> LoopBoundClient:
        self.opened_on = asyncio.get_running_loop()
        client = await self.client.connect()
        if self.require_authorized and not await client.is_user_authorized():
            await client.disconnect()
            raise AuthError("The Telegram session is not authorized", hint="Run `xtg auth login`.")
        return client

    async def close(self) -> None:
        self.closed_on = asyncio.get_running_loop()
        if self.close_error is not None:
            raise self.close_error
        await self.client.disconnect()


@pytest.fixture
def telegram_settings(settings: Settings) -> Settings:
    settings.api_id = 12345
    settings.api_hash = "hash"
    settings.request_delay = 0.0
    return settings


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnection]:
    """Replace the real connection so no credentials, session or network are needed."""
    FakeConnection.instances = []
    monkeypatch.setattr("xtg.sources.telegram.source.TelegramConnection", FakeConnection)
    return FakeConnection


@pytest.fixture
def unauthorized(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnection]:
    """A connection whose session exists but was never signed in."""

    class UnauthorizedConnection(FakeConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, authorized=False, **kwargs)

    FakeConnection.instances = []
    monkeypatch.setattr("xtg.sources.telegram.source.TelegramConnection", UnauthorizedConnection)
    return FakeConnection


def telegram_source(telegram_settings: Settings, database: Database) -> TelegramSource:
    return TelegramSource(telegram_settings, database=database)


# --- the regression: one loop for the whole life cycle ----------------------


def test_a_collection_never_leaves_the_loop_it_connected_on(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """connect, collect and close must all run on one loop.

    Regression: each step used to get its own :func:`asyncio.run`, so the very
    first API call after ``connect()`` raised ``LoopChangedError``.
    """
    with telegram_source(telegram_settings, database) as source:
        report = source.collect("example", sections=["messages"], limit=2)

    assert report.counts["messages"] == 2
    client_connection = connection.instances[0]
    assert client_connection.opened_on is not None
    # Every loop the client ever saw is the one it connected on.
    assert client_connection.client.seen_loops
    assert all(seen is client_connection.opened_on for seen in client_connection.client.seen_loops)
    assert client_connection.closed_on is client_connection.opened_on
    assert client_connection.client.disconnected is True


def test_every_section_shares_the_connecting_loop(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """Sections are walked one after another, on the same loop and the same client."""
    with telegram_source(telegram_settings, database) as source:
        report = source.collect("example", sections=["messages", "members"], limit=2)

    assert report.counts["messages"] == 2
    assert report.counts["members"] == 1
    assert len(set(map(id, connection.instances[0].client.seen_loops))) == 1


def test_the_collector_is_driven_by_the_sources_loop(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """The adapter must not reach for its own ``asyncio.run``."""
    with telegram_source(telegram_settings, database) as source:
        adapter = source.collector
        assert isinstance(adapter, TelegramCollectorAdapter)
        # The adapter is handed the source's runner, not a fresh loop per call.
        assert adapter._run.__self__ is source  # noqa: SLF001 - the point of the test
        adapter.resolve(database, "example")

    opened, closed = connection.instances[0].opened_on, connection.instances[0].closed_on
    assert opened is closed
    assert connection.instances[0].client.seen_loops[-1] is opened


def test_using_asyncio_run_after_connecting_is_what_used_to_break(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """Guard the guard: a second loop must really fail.

    Without this, the loop assertions above would also pass if the double
    stopped checking, and the regression would slip back in unnoticed.
    """
    source = telegram_source(telegram_settings, database)
    source.connect()
    try:
        # The collector maps the client error onto a clean CLI error, which is
        # how this regression reached users in the first place.
        with pytest.raises(CollectionError) as exc:
            asyncio.run(source.collector.delegate.collect_messages("example", limit=1))
        assert "asyncio event loop must not change" in str(exc.value)
    finally:
        source.close()


# --- loop ownership and release --------------------------------------------


def test_close_disconnects_on_the_connecting_loop_then_releases_it(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    source = telegram_source(telegram_settings, database)
    source.connect()
    loop = source.loop

    source.close()

    assert connection.instances[0].closed_on is loop
    assert loop.is_closed() is True
    assert source.connection is None


def test_close_without_connecting_never_builds_a_loop(
    telegram_settings: Settings, database: Database
) -> None:
    """Closing a source that never ran must not allocate an event loop."""
    source = telegram_source(telegram_settings, database)
    source.close()
    assert source._loop is None  # noqa: SLF001 - the point of the test


def test_close_survives_a_failing_disconnect(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """A broken disconnect is logged, never raised, and still frees the loop."""
    source = telegram_source(telegram_settings, database)
    source.connect()
    connection.instances[0].close_error = OSError("socket already gone")
    loop = source.loop

    source.close()

    assert loop.is_closed() is True


def test_a_source_is_reusable_after_close(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """Reconnecting must not reuse a closed loop."""
    source = telegram_source(telegram_settings, database)
    with source:
        source.collect("example", sections=["messages"], limit=1)
    first = connection.instances[0]

    with source:
        report = source.collect("example", sections=["messages"], limit=1)
    second = connection.instances[1]

    # The second run re-walks the same messages, so it stores nothing new.
    assert report.counts["messages"] == 0
    assert report.duplicated == 1
    assert first is not second
    assert first.closed_on.is_closed() is True
    assert second.closed_on is not first.closed_on
    assert second.closed_on.is_closed() is True


def test_a_failed_connection_releases_the_loop(
    telegram_settings: Settings, database: Database, unauthorized: type[FakeConnection]
) -> None:
    """A failed ``__enter__`` never reaches ``close()``, so the loop must go then.

    Otherwise a refused proxy or an unauthorized session leaks an open event
    loop for the rest of the process.
    """
    source = telegram_source(telegram_settings, database)

    with pytest.raises(AuthError), source:
        pass

    assert source._loop is None  # noqa: SLF001 - the point of the test


def test_the_source_leaves_a_usable_default_loop_behind(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """Later work in the same process still finds a loop it can schedule on."""
    source = telegram_source(telegram_settings, database)
    source.connect()
    source.close()

    assert asyncio.get_event_loop_policy().get_event_loop() is not None


def test_connect_is_idempotent_and_keeps_one_loop(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    source = telegram_source(telegram_settings, database)
    source.connect()
    client = source.client
    loop = source.loop

    source.connect()

    assert source.client is client
    assert source.loop is loop
    assert len(connection.instances) == 1
    source.close()


def test_the_loop_is_the_default_one_while_connected(
    telegram_settings: Settings, database: Database, connection: type[FakeConnection]
) -> None:
    """Telethon helpers that reach for the current loop must see ours."""
    source = telegram_source(telegram_settings, database)
    assert source.loop is asyncio.get_event_loop_policy().get_event_loop()
    source.connect()
    assert asyncio.get_event_loop_policy().get_event_loop() is source.loop
    source.close()
