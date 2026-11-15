"""SQLite connection, schema, permissions, and the append-only audit log.

Opening a database never deletes an existing file. Importing this module
does not create or open a database.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from homebase.util import HomeBaseError

SCHEMA_VERSION = 1

# Directories we must never chmod. The ledger lives in its own folder.
_PROTECTED_DIRS = {
    Path("/"),
    Path("/tmp"),
    Path("/var"),
    Path("/var/tmp"),
    Path("/usr"),
    Path("/opt"),
    Path("/home"),
    Path("/etc"),
    Path("/root"),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name TEXT NOT NULL UNIQUE,
    account_type TEXT NOT NULL CHECK (account_type IN ('Asset','Liability','Equity','Income','Expense')),
    parent_id INTEGER,
    FOREIGN KEY (parent_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    merchant_id INTEGER NOT NULL,
    payment_method TEXT NOT NULL,
    raw_subtotal INTEGER NOT NULL,
    calculated_tax INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    description TEXT,
    bank_ref TEXT,
    reconciled INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (merchant_id) REFERENCES merchants(merchant_id)
);

CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS transaction_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    sku_or_asin TEXT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
    category TEXT NOT NULL,
    is_hsa_eligible INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hsa_catalog (
    catalog_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_pattern TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL UNIQUE,
    low_stock_threshold INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inventory (
    product_id INTEGER PRIMARY KEY,
    current_stock INTEGER NOT NULL DEFAULT 0,
    last_audit_date TEXT,
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE IF NOT EXISTS receipt_product_mappings (
    raw_item_name TEXT PRIMARY KEY,
    product_id INTEGER NOT NULL,
    multiplier INTEGER NOT NULL DEFAULT 1 CHECK (multiplier > 0),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE IF NOT EXISTS bank_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_date TEXT NOT NULL,
    bank_date TEXT NOT NULL,
    bank_description TEXT NOT NULL,
    bank_amount INTEGER NOT NULL,
    bank_ref TEXT,
    account_name TEXT NOT NULL,
    matched_txn_id INTEGER,
    FOREIGN KEY (matched_txn_id) REFERENCES transactions(transaction_id)
);

CREATE TABLE IF NOT EXISTS inventory_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    audit_date TEXT NOT NULL,
    previous_stock INTEGER NOT NULL,
    counted_stock INTEGER NOT NULL,
    adjustment INTEGER NOT NULL,
    notes TEXT,
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    detail TEXT
);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TABLE IF NOT EXISTS subscriptions (
    sub_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    frequency TEXT NOT NULL CHECK (frequency IN ('weekly','monthly','quarterly','annual')),
    due_day INTEGER NOT NULL,
    category TEXT,
    auto_pay INTEGER NOT NULL DEFAULT 0,
    next_due TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_schedule (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL UNIQUE,
    location TEXT,
    frequency_days INTEGER NOT NULL,
    last_completed TEXT,
    next_due TEXT,
    estimated_cost_cents INTEGER,
    supplies TEXT,
    assigned_to TEXT
);

CREATE TABLE IF NOT EXISTS warranties (
    warranty_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item TEXT NOT NULL,
    purchase_date TEXT,
    warranty_expires TEXT,
    vendor TEXT,
    serial_number TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_reconcile ON transactions(reconciled, date, total_amount);
CREATE INDEX IF NOT EXISTS idx_journal_txn ON journal_entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_journal_account ON journal_entries(account_id);
CREATE INDEX IF NOT EXISTS idx_items_txn ON transaction_items(transaction_id);
CREATE INDEX IF NOT EXISTS idx_items_hsa ON transaction_items(is_hsa_eligible);
CREATE INDEX IF NOT EXISTS idx_bank_dedupe ON bank_imports(account_name, bank_date, bank_amount);
CREATE INDEX IF NOT EXISTS idx_bank_ref ON bank_imports(account_name, bank_ref);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_subscriptions_due ON subscriptions(active, next_due);
"""

DEFAULT_ACCOUNTS = (
    ("Checking Account", "Asset"),
    ("Credit Card", "Liability"),
    ("HSA Account", "Asset"),
    ("Groceries", "Expense"),
    ("Household Supplies", "Expense"),
    ("Sales Tax", "Expense"),
    ("HSA Medical", "Expense"),
    ("Healthcare", "Expense"),
    ("Utilities", "Expense"),
    ("Auto/Fuel", "Expense"),
    ("Subscriptions", "Expense"),
    ("Home Maintenance", "Expense"),
    ("Discounts & Adjustments", "Expense"),
    ("Income", "Income"),
    ("Opening Balance", "Equity"),
)

DEFAULT_HSA_PATTERNS = (
    ("%IBUPROFEN%", "Pain Relief"),
    ("%TYLENOL%", "Pain Relief"),
    ("%ACETAMINOPHEN%", "Pain Relief"),
    ("%ADVIL%", "Pain Relief"),
    ("%BANDAGE%", "First Aid"),
    ("%BAND-AID%", "First Aid"),
    ("%THERMOMETER%", "Diagnostics"),
    ("%BLOOD PRESSURE%", "Diagnostics"),
    ("%GLUCOSE%", "Diagnostics"),
    ("%ALLERGY%", "Allergy"),
    ("%ZYRTEC%", "Allergy"),
    ("%CLARITIN%", "Allergy"),
    ("%BENADRYL%", "Allergy"),
    ("%SUNSCREEN%", "Sun Protection"),
    ("%CONTACT LENS%", "Vision"),
    ("%SALINE%", "Vision"),
    ("%FIRST AID%", "First Aid"),
    ("%COLD MEDICINE%", "Cold & Flu"),
    ("%COUGH%", "Cold & Flu"),
    ("%MUCINEX%", "Cold & Flu"),
    ("%ANTACID%", "Digestive"),
    ("%PEPTO%", "Digestive"),
    ("%TUMS%", "Digestive"),
)

DEFAULT_MAINTENANCE = (
    ("Replace HVAC filter", "Basement", 90, 1500, "20x25x1 MERV 13 filter"),
    ("Clean gutters", "Exterior", 180, 0, None),
    ("Flush water heater", "Utility room", 365, 0, None),
    ("Replace smoke detector batteries", "All floors", 180, 2000, "9V batteries x4"),
    ("Clean dryer vent", "Laundry room", 365, 0, "Vent brush kit"),
    ("Clean refrigerator coils", "Kitchen", 365, 0, None),
    ("Test garage door safety sensors", "Garage", 90, 0, None),
    ("Inspect roof/attic", "Exterior/Attic", 365, 0, None),
)


def default_data_dir() -> Path:
    """Private data directory. Never /tmp."""
    configured = os.environ.get("HOMEBASE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "homebase"


def default_db_path() -> Path:
    configured = os.environ.get("HOMEBASE_DB")
    if configured:
        return Path(configured).expanduser()
    return default_data_dir() / "homebase.db"


def restrict_directory(directory: Path) -> None:
    """Create ``directory`` and set it to mode 0700."""
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve()
    if resolved in _PROTECTED_DIRS:
        raise HomeBaseError(f"Refusing to change permissions on {resolved}")
    os.chmod(resolved, 0o700)


def harden_db_permissions(path: Path) -> None:
    """Force the database and its WAL/SHM sidecars to mode 0600."""
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists():
            os.chmod(candidate, 0o600)


class LedgerConnection(sqlite3.Connection):
    """sqlite3.Connection cannot store extra attributes in this interpreter."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hb_path = ""
        self._hb_depth = 0


def harden_connection_files(conn: sqlite3.Connection) -> None:
    raw = getattr(conn, "_hb_path", None)
    if raw:
        harden_db_permissions(Path(raw))


def _apply_optional_sqlcipher(conn: sqlite3.Connection) -> None:
    """Honor HOMEBASE_DB_KEY only when SQLCipher is actually linked in.

    Stock CPython sqlite3 ignores ``PRAGMA key``. If a key was requested and
    ``cipher_version`` is empty, refuse to continue rather than store plaintext.
    SQLCipher is not a dependency of this package.
    """
    key = os.environ.get("HOMEBASE_DB_KEY", "")
    if key == "":
        return
    if "\x00" in key:
        raise HomeBaseError("HOMEBASE_DB_KEY is invalid")
    hexkey = key.encode("utf-8").hex()
    if re.fullmatch(r"[0-9a-f]+", hexkey) is None:
        raise HomeBaseError("HOMEBASE_DB_KEY is invalid")
    conn.execute('PRAGMA key = "x\'' + hexkey + '\'"')
    row = conn.execute("PRAGMA cipher_version").fetchone()
    if row is None or row[0] in (None, ""):
        raise HomeBaseError(
            "HOMEBASE_DB_KEY is set, but this Python is linked to SQLite without SQLCipher. "
            "HomeBase will not silently store the ledger in plaintext. "
            "Unset HOMEBASE_DB_KEY, or use a SQLCipher build of SQLite."
        )


def _configure(conn: sqlite3.Connection) -> None:
    _apply_optional_sqlcipher(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")


def _seed(conn: sqlite3.Connection) -> None:
    for name, account_type in DEFAULT_ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (account_name, account_type) VALUES (?, ?)",
            (name, account_type),
        )
    for pattern, category in DEFAULT_HSA_PATTERNS:
        conn.execute(
            "INSERT OR IGNORE INTO hsa_catalog (match_pattern, category) VALUES (?, ?)",
            (pattern, category),
        )
    today = date.today()
    for task, location, frequency_days, cost, supplies in DEFAULT_MAINTENANCE:
        conn.execute(
            """
            INSERT OR IGNORE INTO maintenance_schedule
                (task, location, frequency_days, estimated_cost_cents, supplies, next_due)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task, location, frequency_days, cost, supplies, (today + timedelta(days=frequency_days)).isoformat()),
        )


def migrate(conn: sqlite3.Connection) -> None:
    """Create tables and indexes. Never deletes an existing database."""
    conn.executescript(SCHEMA_SQL)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("BEGIN")
        try:
            _seed(conn)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return
    version = row[0]
    if version > SCHEMA_VERSION:
        raise HomeBaseError(
            f"Database schema version {version} is newer than this program ({SCHEMA_VERSION})"
        )
    if version < SCHEMA_VERSION:
        raise HomeBaseError(
            f"Database schema version {version} is older than this program ({SCHEMA_VERSION}) "
            "and no migration is defined"
        )


def open_db(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open or create the ledger. Existing files are migrated, not deleted.

    The parent directory is mode 0700 and the database file is mode 0600.
    """
    db_path = Path(path).expanduser() if path is not None else default_db_path()
    restrict_directory(db_path.parent)
    existed = db_path.exists()
    previous_umask = os.umask(0o077)
    try:
        conn = sqlite3.connect(db_path, isolation_level=None, factory=LedgerConnection)
    finally:
        os.umask(previous_umask)
    conn._hb_path = str(db_path)
    conn._hb_depth = 0
    try:
        _configure(conn)
        migrate(conn)
    except Exception:
        conn.close()
        if not existed and db_path.exists():
            db_path.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(db_path) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        raise
    harden_db_permissions(db_path)
    return conn


def get_account_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT account_id FROM accounts WHERE account_name = ?", (name,)).fetchone()
    return row[0] if row else None


def audit(
    conn: sqlite3.Connection,
    action: str,
    entity: str,
    entity_id: int | None = None,
    detail: str | None = None,
) -> None:
    """Append one audit row. Updates and deletes are rejected by triggers."""
    conn.execute(
        "INSERT INTO audit_log (timestamp, action, entity, entity_id, detail) VALUES (?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            action,
            entity,
            entity_id,
            detail,
        ),
    )


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back the whole unit of work on any error.

    Nested calls use savepoints so a helper can join the caller's transaction
    instead of committing in the middle of it.
    """
    depth = getattr(conn, "_hb_depth", 0)
    if not isinstance(depth, int) or depth < 0:
        raise HomeBaseError("invalid transaction depth")
    if depth == 0:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT hb_" + str(depth))
    conn._hb_depth = depth + 1
    try:
        yield conn
        if depth == 0:
            conn.execute("COMMIT")
        else:
            conn.execute("RELEASE SAVEPOINT hb_" + str(depth))
    except BaseException:
        if depth == 0:
            conn.execute("ROLLBACK")
        else:
            conn.execute("ROLLBACK TO SAVEPOINT hb_" + str(depth))
            conn.execute("RELEASE SAVEPOINT hb_" + str(depth))
        raise
    finally:
        conn._hb_depth = depth
        if depth == 0:
            harden_connection_files(conn)
