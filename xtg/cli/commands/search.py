"""``xtg search`` - search the collected dataset."""

from __future__ import annotations

from datetime import datetime

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode, NotFoundError
from xtg.core.utils import normalize_entity_ref
from xtg.database import queries


@output.handle_errors
def search(
    ctx: typer.Context,
    keyword: str | None = typer.Argument(None, help="Text or message id to look for."),
    chat: str | None = typer.Option(None, "--chat", "-c", help="Restrict to @username or id."),
    sender: str | None = typer.Option(None, "--sender", "-s", help="Restrict to @username or id."),
    since: datetime | None = typer.Option(None, "--since", help="ISO date lower bound."),
    until: datetime | None = typer.Option(None, "--until", help="ISO date upper bound."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="Maximum results."),
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Full-text search over the local message dataset.

    Example::

        xtg search "crypto wallet"
    """
    if not keyword:
        output.hint('Provide a keyword: xtg search "keyword"')
        raise typer.Exit(ExitCode.USAGE)

    app_ctx = get_app_context(ctx)
    database = app_ctx.db
    total = database.total_rows()
    if total == 0:
        output.warn("The dataset is empty - collect data first (`xtg collect`).")
        raise typer.Exit(ExitCode.NOT_FOUND)

    with database.session() as session:
        statement = queries.build_message_search(
            keyword,
            chat=queries.resolve_chat(session, chat) if chat else None,
            sender=_resolve_sender(session, sender) if sender else None,
            since=since,
            until=until,
            limit=limit,
        )
        results = queries.search_messages(session, statement)
        payload = [
            {
                "chat_id": row.chat_id,
                "message_id": row.telegram_message_id,
                "sender_id": row.sender_id,
                "date": row.date.isoformat() if row.date else None,
                "text": row.text,
            }
            for row in results
        ]

    if json_output:
        output.emit_json({"query": keyword, "count": len(payload), "results": payload})
        return

    output.heading(f"Search results for {keyword!r}", subtitle=f"{len(payload)} match(es)")
    if not payload:
        output.warn("Nothing found.")
        raise typer.Exit(ExitCode.NOT_FOUND)
    output.print_table(
        output.table(
            columns=("MSG ID", "SENDER", "DATE", "TEXT"),
            rows=[
                (
                    row["message_id"],
                    row["sender_id"] or "-",
                    row["date"] or "-",
                    (row["text"] or "")[:80].replace("\n", " "),
                )
                for row in payload
            ],
        )
    )


def _resolve_sender(session, reference: str):
    """Resolve ``@username`` or an id to a stored user."""
    kind, value = normalize_entity_ref(reference)
    if kind == "username":
        user = queries.get_user_by_username(session, value)
    elif kind == "id":
        user = queries.get_user_by_telegram_id(session, int(value))
    else:
        user = None
    if user is None:
        raise NotFoundError(
            f"No collected user matches {reference!r}",
            hint="Collect it first: xtg collect @channel --members",
        )
    return user
