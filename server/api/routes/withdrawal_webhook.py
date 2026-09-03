"""
Withdrawal webhook handler — receives OxaPay payout status callbacks.

OxaPay sends POST requests to WITHDRAWAL_CALLBACK_URL with JSON payload.
Authenticity is verified via HMAC-SHA512 using the payout API key as secret.

Webhook payload fields (from OxaPay docs):
  type        — "payout"
  track_id    — OxaPay track_id
  address     — recipient address
  currency    — e.g. "USDT"
  network     — e.g. "TRX"
  amount      — payout amount
  fee         — gateway fee
  status      — processing | pending | confirming | confirmed | canceled | rejected
  tx_hash     — blockchain transaction hash (when available)
  description — description sent with payout
  internal    — bool
  memo        — memo/tag
  date        — UNIX timestamp

Security:
  - HMAC header 'HMAC' = HMAC-SHA512(raw_body, payout_api_key)
  - Reject if HMAC invalid
  - Idempotent: re-processing same track_id + status is a no-op
  - Must return HTTP 200 with body "OK" to acknowledge
"""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

import config
from server.logging import LOGGER
from server.utils.withdrawal_statuses import WithdrawalStatus
from server.utils.database.withdrawaldb import (
    get_withdrawal_by_track_id,
    mark_webhook_received,
    release_withdrawal,
    finalize_withdrawal,
    log_withdrawal_event,
    update_withdrawal_status,
)
from server.utils.database.userdb import get_balance

_log = LOGGER(__name__)
router = APIRouter(tags=["Withdrawal-Webhook"], include_in_schema=False)


@router.post("/webhooks/withdrawal", response_class=PlainTextResponse)
async def withdrawal_webhook(request: Request) -> PlainTextResponse:
    """
    OxaPay payout webhook endpoint.

    OxaPay retries up to 5 times on non-200 responses:
      1st retry: ~1 min; 2nd: ~3 min; 3rd: ~30 min; 4th: ~3 hrs
    This handler is idempotent — duplicate deliveries are safe.
    """
    # ── Read raw body ─────────────────────────────────────────────────────────
    raw_body = await request.body()

    # ── HMAC validation ───────────────────────────────────────────────────────
    if not _verify_hmac(raw_body, request.headers.get("HMAC", "")):
        # Log only IP + body length — never log raw payment payload (info leakage)
        _log.warning(
            "Withdrawal webhook: HMAC validation failed. IP=%s body_len=%d",
            _client_ip(request), len(raw_body),
        )
        # Return 200 to avoid leaking that HMAC failed (OxaPay would keep retrying on 400)
        return PlainTextResponse("OK", status_code=200)

    # ── Parse JSON ────────────────────────────────────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.error("Withdrawal webhook: invalid JSON body: %s", exc)
        return PlainTextResponse("OK", status_code=200)

    # ── Validate type ─────────────────────────────────────────────────────────
    event_type = payload.get("type", "")
    if event_type != "payout":
        _log.debug("Withdrawal webhook: ignoring event type '%s'", event_type)
        return PlainTextResponse("OK", status_code=200)

    track_id      = payload.get("track_id", "")
    oxapay_status = payload.get("status", "")
    tx_hash       = payload.get("tx_hash") or None
    gateway_fee   = payload.get("fee")

    if not track_id:
        _log.warning("Withdrawal webhook: missing track_id in payload")
        return PlainTextResponse("OK", status_code=200)

    _log.info(
        "Withdrawal webhook: track_id=%s status=%s tx_hash=%s",
        track_id, oxapay_status, tx_hash,
    )

    # ── Look up the withdrawal ────────────────────────────────────────────────
    wit = await get_withdrawal_by_track_id(track_id)
    if not wit:
        _log.warning("Withdrawal webhook: no withdrawal found for track_id=%s", track_id)
        return PlainTextResponse("OK", status_code=200)

    withdrawal_id = wit["withdrawal_id"]
    user_id       = wit["user_id"]
    current_status = wit.get("status", "")

    # ── Idempotency: skip if already in a final state ─────────────────────────
    if current_status in WithdrawalStatus.FINAL:
        _log.info(
            "Withdrawal webhook: %s already in final state '%s' — skipping",
            withdrawal_id, current_status,
        )
        return PlainTextResponse("OK", status_code=200)

    # ── Map OxaPay status → internal status ───────────────────────────────────
    new_internal_status = WithdrawalStatus.from_oxapay(oxapay_status)

    # ── Apply the status transition ───────────────────────────────────────────
    await log_withdrawal_event(withdrawal_id, "webhook_received", {
        "oxapay_status":   oxapay_status,
        "internal_status": new_internal_status,
        "track_id":        track_id,
        "tx_hash":         tx_hash,
    })

    await mark_webhook_received(withdrawal_id, payload)

    if new_internal_status == WithdrawalStatus.COMPLETED:
        await finalize_withdrawal(
            withdrawal_id=withdrawal_id,
            user_id=user_id,
            amount=wit["amount"],
            tx_hash=tx_hash,
            gateway_fee=gateway_fee,
        )
        await log_withdrawal_event(withdrawal_id, "completed_via_webhook", {"tx_hash": tx_hash})
        await _notify_user(user_id, "withdrawal_approved", wit=wit, tx_hash=tx_hash or "")

    elif new_internal_status in WithdrawalStatus.RELEASE:
        await release_withdrawal(
            withdrawal_id=withdrawal_id,
            user_id=user_id,
            amount=wit["amount"],
            new_status=new_internal_status,
            reason=f"OxaPay status: {oxapay_status}",
            gateway_status=oxapay_status,
        )
        await log_withdrawal_event(withdrawal_id, f"released_via_webhook_{new_internal_status}", {
            "oxapay_status": oxapay_status
        })
        await _notify_user(user_id, "withdrawal_rejected", wit=wit, note=f"Gateway status: {oxapay_status}")

    else:
        # In-flight status update
        await update_withdrawal_status(
            withdrawal_id,
            new_status=new_internal_status,
            gateway_status=oxapay_status,
            tx_hash=tx_hash,
        )

    return PlainTextResponse("OK", status_code=200)


# ── HMAC verification ─────────────────────────────────────────────────────────

def _verify_hmac(raw_body: bytes, received_hmac: str) -> bool:
    """
    Validate OxaPay HMAC signature.
    HMAC = HMAC-SHA512(raw_body_bytes, payout_api_key.encode())
    """
    api_key = config.OXAPAY_PAYOUT_API_KEY
    if not api_key:
        # Key not configured → reject all incoming callbacks to prevent forged payout status
        _log.error("OXAPAY_PAYOUT_API_KEY not set — rejecting withdrawal webhook (set the secret to enable)")
        return False

    if not received_hmac:
        return False

    try:
        expected = hmac.new(
            api_key.encode("utf-8"),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, received_hmac)
    except Exception as exc:
        _log.error("HMAC computation error: %s", exc)
        return False


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _notify_user(user_id: int, event: str, *, wit: dict, **kwargs) -> None:
    try:
        from server.utils.notifications import notify
        await notify(
            user_id, event,
            withdrawal_id=wit.get("withdrawal_id", ""),
            amount=wit.get("amount", 0.0),
            network=wit.get("network", ""),
            wallet_address=wit.get("wallet_address", ""),
            **kwargs,
        )
    except Exception as exc:
        _log.warning("Webhook notify failed for user %s: %s", user_id, exc)
