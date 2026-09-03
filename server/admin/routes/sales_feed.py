"""Admin routes for Sales Feed and isolated Fake Sales configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.configdb import set_setting
from server.utils.database.auditdb import log_action
from server.web.security import client_ip
from server.core import memstore

router = APIRouter(tags=["Admin-SalesFeed"], include_in_schema=False)

# ── Settings keys managed here ────────────────────────────────────────────────
_FEED_BOOL_KEYS    = {"sales_feed_enabled", "sales_feed_silent", "fake_sales_enabled"}
_FEED_INT_KEYS     = {"sales_feed_delay_seconds", "fake_sales_interval_min",
                      "fake_sales_interval_max", "fake_sales_randomization_level"}
_FEED_STRING_KEYS  = {"sales_feed_chat_id"}
_FEED_LIST_KEYS    = {"fake_sales_product_pool", "fake_sales_country_pool"}
_ALL_FEED_KEYS     = _FEED_BOOL_KEYS | _FEED_INT_KEYS | _FEED_STRING_KEYS | _FEED_LIST_KEYS


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/admin/sales-feed", response_class=HTMLResponse)
async def sales_feed_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/sales_feed.html", {"page": "sales_feed"})


# ── GET settings ──────────────────────────────────────────────────────────────

@router.get("/admin/api/sales-feed/settings")
async def get_sales_feed_settings(_session=Depends(require_session)):
    s = memstore.settings
    return JSONResponse({
        "sales_feed_enabled":           bool(s.get("sales_feed_enabled",          False)),
        "sales_feed_chat_id":           str(s.get("sales_feed_chat_id",           "") or ""),
        "sales_feed_silent":            bool(s.get("sales_feed_silent",            False)),
        "sales_feed_delay_seconds":     int(s.get("sales_feed_delay_seconds",      0)),
        "fake_sales_enabled":           bool(s.get("fake_sales_enabled",           False)),
        "fake_sales_interval_min":      int(s.get("fake_sales_interval_min",       300)),
        "fake_sales_interval_max":      int(s.get("fake_sales_interval_max",       900)),
        "fake_sales_randomization_level": int(s.get("fake_sales_randomization_level", 5)),
        "fake_sales_product_pool":      list(s.get("fake_sales_product_pool",      ["account", "session"])),
        "fake_sales_country_pool":      list(s.get("fake_sales_country_pool",      [])),
    })


@router.get("/admin/api/sales-feed/recent")
async def get_recent_completed_sales(_session=Depends(require_session)):
    """Return recent real completed orders eligible for Sales Feed visibility."""
    from server.utils.database.orderdb import ordersdb

    cursor = ordersdb.find(
        {"status": "completed"},
        {"_id": 0, "order_id": 1, "buyer_id": 1, "amount": 1, "fee": 1, "completed_at": 1},
    ).sort("completed_at", -1).limit(8)
    items = []
    async for doc in cursor:
        completed_at = doc.get("completed_at")
        items.append({
            "order_id": str(doc.get("order_id") or ""),
            "buyer_id": doc.get("buyer_id"),
            "amount": doc.get("amount") or 0,
            "fee": doc.get("fee") or 0,
            "completed_at": completed_at.isoformat() if hasattr(completed_at, "isoformat") else completed_at,
        })
    return JSONResponse({"items": items})


# ── POST settings ─────────────────────────────────────────────────────────────

@router.post("/admin/api/sales-feed/settings")
async def update_sales_feed_settings(request: Request, _session=Depends(require_session)):
    body = await request.json()
    updated = []

    for key in _FEED_BOOL_KEYS:
        if key in body:
            await set_setting(key, bool(body[key]))
            updated.append(key)

    for key in _FEED_INT_KEYS:
        if key in body:
            try:
                val = int(body[key])
                if key == "fake_sales_randomization_level":
                    val = max(1, min(10, val))
                elif key in ("fake_sales_interval_min", "fake_sales_interval_max"):
                    val = max(30, val)
                elif key == "sales_feed_delay_seconds":
                    val = max(0, val)
                await set_setting(key, val)
                updated.append(key)
            except (TypeError, ValueError):
                pass

    for key in _FEED_STRING_KEYS:
        if key in body:
            await set_setting(key, str(body[key] or "").strip())
            updated.append(key)

    for key in _FEED_LIST_KEYS:
        if key in body and isinstance(body[key], list):
            await set_setting(key, body[key])
            updated.append(key)

    await log_action(
        "settings", "sales_feed_settings_updated",
        ip=client_ip(request),
        detail=", ".join(updated),
    )
    return JSONResponse({"ok": True, "updated": updated})


# ── Test real feed ────────────────────────────────────────────────────────────

@router.post("/admin/api/sales-feed/test")
async def test_sales_feed(request: Request, _session=Depends(require_session)):
    from server.services.sales_feed import sales_feed_service
    ok, reason = await sales_feed_service.send_test()
    await log_action(
        "settings", "sales_feed_test_sent",
        ip=client_ip(request),
        ok=ok,
        detail=reason if not ok else "",
    )
    if ok:
        return JSONResponse({"ok": True, "message": "Test notification sent successfully."})
    return JSONResponse({"ok": False, "error": reason or "Unknown error."}, status_code=400)


@router.post("/admin/api/sales-feed/fake-preview")
async def fake_sales_preview(request: Request, _session=Depends(require_session)):
    from server.services.sales_feed import sales_feed_service
    ok, reason = await sales_feed_service.send_fake_preview()
    await log_action(
        "settings", "fake_sales_preview_sent",
        ip=client_ip(request),
        ok=ok,
        detail=reason if not ok else "",
    )
    if ok:
        return JSONResponse({"ok": True, "message": "Fake sale preview sent successfully."})
    return JSONResponse({"ok": False, "error": reason or "Unknown error."}, status_code=400)
