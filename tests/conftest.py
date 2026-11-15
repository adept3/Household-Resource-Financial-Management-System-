import pytest

from homebase import open_db

WALMART = {
    "date": "2026-09-20",
    "merchant": "Walmart",
    "payment_method": "Debit Card",
    "items": [
        {"name": "GV Whole Milk 1 Gal", "qty": 2, "price": 3.48},
        {"name": "Ibuprofen 200mg 100ct", "qty": 1, "price": 8.97},
        {"name": "GV Butter 4pk", "qty": 1, "price": 4.28},
        {"name": "Dozen Eggs Large", "qty": 1, "price": 3.12},
        {"name": "GV Paper Towels 6pk", "qty": 1, "price": 9.97, "category": "Household Supplies"},
        {"name": "Band-Aid Flexible 100ct", "qty": 1, "price": 7.49},
    ],
    "tax": 2.14,
    "total": 42.93,
}

SAMS = {
    "date": "2026-09-22",
    "merchant": "Sam's Club",
    "payment_method": "Credit Card",
    "items": [
        {"name": "Zyrtec 24hr 30ct", "qty": 1, "price": 22.48},
        {"name": "GV Whole Milk 1 Gal", "qty": 3, "price": 3.28},
        {"name": "Dozen Eggs Large", "qty": 2, "price": 2.98},
    ],
    "tax": 1.87,
    "total": 37.87,
}


@pytest.fixture
def conn(tmp_path):
    connection = open_db(tmp_path / "ledger.db")
    try:
        yield connection
    finally:
        connection.close()


def journal_map(connection, txn_id):
    rows = connection.execute(
        """
        SELECT a.account_name, SUM(je.amount)
        FROM journal_entries je
        JOIN accounts a ON a.account_id = je.account_id
        WHERE je.transaction_id = ?
        GROUP BY a.account_name
        """,
        (txn_id,),
    ).fetchall()
    return {name: amount for name, amount in rows}
