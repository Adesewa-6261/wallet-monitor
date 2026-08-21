"""
app/core/security.py

Signup, login and request authentication.

Passwords are hashed with bcrypt, never stored or logged in plain text. Sessions
are stateless JWTs, so a request can be verified without a database round trip.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import db
from .config import config
from .errors import RequestError

# How long a session lasts. Ninety days rather than thirty because the app is
# opened rarely and sits behind its own passcode, and rather than never because
# there is no revocation list: a token that does not expire is permanent access
# for whoever ends up holding the phone, and what this app displays — every
# address someone owns and what it is worth — is exactly what makes that
# dangerous. Forgetting a password is a separate problem, answered by the
# Telegram reset rather than by leaving sessions open forever.
TOKEN_TTL_DAYS = 90

# bcrypt hashes at most 72 bytes and silently ignores the rest, so a long
# passphrase would be truncated without anyone noticing. Rejecting is better
# than quietly weakening the password.
MAX_PASSWORD_BYTES = 72

# Declaring the scheme is what puts the Authorize button in /docs and marks the
# protected endpoints with a padlock.
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise RequestError(
            "INVALID_REQUEST",
            "Password is too long. Please use 72 characters or fewer.",
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode("utf-8")[:MAX_PASSWORD_BYTES]
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database. Treat as a failed login rather than a
        # server error.
        return False


def issue_token(user_id: str) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, config.jwt_secret, algorithm="HS256")


def read_token(token: str) -> str:
    """Return the user id from a token, or raise if it is invalid or expired."""
    try:
        payload = jwt.decode(token, config.jwt_secret, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise RequestError(
            "INVALID_REQUEST", "Your session has expired. Please sign in again."
        )
    except jwt.PyJWTError:
        raise RequestError("INVALID_REQUEST", "Invalid session.")


async def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """
    FastAPI dependency. Pulls the user id out of the Authorization header.

    Every wallet and transaction query filters on this id — it is what keeps one
    user's wallets invisible to another.
    """
    if not credentials:
        raise RequestError("INVALID_REQUEST", "Not signed in.")
    return read_token(credentials.credentials)


def new_link_code() -> str:
    """
    Six digits, for linking a Telegram chat to an account.

    secrets rather than random: this is a credential, however short-lived, and a
    predictable code would let someone else attach their chat to an account.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def user_from_chat_id(chat_id: int) -> Optional[str]:
    """Resolve a Telegram chat to the account it is linked to."""
    row = await db.fetchrow(
        "select user_id from telegram_links where chat_id = $1", chat_id
    )
    return str(row["user_id"]) if row else None