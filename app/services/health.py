"""
app/services/health.py

The daily "still working" message.

This exists because of a failure mode specific to monitoring products: when the
poller stops, nothing happens — which looks exactly like nothing happening. A
user seeing no alerts cannot tell whether their wallets are quiet or whether we
stopped watching two days ago.

So we send a message even when there is nothing to report. Silence then becomes
a signal in itself: if the daily digest does not arrive, something is wrong.
"""

import logging
from datetime import datetime, timedelta, timezone

from .. import db
from ..adapters import telegram

logger = logging.getLogger("walletnest.health")

# A wallet that has not synced in this long is stale, whether or not it recorded
# an error — a hung request leaves no error but stops progress.
STALE_AFTER_MINUTES = 30

DIGEST_INTERVAL_HOURS = 24


def _humanise(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


async def send_digests() -> int:
    """
    Send a health summary to any user due one. Returns how many were sent.

    Called from the poll cycle rather than on its own timer, so there is one
    loop to reason about.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=DIGEST_INTERVAL_HOURS)

    users = await db.fetch(
        """
        select u.id, tl.chat_id
        from users u
        join telegram_links tl on tl.user_id = u.id
        join alert_settings a on a.user_id = u.id
        where a.daily_digest
          and (a.last_digest_at is null or a.last_digest_at < $1)
        """,
        cutoff,
    )

    sent = 0
    for user in users:
        wallets = await db.fetch(
            """
            select w.label, w.chain, s.last_synced_at, s.last_error
            from wallets w
            left join wallet_sync_state s on s.wallet_id = w.id
            where w.user_id = $1
            order by w.created_at
            """,
            user["id"],
        )

        if not wallets:
            continue

        counts = await db.fetchrow(
            """
            select count(*) as total,
                   count(*) filter (where direction = 'receive') as received,
                   count(*) filter (where direction = 'send') as sent
            from transactions t
            join wallets w on w.id = t.wallet_id
            where w.user_id = $1 and t.block_time > $2
            """,
            user["id"],
            cutoff,
        )

        text = _build_digest(wallets, counts)

        if await telegram.send_message(user["chat_id"], text):
            await db.execute(
                "update alert_settings set last_digest_at = now() where user_id = $1",
                user["id"],
            )
            sent += 1

    return sent


def _build_digest(wallets: list, counts) -> str:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=STALE_AFTER_MINUTES)

    healthy = []
    problems = []

    for wallet in wallets:
        label = wallet["label"] or f"{wallet['chain'].title()} wallet"
        synced = wallet["last_synced_at"]

        if wallet["last_error"]:
            problems.append(f"• {label} — {wallet['last_error'][:60]}")
        elif synced is None:
            problems.append(f"• {label} — never synced")
        elif synced < stale_cutoff:
            problems.append(f"• {label} — last checked {_humanise(now - synced)}")
        else:
            healthy.append(label)

    lines = []

    if problems:
        lines.append("<b>Wallet monitoring — attention needed</b>")
        lines.append("")
        lines.append(f"{len(problems)} of {len(wallets)} wallets are not updating:")
        lines.extend(problems)
        if healthy:
            lines.append("")
            lines.append(f"{len(healthy)} still healthy.")
    else:
        lines.append("<b>Wallet monitoring — all healthy</b>")
        lines.append("")
        lines.append(f"All {len(wallets)} wallets checked within the last few minutes.")

    total = counts["total"] if counts else 0
    lines.append("")
    if total:
        lines.append(
            f"Last 24h: {total} transactions "
            f"({counts['received']} in, {counts['sent']} out)"
        )
    else:
        lines.append("Last 24h: no activity.")

    return "\n".join(lines)


async def stale_wallet_count() -> int:
    """
    How many wallets have stopped updating. Exposed so the app can show a
    warning banner rather than silently displaying old numbers.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_AFTER_MINUTES)

    row = await db.fetchrow(
        """
        select count(*) as stale
        from wallets w
        left join wallet_sync_state s on s.wallet_id = w.id
        where s.last_synced_at is null
           or s.last_synced_at < $1
           or s.last_error is not null
        """,
        cutoff,
    )

    return row["stale"] if row else 0
