"""
app/api/routes/transactions.py

The transaction feed, and alert settings.

Filtering happens in SQL rather than in Python because the feed is paginated —
fetching everything to filter afterwards would read the whole table to return
twenty rows.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app import db
from app.core import security
from app.core.errors import RequestError
from app.schemas.wallets import AlertSettingsOut, AlertSettingsUpdate, TransactionOut

router = APIRouter(prefix="/api", tags=["activity"])


@router.get("/transactions", response_model=list[TransactionOut])
async def list_transactions(
    wallet_id: Optional[str] = Query(default=None),
    symbol: Optional[str] = Query(default=None),
    limit: int = Query(default=25, le=100),
    before: Optional[str] = Query(default=None, description="ISO timestamp cursor"),
    user_id: str = Depends(security.current_user),
) -> list[TransactionOut]:
    conditions = ["w.user_id = $1"]
    args: list = [user_id]

    if wallet_id:
        args.append(wallet_id)
        conditions.append(f"t.wallet_id = ${len(args)}")

    if symbol:
        args.append(symbol.upper())
        conditions.append(f"t.symbol = ${len(args)}")

    if before:
        args.append(before)
        conditions.append(f"t.block_time < ${len(args)}::timestamptz")

    args.append(limit)

    rows = await db.fetch(
        f"""
        select t.id, t.wallet_id, w.label as wallet_label, t.tx_hash, t.chain,
               t.direction, t.symbol, t.amount, t.counterparty, t.block_time,
               t.value_usd
        from transactions t
        join wallets w on w.id = t.wallet_id
        where {' and '.join(conditions)}
        order by t.block_time desc
        limit ${len(args)}
        """,
        *args,
    )

    return [
        TransactionOut(
            id=str(r["id"]),
            wallet_id=str(r["wallet_id"]),
            wallet_label=r["wallet_label"],
            tx_hash=r["tx_hash"],
            chain=r["chain"],
            direction=r["direction"],
            symbol=r["symbol"],
            amount=r["amount"],
            counterparty=r["counterparty"],
            block_time=r["block_time"].isoformat(),
            value_usd=float(r["value_usd"]) if r["value_usd"] is not None else None,
        )
        for r in rows
    ]


@router.get("/alerts/settings", response_model=AlertSettingsOut)
async def get_alert_settings(user_id: str = Depends(security.current_user)) -> AlertSettingsOut:
    row = await db.fetchrow(
        "select enabled, min_value_usd, alert_on_receive, alert_on_send, daily_digest "
        "from alert_settings where user_id = $1",
        user_id,
    )

    if not row:
        raise RequestError("INVALID_REQUEST", "No alert settings found.")

    return AlertSettingsOut(
        enabled=row["enabled"],
        min_value_usd=float(row["min_value_usd"]),
        alert_on_receive=row["alert_on_receive"],
        alert_on_send=row["alert_on_send"],
        daily_digest=row["daily_digest"],
    )


@router.patch("/alerts/settings", response_model=AlertSettingsOut)
async def update_alert_settings(
    body: AlertSettingsUpdate,
    user_id: str = Depends(security.current_user),
) -> AlertSettingsOut:
    updates = body.model_dump(exclude_none=True)

    if not updates:
        return await get_alert_settings(user_id)

    args = [user_id]
    assignments = []
    for field, value in updates.items():
        args.append(value)
        assignments.append(f"{field} = ${len(args)}")

    await db.execute(
        f"update alert_settings set {', '.join(assignments)} where user_id = $1",
        *args,
    )

    return await get_alert_settings(user_id)


@router.get("/monitor/status")
async def monitor_status(user_id: str = Depends(security.current_user)) -> dict:
    """
    Whether the user's wallets are actually being watched.

    The app should surface this — displaying a balance that stopped updating two
    days ago, with no indication, is worse than showing nothing.
    """
    rows = await db.fetch(
        """
        select w.id, w.label, w.chain, s.last_synced_at, s.last_error, s.last_block
        from wallets w
        left join wallet_sync_state s on s.wallet_id = w.id
        where w.user_id = $1
        """,
        user_id,
    )

    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)

    stale = [
        {
            "wallet_id": str(r["id"]),
            "label": r["label"],
            "last_synced_at": r["last_synced_at"].isoformat() if r["last_synced_at"] else None,
            "error": r["last_error"],
            "chain": r["chain"],
        }
        for r in rows
        if r["last_synced_at"] is None
        or r["last_synced_at"] < stale_cutoff
        or r["last_error"]
    ]

    last_sync = max(
        (r["last_synced_at"] for r in rows if r["last_synced_at"]), default=None
    )

    return {
        "healthy": not stale,
        "wallets": len(rows),
        "stale_wallets": stale,
        "last_sync_at": last_sync.isoformat() if last_sync else None,
    }
