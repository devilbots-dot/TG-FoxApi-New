"""
Withdrawal provider registry.

To add a new provider:
  1. Implement WithdrawalProvider in providers/<name>.py
  2. Import and append an instance to _PROVIDERS below.
"""

from server.services.withdrawal.base import WithdrawalProvider
from server.services.withdrawal.providers.oxapay import OxaPayWithdrawalProvider

_PROVIDERS: list[WithdrawalProvider] = [
    OxaPayWithdrawalProvider(),
    # NowPaymentsWithdrawalProvider(),   ← future
    # BinancePayWithdrawalProvider(),    ← future
]


class WithdrawalRegistry:
    """Immutable registry of all registered withdrawal providers."""

    def __init__(self, providers: list[WithdrawalProvider]) -> None:
        self._providers = list(providers)

    def all(self) -> list[WithdrawalProvider]:
        """All registered providers regardless of configuration status."""
        return list(self._providers)

    def configured(self) -> list[WithdrawalProvider]:
        """Providers that are fully configured and ready to use."""
        return [p for p in self._providers if p.is_configured()]

    def get(self, provider_id: str) -> "WithdrawalProvider | None":
        """Return provider by ID, or None."""
        for p in self._providers:
            if p.provider_id == provider_id:
                return p
        return None

    def default(self) -> "WithdrawalProvider | None":
        """Return the first configured provider, or None."""
        configured = self.configured()
        return configured[0] if configured else None


_registry = WithdrawalRegistry(_PROVIDERS)


def get_registry() -> WithdrawalRegistry:
    return _registry


def get_provider(provider_id: str) -> "WithdrawalProvider | None":
    return _registry.get(provider_id)
