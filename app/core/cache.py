"""
lib/cache.py

A tiny in-memory cache with TTL.

Why this matters more than it looks: CoinGecko's demo tier allows 10,000 calls
a month. Without caching, a user pulling to refresh a few times a minute would
burn the monthly budget in days.

Caveat worth understanding: on Vercel each serverless instance has its own
memory, and instances come and go. So this is a best-effort cache, not a shared
one — a cold instance always misses. That is fine for our purpose (smoothing
repeated calls within a warm instance). If we later need a real shared cache,
Vercel KV or Upstash Redis drops in behind this same interface.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")

_store: dict[str, tuple[Any, float]] = {}
_lock = asyncio.Lock()


def cache_get(key: str) -> Optional[Any]:
    hit = _store.get(key)
    if hit is None:
        return None

    value, expires_at = hit
    if time.time() > expires_at:
        _store.pop(key, None)
        return None

    return value


def cache_set(key: str, value: T, ttl_seconds: int) -> T:
    _store[key] = (value, time.time() + ttl_seconds)

    # Cheap bound so a long-lived instance cannot grow without limit.
    if len(_store) > 500:
        now = time.time()
        expired = [k for k, (_, exp) in _store.items() if now > exp]
        for k in expired:
            _store.pop(k, None)

    return value


async def cached(
    key: str,
    ttl_seconds: int,
    loader: Callable[[], Awaitable[T]],
) -> T:
    """
    Get from cache, or run the loader and cache its result.

    Deliberately does not cache failures — a transient provider error should not
    be remembered for 60 seconds.
    """
    hit = cache_get(key)
    if hit is not None:
        return hit

    # The lock prevents a thundering herd: without it, ten simultaneous requests
    # on a cold cache would all call the provider.
    async with _lock:
        hit = cache_get(key)
        if hit is not None:
            return hit

        value = await loader()
        return cache_set(key, value, ttl_seconds)


def cache_clear() -> None:
    _store.clear()
