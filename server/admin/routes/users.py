"""User management routes."""

from typing import Optional
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import csv
import io

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.userdb import (
    usersdb,
    add_banned_user,
    remove_banned_user,
    update_balance,
    set_balance,
    set_rank,
    set_api_access,
    get_user,
    RANK_THRESHOLDS,
)
from server.utils.notifications import notify as _notify
from server.utils.database.walletdb import (
    transactionsdb,
    log_transaction,
)
from server.utils.database.orderdb import ordersdb
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Users"], include_in_schema=False)


from datetime import datetime


def _ser_datetime(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _ser_datetime(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_ser_datetime(i) for i in v]
    return v


def _ser_user(u: dict) -> dict:
    u.pop("_id", None)
    return _ser_datetime(u)


_USER_FIELDS = {
    "_id": 0, "user_id": 1, "username": 1, "full_name": 1, "balance": 1,
    "rank": 1, "is_banned": 1, "ban_reason": 1, "is_verified": 1,
    "api_access": 1, "total_account_buy": 1, "total_account_sell": 1,
    "total_deposit": 1, "total_spend": 1, "referral_count": 1,
    "joined_at": 1, "language": 1,
}


@router.get("/admin/users", response_class=HTMLResponse)
async def users_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/users.html", {"page": "users"})


@router.get("/admin/api/users")
async def list_users(
    _session=Depends(require_session),
    q: Optional[str] = Query(None),
    banned: Optional[str] = Query(None),
    rank: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    query: dict = {"user_id": {"$gt": 0}}
    if banned == "true":
        query["is_banned"] = True
    elif banned == "false":
        query["is_banned"] = False
    if rank:
        query["rank"] = rank
    if q:
        import re
        q_raw = q.strip()
        q_esc = re.escape(q_raw)
        try:
            qint = int(q_raw)
            query["$or"] = [
                {"user_id": qint},
                {"username": {"$regex": q_esc, "$options": "i"}},
                {"full_name": {"$regex": q_esc, "$options": "i"}},
            ]
        except ValueError:
            query["$or"] = [
                {"username": {"$regex": q_esc, "$options": "i"}},
                {"full_name": {"$regex": q_esc, "$options": "i"}},
            ]

    skip = (page - 1) * limit
    total = await usersdb.count_documents(query)
    cursor = usersdb.find(query, _USER_FIELDS).sort("joined_at", -1).skip(skip).limit(limit)
    docs = [_ser_user(u) async for u in cursor]
    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


@router.get("/admin/api/users/export")
async def export_users(
    _session=Depends(require_session),
    q: Optional[str] = Query(None),
    banned: Optional[str] = Query(None),
    rank: Optional[str] = Query(None),
):
    """Export all matching users as CSV."""
    query: dict = {"user_id": {"$gt": 0}}
    if banned == "true":
        query["is_banned"] = True
    elif banned == "false":
        query["is_banned"] = False
    if rank:
        query["rank"] = rank
    if q:
        import re
        q_raw = q.strip()
        q_esc = re.escape(q_raw)
        try:
            qint = int(q_raw)
            query["$or"] = [
                {"user_id": qint},
                {"username": {"$regex": q_esc, "$options": "i"}},
                {"full_name": {"$regex": q_esc, "$options": "i"}},
            ]
        except ValueError:
            query["$or"] = [
                {"username": {"$regex": q_esc, "$options": "i"}},
                {"full_name": {"$regex": q_esc, "$options": "i"}},
            ]

    cursor = usersdb.find(query, _USER_FIELDS).sort("joined_at", -1)
    docs = [_ser_user(u) async for u in cursor]
    headers = ["user_id", "username", "full_name", "balance", "rank", "is_banned", "total_deposit", "total_spend", "total_account_buy", "joined_at"]

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for d in docs:
            writer.writerow(d)
            buf.seek(0)
            data = buf.read()
            buf.seek(0); buf.truncate(0)
            yield data

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


@router.get("/admin/api/users/{user_id}")
async def get_user_detail(user_id: int, _session=Depends(require_session)):
    user = await get_user(user_id)
    if not user:
        return JSONResponse({"ok": False, "detail": "Not found"}, status_code=404)
    user = _ser_user(user)
    user.pop("two_fa_secret", None)
    # Fetch recent transactions
    txns = await transactionsdb.find(
        {"user_id": user_id}, {"_id": 0}
    ).sort("created_at", -1).limit(10).to_list(length=10)
    for t in txns:
        if t.get("created_at"):
            t["created_at"] = t["created_at"].isoformat()
    # Fetch recent orders
    recent_orders = await ordersdb.find(
        {"buyer_id": user_id}, {"_id": 0}
    ).sort("created_at", -1).limit(5).to_list(length=5)
    for o in recent_orders:
        for k in ("created_at", "completed_at", "cancelled_at"):
            if o.get(k):
                o[k] = o[k].isoformat()
    return JSONResponse({"user": user, "transactions": txns, "orders": recent_orders})


@router.post("/admin/api/users/{user_id}/ban")
async def ban_user(user_id: int, request: Request, _session=Depends(require_session)):
    body = await request.json()
    reason = body.get("reason", "Admin action")
    await add_banned_user(user_id, reason)
    await log_action("user", "user_banned", ip=client_ip(request), target=str(user_id), detail=reason)
    # Best-effort notification — may fail if user blocked the bot
    try:
        await _notify(user_id, "account_banned", reason=reason)
    except Exception:
        pass
    return JSONResponse({"ok": True})


@router.post("/admin/api/users/{user_id}/unban")
async def unban_user(user_id: int, request: Request, _session=Depends(require_session)):
    await remove_banned_user(user_id)
    await log_action("user", "user_unbanned", ip=client_ip(request), target=str(user_id))
    try:
        await _notify(user_id, "account_unbanned")
    except Exception:
        pass
    return JSONResponse({"ok": True})


@router.post("/admin/api/users/{user_id}/balance")
async def adjust_balance(user_id: int, request: Request, _session=Depends(require_session)):
    body = await request.json()
    mode = body.get("mode", "add")  # "add" | "set"
    amount = float(body.get("amount", 0))
    note = body.get("note", "Admin adjustment")
    if mode == "set":
        await set_balance(user_id, amount)
    else:
        await update_balance(user_id, amount)
    await log_transaction(user_id, "adjustment", amount, note=note)
    await log_action("user", "balance_adjusted", ip=client_ip(request), target=str(user_id), detail=f"{mode} {amount}")
    # Notify user of admin balance change (best-effort)
    try:
        await _notify(user_id, "balance_adjusted", amount=amount, mode=mode, note=note)
    except Exception:
        pass
    return JSONResponse({"ok": True})


@router.post("/admin/api/users/{user_id}/rank")
async def change_rank(user_id: int, request: Request, _session=Depends(require_session)):
    body = await request.json()
    rank = body.get("rank", "VIP1")
    if rank not in RANK_THRESHOLDS:
        return JSONResponse({"ok": False, "detail": "Invalid rank"}, status_code=400)
    await set_rank(user_id, rank)
    await log_action("user", "rank_changed", ip=client_ip(request), target=str(user_id), detail=rank)
    return JSONResponse({"ok": True})


@router.post("/admin/api/users/{user_id}/api-access")
async def toggle_api_access(user_id: int, request: Request, _session=Depends(require_session)):
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    await set_api_access(user_id, enabled)
    await log_action("user", "api_access_toggled", ip=client_ip(request), target=str(user_id), detail=str(enabled))
    return JSONResponse({"ok": True})


@router.post("/admin/api/users/{user_id}/notify")
async def notify_user(user_id: int, request: Request, _session=Depends(require_session)):
    """Send a manual Telegram message to a user from the admin panel."""
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"ok": False, "detail": "Message cannot be empty."}, status_code=400)
    from server.utils.notifications import send_raw
    sent = await send_raw(user_id, message)
    await log_action(
        "user", "user_notified",
        ip=client_ip(request),
        target=str(user_id),
        detail=message[:200],
        ok=sent,
    )
    if not sent:
        return JSONResponse({"ok": False, "detail": "Failed to send — user may have blocked the bot."}, status_code=500)
    return JSONResponse({"ok": True})


@router.get("/admin/api/users/{user_id}/sessions")
async def get_user_sessions(user_id: int, _session=Depends(require_session)):
    """Return all sessions sold by and bought by this user."""
    from server.utils.database.sessiondb import sessionaccountsdb
    from server.utils.database.sellrequestdb import sellrequestsdb

    bought_cursor = sessionaccountsdb.find(
        {"sold_to": user_id, "sold": True},
        {"_id": 0, "tfa_password_enc": 0, "password": 0, "session_msg_id": 0, "session_chat_id": 0},
    ).sort("sold_at", -1).limit(50)
    bought = []
    async for s in bought_cursor:
        for k in ("uploaded_at", "sold_at", "login_time"):
            if s.get(k):
                s[k] = s[k].isoformat()
        bought.append(s)

    sold_cursor = sellrequestsdb.find(
        {"user_id": user_id},
        {"_id": 0, "session_bytes_enc": 0},
    ).sort("submitted_at", -1).limit(50)
    sold = []
    async for r in sold_cursor:
        for k in ("submitted_at", "reviewed_at", "payment_release_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
        sold.append(r)

    return JSONResponse({"bought": bought, "sold": sold})
