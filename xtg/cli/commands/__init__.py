"""CLI command groups.

Each module exposes a Typer application named ``app`` that ``xtg.cli.main``
registers on the root command.
"""

from xtg.cli.commands import analyze, auth, collect, db, export, info, proxy, search, status

__all__ = ["analyze", "auth", "collect", "db", "export", "info", "proxy", "search", "status"]
