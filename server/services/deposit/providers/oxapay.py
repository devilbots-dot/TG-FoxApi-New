"""
OxaPay Merchant deposit provider.

Creates a hosted payment invoice via OxaPay Merchant API.
The user opens the payLink to send crypto; OxaPay posts a webhook
to /webhooks/deposit when the payment status changes.

Configuration:
  OXAPAY_MERCHANT_KEY   — required; Merchant API key from OxaPay dashboard
  OXAPAY_DEPOSIT_TTL    — optional; invoice lifetime in minutes (default: 30)
  DEPOSIT_WEBHOOK_URL   — optional; explicit callback URL for OxaPay webhooks
                          Falls back to https://$REPLIT_DEV_DOMAIN/webhooks/deposit
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from os import getenv
from typing import Optional

import aiohttp

from server.logging import LOGGER
from server.services.deposit.base import DepositProvider, PaymentDetails

_log = LOGGER(__name__)

_MERCHANT_BASE = "https://api.oxapay.com"
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

# OxaPay merchant payment status → internal deposit status
_STATUS_MAP: dict[str, str] = {
    "waiting":    "pending",
    "confirming": "confirming",
    "sending":    "confirming",
    "paid":       "completed",
    "overpaid":   "completed",   # treat overpayment as paid
    "underpaid":  "confirming",  # partial — OxaPay may fire again on full payment
    "expired":    "expired",
    "error":      "failed",
    "refunded":   "failed",
}


def map_oxapay_status(oxapay_status: str) -> str:
    """Translate OxaPay merchant payment status → internal deposit status."""
    normalized = str(oxapay_status or "").strip().lower()
    return _STATUS_MAP.get(normalized, "pending")


class OxaPayDepositProvider(DepositProvider):
    """
    OxaPay hosted-payment deposit provider.

    POST /merchants/request  → get payLink + trackId
    Webhook POST /webhooks/deposit → balance credited automatically
    """

    method_id   = "oxapay"
    method_name = "Crypto (OxaPay)"
    networks: list[str] = ["TRC20", "BEP20", "ERC20", "BEP2"]

    def __init__(self) -> None:
        self._merchant_key = getenv("OXAPAY_MERCHANT_KEY", "")

    def is_configured(self) -> bool:
        return bool(self._merchant_key and self._merchant_key.strip())

    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        """
        Call POST /merchants/request to generate an OxaPay invoice.
        Returns PaymentDetails with payment_url (the hosted checkout link).
        Raises ValueError for invalid inputs, RuntimeError for gateway errors.
        """
        ttl_minutes  = int(getenv("OXAPAY_DEPOSIT_TTL", "30"))
        callback_url = _build_callback_url()

        import config as _cfg
        payload: dict = {
            "merchant":    self._merchant_key,
            "amount":      round(amount, 4),
            "currency":    "USDT",
            "lifeTime":    ttl_minutes,
            "callbackUrl": callback_url,
            "description": f"Deposit {deposit_id}",
            "orderId":     deposit_id,
            "sandbox":     _cfg.OXAPAY_SANDBOX,
        }
        if network:
            payload["network"] = network

        url = f"{_MERCHANT_BASE}/merchants/request"
        _log.info(
            "OxaPay create_invoice: deposit_id=%s amount=%.4f user=%s network=%s",
            deposit_id, amount, user_id, network,
        )

        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, json=payload) as resp:
                    raw_text = await resp.text()
                    try:
                        data = json.loads(raw_text)
                    except json.JSONDecodeError:
                        _log.error(
                            "OxaPay non-JSON response (HTTP %d): %s",
                            resp.status, raw_text[:500],
                        )
                        raise RuntimeError(
                            f"Payment gateway returned an invalid response (HTTP {resp.status}). "
                            "Please try again."
                        )

        except asyncio.TimeoutError:
            _log.error("OxaPay create_invoice timeout: deposit_id=%s", deposit_id)
            raise RuntimeError("Payment gateway timed out. Please try again.")
        except aiohttp.ClientError as exc:
            _log.error("OxaPay create_invoice network error: %s", exc)
            raise RuntimeError(f"Cannot reach payment gateway: {exc}")

        return self._parse_response(data, ttl_minutes)

    # ── Response parser ───────────────────────────────────────────────────────

    def _parse_response(self, data: dict, ttl_minutes: int) -> PaymentDetails:
        """
        Parse POST /merchants/request response.

        Success: { result: 100, message: "ok", payLink: "...", trackId: "...", expiredAt: ts }
        Error:   { result: <non-100>, message: "Error description" }
        """
        result  = data.get("result")
        message = data.get("message", "")

        if result != 100:
            _log.warning(
                "OxaPay invoice creation failed: result=%s message=%s",
                result, message,
            )
            raise RuntimeError(
                f"Payment gateway error: {message or 'Unknown error'}. Please try again."
            )

        pay_link = (
            data.get("payLink")
            or data.get("paylink")
            or data.get("pay_link")
        )
        track_id       = data.get("trackId") or data.get("track_id")
        expired_at_ts  = data.get("expiredAt")

        if not pay_link:
            _log.error("OxaPay response missing payLink: %s", data)
            raise RuntimeError(
                "Payment gateway did not return a payment link. Please try again."
            )

        # Parse expiry timestamp
        if expired_at_ts:
            try:
                expires_at = datetime.fromtimestamp(int(expired_at_ts), tz=timezone.utc)
            except (ValueError, TypeError, OSError):
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        else:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)

        _log.info("OxaPay invoice created: track_id=%s", track_id)

        return PaymentDetails(
            currency="USDT",
            payment_url=pay_link,
            expires_at=expires_at,
            instructions=(
                "Click the payment link to open the OxaPay checkout page. "
                "Send USDT on your chosen network. "
                "Your balance will be credited automatically after on-chain confirmation."
            ),
            extra={"track_id": track_id, "provider": "oxapay"},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def inquiry_payment(merchant_key: str, track_id: str) -> dict:
    """
    POST /merchants/inquiry — fetch current invoice status from OxaPay.

    Returns dict with keys:
      ok       — True if request succeeded
      status   — OxaPay status string (e.g. "Paid", "Waiting")
      internal — mapped internal status (e.g. "completed", "pending")
      data     — full response data dict
      error    — error message string (when ok=False)
    """
    payload = {"merchant": merchant_key, "trackId": track_id}
    url = f"{_MERCHANT_BASE}/merchants/inquiry"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=payload) as resp:
                raw_text = await resp.text()
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    return {"ok": False, "error": f"Non-JSON response (HTTP {resp.status})"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Request timed out"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "error": str(exc)}

    return _parse_inquiry_response(data, track_id)


def _parse_inquiry_response(data: dict, track_id: str = "") -> dict:
    """Normalize both response shapes returned by OxaPay's inquiry API."""
    result = data.get("result")
    if result != 100:
        msg = data.get("message", "Unknown error")
        return {"ok": False, "error": msg, "data": data}

    # OxaPay has returned both legacy top-level fields and a nested `data`
    # object.  Read both shapes instead of converting a top-level Paid result
    # into an empty status (which maps to pending).
    nested = data.get("data")
    pdata = nested if isinstance(nested, dict) else data
    oxapay_status = pdata.get("status") or data.get("status") or ""
    if not oxapay_status:
        _log.warning(
            "OxaPay inquiry response missing status: track_id=%s keys=%s data_keys=%s",
            track_id,
            sorted(data.keys()),
            sorted(pdata.keys()) if isinstance(pdata, dict) else [],
        )
    return {
        "ok":       True,
        "status":   oxapay_status,
        "internal": map_oxapay_status(oxapay_status),
        "data":     pdata,
    }


def _build_callback_url() -> str:
    """
    Returns the deposit webhook URL from config (auto-resolved from
    REPLIT_DEV_DOMAIN on every restart, or overridden via DEPOSIT_WEBHOOK_URL).
    """
    import config as _config
    return _config.DEPOSIT_WEBHOOK_URL
