"""Tests for the Telegram authentication flow (no real account required)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from tests.fakes import FakeClient, FakeUser, StaticPrompter, write_session_file
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
    assert path == settings.sessions_path / settings.session_name
    assert path.parent.is_dir()


def test_describe_session_without_files(settings: Settings) -> None:
    info = telegram_auth.describe_session(settings)
    assert info.exists is False
    assert info.size_bytes == 0
    assert info.state is telegram_auth.AuthState.NOT_AUTHORIZED
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
    assert info.state is telegram_auth.AuthState.NOT_AUTHORIZED
    assert telegram_auth.auth_state(settings) is telegram_auth.AuthState.NOT_AUTHORIZED


def test_an_empty_session_file_is_not_a_session(settings: Settings) -> None:
    path = telegram_auth.session_path(settings)
    path.touch()
    assert telegram_auth.describe_session(settings).valid is False


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
