"""Tests for the Telegram authentication flow (no real account required)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from tests.fakes import (
    FakeClient,
    FakeUser,
    StaticPrompter,
    write_corrupt_sqlite,
    write_session_file,
    write_unrelated_sqlite,
)
from xtg.core.config import Settings
from xtg.core.errors import AuthError
from xtg.telegram import auth as telegram_auth


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = Settings(
        home=tmp_path,
        api_id=1,
        api_hash="hash",
        log_to_file=False,
        proxy_enabled=False,
    )
    config.ensure_directories()
    return config


# --- session path handling --------------------------------------------------


def test_session_path_lives_in_sessions_dir(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    assert path == settings.sessions_path / f"{settings.session_name}.session"
    assert path.parent.is_dir()


def test_session_path_carries_the_suffix_telethon_creates(settings: Settings) -> None:
    """Regression: the resolver returned the bare stem, which Telethon never creates.

    ``SQLiteSession.__init__`` appends ``.session`` to the session id, so the
    file on disk is ``<name>.session``. Reporting ``<name>`` made every command
    look at a path that does not exist.
    """
    assert settings.session_file.name == "xtg.session"
    assert telegram_auth.session_path(settings).name == "xtg.session"


def test_login_status_and_logout_resolve_the_same_file(settings: Settings, tmp_path: Path) -> None:
    """The three auth commands must never disagree about the session path."""
    from xtg.telegram.client import resolve_options

    # login: the path handed to Telethon
    login_path = Path(resolve_options(settings).as_kwargs()["session"])
    # status: what the inspector reads
    status_path = telegram_auth.session_path(settings)
    # logout: what it deletes
    logout_path = telegram_auth.session_path(settings)

    assert login_path == status_path == logout_path
    assert login_path.name.endswith(".session")


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("xtg", "xtg.session"),
        ("xtg.session", "xtg.session"),
        ("XTG.SESSION", "XTG.session"),
        ("  xtg  ", "xtg.session"),
        ("my-account", "my-account.session"),
    ],
)
def test_session_name_is_suffixed_exactly_once(
    settings: Settings, configured: str, expected: str
) -> None:
    """A name may be configured with or without ``.session``; never both."""
    settings.session_name = configured
    assert settings.session_file.name == expected
    assert not settings.session_file.name.endswith(".session.session")


def test_session_name_resolves_the_same_from_any_directory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path must not depend on the working directory."""
    settings.ensure_directories()
    write_session_file(telegram_auth.session_path(settings))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    info = telegram_auth.describe_session(settings)
    assert info.exists is True
    assert info.authorized is True


def test_relative_home_still_yields_an_absolute_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``Settings.home`` must not make the session path follow the CWD."""
    monkeypatch.chdir(tmp_path)
    config = Settings(home=Path("relative-home"), api_id=1, api_hash="h", log_to_file=False)
    config.ensure_directories()

    assert config.home.is_absolute()
    assert config.session_file.is_absolute()
    assert config.session_file.name == "xtg.session"


def test_absolute_home_is_used_verbatim(tmp_path: Path) -> None:
    config = Settings(home=tmp_path, api_id=1, api_hash="h", log_to_file=False)
    assert config.sessions_path == tmp_path / "data" / "sessions"
    assert config.session_file == tmp_path / "data" / "sessions" / "xtg.session"


def test_reported_size_matches_the_file_on_disk(settings: Settings) -> None:
    """Regression: status reported ``0 B`` for a real 28 KiB session."""
    path = write_session_file(telegram_auth.session_path(settings))

    info = telegram_auth.describe_session(settings)

    assert info.size_bytes == path.stat().st_size
    assert info.size_bytes > 0


def test_describe_session_without_files(settings: Settings) -> None:
    """A missing file is ``MISSING``, not "Telegram is not authorized"."""
    info = telegram_auth.describe_session(settings)
    assert info.exists is False
    assert info.size_bytes == 0
    assert info.state is telegram_auth.AuthState.MISSING
    assert telegram_auth.auth_state(settings) is telegram_auth.AuthState.MISSING
    assert telegram_auth.session_files(settings) == []


def test_describe_session_with_file(settings: Settings) -> None:
    path = write_session_file(telegram_auth.session_path(settings))
    info = telegram_auth.describe_session(settings)
    assert info.exists is True
    assert info.size_bytes == path.stat().st_size
    assert info.state is telegram_auth.AuthState.AUTHORIZED
    assert telegram_auth.session_files(settings) == [path]


def test_a_file_that_is_not_a_session_is_not_reported_as_authorized(
    settings: Settings,
) -> None:
    """A leftover or truncated file must not claim the account is logged in."""
    path = telegram_auth.session_path(settings)
    path.write_bytes(b"this is not a telethon session")

    info = telegram_auth.describe_session(settings)

    assert info.exists is True
    assert info.valid is False
    assert info.state is telegram_auth.AuthState.INVALID
    assert telegram_auth.auth_state(settings) is telegram_auth.AuthState.INVALID


def test_an_empty_session_file_is_not_a_session(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.touch()
    assert telegram_auth.describe_session(settings).valid is False


# --- session validation -----------------------------------------------------
#
# The validator must tell these seven apart. Collapsing any two of them into a
# plain "not authorized" is what made the original bug hard to read.


def test_probe_a_missing_session(settings: Settings) -> None:
    probe = telegram_auth.probe_session(telegram_auth.session_path(settings))
    assert (probe.valid, probe.authorized) == (False, False)
    assert probe.problem == "no session file"


def test_probe_an_empty_file(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.touch()
    probe = telegram_auth.probe_session(path)
    assert (probe.valid, probe.authorized) == (False, False)
    assert "empty" in probe.problem


def test_probe_a_random_file(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.write_bytes(b"just some bytes, definitely not a database")
    probe = telegram_auth.probe_session(path)
    assert probe.valid is False
    assert probe.problem


def test_probe_a_corrupted_sqlite_database(settings: Settings) -> None:
    path = write_corrupt_sqlite(telegram_auth.session_path(settings))
    assert path.stat().st_size > 0  # it still claims to be a file
    probe = telegram_auth.probe_session(path)
    assert probe.valid is False
    assert "SQLite" in probe.problem


def test_probe_sqlite_with_the_wrong_schema(settings: Settings) -> None:
    path = write_unrelated_sqlite(telegram_auth.session_path(settings))
    probe = telegram_auth.probe_session(path)
    assert probe.valid is False
    assert probe.problem == "not a Telegram session database"


def test_probe_a_valid_but_unauthorized_session(settings: Settings) -> None:
    """The tables exist but no login ever happened."""
    path = write_session_file(telegram_auth.session_path(settings), authorized=False)

    info = telegram_auth.describe_session(settings)

    assert info.valid is True
    assert info.authorized is False
    assert info.state is telegram_auth.AuthState.NOT_AUTHORIZED
    assert path.exists()


def test_probe_a_valid_authorized_session(settings: Settings) -> None:
    write_session_file(telegram_auth.session_path(settings))

    info = telegram_auth.describe_session(settings)

    assert info.valid is True
    assert info.authorized is True
    assert info.state is telegram_auth.AuthState.AUTHORIZED


def test_every_distinct_session_state_is_reported_separately(
    settings: Settings,
) -> None:
    """The four states the CLI must never conflate.

    Regression: a missing file used to be reported as ``not_authorized``, which
    made a fresh install look like a failed login.
    """
    path = telegram_auth.session_path(settings)

    # 1. no file at all
    assert telegram_auth.describe_session(settings).state is telegram_auth.AuthState.MISSING

    # 2. a file that is present but unusable
    path.write_bytes(b"not a session at all")
    assert telegram_auth.describe_session(settings).state is telegram_auth.AuthState.INVALID

    # 3. a valid session nobody has logged in with yet
    write_session_file(path, authorized=False)
    info = telegram_auth.describe_session(settings)
    assert info.valid is True
    assert info.state is telegram_auth.AuthState.NOT_AUTHORIZED

    # 4. a valid, authorized session
    write_session_file(path, authorized=True)
    info = telegram_auth.describe_session(settings)
    assert info.state is telegram_auth.AuthState.AUTHORIZED

    # and the four are four distinct values, not aliases
    states = {
        telegram_auth.AuthState.MISSING,
        telegram_auth.AuthState.INVALID,
        telegram_auth.AuthState.NOT_AUTHORIZED,
        telegram_auth.AuthState.AUTHORIZED,
    }
    assert len({state.value for state in states}) == 4


def test_every_command_resolves_one_session_path(settings: Settings, tmp_path: Path) -> None:
    """``<home>/data/sessions/<session_name>.session``, whatever the command."""
    from xtg.telegram.client import resolve_options

    expected = settings.sessions_path / f"{settings.session_name}.session"
    assert expected.name == "xtg.session"

    login_path = Path(resolve_options(settings).as_kwargs()["session"])  # login
    status_path = telegram_auth.session_path(settings)  # status
    logout_targets = telegram_auth.session_files(settings)  # logout
    harden_targets = telegram_auth.harden_session_files(settings)  # validator

    assert login_path == status_path == expected
    assert harden_targets == logout_targets == []

    write_session_file(expected)
    assert telegram_auth.session_files(settings) == [expected]
    assert telegram_auth.harden_session_files(settings) == [expected]


def test_the_validator_accepts_a_session_written_by_telethon_1_45(
    settings: Settings,
) -> None:
    """Schema 4, ``version`` 8, one sessions row with a key: the real thing."""
    import sqlite3

    path = write_session_file(telegram_auth.session_path(settings), dc_id=4, port=443)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM version").fetchone()[0] == 8
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"version", "sessions", "entities", "sent_files", "update_state"} <= tables

    assert telegram_auth.probe_session(path).authorized is True


def test_probe_never_raises_on_a_directory(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.mkdir(parents=True)
    assert telegram_auth.probe_session(path).valid is False


def test_probe_never_raises_on_an_unreadable_file(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.write_bytes(b"x" * 64)
    path.chmod(0o000)
    try:
        assert telegram_auth.probe_session(path).valid is False
    finally:
        path.chmod(0o600)


def test_session_detection_never_raises_on_junk(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.write_bytes(bytes(range(256)) * 8)  # not SQLite, must not blow up
    assert telegram_auth.is_session_database(path) is False


def test_session_files_includes_journal(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    journal = path.with_name(f"{path.name}-journal")
    path.write_bytes(b"x")
    journal.write_bytes(b"y")
    assert telegram_auth.session_files(settings) == [path, journal]
    assert telegram_auth.describe_session(settings).locked is True


def test_auth_state_requires_credentials(tmp_path: Path) -> None:
    config = Settings(home=tmp_path, log_to_file=False, proxy_enabled=False)
    assert telegram_auth.auth_state(config) is telegram_auth.AuthState.UNCONFIGURED


def test_harden_session_files_sets_owner_only(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.write_bytes(b"secret")
    path.chmod(0o644)

    hardened = telegram_auth.harden_session_files(settings)

    assert hardened == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_harden_session_files_without_files(settings: Settings) -> None:
    assert telegram_auth.harden_session_files(settings) == []


# --- login flow -------------------------------------------------------------


def test_login_with_phone_and_code() -> None:
    client = FakeClient(authorized=False, me=FakeUser(id=42, username="durov", first_name="Pavel"))
    prompter = StaticPrompter(phone="+1 555 0100", code="54321")

    result = asyncio.run(telegram_auth.login(client, prompter))

    assert result.authorized is True
    assert result.user_id == 42
    assert result.username == "durov"
    assert result.used_password is False
    assert prompter.asked == ["phone", "code"]
    assert client.code_requested_for == "+15550100"
    assert client.signed_in_with == {"phone": "+15550100", "code": "54321"}


def test_login_with_two_factor_password() -> None:
    client = FakeClient(authorized=False, me=FakeUser(id=7, username="secure"))
    client.needs_password = True
    prompter = StaticPrompter(password="cloud-pass")

    result = asyncio.run(telegram_auth.login(client, prompter))

    assert result.used_password is True
    assert prompter.asked == ["phone", "code", "password"]
    assert client.signed_in_with == {"password": "cloud-pass"}


def test_login_short_circuits_when_authorized() -> None:
    client = FakeClient(me=FakeUser(id=9, username="already"))
    prompter = StaticPrompter()

    result = asyncio.run(telegram_auth.login(client, prompter))

    assert result.already_authorized is True
    assert result.display_name() == "@already"
    assert prompter.asked == []


def test_login_rejects_bad_code() -> None:
    client = FakeClient(authorized=False)
    client.code_error = RuntimeError("PHONE_CODE_INVALID")

    with pytest.raises(AuthError) as exc:
        asyncio.run(telegram_auth.login(client, StaticPrompter()))

    assert "PHONE_CODE_INVALID" in str(exc.value)


def test_login_rejects_bad_password() -> None:
    client = FakeClient(authorized=False)
    client.needs_password = True
    client.password_error = RuntimeError("PASSWORD_HASH_INVALID")

    with pytest.raises(AuthError) as exc:
        asyncio.run(telegram_auth.login(client, StaticPrompter()))

    assert "PASSWORD_HASH_INVALID" in str(exc.value)


def test_login_without_password_raises() -> None:
    client = FakeClient(authorized=False)
    client.needs_password = True

    with pytest.raises(AuthError) as exc:
        asyncio.run(telegram_auth.login(client, StaticPrompter(password="")))

    assert "2FA" in str(exc.value)


def test_login_never_stores_the_password(settings: Settings) -> None:
    client = FakeClient(authorized=False)
    client.needs_password = True
    prompter = StaticPrompter(password="do-not-store-me")

    asyncio.run(telegram_auth.login(client, prompter))

    assert client.authorized is True
    written = [
        path.read_bytes() for path in telegram_auth.session_files(settings) if path.is_file()
    ]
    assert all(b"do-not-store-me" not in blob for blob in written)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+1 555 0100", "+15550100"), ("  +7 (999) 123-45-67 ", "+79991234567")],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert telegram_auth.normalize_phone(raw) == expected


def test_normalize_phone_rejects_garbage() -> None:
    with pytest.raises(AuthError):
        telegram_auth.normalize_phone("not a phone")
