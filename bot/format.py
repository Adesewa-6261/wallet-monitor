"""
bot/format.py

Turning API responses into Telegram messages.

Two rules run through all of it.

**Addresses are never truncated.** Address poisoning works by generating a
lookalike matching the first and last few characters of an address the user
really pays. An abbreviated address in a chat is precisely what the attack
needs, so counterparties are shown whole even though it is uglier.

**Silence must be distinguishable from stillness.** Where a value is unknown we
say so rather than printing a zero, because "no price available" and "worth
nothing" are different facts.
"""

from datetime import datetime, timezone
from typing import Optional

EXPLORER_TX = {
    "ethereum": "https://etherscan.io/tx/",
    "base": "https://basescan.org/tx/",
    "bitcoin": "https://mempool.space/tx/",
    "tron": "https://tronscan.org/#/transaction/",
}

CHAIN_LABEL = {
    "ethereum": "Ethereum",
    "base": "Base",
    "bitcoin": "Bitcoin",
    "tron": "Tron",
}


def escape(text: str) -> str:
    """Telegram HTML parse mode only cares about these three."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def ago(iso: Optional[str]) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"

    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def money(value: Optional[float]) -> str:
    return f"${value:,.2f}" if value is not None else "value unknown"


def wallets(rows: list) -> str:
    if not rows:
        return (
            "You are not watching any wallets yet.\n\n"
            "Add one in the app, and alerts will start arriving here."
        )

    lines = [f"<b>Your wallets</b> ({len(rows)})", ""]

    for row in rows:
        label = escape(row.get("label") or f"{CHAIN_LABEL.get(row['chain'], row['chain'])} wallet")
        lines.append(f"<b>{label}</b>")
        lines.append(f"  {CHAIN_LABEL.get(row['chain'], row['chain'])} · {row['input_type']}")
        lines.append(f"  <code>{escape(row['display_key'])}</code>")

        if row.get("error"):
            lines.append(f"  ⚠️ {escape(row['error'][:70])}")
        else:
            lines.append(f"  checked {ago(row.get('last_synced_at'))}")
        lines.append("")

    return "\n".join(lines).strip()


def transactions(rows: list) -> str:
    if not rows:
        return "No transactions recorded yet."

    lines = ["<b>Recent activity</b>", ""]

    for row in rows:
        if row["direction"] == "internal":
            headline = f"↔️ Internal transfer · fee {row['amount']} {row['symbol']}"
        else:
            arrow = "📥" if row["direction"] == "receive" else "📤"
            verb = "Received" if row["direction"] == "receive" else "Sent"
            headline = f"{arrow} {verb} {row['amount']} {row['symbol']}"

        lines.append(headline)

        detail = [escape(row.get("wallet_label") or "Wallet")]
        if row.get("value_usd") is not None:
            detail.append(money(row["value_usd"]))
        detail.append(ago(row.get("block_time")))
        lines.append("  " + " · ".join(detail))

        base = EXPLORER_TX.get(row["chain"])
        if base and row.get("tx_hash"):
            lines.append(f'  <a href="{base}{row["tx_hash"]}">view transaction</a>')

        lines.append("")

    return "\n".join(lines).strip()


def status(payload: dict) -> str:
    healthy = payload.get("healthy")
    total = payload.get("wallets", 0)
    stale = payload.get("stale_wallets") or []

    if not total:
        return "No wallets to monitor yet."

    if healthy:
        lines = [
            "<b>✅ Monitoring is healthy</b>",
            "",
            f"All {total} wallet{'s' if total != 1 else ''} checked recently.",
        ]
    else:
        lines = [
            "<b>⚠️ Some wallets are not updating</b>",
            "",
            f"{len(stale)} of {total} need attention:",
        ]
        for wallet in stale[:10]:
            label = escape(wallet.get("label") or "Wallet")
            reason = wallet.get("error") or f"last checked {ago(wallet.get('last_synced_at'))}"
            lines.append(f"• {label} — {escape(str(reason)[:60])}")

    lines.append("")
    lines.append(f"Last sync: {ago(payload.get('last_sync_at'))}")
    return "\n".join(lines)


def settings(payload: dict) -> str:
    on_off = lambda flag: "on" if flag else "off"  # noqa: E731

    threshold = payload.get("min_value_usd") or 0
    threshold_line = (
        f"Ignore incoming under ${threshold:,.2f}"
        if threshold
        else "Alert on every amount"
    )

    return "\n".join([
        "<b>Alert settings</b>",
        "",
        f"Alerts: <b>{on_off(payload.get('enabled'))}</b>",
        f"Incoming: {on_off(payload.get('alert_on_receive'))}",
        f"Outgoing: {on_off(payload.get('alert_on_send'))}",
        f"Daily digest: {on_off(payload.get('daily_digest'))}",
        threshold_line,
        "",
        "<i>The amount filter applies to incoming only — a small outgoing",
        "transfer is often the more urgent signal.</i>",
        "",
        "Change with /mute, /unmute, /digest, /threshold &lt;amount&gt;",
    ])
