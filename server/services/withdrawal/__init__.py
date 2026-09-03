"""
Withdrawal service package.

Public surface:
    from server.services.withdrawal import get_withdrawal_service
    from server.services.withdrawal import get_registry, get_provider
    from server.utils.withdrawal_statuses import WithdrawalStatus

⚠ Import note: WithdrawalStatus lives in server.utils.withdrawal_statuses — NOT
   inside this package — to avoid a circular import between withdrawaldb → this
   package's __init__ → service → withdrawaldb.
   All public functions here use lazy imports for the same reason.
"""

from __future__ import annotations

# NOTE: Do NOT import service/registry here at module level —
# those modules import withdrawaldb which imports withdrawal_statuses,
# and placing imports here would create a circular chain during package init.
# All imports are deferred to function call time via get_withdrawal_service().

_service = None


def get_withdrawal_service():
    """Lazily initialise and return the shared WithdrawalService instance."""
    global _service
    if _service is None:
        from server.services.withdrawal.service import WithdrawalService
        from server.services.withdrawal.registry import get_registry
        _service = WithdrawalService(get_registry())
    return _service


def get_registry():
    from server.services.withdrawal.registry import get_registry as _gr
    return _gr()


def get_provider(provider_id: str):
    from server.services.withdrawal.registry import get_provider as _gp
    return _gp(provider_id)


__all__ = ["get_withdrawal_service", "get_registry", "get_provider"]
