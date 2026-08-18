"""
app/api/routes/auth.py

Signup, login, and linking a Telegram chat to an account.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from app import db
from app.core import link_codes, security
from app.core.errors import RequestError

router = APIRouter(prefix="/api/auth", tags=["auth"])

# How long a link code lives. Long enough to switch apps and paste it, short
# enough that a leaked code is useless by the time anyone finds it.
CODE_TTL_MINUTES = 10

# Failed link attempts before a chat is locked out, and for how long. Without
# this, an attacker can simply enumerate codes — they do not need to know whose
# code they are guessing, since any valid one would work.
MAX_LINK_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    token: str
    user_id: str
    email: str


@router.post("/signup", response_model=TokenResponse)
async def signup(body: SignupRequest) -> TokenResponse:
    existing = await db.fetchrow("select id from users where email = $1", body.email.lower())
    if existing:
        raise RequestError("INVALID_REQUEST", "An account with that email already exists.")

    row = await db.fetchrow(
        """
        insert into users (email, password_hash, display_name)
        values ($1, $2, $3)
        returning id, email
        """,
        body.email.lower(),
        security.hash_password(body.password),
        body.display_name,
    )

    # Every user gets alert settings immediately, so the poller never has to
    # handle a missing row.
    await db.execute("insert into alert_settings (user_id) values ($1)", row["id"])

    return TokenResponse(
        token=security.issue_token(row["id"]), user_id=str(row["id"]), email=row["email"]
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    row = await db.fetchrow(
        "select id, email, password_hash from users where email = $1", body.email.lower()
    )

    # Same message whether the email is unknown or the password is wrong —
    # distinguishing them tells an attacker which emails have accounts.
    if not row or not security.verify_password(body.password, row["password_hash"]):
        raise RequestError("INVALID_REQUEST", "Email or password is incorrect.")

    return TokenResponse(
        token=security.issue_token(row["id"]), user_id=str(row["id"]), email=row["email"]
    )


@router.post("/logout")
async def logout(user_id: str = Depends(security.current_user)) -> dict:
    """
    Sessions are stateless JWTs with no revocation list, so there is nothing to
    invalidate server-side — logging out is the app discarding its token. This
    endpoint exists so that is an explicit call rather than something the app
    does silently, and so a call with an already-invalid token fails visibly.
    """
    return {"logged_out": True}


class LinkCodeResponse(BaseModel):
    code: str
    expires_in_seconds: int


@router.post("/telegram/code", response_model=LinkCodeResponse)
async def create_link_code(user_id: str = Depends(security.current_user)) -> LinkCodeResponse:
    """
    Issue a short-lived code the user sends to the bot as `/link ABCD-EFGH`.

    This exists so a password never has to be typed into a Telegram chat, where
    it would sit in message history indefinitely.
    """
    await db.execute("delete from telegram_link_codes where user_id = $1", user_id)

    code = link_codes.generate()
    expires = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)

    # Only the hash is stored. A database leak while a code is live would
    # otherwise let someone attach their own Telegram to this account.
    await db.execute(
        "insert into telegram_link_codes (code_hash, user_id, expires_at) values ($1, $2, $3)",
        link_codes.hash_code(code),
        user_id,
        expires,
    )

    return LinkCodeResponse(
        code=link_codes.for_display(code),
        expires_in_seconds=CODE_TTL_MINUTES * 60,
    )


class LinkRequest(BaseModel):
    code: str
    chat_id: int


async def _check_not_locked(chat_id: int) -> None:
    row = await db.fetchrow(
        "select locked_until from telegram_link_attempts where chat_id = $1", chat_id
    )

    if row and row["locked_until"] and row["locked_until"] > datetime.now(timezone.utc):
        raise RequestError(
            "RATE_LIMITED",
            "Too many incorrect codes. Please wait a few minutes and try again.",
        )


async def _record_failure(chat_id: int) -> None:
    row = await db.fetchrow(
        """
        insert into telegram_link_attempts (chat_id, failed_count)
        values ($1, 1)
        on conflict (chat_id) do update
            set failed_count = telegram_link_attempts.failed_count + 1
        returning failed_count
        """,
        chat_id,
    )

    if row["failed_count"] >= MAX_LINK_ATTEMPTS:
        await db.execute(
            """
            update telegram_link_attempts
            set locked_until = now() + ($2 || ' minutes')::interval,
                failed_count = 0
            where chat_id = $1
            """,
            chat_id,
            str(LOCKOUT_MINUTES),
        )


@router.post("/telegram/link")
async def link_telegram(body: LinkRequest) -> dict:
    """
    Called by the bot, not the app. Exchanges a code for a permanent link.

    Unauthenticated by design — the bot has no user token. The code itself is
    the credential, which is why it is hashed at rest, expires in ten minutes,
    is deleted on use, and why failed attempts are rate limited.
    """
    await _check_not_locked(body.chat_id)

    code_hash = link_codes.hash_code(body.code)

    row = await db.fetchrow(
        "select user_id, expires_at from telegram_link_codes where code_hash = $1",
        code_hash,
    )

    if not row:
        await _record_failure(body.chat_id)
        raise RequestError("INVALID_REQUEST", "That code is not valid.")

    if row["expires_at"] < datetime.now(timezone.utc):
        await db.execute("delete from telegram_link_codes where code_hash = $1", code_hash)
        raise RequestError("INVALID_REQUEST", "That code has expired. Generate a new one.")

    await db.execute(
        """
        insert into telegram_links (user_id, chat_id) values ($1, $2)
        on conflict (user_id) do update
            set chat_id = excluded.chat_id, linked_at = now()
        """,
        row["user_id"],
        body.chat_id,
    )

    await db.execute("delete from telegram_link_codes where code_hash = $1", code_hash)
    await db.execute("delete from telegram_link_attempts where chat_id = $1", body.chat_id)

    return {"linked": True}
