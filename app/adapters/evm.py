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


async def get_safe_head(chain: str) -> int:
    """
    The highest block safe to both scan and record as synced.

    Held back from the true tip so a range built here is not rejected by a node
    that happens to be a block or two behind.
    """
    return max(0, await get_block_number(chain) - HEAD_SAFETY_MARGIN)


# Providers cap how many blocks a single eth_getLogs call may span. Bitnob's
# limit is 1000; exceeding it returns an error rather than truncated results, so
# any range wider than this has to be split.
MAX_LOG_RANGE = 1000

# Stop walking a range once this many logs have been collected. An exchange hot
# wallet can produce thousands in a few hundred blocks, and processing all of
# them costs a round trip per unique block afterwards.
MAX_LOGS = 500


# The provider load-balances across nodes, and they are not always at the same
# height. A range built from one node's tip can be rejected by the next node as
# "beyond current head", so the top of the range is held back slightly.
#
# The margin is applied in exactly one place — get_safe_head — because the
# caller records whatever head it was given as the sync position. Clamping
# inside get_transfers while the caller records the unclamped tip would skip
# those blocks permanently rather than deferring them.
HEAD_SAFETY_MARGIN = 5


async def _get_logs_chunked(
    chain: str,
    contracts: list[str],
    topics: list,
    from_block: int,
    to_block: int,
) -> list[dict]:
    """Walk a block range in provider-sized windows and concatenate the logs."""
    logs: list[dict] = []
    start = from_block

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
        start = end + 1

        if len(logs) >= MAX_LOGS:
            break

    return logs


async def get_transfers(
    chain: str,
    address: str,
    from_block: int,
    to_block: Optional[int] = None,
) -> list[dict]:
    """
    Token transfers touching this address since from_block.

    Two queries because the address can be either sender or recipient, and
    eth_getLogs treats topics positionally — topic[1] is the sender, topic[2]
    the recipient. There is no "either" filter.
    """
    padded = _pad_address(address)
    from_block = max(0, from_block)

    # An explicit to_block comes from the caller, which has already applied the
    # margin via get_safe_head. Only a tip we derive here needs clamping.
    if to_block is None:
        to_block = await get_safe_head(chain)

    contracts = [t["address"] for t in TOKENS.get(chain, {}).values()]
    if not contracts:
        return []

    outgoing = await _get_logs_chunked(
        chain, contracts, [TRANSFER_TOPIC, padded], from_block, to_block
    )

    incoming = await _get_logs_chunked(
        chain, contracts, [TRANSFER_TOPIC, None, padded], from_block, to_block
    )

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

    return transfers


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

async def get_transaction_fee(chain: str, tx_hash: str) -> Optional[str]:
    """
    What a transaction cost to send, in the chain's native coin.

    The fee is gas used multiplied by the price actually paid per unit, and
    neither number exists until the transaction has been mined — which is why
    it comes from the receipt rather than the logs we scanned to find it.

    Note the result is denominated in ETH even when the transfer moved USDT:
    fees are always paid in the native coin, never in the token being sent.

    Returns None rather than raising when the receipt is unavailable. A missing
    fee is worth showing as unknown; it is not worth losing the transaction over.
    """
    receipt = await bitnob.rpc(chain, "eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None

    gas_used = receipt.get("gasUsed")
    # effectiveGasPrice covers both the legacy flat price and EIP-1559's
    # base-fee-plus-tip, so it is the one field that is right on either.
    price = receipt.get("effectiveGasPrice")

    if gas_used is None or price is None:
        return None

    try:
        wei = int(gas_used, 16) * int(price, 16)
    except (TypeError, ValueError):
        return None

    # 18 decimals, kept as a string for the same reason balances are: the value
    # is exact in wei and a float would not preserve it. _to_amount speaks hex,
    # which is what the rest of this module hands it.
    return _to_amount(hex(wei), 18)
