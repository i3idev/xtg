"""``xtg analyze`` - statistics and insights over collected data."""

from __future__ import annotations

from typing import Any

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode

app = typer.Typer(
    name="analyze",
    help="Aggregate statistics, entity extraction and knowledge graph building.",
    no_args_is_help=True,
)


@app.command("stats")
@output.handle_errors
def stats(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Aggregate statistics of the collected dataset."""
    database = get_app_context(ctx).db
    counts = database.counts()
    if sum(counts.values()) == 0:
        output.warn("The dataset is empty - nothing to analyze.")
        raise typer.Exit(code=ExitCode.NOT_FOUND)

    payload: dict[str, Any] = {
        "tables": counts,
        "total_rows": sum(counts.values()),
        "database_size": database.size_bytes(),
    }
    if json_output:
        output.emit_json(payload)
        return

    output.heading("Dataset overview")
    output.print_table(
        output.table(
            columns=("TABLE", "ROWS"),
            rows=[(name, str(value)) for name, value in counts.items()],
        )
    )


@app.command("entities")
@output.handle_errors
def entities(
    ctx: typer.Context,
    entity_type: str = typer.Argument("email", help="email, domain, phone, hashtag, url, ip."),
    limit: int = typer.Option(50, "--limit", "-n", min=1),
) -> None:
    """List extracted intelligence pivots of one type."""
    from xtg.database import queries

    app_ctx = get_app_context(ctx)
    with app_ctx.db.session() as session:
        rows = queries.search_entities(session, entity_type, "%")
    if not rows:
        output.warn(f"No {entity_type} entities extracted yet.")
        raise typer.Exit(code=ExitCode.NOT_FOUND)
    output.print_table(
        output.table(
            columns=("VALUE", "OCCURRENCES"),
            rows=[(row.value, row.occurrences) for row in rows[:limit]],
        )
    )


@app.command("graph")
@output.handle_errors
def graph(
    ctx: typer.Context,  # noqa: ARG001 - injected by Typer, used from Phase 3
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="Maximum edges to display."),
) -> None:
    """Show knowledge graph edges (relations between nodes)."""
    output.info(f"Planned output: up to {limit} relation(s) from the local dataset.")
    output.not_implemented("Knowledge graph extraction", phase="Phase 3")


@app.command("sentiment")
@output.handle_errors
def sentiment(
    ctx: typer.Context,  # noqa: ARG001 - injected by Typer, used from Phase 3
    target: str = typer.Argument(..., help="@username or chat id."),
) -> None:
    """Language and tone breakdown of a conversation."""
    output.info(f"Planned analysis target: {target}")
    output.not_implemented("Language / tone analysis", phase="Phase 3")
