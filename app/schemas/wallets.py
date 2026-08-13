"""Request and response models for wallet endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class AddWalletRequest(BaseModel):
    # Exactly what the user pasted or scanned. Not pre-cleaned — validation
    # needs to see it verbatim to detect what it is.
    input: str
    label: Optional[str] = Field(default=None, max_length=60)


class WalletHolding(BaseModel):
    symbol: str
    # String, not float. An 8-decimal Bitcoin amount does not survive a float.
    amount: str
    value_usd: Optional[float] = None


class WalletSummary(BaseModel):
    id: str
    label: Optional[str]
    chain: str
    input_type: str
    address_type: Optional[str]
    display_key: str
    holdings: list[WalletHolding] = []
    value_usd: Optional[float] = None
    last_synced_at: Optional[str] = None
    error: Optional[str] = None


class TransactionOut(BaseModel):
    id: str
    wallet_id: str
    wallet_label: Optional[str]
    tx_hash: str
    chain: str
    # receive | send | internal
    direction: str
    symbol: str
    amount: str
    counterparty: Optional[str]
    block_time: str
    value_usd: Optional[float]


class AlertSettingsOut(BaseModel):
    enabled: bool
    min_value_usd: float
    alert_on_receive: bool
    alert_on_send: bool
    # A daily message even when nothing happened, so that silence from the bot
    # means something is wrong rather than nothing is happening.
    daily_digest: bool


class AlertSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    # Ignore transactions below this USD value. Exists because dust spam and
    # address-poisoning attacks generate a lot of tiny incoming transactions.
    min_value_usd: Optional[float] = Field(default=None, ge=0)
    alert_on_receive: Optional[bool] = None
    alert_on_send: Optional[bool] = None
    daily_digest: Optional[bool] = None
