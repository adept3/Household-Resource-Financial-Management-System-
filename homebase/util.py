"""Shared helpers: money, validation, and a mode-checked .env loader.

No database is opened here. Importing this module has no side effects.
"""

from __future__ import annotations

import math
import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class HomeBaseError(Exception):
    """Base class for errors raised by HomeBase."""


class ValidationError(HomeBaseError):
    """Input failed validation. Nothing was written."""


class LedgerImbalanceError(HomeBaseError):
    """A journal does not sum to zero. This is a real error, not an assert."""

    def __init__(self, transaction_id: int, balance_cents: int):
        self.transaction_id = transaction_id
        self.balance_cents = balance_cents
        super().__init__(
            f"Journal for transaction {transaction_id} is out of balance by {balance_cents} cents"
        )


def to_cents(amount: object) -> int:
    """Convert a dollar amount to integer cents.

    Uses Decimal, not binary floating-point multiplication. Integers are
    dollar amounts too: ``to_cents(3)`` is 300, not 3.
    """
    if isinstance(amount, bool) or amount is None:
        raise ValidationError("amount must be a number of dollars")
    if isinstance(amount, Decimal):
        dec = amount
    elif isinstance(amount, int):
        dec = Decimal(amount)
    elif isinstance(amount, float):
        if not math.isfinite(amount):
            raise ValidationError("amount must be finite")
        dec = Decimal(str(amount))
    elif isinstance(amount, str):
        text = amount.strip().replace("$", "").replace(",", "")
        if text == "" or text in {"+", "-"}:
            raise ValidationError(f"amount is not a number: {amount!r}")
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise ValidationError(f"amount is not a number: {amount!r}") from exc
    else:
        raise ValidationError("amount must be a number of dollars")
    if not dec.is_finite():
        raise ValidationError("amount must be finite")
    quantized = dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def fmt_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(int(cents)) / 100:.2f}"


def parse_iso_date(value: object, field: str = "date") -> date:
    if not isinstance(value, str) or ISO_DATE.fullmatch(value) is None:
        raise ValidationError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} is not a real calendar date") from exc


def sanitize_str(value: object, field: str, *, max_len: int = 200, allow_empty: bool = False) -> str:
    """Strip nulls and control characters, collapse whitespace, and bound length."""
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    cleaned = "".join(ch for ch in value.replace("\x00", "") if ch >= " " and ch != "\x7f")
    cleaned = " ".join(cleaned.split())
    if cleaned == "" and not allow_empty:
        raise ValidationError(f"{field} is required")
    if len(cleaned) > max_len:
        raise ValidationError(f"{field} exceeds {max_len} characters")
    return cleaned


def load_dotenv(path: str | os.PathLike[str] | None = None, *, override: bool = False) -> bool:
    """Load KEY=VALUE pairs from a .env file that is mode 0600.

    Existing environment variables win unless ``override`` is set. A missing
    file is ignored. A file that is group- or world-readable is refused so a
    secret file with a loose mode is never silently used.
    """
    env_path = Path(path or os.environ.get("HOMEBASE_ENV_FILE") or ".env")
    if not env_path.is_file():
        return False
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"{env_path} is mode {mode:03o}; secrets must be mode 0600. Run: chmod 600 {env_path}"
        )
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == "" or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if ENV_KEY.fullmatch(key) is None:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    return True
