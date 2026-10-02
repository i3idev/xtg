"""Reusable query helpers.

Keeping SQL in one place means the CLI, the search engine and the future
knowledge-graph builder all read the dataset the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from xtg.core.errors import NotFoundError
from xtg.core.utils import normalize_entity_ref, to_utc
from xtg.database.models import (
    Chat,
    Entity,
    Media,
    Membership,
    Message,
    RedditComment,
    RedditPost,
    RedditSubreddit,
    RedditUser,
    Relation,
    User,
)

# --- counting ---------------------------------------------------------------


def count_rows(session: Session, *model: type[Any]) -> int:
    """Total number of rows for one or more models."""
    if not model:
        return 0
    if len(model) == 1:
        return session.scalar(select(func.count()).select_from(model[0])) or 0
    total = 0
    for item in model:
        total += session.scalar(select(func.count()).select_from(item)) or 0
    return total


def count_chat_messages(session: Session, chat: Chat) -> int:
    """Number of stored messages for ``chat``."""
    return session.scalar(select(func.count(Message.id)).where(Message.chat_id == chat.id)) or 0


# --- users ------------------------------------------------------------------


def get_user_by_telegram_id(session: Session, telegram_id: int) -> User | None:
    return session.scalar(select(User).where(User.telegram_id == telegram_id))


def get_user_by_username(session: Session, username: str) -> User | None:
    normalized = username.lstrip("@")
    return session.scalar(select(User).where(func.lower(User.username) == normalized.lower()))


def upsert_user(session: Session, telegram_id: int, **fields: Any) -> User:
    """Create or update a user row and return it."""
    user = get_user_by_telegram_id(session, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, raw_data={})
        session.add(user)
    for key, value in fields.items():
        if value is not None:
            setattr(user, key, value)
    session.flush()
    return user


def iter_users(session: Session, limit: int | None = None) -> Sequence[User]:
    statement: Select[tuple[User]] = select(User).order_by(User.telegram_id)
    if limit:
        statement = statement.limit(limit)
    return session.scalars(statement).all()


# --- chats ------------------------------------------------------------------


def get_chat_by_telegram_id(session: Session, telegram_id: int) -> Chat | None:
    return session.scalar(select(Chat).where(Chat.telegram_id == telegram_id))


def get_chat_by_username(session: Session, username: str) -> Chat | None:
    normalized = username.lstrip("@")
    return session.scalar(select(Chat).where(func.lower(Chat.username) == normalized.lower()))


def resolve_chat(session: Session, reference: str) -> Chat:
    """Resolve ``@name``, a numeric id or a ``t.me`` link to a stored chat."""
    kind, value = normalize_entity_ref(reference)
    chat: Chat | None = None
    if kind == "username":
        chat = get_chat_by_username(session, value)
    elif kind == "id":
        chat = get_chat_by_telegram_id(session, int(value))
    if chat is None:
        raise NotFoundError(
            f"No collected chat matches {reference!r}",
            hint="Collect it first: xtg collect @channel --messages",
        )
    return chat


def upsert_chat(session: Session, telegram_id: int, **fields: Any) -> Chat:
    """Create or update a chat row and return it."""
    chat = get_chat_by_telegram_id(session, telegram_id)
    if chat is None:
        chat = Chat(telegram_id=telegram_id, raw_data={})
        session.add(chat)
    for key, value in fields.items():
        if value is not None:
            setattr(chat, key, value)
    session.flush()
    return chat


# --- memberships ------------------------------------------------------------


def upsert_membership(session: Session, user: User, chat: Chat, role: str | None) -> Membership:
    membership = session.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.chat_id == chat.id)
    )
    if membership is None:
        membership = Membership(user_id=user.id, chat_id=chat.id, role=role, raw_data={})
        session.add(membership)
    elif role:
        membership.role = role
    session.flush()
    return membership


def list_chat_members(
    session: Session, chat: Chat, limit: int | None = None
) -> Sequence[Membership]:
    statement: Select[tuple[Membership]] = (
        select(Membership).where(Membership.chat_id == chat.id).order_by(Membership.id)
    )
    if limit:
        statement = statement.limit(limit)
    return session.scalars(statement).all()


# --- messages ---------------------------------------------------------------


def get_message(session: Session, chat: Chat, telegram_message_id: int) -> Message | None:
    return session.scalar(
        select(Message).where(
            Message.chat_id == chat.id, Message.telegram_message_id == telegram_message_id
        )
    )


def upsert_message(
    session: Session,
    chat: Chat,
    telegram_message_id: int,
    *,
    sender: User | None = None,
    **fields: Any,
) -> Message:
    """Insert or update a message, keeping collection runs idempotent."""
    message = get_message(session, chat, telegram_message_id)
    if message is None:
        message = Message(chat_id=chat.id, telegram_message_id=telegram_message_id, raw_data={})
        session.add(message)
    if sender is not None:
        message.sender_id = sender.id
    for key, value in fields.items():
        if value is not None:
            setattr(message, key, value)
    if isinstance(message.date, datetime):
        message.date = to_utc(message.date)
    session.flush()
    return message


def latest_message_id(session: Session, chat: Chat) -> int | None:
    """Newest stored message id of ``chat`` (used to resume a collection)."""
    return session.scalar(
        select(func.max(Message.telegram_message_id)).where(Message.chat_id == chat.id)
    )


def has_media(session: Session, message: Message) -> bool:
    """True when media rows were already stored for ``message``."""
    return bool(session.scalar(select(func.count(Media.id)).where(Media.message_id == message.id)))


def build_message_search(
    keyword: str,
    *,
    chat: Chat | None = None,
    sender: User | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    has_media: bool | None = None,
    order_by: str = "date",
    limit: int = 200,
) -> Select[tuple[Message]]:
    """Composable ``SELECT`` used by ``xtg search``."""
    pattern = f"%{keyword.lower()}%"
    text_match = func.lower(Message.text).like(pattern)
    numeric_id = _as_int(keyword)
    # A keyword matches the message body *or* the message id.
    conditions = [
        text_match
        if numeric_id is None
        else or_(text_match, Message.telegram_message_id == numeric_id)
    ]
    if chat is not None:
        conditions.append(Message.chat_id == chat.id)
    if sender is not None:
        conditions.append(Message.sender_id == sender.id)
    if since is not None:
        conditions.append(Message.date >= to_utc(since))
    if until is not None:
        conditions.append(Message.date <= to_utc(until))
    if has_media is not None:
        conditions.append(Message.has_media.is_(has_media))
    sort_column = Message.date if order_by == "date" else Message.id
    return select(Message).where(*conditions).order_by(sort_column.desc()).limit(max(1, limit))


def search_messages(session: Session, statement: Select[tuple[Message]]) -> Sequence[Message]:
    return session.scalars(statement).all()


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- media ------------------------------------------------------------------


def add_media(session: Session, message: Message, media_type: str, **fields: Any) -> Media:
    payload = fields.pop("metadata", None) or {}
    media = Media(message_id=message.id, type=media_type, metadata_=payload)
    for key, value in fields.items():
        setattr(media, key, value)
    session.add(media)
    session.flush()
    return media


# --- intelligence pivots ----------------------------------------------------


def upsert_entity(session: Session, entity_type: str, value: str, **fields: Any) -> Entity:
    """Register a pivot (email, domain, ...) and count the new occurrence."""
    entity = session.scalar(select(Entity).where(Entity.type == entity_type, Entity.value == value))
    if entity is None:
        entity = Entity(type=entity_type, value=value, metadata_={})
        session.add(entity)
    else:
        entity.occurrences += 1
    for key, field_value in fields.items():
        if field_value is not None:
            setattr(entity, key, field_value)
    session.flush()
    return entity


def add_relation(
    session: Session,
    source: str,
    target: str,
    relation_type: str,
    **fields: Any,
) -> Relation:
    """Create a graph edge, skipping exact duplicates."""
    relation = session.scalar(
        select(Relation).where(
            Relation.source == source,
            Relation.target == target,
            Relation.relation_type == relation_type,
        )
    )
    if relation is None:
        relation = Relation(
            source=source,
            target=target,
            relation_type=relation_type,
            metadata_=fields.pop("metadata", None) or {},
            **fields,
        )
        session.add(relation)
    session.flush()
    return relation


def search_entities(session: Session, entity_type: str, pattern: str) -> Sequence[Entity]:
    return session.scalars(
        select(Entity)
        .where(Entity.type == entity_type, Entity.value.ilike(f"%{pattern}%"))
        .order_by(Entity.occurrences.desc())
    ).all()


# --- Reddit -----------------------------------------------------------------
# The Reddit tables live in the same database as the Telegram ones and use the
# same rules: lookup by upstream id, then update, so a repeated collection
# updates counters instead of inserting duplicates.


def get_reddit_user(session: Session, reddit_id: str) -> RedditUser | None:
    return session.scalar(select(RedditUser).where(RedditUser.reddit_id == reddit_id))


def upsert_reddit_user(session: Session, reddit_id: str, **fields: Any) -> RedditUser:
    """Create or update a Reddit account."""
    user = get_reddit_user(session, reddit_id)
    if user is None:
        user = RedditUser(reddit_id=reddit_id, name=fields.pop("name", reddit_id), raw_data={})
        session.add(user)
    for key, value in fields.items():
        if value is not None:
            setattr(user, key, value)
    session.flush()
    return user


def get_reddit_subreddit(session: Session, name: str) -> RedditSubreddit | None:
    normalized = normalize_reddit_name(name)
    return session.scalar(
        select(RedditSubreddit).where(func.lower(RedditSubreddit.name) == normalized)
    )


def upsert_reddit_subreddit(session: Session, reddit_id: str, **fields: Any) -> RedditSubreddit:
    """Create or update a subreddit."""
    name = fields.pop("name", reddit_id)
    subreddit = session.scalar(
        select(RedditSubreddit).where(
            func.lower(RedditSubreddit.name) == normalize_reddit_name(name)
        )
    )
    if subreddit is None:
        subreddit = RedditSubreddit(
            reddit_id=reddit_id, name=normalize_reddit_name(name), raw_data={}
        )
        session.add(subreddit)
    for key, value in fields.items():
        if value is not None:
            setattr(subreddit, key, value)
    session.flush()
    return subreddit


def get_reddit_post(session: Session, reddit_id: str) -> RedditPost | None:
    return session.scalar(select(RedditPost).where(RedditPost.reddit_id == reddit_id))


def upsert_reddit_post(
    session: Session,
    reddit_id: str,
    *,
    subreddit: RedditSubreddit | None = None,
    author: RedditUser | None = None,
    **fields: Any,
) -> RedditPost:
    """Insert or update a submission, keeping collection runs idempotent."""
    post = get_reddit_post(session, reddit_id)
    if post is None:
        post = RedditPost(reddit_id=reddit_id, title=fields.get("title") or reddit_id, raw_data={})
        session.add(post)
    if subreddit is not None:
        post.subreddit_id = subreddit.id
    if author is not None:
        post.author_id = author.id
    for key, value in fields.items():
        if value is not None:
            setattr(post, key, value)
    if isinstance(post.created_utc, datetime):
        post.created_utc = to_utc(post.created_utc)
    session.flush()
    return post


def get_reddit_comment(session: Session, reddit_id: str) -> RedditComment | None:
    return session.scalar(select(RedditComment).where(RedditComment.reddit_id == reddit_id))


def upsert_reddit_comment(
    session: Session,
    reddit_id: str,
    *,
    post: RedditPost | None = None,
    author: RedditUser | None = None,
    **fields: Any,
) -> RedditComment:
    """Insert or update a comment."""
    comment = get_reddit_comment(session, reddit_id)
    if comment is None:
        comment = RedditComment(reddit_id=reddit_id, raw_data={})
        session.add(comment)
    if post is not None:
        comment.post_id = post.id
    if author is not None:
        comment.author_id = author.id
    for key, value in fields.items():
        if value is not None:
            setattr(comment, key, value)
    if isinstance(comment.created_utc, datetime):
        comment.created_utc = to_utc(comment.created_utc)
    session.flush()
    return comment


def list_reddit_posts(
    session: Session,
    subreddit: RedditSubreddit | None = None,
    *,
    limit: int | None = None,
) -> Sequence[RedditPost]:
    """Stored submissions, newest first (used to resume comment collection)."""
    statement: Select[tuple[RedditPost]] = select(RedditPost)
    if subreddit is not None:
        statement = statement.where(RedditPost.subreddit_id == subreddit.id)
    statement = statement.order_by(RedditPost.created_utc.desc())
    if limit:
        statement = statement.limit(limit)
    return session.scalars(statement).all()


def newest_reddit_post(session: Session, subreddit: RedditSubreddit) -> RedditPost | None:
    """Newest stored submission of a subreddit, used to resume a collection."""
    return session.scalar(
        select(RedditPost)
        .where(RedditPost.subreddit_id == subreddit.id)
        .order_by(RedditPost.created_utc.desc(), RedditPost.id.desc())
        .limit(1)
    )


def normalize_reddit_name(value: str) -> str:
    """``r/Linux`` / ``/r/linux/`` / ``linux`` -> ``linux``."""
    name = value.strip().strip("/")
    if name.lower().startswith("r/"):
        name = name[2:]
    return name.strip("/").lower()
