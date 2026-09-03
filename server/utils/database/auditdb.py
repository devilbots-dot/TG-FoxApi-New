"""
admin_audit_logs collection — records every admin-panel action for real,
searchable Logs / Security-audit pages. No synthetic/fake entries: a row
only exists here because something real actually happened.

Schema per document:
  ts         — datetime (UTC)
  category   — "login" | "admin" | "order" | "payment" | "proxy" | "session" | "country" | "user" | "settings"
  action     — short machine-readable action name, e.g. "login_success", "order_refund"
  actor      — "admin" (single-admin system today) or a future admin username
  ip         — client IP address
  target     — free-text identifier of what was acted on (order_id, phone, user_id, ...)
  detail     — free-text human-readable detail
  ok         — bool, whether the action succeeded
"""

from datetime import datetime
from typing import Optional, List

from server.core.mongo import collection
from server.utils.common import utcnow as _now

auditlogsdb = collection("admin_audit_logs")


async def log_action(
    category: str,
    action: str,
    *,
    actor: str = "admin",
    ip: Optional[str] = None,
    target: Optional[str] = None,
    detail: str = "",
    ok: bool = True,
) -> None:
    await auditlogsdb.insert_one({
        "ts": _now(),
        "category": category,
        "action": action,
        "actor": actor,
        "ip": ip,
        "target": target,
        "detail": detail,
        "ok": ok,
    })


async def get_logs(
    category: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
) -> List[dict]:
    query: dict = {}
    if category:
        query["category"] = category
    if q:
        query["$or"] = [
            {"action": {"$regex": q, "$options": "i"}},
            {"target": {"$regex": q, "$options": "i"}},
            {"detail": {"$regex": q, "$options": "i"}},
            {"ip": {"$regex": q, "$options": "i"}},
        ]
    cursor = auditlogsdb.find(query).sort("ts", -1).limit(limit)
    out = []
    async for d in cursor:
        d.pop("_id", None)
        out.append(d)
    return out


async def count_logs(category: Optional[str] = None) -> int:
    query = {"category": category} if category else {}
    return await auditlogsdb.count_documents(query)


async def count_failed_logins_since(since: datetime) -> int:
    return await auditlogsdb.count_documents({
        "category": "login", "action": "login_failed", "ts": {"$gte": since},
    })
