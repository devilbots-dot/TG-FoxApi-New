"""
Binance Pay Merchant webhook handler.

Binance POSTs signed payment status updates to /webhooks/binance_pay.
Signature is verified via HMAC-SHA512 using BINANCE_PAY_SECRET_KEY.

Webhook headers:
  BinancePay-Timestamp  — millisecond timestamp when the request was sent
  BinancePay-Nonce      — random 32-char string
  BinancePay-Signature  — HMAC-SHA512("{ts}\\n{nonce}\\n{body}\\n", secret_key).upper()

Webhook body (bizType=PAY):
  {
    "bizType": "PAY",
    "bizId":   "...",
    "bizStatus": "PAY_SUCCESS" | "PAY_CLOSED",
    "data": "{JSON string with order details}"
  }

data (decoded):
  merchantTradeNo  — our deposit_id (DEP-...)
  transactionId    — Binance Pay internal transaction ID
  totalFee         — amount paid
  currency         — e.g. "USDT"
  openUserId       — Binance user ID (hashed)

Response: {"returnCode": "SUCCESS", "returnMessage": null}

Security:
  - HMAC-SHA512 signature required (silently ignored if invalid)
  - Idempotent — duplicate callbacks cause no double-credit
  - Always respond 200 with returnCode SUCCESS
"""

from __future__ import annotations

import json
from os import getenv

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.logging import LOGGER
from server.services.deposit.providers.binance_pay import verify_webhook_signature
from server.utils.database.walletdb import confirm_deposit, depositsdb, get_deposit

_log = LOGGER(__name__)
router = APIRouter(tags=["Binance-Pay-Webhook"], include_in_schema=False)

_FINAL_STATUSES = frozenset({"completed", "expired", "failed", "rejected"})
_OK_RESPONSE    = JSONResponse({"returnCode": "SUCCESS", "returnMessage": None})


@router.post("/webhooks/binance_pay")
async def binance_pay_webhook(request: Request) -> JSONResponse:
    """
    Receive Binance Pay payment status callbacks and credit confirmed deposits.

    Binance retries on non-SUCCESS returnCode.
    This handler is fully idempotent — duplicate deliveries are safe.
    """
    raw_body = await request.body()
    body_str = raw_body.decode("utf-8", errors="replace")

    # ── HMAC verification ─────────────────────────────────────────────────────
    secret_key = getenv("BINANCE_PAY_SECRET_KEY", "").strip()
    timestamp  = request.headers.get("BinancePay-Timestamp", "")
    nonce      = request.headers.get("BinancePay-Nonce", "")
    signature  = request.headers.get("BinancePay-Signature", "")

    if not secret_key:
        _log.error(
            "BINANCE_PAY_SECRET_KEY not set — rejecting BinancePay webhook to prevent "
            "forged payment confirmations. Set the secret to enable auto-processing."
        )
        # Return SUCCESS so Binance stops retrying; we will not credit without auth.
        return _OK_RESPONSE

    if not verify_webhook_signature(secret_key, timestamp, nonce, body_str, signature):
        _log.warning(
            "BinancePay webhook: signature mismatch. IP=%s",
            _client_ip(request),
        )
        return _OK_RESPONSE

    # ── Parse payload ─────────────────────────────────────────────────────────
    try:
        payload: dict = json.loads(body_str)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.error("BinancePay webhook: invalid JSON: %s", exc)
        return _OK_RESPONSE

    biz_type   = payload.get("bizType", "")
    biz_status = payload.get("bizStatus", "")
    data_raw   = payload.get("data", "")

    _log.info(
        "BinancePay webhook: bizType=%s bizStatus=%s",
        biz_type, biz_status,
    )

    if biz_type != "PAY":
        _log.debug("BinancePay webhook: ignoring bizType=%s", biz_type)
        return _OK_RESPONSE

    # ── Decode data JSON string ───────────────────────────────────────────────
    try:
        if isinstance(data_raw, str):
            order_data: dict = json.loads(data_raw)
        else:
            order_data = data_raw or {}
    except (json.JSONDecodeError, ValueError) as exc:
        _log.error("BinancePay webhook: invalid data JSON: %s | raw=%s", exc, data_raw[:300])
        return _OK_RESPONSE

    deposit_id     = order_data.get("merchantTradeNo", "")
    transaction_id = order_data.get("transactionId", "")
    total_fee      = order_data.get("totalFee", "")
    currency       = order_data.get("currency", "USDT")

    if not deposit_id:
        _log.warning("BinancePay webhook: missing merchantTradeNo in data: %s", order_data)
        return _OK_RESPONSE

    # ── Look up deposit record ────────────────────────────────────────────────
    dep = await get_deposit(deposit_id)
    if not dep:
        _log.warning("BinancePay webhook: no deposit found for id=%s", deposit_id)
        return _OK_RESPONSE

    current_status = dep.get("status", "")

    # ── Idempotency guard ─────────────────────────────────────────────────────
    if current_status in _FINAL_STATUSES:
        _log.info(
            "BinancePay webhook: %s already in final state '%s' — skipping",
            deposit_id, current_status,
        )
        return _OK_RESPONSE

    # ── Route by bizStatus ────────────────────────────────────────────────────
    if biz_status == "PAY_SUCCESS":
        dep_doc = await confirm_deposit(deposit_id, confirmed_by=None)
        if dep_doc:
            _log.info(
                "BinancePay webhook: credited $%.4f to user %s (deposit=%s tx=%s)",
                dep_doc["amount"], dep_doc["user_id"], deposit_id, transaction_id,
            )
            # Store Binance transaction ID
            if transaction_id:
                await depositsdb.update_one(
                    {"deposit_id": deposit_id},
                    {"$set": {
                        "tx_hash":            transaction_id,
                        "extra.tx_hash":      transaction_id,
                        "extra.biz_status":   biz_status,
                        "extra.currency":     currency,
                        "extra.total_fee":    total_fee,
                    }},
                )
            await _notify_user(
                dep_doc["user_id"], "deposit_confirmed",
                deposit_id=deposit_id,
                amount=dep_doc["amount"],
                method="binance_pay",
            )
        else:
            _log.info("BinancePay webhook: %s already credited (idempotent no-op)", deposit_id)

    elif biz_status == "PAY_CLOSED":
        result = await depositsdb.update_one(
            {
                "deposit_id": deposit_id,
                "status":     {"$in": ["pending", "confirming"]},
            },
            {"$set": {
                "status":           "expired",
                "note":             "Binance Pay order closed",
                "extra.biz_status": biz_status,
            }},
        )
        if result.modified_count:
            _log.info("BinancePay webhook: %s → expired (PAY_CLOSED)", deposit_id)

    else:
        _log.debug(
            "BinancePay webhook: %s — no action for bizStatus '%s'",
            deposit_id, biz_status,
        )

    return _OK_RESPONSE


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
        _log.warning("BinancePay webhook notify failed for user %s: %s", user_id, exc)
