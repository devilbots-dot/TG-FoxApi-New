"""
Withdrawal provider implementations.

Current providers:
    OxaPayWithdrawalProvider  — USDT payouts via OxaPay (TRC20 / BEP20)

To add a provider: implement WithdrawalProvider ABC in a new file here,
then register the instance in server/services/withdrawal/registry.py.
"""
from server.services.withdrawal.providers.oxapay import OxaPayWithdrawalProvider

__all__ = ["OxaPayWithdrawalProvider"]
