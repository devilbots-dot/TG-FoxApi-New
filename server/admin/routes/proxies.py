"""Proxy management routes."""

import re
from typing import List
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.proxydb import (
    proxiesdb,
    add_proxy,
    delete_proxy,
    toggle_proxy,
    list_all_proxies,
    reset_proxy_fails,
    increment_proxy_fail,
    test_proxy_connection,
)
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Proxies"], include_in_schema=False)


_SUFFIX_RE = re.compile(r"_[A-Za-z]{2}$")


def _rewrite_username_for_country(username: str, country_code: str) -> str:
    """Replace trailing `_XX` country suffix in username with `_<CC>`.
    If no such suffix exists, append `_<CC>`."""
    cc = (country_code or "").upper()
    if not username:
        return username
    if _SUFFIX_RE.search(username):
        return _SUFFIX_RE.sub(f"_{cc}", username)
    return f"{username}_{cc}"


@router.get("/admin/proxies", response_class=HTMLResponse)
async def proxies_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/proxies.html", {"page": "proxies"})


@router.get("/admin/api/proxies")
async def list_proxies(_session=Depends(require_session)):
    proxies = await list_all_proxies()
    for p in proxies:
        if p.get("added_at"):
            p["added_at"] = p["added_at"].isoformat()
    return JSONResponse({"items": proxies})


@router.post("/admin/api/proxies")
async def add_proxy_route(request: Request, _session=Depends(require_session)):
    body = await request.json()
    try:
        # Validate the proxy actually works before saving it.
        test = await test_proxy_connection(
            host=body["host"],
            port=int(body["port"]),
            proxy_type=body.get("type", "socks5"),
            username=body.get("username", ""),
            password=body.get("password", ""),
        )
        if not test["ok"]:
            return JSONResponse(
                {"ok": False, "detail": f"Proxy test failed: {test['detail']}"},
                status_code=400,
            )

        proxy_id = await add_proxy(
            country_code=body.get("country_code", "*"),
            host=body["host"],
            port=int(body["port"]),
            proxy_type=body.get("type", "socks5"),
            username=body.get("username", ""),
            password=body.get("password", ""),
        )
        await log_action("proxy", "proxy_added", ip=client_ip(request), target=proxy_id)
        return JSONResponse({"ok": True, "proxy_id": proxy_id, "tested": True, "latency_ms": test["latency_ms"]})
    except (KeyError, ValueError) as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=400)


@router.post("/admin/api/proxies/bulk-universal")
async def bulk_universal_proxy(request: Request, _session=Depends(require_session)):
    """
    Universal proxy: for each selected country_code, rewrite the trailing
    `_XX` suffix in the username template with the country code and add
    the proxy. Tests each variant before saving. Returns per-country result.
    """
    body = await request.json()
    try:
        host = str(body["host"]).strip()
        port = int(body["port"])
        proxy_type = str(body.get("type", "socks5")).lower()
        username_tpl = str(body.get("username", ""))
        password = str(body.get("password", ""))
        country_codes: List[str] = [
            str(c).upper().strip() for c in (body.get("country_codes") or []) if str(c).strip()
        ]
        skip_test = bool(body.get("skip_test", False))
    except (KeyError, ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "detail": f"Invalid input: {e}"}, status_code=400)

    if not host or not port:
        return JSONResponse({"ok": False, "detail": "Host and port required"}, status_code=400)
    if not country_codes:
        return JSONResponse({"ok": False, "detail": "Select at least one country"}, status_code=400)

    results = []
    added = 0
    failed = 0

    for cc in country_codes:
        rewritten_user = _rewrite_username_for_country(username_tpl, cc)
        try:
            if skip_test:
                test = {"ok": True, "latency_ms": 0}
            else:
                test = await test_proxy_connection(
                    host=host, port=port, proxy_type=proxy_type,
                    username=rewritten_user, password=password,
                )
            if not test.get("ok"):
                failed += 1
                results.append({
                    "country_code": cc, "ok": False,
                    "username": rewritten_user,
                    "detail": test.get("detail", "test failed"),
                })
                continue

            proxy_id = await add_proxy(
                country_code=cc, host=host, port=port,
                proxy_type=proxy_type, username=rewritten_user, password=password,
            )
            added += 1
            results.append({
                "country_code": cc, "ok": True,
                "proxy_id": proxy_id, "username": rewritten_user,
                "latency_ms": test.get("latency_ms", 0),
            })
        except Exception as e:
            failed += 1
            results.append({
                "country_code": cc, "ok": False,
                "username": rewritten_user, "detail": str(e),
            })

    await log_action(
        "proxy", "proxy_bulk_universal",
        ip=client_ip(request),
        detail=f"added={added} failed={failed} total={len(country_codes)}",
    )
    return JSONResponse({
        "ok": True, "added": added, "failed": failed,
        "total": len(country_codes), "results": results,
    })


@router.delete("/admin/api/proxies/{proxy_id}")
async def delete_proxy_route(proxy_id: str, request: Request, _session=Depends(require_session)):
    ok = await delete_proxy(proxy_id)
    await log_action("proxy", "proxy_deleted", ip=client_ip(request), target=proxy_id, ok=ok)
    return JSONResponse({"ok": ok})


@router.patch("/admin/api/proxies/{proxy_id}/toggle")
async def toggle_proxy_route(proxy_id: str, request: Request, _session=Depends(require_session)):
    body = await request.json()
    is_active = bool(body.get("is_active", True))
    ok = await toggle_proxy(proxy_id, is_active)
    await log_action("proxy", "proxy_toggled", ip=client_ip(request), target=proxy_id, detail=str(is_active), ok=ok)
    return JSONResponse({"ok": ok})


@router.post("/admin/api/proxies/{proxy_id}/test")
async def test_proxy(proxy_id: str, request: Request, _session=Depends(require_session)):
    proxy = await proxiesdb.find_one({"proxy_id": proxy_id}, {"_id": 0})
    if not proxy:
        return JSONResponse({"ok": False, "detail": "Proxy not found"}, status_code=404)

    result = await test_proxy_connection(
        host=proxy["host"],
        port=int(proxy["port"]),
        proxy_type=proxy.get("type", "socks5"),
        username=proxy.get("username", ""),
        password=proxy.get("password", ""),
    )
    if result["ok"]:
        await reset_proxy_fails(proxy_id)
    else:
        await increment_proxy_fail(proxy_id)
    return JSONResponse(result)
