"""
Abstract base class for all deposit providers.

To add a new payment provider:
  1. Create `server/services/deposit/providers/<name>.py`
  2. Subclass DepositProvider and implement `create_payment()`
  3. Register it in `server/services/deposit/registry.py`

That's it — no endpoint changes needed.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PaymentDetails:
    """Returned by every provider after creating a deposit intent."""
    currency: str                          # e.g. "USDT", "BNB", "Stars"
    address: Optional[str] = None          # crypto wallet address
    payment_url: Optional[str] = None      # redirect / deep link
    qr_url: Optional[str] = None           # QR code image URL
    memo: Optional[str] = None             # MEMO/tag for some networks
    phone: Optional[str] = None            # for mobile-wallet providers
    instructions: Optional[str] = None    # human-readable note
    expires_at: Optional[datetime] = None  # when the slot expires
    extra: dict = field(default_factory=dict)


class DepositProvider(ABC):
    """
    Every payment provider implements this interface.
    Instance-level attributes are set on the class body so the registry
    can introspect them without calling create_payment().
    """
    method_id: str = ""          # unique slug used in API bodies, e.g. "crypto"
    method_name: str = ""        # human label,               e.g. "Crypto"
    networks: list[str] = []     # supported sub-networks (empty = N/A)

    def is_configured(self) -> bool:
        """
        Return True when all required env-vars / secrets are present.
        Providers that are not configured are hidden from /deposit/methods.
        Override this in concrete providers that need credentials.
        Default: always available (no credentials needed).
        """
        return True

    @abstractmethod
    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        """
        Generate a payment slot for the given deposit.
        Raise ValueError with a user-facing message if inputs are invalid.
        Raise RuntimeError if the provider is temporarily unavailable.
        """
