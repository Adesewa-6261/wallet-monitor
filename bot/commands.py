"""
bot/commands.py

What each command does.

Every handler returns text to send. None of them raise for ordinary problems —
an unlinked chat or an unreachable API is a message, not a stack trace, because
the person on the other end cannot act on a traceback.
"""

import logging
from typing import Optional

from app.core.errors import RequestError
from app.core import link_codes

from . import api, format as fmt

logger = logging.getLogger("walletnest.bot")

HELP = """<b>WalletNest</b>

I watch your crypto wallets and tell you the moment money moves.

<b>Getting started</b>
/link CODE — connect this chat to your account

<b>Once connected</b>
/wallets — what I am watching
/recent — latest transactions
/status — is monitoring actually working
/settings — your alert preferences

<b>Adjusting alerts</b>
/mute — pause all alerts
/unmute — resume
/digest — toggle the daily summary
/threshold 5 — ignore incoming under $5
/stop — disconnect this chat

Get your link code from the WalletNest app."""

NOT_LINKED = (
    "This chat is not connected to an account yet.\n\n"
    "Open the WalletNest app, generate a link code, then send it here as:\n"
    "<code>/link ABCD-EFGH</code>"
)


async def start(chat_id: int, args: str) -> str:
    try:
        await api.token_for(chat_id)
    except api.NotLinked:
        return (
            "<b>Welcome to WalletNest</b>\n\n"
            "I send an alert the moment money moves in or out of your wallets — "
            "across Bitcoin, Ethereum, Base and Tron.\n\n" + NOT_LINKED
        )
    return "You are already connected. Try /wallets or /recent.\n\n" + HELP


async def help_command(chat_id: int, args: str) -> str:
    return HELP


async def link(chat_id: int, args: str) -> str:
    code = link_codes.normalise(args)

    if not code:
        return (
            "Send the code along with the command:\n"
            "<code>/link ABCD-EFGH</code>"
        )

    try:
        await api.link(code, chat_id)
    except RequestError as err:
        # The API's messages here are already written for a person — expired,
        # invalid, rate limited — so they are passed through rather than
        # replaced with something vaguer.
        return f"❌ {fmt.escape(err.message)}"

    api.forget(chat_id)
    return (
        "✅ <b>Connected</b>\n\n"
        "This chat is now linked to your account. I will alert you here when "
        "money moves.\n\n"
        "Try /wallets to see what I am watching."
    )


async def stop(chat_id: int, args: str) -> str:
    try:
        result = await api.unlink(chat_id)
    except RequestError as err:
        return f"❌ {fmt.escape(err.message)}"

    if not result.get("unlinked"):
        return "This chat was not connected to anything."

    return (
        "Disconnected. No more alerts will arrive here.\n\n"
        "Link again any time with /link and a fresh code."
    )


async def wallets(chat_id: int, args: str) -> str:
    rows = await api.authed("GET", "/api/wallets", chat_id)
    return fmt.wallets(rows)


async def recent(chat_id: int, args: str) -> str:
    limit = 10
    if args.strip().isdigit():
        limit = max(1, min(25, int(args.strip())))
    rows = await api.authed("GET", f"/api/transactions?limit={limit}", chat_id)
    return fmt.transactions(rows)


async def status(chat_id: int, args: str) -> str:
    payload = await api.authed("GET", "/api/monitor/status", chat_id)
    return fmt.status(payload)


async def settings(chat_id: int, args: str) -> str:
    payload = await api.authed("GET", "/api/alerts/settings", chat_id)
    return fmt.settings(payload)


async def _patch(chat_id: int, changes: dict) -> dict:
    return await api.authed("PATCH", "/api/alerts/settings", chat_id, changes)


async def mute(chat_id: int, args: str) -> str:
    await _patch(chat_id, {"enabled": False})
    return "🔕 Alerts paused. Turn them back on with /unmute."


async def unmute(chat_id: int, args: str) -> str:
    await _patch(chat_id, {"enabled": True})
    return "🔔 Alerts resumed."


async def digest(chat_id: int, args: str) -> str:
    current = await api.authed("GET", "/api/alerts/settings", chat_id)
    now_on = not current.get("daily_digest")
    await _patch(chat_id, {"daily_digest": now_on})

    if now_on:
        return (
            "📬 Daily digest on.\n\n"
            "You will get a summary once a day even when nothing happened — so "
            "if it stops arriving, you know something is wrong rather than "
            "assuming your wallets are quiet."
        )
    return (
        "Daily digest off.\n\n"
        "Note that without it, a monitoring outage looks exactly like a quiet "
        "week."
    )


async def threshold(chat_id: int, args: str) -> str:
    raw = args.strip().lstrip("$")

    if not raw:
        return (
            "Give an amount, for example:\n"
            "<code>/threshold 5</code> — ignore incoming under $5\n"
            "<code>/threshold 0</code> — alert on everything"
        )

    try:
        amount = float(raw)
    except ValueError:
        return "That does not look like an amount. Try <code>/threshold 5</code>."

    if amount < 0:
        return "The amount cannot be negative."

    await _patch(chat_id, {"min_value_usd": amount})

    if amount == 0:
        return "Threshold cleared. You will be alerted on every amount."

    return (
        f"Incoming transfers under ${amount:,.2f} will no longer alert.\n\n"
        "Outgoing transfers still alert at any size — a small unexpected "
        "payment out is usually the one that matters most."
    )


HANDLERS = {
    "start": start,
    "help": help_command,
    "link": link,
    "stop": stop,
    "wallets": wallets,
    "recent": recent,
    "transactions": recent,
    "status": status,
    "settings": settings,
    "mute": mute,
    "unmute": unmute,
    "digest": digest,
    "threshold": threshold,
}


async def dispatch(chat_id: int, text: str) -> Optional[str]:
    """
    Route one message. Returns None when there is nothing to say.

    The account check lives here rather than in each handler, so a new command
    cannot forget it. Only the handlers in OPEN work without a linked account.
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return None

    head, _, args = text.partition(" ")

    # Group chats get commands as /wallets@MyBotName.
    command = head[1:].split("@")[0].lower()

    handler = HANDLERS.get(command)
    if handler is None:
        return "I do not know that command. /help lists what I can do."

    try:
        return await handler(chat_id, args)
    except api.NotLinked:
        return NOT_LINKED
    except RequestError as err:
        # Written-for-humans messages from our own API are safe to show.
        return f"❌ {fmt.escape(err.message)}"
    except Exception:
        # Never leak an internal error into a chat: it can carry an API key from
        # an upstream URL, and it is useless to the person reading it anyway.
        logger.exception("bot command failed: %s", command)
        return "Something went wrong on my side. Please try again in a moment."


# Commands usable before an account exists. Everything else answers with
# NOT_LINKED, which dispatch handles by catching api.NotLinked.
OPEN = {"start", "help", "link"}
