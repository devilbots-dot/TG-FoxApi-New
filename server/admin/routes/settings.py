"""System settings routes."""

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, Depends, Path
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.configdb import get_all_settings, get_setting, set_setting, DEFAULTS, coerce_setting_value
from server.utils.database.auditdb import log_action
from server.utils.database.envdb import (
    get_all_custom_env,
    set_custom_env,
    delete_custom_env,
)
from server.web.security import client_ip
from server.logging import LOGGER

router = APIRouter(tags=["Admin-Settings"], include_in_schema=False)

_ENV_DISPLAY = [
    "API_ID", "API_HASH", "BOT_TOKEN", "MONGO_DB_URI",
    "OWNER_ID", "ADMIN_PASSWORD", "SESSION_CHANNEL_ID", "SESSION_SECRET",
    "LOG_GROUP_ID", "OXAPAY_MERCHANT_KEY", "OXAPAY_PAYOUT_KEY", "ADMIN_SECRET_PATH",
    "BOT_USERNAME", "API_PORT",
]

_BOOL_KEYS = {
    "maintenance_mode", "deposits_enabled", "withdrawals_enabled",
    "sell_requests_enabled", "registration_enabled",
    "session_selling_enabled",
    "auto_transfer_on_approval",
    "auto_transfer_on_payment_release",
    "hide_fake_stock_when_real_zero",
}
_NUMERIC_KEYS = {
    "low_stock_threshold", "order_timeout_minutes", "platform_fee_percent",
}
_STRING_KEYS = {
    "session_selling_min_rank",
}
_TOGGLE_KEYS = _BOOL_KEYS | _NUMERIC_KEYS | _STRING_KEYS


def _rank_names() -> list:
    try:
        from server.utils.database.userdb import RANK_THRESHOLDS
        return list(RANK_THRESHOLDS.keys())
    except Exception:
        return ["VIP1", "VIP2", "VIP3", "PREMIUM", "DIAMOND"]


def _rank_min_spend_defaults() -> dict:
    """Baseline min-spend per rank (from RANK_THRESHOLDS)."""
    try:
        from server.utils.database.userdb import RANK_THRESHOLDS
        return {rn: float((meta or {}).get("min_spend", 0) or 0)
                for rn, meta in RANK_THRESHOLDS.items()}
    except Exception:
        return {"VIP1": 0.0, "VIP2": 100.0, "VIP3": 500.0,
                "PREMIUM": 2000.0, "DIAMOND": 10000.0}


_RESTART_REQUIRED_KEYS = {"API_ID", "API_HASH", "BOT_TOKEN", "MONGO_DB_URI",
                          "OWNER_ID", "SESSION_CHANNEL_ID", "SESSION_SECRET",
                          "LOG_GROUP_ID", "OXAPAY_MERCHANT_KEY", "OXAPAY_PAYOUT_KEY",
                          "ADMIN_PASSWORD", "ADMIN_SECRET_PATH", "BOT_USERNAME", "API_PORT"}


@router.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/settings.html", {"page": "settings"})


@router.get("/admin/api/settings")
async def get_settings(_session=Depends(require_session)):
    settings = await get_all_settings()
    configurable = {k: v for k, v in settings.items() if k in _TOGGLE_KEYS}
    env_status = {k: ("set" if os.getenv(k) else "not_set") for k in _ENV_DISPLAY}
    return JSONResponse({
        "settings": configurable,
        "bool_keys": list(_BOOL_KEYS),
        "numeric_keys": list(_NUMERIC_KEYS),
        "string_keys": list(_STRING_KEYS),
        "env": env_status,
        "defaults": DEFAULTS,
        "restart_required_env": list(_RESTART_REQUIRED_KEYS),
        "ranks": _rank_names(),
        "rank_min_spend_defaults": _rank_min_spend_defaults(),
    })


@router.post("/admin/api/settings")
async def update_settings(request: Request, _session=Depends(require_session)):
    body = await request.json()
    updated = []
    for key, value in body.items():
        if key not in _TOGGLE_KEYS:
            continue
        value = coerce_setting_value(key, value)
        await set_setting(key, value)
        updated.append(key)
    await log_action("settings", "settings_updated", ip=client_ip(request), detail=", ".join(updated))
    return JSONResponse({"ok": True, "updated": updated})


# ── Rank Discounts (keyed by rank NAME: VIP1/VIP2/VIP3/…) ───────────────

@router.get("/admin/api/rank-discounts")
async def get_rank_discounts(_session=Depends(require_session)):
    data = await get_setting("rank_discounts") or {}
    ranks = _rank_names()
    tiers = []
    if isinstance(data, dict):
        for rn in ranks:
            if rn in data:
                try:
                    tiers.append({"rank": rn, "percent": float(data[rn])})
                except (TypeError, ValueError):
                    continue
    return JSONResponse({"tiers": tiers, "ranks": ranks})


@router.post("/admin/api/rank-discounts")
async def save_rank_discounts(request: Request, _session=Depends(require_session)):
    """Body: {"tiers": [{"rank": "VIP1", "percent": 0}, ...]}"""
    body = await request.json()
    tiers = body.get("tiers") or []
    valid_ranks = set(_rank_names())
    out = {}
    for t in tiers:
        rn = str(t.get("rank") or "").strip().upper()
        if rn not in valid_ranks:
            continue
        try:
            pct = float(t.get("percent", 0))
        except (TypeError, ValueError):
            continue
        if pct < 0 or pct > 100:
            continue
        out[rn] = pct
    await set_setting("rank_discounts", out)
    await log_action("settings", "rank_discounts_updated", ip=client_ip(request), detail=str(out))
    return JSONResponse({"ok": True, "tiers": out})


# ── Country Discounts ──────────────────────────────────────────────────────

def _iso_now():
    return datetime.now(timezone.utc)


@router.get("/admin/api/country-discounts")
async def get_country_discounts(_session=Depends(require_session)):
    data = await get_setting("country_discounts") or {}
    items = []
    now = _iso_now()
    if isinstance(data, dict):
        for cc, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                pct = float(entry.get("percent", 0))
            except (TypeError, ValueError):
                pct = 0.0
            exp_raw = entry.get("expires_at")
            exp = None
            try:
                if exp_raw:
                    s = str(exp_raw).replace("Z", "+00:00")
                    exp = datetime.fromisoformat(s)
                    if not exp.tzinfo:
                        exp = exp.replace(tzinfo=timezone.utc)
            except Exception:
                exp = None
            active = bool(exp and exp > now)
            items.append({
                "country_code": str(cc).upper(),
                "percent": pct,
                "expires_at": exp.isoformat() if exp else None,
                "active": active,
                "remaining_seconds": int((exp - now).total_seconds()) if active else 0,
            })
    items.sort(key=lambda x: (not x["active"], x["country_code"]))
    return JSONResponse({"items": items})


@router.post("/admin/api/country-discounts")
async def upsert_country_discount(request: Request, _session=Depends(require_session)):
    """Body: {country_code, percent, duration_hours}"""
    body = await request.json()
    cc = str(body.get("country_code") or "").strip().upper()
    try:
        pct = float(body.get("percent", 0))
        hours = float(body.get("duration_hours", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Invalid number"}, status_code=400)
    if not cc or len(cc) < 2 or pct < 0 or pct > 100 or hours <= 0:
        return JSONResponse({"ok": False, "error": "Invalid input"}, status_code=400)

    current = await get_setting("country_discounts") or {}
    if not isinstance(current, dict):
        current = {}
    expires = _iso_now() + timedelta(hours=hours)
    current = dict(current)
    current[cc] = {"percent": pct, "expires_at": expires.isoformat()}
    await set_setting("country_discounts", current)
    await log_action("settings", "country_discount_set", ip=client_ip(request),
                     target=cc, detail=f"{pct}% for {hours}h")
    return JSONResponse({"ok": True, "country_code": cc, "percent": pct,
                         "expires_at": expires.isoformat()})


@router.delete("/admin/api/country-discounts/{cc}")
async def delete_country_discount(request: Request, cc: str = Path(...),
                                _session=Depends(require_session)):
    code = cc.strip().upper()
    current = await get_setting("country_discounts") or {}
    if not isinstance(current, dict) or code not in current:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    current = dict(current)
    current.pop(code, None)
    await set_setting("country_discounts", current)
    await log_action("settings", "country_discount_removed", ip=client_ip(request),
                     target=code)
    return JSONResponse({"ok": True})



# ── Rank Min Spend (per-rank required $ spend) ────────────────────────

@router.get("/admin/api/rank-min-spend")
async def get_rank_min_spend(_session=Depends(require_session)):
    data = await get_setting("rank_min_spend") or {}
    defaults = _rank_min_spend_defaults()
    ranks = _rank_names()
    effective = dict(defaults)
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                effective[str(k).upper()] = float(v)
            except (TypeError, ValueError):
                continue
    tiers = [{"rank": rn,
              "required_spend": float(effective.get(rn, 0)),
              "is_override": isinstance(data, dict) and rn in data,
              "default": float(defaults.get(rn, 0))}
             for rn in ranks]
    return JSONResponse({"tiers": tiers, "ranks": ranks, "defaults": defaults})


@router.post("/admin/api/rank-min-spend")
async def save_rank_min_spend(request: Request, _session=Depends(require_session)):
    """Body: {"tiers": [{"rank": "VIP2", "required_spend": 30}, ...]}"""
    body = await request.json()
    tiers = body.get("tiers") or []
    valid_ranks = set(_rank_names())
    out = {}
    for t in tiers:
        rn = str(t.get("rank") or "").strip().upper()
        if rn not in valid_ranks:
            continue
        try:
            val = float(t.get("required_spend", 0))
        except (TypeError, ValueError):
            continue
        if val < 0:
            continue
        out[rn] = val
    await set_setting("rank_min_spend", out)
    await log_action("settings", "rank_min_spend_updated", ip=client_ip(request), detail=str(out))
    return JSONResponse({"ok": True, "tiers": out})


# ── Env Vars ───────────────────────────────────────────────────────────

@router.get("/admin/api/env-vars")
async def list_env_vars(_session=Depends(require_session)):
    custom_docs = await get_all_custom_env()
    custom_map = {d["key"]: d for d in custom_docs}
    items = []
    for key in _ENV_DISPLAY:
        items.append({
            "key": key, "source": "system",
            "is_set": bool(os.getenv(key)), "value": None,
            "is_editable": True, "is_deletable": False,
            "requires_restart": key in _RESTART_REQUIRED_KEYS,
            "has_override": key in custom_map,
        })
    for key, doc in custom_map.items():
        if key not in _ENV_DISPLAY:
            items.append({
                "key": key, "source": "custom",
                "is_set": True, "value": doc.get("value", ""),
                "is_editable": True, "is_deletable": True,
                "requires_restart": False, "has_override": False,
            })
    items.sort(key=lambda x: (x["source"] != "system", x["key"]))
    return JSONResponse({"items": items})


@router.post("/admin/api/env-vars")
async def upsert_env_var(request: Request, _session=Depends(require_session)):
    body = await request.json()
    key = (body.get("key") or "").strip().upper()
    value = body.get("value", "")
    if not key:
        return JSONResponse({"ok": False, "error": "Key is required"}, status_code=400)
    if not _is_safe_key(key):
        return JSONResponse({"ok": False, "error": f"Key '{key}' is reserved"}, status_code=400)
    await set_custom_env(key, value)
    await log_action("settings", "env_var_updated", ip=client_ip(request),
                     target=key, detail="custom env override saved", ok=True)
    return JSONResponse({"ok": True, "key": key,
                         "requires_restart": key in _RESTART_REQUIRED_KEYS})


@router.delete("/admin/api/env-vars/{key}")
async def delete_env_var(request: Request, key: str = Path(...),
                         _session=Depends(require_session)):
    k = key.strip().upper()
    if not _is_safe_key(k):
        return JSONResponse({"ok": False, "error": f"Key '{k}' is reserved"}, status_code=400)
    deleted = await delete_custom_env(k)
    if not deleted:
        return JSONResponse({"ok": False, "error": "Key not found"}, status_code=404)
    await log_action("settings", "env_var_deleted", ip=client_ip(request),
                     target=k, detail="custom env override deleted", ok=True)
    return JSONResponse({"ok": True, "key": k,
                         "requires_restart": k in _RESTART_REQUIRED_KEYS})


# ── Restart ─────────────────────────────────────────────────────────────────────

@router.post("/admin/api/restart")
async def restart_server(request: Request, _session=Depends(require_session)):
    ip = client_ip(request)
    await log_action("settings", "server_restart_requested", ip=ip,
                     detail="admin triggered restart", ok=True)

    def _restart():
        time.sleep(1.5)
        LOGGER(__name__).info("Restarting server process via execv...")
        os.execv(sys.executable, [sys.executable, "-m", "server"])

    threading.Thread(target=_restart, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Server is restarting..."})


# ── Deposit Methods & Sell Timing ───────────────────────────────────────

@router.get("/admin/api/deposit-methods")
async def get_deposit_methods(_session=Depends(require_session)):
    from server.services.deposit.registry import get_all_providers
    all_providers = get_all_providers()
    enabled_map: dict = await get_setting("deposit_methods") or {}
    return JSONResponse({
        "methods": [
            {
                "method_id": p.method_id,
                "method_name": p.method_name,
                "configured": p.is_configured(),
                "enabled": enabled_map.get(p.method_id, True),
            }
            for p in all_providers
        ]
    })


@router.get("/admin/api/sell-timing")
async def get_sell_timing(_session=Depends(require_session)):
    return JSONResponse({
        "termination_delay_minutes": int(await get_setting("termination_delay_minutes") or 1),
        "payment_hold_hours": int(await get_setting("payment_hold_hours") or 48),
        "payment_hold_auto_term_hours": int(
            await get_setting("payment_hold_auto_term_hours")
            or await get_setting("payment_hold_hours") or 48
        ),
    })


@router.post("/admin/api/sell-timing")
async def update_sell_timing(request: Request, _session=Depends(require_session)):
    body = await request.json()
    updated = []
    for key in ("termination_delay_minutes", "payment_hold_hours",
                "payment_hold_auto_term_hours"):
        if key in body:
            try:
                val = int(body[key])
                if val < 0:
                    continue
                await set_setting(key, val)
                updated.append(key)
            except (ValueError, TypeError):
                pass
    await log_action("settings", "sell_timing_updated", ip=client_ip(request),
                     detail=", ".join(f"{k}={body[k]}" for k in updated))
    return JSONResponse({"ok": True, "updated": updated})


@router.post("/admin/api/deposit-methods")
async def update_deposit_methods(request: Request, _session=Depends(require_session)):
    from server.services.deposit.registry import get_all_providers
    body = await request.json()
    valid_ids = {p.method_id for p in get_all_providers()}
    cleaned = {k: bool(v) for k, v in body.items() if k in valid_ids}
    await set_setting("deposit_methods", cleaned)
    await log_action("settings", "deposit_methods_updated", ip=client_ip(request),
                     detail=str(cleaned))
    return JSONResponse({"ok": True})


_RESERVED_KEYS = {
    "DATABASE_URL", "PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD",
    "REPLIT_DOMAINS", "REPLIT_DEV_DOMAIN", "REPL_ID", "REPL_SLUG", "REPL_OWNER",
    "HOME", "PATH", "PWD", "USER", "SHELL", "HOSTNAME", "TERM", "LANG",
}


def _is_safe_key(key: str) -> bool:
    return key not in _RESERVED_KEYS
