"""
Binance Pay Merchant API Deposit Provider.

Creates a real Binance Pay checkout order via the Binance Pay Merchant API v3.
Users are redirected to the official Binance Pay checkout URL.
Payments are confirmed automatically via webhook callback.
The "Verify Payment" button manually polls the order status.

Configuration (Replit Secrets):
  BINANCE_PAY_API_KEY    — required; Merchant API key from Binance Pay dashboard
  BINANCE_PAY_SECRET_KEY — required; Merchant secret key from Binance Pay dashboard

Optional:
  BINANCE_PAY_WEBHOOK_URL — explicit webhook URL (auto-detected from REPLIT_DEV_DOMAIN)
  DEPOSIT_SCAN_TTL        — deposit lifetime in minutes (default: 60)

Binance Pay Merchant API docs (v3):
  https://developers.binance.com/en/docs/products/binance-pay-merchant/api-order-create-v3
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import string
import time
from datetime import datetime, timezone, timedelta
from os import getenv
from typing import Optional

import aiohttp

from server.logging import LOGGER
from server.services.deposit.base import DepositProvider, PaymentDetails

_log = LOGGER(__name__)

_BASE_URL = "https://bpay.binanceapi.com"
_TIMEOUT  = aiohttp.ClientTimeout(total=30, connect=10)

# Binance Pay order status → internal deposit status
_STATUS_MAP: dict[str, str] = {
    "INITIAL":   "pending",
    "PENDING":   "pending",
    "PAID":      "completed",
    "CANCELED":  "expired",
    "EXPIRED":   "expired",
    "ERROR":     "failed",
    "REFUNDING": "failed",
    "REFUNDED":  "failed",
}


def map_binance_status(binance_status: str) -> str:
    """Translate Binance Pay order status → internal deposit status."""
    return _STATUS_MAP.get(binance_status.upper(), "pending")


def _nonce(length: int = 32) -> str:
    """Generate a random alphanumeric nonce."""
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def _sign(secret_key: str, timestamp: str, nonce: str, body_str: str) -> str:
    """
    Binance Pay HMAC-SHA512 signature.
    payload = "{timestamp}\\n{nonce}\\n{body}\\n"
    signature = HMAC-SHA512(secret_key, payload).hexdigest().upper()
    """
    payload = f"{timestamp}\n{nonce}\n{body_str}\n"
    return hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest().upper()


def _build_headers(api_key: str, secret_key: str, body_str: str) -> dict:
    ts    = str(int(time.time() * 1000))
    nc    = _nonce()
    sig   = _sign(secret_key, ts, nc, body_str)
    return {
        "Content-Type":              "application/json",
        "BinancePay-Timestamp":      ts,
        "BinancePay-Nonce":          nc,
        "BinancePay-Certificate-SN": api_key,
        "BinancePay-Signature":      sig,
    }


class BinancePayProvider(DepositProvider):
    """
    Binance Pay Merchant API deposit provider (v3).

    POST /binancepay/openapi/v3/order → get checkoutUrl + prepayId
    Webhook POST /webhooks/binance_pay → balance credited automatically
    """

    method_id   = "binance_pay"
    method_name = "Binance Pay"
    networks: list[str] = []

    def is_configured(self) -> bool:
        api_key    = getenv("BINANCE_PAY_API_KEY", "").strip()
        secret_key = getenv("BINANCE_PAY_SECRET_KEY", "").strip()
        return bool(api_key and secret_key)

    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        api_key    = getenv("BINANCE_PAY_API_KEY", "").strip()
        secret_key = getenv("BINANCE_PAY_SECRET_KEY", "").strip()
        ttl        = int(getenv("DEPOSIT_SCAN_TTL", "60"))

        import config as _cfg
        webhook_url = _cfg.BINANCE_PAY_WEBHOOK_URL

        payload = {
            "env": {"terminalType": "WEB"},
            "merchantTradeNo": deposit_id,
            "orderAmount": round(amount, 4),
            "currency": "USDT",
            "description": f"Balance Deposit — ${amount:.2f} USDT",
            "goodsDetails": [
                {
                    "goodsType":        "02",
                    "goodsCategory":    "Z000",
                    "referenceGoodsId": deposit_id,
                    "goodsName":        "Balance Deposit",
                    "goodsDetail":      f"Deposit {deposit_id} — ${amount:.2f} USDT",
                }
            ],
        }
        # webhookUrl: Binance Pay POSTs PAY_SUCCESS / PAY_CLOSED callbacks here.
        # (auto-detected from REPLIT_DEV_DOMAIN when running on Replit)
        if webhook_url:
            payload["webhookUrl"] = webhook_url

        body_str = json.dumps(payload)
        headers  = _build_headers(api_key, secret_key, body_str)
        url      = f"{_BASE_URL}/binancepay/openapi/v3/order"

        _log.info(
            "BinancePay create_order: deposit_id=%s amount=%.4f user=%s",
            deposit_id, amount, user_id,
        )

        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, data=body_str, headers=headers) as resp:
                    raw_text = await resp.text()
                    try:
                        data = json.loads(raw_text)
                    except json.JSONDecodeError:
                        _log.error(
                            "BinancePay non-JSON response (HTTP %d): %s",
                            resp.status, raw_text[:500],
                        )
                        raise RuntimeError(
                            f"Binance Pay returned an invalid response (HTTP {resp.status}). "
                            "Please try again."
                        )
        except asyncio.TimeoutError:
            _log.error("BinancePay create_order timeout: deposit_id=%s", deposit_id)
            raise RuntimeError("Binance Pay timed out. Please try again.")
        except aiohttp.ClientError as exc:
            _log.error("BinancePay create_order network error: %s", exc)
            raise RuntimeError(f"Cannot reach Binance Pay: {exc}")

        return self._parse_response(data, deposit_id, amount, ttl)

    def _parse_response(
        self, data: dict, deposit_id: str, amount: float, ttl_minutes: int
    ) -> PaymentDetails:
        status = data.get("status", "")
        code   = data.get("code", "")

        if status != "SUCCESS" or code != "000000":
            err_msg = data.get("errorMessage") or data.get("message") or f"code={code}"
            _log.warning(
                "BinancePay order creation failed: status=%s code=%s msg=%s",
                status, code, err_msg,
            )
            raise RuntimeError(
                f"Binance Pay error: {err_msg}. Please try again."
            )

        result       = data.get("data") or {}
        checkout_url = result.get("checkoutUrl") or result.get("checkOutUrl")
        qrcode_link  = result.get("qrcodeLink")
        prepay_id    = result.get("prepayId")
        expire_ts    = result.get("expireTime")  # milliseconds

        if not checkout_url:
            _log.error("BinancePay response missing checkoutUrl: %s", data)
            raise RuntimeError(
                "Binance Pay did not return a checkout URL. Please try again."
            )

        # Parse expiry (milliseconds epoch)
        if expire_ts:
            try:
                expires_at = datetime.fromtimestamp(int(expire_ts) / 1000, tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

        _log.info("BinancePay order created: prepay_id=%s deposit_id=%s", prepay_id, deposit_id)

        return PaymentDetails(
            currency    = "USDT",
            payment_url = checkout_url,
            qr_url      = qrcode_link,
            expires_at  = expires_at,
            instructions= (
                "Tap **Pay Now** to open the Binance Pay checkout page. "
                "Complete the payment in USDT. "
                "Your balance will be credited automatically after confirmation."
            ),
            extra={
                "provider":    "binance_pay",
                "prepay_id":   prepay_id,
                "ttl_minutes": ttl_minutes,
            },
        )


# ── Order status query ────────────────────────────────────────────────────────

async def query_order(deposit_id: str) -> dict:
    """
    POST /binancepay/openapi/v2/order/query — fetch current order status.

    Returns:
      ok        — True if request succeeded
      status    — Binance order status string (e.g. "PAID", "PENDING")
      internal  — mapped internal status (e.g. "completed", "pending")
      data      — full response data dict
      error     — error message string (when ok=False)
    """
    api_key    = getenv("BINANCE_PAY_API_KEY", "").strip()
    secret_key = getenv("BINANCE_PAY_SECRET_KEY", "").strip()

    if not api_key or not secret_key:
        return {"ok": False, "error": "BINANCE_PAY_API_KEY / BINANCE_PAY_SECRET_KEY not configured"}

    payload  = {"merchantTradeNo": deposit_id}
    body_str = json.dumps(payload)
    headers  = _build_headers(api_key, secret_key, body_str)
    url      = f"{_BASE_URL}/binancepay/openapi/v2/order/query"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, data=body_str, headers=headers) as resp:
                raw_text = await resp.text()
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    return {"ok": False, "error": f"Non-JSON response (HTTP {resp.status})"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Request timed out"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "error": str(exc)}

    if data.get("status") != "SUCCESS" or data.get("code") != "000000":
        err = data.get("errorMessage") or data.get("message") or f"code={data.get('code')}"
        return {"ok": False, "error": err, "data": data}

    order_data    = data.get("data") or {}
    order_status  = order_data.get("status", "")
    return {
        "ok":       True,
        "status":   order_status,
        "internal": map_binance_status(order_status),
        "data":     order_data,
    }


def verify_webhook_signature(
    secret_key: str,
    timestamp: str,
    nonce: str,
    body: str,
    received_sig: str,
) -> bool:
    """
    Verify a Binance Pay webhook callback signature.
    payload = "{timestamp}\\n{nonce}\\n{body}\\n"
    expected = HMAC-SHA512(secret_key, payload).upper()
    """
    if not secret_key or not received_sig:
        return False
    try:
        expected = _sign(secret_key, timestamp, nonce, body)
        return hmac.compare_digest(expected, received_sig.upper())
    except Exception as exc:
        _log.error("BinancePay signature verification error: %s", exc)
        return False
