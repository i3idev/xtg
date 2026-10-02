"""``xtg info`` - information about stored entities."""

from __future__ import annotations

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode, NotFoundError
from xtg.core.utils import format_datetime, normalize_entity_ref, truncate

app = typer.Typer(
    name="info",
    help="Inspect stored users, chats, messages and entities.",
    no_args_is_help=True,
)


@app.command("chat")
@output.handle_errors
def chat(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="@username, chat id or t.me link."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show everything known about a stored chat."""
    app_ctx = get_app_context(ctx)
    with app_ctx.db.session() as session:
        from xtg.database import queries

        entity = queries.resolve_chat(session, target)
        payload = {
            "telegram_id": entity.telegram_id,
            "title": entity.title,
            "username": entity.username,
            "type": entity.type,
            "members_count": entity.members_count,
            "messages": queries.count_chat_messages(session, entity),
            "created_at": format_datetime(entity.created_at),
        }
    if json_output:
        output.emit_json(payload)
        return
    output.kv_panel(payload, title=f"Chat {target}")


@app.command("user")
@output.handle_errors
def user(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="@username or numeric user id."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show everything known about a stored user."""
    from xtg.database import queries

    app_ctx = get_app_context(ctx)
    with app_ctx.db.session() as session:
        kind, value = normalize_entity_ref(target)
        entity = (
            queries.get_user_by_username(session, value)
            if kind == "username"
            else queries.get_user_by_telegram_id(session, int(value))
            if kind == "id"
            else None
        )
        if entity is None:
            raise NotFoundError(
                f"No collected user matches {target!r}",
                hint="Collect it first: xtg collect @channel --members",
            )
        payload = {
            "telegram_id": entity.telegram_id,
            "username": entity.username,
            "name": " ".join(filter(None, (entity.first_name, entity.last_name))) or "-",
            "bio": truncate(entity.bio, 200) or "-",
            "phone": entity.phone or "-",
            "is_bot": entity.is_bot,
            "created_at": format_datetime(entity.created_at),
        }
    if json_output:
        output.emit_json(payload)
        return
    output.kv_panel(payload, title=f"User {target}")


@app.command("stats")
@output.handle_errors
def stats(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Summarise the collected dataset."""
    database = get_app_context(ctx).db
    counts = database.counts()
    if json_output:
        output.emit_json({"tables": counts, "total_rows": sum(counts.values())})
        return
    output.print_table(
        output.table(
            columns=("TABLE", "ROWS"),
            rows=[(name, str(value)) for name, value in counts.items()],
        )
    )
    if sum(counts.values()) == 0:
        output.warn("The dataset is empty.")
        raise typer.Exit(code=ExitCode.NOT_FOUND)
