"""``xtg proxy`` - Proxy management.

Two subsystems share the ``xtg proxy`` namespace:

* ``xtg proxy global`` - the global pool (``~/.config/xtg/proxy/``). It is the
  single source of truth for every network consumer: enable/disable it, seed
  the pool, pick the active proxy, test entries via sing-box, and refresh from
  a subscription URL. The Telegram client reads from here when enabled.
* ``xtg proxy telegram`` - the legacy per-command proxy list
  (``XTG_PROXY_FILE``). It still backs ``xtg collect`` / ``xtg auth`` /
  ``xtg forward`` when the global pool is disabled.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import typer

from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.config import mask_secret
from xtg.core.errors import ExitCode, ProxyError
from xtg.proxy.checker import ProxyCheckResult
from xtg.proxy.core.manager import GlobalProxyManager
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyEntry, ProxyType
from xtg.proxy.sources.manager import SourcesManager

app = typer.Typer(
    name="proxy",
    help="Proxy management: the global pool and the legacy per-command list.",
    no_args_is_help=True,
)

global_app = typer.Typer(
    name="global",
    help="Manage the global proxy pool (sing-box backed).",
    no_args_is_help=True,
)
app.add_typer(global_app, name="global")

telegram_app = typer.Typer(
    name="telegram",
    help="Manage the legacy per-command proxy list used by collect/auth/forward.",
    no_args_is_help=True,
)
app.add_typer(telegram_app, name="telegram")

source_app = typer.Typer(
    name="source",
    help="Manage named subscription sources for the global pool.",
    no_args_is_help=True,
)
global_app.add_typer(source_app, name="source")


@global_app.command("status")
def status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Show global proxy status."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    active_uri = manager.get_active_proxy_uri()
    active_id = manager.active_proxy_id
    pool = manager.get_pool()

    if json_output:
        output.emit_json(
            {
                "enabled": manager.enabled,
                "active": active_id,
                "active_uri": active_uri,
                "pool_size": len(pool),
                "proxies": pool,
            }
        )
        return

    output.kv_panel(
        {
            "enabled": "yes" if manager.enabled else "no",
            "active": active_id or "none",
            "active_uri": active_uri or "none",
            "pool size": str(len(pool)),
        },
        title="Global Proxy Status",
    )

    if pool:
        output.print_table(
            output.table(
                columns=("ID", "URI"),
                rows=[
                    (pid, uri[:80] + "..." if len(uri) > 80 else uri) for pid, uri in pool.items()
                ],
            )
        )


@global_app.command("enable")
def enable(ctx: typer.Context) -> None:
    """Enable global proxy."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)
    manager.enable()
    output.success("Global proxy enabled")


@global_app.command("disable")
def disable(ctx: typer.Context) -> None:
    """Disable global proxy."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)
    manager.disable()
    output.success("Global proxy disabled")


@global_app.command("list")
def list_proxies(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """List all proxies in the global pool."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)
    pool = manager.get_pool()

    if json_output:
        output.emit_json({"proxies": pool, "count": len(pool)})
        return

    if not pool:
        output.warn("Proxy pool is empty.")
        return

    output.print_table(
        output.table(
            columns=("ID", "URI"),
            rows=[(pid, uri[:80] + "..." if len(uri) > 80 else uri) for pid, uri in pool.items()],
        )
    )
    output.info(f"Total: {len(pool)} proxies")


@global_app.command("add")
def add_proxy(
    ctx: typer.Context,
    uri: str = typer.Argument(..., help="Proxy URI (ss://, vmess://, vless://, etc.)"),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Custom ID (auto-generated if omitted)."
    ),
) -> None:
    """Add a proxy to the global pool."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    # Generate ID if not provided
    name = name or f"P{hashlib.md5(uri.encode()).hexdigest()[:8]}"

    manager.add_proxy(name, uri)
    output.success(f"Added proxy {name}")


@global_app.command("remove")
def remove_proxy(
    ctx: typer.Context,
    name: str = typer.Argument(
        ...,
        help=(
            "Proxy ID to remove, or a range ``START=>END`` "
            "(e.g. ``P1=>P456`` removes P1 through P456)."
        ),
    ),
) -> None:
    """Remove a proxy from the pool."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    if "=>" in name:
        start, end = name.split("=>", 1)
        removed = manager.remove_range(start.strip(), end.strip())
        if not removed:
            output.warn(f"No proxies in range {name}")
            return
        output.success(f"Removed {len(removed)} proxies in range {name}")
        for pid in removed:
            output.line(f"  - {pid}")
        return

    if manager.remove_proxy(name):
        output.success(f"Removed proxy {name}")
    else:
        output.warn(f"Proxy {name} not found")


@global_app.command("clear")
def clear_pool(ctx: typer.Context) -> None:
    """Clear all proxies from the pool."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)
    manager.clear_pool()
    output.success("Proxy pool cleared")


@global_app.command("use")
def use_proxy(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Proxy ID to set as active."),
) -> None:
    """Set the active proxy."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    try:
        manager.set_active(name)
    except ProxyError as e:
        output.fail(str(e))
        raise typer.Exit(code=ExitCode.USAGE) from e
    output.success(f"Active proxy set to {name}")


@global_app.command("test")
def test_proxy(
    ctx: typer.Context,
    name: str = typer.Argument(None, help="Proxy ID to test (default: all)."),
    timeout: float = typer.Option(10.0, "--timeout", min=1.0, help="Test timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Test a proxy or all proxies via sing-box."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    if name:
        pool = manager.get_pool()
        if name not in pool:
            output.fail(f"Proxy {name} not found")
            raise typer.Exit(code=ExitCode.USAGE)

        result = asyncio.run(manager.test_proxy(name, timeout=timeout))
        _render_test_result(name, pool[name], result, json_output)
    else:
        output.progress_note("Testing all proxies...")
        results = asyncio.run(manager.test_all(timeout=timeout))
        _render_test_results(results, json_output)


@global_app.command("refresh")
def refresh(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Subscription URL."),
    run_test: bool = typer.Option(True, "--test/--no-test", help="Test proxies after fetching."),
    timeout: float = typer.Option(10.0, "--timeout", min=1.0, help="Test timeout per proxy."),
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Refresh proxy pool from a subscription URL."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    output.progress_note("Fetching subscription...")

    result = asyncio.run(
        manager.refresh_from_subscription(
            url,
            test_proxies=run_test,
            timeout=timeout,
        )
    )

    if json_output:
        output.emit_json(result)
    else:
        output.success(f"Refreshed: {result['added']} added, {result['removed']} removed")
        output.info(f"Tested: {result['tested']}, Working: {result['working']}")


@source_app.command("list")
def source_list(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """List every registered subscription source."""
    manager = _sources_manager(ctx)
    sources = manager.sources

    if json_output:
        output.emit_json({"sources": sources, "count": len(sources)})
        return

    if not sources:
        output.warn("No subscription sources registered.")
        output.hint("Add one with `xtg proxy global source add <name> <url>`.")
        return

    output.print_table(
        output.table(
            columns=("NAME", "URL"),
            rows=list(sources.items()),
        )
    )
    output.info(f"Total: {len(sources)} sources")


@source_app.command("add")
def source_add(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Source name (unique key)."),
    url: str = typer.Argument(..., help="Subscription URL."),
) -> None:
    """Register a subscription source."""
    manager = _sources_manager(ctx)
    manager.add(name, url)
    output.success(f"Added source {name}")


@source_app.command("remove")
def source_remove(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Source name to remove."),
) -> None:
    """Remove a subscription source."""
    manager = _sources_manager(ctx)
    if manager.remove(name):
        output.success(f"Removed source {name}")
    else:
        output.warn(f"Source {name} not found")


@source_app.command("refresh")
def source_refresh(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Source name to refresh from."),
    run_test: bool = typer.Option(True, "--test/--no-test", help="Test proxies after fetching."),
    timeout: float = typer.Option(10.0, "--timeout", min=1.0, help="Test timeout per proxy."),
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Refresh the global pool from a registered subscription source."""
    app_ctx = get_app_context(ctx)
    manager = GlobalProxyManager.from_settings(app_ctx.settings)

    sources = _sources_manager(ctx)
    url = sources.get(name)
    if url is None:
        output.fail(f"Source {name} not found")
        raise typer.Exit(code=ExitCode.NOT_FOUND)

    output.progress_note(f"Fetching subscription from {name}...")
    try:
        result = asyncio.run(
            manager.refresh_from_subscription(
                url,
                test_proxies=run_test,
                timeout=timeout,
            )
        )
    except ProxyError as exc:
        output.fail(str(exc))
        if exc.hint:
            output.hint(exc.hint)
        raise typer.Exit(code=ExitCode.PROXY) from exc

    if json_output:
        output.emit_json({"source": name, "url": url, **result})
    else:
        output.success(
            f"Refreshed from {name}: {result['added']} added, {result['removed']} removed"
        )
        output.info(f"Tested: {result['tested']}, Working: {result['working']}")


@source_app.command("clear")
def source_clear(ctx: typer.Context) -> None:
    """Remove every subscription source."""
    manager = _sources_manager(ctx)
    count = manager.clear()
    if count:
        output.success(f"Cleared {count} sources")
    else:
        output.warn("No sources to clear")


def _sources_manager(ctx: typer.Context) -> SourcesManager:
    """Build a SourcesManager pointing at the configured proxy state dir."""
    app_ctx = get_app_context(ctx)
    state_dir = app_ctx.settings.home / ".config" / "xtg" / "proxy"
    return SourcesManager(state_dir)


def _render_test_result(name: str, uri: str, result: dict, json_output: bool) -> None:
    if json_output:
        output.emit_json({"id": name, "uri": uri, **result})
        return

    status = "✓" if result.get("alive") else "✗"
    latency = f"{result.get('latency_ms', 0):.0f}ms" if result.get("latency_ms") else "N/A"
    error = result.get("error", "")

    output.print_table(
        output.table(
            columns=("NAME", "URI", "STATUS", "LATENCY", "ERROR"),
            rows=[
                (
                    name,
                    uri[:60] + "..." if len(uri) > 60 else uri,
                    status,
                    latency,
                    error,
                ),
            ],
        )
    )


def _render_test_results(results: list[dict], json_output: bool) -> None:
    if json_output:
        output.emit_json({"results": results})
        return

    output.print_table(
        output.table(
            columns=("ID", "URI", "STATUS", "LATENCY", "ERROR"),
            rows=[
                (
                    r.get("id", ""),
                    _clip(r.get("uri", "")),
                    "✓" if r.get("alive") else "✗",
                    f"{r.get('latency_ms', 0):.0f}ms" if r.get("latency_ms") else "N/A",
                    r.get("error", "-"),
                )
                for r in results
            ],
        )
    )
    alive = sum(1 for r in results if r.get("alive"))
    output.info(f"Alive: {alive}/{len(results)}")


def _clip(value: str, limit: int = 60) -> str:
    return value[:limit] + "..." if len(value) > limit else value


# ---------------------------------------------------------------------------
# Legacy per-command proxy list (``xtg proxy telegram``)
# ---------------------------------------------------------------------------


@telegram_app.command("list")
@output.handle_errors
def list_legacy(
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


@telegram_app.command("check")
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
    output.progress_note(f"Checking {len(selected)} proxy/proxies with a {timeout:g}s timeout...")

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
        for result in results:
            if result.detail and result.entry.type is ProxyType.MTPROTO:
                output.info(f"{result.entry.name}: {result.detail}")

    if any(result.alive for result in results):
        return
    if any(result.status in ("invalid", "unsupported") for result in results):
        raise typer.Exit(code=ExitCode.CONFIG_ERROR)
    raise typer.Exit(code=ExitCode.NETWORK)


@telegram_app.command("show")
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
    """Health as tracked by the last ``xtg proxy telegram check`` run."""
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
        "detail": result.detail,
    }


__all__ = ["app", "global_app", "telegram_app", "source_app"]
