"""Subscription source for proxy pools."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import urlparse

import aiohttp

from xtg.core.errors import ProxyError

#: URI schemes that are real proxy entries (not nested subscription URLs).
PROXY_SCHEMES = (
    "ss://",
    "vmess://",
    "vless://",
    "trojan://",
    "socks5://",
    "socks4://",
    "http://",
    "https://",
    "tg://",
)


class SubscriptionSource:
    """Handles downloading and parsing proxy subscription URLs."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        test_proxies: bool = True,
        test_timeout: float = 10.0,
        concurrency: int = 5,
    ) -> None:
        self.timeout = timeout
        self._test_proxies = test_proxies
        self.test_timeout = test_timeout
        self.concurrency = concurrency

    async def fetch(self, url: str) -> str:
        """Download subscription content."""
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session,
                session.get(url) as resp,
            ):
                if resp.status != 200:
                    raise ProxyError(f"Failed to download subscription: HTTP {resp.status}")
                return await resp.text()
        except aiohttp.ClientError as exc:
            raise ProxyError(
                f"Failed to download subscription: {exc}",
                hint="Check the URL and your network connection.",
            ) from exc

    def parse(self, content: str) -> dict[str, str]:
        """Parse subscription content and extract proxy URIs.

        Supports:
        - Base64 encoded subscription content
        - Plain text with one URI per line
        - URI formats: ss://, vmess://, vless://, trojan://, socks5://, tg://

        Lines that are themselves ``http(s)://`` URLs are treated as nested
        subscription manifests (Hiddify-style profile files that list other
        subscription URLs) and are *not* stored as proxies.
        """
        content = content.strip()

        # Try base64 decode
        try:
            decoded = base64.b64decode(content + "=" * (-len(content) % 4)).decode()
            content = decoded
        except Exception:
            pass  # Not base64, use as-is

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        proxies = {}

        for line in lines:
            if not line or line.startswith("#"):
                continue

            # A bare http(s):// URL is a nested subscription, not a proxy.
            if _is_manifest_url(line):
                continue

            if not any(line.startswith(proto) for proto in PROXY_SCHEMES):
                continue

            # Generate ID from URL hash
            pid = f"P{hashlib.md5(line.encode()).hexdigest()[:8]}"
            proxies[pid] = line

        return proxies

    async def refresh(
        self,
        url: str,
        *,
        test_proxies: bool | None = None,
        test_timeout: float | None = None,
        concurrency: int | None = None,
    ) -> dict[str, str]:
        """Refresh from subscription URL, following nested manifests."""
        test_proxies = self._test_proxies if test_proxies is None else test_proxies
        test_timeout = self.test_timeout if test_timeout is None else test_timeout
        concurrency = self.concurrency if concurrency is None else concurrency

        proxies = await self._fetch_recursive(url)

        if not proxies:
            raise ProxyError("No valid proxies found in subscription")

        if not test_proxies:
            return proxies

        from xtg.proxy.core.singbox import test_proxy_via_singbox

        semaphore = asyncio.Semaphore(self.concurrency)

        async def test_one(uri: str) -> dict:
            async with semaphore:
                try:
                    result = await test_proxy_via_singbox(uri, timeout=test_timeout)
                    return {"uri": uri, **result}
                except Exception as e:
                    return {"uri": uri, "alive": False, "error": str(e)}

        tasks = [test_one(uri) for uri in proxies.values()]
        results = await asyncio.gather(*tasks)

        working = {res["uri"]: res for res in results if res.get("alive")}

        # Only keep working proxies
        return {k: v for k, v in proxies.items() if v in working}

    async def _fetch_recursive(self, url: str) -> dict[str, str]:
        """Fetch ``url`` and merge every proxy found, following manifests."""
        content = await self.fetch(url)
        proxies = self.parse(content)

        # Find nested subscription URLs (manifest lines the parser skipped).
        manifest_urls = _extract_manifest_urls(content)
        if not manifest_urls:
            return proxies

        nested = await asyncio.gather(*[self._fetch_recursive(u) for u in manifest_urls])
        for chunk in nested:
            for uri in chunk.values():
                # Keep the first occurrence of a URI so a duplicate across
                # manifests keeps its original ID.
                if uri not in proxies.values():
                    proxies[f"P{hashlib.md5(uri.encode()).hexdigest()[:8]}"] = uri
        return proxies


def _is_manifest_url(line: str) -> bool:
    """True when ``line`` is an http(s):// URL that is not itself a proxy URI.

    ``https://`` is a legitimate proxy scheme, so a bare http(s) URL is only
    treated as a nested subscription (a Hiddify profile line pointing at another
    subscription) when it carries a path - real proxy endpoints are written as
    ``https://user:pass@host:port`` with no path.
    """
    if not (line.startswith("http://") or line.startswith("https://")):
        return False
    parsed = urlparse(line)
    return bool(parsed.path and parsed.path != "/")


def _extract_manifest_urls(content: str) -> list[str]:
    """Return every bare http(s):// URL in ``content`` that is not a proxy URI."""
    urls: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if _is_manifest_url(line):
            urls.append(line)
    return urls


async def refresh_from_subscription(
    url: str,
    *,
    test_proxies: bool = True,
    timeout: float = 10.0,
    concurrency: int = 5,
) -> dict[str, str]:
    """Convenience function to refresh from a subscription URL."""
    source = SubscriptionSource()
    return await source.refresh(
        url,
        test_proxies=test_proxies,
        test_timeout=timeout,
        concurrency=concurrency,
    )
