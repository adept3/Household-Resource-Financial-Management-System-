from homebase.bank import import_bank_csv
from homebase.ledger import ingest_receipt


def _receipt(day, total, merchant="Shop"):
    return {
        "date": day,
        "merchant": merchant,
        "payment_method": "Debit Card",
        "items": [{"name": "Thing", "qty": 1, "price": total}],
        "tax": 0,
        "total": total,
    }


def _csv(rows):
    lines = ["Date,Description,Amount,Reference"]
    lines.extend(rows)
    return "\n".join(lines) + "\n"


def test_signed_amount_and_exact_date(conn):
    ingest_receipt(conn, _receipt("2026-09-20", 42.93, "Walmart"))
    result = import_bank_csv(
        conn,
        _csv(
            [
                "2026-09-20,WALMART REFUND,42.93,REFUND1",
                "2026-09-20,WALMART SUPERCENTER,-42.93,CHK1",
            ]
        ),
    )
    assert result["imported"] == 2
    assert result["matched"] == 1
    assert result["rejected"] == []
    assert result["unmatched"][0]["reason"].startswith("inflow")
    reconciled = conn.execute("SELECT COUNT(*) FROM transactions WHERE reconciled = 1").fetchone()[0]
    assert reconciled == 1


def test_date_window_picks_the_closer_purchase(conn):
    first = ingest_receipt(conn, _receipt("2026-09-01", 10.00, "A"))
    second = ingest_receipt(conn, _receipt("2026-09-10", 10.00, "B"))
    near = import_bank_csv(conn, _csv(["2026-09-11,CARD,-10.00,NEAR"]))
    assert near["matched"] == 1
    matched_id = conn.execute("SELECT matched_txn_id FROM bank_imports WHERE bank_ref = 'NEAR'").fetchone()[0]
    assert matched_id == second
    assert conn.execute("SELECT reconciled FROM transactions WHERE transaction_id = ?", (first,)).fetchone()[0] == 0

    # 09-05 is 4 days from the 1st and 5 days from the 10th, outside a 3-day window.
    # The first purchase is still open; this row must not claim it.
    far = import_bank_csv(conn, _csv(["2026-09-05,CARD,-10.00,FAR"]))
    assert far["matched"] == 0
    assert far["unmatched"][0]["reason"].startswith("no unreconciled")


def test_exact_window_rejects_one_day_offset(conn):
    ingest_receipt(conn, _receipt("2026-09-01", 10.00))
    result = import_bank_csv(conn, _csv(["2026-09-02,CARD,-10.00,OFF"]), window_days=0)
    assert result["matched"] == 0
    hit = import_bank_csv(conn, _csv(["2026-09-01,CARD,-10.00,ON"]), window_days=0)
    assert hit["matched"] == 1


def test_tie_prefers_the_lower_transaction_id(conn):
    first = ingest_receipt(conn, _receipt("2026-09-01", 8.00, "Early"))
    ingest_receipt(conn, _receipt("2026-09-03", 8.00, "Late"))
    result = import_bank_csv(conn, _csv(["2026-09-02,CARD,-8.00,TIE"]))
    assert result["matched"] == 1
    matched_id = conn.execute("SELECT matched_txn_id FROM bank_imports WHERE bank_ref = 'TIE'").fetchone()[0]
    assert matched_id == first


def test_duplicate_reference_is_skipped_and_bad_rows_are_reported(conn):
    first = import_bank_csv(conn, _csv(["2026-09-20,COFFEE,-3.50,REF1"]))
    assert first["imported"] == 1
    second = import_bank_csv(conn, _csv(["2026-09-21,COFFEE AGAIN,-9.00,REF1"]))
    assert second["imported"] == 0
    assert second["skipped"] == 1

    different_ref = import_bank_csv(conn, _csv(["2026-09-20,COFFEE,-3.50,REF2"]))
    assert different_ref["imported"] == 1
    assert different_ref["skipped"] == 0

    rejected = import_bank_csv(
        conn,
        _csv(
            [
                "not-a-date,FOO,-1.00,BAD1",
                "2026-09-20,BAR,nope,BAD2",
                "2026-09-20,,-1.00,BAD3",
                "2026-09-20,ZERO,0.00,BAD4",
                "2026-09-20,PARENS,(4.25),BAD5",
            ]
        ),
    )
    reasons = " ".join(item["reason"] for item in rejected["rejected"])
    assert "invalid date" in reasons
    assert "not a number" in reasons
    assert "description is required" in reasons
    assert "must not be zero" in reasons
    assert rejected["imported"] == 1
    assert rejected["imported"] + len(rejected["rejected"]) == 5
    amount = conn.execute("SELECT bank_amount FROM bank_imports WHERE bank_ref = 'BAD5'").fetchone()[0]
    assert amount == -425


def test_reimporting_the_same_file_skips(conn):
    content = _csv(["2026-09-20,GROCER,-12.00,ONCE"])
    assert import_bank_csv(conn, content)["imported"] == 1
    again = import_bank_csv(conn, content)
    assert again["imported"] == 0
    assert again["skipped"] == 1
