# HomeBase

A small, private household ledger for a machine you control. It is Python 3 and SQLite, with no third-party libraries required to run. The core is a double-entry journal, receipt ingest, bank CSV reconciliation, and pantry inventory.

This repository is a fork of Naveen4798's household-finance prototype. The earlier `Demo.py` / `Updated_Demo.py` scripts and the previous readme described a large container platform (cameras, Ecobee, Pi-hole analytics, Immich, email parsing, a web UI). Those integrations are **not implemented**. What is here is the ledger MVP, plus a few local reminder tables and an optional Docker Compose file that does not start unless you ask it to.

There is no `LICENSE` file in this fork. Adding one is the original author's decision. Do not assume the MIT notice from the old readme still applies.

## What it does

- **Double-entry ledger.** Receipt lines post to an expense account. Payment methods map to Checking, Credit Card, or HSA by exact name, not by searching the string for `"debit"`. Amounts are integer cents.
- **Discounts and fees.** If item prices plus tax do not equal the receipt total, the difference is its own `Discounts & Adjustments` line. Every journal must sum to zero or ingest raises `LedgerImbalanceError`.
- **Receipt ingest** in one database transaction. A failure rolls back the receipt, the journal, the merchant insert, stock changes, and the audit row together.
- **Pantry.** Map a receipt line to a product with a pack multiplier, then adjust stock, record a physical count, and list low items.
- **Bank CSV** (ISO dates, a `Date,Description,Amount,Reference` header). Outflows are negative and match a purchase only when the signed amount agrees. Matches allow a short date window (default 3 days). Duplicates are skipped, and bad rows are reported instead of dropped silently.
- **HSA and sales-tax summaries** with bound parameters. A year argument must be four digits.
- **Append-only `audit_log`** for financial writes. Triggers reject `UPDATE` and `DELETE`.
- **Subscriptions** whose next due date is not already in the past, and which move forward when you mark them paid. Also local maintenance tasks and warranty dates. These are reminders in SQLite, not connections to a bank or a utility.
- **Alerts** whose default text does not include dollar amounts. Set `HOMEBASE_NOTIFY_AMOUNTS=1` to include them, and `HOMEBASE_NTFY_URL` if you want them posted to ntfy.

## What it does not do

No web UI, no accounts or passwords for the ledger itself, no email/IMAP parsing, and no live clients for Actual Budget, Grocy, Mealie, Frigate, Ecobee, Immich, or Pi-hole. The Compose file can describe those services for later; they are in a `later` profile and stay off.

SQLCipher is optional and not bundled. If `HOMEBASE_DB_KEY` is set and this Python's SQLite has no SQLCipher, HomeBase refuses to open the file rather than store it in plaintext.

## Requirements

- Python 3.10 or newer
- No pip packages for the ledger
- pytest, if you want to run the tests (`requirements-dev.txt`)
- Docker only if you later enable the optional Compose profile

An 8 GB machine is enough for the ledger. The optional photo and camera containers are not sized for that machine and are off by default.

## Secure setup

```bash
git clone https://github.com/adept3/Household-Resource-Financial-Management-System-.git
cd Household-Resource-Financial-Management-System-

cp .env.example .env
chmod 600 .env
# Edit .env only if you need a non-default path, ntfy, or a SQLCipher key.

python3 -m homebase init
```

`init` creates the data directory at mode `0700` and the database at mode `0600`.

| Variable | Meaning |
|---|---|
| `HOMEBASE_DATA_DIR` | Directory for the ledger. Default: `$XDG_DATA_HOME/homebase` or `~/.local/share/homebase` |
| `HOMEBASE_DB` | Database file. Default: `$HOMEBASE_DATA_DIR/homebase.db` |
| `HOMEBASE_ENV_FILE` | Alternate path to the `.env` file |
| `HOMEBASE_NOTIFY_AMOUNTS` | `0` (default) keeps amounts out of push text |
| `HOMEBASE_NTFY_URL` / `HOMEBASE_NTFY_TOPIC` | Optional ntfy endpoint. Unset means alerts are printed only |
| `HOMEBASE_DB_KEY` | Optional SQLCipher key. Unset on stock CPython |

HomeBase will not load a `.env` that is readable by group or others. Importing the package does not create or delete a database. The demo uses a temporary file and leaves the real ledger alone.

The default path is under your home directory, not `/tmp`.

Back up with SQLite's online backup, not by copying a live database file:

```bash
sqlite3 ~/.local/share/homebase/homebase.db ".backup /path/to/private/homebase-backup.db"
```

Keep that backup file mode `0600` as well. The program does not open a network port.

## Commands

```bash
python3 -m homebase demo
python3 -m homebase balances
python3 -m homebase inventory
python3 -m homebase subscriptions
python3 -m homebase alerts
python3 -m homebase ingest receipt.json
python3 -m homebase import-csv statement.csv --account Checking
python3 -m homebase compose > docker-compose.yml
```

`demo` prints a balanced Walmart receipt and a Sam's Club receipt whose member pricing is $2.28 under the item-plus-tax total. That $2.28 is a discount line, not an adjustment buried in Groceries. Payroll in the sample CSV is an inflow and is not matched to a purchase.

### Receipt JSON

```json
{
  "date": "2026-10-01",
  "merchant": "Walmart",
  "payment_method": "Debit Card",
  "items": [
    {"name": "Milk 1 Gal", "qty": 1, "price": 3.48},
    {"name": "Paper Towels", "qty": 1, "price": 9.97, "category": "Household Supplies"},
    {"name": "Ibuprofen 200mg", "qty": 1, "price": 8.97}
  ],
  "tax": 0.62,
  "total": 23.04
}
```

Required fields are `date` (YYYY-MM-DD), `merchant`, `payment_method`, `items`, `tax`, and `total`. `qty` is an integer greater than zero. `price` and `tax` are zero or positive dollar amounts. Strings are stripped of nulls and control characters.

Payment methods: `cash`, `checking`, `debit`, `debit card`, `ach`, `credit`, `credit card`, `hsa`, `hsa card`, `hsa debit`, `hsa debit card`.

Item categories, when you set one: Groceries, Household Supplies, Healthcare, HSA Medical, Utilities, Auto/Fuel, Subscriptions, Home Maintenance. If you omit `category`, a catalog match (ibuprofen, bandages, and the other seeded patterns) posts to HSA Medical and anything else posts to Groceries.

### Bank CSV

```text
Date,Description,Amount,Reference
2026-09-20,WALMART SUPERCENTER,-42.93,CHK9920
2026-09-21,PAYROLL DIRECT DEP,2850.00,DD2609
```

Negative amounts are money leaving the account. A second import of the same reference is skipped. Rows with a bad date or amount come back in `rejected` with a line number and a reason.

### Library

```python
from homebase import open_db, ingest_receipt

conn = open_db()  # creates ~/.local/share/homebase/homebase.db
try:
    ingest_receipt(conn, receipt)
finally:
    conn.close()
```

## Optional containers

`python3 -m homebase compose` writes a Compose file for services that are **not** part of the running ledger: Traefik, Pi-hole, Unbound, WireGuard, Authelia, CrowdSec, Vaultwarden, Actual Budget, Grocy, Mealie, Paperless, Immich, Frigate, n8n, ntfy, Grafana, Uptime Kuma, Homepage, Syncthing, Stirling PDF, Portainer, and Dozzle.

Every service is in the `later` profile. `docker compose up` starts none of them. Image tags are pinned. Passwords are `${VAR:?...}` references with no default. Docker's socket is mounted read-only, and nothing sets `privileged`. Pi-hole and WireGuard still request the capabilities those daemons need; leave the profile off unless you mean to run them.

```bash
python3 -m homebase compose > docker-compose.yml
# Only if you have filled the matching keys in a mode-0600 .env:
docker compose --profile later up -d
```

Treat that file as a reviewed starting point. Follow each project's own docs before enabling it, especially on 8 GB of RAM.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

The suite covers a discounted journal, atomic ingest rollback, bank matching (sign, date window, duplicates, rejected rows), SQL-injection rejection, private file modes, and the fact that importing the package does not touch the database. GitHub Actions runs the same tests and the demo.

## Schema

Monetary columns are integer cents. Dates are ISO `YYYY-MM-DD` text. Foreign keys are on. The journal uses WAL mode. `schema_version` is currently `1`.

| Area | Tables |
|---|---|
| Ledger | `accounts`, `merchants`, `transactions`, `journal_entries`, `transaction_items`, `hsa_catalog` |
| Pantry | `products`, `inventory`, `receipt_product_mappings`, `inventory_audits` |
| Bank | `bank_imports` |
| Reminders | `subscriptions`, `maintenance_schedule`, `warranties` |
| Meta | `schema_version`, `audit_log` |

Indexes cover transaction dates, reconciliation lookups, journal and item foreign keys, bank dedupe, and the audit timestamp.

Journal sign: a positive amount is a debit, a negative amount is a credit. Expenses and sales tax are positive. The payment account is negative. A discount is a negative amount on `Discounts & Adjustments`; a fee is a positive amount on that same account.

## Layout

```text
homebase/          Python package (stdlib only)
  cli.py           python -m homebase
  ledger.py        receipts and the journal
  bank.py          CSV reconciliation
  inventory.py     pantry
  db.py            path, permissions, schema, audit log
tests/             pytest
.env.example       documented variables, no secrets
```

`Demo.py` and `Updated_Demo.py` were removed. The useful part of the v4 journal note (the Sam's Club receipt did not balance) is fixed in `homebase/ledger.py` by posting the gap explicitly. The note's approach of hiding the gap inside Groceries was not kept, and its `assert` was replaced with `LedgerImbalanceError`.
