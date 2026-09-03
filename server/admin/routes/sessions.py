"""Session accounts management routes."""

from typing import Optional
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.sessiondb import (
    delete_session_account,
    mark_session_sold,
    revert_session_sold,
    get_session_stats,
    sessionaccountsdb,
)
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Sessions"], include_in_schema=False)


@router.get("/admin/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/sessions.html", {"page": "sessions"})


@router.get("/admin/api/sessions")
async def list_sessions(
    _session=Depends(require_session),
    country: Optional[str] = Query(None),
    sold: Optional[str] = Query(None),
    spam: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    # BIN/quarantined records have their own admin section and must not appear
    # in the normal active inventory list.
    query: dict = {"inventory_state": {"$ne": "bin"}}
    if country:
        query["country_code"] = country.upper()
    if sold == "true":
        query["sold"] = True
    elif sold == "false":
        query["sold"] = False
    if spam:
        query["spam_status"] = spam
    if q:
        import re
        q = re.escape(q.strip())
        query["$or"] = [
            {"phone": {"$regex": q, "$options": "i"}},
            {"username": {"$regex": q, "$options": "i"}},
            {"first_name": {"$regex": q, "$options": "i"}},
            {"account_id": {"$regex": q, "$options": "i"}},
        ]

    skip = (page - 1) * limit
    total = await sessionaccountsdb.count_documents(query)
    cursor = sessionaccountsdb.find(query, {"_id": 0}).sort("uploaded_at", -1).skip(skip).limit(limit)
    docs = []
    async for d in cursor:
        # Sanitise: remove encrypted password from response
        d.pop("tfa_password_enc", None)
        d.pop("password", None)
        # Serialize datetimes
        for k in ("uploaded_at", "sold_at", "login_time"):
            if d.get(k):
                d[k] = d[k].isoformat()
        docs.append(d)

    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


@router.get("/admin/api/sessions/stats")
async def session_stats(_session=Depends(require_session)):
    stats = await get_session_stats()
    return JSONResponse({"countries": stats})


@router.delete("/admin/api/sessions/{account_id}")
async def delete_session(account_id: str, request: Request, _session=Depends(require_session)):
    ok = await delete_session_account(account_id)
    await log_action("session", "session_deleted", ip=client_ip(request), target=account_id, ok=ok)
    return JSONResponse({"ok": ok})


@router.post("/admin/api/sessions/{account_id}/mark-sold")
async def session_mark_sold(account_id: str, request: Request, _session=Depends(require_session)):
    from pydantic import BaseModel

    class Body(BaseModel):
        sold_to: int = 0

    body_raw = await request.json()
    sold_to = body_raw.get("sold_to", 0)
    ok = await mark_session_sold(account_id, sold_to)
    await log_action("session", "session_marked_sold", ip=client_ip(request), target=account_id, ok=ok)
    return JSONResponse({"ok": ok})


@router.post("/admin/api/sessions/{account_id}/revert-sold")
async def session_revert_sold(account_id: str, request: Request, _session=Depends(require_session)):
    ok = await revert_session_sold(account_id)
    await log_action("session", "session_reverted_sold", ip=client_ip(request), target=account_id, ok=ok)
    return JSONResponse({"ok": ok})


@router.patch("/admin/api/sessions/{account_id}/spam-status")
async def update_spam_status_route(account_id: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    status = body.get("spam_status", "unknown")
    if status not in ("clean", "temporary_spam", "permanent_spam", "frozen", "unknown"):
        return JSONResponse({"ok": False, "detail": "Invalid spam_status"}, status_code=400)
    # Use sessiondb helper so memstore stock counts stay consistent
    from server.utils.database.sessiondb import update_spam_status as _update_spam
    ok = await _update_spam(account_id, status)
    await log_action("session", "spam_status_updated", ip=client_ip(request), target=account_id, detail=status, ok=ok)
    return JSONResponse({"ok": ok})
