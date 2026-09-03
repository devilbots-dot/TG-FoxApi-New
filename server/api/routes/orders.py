"""
Orders API — buy session accounts, poll for OTP delivery.

Authentication: X-Api-Key: tg_{user_id}_{secret}

Endpoints
─────────
  POST /api/v1/orders                  → buy an account, start OTP delivery
  GET  /api/v1/orders/{order_id}/otp   → poll for OTP (by order_id)

Flow
────
  1. POST /api/v1/orders {"country_code": "IN"}
     → reserves account, deducts balance, starts OTP listener
     → returns order_id + poll endpoint

  2. GET /api/v1/orders/{order_id}/otp  (poll every 3-5 seconds)
     → {otp_status: "waiting"}  while OTP hasn't arrived
     → {otp_status: "ready", otp: "12345"} once received
     → 408 on timeout (> 3 min with no Telegram login)

Response envelope (all endpoints):
  Success  → { "success": true, "message": "...", "data": {...}, "meta": {...} }
  Error    → { "success": false, "message": "...", "error": {"code": N, "type": "..."}, "meta": {...} }
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, field_validator

from server.api.deps import require_api_key
from server.api.response import ok, err, ERR_NOT_FOUND, ERR_FORBIDDEN, ERR_CONFLICT
from server import LOGGER

router = APIRouter(prefix="/api/v1/orders", tags=["Orders"])
_log = LOGGER(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  POST /api/v1/orders
# ══════════════════════════════════════════════════════════════════════════════

class CreateOrderIn(BaseModel):
    country_code: str

    @field_validator("country_code")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper()


@router.post(
    "",
    summary="Buy a session account (initiates OTP delivery)",
)
async def create_order(body: CreateOrderIn, user=Depends(require_api_key)):
    """
    Purchase a session account for the requested country.

    Steps:
      1. Validates country availability and your balance
      2. Atomically reserves the oldest unsold session for that country
      3. Deducts balance and creates an order record
      4. Spawns a background OTP listener (via proxy if enabled)
      5. Returns an `order_id` — poll `GET /api/v1/orders/{order_id}/otp` for the code

    Once you receive the OTP, use it to log in to the Telegram account on your device.
    The OTP window is 3 minutes — place a new order if it expires.
    """
    import config as _cfg
    from server.utils.database.countrydb import get_country
    from server.utils.database.userdb import get_balance
    from server.services.inventory_service import reserve_valid_session
    from server.services.market_service import buy_from_server, MarketError
    from server import bot as _bot

    code    = body.country_code
    user_id = user["user_id"]

    # ── 1. Validate country ───────────────────────────────────────────────
    country = await get_country(code)
    if not country or country.get("temp_disable"):
        return err(
            404,
            f"Country '{code}' is not available for purchase. "
            "Use GET /api/v1/countries to see available countries.",
            ERR_NOT_FOUND,
        )

    # ── 2. Balance pre-flight ─────────────────────────────────────────────
    price   = float(country.get("price", 0))
    balance = await get_balance(user_id)
    if balance < price:
        return err(
            402,
            f"Insufficient balance. Required: ${price:.2f} | Available: ${balance:.2f}. "
            "Top up via POST /api/v1/wallet/deposit.",
        )

    # ── 4. Ensure OTP delivery is configured ─────────────────────────────
    channel_id = getattr(_cfg, "SESSION_CHANNEL_ID", None)
    if not channel_id:
        return err(503, "OTP delivery is not configured on this server. Contact support.")

    # ── 5. Atomically reserve and live-validate the session ────────────────
    reservation = await reserve_valid_session(
        bot_client=_bot,
        user_id=user_id,
        country_code=code,
        max_attempts=25,
    )
    if not reservation.account:
        if reservation.retryable:
            return err(503, reservation.error or "Session validation is temporarily unavailable.")
        return err(
            404,
            f"No valid session accounts currently exist for '{code}'.",
            ERR_NOT_FOUND,
        )
    session = reservation.account

    # ── 6. Deduct balance + create order record ───────────────────────────
    try:
        result = await buy_from_server(
            user_id,
            code,
            account_id_override=session["account_id"],
        )
    except MarketError as e:
        from server.utils.database.sessiondb import revert_session_sold
        await revert_session_sold(session["account_id"])
        return err(400, e.message)
    except Exception as e:
        _log.error(
            "Unexpected error in create_order: user=%s code=%s acc=%s: %s",
            user_id, code, session["account_id"], e,
        )
        from server.utils.database.sessiondb import revert_session_sold
        await revert_session_sold(session["account_id"])
        return err(
            500,
            "An unexpected error occurred while processing your order. "
            "Please contact support — do not retry immediately.",
        )

    order_id = result["order_id"]
    phone    = session["phone"]
    password = session.get("tfa_password_enc", "") or ""

    # ── 7–9. Register the OTP slot and launch the listener ───────────────
    # These steps happen after the wallet debit. If local setup unexpectedly
    # fails, compensate exactly once while the order is still pending.
    try:
        from server.utils.sessions.otp_store import create as otp_create
        otp_create(phone, session["account_id"], password, owner_id=user_id, order_id=order_id)

        proxy_doc = None
        if country.get("proxy_login_enabled"):
            from server.utils.database.proxydb import get_active_proxy_for_country
            proxy_doc = await get_active_proxy_for_country(code)

        asyncio.create_task(
            _run_otp_listener(
                msg_id=session["session_msg_id"],
                channel_id=session["session_chat_id"],
                phone=phone,
                api_id=_cfg.API_ID,
                api_hash=_cfg.API_HASH,
                proxy_doc=proxy_doc,
                account_id=session["account_id"],
            )
        )
    except Exception as setup_exc:
        _log.error(
            "Order %s OTP setup failed after charge: %s",
            order_id, setup_exc, exc_info=True,
        )
        from server.utils.database.orderdb import refund_order
        from server.utils.database.userdb import update_balance
        from server.utils.database.walletdb import log_transaction
        try:
            if await refund_order(order_id, "otp_setup_failed"):
                await update_balance(user_id, price)
                await log_transaction(
                    user_id=user_id,
                    txn_type="refund",
                    amount=price,
                    ref_id=order_id,
                    note="Automatic refund: OTP setup failed",
                )
        finally:
            from server.utils.database.sessiondb import revert_session_sold
            await revert_session_sold(session["account_id"])
        return err(500, "Could not start OTP delivery. Your balance was refunded; please try again.")

    poll_url = f"/api/v1/orders/{order_id}/otp"

    return ok(
        data={
            "order_id":       order_id,
            "phone":          phone,
            "country":        country.get("country_name", code),
            "country_code":   code,
            "price":          price,
            "currency":       "USD",
            "new_balance":    round(float(result["new_balance"]), 4),
            "otp_status":     "pending",
            "poll_url":       poll_url,
            "poll_interval_seconds": 4,
            "otp_expires_in_seconds": 180,
            "via_proxy":      proxy_doc is not None,
        },
        message=(
            f"Order placed for {country.get('country_name', code)}. "
            f"Poll {poll_url} every 4 seconds for the OTP."
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  GET /api/v1/orders/{order_id}/otp
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{order_id}/otp",
    summary="Poll for OTP delivery status",
)
async def get_order_otp(
    order_id: str = Path(..., description="Order ID from POST /api/v1/orders"),
    user=Depends(require_api_key),
):
    """
    Poll this endpoint after `POST /api/v1/orders`.

    **Waiting** — OTP not yet received, retry in 3-5 seconds:
    ```json
    {"success": true, "data": {"otp_status": "waiting", "otp": null}, "message": "..."}
    ```

    **Ready** — OTP received:
    ```json
    {"success": true, "data": {"otp_status": "ready", "otp": "47607", "password": null}}
    ```

    **Timeout** (408) — No login detected within 3 minutes → place a new order.

    **Gone** (410) — OTP was delivered but polling stopped before you retrieved it → place a new order.
    """
    from server.utils.sessions.otp_store import get_by_order_id, get_phone_by_order_id, get_owner_id

    phone = get_phone_by_order_id(order_id)
    if phone is not None:
        owner = get_owner_id(phone)
        if owner is not None and owner != user["user_id"]:
            return err(403, "You do not have access to this order.", ERR_FORBIDDEN)

    state = get_by_order_id(order_id)

    if state is None:
        return err(
            404,
            f"Order '{order_id}' not found. It may have expired or never existed. "
            "OTP slots are cleaned up 10 minutes after delivery.",
            ERR_NOT_FOUND,
        )

    if state["status"] == "timeout":
        return err(
            408,
            "OTP timed out — no Telegram login was detected within 3 minutes. "
            "Please place a new order to get a fresh session.",
        )

    if state["status"] == "expired":
        return err(
            410,
            "OTP window closed — the code was delivered but polling stopped before retrieval. "
            "Please place a new order.",
        )

    if state["status"] == "waiting":
        return ok(
            data={
                "order_id":       order_id,
                "otp_status":     "waiting",
                "otp":            None,
                "password":       None,
                "poll_interval_seconds": 4,
            },
            message="OTP not yet received. Retry in 3-5 seconds.",
        )

    # status == "ready" — fire delivery tracking as a background task so it
    # never adds latency to the poll response.  Uses claim_order_delivery so
    # only the first successful poll triggers the delivered_at / delivery_status
    # update; subsequent polls for the same order are no-ops.
    async def _mark_delivered(oid: str) -> None:
        try:
            from server.utils.database.orderdb import claim_order_delivery, finish_order_delivery
            claimed = await claim_order_delivery(oid)
            if claimed:
                await finish_order_delivery(oid)
            from server.services.market_service import finalize_successful_purchase
            await finalize_successful_purchase(
                oid,
                country_code="XX",
                country_name="Unknown",
            )
        except Exception as _exc:
            _log.warning("Failed to mark order %s as delivered: %s", oid, _exc)

    asyncio.create_task(_mark_delivered(order_id))

    return ok(
        data={
            "order_id":   order_id,
            "otp_status": "ready",
            "otp":        state["otp"],
            "password":   state.get("password") or None,
        },
        message="OTP delivered successfully. Use it to log in to the Telegram account.",
    )


# ── Background OTP listener ───────────────────────────────────────────────────

async def _run_otp_listener(
    msg_id: int,
    channel_id: int,
    phone: str,
    api_id: int,
    api_hash: str,
    proxy_doc: dict | None = None,
    account_id: str | None = None,
) -> None:
    """
    Background task — runs concurrently with the HTTP response:
      1. Downloads .session bytes from the Telegram storage channel
      2. Connects via Telethon (with proxy if provided, direct as fallback)
      3. Waits for a message from Telegram (777000) with a 5-digit OTP
      4. Stores in otp_store; clears slot on timeout or error
    """
    from server import bot as _bot
    from server.utils.sessions.channel_storage import download_session_from_channel
    from server.utils.sessions.delivery_otp import get_otp_from_session
    from server.utils.sessions.otp_store import set_otp, clear
    from server import LOGGER as _LOGGER

    log = _LOGGER(__name__)

    try:
        session_bytes = await download_session_from_channel(
            _bot, channel_id, msg_id, account_id=account_id, phone=phone,
        )
        otp = await get_otp_from_session(
            session_bytes,
            api_id,
            api_hash,
            timeout=180,
            proxy_doc=proxy_doc,
        )
        if otp:
            set_otp(phone, otp)
            log.info("OTP received for %s (via %s)", phone, "proxy" if proxy_doc else "direct")
        else:
            log.warning("OTP timeout for %s", phone)
            clear(phone)
    except Exception as exc:
        log.error(
            "OTP listener storage failure account=%s phone=%s chat_id=%s msg_id=%s code=%s context=%s error=%s",
            account_id, phone, channel_id, msg_id,
            getattr(exc, "code", "UNKNOWN_STORAGE_ERROR"),
            getattr(exc, "context", {}), exc,
        )
        clear(phone)
