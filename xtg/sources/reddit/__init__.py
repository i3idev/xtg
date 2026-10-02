"""Reddit connector (PRAW).

The PRAW import stays inside :mod:`xtg.sources.reddit.client`, so importing this
package (or ``xtg --help``) works without the dependency installed.
"""

from __future__ import annotations

from xtg.sources.reddit.client import create_reddit, describe_client, praw_available
from xtg.sources.reddit.collector import RedditCollector, SubredditTarget
from xtg.sources.reddit.source import RedditSource

__all__ = [
    "RedditCollector",
    "RedditSource",
    "SubredditTarget",
    "create_reddit",
    "describe_client",
    "praw_available",
]
