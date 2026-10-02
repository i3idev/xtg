"""``xtg proxy`` - proxy list management."""

from __future__ import annotations

import asyncio
from typing import Any

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.config import mask_secret
from xtg.core.errors import ExitCode
from xtg.proxy.checker import ProxyCheckResult
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyEntry

app = typer.Typer(
    name="proxy",
    help="Manage the proxy list: list, check availability, inspect status.",
    no_args_is_help=True,
)


@app.command("list")
@output.handle_errors
def list_proxies(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """List every proxy from the configured list file."""
    manager = get_app_context(ctx).proxies
    entries = manager.entries
    invalid = manager.invalid
    stats = manager.stats()

    if json_output:
        output.emit_json(
            {
                "file": str(manager.source) if manager.source else None,
                "enabled": manager.enabled,
                "proxies": [entry.to_dict() for entry in entries],
                "invalid": [
                    {"name": item.name, "raw": item.raw, "reason": item.reason} for item in invalid
                ],
            }
        )
        return

    output.heading(
        "Proxy list",
        subtitle=str(manager.source) if manager.source else "no file configured",
    )
    if not entries and not invalid:
        output.warn("The proxy list is empty.")
        raise typer.Exit

    output.print_table(
        output.table(
            columns=("NAME", "TYPE", "ENDPOINT", "AUTH", "HEALTH"),
            rows=[
                (
                    entry.name,
                    str(entry.type),
                    entry.endpoint,
                    "secret" if entry.has_secret else ("yes" if entry.has_auth else "no"),
                    _health_of(manager, entry),
                )
                for entry in entries
            ],
        )
    )
    if invalid:
        output.heading("Invalid entries")
        output.print_table(
            output.table(
                columns=("NAME", "RAW", "REASON"),
                rows=[(item.name, item.raw or "-", item.reason) for item in invalid],
            )
        )
    output.info(f"{stats.valid} valid / {stats.invalid} invalid entries")


@app.command("check")
@output.handle_errors
def check(
    ctx: typer.Context,
    names: list[str] | None = typer.Argument(None, help="Proxy name(s). Default: whole list."),
    timeout: float = typer.Option(5.0, "--timeout", min=0.1, help="Per-proxy timeout."),
    concurrency: int = typer.Option(20, "--concurrency", min=1, max=200),
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Test whether proxies can serve Telegram traffic.

    Classical proxies are checked with a TCP connect; MTProto proxies must also
    complete an obfuscated handshake, so an open port alone is not enough.
    """
    manager = get_app_context(ctx).proxies
    if not manager.entries:
        output.warn("The proxy list is empty - nothing to check.")
        raise typer.Exit(code=ExitCode.NOT_FOUND)

    selected = _select(manager, names)
    selected_names = {entry.name for entry in selected}
    output.progress_note(f"Checking {len(selected)} proxy/proxies with a {timeout:g}s timeout…")

    results = asyncio.run(manager.check(timeout=timeout, concurrency=concurrency))
    results = [result for result in results if result.entry.name in selected_names]

    if json_output:
        output.emit_json([_result_payload(result) for result in results])
    else:
        output.print_table(
            output.table(
                columns=("NAME", "TYPE", "ENDPOINT", "STATUS", "LATENCY", "ERROR"),
                rows=[
                    (
                        result.entry.name,
                        str(result.entry.type),
                        result.entry.endpoint,
                        result.status,
                        f"{result.latency_ms:.0f} ms" if result.latency_ms else "-",
                        result.error or "-",
                    )
                    for result in results
                ],
            )
        )
        alive = sum(1 for result in results if result.alive)
        output.info(f"{alive}/{len(results)} proxies usable.")

    if any(result.alive for result in results):
        return
    # Distinguish "nothing listens there" from "the entries themselves are wrong":
    # a list made of invalid or unsupported entries is a configuration problem.
    if any(result.status in ("invalid", "unsupported") for result in results):
        raise typer.Exit(code=ExitCode.CONFIG_ERROR)
    raise typer.Exit(code=ExitCode.NETWORK)


@app.command("status")
@output.handle_errors
def status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show the proxy configuration and the current selection."""
    manager = get_app_context(ctx).proxies
    stats = manager.stats()
    current = manager.next_proxy()
    payload: dict[str, Any] = {
        "enabled": manager.enabled,
        "file": str(manager.source) if manager.source else None,
        "file_exists": bool(manager.source and manager.source.exists()),
        "stats": {
            "total": stats.total,
            "valid": stats.valid,
            "invalid": stats.invalid,
            "alive": stats.alive,
            "dead": stats.dead,
        },
        "current": current.name if current else None,
    }
    if json_output:
        output.emit_json(payload)
        return

    output.kv_panel(
        {
            "enabled": "yes" if manager.enabled else "no",
            "list file": payload["file"] or "-",
            "file exists": "yes" if payload["file_exists"] else "no",
            "proxies (valid)": stats.valid,
            "invalid entries": stats.invalid,
            "alive / dead": f"{stats.alive} / {stats.dead}",
            "failures recorded": sum(stats.failures.values()),
            "current selection": current.name if current else "-",
        },
        title="Proxy status",
    )
    if not manager.enabled:
        output.hint("Enable proxies with XTG_PROXY_ENABLED=true.")
    elif stats.valid == 0:
        output.hint("Add entries to the proxy list file and run `xtg proxy list`.")


@app.command("show")
@output.handle_errors
def show(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Proxy name."),
    reveal: bool = typer.Option(False, "--reveal", help="Show the password/secret in clear text."),
) -> None:
    """Show a single proxy in detail."""
    entry = get_app_context(ctx).proxies.get(name)
    output.kv_panel(
        {
            "name": entry.name,
            "type": str(entry.type),
            "host": entry.host,
            "port": entry.port,
            "username": entry.username or "-",
            "password": (entry.password if reveal else mask_secret(entry.password)) or "-",
            "secret": (entry.secret if reveal else mask_secret(entry.secret)) or "-",
            "url": entry.url if reveal else entry.masked_url,
        },
        title=f"Proxy {entry.name}",
    )


def _select(manager: ProxyManager, names: list[str] | None) -> list[ProxyEntry]:
    return manager.entries if not names else [manager.get(name) for name in names]


def _health_of(manager: ProxyManager, entry: ProxyEntry) -> str:
    """Health as tracked by the last ``xtg proxy check`` run."""
    state = manager.health.get(entry.name)
    if state:
        return state
    failures = manager.stats().failures.get(entry.name, 0)
    return f"failed x{failures}" if failures else "not checked"


def _result_payload(result: ProxyCheckResult) -> dict[str, Any]:
    return {
        "name": result.entry.name,
        "type": str(result.entry.type),
        "endpoint": result.entry.endpoint,
        "status": result.status,
        "latency_ms": round(result.latency_ms, 2) if result.latency_ms else None,
        "error": result.error,
    }
