"""``xtg auth`` - Telegram authentication management."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from xtg import __version__
from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.config import mask_secret
from xtg.core.errors import ExitCode
from xtg.core.utils import format_size

app = typer.Typer(
    name="auth",
    help="Manage Telegram authentication (login, logout, status).",
    no_args_is_help=True,
)


def _session_files(session_path: Path) -> list[Path]:
    """All files Telethon may have created for the configured session."""
    if not session_path.exists():
        return []
    directory = session_path.parent
    stem = session_path.name
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and (path.name == stem or path.name.startswith(f"{stem}."))
    )


@app.command("status")
@output.handle_errors
def status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show whether the CLI is configured and authenticated."""
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    files = _session_files(settings.session_file)
    payload = {
        "version": __version__,
        "configured": settings.has_api_credentials,
        "api_id": settings.api_id,
        "api_hash": mask_secret(settings.api_hash),
        "session_name": settings.session_name,
        "session_file": str(settings.session_file),
        "session_exists": bool(files),
        "session_files": [str(path) for path in files],
        "authorized": bool(files) and settings.has_api_credentials,
    }
    if json_output:
        output.emit_json(payload)
        return

    rows = [
        ("version", __version__),
        ("api id", payload["api_id"] or "not set"),
        ("api hash", payload["api_hash"] or "not set"),
        ("session name", settings.session_name),
        ("session file", str(settings.session_file)),
        ("authorized", "yes" if payload["authorized"] else "no"),
    ]
    output.kv_panel(dict(rows), title="Authentication")
    if not payload["configured"]:
        output.hint(f"Set XTG_API_ID and XTG_API_HASH in {settings.env_file} (see .env.example).")
    elif not files:
        output.hint("No session found yet - run `xtg auth login`.")
    if files:
        output.heading("Session files")
        output.print_table(
            output.table(
                columns=("FILE", "SIZE", "MODIFIED"),
                rows=[
                    (
                        path.name,
                        format_size(path.stat().st_size),
                        datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(
                            timespec="seconds"
                        ),
                    )
                    for path in files
                ],
            )
        )


@app.command("login")
@output.handle_errors
def login(ctx: typer.Context) -> None:
    """Interactive Telegram login (phone number, code, 2FA)."""
    get_app_context(ctx).settings.require_api_credentials()
    output.not_implemented("Telegram login (MTProto client)", phase="Phase 2")


@app.command("logout")
@output.handle_errors
def logout(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Delete the local Telegram session and disconnect."""
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    files = _session_files(settings.session_file)
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
