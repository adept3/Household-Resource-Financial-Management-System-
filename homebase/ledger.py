"""Double-entry receipt ledger.

Item prices are posted to the account each line names. When the receipt total
is not the sum of lines plus tax, the difference is an explicit
Discounts & Adjustments line. The journal is then checked and a real
LedgerImbalanceError is raised if it does not sum to zero.

Payment accounts come from an exact method name. Substring matching is not
used, so "HSA Debit Card" credits the HSA account rather than checking.
"""

from __future__ import annotations

import json
import re
import sqlite3

from homebase.db import atomic, audit, get_account_id
from homebase.inventory import update_inventory_from_purchase
from homebase.util import (
    LedgerImbalanceError,
    ValidationError,
    fmt_money,
    parse_iso_date,
    sanitize_str,
    to_cents,
)

PAYMENT_ACCOUNTS = {
    "cash": "Checking Account",
    "checking": "Checking Account",
    "debit": "Checking Account",
    "debit card": "Checking Account",
    "ach": "Checking Account",
    "credit": "Credit Card",
    "credit card": "Credit Card",
    "hsa": "HSA Account",
    "hsa card": "HSA Account",
    "hsa debit": "HSA Account",
    "hsa debit card": "HSA Account",
}

CATEGORY_ACCOUNTS = {
    "groceries": "Groceries",
    "grocery": "Groceries",
    "household": "Household Supplies",
    "household supplies": "Household Supplies",
    "healthcare": "Healthcare",
    "hsa": "HSA Medical",
    "hsa medical": "HSA Medical",
    "medical": "HSA Medical",
    "utilities": "Utilities",
    "auto": "Auto/Fuel",
    "auto/fuel": "Auto/Fuel",
    "fuel": "Auto/Fuel",
    "subscriptions": "Subscriptions",
    "maintenance": "Home Maintenance",
    "home maintenance": "Home Maintenance",
}

TAX_ACCOUNT = "Sales Tax"
DISCOUNT_ACCOUNT = "Discounts & Adjustments"
HSA_ACCOUNT = "HSA Medical"
DEFAULT_EXPENSE = "Groceries"
_YEAR = re.compile(r"\d{4}")


def payment_account(method: str) -> str:
    key = method.strip().lower()
    try:
        return PAYMENT_ACCOUNTS[key]
    except KeyError as exc:
        known = ", ".join(sorted(PAYMENT_ACCOUNTS))
        raise ValidationError(f"Unknown payment method {method!r}. Expected one of: {known}") from exc


def expense_account(category: str) -> str:
    key = category.strip().lower()
    try:
        return CATEGORY_ACCOUNTS[key]
    except KeyError as exc:
        known = ", ".join(sorted(set(CATEGORY_ACCOUNTS.values())))
        raise ValidationError(f"Unknown category {category!r}. Expected one of: {known}") from exc


def check_hsa_eligible(conn: sqlite3.Connection, item_name: str) -> tuple[bool, str | None]:
    row = conn.execute(
        "SELECT category FROM hsa_catalog WHERE UPPER(?) LIKE UPPER(match_pattern) LIMIT 1",
        (item_name,),
    ).fetchone()
    if row:
        return True, row[0]
    return False, None


def add_hsa_pattern(conn: sqlite3.Connection, pattern: str, category: str) -> None:
    pattern = sanitize_str(pattern, "pattern", max_len=80)
    category = sanitize_str(category, "category", max_len=40)
    if "%" not in pattern:
        pattern = "%" + pattern + "%"
    with atomic(conn):
        conn.execute(
            "INSERT OR IGNORE INTO hsa_catalog (match_pattern, category) VALUES (?, ?)",
            (pattern, category),
        )


def _classify(conn: sqlite3.Connection, name: str, category: str | None) -> tuple[str, int]:
    catalog_hit, _category = check_hsa_eligible(conn, name)
    if category:
        account = expense_account(category)
    elif catalog_hit:
        account = HSA_ACCOUNT
    else:
        account = DEFAULT_EXPENSE
    return account, 1 if account == HSA_ACCOUNT else 0


def validate_receipt(receipt: object) -> dict[str, object]:
    if not isinstance(receipt, dict):
        raise ValidationError("receipt must be an object")
    missing = [key for key in ("date", "merchant", "payment_method", "items", "tax", "total") if key not in receipt]
    if missing:
        raise ValidationError("receipt missing required keys: " + ", ".join(missing))
    parsed_date = parse_iso_date(receipt["date"], "date")
    merchant = sanitize_str(receipt["merchant"], "merchant", max_len=120)
    method = sanitize_str(receipt["payment_method"], "payment_method", max_len=40)
    payment_account(method)
    items = receipt["items"]
    if not isinstance(items, list) or not items:
        raise ValidationError("items must be a non-empty list")
    if len(items) > 500:
        raise ValidationError("too many items on one receipt")
    tax = to_cents(receipt["tax"])
    total = to_cents(receipt["total"])
    if tax < 0:
        raise ValidationError("tax must be >= 0")
    if total < 0:
        raise ValidationError("total must be >= 0")
    cleaned_items = []
    subtotal = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError(f"items[{index}] must be an object")
        for key in ("name", "price"):
            if key not in item:
                raise ValidationError(f"items[{index}] missing {key}")
        name = sanitize_str(item["name"], f"items[{index}].name", max_len=200)
        if "qty" not in item or item["qty"] is None:
            qty = 1
        else:
            qty = item["qty"]
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValidationError(f"items[{index}].qty must be an integer > 0")
        price = to_cents(item["price"])
        if price < 0:
            raise ValidationError(f"items[{index}].price must be >= 0")
        sku = None
        if item.get("sku") not in (None, ""):
            sku = sanitize_str(item["sku"], f"items[{index}].sku", max_len=64)
        category = None
        if item.get("category") not in (None, ""):
            category = sanitize_str(item["category"], f"items[{index}].category", max_len=40)
            expense_account(category)
        subtotal += price * qty
        cleaned_items.append(
            {"name": name, "qty": qty, "price": price, "sku": sku, "category": category}
        )
    return {
        "date": parsed_date.isoformat(),
        "merchant": merchant,
        "payment_method": method,
        "items": cleaned_items,
        "tax": tax,
        "total": total,
        "subtotal": subtotal,
    }


def get_or_create_merchant(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT merchant_id FROM merchants WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO merchants (name) VALUES (?)", (name,))
    return int(cur.lastrowid)


def assert_journal_balanced(conn: sqlite3.Connection, txn_id: int) -> None:
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM journal_entries WHERE transaction_id = ?",
        (txn_id,),
    ).fetchone()[0]
    if balance != 0:
        raise LedgerImbalanceError(txn_id, balance)


def post_journal(conn: sqlite3.Connection, txn_id: int) -> None:
    """Post the receipt and require the signed amounts to sum to zero.

    ``gap`` is ``total - (subtotal + tax)``. A members' discount is negative
    and is credited to Discounts & Adjustments. A fee is positive and is
    debited to the same account. The gap is never folded into Groceries.
    """
    txn = conn.execute(
        """
        SELECT raw_subtotal, calculated_tax, total_amount, payment_method
        FROM transactions WHERE transaction_id = ?
        """,
        (txn_id,),
    ).fetchone()
    if txn is None:
        raise ValidationError(f"transaction {txn_id} does not exist")
    subtotal, tax, total, method = txn
    rows = conn.execute(
        "SELECT category, unit_price, quantity FROM transaction_items WHERE transaction_id = ?",
        (txn_id,),
    ).fetchall()
    buckets: dict[str, int] = {}
    item_sum = 0
    for category, unit_price, quantity in rows:
        extended = unit_price * quantity
        item_sum += extended
        buckets[category] = buckets.get(category, 0) + extended
    if item_sum != subtotal:
        raise LedgerImbalanceError(txn_id, item_sum - subtotal)
    entries: list[tuple[str, int]] = []
    for account_name in sorted(buckets):
        amount = buckets[account_name]
        if amount != 0:
            entries.append((account_name, amount))
    if tax:
        entries.append((TAX_ACCOUNT, tax))
    gap = total - (subtotal + tax)
    if gap != 0:
        entries.append((DISCOUNT_ACCOUNT, gap))
    pay = payment_account(method)
    if total != 0 or not entries:
        entries.append((pay, -total))
    for account_name, amount in entries:
        account_id = get_account_id(conn, account_name)
        if account_id is None:
            raise LedgerImbalanceError(txn_id, amount)
        conn.execute(
            "INSERT INTO journal_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
            (txn_id, account_id, amount),
        )
    assert_journal_balanced(conn, txn_id)


def ingest_receipt(conn: sqlite3.Connection, receipt: object) -> int:
    """Validate, then write the receipt, stock, and journal in one transaction."""
    data = validate_receipt(receipt)
    with atomic(conn):
        merchant_id = get_or_create_merchant(conn, str(data["merchant"]))
        cur = conn.execute(
            """
            INSERT INTO transactions
                (date, merchant_id, payment_method, raw_subtotal, calculated_tax, total_amount, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["date"],
                merchant_id,
                data["payment_method"],
                data["subtotal"],
                data["tax"],
                data["total"],
                str(data["merchant"]) + " - " + str(data["date"]),
            ),
        )
        txn_id = int(cur.lastrowid)
        for item in data["items"]:  # type: ignore[union-attr]
            account, hsa_flag = _classify(conn, item["name"], item["category"])
            conn.execute(
                """
                INSERT INTO transaction_items
                    (transaction_id, item_name, sku_or_asin, quantity, unit_price, category, is_hsa_eligible)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (txn_id, item["name"], item["sku"], item["qty"], item["price"], account, hsa_flag),
            )
            update_inventory_from_purchase(conn, item["name"], item["qty"])
        post_journal(conn, txn_id)
        audit(
            conn,
            "ingest_receipt",
            "transaction",
            txn_id,
            json.dumps(
                {
                    "merchant": data["merchant"],
                    "date": data["date"],
                    "total_cents": data["total"],
                    "payment_method": data["payment_method"],
                },
                sort_keys=True,
            ),
        )
    return txn_id


def journal_lines(conn: sqlite3.Connection, txn_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT a.account_name, a.account_type, je.amount
        FROM journal_entries je
        JOIN accounts a ON a.account_id = je.account_id
        WHERE je.transaction_id = ?
        ORDER BY je.entry_id
        """,
        (txn_id,),
    ).fetchall()
    return [
        {"account": row[0], "type": row[1], "amount_cents": row[2], "amount": fmt_money(row[2])}
        for row in rows
    ]


def get_account_balances(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT a.account_name, a.account_type, COALESCE(SUM(je.amount), 0)
        FROM accounts a
        LEFT JOIN journal_entries je ON a.account_id = je.account_id
        GROUP BY a.account_id
        ORDER BY a.account_type, a.account_name
        """
    ).fetchall()
    return [
        {
            "account": row[0],
            "type": row[1],
            "balance_cents": row[2],
            "balance": fmt_money(row[2]),
        }
        for row in rows
    ]


def _year_bounds(year: object) -> tuple[str, str] | None:
    if year is None:
        return None
    if isinstance(year, bool) or not isinstance(year, (int, str)):
        raise ValidationError("year must be YYYY")
    text = str(year)
    if _YEAR.fullmatch(text) is None:
        raise ValidationError("year must be YYYY")
    start = text + "-01-01"
    end = str(int(text) + 1) + "-01-01"
    return start, end


def get_hsa_summary(conn: sqlite3.Connection, year: object = None) -> dict[str, object]:
    bounds = _year_bounds(year)
    sql = """
        SELECT ti.item_name, ti.unit_price * ti.quantity, t.date, m.name
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id = t.transaction_id
        JOIN merchants m ON t.merchant_id = m.merchant_id
        WHERE ti.is_hsa_eligible = 1
    """
    params: list[object] = []
    if bounds is not None:
        sql += " AND t.date >= ? AND t.date < ?"
        params.extend(bounds)
    sql += " ORDER BY t.date, ti.item_id"
    rows = conn.execute(sql, params).fetchall()
    total = sum(row[1] for row in rows)
    return {
        "total_cents": total,
        "total": fmt_money(total),
        "count": len(rows),
        "items": [
            {"name": row[0], "amount_cents": row[1], "amount": fmt_money(row[1]), "date": row[2], "merchant": row[3]}
            for row in rows
        ],
    }


def get_tax_summary(conn: sqlite3.Connection, year: object = None) -> dict[str, object]:
    bounds = _year_bounds(year)
    sql = """
        SELECT m.name, SUM(t.calculated_tax), COUNT(*)
        FROM transactions t
        JOIN merchants m ON t.merchant_id = m.merchant_id
    """
    params: list[object] = []
    if bounds is not None:
        sql += " WHERE t.date >= ? AND t.date < ?"
        params.extend(bounds)
    sql += " GROUP BY m.name ORDER BY 2 DESC, m.name"
    rows = conn.execute(sql, params).fetchall()
    total = sum(row[1] for row in rows)
    return {
        "total_cents": total,
        "total": fmt_money(total),
        "by_merchant": [
            {"merchant": row[0], "tax_cents": row[1], "tax": fmt_money(row[1]), "txns": row[2]}
            for row in rows
        ],
    }
