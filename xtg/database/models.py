"""SQLAlchemy models.

Design notes
------------
* Every entity table keeps ``raw_data`` (a JSON document with the original
  Telegram payload) so nothing is lost and later phases can re-parse the data.
* Integer primary keys are local surrogate keys; ``telegram_id`` keeps the
  upstream identifier, which is unique per table.
* :class:`Relation` and :class:`Entity` store *string* node references instead
  of foreign keys. That keeps the knowledge graph able to link users, chats,
  messages and extracted pivots (emails, domains, phones, ...) in one table
  without polymorphic foreign keys.
* ``Message`` and ``Membership`` carry uniqueness constraints so that
  re-running a collection is idempotent (``INSERT ... ON CONFLICT`` via
  :func:`xtg.database.queries.upsert_message`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base shared by every model."""

    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}

    def to_dict(self) -> dict[str, Any]:
        """Shallow column -> value mapping (used by ``--json`` output)."""
        return {column.name: getattr(self, column.name) for column in self.__table__.columns}


class TimestampMixin:
    """``created_at`` / ``updated_at`` bookkeeping columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    """A Telegram user or bot."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    bio: Mapped[str | None] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_premium: Mapped[bool | None] = mapped_column(default=None)
    phone: Mapped[str | None] = mapped_column(String(32))
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<User {self.telegram_id} {self.username or self.first_name or '?'}>"


class Chat(Base, TimestampMixin):
    """A Telegram chat: channel, group, supergroup or private dialog."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(256), index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    type: Mapped[str | None] = mapped_column(String(32), index=True)
    is_verified: Mapped[bool | None] = mapped_column(default=None)
    members_count: Mapped[int | None] = mapped_column(Integer)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Chat {self.telegram_id} {self.username or self.title or '?'}>"


class Membership(Base):
    """Link between a :class:`User` and a :class:`Chat`."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "chat_id", name="uq_membership_user_chat"),
        Index("ix_memberships_chat_role", "chat_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str | None] = mapped_column(String(64))
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    chat: Mapped[Chat] = relationship(back_populates="memberships")


class Message(Base, TimestampMixin):
    """A single message inside a chat."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "telegram_message_id", name="uq_message_chat_telegram_id"),
        Index("ix_messages_chat_date", "chat_id", "date"),
        Index("ix_messages_sender", "sender_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sender_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reply_to_id: Mapped[int | None] = mapped_column(Integer)
    views: Mapped[int | None] = mapped_column(Integer)
    forwards: Mapped[int | None] = mapped_column(Integer)
    has_media: Mapped[bool] = mapped_column(default=False, nullable=False)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Message {self.telegram_message_id} in chat {self.chat_id}>"


class Media(Base):
    """A media attachment linked to a :class:`Message`."""

    __tablename__ = "media"
    __table_args__ = (Index("ix_media_type", "type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    path: Mapped[str | None] = mapped_column(String(1024))
    file_name: Mapped[str | None] = mapped_column(String(512))
    file_size: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Media {self.type} message={self.message_id}>"


class Entity(Base, TimestampMixin):
    """An extracted intelligence pivot (email, domain, phone, hashtag, ...)."""

    __tablename__ = "entities"
    __table_args__ = (UniqueConstraint("type", "value", name="uq_entity_type_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    first_seen_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )


class Relation(Base, TimestampMixin):
    """A directed knowledge-graph edge between two node references.

    ``source`` / ``target`` use stable string references such as ``user:1234``,
    ``chat:@durov`` or ``entity:email:example.org``.
    """

    __tablename__ = "relations"
    __table_args__ = (
        UniqueConstraint("source", "target", "relation_type", name="uq_relation_edge"),
        Index("ix_relations_edge", "source", "target"),
        Index("ix_relations_type", "relation_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    target: Mapped[str] = mapped_column(String(512), index=True, nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    weight: Mapped[float] = mapped_column(default=1.0, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Relation {self.source} -[{self.relation_type}]-> {self.target}>"
