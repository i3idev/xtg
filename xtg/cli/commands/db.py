"""``xtg db`` - local database management."""

from __future__ import annotations

import shutil
from typing import Any

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode
from xtg.core.utils import format_size, pluralize

app = typer.Typer(
    name="db",
    help="Initialise, inspect and maintain the local SQLite database.",
    no_args_is_help=True,
)


@app.command("init")
@output.handle_errors
def init_cmd(ctx: typer.Context) -> None:
    """Create the database file and all tables (idempotent)."""
    database = get_app_context(ctx).db
    output.success(f"Database ready at {database.path}")
    output.print_table(
        output.table(columns=("TABLE",), rows=[(name,) for name in database.table_names()])
    )


@app.command("stats")
@output.handle_errors
def stats(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show row counts per table and the file size."""
    database = get_app_context(ctx).db
    counts = database.counts()
    payload: dict[str, Any] = {
        "file": str(database.path),
        "exists": database.exists,
        "size_bytes": database.size_bytes(),
        "size_human": format_size(database.size_bytes()),
        "tables": counts,
        "total_rows": sum(counts.values()),
    }
    if json_output:
        output.emit_json(payload)
        return

    output.kv_panel(
        {
            "file": str(database.path),
            "exists": "yes" if payload["exists"] else "no",
            "size": payload["size_human"],
            "total rows": payload["total_rows"],
        },
        title="Database",
    )
    output.print_table(
        output.table(
            columns=("TABLE", "ROWS"),
            rows=[(name, str(count)) for name, count in counts.items()],
        )
    )


@app.command("path")
@output.handle_errors
def path_cmd(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Print the database file path."""
    database = get_app_context(ctx).db
    if json_output:
        output.emit_json({"path": str(database.path), "exists": database.exists})
    else:
        output.console.print(str(database.path))


@app.command("vacuum")
@output.handle_errors
def vacuum(ctx: typer.Context) -> None:
    """Reclaim unused disk space."""
    database = get_app_context(ctx).db
    before = database.size_bytes()
    database.vacuum()
    output.success(f"Vacuumed: {format_size(before)} -> {format_size(database.size_bytes())}")


@app.command("reset")
@output.handle_errors
def reset(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    keep_backup: bool = typer.Option(True, "--backup/--no-backup", help="Copy the file first."),
) -> None:
    """Drop every table and recreate an empty schema (destructive)."""
    database = get_app_context(ctx).db
    counts = database.counts()
    total = sum(counts.values())
    if total:
        output.warn(
            f"This will permanently delete {pluralize(total, 'row')} "
            f"({', '.join(f'{name}: {value}' for name, value in counts.items() if value)})."
        )
    else:
        output.info("The database is already empty.")
    if not output.confirm("Reset the database?", default=False, yes=yes):
        raise typer.Exit(code=ExitCode.CANCELLED)

    backup: str | None = None
    if keep_backup and database.exists:
        backup_path = database.path.with_suffix(".db.bak")
        shutil.copy2(database.path, backup_path)
        backup = str(backup_path)
        output.info(f"Backup written to {backup}")
    database.reset()
    output.success("Database schema recreated (empty).")
