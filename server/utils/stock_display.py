"""
Displayed-stock helper.

This is display-only: purchases still always consume real inventory.  A
country may show its configured fake stock while it is in fake mode.  The
admin safety toggle can force fake-mode countries to show zero as soon as
their real stock reaches zero.
"""

from __future__ import annotations

from server.core import memstore


SETTING_KEY = "hide_fake_stock_when_real_zero"


def hide_fake_when_real_zero() -> bool:
    """Read the admin safety toggle from RAM without an async DB round-trip."""
    try:
        value = memstore.settings.get(SETTING_KEY, False)
    except Exception:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def displayed_stock(country: dict, real_stock: int) -> int:
    """Return the buyer-visible stock for a country."""
    try:
        real_stock = int(real_stock or 0)
    except (TypeError, ValueError):
        real_stock = 0

    if (country or {}).get("stock_mode") != "fake":
        return max(0, real_stock)

    if real_stock <= 0 and hide_fake_when_real_zero():
        return 0

    try:
        return max(0, int((country or {}).get("fake_stock", 0) or 0))
    except (TypeError, ValueError):
        return 0
