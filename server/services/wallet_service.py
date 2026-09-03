"""
Wallet service — business logic between the DB layer and the API/admin routes.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from server.utils.database.userdb import get_balance
from server.logging import LOGGER

PLATFORM_CURRENCY = "USD"
_TWO_PLACES = Decimal("0.01")
_log = LOGGER(__name__)


async def fetch_wallet_balance(user_id: int) -> dict:
    """Return a balance snapshot rounded to 2 decimal places."""
    raw = await get_balance(user_id)
    balance = Decimal(str(raw)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    return {"balance": balance, "currency": PLATFORM_CURRENCY}
