"""Tests for the Reddit connector: config, PRAW client, parsing and writing.

Nothing here touches the network or needs a Reddit account: PRAW is never
imported and the client is always a double.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from tests.fakes_reddit import (
    FakeReddit,
    FakeRedditor,
    FakeSubreddit,
    make_comments,
    make_subreddit,
)
from xtg.core.config import Settings, reload_settings
from xtg.core.errors import ConfigError, NotFoundError
from xtg.database import queries
from xtg.database.db import Database
from xtg.sources.reddit import RedditCollector, RedditSource, create_reddit, serializers


@pytest.fixture
def reddit_settings(settings: Settings) -> Settings:
    settings.reddit_client_id = "client-id"
    settings.reddit_client_secret = "client-secret"
    settings.reddit_user_agent = "xtg/0.1.0 by u/tester"
    return settings


@pytest.fixture
def subreddit() -> FakeSubreddit:
    return make_subreddit("linux", posts=3)


@pytest.fixture
def client(subreddit: FakeSubreddit) -> FakeReddit:
    return FakeReddit({"linux": subreddit}, user_agent="xtg/0.1.0 by u/tester")


def collector_for(client: Any, database: Database, **options: Any) -> RedditCollector:
    return RedditCollector(client, database, **options)


# --- configuration ----------------------------------------------------------


def test_reddit_credentials_are_unset_by_default(settings: Settings) -> None:
    assert settings.reddit_client_id is None
    assert settings.has_reddit_credentials is False
    assert settings.has_reddit_account is False


def test_blank_reddit_values_mean_unset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XTG_HOME", str(tmp_path))
    monkeypatch.setenv("REDDIT_CLIENT_ID", "  ")
    config = reload_settings()
    assert config.reddit_client_id is None


def test_bare_reddit_env_names_are_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XTG_HOME", str(tmp_path))
    monkeypatch.setenv("REDDIT_CLIENT_ID", "bare-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "bare-secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "bare-agent")
    config = reload_settings()

    assert config.reddit_client_id == "bare-id"
    assert config.has_reddit_credentials is True
    assert config.reddit_client_kwargs() == {
        "client_id": "bare-id",
        "client_secret": "bare-secret",
        "user_agent": "bare-agent",
    }


def test_prefixed_reddit_env_names_are_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XTG_HOME", str(tmp_path))
    monkeypatch.setenv("XTG_REDDIT_CLIENT_ID", "prefixed-id")
    monkeypatch.setenv("XTG_REDDIT_CLIENT_SECRET", "prefixed-secret")
    monkeypatch.setenv("XTG_REDDIT_USER_AGENT", "prefixed-agent")
    config = reload_settings()

    assert config.reddit_client_id == "prefixed-id"
    assert config.has_reddit_credentials is True


def test_optional_account_is_added_when_complete(reddit_settings: Settings) -> None:
    reddit_settings.reddit_username = "tester"
    reddit_settings.reddit_password = "secret-password"

    kwargs = reddit_settings.reddit_client_kwargs()

    assert kwargs["username"] == "tester"
    assert kwargs["password"] == "secret-password"
    assert reddit_settings.has_reddit_account is True


def test_half_configured_account_is_rejected(reddit_settings: Settings) -> None:
    reddit_settings.reddit_username = "tester"
    with pytest.raises(ConfigError, match="together"):
        reddit_settings.require_reddit_credentials()


def test_missing_credentials_are_reported(settings: Settings) -> None:
    with pytest.raises(ConfigError) as exc:
        settings.require_reddit_credentials()
    assert "REDDIT_CLIENT_ID" in str(exc.value)
    assert "REDDIT_USER_AGENT" in str(exc.value)


def test_secrets_are_never_rendered(reddit_settings: Settings) -> None:
    reddit_settings.reddit_password = "super-secret"
    redacted = reddit_settings.redacted()

    assert "super-secret" not in str(redacted)
    assert "client-secret" not in str(redacted)
    assert redacted["reddit_configured"] is True


def test_listing_setting_is_lowercased(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XTG_HOME", str(tmp_path))
    monkeypatch.setenv("REDDIT_LISTING", "TOP")
    assert reload_settings().reddit_listing == "top"


# --- PRAW client ------------------------------------------------------------


def test_create_reddit_passes_the_credentials(reddit_settings: Settings) -> None:
    built: dict[str, Any] = {}

    def factory(**kwargs: Any) -> str:
        built.update(kwargs)
        return "reddit-client"

    assert create_reddit(reddit_settings, factory=factory) == "reddit-client"
    assert built == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "user_agent": "xtg/0.1.0 by u/tester",
    }


def test_create_reddit_adds_the_account(reddit_settings: Settings) -> None:
    reddit_settings.reddit_username = "tester"
    reddit_settings.reddit_password = "pw"
    built: dict[str, Any] = {}

    create_reddit(reddit_settings, factory=lambda **fields: built.update(fields))

    assert built["username"] == "tester"
    assert built["password"] == "pw"


def test_create_reddit_forwards_the_proxy(reddit_settings: Settings) -> None:
    built: dict[str, Any] = {}
    create_reddit(
        reddit_settings,
        factory=lambda **kwargs: built.update(kwargs),
        proxies="socks5://127.0.0.1:1080",
    )
    assert built["proxies"] == "socks5://127.0.0.1:1080"


def test_create_reddit_requires_credentials(settings: Settings) -> None:
    with pytest.raises(ConfigError):
        create_reddit(settings, factory=lambda **_fields: None)


def test_create_reddit_maps_rejected_credentials(reddit_settings: Settings) -> None:
    def factory(**_kwargs: Any) -> None:
        raise ValueError("invalid client id")

    with pytest.raises(ConfigError, match="PRAW refused"):
        create_reddit(reddit_settings, factory=factory)


def test_praw_absence_is_a_config_error(reddit_settings: Settings, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "praw":
            raise ImportError("no praw")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ConfigError) as exc:
        create_reddit(reddit_settings)
    assert "PRAW is not installed" in str(exc.value)


def test_describe_client_never_leaks_secrets(reddit_settings: Settings) -> None:
    from xtg.sources.reddit import describe_client

    client = create_reddit(reddit_settings, factory=FakeReddit)
    described = describe_client(client)
    assert described["read_only"] is True
    assert described["user_agent"] == "xtg/0.1.0 by u/tester"
    assert "client-secret" not in str(described)


# --- serializers ------------------------------------------------------------


def test_fullname_and_kind_of() -> None:
    assert serializers.fullname(FakeRedditor(id="a1", name="alice")) == "t2_a1"
    assert serializers.kind_of(FakeRedditor(id="a1", name="alice")) == "user"


def test_post_fields_map_the_model_columns(subreddit: FakeSubreddit) -> None:
    fields = serializers.post_fields(subreddit.posts[0])

    assert fields["reddit_id"] == "t3_post1"
    assert fields["title"] == "Thread #1 about #security"
    assert fields["url"] == "https://example1.org/article"
    assert fields["domain"] == "example1.org"
    assert fields["score"] == 3
    assert isinstance(fields["created_utc"], datetime)
    assert fields["created_utc"].tzinfo is UTC
    assert fields["raw_data"]["id"] == "post1"


def test_comment_fields(subreddit: FakeSubreddit) -> None:
    make_comments(subreddit)
    comment = subreddit.posts[0].comments[0]

    fields = serializers.comment_fields(comment)

    assert fields["reddit_id"] == comment.fullname
    assert fields["parent_reddit_id"] == "t3_post1"
    assert fields["body"] == "Top level comment"
    assert fields["depth"] == 0


def test_user_fields() -> None:
    fields = serializers.user_fields(FakeRedditor(id="a1", name="alice", comment_karma=12))

    assert fields["name"] == "alice"
    assert fields["comment_karma"] == 12
    assert fields["verified"] is False


def test_subreddit_fields_normalises_the_name(subreddit: FakeSubreddit) -> None:
    fields = serializers.subreddit_fields(subreddit, "r/Linux")
    assert fields["name"] == "linux"
    assert fields["reddit_id"] == "t5_2lin"


def test_domain_of() -> None:
    assert serializers.domain_of("https://news.example.org/a?b=1") == "news.example.org"
    assert serializers.domain_of("self") is None
    assert serializers.domain_of(None) is None


def test_hashtags() -> None:
    assert serializers.hashtags("About #security and #osint, #Security") == ["osint", "security"]
    assert serializers.hashtags(None) == []


def test_raw_data_is_bounded(subreddit: FakeSubreddit) -> None:
    subreddit.posts[0].selftext = "x" * 50_000
    fields = serializers.post_fields(subreddit.posts[0])
    assert len(fields["body"]) <= 20_000
    assert all(isinstance(key, str) for key in fields["raw_data"])


# --- collection -------------------------------------------------------------


def test_collect_posts(client: FakeReddit, database: Database) -> None:
    report = collector_for(client, database).collect("linux", sections=["posts"])

    assert report.source == "reddit"
    assert report.target == "r/linux"
    assert report.counts["posts"] == 3
    assert report.duplicated == 0
    assert database.counts()["reddit_posts"] == 3


def test_collect_resolves_subreddit_prefixes(client: FakeReddit, database: Database) -> None:
    for name in ("linux", "r/linux", "/r/Linux/"):
        report = collector_for(client, database).collect(name, sections=["subreddit"])
        assert report.target == "r/linux"
    assert database.counts()["reddit_subreddits"] == 1


def test_unknown_subreddit_is_not_found(client: FakeReddit, database: Database) -> None:
    with pytest.raises(NotFoundError):
        collector_for(client, database).collect("ghosttown")


def test_invalid_target_is_not_found(client: FakeReddit, database: Database) -> None:
    with pytest.raises(NotFoundError):
        collector_for(client, database).collect("r/linux/extra")


def test_collect_subreddit_section(client: FakeReddit, database: Database) -> None:
    report = collector_for(client, database).collect("linux", sections=["subreddit"])

    assert report.counts["subreddit"] == 1
    with database.session() as session:
        stored = queries.get_reddit_subreddit(session, "linux")
        assert stored is not None
        assert stored.subscribers == 1000
        assert stored.raw_data["display_name"] == "linux"


def test_post_rows_are_complete(client: FakeReddit, database: Database) -> None:
    collector_for(client, database).collect("linux", sections=["posts"])

    with database.session() as session:
        post = queries.get_reddit_post(session, "t3_post1")
        assert post is not None
        assert post.title == "Thread #1 about #security"
        assert post.score == 3
        assert post.num_comments == 1
        assert post.upvote_ratio == 0.75
        assert post.subreddit_id is not None
        assert post.author_id is not None
        assert post.raw_data["id"] == "post1"


def test_author_is_stored_in_the_reddit_user_table(client: FakeReddit, database: Database) -> None:
    collector_for(client, database).collect("linux", sections=["posts"])

    from xtg.database.models import RedditUser

    with database.session() as session:
        names = {user.name for user in session.query(RedditUser).all()}  # noqa: S608
    assert names == {"alice"}


def test_graph_edges_and_entities(client: FakeReddit, database: Database) -> None:
    collector_for(client, database).collect("linux", sections=["posts"])

    with database.session() as session:
        from xtg.database.models import Entity, Relation

        edges = {(row.source, row.relation_type, row.target) for row in session.query(Relation)}  # noqa: S608
        entities = {(row.type, row.value, row.source) for row in session.query(Entity)}  # noqa: S608

    assert ("reddit:subreddit:r/linux", "contains", "reddit:post:t3_post1") in edges
    assert ("reddit:user:alice", "authored", "reddit:post:t3_post1") in edges
    assert ("domain", "example1.org", "reddit") in entities
    assert ("hashtag", "security", "reddit") in entities


def test_repeat_collection_is_a_noop(client: FakeReddit, database: Database) -> None:
    """With the resume cursor the second run fetches nothing new."""
    collector = collector_for(client, database)
    collector.collect("linux", sections=["posts"])
    second = collector.collect("linux", sections=["posts"])

    assert second.counts.get("posts", 0) == 0
    assert second.errors == []
    assert database.counts()["reddit_posts"] == 3


def test_full_rewalk_counts_duplicates_instead_of_inserting(
    client: FakeReddit, database: Database
) -> None:
    """``--full`` re-reads the listing; existing posts must not be duplicated."""
    collector_for(client, database).collect("linux", sections=["posts"])

    report = collector_for(client, database, resume=False).collect("linux", sections=["posts"])

    assert report.counts["posts"] == 3
    assert report.duplicated == 3
    assert database.counts()["reddit_posts"] == 3


def test_recollection_updates_changed_values(client: FakeReddit, database: Database) -> None:
    collector_for(client, database).collect("linux", sections=["posts"])
    client.subreddits["linux"].posts[0].score = 999

    collector_for(client, database, resume=False).collect("linux", sections=["posts"])

    with database.session() as session:
        assert queries.get_reddit_post(session, "t3_post1").score == 999


def test_collection_resumes_after_the_newest_post(client: FakeReddit, database: Database) -> None:
    collector = collector_for(client, database)
    collector.collect("linux", sections=["posts"])

    # A brand new submission appears after the first run.
    from tests.fakes_reddit import FakeSubmission

    client.subreddits["linux"].posts.append(
        FakeSubmission(id="post9", title="Fresh #security", index=9)
    )

    report = collector_for(client, database).collect("linux", sections=["posts"])

    assert report.counts["posts"] == 1  # only the new one is fetched
    assert database.counts()["reddit_posts"] == 4


def test_limit_is_forwarded_to_the_listing(client: FakeReddit, database: Database) -> None:
    report = collector_for(client, database).collect("linux", sections=["posts"], limit=2)
    assert report.counts["posts"] == 2


def test_listing_selection_is_configurable(client: FakeReddit, database: Database) -> None:
    collector_for(client, database, listing="top").collect("linux", sections=["posts"])
    assert database.counts()["reddit_posts"] == 3


def test_unknown_listing_falls_back_to_new(client: FakeReddit, database: Database) -> None:
    assert collector_for(client, database, listing="nonsense").listing == "new"


def test_collect_comments(client: FakeReddit, database: Database) -> None:
    make_comments(client.subreddits["linux"])
    collector_for(client, database).collect("linux", sections=["posts"])

    report = collector_for(client, database).collect("linux", sections=["comments"], limit=10)

    # Two comments per post, the MoreComments placeholder is skipped.
    assert report.counts["comments"] == 6
    assert database.counts()["reddit_comments"] == 6


def test_comment_rows_and_graph_edges(client: FakeReddit, database: Database) -> None:
    make_comments(client.subreddits["linux"])
    collector_for(client, database).collect("linux", sections=["posts"])
    collector_for(client, database).collect("linux", sections=["comments"], limit=10)

    with database.session() as session:
        from xtg.database.models import Relation

        comment = queries.get_reddit_comment(session, "t1_cpost1")
        assert comment is not None
        assert comment.body == "Top level comment"
        assert comment.depth == 0
        assert comment.post_id is not None
        reply = queries.get_reddit_comment(session, "t1_cpost1reply")
        assert reply is not None
        assert reply.depth == 1
        edges = {(row.source, row.relation_type, row.target) for row in session.query(Relation)}  # noqa: S608

    assert ("reddit:post:t3_post1", "has_comment", "reddit:comment:t1_cpost1") in edges
    assert ("reddit:user:bob", "commented", "reddit:comment:t1_cpost1") in edges


def test_comments_are_deduplicated(client: FakeReddit, database: Database) -> None:
    make_comments(client.subreddits["linux"])
    collector = collector_for(client, database)
    collector.collect("linux", sections=["posts"])
    collector.collect("linux", sections=["comments"], limit=10)

    second = collector_for(client, database).collect("linux", sections=["comments"], limit=10)

    assert second.counts["comments"] == 6
    assert second.duplicated == 6
    assert database.counts()["reddit_comments"] == 6


def test_comments_without_posts_exits_not_found(client: FakeReddit, database: Database) -> None:
    with pytest.raises(NotFoundError):
        collector_for(client, database).collect("linux", sections=["comments"], limit=5)


def test_comment_budget_is_respected(client: FakeReddit, database: Database) -> None:
    make_comments(client.subreddits["linux"])
    collector_for(client, database).collect("linux", sections=["posts"])

    report = collector_for(client, database).collect("linux", sections=["comments"], limit=3)

    assert report.counts["comments"] == 3


def test_broken_comment_read_is_reported_not_fatal(client: FakeReddit, database: Database) -> None:
    collector_for(client, database).collect("linux", sections=["posts"])

    def boom(_identifier: str) -> Any:
        raise RuntimeError("502 from reddit")

    client.submission = boom  # type: ignore[method-assign]
    report = collector_for(client, database).collect("linux", sections=["comments"], limit=10)

    assert report.counts.get("comments") is None
    assert report.errors  # the failure is reported, the run continues


def test_all_sections(client: FakeReddit, database: Database) -> None:
    make_comments(client.subreddits["linux"])
    report = collector_for(client, database).collect(
        "linux", sections=["subreddit", "posts", "comments"], limit=10
    )

    assert report.counts["subreddit"] == 1
    assert report.counts["posts"] == 3
    assert report.counts["comments"] == 6
    assert report.errors == []


def test_media_is_not_downloaded(client: FakeReddit, database: Database) -> None:
    client.subreddits["linux"].posts[0].url = "https://i.redd.it/photo.jpg"
    collector_for(client, database).collect("linux", sections=["posts"])

    from xtg.database.models import Media

    with database.session() as session:
        assert session.query(Media).count() == 0  # noqa: S608


# --- source facade ----------------------------------------------------------


def test_source_collects(reddit_settings: Settings, database: Database, client: FakeReddit) -> None:
    source = RedditSource(reddit_settings, database=database, factory=lambda **_kwargs: client)
    with source:
        report = source.collect("linux", sections=["posts"], limit=2)
    assert report.counts["posts"] == 2


def test_source_requires_credentials(settings: Settings, database: Database) -> None:
    source = RedditSource(settings, database=database, factory=FakeReddit)
    with pytest.raises(ConfigError), source:
        source.collect("linux")


def test_source_parse_and_save(
    reddit_settings: Settings, database: Database, client: FakeReddit
) -> None:
    source = RedditSource(reddit_settings, database=database, factory=lambda **_kwargs: client)
    parsed = source.parse(client.subreddits["linux"].posts[0])
    assert parsed["kind"] == "post"

    with database.session() as session:
        item = source.save(session, parsed)
    assert item.ref == "reddit:post:t3_post1"
    assert item.created is True
