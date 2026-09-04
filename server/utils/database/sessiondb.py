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

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from server.core import mongo as _mongo
from server.core.mongo import collection
from server.core import memstore
from server.utils.common import utcnow as _now

sessionaccountsdb = collection("session_accounts")

BIN_RETENTION_DAYS = 30
BIN_STATE = "bin"
ACTIVE_STATE = "active"

# Older deployments used a dedicated BIN collection, while newer deployments
# keep quarantined sessions in session_accounts with inventory_state="bin".
# Keep the adapter deliberately conservative: only explicit BIN/quarantine
# markers are treated as quarantined when scanning session_accounts.
_KNOWN_LEGACY_BIN_COLLECTIONS = (
    "bin_sessions",
    "bin_session",
    "session_bin",
    "session_bins",
    "invalid_sessions",
    "invalid_session_accounts",
    "quarantine_sessions",
    "quarantined_sessions",
    "bin",
)
_LEGACY_BIN_STATE_VALUES = {"bin", "quarantine", "quarantined", "invalid"}
_LEGACY_BIN_COLLECTION_RE = re.compile(
    r"(?:^|[_-])(bin|bins|quarantine|quarantined|invalid[_-]?sessions?)(?:$|[_-])",
    re.IGNORECASE,
)


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
    if result.modified_count == 0:
        # Older records in session_accounts may use a boolean/legacy status
        # instead of inventory_state="bin".
        result = await sessionaccountsdb.update_one(
            {"account_id": account_id, **_legacy_bin_marker_query()},
            {"$set": {
                "inventory_state": ACTIVE_STATE,
                "sold": False,
                "sold_to": None,
                "sold_at": None,
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
        return True

    # If the old deployment used a dedicated BIN collection, promote the
    # record into the canonical session_accounts inventory before removing the
    # legacy copy. This keeps existing Mongo data usable without a destructive
    # startup migration.
    for name in await _legacy_bin_collection_names():
        legacy = await _mongo.mongodb[name].find_one({"account_id": account_id}, {"_id": 0})
        if not legacy:
            continue
        promoted = dict(legacy)
        promoted["account_id"] = account_id
        promoted["inventory_state"] = ACTIVE_STATE
        promoted["sold"] = False
        promoted["sold_to"] = None
        promoted["sold_at"] = None
        for key in (
            "bin", "is_bin", "in_bin", "quarantine", "quarantined",
            "bin_reason", "bin_issue", "bin_detected_at", "bin_moved_at",
            "bin_expires_at", "bin_order_id", "bin_original_status",
        ):
            promoted.pop(key, None)
        await sessionaccountsdb.replace_one(
            {"account_id": account_id},
            promoted,
            upsert=True,
        )
        await _mongo.mongodb[name].delete_one({"account_id": account_id})
        cc = str(promoted.get("country_code", "XX")).upper()
        spam = promoted.get("spam_status", "unknown")
        memstore.inc_stock(cc, spam)
        memstore.register_account(account_id, cc, spam)
        return True
    return False


async def get_bin_session(account_id: str) -> Optional[dict]:
    """Fetch a BIN record from the canonical or any supported legacy store."""
    doc = await sessionaccountsdb.find_one(
        {"account_id": account_id, "$or": [
            {"inventory_state": BIN_STATE},
            *_legacy_bin_marker_query()["$or"],
        ]},
        {"_id": 0},
    )
    if doc:
        return _normalise_bin_doc(doc, "session_accounts")
    for name in await _legacy_bin_collection_names():
        doc = await _mongo.mongodb[name].find_one({"account_id": account_id}, {"_id": 0})
        if doc:
            return _normalise_bin_doc(doc, name)
    return None


async def delete_bin_session(account_id: str) -> bool:
    """Delete only a BIN account, including records in legacy BIN stores."""
    result = await sessionaccountsdb.delete_one(
        {"account_id": account_id, **_legacy_bin_marker_query()}
    )
    if result.deleted_count > 0:
        return True
    for name in await _legacy_bin_collection_names():
        result = await _mongo.mongodb[name].delete_one({"account_id": account_id})
        if result.deleted_count > 0:
            return True
    return False


def _legacy_bin_marker_query() -> dict:
    """Match explicit legacy BIN markers inside session_accounts."""
    return {"$or": [
        {"inventory_state": {"$in": list(_LEGACY_BIN_STATE_VALUES)}},
        {"bin": True},
        {"is_bin": True},
        {"in_bin": True},
        {"quarantine": True},
        {"quarantined": True},
        {"bin_issue": {"$exists": True}},
        {"bin_reason": {"$exists": True}},
        {"status": {"$in": list(_LEGACY_BIN_STATE_VALUES)}},
    ]}


async def _legacy_bin_collection_names() -> list[str]:
    """Return existing legacy BIN collection names without creating any."""
    try:
        existing = set(await _mongo.mongodb.list_collection_names())
    except Exception:
        # A named Mongo collection can be queried safely even when it has not
        # been created; use only known names if metadata lookup is unavailable.
        existing = set(_KNOWN_LEGACY_BIN_COLLECTIONS)
    names = [name for name in _KNOWN_LEGACY_BIN_COLLECTIONS if name in existing]
    for name in sorted(existing):
        if name not in names and _LEGACY_BIN_COLLECTION_RE.search(name):
            names.append(name)
    return names


def _bin_value(doc: dict, *keys: str):
    for key in keys:
        value = doc.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalise_bin_doc(doc: dict, source: str) -> dict:
    """Map current and historical BIN field names to one admin shape."""
    item = dict(doc)
    item.pop("_id", None)
    account_id = _bin_value(item, "account_id", "session_id", "id", "phone")
    country_code = _bin_value(item, "country_code", "country", "cc", "country_iso")
    item["account_id"] = str(account_id or "")
    item["country_code"] = str(country_code or "XX").upper()
    item.setdefault("country_name", _bin_value(item, "country_name", "country") or item["country_code"])
    item["bin_issue"] = str(_bin_value(
        item, "bin_issue", "issue", "error_code", "status", "reason",
    ) or "invalid_session")
    item["bin_reason"] = str(_bin_value(
        item, "bin_reason", "reason", "error", "message", "details",
    ) or item["bin_issue"])
    item["bin_detected_at"] = _bin_value(
        item, "bin_detected_at", "detected_at", "failed_at", "bin_moved_at",
        "created_at", "uploaded_at",
    )
    item["bin_moved_at"] = _bin_value(
        item, "bin_moved_at", "moved_at", "bin_detected_at", "detected_at",
    )
    item["bin_expires_at"] = _bin_value(item, "bin_expires_at", "expires_at")
    item["_bin_source"] = source
    return item


def _bin_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _bin_matches(
    item: dict,
    *,
    country_code: Optional[str],
    issue: Optional[str],
    search: Optional[str],
    detected_after,
    detected_before,
) -> bool:
    if country_code and item["country_code"] != country_code.upper():
        return False
    if issue and issue.strip().lower() not in item["bin_issue"].lower():
        return False
    if search:
        needle = search.strip().lower()
        haystack = " ".join(str(item.get(k) or "") for k in (
            "account_id", "phone", "bin_reason", "bin_issue",
        )).lower()
        if needle not in haystack:
            return False
    detected = _bin_datetime(item.get("bin_detected_at") or item.get("bin_moved_at"))
    after = _bin_datetime(detected_after)
    before = _bin_datetime(detected_before)
    if after and (detected is None or detected < after):
        return False
    if before and (detected is None or detected > before):
        return False
    return True


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
    current_query = {
        "$or": [
            {"inventory_state": BIN_STATE},
            _legacy_bin_marker_query(),
        ]
    }
    docs = []
    cursor = sessionaccountsdb.find(current_query, {"_id": 0})
    async for doc in cursor:
        item = _normalise_bin_doc(doc, "session_accounts")
        if _bin_matches(
            item,
            country_code=country_code,
            issue=issue,
            search=search,
            detected_after=detected_after,
            detected_before=detected_before,
        ):
            docs.append(item)

    # Dedicated legacy BIN collections contain only quarantined records, so
    # their documents do not need the marker query used for session_accounts.
    for name in await _legacy_bin_collection_names():
        cursor = _mongo.mongodb[name].find({}, {"_id": 0})
        async for doc in cursor:
            item = _normalise_bin_doc(doc, name)
            if _bin_matches(
                item,
                country_code=country_code,
                issue=issue,
                search=search,
                detected_after=detected_after,
                detected_before=detected_before,
            ):
                docs.append(item)

    # Prefer the current canonical record if a legacy collection still has a
    # duplicate copy of the same account.
    unique = {}
    for item in docs:
        key = item.get("account_id") or f"{item.get('_bin_source')}:{len(unique)}"
        if key not in unique or unique[key].get("_bin_source") != "session_accounts":
            unique[key] = item
    docs = list(unique.values())
    docs.sort(
        key=lambda item: _bin_datetime(
            item.get("bin_moved_at") or item.get("bin_detected_at")
        ) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    total = len(docs)
    start = max(0, (page - 1) * limit)
    return docs[start:start + limit], total


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
