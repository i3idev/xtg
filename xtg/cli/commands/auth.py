"""``xtg auth`` - Telegram authentication management."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer

from xtg import __version__
from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.config import mask_secret
from xtg.core.errors import AuthError, ExitCode
from xtg.core.utils import format_size
from xtg.telegram import auth as telegram_auth

app = typer.Typer(
    name="auth",
    help="Manage Telegram authentication (login, logout, status).",
    no_args_is_help=True,
)


class CliPrompter:
    """Interactive prompts for the login flow.

    Implements :class:`xtg.telegram.auth.Prompter` so the flow itself stays free
    of any CLI dependency and can be driven by tests.
    """

    def phone(self) -> str:
        return typer.prompt("Phone number (international format)")

    def code(self) -> str:
        return typer.prompt("Login code")

    def password(self) -> str:
        return typer.prompt("Two-factor password", hide_input=True)


@app.command("status")
@output.handle_errors
def status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show whether the CLI is configured and authenticated."""
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    info = telegram_auth.describe_session(settings)
    files = telegram_auth.session_files(settings)
    payload = {
        "version": __version__,
        "configured": settings.has_api_credentials,
        "api_id": settings.api_id,
        "api_hash": mask_secret(settings.api_hash),
        "session_name": settings.session_name,
        "session_file": str(info.path),
        "session_exists": bool(files),
        "session_files": [str(path) for path in files],
        "session_size_bytes": info.size_bytes,
        "session_mode": info.mode,
        "session_locked": info.locked,
        "session_valid": info.valid,
        "state": telegram_auth.auth_state(settings),
        "authorized": info.authorized and settings.has_api_credentials,
    }
    if json_output:
        output.emit_json(payload)
        return

    output.kv_panel(
        {
            "version": __version__,
            "state": payload["state"],
            "api id": payload["api_id"] or "not set",
            "api hash": payload["api_hash"] or "not set",
            "session name": settings.session_name,
            "session file": payload["session_file"],
            "session size": format_size(info.size_bytes) if info.exists else "-",
            "permissions": info.mode,
            "authorized": "yes" if payload["authorized"] else "no",
        },
        title="Authentication",
    )
    if not payload["configured"]:
        output.hint(f"Set XTG_API_ID and XTG_API_HASH in {settings.env_file} (see .env.example).")
    elif not info.exists:
        output.hint("No session found yet - run `xtg auth login`.")
    elif not info.valid:
        output.warn(f"{info.path.name} is not a Telegram session file ({info.problem}).")
        output.hint("Run `xtg auth logout` then `xtg auth login` to rebuild it.")
    elif info.valid and not info.authorized:
        output.warn(f"{info.path.name} holds no authorization yet.")
        output.hint("Run `xtg auth login` to authorize this session.")
    elif info.locked:
        output.warn("The session is in use by another xtg process.")
    if files:
        output.heading("Session files")
        output.print_table(
            output.table(
                columns=("FILE", "SIZE", "MODIFIED"),
                rows=[_file_row(path) for path in files],
            )
        )


@app.command("login")
@output.handle_errors
def login(
    ctx: typer.Context,
    phone: str | None = typer.Option(None, "--phone", help="Skip the phone prompt."),
    code: str | None = typer.Option(None, "--code", help="Skip the code prompt."),
    password: str | None = typer.Option(
        None, "--password", help="2FA password (prefer the prompt; it is not stored)."
    ),
) -> None:
    """Log into Telegram and store the session in the session directory.

    Asks for the phone number, the login code and - only when 2FA is enabled -
    the cloud password. The password is used once and never written to disk.
    """
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    settings.require_api_credentials()

    prompter = _ScriptedPrompter(phone=phone, code=code, password=password)
    output.info(f"Connecting Telegram (session: {settings.session_file.name})")
    connection = app_ctx.connection(require_authorized=False)

    async def _run() -> telegram_auth.LoginResult:
        try:
            client = await connection.open()
            return await telegram_auth.login(client, prompter)
        finally:
            await connection.close()

    result = asyncio.run(_run())
    hardened = telegram_auth.harden_session_files(settings)

    if result.already_authorized:
        output.success(f"Already authorized as {result.display_name()}.")
    else:
        output.success(f"Authorized as {result.display_name()}.")
    output.kv_panel(
        {
            "session": str(settings.session_file),
            "user id": result.user_id or "-",
            "username": f"@{result.username}" if result.username else "-",
            "2FA used": "yes" if result.used_password else "no",
            "files secured": ", ".join(path.name for path in hardened) or "-",
        },
        title="Session stored",
    )
    output.hint("The session grants full access to the account: keep data/sessions private.")


@app.command("logout")
@output.handle_errors
def logout(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Delete the local Telegram session and disconnect."""
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    files = telegram_auth.session_files(settings)
    if not files:
        output.info(f"No session to remove ({settings.session_file.name}).")
        raise typer.Exit

    names = ", ".join(path.name for path in files)
    if not output.confirm(f"Delete the session of {settings.session_name} ({names})?", yes=yes):
        raise typer.Exit(ExitCode.CANCELLED)
    for path in files:
        path.unlink(missing_ok=True)
        output.info(f"Removed {path.name}")
    output.success(f"Session {settings.session_name} removed.")


class _ScriptedPrompter:
    """Prompter that uses command line options when provided, else asks.

    Every prompt is guarded the same way: Ctrl-C, EOF or a closed stdin means
    the operator cancelled, and that must surface as a clean documented exit
    code rather than a Click abort with exit status 1.
    """

    def __init__(self, *, phone: str | None, code: str | None, password: str | None) -> None:
        self._phone = phone
        self._code = code
        self._password = password
        self.cli = CliPrompter()

    def _ask(self, value: str | None, prompt: str, *, what: str) -> str:
        if value is not None:
            return value
        try:
            return getattr(self.cli, prompt)()
        except (typer.Abort, EOFError, KeyboardInterrupt) as exc:
            raise AuthError(
                f"No {what} provided",
                hint="Re-run `xtg auth login` and answer the prompts, "
                "or pass --phone/--code/--password.",
            ) from exc

    def phone(self) -> str:
        return self._ask(self._phone, "phone", what="phone number")

    def code(self) -> str:
        return self._ask(self._code, "code", what="login code")

    def password(self) -> str:
        return self._ask(self._password, "password", what="2FA password")


def _file_row(path: Path) -> tuple[str, str, str]:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    return (path.name, format_size(path.stat().st_size), modified)
