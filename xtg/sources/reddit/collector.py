"""Reddit collector: subreddits, submissions and comments into SQLite.

Rules shared with the Telegram collector:

* every write goes through the idempotent ``upsert_reddit_*`` helpers, so
  re-running a collection updates counters instead of inserting duplicates;
* a collection can be interrupted and repeated: submissions are requested with
  Reddit's ``after`` cursor when the subreddit already has stored posts, and
  duplicates are still counted rather than stored;
* API calls are paced (``XTG_REQUEST_DELAY``) and every listing is bounded by
  the caller's ``limit``;
* media is metadata only. Previews, images and galleries are *not* downloaded:
  only the link target is stored, which is the interesting part for OSINT;
* every row keeps the original payload in ``raw_data`` and every relationship
  is also written as a :class:`~xtg.database.models.Relation` edge, so a Reddit
  post and a Telegram message end up in the same knowledge graph.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from xtg.core.errors import AuthError, CollectionError, NotFoundError, XTGError
from xtg.core.logger import logger
from xtg.database import queries
from xtg.database.models import RedditComment, RedditPost, RedditSubreddit
from xtg.database.reddit_writer import RedditBatchWriter
from xtg.database.writer import WriteResult
from xtg.sources.base import BaseCollector, SavedItem, SourceReport
from xtg.sources.reddit import serializers

#: Subreddit listings Reddit accepts, mapped to the PRAW method name.
LISTINGS: tuple[str, ...] = ("new", "hot", "top", "controversial", "rising")


def _without(fields: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """Copy of ``fields`` without ``keys``; the batch writer owns those columns."""
    return {key: value for key, value in fields.items() if key not in keys}


#: Item kind -> the plural counter name used in reports and CLI tables, so the
#: Reddit report reads like the Telegram one (``posts``, ``comments``, ...).
COUNT_NAME: dict[str, str] = {
    "subreddit": "subreddit",
    "post": "posts",
    "comment": "comments",
    "user": "users",
}


@dataclass
class SubredditTarget:
    """Resolved collection target: the name plus the PRAW handle."""

    name: str
    handle: Any

    @property
    def label(self) -> str:
        return f"r/{self.name}"


class RedditCollector(BaseCollector):
    """Walk a subreddit and store posts, comments, users and the graph edges."""

    source = "reddit"
    sections = {
        "subreddit": "collect_subreddit",
        "posts": "collect_posts",
        "comments": "collect_comments",
    }

    def __init__(self, client: Any, database: Any, **options: Any) -> None:
        listing = str(options.pop("listing", "new") or "new")
        resume = bool(options.pop("resume", True))
        super().__init__(client, database, **options)
        self.listing = listing if listing in LISTINGS else "new"
        #: Resume uses Reddit's ``after`` cursor: a repeated run only fetches
        #: submissions newer than the newest stored one. Turning it off re-walks
        #: the listing, which refreshes scores and comment counts at the cost of
        #: more API calls.
        self.resume = resume

    # --- hooks -------------------------------------------------------------
    def resolve(self, session: Session, target: str) -> SubredditTarget:  # noqa: ARG002 - the subreddit row is written on demand
        """Resolve ``linux`` / ``r/linux`` / ``/r/linux/`` into a PRAW handle."""
        name = queries.normalize_reddit_name(target)
        if not name or "/" in name:
            raise NotFoundError(
                f"{target!r} is not a subreddit name",
                hint="Use the plain name, for example `xtg reddit collect linux`.",
            )
        return SubredditTarget(name=name, handle=self._handle(name))

    def parse(self, raw: Any) -> dict[str, Any]:
        """PRAW object -> column mapping, ``kind`` included."""
        kind = serializers.kind_of(raw)
        payload: dict[str, Any] = {"kind": kind}
        if kind == "post":
            payload.update(serializers.post_fields(raw))
        elif kind == "comment":
            payload.update(serializers.comment_fields(raw))
        elif kind == "user":
            payload.update(serializers.user_fields(raw))
        elif kind == "subreddit":
            payload.update(
                serializers.subreddit_fields(raw, getattr(raw, "display_name", "") or "")
            )
        else:  # pragma: no cover - kind_of raises for unknown prefixes
            raise ValueError(f"Unsupported Reddit object: {kind}")
        author = serializers.author(raw)
        if author is not None and kind != "user":
            payload["author"] = serializers.user_fields(author)
        return payload

    def save(self, session: Session, parsed: Mapping[str, Any]) -> SavedItem:
        """Persist one parsed Reddit item and report whether it was new."""
        fields = dict(parsed)
        kind = str(fields.pop("kind", ""))
        if kind == "subreddit":
            return self._save_subreddit(session, fields)
        if kind == "user":
            reddit_id = str(fields.pop("reddit_id"))
            existed = queries.get_reddit_user(session, reddit_id) is not None
            row = queries.upsert_reddit_user(session, reddit_id, **fields)
            return SavedItem(kind=COUNT_NAME[kind], ref=row.ref, row=row, created=not existed)

        author = self._save_author(session, fields.pop("author", None))
        if kind == "post":
            return self._save_post(session, fields, author)
        if kind == "comment":
            return self._save_comment(session, fields, author)
        raise ValueError(f"Unsupported Reddit kind: {kind!r}")

    # --- sections ----------------------------------------------------------
    def collect_subreddit(
        self,
        context: SubredditTarget,
        *,
        limit: int,  # noqa: ARG002 - a single subreddit row has no paging
        report: SourceReport,
    ) -> None:
        """Store the subreddit metadata."""
        parsed = {
            "kind": "subreddit",
            **serializers.subreddit_fields(context.handle, context.name),
        }
        with self.database.session() as session:
            self._account(self.save(session, parsed), report)

    def collect_posts(self, context: SubredditTarget, *, limit: int, report: SourceReport) -> None:
        """Store submissions newest first, resuming after the newest known post.

        Rows are buffered and written by
        :class:`~xtg.database.reddit_writer.RedditBatchWriter`, which collapses
        the per-item lookups into a few statements per batch. The ``after`` token
        of the last stored submission rides along in the same transaction, so an
        interrupted listing resumes where it stopped.
        """
        with self.database.session() as session:
            subreddit = self._subreddit_row(session, context)
            newest = queries.newest_reddit_post(session, subreddit) if subreddit else None
            cursor = newest.reddit_id if (newest is not None and self.resume) else None
        if cursor:
            logger.debug("Resuming r/{} after {}", context.name, cursor)

        writer = RedditBatchWriter(
            self.database, batch_size=self.batch_size, target=context.name, section="posts"
        )
        newest_seen = cursor
        for submission in self._listing(context, limit=limit, after=cursor):
            try:
                parsed = self.parse(submission)
                parsed["subreddit"] = context.name
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the section
                report.errors.append(f"post: {exc}")
                logger.debug("Could not parse a Reddit post: {}", exc)
                continue
            newest_seen = str(parsed.get("reddit_id") or newest_seen)
            writer.add_post(
                _without(parsed, ("kind", "subreddit")),
                author=parsed.get("author"),
                relations=self._post_edges(parsed),
                entities=self._post_entities(parsed),
            )
            if writer.full:
                result = writer.flush()
                self._account_batch(result, report)
        writer.set_cursor(newest_seen)
        result = writer.close()
        self._account_batch(result, report)

    def _post_edges(self, parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Graph edges of one submission: subreddit -> post, author -> post."""
        ref = f"reddit:post:{parsed.get('reddit_id')}"
        edges = [
            {
                "source": f"reddit:subreddit:r/{self._target_name(parsed)}",
                "target": ref,
                "relation_type": "contains",
            }
        ]
        author = parsed.get("author")
        if author and author.get("name"):
            edges.append(
                {
                    "source": f"reddit:user:{author['name']}",
                    "target": ref,
                    "relation_type": "authored",
                }
            )
        return edges

    def _post_entities(self, parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Pivots of one submission: the link domain and any hashtags."""
        ref = f"reddit:post:{parsed.get('reddit_id')}"
        entities: list[dict[str, Any]] = []
        domain = serializers.domain_of(parsed.get("url"))
        if domain:
            entities.append(
                {
                    "type": "domain",
                    "value": domain,
                    "source": self.source,
                    "first_seen_ref": ref,
                }
            )
        for tag in serializers.hashtags(parsed.get("title")):
            entities.append(
                {
                    "type": "hashtag",
                    "value": tag,
                    "source": self.source,
                    "first_seen_ref": ref,
                }
            )
        return entities

    @staticmethod
    def _target_name(parsed: Mapping[str, Any]) -> str:
        return str(parsed.get("subreddit") or parsed.get("target") or "unknown")

    @staticmethod
    def _account_batch(result: WriteResult, report: SourceReport) -> None:
        """Fold one flushed batch into the report.

        ``counts`` totals everything the batch touched (new plus already known),
        matching what :meth:`SourceReport.add` recorded per item before;
        ``duplicated`` carries the already-known rows.
        """
        report.counts["posts"] = report.counts.get("posts", 0) + result.inserted + result.updated
        report.duplicated += result.updated
        if result.relations:
            report.counts["relations"] = report.counts.get("relations", 0) + result.relations
        if result.entities:
            report.counts["entities"] = report.counts.get("entities", 0) + result.entities

    def collect_comments(
        self, context: SubredditTarget, *, limit: int, report: SourceReport
    ) -> None:
        """Store comments of the newest stored submissions.

        ``MoreComments`` placeholders are skipped on purpose: expanding them
        costs extra API calls and pulls content nobody asked for.
        """
        with self.database.session() as session:
            subreddit = queries.get_reddit_subreddit(session, context.name)
            posts = list(queries.list_reddit_posts(session, subreddit, limit=limit))
        if not posts:
            raise NotFoundError(
                f"No stored submissions for r/{context.name}",
                hint=f"Run `xtg reddit collect r/{context.name} --posts` first.",
            )

        budget = limit
        for post in posts:
            if budget <= 0:
                break
            try:
                comments = [
                    {**parsed, "depth": depth} for parsed, depth in self._comments_of(post, budget)
                ]
            except Exception as exc:  # noqa: BLE001 - one post must not stop the section
                report.errors.append(f"comments {post.reddit_id}: {exc}")
                logger.warning("Cannot read the comments of {}: {}", post.reddit_id, exc)
                continue
            if not comments:
                continue
            budget -= len(comments)
            with self.database.session() as session:
                for parsed in comments:
                    try:
                        self._account(self.save(session, parsed), report)
                    except Exception as exc:  # noqa: BLE001 - keep the rest of the batch
                        report.errors.append(f"comment {parsed.get('reddit_id')}: {exc}")
                        logger.debug("Could not store a Reddit comment: {}", exc)

    # --- PRAW helpers ------------------------------------------------------
    def _handle(self, name: str) -> Any:
        try:
            return self.client.subreddit(name)
        except Exception as exc:  # noqa: BLE001 - mapped to a clean CLI error
            raise self._map_error(f"Cannot open r/{name}", exc) from exc

    @staticmethod
    def _map_error(message: str, exc: Exception) -> XTGError:
        """Translate a PRAW/prawcore failure into a typed error.

        PRAW raises prawcore exceptions whose class name says what happened
        (``NotFound``, ``TooManyRequests``, ``ResponseException``, ...), which
        is the only portable way to classify them without importing prawcore.
        """
        name = type(exc).__name__
        if "NotFound" in name or "Redirect" in name:
            return NotFoundError(f"{message}: {exc}")
        if "TooManyRequests" in name:
            return CollectionError(
                f"{message}: Reddit rate limit reached",
                hint="Lower XTG_REQUEST_DELAY and try again later.",
            )
        if "Forbidden" in name or "Unauthorized" in name:
            return AuthError(f"{message}: {exc}")
        return CollectionError(f"{message}: {exc}")

    def _listing(self, context: SubredditTarget, *, limit: int, after: str | None) -> Any:
        """Submission listing for a subreddit, bounded by ``limit``."""
        method = getattr(context.handle, self.listing, None)
        if method is None:  # pragma: no cover - guarded by LISTINGS
            raise CollectionError(f"Unknown listing {self.listing!r}")
        kwargs: dict[str, Any] = {"limit": max(1, limit)}
        if after:
            # Reddit's own resume cursor: only items newer than `after`.
            kwargs["params"] = {"after": after}
        try:
            return method(**kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped to a clean CLI error
            raise CollectionError(f"Cannot list r/{context.name}: {exc}") from exc

    def _comments_of(self, post: RedditPost, limit: int) -> Iterator[tuple[dict[str, Any], int]]:
        """Depth-first walk over a submission's comment forest.

        The depth comes from the walk, not from the API, so a truncated tree is
        stored with the depth it was actually seen at.
        """
        identifier = post.reddit_id.split("_", 1)[-1]
        try:
            submission = self.client.submission(identifier)
            root = list(getattr(submission, "comments", []) or [])
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            raise self._map_error(f"Cannot read the comments of {post.reddit_id}", exc) from exc
        produced = 0
        stack: list[tuple[Any, int]] = [(comment, 0) for comment in reversed(root)]
        while stack and produced < limit:
            comment, depth = stack.pop()
            if type(comment).__name__ == "MoreComments":
                continue
            produced += 1
            yield self.parse(comment), depth
            replies = getattr(comment, "replies", None)
            if replies:
                stack.extend((reply, depth + 1) for reply in reversed(list(replies)))

    def _subreddit_row(self, session: Session, context: SubredditTarget) -> RedditSubreddit:
        """Stored subreddit, creating it from the API on first use."""
        subreddit = queries.get_reddit_subreddit(session, context.name)
        if subreddit is None:
            parsed = {
                "kind": "subreddit",
                **serializers.subreddit_fields(context.handle, context.name),
            }
            subreddit = self.save(session, parsed).row
        return subreddit

    # --- persistence helpers -----------------------------------------------
    def _save_subreddit(self, session: Session, fields: dict[str, Any]) -> SavedItem:
        reddit_id = str(fields.pop("reddit_id"))
        name = str(fields["name"])
        existed = queries.get_reddit_subreddit(session, name) is not None
        row = queries.upsert_reddit_subreddit(session, reddit_id, **fields)
        return SavedItem(kind="subreddit", ref=row.ref, row=row, created=not existed)

    def _save_author(self, session: Session, author: Mapping[str, Any] | None) -> Any:
        if not author:
            return None
        fields = dict(author)
        reddit_id = str(fields.pop("reddit_id"))
        return queries.upsert_reddit_user(session, reddit_id, **fields)

    def _save_post(self, session: Session, fields: dict[str, Any], author: Any) -> SavedItem:
        subreddit_name = fields.pop("subreddit", None)
        subreddit = self._subreddit_by_name(session, subreddit_name)
        reddit_id = str(fields.pop("reddit_id"))
        existed = queries.get_reddit_post(session, reddit_id) is not None
        post = queries.upsert_reddit_post(
            session, reddit_id, subreddit=subreddit, author=author, **fields
        )
        self._link_post(session, post, subreddit, author)
        return SavedItem(kind="posts", ref=post.ref, row=post, created=not existed)

    def _save_comment(self, session: Session, fields: dict[str, Any], author: Any) -> SavedItem:
        parent = str(fields.pop("parent_reddit_id", "") or "")
        post = queries.get_reddit_post(session, parent) if parent.startswith("t3_") else None
        reddit_id = str(fields.pop("reddit_id"))
        existed = queries.get_reddit_comment(session, reddit_id) is not None
        comment = queries.upsert_reddit_comment(
            session, reddit_id, post=post, author=author, **fields
        )
        self._link_comment(session, comment, post, author)
        return SavedItem(kind="comments", ref=comment.ref, row=comment, created=not existed)

    def _subreddit_by_name(self, session: Session, name: str | None) -> RedditSubreddit | None:
        return queries.get_reddit_subreddit(session, name) if name else None

    def _flush_posts(
        self, context: SubredditTarget, batch: list[dict[str, Any]], report: SourceReport
    ) -> None:
        with self.database.session() as session:
            subreddit = self._subreddit_row(session, context)
            for parsed in batch:
                try:
                    item = self.save(session, {**parsed, "subreddit": subreddit.name})
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the run
                    report.errors.append(f"post: {exc}")
                    logger.debug("Could not store a Reddit post: {}", exc)
                    continue
                self._account(item, report)
        self.notify("posts", report.counts.get("posts", 0), 0)

    @staticmethod
    def _account(item: SavedItem, report: SourceReport) -> None:
        """Record one stored item, counting duplicates."""
        report.add(item.kind, created=item.created)

    # --- knowledge graph ---------------------------------------------------
    def _link_post(self, session: Session, post: RedditPost, subreddit: Any, author: Any) -> None:
        """Graph edges and extracted pivots of a submission."""
        if subreddit is not None:
            queries.add_relation(session, subreddit.ref, post.ref, "contains")
        if author is not None:
            queries.add_relation(session, author.ref, post.ref, "authored")
        domain = serializers.domain_of(post.url)
        if domain:
            queries.upsert_entity(
                session, "domain", domain, source=self.source, first_seen_ref=post.ref
            )
        for tag in serializers.hashtags(post.title):
            queries.upsert_entity(
                session, "hashtag", tag, source=self.source, first_seen_ref=post.ref
            )

    def _link_comment(
        self, session: Session, comment: RedditComment, post: Any, author: Any
    ) -> None:
        """Graph edges of a comment."""
        if post is not None:
            queries.add_relation(session, post.ref, comment.ref, "has_comment")
        if author is not None:
            queries.add_relation(session, author.ref, comment.ref, "commented")
