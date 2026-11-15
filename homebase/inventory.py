"""Pantry inventory: receipt mappings, stock, and physical counts."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from homebase.db import atomic, audit
from homebase.util import ValidationError, sanitize_str


def get_or_create_product(conn: sqlite3.Connection, name: str, threshold: int = 1) -> int:
    name = sanitize_str(name, "product", max_len=120)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise ValidationError("threshold must be an integer >= 0")
    with atomic(conn):
        row = conn.execute("SELECT product_id FROM products WHERE display_name = ?", (name,)).fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            "INSERT INTO products (display_name, low_stock_threshold) VALUES (?, ?)",
            (name, threshold),
        )
        conn.execute(
            "INSERT INTO inventory (product_id, current_stock) VALUES (?, 0)",
            (cur.lastrowid,),
        )
        return int(cur.lastrowid)


def map_receipt_item_to_product(
    conn: sqlite3.Connection, raw_name: str, product: str, multiplier: int = 1, threshold: int = 1
) -> int:
    raw_name = sanitize_str(raw_name, "raw_item_name", max_len=200)
    if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier <= 0:
        raise ValidationError("multiplier must be an integer > 0")
    with atomic(conn):
        product_id = get_or_create_product(conn, product, threshold)
        conn.execute(
            """
            INSERT INTO receipt_product_mappings (raw_item_name, product_id, multiplier)
            VALUES (?, ?, ?)
            ON CONFLICT(raw_item_name) DO UPDATE SET product_id = excluded.product_id,
                                                    multiplier = excluded.multiplier
            """,
            (raw_name, product_id, multiplier),
        )
    return product_id


def set_low_stock_threshold(conn: sqlite3.Connection, name: str, threshold: int) -> None:
    name = sanitize_str(name, "product", max_len=120)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise ValidationError("threshold must be an integer >= 0")
    with atomic(conn):
        cur = conn.execute(
            "UPDATE products SET low_stock_threshold = ? WHERE display_name = ?",
            (threshold, name),
        )
        if cur.rowcount != 1:
            raise ValidationError(f"Unknown product {name!r}")


def update_inventory_from_purchase(conn: sqlite3.Connection, raw_item_name: str, qty: int) -> None:
    """Increase stock when a mapped receipt line is purchased. No commit."""
    row = conn.execute(
        "SELECT product_id, multiplier FROM receipt_product_mappings WHERE raw_item_name = ?",
        (raw_item_name,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE inventory SET current_stock = current_stock + ? WHERE product_id = ?",
        (qty * row[1], row[0]),
    )


def consume_product(conn: sqlite3.Connection, name: str, qty: int = 1) -> int:
    name = sanitize_str(name, "product", max_len=120)
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise ValidationError("qty must be an integer > 0")
    with atomic(conn):
        row = conn.execute(
            """
            SELECT p.product_id, i.current_stock
            FROM products p JOIN inventory i ON p.product_id = i.product_id
            WHERE p.display_name = ?
            """,
            (name,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"Unknown product {name!r}")
        new_stock = max(0, row[1] - qty)
        conn.execute(
            "UPDATE inventory SET current_stock = ? WHERE product_id = ?",
            (new_stock, row[0]),
        )
    return new_stock


def physical_inventory_count(
    conn: sqlite3.Connection, counts: dict[str, int], notes: str = ""
) -> list[dict[str, object]]:
    if not isinstance(counts, dict) or not counts:
        raise ValidationError("counts must be a non-empty mapping of product to quantity")
    note = sanitize_str(notes, "notes", max_len=200, allow_empty=True) if notes != "" else ""
    today = date.today().isoformat()
    adjustments: list[dict[str, object]] = []
    with atomic(conn):
        for name, counted in counts.items():
            clean = sanitize_str(name, "product", max_len=120)
            if isinstance(counted, bool) or not isinstance(counted, int) or counted < 0:
                raise ValidationError(f"counted stock for {clean!r} must be an integer >= 0")
            row = conn.execute(
                """
                SELECT p.product_id, i.current_stock
                FROM products p JOIN inventory i ON p.product_id = i.product_id
                WHERE p.display_name = ?
                """,
                (clean,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Unknown product {clean!r}")
            delta = counted - row[1]
            conn.execute(
                """
                INSERT INTO inventory_audits
                    (product_id, audit_date, previous_stock, counted_stock, adjustment, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row[0], today, row[1], counted, delta, note),
            )
            conn.execute(
                "UPDATE inventory SET current_stock = ?, last_audit_date = ? WHERE product_id = ?",
                (counted, today, row[0]),
            )
            adjustments.append({"product": clean, "was": row[1], "now": counted, "adj": delta})
        audit(
            conn,
            "inventory_count",
            "inventory_audit",
            None,
            json.dumps({"items": [item["product"] for item in adjustments]}),
        )
    return adjustments


def get_inventory_report(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT p.display_name, i.current_stock, p.low_stock_threshold,
               CASE WHEN i.current_stock <= p.low_stock_threshold THEN 'LOW' ELSE 'OK' END
        FROM products p JOIN inventory i ON p.product_id = i.product_id
        ORDER BY 4 DESC, 1
        """
    ).fetchall()
    return [
        {"product": row[0], "stock": row[1], "threshold": row[2], "status": row[3]}
        for row in rows
    ]


def get_low_stock_alerts(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT p.display_name, i.current_stock, p.low_stock_threshold
        FROM products p JOIN inventory i ON p.product_id = i.product_id
        WHERE i.current_stock <= p.low_stock_threshold
        ORDER BY p.display_name
        """
    ).fetchall()
    return [{"product": row[0], "stock": row[1], "threshold": row[2]} for row in rows]
