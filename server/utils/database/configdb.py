"""
admin_config collection — small key/value store for settings that must be
editable from the admin panel without a code change or restart.

RAM-first: reads served from memstore.settings (zero Mongo round-trips).
Writes: RAM updated immediately, MongoDB synced async via sync_write().
"""

from typing import Any

from server.core.mongo import collection
from server.core import memstore

configdb = collection("admin_config")

DEFAULTS: dict[str, Any] = {
    "maintenance_mode":           False,
    "deposits_enabled":           True,
    "withdrawals_enabled":        True,
    "sell_requests_enabled":      True,
    "registration_enabled":       True,
    "deposit_methods":            {},
    "termination_delay_minutes":  1,
    "payment_hold_hours":         48,
    # Separate hold used ONLY for country-based AUTO-TERMINATION flows.
    # 0 / unset  ->  falls back to payment_hold_hours (backward compatible).
    "payment_hold_auto_term_hours": 48,
    "low_stock_threshold":        5,
    "order_timeout_minutes":      30,
    "platform_fee_percent":       5.0,
    # ── Session-selling rank gate ─────────────────────────────────────
    # Master toggle: when False, no user can start a sell flow.
    "session_selling_enabled":    True,
    # Minimum user rank (VIP name) allowed to sell. Empty / "VIP1" allows all.
    "session_selling_min_rank":   "VIP1",
    # ── Rank-wise discount ────────────────────────────────────────────
    # Dict keyed by rank NAME: { "VIP1": 0, "VIP2": 5, "VIP3": 10 }.
    # The user's own rank is looked up; that exact percent is applied.
    "rank_discounts":             {},
    # ── Country-wise time-limited discount ───────────────────────────
    # Dict: { "<CC>": {"percent": float, "expires_at": "<iso utc>"} }
    # Expired entries are ignored at read time.
    "country_discounts":          {},
    # ── Auto-transfer: user sell → inventory ─────────────────────────
    # When True, sessions are pushed to session_accounts inventory automatically:
    #   auto_transfer_on_approval      → immediately on admin approval
    #   auto_transfer_on_payment_release → after the payment hold timer expires
    #                                      and live re-verification passes
    # Both are False by default (safe; admin retains full manual control).
    "auto_transfer_on_approval":       False,
    "auto_transfer_on_payment_release": False,
    # ── Per-spam-status buy/sell toggles ──────────────────────────────
    # Admin can enable/disable buying or selling for each spam category
    # independently via /spam_buy and /spam_sell bot commands.
    # Buy-side: can users purchase accounts of this spam status?
    "buy_enabled_clean":      True,
    "buy_enabled_temp_spam":  True,
    "buy_enabled_perm_spam":  False,
    "buy_enabled_frozen":     False,
    "buy_enabled_unknown":    True,
    # Sell-side: can users submit sell requests for accounts of this spam status?
    "sell_enabled_clean":     True,
    "sell_enabled_temp_spam": True,
    "sell_enabled_perm_spam": False,
    "sell_enabled_frozen":    False,
    "sell_enabled_unknown":   True,
    # ── Sales Feed ─────────────────────────────────────────────────────────
    # Logs real purchases to a configurable Telegram chat.
    "sales_feed_enabled":           False,
    "sales_feed_chat_id":           "",
    "sales_feed_silent":            False,
    "sales_feed_delay_seconds":     0,
    # ── Fake Sales ─────────────────────────────────────────────────────────
    # Synthetic feed events are isolated from orders, balances, stock, and DB
    # business records; they only publish to the configured feed chat.
    "fake_sales_enabled":              False,
    "fake_sales_interval_min":         300,
    "fake_sales_interval_max":         900,
    "fake_sales_randomization_level":  5,
    "fake_sales_product_pool":         ["account", "session"],
    "fake_sales_country_pool":         [],
    # When enabled, fake stock stops displaying once real stock reaches zero.
    "hide_fake_stock_when_real_zero":  False,
    "miniapp_enabled":                 True,
}


def coerce_setting_value(key: str, value: Any) -> Any:
    """Normalize admin setting values to the same type as DEFAULTS."""
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        if value is None:
            return default
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, dict):
        if isinstance(value, dict):
            return value
        return default
    if isinstance(default, str):
        if value is None:
            return default
        return str(value)
    return value


async def get_setting(key: str) -> Any:
    return coerce_setting_value(key, memstore.settings.get(key, DEFAULTS.get(key)))


async def get_all_settings() -> dict:
    data = dict(DEFAULTS)
    data.update(memstore.settings)
    return {key: coerce_setting_value(key, value) for key, value in data.items()}


async def set_setting(key: str, value: Any) -> None:
    value = coerce_setting_value(key, value)
    memstore.settings[key] = value
    _key, _value = key, value
    memstore.sync_write(
        lambda: configdb.update_one(
            {"key": _key},
            {"$set": {"value": _value}},
            upsert=True,
        ),
        f"set_setting:{key}",
    )
