"""Small, dependency-free helpers shared across subsystems."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_RE = re.compile(
    r"(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)([\w-]+)", re.IGNORECASE
)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(UTC)


def to_utc(value: datetime | None) -> datetime | None:
    """Normalise a naive datetime (assumed UTC) into an aware one."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def format_datetime(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Human readable timestamp, ``-`` when the value is missing."""
    if value is None:
        return "-"
    return to_utc(value).strftime(fmt)  # type: ignore[union-attr]


def format_size(num_bytes: float) -> str:
    """Format a byte count using binary units."""
    if num_bytes < 1024:
        return f"{int(num_bytes)} B"
    size = float(num_bytes)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TiB"  # pragma: no cover - unreachable


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as ``1h 02m 03s``."""
    seconds = int(max(seconds, 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """``pluralize(1, "proxy") -> "1 proxy"``, ``pluralize(2, ...) -> "2 proxies"``."""
    word = singular if count == 1 else (plural or _english_plural(singular))
    return f"{count} {word}"


def _english_plural(word: str) -> str:
    """Minimal English pluralisation: handles the regular cases only."""
    if word.endswith("y") and len(word) > 1 and word[-2].lower() not in "aeiou":
        return f"{word[:-1]}ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    return f"{word}s"


def chunked[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Yield ``size``-long chunks of ``items``."""
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def ensure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def truncate(text: str | None, limit: int = 60) -> str:
    """Single-line, length-limited representation of ``text``."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"


def clean_username(value: str | None) -> str | None:
    """Normalise a Telegram username (``@name``, ``t.me/name``, full URL)."""
    if not value:
        return None
    candidate = value.strip().strip("/")
    if _URL_RE.match(candidate):
        candidate = candidate.split("//", 1)[1]
    if "/" in candidate:
        candidate = candidate.rsplit("/", 1)[-1]
    candidate = candidate.lstrip("@").strip()
    return candidate or None


def is_valid_username(value: str | None) -> bool:
    """True when ``value`` looks like a Telegram username."""
    return bool(value) and bool(_USERNAME_RE.match(value or ""))


def extract_invite_hash(url: str) -> str | None:
    """Return the invite hash of a ``t.me/joinchat/...`` style link."""
    match = _INVITE_RE.search(url)
    return match.group(1) if match else None


def normalize_entity_ref(value: str) -> tuple[str, str]:
    """Classify a user supplied reference.

    Returns ``(kind, value)`` where kind is one of ``username``, ``invite``,
    ``id`` or ``unknown``. Telegram ids may be negative (``-100...``).
    """
    raw = value.strip()
    if not raw:
        return "unknown", ""
    if _URL_RE.match(raw) or "t.me/" in raw or "telegram.me/" in raw:
        invite = extract_invite_hash(raw)
        if invite:
            return "invite", invite
        username = clean_username(raw)
        if username and is_valid_username(username):
            return "username", username
        return "unknown", raw
    if raw.startswith("@"):
        username = clean_username(raw)
        if username and is_valid_username(username):
            return "username", username
        return "unknown", raw
    if re.fullmatch(r"-?\d+", raw):
        return "id", raw
    username = clean_username(raw)
    if username and is_valid_username(username):
        return "username", username
    return "unknown", raw


def parse_json_option(raw: str | None, name: str = "value") -> Any:
    """Parse an inline JSON argument, raising ``ValueError`` on bad input."""
    import json

    if raw is None:
        raise ValueError(f"{name} is required")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON: {exc}") from exc
