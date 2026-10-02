"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from xtg.core.config import Settings, reload_settings
from xtg.database.db import Database
from xtg.proxy.manager import ProxyManager

#: Every environment variable this project reads. Real variables outrank the
#: ``.env`` file, which is correct in production but means a developer shell
#: that exported the project's own ``.env`` silently configures the whole test
#: suite - with real credentials. ``XTG_`` is the settings prefix; ``REDDIT_``
#: covers the unprefixed aliases the Reddit fields also accept.
CONFIG_ENV_PREFIXES = ("XTG_", "REDDIT_")


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip the project's own environment variables around every test.

    Tests declare the configuration they need explicitly, so the ambient copy
    is removed once here, for every test, rather than patched out one test at a
    time. ``monkeypatch`` restores the variables on teardown.
    """
    for name in [name for name in os.environ if name.startswith(CONFIG_ENV_PREFIXES)]:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _fresh_settings_cache() -> Iterator[None]:
    """Drop the process-wide settings cache around every test.

    ``get_settings()`` is cached, so without this each test would reuse the home
    directory of whichever test ran first.
    """
    reload_settings()
    yield
    reload_settings()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Isolated settings rooted in a temporary directory."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("XTG_HOME", str(home))
    monkeypatch.setenv("XTG_LOG_TO_FILE", "false")
    monkeypatch.setenv("XTG_PROXY_ENABLED", "false")
    config = reload_settings()
    config.ensure_directories()
    return config


@pytest.fixture
def database(settings: Settings) -> Iterator[Database]:
    """A ready-to-use SQLite database inside ``tmp_path``."""
    db = Database(settings)
    db.create_all()
    yield db
    db.dispose()


@pytest.fixture
def proxy_file(tmp_path: Path) -> Path:
    path = tmp_path / "list_proxy.json"
    path.write_text(
        json.dumps(
            {
                "proxys": {
                    "P1": "socks5://127.0.0.1:1080",
                    "P2": "socks5://user:pass@127.0.0.1:1081",
                    "P3": "127.0.0.1:1082",
                    "P4": "http://proxy.example.com:8080",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def proxy_manager(proxy_file: Path) -> ProxyManager:
    from xtg.proxy.loader import load_proxy_list

    return ProxyManager.from_result(load_proxy_list(proxy_file))


def row(**values: Any) -> dict[str, Any]:
    """Small helper to keep table assertions readable."""
    return values
