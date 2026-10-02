"""``xtg export`` - export the collected dataset."""

from __future__ import annotations

from pathlib import Path

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode, NotFoundError
from xtg.export import ExportFormat, available_formats, get_writer


@output.handle_errors
def export(
    ctx: typer.Context,
    export_format: ExportFormat | None = typer.Argument(
        None, help="json, csv, markdown or obsidian."
    ),
    target: str | None = typer.Option(None, "--chat", "-c", help="Restrict to one chat."),
    output_path: Path | None = typer.Option(
        None, "--output", "-o", help="Destination file (default: data/exports/<stamp>.<ext>)."
    ),
    list_formats: bool = typer.Option(
        False, "--formats", help="List the known export formats and exit."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Export collected data.

    Examples::

        xtg export json
        xtg export csv --chat @channel -o messages.csv
        xtg export obsidian -o ./vault
        xtg export --formats
    """
    if list_formats:
        _list_formats(json_output=json_output)
        return
    if export_format is None:
        output.hint("Pick a format: xtg export json | csv | markdown | obsidian")
        raise typer.Exit(code=ExitCode.USAGE)

    app_ctx = get_app_context(ctx)
    database = app_ctx.db
    if database.total_rows() == 0:
        raise NotFoundError(
            "There is nothing to export",
            hint="Collect data first: xtg collect @channel --all",
        )
    if target:
        with database.session() as session:
            from xtg.database import queries

            queries.resolve_chat(session, target)

    writer = get_writer(export_format)
    destination = output_path or app_ctx.settings.exports_path / f"xtg-export.{export_format}"
    output.info(f"Planned destination: {destination}")
    output.not_implemented(
        f"The {export_format} export writer ({writer.__name__})", phase="Phase 3"
    )


def _list_formats(*, json_output: bool) -> None:
    payload = {
        "known": [str(item) for item in ExportFormat],
        "implemented": available_formats(),
    }
    if json_output:
        output.emit_json(payload)
        return
    output.print_table(
        output.table(
            columns=("FORMAT", "IMPLEMENTED"),
            rows=[
                (item, "yes" if item in payload["implemented"] else "no")
                for item in payload["known"]
            ],
        )
    )
