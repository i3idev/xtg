"""Tests for configuration, errors and utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from xtg.core.config import Settings, find_project_root, mask_secret
from xtg.core.errors import ConfigError, ExitCode, FeatureNotAvailableError, XTGError
from xtg.core.utils import (
    chunked,
    format_datetime,
    format_duration,
    format_size,
    normalize_entity_ref,
    pluralize,
    truncate,
)


def test_project_root_contains_pyproject() -> None:
    assert (find_project_root() / "pyproject.toml").is_file()


def test_home_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XTG_HOME", str(tmp_path))
    assert find_project_root() == tmp_path.resolve()


def test_derived_paths_are_absolute(settings: Settings) -> None:
    assert settings.database_file.is_absolute()
    assert settings.sessions_path.is_absolute()
    assert settings.exports_path.is_absolute()
    assert settings.database_url.startswith("sqlite+pysqlite:///")


def test_absolute_paths_win(settings: Settings, tmp_path: Path) -> None:
    settings.database_path = tmp_path / "custom.db"
    assert settings.database_file == tmp_path / "custom.db"


def test_ensure_directories_is_idempotent(settings: Settings) -> None:
    settings.ensure_directories()
    settings.ensure_directories()
    assert settings.sessions_path.is_dir()
    assert settings.logs_path.is_dir()


def test_require_api_credentials_raises(settings: Settings) -> None:
    with pytest.raises(ConfigError) as exc:
        settings.require_api_credentials()
    assert "XTG_API_ID" in str(exc.value)
    assert exc.value.exit_code == ExitCode.CONFIG


def test_api_hash_is_masked_in_redacted(settings: Settings) -> None:
    settings.api_id = 12345
    settings.api_hash = "0123456789abcdef"
    redacted = settings.redacted()
    assert redacted["api_hash"] == "0123********cdef"
    assert settings.has_api_credentials is True


def test_env_file_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("XTG_SESSION_NAME=custom\n", encoding="utf-8")
    assert Settings().session_name == "custom"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ab", "**"),
        ("abcdef", "******"),
        ("0123456789", "0123**6789"),
    ],
)
def test_mask_secret(value: str, expected: str) -> None:
    assert mask_secret(value) == expected


def test_error_hierarchy_exit_codes() -> None:
    error = XTGError("boom")
    assert error.exit_code == ExitCode.ERROR
    assert FeatureNotAvailableError("Thing").exit_code == ExitCode.NOT_IMPLEMENTED
    assert FeatureNotAvailableError("Thing").hint


@pytest.mark.parametrize(
    ("size", "expected"),
    [(512, "512 B"), (2048, "2.0 KiB"), (5 * 1024 * 1024, "5.0 MiB")],
)
def test_format_size(size: int, expected: str) -> None:
    assert format_size(size) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (125, "2m 05s"), (3725, "1h 02m 05s")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert format_duration(seconds) == expected


def test_format_datetime_handles_none() -> None:
    assert format_datetime(None) == "-"
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert format_datetime(moment) == "2026-01-02 03:04:05"


@pytest.mark.parametrize(
    ("value", "kind", "result"),
    [
        ("@durov", "username", "durov"),
        ("durov", "username", "durov"),
        ("https://t.me/durov", "username", "durov"),
        ("t.me/durov", "username", "durov"),
        ("-1001234567890", "id", "-1001234567890"),
        ("12345", "id", "12345"),
        ("https://t.me/joinchat/AAAA", "invite", "AAAA"),
        ("not a reference", "unknown", "not a reference"),
    ],
)
def test_normalize_entity_ref(value: str, kind: str, result: str) -> None:
    assert normalize_entity_ref(value) == (kind, result)


def test_pluralize_and_truncate() -> None:
    assert pluralize(1, "proxy") == "1 proxy"
    assert pluralize(2, "proxy") == "2 proxies"
    assert truncate("a\n  b   c", 10) == "a b c"
    assert truncate(None) == ""


def test_chunked() -> None:
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError):
        list(chunked([1], 0))
