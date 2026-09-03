"""
Shared data models for the withdrawal subsystem.

Withdrawal lifecycle (internal statuses):
  pending              → created, balance reserved, gateway not yet called
  processing           → gateway call succeeded; OxaPay is processing the request
  waiting_confirmation → gateway accepted; awaiting blockchain confirmation
  completed            → funds sent on-chain, balance finalized
  failed               → gateway call or blockchain failed; balance released
  cancelled            → user cancelled before gateway submission
  rejected             → admin or gateway rejected; balance released
  expired              → no resolution within expiry window; balance released
  unknown              → gateway returned an unrecognized status

OxaPay payout status → our internal status mapping:
  processing  → processing
  pending     → waiting_confirmation
  confirming  → waiting_confirmation
  confirmed   → completed
  canceled    → cancelled
  rejected    → rejected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# Re-export WithdrawalStatus from the zero-dependency module so callers can
# import from either server.services.withdrawal.models or .statuses.
from server.utils.withdrawal_statuses import WithdrawalStatus  # noqa: F401


# ── Gateway result dataclasses ────────────────────────────────────────────────

@dataclass
class PayoutResult:
    """
    Returned by WithdrawalProvider.submit_payout() after calling the gateway.
    """
    success: bool
    track_id: Optional[str] = None       # gateway-assigned transaction ID
    gateway_status: Optional[str] = None # raw status string from gateway
    internal_status: str = WithdrawalStatus.PENDING
    message: str = ""                    # human-readable message / error
    error_type: Optional[str] = None     # error.type from gateway
    error_key: Optional[str] = None      # error.key from gateway
    raw_request: Optional[dict] = None   # sanitized request body sent
    raw_response: Optional[dict] = None  # full response received


@dataclass
class VerifyResult:
    """
    Returned by WithdrawalProvider.verify_payout() after polling the gateway.
    """
    found: bool
    track_id: Optional[str] = None
    gateway_status: Optional[str] = None
    internal_status: str = WithdrawalStatus.UNKNOWN
    tx_hash: Optional[str] = None
    fee: Optional[float] = None
    message: str = ""
    raw_response: Optional[dict] = None


@dataclass
class WithdrawalLimits:
    """Configurable limits for a withdrawal request."""
    min_amount: float = 5.0
    max_amount: float = 10_000.0
    daily_limit: float = 1_000.0
    monthly_limit: float = 10_000.0
    max_pending: int = 3


@dataclass
class FeeCalculation:
    """Result of fee calculation for a withdrawal."""
    requested_amount: float    # amount user requested (USD)
    fee_amount: float          # fee deducted (USD)
    net_amount: float          # amount user receives (crypto)
    fee_percent: float         # percentage fee applied
    fee_fixed: float           # fixed fee applied

    @classmethod
    def calculate(
        cls,
        amount: float,
        fee_percent: float = 0.0,
        fee_fixed: float = 0.0,
    ) -> "FeeCalculation":
        fee = round(amount * fee_percent / 100.0 + fee_fixed, 4)
        net = round(amount - fee, 4)
        return cls(
            requested_amount=round(amount, 4),
            fee_amount=fee,
            net_amount=max(net, 0.0),
            fee_percent=fee_percent,
            fee_fixed=fee_fixed,
        )
