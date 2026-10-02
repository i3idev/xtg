"""Error classification and bounded retry with backoff.

Retry policies are only useful when they distinguish *what kind* of failure
happened. Retrying a malformed request or an authentication failure just burns
time and, on rate limited APIs, makes the situation worse.

The classification is done by exception *class name*, which keeps this module
free of Telethon/PRAW imports and therefore usable in tests without those
packages installed. :func:`classify` maps an exception to one of:

``TRANSIENT``
    connection reset, DNS blip, server hiccup - retry with backoff.
``TIMEOUT``
    the peer or the socket ran out of time - retry with backoff.
``RATE_LIMIT``
    the API asked us to slow down (Telegram ``FloodWait``, Reddit
    ``TooManyRequests``). Retry, but honour the wait the server asked for and
    let :mod:`xtg.pipeline` reduce concurrency while it lasts.
``AUTH``
    bad session, revoked token, missing 2FA - never retry.
``PERMANENT``
    the request itself is wrong (unknown chat, malformed id) - never retry.

Backoff is exponential with full jitter and a hard ceiling, so a fleet of
workers cannot synchronise into a thundering herd.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from xtg.core.logger import logger


class ErrorKind(StrEnum):
    """What kind of failure an exception represents."""

    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    PERMANENT = "permanent"


#: Class name fragments -> kind. Checked against the MRO, most specific first.
_NAME_RULES: tuple[tuple[tuple[str, ...], ErrorKind], ...] = (
    (
        ("FloodWait", "FloodPremiumWait", "TooManyRequests", "RateLimit", "SlowDown"),
        ErrorKind.RATE_LIMIT,
    ),
    (("TimedOut", "Timeout", "TimeoutError"), ErrorKind.TIMEOUT),
    (
        (
            "Unauthorized",
            "Forbidden",
            "AuthKeyError",
            "AuthKeyUnregistered",
            "SessionPasswordNeeded",
            "InvalidToken",
            "NotLoggedIn",
        ),
        ErrorKind.AUTH,
    ),
    (
        (
            "UsernameNotOccupied",
            "UsernameInvalid",
            "ChatIdInvalid",
            "ChannelInvalid",
            "NotFound",
            "Redirect",
            "ValueError",
            "TypeError",
            "KeyError",
            "BadRequest",
            "InvalidRequest",
        ),
        ErrorKind.PERMANENT,
    ),
    (
        (
            "ConnectionError",
            "ConnectionReset",
            "ConnectionRefused",
            "ConnectionAborted",
            "BrokenPipe",
            "ServerDisconnected",
            "OSError",
            "Timeout",
            "Proxy",
            "ReadError",
            "WriteError",
            "InternalServerError",
            "ServiceUnavailable",
            "HTTPError",
            "ChunkedEncodingError",
            "RemoteDisconnected",
            "IncompleteRead",
            "ResponseException",
            "SSLError",
        ),
        ErrorKind.TRANSIENT,
    ),
)

#: Kinds worth another attempt.
RETRYABLE: frozenset[ErrorKind] = frozenset(
    {ErrorKind.TRANSIENT, ErrorKind.TIMEOUT, ErrorKind.RATE_LIMIT}
)


def classify(exc: BaseException) -> ErrorKind:
    """Map an exception to an :class:`ErrorKind`."""
    if isinstance(exc, TimeoutError):
        return ErrorKind.TIMEOUT
    for base in type(exc).__mro__:
        name = base.__name__
        for fragments, kind in _NAME_RULES:
            if any(fragment in name for fragment in fragments):
                return kind
    return ErrorKind.TRANSIENT


def retry_after_of(exc: BaseException) -> float | None:
    """Seconds the server asked us to wait, when it said so.

    Telethon puts it on ``FloodWait.seconds`` / ``.expire``, PRAW on
    ``response.headers['retry-after']`` and prawcore on ``retry_after``.
    """
    for attribute in ("seconds", "expire", "retry_after", "retry_after_seconds"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        try:
            raw = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:  # noqa: BLE001 - exotic header containers
            raw = None
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter.

    ``delay = min(max_delay, base * multiplier ** attempt)``, then randomised
    inside ``[delay / 2, delay]`` so workers do not retry in lockstep.
    """

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: bool = True
    #: Upper bound for a server supplied ``retry-after``; keeps a hostile or
    #: broken value from parking a worker for an hour.
    max_server_delay: float = 300.0

    def delay_for(
        self, attempt: int, *, server_delay: float | None = None, random_source: Any = None
    ) -> float:
        """Seconds to wait before retry number ``attempt`` (0 based)."""
        if server_delay is not None:
            wait = min(max(server_delay, 0.0), self.max_server_delay)
            return wait
        raw = self.base_delay * (self.multiplier ** max(0, attempt))
        capped = min(raw, self.max_delay)
        if not self.jitter:
            return capped
        rng = random_source or random
        return capped / 2 + rng.random() * (capped / 2)

    def allows(self, attempt: int, kind: ErrorKind) -> bool:
        """True when ``attempt`` (number of failures so far) may be retried."""
        return kind in RETRYABLE and attempt < self.attempts


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    policy: RetryPolicy | None = None,
    *,
    on_retry: Callable[[ErrorKind, int, float], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    random_source: Any = None,
    label: str = "",
) -> Any:
    """Await ``operation``, retrying transient failures with backoff.

    Raises:
        Exception: the last exception, once the policy is exhausted or the error
            is not retryable.
    """
    policy = policy or RetryPolicy()
    nap = sleep or _default_async_sleep
    attempt = 0
    while True:
        try:
            return await operation()
        except BaseException as exc:
            kind = classify(exc)
            if not policy.allows(attempt, kind):
                raise
            delay = policy.delay_for(
                attempt, server_delay=retry_after_of(exc), random_source=random_source
            )
            attempt += 1
            if on_retry is not None:
                on_retry(kind, attempt, delay)
            logger.debug(
                "{}retrying after {} ({}/{}) in {:.2f}s",
                f"{label}: " if label else "",
                kind.value,
                attempt,
                policy.attempts,
                delay,
            )
            await nap(delay)


def retry_sync(
    operation: Callable[[], Any],
    policy: RetryPolicy | None = None,
    *,
    on_retry: Callable[[ErrorKind, int, float], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    random_source: Any = None,
    label: str = "",
) -> Any:
    """Blocking twin of :func:`retry_async` (used by the Reddit collector)."""
    policy = policy or RetryPolicy()
    import time

    nap = sleep or time.sleep
    attempt = 0
    while True:
        try:
            return operation()
        except BaseException as exc:
            kind = classify(exc)
            if not policy.allows(attempt, kind):
                raise
            delay = policy.delay_for(
                attempt, server_delay=retry_after_of(exc), random_source=random_source
            )
            attempt += 1
            if on_retry is not None:
                on_retry(kind, attempt, delay)
            logger.debug(
                "{}retrying after {} ({}/{}) in {:.2f}s",
                f"{label}: " if label else "",
                kind.value,
                attempt,
                policy.attempts,
                delay,
            )
            nap(delay)


async def _default_async_sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)
