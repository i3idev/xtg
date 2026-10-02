"""Rich powered output helpers.

Every command funnels its user facing output through this module so that the
look and feel is consistent: structured data on stdout, logs and errors on
stderr, and an optional ``--json`` mode for scripting.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.box import SIMPLE_HEAD
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from xtg.core.errors import ExitCode, FeatureNotAvailableError, XTGError

THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "hint": "dim italic",
        "key": "bold cyan",
        "muted": "dim",
        "accent": "bold magenta",
    }
)

#: stdout is reserved for data, stderr for diagnostics.
console = Console(theme=THEME, soft_wrap=False)
err_console = Console(theme=THEME, stderr=True, soft_wrap=False)


def set_json_mode(enabled: bool) -> None:
    """Silence Rich decorations when raw JSON is requested."""
    if enabled:
        console.no_color = True
        console.is_jupyter = False


def info(message: str) -> None:
    console.print(f"[info]›[/info] {message}")


def success(message: str) -> None:
    console.print(f"[success]✓[/success] {message}")


def warn(message: str) -> None:
    err_console.print(f"[warning]![/warning] {message}")


def hint(message: str) -> None:
    err_console.print(f"[hint]{message}[/hint]")


def fail(message: str) -> None:
    err_console.print(f"[error]✗[/error] {message}")


def heading(message: str, subtitle: str = "") -> None:
    """Print a section heading with an optional subtitle."""
    console.print()
    console.print(
        f"[accent]{message}[/accent]" + (f" [muted]{subtitle}[/muted]" if subtitle else "")
    )


def print_error(error: XTGError) -> None:
    """Render an :class:`XTGError` on stderr, including its hint."""
    fail(error.message)
    if error.hint:
        hint(error.hint)


def not_implemented(feature: str, *, phase: str = "Phase 2") -> NoReturn:
    """Abort a command that belongs to a later development phase.

    Raises:
        FeatureNotAvailableError: always, so the process exits with code 9.
    """
    raise FeatureNotAvailableError(feature, phase=phase)


def handle_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Translate an :class:`XTGError` into a message plus a stable exit code.

    Applied to every command so that the tests and the console script behave
    identically: the error is printed on stderr and the process exits with the
    documented code instead of showing a traceback.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except XTGError as error:
            print_error(error)
            raise typer.Exit(code=error.exit_code) from None

    return wrapper


def table(
    title: str | None = None,
    *,
    columns: Sequence[str] = (),
    rows: Iterable[Sequence[Any]] = (),
    box: Any = SIMPLE_HEAD,
    expand: bool = False,
) -> Table:
    """Build a styled table."""
    result = Table(
        title=title,
        box=box,
        show_lines=False,
        header_style="bold",
        title_style="accent",
        expand=expand,
        pad_edge=False,
    )
    for column in columns:
        result.add_column(column, overflow="fold")
    for row in rows:
        result.add_row(*["" if value is None else str(value) for value in row])
    return result


def print_table(result: Table) -> None:
    console.print(result)


def kv_panel(data: Mapping[str, Any], *, title: str = "Details", key_width: int = 18) -> None:
    """Render a mapping as an aligned key/value panel."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="key", no_wrap=True, width=key_width)
    grid.add_column(style="default", overflow="fold")
    for key, value in data.items():
        grid.add_row(str(key), "-" if value in (None, "") else str(value))
    console.print(Panel(grid, title=title, title_align="left", expand=False))


def emit_json(payload: Any) -> None:
    """Print a JSON document with stable key order."""
    console.print_json(json.dumps(payload, default=_json_default, ensure_ascii=False))


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, set | frozenset):
        return sorted(value)
    return str(value)


def confirm(question: str, *, default: bool = False, yes: bool = False) -> bool:
    """Ask for confirmation on stderr; abort the command when declined."""
    if yes:
        return True
    try:
        return typer.confirm(question, default=default)
    except (typer.Abort, EOFError, KeyboardInterrupt) as exc:
        raise typer.Exit(code=ExitCode.CANCELLED) from exc


def progress_note(message: str) -> None:
    """Neutral status line used while long operations run."""
    err_console.print(Text(message, style="muted"))
