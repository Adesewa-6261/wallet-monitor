"""
app/adapters/bitcoin.py

Bitcoin balances and transaction history.

Uses Esplora (mempool.space, with Blockstream as a fallback) rather than
Bitnob's RPC gateway. That is not a preference: bitcoind has no address index,
and Bitnob's gateway exposes neither Electrum methods nor scantxoutset, so there
is no way to ask it what an address holds. See BITNOB_FINDINGS.md item 5.

Everything else about this file is chain logic that would be identical whatever
the data source is, so swapping back later means changing the two fetch
functions and nothing else.

A wallet is not one address. An extended public key is the root of a tree, and
wallet software hands out addresses one at a time as payments arrive. Finding
the balance means walking that tree:

    .../0/i   receive addresses — the ones you give people
    .../1/i   change addresses  — where your own change comes back

Both hold real money. Skipping the change branch silently under-reports.
"""

import re
from decimal import Decimal
from typing import Optional

from ..core.config import config
from ..core.errors import RequestError
from ..core.http import fetch_json, map_limit
from .bip32 import derive_addresses as _derive

SATS_PER_BTC = Decimal("100000000")

# Free public APIs with no published limits. They throttle by dropping TCP
# connections rather than returning 429, so the only safe approach is modest
# concurrency plus a second host to fall back to. Both run Esplora, so the
# response shapes are identical.
ESPLORA_HOSTS = [
    "https://mempool.space/api",
    "https://blockstream.info/api",
]

SCAN_CONCURRENCY = 2
SCAN_BLOCK = 20
PROBE_DEPTH = 5


def derive_addresses(
    xpub: str,
    address_type: str,
    change: int,
    start: int,
    count: int,
) -> list[str]:
    """
    Derive addresses on one branch. `change` is 0 for receive, 1 for change.

    Any SLIP-132 prefix works — xpub, ypub and zpub carry identical key material
    and differ only in four version bytes, so the address type is tracked
    separately rather than inferred from the prefix.
    """
    return _derive(xpub, address_type, change, start, count)


# ------------------------------------------------------------------- fetching


async def _esplora(path: str) -> object:
    """Try each host in turn. One being throttled should not fail the wallet."""
    last_error: Optional[Exception] = None

    for host in ESPLORA_HOSTS:
        try:
            return await fetch_json(
                f"{host}{path}",
                label=host.split("//")[1].split("/")[0],
                timeout=20.0,
                retries=1,
            )
        except RequestError as err:
            last_error = err

    raise last_error or RequestError(
        "UPSTREAM_UNAVAILABLE", "No Bitcoin data provider is reachable."
    )


async def fetch_address_balance(address: str) -> dict:
    """
    Balance and usage for one address.

    "Ever used" matters more than "has a balance" for gap scanning: an address
    that received and later spent everything still proves the wallet reached
    this far down the tree, so it must reset the gap counter.
    """
    data = await _esplora(f"/address/{address}")

    chain = data.get("chain_stats", {})
    pending = data.get("mempool_stats", {})

    confirmed = int(chain.get("funded_txo_sum", 0)) - int(chain.get("spent_txo_sum", 0))
    unconfirmed = int(pending.get("funded_txo_sum", 0)) - int(pending.get("spent_txo_sum", 0))
    tx_count = int(chain.get("tx_count", 0)) + int(pending.get("tx_count", 0))

    return {
        "address": address,
        "sats": confirmed + unconfirmed,
        "used": tx_count > 0,
    }


async def fetch_address_transactions(address: str, limit: int = 25) -> list[dict]:
    """Recent transactions touching one address, newest first."""
    return await _esplora(f"/address/{address}/txs") or []


# ------------------------------------------------------------ type detection


async def detect_address_type_with_probes(
    xpub: str,
) -> tuple[Optional[str], dict[str, dict]]:
    """
    Work out which address type a key uses by asking the blockchain rather than
    trusting the prefix.

    A plain xpub predates the SLIP-132 convention entirely, so the same key could
    be Legacy or Nested SegWit depending on the wallet that produced it. Even a
    zpub can be wrong if the wallet is unusual. Guessing wrong shows a zero
    balance, which to a user is indistinguishable from losing their money.

    Returns the type plus every lookup made, so the scan can reuse them instead
    of fetching the first few addresses twice.

    None means no type showed any history — the wallet is empty, so the choice
    does not matter.
    """
    candidates = ["Native SegWit", "Nested SegWit", "Legacy", "Taproot"]

    probes = [
        (address_type, address)
        for address_type in candidates
        for address in derive_addresses(xpub, address_type, 0, 0, PROBE_DEPTH)
    ]

    async def check(pair: tuple[str, str]) -> tuple[str, dict]:
        address_type, address = pair
        return address_type, await fetch_address_balance(address)

    results = await map_limit(probes, SCAN_CONCURRENCY, check)
    probe_cache = {result["address"]: result for _, result in results}

    funded: dict[str, int] = {}
    used: dict[str, int] = {}

    for address_type, result in results:
        if result["sats"] > 0:
            funded[address_type] = funded.get(address_type, 0) + result["sats"]
        if result["used"]:
            used[address_type] = used.get(address_type, 0) + 1

    if funded:
        return max(funded, key=funded.get), probe_cache
    if used:
        return max(used, key=used.get), probe_cache
    return None, probe_cache


async def detect_address_type(xpub: str) -> Optional[str]:
    address_type, _ = await detect_address_type_with_probes(xpub)
    return address_type


# -------------------------------------------------------------------- scanning


async def scan_xpub(
    xpub: str,
    address_type: str,
    probe_cache: Optional[dict[str, dict]] = None,
) -> dict:
    """
    Walk both branches until the gap limit is hit on each.

    The gap limit sets a floor on request count: a gap of N across two branches
    means at least 2N lookups even for an empty wallet. 20 is the BIP44 standard
    and is what wallet software itself enforces when generating addresses, so
    funds beyond it normally cannot exist. We extend past it when a block shows
    activity, which covers the unusual case without slowing the common one.
    """
    gap_limit = config.bitcoin_gap_limit
    probe_cache = probe_cache or {}

    total_sats = 0
    funded: list[dict] = []
    watched: list[str] = []
    scanned = 0

    for change in (0, 1):
        consecutive_empty = 0
        index = 0

        while consecutive_empty < gap_limit:
            block = derive_addresses(xpub, address_type, change, index, SCAN_BLOCK)

            unknown = [a for a in block if a not in probe_cache]
            fetched = await map_limit(unknown, SCAN_CONCURRENCY, fetch_address_balance)

            by_address = {r["address"]: r for r in fetched}
            by_address.update({a: probe_cache[a] for a in block if a in probe_cache})

            # Derivation order matters — gap counting depends on it.
            results = [by_address[a] for a in block]
            block_had_activity = False

            for result in results:
                if result["address"] not in probe_cache:
                    scanned += 1

                if result["used"]:
                    consecutive_empty = 0
                    block_had_activity = True
                    watched.append(result["address"])
                    if result["sats"] > 0:
                        total_sats += result["sats"]
                        funded.append({
                            "address": result["address"],
                            "amount": str(Decimal(result["sats"]) / SATS_PER_BTC),
                        })
                else:
                    consecutive_empty += 1

            if block_had_activity:
                consecutive_empty = 0

            index += SCAN_BLOCK

    return {
        "sats": total_sats,
        "btc": str(Decimal(total_sats) / SATS_PER_BTC),
        "addresses_scanned": scanned,
        "addresses_funded": len(funded),
        "funded_addresses": funded,
        # Addresses with history, stored so the poller does not re-derive the
        # whole tree every cycle.
        "watched_addresses": watched,
    }


async def scan_single_address(address: str) -> dict:
    result = await fetch_address_balance(address)
    amount = str(Decimal(result["sats"]) / SATS_PER_BTC)

    return {
        "sats": result["sats"],
        "btc": amount,
        "addresses_scanned": 1,
        "addresses_funded": 1 if result["sats"] > 0 else 0,
        "funded_addresses": [{"address": address, "amount": amount}] if result["sats"] else [],
        "watched_addresses": [address],
    }


def extract_key_from_descriptor(descriptor: str) -> str:
    """Pull the extended key out of  wpkh([d34db33f/84h/0h/0h]zpub6r.../0/*)"""
    match = re.search(r"([xyztuv]pub[1-9A-HJ-NP-Za-km-z]{50,})", descriptor)
    if not match:
        raise RequestError(
            "UNSUPPORTED_INPUT", "We could not find a public key inside that descriptor."
        )
    return match.group(1)


async def get_balance(
    value: str,
    input_type: str,
    address_type: Optional[str] = None,
) -> dict:
    """Entry point. Routes to the right strategy based on what validation found."""
    if input_type == "address":
        return await scan_single_address(value)

    if input_type not in ("xpub", "descriptor"):
        raise RequestError("UNSUPPORTED_INPUT", f"Cannot scan input type: {input_type}")

    key = extract_key_from_descriptor(value) if input_type == "descriptor" else value
    probe_cache: dict[str, dict] = {}

    if not address_type:
        address_type, probe_cache = await detect_address_type_with_probes(key)

        if address_type is None:
            # No history under any type. The wallet is empty, so which type we
            # would have scanned does not change the answer.
            return {
                "sats": 0,
                "btc": "0",
                "addresses_scanned": len(probe_cache),
                "addresses_funded": 0,
                "funded_addresses": [],
                "watched_addresses": [],
                "detected_address_type": None,
            }

    result = await scan_xpub(key, address_type, probe_cache)
    result["detected_address_type"] = address_type
    return result
