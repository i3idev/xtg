"""Tests for turning raw Telegram entities into JSON-safe ``raw_data``.

A Telethon entity is not JSON. ``Channel.date`` is a ``datetime``, a chat photo
carries ``bytes``, and a message nests forwards headers and media blobs. The
shallow sanitiser that used to guard the column passed a ``datetime`` straight
through - so ``queries.upsert_chat()`` died at ``session.flush()`` with
``TypeError: Object of type datetime is not JSON serializable`` - and collapsed
every list into a Python ``repr``, losing whatever was inside it.

These tests use the real ``to_dict()`` shapes, not approximations, and assert on
the three properties that matter: the value is JSON, the value round-trips
through ``json.loads`` without hooks, and the same input always produces the
same output.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

import pytest
from tests.fakes import (
    FIXED_DATE,
    THUMB_BYTES,
    BrokenPayloadEntity,
    FakeChannelEntity,
    FakeChat,
    FakeMessageEntity,
    FakeUser,
    SlottedEntity,
    channel_to_dict,
    message_to_dict,
)
from xtg.core.utils import (
    BYTES_PREFIX,
    CYCLE_MARKER,
    TRUNCATION_MARKER,
    to_jsonable,
    to_jsonable_dict,
)
from xtg.database import queries
from xtg.database.db import Database
from xtg.telegram import serializers

#: Every leaf a ``raw_data`` document is allowed to hold.
JSON_LEAVES = (str, int, float, bool, type(None))


def assert_json_native(value: Any, *, path: str = "raw_data") -> None:
    """Fail with the exact path of the first non-JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), f"{path}: non-string key {key!r}"
            assert_json_native(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_json_native(item, path=f"{path}[{index}]")
    else:
        assert isinstance(value, JSON_LEAVES), f"{path}: {type(value).__name__} is not JSON"


# --- the reported failure ----------------------------------------------------


def test_channel_fields_are_json_serializable() -> None:
    """Regression: a real channel payload must survive ``json.dumps``.

    ``Channel.to_dict()`` carries a ``datetime`` in ``date`` and ``bytes`` in the
    chat photo, so this used to raise at flush time.
    """
    fields = serializers.chat_fields(FakeChannelEntity())

    assert_json_native(fields["raw_data"])
    assert json.loads(json.dumps(fields["raw_data"])) == fields["raw_data"]


def test_message_fields_are_json_serializable() -> None:
    """A message nests more than a channel does, and used to fail the same way."""
    fields = serializers.message_fields(FakeMessageEntity())

    assert_json_native(fields["raw_data"])
    assert json.loads(json.dumps(fields["raw_data"])) == fields["raw_data"]


def test_a_datetime_in_raw_data_becomes_iso_8601() -> None:
    raw = serializers.chat_fields(FakeChannelEntity())["raw_data"]

    assert raw["date"] == FIXED_DATE.isoformat()
    assert raw["date"].endswith("+00:00")


def test_nested_datetimes_survive_at_any_depth() -> None:
    """A datetime several levels down is normalised, not stringified as a repr.

    ``restrictions`` used to be ``str(list)``, which buried the ``until_date``
    datetime inside a Python repr and made the value unreadable to ``json.loads``
    consumers.
    """
    raw = serializers.chat_fields(FakeChannelEntity())["raw_data"]

    assert isinstance(raw["restrictions"], list)
    assert raw["restrictions"][0]["until_date"] == "2030-01-01T00:00:00+00:00"
    # A None date is preserved as None rather than turned into a string.
    assert raw["default_banned_rights"]["until_date"] is None
    # A datetime inside a message's forwards header and reply header.
    assert raw is not None
    message = serializers.message_fields(FakeMessageEntity())["raw_data"]
    assert message["edit_date"] == "2024-05-06T07:08:09+00:00"
    assert message["reply_to"]["reply_to_date"] == "2024-05-06T07:00:00+00:00"
    assert message["forwards"]["date"] == "2024-04-03T02:01:00+00:00"
    assert message["media"]["document"]["date"] == FIXED_DATE.isoformat()


# --- timezone handling -------------------------------------------------------


def test_a_non_utc_offset_is_preserved_not_converted() -> None:
    """Converting to UTC would silently discard the original offset."""
    moment = datetime(2024, 3, 1, 12, 30, 45, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    encoded = to_jsonable(moment)

    assert encoded == "2024-03-01T12:30:45+05:30"
    assert encoded != "2024-03-01T07:00:45+00:00"


def test_a_naive_datetime_is_not_given_an_invented_offset() -> None:
    """A naive value has no offset to keep; inventing one would be a lie."""
    assert to_jsonable(datetime(2024, 3, 1, 12, 30, 45)) == "2024-03-01T12:30:45"


def test_utc_offset_survives_several_levels_of_nesting() -> None:
    encoded = to_jsonable({"a": [{"b": {"c": (FIXED_DATE,)}}]})

    assert encoded == {"a": [{"b": {"c": [FIXED_DATE.isoformat()]}}]}


def test_date_and_time_are_encoded_too() -> None:
    assert to_jsonable(date(2024, 3, 1)) == "2024-03-01"
    assert to_jsonable(time(7, 8, 9)) == "07:08:09"


# --- bytes -------------------------------------------------------------------


def test_bytes_keep_their_content() -> None:
    """Regression: bytes became ``<bytes:16>``, which threw the payload away."""
    encoded = to_jsonable(THUMB_BYTES)

    assert encoded.startswith(BYTES_PREFIX)
    assert base64.b64decode(encoded[len(BYTES_PREFIX) :]) == THUMB_BYTES


def test_bytes_in_a_photo_stay_decodable() -> None:
    raw = serializers.chat_fields(FakeChannelEntity())["raw_data"]
    stripped = raw["photo"]["stripped_thumb"]

    assert base64.b64decode(stripped[len(BYTES_PREFIX) :]) == THUMB_BYTES
    # The 3-byte verification hash is preserved too, not replaced by its length.
    assert base64.b64decode(raw["verify"]["hash"][len(BYTES_PREFIX) :]) == b"\x01\x02\x03"


def test_bytes_encoding_is_deterministic() -> None:
    assert to_jsonable(b"\x00\xff") == to_jsonable(b"\x00\xff")
    assert to_jsonable(b"\x00\xff") != to_jsonable(b"\x00\xfe")


def test_bytearray_and_memoryview_are_handled_like_bytes() -> None:
    assert to_jsonable(bytearray(b"ab")) == to_jsonable(b"ab")
    assert to_jsonable(memoryview(b"ab")) == to_jsonable(b"ab")


# --- containers --------------------------------------------------------------


def test_tuples_become_arrays_and_keep_every_element() -> None:
    """A tuple used to become ``str(tuple)``: one line of text, no structure."""
    encoded = to_jsonable(("a", 1, None, (True,)))

    assert encoded == ["a", 1, None, [True]]


def test_lists_keep_nested_dictionaries() -> None:
    encoded = to_jsonable([{"a": 1}, [{"b": 2}]])

    assert encoded == [{"a": 1}, [{"b": 2}]]


def test_sets_are_sorted_so_the_snapshot_is_reproducible() -> None:
    encoded = to_jsonable({"z", "a", "m"})

    assert encoded == ["a", "m", "z"]
    assert to_jsonable({"z", "a", "m"}) == encoded


def test_mixed_sets_are_sorted_deterministically() -> None:
    encoded = to_jsonable({1, "a", None})

    assert encoded == to_jsonable({1, "a", None})
    assert sorted(map(repr, encoded)) == sorted(map(repr, [1, "a", None]))


def test_non_string_keys_become_strings() -> None:
    """JSON object keys are strings; the value is kept, the key is labelled."""
    encoded = to_jsonable({1: "one", None: "none", 2.5: "half"})

    assert encoded == {"1": "one", "None": "none", "2.5": "half"}


def test_enums_are_stored_as_their_value() -> None:
    class ChatKind(Enum):
        CHANNEL = "channel"
        DC = 2

    class Level(IntEnum := type("IntEnum", (), {})):  # noqa: N801 - local stand-in
        pass

    encoded = to_jsonable({"kind": ChatKind.CHANNEL, "dc": ChatKind.DC})

    assert encoded == {"kind": "channel", "dc": 2}


def test_a_real_int_enum_keeps_its_number() -> None:
    from enum import IntEnum

    class Level(IntEnum):
        LOW = 1
        HIGH = 2

    assert to_jsonable(Level.HIGH) == 2


def test_objects_without_to_dict_fall_back_to_public_attributes() -> None:
    class Plain:
        def __init__(self) -> None:
            self.id = 1
            self.title = "plain"
            self._secret = "hidden"
            self.date = FIXED_DATE

    encoded = to_jsonable(Plain())

    assert encoded == {"id": 1, "title": "plain", "date": FIXED_DATE.isoformat()}


def test_an_object_with_nothing_introspectable_is_marked_by_type() -> None:
    """Naming the type keeps the gap visible; an arbitrary repr would hide it."""
    encoded = to_jsonable(complex(1, 2))

    assert encoded == "<complex>"


def test_a_cyclic_payload_terminates() -> None:
    """``reply_to.reply_to`` is a real shape; it must not recurse forever."""
    payload: dict[str, Any] = {"id": 1}
    payload["reply_to"] = payload

    assert to_jsonable(payload) == {"id": 1, "reply_to": CYCLE_MARKER}


def test_a_cyclic_object_terminates() -> None:
    class Node:
        def __init__(self) -> None:
            self.name = "n"
            self.peer: Any = None

    first, second = Node(), Node()
    first.peer = second
    second.peer = first

    assert to_jsonable(first) == {"name": "n", "peer": {"name": "n", "peer": CYCLE_MARKER}}


def test_a_to_dict_that_raises_does_not_lose_the_row() -> None:
    encoded = to_jsonable(BrokenPayloadEntity())

    assert encoded["title"] == "Broken"
    assert encoded["date"] == FIXED_DATE.isoformat()


def test_a_slotted_entity_is_described_instead_of_crashing() -> None:
    """``vars()`` raises ``TypeError`` here, which used to abort the snapshot."""
    raw = serializers.serialize_raw(SlottedEntity())

    assert raw == {"id": 5, "title": "Slotted", "date": FIXED_DATE.isoformat()}


# --- the normalisation boundary ---------------------------------------------


def test_scalars_pass_through_untouched() -> None:
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable(False) is False
    assert to_jsonable(7) == 7
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable("text") == "text"
    assert to_jsonable("") == ""


def test_a_long_string_is_cut_and_marked() -> None:
    encoded = to_jsonable("x" * 50, limit=10)

    assert encoded == "x" * 10 + TRUNCATION_MARKER


def test_a_short_string_is_not_cut() -> None:
    assert to_jsonable("x" * 10, limit=10) == "x" * 10


def test_to_jsonable_dict_wraps_a_non_object() -> None:
    """``raw_data`` is a JSON object column, so a scalar needs a home."""
    assert to_jsonable_dict([1, 2]) == {"value": [1, 2]}
    assert to_jsonable_dict({"a": 1}) == {"a": 1}


def test_none_serializes_to_an_empty_document() -> None:
    assert serializers.serialize_raw(None) == {}


# --- unchanged behaviour for the scalar columns -----------------------------


def test_chat_columns_are_unchanged() -> None:
    """The columns the collector actually queries must not drift."""
    fields = serializers.chat_fields(FakeChannelEntity())

    assert fields["telegram_id"] == 1000000000
    assert fields["title"] == "Telegram"
    assert fields["username"] == "telegram"
    assert fields["type"] == "channel"
    assert fields["is_verified"] is True
    assert fields["members_count"] == 2_500_000


def test_user_columns_are_unchanged() -> None:
    user = FakeUser(id=7, username="@alice", first_name="Alice", about="hello")

    fields = serializers.user_fields(user)

    assert fields["telegram_id"] == 7
    assert fields["username"] == "alice"
    assert fields["first_name"] == "Alice"
    assert fields["bio"] == "hello"
    assert fields["is_bot"] is False


def test_message_columns_are_unchanged() -> None:
    fields = serializers.message_fields(FakeMessageEntity())

    assert fields["telegram_message_id"] == 42
    assert fields["text"] == "hello"
    assert fields["date"] == FIXED_DATE
    assert fields["views"] == 1234
    # Telethon hands back a ``MessageReplyHeader`` object, not the dict the raw
    # payload carries, so a dict reference yields no id. Unchanged behaviour.
    assert fields["reply_to_id"] is None


def test_reply_to_id_is_read_from_the_header_object() -> None:
    class Header:
        reply_to_msg_id = 41

    entity = FakeMessageEntity()
    entity.reply_to = Header()

    assert serializers.message_fields(entity)["reply_to_id"] == 41


def test_chat_type_names_are_unchanged() -> None:
    def chat(**flags: Any) -> str:
        entity = FakeChat(id=1, title="T", **flags)
        return serializers.chat_fields(entity)["type"]

    assert chat(megagroup=True) == "supergroup"
    assert chat(channel=True) == "channel"

    class Private:
        id, title, username = 1, "A Person", None
        user = True

    class Group:
        id, title, username = -1, "A Group", None
        group = True

    class Bot:
        id, title, username = 1, None, "a_bot"
        bot = True

    assert serializers.chat_fields(Private())["type"] == "private"
    assert serializers.chat_fields(Group())["type"] == "group"
    assert serializers.chat_fields(Bot())["type"] == "bot"


def test_plain_scalars_still_serialize_as_before() -> None:
    """A chat with no nested payload keeps the snapshot the old code produced."""
    chat = FakeChat(id=-1001234567890, title="Example", username="example")
    raw = serializers.chat_fields(chat)["raw_data"]

    assert raw == {
        "id": -1001234567890,
        "title": "Example",
        "username": "example",
        "participants_count": None,
    }


def test_determinism_across_repeated_serialisation() -> None:
    first = serializers.chat_fields(FakeChannelEntity())["raw_data"]
    second = serializers.chat_fields(FakeChannelEntity())["raw_data"]

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- the database round-trip -------------------------------------------------


def test_upsert_chat_flushes_a_realistic_serialized_chat(database: Database) -> None:
    """Regression: this exact statement used to raise ``StatementError``."""
    fields = serializers.chat_fields(FakeChannelEntity())
    payload = {key: value for key, value in fields.items() if key != "telegram_id"}

    with database.session() as session:
        chat = queries.upsert_chat(session, fields["telegram_id"], **payload)
        stored = session.get(type(chat), chat.id)

    assert stored is not None
    assert stored.title == "Telegram"
    assert stored.type == "channel"


def test_raw_data_round_trips_through_the_database(database: Database) -> None:
    """Whatever goes in must come back out as plain ``json.loads`` data."""
    fields = serializers.chat_fields(FakeChannelEntity())
    payload = {key: value for key, value in fields.items() if key != "telegram_id"}

    with database.session() as session:
        chat = queries.upsert_chat(session, fields["telegram_id"], **payload)
        chat_id = chat.id

    with database.session() as session:
        stored = queries.get_chat_by_telegram_id(session, 1000000000)
        assert stored is not None and stored.id == chat_id
        raw = stored.raw_data

    assert raw == fields["raw_data"]
    # No object hooks, no custom decoder: plain json is enough.
    assert json.loads(json.dumps(raw)) == fields["raw_data"]
    assert raw["date"] == FIXED_DATE.isoformat()
    assert base64.b64decode(raw["photo"]["stripped_thumb"][len(BYTES_PREFIX) :]) == THUMB_BYTES
    assert raw["restrictions"][0]["until_date"] == "2030-01-01T00:00:00+00:00"


def test_re_upserting_the_same_chat_stays_idempotent(database: Database) -> None:
    """A second collection re-normalises the same payload without a conflict."""
    fields = serializers.chat_fields(FakeChannelEntity())
    payload = {key: value for key, value in fields.items() if key != "telegram_id"}

    with database.session() as session:
        queries.upsert_chat(session, fields["telegram_id"], **payload)
    with database.session() as session:
        queries.upsert_chat(session, fields["telegram_id"], **payload)

    with database.session() as session:
        chats = queries.get_chat_by_telegram_id(session, 1000000000)

    assert chats is not None
    assert chats.raw_data == fields["raw_data"]


def test_a_message_payload_round_trips(database: Database) -> None:
    fields = serializers.message_fields(FakeMessageEntity())
    payload = {key: value for key, value in fields.items() if key != "telegram_message_id"}

    with database.session() as session:
        chat = queries.upsert_chat(session, 1000000000, title="Telegram")
        queries.upsert_message(session, chat, 42, **payload)

    with database.session() as session:
        chat = queries.get_chat_by_telegram_id(session, 1000000000)
        assert chat is not None
        message = queries.get_message(session, chat, 42)

    assert message is not None
    assert message.raw_data == fields["raw_data"]
    assert json.loads(json.dumps(message.raw_data)) == fields["raw_data"]


@pytest.mark.parametrize("factory", [channel_to_dict, message_to_dict])
def test_every_real_payload_is_json_native(factory: Any) -> None:
    """Guards the whole shape, not the fields one test happens to assert on."""
    assert_json_native(to_jsonable(factory()))
