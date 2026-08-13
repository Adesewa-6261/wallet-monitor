"""
app/adapters/telegram.py

Sending alerts to Telegram.

The bot itself is built by a colleague; this only pushes messages. All it needs
is a bot token and the chat id we stored when the user linked their account.
"""

import os
from typing import Optional

from ..core.errors import RequestError
from ..core.http import fetch_json

API_BASE = "https://api.telegram.org"


def _token() -> Optional[str]:
    # Read lazily rather than at import: the poller should still run and record
    # transactions on a deployment where the bot is not configured yet.
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None


async def send_message(chat_id: int, text: str) -> bool:
    """
    Send one message. Returns False rather than raising when delivery fails —
    the caller decides whether to retry, and a Telegram outage should never
    stop the poller from recording transactions.
    """
    token = _token()
    if not token:
        return False

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
        return True
    except RequestError:
        return False


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
    if direction == "internal":
        return (
            f"<b>{wallet_label}</b>\n"
            f"Internal transfer — moved between your own addresses\n"
            f"Network fee: {amount} {symbol}"
        )

    arrow = "Received" if direction == "receive" else "Sent"
    lines = [f"<b>{wallet_label}</b>", f"{arrow} {amount} {symbol}"]

    if value_usd is not None:
        lines.append(f"≈ ${value_usd:,.2f}")

    lines.append(f"Chain: {chain}")

    if counterparty:
        label = "From" if direction == "receive" else "To"
        lines.append(f"{label}: <code>{counterparty}</code>")

    if tx_hash:
        base_url = EXPLORER_TX.get(chain, "")
        if base_url:
            lines.append(f'Tx: <a href="{base_url}{tx_hash}">{tx_hash[:16]}…</a>')
            lines.append(f"<code>{tx_hash}</code>")
        else:
            lines.append(f"Tx: <code>{tx_hash}</code>")

    if balance:
        lines.append(f"Wallet balance: {balance} {symbol}")

    return "\n".join(lines)