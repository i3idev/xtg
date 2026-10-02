"""Small, dependency-free helpers shared across subsystems."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_INVITE_RE = re.compile(
    r"(?:t\.me|telegram\.me|telegram\.dog)/(?:joinchat/|\+)([\w-]+)", re.IGNORECASE
)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

#: How long a single string may get in a ``raw_data`` snapshot before it is cut.
RAW_TEXT_LIMIT = 20_000

#: JSON has no binary type, so ``bytes`` are stored base64 encoded behind this
#: marker. The prefix is what keeps a decoded value distinguishable from text
#: that merely looks like base64, and base64 is deterministic, so the same input
#: always produces the same snapshot.
BYTES_PREFIX = "base64:"

#: Placeholder for a value that refers back to one of its own ancestors. Entity
#: payloads can be cyclic (``reply_to.reply_to``); without this a snapshot would
#: recurse forever.
CYCLE_MARKER = "<cycle>"

#: Suffix appended to a string that was cut at :data:`RAW_TEXT_LIMIT`.
TRUNCATION_MARKER = "…"


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


# --- JSON normalisation -----------------------------------------------------


def to_jsonable(
    value: Any,
    *,
    limit: int = RAW_TEXT_LIMIT,
    _seen: frozenset[int] = frozenset(),
) -> Any:
    """Recursively turn a platform payload into JSON-native data.

    This is the **single** boundary between what a platform object carries and
    what a ``JSON`` column can hold. It exists because a raw Telethon entity is
    not JSON: ``Channel.date`` is a :class:`~datetime.datetime`, a chat photo
    carries raw ``bytes``, and nested objects hold further objects. Storing any
    of those verbatim raises ``TypeError: Object of type datetime is not JSON
    serializable`` at flush time, long after the value was collected.

    The result holds only ``dict``, ``list``, ``str``, ``int``, ``float``,
    ``bool`` and ``None``, so it round-trips through :func:`json.loads` without
    object hooks, and it is deterministic: the same input always yields the same
    output.

    How each type is represented, and why:

    ``datetime`` / ``date`` / ``time``
        ISO-8601 via :meth:`~datetime.datetime.isoformat`, which **keeps the UTC
        offset** of an aware value. Converting to UTC first would silently
        discard the original offset, and assuming UTC for a naive value would
        invent information the payload never carried.
    ``bytes`` / ``bytearray`` / ``memoryview``
        ``base64:<data>``. The old behaviour wrote ``<bytes:16>``, which threw
        the content away; an encoded payload stays decodable.
    ``Enum``
        Its value, normalised in turn. An ``IntEnum`` member is a number, and
        the member name is a Python detail the database has no use for.
    ``dict``
        Recursively normalised keys and values. Keys become strings, because
        JSON object keys are strings.
    ``list`` / ``tuple``
        A JSON array with every element normalised. A sequence is no longer
        collapsed into a Python ``repr``, which used to bury whole nested
        structures - datetimes included - in a single line of text.
    ``set`` / ``frozenset``
        A sorted array, so the order never depends on hash randomisation.
    ``str``, ``int``, ``float``, ``bool``, ``None``
        Unchanged, except that a ``str`` longer than ``limit`` is cut.
    objects
        Through their own ``to_dict()`` when they have one (a Telethon TLObject,
        an SQLAlchemy model), otherwise through their public attributes. Only a
        value exposing neither is marked ``<ClassName>``: naming the type keeps
        the gap visible instead of hiding it behind an arbitrary ``repr``.

    ``limit`` bounds a single string, not the document: a message body is worth
    keeping, an unbounded one is not.
    """
    # Enums first: an IntEnum is an int, and we want the value, not the member.
    if isinstance(value, Enum):
        return to_jsonable(value.value, limit=limit, _seen=_seen)

    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clip(value, limit)

    # datetime is a date, so one branch covers all three.
    if isinstance(value, datetime | date | time):
        return value.isoformat()

    if isinstance(value, bytes | bytearray | memoryview):
        return f"{BYTES_PREFIX}{base64.b64encode(bytes(value)).decode('ascii')}"

    if isinstance(value, Mapping):
        if id(value) in _seen:
            return CYCLE_MARKER
        nested = _seen | {id(value)}
        return {
            _json_key(key): to_jsonable(item, limit=limit, _seen=nested)
            for key, item in value.items()
        }

    if isinstance(value, set | frozenset):
        if id(value) in _seen:
            return CYCLE_MARKER
        nested = _seen | {id(value)}
        items = [to_jsonable(item, limit=limit, _seen=nested) for item in value]
        return _sorted(items)

    if isinstance(value, Sequence):
        if id(value) in _seen:
            return CYCLE_MARKER
        nested = _seen | {id(value)}
        return [to_jsonable(item, limit=limit, _seen=nested) for item in value]

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        if id(value) in _seen:
            return CYCLE_MARKER
        nested = _seen | {id(value)}
        try:
            return to_jsonable(to_dict(), limit=limit, _seen=nested)
        except Exception:  # noqa: BLE001 - a broken to_dict must not lose the row
            # Fall through: the instance attributes are usually still readable.
            pass

    attributes = _public_attributes(value)
    if attributes is not None:
        if id(value) in _seen:
            return CYCLE_MARKER
        nested = _seen | {id(value)}
        return {
            _json_key(key): to_jsonable(item, limit=limit, _seen=nested)
            for key, item in attributes.items()
        }

    # Nothing introspectable is left: name the type instead of guessing a repr.
    return _clip(f"<{type(value).__name__}>", limit)


def to_jsonable_dict(value: Any, *, limit: int = RAW_TEXT_LIMIT) -> dict[str, Any]:
    """:func:`to_jsonable` for a payload that has to stay a JSON object."""
    normalized = to_jsonable(value, limit=limit)
    return normalized if isinstance(normalized, dict) else {"value": normalized}


def _clip(text: str, limit: int) -> str:
    """Cut a string at ``limit``, marking that it was cut."""
    return text if len(text) <= limit else f"{text[:limit]}{TRUNCATION_MARKER}"


def _json_key(key: Any) -> str:
    """A JSON object key, which is always a string."""
    if isinstance(key, str):
        return key
    if isinstance(key, bool | int | float | type(None)):
        return str(key)
    if isinstance(key, Enum):
        return str(key.value)
    return str(to_jsonable(key, limit=256))


def _sorted(items: list[Any]) -> list[Any]:
    """Sort normalised set members deterministically, whatever their types are."""
    import json

    try:
        return sorted(items)
    except TypeError:
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))


def _public_attributes(obj: Any) -> dict[str, Any] | None:
    """Readable instance attributes, or ``None`` when the object exposes none.

    ``vars()`` raises ``TypeError`` on an object without ``__dict__`` - anything
    using ``__slots__``, for instance - so both layouts are handled. ``None``
    means "nothing to describe", which is a different answer from "an empty
    attribute dict".
    """
    attributes: dict[str, Any] = {}
    found = False
    instance_dict = getattr(obj, "__dict__", None)
    if isinstance(instance_dict, dict):
        found = True
        attributes.update(instance_dict)
    for slot in getattr(type(obj), "__slots__", ()) or ():
        if hasattr(obj, slot):
            found = True
            attributes[slot] = getattr(obj, slot)
    if not found:
        return None
    return {name: value for name, value in attributes.items() if not name.startswith("_")}
