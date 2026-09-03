"""Country management routes."""

from typing import Optional
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.countrydb import (
    countriesdb,
    get_all_countries,
    get_country,
    upsert_country,
    set_country_field,
    reorder_country_rank,
    delete_country,
    get_next_rank,
    set_country_stock_mode,
    bulk_hide_real_stock_except,
    bulk_update_countries,
)
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Countries"], include_in_schema=False)


@router.get("/admin/countries", response_class=HTMLResponse)
async def countries_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/countries.html", {"page": "countries"})


@router.get("/admin/api/countries")
async def list_countries(_session=Depends(require_session)):
    countries = await get_all_countries()
    # Add real stock count from session_accounts
    from server.utils.database.sessiondb import sessionaccountsdb
    pipeline = [
        {"$match": {"sold": False}},
        {"$group": {"_id": "$country_code", "real_stock": {"$sum": 1}}},
    ]
    real_stocks = {}
    async for doc in sessionaccountsdb.aggregate(pipeline):
        real_stocks[doc["_id"]] = doc["real_stock"]
    for c in countries:
        c["real_stock"] = real_stocks.get(c.get("code", ""), 0)
        c["fake_stock"] = c.get("fake_stock", 0)
        c["stock_mode"] = c.get("stock_mode", "real")
        if c.get("updated_at"):
            v = c["updated_at"]
            c["updated_at"] = v.isoformat() if hasattr(v, "isoformat") else str(v)
    return JSONResponse({"items": countries})


@router.post("/admin/api/countries")
async def upsert_country_route(request: Request, _session=Depends(require_session)):
    body = await request.json()
    code = body.get("code", "").upper()
    if not code:
        return JSONResponse({"ok": False, "detail": "code required"}, status_code=400)

    desired_rank = body.get("country_rank")

    # For new countries with no rank specified, place at end
    if not desired_rank:
        desired_rank = await get_next_rank()

    desired_rank = int(desired_rank)

    # Save all fields (rank stored temporarily; reorder_country_rank will fix it)
    await upsert_country(
        code=code,
        country_name=body.get("country_name", code),
        country_rank=desired_rank,
        idc=body.get("idc", ""),
        price=float(body.get("price", 0.0)),
        sell_price=float(body.get("sell_price", 0.0)),
        temp_disable=bool(body.get("temp_disable", False)),
        is_full=bool(body.get("is_full", False)),
        fake_stock=int(body.get("fake_stock", 0) or 0),
        stock_mode=body.get("stock_mode", "real"),
        # per-country spam config
        accept_clean=bool(body.get("accept_clean", True)),
        accept_temp_spam=bool(body.get("accept_temp_spam", True)),
        accept_perm_spam=bool(body.get("accept_perm_spam", False)),
        price_clean=float(body.get("price_clean", 0.0) or 0.0),
        price_temp_spam=float(body.get("price_temp_spam", 0.0) or 0.0),
        price_perm_spam=float(body.get("price_perm_spam", 0.0) or 0.0),
        termination_delay_hours=int(body.get("termination_delay_hours", 24) or 24),
    )

    # Re-order all countries so ranks are unique and sequential
    await reorder_country_rank(code, desired_rank)

    await log_action("country", "country_upserted", ip=client_ip(request), target=code)
    return JSONResponse({"ok": True})


@router.delete("/admin/api/countries/{code}")
async def delete_country_route(code: str, request: Request, _session=Depends(require_session)):
    ok = await delete_country(code)
    await log_action("country", "country_deleted", ip=client_ip(request), target=code, ok=ok)
    return JSONResponse({"ok": ok})


@router.patch("/admin/api/countries/{code}/price")
async def set_price(code: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    price = round(float(body.get("price", 0)), 4)
    await set_country_field(code, "price", price)
    await log_action("country", "country_price_updated", ip=client_ip(request), target=code, detail=str(price))
    return JSONResponse({"ok": True})


@router.patch("/admin/api/countries/{code}/sell-price")
async def set_sell_price(code: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    sell_price = round(float(body.get("sell_price", 0)), 4)
    await set_country_field(code, "sell_price", sell_price)
    await log_action("country", "country_sell_price_updated", ip=client_ip(request), target=code, detail=str(sell_price))
    return JSONResponse({"ok": True})


@router.patch("/admin/api/countries/{code}/toggle-disable")
async def toggle_disable(code: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    val = bool(body.get("temp_disable", False))
    await set_country_field(code, "temp_disable", val)
    await log_action("country", "country_toggle_disable", ip=client_ip(request), target=code, detail=str(val))
    return JSONResponse({"ok": True})


@router.patch("/admin/api/countries/{code}/toggle-full")
async def toggle_full(code: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    val = bool(body.get("is_full", False))
    await set_country_field(code, "is_full", val)
    await log_action("country", "country_toggle_full", ip=client_ip(request), target=code, detail=str(val))
    return JSONResponse({"ok": True})


@router.patch("/admin/api/countries/{code}/rank")
async def set_rank(code: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    rank = int(body.get("rank", 1))
    await reorder_country_rank(code, rank)
    await log_action("country", "country_rank_updated", ip=client_ip(request), target=code, detail=str(rank))
    return JSONResponse({"ok": True})


@router.get("/admin/api/countries/next-rank")
async def next_rank(_session=Depends(require_session)):
    return JSONResponse({"next_rank": await get_next_rank()})


@router.patch("/admin/api/countries/{code}/fake-stock")
async def set_fake_stock(code: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    fake_stock = max(0, int(body.get("fake_stock", 0) or 0))
    await set_country_field(code, "fake_stock", fake_stock)
    await log_action("country", "country_fake_stock_updated", ip=client_ip(request), target=code, detail=str(fake_stock))
    return JSONResponse({"ok": True})


@router.patch("/admin/api/countries/{code}/stock-mode")
async def set_country_stock_mode_route(code: str, request: Request, _session=Depends(require_session)):
    """Switch this country's buyer-visible stock between real and fake."""
    body = await request.json()
    mode = body.get("stock_mode", "real")
    if mode not in ("real", "fake"):
        return JSONResponse({"ok": False, "detail": "stock_mode must be 'real' or 'fake'"}, status_code=400)
    await set_country_stock_mode(code, mode)
    await log_action("country", "country_stock_mode_changed", ip=client_ip(request), target=code, detail=mode)
    return JSONResponse({"ok": True, "stock_mode": mode})


@router.post("/admin/api/countries/hide-real-stock")
async def hide_real_stock_route(request: Request, _session=Depends(require_session)):
    """Switch every country except the selected keep-list to fake stock 0."""
    body = await request.json()
    keep = [c.upper() for c in (body.get("keep_codes") or [])]
    if not keep:
        return JSONResponse({"ok": False, "detail": "keep_codes required (at least one country to leave untouched)"}, status_code=400)
    count = await bulk_hide_real_stock_except(keep)
    await log_action("country", "bulk_hide_real_stock", ip=client_ip(request), detail=f"kept={keep} affected={count}")
    return JSONResponse({"ok": True, "affected": count, "kept": keep})

@router.post("/admin/api/countries/bulk-edit")
async def bulk_edit_countries_route(request: Request, _session=Depends(require_session)):
    """Apply the same field changes to multiple countries at once.
    Body: { codes: ["IN","US"], fields: { sell_price: 0.5, termination_delay_hours: 12, ... } }
    Only whitelisted fields are accepted; unspecified fields left untouched.
    """
    body = await request.json()
    codes = [c.upper() for c in (body.get("codes") or []) if c]
    fields = body.get("fields") or {}
    if not codes:
        return JSONResponse({"ok": False, "detail": "codes required"}, status_code=400)
    if not fields:
        return JSONResponse({"ok": False, "detail": "fields required"}, status_code=400)
    count = await bulk_update_countries(codes, fields)
    await log_action("country", "bulk_edit_countries", ip=client_ip(request), detail=f"codes={codes} fields={list(fields.keys())} affected={count}")
    return JSONResponse({"ok": True, "affected": count})
