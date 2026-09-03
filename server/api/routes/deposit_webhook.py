"""
OxaPay Merchant deposit webhook handler.

OxaPay POSTs payment status updates to DEPOSIT_WEBHOOK_URL (our /webhooks/deposit).
Authenticity is verified via HMAC-SHA512(raw_body, merchant_key) in the 'HMAC' header.

Webhook payload (OxaPay merchant payment events):
  type        — "payment"
  trackId     — OxaPay internal invoice ID
  orderId     — our deposit_id (DEP-...)
  status      — Waiting | Confirming | Paid | Expired | Error | Overpaid | Underpaid | Refunded
  amount      — amount paid by customer
  currency    — e.g. "USDT"
  network     — e.g. "TRX", "BSC"
  address     — destination wallet address
  txID        — blockchain transaction hash (present when confirmed)
  date        — UNIX timestamp

Security:
  - HMAC-SHA512(raw_body_bytes, OXAPAY_MERCHANT_KEY) must match 'HMAC' request header
  - Duplicate / replayed webhooks are idempotent (no double-credit)
  - Always respond HTTP 200 "OK" — OxaPay stops retrying on 200
"""

from __future__ import annotations

import hashlib
import hmac
import json
from os import getenv

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from server.logging import LOGGER
from server.services.deposit.providers.oxapay import map_oxapay_status
from server.utils.database.walletdb import (
    confirm_deposit,
    depositsdb,
    get_deposit,
)
from server.utils.common import utcnow

_log = LOGGER(__name__)
router = APIRouter(tags=["Deposit-Webhook"], include_in_schema=False)

_TERMINAL_FAILURE_STATUSES = frozenset({"expired", "failed", "rejected"})


@router.post("/webhooks/deposit", response_class=PlainTextResponse)
async def deposit_webhook(request: Request) -> PlainTextResponse:
    """
    Receive OxaPay payment status updates and credit confirmed deposits.

    OxaPay retries up to 5 times on non-200 responses.
    This handler is fully idempotent — duplicate deliveries are safe.
    """
    raw_body = await request.body()

    # ── HMAC authentication ───────────────────────────────────────────────────
    if not _verify_hmac(raw_body, request.headers.get("HMAC", "")):
        # Log only IP + body length — never log raw payment payload (info leakage)
        _log.warning(
            "Deposit webhook: HMAC mismatch. IP=%s body_len=%d",
            _client_ip(request), len(raw_body),
        )
        # Always 200 — do not reveal HMAC failure via HTTP status
        return PlainTextResponse("OK", status_code=200)

    # ── Parse payload ─────────────────────────────────────────────────────────
    try:
        payload: dict = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.error("Deposit webhook: invalid JSON: %s", exc)
        return PlainTextResponse("OK", status_code=200)

    # ── Validate event type ───────────────────────────────────────────────────
    if payload.get("type") != "payment":
        _log.debug("Deposit webhook: ignoring event type '%s'", payload.get("type"))
        return PlainTextResponse("OK", status_code=200)

    order_id      = payload.get("orderId") or payload.get("order_id", "")
    track_id      = payload.get("trackId") or payload.get("track_id", "")
    oxapay_status = payload.get("status", "")
    tx_hash       = payload.get("txID") or payload.get("tx_hash") or None
    network       = payload.get("network", "")

    _log.info(
        "Deposit webhook: order_id=%s track_id=%s status=%s tx_hash=%s",
        order_id, track_id, oxapay_status, tx_hash,
    )

    if not order_id:
        _log.warning("Deposit webhook: missing orderId in payload: %s", payload)
        return PlainTextResponse("OK", status_code=200)

    # ── Look up deposit record ────────────────────────────────────────────────
    dep = await get_deposit(order_id)
    if not dep:
        _log.warning("Deposit webhook: no deposit found for order_id=%s", order_id)
        return PlainTextResponse("OK", status_code=200)

    current_status  = dep.get("status", "")
    internal_status = map_oxapay_status(oxapay_status)
    _log.info(
        "Deposit webhook DB before: deposit_id=%s status=%s balance_credited=%s document=%r",
        order_id, current_status, dep.get("balance_credited"), dep,
    )

    # Failed/expired/rejected records are terminal.  A completed record is not
    # skipped here because confirm_deposit also recovers a stale credit claim.
    if current_status in _TERMINAL_FAILURE_STATUSES:
        _log.info(
            "Deposit webhook: %s already in final state '%s' — skipping",
            order_id, current_status,
        )
        return PlainTextResponse("OK", status_code=200)

    # ── Route by internal status ──────────────────────────────────────────────

    if internal_status == "completed":
        # Atomic claim + balance credit (idempotent — returns None if already done)
        dep_doc = await confirm_deposit(order_id, confirmed_by=None)
        if dep_doc:
            _log.info(
                "Deposit webhook: credited $%.4f to user %s (deposit=%s tx=%s)",
                dep_doc["amount"], dep_doc["user_id"], order_id, tx_hash,
            )
            await _notify_user(
                dep_doc["user_id"], "deposit_confirmed",
                deposit_id=order_id,
                amount=dep_doc["amount"],
                method="oxapay",
            )
        else:
            current = await get_deposit(order_id)
            if current and current.get("status") == "completed" and current.get("balance_credited") is True:
                _log.info("Deposit webhook: %s already credited (idempotent no-op)", order_id)
            else:
                _log.warning(
                    "Deposit webhook: %s credit not claimed; status=%s balance_credited=%s document=%r",
                    order_id,
                    (current or {}).get("status"),
                    (current or {}).get("balance_credited"),
                    current,
                )

    elif internal_status in ("expired", "failed"):
        result = await depositsdb.update_one(
            {
                "deposit_id": order_id,
                "status": {"$in": ["pending", "confirming"]},
            },
            {"$set": {
                "status": internal_status,
                "note":   f"OxaPay status: {oxapay_status}",
                "extra.tx_hash":       tx_hash,
                "extra.oxapay_status": oxapay_status,
            }},
        )
        if result.modified_count:
            _log.info(
                "Deposit webhook: %s → %s (oxapay: %s)",
                order_id, internal_status, oxapay_status,
            )

    elif internal_status == "confirming":
        result = await depositsdb.update_one(
            {"deposit_id": order_id, "status": "pending"},
            {"$set": {
                "status":              "confirming",
                "extra.tx_hash":       tx_hash,
                "extra.oxapay_status": oxapay_status,
                "extra.network":       network,
            }},
        )
        after = await get_deposit(order_id)
        _log.info(
            "Deposit webhook confirming update: deposit_id=%s matched=%s modified=%s "
            "status=%s balance_credited=%s document=%r",
            order_id,
            result.matched_count,
            result.modified_count,
            (after or {}).get("status"),
            (after or {}).get("balance_credited"),
            after,
        )

    else:
        _log.debug(
            "Deposit webhook: %s — no action for status '%s' (internal: %s)",
            order_id, oxapay_status, internal_status,
        )

    return PlainTextResponse("OK", status_code=200)


# ── HMAC verification ─────────────────────────────────────────────────────────

def _verify_hmac(raw_body: bytes, received_hmac: str) -> bool:
    """
    Validate OxaPay HMAC-SHA512 signature.
    Expected: HMAC-SHA512(raw_body_bytes, OXAPAY_MERCHANT_KEY.encode('utf-8'))
    """
    merchant_key = getenv("OXAPAY_MERCHANT_KEY", "")
    if not merchant_key:
        # Key not configured → reject all incoming webhooks to prevent forged callbacks
        _log.error(
            "OXAPAY_MERCHANT_KEY not set — rejecting deposit webhook (set the secret to enable)"
        )
        return False

    if not received_hmac:
        return False

    try:
        expected = hmac.new(
            merchant_key.encode("utf-8"),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, received_hmac)
    except Exception as exc:
        _log.error("Deposit HMAC computation error: %s", exc)
        return False


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _notify_user(user_id: int, event: str, **kwargs) -> None:
    try:
        from server.utils.notifications import notify
        await notify(user_id, event, **kwargs)
    except Exception as exc:
        _log.warning("Deposit webhook notify failed for user %s: %s", user_id, exc)
