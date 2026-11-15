"""Alert text for push notifications.

Dollar amounts are omitted unless HOMEBASE_NOTIFY_AMOUNTS is explicitly on
(1, true, yes, on) or the caller passes include_amounts=True. Delivery uses
the stdlib HTTP client and only runs when HOMEBASE_NTFY_URL is set.
"""

from __future__ import annotations

import os
import re
import sqlite3
import urllib.request
from datetime import date

from homebase.bills import get_upcoming_bills
from homebase.inventory import get_low_stock_alerts
from homebase.util import ValidationError, fmt_money

_MONEY = re.compile(r"\$\d")
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")


def include_amounts_enabled(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("HOMEBASE_NOTIFY_AMOUNTS", "0").strip().lower() in {"1", "true", "yes", "on"}


def notification_text(
    kind: str,
    subject: str,
    *,
    amount_cents: int | None = None,
    include_amounts: bool | None = None,
) -> str:
    text = f"{kind}: {subject}"
    if include_amounts_enabled(include_amounts) and amount_cents is not None:
        text += " (" + fmt_money(amount_cents) + ")"
    return text


def check_and_notify_alerts(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    include_amounts: bool | None = None,
    send: bool = False,
    today: date | None = None,
) -> list[str]:
    """Build alert lines. Amounts stay out unless explicitly requested."""
    today = today or date.today()
    messages: list[str] = []
    for item in get_low_stock_alerts(conn):
        messages.append(
            notification_text(
                "Low stock",
                f"{item['product']} is at {item['stock']} (threshold {item['threshold']})",
                include_amounts=False,
            )
        )
    for bill in get_upcoming_bills(conn, days=days, today=today):
        messages.append(
            notification_text(
                "Bill due",
                f"{bill['name']} on {bill['due']}",
                amount_cents=int(bill["amount_cents"]),
                include_amounts=include_amounts,
            )
        )
    if send and messages:
        deliver_notifications(messages, include_amounts=include_amounts_enabled(include_amounts))
    return messages


def deliver_notifications(messages: list[str], *, include_amounts: bool = False) -> bool:
    """POST alert text to ntfy when HOMEBASE_NTFY_URL is configured.

    Returns False when no URL is set. Refuses to send a message that still
    contains a dollar amount unless amounts were explicitly enabled.
    """
    if not include_amounts:
        for message in messages:
            if _MONEY.search(message):
                raise RuntimeError("refusing to send a notification that contains a dollar amount")
    base = os.environ.get("HOMEBASE_NTFY_URL", "").strip().rstrip("/")
    if base == "":
        return False
    topic = os.environ.get("HOMEBASE_NTFY_TOPIC", "homebase")
    if _TOPIC.fullmatch(topic) is None:
        raise ValidationError("HOMEBASE_NTFY_TOPIC must be a short token")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ValidationError("HOMEBASE_NTFY_URL must be an http(s) URL")
    body = "\n".join(messages).encode("utf-8")
    request = urllib.request.Request(
        base + "/" + topic,
        data=body,
        method="POST",
        headers={"Title": "HomeBase", "Content-Type": "text/plain; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()
    return True
