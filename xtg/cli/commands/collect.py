"""``xtg collect`` - Telegram data collection.

Runs the Phase 2 collector through the Telegram source
(:mod:`xtg.sources.telegram`), which presents the async Telethon collector as
the generic :class:`~xtg.sources.base.BaseSource` contract: it resolves the
entity, stores the chat, walks the message history and/or the participant list,
and writes everything through the idempotent upsert helpers. Media is recorded
as metadata only.
"""

from __future__ import annotations

import time
from typing import Any

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.errors import ExitCode
from xtg.core.utils import normalize_entity_ref, pluralize
from xtg.sources.base import SourceReport
from xtg.sources.telegram import TelegramSource


@output.handle_errors
def collect(
    ctx: typer.Context,
    target: str | None = typer.Argument(None, help="@username, chat id or t.me link."),
    messages: bool = typer.Option(False, "--messages", help="Collect the message history."),
    members: bool = typer.Option(False, "--members", help="Collect the member list."),
    media: bool = typer.Option(False, "--media", help="Collect media metadata."),
    everything: bool = typer.Option(False, "--all", help="Collect everything available."),
    limit: int = typer.Option(500, "--limit", "-n", min=1, help="Maximum items per section."),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Continue after the stored cursor instead of re-walking the history.",
    ),
    download: bool = typer.Option(False, "--download", help="Reserved: Phase 3 downloads media."),
    json_output: bool = typer.Option(False, "--json", help="Machine readable summary."),
) -> None:
    """Collect data from a Telegram chat into the local database.

    Examples::

        xtg collect @channel --messages
        xtg collect @channel --members
        xtg collect @channel --all
        xtg collect @channel --messages --resume
    """
    if not target:
        output.hint("Provide a target: xtg collect @channel --messages")
        raise typer.Exit(code=ExitCode.USAGE)

    kind, value = _validate(target)
    sections = _sections(messages, members, media, everything)
    if not sections:
        output.hint("Choose what to collect: --messages, --members, --media or --all.")
        raise typer.Exit(code=ExitCode.USAGE)

    if download:
        output.warn("Media downloads are not implemented yet; metadata is stored instead.")

    app_ctx = get_app_context(ctx)
    app_ctx.settings.require_api_credentials()
    output.progress_note(f"Resolving {target}")

    report = _run_collection(
        app_ctx, target, sections=sections, limit=limit, resume=resume, quiet=json_output
    )
    _render(report, target=target, sections=sections, json_output=json_output)


def _run_collection(
    app_ctx: Any,
    target: str,
    *,
    sections: list[str],
    limit: int,
    resume: bool,
    quiet: bool,
) -> SourceReport:
    """Connect, collect and disconnect through the Telegram source."""
    output.progress_note("Connecting Telegram")
    settings = app_ctx.settings
    # The flag wins over the config default so one invocation can be resumed
    # without editing the environment.
    settings.resume = resume or settings.resume
    with TelegramSource(settings, database=app_ctx.db) as source:
        if not quiet:
            source.collector.delegate.progress = _progress_printer()
        return source.collect(target, sections=sections, limit=limit)


def _progress_printer():
    """Live progress on one rewritten line, rate limited to 5 updates/second."""
    state = {"last": 0.0}

    def report(section: str, processed: int, limit: int) -> None:
        now = time.monotonic()
        if processed < limit and now - state["last"] < 0.2:
            return
        state["last"] = now
        target = str(limit) if limit else "?"
        typer.echo(f"  {section}: {processed}/{target}", err=True)

    return report


def _render(report: SourceReport, *, target: str, sections: list[str], json_output: bool) -> None:
    """Print the summary block."""
    counts = report.counts
    details = report.details
    if json_output:
        output.emit_json(
            {
                "target": target,
                "sections": sections,
                "chat": report.target,
                "messages": counts.get("messages", 0),
                "duplicated": report.duplicated,
                "members": counts.get("members", 0),
                "media": counts.get("media", 0),
                "errors": report.errors,
                "performance": details,
            }
        )
        return

    output.heading(f"Collected {report.target or target}", subtitle=", ".join(sections))
    output.print_table(
        output.table(
            columns=("SECTION", "SAVED", "SKIPPED"),
            rows=[
                ("messages", str(counts.get("messages", 0)), str(report.duplicated)),
                ("members", str(counts.get("members", 0)), "-"),
                ("media", str(counts.get("media", 0)), "-"),
            ],
        )
    )
    performance = _performance_rows(details)
    if performance:
        output.kv_panel(dict(performance), title="Run")
    if report.errors:
        output.warn(f"{len(report.errors)} item(s) could not be stored:")
        for error in report.errors[:5]:
            output.hint(f"  - {error}")
    output.success(
        f"Completed: {pluralize(counts.get('messages', 0), 'message')} and "
        f"{pluralize(counts.get('members', 0), 'member')} in {report.target or target}."
    )


def _performance_rows(details: dict[str, Any]) -> list[tuple[str, str]]:
    """Only the metrics that are worth showing, so the panel never lies."""
    rows: list[tuple[str, str]] = []
    if details.get("batches"):
        rows.append(("batches", str(details["batches"])))
    if details.get("items_per_second"):
        rows.append(("throughput", f"{details['items_per_second']:,.0f} items/s"))
    if details.get("retries"):
        rows.append(("retries", str(details["retries"])))
    if details.get("backpressure_waits"):
        rows.append(("backpressure", f"{details['backpressure_waits']:,} waits"))
    if details.get("cursor"):
        rows.append(("cursor", str(details["cursor"])))
    return rows


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
    return [
        name
        for name, flag in (("messages", messages), ("members", members), ("media", media))
        if flag
    ]
