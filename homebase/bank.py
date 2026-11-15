"""Bank CSV import with signed amounts, a short date window, and dedupe.

Purchases are stored as positive receipt totals. A bank outflow is negative,
so a row matches only when ``bank_amount == -total_amount``. A deposit of the
same magnitude does not match a purchase. Dates may differ by up to
``window_days`` (default 3). Duplicate detection prefers the bank reference
and otherwise uses account + date + description + amount.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, date

from homebase.db import atomic, audit
from homebase.util import ValidationError, fmt_money, sanitize_str, to_cents

DATE_WINDOW_DAYS = 3
MAX_CSV_CHARS = 5_000_000

PROFILES = {
    "generic": {
        "date_col": "Date",
        "desc_col": "Description",
        "amount_col": "Amount",
        "ref_col": "Reference",
        "date_fmt": "%Y-%m-%d",
    }
}


def _parse_amount(raw: object) -> int:
    if not isinstance(raw, str):
        raise ValidationError("amount must be text")
    text = raw.strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    cents = to_cents(text)
    if negative:
        cents = -abs(cents)
    if cents == 0:
        raise ValidationError("amount must not be zero")
    return cents


def _parse_row(row: dict[str, str | None], cfg: dict[str, str]) -> dict[str, object]:
    try:
        date_raw = (row.get(cfg["date_col"]) or "").strip()
        desc_raw = row.get(cfg["desc_col"])
        amount_raw = row.get(cfg["amount_col"])
    except AttributeError as exc:
        raise ValidationError("row is not a CSV record") from exc
    if date_raw == "" or desc_raw is None or amount_raw is None:
        raise ValidationError("date, description, and amount are required")
    try:
        parsed = datetime.strptime(date_raw, cfg["date_fmt"]).date()
    except ValueError as exc:
        raise ValidationError(f"invalid date {date_raw!r}") from exc
    description = sanitize_str(desc_raw, "description", max_len=200)
    amount = _parse_amount(amount_raw)
    ref = ""
    ref_col = cfg.get("ref_col") or ""
    if ref_col and row.get(ref_col) not in (None, ""):
        ref = sanitize_str(row.get(ref_col) or "", "reference", max_len=64)
    return {
        "date": parsed.isoformat(),
        "description": description,
        "amount": amount,
        "ref": ref,
    }


def _is_duplicate(conn: sqlite3.Connection, account_name: str, parsed: dict[str, object]) -> bool:
    ref = str(parsed["ref"])
    if ref:
        existing = conn.execute(
            "SELECT 1 FROM bank_imports WHERE account_name = ? AND bank_ref = ?",
            (account_name, ref),
        ).fetchone()
        if existing:
            return True
    existing = conn.execute(
        """
        SELECT 1 FROM bank_imports
        WHERE account_name = ? AND bank_date = ? AND bank_description = ?
          AND bank_amount = ? AND COALESCE(bank_ref, '') = ?
        """,
        (account_name, parsed["date"], parsed["description"], parsed["amount"], ref),
    ).fetchone()
    return existing is not None


def _find_match(conn: sqlite3.Connection, parsed: dict[str, object], window_days: int) -> int | None:
    amount = int(parsed["amount"])
    # Inflows are not purchases. abs() would pair a refund or payroll with a receipt.
    if amount >= 0:
        return None
    target = -amount
    bank_day = date.fromisoformat(str(parsed["date"]))
    rows = conn.execute(
        "SELECT transaction_id, date FROM transactions WHERE reconciled = 0 AND total_amount = ?",
        (target,),
    ).fetchall()
    best: int | None = None
    best_distance = window_days + 1
    for txn_id, txn_date in rows:
        try:
            txn_day = date.fromisoformat(txn_date)
        except ValueError:
            continue
        distance = abs((txn_day - bank_day).days)
        if distance > window_days:
            continue
        if distance < best_distance or (distance == best_distance and best is not None and txn_id < best):
            best = txn_id
            best_distance = distance
    return best


def import_bank_csv(
    conn: sqlite3.Connection,
    csv_content: str,
    profile: str = "generic",
    account_name: str = "Checking",
    window_days: int = DATE_WINDOW_DAYS,
) -> dict[str, object]:
    if profile not in PROFILES:
        raise ValidationError(f"Unknown CSV profile {profile!r}")
    if isinstance(window_days, bool) or not isinstance(window_days, int) or not 0 <= window_days <= 14:
        raise ValidationError("window_days must be an integer from 0 to 14")
    if not isinstance(csv_content, str):
        raise ValidationError("csv_content must be a string")
    if len(csv_content) > MAX_CSV_CHARS:
        raise ValidationError("CSV is too large")
    if csv_content.startswith("\ufeff"):
        csv_content = csv_content.lstrip("\ufeff")
    account_name = sanitize_str(account_name, "account_name", max_len=80)
    cfg = PROFILES[profile]
    reader = csv.DictReader(io.StringIO(csv_content))
    if not reader.fieldnames:
        raise ValidationError("CSV has no header")
    missing = [column for column in (cfg["date_col"], cfg["desc_col"], cfg["amount_col"]) if column not in reader.fieldnames]
    if missing:
        raise ValidationError("CSV missing columns: " + ", ".join(missing))

    imported = 0
    matched = 0
    skipped = 0
    rejected: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []

    with atomic(conn):
        for row in reader:
            line_no = reader.line_num
            try:
                parsed = _parse_row(row, cfg)
            except ValidationError as exc:
                rejected.append({"line": line_no, "reason": str(exc)})
                continue
            if _is_duplicate(conn, account_name, parsed):
                skipped += 1
                continue
            cur = conn.execute(
                """
                INSERT INTO bank_imports
                    (import_date, bank_date, bank_description, bank_amount, bank_ref, account_name)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    date.today().isoformat(),
                    parsed["date"],
                    parsed["description"],
                    parsed["amount"],
                    parsed["ref"] or None,
                    account_name,
                ),
            )
            import_id = int(cur.lastrowid)
            imported += 1
            match = _find_match(conn, parsed, window_days)
            if match is not None:
                conn.execute(
                    "UPDATE bank_imports SET matched_txn_id = ? WHERE import_id = ?",
                    (match, import_id),
                )
                conn.execute(
                    "UPDATE transactions SET reconciled = 1, bank_ref = ? WHERE transaction_id = ?",
                    (parsed["ref"] or None, match),
                )
                matched += 1
                audit(
                    conn,
                    "reconcile",
                    "transaction",
                    match,
                    json.dumps(
                        {
                            "import_id": import_id,
                            "bank_amount_cents": parsed["amount"],
                            "bank_date": parsed["date"],
                        },
                        sort_keys=True,
                    ),
                )
            else:
                if int(parsed["amount"]) > 0:
                    reason = "inflow is not matched to a purchase"
                else:
                    reason = "no unreconciled purchase within the date window"
                unmatched.append(
                    {
                        "line": line_no,
                        "date": parsed["date"],
                        "description": parsed["description"],
                        "amount_cents": parsed["amount"],
                        "amount": fmt_money(int(parsed["amount"])),
                        "reason": reason,
                    }
                )
        audit(
            conn,
            "import_bank_csv",
            "bank_import",
            None,
            json.dumps(
                {
                    "account": account_name,
                    "imported": imported,
                    "matched": matched,
                    "skipped": skipped,
                    "rejected": len(rejected),
                },
                sort_keys=True,
            ),
        )

    return {
        "imported": imported,
        "matched": matched,
        "skipped": skipped,
        "rejected": rejected,
        "unmatched": unmatched,
    }
