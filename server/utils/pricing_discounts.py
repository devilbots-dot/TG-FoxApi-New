"""
Discount pricing helpers — rank-wise and country-wise (time-limited) discounts.

Both discount sources are stored in admin_config:
  - rank_discounts    : {"<RANK_NAME>": percent}   e.g. {"VIP1":0,"VIP2":5,"VIP3":10}
  - country_discounts : {"<CC>": {"percent": float, "expires_at": "<iso>"}}

Rule: the effective discount for a user+country is the MAX of the two
applicable percentages (they do not stack).

Usage in buy pricing:
    final_price = await apply_discounts(user_id, country_code, base_price)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from server.utils.database.configdb import get_setting


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rank_names() -> list[str]:
    """Ordered list of valid rank names (lowest → highest)."""
    try:
        from server.utils.database.userdb import RANK_THRESHOLDS
        return list(RANK_THRESHOLDS.keys())
    except Exception:
        return ["VIP1", "VIP2", "VIP3"]


def rank_index(name: str) -> int:
    """Return the index of a rank name in the ordered rank list, or -1."""
    if not name:
        return -1
    ranks = _rank_names()
    try:
        return ranks.index(name)
    except ValueError:
        return -1


async def get_user_rank(user_id: int) -> str:
    """Return the user's current rank name (defaults to lowest rank)."""
    try:
        from server.utils.database.userdb import get_rank
        r = await get_rank(user_id)
        if r:
            return r
    except Exception:
        pass
    ranks = _rank_names()
    return ranks[0] if ranks else "VIP1"


async def get_active_country_discount(country_code: str) -> float:
    """Return active country discount percent (0..100), or 0.0 if none/expired."""
    cc = (country_code or "").upper()
    if not cc:
        return 0.0
    data = await get_setting("country_discounts") or {}
    entry = data.get(cc) or data.get(cc.lower())
    if not isinstance(entry, dict):
        return 0.0
    exp = _parse_iso(entry.get("expires_at"))
    if exp and exp < _now():
        return 0.0
    try:
        pct = float(entry.get("percent", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(pct, 100.0))


async def get_rank_discount(rank_name: str) -> float:
    """Return the discount percent configured for the given rank name."""
    tiers = await get_setting("rank_discounts") or {}
    if not isinstance(tiers, dict) or not rank_name:
        return 0.0
    try:
        pct = float(tiers.get(rank_name, 0) or 0)
    except (TypeError, ValueError):
        pct = 0.0
    return max(0.0, min(pct, 100.0))


def _country_discount_from_snapshot(
    country_code: str,
    country_discounts: dict[str, Any],
    *,
    evaluated_at: datetime,
) -> float:
    """Read a country discount from one request-local configuration snapshot."""
    cc = (country_code or "").upper()
    if not cc:
        return 0.0
    entry = country_discounts.get(cc) or country_discounts.get(cc.lower())
    if not isinstance(entry, dict):
        return 0.0
    expires_at = _parse_iso(entry.get("expires_at"))
    if expires_at and expires_at < evaluated_at:
        return 0.0
    try:
        pct = float(entry.get("percent", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(pct, 100.0))


async def get_discount_context(user_id: int) -> tuple[float, dict[str, Any], datetime]:
    """Load one consistent discount snapshot for a single response or checkout."""
    rank = await get_user_rank(user_id)
    rank_pct = await get_rank_discount(rank)
    country_discounts = await get_setting("country_discounts") or {}
    if not isinstance(country_discounts, dict):
        country_discounts = {}
    return rank_pct, country_discounts, _now()


def apply_discount_context(
    base_price: float,
    country_code: str,
    rank_pct: float,
    country_discounts: dict[str, Any],
    evaluated_at: datetime,
) -> float:
    """Apply a previously loaded request-local discount context to one price."""
    try:
        base = float(base_price)
    except (TypeError, ValueError):
        return float(base_price or 0)
    if base <= 0:
        return base
    country_pct = _country_discount_from_snapshot(
        country_code,
        country_discounts,
        evaluated_at=evaluated_at,
    )
    pct = max(rank_pct, country_pct)
    if pct <= 0:
        return base
    return round(base * (1.0 - pct / 100.0), 4)


async def apply_discounts(user_id: int, country_code: str, base_price: float) -> float:
    """Return the discounted price (base_price * (1 - max_discount%/100))."""
    context = await get_discount_context(user_id)
    return apply_discount_context(base_price, country_code, *context)


async def get_current_rank_label(user_id: int) -> tuple[str, str]:
    """Return (current_rank, min_required_rank) for display purposes."""
    rank = await get_user_rank(user_id)
    min_rank = str(await get_setting("session_selling_min_rank") or "")
    return rank, min_rank


async def get_user_total_spend(user_id: int) -> float:
    """Return total lifetime spend (USD) for a user (0.0 on failure)."""
    try:
        from server.utils.database.userdb import get_total_spent  # type: ignore
        val = await get_total_spent(user_id)
        if val is not None:
            return float(val or 0)
    except Exception:
        pass
    try:
        from server.core.mongo import db  # type: ignore
        total = 0.0
        cursor = db.orders.find({"user_id": int(user_id)})
        async for o in cursor:
            status = str(o.get("status", "")).lower()
            if status in ("delivered", "completed", "paid", "success"):
                try:
                    total += float(o.get("total_price") or o.get("price") or o.get("amount") or 0)
                except Exception:
                    continue
        return round(total, 4)
    except Exception:
        return 0.0


async def get_required_spend_for(rank_name: str) -> float:
    """Return admin-configured min spend (USD) required to reach rank_name."""
    if not rank_name:
        return 0.0
    thresholds = await get_setting("rank_min_spend") or {}
    if not isinstance(thresholds, dict):
        return 0.0
    try:
        return float(thresholds.get(rank_name, 0) or 0)
    except (TypeError, ValueError):
        return 0.0
