import secrets
import logging
from datetime import timedelta
from typing import Optional, List

from server.core.mongo import collection
from server.utils.common import utcnow as _now

depositsdb     = collection("deposits")
withdrawalsdb  = collection("withdrawals")
transactionsdb = collection("transactions")
_log = logging.getLogger(__name__)


def _generate_txn_id() -> str:
    return f"TXN-{secrets.token_hex(10).upper()}"


def _generate_dep_id() -> str:
    return f"DEP-{secrets.token_hex(8).upper()}"


def _generate_wit_id() -> str:
    return f"WIT-{secrets.token_hex(8).upper()}"


# API-facing deposit statuses
DEPOSIT_STATUSES = ["pending", "confirming", "completed", "expired", "failed"]

# Valid transaction types — any other value is a bug caught at write time
VALID_TXN_TYPES = frozenset([
    "deposit", "withdrawal", "refund", "purchase", "sale", "fee", "bonus",
    "adjustment", "referral", "reversal", "sale_reversal",
])


# ─── Deposits ────────────────────────────────────────────────────────────────

async def create_deposit_v2(
    user_id: int,
    amount: float,
    method: str,
    *,
    deposit_id: Optional[str] = None,   # pass the ID you already gave to the payment provider
    network: Optional[str] = None,
    currency: Optional[str] = None,
    address: Optional[str] = None,
    payment_url: Optional[str] = None,
    qr_url: Optional[str] = None,
    memo: Optional[str] = None,
    phone: Optional[str] = None,
    instructions: Optional[str] = None,
    expires_at=None,
    extra: Optional[dict] = None,
) -> str:
    """
    Create a deposit record using provider-generated payment details.

    Always pass `deposit_id` = the same ID you gave to the payment provider
    (e.g. OxaPay order_id), so the webhook can find the record by that ID.
    """
    dep_id = deposit_id or _generate_dep_id()
    doc = {
        "deposit_id": dep_id,
        "user_id": user_id,
        "amount": round(amount, 4),
        "method": method,
        "network": network,
        "currency": currency,
        "address": address,
        "payment_url": payment_url,
        "qr_url": qr_url,
        "memo": memo,
        "phone": phone,
        "instructions": instructions,
        "extra": extra or {},
        "status": "pending",
        "expires_at": expires_at,
        "confirmed_by": None,
        "note": None,
        "tx_hash": None,
        "created_at": _now(),
        "confirmed_at": None,
    }
    await depositsdb.insert_one(doc)
    return dep_id


async def expire_overdue_deposits() -> int:
    """Mark all pending deposits whose expires_at has passed as 'expired'."""
    now = _now()
    result = await depositsdb.update_many(
        {"status": "pending", "expires_at": {"$lt": now, "$ne": None}},
        {"$set": {"status": "expired"}},
    )
    return result.modified_count


async def confirm_deposit(deposit_id: str, confirmed_by: int = None) -> Optional[dict]:
    """
    Atomically claim a payable deposit and credit the user's balance exactly once.

    Three-state idempotency — balance_credited values:
      "crediting"  — Phase 2 is owned by the claimer; stale claims (>120 s) are
                     re-claimable to recover from process crashes mid-Phase-2.
      True         — Fully confirmed; all subsequent calls are no-ops.

    Invariant: a process enters Phase 2 ONLY if it holds the "crediting" claim
    (atomically acquired via find_one_and_update).  Phase 1 never writes False —
    it goes straight to "crediting", so there is no window where two concurrent
    processes can both see an unclaimed completed record and both proceed to credit.

    Returns the deposit doc (pre-update) on success, or None if the deposit is
    already fully confirmed, actively claimed by another process, or not found.
    """
    now = _now()

    before = await depositsdb.find_one({"deposit_id": deposit_id})
    _log.info(
        "confirm_deposit before: deposit_id=%s status=%s balance_credited=%s",
        deposit_id,
        (before or {}).get("status"),
        (before or {}).get("balance_credited"),
    )

    # Phase 1: atomically claim from either unpaid state.  OxaPay emits
    # Confirming before Paid, so a confirming deposit must remain claimable.
    dep = await depositsdb.find_one_and_update(
        {
            "deposit_id": deposit_id,
            "status": {"$in": ["pending", "confirming"]},
            "balance_credited": {"$ne": True},
        },
        {"$set": {
            "status":               "completed",
            "confirmed_by":         confirmed_by,
            "confirmed_at":         now,
            "balance_credited":     "crediting",
            "crediting_started_at": now,
        }},
        # return_document=False (default) → returns the doc BEFORE the update
    )

    if dep is None:
        # Not pending — check for a stale "crediting" claim left by a crashed process.
        # A live claimer's timestamp will be recent; only re-claim if it's stale.
        stale_before = now - timedelta(seconds=120)
        dep = await depositsdb.find_one_and_update(
            {
                "deposit_id": deposit_id,
                "status": "completed",
                "balance_credited": "crediting",
                "crediting_started_at": {"$lt": stale_before},
            },
            {"$set": {"crediting_started_at": now}},
        )
        if dep is None:
            # Fully confirmed (True), actively claimed (recent timestamp), or not found.
            current = await depositsdb.find_one({"deposit_id": deposit_id})
            _log.info(
                "confirm_deposit no-op: deposit_id=%s status=%s balance_credited=%s",
                deposit_id,
                (current or {}).get("status"),
                (current or {}).get("balance_credited"),
            )
            return None

    # Phase 2: credit balance and log transaction.
    # Only reachable by the process that holds the "crediting" claim.
    from server.utils.database.userdb import record_deposit as _record_deposit
    await _record_deposit(
        dep["user_id"],
        dep["amount"],
        deposit_id=deposit_id,
    )
    await log_transaction_once(
        user_id=dep["user_id"],
        txn_type="deposit",
        amount=dep["amount"],
        ref_id=deposit_id,
        note=f"Deposit via {dep['method']}",
    )

    # Mark Phase 2 complete — subsequent calls return None (no-op).
    result = await depositsdb.update_one(
        {"deposit_id": deposit_id, "balance_credited": "crediting"},
        {"$set": {"balance_credited": True}, "$unset": {"crediting_started_at": ""}},
    )
    after = await depositsdb.find_one({"deposit_id": deposit_id})
    _log.info(
        "confirm_deposit after: deposit_id=%s matched=%s modified=%s status=%s balance_credited=%s",
        deposit_id,
        result.matched_count,
        result.modified_count,
        (after or {}).get("status"),
        (after or {}).get("balance_credited"),
    )
    if result.matched_count != 1 or (after or {}).get("balance_credited") is not True:
        raise RuntimeError(f"Deposit {deposit_id} credit finalization did not persist")
    return dep


async def reject_deposit(deposit_id: str, note: str = "") -> Optional[dict]:
    """
    Reject a pending deposit. Returns the original doc (for notification),
    or None if the deposit was not found / not in pending state.
    """
    return await depositsdb.find_one_and_update(
        {"deposit_id": deposit_id, "status": "pending"},
        {"$set": {"status": "rejected", "note": note}},
    )


async def get_deposit(deposit_id: str) -> Optional[dict]:
    return await depositsdb.find_one({"deposit_id": deposit_id})


async def get_user_deposits(user_id: int, limit: int = 20) -> List[dict]:
    cursor = depositsdb.find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_user_deposits_paginated(user_id: int, page: int = 1, limit: int = 20) -> List[dict]:
    skip = (page - 1) * limit
    cursor = depositsdb.find({"user_id": user_id}).sort("created_at", -1).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)


async def count_user_deposits(user_id: int) -> int:
    return await depositsdb.count_documents({"user_id": user_id})


async def get_pending_deposits() -> List[dict]:
    cursor = depositsdb.find({"status": "pending"}).sort("created_at", 1)
    return await cursor.to_list(length=1000)


# ─── Withdrawals ─────────────────────────────────────────────────────────────

async def create_withdrawal(
    user_id: int,
    amount: float,
    method: str,
    wallet_address: str,
) -> str:
    """
    Create a withdrawal record. Balance must be pre-deducted by the caller.

    If the transaction log write fails after the withdrawal is inserted, the
    record is deleted as a compensating action so the caller can safely refund.
    """
    wit_id = _generate_wit_id()
    doc = {
        "withdrawal_id": wit_id,
        "user_id": user_id,
        "amount": round(amount, 4),
        "method": method,
        "wallet_address": wallet_address,
        "tx_hash": None,
        "status": "pending",
        "processed_by": None,
        "note": None,
        "created_at": _now(),
        "processed_at": None,
    }
    await withdrawalsdb.insert_one(doc)
    try:
        await log_transaction(
            user_id=user_id,
            txn_type="withdrawal",
            amount=-round(amount, 4),
            ref_id=wit_id,
            note=f"Withdrawal request via {method} — pending approval",
        )
    except Exception as txn_exc:
        # Compensating delete — put the withdrawal record back so the caller
        # can safely refund the balance without leaving an orphaned record.
        # If the delete itself also fails, log it explicitly so an admin can
        # manually clean up — this is far better than silent data corruption.
        try:
            await withdrawalsdb.delete_one({"withdrawal_id": wit_id})
        except Exception as del_exc:
            import logging as _logging
            _logging.getLogger(__name__).error(
                "CRITICAL: withdrawal %s log failed AND compensating delete failed. "
                "Manual cleanup required. log_error=%s delete_error=%s",
                wit_id, txn_exc, del_exc,
            )
        raise
    return wit_id


async def claim_withdrawal_for_processing(
    withdrawal_id: str,
    processed_by: Optional[int] = None,
) -> Optional[dict]:
    """
    Atomically move a withdrawal from pending → processing.

    Returns the original doc (before state change) or None if not pending.
    This is the ONLY safe entry point for approval — ensures exactly-once payout.
    """
    return await withdrawalsdb.find_one_and_update(
        {"withdrawal_id": withdrawal_id, "status": "pending"},
        {"$set": {"status": "processing", "processed_by": processed_by}},
        # return_document=False (default) → original doc before update
    )


async def complete_withdrawal(withdrawal_id: str, tx_hash: str) -> None:
    """Mark a processing withdrawal as completed. Called after a successful payout."""
    await withdrawalsdb.update_one(
        {"withdrawal_id": withdrawal_id, "status": "processing"},
        {"$set": {"status": "completed", "tx_hash": tx_hash, "processed_at": _now()}},
    )


async def revert_withdrawal_to_pending(withdrawal_id: str) -> None:
    """
    Revert a processing withdrawal back to pending.
    Called when a payout attempt fails — allows the admin to retry.
    """
    await withdrawalsdb.update_one(
        {"withdrawal_id": withdrawal_id, "status": "processing"},
        {"$set": {"status": "pending", "processed_by": None}},
    )


async def reject_withdrawal(withdrawal_id: str, note: str = "") -> Optional[dict]:
    """
    Atomically reject a pending withdrawal.

    Uses find_one_and_update so concurrent reject+approve cannot both succeed.
    Returns the original doc (for balance refund) or None if not pending.
    """
    return await withdrawalsdb.find_one_and_update(
        {"withdrawal_id": withdrawal_id, "status": "pending"},
        {"$set": {"status": "rejected", "note": note}},
        # return_document=False → returns original doc with user_id/amount for refund
    )


# Keep for backwards compat (used in __init__.py export; logic now split above)
async def approve_withdrawal(
    withdrawal_id: str,
    tx_hash: Optional[str] = None,
    processed_by: Optional[int] = None,
) -> Optional[dict]:
    """
    Legacy single-step approve (safe for manual tx_hash path with no concurrent risk).
    Prefer claim_withdrawal_for_processing + complete_withdrawal for auto-payout flows.
    """
    wit = await claim_withdrawal_for_processing(withdrawal_id, processed_by)
    if not wit:
        return None
    await complete_withdrawal(withdrawal_id, tx_hash or "manual")
    return wit


async def get_withdrawal(withdrawal_id: str) -> Optional[dict]:
    # Delegate to the comprehensive withdrawaldb if the record was created by the new system
    from server.utils.database.withdrawaldb import get_withdrawal_record
    return await get_withdrawal_record(withdrawal_id)


async def get_user_withdrawals(user_id: int, limit: int = 20, page: int = 1) -> List[dict]:
    skip = (page - 1) * limit
    cursor = withdrawalsdb.find({"user_id": user_id}).sort("created_at", -1).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)


async def get_pending_withdrawals() -> List[dict]:
    cursor = withdrawalsdb.find({"status": "pending"}).sort("created_at", 1)
    return await cursor.to_list(length=1000)


# ─── Transaction Ledger ──────────────────────────────────────────────────────

async def log_transaction(
    user_id: int,
    txn_type: str,
    amount: float,
    ref_id: str = None,
    note: str = "",
) -> str:
    """
    Append an entry to the transaction ledger.

    `txn_type` must be one of VALID_TXN_TYPES — any other value raises ValueError
    so typos are caught at write time rather than silently corrupting the ledger.
    """
    if txn_type not in VALID_TXN_TYPES:
        raise ValueError(
            f"Invalid txn_type '{txn_type}'. Must be one of: {sorted(VALID_TXN_TYPES)}"
        )
    txn_id = _generate_txn_id()
    doc = {
        "txn_id": txn_id,
        "user_id": user_id,
        "type": txn_type,
        "amount": round(amount, 4),
        "ref_id": ref_id,
        "note": note,
        "created_at": _now(),
    }
    await transactionsdb.insert_one(doc)
    return txn_id


async def log_transaction_once(
    user_id: int,
    txn_type: str,
    amount: float,
    ref_id: str,
    note: str = "",
) -> str:
    """Append a transaction only when this reference has not been logged.

    Provider callbacks and stale-claim recovery can replay the same deposit.
    The lookup makes normal retries idempotent while preserving the existing
    append-only behavior for transactions without a reference ID.
    """
    if not ref_id:
        return await log_transaction(user_id, txn_type, amount, ref_id, note)
    if txn_type not in VALID_TXN_TYPES:
        raise ValueError(
            f"Invalid txn_type '{txn_type}'. Must be one of: {sorted(VALID_TXN_TYPES)}"
        )
    existing = await transactionsdb.find_one(
        {"type": txn_type, "ref_id": ref_id},
        {"txn_id": 1},
    )
    if existing and existing.get("txn_id"):
        return existing["txn_id"]
    return await log_transaction(user_id, txn_type, amount, ref_id, note)


async def get_user_transactions(
    user_id: int,
    limit: int = 30,
    page: int = 1,
    txn_type: Optional[str] = None,
) -> List[dict]:
    query: dict = {"user_id": user_id}
    if txn_type:
        query["type"] = txn_type
    skip = (page - 1) * limit
    cursor = transactionsdb.find(query).sort("created_at", -1).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)


async def get_all_transactions(txn_type: str = None, limit: int = 50) -> List[dict]:
    query = {}
    if txn_type:
        if txn_type not in VALID_TXN_TYPES:
            raise ValueError(f"Invalid txn_type '{txn_type}'.")
        query["type"] = txn_type
    cursor = transactionsdb.find(query).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)
