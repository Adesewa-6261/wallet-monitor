"""
bot/api.py

The bot's client for the WalletNest API.

The bot is not a user. It holds no password and no token of its own — it proves
itself to the API with a shared secret and asks for a token on behalf of a chat
that has already completed the link flow. That is the whole reason
POST /api/auth/telegram/session exists.

Tokens are cached per chat rather than fetched per command, because a chatty
user would otherwise mint one on every keystroke. They are dropped on the first
401 so an expired one self-heals instead of wedging the chat.
"""

import os
import time
from typing import Any, Optional

import httpx

from app.core.errors import RequestError


def api_base() -> str:
    # Defaults to the local app, which is where it runs when the bot is hosted
    # in-process with the API.
    return os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def bot_secret() -> str:
    return os.environ.get("BOT_SHARED_SECRET", "").strip()


# chat_id -> (token, expires_at). The API issues 30-day tokens; we refresh well
# inside that so a command never fails on a boundary.
_tokens: dict[int, tuple[str, float]] = {}
TOKEN_CACHE_SECONDS = 60 * 60


class NotLinked(Exception):
    """This chat has no account attached yet."""


async def _request(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    json_body: Any = None,
    headers: Optional[dict] = None,
) -> Any:
    """
    Call our own API and surface its error messages.

    Deliberately NOT core.http.fetch_json. That helper strips provider response
    bodies out of errors, because an upstream provider's error text can echo
    back a request URL containing our API key. That protection is right for
    third parties and wrong here: this API is ours, its messages are written
    for a person to read, and swallowing them leaves the bot saying "request
    failed (400)" where it should say "that code has expired".
    """
    all_headers = {"Accept": "application/json", **(headers or {})}
    if token:
        all_headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.request(
                method, f"{api_base()}{path}", json=json_body, headers=all_headers
            )
        except httpx.HTTPError as err:
            raise RequestError(
                "UPSTREAM_UNAVAILABLE",
                "I could not reach the WalletNest service. Please try again shortly.",
            ) from err

    if response.status_code < 400:
        return response.json()

    # Our error envelope is {"error": {"code", "message"}}; FastAPI's own
    # validation failures use {"detail": [...]} instead.
    try:
        body = response.json()
    except ValueError:
        body = {}

    error = body.get("error") or {}
    message = error.get("message")

    if not message:
        message = "That request was not valid." if response.status_code < 500 \
            else "The WalletNest service had a problem. Please try again."

    raise RequestError(error.get("code") or "INVALID_REQUEST", message)


async def token_for(chat_id: int) -> str:
    """A user token for this chat, minting one if the cache is cold or stale."""
    cached = _tokens.get(chat_id)
    if cached and cached[1] > time.time():
        return cached[0]

    try:
        result = await _request(
            "POST",
            "/api/auth/telegram/session",
            json_body={"chat_id": chat_id},
            headers={"X-Bot-Secret": bot_secret()},
        )
    except RequestError as err:
        # The API cannot distinguish "not linked" from other 400s in its error
        # code, so the message is what tells us. Anything else is a real fault
        # and should surface rather than be reported as "not linked".
        if "not linked" in str(err).lower():
            raise NotLinked() from err
        raise

    token = result["token"]
    _tokens[chat_id] = (token, time.time() + TOKEN_CACHE_SECONDS)
    return token


def forget(chat_id: int) -> None:
    _tokens.pop(chat_id, None)


async def authed(method: str, path: str, chat_id: int, json_body: Any = None) -> Any:
    """Call an authenticated endpoint, retrying once with a fresh token."""
    token = await token_for(chat_id)
    try:
        return await _request(method, path, token=token, json_body=json_body)
    except RequestError as err:
        if "session" in str(err).lower() or "sign in" in str(err).lower():
            forget(chat_id)
            token = await token_for(chat_id)
            return await _request(method, path, token=token, json_body=json_body)
        raise


# ------------------------------------------------------------ unauthenticated


async def link(code: str, chat_id: int) -> dict:
    return await _request(
        "POST",
        "/api/auth/telegram/link",
        json_body={"code": code, "chat_id": chat_id},
    )


async def unlink(chat_id: int) -> dict:
    forget(chat_id)
    return await _request(
        "POST",
        "/api/auth/telegram/unlink",
        json_body={"chat_id": chat_id},
        headers={"X-Bot-Secret": bot_secret()},
    )
