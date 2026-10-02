"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from xtg.core.config import Settings
from xtg.database.db import Database
from xtg.proxy.manager import ProxyManager


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Isolated settings rooted in a temporary directory."""
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    config = Settings(home=home, log_to_file=False, proxy_enabled=False)
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
