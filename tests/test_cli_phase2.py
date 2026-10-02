"""End-to-end CLI tests for the Phase 2 commands (auth, collect).

Everything runs against fake Telegram objects, so no network or credentials are
needed; the Telegram client is injected where ``create_client`` is called.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.fakes import (
    FakeClient,
    FakeParticipant,
    FakeUser,
    make_chat,
    make_messages,
    write_session_file,
)
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
    monkeypatch.setenv("XTG_API_ID", "12345")
    monkeypatch.setenv("XTG_API_HASH", "hash")
    monkeypatch.chdir(tmp_path)
    reload_settings()
    return home


@pytest.fixture
def chat() -> Any:
    return make_chat()


def fake_client(monkeypatch: pytest.MonkeyPatch, client: Any) -> FakeClient:
    """Inject a fake Telegram client wherever the CLI builds one."""
    monkeypatch.setattr("xtg.telegram.client.create_client", lambda _options: client)
    return client


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.stdout)


# --- collect ----------------------------------------------------------------


def test_collect_messages_json(cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any) -> None:
    fake_client(monkeypatch, FakeClient(entities={"example": chat}, messages=make_messages(6)))

    result = runner.invoke(cli_app, ["collect", "@example", "--messages", "--json"])

    assert result.exit_code == 0
    data = payload(result)
    assert data["chat"] == "example"
    assert data["messages"] == 6
    assert data["duplicated"] == 0
    assert data["media"] == 2
    assert data["errors"] == []


def test_collect_persists_between_invocations(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any
) -> None:
    """Collected data lands in the database that later commands read."""
    fake_client(monkeypatch, FakeClient(entities={"example": chat}, messages=make_messages(4)))

    assert runner.invoke(cli_app, ["collect", "@example", "--messages"]).exit_code == 0
    stats = runner.invoke(cli_app, ["db", "stats", "--json"])
    assert stats.exit_code == 0
    assert payload(stats)["tables"]["messages"] == 4


def test_collect_members(cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any) -> None:
    client = FakeClient(entities={"example": chat})
    client.participants = [
        FakeParticipant(FakeUser(id=index, username=f"user{index}")) for index in range(3)
    ]
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["collect", "https://t.me/example", "--members", "--json"])

    assert result.exit_code == 0
    assert payload(result)["members"] == 3


def test_collect_all_sections(cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any) -> None:
    client = FakeClient(entities={"example": chat}, messages=make_messages(3))
    client.participants = [FakeParticipant(FakeUser(id=5))]
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["collect", "@example", "--all", "--json"])

    data = payload(result)
    assert data["sections"] == ["messages", "members", "media"]
    assert data["messages"] == 3
    assert data["members"] == 1


def test_collect_respects_limit(cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any) -> None:
    fake_client(monkeypatch, FakeClient(entities={"example": chat}, messages=make_messages(10)))

    result = runner.invoke(cli_app, ["collect", "@example", "--messages", "-n", "2", "--json"])

    assert payload(result)["messages"] == 2


def test_collect_table_output(cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any) -> None:
    fake_client(monkeypatch, FakeClient(entities={"example": chat}, messages=make_messages(2)))

    result = runner.invoke(cli_app, ["collect", "@example", "--messages"])

    assert result.exit_code == 0
    assert "Collected example" in result.output
    assert "messages" in result.output


def test_collect_download_flag_warns(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any
) -> None:
    fake_client(monkeypatch, FakeClient(entities={"example": chat}, messages=make_messages(1)))

    result = runner.invoke(cli_app, ["collect", "@example", "--media", "--download"])

    assert result.exit_code == 0
    assert "not implemented" in result.output


def test_collect_unknown_target_exits_not_found(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any
) -> None:
    fake_client(monkeypatch, FakeClient(entities={"example": chat}))

    result = runner.invoke(cli_app, ["collect", "@ghost", "--messages"])

    assert result.exit_code == ExitCode.NOT_FOUND


def test_collect_without_credentials(cli_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XTG_API_ID")
    monkeypatch.delenv("XTG_API_HASH")
    reload_settings()

    result = runner.invoke(cli_app, ["collect", "@example", "--messages"])

    assert result.exit_code == ExitCode.CONFIG


def test_collect_reports_connection_failure(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, chat: Any
) -> None:
    class Offline(FakeClient):
        async def connect(self) -> FakeClient:
            raise ConnectionError("no route to host")

    fake_client(monkeypatch, Offline(entities={"example": chat}))

    result = runner.invoke(cli_app, ["collect", "@example", "--messages"])

    assert result.exit_code == ExitCode.NETWORK


# --- auth -------------------------------------------------------------------


def test_auth_login_with_prompted_values(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    client = FakeClient(authorized=False, me=FakeUser(id=42, username="durov"))
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="+1 555 0100\n54321\n")

    assert result.exit_code == 0
    assert client.signed_in_with == {"phone": "+15550100", "code": "54321"}
    assert "Authorized as @durov" in result.output


def test_auth_login_with_two_factor(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    client = FakeClient(authorized=False, me=FakeUser(id=7, username="secure"))
    client.needs_password = True
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="+79991234567\n11111\ncloud-pass\n")

    assert result.exit_code == 0
    assert client.signed_in_with["password"] == "cloud-pass"
    assert "2FA used" in result.output


def test_auth_login_options_skip_prompts(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    client = FakeClient(authorized=False, me=FakeUser(id=11, username="cli"))
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login", "--phone", "+15550100", "--code", "999"])

    assert result.exit_code == 0
    assert client.signed_in_with == {"phone": "+15550100", "code": "999"}


def test_auth_login_rejects_bad_code(cli_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(authorized=False)
    client.code_error = RuntimeError("PHONE_CODE_INVALID")
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="+15550100\n00000\n")

    assert result.exit_code == ExitCode.AUTH
    assert "PHONE_CODE_INVALID" in result.output


def test_auth_login_reports_missing_two_factor_password(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(authorized=False)
    client.needs_password = True
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="+15550100\n12345\n")

    assert result.exit_code == ExitCode.AUTH


def test_auth_login_aborts_cleanly_at_the_phone_prompt(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C / closed stdin at the first prompt must still exit with a code.

    Regression: only the 2FA prompt was guarded, so cancelling the phone or
    code prompt surfaced a Click abort with exit status 1 instead of the
    documented AUTH code.
    """
    client = FakeClient(authorized=False)
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="")

    assert result.exit_code == ExitCode.AUTH
    assert "No phone number provided" in result.output


def test_auth_login_aborts_cleanly_at_the_code_prompt(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(authorized=False)
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="+15550100\n")

    assert result.exit_code == ExitCode.AUTH
    assert "No login code provided" in result.output


def test_auth_login_uses_the_configured_proxy(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Authentication must honour the proxy list like any other connection."""
    proxy_list = tmp_path / "list.json"
    proxy_list.write_text(
        json.dumps({"proxys": {"PX": "socks5://127.0.0.1:1080"}}), encoding="utf-8"
    )
    monkeypatch.setenv("XTG_PROXY_ENABLED", "true")
    monkeypatch.setenv("XTG_PROXY_FILE", str(proxy_list))
    reload_settings()

    client = FakeClient(authorized=False, me=FakeUser(id=5, username="viaproxy"))
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login", "--phone", "+15550100", "--code", "1"])

    assert result.exit_code == 0
    assert "Authorized as @viaproxy" in result.output


def test_auth_login_over_a_direct_connection(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    monkeypatch.setenv("XTG_PROXY_ENABLED", "false")
    reload_settings()

    client = FakeClient(authorized=False, me=FakeUser(id=6, username="viadirect"))
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login", "--phone", "+15550100", "--code", "1"])

    assert result.exit_code == 0
    assert "Authorized as @viadirect" in result.output


def test_auth_login_short_circuits_an_authorized_session(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that is already authorized must not prompt for anything."""
    client = FakeClient(authorized=True, me=FakeUser(id=8, username="alreadyin"))
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="")

    assert result.exit_code == 0
    assert "Already authorized as @alreadyin" in result.output
    assert client.signed_in_with is None


def test_auth_login_hardens_session_files(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The fake client writes a session file, login must lock it down."""
    session = isolated_home / "data" / "sessions" / "xtg.session"

    class Writing(FakeClient):
        async def connect(self) -> FakeClient:
            await super().connect()
            session.parent.mkdir(parents=True, exist_ok=True)
            session.write_bytes(b"session")
            session.chmod(0o644)
            return self

    client = Writing(authorized=False, me=FakeUser(id=1, username="lock"))
    fake_client(monkeypatch, client)

    result = runner.invoke(cli_app, ["auth", "login"], input="+15550100\n12345\n")

    assert result.exit_code == 0
    assert stat.S_IMODE(session.stat().st_mode) == 0o600


def test_auth_status_reports_session(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    write_session_file(isolated_home / "data" / "sessions" / "xtg.session")

    result = runner.invoke(cli_app, ["auth", "status", "--json"])

    assert result.exit_code == 0
    data = payload(result)
    assert data["configured"] is True
    assert data["session_exists"] is True
    assert data["session_size_bytes"] > 0
    assert data["state"] == "authorized"
    assert data["authorized"] is True


def test_auth_status_reports_the_same_file_the_service_layer_resolves(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The command must not report a different session than the library returns.

    Regression: status looked for ``xtg`` while Telethon had written
    ``xtg.session``, so a successful login was reported as ``0 B`` and
    ``not_authorized``.
    """
    expected = write_session_file(isolated_home / "data" / "sessions" / "xtg.session")

    result = runner.invoke(cli_app, ["auth", "status", "--json"])
    assert result.exit_code == 0
    data = payload(result)

    assert data["session_file"] == str(expected)
    assert data["session_files"] == [str(expected)]
    assert data["session_size_bytes"] == expected.stat().st_size
    assert data["session_valid"] is True
    assert data["state"] == "authorized"


def test_auth_status_reports_an_unauthorized_session_distinctly(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """A session file with no auth key is valid, but nobody is logged in."""
    write_session_file(isolated_home / "data" / "sessions" / "xtg.session", authorized=False)

    result = runner.invoke(cli_app, ["auth", "status", "--json"])
    data = payload(result)

    assert data["session_valid"] is True
    assert data["authorized"] is False
    assert data["state"] == "not_authorized"

    table = runner.invoke(cli_app, ["auth", "status"])
    assert "holds no authorization yet" in table.output


def test_auth_status_from_a_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: run the console entry point and check what it reports.

    Uses an isolated ``XTG_HOME`` and a synthetic session, so no real
    credentials and no network are involved.
    """
    home = tmp_path / "home"
    session = write_session_file(home / "data" / "sessions" / "xtg.session")
    env = {
        **os.environ,
        "XTG_HOME": str(home),
        "XTG_API_ID": "12345",
        "XTG_API_HASH": "hash",
        "XTG_LOG_TO_FILE": "false",
        "XTG_PROXY_ENABLED": "false",
    }
    # A stray .env next to the CWD must not influence the result.
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    (workdir / ".env").write_text("XTG_SESSION_NAME=leaked\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "xtg.cli.main", "auth", "status", "--json"],
        capture_output=True,
        text=True,
        cwd=workdir,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    data = json.loads(completed.stdout)
    assert data["session_file"] == str(session)
    assert data["state"] == "authorized"
    assert data["authorized"] is True
    assert data["session_size_bytes"] == session.stat().st_size


def test_auth_status_rejects_a_file_that_is_not_a_session(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """Regression: a junk file used to be reported as an authorized account."""
    session = isolated_home / "data" / "sessions" / "xtg.session"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_bytes(b"not a session")

    result = runner.invoke(cli_app, ["auth", "status", "--json"])

    assert result.exit_code == 0
    data = payload(result)
    assert data["session_exists"] is True
    assert data["session_valid"] is False
    assert data["state"] == "invalid"
    assert data["authorized"] is False


def test_auth_status_reports_a_missing_session_as_missing(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """No file is a different fact from "Telegram is not authorized"."""
    result = runner.invoke(cli_app, ["auth", "status", "--json"])

    assert result.exit_code == 0
    data = payload(result)
    assert data["session_exists"] is False
    assert data["session_valid"] is False
    assert data["state"] == "missing"
    assert data["authorized"] is False

    table = runner.invoke(cli_app, ["auth", "status"])
    assert "run `xtg auth login`" in table.output


def test_auth_status_warns_about_a_broken_session(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    session = isolated_home / "data" / "sessions" / "xtg.session"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_bytes(b"not a session")

    result = runner.invoke(cli_app, ["auth", "status"])

    assert result.exit_code == 0
    assert "not a usable Telegram session" in result.output


def test_auth_status_table_with_session(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    write_session_file(isolated_home / "data" / "sessions" / "xtg.session")

    result = runner.invoke(cli_app, ["auth", "status"])

    assert result.exit_code == 0
    assert "Authentication" in result.output
    assert "authorized" in result.output


def test_auth_logout_removes_session(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    session = isolated_home / "data" / "sessions" / "xtg.session"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_bytes(b"session")

    result = runner.invoke(cli_app, ["auth", "logout", "--yes"])

    assert result.exit_code == 0
    assert not session.exists()


def test_auth_logout_cancelled(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    session = isolated_home / "data" / "sessions" / "xtg.session"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_bytes(b"session")

    result = runner.invoke(cli_app, ["auth", "logout"], input="n\n")

    assert result.exit_code == ExitCode.CANCELLED
    assert session.exists()


# --- status integration -----------------------------------------------------


def test_status_reflects_login(
    cli_app: Any, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    fake_client(monkeypatch, FakeClient(authorized=False, me=FakeUser(id=3, username="st")))

    assert runner.invoke(cli_app, ["auth", "login"], input="+15550100\n12345\n").exit_code == 0

    result = runner.invoke(cli_app, ["status", "--json"])
    assert result.exit_code == 0
    assert payload(result)["auth"]["configured"] is True


@pytest.fixture
def cli_app() -> Any:
    return app
