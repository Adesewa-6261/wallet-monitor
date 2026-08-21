"""
app/api/routes/collectibles.py

NFTs, kept on their own endpoints rather than inside the balance response.

They are opened deliberately and take longer to fetch than balances, which are
read on every refresh. Combining them would make the screen everyone sees wait
for the screen most people do not.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app import db
from app.adapters import alchemy
from app.core import networks, security
from app.core.errors import RequestError
from app.schemas.wallets import CollectiblesResponse, WalletCollectibles
from app.services import collectibles as service

router = APIRouter(prefix="/api", tags=["collectibles"])

WALLET_COLUMNS = "id, label, chain, input, input_type, address_type"


def _label(result: WalletCollectibles) -> WalletCollectibles:
    """Fill in each item's display network, so the app does not assemble it."""
    for item in result.collectibles or []:
        item.network = networks.network_label(item.chain)
    return result


@router.get("/collectibles", response_model=CollectiblesResponse)
async def list_collectibles(
    user_id: str = Depends(security.current_user),
) -> CollectiblesResponse:
    """Every NFT across every wallet on this account."""
    wallets = await db.fetch(
        f"select {WALLET_COLUMNS} from wallets where user_id = $1 order by created_at",
        user_id,
    )

    results, total = await service.all_collectibles([dict(w) for w in wallets])

    return CollectiblesResponse(
        wallets=[_label(r) for r in results],
        total=total,
        as_of=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        spam_filtered=alchemy.spam_filtered(),
    )


@router.get("/wallets/{wallet_id}/collectibles", response_model=WalletCollectibles)
async def wallet_collectibles(
    wallet_id: str,
    user_id: str = Depends(security.current_user),
) -> WalletCollectibles:
    """NFTs for one wallet."""
    row = await db.fetchrow(
        f"select {WALLET_COLUMNS} from wallets where id = $1 and user_id = $2",
        wallet_id,
        user_id,
    )

    if not row:
        raise RequestError("INVALID_REQUEST", "Wallet not found.")

    return _label(await service.wallet_collectibles(dict(row)))
