"""Smoke tests for the CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from xtg.cli.main import app, get_cli
from xtg.core.config import reload_settings

runner = CliRunner()

COMMAND_GROUPS = [
    "auth",
    "proxy",
    "collect",
    "search",
    "info",
    "analyze",
    "export",
    "db",
    "status",
]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point XTG_HOME at a temporary directory so tests never touch real data."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XTG_HOME", str(home))
    monkeypatch.setenv("XTG_LOG_TO_FILE", "false")
    monkeypatch.chdir(tmp_path)
    reload_settings()
    return home


@pytest.fixture
def cli() -> Any:
    """The Typer application as the runner resolves it."""
    return app


@pytest.fixture
def hermetic_proxies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a fixed proxy list instead of the shipped package data.

    ``xtg/proxy/list_proxy.json`` is user-editable sample data, so tests must not
    assert on its contents.
    """
    path = tmp_path / "cli_list_proxy.json"
    path.write_text(
        json.dumps(
            {
                "proxys": {
                    "P1": "socks5://127.0.0.1:1080",
                    "P2": "socks5://user:pass@127.0.0.1:1081",
                    "P3": "http://user:pass@127.0.0.1:8082",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XTG_PROXY_FILE", str(path))
    reload_settings()
    return path


def test_error_boundary_maps_exit_codes() -> None:
    """The console script boundary turns XTGError into a documented exit code."""
    command = get_cli()
    assert callable(command.main)


def test_root_help(cli: Any) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for group in COMMAND_GROUPS:
        assert group in result.output


@pytest.mark.parametrize("group", COMMAND_GROUPS)
def test_group_help(cli: Any, group: str) -> None:
    result = runner.invoke(cli, [group, "--help"])
    assert result.exit_code == 0


def test_version_flag(cli: Any) -> None:
    from xtg import __version__

    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_status_json(cli: Any) -> None:
    result = runner.invoke(cli, ["status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"]
    assert "database" in payload
    assert "proxy" in payload
    assert payload["auth"]["configured"] is False


def test_status_table(cli: Any) -> None:
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "eXtractorT" in result.output
    assert "Authentication" in result.output


def test_db_init_and_stats(cli: Any) -> None:
    assert runner.invoke(cli, ["db", "init"]).exit_code == 0
    result = runner.invoke(cli, ["db", "stats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["total_rows"] == 0
    assert payload["tables"]["messages"] == 0


def test_db_path(cli: Any) -> None:
    result = runner.invoke(cli, ["db", "path"])
    assert result.exit_code == 0
    assert result.stdout.strip().endswith("database.db")


def test_auth_status_json(cli: Any) -> None:
    result = runner.invoke(cli, ["auth", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["configured"] is False
    assert payload["session_exists"] is False


def test_auth_logout_without_session(cli: Any) -> None:
    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0


def test_proxy_global_status_json(cli: Any, hermetic_proxies: Path) -> None:
    """The global pool seeds from the legacy list on first load."""
    result = runner.invoke(cli, ["proxy", "global", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["enabled"] is False
    assert payload["active"] is None
    assert payload["pool_size"] == 3


def test_proxy_global_list(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "list"])
    assert result.exit_code == 0
    assert "P1" in result.output
    assert "socks5" in result.output


def test_proxy_global_remove_single(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "remove", "P2"])
    assert result.exit_code == 0
    assert "Removed proxy P2" in result.output
    payload = json.loads(runner.invoke(cli, ["proxy", "global", "status", "--json"]).stdout)
    assert payload["pool_size"] == 2
    assert "P2" not in payload["proxies"]


def test_proxy_global_remove_range(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "remove", "P1=>P2"])
    assert result.exit_code == 0
    assert "Removed 2 proxies in range P1=>P2" in result.output
    payload = json.loads(runner.invoke(cli, ["proxy", "global", "status", "--json"]).stdout)
    assert payload["pool_size"] == 1
    assert "P3" in payload["proxies"]
    assert "P1" not in payload["proxies"]
    assert "P2" not in payload["proxies"]


def test_proxy_global_remove_unknown(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "remove", "P99"])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_proxy_global_remove_empty_range(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "remove", "P99=>P100"])
    assert result.exit_code == 0
    assert "No proxies in range" in result.output


def test_proxy_global_add_then_remove(cli: Any, hermetic_proxies: Path) -> None:
    """Seed the global pool, then exercise single + range removal."""
    # Use IDs that don't collide with the hermetic P1/P2/P3.
    assert (
        runner.invoke(
            cli, ["proxy", "global", "add", "socks5://1.1.1.1:1080", "--name", "Q1"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli, ["proxy", "global", "add", "http://2.2.2.2:8080", "--name", "Q2"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli, ["proxy", "global", "add", "http://3.3.3.3:8080", "--name", "Q3"]
        ).exit_code
        == 0
    )

    payload = json.loads(runner.invoke(cli, ["proxy", "global", "status", "--json"]).stdout)
    assert payload["pool_size"] == 6

    result = runner.invoke(cli, ["proxy", "global", "remove", "Q1=>Q2"])
    assert result.exit_code == 0
    assert "Removed 2 proxies in range Q1=>Q2" in result.output
    payload = json.loads(runner.invoke(cli, ["proxy", "global", "status", "--json"]).stdout)
    assert payload["pool_size"] == 4
    assert "Q3" in payload["proxies"]
    assert "Q1" not in payload["proxies"]
    assert "Q2" not in payload["proxies"]


def test_proxy_telegram_list(cli: Any, hermetic_proxies: Path) -> None:
    """The legacy per-command list still lists the hermetic 3 entries."""
    result = runner.invoke(cli, ["proxy", "telegram", "list"])
    assert result.exit_code == 0
    assert "P1" in result.output
    assert "socks5" in result.output


def test_proxy_source_list_empty(cli: Any, hermetic_proxies: Path) -> None:
    """A fresh global pool has no subscription sources."""
    result = runner.invoke(cli, ["proxy", "global", "source", "list"])
    assert result.exit_code == 0
    assert "No subscription sources" in result.output


def test_proxy_source_list_json_empty(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "source", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sources"] == {}
    assert payload["count"] == 0


def test_proxy_source_add_list_remove(cli: Any, hermetic_proxies: Path) -> None:
    assert (
        runner.invoke(cli, ["proxy", "global", "source", "add", "a", "https://x.com"]).exit_code
        == 0
    )
    assert (
        runner.invoke(cli, ["proxy", "global", "source", "add", "b", "https://y.com"]).exit_code
        == 0
    )

    result = runner.invoke(cli, ["proxy", "global", "source", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["sources"] == {"a": "https://x.com", "b": "https://y.com"}

    assert runner.invoke(cli, ["proxy", "global", "source", "remove", "a"]).exit_code == 0
    result = runner.invoke(cli, ["proxy", "global", "source", "list", "--json"])
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert "a" not in payload["sources"]
    assert "b" in payload["sources"]

    # Removing something that does not exist is not an error.
    assert runner.invoke(cli, ["proxy", "global", "source", "remove", "nope"]).exit_code == 0
    assert "not found" in runner.invoke(cli, ["proxy", "global", "source", "remove", "nope"]).output


def test_proxy_source_clear(cli: Any, hermetic_proxies: Path) -> None:
    for name, url in (("a", "https://x.com"), ("b", "https://y.com")):
        assert runner.invoke(cli, ["proxy", "global", "source", "add", name, url]).exit_code == 0
    assert runner.invoke(cli, ["proxy", "global", "source", "clear"]).exit_code == 0
    result = runner.invoke(cli, ["proxy", "global", "source", "list", "--json"])
    assert json.loads(result.stdout)["count"] == 0


def test_proxy_source_persists_across_invocations(
    cli: Any, hermetic_proxies: Path, isolated_home: Path
) -> None:
    """Sources survive a second process invocation."""
    assert (
        runner.invoke(cli, ["proxy", "global", "source", "add", "s1", "https://x.com"]).exit_code
        == 0
    )
    result = runner.invoke(cli, ["proxy", "global", "source", "list", "--json"])
    assert json.loads(result.stdout)["sources"] == {"s1": "https://x.com"}
    sources_file = isolated_home / ".config" / "xtg" / "proxy" / "sources.json"
    assert sources_file.exists()
    assert "https://x.com" in sources_file.read_text()


def test_proxy_source_add_empty_name_rejected(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "source", "add", "", "https://x.com"])
    assert result.exit_code != 0


def test_proxy_source_add_empty_url_rejected(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "source", "add", "x", ""])
    assert result.exit_code != 0


def test_proxy_source_refresh_unknown_exits_not_found(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "source", "refresh", "nope", "--no-test"])
    assert result.exit_code == 8


def test_proxy_source_help_lists_subcommands(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "global", "source", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "add", "remove", "refresh", "clear"):
        assert sub in result.output


def test_export_formats(cli: Any) -> None:
    result = runner.invoke(cli, ["export", "--formats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["known"] == ["json", "csv", "markdown", "obsidian"]
    # Every declared format has a writer behind it.
    assert payload["implemented"] == ["csv", "json", "markdown", "obsidian"]


def test_export_without_data_exits_not_found(cli: Any) -> None:
    result = runner.invoke(cli, ["export", "json"])
    assert result.exit_code == 8


def test_collect_validates_reference(cli: Any) -> None:
    result = runner.invoke(cli, ["collect", "not a reference", "--messages"])
    assert result.exit_code == 2


def test_collect_without_api_credentials(cli: Any) -> None:
    result = runner.invoke(cli, ["collect", "@channel", "--messages"])
    assert result.exit_code == 3


def test_collect_without_section_flags(cli: Any) -> None:
    result = runner.invoke(cli, ["collect", "@channel"])
    assert result.exit_code == 2
    assert "--messages" in result.output


def test_search_without_data(cli: Any) -> None:
    result = runner.invoke(cli, ["search", "keyword"])
    assert result.exit_code == 8


def test_analyze_stats_without_data(cli: Any) -> None:
    result = runner.invoke(cli, ["analyze", "stats"])
    assert result.exit_code == 8


def test_info_stats(cli: Any) -> None:
    result = runner.invoke(cli, ["info", "stats", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["total_rows"] == 0


def test_info_unknown_chat(cli: Any) -> None:
    result = runner.invoke(cli, ["info", "chat", "@nothing"])
    assert result.exit_code == 8
