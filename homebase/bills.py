"""Subscriptions, maintenance reminders, and warranties.

Subscription due dates are computed so a new bill is never already overdue.
``mark_subscription_paid`` advances ``next_due`` by at least one period and
keeps advancing while the result is still in the past.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from homebase.db import atomic, audit
from homebase.util import ValidationError, fmt_money, parse_iso_date, sanitize_str, to_cents

FREQUENCIES = ("weekly", "monthly", "quarterly", "annual")


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, 28))


def first_due_on_or_after(today: date, due_day: int, frequency: str) -> date:
    if frequency not in FREQUENCIES:
        raise ValidationError("frequency must be weekly, monthly, quarterly, or annual")
    if isinstance(due_day, bool) or not isinstance(due_day, int):
        raise ValidationError("due_day must be an integer")
    if frequency == "weekly":
        if not 1 <= due_day <= 7:
            raise ValidationError("weekly due_day must be an ISO weekday from 1 (Monday) to 7 (Sunday)")
        delta = ((due_day - 1) - today.weekday()) % 7
        return today + timedelta(days=delta)
    if not 1 <= due_day <= 28:
        raise ValidationError("due_day must be from 1 to 28")
    if frequency == "annual":
        candidate = date(today.year, today.month, due_day)
        if candidate < today:
            candidate = date(today.year + 1, today.month, due_day)
        return candidate
    if frequency == "monthly":
        candidate = date(today.year, today.month, due_day)
        if candidate < today:
            candidate = add_months(candidate, 1)
        return candidate
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    for step in range(5):
        month_num = quarter_month + step * 3
        year = today.year + (month_num - 1) // 12
        month = (month_num - 1) % 12 + 1
        candidate = date(year, month, due_day)
        if candidate >= today:
            return candidate
    raise ValidationError("could not compute a quarterly due date")


def advance_due(current: date, frequency: str) -> date:
    if frequency == "weekly":
        return current + timedelta(days=7)
    if frequency == "monthly":
        return add_months(current, 1)
    if frequency == "quarterly":
        return add_months(current, 3)
    if frequency == "annual":
        return add_months(current, 12)
    raise ValidationError("frequency must be weekly, monthly, quarterly, or annual")


def add_subscription(
    conn: sqlite3.Connection,
    name: str,
    amount: object,
    frequency: str = "monthly",
    due_day: int = 1,
    category: str = "",
    auto_pay: bool = False,
    today: date | None = None,
) -> str:
    name = sanitize_str(name, "name", max_len=80)
    frequency = sanitize_str(frequency, "frequency", max_len=20).lower()
    cents = to_cents(amount)
    if cents <= 0:
        raise ValidationError("amount must be > 0")
    today = today or date.today()
    next_due = first_due_on_or_after(today, due_day, frequency)
    if next_due < today:
        raise ValidationError("next_due was computed in the past")
    category_text = sanitize_str(category, "category", max_len=40, allow_empty=True) if category else ""
    with atomic(conn):
        try:
            cur = conn.execute(
                """
                INSERT INTO subscriptions
                    (name, amount_cents, frequency, due_day, category, auto_pay, next_due)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, cents, frequency, due_day, category_text, 1 if auto_pay else 0, next_due.isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"subscription {name!r} already exists") from exc
        audit(
            conn,
            "add_subscription",
            "subscription",
            int(cur.lastrowid),
            json.dumps({"name": name, "next_due": next_due.isoformat(), "amount_cents": cents}, sort_keys=True),
        )
    return next_due.isoformat()


def mark_subscription_paid(conn: sqlite3.Connection, name: str, today: date | None = None) -> str:
    name = sanitize_str(name, "name", max_len=80)
    today = today or date.today()
    with atomic(conn):
        row = conn.execute(
            "SELECT sub_id, frequency, next_due FROM subscriptions WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"No subscription named {name!r}")
        nxt = advance_due(date.fromisoformat(row[2]), row[1])
        while nxt < today:
            nxt = advance_due(nxt, row[1])
        conn.execute("UPDATE subscriptions SET next_due = ? WHERE sub_id = ?", (nxt.isoformat(), row[0]))
        audit(
            conn,
            "mark_subscription_paid",
            "subscription",
            row[0],
            json.dumps({"name": name, "next_due": nxt.isoformat()}, sort_keys=True),
        )
    return nxt.isoformat()


def list_subscriptions(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT name, amount_cents, frequency, next_due, auto_pay, category, active
        FROM subscriptions ORDER BY next_due, name
        """
    ).fetchall()
    return [
        {
            "name": row[0],
            "amount_cents": row[1],
            "amount": fmt_money(row[1]),
            "frequency": row[2],
            "due": row[3],
            "auto_pay": bool(row[4]),
            "category": row[5],
            "active": bool(row[6]),
        }
        for row in rows
    ]


def get_upcoming_bills(conn: sqlite3.Connection, days: int = 7, today: date | None = None) -> list[dict[str, object]]:
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValidationError("days must be an integer >= 0")
    today = today or date.today()
    until = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT name, amount_cents, frequency, next_due, auto_pay, category
        FROM subscriptions
        WHERE active = 1 AND next_due <= ?
        ORDER BY next_due, name
        """,
        (until,),
    ).fetchall()
    return [
        {
            "name": row[0],
            "amount_cents": row[1],
            "amount": fmt_money(row[1]),
            "frequency": row[2],
            "due": row[3],
            "auto_pay": bool(row[4]),
            "category": row[5],
        }
        for row in rows
    ]


def monthly_equivalent_cents(cents: int, frequency: str) -> int:
    factors = {"weekly": (52, 12), "monthly": (1, 1), "quarterly": (1, 3), "annual": (1, 12)}
    if frequency not in factors:
        raise ValidationError("frequency must be weekly, monthly, quarterly, or annual")
    numer, denom = factors[frequency]
    return (cents * numer + denom // 2) // denom


def get_monthly_subscription_total(conn: sqlite3.Connection) -> dict[str, object]:
    rows = conn.execute(
        "SELECT frequency, amount_cents FROM subscriptions WHERE active = 1"
    ).fetchall()
    monthly = sum(monthly_equivalent_cents(amount, frequency) for frequency, amount in rows)
    return {
        "monthly_cents": monthly,
        "monthly_total": fmt_money(monthly),
        "annual_cents": monthly * 12,
        "annual_total": fmt_money(monthly * 12),
    }


def get_maintenance_due(conn: sqlite3.Connection, days: int = 30, today: date | None = None) -> list[dict[str, object]]:
    today = today or date.today()
    until = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT task, location, next_due, estimated_cost_cents, supplies
        FROM maintenance_schedule
        WHERE next_due <= ?
        ORDER BY next_due, task
        """,
        (until,),
    ).fetchall()
    return [
        {
            "task": row[0],
            "location": row[1],
            "due": row[2],
            "cost_cents": row[3] or 0,
            "cost": fmt_money(row[3] or 0),
            "supplies": row[4],
        }
        for row in rows
    ]


def complete_maintenance(conn: sqlite3.Connection, task_name: str, today: date | None = None) -> str:
    task_name = sanitize_str(task_name, "task", max_len=120)
    today = today or date.today()
    with atomic(conn):
        row = conn.execute(
            "SELECT task_id, frequency_days FROM maintenance_schedule WHERE task = ?",
            (task_name,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"Unknown maintenance task {task_name!r}")
        nxt = today + timedelta(days=row[1])
        conn.execute(
            "UPDATE maintenance_schedule SET last_completed = ?, next_due = ? WHERE task_id = ?",
            (today.isoformat(), nxt.isoformat(), row[0]),
        )
        audit(conn, "complete_maintenance", "maintenance", row[0], json.dumps({"task": task_name}))
    return nxt.isoformat()


def add_warranty(
    conn: sqlite3.Connection,
    item: str,
    purchase_date: str,
    expires: str,
    vendor: str | None = None,
    serial: str | None = None,
    notes: str | None = None,
) -> int:
    item = sanitize_str(item, "item", max_len=120)
    purchased = parse_iso_date(purchase_date, "purchase_date").isoformat()
    expiry = parse_iso_date(expires, "warranty_expires").isoformat()
    vendor_text = sanitize_str(vendor, "vendor", max_len=80, allow_empty=True) if vendor else ""
    serial_text = sanitize_str(serial, "serial", max_len=80, allow_empty=True) if serial else ""
    note_text = sanitize_str(notes, "notes", max_len=200, allow_empty=True) if notes else ""
    with atomic(conn):
        cur = conn.execute(
            """
            INSERT INTO warranties
                (item, purchase_date, warranty_expires, vendor, serial_number, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item, purchased, expiry, vendor_text, serial_text, note_text),
        )
    return int(cur.lastrowid)


def get_expiring_warranties(
    conn: sqlite3.Connection, days: int = 90, today: date | None = None
) -> list[dict[str, object]]:
    today = today or date.today()
    until = (today + timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT item, warranty_expires, vendor, serial_number
        FROM warranties
        WHERE warranty_expires <= ? AND warranty_expires >= ?
        ORDER BY warranty_expires
        """,
        (until, today.isoformat()),
    ).fetchall()
    return [{"item": row[0], "expires": row[1], "vendor": row[2], "serial": row[3]} for row in rows]
