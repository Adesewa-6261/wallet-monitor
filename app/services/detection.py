"""
routes/validate.py

Called when the user pastes or scans something, before the Add button enables.

Two jobs:

1. Work out what they gave us — an xpub, a descriptor, a Bitcoin address, an
   EVM address, a Tron address — and which chain it belongs to.

2. Refuse anything that could spend money. A watch-only app must never accept a
   private key or seed phrase, even by accident. If someone pastes one we reject
   it loudly and never store it.

Everything here is offline. No network calls, so validation is instant and works
even when a provider is down.
"""

import hashlib
import re
from typing import Optional

from dataclasses import dataclass
from typing import Optional as Opt

from ..adapters.bip32 import encode_address
from ..core.errors import RequestError


@dataclass
class DetectionError:
    code: str
    message: str


@dataclass
class Detection:
    valid: bool
    input_type: Opt[str] = None
    chain: Opt[str] = None
    address_type: Opt[str] = None
    default_label: Opt[str] = None
    warning: Opt[str] = None
    error: Opt[DetectionError] = None
    # Set when what the user typed should be stored as something else — a
    # one-key descriptor is saved as the address it names, so the rest of the
    # system never has to know it arrived wearing descriptor syntax.
    normalised: Opt[str] = None


def wallet_error(code: str, message: str, retryable: bool = True) -> DetectionError:
    return DetectionError(code=code, message=message)


# --------------------------------------------------------------- base58check

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _b58_decode(value: str) -> bytes:
    """Decode base58. Raises ValueError on an invalid character."""
    num = 0
    for char in value:
        if char not in _B58_INDEX:
            raise ValueError(f"invalid base58 character: {char}")
        num = num * 58 + _B58_INDEX[char]

    decoded = num.to_bytes((num.bit_length() + 7) // 8, "big")

    # Leading '1's in base58 encode leading zero bytes, which the integer
    # conversion above discards.
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + decoded


def _b58check_valid(value: str) -> bool:
    """
    Verify a base58check string: the last four bytes are the first four bytes of
    the double SHA-256 of everything before them.

    This is what catches a typo. Without it, one wrong character produces a
    plausible-looking key that silently shows a zero balance — the worst
    possible failure, because to the user it looks like lost funds.
    """
    try:
        raw = _b58_decode(value)
    except ValueError:
        return False

    if len(raw) < 5:
        return False

    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return checksum == expected


# -------------------------------------------------------------------- bech32

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _bech32_polymod(values: list) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            checksum ^= generator[i] if ((top >> i) & 1) else 0
    return checksum


def _bech32_hrp_expand(hrp: str) -> list:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _bech32_valid(address: str) -> bool:
    """
    Verify a bech32 or bech32m address.

    SegWit v0 (bc1q...) uses bech32; Taproot (bc1p...) uses bech32m. They differ
    only in the final constant, so we accept either.
    """
    if any(ord(c) < 33 or ord(c) > 126 for c in address):
        return False

    if address.lower() != address and address.upper() != address:
        return False  # mixed case is not allowed

    address = address.lower()
    pos = address.rfind("1")
    if pos < 1 or pos + 7 > len(address) or len(address) > 90:
        return False

    hrp, data_part = address[:pos], address[pos + 1:]

    data = []
    for char in data_part:
        if char not in _BECH32_CHARSET:
            return False
        data.append(_BECH32_CHARSET.index(char))

    checksum = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    return checksum in (_BECH32_CONST, _BECH32M_CONST)


# ----------------------------------------------------------- secret detection

_WIF_RE = re.compile(r"^[5KL][1-9A-HJ-NP-Za-km-z]{50,51}$")
_HEX_PRIVKEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")

# Extended PRIVATE key prefixes. One character different from the public
# versions, which is exactly why this check matters.
_PRIVATE_PREFIXES = ("xprv", "yprv", "zprv", "tprv", "uprv", "vprv")


def _looks_like_seed_phrase(value: str) -> bool:
    """
    A run of 12/15/18/21/24 lowercase alphabetic words is unmistakably a BIP39
    recovery phrase. We do not need the full 2048-word list to be confident.
    """
    words = value.strip().lower().split()
    if len(words) not in (12, 15, 18, 21, 24):
        return False
    return all(word.isalpha() for word in words)


def _looks_like_private_key(value: str) -> bool:
    cleaned = value.strip()

    # Bitcoin WIF: starts 5 (uncompressed) or K/L (compressed)
    if _WIF_RE.match(cleaned) and _b58check_valid(cleaned):
        return True

    if cleaned[:4] in _PRIVATE_PREFIXES:
        return True

    # Raw 32-byte hex, as exported by many EVM wallets.
    if _HEX_PRIVKEY_RE.match(cleaned):
        return True

    return False


# ----------------------------------------------------------- format detection

# Extended public key prefixes and what they imply.
#
# The prefix encodes the address type by convention (SLIP-132), not by protocol.
# An xpub is genuinely ambiguous: it predates the convention, so the same key
# could be Legacy or Nested SegWit depending on the wallet that produced it.
# Guessing wrong shows a zero balance, so we ask the user instead.
_XPUB_PREFIXES = {
    "xpub": (None, True),                # ambiguous — needs a picker
    "ypub": ("Nested SegWit", False),
    "zpub": ("Native SegWit", False),
    "tpub": (None, True),                # testnet, ambiguous
    "upub": ("Nested SegWit", False),    # testnet
    "vpub": ("Native SegWit", False),    # testnet
}

_DESCRIPTOR_RE = re.compile(r"^(pk|pkh|wpkh|sh|wsh|tr|combo|multi|sortedmulti)\(")
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


def _descriptor_address_type(descriptor: str) -> Optional[str]:
    if descriptor.startswith("tr("):
        return "Taproot"
    if descriptor.startswith("sh(wpkh("):
        return "Nested SegWit"
    if descriptor.startswith("wpkh("):
        return "Native SegWit"
    if descriptor.startswith("pkh("):
        return "Legacy"
    return None


_SINGLE_KEY_RE = re.compile(
    r"\(\s*(?:\[[^\]]*\]\s*)?((?:02|03)[0-9a-fA-F]{64}|[0-9a-fA-F]{64})\s*\)"
)


def _single_key_descriptor(descriptor: str, address_type: Optional[str]) -> Optional[str]:
    """
    The address a one-key descriptor names, or None if it wraps an extended key.

    Only bare keys qualify. Anything containing an xpub describes a range of
    addresses and belongs on the scanning path instead.
    """
    if re.search(r"[xyztuv]pub[1-9A-HJ-NP-Za-km-z]{50,}", descriptor):
        return None

    match = _SINGLE_KEY_RE.search(descriptor)
    if not match or not address_type:
        return None

    raw = match.group(1)

    try:
        key = bytes.fromhex(raw)

        if address_type == "Taproot":
            # Taproot descriptors normally carry the 32-byte x-only key, while
            # the encoder wants the 33-byte compressed form and drops the prefix
            # itself. Re-adding an even-Y prefix is the right inverse: BIP340
            # defines an x-only key as the even-Y point, so this reconstructs
            # exactly the key the descriptor meant.
            if len(key) == 32:
                key = b"" + key
            elif len(key) != 33:
                return None
        elif len(key) != 33:
            # Everything else needs a compressed key; an x-only key has no
            # defined address outside taproot.
            return None

        return encode_address(key, address_type)
    except (ValueError, RequestError):
        return None


def detect(raw: str) -> Detection:
    """
    Work out what the user gave us.

    Pure function, no I/O — easy to test and instant to run.
    """
    value = raw.strip()

    if not value:
        return Detection(
            valid=False,
            error=DetectionError("INVALID_REQUEST", "Nothing to check."),
        )

    # --- secrets first, before anything else can match ---

    if _looks_like_seed_phrase(value):
        return Detection(
            valid=False,
            input_type="seed_phrase",
            error=DetectionError(
                "REJECTED_SECRET",
                (
                    "That looks like a recovery phrase. This app never needs one "
                    "and will not store it. Please paste a public key or address "
                    "instead."
                ),
            ),
        )

    if _looks_like_private_key(value):
        return Detection(
            valid=False,
            input_type="private_key",
            error=DetectionError(
                "REJECTED_SECRET",
                (
                    "That looks like a private key. This app never needs one and "
                    "will not store it. Please paste a public key or address "
                    "instead."
                ),
            ),
        )

    # --- output descriptor ---

    if _DESCRIPTOR_RE.match(value):
        address_type = _descriptor_address_type(value)

        # A descriptor wrapping one bare public key rather than an extended key
        # describes exactly one address, not a wallet to scan. Turn it into that
        # address and carry on, because rejecting it would mean telling someone
        # their perfectly valid descriptor is unsupported when the thing it
        # names is something we handle happily.
        single = _single_key_descriptor(value, address_type)
        if single:
            return Detection(
                valid=True,
                input_type="address",
                chain="bitcoin",
                address_type=address_type,
                default_label="Bitcoin wallet",
                normalised=single,
                warning=(
                    "That descriptor holds a single key, so it describes one "
                    f"address: {single}"
                ),
            )

        return Detection(
            valid=True,
            input_type="descriptor",
            chain="bitcoin",
            address_type=address_type,
            default_label="Bitcoin wallet",
        )

    # --- extended public key ---

    prefix = value[:4]
    if prefix in _XPUB_PREFIXES:
        if not _b58check_valid(value):
            return Detection(
                valid=False,
                input_type="xpub",
                error=wallet_error(
                    "INVALID_CHECKSUM",
                    "This key looks almost right but fails its checksum. A single "
                    "wrong character will do that. Try scanning the QR code "
                    "instead of typing it.",
                    retryable=False,
                ),
            )

        address_type, prefix_is_ambiguous = _XPUB_PREFIXES[prefix]

        # We do NOT ask the user to resolve an ambiguous prefix. The portfolio
        # endpoint probes the chain for which address type actually has history,
        # which is both more accurate and invisible to the user. A prefix hint is
        # only ever a hint — even a zpub can be wrong if the wallet is unusual.
        return Detection(
            valid=True,
            input_type="xpub",
            chain="bitcoin",
            address_type=address_type,
            default_label="Bitcoin wallet",
            warning=(
                "We will work out the address type from the blockchain when we scan."
                if prefix_is_ambiguous
                else None
            ),
        )

    # --- EVM address (Ethereum and Base share the format) ---

    if _EVM_RE.match(value):
        return Detection(
            valid=True,
            input_type="address",
            chain="ethereum",
            address_type=None,
            default_label="Ethereum wallet",
            # Recorded as Ethereum because a wallet row needs one chain, but the
            # balance lookup reads Base under the same address and tags each
            # holding with where it actually lives. The wording used to promise
            # this while nothing did it; it is true now, so keep the two in step
            # if either side changes.
            warning="The same address works on Ethereum and Base. We check both.",
        )

    # --- Tron address ---

    if _TRON_RE.match(value):
        if not _b58check_valid(value):
            return Detection(
                valid=False,
                input_type="address",
                error=wallet_error(
                    "INVALID_CHECKSUM",
                    "This Tron address fails its checksum. Check for a mistyped "
                    "character.",
                    retryable=False,
                ),
            )
        return Detection(
            valid=True,
            input_type="address",
            chain="tron",
            address_type=None,
            default_label="Tron wallet",
        )

    # --- Bitcoin bech32 addresses ---

    if value.lower().startswith(("bc1", "tb1")):
        if not _bech32_valid(value):
            return Detection(
                valid=False,
                input_type="address",
                error=wallet_error(
                    "INVALID_CHECKSUM",
                    "This Bitcoin address fails its checksum. Check for a mistyped "
                    "character.",
                    retryable=False,
                ),
            )
        # bc1p is Taproot, bc1q is Native SegWit.
        address_type = (
            "Taproot"
            if value.lower().startswith(("bc1p", "tb1p"))
            else "Native SegWit"
        )
        return Detection(
            valid=True,
            input_type="address",
            chain="bitcoin",
            address_type=address_type,
            default_label="Bitcoin address",
            warning=(
                "This is a single address, not a whole wallet. If you have an "
                "extended public key, that shows everything."
            ),
        )

    # --- Bitcoin base58 addresses ---

    if value[0] in ("1", "3"):
        if not _b58check_valid(value):
            return Detection(
                valid=False,
                input_type="address",
                error=wallet_error(
                    "INVALID_CHECKSUM",
                    "This Bitcoin address fails its checksum. Check for a mistyped "
                    "character.",
                    retryable=False,
                ),
            )
        return Detection(
            valid=True,
            input_type="address",
            chain="bitcoin",
            address_type="Legacy" if value[0] == "1" else "Nested SegWit",
            default_label="Bitcoin address",
            warning=(
                "This is a single address, not a whole wallet. If you have an "
                "extended public key, that shows everything."
            ),
        )

    # --- nothing matched ---

    return Detection(
        valid=False,
        error=wallet_error(
            "UNSUPPORTED_INPUT",
            "We could not recognise this. We accept Bitcoin addresses and "
            "extended public keys, and Ethereum, Base or Tron addresses.",
            retryable=False,
        ),
    )
