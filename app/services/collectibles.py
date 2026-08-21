"""
app/services/collectibles.py

NFTs held by a wallet.

Kept apart from balances rather than folded into them. The two answer different
questions and cost different amounts: balances feed the headline figure on every
screen refresh, while collectibles are opened deliberately and are slower to
fetch. Sharing an endpoint would make the fast path wait for the slow one.
"""

import logging

from ..adapters import alchemy
from ..core.cache import cache_get, cache_set
from ..core.http import map_limit
from ..schemas.wallets import WalletCollectibles

logger = logging.getLogger("walletnest.collectibles")

# Longer than the sixty seconds balances use. An NFT collection changes when
# something is bought or sold, not continuously, and each lookup is expensive
# enough that re-running it on every glance is waste rather than freshness.
CACHE_SECONDS = 300

FETCH_CONCURRENCY = 4

# The same address is a wallet on every EVM chain, so both are read for one
# wallet, exactly as balances does.
EVM_CHAINS = ("ethereum", "base")


def _cache_key(wallet_id: str) -> str:
    return f"collectibles:{wallet_id}"


async def wallet_collectibles(wallet: dict) -> WalletCollectibles:
    """
    NFTs for one wallet. Never raises — a failure is reported on the wallet.
    """
    summary = {
        "wallet_id": str(wallet["id"]),
        "label": wallet["label"],
        "chain": wallet["chain"],
    }

    # Bitcoin and Tron are not asked. Saying "this chain does not carry NFTs" is
    # a different statement from "this wallet holds none", and an app that
    # cannot tell them apart shows an empty gallery to someone whose wallet
    # could never have had one.
    if wallet["chain"] not in EVM_CHAINS:
        return WalletCollectibles(**summary, supported=False, collectibles=[])

    if not alchemy.configured():
        return WalletCollectibles(
            **summary,
            supported=True,
            collectibles=None,
            error="NFT lookups are not configured on this deployment.",
        )

    key = _cache_key(str(wallet["id"]))
    hit = cache_get(key)
    if hit is not None:
        return hit

    items: list[dict] = []
    failed: list[str] = []

    # The chains are read one after another rather than together. Firing both at
    # once means up to four paged requests landing simultaneously, which the free
    # Alchemy tier rate-limits — observed failing on a wallet holding hundreds of
    # NFTs. This screen is opened deliberately and cached for five minutes, so
    # the extra second costs far less than an empty gallery does.
    #
    # Failures are tracked per chain as they happen. Inferring them from which
    # chains are missing in the results cannot work: a chain that genuinely holds
    # no NFTs also returns nothing, and treating that as a failure would put an
    # error on every wallet that simply owns no collectibles.
    for chain in EVM_CHAINS:
        try:
            items.extend(await alchemy.get_nfts(chain, wallet["input"]))
        except Exception as exc:
            logger.warning("collectibles failed for %s on %s: %s",
                           wallet["id"], chain, exc)
            failed.append(chain)

    if failed and not items:
        # Nothing came back and something broke: report it rather than showing
        # an empty gallery that looks like a definite answer.
        return WalletCollectibles(
            **summary,
            supported=True,
            collectibles=None,
            error=f"Could not reach {' or '.join(failed)}.",
        )

    result = WalletCollectibles(**summary, supported=True, collectibles=items)

    if failed:
        # Partial: show what we have and say so, the same way balances does.
        result.error = f"Could not reach {' or '.join(failed)}. This list may be incomplete."
        return result

    # Only complete answers are cached.
    return cache_set(key, result, CACHE_SECONDS)


async def all_collectibles(wallets: list[dict]) -> tuple[list[WalletCollectibles], int]:
    """Every wallet, read concurrently, plus how many NFTs were found in total."""
    results = await map_limit(wallets, FETCH_CONCURRENCY, wallet_collectibles)
    total = sum(len(w.collectibles or []) for w in results)
    return results, total
