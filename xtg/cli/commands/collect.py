"""``xtg collect`` - Telegram data collection (Phase 2).

The command surface is registered now so that ``--help`` documents the final
CLI, while the implementation is intentionally deferred: it needs the Telethon
client in :mod:`xtg.telegram.client`.
"""

from __future__ import annotations

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode
from xtg.core.utils import normalize_entity_ref


@output.handle_errors
def collect(
    ctx: typer.Context,
    target: str | None = typer.Argument(None, help="@username, chat id or t.me link."),
    messages: bool = typer.Option(False, "--messages", help="Collect the message history."),
    members: bool = typer.Option(False, "--members", help="Collect the member list."),
    media: bool = typer.Option(False, "--media", help="Collect media metadata."),
    everything: bool = typer.Option(False, "--all", help="Collect everything available."),
    limit: int = typer.Option(500, "--limit", "-n", min=1, help="Maximum items per section."),
    download: bool = typer.Option(False, "--download", help="Download media files to disk."),
) -> None:
    """Collect data from a Telegram chat into the local database.

    Examples::

        xtg collect @channel --messages
        xtg collect @channel --members
        xtg collect @channel --all
    """
    if not target:
        output.hint("Provide a target: xtg collect @channel --messages")
        raise typer.Exit(code=ExitCode.USAGE)

    kind, value = _validate(target)
    sections = _sections(messages, members, media, everything)
    if not sections:
        output.hint("Choose what to collect: --messages, --members, --media or --all.")
        raise typer.Exit(code=ExitCode.USAGE)

    get_app_context(ctx).settings.require_api_credentials()
    output.info(
        f"Target: {kind}:{value} | sections: {', '.join(sections)} | limit: {limit} "
        f"| download media: {'yes' if download else 'no'}"
    )
    output.not_implemented("Telegram collection", phase="Phase 2")


def _validate(target: str) -> tuple[str, str]:
    """Fail fast on an unparsable reference."""
    kind, value = normalize_entity_ref(target)
    if kind == "unknown" or not value:
        output.fail(
            f"{target!r} is not a valid Telegram reference "
            "(use @username, a numeric id or a t.me link)."
        )
        raise typer.Exit(code=ExitCode.USAGE)
    return kind, value


def _sections(messages: bool, members: bool, media: bool, everything: bool) -> list[str]:
    if everything:
        return ["messages", "members", "media"]
    selected = [
        name
        for name, flag in (("messages", messages), ("members", members), ("media", media))
        if flag
    ]
    return selected
