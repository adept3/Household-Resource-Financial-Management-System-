import os
import re
import sqlite3
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from homebase import default_db_path, get_hsa_summary, get_tax_summary, ingest_receipt, open_db
from homebase.bills import add_subscription
from homebase.db import SCHEMA_VERSION
from homebase.inventory import map_receipt_item_to_product, set_low_stock_threshold
from homebase.notify import check_and_notify_alerts, deliver_notifications
from homebase.util import HomeBaseError, ValidationError, load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def test_default_database_is_outside_tmp(monkeypatch):
    monkeypatch.delenv("HOMEBASE_DATA_DIR", raising=False)
    monkeypatch.delenv("HOMEBASE_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    path = default_db_path()
    assert path.name == "homebase.db"
    assert path.parent.name == "homebase"
    assert "tmp" not in path.parts


def test_refuses_to_use_tmp_as_the_data_directory():
    target = Path("/tmp") / "homebase-refused.db"
    with pytest.raises(HomeBaseError, match="permissions"):
        open_db(target)
    assert not target.exists()


def test_schema_version_and_indexes(conn):
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    for expected in (
        "idx_transactions_date",
        "idx_transactions_reconcile",
        "idx_journal_txn",
        "idx_items_txn",
        "idx_bank_dedupe",
        "idx_audit_ts",
    ):
        assert expected in names


def test_reopen_preserves_rows(tmp_path):
    path = tmp_path / "ledger.db"
    conn = open_db(path)
    ingest_receipt(
        conn,
        {
            "date": "2026-09-20",
            "merchant": "O'Reilly",
            "payment_method": "Debit Card",
            "items": [{"name": "Wiper", "qty": 1, "price": 12.00, "category": "auto"}],
            "tax": 0,
            "total": 12.00,
        },
    )
    conn.close()
    again = open_db(path)
    try:
        assert again.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        assert again.execute("SELECT name FROM merchants").fetchone()[0] == "O'Reilly"
    finally:
        again.close()


def test_database_permissions_are_private(tmp_path, monkeypatch):
    data = tmp_path / "private"
    monkeypatch.setenv("HOMEBASE_DATA_DIR", str(data))
    monkeypatch.delenv("HOMEBASE_DB", raising=False)
    conn = open_db()
    listed = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    db_path = Path(listed)
    assert db_path.parent == data.resolve() or db_path.parent == data
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    wal = Path(str(db_path) + "-wal")
    if wal.exists():
        assert stat.S_IMODE(wal.stat().st_mode) == 0o600


def test_sqlcipher_key_without_support_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEBASE_DB_KEY", "not-a-real-key")
    path = tmp_path / "sealed.db"
    with pytest.raises(HomeBaseError, match="SQLCipher"):
        open_db(path)
    assert not path.exists()


def test_summaries_reject_injected_years(conn):
    ingest_receipt(
        conn,
        {
            "date": "2026-09-20",
            "merchant": "Pharmacy",
            "payment_method": "Credit Card",
            "items": [{"name": "Ibuprofen 200mg 100ct", "qty": 1, "price": 8.97}],
            "tax": 0,
            "total": 8.97,
        },
    )
    for year in ("2026' OR '1'='1", "2026; DROP TABLE accounts;--", "2026%", "20 OR 1=1"):
        with pytest.raises(ValidationError):
            get_hsa_summary(conn, year)
        with pytest.raises(ValidationError):
            get_tax_summary(conn, year)
    assert get_hsa_summary(conn, "2026")["count"] == 1
    assert get_hsa_summary(conn, "1999")["count"] == 0
    assert get_tax_summary(conn, 2026)["total_cents"] == 0
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 0


def test_audit_log_is_append_only(conn):
    ingest_receipt(
        conn,
        {
            "date": "2026-09-20",
            "merchant": "Walmart",
            "payment_method": "Cash",
            "items": [{"name": "Milk", "qty": 1, "price": 3}],
            "tax": 0,
            "total": 3,
        },
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET action = 'tamper'")
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= 1


def test_notifications_omit_amounts_by_default(conn):
    map_receipt_item_to_product(conn, "Milk", "Whole Milk", 1)
    set_low_stock_threshold(conn, "Whole Milk", 2)
    add_subscription(conn, "Netflix", 15.99, "monthly", due_day=1, today=date(2026, 11, 15))
    messages = check_and_notify_alerts(conn, days=40, today=date(2026, 11, 15))
    assert messages
    blob = "\n".join(messages)
    assert "$" not in blob
    assert "15.99" not in blob
    shown = check_and_notify_alerts(conn, days=40, include_amounts=True, today=date(2026, 11, 15))
    assert any("15.99" in line for line in shown)
    with pytest.raises(RuntimeError, match="dollar amount"):
        deliver_notifications(["Bill due: Netflix ($15.99)"], include_amounts=False)


def test_loose_env_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("HOMEBASE_NOTIFY_AMOUNTS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("HOMEBASE_NOTIFY_AMOUNTS=1\n", encoding="utf-8")
    env_file.chmod(0o644)
    with pytest.raises(PermissionError):
        load_dotenv(env_file)
    env_file.chmod(0o600)
    try:
        assert load_dotenv(env_file) is True
        assert os.environ["HOMEBASE_NOTIFY_AMOUNTS"] == "1"
    finally:
        os.environ.pop("HOMEBASE_NOTIFY_AMOUNTS", None)


def test_source_uses_bound_parameters_and_no_baked_secrets():
    pattern = re.compile(r"\.(execute|executemany|executescript)\(\s*f[\"']")
    for path in (ROOT / "homebase").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert pattern.search(text) is None, path.name
        assert "changeme" not in text.lower()
        assert ":latest" not in text


def test_import_and_demo_do_not_touch_the_real_database(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / "homebase.db"
    sentinel.write_bytes(b"sentinel-do-not-touch")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["HOMEBASE_DATA_DIR"] = str(data)
    env["HOMEBASE_DB"] = str(sentinel)
    env.pop("HOMEBASE_DB_KEY", None)
    env.pop("HOMEBASE_ENV_FILE", None)
    env.pop("HOMEBASE_NOTIFY_AMOUNTS", None)
    probe = subprocess.run(
        [sys.executable, "-c", "import homebase, homebase.demo, homebase.ledger, homebase.bank, homebase.db"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert sentinel.read_bytes() == b"sentinel-do-not-touch"
    assert not (tmp_path / "homebase.db").exists()

    demo = subprocess.run(
        [sys.executable, "-m", "homebase", "demo"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert demo.returncode == 0, demo.stderr + demo.stdout
    assert sentinel.read_bytes() == b"sentinel-do-not-touch"
    assert "balanced" in demo.stdout
    assert "Sam's Club discount" in demo.stdout
    assert not (ROOT / "Demo.py").exists()
    assert not (ROOT / "Updated_Demo.py").exists()
