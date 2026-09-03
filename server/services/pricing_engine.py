"""
Configurable pricing engine — adjusts sell price based on account spam status.

Rules are stored in the `admin_config` MongoDB collection under key
"pricing_rules".  Defaults are applied when no DB config exists.

Multipliers (0.0 = reject, >0 = accept at adjusted price):
  clean          → 1.00  (full price)
  temporary_spam → 0.70  (30% reduction)
  permanent_spam → 0.00  (reject)
  frozen         → 0.00  (always reject)
  unknown        → 0.85  (slight reduction for uncertainty)

Admin can override any multiplier from the admin panel via the admin_config
collection. Changes take effect on the next call (no restart required).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from server import LOGGER
from server.core.mongo import collection

_log = LOGGER(__name__)
_config_col = collection("admin_config")

# In-memory cache: (rules_dict, loop_time_when_cached)
_cache: tuple[dict, float] | None = None
_CACHE_TTL = 60.0   # seconds before re-fetching from DB

# --- Spam status display names (for user-facing messages) --------------------
SPAM_STATUS_LABELS: dict[str, str] = {
    "clean":          "✅ Clean",
    "temporary_spam": "⚠️ Temporary Restriction",
    "permanent_spam": "🚫 Permanent Spam Restriction",
    "frozen":         "🧊 Frozen",
    "unknown":        "❓ Unknown Status",
}

# --- Built-in defaults -------------------------------------------------------
_DEFAULT_RULES: dict[str, float] = {
    "clean":          1.00,
    "temporary_spam": 0.70,
    "permanent_spam": 0.00,  # 0 = reject
    "frozen":         0.00,  # 0 = always reject
    "unknown":        0.85,
}


async def get_pricing_rules() -> dict[str, float]:
    """
    Fetch pricing multipliers, merging DB overrides with defaults.

    Results are cached in memory for CACHE_TTL seconds to avoid a DB round-trip
    on every buy/sell confirmation.  Cache is invalidated immediately after
    save_pricing_rules() is called.
    """
    global _cache
    try:
        loop_time = asyncio.get_event_loop().time()
        if _cache is not None:
            rules, cached_at = _cache
            if loop_time - cached_at < _CACHE_TTL:
                return dict(rules)

        doc = await _config_col.find_one({"key": "pricing_rules"})
        if doc and isinstance(doc.get("rules"), dict):
            merged = dict(_DEFAULT_RULES)
            for k, v in doc["rules"].items():
                try:
                    merged[k] = float(v)
                except (TypeError, ValueError):
                    pass
        else:
            merged = dict(_DEFAULT_RULES)

        _cache = (merged, loop_time)
        return dict(merged)
    except Exception as exc:
        _log.warning("get_pricing_rules DB error: %s — using defaults", exc)
        return dict(_DEFAULT_RULES)


async def save_pricing_rules(rules: dict[str, float]) -> None:
    """Persist pricing multipliers to DB and invalidate the in-memory cache."""
    global _cache
    await _config_col.update_one(
        {"key": "pricing_rules"},
        {"$set": {"key": "pricing_rules", "rules": rules}},
        upsert=True,
    )
    _cache = None   # force re-fetch on next call


async def apply_pricing(base_price: float, spam_status: str) -> Optional[float]:
    """
    Compute the adjusted sell price for an account.

    Returns the adjusted price (>0) on accept, or None to signal rejection.
    Frozen and permanent_spam always return None unless the admin raises
    their multiplier above 0 in the DB.
    """
    rules = await get_pricing_rules()
    multiplier = rules.get(spam_status, rules.get("unknown", 0.85))
    if multiplier <= 0:
        return None  # reject
    adjusted = round(base_price * multiplier, 4)
    return adjusted if adjusted > 0 else None


def is_hard_reject(spam_status: str) -> bool:
    """
    Synchronous check: is this spam status a hard rejection by default?
    Used before entering the async pipeline to short-circuit early.
    Configurable overrides in the DB are NOT consulted here — this is
    purely the hardcoded baseline so callers don't need to await.
    """
    return spam_status in ("frozen", "permanent_spam")


def spam_label(spam_status: str) -> str:
    return SPAM_STATUS_LABELS.get(spam_status, spam_status)
