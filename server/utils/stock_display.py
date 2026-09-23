"""
Displayed-stock helper.

For fake-stock mode the buyer-visible stock is the configured fake stock
PLUS the current real unsold inventory.  Real inventory is consumed by
purchases, so the displayed number decreases with each real purchase.

When real inventory reaches zero, the country is hidden as out-of-stock
(displayed stock becomes 0), even if a fake stock value is configured.
"""

from __future__ import annotations

from server.core import memstore


SETTING_KEY = "hide_fake_stock_when_real_zero"


def hide_fake_when_real_zero() -> bool:
    """Read the legacy admin safety toggle from RAM."""
    try:
        value = memstore.settings.get(SETTING_KEY, False)
    except Exception:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def displayed_stock(country: dict, real_stock: int) -> int:
    """Return the buyer-visible stock for a country.

    Normal mode:
        displayed stock = real stock

    Fake mode:
        displayed stock = fake stock + real stock while real stock > 0

    Once real inventory reaches zero:
        displayed stock = 0
    """
    try:
        real_stock = max(0, int(real_stock or 0))
    except (TypeError, ValueError):
        real_stock = 0

    if (country or {}).get("stock_mode") != "fake":
        return real_stock

    # Real inventory is required for the country to remain available.
    if real_stock <= 0:
        return 0

    try:
        fake_stock = max(0, int((country or {}).get("fake_stock", 0) or 0))
    except (TypeError, ValueError):
        fake_stock = 0

    return fake_stock + real_stock
    
