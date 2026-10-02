"""``xtg status`` - one screen overview of the whole toolkit."""

from __future__ import annotations

import platform
import sys
from typing import Any

import typer

from xtg import __version__
from xtg.cli import output
from xtg.cli.context import get_app_context
from xtg.core.config import mask_secret
from xtg.core.utils import format_size, pluralize


@output.handle_errors
def show(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Machine readable output."),
) -> None:
    """Show version, paths, database, proxy and authentication status."""
    app_ctx = get_app_context(ctx)
    settings = app_ctx.settings
    payload: dict[str, Any] = _collect(app_ctx)

    if json_output:
        output.emit_json(payload)
        return

    output.kv_panel(
        {
            "version": __version__,
            "python": f"{platform.python_version()} ({sys.executable})",
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        },
        title="eXtractorT",
    )

    output.kv_panel(
        {
            "home": str(settings.home),
            "data directory": str(settings.data_dir),
            "database": f"{payload['database']['file']} ({payload['database']['size_human']})",
            "database rows": payload["database"]["total_rows"],
            "session directory": str(settings.sessions_path),
            "export directory": str(settings.exports_path),
            "log directory": str(settings.logs_path),
        },
        title="Storage",
    )

    output.kv_panel(
        {
            "telegram credentials": "configured" if settings.has_api_credentials else "missing",
            "api id": settings.api_id or "-",
            "api hash": mask_secret(settings.api_hash) or "-",
            "session name": settings.session_name,
            "session file": payload["auth"]["session_file"],
            "authorized": "yes" if payload["auth"]["session_exists"] else "no",
        },
        title="Authentication",
    )

    output.kv_panel(
        {
            "enabled": "yes" if payload["proxy"]["enabled"] else "no",
            "list file": payload["proxy"]["file"] or "-",
            "valid proxies": payload["proxy"]["valid"],
            "invalid entries": payload["proxy"]["invalid"],
            "alive / dead": f"{payload['proxy']['alive']} / {payload['proxy']['dead']}",
        },
        title="Proxy",
    )

    output.heading("Dataset", subtitle=pluralize(payload["database"]["total_rows"], "row"))
    output.print_table(
        output.table(
            columns=("TABLE", "ROWS"),
            rows=[(name, str(value)) for name, value in payload["database"]["tables"].items()],
        )
    )

    if not settings.has_api_credentials:
        output.hint(
            f"Add XTG_API_ID / XTG_API_HASH to {settings.env_file} to enable `xtg auth login`."
        )
    if payload["database"]["total_rows"] == 0:
        output.hint("Nothing collected yet - see `xtg collect --help` (Phase 2).")


def _collect(app_ctx) -> dict[str, Any]:
    """Gather every status section. Never raises: degraded info is reported."""
    settings = app_ctx.settings
    snapshot = app_ctx.snapshot()

    database: dict[str, Any] = {
        "file": str(settings.database_file),
        "exists": settings.database_file.exists(),
        "size_bytes": 0,
        "size_human": format_size(0),
        "tables": {},
        "total_rows": 0,
    }
    try:
        db = app_ctx.db
        counts = db.counts()
        size = db.size_bytes()
        database.update(
            exists=db.exists,
            size_bytes=size,
            size_human=format_size(size),
            tables=counts,
            total_rows=sum(counts.values()),
        )
    except Exception as exc:  # noqa: BLE001 - status must never crash
        database["error"] = str(exc)

    proxy: dict[str, Any] = {
        "enabled": False,
        "file": str(settings.proxy_path) if settings.proxy_enabled else None,
        "valid": 0,
        "invalid": 0,
        "alive": 0,
        "dead": 0,
    }
    if settings.proxy_enabled:
        try:
            manager = app_ctx.proxies
            stats = manager.stats()
            proxy.update(
                valid=stats.valid, invalid=stats.invalid, alive=stats.alive, dead=stats.dead
            )
        except Exception as exc:  # noqa: BLE001 - status must never crash
            proxy["error"] = str(exc)

    session_file = settings.session_file
    session_exists = (
        any(path.is_file() for path in session_file.parent.glob(f"{session_file.name}*"))
        if session_file.parent.exists()
        else False
    )

    return {
        "version": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "paths": snapshot["paths"],
        "settings": snapshot["settings"],
        "database": database,
        "proxy": proxy,
        "auth": {
            "configured": settings.has_api_credentials,
            "session_name": settings.session_name,
            "session_file": str(session_file),
            "session_exists": session_exists,
        },
    }
