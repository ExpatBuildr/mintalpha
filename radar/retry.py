"""Retries with exponential backoff + jitter, honoring server-provided retry-after."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

import httpx

T = TypeVar("T")
log = logging.getLogger("radar.retry")


class RetryableError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class FatalError(Exception):
    """Do not retry (bad credentials, no access, invalid request)."""


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 120.0,
    op: str = "operation",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except FatalError:
            raise
        except (RetryableError, httpx.TransportError, httpx.TimeoutException) as exc:
            last = exc
            if attempt == attempts:
                break
            retry_after = getattr(exc, "retry_after", None)
            delay = retry_after if retry_after is not None else min(
                max_delay, base_delay * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
            delay = min(delay, max_delay)
            log.warning("retrying", extra={"op": op, "attempt": attempt, "delay_s": round(delay, 2),
                                           "error": str(exc)})
            await sleep(delay)
    assert last is not None
    raise last
