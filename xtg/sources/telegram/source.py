"""Telegram source: the Phase 2 collector behind the generic source contract.

Nothing here rewrites :mod:`xtg.telegram` - the connection, the authentication
and the async collector stay exactly as they are. This module is the adapter
that presents them as a :class:`~xtg.sources.base.BaseSource`:

* :meth:`TelegramSource.connect` / :meth:`TelegramSource.close` drive the async
  connection through ``asyncio.run`` at the boundary, so the CLI stays
  synchronous;
* :meth:`TelegramCollectorAdapter.parse` / :meth:`~TelegramCollectorAdapter.save`
  map Telegram objects onto the same contract the Reddit source implements;
* :meth:`TelegramCollectorAdapter.collect` translates the Telegram
  ``CollectionReport`` into a :class:`~xtg.sources.base.SourceReport`.

The async engine is the one thing the base class cannot abstract away, so
:meth:`TelegramCollectorAdapter.collect` overrides the base implementation; the
section vocabulary is still declared through ``sections``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from xtg.core.logger import logger
from xtg.core.metrics import Metrics
from xtg.core.retry import RetryPolicy
from xtg.database import queries
from xtg.sources.base import BaseCollector, BaseSource, SavedItem, SourceReport
from xtg.telegram import serializers
from xtg.telegram.client import TelegramConnection
from xtg.telegram.collector import TelegramCollector, store_chat, store_message, store_user


def detect_kind(raw: Any) -> str:
    """Guess the kind of a raw Telegram object by duck typing.

    The order matters: a message carries a peer id or media attributes, a chat
    has a title, a username or a participant count, and whatever is left with a
    first name is a user. Callers that already know the kind should pass it to
    :meth:`TelegramCollectorAdapter.parse`.
    """
    if hasattr(raw, "entity_id") or hasattr(raw, "peer_id") or hasattr(raw, "media"):
        return "message"
    if hasattr(raw, "participants_count") or hasattr(raw, "title") or hasattr(raw, "username"):
        return "chat"
    return "user"


def chat_ref(chat: Any) -> str:
    """Stable graph reference for a stored chat."""
    username = getattr(chat, "username", None)
    title = getattr(chat, "title", None)
    telegram_id = getattr(chat, "telegram_id", "?")
    return (
        f"chat:@{username}" if username else f"chat:{telegram_id}" if not title else f"chat:{title}"
    )


class TelegramCollectorAdapter(BaseCollector):
    """Expose the async Telegram collector through the base collector contract."""

    source = "telegram"
    sections = {
        "messages": "collect_messages",
        "media": "collect_messages",
        "members": "collect_members",
    }

    def __init__(self, client: Any, database: Any, **options: Any) -> None:
        #: Run-wide metrics, exposed through ``SourceReport.details`` so the CLI
        #: can report throughput without the collector knowing about output.
        self.metrics = options.pop("metrics", None) or Metrics()
        # Options the base engine understands; the rest configure the async
        # delegate (queue size, resume, retry policy).
        base_options = {
            name: options.pop(name)
            for name in ("delay", "batch_size", "progress")
            if name in options
        }
        super().__init__(client, database, **base_options)
        self.delegate = TelegramCollector(client, database, metrics=self.metrics, **options)

    # --- hooks -------------------------------------------------------------
    def resolve(self, session: Session, target: str) -> Any:  # noqa: ARG002 - async delegate
        """Resolve a target through the async delegate."""
        return asyncio.run(self.delegate.resolve(target))

    def parse(self, raw: Any, kind: str | None = None) -> dict[str, Any]:
        """Telegram object -> column mapping (``chat``, ``user`` or ``message``)."""
        resolved = kind or detect_kind(raw)
        if resolved == "message":
            columns: Mapping[str, Any] = serializers.message_fields(raw)
        elif resolved == "chat":
            columns = serializers.chat_fields(raw)
        else:
            columns = serializers.user_fields(raw)
        return {"kind": resolved, **columns}

    def save(self, session: Session, parsed: Mapping[str, Any]) -> SavedItem:
        """Store one parsed Telegram item and report whether it was new."""
        fields = dict(parsed)
        kind = str(fields.pop("kind", "message"))
        if kind == "chat":
            chat = store_chat(session, **fields)
            return SavedItem(kind=kind, ref=chat_ref(chat), row=chat, created=True)
        if kind == "user":
            user = store_user(session, **fields)
            return SavedItem(kind=kind, ref=f"user:{user.telegram_id}", row=user, created=True)

        telegram_id = fields.pop("telegram_message_id", None)
        if telegram_id is None:
            raise ValueError("message payload without telegram_message_id")
        chat = self._require_chat(session, fields.pop("chat", None))
        existing = queries.get_message(session, chat, int(telegram_id))
        message = store_message(session, chat, int(telegram_id), **fields)
        return SavedItem(
            kind="message",
            ref=f"message:{chat.telegram_id}:{telegram_id}",
            row=message,
            created=existing is None,
        )

    # --- engine ------------------------------------------------------------
    def collect(
        self,
        target: str,
        *,
        sections: Sequence[str] = (),
        limit: int = 100,
    ) -> SourceReport:
        """Run the async Telegram collector and translate its report."""
        report = asyncio.run(
            self.delegate.run(target, sections=list(sections) or ["messages"], limit=limit)
        )
        return SourceReport(
            source=self.source,
            target=report.chat or target,
            counts={
                "messages": report.messages,
                "members": report.members,
                "media": report.media,
            },
            duplicated=report.duplicated,
            errors=list(report.errors),
            details={
                "batches": report.batches,
                "cursor": report.cursor,
                "backpressure_waits": self.metrics.get("backpressure_waits"),
                "retries": self.metrics.get("retries"),
                "items_per_second": round(self.metrics.rate("messages_written"), 1),
            },
        )

    @staticmethod
    def _require_chat(session: Session, reference: Any) -> Any:
        """Resolve the chat a message payload belongs to."""
        if reference is None:
            raise ValueError("message payload without a chat reference")
        if hasattr(reference, "telegram_id"):
            return reference
        return queries.resolve_chat(session, str(reference))


class TelegramSource(BaseSource):
    """Telegram as a source: connect, parse, save, collect."""

    name = "telegram"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.connection: TelegramConnection | None = None

    def create_client(self) -> Any:
        """Open the MTProto connection (the async boundary of this adapter)."""
        self.connection = TelegramConnection(
            self.settings, require_authorized=self.require_authorized
        )
        return asyncio.run(self.connection.open())

    def create_collector(self, client: Any) -> BaseCollector:
        settings = self.settings
        return TelegramCollectorAdapter(
            client,
            self.database,
            delay=settings.request_delay,
            batch_size=settings.batch_size,
            queue_size=settings.queue_size,
            resume=settings.resume,
            retry_policy=RetryPolicy(
                attempts=settings.max_retries,
                base_delay=settings.retry_base_delay,
                max_delay=settings.retry_max_delay,
            ),
            progress=self._progress,
        )

    def collect(
        self,
        target: str,
        *,
        sections: Sequence[str] = (),
        limit: int = 100,
    ) -> SourceReport:
        """Connect, run the async collector and return a platform neutral report."""
        self.connect()
        return self.collector.collect(target, sections=sections, limit=limit)

    def close(self) -> None:
        """Disconnect when a connection was opened; never raises."""
        connection, self.connection = self.connection, None
        if connection is not None:
            asyncio.run(connection.close())
        super().close()

    @staticmethod
    def _progress(section: str, processed: int, limit: int) -> None:
        logger.debug("telegram {}: {}/{}", section, processed, limit)
