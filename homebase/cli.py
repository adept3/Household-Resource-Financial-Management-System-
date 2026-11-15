"""Command-line entry point. ``python -m homebase <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from homebase.bank import import_bank_csv
from homebase.bills import list_subscriptions
from homebase.compose import generate_docker_compose
from homebase.db import default_db_path, open_db
from homebase.inventory import get_inventory_report
from homebase.ledger import get_account_balances, ingest_receipt
from homebase.notify import check_and_notify_alerts
from homebase.util import HomeBaseError, load_dotenv

HELP = """
HomeBase — private household ledger

  python -m homebase init
  python -m homebase demo
  python -m homebase balances
  python -m homebase ingest receipt.json
  python -m homebase import-csv statement.csv
  python -m homebase inventory
  python -m homebase subscriptions
  python -m homebase alerts
  python -m homebase backup run
  python -m homebase compose

The ledger file defaults to $XDG_DATA_HOME/homebase/homebase.db
(or ~/.local/share/homebase/homebase.db). Override with HOMEBASE_DATA_DIR
or HOMEBASE_DB. The directory is mode 0700 and the file is mode 0600.
Importing the package does not create or delete a database.
`demo` uses a throwaway file and does not touch the real ledger.
""".strip()


def _open():
    return open_db()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    try:
        load_dotenv()
    except PermissionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    command = argv[0]
    rest = argv[1:]
    try:
        if command == "demo":
            from homebase.demo import run_demo

            run_demo()
            return 0
        if command == "init":
            conn = _open()
            try:
                version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            finally:
                conn.close()
            print(f"Ledger ready at {default_db_path()} (schema {version})")
            return 0
        if command == "balances":
            conn = _open()
            try:
                for row in get_account_balances(conn):
                    if row["balance_cents"] != 0:
                        print(f"{row['account']:<28} {row['type']:<10} {row['balance']}")
            finally:
                conn.close()
            return 0
        if command == "inventory":
            conn = _open()
            try:
                report = get_inventory_report(conn)
            finally:
                conn.close()
            if not report:
                print("Pantry is empty.")
            for item in report:
                print(f"{item['product']:<24} {item['stock']:>6}  {item['status']}")
            return 0
        if command == "subscriptions":
            conn = _open()
            try:
                rows = list_subscriptions(conn)
            finally:
                conn.close()
            for row in rows:
                print(f"{row['due']}  {row['name']:<20} {row['amount']}")
            return 0
        if command == "alerts":
            parser = argparse.ArgumentParser(prog="homebase alerts")
            parser.add_argument("--send", action="store_true", help="deliver via HOMEBASE_NTFY_URL")
            args = parser.parse_args(rest)
            conn = _open()
            try:
                messages = check_and_notify_alerts(conn, send=args.send)
            finally:
                conn.close()
            for line in messages:
                print(line)
            if not messages:
                print("No alerts.")
            return 0
        if command == "ingest":
            parser = argparse.ArgumentParser(prog="homebase ingest")
            parser.add_argument("path")
            args = parser.parse_args(rest)
            payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
            conn = _open()
            try:
                txn_id = ingest_receipt(conn, payload)
            finally:
                conn.close()
            print(f"Ingested transaction {txn_id}")
            return 0
        if command == "import-csv":
            parser = argparse.ArgumentParser(prog="homebase import-csv")
            parser.add_argument("path")
            parser.add_argument("--account", default="Checking")
            args = parser.parse_args(rest)
            content = Path(args.path).read_text(encoding="utf-8")
            conn = _open()
            try:
                result = import_bank_csv(conn, content, account_name=args.account)
            finally:
                conn.close()
            print(
                f"imported {result['imported']}, matched {result['matched']}, "
                f"skipped {result['skipped']}, rejected {len(result['rejected'])}"
            )
            for row in result["rejected"]:
                print(f"  rejected line {row['line']}: {row['reason']}")
            return 0
        if command == "backup":
            from homebase.backup import dispatch

            dispatch(rest)
            return 0
        if command == "compose":
            print(generate_docker_compose(), end="")
            return 0
    except HomeBaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(HELP, file=sys.stderr)
    return 1
