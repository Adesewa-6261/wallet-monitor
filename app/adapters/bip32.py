"""
app/adapters/bip32.py

BIP32 public-key derivation and Bitcoin address encoding.

Written directly rather than pulled from a library. bip_utils supports around
200 coins and drags in build-time dependencies that have no Windows wheels; we
need public derivation on one curve and four address formats. The relevant
specifications are BIP32, BIP49, BIP84 and BIP86, and the code below is checked
against their published test vectors.

Only PUBLIC derivation is implemented. There is deliberately no code path that
can produce a private key, which suits a watch-only product: even a bug cannot
turn an extended public key into something that spends.
"""

import hashlib
import hmac

from coincurve import PublicKey

from ..core.errors import RequestError

# SLIP-132 version prefixes. These differ only in four bytes; the key material
# underneath is identical. The prefix hints at the intended address type, which
# is a convention rather than part of the protocol.
VERSIONS = {
    "xpub": bytes.fromhex("0488b21e"),
    "ypub": bytes.fromhex("049d7cb2"),
    "zpub": bytes.fromhex("04b24746"),
    "tpub": bytes.fromhex("043587cf"),
    "upub": bytes.fromhex("044a5262"),
    "vpub": bytes.fromhex("045f1cf6"),
}

HARDENED = 0x80000000

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


# ------------------------------------------------------------------- base58


def b58_decode(value: str) -> bytes:
    num = 0
    for char in value:
        if char not in _B58_INDEX:
            raise ValueError("invalid base58 character")
        num = num * 58 + _B58_INDEX[char]
    decoded = num.to_bytes((num.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + decoded


def b58_encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def b58check_encode(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return b58_encode(payload + checksum)


def b58check_decode(value: str) -> bytes:
    raw = b58_decode(value)
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("bad checksum")
    return payload


# ------------------------------------------------------------------- bech32


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            checksum ^= generator[i] if ((top >> i) & 1) else 0
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    out = []
    maxv = (1 << tobits) - 1

    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)

    if pad and bits:
        out.append((acc << (tobits - bits)) & maxv)

    return out


def bech32_encode(hrp: str, witness_version: int, witness_program: bytes) -> str:
    """
    SegWit v0 uses bech32; Taproot (v1) uses bech32m. They differ only in the
    checksum constant.
    """
    data = [witness_version] + _convertbits(witness_program, 8, 5)
    const = _BECH32_CONST if witness_version == 0 else _BECH32M_CONST

    values = _hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]

    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


# ------------------------------------------------------------------- hashing


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def tagged_hash(tag: str, data: bytes) -> bytes:
    """BIP340 tagged hash, used by Taproot to tweak the output key."""
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


# ------------------------------------------------------------- key derivation


class ExtendedKey:
    """An extended public key, and the ability to derive children from it."""

    def __init__(self, public_key: bytes, chain_code: bytes) -> None:
        self.public_key = public_key
        self.chain_code = chain_code

    @classmethod
    def parse(cls, extended: str) -> "ExtendedKey":
        """
        Read a serialised extended key.

        Any SLIP-132 prefix is accepted. The version bytes only record the
        intended address type, and we track that separately, so a ypub and an
        xpub with the same key material derive identical addresses.
        """
        try:
            raw = b58check_decode(extended)
        except ValueError as err:
            raise RequestError(
                "INVALID_CHECKSUM",
                "This key fails its checksum. A single wrong character will do "
                "that — try scanning the QR code instead of typing it.",
            ) from err

        if len(raw) != 78:
            raise RequestError("UNSUPPORTED_INPUT", "This is not an extended key.")

        # version(4) depth(1) fingerprint(4) child(4) chaincode(32) key(33)
        chain_code = raw[13:45]
        key = raw[45:78]

        if key[0] not in (0x02, 0x03):
            raise RequestError(
                "REJECTED_SECRET",
                "That is an extended private key. This app only accepts public keys.",
            )

        return cls(public_key=key, chain_code=chain_code)

    def child(self, index: int) -> "ExtendedKey":
        """
        Derive a non-hardened child.

        Hardened derivation is impossible from a public key by design — that is
        the property that makes sharing an xpub safe.
        """
        if index >= HARDENED:
            raise RequestError(
                "UNSUPPORTED_INPUT",
                "Hardened derivation is not possible from a public key.",
            )

        data = self.public_key + index.to_bytes(4, "big")
        digest = hmac.new(self.chain_code, data, hashlib.sha512).digest()
        tweak, child_chain_code = digest[:32], digest[32:]

        # Child key = parent point + tweak * G
        parent = PublicKey(self.public_key)
        child_point = parent.add(tweak)

        return ExtendedKey(child_point.format(compressed=True), child_chain_code)

    def derive_path(self, *indices: int) -> "ExtendedKey":
        node = self
        for index in indices:
            node = node.child(index)
        return node


# ---------------------------------------------------------- address encoding


def p2pkh(public_key: bytes) -> str:
    """Legacy. Addresses start with 1."""
    return b58check_encode(b"\x00" + hash160(public_key))


def p2sh_p2wpkh(public_key: bytes) -> str:
    """Nested SegWit — a witness program wrapped in a P2SH. Starts with 3."""
    witness_script = b"\x00\x14" + hash160(public_key)
    return b58check_encode(b"\x05" + hash160(witness_script))


def p2wpkh(public_key: bytes) -> str:
    """Native SegWit. Starts with bc1q."""
    return bech32_encode("bc", 0, hash160(public_key))


def p2tr(public_key: bytes) -> str:
    """
    Taproot. Starts with bc1p.

    The address commits to a tweaked key, not the raw one: the internal key is
    shifted by a hash of itself so that a key-path spend cannot be confused with
    a script-path spend. Skipping the tweak produces a valid-looking address that
    belongs to nobody.
    """
    internal = public_key[1:33]  # x-only
    tweak = tagged_hash("TapTweak", internal)

    point = PublicKey(public_key)
    # BIP340 keys are implicitly even-y; lift to the even representation first.
    if public_key[0] == 0x03:
        point = PublicKey(b"\x02" + internal)

    output = point.add(tweak)
    return bech32_encode("bc", 1, output.format(compressed=True)[1:33])


ENCODERS = {
    "Legacy": p2pkh,
    "Nested SegWit": p2sh_p2wpkh,
    "Native SegWit": p2wpkh,
    "Taproot": p2tr,
}


def encode_address(public_key: bytes, address_type: str) -> str:
    encoder = ENCODERS.get(address_type)
    if not encoder:
        raise RequestError("UNSUPPORTED_INPUT", f"Unknown address type: {address_type}")
    return encoder(public_key)


def derive_addresses(
    extended_key: str,
    address_type: str,
    change: int,
    start: int,
    count: int,
) -> list[str]:
    """
    Derive `count` addresses on one branch, starting at `start`.
    `change` is 0 for receive addresses, 1 for change.

    Only the last two path levels apply here — the account path is already baked
    into the extended key the user gave us.
    """
    branch = ExtendedKey.parse(extended_key).child(change)
    return [
        encode_address(branch.child(i).public_key, address_type)
        for i in range(start, start + count)
    ]
