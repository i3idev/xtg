"""``xtg`` entrypoint and application assembly."""

from __future__ import annotations

import functools
from functools import lru_cache
from typing import Annotated, Any

import typer
import typer.main

from xtg import __version__
from xtg.cli import output
from xtg.cli.commands import (
    analyze,
    auth,
    collect,
    db,
    export,
    info,
    proxy,
    reddit,
    search,
    status,
)
from xtg.cli.context import AppContext
from xtg.core.config import get_settings
from xtg.core.errors import ExitCode, XTGError
from xtg.core.logger import Verbosity, configure_logging


class XTGApp(typer.Typer):
    """Marker subclass: eXtractorT builds its own Click layer on top of Typer.

    The global error boundary lives in :func:`get_cli` (see
    :func:`_install_error_boundary`) so that the console script and the test
    suite share the exact same behaviour.
    """


app = XTGApp(
    name="xtg",
    help=(
        "eXtractorT - Social Media Intelligence & Extraction Toolkit.\n\n"
        "A CLI toolkit to collect, search, analyze and export Telegram and Reddit data."
    ),
    no_args_is_help=True,
    add_completion=True,
)

app.add_typer(auth.app, name="auth")
app.add_typer(proxy.app, name="proxy")
app.add_typer(reddit.app, name="reddit")
app.add_typer(info.app, name="info")
app.add_typer(analyze.app, name="analyze")
app.add_typer(db.app, name="db")

# `collect`, `search`, `export` and `status` are single commands, not groups:
# they are registered directly so `xtg collect @channel --messages` keeps working.
app.command("collect")(collect.collect)
app.command("search")(search.search)
app.command("export")(export.export)
app.command("status")(status.show)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"xtg {__version__}")
        raise typer.Exit


@app.callback()
def main(
    ctx: typer.Context,
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="Increase log verbosity (-vv).")
    ] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only log warnings.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable coloured output.")] = False,
    version: Annotated[  # noqa: ARG001 - consumed by the eager callback
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Root callback: configure logging and build the shared application context."""
    verbosity: Verbosity = _verbosity(verbose, quiet)

    if no_color:
        output.console.no_color = True
        output.err_console.no_color = True

    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings, verbosity=verbosity)
    ctx.obj = AppContext(settings)


def _verbosity(verbose: int, quiet: bool) -> Verbosity:
    if quiet:
        return "quiet"
    if verbose >= 2:
        return "debug"
    return "verbose" if verbose == 1 else "normal"


def _install_error_boundary(command: Any) -> Any:
    """Wrap ``command.main`` so every expected error becomes an exit code.

    An :class:`XTGError` is rendered as a clean message on stderr and mapped to
    its documented exit code; nothing else is swallowed, so real bugs still
    produce a traceback.
    """
    original_main = command.main

    @functools.wraps(original_main)
    def main(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_main(*args, **kwargs)
        except XTGError as error:
            output.print_error(error)
            raise SystemExit(error.exit_code) from None
        except KeyboardInterrupt:
            output.warn("Interrupted.")
            raise SystemExit(ExitCode.CANCELLED) from None
        except BrokenPipeError:  # pragma: no cover - `xtg status | head`
            raise SystemExit(ExitCode.OK) from None

    command.main = main
    return command


@lru_cache(maxsize=1)
def get_cli() -> Any:
    """Return the assembled Click command with the error boundary applied."""
    return _install_error_boundary(typer.main.get_command(app))


def run() -> None:
    """Console script entrypoint."""
    get_cli()()


if __name__ == "__main__":  # pragma: no cover
    run()
