"""
app/api/routes/balances.py

Current balances — one wallet, or all of them with a combined total.

Every query filters on the authenticated user id. That filter is the only thing
separating one person's wallets from another's, so it belongs in the query
itself rather than in a check afterwards.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app import db
from app.core import security
from app.core.errors import RequestError
from app.schemas.wallets import BalancesResponse, WalletBalance
from app.services import balances

router = APIRouter(prefix="/api", tags=["balances"])


def _as_of() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/wallets/{wallet_id}/balance", response_model=WalletBalance)
async def wallet_balance(
    wallet_id: str,
    user_id: str = Depends(security.current_user),
) -> WalletBalance:
    row = await db.fetchrow(
        """
        select id, label, chain, input, input_type, address_type
        from wallets
        where id = $1 and user_id = $2
        """,
        wallet_id,
        user_id,
    )

    if not row:
        raise RequestError("INVALID_REQUEST", "Wallet not found.")

    return await balances.wallet_balance(dict(row))


@router.get("/balances", response_model=BalancesResponse)
async def all_balances(
    user_id: str = Depends(security.current_user),
) -> BalancesResponse:
    """
    Every wallet plus the total. This is what the dashboard shows.

    Returns 200 even when wallets failed — each carries its own error, and the
    total covers the rest. A partial portfolio is more useful than a blank one.
    """
    rows = await db.fetch(
        """
        select id, label, chain, input, input_type, address_type
        from wallets
        where user_id = $1
        order by created_at
        """,
        user_id,
    )

    wallets, total = await balances.wallet_balances([dict(r) for r in rows])

    return BalancesResponse(wallets=wallets, total_usd=total, as_of=_as_of())
