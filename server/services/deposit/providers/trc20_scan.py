"""
TRC20 Direct Deposit Provider — TronGrid blockchain monitoring.

The admin provides a static USDT TRC20 wallet address. Users send USDT
directly to that address; this provider verifies the on-chain transaction
via the TronGrid API (Tron's official free API — no key required).

Configuration (Replit Secrets):
  DEPOSIT_TRC20_ADDRESS   — required; admin's USDT TRC20 wallet address
  TRONGRID_API_KEY        — optional; TronGrid API key for higher rate limits
                            (get free key at https://www.trongrid.io)
  DEPOSIT_SCAN_TTL        — optional; deposit lifetime in minutes (default: 60)

Verification:
  Uses TronGrid's TRC20 transfer API. Matches by exact amount and timestamp.
  Amount tolerance is ±$0.01 to handle minor floating-point differences.

Why TronGrid instead of TronScan:
  TronScan's public API (apilist.tronscan.org / apilist.tronscanapi.com)
  now returns HTTP 401 for all unauthenticated requests. TronGrid is
  Tron's official API, always free for basic use, and more reliable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from os import getenv
from typing import Optional

import aiohttp

from server.logging import LOGGER
from server.services.deposit.base import DepositProvider, PaymentDetails

_log = LOGGER(__name__)

# USDT TRC20 contract address on Tron
_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
_USDT_DECIMALS = 6          # 1 USDT = 1,000,000 units

_TRONGRID_BASE = "https://api.trongrid.io"
_TIMEOUT       = aiohttp.ClientTimeout(total=15, connect=8)
_AMOUNT_TOL    = 0.01        # $0.01 tolerance


class TRC20ScanProvider(DepositProvider):
    """
    Direct TRC20 USDT deposit — admin wallet + TronGrid verification.
    No external payment gateway required.
    """

    method_id   = "trc20_scan"
    method_name = "USDT (TRC20)"
    networks: list[str] = []

    def is_configured(self) -> bool:
        addr = getenv("DEPOSIT_TRC20_ADDRESS", "").strip()
        return bool(addr)

    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        address  = getenv("DEPOSIT_TRC20_ADDRESS", "").strip()
        ttl      = int(getenv("DEPOSIT_SCAN_TTL", "60"))
        expires  = datetime.now(timezone.utc) + timedelta(minutes=ttl)

        return PaymentDetails(
            currency    = "USDT",
            address     = address,
            expires_at  = expires,
            instructions= (
                f"Send exactly **${amount:.2f} USDT** (TRC20) to the address above. "
                f"After sending, tap **Verify Payment**. "
                f"Your balance will be credited once the transaction is found on-chain."
            ),
            extra={
                "provider":    "trc20_scan",
                "ttl_minutes": ttl,
                "network":     "TRC20",
            },
        )


# ── Blockchain verification ───────────────────────────────────────────────────

async def verify_transaction(
    address: str,
    amount: float,
    after: datetime,
) -> Optional[str]:
    """
    Query TronGrid for a recent incoming USDT TRC20 transfer to `address`
    that matches `amount` (±$0.01) and was confirmed after `after`.

    Returns the transaction hash on match, or None if not found yet.

    Uses TronGrid's /v1/accounts/{address}/transactions/trc20 endpoint
    (official Tron API, free, no auth required for basic use).
    Optional TRONGRID_API_KEY env var increases the rate limit.
    """
    if not address:
        return None

    # Convert `after` to millisecond timestamp for TronGrid filter
    after_ms = int(after.timestamp() * 1000)

    url = f"{_TRONGRID_BASE}/v1/accounts/{address}/transactions/trc20"
    params = {
        "limit":            50,
        "contract_address": _USDT_CONTRACT,
        "only_to":          "true",
        "min_timestamp":    after_ms,
    }

    headers: dict = {"Accept": "application/json"}
    api_key = getenv("TRONGRID_API_KEY", "").strip()
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    _log.warning("TronGrid returned HTTP %d for address %s…", resp.status, address[:8])
                    return None
                data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        _log.warning("TronGrid API timed out")
        return None
    except Exception as exc:
        _log.warning("TronGrid API error: %s", exc)
        return None

    if not data.get("success"):
        _log.warning("TronGrid API returned success=false: %s", data)
        return None

    transactions = data.get("data") or []
    for tx in transactions:
        try:
            # TronGrid returns value as a string of the raw integer amount
            raw_value = int(tx.get("value", "0"))
            tx_amount = raw_value / (10 ** _USDT_DECIMALS)

            # block_timestamp is in milliseconds
            block_ts_ms = int(tx.get("block_timestamp", 0))
            block_ts_s  = block_ts_ms / 1000

            to_addr = (tx.get("to") or "").strip()

            if (
                to_addr.lower() == address.lower()
                and abs(tx_amount - amount) <= _AMOUNT_TOL
                and block_ts_s >= after.timestamp()
            ):
                tx_hash = tx.get("transaction_id")
                _log.info(
                    "TronGrid match: amount=%.4f tx=%s",
                    tx_amount, tx_hash,
                )
                return tx_hash
        except Exception:
            continue

    return None
