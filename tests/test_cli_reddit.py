"""End-to-end CLI tests for ``xtg reddit`` (no account, no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes_reddit import FakeReddit, make_comments, make_subreddit
from typer.testing import CliRunner
from xtg.cli.main import app
from xtg.core.config import reload_settings
from xtg.core.errors import ExitCode

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XTG_HOME", str(home))
    monkeypatch.setenv("XTG_LOG_TO_FILE", "false")
    monkeypatch.chdir(tmp_path)
    reload_settings()
    return home


@pytest.fixture
def subreddit():
    return make_subreddit("linux", posts=3)


@pytest.fixture
def reddit_client(monkeypatch: pytest.MonkeyPatch, subreddit):
    """Make every ``praw.Reddit`` built by the CLI return the fake client."""
    client = FakeReddit({"linux": subreddit}, user_agent="xtg/0.1.0 by u/tester")
    monkeypatch.setattr("xtg.sources.reddit.source.create_reddit", lambda *_args, **_kwargs: client)
    return client


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDDIT_CLIENT_ID", "client-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", "xtg/0.1.0 by u/tester")
    reload_settings()


@pytest.fixture
def cli_app() -> Any:
    return app


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


# --- status -----------------------------------------------------------------


def test_reddit_status_without_credentials(cli_app: Any) -> None:
    result = runner.invoke(cli_app, ["reddit", "status", "--json"])

    assert result.exit_code == 0
    data = payload(result)
    assert data["configured"] is False
    assert data["client_id"] is None
    assert data["rows"] == {
        "reddit_users": 0,
        "reddit_subreddits": 0,
        "reddit_posts": 0,
        "reddit_comments": 0,
    }


def test_reddit_status_when_configured(cli_app: Any, configured: None) -> None:
    result = runner.invoke(cli_app, ["reddit", "status", "--json"])

    data = payload(result)
    assert data["configured"] is True
    assert data["client_secret_set"] is True
    assert data["user_agent"] == "xtg/0.1.0 by u/tester"
    assert data["account"] is None


def test_reddit_status_masks_the_client_id(cli_app: Any, configured: None) -> None:
    result = runner.invoke(cli_app, ["reddit", "status", "--json"])
    data = payload(result)

    assert data["client_id"] != "client-id"
    assert "client-secret" not in result.stdout


def test_reddit_status_table(cli_app: Any, configured: None) -> None:
    result = runner.invoke(cli_app, ["reddit", "status"])

    assert result.exit_code == 0
    assert "Reddit" in result.output
    assert "configured" in result.output


def test_reddit_status_reports_praw_presence(cli_app: Any, configured: None) -> None:
    data = payload(runner.invoke(cli_app, ["reddit", "status", "--json"]))
    assert isinstance(data["praw_installed"], bool)


# --- collect ----------------------------------------------------------------


def test_collect_defaults_to_posts(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "linux", "--json"])

    assert result.exit_code == 0
    data = payload(result)
    assert data["source"] == "reddit"
    assert data["target"] == "r/linux"
    assert data["requested"] == ["posts"]
    assert data["counts"]["posts"] == 3
    assert data["errors"] == []


def test_collect_persists_between_invocations(
    cli_app: Any, configured: None, reddit_client
) -> None:
    assert runner.invoke(cli_app, ["reddit", "collect", "linux"]).exit_code == 0

    stats = runner.invoke(cli_app, ["db", "stats", "--json"])
    assert payload(stats)["tables"]["reddit_posts"] == 3
    assert payload(stats)["tables"]["reddit_subreddits"] == 1


def test_collect_posts_flag(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "cybersecurity", "--posts", "--json"])
    assert result.exit_code == ExitCode.NOT_FOUND  # the fake only knows r/linux


def test_collect_all_sections(cli_app: Any, configured: None, reddit_client, subreddit) -> None:
    make_comments(subreddit)

    result = runner.invoke(cli_app, ["reddit", "collect", "linux", "--all", "--json"])

    data = payload(result)
    assert data["requested"] == ["subreddit", "posts", "comments"]
    assert data["counts"]["posts"] == 3
    assert data["counts"]["comments"] == 6
    assert data["counts"]["subreddit"] == 1


def test_collect_limit(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "linux", "-n", "1", "--json"])
    assert payload(result)["counts"]["posts"] == 1


def test_collect_full_rewalk(cli_app: Any, configured: None, reddit_client) -> None:
    assert runner.invoke(cli_app, ["reddit", "collect", "linux"]).exit_code == 0

    second = runner.invoke(cli_app, ["reddit", "collect", "linux", "--full", "--json"])

    data = payload(second)
    assert data["counts"]["posts"] == 3
    assert data["duplicated"] == 3
    assert payload(runner.invoke(cli_app, ["db", "stats", "--json"]))["tables"]["reddit_posts"] == 3


def test_collect_table_output(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "linux"])

    assert result.exit_code == 0
    assert "Collected r/linux" in result.output
    assert "Posts" in result.output


def test_collect_resumes_on_a_second_run(cli_app: Any, configured: None, reddit_client) -> None:
    assert runner.invoke(cli_app, ["reddit", "collect", "linux"]).exit_code == 0

    second = payload(runner.invoke(cli_app, ["reddit", "collect", "linux", "--json"]))

    assert second["counts"].get("posts", 0) == 0


def test_collect_listing_option(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "linux", "-s", "top", "--json"])
    assert payload(result)["counts"]["posts"] == 3


def test_collect_rejects_unknown_listing(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "linux", "-s", "nonsense"])
    assert result.exit_code == ExitCode.USAGE


def test_collect_without_credentials(cli_app: Any, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "linux", "--json"])
    assert result.exit_code == ExitCode.CONFIG


def test_collect_unknown_subreddit(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect", "ghosttown", "--json"])
    assert result.exit_code == ExitCode.NOT_FOUND


def test_collect_requires_a_target(cli_app: Any, configured: None, reddit_client) -> None:
    result = runner.invoke(cli_app, ["reddit", "collect"])
    assert result.exit_code == ExitCode.USAGE


def test_reddit_help(cli_app: Any) -> None:
    result = runner.invoke(cli_app, ["reddit", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "collect" in result.output


def test_root_help_lists_reddit(cli_app: Any) -> None:
    assert "reddit" in runner.invoke(cli_app, ["--help"]).output


def test_status_command_mentions_reddit(cli_app: Any) -> None:
    data = payload(runner.invoke(cli_app, ["status", "--json"]))
    assert "reddit_configured" in data["settings"]
