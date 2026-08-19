"""
bot/main.py

The Telegram bot: a long-poll loop that reads messages and answers them.

Long polling rather than a webhook, for two reasons. It needs no public URL or
registration step, so it works identically on a laptop and on Render. And the
service is already kept awake by the monitor loop, which is the condition a
webhook would need anyway.

The bot does NOT deliver alerts — the poller already does that directly, in
app/services/monitor.py. This is the command surface: linking, listing, and
changing settings.
"""

import asyncio
import logging
import os
from typing import Optional

from app.core.errors import RequestError
from app.core.http import fetch_json

from . import commands

logger = logging.getLogger("walletnest.bot")

API_BASE = "https://api.telegram.org"

# How long Telegram holds a request open when there is nothing new. Long enough
# that the loop is nearly free when idle, short enough to notice a shutdown.
LONG_POLL_SECONDS = 25


def token() -> Optional[str]:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None


async def _call(method: str, payload: dict, timeout: float = 40.0):
    return await fetch_json(
        f"{API_BASE}/bot{token()}/{method}",
        method="POST",
        json_body=payload,
        label=f"telegram/{method}",
        timeout=timeout,
        retries=1,
    )


async def send(chat_id: int, text: str) -> bool:
    try:
        await _call("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        return True
    except RequestError as err:
        logger.warning("could not reply to %s: %s", chat_id, err)
        return False


async def set_commands() -> None:
    """
    Register the command list so Telegram shows a menu.

    Best effort: failing here should not stop the bot from working, it just
    means users type commands without autocomplete.
    """
    menu = [
        ("link", "Connect this chat to your account"),
        ("wallets", "Wallets I am watching"),
        ("recent", "Latest transactions"),
        ("status", "Is monitoring working"),
        ("settings", "Your alert preferences"),
        ("mute", "Pause alerts"),
        ("unmute", "Resume alerts"),
        ("digest", "Toggle the daily summary"),
        ("threshold", "Ignore small incoming amounts"),
        ("stop", "Disconnect this chat"),
        ("help", "What I can do"),
    ]
    try:
        await _call("setMyCommands", {
            "commands": [{"command": c, "description": d} for c, d in menu]
        })
    except RequestError as err:
        logger.warning("could not register command menu: %s", err)


async def handle_update(update: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")

    if not chat_id or not text:
        return

    reply = await commands.dispatch(chat_id, text)
    if reply:
        await send(chat_id, reply)


async def run_forever() -> None:
    """
    Poll for updates until cancelled.

    `offset` is what makes this safe to restart: Telegram holds each update
    until it is acknowledged by asking for the next id, so a crash mid-handling
    means the message is redelivered rather than lost.
    """
    if not token():
        logger.info("bot not started: TELEGRAM_BOT_TOKEN is not set")
        return

    await set_commands()
    logger.info("bot started, long-polling for updates")

    offset: Optional[int] = None
    backoff = 1.0

    while True:
        try:
            payload = {"timeout": LONG_POLL_SECONDS, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset

            result = await _call(
                "getUpdates", payload, timeout=LONG_POLL_SECONDS + 15
            )
            updates = (result or {}).get("result") or []
            backoff = 1.0

            for update in updates:
                # Advance the offset BEFORE handling. A message that crashes a
                # handler would otherwise be redelivered forever, and the bot
                # would never get past it.
                offset = update["update_id"] + 1
                try:
                    await handle_update(update)
                except Exception:
                    logger.exception("update %s failed", update.get("update_id"))

        except asyncio.CancelledError:
            logger.info("bot stopping")
            raise

        except Exception:
            logger.exception("poll failed; retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
