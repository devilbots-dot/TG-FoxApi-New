"""
Re-export shim — WithdrawalStatus canonical home is server.utils.withdrawal_statuses.

This file exists only so that any code importing from server.services.withdrawal.statuses
still works. Always prefer importing from server.utils.withdrawal_statuses directly.
"""
from server.utils.withdrawal_statuses import WithdrawalStatus  # noqa: F401
