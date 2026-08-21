"""
app/adapters/alchemy.py

NFT lookups.

Bitnob proxies a node, and a node cannot answer "what NFTs does this address
own". No Ethereum node can: ownership lives in each collection's own storage,
so answering the question means having indexed every collection in advance.
That indexing is the entire product Alchemy sells here, and it is why this is
a second provider rather than another call through the existing gateway —
the same reason prices come from CoinGecko and Bitcoin scanning from Esplora.

Unconfigured deployments get an empty result and a clear reason, not an error.
"""

import logging
import re
from typing import Optional

from ..core.config import config
from ..core.errors import RequestError
from ..core.http import fetch_json

logger = logging.getLogger("walletnest.alchemy")

# NFTs exist on these chains only. Bitcoin and Tron wallets are not queried at
# all, which is different from querying them and finding nothing.
HOSTS = {
    "ethereum": "https://eth-mainnet.g.alchemy.com",
    "base": "https://base-mainnet.g.alchemy.com",
}

# Alchemy caps a page at 100. Two pages is a deliberate ceiling: an address
# holding more than 200 NFTs is almost always a marketplace or a bot, and
# walking its whole collection would stall a request a person is waiting on.
PAGE_SIZE = 100
MAX_PAGES = 2


def configured() -> bool:
    return bool(config.alchemy_api_key)


# Whether this account's plan allows server-side spam filtering. Assumed until
# proven otherwise, then remembered, so the 400 is paid for once per process
# rather than on every lookup.
_spam_filter_available = True


def spam_filtered() -> bool:
    """Whether unsolicited NFTs are being excluded upstream."""
    return _spam_filter_available


def _disable_spam_filter() -> None:
    global _spam_filter_available
    if _spam_filter_available:
        logger.warning(
            "Alchemy rejected the spam filter, which needs a paid plan. "
            "Continuing without it: unsolicited NFTs will appear in results."
        )
    _spam_filter_available = False


# Scam NFTs are airdropped into wallets constantly, and the payload is the text
# itself: a name reading "claim 5 ETH at wallet-verify.io" turns the gallery
# into a delivery channel. These are marked rather than hidden — hiding relies
# on the guess being right, and a wrong guess silently removes something the
# owner actually holds. Marking is safe when wrong, which hiding is not.

# Words that carry no meaning in a collection's title but every meaning in bait.
_SCAM_WORDS = (
    "claim", "reward", "rewards", "airdrop", "voucher", "giveaway",
    "verify", "visit", "unlock", "winner", "bonus",
)

# A link, a domain, a wallet prompt, or a cash figure. None belong in a name.
_SCAM_SHAPES = (
    r"https?://",
    r"www\.",
    r"\.(?:com|net|io|xyz|org|co|app|gift|claim|finance|live)(?:\b|/|$)",
    r"connect\s+(?:your\s+)?wallet",
    r"[$]\s*\d",
)

_SCAM_PATTERNS = re.compile(
    "|".join(
        [rf"\b{re.escape(w)}\b" for w in _SCAM_WORDS] + list(_SCAM_SHAPES)
    ),
    re.IGNORECASE,
)


def _suspicious(name: str, collection: str) -> bool:
    """Whether this item's text reads like bait rather than a title."""
    return bool(_SCAM_PATTERNS.search(f"{name} {collection}"))


def _image(nft: dict) -> Optional[str]:
    """
    A URL the app can actually load.

    Alchemy caches and re-hosts artwork over HTTPS, which matters because the
    original is frequently an ipfs:// URI that no HTTP client can fetch without
    a gateway. Only https URLs are returned; an unloadable one is worse than
    none, because the app would render a broken image instead of a placeholder.
    """
    image = nft.get("image") or {}
    for key in ("thumbnailUrl", "cachedUrl", "pngUrl", "originalUrl"):
        url = image.get(key)
        if url and url.startswith("https://"):
            return url
    return None


def _name(nft: dict, contract: dict) -> str:
    """Best available name, falling back to the collection and token id."""
    if nft.get("name"):
        return nft["name"]

    collection = contract.get("name") or "Unknown collection"
    token_id = nft.get("tokenId", "")

    # Token ids are frequently long decimal strings; a truncated one is more
    # readable than the full value and no less identifying next to its collection.
    if len(token_id) > 12:
        token_id = token_id[:8] + "…"
    return f"{collection} #{token_id}" if token_id else collection


async def get_nfts(chain: str, address: str) -> list[dict]:
    """
    Every NFT this address owns on one chain.

    Spam is excluded at the source. Unsolicited NFTs are airdropped constantly,
    frequently as bait carrying a lookalike name or a phishing link, and a
    wallet screen that lists them alongside real holdings is doing the attacker
    a favour rather than the owner.
    """
    if not configured():
        raise RequestError(
            "UNSUPPORTED_INPUT",
            "NFT lookups are not configured on this deployment.",
        )

    host = HOSTS.get(chain)
    if not host:
        return []

    url = f"{host}/nft/v3/{config.alchemy_api_key}/getNFTsForOwner"
    results: list[dict] = []
    page_key: Optional[str] = None

    for _ in range(MAX_PAGES):
        params = {
            "owner": address,
            "withMetadata": "true",
            "pageSize": str(PAGE_SIZE),
        }
        # Read the shared flag once, into a local. Chains are queried
        # concurrently, and the retry below must be decided by what *this*
        # request actually sent: if another chain flips the flag mid-flight, a
        # check against the flag would see "already disabled" and re-raise a
        # request that had in fact just tried the filter for the first time.
        used_filter = _spam_filter_available

        if used_filter:
            params["excludeFilters[]"] = "SPAM"
        if page_key:
            params["pageKey"] = page_key

        try:
            payload = await fetch_json(url, params=params, label="alchemy", timeout=20.0)
        except RequestError:
            # Spam filtering is a paid feature and answers 400 on the free tier.
            # Losing the whole gallery over a filter would be the wrong trade, so
            # drop it and carry on — noting that spam is now unfiltered, which
            # the caller needs to know rather than discover from odd artwork.
            if not used_filter:
                raise
            _disable_spam_filter()
            params.pop("excludeFilters[]", None)
            payload = await fetch_json(url, params=params, label="alchemy", timeout=20.0)

        for nft in payload.get("ownedNfts", []) or []:
            contract = nft.get("contract") or {}

            name = _name(nft, contract)
            collection = contract.get("name") or "Unknown collection"

            results.append({
                "token_id": nft.get("tokenId"),
                "name": name,
                "collection": collection,
                "suspicious": _suspicious(name, collection),
                "contract": contract.get("address"),
                "image_url": _image(nft),
                "chain": chain,
                # ERC-1155 lets one token id be held in quantity, unlike ERC-721
                # where ownership is always exactly one.
                "token_type": nft.get("tokenType") or contract.get("tokenType"),
                "balance": nft.get("balance") or "1",
            })

        page_key = payload.get("pageKey")
        if not page_key:
            break

    return results
