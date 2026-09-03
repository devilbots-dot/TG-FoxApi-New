"""Telegram Mini-App user panel routes."""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from server.utils.database import userdb
from server.utils.database.userdb import usersdb

from .auth import require_webapp_user

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/webapp", tags=["webapp"])
_TERMINATION_LOCKS: dict[str, asyncio.Lock] = {}


# ── Themes catalogue ─────────────────────────────────────────────────
THEMES = {
    "dark_gold":     ("Dark Gold",     False, 0.0),
    "midnight_blue": ("Midnight Blue", False, 0.0),
    "neon_purple":   ("Neon Purple",   False, 0.0),
    "greenfield":    ("Green Field",   True,  0.10),
    "village":       ("Village",       True,  0.10),
    "mountains":     ("Mountains",     True,  0.10),
}
DEFAULT_THEME = "dark_gold"
FREE_THEMES = [tid for tid, (_, prem, _) in THEMES.items() if not prem]


# ── Helpers ──────────────────────────────────────────────────────────
async def _get_user_doc(user_id: int) -> dict:
    doc = await usersdb.find_one({"user_id": int(user_id)}) or {}
    return doc


def _account_doc_from_auth(user: dict, user_id: int) -> dict:
    """Reuse the API-key dependency's just-loaded canonical account document."""
    doc = user.get("_webapp_account_doc")
    if isinstance(doc, dict) and int(doc.get("user_id") or 0) == int(user_id):
        return doc
    return {}


def _prefs_from_doc(doc: dict) -> dict:
    """Serialize Mini App preferences from an already-loaded canonical user."""
    prefs = doc.get("webapp_prefs") or {}
    return {
        "theme": prefs.get("theme") or DEFAULT_THEME,
        "owned_themes": list(prefs.get("owned_themes") or []) + FREE_THEMES,
    }


async def _set_theme(user_id: int, theme: str) -> None:
    await usersdb.update_one(
        {"user_id": int(user_id)},
        {"$set": {"webapp_prefs.theme": theme}},
        upsert=True,
    )


async def _grant_theme(user_id: int, theme: str) -> None:
    await usersdb.update_one(
        {"user_id": int(user_id)},
        {"$addToSet": {"webapp_prefs.owned_themes": theme}},
        upsert=True,
    )


def _rank_thresholds() -> dict:
    defaults = {"VIP1": 0, "VIP2": 50, "VIP3": 200, "PREMIUM": 500, "DIAMOND": 1000}
    try:
        from server.utils.database.configdb import get_config
        cfg = get_config("rank_min_spend")
        if isinstance(cfg, dict) and cfg:
            return {k: float(v or 0) for k, v in cfg.items()}
    except Exception:
        pass
    return defaults


def _next_reward(rank: str, thresholds: dict) -> dict:
    order = ["VIP1", "VIP2", "VIP3", "PREMIUM", "DIAMOND"]
    try:
        idx = order.index((rank or "VIP1").upper())
    except ValueError:
        idx = 0
    if idx >= len(order) - 1:
        return {"next_rank": None, "goal": 0, "remaining": 0}
    nxt = order[idx + 1]
    return {"next_rank": nxt, "goal": float(thresholds.get(nxt, 50))}


async def _counts_for(user_id: int) -> dict:
    async def _purchase_count() -> int:
        try:
            from server.utils.database.orderdb import ordersdb
            return await ordersdb.count_documents(
                {"buyer_id": int(user_id), "status": {"$in": ["delivered", "completed", "paid", "success"]}}
            )
        except Exception as exc:
            _log.debug("purchases count failed: %s", exc)
            return 0

    async def _sales_count() -> int:
        try:
            from server.utils.database.sellrequestdb import sellrequestsdb as sell_requestsdb
            return await sell_requestsdb.count_documents(
                {"user_id": int(user_id), "status": {"$in": ["approved", "paid", "completed"]}}
            )
        except Exception as exc:
            _log.debug("sales count failed: %s", exc)
            return 0

    purchases, sales = await asyncio.gather(_purchase_count(), _sales_count())
    return {"purchases": purchases, "sales": sales}


def _iso(dt: Any) -> str:
    try:
        return dt.isoformat() if dt else ""
    except Exception:
        return str(dt) if dt else ""


def _flag_emoji(code: str) -> str:
    code = (code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return "🌍"
    return chr(0x1F1E6 + ord(code[0]) - ord("A")) + chr(0x1F1E6 + ord(code[1]) - ord("A"))


def _bot_username() -> str:
    try:
        import config as _c
        return str(getattr(_c, "BOT_USERNAME", "") or "").lstrip("@")
    except Exception:
        return ""


def _public_deposit(doc: dict) -> dict:
    """Return only user-safe deposit fields; never expose provider credentials."""
    extra = doc.get("extra") or {}
    verification = extra.get("manual_chain_verification") or {}
    return {
        "deposit_id": doc.get("deposit_id", ""),
        "status": doc.get("status", "pending"),
        "amount": round(float(doc.get("amount") or 0), 4),
        "method": doc.get("method", ""),
        "currency": doc.get("currency") or "USD",
        "network": doc.get("network") or "",
        "address": doc.get("address") or "",
        "payment_url": doc.get("payment_url") or "",
        "qr_url": doc.get("qr_url") or "",
        "memo": doc.get("memo") or "",
        "phone": doc.get("phone") or "",
        "payment_reference": extra.get("pay_uid") or "",
        "instructions": doc.get("instructions") or "",
        "expires_at": _iso(doc.get("expires_at")),
        "created_at": _iso(doc.get("created_at")),
        "confirmed_at": _iso(doc.get("confirmed_at")),
        "transaction_hash_required": doc.get("method") in {"bep20_scan", "trc20_scan"},
        "transaction_hash_state": verification.get("state") or "",
        "transaction_hash_code": verification.get("code") or "",
        "transaction_confirmations": verification.get("confirmations"),
    }


def _public_transaction(doc: dict) -> dict:
    """Sanitize a ledger document for the owner-facing Mini App."""
    return {
        "id": doc.get("transaction_id") or str(doc.get("_id") or ""),
        "type": doc.get("type") or "adjustment",
        "amount": round(float(doc.get("amount") or 0), 4),
        "status": doc.get("status") or "completed",
        "note": doc.get("note") or doc.get("description") or "",
        "reference_id": doc.get("reference_id") or doc.get("deposit_id") or doc.get("order_id") or "",
        "created_at": _iso(doc.get("created_at") or doc.get("at")),
    }


def _public_sell_request(doc: dict) -> dict:
    """Seller-facing status only; session credentials and references never leave MongoDB."""
    return {
        "id": doc.get("request_id") or "",
        "country": doc.get("country_name") or doc.get("code") or "",
        "code": doc.get("code") or "",
        "phone": doc.get("phone") or "",
        "offer_price": round(float(doc.get("offer_price") or 0), 4),
        "final_price": round(float(doc.get("final_price") or 0), 4) if doc.get("final_price") is not None else None,
        "pending_amount": round(float(doc.get("pending_amount") or 0), 4),
        "status": doc.get("status") or "pending",
        "lifecycle_status": doc.get("lifecycle_status") or "pending",
        "admin_note": doc.get("admin_note") or "",
        "submitted_at": _iso(doc.get("submitted_at")),
        "reviewed_at": _iso(doc.get("reviewed_at")),
        "payment_release_at": _iso(doc.get("payment_release_at")),
    }


# ── Page routes ────────────────────────────────────────────────────
@router.get("/", include_in_schema=False)
async def webapp_index() -> RedirectResponse:
    """Route old saved Telegram WebApp buttons into the current Mini App."""
    return RedirectResponse(url="/app/", status_code=307)


@router.get("/buy", include_in_schema=False)
async def webapp_buy() -> RedirectResponse:
    return RedirectResponse(url="/app/", status_code=307)


@router.get("/record", include_in_schema=False)
async def webapp_record() -> RedirectResponse:
    return RedirectResponse(url="/app/", status_code=307)


# ── API routes (all require a verified canonical user) ─────────────
@router.get("/api/me")
async def api_me(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    uid = int(user["id"])
    doc = _account_doc_from_auth(user, uid) or await _get_user_doc(uid)
    if not doc:
        # A user may open the bot menu before sending /start. Create the same
        # canonical usersdb profile the bot uses, keyed only by Telegram user.id.
        full_name = " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        await userdb.add_served_user(
            uid,
            username=user.get("username") or None,
            full_name=full_name or None,
        )
        doc = await _get_user_doc(uid)
    prefs = _prefs_from_doc(doc)
    counts = await _counts_for(uid)
    thresholds = _rank_thresholds()

    balance = round(float(doc.get("balance") or 0), 4)
    total_spent = round(float(doc.get("total_spend") or doc.get("total_spent") or 0), 4)
    total_earned = round(float(doc.get("total_earn") or doc.get("total_earned") or 0), 4)
    rank = (doc.get("rank") or "VIP1").upper()
    nxt = _next_reward(rank, thresholds)
    current_progress = total_spent
    remaining = max(0.0, float(nxt["goal"]) - current_progress) if nxt["next_rank"] else 0.0

    return JSONResponse({
        "user": {
            "id": uid,
            "first_name": user.get("first_name") or "",
            "username": user.get("username") or "",
            "language_code": user.get("language_code") or "en",
        },
        "balance": balance,
        "buy_spent": total_spent,
        "sell_earned": total_earned,
        "rank": rank,
        "next_reward": {
            "rank": nxt["next_rank"],
            "goal": nxt["goal"],
            "progress": current_progress,
            "remaining": remaining,
        },
        "counts": counts,
        "prefs": prefs,
        "bot_username": _bot_username(),
    })


@router.get("/api/record")
async def api_record(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    uid = int(user["id"])
    async def _purchases() -> list[dict]:
        items: list[dict] = []
        try:
            from server.utils.database.orderdb import ordersdb
            cursor = ordersdb.find({"buyer_id": uid}).sort("created_at", -1).limit(50)
            async for doc in cursor:
                items.append({
                    "id": doc.get("order_id") or str(doc.get("_id")),
                    "amount": round(float(doc.get("amount") or 0), 4),
                    "status": doc.get("status") or "pending",
                    "account_id": doc.get("account_id") or "",
                    "at": _iso(doc.get("created_at")),
                    "completed_at": _iso(doc.get("completed_at")),
                })
        except Exception as exc:
            _log.warning("record.purchases failed: %s", exc)
        return items

    async def _sales() -> list[dict]:
        items: list[dict] = []
        try:
            from server.utils.database.sellrequestdb import sellrequestsdb as sell_requestsdb
            cursor = sell_requestsdb.find({"user_id": uid}).sort("submitted_at", -1).limit(50)
            async for doc in cursor:
                price = doc.get("final_price") if doc.get("final_price") is not None else doc.get("offer_price")
                items.append({
                    "id": doc.get("request_id") or str(doc.get("_id")),
                    "code": doc.get("code") or "",
                    "country": doc.get("country_name") or (doc.get("code") or ""),
                    "phone": doc.get("phone") or "",
                    "price": round(float(price or 0), 4),
                    "status": doc.get("status") or "pending",
                    "lifecycle_status": doc.get("lifecycle_status") or "",
                    "at": _iso(doc.get("submitted_at") or doc.get("created_at")),
                })
        except Exception as exc:
            _log.warning("record.sales failed: %s", exc)
        return items

    purchases, sales = await asyncio.gather(_purchases(), _sales())

    return JSONResponse({
        "purchases": purchases,
        "sales": sales,
        "totals": {"purchases_count": len(purchases), "sales_count": len(sales)},
    })


# ── Buy Account: countries + purchase ────────────────────────────
@router.get("/api/countries")
async def api_countries(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    """List active countries with stock and price for buying."""
    from server.utils.database.countrydb import get_all_countries
    from server.utils.database.sessiondb import get_unsold_counts_all_countries

    uid = int(user["id"])
    countries = await get_all_countries(buy_only=True)
    stock_map = await get_unsold_counts_all_countries(clean_only=True)

    # Optional per-user discount preview. Rank/configuration are read once for
    # this response; country and settings collections are already RAM-backed.
    try:
        from server.utils.pricing_discounts import apply_discount_context, get_discount_context
        discount_context = await get_discount_context(uid)
    except Exception:
        apply_discount_context = None  # type: ignore
        discount_context = None

    items = []
    for c in countries or []:
        code = str(c.get("code") or "").upper()
        if not code:
            continue
        base = round(float(c.get("price") or 0), 4)
        final = base
        if apply_discount_context and discount_context:
            try:
                final = round(float(apply_discount_context(base, code, *discount_context)), 4)
            except Exception:
                final = base
        items.append({
            "code": code,
            "name": c.get("country_name") or code,
            "flag": _flag_emoji(code),
            "idc": c.get("idc") or "",
            "price": base,
            "final_price": final,
            "discounted": final < base,
            "stock": int(stock_map.get(code, 0)),
            "is_full": bool(c.get("is_full")),
            "disabled": bool(c.get("temp_disable")),
        })

    # Sort: available + in-stock first, then by rank
    items.sort(key=lambda x: (x["disabled"], x["is_full"], -x["stock"], x["name"].lower()))
    return JSONResponse({"countries": items})


@router.post("/api/buy")
async def api_buy(request: Request, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    """Purchase a session account for the requested country (mirrors POST /api/v1/orders)."""
    import config as _cfg
    from server.utils.database.countrydb import get_country
    from server.utils.database.userdb import get_balance
    from server.services.inventory_service import reserve_valid_session
    from server.utils.database.sessiondb import revert_session_sold
    from server.services.market_service import buy_from_server, MarketError
    from server import bot as _bot
    from server.utils.sessions.otp_store import create as otp_create

    body = await request.json()
    code = str(body.get("country_code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "country_code required")

    uid = int(user["id"])

    country = await get_country(code)
    if not country or country.get("temp_disable"):
        raise HTTPException(404, f"Country '{code}' is not available.")

    reservation = await reserve_valid_session(
        bot_client=_bot,
        user_id=uid,
        country_code=code,
        max_attempts=25,
    )
    if not reservation.account:
        if reservation.retryable:
            raise HTTPException(503, reservation.error or "Session validation is temporarily unavailable.")
        raise HTTPException(404, f"No valid accounts in stock for '{code}'.")
    session = reservation.account

    price = float(country.get("price", 0) or 0)
    try:
        from server.utils.pricing_discounts import apply_discounts
        price = round(float(await apply_discounts(uid, code, price)), 4)
    except Exception:
        price = round(price, 4)
    balance = await get_balance(uid)
    if balance < price:
        await revert_session_sold(session["account_id"])
        raise HTTPException(402, f"Insufficient balance. Need ${price:.2f}, have ${balance:.2f}.")

    if not session.get("session_chat_id") or not session.get("session_msg_id"):
        await revert_session_sold(session["account_id"])
        raise HTTPException(503, "Stored session reference is incomplete. Contact support.")

    try:
        result = await buy_from_server(uid, code, account_id_override=session["account_id"])
    except MarketError as e:
        await revert_session_sold(session["account_id"])
        raise HTTPException(400, e.message)
    except Exception as e:
        _log.error("api_buy user=%s code=%s: %s", uid, code, e, exc_info=True)
        await revert_session_sold(session["account_id"])
        raise HTTPException(500, "Unexpected error. Contact support.")

    order_id = result["order_id"]
    phone = session["phone"]
    password = session.get("tfa_password_enc", "") or ""
    otp_create(phone, session["account_id"], password, owner_id=uid, order_id=order_id)

    # Launch background OTP listener (import from orders.py to reuse)
    proxy_doc = None
    if country.get("proxy_login_enabled"):
        try:
            from server.utils.database.proxydb import get_active_proxy_for_country
            proxy_doc = await get_active_proxy_for_country(code)
        except Exception:
            proxy_doc = None
    try:
        from server.api.routes.orders import _run_otp_listener
        asyncio.create_task(_run_otp_listener(
            msg_id=session["session_msg_id"],
            channel_id=session["session_chat_id"],
            phone=phone,
            api_id=_cfg.API_ID,
            api_hash=_cfg.API_HASH,
            proxy_doc=proxy_doc,
            account_id=session["account_id"],
        ))
    except Exception as e:
        _log.error("OTP listener launch failed: %s", e)

    return JSONResponse({
        "order_id": order_id,
        "phone": phone,
        "country": country.get("country_name", code),
        "country_code": code,
        "price_paid": result["price_paid"],
        "new_balance": round(result["new_balance"], 2),
        "otp_status": "pending",
    })


@router.get("/api/order/{order_id}/otp")
async def api_order_otp(
    order_id: str = Path(...),
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    """Poll OTP for a purchased order (mirrors GET /api/v1/orders/{id}/otp)."""
    from server.utils.sessions.otp_store import (
        get_by_order_id,
        get_phone_by_order_id,
        get_owner_id,
    )
    uid = int(user["id"])
    phone = get_phone_by_order_id(order_id)
    if phone is not None:
        owner = get_owner_id(phone)
        if owner is not None and owner != uid:
            raise HTTPException(403, "You do not have access to this order.")

    state = get_by_order_id(order_id)
    if state is None:
        return JSONResponse({"order_id": order_id, "otp_status": "unknown"}, status_code=404)

    if state["status"] == "timeout":
        return JSONResponse({"order_id": order_id, "otp_status": "timeout"}, status_code=408)
    if state["status"] == "expired":
        return JSONResponse({"order_id": order_id, "otp_status": "expired"}, status_code=410)
    if state["status"] == "waiting":
        return JSONResponse({"order_id": order_id, "otp": None, "otp_status": "waiting"})
    # Keep the Mini App delivery path identical to the bot path. Previously
    # this endpoint returned the OTP but never marked the paid order delivered,
    # so the timeout worker later refunded an already-delivered purchase.
    from server.utils.database.orderdb import get_order, mark_order_delivered
    order = await get_order(order_id)
    if not order or int(order.get("buyer_id", -1)) != uid:
        raise HTTPException(404, "Order record could not be found for this account.")

    delivered = await mark_order_delivered(order_id)
    order = await get_order(order_id)
    if not order or order.get("delivery_status") != "delivered":
        _log.error("Mini App OTP delivery could not finalize order %s", order_id)
        raise HTTPException(
            503,
            "OTP arrived, but order completion is temporarily unavailable. Keep this page open and retry.",
        )

    accounting_status = "completed"
    # Repeated OTP polls must not re-run successful-sale accounting once both
    # durable markers are set. The finalizer itself is idempotent as a second
    # safety net for concurrent requests/processes.
    if not order.get("successful_accounting") or not order.get("sales_feed_queued"):
        try:
            from server.services.market_service import finalize_successful_purchase
            finalized = await finalize_successful_purchase(
                order_id,
                country_code=str(order.get("country_code") or ""),
                country_name=str(order.get("country_name") or ""),
            )
            refreshed = await get_order(order_id)
            if not finalized and not (refreshed or {}).get("successful_accounting"):
                accounting_status = "pending"
        except Exception as exc:
            # Delivery is already durable, so it cannot be timed out/refunded.
            accounting_status = "pending"
            _log.error("Mini App successful-sale accounting failed for %s: %s", order_id, exc, exc_info=True)

    return JSONResponse({
        "order_id": order_id,
        "otp": state["otp"],
        "password": state.get("password") or None,
        "otp_status": "ready",
        "order_status": "completed",
        "delivery_status": "delivered",
        "accounting_status": accounting_status,
    })


@router.post("/api/order/{order_id}/terminate")
async def api_terminate_order(
    order_id: str = Path(...),
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    """Terminate the platform-side Telegram session after OTP delivery."""
    from server.utils.database.orderdb import get_order, ordersdb

    uid = int(user["id"])
    # Prevent two browser taps/tabs from trying to invalidate the same
    # Telethon session at the same time. The durable order marker below keeps
    # retries idempotent after the lock has gone away.
    termination_lock = _TERMINATION_LOCKS.setdefault(order_id, asyncio.Lock())
    async with termination_lock:
        order = await get_order(order_id)
        if not order or int(order.get("buyer_id", -1)) != uid:
            raise HTTPException(404, "Order not found for this account.")
        if order.get("delivery_status") != "delivered" or order.get("status") != "completed":
            raise HTTPException(409, "Terminate is available only after OTP delivery is completed.")
        if order.get("platform_session_terminated_at"):
            return JSONResponse({
                "order_id": order_id,
                "ok": True,
                "terminated": True,
                "already_terminated": True,
            })

        from server.utils.database.sessiondb import get_session_account
        account = await get_session_account(str(order.get("account_id") or ""))
        if not account:
            raise HTTPException(404, "The stored account session is no longer available.")
        chat_id = account.get("session_chat_id")
        msg_id = account.get("session_msg_id")
        phone = str(account.get("phone") or "")
        if not chat_id or not msg_id:
            raise HTTPException(503, "The stored account session reference is incomplete.")

        from server import bot as _bot
        from server.utils.sessions.channel_storage import download_session_from_channel
        from server.utils.sessions.telethon_client import (
            cleanup_session_files,
            connect_with_proxy_fallback,
            new_temp_session_path,
            write_session_bytes,
        )
        import config as _cfg
        from server.utils.common import utcnow

        tmp_path = new_temp_session_path()
        client = None
        try:
            session_bytes = await download_session_from_channel(
                _bot,
                int(chat_id),
                int(msg_id),
                account_id=str(account.get("account_id") or ""),
                phone=phone,
            )
            await write_session_bytes(tmp_path, session_bytes)
            client, _proxy_id, _proxy_doc = await connect_with_proxy_fallback(
                tmp_path[:-8],
                _cfg.API_ID,
                _cfg.API_HASH,
                str(account.get("country_code") or "XX"),
            )
            try:
                authorized = await client.is_user_authorized()
            except Exception:
                authorized = True
            if authorized:
                await client.log_out()

            await ordersdb.update_one(
                {
                    "order_id": order_id,
                    "buyer_id": uid,
                    "status": "completed",
                    "delivery_status": "delivered",
                    "platform_session_terminated_at": {"$exists": False},
                },
                {
                    "$set": {
                        "platform_session_terminated_at": utcnow(),
                        "platform_session_termination": "miniapp",
                    }
                }
            )
            return JSONResponse({
                "order_id": order_id,
                "ok": True,
                "terminated": True,
                "already_terminated": False,
            })
        except HTTPException:
            raise
        except Exception as exc:
            _log.error("Mini App terminate failed for order %s phone=%s: %s", order_id, phone, exc, exc_info=True)
            raise HTTPException(502, "Could not terminate the platform session. You can retry the button.")
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            try:
                await cleanup_session_files(tmp_path)
            except Exception:
                pass


# ── Wallet, deposits and transaction ledger ────────────────────────────────
@router.get("/api/wallet")
async def api_wallet(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    """Current user's real wallet snapshot and configured payment capabilities."""
    from server.services.deposit import get_configured_providers
    from server.utils.constants import MIN_DEPOSIT
    from server.utils.database.userdb import (
        SUPPORTED_NETWORKS,
        get_wallet_snapshot,
    )
    import config as _cfg

    uid = int(user["id"])
    snapshot = await get_wallet_snapshot(uid)
    balance = snapshot["balance"]
    reserved_amount = snapshot["reserved_balance"]
    addresses = snapshot["addresses"]
    methods = []
    for provider in get_configured_providers():
        networks = provider.networks if hasattr(provider, "networks") else []
        if callable(networks):
            networks = networks()
        # OxaPay owns network selection inside its hosted checkout. Sending its
        # provider capabilities to the Mini App previously rendered a redundant
        # network picker before invoice creation.
        if provider.method_id == "oxapay":
            networks = []
        methods.append({
            "id": provider.method_id,
            "name": provider.method_name,
            "networks": list(networks or []),
            "min_amount": MIN_DEPOSIT,
        })
    return JSONResponse({
        "balance": balance,
        "reserved": reserved_amount,
        "net_spendable": round(max(0, balance - reserved_amount), 4),
        "currency": "USD",
        "addresses": addresses,
        "deposit_methods": methods,
        "deposit_minimum": MIN_DEPOSIT,
        "withdrawal": {
            "networks": SUPPORTED_NETWORKS,
            "minimum": _cfg.WITHDRAWAL_MIN_AMOUNT,
            "maximum": _cfg.WITHDRAWAL_MAX_AMOUNT,
            "fee_percent": _cfg.WITHDRAWAL_FEE_PERCENT,
            "fee_fixed": _cfg.WITHDRAWAL_FEE_FIXED,
        },
    })


@router.post("/api/deposit")
async def api_create_deposit(request: Request, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    """Create a real user-owned deposit through an enabled server provider."""
    from server.services.deposit import get_provider
    from server.utils.constants import MIN_DEPOSIT
    from server.utils.database.walletdb import create_deposit_v2, get_deposit
    from server.utils.money import parse_money

    body = await request.json()
    method = str(body.get("method") or "").strip()
    network = str(body.get("network") or "").strip() or None
    amount = parse_money(body.get("amount"), field="deposit amount", minimum=MIN_DEPOSIT)
    provider = get_provider(method)
    if not provider or not provider.is_configured():
        raise HTTPException(503, "This payment method is currently unavailable.")

    deposit_id = f"DEP-{secrets.token_hex(8).upper()}"
    try:
        payment = await provider.create_payment(
            deposit_id=deposit_id,
            amount=amount,
            user_id=int(user["id"]),
            network=network,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        _log.error("webapp deposit provider error user=%s method=%s: %s", user["id"], method, exc)
        raise HTTPException(503, "Payment provider is temporarily unavailable.")

    await create_deposit_v2(
        deposit_id=deposit_id,
        user_id=int(user["id"]),
        amount=amount,
        method=method,
        network=network,
        currency=payment.currency,
        address=payment.address,
        payment_url=payment.payment_url,
        qr_url=payment.qr_url,
        memo=payment.memo,
        phone=payment.phone,
        instructions=payment.instructions,
        expires_at=payment.expires_at,
        extra=payment.extra,
    )
    doc = await get_deposit(deposit_id)
    return JSONResponse({"deposit": _public_deposit(doc or {})}, status_code=201)


@router.get("/api/deposits")
async def api_deposit_history(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    from server.utils.database.walletdb import count_user_deposits, get_user_deposits_paginated

    uid = int(user["id"])
    total, docs = await asyncio.gather(
        count_user_deposits(uid),
        get_user_deposits_paginated(uid, page=page, limit=limit),
    )
    return JSONResponse({"page": page, "limit": limit, "total": total, "items": [_public_deposit(doc) for doc in docs]})


@router.get("/api/deposit/{deposit_id}")
async def api_deposit_status(deposit_id: str, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    from server.utils.database.walletdb import get_deposit

    doc = await get_deposit(deposit_id)
    if not doc or int(doc.get("user_id") or 0) != int(user["id"]):
        raise HTTPException(404, "Deposit not found.")
    return JSONResponse({"deposit": _public_deposit(doc)})


@router.post("/api/deposit/{deposit_id}/transaction-hash")
async def api_submit_deposit_transaction_hash(
    deposit_id: str,
    request: Request,
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    """Verify a user-submitted BEP20/TRC20 hash for this user's pending deposit."""
    from server.services.deposit.manual_chain import (
        ManualChainError,
        submit_transaction_hash,
    )

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "A JSON transaction hash is required.") from exc
    tx_hash = body.get("transaction_hash") if isinstance(body, dict) else None
    if not isinstance(tx_hash, str):
        raise HTTPException(400, "transaction_hash is required.")

    try:
        result = await submit_transaction_hash(deposit_id, int(user["id"]), tx_hash)
    except LookupError as exc:
        raise HTTPException(404, "Deposit not found.") from exc
    except ManualChainError as exc:
        message = str(exc)
        status_code = 409 if "already been submitted" in message else 400
        raise HTTPException(status_code, message) from exc
    except RuntimeError as exc:
        _log.error("webapp manual chain verification failed deposit=%s user=%s: %s", deposit_id, user["id"], type(exc).__name__)
        raise HTTPException(503, "Transaction verification is temporarily unavailable. Please retry shortly.") from exc

    outcome = str(result.get("outcome") or "pending")
    response_status = 201 if outcome == "completed" else 202 if outcome == "pending" else 200
    return JSONResponse({
        "outcome": outcome,
        "deposit": _public_deposit(result.get("deposit") or {}),
        "verification": result.get("verification") or {},
    }, status_code=response_status)


@router.post("/api/deposit/{deposit_id}/binance-order")
async def api_submit_binance_order_id(
    deposit_id: str,
    request: Request,
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    """Verify a canonical user's Binance Pay deposit by its Order ID."""
    from server.services.deposit.providers.binance_pay_tx import (
        claim_order_id,
        release_order_id,
        verify_by_order_id,
    )
    from server.utils.database.walletdb import confirm_deposit, depositsdb, get_deposit

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "A JSON Binance Order ID is required.") from exc
    order_id = body.get("order_id") if isinstance(body, dict) else None
    order_id = str(order_id or "").strip()
    if len(order_id) < 5:
        raise HTTPException(400, "Enter a valid Binance Order ID.")

    doc = await get_deposit(deposit_id)
    if not doc or int(doc.get("user_id") or 0) != int(user["id"]):
        raise HTTPException(404, "Deposit not found.")
    if doc.get("method") != "binance_pay_tx":
        raise HTTPException(400, "This deposit does not use Binance Order ID verification.")
    status = str(doc.get("status") or "pending")
    if status == "completed":
        return JSONResponse({"outcome": "completed", "deposit": _public_deposit(doc)}, status_code=200)
    if status != "pending":
        raise HTTPException(400, f"This deposit is {status} and cannot be verified.")

    result = await verify_by_order_id(order_id, float(doc.get("amount") or 0), deposit_id)
    if not result.get("ok"):
        status_code = 409 if result.get("duplicate") else 400
        raise HTTPException(status_code, result.get("error") or "Binance Order ID verification failed.")
    if not await claim_order_id(order_id, deposit_id, int(user["id"]), float(doc.get("amount") or 0)):
        raise HTTPException(409, "This Binance Order ID has already been used for another deposit.")
    try:
        credited = await confirm_deposit(deposit_id, confirmed_by=None)
    except Exception as exc:
        await release_order_id(order_id)
        _log.error("webapp Binance Order ID credit failed deposit=%s user=%s: %s", deposit_id, user["id"], type(exc).__name__)
        raise HTTPException(503, "Could not credit this deposit. Please retry shortly.") from exc

    if credited:
        tx_data = result.get("tx") or {}
        tx_id = str(tx_data.get("orderId") or order_id)
        await depositsdb.update_one(
            {"deposit_id": deposit_id, "user_id": int(user["id"])},
            {"$set": {
                "tx_hash": tx_id,
                "extra.tx_hash": tx_id,
                "extra.binance_order_id": order_id,
            }},
        )
        try:
            from server.utils.notifications import notify
            await notify(int(user["id"]), "deposit_confirmed", deposit_id=deposit_id, amount=credited["amount"], method="binance_pay_tx")
        except Exception:
            pass
    final_doc = await get_deposit(deposit_id) or doc
    return JSONResponse({"outcome": "completed", "deposit": _public_deposit(final_doc)}, status_code=201)


@router.get("/api/transactions")
async def api_transactions(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=50),
    txn_type: str | None = Query(None),
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    from server.utils.database.walletdb import get_user_transactions, transactionsdb

    allowed = {"deposit", "withdrawal", "purchase", "sale", "refund", "bonus", "adjustment", "referral"}
    if txn_type and txn_type not in allowed:
        raise HTTPException(400, "Unsupported transaction filter.")
    query: dict[str, Any] = {"user_id": int(user["id"])}
    if txn_type:
        query["type"] = txn_type
    docs, total = await asyncio.gather(
        get_user_transactions(int(user["id"]), page=page, limit=limit, txn_type=txn_type),
        transactionsdb.count_documents(query),
    )
    return JSONResponse({"page": page, "limit": limit, "total": total, "items": [_public_transaction(doc) for doc in docs]})


@router.post("/api/withdraw/address")
async def api_set_withdrawal_address(request: Request, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    """Save a validated withdrawal address for the signed-in Telegram user."""
    from server.utils.database.userdb import SUPPORTED_NETWORKS, set_wallet_address
    from server.utils.validation import validate_wallet_address

    body = await request.json()
    network = str(body.get("network") or "").strip().upper()
    address = str(body.get("address") or "").strip()
    if network not in SUPPORTED_NETWORKS:
        raise HTTPException(400, f"Unsupported network. Use: {', '.join(SUPPORTED_NETWORKS)}")
    error = validate_wallet_address(network, address)
    if error:
        raise HTTPException(400, f"Invalid {network} address: {error}")
    await set_wallet_address(int(user["id"]), network, address)
    return JSONResponse({"network": network, "address": address})


@router.post("/api/withdraw")
async def api_withdraw(request: Request, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    """Create a real user-owned withdrawal using the existing withdrawal service."""
    from server.services.withdrawal import get_withdrawal_service
    from server.utils.database.configdb import get_setting
    from server.utils.money import parse_money
    import config as _cfg

    body = await request.json()
    amount = parse_money(
        body.get("amount"),
        field="withdrawal amount",
        minimum=_cfg.WITHDRAWAL_MIN_AMOUNT,
        maximum=_cfg.WITHDRAWAL_MAX_AMOUNT,
    )
    network = str(body.get("network") or "").strip().upper()
    if not await get_setting("withdrawals_enabled"):
        raise HTTPException(503, "Withdrawals are temporarily unavailable.")
    result = await get_withdrawal_service().create_withdrawal(
        user_id=int(user["id"]), amount=amount, network=network,
    )
    if not result.get("ok"):
        raise HTTPException(400, str(result.get("error") or "Withdrawal could not be created."))
    return JSONResponse({
        "withdrawal_id": result["withdrawal_id"],
        "status": result["status"],
        "amount": result["amount"],
        "fee": result.get("fee", 0.0),
        "net_amount": result.get("net_amount", result["amount"]),
        "network": result["network"],
        "wallet_address": result["wallet_address"],
    }, status_code=201)


@router.get("/api/withdrawals")
async def api_withdrawals(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    user: dict = Depends(require_webapp_user),
) -> JSONResponse:
    from server.api.routes.wallet import _withdrawal_view
    from server.utils.database.withdrawaldb import count_user_withdrawals, get_user_withdrawals

    uid = int(user["id"])
    docs, total = await asyncio.gather(
        get_user_withdrawals(uid, page=page, limit=limit),
        count_user_withdrawals(uid),
    )
    return JSONResponse({"page": page, "limit": limit, "total": total, "items": [_withdrawal_view(doc) for doc in docs]})


# ── Personal growth, support and seller status ───────────────────────────────
@router.get("/api/referral")
async def api_referral(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    from server.utils.database.userdb import get_referral_stats

    stats = await get_referral_stats(int(user["id"]))
    username = _bot_username()
    code = str(stats.get("code") or "")
    return JSONResponse({
        "code": code,
        "count": int(stats.get("count") or 0),
        "earnings": round(float(stats.get("earnings") or 0), 4),
        "link": f"https://t.me/{username}?start=ref_{code}" if username and code else "",
    })


@router.get("/api/support")
async def api_support(_: dict = Depends(require_webapp_user)) -> JSONResponse:
    import config as _cfg
    return JSONResponse({
        "support_group": _cfg.SUPPORT_GROUP,
        "support_channel": _cfg.SUPPORT_CHANNEL,
        "updates_channel": _cfg.UPDATES_CHANNEL,
        "support_link": _cfg.support_link(),
    })


@router.get("/api/sell-requests")
async def api_sell_requests(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    from server.utils.database.sellrequestdb import get_user_sell_requests
    docs = await get_user_sell_requests(
        int(user["id"]),
        limit=30,
        include_session_payload=False,
    )
    return JSONResponse({"items": [_public_sell_request(doc) for doc in docs]})


# ── Themes ──────────────────────────────────────────────────────────
@router.get("/api/themes")
async def api_themes(user: dict = Depends(require_webapp_user)) -> JSONResponse:
    uid = int(user["id"])
    prefs = _prefs_from_doc(_account_doc_from_auth(user, uid) or await _get_user_doc(uid))
    items = []
    for tid, (name, prem, price) in THEMES.items():
        items.append({
            "id": tid, "name": name, "premium": prem, "price": price,
            "owned": tid in prefs["owned_themes"],
            "active": tid == prefs["theme"],
        })
    return JSONResponse({"themes": items, "active": prefs["theme"]})


@router.post("/api/theme/select")
async def api_theme_select(request: Request, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    body = await request.json()
    theme = str(body.get("theme") or "")
    if theme not in THEMES:
        raise HTTPException(400, "Unknown theme")
    uid = int(user["id"])
    prefs = _prefs_from_doc(_account_doc_from_auth(user, uid) or await _get_user_doc(uid))
    if theme not in prefs["owned_themes"]:
        raise HTTPException(403, "Theme not owned")
    await _set_theme(uid, theme)
    return JSONResponse({"ok": True, "theme": theme})


@router.post("/api/theme/purchase")
async def api_theme_purchase(request: Request, user: dict = Depends(require_webapp_user)) -> JSONResponse:
    body = await request.json()
    theme = str(body.get("theme") or "")
    if theme not in THEMES:
        raise HTTPException(400, "Unknown theme")
    name, prem, price = THEMES[theme]
    if not prem or price <= 0:
        raise HTTPException(400, "Not a premium theme")
    uid = int(user["id"])
    prefs = _prefs_from_doc(_account_doc_from_auth(user, uid) or await _get_user_doc(uid))
    if theme in prefs["owned_themes"]:
        return JSONResponse({"ok": True, "already_owned": True, "theme": theme})
    ok = await userdb.deduct_balance_atomic(uid, float(price))
    if not ok:
        bal = await userdb.get_balance(uid)
        raise HTTPException(402, f"Insufficient balance (${bal:.2f}). Deposit and try again.")
    await _grant_theme(uid, theme)
    await _set_theme(uid, theme)
    new_bal = await userdb.get_balance(uid)
    return JSONResponse({"ok": True, "theme": theme, "name": name, "charged": price, "new_balance": new_bal})


@router.get("/{legacy_path:path}", include_in_schema=False)
async def redirect_legacy_webapp_path(legacy_path: str):
    """Keep historic non-API WebApp buttons inside the canonical Mini App.

    Telegram messages preserve the URL that existed when they were sent. A
    stale button can therefore contain a path such as `/webapp/app/`; route it
    to `/app/` rather than showing a 404. API typos remain true API 404s.
    """
    if legacy_path.startswith("api/"):
        raise HTTPException(404, "WebApp API route not found.")
    return RedirectResponse(url="/app/", status_code=307)
