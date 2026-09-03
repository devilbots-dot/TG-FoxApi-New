"""
session_accounts collection — stock management for Telegram .session files.

RAM-first for stock counts:
  count_unsold_by_country()       → memstore.stock_counts (no Mongo round-trip)
  get_unsold_counts_all_countries() → memstore.stock_counts (no Mongo round-trip)
  Buyer counts include clean + temporary_spam + permanent_spam because only
  an explicit admin transfer admits an account to this inventory.

  add_session_account()  → Mongo insert (awaited) + memstore.inc_stock()
  mark_session_sold()    → Mongo update (awaited) + memstore.dec_stock()
  revert_session_sold()  → Mongo update (awaited) + memstore.inc_stock()
  delete_session_account() → Mongo delete (awaited) + memstore update

Actual session docs (with session_msg_id etc.) still fetched from Mongo —
the full binary metadata isn't cached in RAM.
"""

import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from server.core.mongo import collection
from server.core import memstore
from server.utils.common import utcnow as _now

sessionaccountsdb = collection("session_accounts")

BIN_RETENTION_DAYS = 30
BIN_STATE = "bin"
ACTIVE_STATE = "active"


def _gen_id() -> str:
    return f"ACC-{secrets.token_hex(6).upper()}"


# ── Schema ─────────────────────────────────────────────────────────────────
# {
#   "account_id":           "ACC-XXXXXXXXXXXX",
#   "phone":                "+919579535567",
#   "country_code":         "IN",
#   "country_name":         "India 🇮🇳",
#   "tg_user_id":           123456789,
#   "username":             "johndoe",
#   "first_name":           "John",
#   "session_msg_id":       12345,
#   "session_chat_id":      -1001234567890,
#   "password":             "",
#   "tfa_password_enc":     "gAAAAA...",
#   "has_2fa":              True,
#   "tfa_updated":          True,
#   "spam_status":          "clean",
#   "verified":             True,
#   "verification_status":  "verified",
#   "login_time":           datetime,
#   "proxy_used":           "PRX-XXXXX",
#   "api_id_used":          12345678,
#   "terminated_others":    True,
#   "sold":                 False,
#   "sold_to":              None,
#   "uploaded_at":          datetime,
#   "sold_at":              None,
# }


async def add_session_account(
    phone: str,
    country_code: str,
    country_name: str,
    session_msg_id: int,
    session_chat_id: int,
    password: str = "",
    *,
    tg_user_id: Optional[int] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    has_2fa: bool = False,
    tfa_updated: bool = False,
    tfa_password_enc: str = "",
    spam_status: str = "unknown",
    verified: bool = False,
    verification_status: str = "unverified",
    login_time: Optional[datetime] = None,
    proxy_used: str = "none",
    api_id_used: Optional[int] = None,
    terminated_others: bool = False,
) -> str:
    account_id = _gen_id()
    cc         = country_code.upper()
    doc = {
        "account_id":          account_id,
        "phone":               phone,
        "country_code":        cc,
        "country_name":        country_name,
        "tg_user_id":          tg_user_id,
        "username":            username,
        "first_name":          first_name,
        "session_msg_id":      session_msg_id,
        "session_chat_id":     session_chat_id,
        "password":            password,
        "tfa_password_enc":    tfa_password_enc,
        "has_2fa":             has_2fa,
        "tfa_updated":         tfa_updated,
        "spam_status":         spam_status,
        "verified":            verified,
        "verification_status": verification_status,
        "login_time":          login_time or _now(),
        "proxy_used":          proxy_used,
        "api_id_used":         api_id_used,
        "terminated_others":   terminated_others,
        "sold":                False,
        "sold_to":             None,
        "inventory_state":     ACTIVE_STATE,
        "uploaded_at":         _now(),
        "sold_at":             None,
    }
    await sessionaccountsdb.insert_one(doc)

    # Update RAM stock counts
    memstore.inc_stock(cc, spam_status)
    memstore.register_account(account_id, cc, spam_status)

    return account_id


async def get_session_account(account_id: str) -> Optional[dict]:
    doc = await sessionaccountsdb.find_one({"account_id": account_id})
    if doc:
        doc.pop("_id", None)
    return doc


async def get_session_by_phone(phone: str) -> Optional[dict]:
    """Check if a phone number is already in the DB (duplicate detection)."""
    doc = await sessionaccountsdb.find_one({"phone": phone})
    if doc:
        doc.pop("_id", None)
    return doc


async def get_unsold_session_for_country(
    country_code: str,
    clean_only: bool = True,
    exclude_account_ids: Optional[list[str]] = None,
) -> Optional[dict]:
    """
    Pick the oldest unsold session for a given country.
    Always fetches from Mongo — we need the full doc (session_msg_id etc.).
    """
    query: dict = {
        "country_code": country_code.upper(),
        "sold": False,
        "inventory_state": {"$ne": BIN_STATE},
    }
    if exclude_account_ids:
        query["account_id"] = {"$nin": list(exclude_account_ids)}
    if clean_only:
        # Sellable pool = clean + temporary_spam + permanent_spam. Admin explicitly
        # pushes accepted accounts to inventory; frozen/dead/unknown are excluded.
        query["spam_status"] = {"$in": ["clean", "temporary_spam", "permanent_spam"]}
    doc = await sessionaccountsdb.find_one(query, sort=[("uploaded_at", 1)])
    if doc:
        doc.pop("_id", None)
    return doc


async def mark_session_sold(account_id: str, sold_to: int) -> bool:
    r = await sessionaccountsdb.update_one(
        {
            "account_id": account_id,
            "sold": False,
            "inventory_state": {"$ne": BIN_STATE},
        },
        {"$set": {"sold": True, "sold_to": sold_to, "sold_at": _now()}},
    )
    if r.modified_count > 0:
        meta = memstore.get_account_meta(account_id)
        if meta:
            memstore.dec_stock(meta["cc"], meta["spam"])
            memstore.unregister_account(account_id)
    return r.modified_count > 0


async def revert_session_sold(account_id: str) -> bool:
    """Roll back a mark_session_sold reservation — puts account back into unsold pool."""
    # Need the account's metadata to restore stock counts — fetch from Mongo
    doc = await sessionaccountsdb.find_one(
        {"account_id": account_id},
        {"country_code": 1, "spam_status": 1},
    )
    r = await sessionaccountsdb.update_one(
        {"account_id": account_id, "sold": True},
        {"$set": {"sold": False, "sold_to": None, "sold_at": None}},
    )
    if r.modified_count > 0 and doc:
        cc   = str(doc.get("country_code", "XX")).upper()
        spam = doc.get("spam_status", "unknown")
        memstore.inc_stock(cc, spam)
        memstore.register_account(account_id, cc, spam)
    return r.modified_count > 0


async def delete_session_account(account_id: str) -> bool:
    # Get metadata before deleting so we can update stock counts
    doc = await sessionaccountsdb.find_one(
        {"account_id": account_id},
        {"sold": 1, "country_code": 1, "spam_status": 1},
    )
    r = await sessionaccountsdb.delete_one({"account_id": account_id})
    if r.deleted_count > 0 and doc and not doc.get("sold"):
        cc   = str(doc.get("country_code", "XX")).upper()
        spam = doc.get("spam_status", "unknown")
        memstore.dec_stock(cc, spam)
        memstore.unregister_account(account_id)
    return r.deleted_count > 0


_VALID_SPAM_STATUSES = ("clean", "temporary_spam", "permanent_spam", "frozen", "unknown")


async def update_spam_status(account_id: str, status: str) -> bool:
    if status not in _VALID_SPAM_STATUSES:
        return False

    # Persist to MongoDB FIRST so that if the write fails the in-memory stock
    # counts are never mutated — previously memstore was updated before the
    # Mongo write, causing permanent desync on any DB error until restart.
    r = await sessionaccountsdb.update_one(
        {"account_id": account_id},
        {"$set": {"spam_status": status}},
    )
    if r.modified_count == 0:
        return False

    # Only update RAM counts after a confirmed DB write.  This keeps
    # memstore.stock_counts in sync with what is actually stored in Mongo.
    meta = memstore.get_account_meta(account_id)
    if meta and meta["spam"] != status:
        cc = meta["cc"]
        memstore.dec_stock(cc, meta["spam"])
        memstore.inc_stock(cc, status)
        memstore._account_index[account_id]["spam"] = status

    return True


async def get_all_session_accounts(
    country_code: Optional[str] = None,
    sold: Optional[bool] = None,
    limit: int = 200,
) -> List[dict]:
    query: dict = {"inventory_state": {"$ne": BIN_STATE}}
    if country_code:
        query["country_code"] = country_code.upper()
    if sold is not None:
        query["sold"] = sold
    cursor  = sessionaccountsdb.find(query).sort("uploaded_at", -1).limit(limit)
    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(doc)
    return results


async def add_extra_session(phone: str, msg_id: int, chat_id: int) -> bool:
    r = await sessionaccountsdb.update_one(
        {"phone": phone},
        {"$push": {"extra_sessions": {
            "msg_id":     msg_id,
            "chat_id":    chat_id,
            "created_at": _now(),
        }}},
    )
    return r.modified_count > 0


async def update_2fa_state(
    phone: str,
    has_2fa: bool,
    tfa_password_enc: str,
    tfa_updated: bool = True,
) -> bool:
    r = await sessionaccountsdb.update_one(
        {"phone": phone},
        {"$set": {
            "has_2fa":          has_2fa,
            "tfa_password_enc": tfa_password_enc,
            "tfa_updated":      tfa_updated,
        }},
    )
    return r.modified_count > 0


async def move_session_to_bin(
    account_id: str,
    reason: str,
    *,
    issue: Optional[str] = None,
    order_id: Optional[str] = None,
) -> bool:
    """Quarantine a reserved session atomically as BIN inventory.

    The caller must have reserved the account with ``sold=True``.  Keeping the
    source document in ``session_accounts`` preserves the Telegram channel
    reference and session metadata for admin inspection without exposing it in
    normal stock queries.
    """
    now = _now()
    result = await sessionaccountsdb.update_one(
        {
            "account_id": account_id,
            "sold": True,
            "inventory_state": {"$ne": BIN_STATE},
        },
        {"$set": {
            "inventory_state": BIN_STATE,
            "bin_reason": (reason or "invalid_session")[:500],
            "bin_issue": (issue or reason or "invalid_session")[:120],
            "bin_detected_at": now,
            "bin_moved_at": now,
            "bin_expires_at": now + timedelta(days=BIN_RETENTION_DAYS),
            "bin_order_id": order_id,
            "bin_original_status": "sold",
        }},
    )
    return result.modified_count > 0


async def restore_session_from_bin(account_id: str) -> bool:
    """Restore a BIN account to active unsold inventory atomically."""
    result = await sessionaccountsdb.update_one(
        {"account_id": account_id, "inventory_state": BIN_STATE},
        {"$set": {
            "inventory_state": ACTIVE_STATE,
            "sold": False,
            "sold_to": None,
            "sold_at": None,
        }, "$unset": {
            "bin_reason": "",
            "bin_issue": "",
            "bin_detected_at": "",
            "bin_moved_at": "",
            "bin_expires_at": "",
            "bin_order_id": "",
            "bin_original_status": "",
        }},
    )
    if result.modified_count > 0:
        doc = await sessionaccountsdb.find_one(
            {"account_id": account_id},
            {"country_code": 1, "spam_status": 1},
        )
        if doc:
            cc = str(doc.get("country_code", "XX")).upper()
            spam = doc.get("spam_status", "unknown")
            memstore.inc_stock(cc, spam)
            memstore.register_account(account_id, cc, spam)
    return result.modified_count > 0


async def delete_bin_session(account_id: str) -> bool:
    """Delete only a BIN account; active inventory can never match this filter."""
    result = await sessionaccountsdb.delete_one(
        {"account_id": account_id, "inventory_state": BIN_STATE}
    )
    return result.deleted_count > 0


async def list_bin_sessions(
    *,
    country_code: Optional[str] = None,
    issue: Optional[str] = None,
    search: Optional[str] = None,
    detected_after=None,
    detected_before=None,
    page: int = 1,
    limit: int = 50,
) -> tuple[list[dict], int]:
    query: dict = {"inventory_state": BIN_STATE}
    if country_code:
        query["country_code"] = country_code.upper()
    if issue:
        query["bin_issue"] = {"$regex": issue.strip()[:80], "$options": "i"}
    if search:
        import re
        q = re.escape(search.strip()[:80])
        query["$or"] = [
            {"account_id": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
            {"bin_reason": {"$regex": q, "$options": "i"}},
        ]
    if detected_after or detected_before:
        query["bin_detected_at"] = {}
        if detected_after:
            query["bin_detected_at"]["$gte"] = detected_after
        if detected_before:
            query["bin_detected_at"]["$lte"] = detected_before
    total = await sessionaccountsdb.count_documents(query)
    cursor = sessionaccountsdb.find(query, {"_id": 0}).sort("bin_moved_at", -1).skip((page - 1) * limit).limit(limit)
    docs = []
    async for doc in cursor:
        docs.append(doc)
    return docs, total


async def cleanup_expired_bin_sessions() -> int:
    """Delete expired BIN records idempotently without touching active inventory."""
    result = await sessionaccountsdb.delete_many({
        "inventory_state": BIN_STATE,
        "bin_expires_at": {"$lte": _now(), "$ne": None},
    })
    return result.deleted_count


async def count_unsold_by_country(country_code: str, clean_only: bool = True) -> int:
    """Served from RAM. clean_only=True means the complete sellable pool."""
    return memstore.get_stock_count(country_code, clean_only=clean_only)


async def get_unsold_counts_all_countries(clean_only: bool = True) -> dict:
    """
    Return {country_code: unsold_count} for ALL countries — served from RAM.
    Replaces the old O(N) aggregation round-trip.
    """
    field = "sellable" if clean_only else "total"
    return {
        cc: entry.get(field, 0)
        for cc, entry in memstore.stock_counts.items()
    }


async def get_session_stats(clean_only: bool = False) -> list:
    """
    Per-country unsold counts for the admin /sessions_stats command.
    Still fetches from Mongo — admin view needs exact counts including
    the country_name field which isn't in the stock_counts index.
    """
    match: dict = {"sold": False, "inventory_state": {"$ne": BIN_STATE}}
    if clean_only:
        match["spam_status"] = "clean"
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id":          "$country_code",
                "count":        {"$sum": 1},
                "clean":        {"$sum": {"$cond": [{"$eq": ["$spam_status", "clean"]}, 1, 0]}},
                "country_name": {"$first": "$country_name"},
            }
        },
        {"$sort": {"count": -1}},
    ]
    results = []
    async for doc in sessionaccountsdb.aggregate(pipeline):
        results.append({
            "country_code": doc["_id"],
            "country_name": doc["country_name"],
            "unsold":       doc["count"],
            "sellable":     doc["clean"],
        })
    return results
