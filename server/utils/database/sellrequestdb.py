import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from server.core.mongo import collection
from server.utils.common import utcnow as _now

sellrequestsdb = collection("sell_requests")


def _generate_request_id() -> str:
    return f"SEL-{secrets.token_hex(8).upper()}"


SELL_REQUEST_STATUS = ["pending", "approved", "rejected", "paid"]

# Anti-abuse limits
SELL_RATE_LIMIT_HOURS = 24     # rolling window
SELL_RATE_LIMIT_MAX   = 5      # max submissions per window
SELL_MAX_PENDING      = 3      # max simultaneous pending requests per user

# Lifecycle statuses (stored alongside the main "status" field):
#   "pending"              — awaiting admin review (normal path)
#   "pending_termination"  — termination blocked ("session too new"), retry in 48h
#   "completed"            — all checks passed, session in stock
#   "payment_released"     — pending_balance converted to earned balance
LIFECYCLE_STATUSES = [
    "pending", "pending_termination", "completed", "payment_released",
]


# ── Schema ───────────────────────────────────────────────────────────────────
# {
#   "request_id":         "SEL-XXXXXXXX",
#   "user_id":            int,
#   "code":               "IN",
#   "country_name":       "India 🇮🇳",
#   "phone":              "+91xxxxxxxxxx",
#   "offer_price":        0.35,
#   "final_price":        None,
#   "pending_amount":     0.35,
#   "session_msg_id":     None,
#   "session_chat_id":    None,
#   "validation_passed":  [],
#   "spam_status":        "clean",
#   "tg_user_id":         None,
#   "username":           None,
#   "first_name":         None,
#   "has_2fa":            True,
#   "tfa_updated":        True,
#   "tfa_password_enc":   "",
#   "api_id_used":        None,
#   "proxy_used":         "none",
#   "terminated_others":  False,
#   "status":             "pending",  # pending | approved | rejected | paid
#   "admin_note":         "",
#   "submitted_at":       datetime,
#   "reviewed_at":        None,
#   # ── Extended lifecycle (new sell-account / sell-session flows) ────────
#   "lifecycle_status":   "pending",             # see LIFECYCLE_STATUSES
#   "retry_at":           None,                  # datetime: when to retry termination
#   "retry_count":        0,                     # termination retry attempts made
#   "session_bytes_enc":  None,                  # encrypted bytes (for retry path)
#   "payment_release_at": None,                  # datetime: when to release pending→earned
#   "sell_type":          "account",             # "account" | "session"
# }


async def create_sell_request(
    user_id: int,
    code: str,
    country_name: str,
    phone: str,
    offer_price: float,
    *,
    session_msg_id: Optional[int] = None,
    session_chat_id: Optional[int] = None,
    pending_amount: float = 0.0,
    validation_passed: Optional[list] = None,
    spam_status: str = "unknown",
    tg_user_id: Optional[int] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    has_2fa: bool = False,
    tfa_updated: bool = False,
    tfa_password_enc: str = "",
    api_id_used: Optional[int] = None,
    proxy_used: str = "none",
    terminated_others: bool = False,
    # Extended lifecycle fields
    lifecycle_status: str = "pending",
    retry_at: Optional[datetime] = None,
    session_bytes_enc: Optional[bytes] = None,
    sell_type: str = "account",
) -> str:
    request_id = _generate_request_id()
    now = _now()
    # Automatic 48h payment release timer: starts when lifecycle is normal "pending"
    # Criteria already fulfilled during the selling flow -> classic Payment Hold Time.
    try:
        from server.utils.database.configdb import get_setting as _get_setting
        _hold_hours = int(await _get_setting("payment_hold_hours") or 48)
    except Exception:
        _hold_hours = 48
    payment_release_at = (
        (now + timedelta(hours=_hold_hours)) if lifecycle_status == "pending" else None
    )
    doc = {
        "request_id":         request_id,
        "user_id":            user_id,
        "code":               code.upper(),
        "country_name":       country_name,
        "phone":              phone,
        "offer_price":        round(offer_price, 4),
        "final_price":        None,
        "pending_amount":     round(pending_amount or offer_price, 4),
        "session_msg_id":     session_msg_id,
        "session_chat_id":    session_chat_id,
        "validation_passed":  validation_passed or [],
        "spam_status":        spam_status,
        "tg_user_id":         tg_user_id,
        "username":           username,
        "first_name":         first_name,
        "has_2fa":            has_2fa,
        "tfa_updated":        tfa_updated,
        "tfa_password_enc":   tfa_password_enc,
        "api_id_used":        api_id_used,
        "proxy_used":         proxy_used,
        "terminated_others":  terminated_others,
        "status":             "pending",
        "admin_note":         "",
        "submitted_at":       now,
        "reviewed_at":        None,
        # Extended lifecycle
        "lifecycle_status":   lifecycle_status,
        "retry_at":           retry_at,
        "retry_count":        0,
        "session_bytes_enc":  session_bytes_enc,
        "payment_release_at": payment_release_at,
        "sell_type":          sell_type,
    }
    await sellrequestsdb.insert_one(doc)
    return request_id


async def check_phone_has_active_sell(phone: str) -> bool:
    """Return True if this phone already has a pending sell request."""
    doc = await sellrequestsdb.find_one(
        {"phone": phone, "status": "pending"},
        {"_id": 1},
    )
    return doc is not None


async def check_phone_in_stock(phone: str) -> bool:
    """Return True if this phone already exists in our session stock."""
    from server.core.mongo import collection as _col
    doc = await _col("session_accounts").find_one({"phone": phone}, {"_id": 1})
    return doc is not None


async def get_user_recent_sell_count(user_id: int, hours: int = SELL_RATE_LIMIT_HOURS) -> int:
    """Count sell requests submitted by this user within the last `hours` hours."""
    since = _now() - timedelta(hours=hours)
    return await sellrequestsdb.count_documents({
        "user_id": user_id,
        "submitted_at": {"$gte": since},
    })


async def get_user_pending_sell_count(user_id: int) -> int:
    """Count pending (unreviewed) sell requests for this user."""
    return await sellrequestsdb.count_documents({"user_id": user_id, "status": "pending"})


async def get_sell_request(request_id: str) -> Optional[dict]:
    r = await sellrequestsdb.find_one({"request_id": request_id})
    if r:
        r.pop("_id", None)
    return r


async def get_user_sell_requests(
    user_id: int,
    limit: int = 20,
    *,
    include_session_payload: bool = True,
) -> List[dict]:
    """Return a user's seller requests, optionally excluding retry-only binary data."""
    projection = None if include_session_payload else {"_id": 0, "session_bytes_enc": 0}
    cursor = sellrequestsdb.find({"user_id": user_id}, projection).sort("submitted_at", -1).limit(limit)
    results = []
    async for r in cursor:
        r.pop("_id", None)
        results.append(r)
    return results


async def get_pending_sell_requests(limit: int = 50) -> List[dict]:
    cursor = sellrequestsdb.find({"status": "pending"}).sort("submitted_at", 1).limit(limit)
    results = []
    async for r in cursor:
        r.pop("_id", None)
        results.append(r)
    return results


async def get_all_sell_requests(status: Optional[str] = None, limit: int = 100) -> List[dict]:
    query = {"status": status} if status else {}
    # Exclude heavy binary field — only the retry worker needs session_bytes_enc
    projection = {"_id": 0, "session_bytes_enc": 0}
    cursor = sellrequestsdb.find(query, projection).sort("submitted_at", -1).limit(limit)
    results = []
    async for r in cursor:
        results.append(r)
    return results


async def approve_sell_request(
    request_id: str,
    final_price: float,
    admin_note: str = "",
) -> Optional[dict]:
    """
    Atomically approve a pending sell request.
    Returns the original doc (with user_id, pending_amount) for balance movement,
    or None if the request was not found in pending state.
    """
    return await sellrequestsdb.find_one_and_update(
        {"request_id": request_id, "status": "pending"},
        {"$set": {
            "status":      "paid",
            "final_price": round(final_price, 4),
            "admin_note":  admin_note,
            "reviewed_at": _now(),
        }},
        # return_document=False (default) → returns pre-update doc for user_id/pending_amount
    )


async def reject_sell_request(request_id: str, admin_note: str = "") -> Optional[dict]:
    """
    Atomically reject a pending sell request.
    Returns the original doc (with user_id, pending_amount) for balance
    reversal, or None if the request was not found in pending state.
    """
    return await sellrequestsdb.find_one_and_update(
        {"request_id": request_id, "status": "pending"},
        {"$set": {
            "status": "rejected",
            "admin_note": admin_note,
            "reviewed_at": _now(),
        }},
        # return_document=False (default) → returns pre-update doc for user_id/pending_amount
    )


async def count_pending_sell_requests() -> int:
    return await sellrequestsdb.count_documents({"status": "pending"})


# ── Extended lifecycle queries (new sell-account / sell-session flows) ─────────

async def update_sell_request_status(
    request_id: str,
    lifecycle_status: str,
    extra: Optional[dict] = None,
) -> None:
    """Update the lifecycle_status and any extra fields on a sell request."""
    update = {"$set": {"lifecycle_status": lifecycle_status, **(extra or {})}}
    await sellrequestsdb.update_one({"request_id": request_id}, update)


async def get_pending_termination_due(limit: int = 50) -> List[dict]:
    """
    Return PENDING_TERMINATION sell requests whose retry_at timestamp has passed.
    These are ready for the background worker to retry session termination.
    """
    now = _now()
    cursor = sellrequestsdb.find(
        {
            "lifecycle_status": "pending_termination",
            "status": "pending",
            "retry_at": {"$lte": now},
        }
    ).sort("retry_at", 1).limit(limit)
    results = []
    async for r in cursor:
        r.pop("_id", None)
        results.append(r)
    return results


async def increment_retry_count(request_id: str, next_retry_at) -> None:
    """Bump retry_count and set next retry timestamp after a failed termination attempt."""
    await sellrequestsdb.update_one(
        {"request_id": request_id},
        {"$inc": {"retry_count": 1}, "$set": {"retry_at": next_retry_at}},
    )


async def get_payment_release_due(limit: int = 100) -> List[dict]:
    """
    Return sell requests whose payment hold has elapsed and whose
    pending_balance should be moved to earned balance.

    Matches two flows:
      • sell_account (live-phone): lifecycle_status progresses to "completed"
        after termination; payment_release_at is set by _mark_termination_complete.
      • sell_session (file upload): lifecycle_status stays "pending" but
        payment_release_at = now+48h is set at submission time.

    We intentionally exclude lifecycle_status from the filter so both flows
    are caught.  Requests with payment_release_at=None (no timer started yet,
    e.g. still in pending_termination) are excluded by the $ne: None guard.
    """
    now = _now()
    cursor = sellrequestsdb.find(
        {
            "status":             "pending",
            "payment_release_at": {"$lte": now, "$ne": None},
            # Exclude requests still waiting for termination (timer not started yet)
            "lifecycle_status":   {"$nin": ["pending_termination"]},
        }
    ).sort("payment_release_at", 1).limit(limit)
    results = []
    async for r in cursor:
        r.pop("_id", None)
        results.append(r)
    return results


async def mark_payment_released(request_id: str) -> None:
    """Record that pending_balance has been converted to earned balance."""
    await sellrequestsdb.update_one(
        {"request_id": request_id},
        {"$set": {
            "lifecycle_status": "payment_released",
            "status": "paid",
            "reviewed_at": _now(),
            "admin_note": "Auto-released after 48h hold",
        }},
    )


async def get_seller_profile(user_id: int) -> dict:
    """
    Return a summary of all sell activity for a given user_id, used by the
    admin seller profile/search page.

    Returns:
        {
          "user_id": int,
          "total": int,
          "pending": int,
          "paid": int,
          "rejected": int,
          "total_earned": float,
          "total_pending": float,
          "spam_breakdown": {"clean": N, "temporary_spam": N, "permanent_spam": N, "unknown": N},
          "country_breakdown": {"IN": N, ...},
          "sell_type_breakdown": {"account": N, "session": N},
          "recent": [<last 30 requests, no session_bytes_enc>],
        }
    """
    projection = {"_id": 0, "session_bytes_enc": 0}
    cursor = sellrequestsdb.find({"user_id": user_id}, projection).sort("submitted_at", -1)
    all_docs: List[dict] = []
    async for doc in cursor:
        all_docs.append(doc)

    pending  = sum(1 for d in all_docs if d.get("status") == "pending")
    paid     = sum(1 for d in all_docs if d.get("status") in ("paid",))
    rejected = sum(1 for d in all_docs if d.get("status") == "rejected")

    total_earned  = sum(d.get("final_price") or 0.0  for d in all_docs if d.get("status") == "paid")
    total_pending = sum(d.get("pending_amount") or 0.0 for d in all_docs if d.get("status") == "pending")

    spam_breakdown: dict[str, int] = {}
    country_breakdown: dict[str, int] = {}
    sell_type_breakdown: dict[str, int] = {}
    for d in all_docs:
        s = d.get("spam_status") or "unknown"
        spam_breakdown[s] = spam_breakdown.get(s, 0) + 1
        c = d.get("code") or "??"
        country_breakdown[c] = country_breakdown.get(c, 0) + 1
        t = d.get("sell_type") or "account"
        sell_type_breakdown[t] = sell_type_breakdown.get(t, 0) + 1

    # Serialize datetimes for JSON transport
    recent = []
    for d in all_docs[:30]:
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        recent.append(d)

    return {
        "user_id":            user_id,
        "total":              len(all_docs),
        "pending":            pending,
        "paid":               paid,
        "rejected":           rejected,
        "total_earned":       round(total_earned, 4),
        "total_pending":      round(total_pending, 4),
        "spam_breakdown":     spam_breakdown,
        "country_breakdown":  country_breakdown,
        "sell_type_breakdown": sell_type_breakdown,
        "recent":             recent,
    }
