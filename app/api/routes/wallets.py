"""
app/api/routes/wallets.py

Adding, listing and removing watched wallets.

Every query filters on the authenticated user id. That filter is the only thing
separating one person's wallets from another's, so it belongs in the query
itself rather than in a check afterwards.
"""

from fastapi import APIRouter, Depends

from app import db
from app.core import security
from app.core.errors import RequestError
from app.schemas.wallets import AddWalletRequest, WalletSummary
from app.services import detection

router = APIRouter(prefix="/api/wallets", tags=["wallets"])


def _display_key(value: str) -> str:
    """
    Shorten a key for display.

    Note we keep 12 leading characters rather than the usual 6. Address
    poisoning works by generating a lookalike that matches the first and last
    few characters of a real address, so an aggressive truncation is exactly
    what the attack relies on.
    """
    if len(value) <= 20:
        return value
    return f"{value[:12]}…{value[-6:]}"


@router.post("", response_model=WalletSummary)
async def add_wallet(
    body: AddWalletRequest,
    user_id: str = Depends(security.current_user),
) -> WalletSummary:
    detected = detection.detect(body.input)

    if not detected.valid:
        raise RequestError(
            "INVALID_REQUEST",
            detected.error.message if detected.error else "That input was not recognised.",
        )

    existing = await db.fetchrow(
        "select id from wallets where user_id = $1 and input = $2",
        user_id,
        body.input.strip(),
    )
    if existing:
        raise RequestError("INVALID_REQUEST", "You are already watching that wallet.")

    row = await db.fetchrow(
        """
        insert into wallets (user_id, label, input, input_type, chain, address_type)
        values ($1, $2, $3, $4, $5, $6)
        returning id, label, chain, input_type, address_type, input
        """,
        user_id,
        body.label,
        body.input.strip(),
        detected.input_type,
        detected.chain,
        detected.address_type,
    )

    await db.execute(
        "insert into wallet_sync_state (wallet_id) values ($1)", row["id"]
    )

    return WalletSummary(
        id=str(row["id"]),
        label=row["label"],
        chain=row["chain"],
        input_type=row["input_type"],
        address_type=row["address_type"],
        display_key=_display_key(row["input"]),
    )


@router.get("", response_model=list[WalletSummary])
async def list_wallets(user_id: str = Depends(security.current_user)) -> list[WalletSummary]:
    rows = await db.fetch(
        """
        select w.id, w.label, w.chain, w.input_type, w.address_type, w.input,
               s.last_synced_at, s.last_error
        from wallets w
        left join wallet_sync_state s on s.wallet_id = w.id
        where w.user_id = $1
        order by w.created_at
        """,
        user_id,
    )

    return [
        WalletSummary(
            id=str(r["id"]),
            label=r["label"],
            chain=r["chain"],
            input_type=r["input_type"],
            address_type=r["address_type"],
            display_key=_display_key(r["input"]),
            last_synced_at=r["last_synced_at"].isoformat() if r["last_synced_at"] else None,
            error=r["last_error"],
        )
        for r in rows
    ]


@router.delete("/{wallet_id}")
async def remove_wallet(
    wallet_id: str,
    user_id: str = Depends(security.current_user),
) -> dict:
    result = await db.execute(
        "delete from wallets where id = $1 and user_id = $2", wallet_id, user_id
    )

    if result.endswith("0"):
        raise RequestError("INVALID_REQUEST", "Wallet not found.")

    return {"removed": True}
