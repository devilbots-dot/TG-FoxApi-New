import secrets
from datetime import datetime
from typing import Optional, List

from server.core.mongo import collection
from server.utils.common import utcnow as _now
from server.utils.constants import PLATFORM_FEE_PERCENT as _DEFAULT_FEE_PERCENT

ordersdb = collection("orders")


def _get_fee_percent() -> float:
    """Return the current platform fee % from memstore (config-driven, default 5.0)."""
    try:
        from server.core import memstore
        return float(memstore.settings.get("platform_fee_percent", _DEFAULT_FEE_PERCENT))
    except Exception:
        return _DEFAULT_FEE_PERCENT


# Public alias kept for backward-compat imports in server/utils/database/__init__.py
PLATFORM_FEE_PERCENT = _DEFAULT_FEE_PERCENT


def _generate_order_id() -> str:
    return f"ORD-{secrets.token_hex(8).upper()}"


ORDER_STATUS = ["pending", "completed", "cancelled", "disputed", "refunded"]


# ─── Create Order ───────────────────────────────────────────────────────────

async def create_order(
    buyer_id: int,
    seller_id: int,
    account_id: str,
    amount: float,
    *,
    quantity: int = 1,
    account_ids: Optional[List[str]] = None,
    order_type: str = "account",
) -> str:
    fee = round(amount * _get_fee_percent() / 100, 4)
    net_amount = round(amount - fee, 4)
    order_id = _generate_order_id()
    doc = {
        "order_id": order_id,
        "buyer_id": buyer_id,
        "seller_id": seller_id,
        "account_id": account_id,
        "account_ids": account_ids or [account_id],
        "quantity": int(quantity),
        "order_type": order_type,
        "amount": round(amount, 4),
        "fee": fee,
        "net_amount": net_amount,
        "status": "pending",
        "dispute_reason": None,
        "refund_reason": None,
        "created_at": _now(),
        "completed_at": None,
        "cancelled_at": None,
        "delivered_at": None,
        "delivery_status": "pending",
        "successful_accounting": False,
        "sales_feed_queued": False,
    }
    await ordersdb.insert_one(doc)
    return order_id


# ─── Update Order Status ────────────────────────────────────────────────────

async def complete_order(order_id: str) -> bool:
    """Complete an order only after delivery has been recorded.

    Kept for backwards compatibility with admin/legacy callers, but deliberately
    refuses to turn a paid-but-undelivered order into a successful sale.
    """
    result = await ordersdb.update_one(
        {
            "order_id": order_id,
            "status": "pending",
            "delivery_status": "delivered",
            "delivered_at": {"$ne": None},
        },
        {"$set": {"status": "completed", "completed_at": _now()}},
    )
    return result.modified_count > 0


async def complete_order_after_delivery(order_id: str) -> bool:
    """Atomically mark a successfully delivered order as completed."""
    result = await ordersdb.update_one(
        {
            "order_id": order_id,
            "status": {"$in": ["pending", "completed"]},
            "delivery_status": "processing",
        },
        {
            "$set": {
                "status": "completed",
                "delivery_status": "delivered",
                "delivered_at": _now(),
                "completed_at": _now(),
            }
        },
    )
    return result.modified_count > 0


async def mark_order_delivered(order_id: str) -> bool:
    """
    Records when the buyer actually received their login credentials
    (phone + OTP + 2FA password) — distinct from `completed_at`, which is
    Completion is intentionally tied to this delivery event, not payment time.
    Duplicate calls are harmless because the transition is conditional.
    """
    result = await ordersdb.update_one(
        {
            "order_id": order_id,
            "status": {"$in": ["pending", "completed"]},
            "delivery_status": {"$in": ["pending", "processing"]},
        },
        {
            "$set": {
                "status": "completed",
                "delivery_status": "delivered",
                "delivered_at": _now(),
                "completed_at": _now(),
            }
        },
    )
    return result.modified_count > 0


async def claim_order_delivery(order_id: str) -> bool:
    """Claim a new batch for delivery; concurrent callbacks get False."""
    result = await ordersdb.update_one(
        {
            "order_id": order_id,
            "status": {"$in": ["pending", "completed"]},
            "delivery_status": "pending",
        },
        {"$set": {"delivery_status": "processing"}},
    )
    return result.modified_count > 0


async def finish_order_delivery(order_id: str) -> bool:
    result = await ordersdb.update_one(
        {
            "order_id": order_id,
            "status": {"$in": ["pending", "completed"]},
            "delivery_status": "processing",
        },
        {
            "$set": {
                "status": "completed",
                "delivery_status": "delivered",
                "delivered_at": _now(),
                "completed_at": _now(),
            }
        },
    )
    return result.modified_count > 0


async def cancel_order(order_id: str, reason: str = "") -> bool:
    result = await ordersdb.update_one(
        {"order_id": order_id, "status": "pending"},
        {
            "$set": {
                "status": "cancelled",
                "refund_reason": reason,
                "cancelled_at": _now(),
            }
        },
    )
    return result.modified_count > 0


async def dispute_order(order_id: str, reason: str) -> bool:
    result = await ordersdb.update_one(
        {"order_id": order_id, "status": "completed"},
        {"$set": {"status": "disputed", "dispute_reason": reason}},
    )
    return result.modified_count > 0


async def refund_order(order_id: str, reason: str = "") -> bool:
    # NOTE: "completed" is included here (not just "pending"/"disputed")
    # because buy_from_server() marks orders "completed" immediately at
    # payment time, before the OTP/2FA delivery flow even starts. Every
    # real refund path — the buyer's own aborted/failed OTP flow
    # (server/plugins/bot/market.py: _refund_buy_auth, _deliver_one_session)
    # and the admin "Refund" button (server/admin/routes/orders.py) — refunds
    # an order that is already "completed". Excluding it here silently
    # no-ops the DB update while the balance is still credited back,
    # leaving the order ledger showing "completed" for an order that was
    # actually refunded.
    result = await ordersdb.update_one(
        {"order_id": order_id, "status": {"$in": ["pending", "disputed", "completed"]}},
        {"$set": {
            "status": "refunded",
            "refund_reason": reason,
            "delivery_status": "refunded",
        }},
    )
    return result.modified_count > 0


# ─── Fetch Orders ───────────────────────────────────────────────────────────

async def get_order(order_id: str) -> Optional[dict]:
    return await ordersdb.find_one({"order_id": order_id})


async def get_buyer_orders(buyer_id: int, limit: int = 20) -> List[dict]:
    cursor = ordersdb.find({"buyer_id": buyer_id}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_seller_orders(seller_id: int, limit: int = 20) -> List[dict]:
    cursor = ordersdb.find({"seller_id": seller_id}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_all_orders(status: str = None, limit: int = 50) -> List[dict]:
    query = {}
    if status:
        query["status"] = status
    cursor = ordersdb.find(query).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def count_orders(status: str = None) -> int:
    query = {}
    if status:
        query["status"] = status
    return await ordersdb.count_documents(query)


async def get_total_volume() -> float:
    pipeline = [
        {"$match": {"status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    result = await ordersdb.aggregate(pipeline).to_list(length=1)
    if not result:
        return 0.0
    return round(result[0]["total"], 4)


async def get_total_fees_collected() -> float:
    pipeline = [
        {"$match": {"status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": "$fee"}}},
    ]
    result = await ordersdb.aggregate(pipeline).to_list(length=1)
    if not result:
        return 0.0
    return round(result[0]["total"], 4)
