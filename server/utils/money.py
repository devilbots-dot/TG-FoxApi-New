"""Decimal-safe validation for money values at external request boundaries.

The existing MongoDB schema stores compatible numeric values. This helper does
not migrate or rewrite those records; it prevents untrusted float, NaN,
infinity, excessive precision, and out-of-range values from entering services.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import HTTPException

_SCALE = Decimal("0.0001")
_MAX_AMOUNT = Decimal("1000000")


def parse_money(value: object, *, field: str, minimum: float | Decimal = 0, maximum: float | Decimal = _MAX_AMOUNT) -> float:
    """Return a finite, positive, four-decimal monetary amount as a float.

    The float return preserves current service/database contracts while all
    external parsing and rounding occurs with Decimal first.
    """
    if isinstance(value, bool) or value is None:
        raise HTTPException(400, f"A valid {field} is required.")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        raise HTTPException(400, f"A valid {field} is required.")
    if not amount.is_finite():
        raise HTTPException(400, f"A valid {field} is required.")

    minimum_decimal = Decimal(str(minimum))
    maximum_decimal = Decimal(str(maximum))
    if amount < minimum_decimal:
        raise HTTPException(400, f"{field.capitalize()} must be at least ${minimum_decimal:.2f}.")
    if amount > maximum_decimal:
        raise HTTPException(400, f"{field.capitalize()} exceeds the permitted limit.")
    return float(amount.quantize(_SCALE, rounding=ROUND_HALF_UP))
