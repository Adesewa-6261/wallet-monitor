"""
app/services/monitor.py

The poller. This is the product.

Every cycle it asks each wallet's chain what has happened since we last looked,
stores anything new, and sends an alert for each one. It runs whether or not
anybody has the app open — that is the whole point, since the CEO wants to know
at 3am that money moved without having to check.

Three things make it safe to restart:

  - Position, not time. We sync from `wallet_sync_state.last_block` rather than
    "the last two minutes". Render restarts services occasionally; a time-based
    window would silently drop everything that happened while we were down.

  - Idempotent inserts. `transactions` has a unique constraint on
    (wallet_id, tx_hash, symbol, direction), so seeing the same transaction
    twice does nothing.

  - Idempotent alerts. `alerted_at` is null until delivery succeeds. Crashing
    between insert and send means the next cycle picks it up, and crashing after
    send means it is already marked.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from .. import db
from ..adapters import bitcoin, evm, telegram, tron
from ..adapters import bitnob
from ..core.config import config
from ..core.http import map_limit
from . import health, prices

logger = logging.getLogger("walletnest.monitor")

# How far back to look on a wallet we have never synced. Far enough to give the
# user immediate context, short enough that adding a wallet does not spend
# minutes reading history.
#
# Providers cap eth_getLogs at a fixed span (1000 blocks on Bitnob), so a wide
# lookback becomes several sequential calls per wallet. These values are chosen
# to be a few chunks, not dozens: roughly three hours on Ethereum and two hours
# on Base, which have very different block times.
INITIAL_LOOKBACK_BLOCKS = {"ethereum": 1000, "base": 3000}

# Ceiling on how many transfers we process in one cycle for one wallet.
#
# Exchange hot wallets move thousands of tokens per hour. Without a cap, adding
# one turns a poll cycle into thousands of sequential RPC calls and the request
# appears to hang. Anything beyond this is dropped for the cycle; the sync
# position still advances, so the wallet stays current rather than falling
# permanently behind.
MAX_TRANSFERS_PER_CYCLE = 200


async def _evm_transactions(wallet: dict, last_block: Optional[int]) -> tuple[list[dict], int]:
    chain = wallet["chain"]
    head = await evm.get_block_number(chain)

    if last_block is None:
        last_block = max(0, head - INITIAL_LOOKBACK_BLOCKS.get(chain, 7200))

    if last_block >= head:
        return [], head

    transfers = await evm.get_transfers(chain, wallet["input"], last_block + 1, head)

    if len(transfers) > MAX_TRANSFERS_PER_CYCLE:
        # Keep the most recent, which is what an alert is for.
        transfers.sort(key=lambda t: t["block_number"], reverse=True)
        transfers = transfers[:MAX_TRANSFERS_PER_CYCLE]

    # Many transfers share a block, so each timestamp is fetched once and the
    # lookups run concurrently rather than one after another.
    block_numbers = {t["block_number"] for t in transfers}

    async def timestamp(number: int) -> tuple[int, Optional[int]]:
        return number, await evm.get_block_time(chain, number)

    block_times = dict(await map_limit(sorted(block_numbers), 8, timestamp))

    transactions = []
    for transfer in transfers:
        timestamp = block_times.get(transfer["block_number"])
        transactions.append({
            "tx_hash": transfer["tx_hash"],
            "chain": chain,
            "direction": transfer["direction"],
            "symbol": transfer["symbol"],
            "amount": transfer["amount"],
            "counterparty": transfer["counterparty"],
            "block_time": (
                datetime.fromtimestamp(timestamp, timezone.utc)
                if timestamp
                else datetime.now(timezone.utc)
            ),
            "fee": None,
        })

    return transactions, head


def _bitcoin_net_effect(tx: dict, owned: set[str]) -> Optional[dict]:
    """
    Work out what a Bitcoin transaction actually did to this wallet.

    This is the part that catches people out. Bitcoin has no accounts — a
    transaction consumes whole previous outputs and creates new ones, so
    spending 0.02 BTC from a 1.9 BTC chunk means the entire 1.9 leaves and 1.88
    comes straight back as change, to a fresh address the same wallet owns.

    Reading that naively reports "sent 1.9 BTC", which is alarming and wrong. So
    we compute the net effect: value in, minus value out.

    When every output belongs to the user, nothing left their control at all —
    that is a self-transfer, and only the network fee actually moved.
    """
    value_in = 0   # outputs paying us
    value_out = 0  # inputs spending from us
    external_outputs = 0
    counterparty = None

    for vin in tx.get("vin", []):
        prevout = vin.get("prevout") or {}
        if prevout.get("scriptpubkey_address") in owned:
            value_out += int(prevout.get("value", 0))

    for vout in tx.get("vout", []):
        address = vout.get("scriptpubkey_address")
        value = int(vout.get("value", 0))
        if address in owned:
            value_in += value
        else:
            external_outputs += value
            if counterparty is None:
                counterparty = address

    if value_in == 0 and value_out == 0:
        return None

    net = value_in - value_out
    fee = int(tx.get("fee", 0))

    # We spent, and everything came back to us. Nothing left the wallet.
    if value_out > 0 and external_outputs == 0:
        return {
            "direction": "internal",
            "sats": fee,
            "counterparty": None,
        }

    if net > 0:
        # Find who paid us, which is an input address we do not own.
        for vin in tx.get("vin", []):
            address = (vin.get("prevout") or {}).get("scriptpubkey_address")
            if address and address not in owned:
                counterparty = address
                break
        return {"direction": "receive", "sats": net, "counterparty": counterparty}

    # Net outflow. Report what actually left, not the whole input value.
    return {"direction": "send", "sats": external_outputs, "counterparty": counterparty}


async def _bitcoin_transactions(wallet: dict, last_block: Optional[int]) -> tuple[list[dict], int]:
    """
    Bitcoin has no block-range filter on address history, so we read recent
    transactions per watched address and skip anything at or below the last
    height we recorded.
    """
    watched = list(wallet.get("watched_addresses") or [])

    if not watched:
        # First sync, or a wallet added before scanning. Derive and store the
        # addresses that have history so later cycles are cheap.
        result = await bitcoin.get_balance(
            wallet["input"], wallet["input_type"], wallet.get("address_type")
        )
        watched = result.get("watched_addresses") or []

        if watched:
            await db.execute(
                "update wallets set watched_addresses = $1 where id = $2",
                watched,
                wallet["id"],
            )
        if result.get("detected_address_type") and not wallet.get("address_type"):
            await db.execute(
                "update wallets set address_type = $1 where id = $2",
                result["detected_address_type"],
                wallet["id"],
            )

    if not watched:
        return [], last_block or 0

    owned = set(watched)
    seen: dict[str, dict] = {}
    highest = last_block or 0

    for address in watched:
        for tx in await bitcoin.fetch_address_transactions(address):
            status = tx.get("status") or {}
            height = status.get("block_height") or 0

            # Unconfirmed transactions have no height. Include them — a pending
            # receipt is exactly what someone wants to know about — but do not
            # let them move the sync position.
            if height and last_block and height <= last_block:
                continue

            highest = max(highest, height)
            seen[tx["txid"]] = tx

    transactions = []
    for txid, tx in seen.items():
        effect = _bitcoin_net_effect(tx, owned)
        if not effect:
            continue

        status = tx.get("status") or {}
        block_time = status.get("block_time")

        transactions.append({
            "tx_hash": txid,
            "chain": "bitcoin",
            "direction": effect["direction"],
            "symbol": "BTC",
            "amount": str(effect["sats"] / 100_000_000),
            "counterparty": effect["counterparty"],
            "block_time": (
                datetime.fromtimestamp(block_time, timezone.utc)
                if block_time
                else datetime.now(timezone.utc)
            ),
            "fee": str(int(tx.get("fee", 0)) / 100_000_000),
        })

    return transactions, highest


async def sync_wallet(wallet: dict) -> int:
    """
    Bring one wallet up to date. Returns how many new transactions were stored.

    Failures are recorded against the wallet and swallowed, so one unreachable
    provider cannot stop the other wallets from syncing.
    """
    try:
        state = await db.fetchrow(
            "select last_block from wallet_sync_state where wallet_id = $1", wallet["id"]
        )
        last_block = state["last_block"] if state else None

        if wallet["chain"] in ("ethereum", "base"):
            transactions, head = await _evm_transactions(wallet, last_block)
        elif wallet["chain"] == "bitcoin":
            transactions, head = await _bitcoin_transactions(wallet, last_block)
        elif wallet["chain"] == "tron":
            # Address history lives in TronGrid's HTTP API rather than the node,
            # and Bitnob proxies the node. Balances work; history does not.
            transactions, head = [], last_block or 0
        else:
            return 0

        stored = 0
        for tx in transactions:
            value = await prices.value_usd(tx["symbol"], tx["amount"])

            result = await db.execute(
                """
                insert into transactions
                    (wallet_id, tx_hash, chain, direction, symbol, amount, fee,
                     counterparty, block_time, value_usd)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                on conflict (wallet_id, tx_hash, symbol, direction) do nothing
                """,
                wallet["id"], tx["tx_hash"], tx["chain"], tx["direction"],
                tx["symbol"], tx["amount"], tx["fee"], tx["counterparty"],
                tx["block_time"], value,
            )
            if result.endswith("1"):
                stored += 1

        await db.execute(
            """
            insert into wallet_sync_state (wallet_id, last_block, last_synced_at, last_error)
            values ($1, $2, now(), null)
            on conflict (wallet_id) do update
                set last_block = excluded.last_block,
                    last_synced_at = now(),
                    last_error = null
            """,
            wallet["id"], head,
        )

        return stored

    except Exception as err:
        logger.warning("sync failed for wallet %s: %s", wallet["id"], err)
        await db.execute(
            """
            insert into wallet_sync_state (wallet_id, last_error, last_synced_at)
            values ($1, $2, now())
            on conflict (wallet_id) do update
                set last_error = excluded.last_error, last_synced_at = now()
            """,
            wallet["id"], str(err)[:200],
        )
        return 0



async def _fetch_balance(chain: str, wallet_input: str, symbol: str) -> Optional[str]:
    """
    Best-effort current balance for the wallet. Returns None on any error so a
    failed lookup does not block the alert from sending.
    """
    try:
        if chain in ("ethereum", "base"):
            if symbol in ("ETH",):
                return await evm.get_native_balance(chain, wallet_input)
            return await evm.get_token_balance(chain, wallet_input, symbol)
        if chain == "tron":
            bals = await tron.get_balances(wallet_input)
            return bals.get(symbol)
        if chain == "bitcoin":
            result = await bitcoin.scan_single_address(wallet_input)
            return result.get("btc")
    except Exception:
        return None


async def deliver_alerts() -> int:
    """
    Send every stored transaction that has not been alerted yet.

    `alerted_at` is set only after Telegram confirms, so a failure here means
    the next cycle tries again rather than the alert being lost.
    """
    rows = await db.fetch(
        """
        select t.id, t.direction, t.symbol, t.amount, t.chain, t.counterparty,
               t.value_usd, w.label, w.chain as wallet_chain,
               tl.chat_id, a.min_value_usd, a.enabled,
               a.alert_on_receive, a.alert_on_send
        from transactions t
        join wallets w on w.id = t.wallet_id
        join telegram_links tl on tl.user_id = w.user_id
        join alert_settings a on a.user_id = w.user_id
        where t.alerted_at is null
        order by t.block_time
        limit 50
        """
    )

    sent = 0
    for row in rows:
        skip = (
            not row["enabled"]
            or (row["direction"] == "receive" and not row["alert_on_receive"])
            or (row["direction"] == "send" and not row["alert_on_send"])
            or (
                row["value_usd"] is not None
                and row["min_value_usd"]
                and row["value_usd"] < float(row["min_value_usd"])
            )
        )

        if skip:
            # Mark it anyway. The user chose not to be told, and leaving it
            # pending would re-evaluate it forever.
            await db.execute(
                "update transactions set alerted_at = now() where id = $1", row["id"]
            )
            continue

        text = telegram.format_alert(
            direction=row["direction"],
            amount=row["amount"],
            symbol=row["symbol"],
            wallet_label=row["label"] or "Wallet",
            chain=row["chain"],
            value_usd=float(row["value_usd"]) if row["value_usd"] is not None else None,
            counterparty=row["counterparty"],
        )

        if await telegram.send_message(row["chat_id"], text):
            await db.execute(
                "update transactions set alerted_at = now() where id = $1", row["id"]
            )
            sent += 1

    return sent


async def run_cycle() -> dict:
    """One pass over every wallet, then deliver whatever is pending."""
    wallets = await db.fetch(
        """
        select id, user_id, label, input, input_type, chain, address_type,
               watched_addresses
        from wallets
        """
    )

    found = 0
    for wallet in wallets:
        found += await sync_wallet(dict(wallet))

    sent = await deliver_alerts()

    # Runs every cycle but only sends when a user is actually due one, so there
    # is a single loop rather than a second scheduler.
    digests = await health.send_digests()

    return {
        "wallets": len(wallets),
        "new_transactions": found,
        "alerts_sent": sent,
        "digests_sent": digests,
    }


async def run_forever() -> None:
    """
    Background loop, started when the app boots.

    Runs in-process rather than as a scheduled job because Render keeps one
    service alive continuously — and because the loop itself is what stops the
    free tier from idling the service to sleep.
    """
    interval = config.poll_minutes * 60
    logger.info("monitor started, polling every %s minutes", config.poll_minutes)

    while True:
        try:
            result = await run_cycle()
            if result["new_transactions"] or result["alerts_sent"] or result["digests_sent"]:
                logger.info("cycle: %s", result)
        except Exception:
            # Never let one bad cycle kill the loop.
            logger.exception("poll cycle failed")

        await asyncio.sleep(interval)