"""Admin BIN/quarantine management for invalid inventory sessions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.sessiondb import (
    get_session_account,
    get_bin_session,
    list_bin_sessions,
    restore_session_from_bin,
    delete_bin_session,
)
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-BIN"], include_in_schema=False)
_SENSITIVE_BIN_FIELDS = {
    "password",
    "tfa_password_enc",
    "session_bytes_enc",
    "session_data",
    "session_file",
    "string_session",
}


def _public_bin_doc(doc: dict) -> dict:
    """Keep credentials/session blobs out of admin JSON responses."""
    result = dict(doc)
    for key in _SENSITIVE_BIN_FIELDS:
        result.pop(key, None)
    return result


@router.get("/admin/bin", response_class=HTMLResponse)
async def bin_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/bin.html", {"page": "bin"})


@router.get("/admin/api/bin")
async def list_bin(
    _session=Depends(require_session),
    country: Optional[str] = Query(None),
    issue: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    def parse_date(raw: Optional[str], end_of_day: bool = False):
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.replace(hour=23, minute=59, second=59, microsecond=999999) if end_of_day else value
        except ValueError:
            return None

    items, total = await list_bin_sessions(
        country_code=country,
        issue=issue,
        search=q,
        detected_after=parse_date(date_from),
        detected_before=parse_date(date_to, end_of_day=True),
        page=page,
        limit=limit,
    )
    for item in items:
        for key in ("bin_detected_at", "bin_moved_at", "bin_expires_at", "uploaded_at"):
            value = item.get(key)
            if value is not None and hasattr(value, "isoformat"):
                item[key] = value.isoformat()
    items = [_public_bin_doc(item) for item in items]
    return JSONResponse(jsonable_encoder({
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
    }))


@router.get("/admin/api/storage/diagnose/{account_id}")
async def diagnose_storage(account_id: str, _session=Depends(require_session)):
    """Read-only diagnostic of the exact Mongo-referenced Telegram message."""
    doc = await get_bin_session(account_id) or await get_session_account(account_id)
    if not doc:
        return JSONResponse({"ok": False, "error": "Account not found"}, status_code=404)
    chat_id = doc.get("session_chat_id")
    msg_id = doc.get("session_msg_id")
    if not chat_id or not msg_id:
        return JSONResponse({
            "ok": True,
            "diagnostic": {
                "account_id": account_id,
                "db_chat_id": chat_id,
                "db_message_id": msg_id,
                "error_code": "SESSION_REFERENCE_MISSING",
                "error": "MongoDB record has an incomplete storage reference.",
            },
        })
    from server import bot
    from server.utils.sessions.channel_storage import diagnose_session_message
    diagnostic = await diagnose_session_message(
        bot,
        chat_id,
        msg_id,
        account_id=account_id,
        phone=doc.get("phone"),
    )
    return JSONResponse({"ok": True, "diagnostic": diagnostic})


@router.get("/admin/api/bin/{account_id}")
async def bin_detail(account_id: str, _session=Depends(require_session)):
    doc = await get_bin_session(account_id)
    if not doc:
        return JSONResponse({"ok": False, "error": "BIN session not found"}, status_code=404)
    doc = _public_bin_doc(doc)
    for key, value in list(doc.items()):
        if hasattr(value, "isoformat"):
            doc[key] = value.isoformat()
    return JSONResponse({"ok": True, "item": doc})


class BinIds(BaseModel):
    account_ids: list[str] = Field(default_factory=list, min_length=1, max_length=200)


@router.post("/admin/api/bin/restore")
async def restore_bin(body: BinIds, request: Request, _session=Depends(require_session)):
    restored = 0
    results = []
    for account_id in body.account_ids:
        ok = await restore_session_from_bin(account_id)
        restored += int(ok)
        results.append({"account_id": account_id, "ok": ok})
        if ok:
            await log_action("session", "bin_restored", ip=client_ip(request), target=account_id, ok=True)
    return JSONResponse({"ok": True, "restored": restored, "results": results})


@router.delete("/admin/api/bin/{account_id}")
async def delete_bin(account_id: str, request: Request, _session=Depends(require_session)):
    ok = await delete_bin_session(account_id)
    await log_action("session", "bin_deleted", ip=client_ip(request), target=account_id, ok=ok)
    return JSONResponse({"ok": ok})


@router.post("/admin/api/bin/delete")
async def delete_bin_bulk(body: BinIds, request: Request, _session=Depends(require_session)):
    deleted = 0
    results = []
    for account_id in body.account_ids:
        ok = await delete_bin_session(account_id)
        deleted += int(ok)
        results.append({"account_id": account_id, "ok": ok})
        if ok:
            await log_action("session", "bin_deleted", ip=client_ip(request), target=account_id, ok=True)
    return JSONResponse({"ok": True, "deleted": deleted, "results": results})
