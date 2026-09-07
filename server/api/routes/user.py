"""
User API — profile, balance, rank, referral, orders, transactions, API key management.

Authentication: X-Api-Key: tg_{user_id}_{secret}

Response envelope (all endpoints):
  Success  → { "success": true, "message": "...", "data": {...}, "meta": {...}, "pagination": {...} }
  Error    → { "success": false, "message": "...", "error": {"code": N, "type": "...", "message": "..."}, "meta": {...} }
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.api.deps import require_api_key
from server.api.response import ok, err, paginate, ERR_NOT_FOUND, ERR_FORBIDDEN, ERR_CONFLICT
from server.utils.database import (
    get_user_stats,
    get_balance,
    get_wallet_snapshot,
    regenerate_api_key,
    get_referral_stats,
    set_user_lang,
    set_referral_notifications,
)
from strings import LANGUAGES
from server.logging import LOGGER

router = APIRouter(prefix="/api/v1/user", tags=["User"])
_log = LOGGER(__name__)

ALLOWED_LANGS = list(LANGUAGES.keys())

# Rank tier thresholds — mirrors userdb.RANK_THRESHOLDS; kept here for API metadata
_RANK_TIERS = [
    {"rank": "VIP1",    "min_spend": 0,      "min_buys": 0,   "label": "VIP 1",    "badge": "🥉"},
    {"rank": "VIP2",    "min_spend": 100,    "min_buys": 5,   "label": "VIP 2",    "badge": "🥈"},
    {"rank": "VIP3",    "min_spend": 500,    "min_buys": 20,  "label": "VIP 3",    "badge": "🥇"},
    {"rank": "PREMIUM", "min_spend": 2000,   "min_buys": 100, "label": "Premium",  "badge": "💎"},
    {"rank": "DIAMOND", "min_spend": 10000,  "min_buys": 500, "label": "Diamond",  "badge": "👑"},
]

_RANK_IDX = {r["rank"]: i for i, r in enumerate(_RANK_TIERS)}


def _rank_meta(rank: str, total_spend: float, total_buys: int) -> dict:
    """Build rich rank information for the API response."""
    idx = _RANK_IDX.get(rank, 0)
    current = _RANK_TIERS[idx]
    next_tier = _RANK_TIERS[idx + 1] if idx + 1 < len(_RANK_TIERS) else None

    result = {
        "rank":       rank,
        "label":      current["label"],
        "badge":      current["badge"],
        "is_max_rank": next_tier is None,
    }

    if next_tier:
        spend_needed = max(0.0, next_tier["min_spend"] - total_spend)
        buys_needed  = max(0, next_tier["min_buys"]  - total_buys)
        spend_pct    = min(100, int((total_spend / next_tier["min_spend"]) * 100)) if next_tier["min_spend"] else 100
        buys_pct     = min(100, int((total_buys  / next_tier["min_buys"])  * 100)) if next_tier["min_buys"]  else 100
        result["next_rank"] = {
            "rank":             next_tier["rank"],
            "label":            next_tier["label"],
            "badge":            next_tier["badge"],
            "spend_required":   next_tier["min_spend"],
            "buys_required":    next_tier["min_buys"],
            "spend_remaining":  round(spend_needed, 2),
            "buys_remaining":   buys_needed,
            "spend_progress_pct": spend_pct,
            "buys_progress_pct":  buys_pct,
        }
    else:
        result["next_rank"] = None

    return result


def _serialize_dt(doc: dict, *keys: str) -> dict:
    """Serialize datetime fields to ISO-8601 strings in-place; skip missing/None."""
    for k in keys:
        v = doc.get(k)
        if v is not None:
            try:
                doc[k] = v.isoformat()
            except AttributeError:
                pass
    return doc


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/me", summary="Get your full profile")
async def get_my_profile(user=Depends(require_api_key)):
    """
    Returns your complete profile including balance, rank metadata, referral
    stats, order/sell counts, lifetime totals, and account flags.

    Designed to power a complete profile screen without additional API calls.
    """
    stats, snapshot = await asyncio.gather(
        get_user_stats(user["user_id"]),
        get_wallet_snapshot(user["user_id"]),
    )
    if not stats:
        return err(404, "User profile not found.", ERR_NOT_FOUND)

    total_spend = float(stats.get("total_spend", 0))
    total_buys  = int(stats.get("total_account_buy", 0))

    joined_at = stats.get("joined_at")
    data = {
        # Identity
        "user_id":        stats.get("user_id"),
        "username":       stats.get("username"),
        "full_name":      stats.get("full_name"),
        "is_verified":    stats.get("is_verified", False),
        "api_access":     stats.get("api_access", True),

        # Financials
        "balance":            snapshot["balance"],
        "reserve_balance":    snapshot["reserve_balance"],
        "reserved_balance":   snapshot["reserved_balance"],
        "net_spendable":      round(max(0.0, snapshot["balance"] - snapshot["reserved_balance"]), 4),
        "currency":           "USD",
        "total_deposit":      round(float(stats.get("total_deposit", 0)), 4),
        "total_spend":        round(total_spend, 4),
        "total_earn":         round(float(stats.get("total_earn", 0)), 4),

        # Activity
        "total_account_buy":  total_buys,
        "total_account_sell": int(stats.get("total_account_sell", 0)),

        # Referral
        "referral_count":     int(stats.get("referral_count", 0)),
        "referral_earnings":  round(float(stats.get("referral_earnings", 0)), 4),

        # Rank (rich metadata)
        "rank": _rank_meta(stats.get("rank", "VIP1"), total_spend, total_buys),

        # Timestamps
        "joined_at": joined_at.isoformat() if joined_at else None,
    }

    return ok(data=data, message="Profile retrieved successfully.")


@router.get("/balance", summary="Get your current balance")
async def get_my_balance(user=Depends(require_api_key)):
    """
    Returns your current balance breakdown: spendable, reserved, and net amounts.
    Suitable for wallet cards on mobile and web.
    """
    snapshot = await get_wallet_snapshot(user["user_id"])
    balance = snapshot["balance"]
    reserved = snapshot["reserved_balance"]
    net = max(0.0, balance - reserved)

    return ok(
        data={
            "user_id":          user["user_id"],
            "balance":          round(float(balance), 4),
            "reserved_balance": round(float(reserved), 4),
            "net_spendable":    round(float(net), 4),
            "currency":         "USD",
            "formatted": {
                "balance":          f"${balance:.2f}",
                "reserved_balance": f"${reserved:.2f}",
                "net_spendable":    f"${net:.2f}",
            },
        },
        message="Balance retrieved successfully.",
    )


# ── Rank ──────────────────────────────────────────────────────────────────────

@router.get("/me/rank", summary="Get your current VIP rank and progression")
async def get_my_rank(user=Depends(require_api_key)):
    """
    Returns your rank tier, badge, label, and progress toward the next tier.
    Includes spend/buy requirements for rank upgrades.

    Tiers: VIP1 → VIP2 ($100+5) → VIP3 ($500+20) → PREMIUM ($2K+100) → DIAMOND ($10K+500)
    """
    stats = await get_user_stats(user["user_id"])
    rank      = stats.get("rank", "VIP1") if stats else "VIP1"
    tot_spend = float(stats.get("total_spend", 0)) if stats else 0.0
    tot_buys  = int(stats.get("total_account_buy", 0)) if stats else 0

    return ok(
        data={
            "user_id": user["user_id"],
            **_rank_meta(rank, tot_spend, tot_buys),
            "stats": {
                "total_spend": round(tot_spend, 4),
                "total_buys":  tot_buys,
            },
            "all_tiers": _RANK_TIERS,
        },
        message="Rank retrieved successfully.",
    )


# ── Referral ──────────────────────────────────────────────────────────────────

@router.get("/me/referral", summary="Get your referral stats and link")
async def get_my_referral(user=Depends(require_api_key)):
    """
    Returns your referral code, shareable bot link, total referral count,
    and lifetime referral earnings.
    """
    stats = await get_referral_stats(user["user_id"])

    code = stats.get("code")
    bot_link = f"https://t.me/start?startapp=ref_{code}" if code else None

    return ok(
        data={
            "user_id":           user["user_id"],
            "referral_code":     code,
            "referral_link":     bot_link,
            "total_referrals":   int(stats.get("count", 0)),
            "lifetime_earnings": round(float(stats.get("earnings", 0)), 4),
            "formatted_earnings": f"${stats.get('earnings', 0):.2f}",
        },
        message="Referral stats retrieved successfully.",
    )


# ── Orders ────────────────────────────────────────────────────────────────────

@router.get("/orders", summary="List your purchase orders")
async def get_my_orders(
    user=Depends(require_api_key),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    status: Optional[str] = Query(
        None,
        description="Filter by status: pending | completed | cancelled | refunded",
    ),
):
    """
    Returns your purchase order history, newest first.
    Each order includes full details including delivery info when available.
    """
    from server.utils.database.orderdb import ordersdb

    query: dict = {"buyer_id": user["user_id"]}
    if status:
        valid_statuses = {"pending", "completed", "cancelled", "refunded", "failed"}
        if status not in valid_statuses:
            return err(400, f"Invalid status '{status}'. Allowed: {', '.join(sorted(valid_statuses))}")
        query["status"] = status

    skip = (page - 1) * limit

    async def _fetch_page() -> list[dict]:
        cursor = (
            ordersdb.find(query, {"_id": 0})
            .sort([("created_at", -1), ("order_id", -1)])
            .skip(skip)
            .limit(limit)
        )
        orders = []
        async for order in cursor:
            _serialize_dt(order, "created_at", "completed_at", "cancelled_at", "delivered_at", "updated_at")
            orders.append(order)
        return orders

    total, orders = await asyncio.gather(
        ordersdb.count_documents(query),
        _fetch_page(),
    )

    # Summary stats for this user (always useful for dashboard cards)
    total_all, total_completed, total_pending = await asyncio.gather(
        ordersdb.count_documents({"buyer_id": user["user_id"]}),
        ordersdb.count_documents({"buyer_id": user["user_id"], "status": "completed"}),
        ordersdb.count_documents({"buyer_id": user["user_id"], "status": "pending"}),
    )

    return paginate(
        data=orders,
        page=page,
        limit=limit,
        total=total,
        message="Orders retrieved successfully.",
        summary={
            "total_orders":    total_all,
            "completed_orders": total_completed,
            "pending_orders":   total_pending,
        },
    )


@router.get("/orders/{order_id}", summary="Get a specific order")
async def get_my_order(order_id: str, user=Depends(require_api_key)):
    """Returns full details for one of your orders, identified by order_id."""
    from server.utils.database.orderdb import get_order

    order = await get_order(order_id)
    if not order:
        return err(404, f"Order '{order_id}' not found.", ERR_NOT_FOUND)
    if order.get("buyer_id") != user["user_id"]:
        return err(403, "You do not have access to this order.", ERR_FORBIDDEN)

    order.pop("_id", None)
    _serialize_dt(order, "created_at", "completed_at", "cancelled_at", "delivered_at", "updated_at")

    return ok(data=order, message="Order retrieved successfully.")


@router.delete("/orders/{order_id}", summary="Cancel a pending order")
async def cancel_my_order(order_id: str, user=Depends(require_api_key)):
    """
    Cancel your own pending order and receive a full refund.

    Only **pending** orders can be self-cancelled. For completed or disputed
    orders contact support.
    """
    from server.utils.database.orderdb import get_order, cancel_order
    from server.utils.database.userdb import update_balance as _add_balance
    from server.utils.database.walletdb import log_transaction

    order = await get_order(order_id)
    if not order:
        return err(404, f"Order '{order_id}' not found.", ERR_NOT_FOUND)
    if order.get("buyer_id") != user["user_id"]:
        return err(403, "You do not have access to this order.", ERR_FORBIDDEN)
    if order.get("status") != "pending":
        return err(
            409,
            f"Order is '{order.get('status')}' — only pending orders can be self-cancelled. "
            "Contact support for other cases.",
            ERR_CONFLICT,
        )

    cancelled = await cancel_order(order_id, reason="Cancelled by buyer via API")
    if not cancelled:
        return err(
            409,
            "Order could not be cancelled — it may have just been processed. "
            "Please check the order status again.",
            ERR_CONFLICT,
        )

    # Release every account reserved by this order. Batch orders keep the
    # complete `account_ids` list; older single-account orders only have
    # `account_id`. The conditional DB update makes this safe if a recovery
    # worker already released a reservation.
    from server.utils.database.sessiondb import revert_session_sold
    account_ids = order.get("account_ids") or ([order.get("account_id")] if order.get("account_id") else [])
    for account_id in dict.fromkeys(account_ids):
        try:
            await revert_session_sold(account_id)
        except Exception as exc:
            _log.error(
                "Buyer cancellation could not release reserved account %s for order %s: %s",
                account_id, order_id, exc, exc_info=True,
            )

    amount = float(order.get("amount", 0.0))
    if amount > 0:
        await _add_balance(user["user_id"], amount)
        await log_transaction(
            user_id=user["user_id"],
            txn_type="refund",
            amount=amount,
            ref_id=order_id,
            note="Buyer self-cancelled order via API",
        )

    new_balance = await get_balance(user["user_id"])

    return ok(
        data={
            "order_id":       order_id,
            "status":         "cancelled",
            "refunded_amount": round(amount, 4),
            "new_balance":    round(float(new_balance), 4),
            "currency":       "USD",
        },
        message=f"Order cancelled and ${amount:.2f} refunded to your balance.",
    )


# ── Transactions ──────────────────────────────────────────────────────────────

@router.get("/transactions", summary="List your transaction history")
async def get_my_transactions(
    user=Depends(require_api_key),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    txn_type: Optional[str] = Query(
        None,
        description="Filter by type: deposit | withdrawal | purchase | sale | refund | bonus | adjustment",
    ),
):
    """
    Returns your full transaction ledger (credits and debits), newest first.
    Each transaction records what changed your balance and why.
    """
    from server.utils.database.walletdb import transactionsdb

    valid_types = {
        "deposit", "withdrawal", "purchase", "sale",
        "refund", "bonus", "adjustment", "referral",
    }
    if txn_type and txn_type not in valid_types:
        return err(400, f"Invalid type '{txn_type}'. Allowed: {', '.join(sorted(valid_types))}")

    query: dict = {"user_id": user["user_id"]}
    if txn_type:
        query["type"] = txn_type

    skip  = (page - 1) * limit
    total = await transactionsdb.count_documents(query)
    cursor = (
        transactionsdb.find(query, {"_id": 0})
        .sort([("created_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit)
    )
    txns = []
    async for t in cursor:
        _serialize_dt(t, "created_at")
        txns.append(t)

    return paginate(
        data=txns,
        page=page,
        limit=limit,
        total=total,
        message="Transactions retrieved successfully.",
    )


# ── Session history ───────────────────────────────────────────────────────────

@router.get("/sessions/bought", summary="List session accounts you have purchased")
async def get_my_bought_sessions(
    user=Depends(require_api_key),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    country_code: Optional[str] = Query(None, description="Filter by country code (e.g. IN, UZ)"),
):
    """
    Returns all session accounts you have purchased, newest first.
    Sensitive fields (2FA password, raw session bytes) are excluded.
    """
    from server.utils.database.sessiondb import sessionaccountsdb

    query: dict = {"sold_to": user["user_id"], "sold": True}
    if country_code:
        query["country_code"] = country_code.upper()

    skip  = (page - 1) * limit
    total = await sessionaccountsdb.count_documents(query)

    _exclude = {
        "_id": 0,
        "tfa_password_enc": 0,
        "password": 0,
        "session_msg_id": 0,
        "session_chat_id": 0,
        "session_bytes_enc": 0,
    }
    cursor = (
        sessionaccountsdb.find(query, _exclude)
        .sort([("sold_at", -1), ("account_id", -1)])
        .skip(skip)
        .limit(limit)
    )
    sessions = []
    async for s in cursor:
        _serialize_dt(s, "uploaded_at", "sold_at", "login_time")
        sessions.append(s)

    return paginate(
        data=sessions,
        page=page,
        limit=limit,
        total=total,
        message="Purchased sessions retrieved successfully.",
    )


@router.get("/sessions/sold", summary="List sessions you have sold to the platform")
async def get_my_sold_sessions(
    user=Depends(require_api_key),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    status: Optional[str] = Query(None, description="Filter by status: pending | accepted | rejected"),
):
    """
    Returns all sell requests you have submitted, including accepted,
    rejected, and pending ones — with the reason for any rejection.
    Raw session bytes are excluded from the response.
    """
    from server.utils.database.sellrequestdb import sellrequestsdb

    query: dict = {"user_id": user["user_id"]}
    if status:
        query["status"] = status

    skip  = (page - 1) * limit
    total = await sellrequestsdb.count_documents(query)

    cursor = (
        sellrequestsdb.find(query, {"_id": 0, "session_bytes_enc": 0})
        .sort([("submitted_at", -1), ("request_id", -1)])
        .skip(skip)
        .limit(limit)
    )
    requests = []
    async for r in cursor:
        _serialize_dt(r, "submitted_at", "reviewed_at", "payment_release_at")
        requests.append(r)

    return paginate(
        data=requests,
        page=page,
        limit=limit,
        total=total,
        message="Sell requests retrieved successfully.",
    )


# ── API key management ────────────────────────────────────────────────────────

@router.post("/me/api-key/regenerate", summary="Regenerate your API key")
async def regenerate_my_api_key(user=Depends(require_api_key)):
    """
    Rotates your API key. The old key is **immediately** invalidated.

    ⚠️ Update all your integrations with the new key before calling this.
    There is no grace period — the old key stops working instantly.
    """
    new_key = await regenerate_api_key(user["user_id"])
    return ok(
        data={
            "api_key":    new_key,
            "user_id":    user["user_id"],
            "key_format": "tg_{user_id}_{secret}",
            "header":     "X-Api-Key",
        },
        message="API key regenerated successfully. Your old key is now invalid — update all integrations immediately.",
    )


# ── Settings ──────────────────────────────────────────────────────────────────

class LangIn(BaseModel):
    lang: str


@router.patch("/me/language", summary="Update your preferred language")
async def update_language(body: LangIn, user=Depends(require_api_key)):
    """
    Set your preferred language for bot messages.
    The change takes effect on your next bot interaction.
    """
    lang = body.lang.strip().lower()
    if lang not in ALLOWED_LANGS:
        return err(400, f"Invalid language code '{lang}'. Supported: {', '.join(sorted(ALLOWED_LANGS))}")

    await set_user_lang(user["user_id"], lang)

    return ok(
        data={"language": lang, "supported_languages": ALLOWED_LANGS},
        message=f"Language updated to '{lang}' successfully.",
    )


class NotifIn(BaseModel):
    enabled: bool


@router.patch("/me/referral-notifications", summary="Toggle referral notifications")
async def update_referral_notifications(body: NotifIn, user=Depends(require_api_key)):
    """
    Enable or disable Telegram DM notifications when someone uses your referral code.
    """
    await set_referral_notifications(user["user_id"], body.enabled)
    action = "enabled" if body.enabled else "disabled"

    return ok(
        data={
            "referral_notifications_enabled": body.enabled,
            "user_id": user["user_id"],
        },
        message=f"Referral notifications {action} successfully.",
    )
