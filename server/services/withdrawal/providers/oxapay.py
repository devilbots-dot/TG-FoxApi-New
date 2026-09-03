"""
OxaPay Payout provider.

API reference:
  POST   https://api.oxapay.com/v1/payout          → Generate Payout
  GET    https://api.oxapay.com/v1/payout/{track_id} → Payout Information
  GET    https://api.oxapay.com/v1/payout            → Payout History

Authentication:
  Header:  payout_api_key: <YOUR_KEY>
  Header:  Content-Type: application/json

OxaPay payout statuses:
  processing  → our "processing"
  pending     → our "waiting_confirmation"
  confirming  → our "waiting_confirmation"
  confirmed   → our "completed"
  canceled    → our "cancelled"
  rejected    → our "rejected"
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import aiohttp

import config
from server.logging import LOGGER
from server.services.withdrawal.base import WithdrawalProvider
from server.services.withdrawal.models import PayoutResult, VerifyResult
from server.utils.withdrawal_statuses import WithdrawalStatus

_log = LOGGER(__name__)

# Network name translation: our slug → OxaPay network identifier
_NETWORK_MAP: dict[str, str] = {
    "TRC20": config.OXAPAY_NETWORK_TRC20,   # default: "TRX"
    "BEP20": config.OXAPAY_NETWORK_BEP20,   # default: "BSC"
}

# Request timeout (seconds)
_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


class OxaPayWithdrawalProvider(WithdrawalProvider):
    """
    OxaPay Payout API integration.

    Configured via environment variables:
      OXAPAY_PAYOUT_API_KEY    — required; payout API key from OxaPay dashboard
      OXAPAY_BASE_URL          — optional; default https://api.oxapay.com/v1
      OXAPAY_WITHDRAW_CURRENCY — optional; default USDT
      OXAPAY_NETWORK_TRC20     — optional; default TRX
      OXAPAY_NETWORK_BEP20     — optional; default BSC
    """

    provider_id          = "oxapay"
    provider_name        = "OxaPay"
    supported_networks   = ["TRC20", "BEP20"]
    supported_currencies = ["USDT"]

    def __init__(self) -> None:
        self._api_key  = config.OXAPAY_PAYOUT_API_KEY
        self._base_url = config.OXAPAY_BASE_URL.rstrip("/")

    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_key.strip())

    @property
    def _headers(self) -> dict:
        return {
            "payout_api_key": self._api_key,
            "Content-Type":   "application/json",
        }

    def translate_network(self, network: str) -> str:
        """Convert TRC20 → TRX, BEP20 → BSC, etc."""
        return _NETWORK_MAP.get(network.upper(), network)

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit_payout(
        self,
        *,
        withdrawal_id: str,
        address: str,
        currency: str,
        network: str,
        amount: float,
        callback_url: str = "",
        memo: str = "",
    ) -> PayoutResult:
        """
        POST /payout — generate a payout request.

        Required fields: address, currency, amount
        Optional fields: network, callback_url, memo, description
        """
        oxapay_network = self.translate_network(network)

        payload: dict = {
            "address":     address,
            "currency":    currency.upper(),
            "amount":      round(amount, 8),   # crypto precision
            "network":     oxapay_network,
            "description": f"Withdrawal {withdrawal_id}",
            "sandbox":     config.OXAPAY_SANDBOX,
        }
        if callback_url:
            payload["callback_url"] = callback_url
        if memo:
            payload["memo"] = memo

        # Sanitized copy for logging (no secrets in payload, but keep for audit)
        raw_request = dict(payload)

        url = f"{self._base_url}/payout"
        _log.info("OxaPay submit_payout: %s → %s %s %s",
                  withdrawal_id, amount, currency, oxapay_network)

        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, json=payload, headers=self._headers) as resp:
                    raw_text = await resp.text()
                    try:
                        data = json.loads(raw_text)
                    except json.JSONDecodeError:
                        _log.error("OxaPay non-JSON response (%d): %s", resp.status, raw_text[:500])
                        return PayoutResult(
                            success=False,
                            message=f"Gateway returned non-JSON response (HTTP {resp.status})",
                            raw_request=raw_request,
                            raw_response={"_raw": raw_text[:2000]},
                            internal_status=WithdrawalStatus.FAILED,
                        )

            return self._parse_submit_response(data, raw_request)

        except asyncio.TimeoutError:
            _log.error("OxaPay submit_payout timeout for %s", withdrawal_id)
            return PayoutResult(
                success=False,
                message="Gateway request timed out. Will retry.",
                raw_request=raw_request,
                internal_status=WithdrawalStatus.PENDING,   # keep pending, retry
            )
        except aiohttp.ClientError as exc:
            _log.error("OxaPay submit_payout network error for %s: %s", withdrawal_id, exc)
            return PayoutResult(
                success=False,
                message=f"Gateway network error: {exc}",
                raw_request=raw_request,
                internal_status=WithdrawalStatus.PENDING,  # keep pending, retry
            )

    async def verify_payout(self, track_id: str) -> VerifyResult:
        """GET /payout/{track_id} — retrieve current payout status."""
        url = f"{self._base_url}/payout/{track_id}"
        _log.debug("OxaPay verify_payout: %s", track_id)

        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.get(url, headers=self._headers) as resp:
                    raw_text = await resp.text()
                    try:
                        data = json.loads(raw_text)
                    except json.JSONDecodeError:
                        return VerifyResult(
                            found=False,
                            track_id=track_id,
                            message=f"Non-JSON response (HTTP {resp.status})",
                            raw_response={"_raw": raw_text[:2000]},
                        )

            return self._parse_verify_response(data, track_id)

        except asyncio.TimeoutError:
            return VerifyResult(found=False, track_id=track_id, message="Verification request timed out")
        except aiohttp.ClientError as exc:
            return VerifyResult(found=False, track_id=track_id, message=f"Network error: {exc}")

    async def get_payout_history(
        self,
        *,
        status: Optional[str] = None,
        currency: Optional[str] = None,
        network: Optional[str] = None,
        page: int = 1,
        size: int = 50,
        from_date: Optional[int] = None,
        to_date: Optional[int] = None,
    ) -> dict:
        """GET /payout — list payout history from OxaPay."""
        params: dict = {"page": page, "size": size}
        if status:
            params["status"] = status
        if currency:
            params["currency"] = currency
        if network:
            params["network"] = self.translate_network(network)
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date

        url = f"{self._base_url}/payout"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.get(url, params=params, headers=self._headers) as resp:
                    raw_text = await resp.text()
                    try:
                        return json.loads(raw_text)
                    except json.JSONDecodeError:
                        return {"error": raw_text[:500]}
        except Exception as exc:
            return {"error": str(exc)}

    # ── Response parsers ──────────────────────────────────────────────────────

    def _parse_submit_response(self, data: dict, raw_request: dict) -> PayoutResult:
        """
        Parse a POST /payout response.

        Success (HTTP 200 + status=200):
          { "data": {"track_id": "...", "status": "..."}, "message": "...", "status": 200 }

        Error (HTTP 400 or status != 200):
          { "data": {}, "message": "...", "error": {"type": "...", "key": "...", "message": "..."}, "status": 400 }
        """
        api_status = data.get("status")
        message    = data.get("message", "")
        error_obj  = data.get("error") or {}
        pdata      = data.get("data") or {}

        if api_status == 200 and pdata.get("track_id"):
            oxapay_status  = pdata.get("status", "processing")
            internal_status = WithdrawalStatus.from_oxapay(oxapay_status)
            return PayoutResult(
                success=True,
                track_id=pdata["track_id"],
                gateway_status=oxapay_status,
                internal_status=internal_status,
                message=message,
                raw_request=raw_request,
                raw_response=data,
            )
        else:
            err_msg = error_obj.get("message") or message or "Unknown gateway error"
            _log.warning("OxaPay payout submission failed: status=%s message=%s error=%s",
                         api_status, message, error_obj)
            return PayoutResult(
                success=False,
                gateway_status=None,
                internal_status=WithdrawalStatus.FAILED,
                message=err_msg,
                error_type=error_obj.get("type"),
                error_key=error_obj.get("key"),
                raw_request=raw_request,
                raw_response=data,
            )

    def _parse_verify_response(self, data: dict, track_id: str) -> VerifyResult:
        """
        Parse a GET /payout/{track_id} response.

        Success data fields:
          track_id, address, currency, network, amount, fee,
          status, tx_hash, description, internal, memo, date
        """
        api_status = data.get("status")
        message    = data.get("message", "")
        pdata      = data.get("data") or {}

        if api_status == 200 and pdata.get("track_id"):
            oxapay_status   = pdata.get("status", "")
            internal_status = WithdrawalStatus.from_oxapay(oxapay_status)
            return VerifyResult(
                found=True,
                track_id=pdata.get("track_id", track_id),
                gateway_status=oxapay_status,
                internal_status=internal_status,
                tx_hash=pdata.get("tx_hash") or None,
                fee=pdata.get("fee"),
                message=message,
                raw_response=data,
            )
        else:
            error_obj = data.get("error") or {}
            err_msg = error_obj.get("message") or message or "Payout not found or unavailable"
            return VerifyResult(
                found=False,
                track_id=track_id,
                message=err_msg,
                raw_response=data,
            )
