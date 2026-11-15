"""Throwaway end-to-end demo. Does not open or delete the real ledger."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from homebase.bank import import_bank_csv
from homebase.bills import add_subscription, list_subscriptions
from homebase.compose import generate_docker_compose
from homebase.db import default_db_path, open_db
from homebase.inventory import (
    consume_product,
    get_inventory_report,
    map_receipt_item_to_product,
    physical_inventory_count,
    set_low_stock_threshold,
)
from homebase.ledger import (
    DISCOUNT_ACCOUNT,
    get_account_balances,
    get_hsa_summary,
    get_tax_summary,
    ingest_receipt,
    journal_lines,
)
from homebase.notify import check_and_notify_alerts
from homebase.util import LedgerImbalanceError, fmt_money


def run_demo() -> None:
    real_ledger = default_db_path()
    print("HomeBase demo")
    print(f"Real ledger (not opened): {real_ledger}")
    with tempfile.TemporaryDirectory(prefix="homebase-demo-") as directory:
        conn = open_db(Path(directory) / "demo.db")
        try:
            _run(conn)
        finally:
            conn.close()
    print("Demo database discarded. The real ledger was not modified.")


def _run(conn) -> None:
    mappings = [
        ("GV Whole Milk 1 Gal", "Whole Milk", 1),
        ("Ibuprofen 200mg 100ct", "Ibuprofen", 1),
        ("GV Butter 4pk", "Butter (stick)", 4),
        ("Dozen Eggs Large", "Eggs", 12),
        ("GV Paper Towels 6pk", "Paper Towels (roll)", 6),
        ("Band-Aid Flexible 100ct", "Band-Aids", 100),
        ("Zyrtec 24hr 30ct", "Zyrtec", 30),
    ]
    for raw, product, multiplier in mappings:
        map_receipt_item_to_product(conn, raw, product, multiplier)
    set_low_stock_threshold(conn, "Butter (stick)", 2)
    set_low_stock_threshold(conn, "Eggs", 6)
    set_low_stock_threshold(conn, "Paper Towels (roll)", 2)

    walmart = ingest_receipt(
        conn,
        {
            "date": "2026-09-20",
            "merchant": "Walmart",
            "payment_method": "Debit Card",
            "items": [
                {"name": "GV Whole Milk 1 Gal", "sku": "001234567", "qty": 2, "price": 3.48},
                {"name": "Ibuprofen 200mg 100ct", "sku": "009876543", "qty": 1, "price": 8.97},
                {"name": "GV Butter 4pk", "sku": "001112233", "qty": 1, "price": 4.28},
                {"name": "Dozen Eggs Large", "sku": "004455667", "qty": 1, "price": 3.12},
                {"name": "GV Paper Towels 6pk", "sku": "007788990", "qty": 1, "price": 9.97, "category": "Household Supplies"},
                {"name": "Band-Aid Flexible 100ct", "sku": "002233445", "qty": 1, "price": 7.49},
            ],
            "tax": 2.14,
            "total": 42.93,
        },
    )
    sams = ingest_receipt(
        conn,
        {
            "date": "2026-09-22",
            "merchant": "Sam's Club",
            "payment_method": "Credit Card",
            "items": [
                {"name": "Zyrtec 24hr 30ct", "sku": "00ZYRT30", "qty": 1, "price": 22.48},
                {"name": "GV Whole Milk 1 Gal", "sku": "001234567", "qty": 3, "price": 3.28},
                {"name": "Dozen Eggs Large", "sku": "004455667", "qty": 2, "price": 2.98},
            ],
            "tax": 1.87,
            "total": 37.87,
        },
    )

    print("\nJournal")
    for txn_id in (walmart, sams):
        lines = journal_lines(conn, txn_id)
        balance = sum(int(line["amount_cents"]) for line in lines)
        if balance != 0:
            raise LedgerImbalanceError(txn_id, balance)
        print(f"  transaction {txn_id}: balanced")
        for line in lines:
            print(f"    {line['account']:<28} {line['amount']:>10}")
    sams_lines = {line["account"]: line["amount_cents"] for line in journal_lines(conn, sams)}
    if sams_lines.get(DISCOUNT_ACCOUNT) != -228:
        raise LedgerImbalanceError(sams, int(sams_lines.get(DISCOUNT_ACCOUNT) or 0))
    print("  Sam's Club discount posted separately: " + fmt_money(-228))

    print("\nBalances")
    for row in get_account_balances(conn):
        if row["balance_cents"] != 0:
            print(f"  {row['account']:<28} {row['type']:<10} {row['balance']}")

    print("\nBank CSV")
    result = import_bank_csv(
        conn,
        "Date,Description,Amount,Reference\n"
        "2026-09-20,WALMART SUPERCENTER,-42.93,CHK9920\n"
        "2026-09-22,SAMS CLUB,-37.87,CHK9922\n"
        "2026-09-21,PAYROLL DIRECT DEP,2850.00,DD2609\n",
    )
    print(
        f"  imported {result['imported']}, matched {result['matched']}, "
        f"skipped {result['skipped']}, rejected {len(result['rejected'])}, "
        f"unmatched {len(result['unmatched'])}"
    )
    if result["matched"] != 2 or result["unmatched"][0]["description"] != "PAYROLL DIRECT DEP":
        raise RuntimeError("bank reconciliation did not match the two purchases only")

    consume_product(conn, "Whole Milk", 3)
    consume_product(conn, "Eggs", 8)
    consume_product(conn, "Butter (stick)", 3)
    consume_product(conn, "Paper Towels (roll)", 5)
    adjustments = physical_inventory_count(
        conn, {"Whole Milk": 2, "Eggs": 30, "Butter (stick)": 1}, "September audit"
    )
    print("\nPantry")
    for adj in adjustments:
        print(f"  count {adj['product']}: {adj['was']} -> {adj['now']} ({adj['adj']:+d})")
    for item in get_inventory_report(conn):
        print(f"  {item['product']:<24} stock {item['stock']:>4}  {item['status']}")

    today = date.today()
    print("\nSubscriptions (next due is never in the past at creation)")
    add_subscription(conn, "Netflix", 15.99, "monthly", 5, "streaming", True, today=today)
    add_subscription(conn, "Electric Bill", 125.00, "monthly", 20, "utility", True, today=today)
    add_subscription(conn, "Car Insurance", 480.00, "quarterly", 1, "insurance", True, today=today)
    for bill in list_subscriptions(conn):
        if bill["due"] < today.isoformat():
            raise RuntimeError(f"{bill['name']} next_due {bill['due']} is in the past")
        print(f"  {bill['due']}  {bill['name']:<16} {bill['amount']}")

    hsa = get_hsa_summary(conn, 2026)
    tax = get_tax_summary(conn, 2026)
    print(f"\nHSA eligible {hsa['total']} ({hsa['count']} items); sales tax {tax['total']}")

    print("\nNotification preview (amounts omitted)")
    alerts = check_and_notify_alerts(conn, days=40, include_amounts=False, today=today)
    for line in alerts:
        if "$" in line:
            raise RuntimeError("notification included a dollar amount: " + line)
        print("  " + line)

    compose = generate_docker_compose()
    baked_secret = "change" + "me"
    if baked_secret in compose.lower() or "privileged:" in compose:
        raise RuntimeError("compose generator regressed")
    print(f"\nOptional compose: {compose.count('image:')} pinned services, all profile 'later' (off).")
    audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    print(f"Audit log rows: {audit_count}")
