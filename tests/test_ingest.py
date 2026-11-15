import pytest

from homebase.inventory import map_receipt_item_to_product
from homebase.ledger import ingest_receipt
from homebase.util import ValidationError

from tests.conftest import WALMART


def _counts(conn):
    tables = ("transactions", "transaction_items", "journal_entries", "merchants", "audit_log")
    return {
        name: conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
        for name in tables
    }


def test_invalid_receipt_writes_nothing(conn):
    before = _counts(conn)
    with pytest.raises(ValidationError):
        ingest_receipt(conn, {"merchant": "Walmart"})
    assert _counts(conn) == before
    with pytest.raises(ValidationError):
        ingest_receipt(
            conn,
            {
                "date": "09/20/2026",
                "merchant": "Walmart",
                "payment_method": "Debit Card",
                "items": [{"name": "Milk", "qty": 1, "price": 1}],
                "tax": 0,
                "total": 1,
            },
        )
    with pytest.raises(ValidationError):
        ingest_receipt(
            conn,
            {
                "date": "2026-09-20",
                "merchant": "Walmart",
                "payment_method": "Debit Card",
                "items": [{"name": "Milk", "qty": 0, "price": 1}],
                "tax": 0,
                "total": 0,
            },
        )
    with pytest.raises(ValidationError):
        ingest_receipt(
            conn,
            {
                "date": "2026-09-20",
                "merchant": "Walmart",
                "payment_method": "Debit Card",
                "items": [{"name": "Milk", "qty": 1, "price": -0.01}],
                "tax": 0,
                "total": 0,
            },
        )
    with pytest.raises(ValidationError):
        ingest_receipt(
            conn,
            {
                "date": "2026-09-20",
                "merchant": "\x00",
                "payment_method": "Debit Card",
                "items": [{"name": "Milk", "qty": 1, "price": 1}],
                "tax": 0,
                "total": 1,
            },
        )
    with pytest.raises(ValidationError):
        ingest_receipt(
            conn,
            {
                "date": "2026-13-40",
                "merchant": "Walmart",
                "payment_method": "Store Card",
                "items": [{"name": "Milk", "qty": 1, "price": 1}],
                "tax": 0,
                "total": 1,
            },
        )
    assert _counts(conn) == before


def test_unknown_payment_method_is_not_guessed(conn):
    with pytest.raises(ValidationError):
        ingest_receipt(
            conn,
            {
                "date": "2026-09-20",
                "merchant": "Walmart",
                "payment_method": "prepaid debit something",
                "items": [{"name": "Milk", "qty": 1, "price": 3}],
                "tax": 0,
                "total": 3,
            },
        )
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_ingest_rolls_back_when_journal_posting_fails(conn, monkeypatch):
    map_receipt_item_to_product(conn, "GV Whole Milk 1 Gal", "Whole Milk", 1)
    product_id = conn.execute(
        "SELECT product_id FROM products WHERE display_name = 'Whole Milk'"
    ).fetchone()[0]

    def boom(_connection, _txn_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("homebase.ledger.post_journal", boom)
    with pytest.raises(RuntimeError, match="boom"):
        ingest_receipt(conn, WALMART)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transaction_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    stock = conn.execute(
        "SELECT current_stock FROM inventory WHERE product_id = ?",
        (product_id,),
    ).fetchone()[0]
    assert stock == 0


def test_failed_second_receipt_keeps_the_first(conn, monkeypatch):
    first = ingest_receipt(conn, WALMART)

    def boom(_connection, _txn_id):
        raise RuntimeError("boom")

    monkeypatch.setattr("homebase.ledger.post_journal", boom)
    with pytest.raises(RuntimeError):
        ingest_receipt(conn, WALMART)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 1
    assert conn.execute("SELECT transaction_id FROM transactions").fetchone()[0] == first


def test_successful_ingest_updates_mapped_stock_and_audits(conn):
    map_receipt_item_to_product(conn, "GV Whole Milk 1 Gal", "Whole Milk", 1)
    ingest_receipt(conn, WALMART)
    stock = conn.execute(
        """
        SELECT i.current_stock FROM inventory i
        JOIN products p ON p.product_id = i.product_id
        WHERE p.display_name = 'Whole Milk'
        """
    ).fetchone()[0]
    assert stock == 2
    actions = [row[0] for row in conn.execute("SELECT action FROM audit_log").fetchall()]
    assert "ingest_receipt" in actions
