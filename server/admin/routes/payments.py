"""
Payment management — deposits, withdrawals, transactions.

Admin endpoints:
  GET    /admin/payments              → UI page
  GET    /admin/api/deposits          → paginated deposit list
  POST   /admin/api/deposits/{id}/approve
  POST   /admin/api/deposits/{id}/reject
  GET    /admin/api/deposits/export

  GET    /admin/api/withdrawals       → paginated withdrawal list (new system)
  GET    /admin/api/withdrawals/{id}  → single withdrawal detail + logs
  POST   /admin/api/withdrawals/{id}/approve
  POST   /admin/api/withdrawals/{id}/reject
  POST   /admin/api/withdrawals/{id}/verify  → force-poll gateway
  POST   /admin/api/withdrawals/{id}/retry   → re-submit to gateway
  GET    /admin/api/withdrawals/export

  GET    /admin/api/transactions      → paginated transaction ledger
  GET    /admin/api/transactions/export
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from bson import ObjectId

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.walletdb import (
    depositsdb,
    transactionsdb,
    confirm_deposit,
    reject_deposit,
)
from server.utils.database.withdrawaldb import (
    get_withdrawal_record,
    get_all_withdrawals,
    get_withdrawal_logs,
    log_withdrawal_event,
)
from server.services.withdrawal import get_withdrawal_service
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Payments"], include_in_schema=False)


# ── Serializer helpers ────────────────────────────────────────────────────────

def _ser(doc: dict) -> dict:
    """Return a JSON-safe admin record, including nested provider evidence."""
    doc.pop("_id", None)
    return jsonable_encoder(doc, custom_encoder={Decimal: float, ObjectId: str})


# ── Main page ─────────────────────────────────────────────────────────────────

@router.get("/admin/payments", response_class=HTMLResponse)
async def payments_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/payments.html", {"page": "payments"})


# ══════════════════════════════════════════════════════════════════════════════
# DEPOSITS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/api/deposits")
async def list_deposits(
    _session=Depends(require_session),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    query: dict = {}
    if status:
        query["status"] = status
    if q:
        q = q.strip()
        try:
            query["$or"] = [
                {"user_id":   int(q)},
                {"deposit_id": {"$regex": q, "$options": "i"}},
            ]
        except ValueError:
            query["deposit_id"] = {"$regex": q, "$options": "i"}

    skip  = (page - 1) * limit
    total = await depositsdb.count_documents(query)
    cursor = depositsdb.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
    docs   = [_ser(d) async for d in cursor]
    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


@router.get("/admin/api/deposits/export")
async def export_deposits(
    _session=Depends(require_session),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    query: dict = {}
    if status:
        query["status"] = status
    if q:
        q = q.strip()
        try:
            query["$or"] = [{"user_id": int(q)}, {"deposit_id": {"$regex": q, "$options": "i"}}]
        except ValueError:
            query["deposit_id"] = {"$regex": q, "$options": "i"}
    cursor = depositsdb.find(query, {"_id": 0}).sort("created_at", -1)
    docs   = [_ser(d) async for d in cursor]
    headers = ["deposit_id", "user_id", "amount", "method", "status", "created_at", "confirmed_at"]

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for d in docs:
            writer.writerow(d)
            buf.seek(0); data = buf.read(); buf.seek(0); buf.truncate(0)
            yield data

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=deposits.csv"},
    )


@router.post("/admin/api/deposits/{deposit_id}/approve")
async def approve_deposit(
    deposit_id: str, request: Request, _session=Depends(require_session)
):
    dep = await confirm_deposit(deposit_id)
    ok  = dep is not None
    await log_action("payment", "deposit_approved", ip=client_ip(request), target=deposit_id, ok=ok)
    if ok and dep:
        try:
            from server.utils.notifications import notify
            await notify(
                dep["user_id"], "deposit_confirmed",
                deposit_id=deposit_id,
                amount=dep.get("amount", 0),
                method=dep.get("method", ""),
            )
        except Exception:
            pass
    return JSONResponse({"ok": ok})


@router.post("/admin/api/deposits/{deposit_id}/reject")
async def reject_deposit_route(
    deposit_id: str, request: Request, _session=Depends(require_session)
):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    note = (body.get("note") or "").strip()
    dep  = await reject_deposit(deposit_id, note=note)
    ok   = dep is not None
    await log_action("payment", "deposit_rejected", ip=client_ip(request), target=deposit_id, ok=ok)
    if ok and dep:
        try:
            from server.utils.notifications import notify
            await notify(
                dep["user_id"], "deposit_rejected",
                deposit_id=deposit_id,
                amount=dep.get("amount", 0),
                note=note,
            )
        except Exception:
            pass
    return JSONResponse({"ok": ok})


# ══════════════════════════════════════════════════════════════════════════════
# WITHDRAWALS  (new production-grade system)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/api/withdrawals")
async def list_withdrawals(
    _session=Depends(require_session),
    status: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Paginated withdrawal list with filtering."""
    total, docs = await get_all_withdrawals(
        status=status,
        user_id=user_id,
        search=q,
        page=page,
        limit=limit,
    )
    return JSONResponse({
        "total": total,
        "page":  page,
        "limit": limit,
        "items": [_ser(d) for d in docs],
    })


@router.get("/admin/api/withdrawals/export")
async def export_withdrawals(
    _session=Depends(require_session),
    status: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
):
    """Export withdrawals as CSV."""
    _, docs = await get_all_withdrawals(status=status, user_id=user_id, page=1, limit=10_000)
    headers = [
        "withdrawal_id", "user_id", "amount", "fee_amount", "net_amount",
        "currency", "network", "wallet_address", "status", "gateway",
        "gateway_track_id", "gateway_tx_hash", "reason",
        "created_at", "updated_at", "completed_at",
    ]

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for d in [_ser(d) for d in docs]:
            writer.writerow(d)
            buf.seek(0); data = buf.read(); buf.seek(0); buf.truncate(0)
            yield data

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=withdrawals.csv"},
    )


@router.get("/admin/api/withdrawals/{withdrawal_id}")
async def get_withdrawal_detail(
    withdrawal_id: str,
    _session=Depends(require_session),
):
    """Full withdrawal detail including event log and gateway responses."""
    from server.utils.database.withdrawaldb import gateway_requestsdb, gateway_responsesdb

    wit = await get_withdrawal_record(withdrawal_id)
    if not wit:
        return JSONResponse({"error": "Not found"}, status_code=404)

    logs = await get_withdrawal_logs(withdrawal_id)

    # Fetch gateway requests/responses for this withdrawal
    gw_requests = await gateway_requestsdb.find(
        {"withdrawal_id": withdrawal_id}, {"_id": 0}
    ).sort("created_at", 1).to_list(length=50)
    gw_responses = await gateway_responsesdb.find(
        {"withdrawal_id": withdrawal_id}, {"_id": 0}
    ).sort("created_at", 1).to_list(length=50)

    return JSONResponse({
        "withdrawal": _ser(dict(wit)),
        "logs":       [_ser(l) for l in logs],
        "gateway_requests":  [_ser(r) for r in gw_requests],
        "gateway_responses": [_ser(r) for r in gw_responses],
    })


@router.post("/admin/api/withdrawals/{withdrawal_id}/approve")
async def approve_withdrawal_route(
    withdrawal_id: str,
    request: Request,
    _session=Depends(require_session),
):
    """
    Manually approve a pending withdrawal.
    Used in manual mode or as an admin override.
    Optionally accepts { tx_hash, note } in request body.
    """
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    tx_hash    = (body.get("tx_hash")    or "").strip()
    admin_note = (body.get("note")       or "").strip()

    svc    = get_withdrawal_service()
    result = await svc.admin_approve(withdrawal_id, tx_hash=tx_hash, admin_note=admin_note)
    ok     = result["ok"]

    await log_action(
        "payment", "withdrawal_approved",
        ip=client_ip(request), target=withdrawal_id, ok=ok,
        detail=f"tx_hash={tx_hash} note={admin_note}",
    )
    return JSONResponse({"ok": ok, "status": result.get("status"), "error": result.get("error")})


@router.post("/admin/api/withdrawals/{withdrawal_id}/reject")
async def reject_withdrawal_route(
    withdrawal_id: str,
    request: Request,
    _session=Depends(require_session),
):
    """
    Reject a pending withdrawal. Releases reserved balance to user.
    Accepts { reason } in request body.
    """
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    reason = (body.get("reason") or body.get("note") or "").strip()

    svc    = get_withdrawal_service()
    result = await svc.admin_reject(withdrawal_id, reason=reason)
    ok     = result["ok"]

    await log_action(
        "payment", "withdrawal_rejected",
        ip=client_ip(request), target=withdrawal_id, ok=ok,
        detail=f"reason={reason}",
    )
    return JSONResponse({"ok": ok, "status": result.get("status"), "error": result.get("error")})


@router.post("/admin/api/withdrawals/{withdrawal_id}/verify")
async def verify_withdrawal_admin(
    withdrawal_id: str,
    request: Request,
    _session=Depends(require_session),
):
    """Force-poll the gateway for the latest status of a withdrawal."""
    svc    = get_withdrawal_service()
    result = await svc.verify_withdrawal(withdrawal_id)

    await log_action(
        "payment", "withdrawal_verified",
        ip=client_ip(request), target=withdrawal_id, ok=result.get("ok", False),
        detail=f"status={result.get('status')} tx_hash={result.get('tx_hash')}",
    )
    return JSONResponse(result)


@router.post("/admin/api/withdrawals/{withdrawal_id}/retry")
async def retry_withdrawal_gateway(
    withdrawal_id: str,
    request: Request,
    _session=Depends(require_session),
):
    """
    Re-submit a failed/pending withdrawal to the gateway.
    Useful when the gateway was temporarily unavailable.
    """
    from server.utils.database.withdrawaldb import (
        get_withdrawal_record as _get,
        update_withdrawal_status,
    )
    from server.utils.withdrawal_statuses import WithdrawalStatus
    from server.services.withdrawal import get_registry

    wit = await _get(withdrawal_id)
    if not wit:
        return JSONResponse({"ok": False, "error": "Withdrawal not found"}, status_code=404)

    if wit.get("status") in WithdrawalStatus.FINAL:
        return JSONResponse({"ok": False, "error": f"Already in final state: {wit['status']}"})

    provider_id = wit.get("gateway", "oxapay")
    if provider_id == "manual":
        return JSONResponse({"ok": False, "error": "Manual mode withdrawal — use approve/reject"})

    registry = get_registry()
    provider = registry.get(provider_id)
    if not provider or not provider.is_configured():
        return JSONResponse({"ok": False, "error": f"Provider '{provider_id}' not configured"})

    # Reset to pending so submit flow runs clean
    await update_withdrawal_status(withdrawal_id, WithdrawalStatus.PENDING)
    await log_withdrawal_event(withdrawal_id, "admin_retry", {"admin_ip": client_ip(request)})

    svc = get_withdrawal_service()
    # Trigger the retry via verify which will detect no track_id and re-submit
    result = await svc.verify_withdrawal(withdrawal_id)

    await log_action(
        "payment", "withdrawal_retry",
        ip=client_ip(request), target=withdrawal_id, ok=True,
        detail=f"new_status={result.get('status')}",
    )
    return JSONResponse({"ok": True, "status": result.get("status"), "message": result.get("message", "")})


# ══════════════════════════════════════════════════════════════════════════════
# TRANSACTIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/api/transactions")
async def list_transactions(
    _session=Depends(require_session),
    txn_type: Optional[str] = Query(None),
    user_id:  Optional[int] = Query(None),
    page:     int           = Query(1, ge=1),
    limit:    int           = Query(50, ge=1, le=200),
):
    query: dict = {}
    if txn_type:
        query["type"] = txn_type
    if user_id:
        query["user_id"] = user_id

    skip  = (page - 1) * limit
    total = await transactionsdb.count_documents(query)
    cursor = transactionsdb.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
    docs   = [_ser(d) async for d in cursor]
    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


@router.get("/admin/api/transactions/export")
async def export_transactions(
    _session=Depends(require_session),
    txn_type: Optional[str] = Query(None),
    user_id:  Optional[int] = Query(None),
):
    query: dict = {}
    if txn_type:
        query["type"] = txn_type
    if user_id:
        query["user_id"] = user_id
    cursor = transactionsdb.find(query, {"_id": 0}).sort("created_at", -1)
    docs   = [_ser(d) async for d in cursor]
    headers = ["txn_id", "user_id", "type", "amount", "ref_id", "note", "created_at"]

    def generate():
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for d in docs:
            writer.writerow(d)
            buf.seek(0); data = buf.read(); buf.seek(0); buf.truncate(0)
            yield data

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"},
    )
