"""Tests for the Telethon client layer: config loading and proxy integration."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from tests.fakes import FakeClient
from xtg.core.config import Settings
from xtg.core.errors import AuthError, ConfigError, ProxyError
from xtg.proxy.manager import ProxyManager
from xtg.proxy.parser import ProxyEntry, parse_proxy_url
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


# --- MTProto proxies --------------------------------------------------------

HEX_SECRET = "0102030405060708090a0b0c0d0e0f10"


def mtproto(name: str, secret: str) -> Any:
    return parse_proxy_url(name, f"tg://proxy?server=1.2.3.4&port=443&secret={secret}")


def test_mtproto_secret_is_decoded_for_the_generic_mapping() -> None:
    payload = to_telethon_proxy(mtproto("P1", HEX_SECRET))
    assert payload == {
        "proxy_type": "mtproto",
        "addr": ("1.2.3.4", 443),
        "password": bytes.fromhex(HEX_SECRET),
    }


def test_mtproto_translation_accepts_hex_secrets() -> None:
    entry = mtproto("P1", HEX_SECRET)
    assert to_telethon_proxy(entry)["password"] == bytes.fromhex(HEX_SECRET)


def test_mtproto_translation_rejects_fake_tls_secret() -> None:
    with pytest.raises(ConfigError, match="fake-tls"):
        to_telethon_proxy(mtproto("P1", f"ee{HEX_SECRET}"))


def test_mtproto_translation_rejects_unusable_secret() -> None:
    with pytest.raises(ConfigError, match="unusable secret"):
        to_telethon_proxy(mtproto("P1", "AAAA"))


def test_mtproto_translation_requires_secret() -> None:
    entry = ProxyEntry(name="P1", scheme="mtproto", host="1.2.3.4", port=443)
    with pytest.raises(ConfigError, match="no secret"):
        to_telethon_proxy(entry)


def test_dd_secrets_are_accepted() -> None:
    """Telethon implements the randomized intermediate transport; we must not block it.

    Regression: ``dd`` secrets were rejected even though the client selects the
    very codec they require, so those proxies could never be used.
    """
    entry = mtproto("P1", f"dd{HEX_SECRET}")
    assert to_telethon_proxy(entry)["password"] == bytes.fromhex(HEX_SECRET)


@pytest.fixture
def telethon_network(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub ``telethon.network.connection`` so the mapping can be asserted.

    Telethon is an optional runtime dependency; the translation is pure logic,
    so it is tested against a stand-in with the same class names.
    """
    network = types.ModuleType("telethon.network.connection")
    network.ConnectionTcpMTProxyIntermediate = type("ConnectionTcpMTProxyIntermediate", (), {})
    network.ConnectionTcpMTProxyRandomizedIntermediate = type(
        "ConnectionTcpMTProxyRandomizedIntermediate", (), {}
    )
    telethon = types.ModuleType("telethon")
    telethon_network = types.ModuleType("telethon.network")
    telethon_network.connection = network
    telethon.network = telethon_network
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.network", telethon_network)
    monkeypatch.setitem(sys.modules, "telethon.network.connection", network)
    return network


def test_as_kwargs_uses_the_tuple_for_mtproto(telethon_network: Any) -> None:
    """MTProto needs ``(host, port, secret)``, not the generic mapping."""
    options = ClientOptions(
        session=Path("s"), api_id=1, api_hash="h", proxy=mtproto("P1", HEX_SECRET)
    )
    kwargs = options.as_kwargs()

    assert kwargs["proxy"] == ("1.2.3.4", 443, HEX_SECRET)
    assert kwargs["connection"] is telethon_network.ConnectionTcpMTProxyIntermediate


def test_as_kwargs_picks_the_codec_the_secret_requires(telethon_network: Any) -> None:
    options = ClientOptions(
        session=Path("s"), api_id=1, api_hash="h", proxy=mtproto("P1", f"dd{HEX_SECRET}")
    )
    assert (
        options.as_kwargs()["connection"]
        is telethon_network.ConnectionTcpMTProxyRandomizedIntermediate
    )


def test_as_kwargs_leaves_classical_proxies_untouched(telethon_network: Any) -> None:
    options = ClientOptions(
        session=Path("s"), api_id=1, api_hash="h", proxy=parse_proxy_url("P", "socks5://h:1")
    )
    kwargs = options.as_kwargs()

    assert kwargs["proxy"] == {"proxy_type": "socks5", "addr": ("h", 1)}
    assert "connection" not in kwargs


def test_direct_connection_never_gets_a_connection_class() -> None:
    options = ClientOptions(session=Path("s"), api_id=1, api_hash="h")
    assert "connection" not in options.as_kwargs()


def test_missing_telethon_is_reported_not_swallowed(
    telethon_network: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing Telethon must raise, never return a half-built proxy mapping."""
    monkeypatch.setitem(sys.modules, "telethon.network", None)
    options = ClientOptions(
        session=Path("s"), api_id=1, api_hash="h", proxy=mtproto("P1", HEX_SECRET)
    )
    with pytest.raises(ConfigError, match="Telethon is not installed"):
        options.as_kwargs()


# --- proxy failover ---------------------------------------------------------


def broken_and_working_list(tmp_path: Path) -> Path:
    path = tmp_path / "broken_list.json"
    path.write_text(
        json.dumps(
            {
                "proxys": {
                    "BAD": "tg://proxy?server=1.2.3.4&port=443&secret=AAAA",
                    "TLS": f"tg://proxy?server=1.2.3.4&port=443&secret=ee{HEX_SECRET}",
                    "OK": f"tg://proxy?server=5.6.7.8&port=443&secret={HEX_SECRET}",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_unusable_proxies_do_not_abort_the_connection(tmp_path: Path) -> None:
    """Regression: one malformed entry used to kill ``xtg auth login`` outright."""
    settings = configured(
        tmp_path, proxy_enabled=True, proxy_file=broken_and_working_list(tmp_path)
    )
    manager = ProxyManager.from_settings(settings)

    chosen = {resolve_options(settings, proxies=manager).proxy.name for _ in range(6)}

    assert chosen == {"OK"}


def test_a_list_with_no_usable_proxy_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "all_bad.json"
    path.write_text(
        json.dumps({"proxys": {"BAD": "tg://proxy?server=1.2.3.4&port=443&secret=AAAA"}}),
        encoding="utf-8",
    )
    settings = configured(tmp_path, proxy_enabled=True, proxy_file=path)
    manager = ProxyManager.from_settings(settings)

    with pytest.raises(ConfigError, match="No usable proxy"):
        resolve_options(settings, proxies=manager)


def test_an_all_bad_list_is_reported_not_a_crash(tmp_path: Path) -> None:
    """Several unusable entries must end in the documented error, not a traceback."""
    entries = {f"BAD{i}": f"tg://proxy?server=1.2.3.{i}&port=443&secret=AAAA{i}" for i in range(5)}
    entries["TLS"] = f"tg://proxy?server=2.2.2.2&port=443&secret=ee{HEX_SECRET}"
    path = tmp_path / "all_bad_many.json"
    path.write_text(json.dumps({"proxys": entries}), encoding="utf-8")
    settings = configured(tmp_path, proxy_enabled=True, proxy_file=path)
    manager = ProxyManager.from_settings(settings)

    with pytest.raises(ConfigError, match="No usable proxy"):
        resolve_options(settings, proxies=manager)


def test_the_bundled_proxy_list_still_yields_a_usable_connection(tmp_path: Path) -> None:
    """Authentication must survive whatever the shipped proxy list contains.

    The bundled list is scraped from public sources and mixes transports
    Telethon cannot speak (fake-TLS) with entries whose secret does not decode
    at all. Selection has to skip them and still return a proxy, because
    ``xtg auth login`` fails outright when it cannot build a client.
    """
    settings = configured(tmp_path, proxy_enabled=True)
    manager = ProxyManager.from_settings(settings)
    assert manager.usable, "the bundled proxy list must not be empty"

    options = resolve_options(settings, proxies=manager)

    assert options.proxy is not None
    assert to_telethon_proxy(options.proxy) is not None


def test_selecting_from_a_realistic_mixed_list_never_hard_fails(tmp_path: Path) -> None:
    """A list that is mostly unusable must still produce a connection."""
    entries = {"BAD1": "tg://proxy?server=1.2.3.4&port=443&secret=AAAA"}
    entries["TLS"] = f"tg://proxy?server=2.3.4.5&port=443&secret=ee{HEX_SECRET}"
    for index in range(20):
        entries[f"JUNK{index}"] = f"tg://proxy?server=9.9.9.{index}&port=443&secret=QUJD{index}"
    entries["GOOD"] = f"tg://proxy?server=8.8.8.8&port=443&secret={HEX_SECRET}"
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps({"proxys": entries}), encoding="utf-8")

    settings = configured(tmp_path, proxy_enabled=True, proxy_file=path)
    manager = ProxyManager.from_settings(settings)

    for _ in range(10):
        options = resolve_options(settings, proxies=manager)
        assert options.proxy is not None
        assert to_telethon_proxy(options.proxy) is not None


def _rejects(entry: Any) -> bool:
    try:
        to_telethon_proxy(entry)
    except ConfigError:
        return True
    return False


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
