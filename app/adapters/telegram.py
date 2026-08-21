"""
app/adapters/telegram.py

Sending alerts to Telegram.

The bot itself is built by a colleague; this only pushes messages. All it needs
is a bot token and the chat id we stored when the user linked their account.
"""

import asyncio
import os
import time
from typing import Optional

from ..core.amounts import format_amount
from ..core.errors import RequestError
from ..core.http import fetch_json

API_BASE = "https://api.telegram.org"

# Telegram accepts roughly one message per second to a single chat. Exceeding
# that returns 429 and the message is dropped, so the interval is enforced here
# rather than discovered at runtime.
MIN_SEND_INTERVAL = 1.1

_last_send: dict[int, float] = {}


def _token() -> Optional[str]:
    # Read lazily rather than at import: the poller should still run and record
    # transactions on a deployment where the bot is not configured yet.
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None


async def send_message(chat_id: int, text: str) -> bool:
    """
    Send one message, pacing to stay inside Telegram's per-chat rate limit.

    Returns False rather than raising when delivery fails — the caller decides
    whether to retry, and a Telegram outage should never stop the poller from
    recording transactions.
    """
    token = _token()
    if not token:
        return False

    # Wait out the remainder of the interval since the last message to this
    # chat. Without this, a backlog is sent as fast as the loop can run and
    # every message is rejected.
    elapsed = time.monotonic() - _last_send.get(chat_id, 0.0)
    if elapsed < MIN_SEND_INTERVAL:
        await asyncio.sleep(MIN_SEND_INTERVAL - elapsed)

    try:
        await fetch_json(
            f"{API_BASE}/bot{token}/sendMessage",
            method="POST",
            json_body={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            label="telegram",
            timeout=10.0,
        )
        _last_send[chat_id] = time.monotonic()
        return True
    except RequestError:
        # Record the attempt anyway. A rejected send still counted against the
        # rate limit, so retrying immediately would fail for the same reason.
        _last_send[chat_id] = time.monotonic()
        return False


def escape(text: str) -> str:
    """
    Escape text going into an HTML-mode message.

    Telegram rejects the whole message if it cannot parse the entities, so a
    wallet the user named "Mum & Dad savings" would fail to send rather than
    arrive with odd punctuation. Since alerts are claimed before delivery, a
    rejected message is a lost alert, not a retried one — which is why this
    applies to anything a user can type.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_password_changed() -> str:
    """
    Told to the user rather than asked of them.

    The point of this message is the second half: if the account holder did not
    change their password, someone else did, and a Telegram they still control
    is the one channel an attacker holding the account cannot quietly silence.
    Changing the password does not unlink Telegram, so this still arrives.
    """
    return (
        "<b>Password changed</b>\n"
        "The password on your WalletNest account was just changed.\n\n"
        "If this was not you, someone else has access to your account. "
        "Change your password again now and check your wallets."
    )


EXPLORER_TX = {
    "ethereum": "https://etherscan.io/tx/",
    "base": "https://basescan.org/tx/",
    "bitcoin": "https://mempool.space/tx/",
    "tron": "https://tronscan.org/#/transaction/",
}


def format_alert(
    direction: str,
    amount: str,
    symbol: str,
    wallet_label: str,
    chain: str,
    value_usd: Optional[float],
    counterparty: Optional[str],
    tx_hash: Optional[str] = None,
    balance: Optional[str] = None,
) -> str:
    """
    Build the alert text.

    Counterparty addresses are shown in full rather than truncated. Address
    poisoning works by generating a lookalike that matches the first and last
    few characters of an address the user really pays, so an abbreviated address
    in an alert is precisely what the attack needs to succeed.
    """
    display_amount = format_amount(amount)
    # The label is whatever the user typed into the rename field.
    wallet_label = escape(wallet_label)

    if direction == "internal":
        return (
            f"<b>{wallet_label}</b>\n"
            f"Internal transfer — moved between your own addresses\n"
            f"Network fee: {display_amount} {symbol}"
        )

    arrow = "Received" if direction == "receive" else "Sent"
    lines = [f"<b>{wallet_label}</b>", f"{arrow} {display_amount} {symbol}"]

    if value_usd is not None:
        lines.append(f"≈ ${value_usd:,.2f}")

    lines.append(f"Chain: {chain}")

    if counterparty:
        label = "From" if direction == "receive" else "To"
        lines.append(f"{label}: <code>{escape(counterparty)}</code>")

    if tx_hash:
        base_url = EXPLORER_TX.get(chain, "")
        if base_url:
            lines.append(f'Tx: <a href="{base_url}{tx_hash}">{tx_hash[:16]}…</a>')
            lines.append(f"<code>{tx_hash}</code>")
        else:
            lines.append(f"Tx: <code>{tx_hash}</code>")

    if balance:
        lines.append(f"Wallet balance: {format_amount(balance)} {symbol}")

    return "\n".join(lines)


def _period(rows: list) -> Optional[str]:
    """Human span the group covers, so a summary is not undated."""
    times = [r["block_time"] for r in rows if r.get("block_time")]
    if not times:
        return None

    first, last = min(times), max(times)
    if first.date() == last.date():
        return f"{first:%H:%M}–{last:%H:%M} today" if first != last else f"{first:%H:%M}"
    return f"{first:%d %b %H:%M} – {last:%d %b %H:%M}"


def format_grouped_alert(rows: list, wallet_label: str) -> str:
    """
    One message summarising several transactions.

    Sending a hundred separate alerts is both rate-limited by Telegram and
    useless to read. Above a handful, a summary carries more than the individual
    messages it replaces.
    """
    received = [r for r in rows if r["direction"] == "receive"]
    sent = [r for r in rows if r["direction"] == "send"]

    def summarise(entries: list, label: str) -> Optional[str]:
        """
        A total covering only the transactions we could price.

        Counting an unpriced transaction as zero would present the figure as if
        it covered all of them. Where some are unpriced the count is stated
        separately, so the total is never read as more complete than it is.
        """
        if not entries:
            return None

        priced = [e for e in entries if e["value_usd"] is not None]
        line = f"{label} {len(entries)}"

        if priced:
            total = sum(float(e["value_usd"]) for e in priced)
            line += f" · ${total:,.2f}"
            if len(priced) < len(entries):
                line += f" ({len(entries) - len(priced)} unpriced)"
        else:
            line += " · value unknown"

        return line

    period = _period(rows)

    lines = [f"<b>{escape(wallet_label)}</b>"]
    lines.append(f"{len(rows)} transactions" + (f" · {period}" if period else ""))
    lines.append("")

    for line in (summarise(received, "In: "), summarise(sent, "Out:")):
        if line:
            lines.append(line)

    priced = [r for r in rows if r["value_usd"] is not None]
    largest = max(priced, key=lambda r: float(r["value_usd"])) if priced else None

    if largest is not None and float(largest["value_usd"]) > 0:
        verb = "received" if largest["direction"] == "receive" else "sent"
        lines.append("")
        lines.append(
            f"Largest: {verb} {format_amount(largest['amount'])} "
            f"{largest['symbol']} (${float(largest['value_usd']):,.2f})"
        )

    lines.append("")
    lines.append("Open the app to see them individually.")

    return "\n".join(lines)

def format_reset_code(code: str, minutes: int) -> str:
    """
    The message carrying a password reset code.

    It says what to do with the code and, just as importantly, what to do if the
    reset was not requested: a code arriving unasked means someone else knows
    the account's email and is trying to take it.
    """
    return (
        "<b>Password reset</b>\n"
        f"Your reset code is <code>{code}</code>\n"
        f"It expires in {minutes} minutes.\n\n"
        "Enter it in the app, along with your new password.\n\n"
        "If you did not ask to reset your password, ignore this message and "
        "your password stays as it is. Someone knowing your email is enough to "
        "trigger this, so it is worth checking your account is still yours."
    )
