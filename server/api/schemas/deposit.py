"""Pydantic schemas for the Deposit API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator


# ── Request bodies ────────────────────────────────────────────────────────────

class CreateDepositIn(BaseModel):
    method: str
    network: Optional[str] = None
    amount: float

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Amount must be greater than 0.")
        return round(v, 4)

    @field_validator("method")
    @classmethod
    def lower_method(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("network")
    @classmethod
    def upper_network(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else None


# ── Deposit detail (public-facing) ────────────────────────────────────────────

class DepositDetailOut(BaseModel):
    deposit_id:  str
    status:      str
    status_label: Optional[str] = None   # human-readable status
    amount:      float
    currency:    Optional[str] = None
    network:     Optional[str] = None
    method:      str
    address:     Optional[str] = None
    payment_url: Optional[str] = None
    qr_url:      Optional[str] = None
    memo:        Optional[str] = None
    phone:       Optional[str] = None
    instructions: Optional[str] = None
    expires_at:  Optional[datetime] = None
    created_at:  Optional[datetime] = None
    is_final:    bool = False             # true when no further state changes expected
    extra:       Optional[Dict[str, Any]] = None

    model_config = {"json_schema_extra": {
        "example": {
            "deposit_id":   "DEP-ABC123",
            "status":       "pending",
            "status_label": "Awaiting Payment",
            "amount":       10.0,
            "currency":     "USDT",
            "network":      "TRC20",
            "method":       "oxapay",
            "address":      "TXxxx...",
            "payment_url":  "https://oxapay.com/pay/...",
            "expires_at":   "2026-08-01T09:00:00Z",
            "created_at":   "2026-08-01T08:00:00Z",
            "is_final":     False,
        }
    }}


_STATUS_LABELS: Dict[str, str] = {
    "pending":    "Awaiting Payment",
    "confirming": "Confirming on Blockchain",
    "completed":  "Confirmed & Credited",
    "expired":    "Expired",
    "failed":     "Failed",
    "rejected":   "Rejected",
}

_FINAL_STATUSES = frozenset({"completed", "expired", "failed", "rejected"})


def enrich_deposit(d: DepositDetailOut) -> DepositDetailOut:
    """Attach derived fields to a DepositDetailOut before returning it."""
    d.status_label = _STATUS_LABELS.get(d.status, d.status.title())
    d.is_final     = d.status in _FINAL_STATUSES
    return d


# ── Response envelopes ────────────────────────────────────────────────────────

_API_VERSION = "1"


def _meta() -> dict:
    from datetime import datetime, timezone
    return {"version": _API_VERSION, "timestamp": datetime.now(timezone.utc).isoformat()}


class DepositMethodOut(BaseModel):
    id:       str
    name:     str
    networks: Optional[List[str]] = None
    min_amount: Optional[float]   = None


class DepositMethodsOut(BaseModel):
    success: bool = True
    status:  bool = True
    message: str  = "Deposit methods retrieved successfully."
    data:    List[DepositMethodOut]
    meta:    Dict[str, Any] = None  # type: ignore[assignment]

    def model_post_init(self, __context: Any) -> None:
        if self.meta is None:
            self.meta = _meta()


class CreateDepositOut(BaseModel):
    success: bool = True
    status:  bool = True
    message: str  = "Deposit created. Send payment to the address provided."
    data:    DepositDetailOut
    meta:    Dict[str, Any] = None  # type: ignore[assignment]

    def model_post_init(self, __context: Any) -> None:
        if self.meta is None:
            self.meta = _meta()


class DepositStatusOut(BaseModel):
    success: bool = True
    status:  bool = True
    message: str  = "Deposit status retrieved successfully."
    data:    DepositDetailOut
    meta:    Dict[str, Any] = None  # type: ignore[assignment]

    def model_post_init(self, __context: Any) -> None:
        if self.meta is None:
            self.meta = _meta()


class DepositListOut(BaseModel):
    success: bool = True
    status:  bool = True
    message: str  = "Deposit history retrieved successfully."
    data:    List[DepositDetailOut]
    page:    Optional[int] = None
    limit:   Optional[int] = None
    total:   Optional[int] = None
    total_pages: Optional[int] = None
    has_next: Optional[bool] = None
    has_prev: Optional[bool] = None
    meta:    Dict[str, Any] = None  # type: ignore[assignment]

    def model_post_init(self, __context: Any) -> None:
        if self.meta is None:
            self.meta = _meta()
        if self.total is not None and self.limit and self.total > 0:
            self.total_pages = max(1, (self.total + self.limit - 1) // self.limit)
            p = self.page or 1
            self.has_next = p < self.total_pages
            self.has_prev = p > 1
