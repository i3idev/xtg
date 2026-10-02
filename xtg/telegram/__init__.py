"""Telegram subsystem.

Telethon is imported lazily (inside :func:`xtg.telegram.client.create_client`)
so ``xtg --help``, ``xtg status`` and the whole database layer keep working
without a live MTProto dependency.
"""

from xtg.telegram.auth import AuthState, LoginResult, describe_session, login, session_path

__all__ = ["AuthState", "LoginResult", "describe_session", "login", "session_path"]
