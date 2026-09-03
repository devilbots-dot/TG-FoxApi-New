"""
Abstract base class for all withdrawal (payout) providers.

To add a new payment gateway:
  1. Create server/services/withdrawal/providers/<name>.py
  2. Subclass WithdrawalProvider and implement submit_payout() + verify_payout()
  3. Register it in server/services/withdrawal/registry.py

No other code changes needed — the WithdrawalService discovers providers
via the registry and calls them through this interface.
"""

from abc import ABC, abstractmethod
from typing import Optional

from server.services.withdrawal.models import PayoutResult, VerifyResult


class WithdrawalProvider(ABC):
    """
    Every withdrawal provider implements this interface.

    Class attributes are introspectable without calling submit_payout():
      provider_id   — unique slug, e.g. "oxapay"
      provider_name — human label, e.g. "OxaPay"
      supported_networks  — networks this provider can send to, e.g. ["TRC20", "BEP20"]
      supported_currencies — currencies this provider handles, e.g. ["USDT"]
    """

    provider_id:          str       = ""
    provider_name:        str       = ""
    supported_networks:   list[str] = []
    supported_currencies: list[str] = []

    def is_configured(self) -> bool:
        """
        Return True when all required credentials/env-vars are present.
        Unconfigured providers are excluded from the active provider list.
        """
        return True

    @abstractmethod
    async def submit_payout(
        self,
        *,
        withdrawal_id: str,     # internal withdrawal ID (used as description)
        address: str,           # recipient crypto address
        currency: str,          # crypto currency, e.g. "USDT"
        network: str,           # network slug, e.g. "TRC20"
        amount: float,          # net amount to send (after fees)
        callback_url: str = "", # URL for status callbacks
        memo: str = "",         # memo/tag if required by network
    ) -> PayoutResult:
        """
        Submit a payout to the gateway.

        Must be idempotent where possible — if the same withdrawal_id is
        submitted twice, prefer returning the existing track_id rather than
        creating a duplicate.

        Raises:
            RuntimeError  — provider is temporarily unavailable (should retry)
            ValueError    — request is invalid (do not retry)
        """

    @abstractmethod
    async def verify_payout(
        self,
        track_id: str,
    ) -> VerifyResult:
        """
        Poll the gateway for the current status of a payout.

        Args:
            track_id — the gateway's own transaction identifier

        Returns VerifyResult with found=True and up-to-date status,
        or found=False if the track_id is not recognized.
        """

    def validate_address(self, network: str, address: str) -> Optional[str]:
        """
        Basic address validation.  Return an error string on failure, None on success.
        Providers can override for gateway-specific rules; the default defers
        to the shared regex validators in server.utils.validation.
        """
        from server.utils.validation import validate_wallet_address
        return validate_wallet_address(network, address)

    def translate_network(self, network: str) -> str:
        """
        Convert our internal network name (e.g. "TRC20") to the gateway's
        network identifier (e.g. "TRX").  Override in concrete providers.
        """
        return network

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"id={self.provider_id!r} "
            f"networks={self.supported_networks!r} "
            f"configured={self.is_configured()}>"
        )
