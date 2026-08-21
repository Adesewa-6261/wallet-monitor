"""
app/core/networks.py

What each chain is called, and what its fees are paid in.

Kept in one place because the same facts are needed by the transaction feed,
the balance endpoints and the Telegram alerts, and because getting the fee
asset wrong is not a cosmetic mistake: an ERC-20 transfer moves USDT but pays
its fee in ETH, so a fee rendered against the wrong symbol misstates what the
transaction cost by several orders of magnitude.
"""

# The asset a network's transaction fees are actually paid in. Note this is
# rarely the asset being moved — only for a native transfer do the two match.
FEE_SYMBOL = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "base": "ETH",
    "tron": "TRX",
}

# Display names. The stored value is lowercase and machine-facing; this is what
# a person should see next to a holding.
NETWORK_LABEL = {
    "bitcoin": "Bitcoin",
    "ethereum": "Ethereum",
    "base": "Base",
    "tron": "Tron",
}


def fee_symbol(chain: str) -> str | None:
    """What a fee on this chain is denominated in, or None for a chain we do not know."""
    return FEE_SYMBOL.get(chain)


def network_label(chain: str) -> str:
    """
    Human name for a chain, falling back to the raw value.

    A chain we have not named yet is still better shown by its identifier than
    hidden, so the fallback is deliberate rather than a missing case.
    """
    return NETWORK_LABEL.get(chain, chain.title() if chain else "Unknown")


def asset_label(symbol: str, chain: str) -> str:
    """
    How an asset should read in a list: "USDC on Base".

    USDC on Base and USDC on Tron are different tokens on different ledgers
    that happen to share a name, and sending one to the other's address
    destroys the funds. The network is part of the asset's identity, not a
    decoration on top of it.
    """
    return f"{symbol} on {network_label(chain)}"
