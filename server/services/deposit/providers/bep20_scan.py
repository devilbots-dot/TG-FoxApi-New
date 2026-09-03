"""
BEP20 Direct Deposit Provider — BSCScan blockchain monitoring.

The admin provides a static USDT BEP20 wallet address. Users send USDT
directly to that address; this provider verifies the on-chain transaction
via the BSCScan API.

Configuration (Replit Secrets):
  DEPOSIT_BEP20_ADDRESS   — required; admin's USDT BEP20 wallet address
  BSCSCAN_API_KEY         — optional but recommended (higher rate limit)
  DEPOSIT_SCAN_TTL        — optional; deposit lifetime in minutes (default: 60)

Binance-Peg USDT on BSC:
  Contract: 0x55d398326f99059fF775485246999027B3197955
  Decimals: 18

Verification:
  Uses BSCScan's token-transfer API. Matches by exact amount and timestamp.
  Amount tolerance is ±$0.01 to handle minor floating-point differences.
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

# Binance-Peg USDT contract on BSC
_USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
_USDT_DECIMALS = 18  # 1 USDT = 10^18 units

_BSCSCAN_API   = "https://api.bscscan.com/api"
_TIMEOUT       = aiohttp.ClientTimeout(total=15, connect=8)
_AMOUNT_TOL    = 0.01   # $0.01 tolerance


class BEP20ScanProvider(DepositProvider):
    """
    Direct BEP20 USDT deposit — admin wallet + BSCScan verification.
    No external payment gateway required.
    """

    method_id   = "bep20_scan"
    method_name = "USDT (BEP20)"
    networks: list[str] = []

    def is_configured(self) -> bool:
        addr = getenv("DEPOSIT_BEP20_ADDRESS", "").strip()
        return bool(addr)

    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        address  = getenv("DEPOSIT_BEP20_ADDRESS", "").strip()
        ttl      = int(getenv("DEPOSIT_SCAN_TTL", "60"))
        expires  = datetime.now(timezone.utc) + timedelta(minutes=ttl)

        return PaymentDetails(
            currency    = "USDT",
            address     = address,
            expires_at  = expires,
            instructions= (
                f"Send exactly **${amount:.2f} USDT** (BEP20 / BSC) to the address above. "
                f"After sending, tap **Verify Payment**. "
                f"Your balance will be credited once the transaction is found on-chain."
            ),
            extra={
                "provider":    "bep20_scan",
                "ttl_minutes": ttl,
                "network":     "BEP20",
            },
        )


# ── Blockchain verification ───────────────────────────────────────────────────

async def verify_transaction(
    address: str,
    amount: float,
    after: datetime,
) -> Optional[str]:
    """
    Query BSCScan for a recent incoming USDT BEP20 transfer to `address`
    that matches `amount` (±$0.01) and was confirmed after `after`.

    Returns the transaction hash on match, or None if not found yet.
    """
    if not address:
        return None

    api_key    = getenv("BSCSCAN_API_KEY", "YourApiKeyToken")
    after_ts   = int(after.timestamp())

    params = {
        "module":          "account",
        "action":          "tokentx",
        "contractaddress": _USDT_CONTRACT,
        "address":         address,
        "page":            1,
        "offset":          50,
        "sort":            "desc",
        "apikey":          api_key,
    }

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(_BSCSCAN_API, params=params) as resp:
                if resp.status != 200:
                    _log.warning("BSCScan returned HTTP %d", resp.status)
                    return None
                data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        _log.warning("BSCScan API timed out")
        return None
    except Exception as exc:
        _log.warning("BSCScan API error: %s", exc)
        return None

    if data.get("status") != "1":
        msg = data.get("message", "")
        if "No transactions" not in msg:
            _log.warning("BSCScan API error response: %s", msg)
        return None

    txns = data.get("result") or []
    for tx in txns:
        try:
            # Only look at incoming transfers to our address
            to_addr    = (tx.get("to") or "").lower()
            if to_addr != address.lower():
                continue

            decimals   = int(tx.get("tokenDecimal", _USDT_DECIMALS))
            raw_value  = int(tx.get("value", "0"))
            tx_amount  = raw_value / (10 ** decimals)
            tx_ts      = int(tx.get("timeStamp", 0))

            if abs(tx_amount - amount) <= _AMOUNT_TOL and tx_ts >= after_ts:
                tx_hash = tx.get("hash")
                _log.info(
                    "BSCScan match: amount=%.4f tx=%s",
                    tx_amount, tx_hash,
                )
                return tx_hash
        except Exception:
            continue

    return None
