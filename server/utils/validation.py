"""
Shared input validation helpers.

Centralises validation logic that was previously duplicated between
server/plugins/bot/wallet.py and server/api/routes/wallet.py.
"""

import re
from typing import Optional


def validate_wallet_address(network: str, address: str) -> Optional[str]:
    """
    Validate a USDT withdrawal address for the given network.

    Returns an error string key on failure, or None if valid.

    Supported networks (case-insensitive):
      TRC20 — must start with T and be exactly 34 chars (alphanumeric)
      BEP20 — must start with 0x and be exactly 42 chars (hex)
    """
    network = network.upper()
    if network == "TRC20":
        if not re.match(r"^T[A-Za-z0-9]{33}$", address):
            return "trc20_invalid"
    elif network == "BEP20":
        if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
            return "bep20_invalid"
    return None
