"""Tests for the proxy list loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from xtg.core.errors import ProxyError
from xtg.proxy.loader import load_proxy_list, write_proxy_list
from xtg.proxy.parser import ProxyEntry


def test_load_canonical_format(proxy_file: Path) -> None:
    result = load_proxy_list(proxy_file)
    assert [entry.name for entry in result.entries] == ["P1", "P2", "P3", "P4"]
    assert result.invalid == []
    assert result.total == 4
    assert result.entries[1].has_auth is True
    assert result.entries[2].has_auth is False


def test_invalid_entries_are_reported_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps({"proxys": {"GOOD": "socks5://1.2.3.4:1080", "BAD": "ftp://x:21"}}),
        encoding="utf-8",
    )
    result = load_proxy_list(path)
    assert len(result.entries) == 1
    assert result.invalid[0].name == "BAD"
    assert "ftp" in result.invalid[0].reason


def test_alternative_shapes_are_supported(tmp_path: Path) -> None:
    as_list = tmp_path / "list.json"
    as_list.write_text(json.dumps(["socks5://1.1.1.1:1080"]), encoding="utf-8")
    assert load_proxy_list(as_list).entries[0].endpoint == "1.1.1.1:1080"

    as_objects = tmp_path / "objects.json"
    as_objects.write_text(
        json.dumps([{"name": "A", "host": "2.2.2.2", "port": 1080, "scheme": "http"}]),
        encoding="utf-8",
    )
    entry = load_proxy_list(as_objects).entries[0]
    assert (entry.name, entry.scheme, entry.endpoint) == ("A", "http", "2.2.2.2:1080")

    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"A": "1.1.1.1:1080"}), encoding="utf-8")
    assert load_proxy_list(flat).entries[0].name == "A"

    correct_plural = tmp_path / "plural.json"
    correct_plural.write_text(json.dumps({"proxies": {"A": "1.1.1.1:1080"}}), encoding="utf-8")
    assert load_proxy_list(correct_plural).entries[0].endpoint == "1.1.1.1:1080"


def test_object_without_host_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "objects.json"
    path.write_text(json.dumps([{"name": "A", "port": 1080}]), encoding="utf-8")
    result = load_proxy_list(path)
    assert not result.entries
    assert "host" in result.invalid[0].reason


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProxyError):
        load_proxy_list(tmp_path / "nope.json")


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProxyError):
        load_proxy_list(path)


def test_unsupported_shape_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"unexpected": 42}), encoding="utf-8")
    with pytest.raises(ProxyError):
        load_proxy_list(path)


def test_write_roundtrip(tmp_path: Path) -> None:
    entries = [parse("P1", "socks5://1.2.3.4:1080"), parse("P2", "http://h:8080")]
    path = write_proxy_list(tmp_path / "out.json", entries)
    reloaded = load_proxy_list(path)
    assert [entry.url for entry in reloaded.entries] == [entry.url for entry in entries]


def parse(name: str, raw: str) -> ProxyEntry:
    from xtg.proxy.parser import parse_proxy_url

    return parse_proxy_url(name, raw)
