"""
Tests for what a Bitcoin transaction actually did to a wallet.

Bitcoin has no accounts. A transaction consumes whole previous outputs and
creates new ones, so spending 0.02 BTC from a 1.9 BTC chunk means the entire
1.9 leaves and 1.88 comes straight back as change to a fresh address the same
wallet owns.

Read naively that reports "sent 1.9 BTC", which is alarming and wrong. These
tests pin the arithmetic that stops that happening.
"""

from app.services.monitor import _bitcoin_net_effect

MINE_1 = "bc1qmine00000000000000000000000000000001"
MINE_2 = "bc1qmine00000000000000000000000000000002"
THEIRS = "bc1qthem00000000000000000000000000000001"
OWNED = {MINE_1, MINE_2}


def tx(vin, vout, fee=0):
    return {
        "vin": [{"prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
        "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout],
        "fee": fee,
    }


def test_plain_receive():
    effect = _bitcoin_net_effect(tx([(THEIRS, 500_000)], [(MINE_1, 500_000)]), OWNED)
    assert effect["direction"] == "receive"
    assert effect["sats"] == 500_000
    assert effect["counterparty"] == THEIRS


def test_send_reports_what_left_not_the_whole_input():
    # THE important case. We spend a 1.9 BTC output to pay 0.02, and 1.88 comes
    # back as change. The alert must say 0.02, not 1.9.
    effect = _bitcoin_net_effect(
        tx([(MINE_1, 190_000_000)],
           [(THEIRS, 2_000_000), (MINE_2, 187_990_000)],
           fee=10_000),
        OWNED,
    )
    assert effect["direction"] == "send"
    assert effect["sats"] == 2_000_000
    assert effect["counterparty"] == THEIRS


def test_self_transfer_is_internal_and_only_the_fee_moved():
    # Every output is ours, so nothing left our control at all.
    effect = _bitcoin_net_effect(
        tx([(MINE_1, 100_000)], [(MINE_2, 95_000)], fee=5_000), OWNED
    )
    assert effect["direction"] == "internal"
    assert effect["sats"] == 5_000
    assert effect["counterparty"] is None


def test_transaction_that_does_not_touch_us_is_ignored():
    assert _bitcoin_net_effect(tx([(THEIRS, 1000)], [(THEIRS, 900)]), OWNED) is None


def test_receive_counterparty_is_an_input_address_we_do_not_own():
    # Consolidation payment: one of our own addresses also funded it, so the
    # payer must be found by skipping ours.
    effect = _bitcoin_net_effect(
        tx([(MINE_1, 10_000), (THEIRS, 90_000)], [(MINE_2, 99_000)]), OWNED
    )
    assert effect["direction"] == "receive"
    assert effect["sats"] == 89_000  # net: 99,000 in minus 10,000 of our own
    assert effect["counterparty"] == THEIRS


def test_sweep_of_entire_balance_has_no_change_output():
    effect = _bitcoin_net_effect(
        tx([(MINE_1, 50_000)], [(THEIRS, 45_000)], fee=5_000), OWNED
    )
    assert effect["direction"] == "send"
    assert effect["sats"] == 45_000


def test_missing_prevout_does_not_crash():
    # Esplora occasionally omits prevout on coinbase-like inputs.
    raw = {"vin": [{}], "vout": [{"scriptpubkey_address": MINE_1, "value": 1000}],
           "fee": 0}
    effect = _bitcoin_net_effect(raw, OWNED)
    assert effect["direction"] == "receive"
    assert effect["sats"] == 1000
