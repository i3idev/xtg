"""Shared per-invocation state for CLI commands.

The context owns the lazily created :class:`~xtg.database.db.Database` and
:class:`~xtg.proxy.manager.ProxyManager`, so a command only pays for the
subsystems it actually touches and the engine is always disposed.
"""

from __future__ import annotations

from typing import Any

import typer

from xtg.core.config import Settings, get_settings
from xtg.database.db import Database
from xtg.proxy.manager import ProxyManager
from xtg.sources.base import BaseSource
from xtg.sources.registry import get_source
from xtg.telegram.client import TelegramConnection


class AppContext:
    """Lazy access to the subsystems used by the commands."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings: Settings = settings or get_settings()
        self._database: Database | None = None
        self._proxies: ProxyManager | None = None

    @property
    def db(self) -> Database:
        """The SQLite database, schema created on first access."""
        if self._database is None:
            self._database = Database(self.settings)
            self._database.create_all()
        return self._database

    @property
    def proxies(self) -> ProxyManager:
        """The proxy manager built from the configured list file."""
        if self._proxies is None:
            self._proxies = ProxyManager.from_settings(self.settings)
        return self._proxies

    def shutdown(self) -> None:
        if self._database is not None:
            self._database.dispose()
            self._database = None

    def snapshot(self) -> dict[str, Any]:
        """Small dict used by ``xtg status --json``."""
        return {"settings": self.settings.redacted(), "paths": self._paths()}

    def connection(self, *, require_authorized: bool = True) -> TelegramConnection:
        """Build a Telegram connection from the current configuration.

        The object is lazy: nothing connects until the client is requested, so
        commands that fail validation never open a socket.
        """
        return TelegramConnection(self.settings, require_authorized=require_authorized)

    def source(self, name: str, **kwargs: Any) -> BaseSource:
        """Build a collection source by name (``telegram``, ``reddit``, ...).

        Sources are imported lazily, so a command only pays for the platform it
        actually uses.
        """
        return get_source(name, self.settings, database=self.db, **kwargs)

    def _paths(self) -> dict[str, str]:
        settings = self.settings
        return {
            "home": str(settings.home),
            "data": str(settings.data_dir),
            "database": str(settings.database_file),
            "sessions": str(settings.sessions_path),
            "exports": str(settings.exports_path),
            "logs": str(settings.logs_path),
            "proxy_file": str(settings.proxy_path),
        }


def get_app_context(ctx: typer.Context | None = None) -> AppContext:
    """Return the context attached to the Typer invocation."""
    if ctx is not None and isinstance(ctx.obj, AppContext):
        return ctx.obj
    return AppContext()
