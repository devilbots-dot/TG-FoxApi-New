"""Audit logs + live server log routes."""

from typing import Optional
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.auditdb import auditlogsdb
from server.utils.logbuffer import subscribe_logs, read_log_tail

router = APIRouter(tags=["Admin-Logs"], include_in_schema=False)


@router.get("/admin/logs", response_class=HTMLResponse)
async def logs_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/logs.html", {"page": "logs"})


# ── Audit Logs (DB) ───────────────────────────────────────────────────────────

@router.get("/admin/api/logs")
async def list_logs(
    _session=Depends(require_session),
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    ok: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
):
    query: dict = {}
    if category:
        query["category"] = category
    if ok == "true":
        query["ok"] = True
    elif ok == "false":
        query["ok"] = False
    if q:
        import re
        q = re.escape(q.strip())
        query["$or"] = [
            {"action":   {"$regex": q, "$options": "i"}},
            {"target":   {"$regex": q, "$options": "i"}},
            {"detail":   {"$regex": q, "$options": "i"}},
            {"ip":       {"$regex": q, "$options": "i"}},
            {"actor":    {"$regex": q, "$options": "i"}},
        ]

    skip  = (page - 1) * limit
    total = await auditlogsdb.count_documents(query)
    cursor = auditlogsdb.find(query, {"_id": 0}).sort("ts", -1).skip(skip).limit(limit)
    docs = []
    async for d in cursor:
        if d.get("ts"):
            d["ts"] = d["ts"].isoformat()
        docs.append(d)

    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


# ── Server Logs (live) ────────────────────────────────────────────────────────

@router.get("/admin/api/logs/server")
async def server_logs_snapshot(
    _session=Depends(require_session),
    n: int = Query(300, ge=1, le=2000),
):
    """Return last *n* lines from log.txt (one-shot REST call)."""
    lines = read_log_tail(n)
    return JSONResponse({"lines": lines})


@router.get("/admin/api/logs/stream")
async def server_logs_stream(request: Request, _session=Depends(require_session)):
    """SSE endpoint — streams live log lines to the browser."""
    async def event_generator():
        async for chunk in subscribe_logs():
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
