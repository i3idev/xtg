"""Telegram subsystem boundary.

Importing this package never imports Telethon: the MTProto client is created
lazily inside :func:`xtg.telegram.client.get_client` so that `xtg --help`,
`xtg status` and the whole database layer keep working without the dependency
installed.
"""

from xtg.telegram.auth import AuthState, session_path

__all__ = ["AuthState", "session_path"]
