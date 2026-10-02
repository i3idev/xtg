"""``xtg reddit`` - Reddit collection.

Thin CLI layer: everything real lives in :mod:`xtg.sources.reddit`. The command
validates its arguments, builds the source, renders the report and maps errors
to exit codes like every other command.
"""

from __future__ import annotations

from typing import Any

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.config import mask_secret
from xtg.core.errors import ExitCode
from xtg.core.utils import pluralize
from xtg.database.db import REDDIT_TABLES
from xtg.sources.base import SourceReport
from xtg.sources.reddit import RedditSource, describe_client
from xtg.sources.reddit.client import praw_available
from xtg.sources.reddit.collector import LISTINGS

app = typer.Typer(
    name="reddit",
    help="Collect Reddit subreddits, posts and comments.",
    no_args_is_help=True,
)

#: Sections rendered in the summary table, in this order.
_SUMMARY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("posts", "Posts"),
    ("comments", "Comments"),
    ("users", "Users"),
    ("subreddit", "Subreddit"),
)


@app.command("status")
@output.handle_errors
def status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show whether the Reddit connector is configured and what it can do."""
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    payload: dict[str, Any] = {
        "configured": settings.has_reddit_credentials,
        "client_id": mask_secret(settings.reddit_client_id) if settings.reddit_client_id else None,
        "client_secret_set": bool(settings.reddit_client_secret),
        "user_agent": settings.reddit_user_agent,
        "account": str(settings.reddit_username) if settings.has_reddit_account else None,
        "listing": settings.reddit_listing,
        "database": str(settings.database_file),
        "database_exists": settings.database_file.exists(),
    }
    counts = app_ctx.db.counts()
    payload["rows"] = {name: counts[name] for name in REDDIT_TABLES}
    payload["total_reddit_rows"] = sum(payload["rows"].values())
    payload["praw_installed"] = praw_available()

    if json_output:
        output.emit_json(payload)
        return

    output.kv_panel(
        {
            "configured": "yes" if payload["configured"] else "no",
            "client id": payload["client_id"] or "not set",
            "client secret": "set" if payload["client_secret_set"] else "not set",
            "user agent": payload["user_agent"] or "not set",
            "account": payload["account"] or "anonymous (read-only)",
            "listing": payload["listing"],
            "praw": "installed" if payload["praw_installed"] else "not installed",
            "database": payload["database"],
            "reddit rows": payload["total_reddit_rows"],
        },
        title="Reddit",
    )
    if not payload["configured"]:
        output.hint(
            "Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT "
            f"in {settings.env_file} (see .env.example)."
        )
    elif not payload["praw_installed"]:
        output.hint('Install the client with `uv add "praw>=7.7"` followed by `uv sync`.')


@app.command("collect")
@output.handle_errors
def collect(
    ctx: typer.Context,
    subreddit: str = typer.Argument(..., help="Subreddit name, e.g. linux or r/linux."),
    posts: bool = typer.Option(False, "--posts", help="Collect the submissions."),
    comments: bool = typer.Option(False, "--comments", help="Collect the comments."),
    everything: bool = typer.Option(False, "--all", help="Subreddit, posts and comments."),
    limit: int = typer.Option(100, "--limit", "-n", min=1, help="Maximum items per section."),
    listing: str = typer.Option(
        None, "--listing", "-s", help="Listing to walk: new, hot, top, controversial, rising."
    ),
    full: bool = typer.Option(
        False, "--full", help="Ignore the resume cursor and re-walk the listing."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine readable summary."),
) -> None:
    """Collect a subreddit into the local database.

    Examples::

        xtg reddit collect linux
        xtg reddit collect cybersecurity --posts
        xtg reddit collect linux --all
    """
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    settings.require_reddit_credentials()
    sections = _sections(posts, comments, everything)
    if listing and listing not in LISTINGS:
        output.fail(f"Unknown listing {listing!r} (use one of: {', '.join(LISTINGS)})")
        raise typer.Exit(code=ExitCode.USAGE)

    output.progress_note(f"Collecting r/{subreddit} ({', '.join(sections)})")
    source = RedditSource(settings, database=app_ctx.db, listing=listing, resume=not full)
    with source:
        report = source.collect(subreddit, sections=sections, limit=limit)
        report.details.update(describe_client(source.client))
    _render(report, target=subreddit, sections=sections, json_output=json_output)


def _sections(posts: bool, comments: bool, everything: bool) -> tuple[str, ...]:
    if everything:
        return ("subreddit", "posts", "comments")
    selected = tuple(name for name, flag in (("posts", posts), ("comments", comments)) if flag)
    return selected or ("posts",)


def _render(
    report: SourceReport, *, target: str, sections: tuple[str, ...], json_output: bool
) -> None:
    """Print the summary block."""
    if json_output:
        output.emit_json(
            {
                "source": report.source,
                "target": report.target,
                "requested": sections,
                "counts": report.counts,
                "duplicated": report.duplicated,
                "total": report.total,
                "errors": report.errors,
                "details": report.details,
            }
        )
        return

    output.heading(f"Collected {report.target or target}", subtitle=", ".join(sections))
    rows = [
        (label, str(report.counts.get(name, 0)))
        for name, label in _SUMMARY_SECTIONS
        if name in report.counts or name in sections
    ]
    output.print_table(
        output.table(
            columns=("SECTION", "SAVED"),
            rows=rows or [("items", str(report.total))],
        )
    )
    if report.errors:
        output.warn(f"{len(report.errors)} item(s) could not be stored:")
        for error in report.errors[:5]:
            output.hint(f"  - {error}")
    output.success(
        f"Completed: {pluralize(report.total - report.duplicated, 'new item')} "
        f"({report.duplicated} already known)."
    )
