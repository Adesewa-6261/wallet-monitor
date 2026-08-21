"""
app/services/balances.py

What each wallet currently holds, and what it is worth.

The dashboard's headline figure comes from here, so the failure modes matter
more than the happy path:

  - A wallet whose provider is down returns an error on that wallet and does not
    fail the request. Four wallets should not go blank because one node is
    unreachable.

  - A holding we cannot price is reported with a null value rather than zero,
    and the wallet is left out of the total. Those are different statements
    about someone's money.

  - Results are cached per wallet. A Bitcoin xpub scan is dozens of HTTP
    requests before the gap limit is satisfied; without a cache, every
    pull-to-refresh re-runs the whole scan.
"""

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from ..adapters import bitcoin, evm, tron
from ..core.amounts import format_amount
from ..core import networks
from ..core.cache import cache_get, cache_set
from ..core.errors import RequestError
from ..core.http import map_limit
from ..schemas.wallets import WalletBalance, WalletHolding
from . import prices

logger = logging.getLogger("walletnest.balances")

CACHE_SECONDS = 60

# Wallets are read concurrently, but not without limit: each one is itself a
# fan-out of provider calls, and multiplying the two gets us rate limited.
FETCH_CONCURRENCY = 4


def _cache_key(wallet_id: str) -> str:
    return f"balance:{wallet_id}"


# An EVM address is the same string on every EVM chain, so one pasted address
# is a wallet on all of them at once. These are the ones we read. Adding a chain
# here is most of what supporting it takes.
EVM_CHAINS = ("ethereum", "base")


async def _amounts(wallet: dict) -> tuple[list[tuple[str, str, str]], list[str]]:
    """
    Raw holdings as (symbol, chain, amount), plus any chains that failed.

    The chain travels with each holding rather than being taken from the wallet,
    because for an EVM address the two genuinely differ: the wallet is recorded
    as Ethereum, and it can still hold USDC on Base.
    """
    chain = wallet["chain"]

    if chain == "bitcoin":
        result = await bitcoin.get_balance(
            wallet["input"], wallet["input_type"], wallet.get("address_type")
        )
        return [("BTC", "bitcoin", result["btc"])], []

    if chain in EVM_CHAINS:
        async def one(target: str):
            return target, await evm.get_balances(target, wallet["input"])

        rows: list[tuple[str, str, str]] = []
        failed: list[str] = []

        # Read the chains concurrently, and let them fail independently. A Base
        # outage should not hide the Ethereum balance we successfully read.
        for outcome in await asyncio.gather(
            *(one(c) for c in EVM_CHAINS), return_exceptions=True
        ):
            if isinstance(outcome, BaseException):
                continue
            target, amounts = outcome
            rows.extend((symbol, target, amount) for symbol, amount in amounts.items())

        done = {c for _, c, _ in rows}
        failed = [c for c in EVM_CHAINS if c not in done]

        # Every chain failed: that is a failed wallet, not a partial one.
        if not rows and failed:
            raise RequestError(
                "UPSTREAM_UNAVAILABLE",
                f"Could not reach {' or '.join(failed)}.",
            )

        return rows, failed

    if chain == "tron":
        amounts = await tron.get_balances(wallet["input"])
        return [(s, "tron", a) for s, a in amounts.items()], []

    raise RequestError("UNSUPPORTED_INPUT", f"Cannot read balances on {chain}.")


async def _price(
    amounts: list[tuple[str, str, str]],
) -> tuple[list[WalletHolding], Optional[float]]:
    """
    Attach USD values. Returns the holdings and the wallet total, or None for
    the total when any holding could not be priced.
    """
    holdings: list[WalletHolding] = []
    total = 0.0
    complete = True

    for symbol, chain, amount in amounts:
        # Zero balances are dropped rather than listed. Every EVM wallet returns
        # a row for each token we track, so keeping them would fill the screen
        # with nothing, and an unpriced zero would null a total we know is exact.
        if Decimal(amount) == 0:
            continue

        value = await prices.value_usd(symbol, amount)

        # A missing price is not zero. Leaving the field null says "we do not
        # know", which is the truth; a zero would say the holding is worthless.
        if value is None:
            complete = False
        else:
            total += value

        holdings.append(
            WalletHolding(
                symbol=symbol,
                amount=format_amount(amount),
                value_usd=round(value, 2) if value is not None else None,
                chain=chain,
                network=networks.network_label(chain),
                label=networks.asset_label(symbol, chain),
            )
        )

    return holdings, round(total, 2) if complete else None


async def wallet_balance(wallet: dict) -> WalletBalance:
    """
    Holdings for one wallet. Never raises — a failure is reported on the wallet
    itself, so one bad provider cannot sink a whole portfolio request.
    """
    key = _cache_key(str(wallet["id"]))

    # cache_get/cache_set rather than cached(): that helper holds a single
    # process-wide lock across the loader, which would serialise the concurrent
    # fan-out below into one wallet at a time.
    hit = cache_get(key)
    if hit is not None:
        return hit

    summary = {
        "wallet_id": str(wallet["id"]),
        "label": wallet["label"],
        "chain": wallet["chain"],
    }

    try:
        rows, failed = await _amounts(wallet)
        holdings, value = await _price(rows)
    except RequestError as err:
        # Our own message, so it is safe to show. Unexpected errors are not:
        # a provider's error text can echo back the request URL, API key included.
        return WalletBalance(**summary, error=err.message)
    except Exception:
        logger.exception("balance lookup failed for wallet %s", wallet["id"])
        return WalletBalance(**summary, error="Could not read this wallet's balance.")

    if failed:
        # Some of the wallet's chains answered and some did not. Show what came
        # back, say so, and withhold the total: a sum missing a chain is not the
        # wallet's value, and presenting it as one understates what is there.
        return WalletBalance(
            **summary,
            holdings=holdings,
            value_usd=None,
            error=f"Could not reach {' or '.join(failed)}. Totals are incomplete.",
        )

    # Only successes are cached. A transient provider failure should not be
    # remembered for a minute.
    return cache_set(
        key,
        WalletBalance(**summary, holdings=holdings, value_usd=value),
        CACHE_SECONDS,
    )


async def wallet_balances(wallets: list[dict]) -> tuple[list[WalletBalance], float]:
    """Every wallet, read concurrently, plus the total of the ones we could price."""
    results = await map_limit(wallets, FETCH_CONCURRENCY, wallet_balance)
    total = sum(w.value_usd for w in results if w.value_usd is not None)
    return results, round(total, 2)
