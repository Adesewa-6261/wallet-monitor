"""
app/adapters/tron.py

Tron balances and transfers, through Bitnob's RPC gateway.

Tron is not an EVM chain despite looking like one. Bitnob's docs list eth_* methods
under Tron, but calling them returns:

    503 quicknode: invalid tron method name: "eth_blockNumber"

The chain works fine with its own method names — getnowblock, getaccount and the
rest. See BITNOB_FINDINGS.md item 6.

Addresses also differ: Tron uses base58 strings starting with T, and most methods
need "visible": true to accept them in that form rather than as hex.
"""

from decimal import Decimal
from typing import Optional

from ..core.errors import RequestError
from . import bitnob

SUN_PER_TRX = Decimal("1000000")

# USDT on Tron. The most-used stablecoin contract on any chain.
TOKENS = {
    "USDT": {
        "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        "decimals": 6,
    },
}


def _to_amount(raw: int | str, decimals: int) -> str:
    """Kept as a string — a token balance in base units overflows a float."""
    return str(Decimal(int(raw)) / (Decimal(10) ** decimals))


async def get_balances(address: str) -> dict[str, str]:
    """
    Native TRX plus the tokens we track.

    getaccount returns both: `balance` is TRX in sun, and `trc20` is a list of
    {contract: amount} maps for token holdings.
    """
    account = await bitnob.rpc(
        "tron",
        "getaccount",
        {"address": address, "visible": True},
    )

    if not account:
        # An address with no history returns an empty object rather than an
        # error, which is not a failure — it just has nothing.
        return {"TRX": "0", **{symbol: "0" for symbol in TOKENS}}

    balances = {"TRX": _to_amount(account.get("balance", 0), 6)}

    holdings: dict[str, str] = {}
    for entry in account.get("trc20", []):
        holdings.update(entry)

    for symbol, token in TOKENS.items():
        raw = holdings.get(token["address"], 0)
        balances[symbol] = _to_amount(raw, token["decimals"])

    return balances


async def get_block_number() -> int:
    block = await bitnob.rpc("tron", "getnowblock", {})
    return int(block.get("block_header", {}).get("raw_data", {}).get("number", 0))


async def get_transfers(address: str, since_timestamp: Optional[int] = None) -> list[dict]:
    """
    Recent transfers touching this address.

    Tron's node RPC has no address-history method — that lives in TronGrid's
    HTTP API rather than the node itself. Bitnob proxies the node, so this
    returns nothing for now and Tron transaction history is unavailable through
    the gateway.

    Recorded as a gap rather than worked around: balances still update, so a
    Tron wallet shows the right total but no per-transaction detail.
    """
    return []
