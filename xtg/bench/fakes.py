"""Deterministic fake sources for the benchmark harness.

The benchmarks must not depend on Telegram or Reddit being reachable: network
noise would make every comparison meaningless. These doubles implement the same
surface the collectors use (``iter_messages``, ``iter_participants``,
``subreddit``, ...) and can be told to simulate latency, errors, rate limits and
proxy failure, which is what the failover scenarios need.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class BenchUser:
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
class BenchChat:
    id: int
    title: str | None = None
    username: str | None = None
    channel: bool = True
    megagroup: bool = False
    participants_count: int | None = None
    verified: bool = False


@dataclass
class BenchMedia:
    file_name: str | None = None
    size: int | None = None
    mime_type: str | None = None
    duration: int | None = None
    w: int | None = None
    h: int | None = None


@dataclass
class BenchMessage:
    id: int
    text: str | None = None
    sender: BenchUser | None = None
    date: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    reply_to: Any | None = None
    views: int | None = None
    forwards: Any | None = None
    photo: BenchMedia | None = None
    document: BenchMedia | None = None
    video: BenchMedia | None = None
    voice: BenchMedia | None = None


@dataclass
class BenchParticipant:
    user: BenchUser
    admin_rights: Any | None = None
    banned_rights: Any | None = None
    creator: bool = False


class BenchFloodWait(Exception):
    """Stands in for ``telethon.errors.FloodWaitError`` (classified by name)."""


class BenchConnectionLost(Exception):
    """Stands in for a dropped MTProto connection."""


class BenchClient:
    """Fake Telegram client with scriptable latency, errors and floods.

    ``iter_messages`` honours ``offset_id`` exactly like Telethon (exclusive
    upper bound) so resume behaviour is benchmarkable offline.
    """

    def __init__(
        self,
        messages: list[BenchMessage],
        *,
        chat: BenchChat | None = None,
        participants: list[BenchParticipant] | None = None,
        latency_ms: float = 0.0,
        fail_every: int = 0,
        flood_every: int = 0,
        seed: int = 1234,
    ) -> None:
        self.messages = messages
        self.chat = chat or BenchChat(id=-1001234567890, title="Bench", username="bench")
        self.participants = participants or []
        self.latency_ms = latency_ms
        self.fail_every = fail_every
        self.flood_every = flood_every
        self.calls = 0
        self.connects = 0
        self.disconnects = 0
        self.connected = False
        self.iter_calls = 0
        self.request_kwargs: list[dict[str, Any]] = []
        self.last_entity: Any = None
        self._rng = random.Random(seed)
        self._offset: int | None = None

    # --- connection ---------------------------------------------------------
    async def connect(self) -> BenchClient:
        self.connects += 1
        self.connected = True
        return self

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return True

    async def get_entity(self, value: Any) -> BenchChat:
        self.last_entity = value
        return self.chat

    # --- api ----------------------------------------------------------------
    async def _tick(self) -> None:
        self.calls += 1
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.flood_every and self.calls % self.flood_every == 0:
            error = BenchFloodWait("wait")
            error.seconds = 0  # type: ignore[attr-defined]
            raise error
        if self.fail_every and self.calls % self.fail_every == 0:
            raise BenchConnectionLost("connection reset by peer")

    def iter_messages(self, entity: Any, **kwargs: Any) -> Any:
        """Like Telethon: a *sync* call returning an async iterator."""
        self.iter_calls += 1
        self.last_entity = entity
        self.request_kwargs.append(kwargs)
        self._offset = kwargs.get("offset_id")
        return self._stream_messages(kwargs.get("limit") or len(self.messages))

    def iter_participants(self, entity: Any, **kwargs: Any) -> Any:
        self.iter_calls += 1
        self.last_entity = entity
        return self._stream_participants(kwargs.get("limit") or len(self.participants))

    async def _stream_messages(self, limit: int) -> Any:
        offset = self._offset
        for message in self.messages:
            if offset and message.id >= offset:
                continue
            await self._tick()
            yield message
            limit -= 1
            if limit <= 0:
                return

    async def _stream_participants(self, limit: int) -> Any:
        for participant in self.participants:
            await self._tick()
            yield participant
            limit -= 1
            if limit <= 0:
                return


def bench_messages(count: int, *, senders: int = 5) -> list[BenchMessage]:
    """Build ``count`` messages spread over ``senders`` distinct users."""
    users = [
        BenchUser(
            id=1000 + index,
            username=f"user{index}",
            first_name=f"User{index}",
            about="benchmark sender",
        )
        for index in range(senders)
    ]
    messages: list[BenchMessage] = []
    for index in range(1, count + 1):
        messages.append(
            BenchMessage(
                id=index,
                text=f"benchmark message {index} " + "payload " * 3,
                sender=users[index % senders],
                views=index,
                forwards=0,
                photo=BenchMedia(file_name=f"p-{index}.jpg", size=1024, mime_type="image/jpeg")
                if index % 4 == 0
                else None,
            )
        )
    return messages


def bench_participants(count: int) -> list[BenchParticipant]:
    return [
        BenchParticipant(user=BenchUser(id=2000 + index, username=f"member{index}", first_name="M"))
        for index in range(count)
    ]


class BenchSubreddit:
    """Minimal PRAW-like subreddit handle with a scriptable listing."""

    def __init__(self, client: BenchRedditClient, name: str) -> None:
        self._client = client
        self.id = f"t5_{name}"
        self.fullname = f"t5_{name}"
        self.name = name
        self.display_name = name
        self.title = f"r/{name}"
        self.public_description = "benchmark subreddit"
        self.subscribers = 1000
        self.over18 = False
        self.created_utc = datetime(2020, 1, 1, tzinfo=UTC)

    def __getattr__(self, name: str) -> Any:
        if name in ("new", "hot", "top"):

            def listing(**kwargs: Any) -> Any:
                return self._client._posts(kwargs.get("limit") or 100)

            return listing
        raise AttributeError(name)


class BenchRedditClient:
    """Fake PRAW client used by the Reddit benchmarks."""

    def __init__(self, posts: int = 200, *, latency_ms: float = 0.0, seed: int = 7) -> None:
        self.post_count = posts
        self.latency_ms = latency_ms
        self.requests = 0
        self._rng = random.Random(seed)

    def subreddit(self, name: str, **_kwargs: Any) -> BenchSubreddit:
        return BenchSubreddit(self, name)

    def _posts(self, limit: int) -> Any:
        for index in range(1, min(limit, self.post_count) + 1):
            self.requests += 1
            if self.latency_ms:
                import time

                time.sleep(self.latency_ms / 1000)
            yield BenchPost(f"t3_bench{index}", index)


class BenchPost:
    """Minimal submission object."""

    def __init__(self, fullname: str, index: int) -> None:
        self.fullname = fullname
        self.id = fullname
        self.title = f"benchmark submission {index}"
        self.selftext = "benchmark body text"
        self.url = f"https://example{index % 7}.com/article"
        self.permalink = f"/r/bench/comments/{index}/"
        self.domain = f"example{index % 7}.com"
        self.score = index * 3
        self.num_comments = index % 11
        self.upvote_ratio = 0.9
        self.created_utc = datetime(2026, 1, 1, tzinfo=UTC)
        self.stickied = False
        self.over_18 = False
        self.link_flair_text = None
        self.author = BenchRedditUser(f"t2_u{index % 13}", f"user{index % 13}")
        self.subreddit = BenchSubredditRef("t5_bench", "bench")
        self.comments = []


class BenchSubredditRef:
    def __init__(self, fullname: str, name: str) -> None:
        self.fullname = fullname
        self.display_name = name
        self.title = f"r/{name}"
        self.public_description = ""
        self.subscribers = 1
        self.over18 = False
        self.created_utc = datetime(2020, 1, 1, tzinfo=UTC)


class BenchRedditUser:
    def __init__(self, fullname: str, name: str) -> None:
        self.fullname = fullname
        self.name = name
        self.id = fullname
        self.created_utc = datetime(2019, 1, 1, tzinfo=UTC)
        self.comment_karma = 10
        self.link_karma = 5
        self.verified = False
        self.has_verified_email = False
        self.is_employee = False
        self.is_mod = False
