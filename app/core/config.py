"""
lib/config.py

Environment variables, read and validated once at import so a missing key fails
at startup rather than deep inside a request.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(".env.local")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in .env.local locally and in Vercel project settings."
        )
    return value


def _optional(name: str) -> str:
    """
    A key the service runs without. Returns "" when unset, so a deployment that
    has not configured an optional provider still boots and the one feature that
    needs it reports itself unconfigured, rather than the whole API refusing to
    start over something most requests never touch.
    """
    return os.environ.get(name, "").strip()


def _optional_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


@dataclass(frozen=True)
class BitnobConfig:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class Config:
    bitnob: BitnobConfig
    coingecko_api_key: str
    database_url: str
    jwt_secret: str
    poll_minutes: int
    # Gap limit for Bitcoin xpub scanning. 20 is the BIP44 standard and is what
    # wallet software itself enforces, so funds beyond it normally cannot exist.
    # The scan extends past it adaptively when a block shows activity.
    bitcoin_gap_limit: int
    # NFT lookups. Optional: Ethereum has no method for "list the NFTs this
    # address owns", so it needs a provider that indexes them, and a deployment
    # without one simply has no collectibles rather than no service.
    alchemy_api_key: str


config = Config(
    bitnob=BitnobConfig(
        client_id=_required("BITNOB_CLIENT_ID"),
        client_secret=_required("BITNOB_CLIENT_SECRET"),
    ),
    coingecko_api_key=_required("COINGECKO_API_KEY"),
    database_url=_required("DATABASE_URL"),
    jwt_secret=_required("JWT_SECRET"),
    poll_minutes=_optional_int("ALERT_POLL_MINUTES", 2),
    bitcoin_gap_limit=_optional_int("BITCOIN_GAP_LIMIT", 20),
    alchemy_api_key=_optional("ALCHEMY_API_KEY"),
)
