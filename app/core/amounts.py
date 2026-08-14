"""
app/core/amounts.py

Rendering token amounts for display.

Lives in core rather than next to either caller because the alert path and the
balance API must agree: the same holding shown two ways would look like two
different numbers to the user.
"""

from decimal import Decimal, InvalidOperation


def format_amount(amount: str) -> str:
    """
    Render an amount without scientific notation.

    Decimal renders very small values in exponent form — 0.00000546 becomes
    "5.46E-6", which is unreadable in an alert about someone's money. Bitcoin
    routinely produces amounts small enough to trigger this.
    """
    try:
        value = Decimal(amount)
    except (InvalidOperation, TypeError):
        return amount

    formatted = f"{value:f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"
