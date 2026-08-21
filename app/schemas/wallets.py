"""Request and response models for wallet endpoints."""

from typing import Optional

from pydantic import BaseModel, Field


class AddWalletRequest(BaseModel):
    # Exactly what the user pasted or scanned. Not pre-cleaned — validation
    # needs to see it verbatim to detect what it is.
    input: str
    label: Optional[str] = Field(default=None, max_length=60)


class UpdateWalletRequest(BaseModel):
    # Only the label. The key a wallet was added with is what its sync position
    # and stored transactions are tied to, so it is deliberately not editable.
    #
    # Required rather than defaulted, so an explicit null clears the label and an
    # empty body is a mistake rather than a silent no-op.
    label: Optional[str] = Field(max_length=60)


class WalletHolding(BaseModel):
    symbol: str
    # String, not float. An 8-decimal Bitcoin amount does not survive a float.
    amount: str
    value_usd: Optional[float] = None
    # Which ledger this holding actually sits on. Not always the wallet's own
    # chain: one Ethereum address is equally an address on Base, so a single
    # wallet can hold USDC on both, and they are different tokens that cannot
    # be sent to each other. `network` is the display form, `label` the full
    # phrase the app can show directly: "USDC on Base".
    chain: str = "bitcoin"
    network: str = "Bitcoin"
    label: str = ""


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


class WalletBalance(BaseModel):
    wallet_id: str
    label: Optional[str]
    chain: str
    # Null rather than empty when the lookup failed — an empty list would read
    # as "this wallet holds nothing", which is a different statement.
    holdings: Optional[list[WalletHolding]] = None
    value_usd: Optional[float] = None
    # Set when something went wrong. It can arrive *with* holdings: an EVM
    # wallet reads two chains, and if only one answers we would rather show
    # what we have and say the picture is incomplete than discard a good half.
    # Whenever this is set, value_usd is null — a total we know is partial is
    # not a total.
    error: Optional[str] = None


class BalancesResponse(BaseModel):
    wallets: list[WalletBalance]
    # Only wallets we could price contribute. A wallet that failed, or whose
    # price is unknown, is left out rather than counted as zero.
    total_usd: float
    as_of: str


class TransactionOut(BaseModel):
    id: str
    wallet_id: str
    wallet_label: Optional[str]
    tx_hash: str
    chain: str
    # Display name for `chain`: "Base" rather than "base".
    network: str
    # receive | send | internal
    direction: str
    # receive | send | internal | swap. `direction` still says which way this
    # leg moved; `type` describes the transaction the leg belongs to. A swap is
    # one transaction holding two legs, so it arrives as two rows sharing a
    # tx_hash — the app should collapse them into a single entry rather than
    # showing the user two transactions they did not make.
    type: str
    symbol: str
    amount: str
    counterparty: Optional[str]
    block_time: str
    value_usd: Optional[float]
    # What it cost to send, and the asset that cost is denominated in. The two
    # are separate because they usually disagree: moving USDT on Ethereum
    # charges the fee in ETH. Null where the chain does not report it yet.
    fee: Optional[str]
    fee_symbol: Optional[str]


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


class Collectible(BaseModel):
    token_id: Optional[str]
    name: str
    collection: str
    contract: Optional[str]
    # Null when the artwork is only available over ipfs:// or not at all. The
    # app should show a placeholder; a URL it cannot load renders as a broken
    # image, which looks worse than admitting there is no picture.
    image_url: Optional[str] = None
    chain: str
    network: str = ""
    # ERC721 or ERC1155. Only ERC-1155 can be held in quantity, so a balance
    # above one is meaningful there and always exactly one for ERC-721.
    token_type: Optional[str] = None
    balance: str = "1"


class WalletCollectibles(BaseModel):
    wallet_id: str
    label: Optional[str]
    chain: str
    # False for Bitcoin and Tron, which do not carry NFTs at all. Distinct from
    # an empty list, which means this wallet could hold them and does not.
    supported: bool = True
    collectibles: Optional[list[Collectible]] = None
    error: Optional[str] = None


class CollectiblesResponse(BaseModel):
    wallets: list[WalletCollectibles]
    total: int
    as_of: str
    # False when unsolicited NFTs are NOT being filtered out upstream, which is
    # the case on Alchemy's free tier. It matters because scam NFTs are airdropped
    # constantly and often carry a phishing link in the name or description. The
    # app should mark an unfiltered list as unverified rather than presenting it
    # as the user's collection, and must never make a name or link tappable
    # without that warning.
    spam_filtered: bool = True
