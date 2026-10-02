"""Tests for the Telethon client layer: config loading and proxy integration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.fakes import FakeClient
from xtg.core.config import Settings
from xtg.core.errors import AuthError, ConfigError, ProxyError
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import parse_proxy_url
from xtg.telegram.client import (
    ClientOptions,
    TelegramConnection,
    create_client,
    resolve_options,
    select_proxy,
    telethon_available,
    to_telethon_proxy,
)


def configured(tmp_path: Path, **overrides) -> Settings:
    values: dict[str, object] = {
        "home": tmp_path,
        "api_id": 12345,
        "api_hash": "hash",
        "log_to_file": False,
        "proxy_enabled": False,
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.ensure_directories()
    return settings


# --- option resolution ------------------------------------------------------


def test_options_from_config(tmp_path: Path) -> None:
    settings = configured(tmp_path)
    options = resolve_options(settings)

    assert options.api_id == 12345
    assert options.api_hash == "hash"
    assert options.session == settings.session_file
    assert options.proxy is None
    assert options.proxy_url == "direct connection"


def test_options_require_credentials(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path, log_to_file=False, proxy_enabled=False)
    with pytest.raises(ConfigError):
        resolve_options(settings)


def test_options_create_session_directory(tmp_path: Path) -> None:
    settings = configured(tmp_path)
    resolve_options(settings)
    assert settings.sessions_path.is_dir()


def test_options_read_retry_settings(tmp_path: Path) -> None:
    settings = configured(tmp_path, max_retries=7, connect_timeout=3.5)
    options = resolve_options(settings)
    assert options.connection_retries == 7
    assert options.request_retries == 7
    assert options.connect_timeout == 3.5


# --- proxy integration ------------------------------------------------------


def test_no_proxy_when_disabled(tmp_path: Path) -> None:
    assert select_proxy(configured(tmp_path)) is None


def test_proxy_selected_from_list(tmp_path: Path) -> None:
    settings = configured(tmp_path, proxy_enabled=True)
    entry = select_proxy(settings)
    assert entry is not None
    assert entry.name == "P1"


def test_proxy_translation_without_credentials() -> None:
    entry = parse_proxy_url("P1", "socks5://127.0.0.1:1080")
    assert to_telethon_proxy(entry) == {
        "proxy_type": "socks5",
        "addr": ("127.0.0.1", 1080),
    }


def test_proxy_translation_with_credentials() -> None:
    entry = parse_proxy_url("P2", "http://user:pass@proxy.local:8080")
    payload = to_telethon_proxy(entry)
    assert payload == {
        "proxy_type": "http",
        "addr": ("proxy.local", 8080),
        "username": "user",
        "password": "pass",
    }


def test_proxy_translation_is_none_for_direct() -> None:
    assert to_telethon_proxy(None) is None


def test_options_kwargs_carry_proxy(tmp_path: Path, proxy_file: Path) -> None:
    settings = configured(tmp_path, proxy_enabled=True, proxy_file=proxy_file)
    manager = ProxyManager.from_settings(settings)
    options = resolve_options(settings, proxies=manager)
    kwargs = options.as_kwargs()

    assert kwargs["proxy"] == {
        "proxy_type": "socks5",
        "addr": ("127.0.0.1", 1080),
    }
    assert kwargs["session"] == str(settings.session_file)


def test_proxy_error_propagates(tmp_path: Path, proxy_file: Path) -> None:
    settings = configured(tmp_path, proxy_enabled=True, proxy_file=proxy_file)
    manager = ProxyManager.from_settings(settings)
    with pytest.raises(ProxyError):
        manager.get("P9")


# --- client creation and lifecycle -----------------------------------------


def test_create_client_requires_telethon(tmp_path: Path) -> None:
    options = resolve_options(configured(tmp_path))
    if telethon_available():  # pragma: no cover - depends on the environment
        assert create_client(options) is not None
    else:
        with pytest.raises(ConfigError) as exc:
            create_client(options)
        assert "Telethon is not installed" in str(exc.value)


def test_connection_is_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeClient] = []
    monkeypatch.setattr(
        "xtg.telegram.client.create_client",
        lambda _options: created.append(FakeClient()) or created[-1],
    )
    connection = TelegramConnection(configured(tmp_path))
    assert created == []  # nothing is built before open() is awaited

    client = asyncio.run(connection.open())
    assert client.connected is True
    assert asyncio.run(connection.open()) is client  # reusable instance


def test_connection_requires_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(authorized=False)
    monkeypatch.setattr("xtg.telegram.client.create_client", lambda _options: fake)
    with pytest.raises(AuthError):
        asyncio.run(TelegramConnection(configured(tmp_path)).open())
    assert fake.disconnected is True


def test_connection_can_skip_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(authorized=False)
    monkeypatch.setattr("xtg.telegram.client.create_client", lambda _options: fake)
    connection = TelegramConnection(configured(tmp_path), require_authorized=False)
    assert asyncio.run(connection.open()) is fake


def test_connection_close_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr("xtg.telegram.client.create_client", lambda _options: fake)
    connection = TelegramConnection(configured(tmp_path))
    asyncio.run(connection.open())
    asyncio.run(connection.close())
    assert fake.disconnected is True
    asyncio.run(connection.close())  # second close is a no-op


def test_connection_async_context_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr("xtg.telegram.client.create_client", lambda _options: fake)

    async def scenario() -> FakeClient:
        connection = TelegramConnection(configured(tmp_path))
        async with connection as client:
            assert client is fake
            return client

    assert asyncio.run(scenario()) is fake
    assert fake.disconnected is True


def test_client_property_requires_open(tmp_path: Path) -> None:
    connection = TelegramConnection(configured(tmp_path))
    with pytest.raises(RuntimeError):
        _ = connection.client


def test_client_options_defaults() -> None:
    options = ClientOptions(session=Path("s"), api_id=1, api_hash="h")
    assert options.proxy is None
    assert options.as_kwargs()["proxy"] is None


def test_connection_repr_mentions_proxy(tmp_path: Path) -> None:
    connection = TelegramConnection(configured(tmp_path))
    assert "direct connection" in repr(connection)
