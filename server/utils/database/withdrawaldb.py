"""
Comprehensive withdrawal database layer.

Collections:
  withdrawals       — main withdrawal request documents
  withdrawal_logs   — append-only event log per withdrawal
  gateway_requests  — raw API request bodies sent to gateways
  gateway_responses — raw API response bodies received from gateways

All financial operations that change balance fields use MongoDB atomicity
(find_one_and_update with conditions) to prevent race conditions.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from pymongo import ASCENDING, DESCENDING

from server.core.mongo import collection
from server.utils.withdrawal_statuses import WithdrawalStatus
from server.utils.common import utcnow as _now

# ── Collections ───────────────────────────────────────────────────────────────

withdrawalsdb       = collection("withdrawals")
withdrawal_logsdb   = collection("withdrawal_logs")
gateway_requestsdb  = collection("gateway_requests")
gateway_responsesdb = collection("gateway_responses")


# ── ID generators ─────────────────────────────────────────────────────────────

def _gen_wit_id() -> str:
    return f"WIT-{secrets.token_hex(10).upper()}"


def _gen_log_id() -> str:
    return f"WLOG-{secrets.token_hex(8).upper()}"


# ── CRUD: Withdrawal Records ──────────────────────────────────────────────────

async def create_withdrawal_record(
    user_id: int,
    amount: float,
    currency: str,
    network: str,
    wallet_address: str,
    fee_amount: float = 0.0,
    net_amount: Optional[float] = None,
    mode: str = "auto",       # "auto" | "manual"
    gateway: str = "manual",  # provider_id or "manual"
    memo: str = "",
    withdrawal_id: Optional[str] = None,
) -> str:
    """
    Create a new withdrawal record with status=pending.
    Balance must already be reserved by the caller before calling this.
    """
    wit_id = withdrawal_id or _gen_wit_id()
    now    = _now()

    doc = {
        # ── Identifiers ───────────────────────────────────────────────────────
        "withdrawal_id":    wit_id,
        "user_id":          user_id,

        # ── Financial ────────────────────────────────────────────────────────
        "amount":           round(amount, 4),        # USD amount requested
        "fee_amount":       round(fee_amount, 4),    # fee deducted
        "net_amount":       round(net_amount if net_amount is not None else amount - fee_amount, 4),
        "currency":         currency.upper(),        # crypto, e.g. "USDT"
        "network":          network.upper(),         # e.g. "TRC20"
        "wallet_address":   wallet_address,
        "memo":             memo,

        # ── Gateway ───────────────────────────────────────────────────────────
        "mode":             mode,            # "auto" | "manual"
        "gateway":          gateway,         # provider_id
        "gateway_track_id": None,            # OxaPay track_id (set after submit)
        "gateway_status":   None,            # raw OxaPay status string
        "gateway_tx_hash":  None,            # blockchain transaction hash
        "gateway_fee":      None,            # fee reported by gateway

        # ── Status ────────────────────────────────────────────────────────────
        "status":           WithdrawalStatus.PENDING,
        "reason":           None,            # failure/rejection reason

        # ── Admin ─────────────────────────────────────────────────────────────
        "processed_by":     None,
        "admin_note":       None,

        # ── Webhook ───────────────────────────────────────────────────────────
        "webhook_received_at": None,
        "webhook_payload":     None,

        # ── Verification tracking ─────────────────────────────────────────────
        "last_verified_at":     None,
        "verification_count":   0,

        # ── Timestamps ───────────────────────────────────────────────────────
        "created_at":   now,
        "updated_at":   now,
        "completed_at": None,
    }

    await withdrawalsdb.insert_one(doc)

    # Append to transaction ledger
    from server.utils.database.walletdb import log_transaction
    await log_transaction(
        user_id=user_id,
        txn_type="withdrawal",
        amount=round(amount, 4),
        ref_id=wit_id,
        note=f"{currency} {network} to {wallet_address[:10]}…",
    )

    return wit_id


async def get_withdrawal_record(withdrawal_id: str) -> Optional[dict]:
    return await withdrawalsdb.find_one({"withdrawal_id": withdrawal_id})


async def get_user_withdrawals(
    user_id: int,
    limit: int = 20,
    page: int = 1,
    status: Optional[str] = None,
    network: Optional[str] = None,
) -> List[dict]:
    query: dict = {"user_id": user_id}
    if status:
        query["status"] = status
    if network:
        query["network"] = network.upper()
    skip = (page - 1) * limit
    cursor = withdrawalsdb.find(query).sort("created_at", DESCENDING).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)


async def count_user_withdrawals(
    user_id: int,
    status: Optional[str] = None,
    network: Optional[str] = None,
) -> int:
    query: dict = {"user_id": user_id}
    if status:
        query["status"] = status
    if network:
        query["network"] = network.upper()
    return await withdrawalsdb.count_documents(query)


async def get_withdrawal_by_track_id(track_id: str) -> Optional[dict]:
    return await withdrawalsdb.find_one({"gateway_track_id": track_id})


# ── Limit helpers ─────────────────────────────────────────────────────────────

async def get_user_active_withdrawal_count(user_id: int) -> int:
    """Count withdrawals in non-final states for this user."""
    return await withdrawalsdb.count_documents({
        "user_id": user_id,
        "status": {"$in": list(WithdrawalStatus.IN_FLIGHT)},
    })


async def get_user_daily_withdrawal_total(user_id: int) -> float:
    """Sum of completed withdrawals in the last 24 hours."""
    since = _now() - timedelta(hours=24)
    pipeline = [
        {
            "$match": {
                "user_id": user_id,
                "status": WithdrawalStatus.COMPLETED,
                "completed_at": {"$gte": since},
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    docs = await withdrawalsdb.aggregate(pipeline).to_list(length=1)
    return float((docs[0].get("total", 0) if docs else 0) or 0)


async def get_user_monthly_withdrawal_total(user_id: int) -> float:
    """Sum of completed withdrawals in the last 30 days."""
    since = _now() - timedelta(days=30)
    pipeline = [
        {
            "$match": {
                "user_id": user_id,
                "status": WithdrawalStatus.COMPLETED,
                "completed_at": {"$gte": since},
            }
        },
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    docs = await withdrawalsdb.aggregate(pipeline).to_list(length=1)
    return float((docs[0].get("total", 0) if docs else 0) or 0)


# ── Status transitions ────────────────────────────────────────────────────────

async def update_withdrawal_status(
    withdrawal_id: str,
    new_status: str,
    gateway_track_id: Optional[str] = None,
    gateway_status: Optional[str] = None,
    tx_hash: Optional[str] = None,
    reason: Optional[str] = None,
    gateway: Optional[str] = None,
) -> bool:
    """Update status fields. Does NOT touch user balance."""
    now = _now()
    update: dict = {
        "$set": {
            "status":     new_status,
            "updated_at": now,
        }
    }
    if gateway_track_id is not None:
        update["$set"]["gateway_track_id"] = gateway_track_id
    if gateway_status is not None:
        update["$set"]["gateway_status"] = gateway_status
    if tx_hash is not None:
        update["$set"]["gateway_tx_hash"] = tx_hash
    if reason is not None:
        update["$set"]["reason"] = reason
    if gateway is not None:
        update["$set"]["gateway"] = gateway
    if new_status == WithdrawalStatus.COMPLETED:
        update["$set"]["completed_at"] = now

    result = await withdrawalsdb.update_one(
        {"withdrawal_id": withdrawal_id},
        update,
    )
    return result.modified_count > 0


async def finalize_withdrawal(
    withdrawal_id: str,
    user_id: int,
    amount: float,
    tx_hash: Optional[str] = None,
    gateway_fee: Optional[float] = None,
    admin_note: Optional[str] = None,
) -> bool:
    """
    Mark withdrawal as COMPLETED and permanently deduct from reserved balance.

    Steps (both must succeed atomically per-collection):
      1. Move amount from reserved earned balance (deduct permanently)
      2. Update withdrawal status to completed
    """
    from server.utils.database.userdb import finalize_reserved_balance

    # Deduct from reserved_balance (permanent)
    deducted = await finalize_reserved_balance(user_id, amount)
    if not deducted:
        _log.warning(
            "finalize_withdrawal: reserved_balance insufficient for user=%s wit=%s amount=%.4f",
            user_id, withdrawal_id, amount,
        )
        # Still mark completed to avoid re-processing, but log the discrepancy
        from server.logging import LOGGER
        LOGGER("withdrawaldb").error(
            "Balance finalization mismatch: withdrawal=%s user=%s amount=%.4f",
            withdrawal_id, user_id, amount,
        )

    now = _now()
    update: dict = {
        "$set": {
            "status":         WithdrawalStatus.COMPLETED,
            "gateway_tx_hash": tx_hash,
            "completed_at":   now,
            "updated_at":     now,
        },
        "$inc": {"verification_count": 1},
    }
    if gateway_fee is not None:
        update["$set"]["gateway_fee"] = gateway_fee
    if admin_note is not None:
        update["$set"]["admin_note"] = admin_note

    result = await withdrawalsdb.update_one({"withdrawal_id": withdrawal_id}, update)
    return result.modified_count > 0


async def release_withdrawal(
    withdrawal_id: str,
    user_id: int,
    amount: float,
    new_status: str,
    reason: str = "",
    gateway_status: Optional[str] = None,
) -> bool:
    """
    Mark withdrawal as failed/rejected/cancelled/expired and return balance.
    Releases reserved_balance back to spendable balance.
    """
    from server.utils.database.userdb import release_reserved_balance

    await release_reserved_balance(user_id, amount)

    now = _now()
    update: dict = {
        "$set": {
            "status":     new_status,
            "reason":     reason,
            "updated_at": now,
        }
    }
    if gateway_status is not None:
        update["$set"]["gateway_status"] = gateway_status

    result = await withdrawalsdb.update_one({"withdrawal_id": withdrawal_id}, update)
    return result.modified_count > 0


async def mark_webhook_received(
    withdrawal_id: str,
    payload: dict,
    new_status: Optional[str] = None,
) -> bool:
    """Record the raw webhook payload and optionally update status."""
    now = _now()
    update: dict = {
        "$set": {
            "webhook_payload":     payload,
            "webhook_received_at": now,
            "updated_at":          now,
        },
        "$inc": {"verification_count": 1},
    }
    if new_status:
        update["$set"]["status"]       = new_status
        update["$set"]["gateway_status"] = payload.get("status")
    if payload.get("tx_hash"):
        update["$set"]["gateway_tx_hash"] = payload["tx_hash"]
    if new_status == WithdrawalStatus.COMPLETED:
        update["$set"]["completed_at"] = now

    result = await withdrawalsdb.update_one({"withdrawal_id": withdrawal_id}, update)
    return result.modified_count > 0


# ── Gateway request/response log ──────────────────────────────────────────────

async def store_gateway_result(withdrawal_id: str, result) -> None:
    """Persist the raw gateway PayoutResult for audit purposes."""
    now = _now()
    if result.raw_request:
        await gateway_requestsdb.insert_one({
            "withdrawal_id": withdrawal_id,
            "provider":      getattr(result, "provider_id", "oxapay"),
            "payload":       result.raw_request,
            "created_at":    now,
        })
    if result.raw_response:
        await gateway_responsesdb.insert_one({
            "withdrawal_id": withdrawal_id,
            "provider":      getattr(result, "provider_id", "oxapay"),
            "payload":       result.raw_response,
            "success":       result.success,
            "track_id":      result.track_id,
            "created_at":    now,
        })


# ── Event log ─────────────────────────────────────────────────────────────────

async def log_withdrawal_event(
    withdrawal_id: str,
    event: str,
    data: dict,
) -> None:
    """Append an event to the withdrawal audit log."""
    await withdrawal_logsdb.insert_one({
        "log_id":        _gen_log_id(),
        "withdrawal_id": withdrawal_id,
        "event":         event,
        "data":          data,
        "created_at":    _now(),
    })


async def get_withdrawal_logs(withdrawal_id: str) -> List[dict]:
    cursor = withdrawal_logsdb.find(
        {"withdrawal_id": withdrawal_id},
        {"_id": 0},
    ).sort("created_at", ASCENDING)
    return await cursor.to_list(length=500)


# ── Background worker helpers ─────────────────────────────────────────────────

async def get_in_flight_withdrawals(limit: int = 50) -> List[dict]:
    """All non-final withdrawals (pending, processing, waiting_confirmation)."""
    cursor = withdrawalsdb.find(
        {"status": {"$in": list(WithdrawalStatus.IN_FLIGHT)}},
        sort=[("created_at", ASCENDING)],
    ).limit(limit)
    return await cursor.to_list(length=limit)


async def get_pending_unsubmitted(limit: int = 20) -> List[dict]:
    """Withdrawals in 'pending' with no gateway_track_id (auto mode, not yet submitted)."""
    cursor = withdrawalsdb.find(
        {
            "status":           WithdrawalStatus.PENDING,
            "mode":             "auto",
            "gateway_track_id": None,
        },
        sort=[("created_at", ASCENDING)],
    ).limit(limit)
    return await cursor.to_list(length=limit)


async def get_expirable_withdrawals(before: datetime, limit: int = 100) -> List[dict]:
    """In-flight withdrawals created before a cutoff datetime."""
    cursor = withdrawalsdb.find(
        {
            "status": {"$in": list(WithdrawalStatus.IN_FLIGHT)},
            "created_at": {"$lt": before},
        },
        sort=[("created_at", ASCENDING)],
    ).limit(limit)
    return await cursor.to_list(length=limit)


# ── Admin helpers ─────────────────────────────────────────────────────────────

async def get_all_withdrawals(
    status: Optional[str] = None,
    user_id: Optional[int] = None,
    search: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[int, List[dict]]:
    """Paginated listing for the admin panel."""
    query: dict = {}
    if status:
        query["status"] = status
    if user_id:
        query["user_id"] = user_id
    if search:
        search = search.strip()
        try:
            query["$or"] = [
                {"user_id":        int(search)},
                {"withdrawal_id":  {"$regex": search, "$options": "i"}},
                {"wallet_address": {"$regex": search, "$options": "i"}},
                {"gateway_track_id": {"$regex": search, "$options": "i"}},
            ]
        except ValueError:
            query["$or"] = [
                {"withdrawal_id":    {"$regex": search, "$options": "i"}},
                {"wallet_address":   {"$regex": search, "$options": "i"}},
                {"gateway_track_id": {"$regex": search, "$options": "i"}},
            ]

    total = await withdrawalsdb.count_documents(query)
    skip  = (page - 1) * limit
    cursor = withdrawalsdb.find(query, {"_id": 0}).sort("created_at", DESCENDING).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return total, docs


# ── Backward-compat shims (used by existing walletdb imports) ─────────────────

async def get_withdrawal(withdrawal_id: str) -> Optional[dict]:
    """Alias for get_withdrawal_record — preserves existing import paths."""
    return await get_withdrawal_record(withdrawal_id)


async def get_user_withdrawals_paginated(
    user_id: int, limit: int = 20, page: int = 1
) -> List[dict]:
    return await get_user_withdrawals(user_id, limit=limit, page=page)


# ── Logger (module-level, used in finalize_withdrawal) ───────────────────────
from server.logging import LOGGER as _LOGGER
_log = _LOGGER(__name__)
