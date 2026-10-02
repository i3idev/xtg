"""Mapping Telegram objects to database fields.

Every function here is pure and only uses ``getattr``/duck typing on the
Telegram object, which keeps the mapping logic testable without Telethon (and
tolerant to its attribute changes across versions).
"""

from __future__ import annotations

from typing import Any

from xtg.core.utils import clean_username, to_utc

#: Chat types as Telegram reports them, mapped to short stable names.
CHAT_TYPES = {
    "channel": "channel",
    "chat": "group",
    "megagroup": "supergroup",
    "gigagroup": "supergroup",
    "forum": "forum",
    "private": "private",
    "bot": "bot",
}

#: Media attribute name -> stored media type.
MEDIA_TYPES = (
    "photo",
    "video",
    "audio",
    "voice",
    "document",
    "sticker",
    "animation",
    "video_note",
    "gif",
    "webpage",
)


def serialize_raw(entity: Any, *, limit: int = 20_000) -> dict[str, Any]:
    """Best-effort JSON-safe snapshot of a Telegram object.

    Used for the ``raw_data`` column: keeping the original payload means later
    phases never have to re-fetch data.
    """
    if entity is None:
        return {}
    payload: Any = None
    to_dict = getattr(entity, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:  # noqa: BLE001 - never let a payload break a collection
            payload = None
    if payload is None:
        payload = {
            key: value
            for key, value in vars(entity).items()
            if not key.startswith("_") and _is_scalar(value)
        }
    if not isinstance(payload, dict):
        return {}
    return _sanitize(payload, limit=limit)


def user_fields(entity: Any) -> dict[str, Any]:
    """Columns for the ``users`` table."""
    return {
        "telegram_id": int(getattr(entity, "id", 0) or 0),
        "username": clean_username(getattr(entity, "username", None)),
        "first_name": _text(getattr(entity, "first_name", None)),
        "last_name": _text(getattr(entity, "last_name", None)),
        "bio": _text(getattr(entity, "about", None) or getattr(entity, "bio", None)),
        "phone": _text(getattr(entity, "phone", None)),
        "is_bot": bool(getattr(entity, "bot", False)),
        "is_premium": getattr(entity, "premium", None),
        "raw_data": serialize_raw(entity),
    }


def chat_fields(entity: Any) -> dict[str, Any]:
    """Columns for the ``chats`` table."""
    if entity is None:
        return {}
    kind = _chat_type_name(entity) or "chat"
    return {
        "telegram_id": int(getattr(entity, "id", 0) or 0),
        "title": _text(getattr(entity, "title", None) or getattr(entity, "first_name", None)),
        "username": clean_username(getattr(entity, "username", None)),
        "type": CHAT_TYPES.get(kind, kind),
        "is_verified": getattr(entity, "verified", None),
        "members_count": getattr(entity, "participants_count", None),
        "raw_data": serialize_raw(entity),
    }


def message_fields(entity: Any) -> dict[str, Any]:
    """Columns for the ``messages`` table."""
    reply_to = getattr(entity, "reply_to", None)
    forwards = getattr(entity, "forwards", None)
    return {
        "telegram_message_id": int(getattr(entity, "id", 0) or 0),
        "text": _message_text(entity),
        "date": to_utc(getattr(entity, "date", None)),
        "reply_to_id": _reply_to_id(reply_to),
        "views": getattr(entity, "views", None),
        "forwards": getattr(forwards, "forwards", None) if forwards is not None else None,
        "has_media": has_media(entity),
        "raw_data": serialize_raw(entity),
    }


def media_entries(entity: Any) -> list[dict[str, Any]]:
    """Media metadata of a message (Phase 2 stores metadata only).

    Returns one dict per attachment with ``type``, ``file_name``, ``file_size``
    and a free-form ``metadata`` payload. ``path`` stays ``None`` because file
    downloads are Phase 3.
    """
    entries: list[dict[str, Any]] = []
    for name in MEDIA_TYPES:
        attachment = getattr(entity, name, None)
        if attachment is None:
            continue
        entries.append(
            {
                "type": name,
                "file_name": _text(getattr(attachment, "file_name", None)),
                "file_size": _file_size(attachment),
                "metadata": {
                    "mime_type": _text(getattr(attachment, "mime_type", None)),
                    "duration": getattr(attachment, "duration", None),
                    "width": getattr(attachment, "w", None),
                    "height": getattr(attachment, "h", None),
                    "title": _text(getattr(attachment, "title", None)),
                    "dc_id": getattr(attachment, "dc_id", None),
                },
            }
        )
    return entries


def has_media(entity: Any) -> bool:
    """True when the message carries an attachment we record."""
    return bool(media_entries(entity))


def _chat_type_name(entity: Any) -> str | None:
    """Derive a Telegram chat type from its flags."""
    if getattr(entity, "megagroup", False):
        return "megagroup"
    if getattr(entity, "group", False):
        return "chat"
    if getattr(entity, "channel", False):
        return "channel"
    if getattr(entity, "bot", False):
        return "bot"
    if getattr(entity, "user", False):
        return "private"
    return None


def _reply_to_id(reply_to: Any) -> int | None:
    if reply_to is None:
        return None
    reply_id = getattr(reply_to, "reply_to_msg_id", None)
    if reply_id is None:
        reply_id = getattr(reply_to, "id", None)
    return int(reply_id) if reply_id else None


def _message_text(entity: Any) -> str | None:
    text = getattr(entity, "text", None)
    if text:
        return str(text)
    message = getattr(entity, "message", None)
    if message:
        return str(message)
    return None


def _file_size(attachment: Any) -> int | None:
    size = getattr(attachment, "size", None)
    if size is not None:
        return int(size)
    sizes = getattr(attachment, "sizes", None) or []
    if sizes:
        return max((int(getattr(item, "size", 0) or 0) for item in sizes), default=None)
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | type(None))


def _sanitize(payload: dict[str, Any], *, limit: int) -> dict[str, Any]:
    """Drop bytes and huge strings so the JSON column stays usable."""
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, bytes):
            result[key] = f"<bytes:{len(value)}>"
        elif isinstance(value, str) and len(value) > limit:
            result[key] = f"{value[:limit]}…"
        elif isinstance(value, dict | list | tuple):
            result[key] = (
                _sanitize(value, limit=limit) if isinstance(value, dict) else str(value)[:limit]
            )
        else:
            result[key] = value
    return result
