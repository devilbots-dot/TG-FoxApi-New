"""
users_sell_stock collection — holding area for sessions bought FROM users.

When admin approves a sell request the account lands here first.
From here the admin can:
  • Check session status (live Telethon ping)
  • Send to Sell Inventory (transfer to session_accounts for buyers)

Schema
------
{
  "stock_id":          "USS-XXXXXXXXXXXXXXXX",
  "sell_id":           "REQ-XXXXXXXX",         # sell_requests.request_id
  "user_id":           123456789,               # seller's Telegram ID
  "phone":             "+911234567890",
  "country_code":      "IN",
  "country_name":      "India 🇮🇳",
  "session_msg_id":    12345,
  "session_chat_id":   -1001234567890,
  "has_2fa":           False,
  "tfa_password_enc":  "",
  "tg_user_id":        None,
  "username":          None,
  "first_name":        None,
  "proxy_used":        "none",
  "api_id_used":       None,
  "sell_type":         "account",
  "payment_status":    "pending",               # changes to paid after final approval
  "session_status":    "unknown",               # updated by Check Status
  "transferred":       False,                   # True after Send to Sell Inventory
  "transferred_at":    None,
  "received_at":       datetime,
}
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from server.core.mongo import collection
from server.utils.common import utcnow as _now

usersellstockdb = collection("users_sell_stock")

VALID_SESSION_STATUSES = (
    "unknown", "clean", "temporary_spam", "permanent_spam",
    "frozen", "restricted", "dead",
)
VALID_PAYMENT_STATUSES = ("paid", "pending", "rejected", "unpaid")


def _gen_id() -> str:
    return f"USS-{secrets.token_hex(8).upper()}"


# ── Write operations ──────────────────────────────────────────────────────────

async def add_user_sell_stock(
    *,
    sell_id: str,
    user_id: int,
    phone: str,
    country_code: str,
    country_name: str,
    session_msg_id: Optional[int] = None,
    session_chat_id: Optional[int] = None,
    has_2fa: bool = False,
    tfa_password_enc: str = "",
    tg_user_id: Optional[int] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    proxy_used: str = "none",
    api_id_used: Optional[int] = None,
    sell_type: str = "account",
    spam_status: str = "unknown",
    payment_status: str = "paid",
) -> str:
    stock_id = _gen_id()
    doc = {
        "stock_id":        stock_id,
        "sell_id":         sell_id,
        "user_id":         user_id,
        "phone":           phone,
        "country_code":    country_code.upper(),
        "country_name":    country_name,
        "session_msg_id":  session_msg_id,
        "session_chat_id": session_chat_id,
        "has_2fa":         has_2fa,
        "tfa_password_enc":tfa_password_enc,
        "tg_user_id":      tg_user_id,
        "username":        username,
        "first_name":      first_name,
        "proxy_used":      proxy_used,
        "api_id_used":     api_id_used,
        "sell_type":       sell_type,
        "spam_status":     spam_status,
        "payment_status":  payment_status,
        "session_status":  "unknown",
        "transferred":     False,
        "transferred_at":  None,
        "received_at":     _now(),
    }
    await usersellstockdb.insert_one(doc)
    return stock_id


async def upsert_by_sell_id(
    *,
    sell_id: str,
    user_id: int,
    phone: str,
    country_code: str,
    country_name: str,
    session_msg_id: Optional[int] = None,
    session_chat_id: Optional[int] = None,
    has_2fa: bool = False,
    tfa_password_enc: str = "",
    tg_user_id: Optional[int] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    proxy_used: str = "none",
    api_id_used: Optional[int] = None,
    sell_type: str = "account",
    spam_status: str = "unknown",
    payment_status: str = "pending",
) -> str:
    """
    Atomically insert a new users_sell_stock entry or update an existing one
    matched by sell_id.  Returns the stock_id (existing or newly created).

    Uses a single find_one_and_update with upsert=True to eliminate the
    previous find_one → insert/update race condition.

    Session reference fields (session_msg_id / session_chat_id) are ONLY
    written on $set when the incoming value is not None — this prevents a
    later finalize_sell_approval call (with session_msg_id=None) from
    overwriting a valid reference that the termination worker already set.
    On a fresh insert, both fields are always initialised (even to None) via
    $setOnInsert so every row has a predictable schema.
    """
    stock_id_new = _gen_id()
    now = _now()

    # Fields applied on every call (update path uses these; insert also picks
    # them up via $set which fires on both insert and update).
    set_fields: dict = {
        "payment_status":   payment_status,
        "spam_status":      spam_status,
        "has_2fa":          has_2fa,
        "tfa_password_enc": tfa_password_enc,
    }
    # Session references: update only when caller supplies a non-None value so
    # we never clobber a valid reference that was set by an earlier worker run.
    if session_msg_id is not None:
        set_fields["session_msg_id"]  = session_msg_id
    if session_chat_id is not None:
        set_fields["session_chat_id"] = session_chat_id

    # Fields only written when the document is inserted for the first time.
    # Keys must NOT overlap with set_fields (MongoDB rejects conflicting paths).
    insert_only: dict = {
        "stock_id":       stock_id_new,
        "sell_id":        sell_id,
        "user_id":        user_id,
        "phone":          phone,
        "country_code":   country_code.upper(),
        "country_name":   country_name,
        "tg_user_id":     tg_user_id,
        "username":       username,
        "first_name":     first_name,
        "proxy_used":     proxy_used,
        "api_id_used":    api_id_used,
        "sell_type":      sell_type,
        "session_status": "unknown",
        "transferred":    False,
        "transferred_at": None,
        "received_at":    now,
    }
    # Initialise session refs in $setOnInsert only when NOT already in $set
    # (MongoDB errors if the same path appears in both operators).
    if "session_msg_id" not in set_fields:
        insert_only["session_msg_id"]  = session_msg_id
    if "session_chat_id" not in set_fields:
        insert_only["session_chat_id"] = session_chat_id

    pre_doc = await usersellstockdb.find_one_and_update(
        {"sell_id": sell_id},
        {"$set": set_fields, "$setOnInsert": insert_only},
        upsert=True,
        # Default return_document=False → returns pre-update doc, or None if inserted
    )

    if pre_doc is None:
        # Document was inserted — return the freshly generated stock_id
        return stock_id_new
    # Document was updated — return the existing stock_id
    return pre_doc.get("stock_id", stock_id_new)


async def update_payment_status_by_sell_id(sell_id: str, status: str) -> bool:
    """Update payment_status for an entry identified by its sell request ID."""
    r = await usersellstockdb.update_one(
        {"sell_id": sell_id},
        {"$set": {"payment_status": status}},
    )
    return r.modified_count > 0


async def update_session_status(stock_id: str, status: str) -> bool:
    if status not in VALID_SESSION_STATUSES:
        return False
    r = await usersellstockdb.update_one(
        {"stock_id": stock_id},
        {"$set": {"session_status": status, "status_checked_at": _now()}},
    )
    return r.modified_count > 0


async def bulk_update_session_status(stock_ids: list[str], status: str) -> int:
    if status not in VALID_SESSION_STATUSES:
        return 0
    r = await usersellstockdb.update_many(
        {"stock_id": {"$in": stock_ids}},
        {"$set": {"session_status": status, "status_checked_at": _now()}},
    )
    return r.modified_count


async def mark_transferred(stock_id: str) -> bool:
    r = await usersellstockdb.update_one(
        {"stock_id": stock_id, "transferred": False},
        {"$set": {"transferred": True, "transferred_at": _now()}},
    )
    return r.modified_count > 0


async def bulk_mark_transferred(stock_ids: list[str]) -> int:
    r = await usersellstockdb.update_many(
        {"stock_id": {"$in": stock_ids}, "transferred": False},
        {"$set": {"transferred": True, "transferred_at": _now()}},
    )
    return r.modified_count


# ── Read operations ───────────────────────────────────────────────────────────

async def get_user_sell_stock(stock_id: str) -> Optional[dict]:
    doc = await usersellstockdb.find_one({"stock_id": stock_id})
    if doc:
        doc.pop("_id", None)
    return doc


async def list_user_sell_stock(
    *,
    user_id: Optional[int] = None,
    sell_id: Optional[str] = None,
    stock_id: Optional[str] = None,
    country_code: Optional[str] = None,
    payment_status: Optional[str] = None,   # "paid" | "unpaid"
    session_status: Optional[str] = None,   # "clean" | "spam" | etc.
    transferred: Optional[bool] = None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    query: dict = {}
    if user_id:
        query["user_id"] = user_id
    if sell_id:
        query["sell_id"] = {"$regex": sell_id, "$options": "i"}
    if stock_id:
        query["stock_id"] = {"$regex": stock_id, "$options": "i"}
    if country_code:
        query["country_code"] = country_code.upper()
    if payment_status and payment_status in VALID_PAYMENT_STATUSES:
        query["payment_status"] = payment_status
    if session_status and session_status in VALID_SESSION_STATUSES:
        query["session_status"] = session_status
    if transferred is not None:
        query["transferred"] = transferred

    total = await usersellstockdb.count_documents(query)
    skip = (page - 1) * limit
    cursor = usersellstockdb.find(query).sort("received_at", -1).skip(skip).limit(limit)
    results: list[dict] = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(doc)
    return results, total


async def get_untransferred_by_ids(stock_ids: list[str]) -> list[dict]:
    cursor = usersellstockdb.find(
        {"stock_id": {"$in": stock_ids}, "transferred": False}
    )
    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(doc)
    return results


async def get_untransferred_by_query(
    *,
    user_id: Optional[int] = None,
    sell_id: Optional[str] = None,
    country_code: Optional[str] = None,
    payment_status: Optional[str] = None,
    session_status: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Used by bulk-transfer: fetch all matching non-transferred records."""
    query: dict = {"transferred": False}
    if user_id:
        query["user_id"] = user_id
    if sell_id:
        query["sell_id"] = sell_id
    if country_code:
        query["country_code"] = country_code.upper()
    if payment_status:
        query["payment_status"] = payment_status
    if session_status:
        query["session_status"] = session_status
    cursor = usersellstockdb.find(query).sort("received_at", 1).limit(limit)
    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(doc)
    return results


async def count_user_sell_stock(transferred: Optional[bool] = False) -> int:
    query: dict = {}
    if transferred is not None:
        query["transferred"] = transferred
    return await usersellstockdb.count_documents(query)



# ── One-shot backfill ─────────────────────────────────────────────────────────

async def backfill_from_sell_requests() -> int:
    """
    Insert a users_sell_stock row for every already-approved sell_request that
    doesn't yet have one. Runs at startup so upgrading from an older codebase
    (which forgot to populate users_sell_stock) doesn't leave already-paid
    sellers invisible in the admin panel.

    Idempotent — safe to run on every boot. Returns rows inserted this run.
    """
    from server.core.mongo import collection as _collection
    sell_requests = _collection("sell_requests")

    inserted = 0
    cursor = sell_requests.find(
        {"status": {"$in": ["paid", "approved"]}},
        {
            "request_id": 1, "user_id": 1, "phone": 1, "code": 1,
            "country_name": 1, "session_msg_id": 1, "session_chat_id": 1,
            "tg_user_id": 1, "username": 1, "first_name": 1,
            "has_2fa": 1, "tfa_password_enc": 1, "proxy_used": 1,
            "api_id_used": 1, "sell_type": 1, "spam_status": 1,
        },
    )
    async for r in cursor:
        sell_id = r.get("request_id")
        if not sell_id:
            continue
        existing = await usersellstockdb.find_one({"sell_id": sell_id}, {"_id": 1})
        if existing:
            continue
        try:
            await add_user_sell_stock(
                sell_id          = sell_id,
                user_id          = r.get("user_id"),
                phone            = r.get("phone", ""),
                country_code     = r.get("code", "XX"),
                country_name     = r.get("country_name", ""),
                session_msg_id   = r.get("session_msg_id"),
                session_chat_id  = r.get("session_chat_id"),
                tg_user_id       = r.get("tg_user_id"),
                username         = r.get("username"),
                first_name       = r.get("first_name"),
                has_2fa          = bool(r.get("has_2fa", False)),
                tfa_password_enc = r.get("tfa_password_enc", ""),
                proxy_used       = r.get("proxy_used", "none"),
                api_id_used      = r.get("api_id_used"),
                sell_type        = r.get("sell_type", "account"),
                spam_status      = r.get("spam_status", "unknown"),
                payment_status   = "paid",
            )
            inserted += 1
        except Exception:
            continue
    return inserted
