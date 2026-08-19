"""
Tests for wallet input detection.

This is the highest-value thing in the codebase to test: it is pure, it runs in
microseconds, and it is the security boundary that stops a private key or a
recovery phrase from ever reaching the database.

It is also where a real bug lived — both the xpub and descriptor branches
passed a keyword the dataclass does not accept, so every extended public key
returned a 500. Any one of these tests would have caught it the moment it was
written.
"""

import pytest

from app.services import detection

XPUB = ("xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKrhko4egpiMZbpiaQL2jkwSB1icqYh2cf"
        "DfVxdx4df189oLKnC5fSwqPfgyP3hooxujYzAu3fDVmz")
ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADq"
        "tfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs")


class TestValidInputs:
    """Every input type the product accepts must round-trip."""

    def test_xpub_is_bitcoin(self):
        d = detection.detect(XPUB)
        assert d.valid
        assert d.input_type == "xpub"
        assert d.chain == "bitcoin"

    def test_ambiguous_xpub_defers_address_type_to_the_chain(self):
        # A bare xpub predates SLIP-132, so the prefix cannot say which address
        # type it is. Guessing wrong shows a zero balance, which to a user looks
        # exactly like losing their money — so we probe instead and say so.
        d = detection.detect(XPUB)
        assert d.address_type is None
        assert d.warning is not None

    def test_zpub_carries_its_address_type(self):
        d = detection.detect(ZPUB)
        assert d.valid
        assert d.address_type == "Native SegWit"
        assert d.warning is None

    def test_descriptor(self):
        d = detection.detect(f"wpkh({XPUB}/0/*)")
        assert d.valid
        assert d.input_type == "descriptor"
        assert d.address_type == "Native SegWit"

    def test_taproot_descriptor(self):
        assert detection.detect(f"tr({XPUB}/0/*)").address_type == "Taproot"

    @pytest.mark.parametrize("address,expected_type", [
        ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "Legacy"),
        ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "Native SegWit"),
    ])
    def test_bitcoin_addresses(self, address, expected_type):
        d = detection.detect(address)
        assert d.valid and d.chain == "bitcoin"
        assert d.address_type == expected_type

    def test_evm_address(self):
        d = detection.detect("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        assert d.valid and d.chain == "ethereum"
        # The same address is valid on Base, and the user should be told.
        assert "Base" in (d.warning or "")

    def test_tron_address(self):
        d = detection.detect("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        assert d.valid and d.chain == "tron"


class TestSecretsAreRejected:
    """
    A watch-only app must never accept something that can spend. These run
    before any format matching, so a secret cannot be mistaken for an address.
    """

    def test_seed_phrase(self):
        phrase = " ".join(["abandon"] * 11 + ["about"])
        d = detection.detect(phrase)
        assert not d.valid
        assert d.error.code == "REJECTED_SECRET"

    def test_extended_private_key(self):
        # One character different from xpub, which is exactly why this matters.
        xprv = ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiCh"
                "kVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi")
        assert detection.detect(xprv).error.code == "REJECTED_SECRET"

    def test_raw_hex_private_key(self):
        key = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
        assert detection.detect(key).error.code == "REJECTED_SECRET"

    def test_secret_is_never_echoed_back(self):
        # The message must not contain the secret itself — it would end up in
        # logs and error trackers.
        phrase = " ".join(["abandon"] * 11 + ["about"])
        assert "abandon" not in detection.detect(phrase).error.message


class TestChecksums:
    """
    A checksum failure must be loud. Without the check, one mistyped character
    yields a plausible key that silently shows a zero balance — the worst
    outcome, because to the user it is indistinguishable from lost funds.
    """

    def test_mistyped_xpub(self):
        d = detection.detect(XPUB[:-1] + "y")
        assert not d.valid
        assert d.error.code == "INVALID_CHECKSUM"

    def test_mistyped_tron_address(self):
        d = detection.detect("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")
        assert not d.valid
        assert d.error.code == "INVALID_CHECKSUM"

    def test_empty_input(self):
        assert not detection.detect("   ").valid

    def test_unrecognised_input(self):
        assert detection.detect("hello world").error.code == "UNSUPPORTED_INPUT"
