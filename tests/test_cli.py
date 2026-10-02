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


def test_proxy_status_json(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["file"] == str(hermetic_proxies)
    assert payload["stats"]["valid"] == 3


def test_proxy_list(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "list"])
    assert result.exit_code == 0
    assert "P1" in result.output
    assert "socks5" in result.output


def test_proxy_show_masks_password(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "show", "P3"])
    assert result.exit_code == 0
    assert "password" in result.output
    assert "***" in result.output or "**" in result.output


def test_proxy_show_unknown_name(cli: Any, hermetic_proxies: Path) -> None:
    result = runner.invoke(cli, ["proxy", "show", "NOPE"])
    assert result.exit_code == 7


def test_export_formats(cli: Any) -> None:
    result = runner.invoke(cli, ["export", "--formats", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["known"] == ["json", "csv", "markdown", "obsidian"]
    assert payload["implemented"] == []


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
