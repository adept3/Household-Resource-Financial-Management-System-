"""HomeBase: a private household ledger.

Importing this package does not open, create, or delete a database.
"""

from homebase.bank import import_bank_csv
from homebase.db import default_db_path, open_db
from homebase.inventory import get_inventory_report, get_low_stock_alerts
from homebase.ledger import get_account_balances, get_hsa_summary, get_tax_summary, ingest_receipt
from homebase.notify import check_and_notify_alerts
from homebase.util import LedgerImbalanceError, ValidationError

__version__ = "0.5.0"

__all__ = [
    "LedgerImbalanceError",
    "ValidationError",
    "check_and_notify_alerts",
    "default_db_path",
    "get_account_balances",
    "get_hsa_summary",
    "get_inventory_report",
    "get_low_stock_alerts",
    "get_tax_summary",
    "import_bank_csv",
    "ingest_receipt",
    "open_db",
]
