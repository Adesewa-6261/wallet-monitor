"""
app/adapters/evm.py

Ethereum and Base, through Bitnob's RPC gateway.

Bitnob gives us raw JSON-RPC rather than a convenience layer, so token balances
and transfer history are built from primitives:

    eth_getBalance   native ETH
    eth_call         a token contract's balanceOf
    eth_getLogs      Transfer events, which is how we see money moving

eth_getLogs is the important one for monitoring. Every ERC-20 transfer emits a
Transfer event, and we can filter those by address, so "what went in and out of
this wallet" is one query per direction.
"""

from decimal import Decimal
from typing import Optional

from ..core.errors import RequestError
from . import bitnob

# keccak256("Transfer(address,address,uint256)") — the topic every ERC-20
# transfer logs under. Filtering on it is what separates transfers from every
# other kind of contract event.
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# balanceOf(address) function selector.
BALANCE_OF = "0x70a08231"

TOKENS = {
    "ethereum": {
        "USDT": {"address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "decimals": 6},
        "USDC": {"address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "decimals": 6},
    },
    "base": {
        "USDC": {"address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "decimals": 6},
    },
}

NATIVE = {"ethereum": "ETH", "base": "ETH"}


def _to_amount(raw_hex: str, decimals: int) -> str:
    """
    Convert an RPC hex integer into a decimal string.

    Kept as a string throughout: a token balance in base units routinely exceeds
    what a float represents exactly, and silently rounding someone's balance is
    not an acceptable failure.
    """
    if not raw_hex or raw_hex == "0x":
        return "0"
    value = int(raw_hex, 16)
    return str(Decimal(value) / (Decimal(10) ** decimals))


def _pad_address(address: str) -> str:
    """Addresses appear in log topics as 32-byte left-padded values."""
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


async def get_native_balance(chain: str, address: str) -> str:
    raw = await bitnob.rpc(chain, "eth_getBalance", [address, "latest"])
    return _to_amount(raw, 18)


async def get_token_balance(chain: str, address: str, symbol: str) -> str:
    token = TOKENS.get(chain, {}).get(symbol)
    if not token:
        raise RequestError("UNSUPPORTED_INPUT", f"{symbol} is not tracked on {chain}")

    # balanceOf takes one address argument, left-padded to 32 bytes.
    data = BALANCE_OF + address.lower().replace("0x", "").rjust(64, "0")

    raw = await bitnob.rpc(
        chain,
        "eth_call",
        [{"to": token["address"], "data": data}, "latest"],
    )
    return _to_amount(raw, token["decimals"])


async def get_balances(chain: str, address: str) -> dict[str, str]:
    """Every asset we track on this chain for one address."""
    balances = {NATIVE[chain]: await get_native_balance(chain, address)}

    for symbol in TOKENS.get(chain, {}):
        balances[symbol] = await get_token_balance(chain, address, symbol)

    return balances


async def get_block_number(chain: str) -> int:
    raw = await bitnob.rpc(chain, "eth_blockNumber")
    return int(raw, 16)


# Providers cap how many blocks a single eth_getLogs call may span. Bitnob's
# limit is 1000; exceeding it returns an error rather than truncated results, so
# any range wider than this has to be split.
MAX_LOG_RANGE = 1000

# Stop walking a range once this many logs have been collected. An exchange hot
# wallet can produce thousands in a few hundred blocks, and processing all of
# them costs a round trip per unique block afterwards.
MAX_LOGS = 500


async def _get_logs_chunked(
    chain: str,
    contracts: list[str],
    topics: list,
    from_block: int,
    to_block: int,
) -> tuple[list[dict], int]:
    """
    Walk a block range in provider-sized windows.

    Returns the logs AND the highest block we actually finished reading. Those
    differ whenever MAX_LOGS cuts the walk short, and the caller must not
    advance its sync position past the second value — everything above it was
    never fetched. Reporting only the logs is how a busy wallet silently loses
    history: the position moves to the chain tip while the middle of the range
    was never read.
    """
    logs: list[dict] = []
    start = from_block
    completed = from_block - 1  # nothing read yet

    while start <= to_block:
        end = min(start + MAX_LOG_RANGE - 1, to_block)

        batch = await bitnob.rpc(
            chain,
            "eth_getLogs",
            [{
                "fromBlock": hex(start),
                "toBlock": hex(end),
                "address": contracts,
                "topics": topics,
            }],
        )

        logs.extend(batch or [])
        completed = end
        start = end + 1

        if len(logs) >= MAX_LOGS:
            break

    return logs, completed


async def get_transfers(
    chain: str,
    address: str,
    from_block: int,
    to_block: Optional[int] = None,
) -> tuple[list[dict], int]:
    """
    Token transfers touching this address since from_block.

    Returns the transfers and the highest block fully scanned, which is not
    necessarily to_block — see _get_logs_chunked.

    Two queries because the address can be either sender or recipient, and
    eth_getLogs treats topics positionally — topic[1] is the sender, topic[2]
    the recipient. There is no "either" filter.
    """
    padded = _pad_address(address)
    from_block = max(0, from_block)
    to_block = to_block if to_block is not None else await get_block_number(chain)

    contracts = [t["address"] for t in TOKENS.get(chain, {}).values()]
    if not contracts:
        # No tracked tokens on this chain, so there is nothing to scan for and
        # the whole range counts as complete.
        return [], to_block

    outgoing, out_done = await _get_logs_chunked(
        chain, contracts, [TRANSFER_TOPIC, padded], from_block, to_block
    )

    incoming, in_done = await _get_logs_chunked(
        chain, contracts, [TRANSFER_TOPIC, None, padded], from_block, to_block
    )

    # Two independent walks, either of which may have stopped early. We are only
    # caught up to where BOTH finished.
    completed = min(out_done, in_done)

    by_contract = {
        token["address"].lower(): (symbol, token["decimals"])
        for symbol, token in TOKENS.get(chain, {}).items()
    }

    transfers = []
    for logs, direction in ((incoming or [], "receive"), (outgoing or [], "send")):
        for log in logs:
            contract = log.get("address", "").lower()
            if contract not in by_contract:
                continue

            symbol, decimals = by_contract[contract]
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue

            counterparty_topic = topics[1] if direction == "receive" else topics[2]

            transfers.append({
                "tx_hash": log.get("transactionHash"),
                "chain": chain,
                "direction": direction,
                "symbol": symbol,
                "amount": _to_amount(log.get("data", "0x"), decimals),
                "counterparty": "0x" + counterparty_topic[-40:],
                "block_number": int(log.get("blockNumber", "0x0"), 16),
            })

    return transfers, completed


async def get_block_time(chain: str, block_number: int) -> Optional[int]:
    """
    Unix timestamp of a block.

    Logs carry a block number but no time, and the alert needs to say when
    something happened. Cached by the caller — many transfers share a block.
    """
    block = await bitnob.rpc(chain, "eth_getBlockByNumber", [hex(block_number), False])
    if not block or not block.get("timestamp"):
        return None
    return int(block["timestamp"], 16)

# --------------------------------------------------------------------- fees


async def get_transaction_fee(chain: str, tx_hash: str) -> tuple[Optional[str], bool]:
    """
    What a transaction actually cost, and whether it succeeded.

    Returns (fee_in_native_units, succeeded). Logs carry no fee — the receipt
    does, as gasUsed x effectiveGasPrice.

    The status matters as much as the fee: a reverted transfer still emits no
    Transfer event, but a reverted *send* would otherwise look like money left
    the wallet when it never did.
    """
    receipt = await bitnob.rpc(chain, "eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None, True

    gas_used = receipt.get("gasUsed")
    gas_price = receipt.get("effectiveGasPrice")

    # status is "0x1" for success, "0x0" for revert. Pre-Byzantium receipts have
    # no status field at all, so a missing value is treated as success rather
    # than silently marking old transactions failed.
    status = receipt.get("status")
    succeeded = status is None or int(status, 16) == 1

    if not gas_used or not gas_price:
        return None, succeeded

    wei = int(gas_used, 16) * int(gas_price, 16)
    return str(Decimal(wei) / (Decimal(10) ** 18)), succeeded


# ------------------------------------------------------- native coin tracking
#
# eth_getLogs cannot see a plain ETH transfer, because a native send emits no
# event log. trace_filter would solve this in one call, but Bitnob's gateway
# returns -32601 for it on both Ethereum and Base (verified 2026-08-17).
#
# What the gateway does expose is archive state — eth_getBalance answers for
# historical blocks, not just `latest`. That makes a different approach work:
# detect that the balance moved, then binary search the range to find exactly
# where, then trace that single block for the detail.
#
# Cost is 2 calls per cycle for a quiet wallet, ~12 for one that moved.


async def get_nonce(chain: str, address: str, block: str = "latest") -> int:
    raw = await bitnob.rpc(chain, "eth_getTransactionCount", [address, block])
    return int(raw, 16) if raw else 0


async def get_native_balance_at(chain: str, address: str, block: int | str) -> int:
    """Balance in wei at a specific height. `block` may be an int or 'latest'."""
    tag = block if isinstance(block, str) else hex(block)
    raw = await bitnob.rpc(chain, "eth_getBalance", [address, tag])
    return int(raw, 16) if raw else 0


async def find_balance_change_blocks(
    chain: str,
    address: str,
    from_block: int,
    to_block: int,
    max_changes: int = 8,
) -> list[int]:
    """
    Blocks within (from_block, to_block] where this address's balance changed.

    Binary search: if the balance at each end of a span differs, the change is
    inside it, so halve and recurse. A span of one block that differs IS the
    answer.

    Recursing into both halves rather than just one is what finds multiple
    changes — cost scales with the number of changes times log(range), not with
    the size of the range. max_changes caps the work for a wallet that moves
    constantly; hitting it means we return what we found and the rest is picked
    up next cycle, because the sync position never advances past what we read.
    """
    if from_block >= to_block:
        return []

    found: list[int] = []
    # (low, high, balance_at_low, balance_at_high)
    start_balance = await get_native_balance_at(chain, address, from_block)
    end_balance = await get_native_balance_at(chain, address, to_block)

    if start_balance == end_balance:
        # Endpoints match. Either nothing happened, or money moved out and back
        # within the range and netted exactly — rare, and the nonce check in the
        # caller catches the outgoing half of that case.
        return []

    stack = [(from_block, to_block, start_balance, end_balance)]

    while stack and len(found) < max_changes:
        low, high, bal_low, bal_high = stack.pop()

        if bal_low == bal_high:
            continue

        if high - low == 1:
            found.append(high)
            continue

        mid = (low + high) // 2
        bal_mid = await get_native_balance_at(chain, address, mid)

        # Push both halves; a differing half contains at least one change.
        if bal_mid != bal_high:
            stack.append((mid, high, bal_mid, bal_high))
        if bal_low != bal_mid:
            stack.append((low, mid, bal_low, bal_mid))

    return sorted(found)


def _trace_value(action: dict) -> int:
    raw = action.get("value") or "0x0"
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return 0


async def get_native_transfers_in_block(
    chain: str,
    block_number: int,
    address: str,
) -> list[dict]:
    """
    Native transfers touching this address in one block, from trace_block.

    Traces rather than the block's transaction list, because traces also include
    internal transfers — ETH moved by a contract rather than by a top-level
    send. Those are invisible in eth_getBlockByNumber and are exactly how funds
    arrive from an exchange withdrawal or a DEX swap.
    """
    traces = await bitnob.rpc(chain, "trace_block", [hex(block_number)])
    if not traces:
        return []

    target = address.lower()
    transfers: list[dict] = []

    for trace in traces:
        action = trace.get("action") or {}
        value = _trace_value(action)
        if value == 0:
            continue

        # A reverted call still appears in the trace but moved nothing.
        if trace.get("error"):
            continue

        sender = (action.get("from") or "").lower()
        recipient = (action.get("to") or "").lower()

        if recipient == target:
            direction, counterparty = "receive", action.get("from")
        elif sender == target:
            direction, counterparty = "send", action.get("to")
        else:
            continue

        transfers.append({
            "tx_hash": trace.get("transactionHash"),
            "chain": chain,
            "direction": direction,
            "symbol": NATIVE[chain],
            "amount": str(Decimal(value) / (Decimal(10) ** 18)),
            "counterparty": counterparty,
            "block_number": block_number,
        })

    return transfers
