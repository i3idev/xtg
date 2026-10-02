"""Reddit serializers: PRAW objects -> plain column mappings.

Duck typed, like :mod:`xtg.telegram.serializers`: everything uses attribute
access only, so the collector can be driven with test doubles and no PRAW
import. Reddit identifies objects by their *fullname* (``t1_`` comment,
``t2_`` user, ``t3_`` submission, ``t5_`` subreddit), which is the most
reliable kind discriminator available - ``subreddit`` on a submission, for
example, is a lazy attribute that costs an API call, so it is never touched
during parsing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

#: Fullname prefix -> kind used in reports and graph references.
KIND_BY_PREFIX: dict[str, str] = {
    "t1_": "comment",
    "t2_": "user",
    "t3_": "post",
    "t5_": "subreddit",
}

_HASHTAG = re.compile(r"#(\w+)")


def fullname(obj: Any) -> str:
    """Return the ``t?_xxxxx`` fullname, deriving it when needed."""
    value = getattr(obj, "fullname", None)
    if isinstance(value, str) and value:
        return value
    prefix = getattr(obj, "PREFIX", None) or _prefix_by_type(obj)
    identifier = getattr(obj, "id", None)
    if not identifier:
        raise ValueError(f"{type(obj).__name__} without an id")
    return f"{prefix}{identifier}"


def _prefix_by_type(obj: Any) -> str:
    for prefix, kind in KIND_BY_PREFIX.items():
        if kind in type(obj).__name__.lower():
            return prefix
    raise ValueError(f"Unknown Reddit object type: {type(obj).__name__}")


def kind_of(obj: Any) -> str:
    """``post`` / ``comment`` / ``user`` / ``subreddit`` for a PRAW object."""
    prefix = fullname(obj).split("_", 1)[0]
    try:
        return KIND_BY_PREFIX[f"{prefix}_"]
    except KeyError as exc:
        raise ValueError(f"Unexpected Reddit fullname prefix: {prefix!r}") from exc


def reddit_name(obj: Any) -> str | None:
    """Account name of a Redditor (``u/alice`` -> ``alice``)."""
    name = getattr(obj, "name", None)
    return str(name).lstrip("u/") if isinstance(name, str) and name else None


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool:
    return bool(value)


def _text(value: Any, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if limit and len(text) > limit:
        return text[:limit]
    return text


def utc(value: Any) -> datetime | None:
    """PRAW returns POSIX timestamps (``created_utc``)."""
    seconds = _float(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - absurd input
        return None


def serialize_raw(obj: Any, limit: int = 4000) -> dict[str, Any]:
    """Best effort snapshot of the public attributes of a PRAW object."""
    payload: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:  # noqa: BLE001 - lazy attributes may fetch
            continue
        if callable(value):
            continue
        payload[name] = _scalar(value, limit=limit)
    identifier = getattr(obj, "id", None)
    if identifier:
        payload["id"] = str(identifier)
    return payload


def _scalar(value: Any, *, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)[:limit]


def user_fields(obj: Any) -> dict[str, Any]:
    """Columns for ``reddit_users``."""
    return {
        "reddit_id": fullname(obj),
        "name": reddit_name(obj) or fullname(obj),
        "created_utc": utc(getattr(obj, "created_utc", None)),
        "comment_karma": _int(getattr(obj, "comment_karma", None)),
        "link_karma": _int(getattr(obj, "link_karma", None)),
        "verified": _bool(getattr(obj, "verified", False)),
        "has_verified_email": _bool(getattr(obj, "has_verified_email", False)),
        "is_employee": _bool(getattr(obj, "is_employee", False)),
        "is_mod": _bool(getattr(obj, "is_mod", False)),
        "raw_data": serialize_raw(obj),
    }


def subreddit_fields(obj: Any, name: str) -> dict[str, Any]:
    """Columns for ``reddit_subreddits``.

    ``name`` is passed in because ``display_name`` is the canonical short name
    while ``name`` is the full ``r/linux`` form.
    """
    display = getattr(obj, "display_name", None) or name
    return {
        "reddit_id": fullname(obj),
        "name": str(display).lstrip("r/").lower(),
        "title": _text(getattr(obj, "title", None), 256),
        "public_description": _text(getattr(obj, "public_description", None), 2000),
        "subscribers": _int(getattr(obj, "subscribers", None)),
        "over18": _bool(getattr(obj, "over18", False)),
        "created_utc": utc(getattr(obj, "created_utc", None)),
        "raw_data": serialize_raw(obj),
    }


def post_fields(obj: Any) -> dict[str, Any]:
    """Columns for ``reddit_posts``."""
    url = _text(getattr(obj, "url", None), 1024)
    return {
        "reddit_id": fullname(obj),
        "title": _text(getattr(obj, "title", None), 512) or fullname(obj),
        "body": _text(getattr(obj, "selftext_html", None) or getattr(obj, "selftext", None), 20000),
        "url": url,
        "permalink": _text(getattr(obj, "permalink", None), 512),
        "domain": _text(getattr(obj, "domain", None), 255),
        "score": _int(getattr(obj, "score", None)),
        "num_comments": _int(getattr(obj, "num_comments", None)),
        "upvote_ratio": _float(getattr(obj, "upvote_ratio", None)),
        "created_utc": utc(getattr(obj, "created_utc", None)),
        "stickied": _bool(getattr(obj, "stickied", False)),
        "over_18": _bool(getattr(obj, "over18", False)),
        "raw_data": serialize_raw(obj),
    }


def comment_fields(obj: Any, *, depth: int = 0) -> dict[str, Any]:
    """Columns for ``reddit_comments``."""
    return {
        "reddit_id": fullname(obj),
        "parent_reddit_id": _parent_id(obj),
        "body": _text(getattr(obj, "body", None), 20000),
        "score": _int(getattr(obj, "score", None)),
        "depth": depth,
        "created_utc": utc(getattr(obj, "created_utc", None)),
        "is_submitter": _bool(getattr(obj, "is_submitter", False)),
        "stickied": _bool(getattr(obj, "stickied", False)),
        "raw_data": serialize_raw(obj),
    }


def author(obj: Any) -> Any | None:
    """The author object of a post or comment, or ``None`` when unavailable.

    Deleted accounts still come back as an object; the name is kept as-is so
    the graph shows ``u/[deleted]`` instead of silently losing the edge.
    """
    return getattr(obj, "author", None)


def _parent_id(obj: Any) -> str | None:
    """Fullname of the parent comment or submission, when it is known.

    PRAW's ``parent_id`` is the fullname (``t1_...`` for a comment, ``t3_...``
    for the submission); ``parent`` is a lazily fetched object and is never
    touched here.
    """
    value = getattr(obj, "parent_id", None)
    if isinstance(value, str) and "_" in value:
        return value
    return None


def domain_of(url: str | None) -> str | None:
    """Host part of a URL, used as a ``domain`` intelligence pivot."""
    if not url or "://" not in url:
        return None
    host = url.split("://", 1)[1].split("/", 1)[0]
    host = host.split("@")[-1].split(":")[0]
    return host.lower() or None


def hashtags(text: str | None) -> list[str]:
    """``#tag`` tokens of a title or body, lower cased and de-duplicated."""
    if not text:
        return []
    return sorted({token.lower() for token in _HASHTAG.findall(text) if len(token) > 2})
