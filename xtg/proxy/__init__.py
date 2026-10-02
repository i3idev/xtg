"""Proxy subsystem - completely independent from the Telegram subsystem."""

from xtg.proxy.loader import InvalidProxy, ProxyLoadResult, load_proxy_list, write_proxy_list
from xtg.proxy.manager import ProxyManager, ProxyStats
from xtg.proxy.parser import ProxyEntry, ProxyType, parse_proxy_url

__all__ = [
    "InvalidProxy",
    "ProxyEntry",
    "ProxyLoadResult",
    "ProxyManager",
    "ProxyStats",
    "ProxyType",
    "load_proxy_list",
    "parse_proxy_url",
    "write_proxy_list",
]
