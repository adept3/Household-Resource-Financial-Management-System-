from datetime import date

import pytest

from homebase.bills import (
    add_subscription,
    get_upcoming_bills,
    list_subscriptions,
    mark_subscription_paid,
    monthly_equivalent_cents,
)
from homebase.util import ValidationError

TODAY = date(2026, 11, 15)


def test_new_subscription_is_not_already_past_due(conn):
    assert add_subscription(conn, "Netflix", 15.99, "monthly", due_day=5, today=TODAY) == "2026-12-05"
    assert add_subscription(conn, "Electric", 125, "monthly", due_day=20, today=TODAY) == "2026-11-20"
    assert add_subscription(conn, "Due today", 10, "monthly", due_day=15, today=TODAY) == "2026-11-15"
    assert add_subscription(conn, "Insurance", 480, "quarterly", due_day=1, today=TODAY) == "2027-01-01"
    assert add_subscription(conn, "Prime", 139, "annual", due_day=10, today=TODAY) == "2027-11-10"
    assert add_subscription(conn, "Weekly", 5, "weekly", due_day=1, today=TODAY) == "2026-11-16"
    assert add_subscription(conn, "Sunday", 5, "weekly", due_day=7, today=TODAY) == "2026-11-15"
    for row in list_subscriptions(conn):
        assert row["due"] >= TODAY.isoformat()


def test_mark_paid_advances_and_catches_up(conn):
    add_subscription(conn, "Electric", 125, "monthly", due_day=20, today=TODAY)
    assert mark_subscription_paid(conn, "Electric", today=TODAY) == "2026-12-20"
    add_subscription(conn, "Old", 9.99, "monthly", due_day=5, today=date(2026, 1, 1))
    conn.execute("UPDATE subscriptions SET next_due = '2026-01-05' WHERE name = 'Old'")
    assert mark_subscription_paid(conn, "Old", today=TODAY) == "2026-12-05"


def test_reading_bills_does_not_silently_advance_overdue(conn):
    add_subscription(conn, "Netflix", 15.99, "monthly", due_day=5, today=TODAY)
    conn.execute("UPDATE subscriptions SET next_due = '2026-10-05' WHERE name = 'Netflix'")
    bills = get_upcoming_bills(conn, days=7, today=TODAY)
    assert bills[0]["due"] == "2026-10-05"
    assert list_subscriptions(conn)[0]["due"] == "2026-10-05"


def test_due_day_is_validated(conn):
    with pytest.raises(ValidationError):
        add_subscription(conn, "Bad", 10, "monthly", due_day=31, today=TODAY)
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


def test_monthly_equivalent_is_integer_cents():
    assert monthly_equivalent_cents(48000, "quarterly") == 16000
    assert monthly_equivalent_cents(13900, "annual") == 1158
    assert monthly_equivalent_cents(1599, "monthly") == 1599
