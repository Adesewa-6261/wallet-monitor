"""
End-to-end tests for the bot's command surface.

The link code exists so a password never has to be typed into a Telegram chat.
It is not what the bot depends on — the bot depends on a row in
`telegram_links`. So these tests insert that row directly and skip the code
exchange entirely, which is what makes the interesting commands testable
without a browser, a ten-minute expiry, or a human pasting a code.

The exchange itself is covered too, at the bottom of the file, by writing the
hashed code row the way POST /api/auth/telegram/code would — so even /link
needs no browser and no human. Between the two halves every handler in
commands.HANDLERS is exercised.

The API is mounted in-process over ASGI rather than reached over the network,
so no server has to be running and — importantly — the FastAPI lifespan never
fires, which keeps the monitor loop and the real Telegram long-poll off. Every
route the bot touches reads only Postgres, so nothing here calls a chain
provider or Telegram.

A live database IS required, since inserting the link is the entire point. The
fixture creates its own throwaway account and deletes it afterwards, touching
no row it did not create — but point DATABASE_URL at a scratch database if you
would rather it stayed nowhere near real data.
"""

import secrets
from datetime import datetime, timedelta, timezone

import pytest

try:
    import httpx
    import pytest_asyncio

    from app import db
    from app.core import link_codes, security
    from app.main import app as fastapi_app
    from bot import api, commands
except RuntimeError as err:  # config validates the environment at import
    pytest.skip(f"environment not configured: {err}", allow_module_level=True)

# One event loop for the module, so the asyncpg pool created by the first test
# is still usable by the last one. A function-scoped loop would leave every
# test after the first holding a pool bound to a closed loop.
pytestmark = pytest.mark.asyncio(loop_scope="module")

# Ids Telegram would not issue, so a stray row can never collide with a real
# linked chat — chat_id is unique, and clobbering it would silently move a real
# user's alerts.
LINKED_CHAT = 8_800_000_001
UNLINKED_CHAT = 8_800_000_002
# Separate, because the lockout test deliberately leaves this one locked out.
LOCKOUT_CHAT = 8_800_000_003

# Captured before the patch below replaces the name, because the patch is on
# the httpx module itself — without this, _client would call itself.
REAL_ASYNC_CLIENT = httpx.AsyncClient

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
COUNTERPARTY = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def _client(**kwargs) -> httpx.AsyncClient:
    """
    An httpx client that speaks to the app object instead of the network.

    raise_app_exceptions=False because Starlette re-raises after running an
    error handler. Without it a 500 would explode inside the test instead of
    arriving at the bot as the error envelope a real deployment would send,
    which is precisely the path worth checking.
    """
    return REAL_ASYNC_CLIENT(
        transport=httpx.ASGITransport(app=fastapi_app, raise_app_exceptions=False),
        **kwargs,
    )


async def _link(user_id, chat_id: int = LINKED_CHAT) -> None:
    """Attach a chat to an account the way /link would, minus the code."""
    await db.execute(
        """
        insert into telegram_links (user_id, chat_id) values ($1, $2)
        on conflict (user_id) do update
            set chat_id = excluded.chat_id, linked_at = now()
        """,
        user_id,
        chat_id,
    )
    # The bot caches a token per chat; a stale entry would outlive the change.
    api.forget(chat_id)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def account(monkeypatch_module):
    """A user with one wallet, two transactions, and a linked chat."""
    try:
        await db.fetchrow("select 1")
    except Exception as err:  # unreachable database, bad URL, sleeping Neon
        pytest.skip(f"database not reachable: {err}")

    user = await db.fetchrow(
        """
        insert into users (email, password_hash, display_name)
        values ($1, $2, $3)
        returning id
        """,
        f"bot-test-{secrets.token_hex(6)}@walletnest.test",
        # A real hash of a password nobody holds: the account exists but cannot
        # be logged into, and no login path is exercised here anyway.
        security.hash_password(secrets.token_urlsafe(24)),
        "Bot test account",
    )
    user_id = user["id"]

    try:
        await db.execute("insert into alert_settings (user_id) values ($1)", user_id)

        wallet = await db.fetchrow(
            """
            insert into wallets (user_id, label, input, input_type, chain, address_type)
            values ($1, 'Bot test wallet', $2, 'address', 'bitcoin', 'Native SegWit')
            returning id
            """,
            user_id,
            ADDRESS,
        )
        wallet_id = wallet["id"]

        # Synced just now, so /status has something healthy to report. Left
        # stale, every run would report a fault and the healthy path would
        # never be covered.
        await db.execute(
            """
            insert into wallet_sync_state (wallet_id, last_block, last_synced_at)
            values ($1, 870000, now())
            """,
            wallet_id,
        )

        now = datetime.now(timezone.utc)
        await db.executemany(
            """
            insert into transactions (
                wallet_id, tx_hash, chain, direction, symbol, amount,
                counterparty, block_time, value_usd, alerted_at
            )
            values ($1, $2, 'bitcoin', $3, 'BTC', $4, $5, $6, $7, now())
            """,
            [
                (wallet_id, "a" * 64, "receive", "0.00500000", COUNTERPARTY,
                 now - timedelta(minutes=5), 320.55),
                (wallet_id, "b" * 64, "send", "0.00120000", COUNTERPARTY,
                 now - timedelta(hours=3), 76.93),
            ],
        )

        await _link(user_id)

        yield {"user_id": user_id, "wallet_id": wallet_id}
    finally:
        # Wallets, transactions, settings and the link all cascade from the user.
        await db.execute("delete from users where id = $1", user_id)
        await db.execute(
            "delete from telegram_link_attempts where chat_id = any($1::bigint[])",
            [LINKED_CHAT, UNLINKED_CHAT, LOCKOUT_CHAT],
        )
        for chat in (LINKED_CHAT, UNLINKED_CHAT, LOCKOUT_CHAT):
            api.forget(chat)

        pool = await db.get_pool()
        await pool.close()
        db._pool = None


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def monkeypatch_module():
    """
    Module-scoped monkeypatch: routes the bot's HTTP at the app, and makes sure
    a bot secret exists. Both sides read BOT_SHARED_SECRET from the environment
    at call time, so setting one here keeps them agreeing even on a machine
    where it was never configured.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(api.httpx, "AsyncClient", _client)
        if not api.bot_secret():
            patch.setenv("BOT_SHARED_SECRET", secrets.token_urlsafe(16))
        yield patch


async def _settings_text() -> str:
    return await commands.dispatch(LINKED_CHAT, "/settings")


# --------------------------------------------------------------- no account


async def test_help_works_without_an_account(account):
    reply = await commands.dispatch(UNLINKED_CHAT, "/help")
    assert "WalletNest" in reply
    assert "/link CODE" in reply


async def test_unknown_command_is_answered_not_ignored(account):
    reply = await commands.dispatch(UNLINKED_CHAT, "/nonsense")
    assert "/help" in reply


async def test_non_command_text_is_ignored(account):
    assert await commands.dispatch(UNLINKED_CHAT, "hello there") is None


async def test_unlinked_chat_is_told_how_to_link(account):
    # The gate that matters: an unlinked chat must never see another account's
    # data, and must be told what to do rather than shown an error.
    reply = await commands.dispatch(UNLINKED_CHAT, "/wallets")
    assert reply == commands.NOT_LINKED


async def test_start_on_an_unlinked_chat_explains_linking(account):
    reply = await commands.dispatch(UNLINKED_CHAT, "/start")
    assert "Welcome to WalletNest" in reply
    assert "/link ABCD-EFGH" in reply


async def test_start_recognises_a_linked_chat(account):
    reply = await commands.dispatch(LINKED_CHAT, "/start")
    assert "already connected" in reply


# ------------------------------------------------------------ reading state


async def test_wallets_lists_what_is_watched(account):
    reply = await commands.dispatch(LINKED_CHAT, "/wallets")
    assert "Bot test wallet" in reply
    assert "Bitcoin · address" in reply
    # Truncated for display, but with enough of the head to defeat a lookalike.
    assert ADDRESS[:12] in reply


async def test_recent_shows_both_directions(account):
    reply = await commands.dispatch(LINKED_CHAT, "/recent")
    assert "Received 0.00500000 BTC" in reply
    assert "Sent 0.00120000 BTC" in reply
    assert "$320.55" in reply
    assert f"mempool.space/tx/{'a' * 64}" in reply


async def test_recent_honours_a_limit(account):
    reply = await commands.dispatch(LINKED_CHAT, "/recent 1")
    assert reply.count("📥") + reply.count("📤") == 1
    # Newest first, so the five-minute-old receive is the one that survives.
    assert "Received" in reply


async def test_transactions_is_an_alias_for_recent(account):
    assert await commands.dispatch(LINKED_CHAT, "/transactions") == await (
        commands.dispatch(LINKED_CHAT, "/recent")
    )


async def test_status_reports_a_freshly_synced_wallet_as_healthy(account):
    reply = await commands.dispatch(LINKED_CHAT, "/status")
    assert "Monitoring is healthy" in reply
    assert "All 1 wallet checked recently" in reply


# --------------------------------------------------------- changing settings


async def test_mute_and_unmute_round_trip(account):
    assert "paused" in await commands.dispatch(LINKED_CHAT, "/mute")
    assert "Alerts: <b>off</b>" in await _settings_text()

    assert "resumed" in await commands.dispatch(LINKED_CHAT, "/unmute")
    assert "Alerts: <b>on</b>" in await _settings_text()


async def test_threshold_sets_and_clears(account):
    reply = await commands.dispatch(LINKED_CHAT, "/threshold 5")
    assert "$5.00" in reply
    assert "Ignore incoming under $5.00" in await _settings_text()

    # A dollar sign is what a person types; it must not become an error.
    await commands.dispatch(LINKED_CHAT, "/threshold $12.50")
    assert "Ignore incoming under $12.50" in await _settings_text()

    assert "cleared" in await commands.dispatch(LINKED_CHAT, "/threshold 0")
    assert "Alert on every amount" in await _settings_text()


async def test_threshold_rejects_nonsense_without_touching_settings(account):
    assert "does not look like an amount" in await commands.dispatch(
        LINKED_CHAT, "/threshold soon"
    )
    assert "cannot be negative" in await commands.dispatch(LINKED_CHAT, "/threshold -1")
    assert "Alert on every amount" in await _settings_text()


async def test_digest_toggles_and_toggles_back(account):
    before = "Daily digest: on" in await _settings_text()

    await commands.dispatch(LINKED_CHAT, "/digest")
    assert ("Daily digest: on" in await _settings_text()) is not before

    await commands.dispatch(LINKED_CHAT, "/digest")
    assert ("Daily digest: on" in await _settings_text()) is before


async def test_group_chat_command_suffix_is_stripped(account):
    # Telegram delivers /wallets@WalletNestBot in a group.
    reply = await commands.dispatch(LINKED_CHAT, "/wallets@WalletNestBot")
    assert "Bot test wallet" in reply


# ------------------------------------------------------------------ /stop


async def test_stop_unlinks_and_relinking_restores_access(account):
    assert "Disconnected" in await commands.dispatch(LINKED_CHAT, "/stop")

    # The row is gone, so the cached token must not keep the chat working.
    assert await commands.dispatch(LINKED_CHAT, "/wallets") == commands.NOT_LINKED
    assert "This chat was not connected" in await commands.dispatch(LINKED_CHAT, "/stop")

    # Re-insert exactly as the fixture did, and the chat is live again. Left
    # linked so this test does not depend on running last.
    await _link(account["user_id"])
    assert "Bot test wallet" in await commands.dispatch(LINKED_CHAT, "/wallets")


# ------------------------------------------------------------------- /link
#
# The bot's half of the exchange, without a browser. POST /api/auth/telegram/code
# stores a hash and hands the plaintext to the app; nothing stops a test from
# writing that row itself and keeping the plaintext, which is what makes the
# success path reachable here at all.


async def _issue_code(user_id, minutes: int = 10) -> str:
    code = link_codes.generate()
    await db.execute(
        """
        insert into telegram_link_codes (code_hash, user_id, expires_at)
        values ($1, $2, now() + ($3 || ' minutes')::interval)
        """,
        link_codes.hash_code(code),
        user_id,
        str(minutes),
    )
    return code


async def test_link_without_a_code_shows_the_format(account):
    reply = await commands.dispatch(UNLINKED_CHAT, "/link")
    assert "/link ABCD-EFGH" in reply


async def test_link_with_an_unknown_code_is_refused(account):
    reply = await commands.dispatch(UNLINKED_CHAT, "/link ZZZZ-ZZZZ")
    assert "not valid" in reply
    assert await commands.dispatch(UNLINKED_CHAT, "/wallets") == commands.NOT_LINKED


async def test_link_with_an_expired_code_says_expired_not_invalid(account):
    # The distinction matters: "expired" tells the user to generate another,
    # "not valid" sends them hunting for a typo that is not there.
    code = await _issue_code(account["user_id"], minutes=-1)

    reply = await commands.dispatch(UNLINKED_CHAT, f"/link {code}")
    assert "expired" in reply

    # Dropped on the way past, so a stale code cannot linger and be retried.
    left = await db.fetchrow(
        "select 1 from telegram_link_codes where code_hash = $1",
        link_codes.hash_code(code),
    )
    assert left is None


async def test_repeated_wrong_codes_lock_the_chat_out(account):
    # An attacker does not need to know whose code they are guessing — any
    # valid code from any account would link them. The attempt cap is the only
    # thing standing in front of that, so it is worth the five round trips.
    for _ in range(5):
        assert "not valid" in await commands.dispatch(LOCKOUT_CHAT, "/link ZZZZ-ZZZZ")

    # Even a genuine code is refused once locked.
    code = await _issue_code(account["user_id"])
    reply = await commands.dispatch(LOCKOUT_CHAT, f"/link {code}")
    assert "Too many incorrect codes" in reply

    await db.execute(
        "delete from telegram_link_codes where code_hash = $1", link_codes.hash_code(code)
    )


async def test_link_with_a_real_code_connects_the_chat(account):
    code = await _issue_code(account["user_id"])

    # Typed the way a person actually sends it: lower case, display hyphen kept.
    typed = link_codes.for_display(code).lower()
    assert "Connected" in await commands.dispatch(UNLINKED_CHAT, f"/link {typed}")

    assert "Bot test wallet" in await commands.dispatch(UNLINKED_CHAT, "/wallets")

    # Single use — the row is consumed, so an overheard code cannot be replayed.
    left = await db.fetchrow(
        "select 1 from telegram_link_codes where code_hash = $1",
        link_codes.hash_code(code),
    )
    assert left is None

    # One chat per account, so this upsert moves the link back and leaves the
    # module as it found it.
    await _link(account["user_id"], LINKED_CHAT)
    api.forget(UNLINKED_CHAT)
    assert await commands.dispatch(UNLINKED_CHAT, "/wallets") == commands.NOT_LINKED
