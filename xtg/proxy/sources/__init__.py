"""Proxy sources for the global proxy registry."""

from __future__ import annotations

from xtg.proxy.sources.manager import SourcesManager
from xtg.proxy.sources.subscription import SubscriptionSource, refresh_from_subscription

__all__ = ["SourcesManager", "SubscriptionSource", "refresh_from_subscription"]
