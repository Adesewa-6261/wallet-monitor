"""
lib/http.py

Every outbound call to a blockchain or price provider goes through here.

Why: providers fail in ordinary, recoverable ways — a momentary 503, a brief
rate limit, a request that hangs. Without a timeout, one slow provider holds a
serverless function open until the platform kills it and the user sees nothing.
Without retries, a single blip fails a wallet that would have worked on the
second attempt.
"""

import asyncio
import random
from typing import Any, Awaitable, Callable, Iterable, Optional, TypeVar

import httpx

from .errors import RequestError

T = TypeVar("T")
R = TypeVar("R")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


async def fetch_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    json_body: Any = None,
    content: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 8.0,
    retries: int = 2,
    label: str = "upstream",
) -> Any:
    """
    Make an HTTP request and return parsed JSON.

    Raises RequestError on failure. Never includes the provider's response body
    in the error — provider errors often echo the request URL back, and our URL
    contains the API key.
    """
    headers = headers or {}
    last_status: Optional[int] = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries + 1):
            # Exponential backoff with jitter. Jitter matters because we fan out
            # to the same provider for many addresses at once — without it every
            # retry fires simultaneously and we rate-limit ourselves.
            if attempt > 0:
                backoff = min(1.0 * (2 ** (attempt - 1)), 4.0)
                await asyncio.sleep(backoff + random.random() * 0.25)

            try:
                response = await client.request(
                    method,
                    url,
                    headers={"Accept": "application/json", **headers},
                    json=json_body if content is None else None,
                    content=content,
                    params=params,
                )

                if response.status_code < 400:
                    return response.json()

                if response.status_code in RETRYABLE_STATUS and attempt < retries:
                    last_status = response.status_code
                    continue

                raise RequestError(
                    "RATE_LIMITED" if response.status_code == 429 else "UPSTREAM_UNAVAILABLE",
                    f"{label} request failed ({response.status_code})",
                )

            except RequestError:
                raise

            except httpx.TimeoutException as err:
                if attempt >= retries:
                    raise RequestError(
                        "UPSTREAM_UNAVAILABLE",
                        f"{label} timed out after {timeout}s",
                    ) from err

            except httpx.HTTPError as err:
                if attempt >= retries:
                    raise RequestError(
                        "UPSTREAM_UNAVAILABLE",
                        f"{label} is unreachable",
                    ) from err

    # Only reachable if the loop runs zero times. Still names the last status
    # rather than a bare "failed" — "responded 503 three times" is diagnosable,
    # "coingecko failed" is not. The provider's own text is never included: it
    # tends to echo the request URL back, and our URL carries the API key.
    raise RequestError(
        "RATE_LIMITED" if last_status == 429 else "UPSTREAM_UNAVAILABLE",
        f"{label} request failed ({last_status})" if last_status else f"{label} failed",
    )


async def map_limit(
    items: list[T],
    limit: int,
    fn: Callable[[T], Awaitable[R]],
) -> list[R]:
    """
    Run an async function over items with a concurrency cap.

    Needed because an xpub scan can mean hundreds of address lookups. Firing
    them all at once gets us rate-limited instantly; running them one at a time
    is far too slow. This keeps a fixed number in flight.
    """
    semaphore = asyncio.Semaphore(limit)

    async def guarded(item: T) -> R:
        async with semaphore:
            return await fn(item)

    return await asyncio.gather(*(guarded(item) for item in items))


async def settle_all(coros: Iterable[Awaitable[T]]) -> list[dict]:
    """
    Like asyncio.gather(return_exceptions=True) but returns a shape that is
    pleasant to branch on.

    Used for per-wallet fan-out, where one failure must not sink the batch.

    Returns a list of {"ok": True, "value": ...} or {"ok": False, "error": ...}
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    return [
        {"ok": False, "error": r} if isinstance(r, BaseException) else {"ok": True, "value": r}
        for r in results
    ]
