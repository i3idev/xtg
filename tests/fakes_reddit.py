"""Fake Reddit (PRAW) objects.

PRAW is never imported by the test suite: the Reddit collector only touches the
attribute surface of ``Redditor``, ``Subreddit``, ``Submission`` and ``Comment``,
so these doubles are enough to cover parsing, writing and duplicate handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class FakeRedditor:
    id: str
    name: str
    comment_karma: int = 0
    link_karma: int = 0
    verified: bool = False
    has_verified_email: bool = False
    is_employee: bool = False
    is_mod: bool = False
    created_utc: float = 1_600_000_000.0

    @property
    def fullname(self) -> str:
        return f"t2_{self.id}"

    @property
    def created(self) -> datetime:
        return datetime.fromtimestamp(self.created_utc, tz=UTC)


@dataclass
class FakeSubreddit:
    id: str
    display_name: str
    title: str = "Subreddit"
    public_description: str = "A fake subreddit."
    subscribers: int = 1000
    over18: bool = False
    created_utc: float = 1_500_000_000.0
    posts: list[FakeSubmission] = field(default_factory=list)

    @property
    def fullname(self) -> str:
        return f"t5_{self.id}"

    def _listing(self, limit: int, after: str | None = None) -> list[FakeSubmission]:
        """Mimic a Reddit listing: newest first, ``after`` is an exclusive cursor."""
        cursor: int | None = None
        if after and "_" in after:
            match = next((post for post in self.posts if post.fullname == after), None)
            cursor = match.index if match else -1
        newer = [post for post in self.posts if cursor is None or post.index > cursor]
        return list(reversed(newer))[:limit]

    def new(self, limit: int = 100, params: dict[str, Any] | None = None) -> list[FakeSubmission]:
        return self._listing(limit, (params or {}).get("after"))

    def hot(self, limit: int = 100, params: dict[str, Any] | None = None) -> list[FakeSubmission]:
        return self._listing(limit, (params or {}).get("after"))

    def top(self, limit: int = 100, params: dict[str, Any] | None = None) -> list[FakeSubmission]:
        return self._listing(limit, (params or {}).get("after"))

    def controversial(self, limit: int = 100, params: dict[str, Any] | None = None):
        return self._listing(limit, (params or {}).get("after"))

    def rising(self, limit: int = 100, params: dict[str, Any] | None = None):
        return self._listing(limit, (params or {}).get("after"))


@dataclass
class FakeComment:
    id: str
    body: str = "a comment"
    score: int = 1
    author: Any | None = None
    parent_id: str | None = None
    created_utc: float = 1_700_000_000.0
    is_submitter: bool = False
    stickied: bool = False
    replies: list[Any] = field(default_factory=list)

    @property
    def fullname(self) -> str:
        return f"t1_{self.id}"


class MoreComments:
    """PRAW placeholder for "load more comments"; the collector skips it."""


class prawcore_NotFound(Exception):
    """Named like ``prawcore.exceptions.NotFound`` so the mapper recognises it."""


class prawcore_TooManyRequests(Exception):
    """Named like ``prawcore.exceptions.TooManyRequests``."""


@dataclass
class FakeSubmission:
    id: str
    title: str
    subreddit: str = "linux"
    author: Any | None = None
    selftext: str = ""
    url: str | None = None
    domain: str | None = None
    permalink: str | None = None
    score: int = 0
    num_comments: int = 0
    upvote_ratio: float | None = None
    created_utc: float = 1_700_000_000.0
    stickied: bool = False
    over18: bool = False
    index: int = 0
    comments: list[Any] = field(default_factory=list)

    @property
    def fullname(self) -> str:
        return f"t3_{self.id}"


class FakeReddit:
    """Client double with the small PRAW surface the collector uses."""

    def __init__(self, subreddits: dict[str, FakeSubreddit] | None = None, **kwargs: Any) -> None:
        self.subreddits = subreddits or {}
        self.kwargs = kwargs
        self.subreddit_calls: list[str] = []
        self.submission_calls: list[str] = []
        self.subreddit_error: Exception | None = kwargs.pop("subreddit_error", None)
        self.user_agent = kwargs.get("user_agent")
        self.read_only = kwargs.get("username") is None
        self._redditor = object() if kwargs.get("username") else None

    def subreddit(self, name: str) -> FakeSubreddit:
        self.subreddit_calls.append(name)
        if self.subreddit_error is not None:
            raise self.subreddit_error
        try:
            return self.subreddits[name.lower()]
        except KeyError as exc:
            raise prawcore_NotFound(f"Subreddit {name!r} does not exist") from exc

    def submission(self, identifier: str) -> FakeSubmission:
        self.submission_calls.append(identifier)
        for subreddit in self.subreddits.values():
            for post in subreddit.posts:
                if post.id == identifier:
                    return post
        raise RuntimeError(f"No submission {identifier!r}")


def make_subreddit(name: str = "linux", posts: int = 3, **overrides: Any) -> FakeSubreddit:
    """Subreddit with ``posts`` submissions, oldest index first."""
    subreddit = FakeSubreddit(id=f"2{name[:3]}", display_name=name, **overrides)
    subreddit.posts = make_posts(posts, subreddit=name)
    return subreddit


def make_posts(count: int = 3, *, subreddit: str = "linux") -> list[FakeSubmission]:
    """Posts with ascending ids and timestamps."""
    author = FakeRedditor(id="alice1", name="alice")
    return [
        FakeSubmission(
            id=f"post{index}",
            title=f"Thread #{index} about #security",
            subreddit=subreddit,
            author=author,
            selftext=f"Body of thread {index}",
            url=f"https://example{index}.org/article",
            domain=f"example{index}.org",
            permalink=f"/r/{subreddit}/comments/post{index}/",
            score=index * 3,
            num_comments=index,
            upvote_ratio=0.75,
            created_utc=1_700_000_000.0 + index,
            index=index,
        )
        for index in range(1, count + 1)
    ]


def make_comments(subreddit: FakeSubreddit) -> None:
    """Attach a small comment tree (including a ``MoreComments``) to each post."""
    for post in subreddit.posts:
        top = FakeComment(
            id=f"c{post.id}",
            body="Top level comment",
            score=5,
            author=FakeRedditor(id="bob2", name="bob"),
            parent_id=f"t3_{post.id}",
            created_utc=1_700_000_100.0,
        )
        top.replies = [
            FakeComment(
                id=f"c{post.id}reply",
                body="A reply",
                score=1,
                author=FakeRedditor(id="alice1", name="alice"),
                parent_id=top.fullname,
                created_utc=1_700_000_200.0,
            ),
            MoreComments(),
        ]
        post.comments = [top]
        post.num_comments = len(post.comments)
