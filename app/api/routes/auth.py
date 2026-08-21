"""
app/api/routes/auth.py

Signup, login, password changes, and linking a Telegram chat to an account.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field

from app import db
from app.adapters import telegram
from app.core import link_codes, security
from app.core.errors import RequestError

logger = logging.getLogger("walletnest.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# How long a link code lives. Long enough to switch apps and paste it, short
# enough that a leaked code is useless by the time anyone finds it.
CODE_TTL_MINUTES = 10

# Failed link attempts before a chat is locked out, and for how long. Without
# this, an attacker can simply enumerate codes — they do not need to know whose
# code they are guessing, since any valid one would work.
MAX_LINK_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

# The same limits for password guessing at /login. Kept as separate constants
# rather than shared ones: they happen to match today, but the two endpoints
# protect different things and either may need tuning without the other.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


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


async def _check_login_not_locked(email: str) -> None:
    row = await db.fetchrow(
        "select locked_until from login_attempts where email = $1", email
    )

    if row and row["locked_until"] and row["locked_until"] > datetime.now(timezone.utc):
        raise RequestError(
            "RATE_LIMITED",
            "Too many incorrect sign-in attempts. Please wait a few minutes and try again.",
        )


async def _record_login_failure(email: str) -> None:
    """
    Count a failed sign-in, whether or not that email has an account.

    Recording misses for unknown emails too is deliberate: only counting real
    accounts would make the lockout itself an oracle for which emails exist.
    """
    row = await db.fetchrow(
        """
        insert into login_attempts (email, failed_count)
        values ($1, 1)
        on conflict (email) do update
            set failed_count = login_attempts.failed_count + 1
        returning failed_count
        """,
        email,
    )

    if row["failed_count"] >= MAX_LOGIN_ATTEMPTS:
        await db.execute(
            """
            update login_attempts
            set locked_until = now() + ($2 || ' minutes')::interval,
                failed_count = 0
            where email = $1
            """,
            email,
            str(LOGIN_LOCKOUT_MINUTES),
        )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    """
    Locked out for LOGIN_LOCKOUT_MINUTES after MAX_LOGIN_ATTEMPTS wrong
    passwords, the same way telegram/link is. Without it the endpoint can be
    guessed at indefinitely, and a password is worth more than a link code.
    """
    email = body.email.lower()
    await _check_login_not_locked(email)

    row = await db.fetchrow(
        "select id, email, password_hash from users where email = $1", email
    )

    # Same message whether the email is unknown or the password is wrong —
    # distinguishing them tells an attacker which emails have accounts.
    if not row or not security.verify_password(body.password, row["password_hash"]):
        await _record_login_failure(email)
        raise RequestError("INVALID_REQUEST", "Email or password is incorrect.")

    # A correct password clears the count, so someone who mistypes twice and
    # then gets it right does not carry those failures into next week.
    await db.execute("delete from login_attempts where email = $1", email)

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


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


async def _notify_password_changed(user_id: str) -> None:
    """
    Tell the user's Telegram that their password changed.

    Best effort, and deliberately after the update has committed: the password
    has already changed by this point, so a Telegram outage must not turn a
    successful change into an error the user would retry.

    Users with no linked chat simply get nothing.
    """
    try:
        row = await db.fetchrow(
            "select chat_id from telegram_links where user_id = $1", user_id
        )
        if row:
            await telegram.send_message(row["chat_id"], telegram.format_password_changed())
    except Exception:
        logger.exception("could not send password-change notice for user %s", user_id)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user_id: str = Depends(security.current_user),
) -> dict:
    """
    Requiring the current password stops a stolen but still-valid token from
    being enough on its own to lock the real owner out permanently.

    No lockout on repeated failures here, unlike telegram/link below — that
    endpoint is reachable by guessing a code with no prior auth, this one
    already requires a valid bearer token, which is a smaller attack surface.

    Other devices stay signed in until their token expires on its own — there
    is no mechanism to revoke one, so nothing here would change that.
    """
    row = await db.fetchrow("select password_hash from users where id = $1", user_id)

    if not row or not security.verify_password(body.current_password, row["password_hash"]):
        raise RequestError("INVALID_REQUEST", "Current password is incorrect.")

    await db.execute(
        "update users set password_hash = $1 where id = $2",
        security.hash_password(body.new_password),
        user_id,
    )

    await _notify_password_changed(user_id)

    return {"changed": True}


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


# --------------------------------------------------------------- reset

# A reset code lives briefly: it is the whole credential for changing a
# password, so its window is deliberately shorter than a login link's.
RESET_TTL_MINUTES = 10
MAX_RESET_ATTEMPTS = 5


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirm(BaseModel):
    email: EmailStr
    code: str
    new_password: str = Field(min_length=8)


@router.post("/reset/request")
async def request_password_reset(body: ResetRequest) -> dict:
    """
    Send a reset code to the account's linked Telegram chat.

    Always answers the same way. Whether that email has an account, and whether
    it has Telegram linked, are both things an anonymous caller should not be
    able to learn by watching the response.

    The code travels *out* to Telegram and is typed back into the app. Doing it
    the other way — accepting a new password sent as a chat message — would
    leave that password sitting in the conversation history forever.
    """
    same_answer = {"sent": True}
    email = body.email.lower()

    row = await db.fetchrow(
        """
        select u.id, t.chat_id
        from users u
        left join telegram_links t on t.user_id = u.id
        where u.email = $1
        """,
        email,
    )

    if not row or not row["chat_id"]:
        return same_answer

    code = link_codes.generate()

    # Replacing any previous code is what makes a leaked one harmless: the real
    # owner asking again is enough to invalidate it.
    await db.execute(
        """
        insert into password_reset_codes (user_id, code_hash, expires_at, attempts)
        values ($1, $2, $3, 0)
        on conflict (user_id) do update
            set code_hash = excluded.code_hash,
                expires_at = excluded.expires_at,
                attempts = 0,
                created_at = now()
        """,
        row["id"],
        link_codes.hash_code(code),
        datetime.now(timezone.utc) + timedelta(minutes=RESET_TTL_MINUTES),
    )

    try:
        await telegram.send_message(
            row["chat_id"],
            telegram.format_reset_code(link_codes.for_display(code), RESET_TTL_MINUTES),
        )
    except Exception:
        # The code is stored either way. Reporting a delivery failure here would
        # confirm the account exists, so it is logged and not surfaced.
        logger.exception("could not deliver reset code for user %s", row["id"])

    return same_answer


@router.post("/reset/confirm")
async def confirm_password_reset(body: ResetConfirm) -> dict:
    """
    Exchange a reset code for a new password.

    Unauthenticated by design — someone resetting a password cannot log in to
    prove who they are. The code is the credential, which is why it is hashed at
    rest, expires, is destroyed on use, and is only guessable a few times.
    """
    email = body.email.lower()
    invalid = RequestError("INVALID_REQUEST", "That code is not valid or has expired.")

    user = await db.fetchrow("select id from users where email = $1", email)
    if not user:
        raise invalid

    row = await db.fetchrow(
        "select code_hash, expires_at, attempts from password_reset_codes where user_id = $1",
        user["id"],
    )
    if not row:
        raise invalid

    if row["expires_at"] < datetime.now(timezone.utc):
        await db.execute("delete from password_reset_codes where user_id = $1", user["id"])
        raise invalid

    if not link_codes.matches(body.code, row["code_hash"]):
        attempts = row["attempts"] + 1
        if attempts >= MAX_RESET_ATTEMPTS:
            # Burn the code rather than locking the account. The person guessing
            # is not necessarily the owner, and locking would let an attacker
            # keep someone out just by guessing badly.
            await db.execute(
                "delete from password_reset_codes where user_id = $1", user["id"]
            )
        else:
            await db.execute(
                "update password_reset_codes set attempts = $2 where user_id = $1",
                user["id"],
                attempts,
            )
        raise invalid

    await db.execute(
        "update users set password_hash = $1 where id = $2",
        security.hash_password(body.new_password),
        user["id"],
    )

    await db.execute("delete from password_reset_codes where user_id = $1", user["id"])

    # Existing sessions are not revoked, because nothing here can revoke them.
    # Say so plainly rather than implying a reset locks anyone else out.
    await _notify_password_changed(user["id"])

    return {"reset": True}
