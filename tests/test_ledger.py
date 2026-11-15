import pytest

from homebase.ledger import DISCOUNT_ACCOUNT, assert_journal_balanced, ingest_receipt
from homebase.util import LedgerImbalanceError, to_cents

from tests.conftest import SAMS, WALMART, journal_map


def test_to_cents_uses_decimal_half_up():
    assert to_cents("2.675") == 268
    assert to_cents(2.675) == 268
    assert to_cents("1.005") == 101
    assert to_cents(3) == 300
    assert to_cents("-42.93") == -4293


def test_balanced_receipt_has_no_discount_line(conn):
    txn_id = ingest_receipt(conn, WALMART)
    lines = journal_map(conn, txn_id)
    assert sum(lines.values()) == 0
    assert DISCOUNT_ACCOUNT not in lines
    assert lines["Groceries"] == 1436
    assert lines["Household Supplies"] == 997
    assert lines["HSA Medical"] == 1646
    assert lines["Sales Tax"] == 214
    assert lines["Checking Account"] == -4293
    assert "Credit Card" not in lines


def test_sams_club_posts_explicit_discount(conn):
    txn_id = ingest_receipt(conn, SAMS)
    lines = journal_map(conn, txn_id)
    assert sum(lines.values()) == 0
    assert lines["Groceries"] == 1580
    assert lines["HSA Medical"] == 2248
    assert lines["Sales Tax"] == 187
    assert lines[DISCOUNT_ACCOUNT] == -228
    assert lines["Credit Card"] == -3787
    # The old v4 note hid this 228 cents inside Groceries (1580 - 228 = 1352).
    assert lines["Groceries"] != 1352


def test_surcharge_posts_positive_adjustment(conn):
    txn_id = ingest_receipt(
        conn,
        {
            "date": "2026-10-01",
            "merchant": "Corner Store",
            "payment_method": "Cash",
            "items": [{"name": "Whole Milk", "qty": 1, "price": 4.00}],
            "tax": 0,
            "total": 4.50,
        },
    )
    lines = journal_map(conn, txn_id)
    assert sum(lines.values()) == 0
    assert lines["Groceries"] == 400
    assert lines[DISCOUNT_ACCOUNT] == 50
    assert lines["Checking Account"] == -450
    assert "Credit Card" not in lines


def test_hsa_debit_card_is_not_a_checking_substring_match(conn):
    txn_id = ingest_receipt(
        conn,
        {
            "date": "2026-10-02",
            "merchant": "Pharmacy",
            "payment_method": "HSA Debit Card",
            "items": [{"name": "Ibuprofen 200mg 100ct", "qty": 1, "price": 4.00}],
            "tax": 0,
            "total": 4.00,
        },
    )
    lines = journal_map(conn, txn_id)
    assert lines["HSA Account"] == -400
    assert "Checking Account" not in lines
    assert lines["HSA Medical"] == 400


def test_explicit_category_overrides_default_groceries(conn):
    txn_id = ingest_receipt(
        conn,
        {
            "date": "2026-10-03",
            "merchant": "Hardware",
            "payment_method": "Credit Card",
            "items": [{"name": "Furnace filter", "qty": 1, "price": 15.00, "category": "maintenance"}],
            "tax": 1.00,
            "total": 16.00,
        },
    )
    lines = journal_map(conn, txn_id)
    assert lines["Home Maintenance"] == 1500
    assert "Groceries" not in lines
    assert sum(lines.values()) == 0


def test_imbalance_raises_a_real_exception(conn):
    conn.execute("INSERT INTO merchants (name) VALUES ('X')")
    merchant_id = conn.execute("SELECT merchant_id FROM merchants WHERE name = 'X'").fetchone()[0]
    conn.execute(
        """
        INSERT INTO transactions
            (date, merchant_id, payment_method, raw_subtotal, calculated_tax, total_amount)
        VALUES ('2026-01-01', ?, 'Cash', 100, 0, 100)
        """,
        (merchant_id,),
    )
    txn_id = conn.execute("SELECT transaction_id FROM transactions").fetchone()[0]
    checking = conn.execute(
        "SELECT account_id FROM accounts WHERE account_name = 'Checking Account'"
    ).fetchone()[0]
    groceries = conn.execute("SELECT account_id FROM accounts WHERE account_name = 'Groceries'").fetchone()[0]
    conn.execute(
        "INSERT INTO journal_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
        (txn_id, groceries, 100),
    )
    conn.execute(
        "INSERT INTO journal_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
        (txn_id, checking, -40),
    )
    with pytest.raises(LedgerImbalanceError) as caught:
        assert_journal_balanced(conn, txn_id)
    assert caught.value.balance_cents == 60
    assert not isinstance(caught.value, AssertionError)
