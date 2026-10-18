"""Tests for the subscription source (manifest resolution + parsing)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from xtg.core.errors import ProxyError
from xtg.proxy.sources.subscription import (
    PROXY_SCHEMES,
    SubscriptionSource,
    _extract_manifest_urls,
    _is_manifest_url,
)


def test_parse_plain_subscription() -> None:
    source = SubscriptionSource()
    content = "ss://YWVzLTI1Ni1nY206cGFzc0BleGFtcGxlLmNvbTo4MDgw\ntrojan://host:443\n"
    proxies = source.parse(content)
    assert len(proxies) == 2
    assert all(uri.startswith(("ss://", "trojan://")) for uri in proxies.values())


def test_parse_base64_subscription() -> None:
    import base64

    source = SubscriptionSource()
    content = base64.b64encode(b"ss://host:443\ntrojan://host2:443").decode()
    proxies = source.parse(content)
    assert len(proxies) == 2


def test_parse_skips_comments_and_blank_lines() -> None:
    source = SubscriptionSource()
    proxies = source.parse("# comment\n\nss://host:443\n# another\n")
    assert len(proxies) == 1


def test_parse_ignores_manifest_urls() -> None:
    """A Hiddify profile that lists other subscription URLs is not a proxy."""
    source = SubscriptionSource()
    content = (
        "#profile-title: Mahsa\n"
        "https://raw.githubusercontent.com/mahsanet/MahsaFreeConfig/refs/heads/main/mci/sub_1.txt\n"
        "https://raw.githubusercontent.com/mahsanet/MahsaFreeConfig/refs/heads/main/mtn/sub_1.txt\n"
        "ss://real.proxy:443\n"
    )
    proxies = source.parse(content)
    assert len(proxies) == 1
    assert list(proxies.values())[0] == "ss://real.proxy:443"


def test_is_manifest_url_treats_http_as_manifest() -> None:
    assert _is_manifest_url("https://example.com/sub") is True
    assert _is_manifest_url("http://example.com/sub") is True


@pytest.mark.parametrize("scheme", PROXY_SCHEMES)
def test_is_manifest_url_keeps_proxy_uris(scheme: str) -> None:
    assert _is_manifest_url(f"{scheme}host:443") is False


def test_extract_manifest_urls() -> None:
    content = (
        "#profile-title: Mahsa\n"
        "https://a.example.com/1\n"
        "https://b.example.com/2\n"
        "ss://real.proxy:443\n"
    )
    assert _extract_manifest_urls(content) == [
        "https://a.example.com/1",
        "https://b.example.com/2",
    ]


def test_extract_manifest_urls_empty() -> None:
    assert _extract_manifest_urls("ss://host:443\n# comment\n") == []


def test_parse_empty_content() -> None:
    assert SubscriptionSource().parse("") == {}


def test_parse_no_proxies_raises() -> None:
    with pytest.raises(ProxyError):
        asyncio.run(SubscriptionSource().refresh("https://example.com/none"))


def test_refresh_follows_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``refresh`` follows nested subscription URLs listed in a manifest."""
    manifest = "#profile-title: Mahsa\nhttps://leaf.example.com/a\nhttps://leaf.example.com/b\n"
    leaf_a = "ss://host-a:443\ntrojan://host-b:443\n"
    leaf_b = "vmess://host-c:443\n"

    async def fake_fetch(self: SubscriptionSource, url: str) -> str:
        if url == "https://root.example.com/manifest":
            return manifest
        if url == "https://leaf.example.com/a":
            return leaf_a
        if url == "https://leaf.example.com/b":
            return leaf_b
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(SubscriptionSource, "fetch", fake_fetch)
    proxies = asyncio.run(
        SubscriptionSource(test_proxies=False).refresh("https://root.example.com/manifest")
    )
    uris = set(proxies.values())
    assert uris == {"ss://host-a:443", "trojan://host-b:443", "vmess://host-c:443"}


def test_refresh_no_manifest_is_flat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(self: SubscriptionSource, url: str) -> str:
        return "ss://host-a:443\ntrojan://host-b:443\n"

    monkeypatch.setattr(SubscriptionSource, "fetch", fake_fetch)
    proxies = asyncio.run(
        SubscriptionSource(test_proxies=False).refresh("https://leaf.example.com/a")
    )
    assert len(proxies) == 2
