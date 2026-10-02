"""PRAW client construction.

PRAW is imported lazily, exactly like Telethon: ``xtg --help``, ``xtg status``
and the whole local dataset keep working when the dependency is missing, and
the error only surfaces on the command that actually needs Reddit.

The credentials come from :class:`~xtg.core.config.Settings` only - nothing is
hardcoded, and nothing is logged.
"""

from __future__ import annotations

from typing import Any

from xtg.core.config import Settings, get_settings
from xtg.core.errors import ConfigError
from xtg.core.logger import logger

PRAW_HINT = 'Install the Reddit client with `uv add "praw>=7.7"` followed by `uv sync`.'


def praw_available() -> bool:
    """True when PRAW can be imported."""
    try:
        import praw  # noqa: F401 - probe only
    except ImportError:
        return False
    return True


def create_reddit(
    settings: Settings | None = None,
    *,
    factory: Any | None = None,
    proxies: str | None = None,
) -> Any:
    """Build a :class:`praw.Reddit` client from the configuration.

    Args:
        settings: configuration to read; defaults to the process settings.
        factory: override for ``praw.Reddit`` (used by the tests).
        proxies: optional ``scheme://host:port`` proxy for the HTTP session.

    Raises:
        ConfigError: when PRAW is missing or the credentials are incomplete.
    """
    settings = settings or get_settings()
    settings.require_reddit_credentials()

    client_class = factory if factory is not None else _import_praw()
    kwargs = settings.reddit_client_kwargs()
    if proxies:
        # PRAW passes this straight to its requests session.
        kwargs["proxies"] = proxies

    logger.info("Creating Reddit client (user_agent={})", kwargs["user_agent"])
    try:
        return client_class(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced as a configuration problem
        raise ConfigError(
            f"PRAW refused the Reddit credentials: {exc}",
            hint=(
                "Check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET, the 'script' app type and the "
                "descriptive REDDIT_USER_AGENT."
            ),
        ) from exc


def _import_praw() -> Any:
    try:
        import praw
    except ImportError as exc:
        raise ConfigError("PRAW is not installed", hint=PRAW_HINT) from exc
    return praw.Reddit


def describe_client(client: Any) -> dict[str, Any]:
    """Read-only summary of a client, safe to print (no secrets)."""
    read_only = getattr(client, "read_only", None)
    return {
        "user_agent": getattr(client, "user_agent", None),
        "read_only": None if read_only is None else bool(read_only),
        "authenticated": bool(getattr(client, "_redditor", None)) or read_only is False,
    }
