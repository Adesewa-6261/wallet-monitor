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

import os
from decimal import Decimal
from typing import Optional

from ..core.errors import RequestError
from ..core.http import fetch_json
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


# ------------------------------------------------------------------- history
#
# Tron's node RPC has no address-history method: that is an index, and indexes
# live in TronGrid's HTTP API rather than in the node. Bitnob proxies the node,
# so history comes from TronGrid directly.
#
# This mirrors the Bitcoin arrangement exactly — a chain whose balances come
# from one place and whose history comes from another, isolated behind this
# adapter so nothing above it needs to know.

TRONGRID_BASE = "https://api.trongrid.io"

# TronGrid serves unauthenticated requests at a lower rate limit, so the key is
# optional. Read lazily rather than at import so a deployment without one still
# starts.
def _trongrid_headers() -> dict[str, str]:
    key = os.environ.get("TRONGRID_API_KEY", "").strip()
    return {"TRON-PRO-API-KEY": key} if key else {}


# TronGrid pages by time, not by height, so the sync position for Tron is a
# millisecond timestamp. It still means "we have processed up to here".
TRONGRID_PAGE = 50
MAX_TRON_PAGES = 6


async def _trongrid(path: str, params: dict) -> dict:
    return await fetch_json(
        f"{TRONGRID_BASE}{path}",
        params=params,
        headers=_trongrid_headers(),
        label="trongrid",
        timeout=20.0,
    ) or {}


async def get_trc20_transfers(
    address: str,
    since_timestamp: Optional[int] = None,
) -> list[dict]:
    """
    TRC-20 transfers touching this address — USDT being the one that matters.

    Returns the normalised shape the poller expects, with block_time in
    milliseconds so the caller can use it as a sync position directly.
    """
    params: dict = {"limit": TRONGRID_PAGE, "only_confirmed": "true"}
    if since_timestamp:
        # TronGrid's bound is inclusive, so step past the last one we recorded
        # rather than re-reading it every cycle.
        params["min_timestamp"] = since_timestamp + 1

    transfers: list[dict] = []
    fingerprint: Optional[str] = None

    for _ in range(MAX_TRON_PAGES):
        if fingerprint:
            params["fingerprint"] = fingerprint

        payload = await _trongrid(f"/v1/accounts/{address}/transactions/trc20", params)
        rows = payload.get("data") or []
        if not rows:
            break

        for row in rows:
            token = row.get("token_info") or {}
            symbol = (token.get("symbol") or "").upper()
            decimals = int(token.get("decimals") or 0)

            # Only the tokens we track, so an airdropped scam token cannot
            # generate an alert.
            if symbol not in TOKENS:
                continue

            sender = row.get("from")
            recipient = row.get("to")
            if recipient == address:
                direction, counterparty = "receive", sender
            elif sender == address:
                direction, counterparty = "send", recipient
            else:
                continue

            transfers.append({
                "tx_hash": row.get("transaction_id"),
                "chain": "tron",
                "direction": direction,
                "symbol": symbol,
                "amount": _to_amount(row.get("value") or 0, decimals),
                "counterparty": counterparty,
                "block_timestamp": int(row.get("block_timestamp") or 0),
            })

        fingerprint = (payload.get("meta") or {}).get("fingerprint")
        if not fingerprint or len(rows) < TRONGRID_PAGE:
            break

    return transfers


async def get_native_transfers(
    address: str,
    since_timestamp: Optional[int] = None,
) -> list[dict]:
    """
    Native TRX transfers. Separate endpoint from TRC-20, and a different shape —
    the amount sits inside the contract parameter rather than at the top level.
    """
    params: dict = {"limit": TRONGRID_PAGE, "only_confirmed": "true"}
    if since_timestamp:
        params["min_timestamp"] = since_timestamp + 1

    payload = await _trongrid(f"/v1/accounts/{address}/transactions", params)
    transfers: list[dict] = []

    for row in payload.get("data") or []:
        contracts = (row.get("raw_data") or {}).get("contract") or []
        if not contracts:
            continue

        contract = contracts[0]
        if contract.get("type") != "TransferContract":
            continue

        value = (contract.get("parameter") or {}).get("value") or {}
        amount = int(value.get("amount") or 0)
        if amount == 0:
            continue

        sender = value.get("owner_address")
        recipient = value.get("to_address")

        # Addresses come back hex-encoded here even when visible=true elsewhere,
        # so compare on whichever form matches.
        if recipient in (address,):
            direction, counterparty = "receive", sender
        elif sender in (address,):
            direction, counterparty = "send", recipient
        else:
            continue

        # A failed contract still appears in the list but moved nothing.
        receipt = (row.get("ret") or [{}])[0]
        if receipt.get("contractRet") not in (None, "SUCCESS"):
            continue

        transfers.append({
            "tx_hash": row.get("txID"),
            "chain": "tron",
            "direction": direction,
            "symbol": "TRX",
            "amount": _to_amount(amount, 6),
            "counterparty": counterparty,
            "block_timestamp": int(row.get("block_timestamp") or 0),
            "fee": _to_amount((row.get("ret") or [{}])[0].get("fee") or 0, 6),
        })

    return transfers


async def get_transfers(address: str, since_timestamp: Optional[int] = None) -> list[dict]:
    """Everything touching this address since the given millisecond timestamp."""
    trc20 = await get_trc20_transfers(address, since_timestamp)
    native = await get_native_transfers(address, since_timestamp)
    return trc20 + native
