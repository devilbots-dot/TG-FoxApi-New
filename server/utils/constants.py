"""
Shared application-level constants.

Centralises values that were previously duplicated across bot plugins
and API routes.  Import from here instead of defining locally.
"""

# ── Wallet limits ─────────────────────────────────────────────────────────────

#: Minimum allowed deposit amount in USD.
MIN_DEPOSIT: float = 0.2

#: Minimum allowed withdrawal amount in USD.
MIN_WITHDRAWAL: float = 1.0

# ── Pagination ────────────────────────────────────────────────────────────────

#: Default number of countries shown per page in the bot market grid.
PAGE_SIZE: int = 10

# ── Trade settings ─────────────────────────────────────────────────────────────

#: Platform fee taken on each sale (percentage, e.g. 5.0 = 5%).
#: Must match the value used in PLATFORM_FEE_PERCENT in orderdb.py.
PLATFORM_FEE_PERCENT: float = 5.0
