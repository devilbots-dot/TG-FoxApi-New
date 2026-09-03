"""Order and sell-request management routes."""

from typing import Optional
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import csv
import io
from datetime import datetime

from server.admin import templates
from server.admin.deps import require_session
from server.logging import LOGGER
from server.utils.database.orderdb import (
    ordersdb,
    complete_order,
    cancel_order,
    refund_order,
    get_order,
)
from server.utils.database.walletdb import log_transaction
from server.utils.database.userdb import update_balance
from server.utils.database.sellrequestdb import (
    sellrequestsdb,
    get_sell_request,
)
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

_log = LOGGER(__name__)

router = APIRouter(tags=["Admin-Orders"], include_in_schema=False)


# Every datetime-typed field on an order doc must be listed here, otherwise
# JSONResponse can't encode it and the whole list endpoint 500s.
_DATETIME_FIELDS = (
    "created_at",
    "completed_at",
    "cancelled_at",
    "delivered_at",
)


def _ser(doc: dict) -> dict:
    """Serialize datetimes in a document."""
    for k in _DATETIME_FIELDS:
        v = doc.get(k)
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


@router.get("/admin/orders", response_class=HTMLResponse)
async def orders_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/orders.html", {"page": "orders"})


@router.get("/admin/api/orders")
async def list_orders(
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
        import re
        q_raw = q.strip()
        q_esc = re.escape(q_raw)
        or_clauses = [
            {"order_id":   {"$regex": q_esc, "$options": "i"}},
            {"account_id": {"$regex": q_esc, "$options": "i"}},
        ]
        try:
            qint = int(q_raw)
            or_clauses.extend([
                {"buyer_id":  qint},
                {"seller_id": qint},
            ])
        except ValueError:
            pass
        query["$or"] = or_clauses

    skip = (page - 1) * limit
    total = await ordersdb.count_documents(query)
    cursor = ordersdb.find(query, {"_id": 0}).sort("created_at", -1).skip(skip).limit(limit)
    docs = [_ser(d) async for d in cursor]
    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


@router.get("/admin/api/orders/export")
async def export_orders(
    _session=Depends(require_session),
    status: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """Export all matching orders as CSV."""
    query: dict = {}
    if status:
        query["status"] = status
    if q:
        import re
        q_raw = q.strip()
        q_esc = re.escape(q_raw)
        or_clauses = [
            {"order_id":   {"$regex": q_esc, "$options": "i"}},
            {"account_id": {"$regex": q_esc, "$options": "i"}},
        ]
        try:
            qint = int(q_raw)
            or_clauses.extend([
                {"buyer_id":  qint},
                {"seller_id": qint},
            ])
        except ValueError:
            pass
        query["$or"] = or_clauses

    cursor = ordersdb.find(query, {"_id": 0}).sort("created_at", -1)
    docs = [_ser(d) async for d in cursor]
    headers = ["order_id", "buyer_id", "seller_id", "amount", "fee", "status", "created_at", "completed_at"]

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
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


@router.post("/admin/api/orders/{order_id}/complete")
async def order_complete(order_id: str, request: Request, _session=Depends(require_session)):
    ok = await complete_order(order_id)
    await log_action("order", "order_completed", ip=client_ip(request), target=order_id, ok=ok)
    return JSONResponse({"ok": ok})


@router.post("/admin/api/orders/{order_id}/cancel")
async def order_cancel(order_id: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    reason = body.get("reason", "Admin cancelled")
    order = await get_order(order_id)
    cancelled = await cancel_order(order_id, reason)
    await log_action("order", "order_cancelled", ip=client_ip(request), target=order_id, detail=reason, ok=cancelled)
    if cancelled and order and order.get("amount", 0) > 0:
        buyer_id = order.get("buyer_id")
        amount = order["amount"]
        await update_balance(buyer_id, amount)
        await log_transaction(
            user_id=buyer_id, txn_type="refund", amount=amount,
            ref_id=order_id, note=f"Admin cancelled order: {reason}",
        )
        try:
            from server.utils.notifications import notify
            await notify(buyer_id, "order_cancelled_refund",
                         order_id=order_id, amount=amount, reason=reason)
        except Exception:
            pass
    return JSONResponse({"ok": cancelled})


@router.post("/admin/api/orders/{order_id}/refund")
async def order_refund(order_id: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    reason = body.get("reason", "Admin refunded")
    order = await get_order(order_id)
    if not order:
        return JSONResponse({"ok": False, "detail": "Order not found"}, status_code=404)
    ok = await refund_order(order_id, reason)
    if ok:
        buyer_id = order["buyer_id"]
        amount = order["amount"]
        await update_balance(buyer_id, amount)
        await log_transaction(
            user_id=buyer_id, txn_type="refund", amount=amount,
            ref_id=order_id, note=f"Refund for order {order_id}: {reason}",
        )
        try:
            from server.utils.notifications import notify
            await notify(buyer_id, "order_cancelled_refund",
                         order_id=order_id, amount=amount, reason=reason)
        except Exception:
            pass
    await log_action("order", "order_refunded", ip=client_ip(request), target=order_id, detail=reason, ok=ok)
    return JSONResponse({"ok": ok})


# ── Sell Requests ─────────────────────────────────────────────────────────────────

@router.get("/admin/sell-requests", response_class=HTMLResponse)
async def sell_requests_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/sell_requests.html", {"page": "sell_requests"})


@router.get("/admin/api/sell-requests")
async def list_sell_requests(
    _session=Depends(require_session),
    status: Optional[str] = Query(None),
    lifecycle_status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    query: dict = {}
    if status:
        query["status"] = status
    if lifecycle_status:
        query["lifecycle_status"] = lifecycle_status
    skip = (page - 1) * limit
    total = await sellrequestsdb.count_documents(query)
    # Exclude heavy binary field so the response is fast (session_bytes_enc
    # can be hundreds of KB per row; it is only needed by the retry worker).
    projection = {"_id": 0, "session_bytes_enc": 0}
    cursor = sellrequestsdb.find(query, projection).sort("submitted_at", -1).skip(skip).limit(limit)
    docs = []
    async for d in cursor:
        # Serialize every datetime field generically so unknown datetime fields
        # (retry_at, payment_release_at, etc.) don't 500 the whole endpoint.
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        docs.append(d)
    return JSONResponse({"total": total, "page": page, "limit": limit, "items": docs})


@router.get("/admin/api/sell-requests/{request_id}/validate")
async def validate_sell_request_session(
    request_id: str,
    request: Request,
    _session=Depends(require_session),
):
    """
    Run live Telethon session verification for a sell request.
    Downloads session from channel, checks authorization, other-session count,
    and spam status. Returns a structured VerificationResult dict.
    This endpoint is intentionally slow (network I/O to Telegram DC) — frontend
    should show a loading indicator while it runs.
    """
    from server.services.sell_verifier import verify_sell_session

    req = await get_sell_request(request_id)
    if not req:
        return JSONResponse({"ok": False, "error": "Sell request not found"}, status_code=404)

    if req.get("status") != "pending":
        return JSONResponse({
            "ok": False,
            "error": f"Request is already '{req.get('status')}' — can only validate pending requests.",
        }, status_code=400)

    result = await verify_sell_session(req)
    await log_action(
        "payment", "sell_request_validated",
        ip=client_ip(request), target=request_id,
        ok=result.verified,
        detail=f"verified={result.verified} fails={result.failed_checks}",
    )
    return JSONResponse({"ok": True, **result.to_dict()})


@router.post("/admin/api/sell-requests/{request_id}/approve")
async def sell_approve(request_id: str, request: Request, _session=Depends(require_session)):
    """
    Approve a sell request.

    Body:
      final_price: float    — the price to pay the seller
      note: str             — admin note (optional)
      force: bool           — bypass session validation failures (default: false)
                            If false and session fails validation, returns 422 with
                            {"validation_failed": true, "verification": {...}}.
                            If true, approves despite failures (noted in admin_note).

    User notification is handled inside finalize_sell_approval — no duplication here.
    """
    from server.services.sell_ops import finalize_sell_approval

    body = await request.json()
    final_price = float(body.get("final_price") or 0)
    note  = body.get("note") or "Approved by admin via web panel"
    force = bool(body.get("force", False))

    if final_price <= 0:
        return JSONResponse({"ok": False, "detail": "Enter a valid price > 0"}, status_code=400)

    # ── Pre-approval session validation (unless force=True) ───────────────────
    if not force:
        try:
            from server.services.sell_verifier import verify_sell_session
            req = await get_sell_request(request_id)
            if req and req.get("status") == "pending":
                verification = await verify_sell_session(req)
                if not verification.verified and not verification.inconclusive:
                    _log.warning(
                        "sell_approve: validation FAILED for %s (force=False) — blocking approve. Reasons: %s",
                        request_id, verification.reasons,
                    )
                    await log_action(
                        "payment", "sell_request_approve_blocked",
                        ip=client_ip(request), target=request_id,
                        ok=False,
                        detail=f"validation_failed: {'; '.join(verification.reasons)}",
                    )
                    return JSONResponse({
                        "ok": False,
                        "validation_failed": True,
                        "detail": "Session verification failed. Use Force Approve to override.",
                        "verification": verification.to_dict(),
                    }, status_code=422)
        except Exception as exc:
            _log.warning("sell_approve: validation error for %s — proceeding without: %s", request_id, exc)

    # ── Append force-override note if applicable ──────────────────────────────
    if force:
        note = (note.rstrip() or "Admin force-approved") + " [FORCE OVERRIDE: validation bypassed]"

    doc = await finalize_sell_approval(request_id, final_price, note=note)
    ok = doc is not None

    await log_action(
        "payment",
        "sell_request_force_approved" if force else "sell_request_approved",
        ip=client_ip(request), target=request_id, ok=ok,
    )
    return JSONResponse({"ok": ok, "detail": None if ok else "Not found or already processed"})


@router.post("/admin/api/sell-requests/{request_id}/reject")
async def sell_reject(request_id: str, request: Request, _session=Depends(require_session)):
    """Reject a sell request via the single shared finalization path (sell_ops).
    User notification is handled inside finalize_sell_rejection — no duplication here.
    """
    from server.services.sell_ops import finalize_sell_rejection

    body = await request.json()
    note = body.get("note") or "Rejected by admin"
    doc  = await finalize_sell_rejection(request_id, note=note)
    ok   = doc is not None

    await log_action("payment", "sell_request_rejected", ip=client_ip(request), target=request_id, ok=ok)
    return JSONResponse({"ok": ok})
