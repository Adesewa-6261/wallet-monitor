"""
app/services/prices.py

USD prices for every asset we track.

Two sources, because neither covers everything: Bitnob quotes BTC, USDT, USDC
and NGN, but not ETH or TRX — which is awkward, since those are what fees are
paid in and appear in nearly every EVM wallet. CoinGecko fills the gap.

Cached for a minute. Without that, a handful of users refreshing would burn
CoinGecko's monthly allowance in days.
"""

from decimal import Decimal
from typing import Optional

from ..adapters import bitnob
from ..core.cache import cached
from ..core.config import config
from ..core.http import fetch_json

COINGECKO_IDS = {"ETH": "ethereum", "TRX": "tron"}

CACHE_KEY = "prices:usd"
CACHE_SECONDS = 60


async def _coingecko_prices() -> dict[str, float]:
    data = await fetch_json(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": ",".join(COINGECKO_IDS.values()),
            "vs_currencies": "usd",
        },
        headers={"x-cg-demo-api-key": config.coingecko_api_key},
        label="coingecko",
        timeout=10.0,
    )

    return {
        symbol: float(data[gecko_id]["usd"])
        for symbol, gecko_id in COINGECKO_IDS.items()
        if gecko_id in data and "usd" in data[gecko_id]
    }


async def _load() -> dict[str, float]:
    prices: dict[str, float] = {}

    # A failure on either side should not blank every price. Whatever we get is
    # better than nothing, and a missing price shows as an unvalued balance
    # rather than a broken screen.
    try:
        prices.update(await bitnob.get_prices())
    except Exception:
        pass

    try:
        prices.update(await _coingecko_prices())
    except Exception:
        pass

    return prices


async def get_prices() -> dict[str, float]:
    return await cached(CACHE_KEY, CACHE_SECONDS, _load)


async def value_usd(symbol: str, amount: str) -> Optional[float]:
    """
    Convert an amount to USD. None when we have no price, which the caller
    should render as a blank rather than a zero — those mean different things.
    """
    prices = await get_prices()
    price = prices.get(symbol.upper())
    if price is None:
        return None

    return float(Decimal(amount) * Decimal(str(price)))
