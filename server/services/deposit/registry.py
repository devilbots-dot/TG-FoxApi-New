"""
Deposit provider registry.

Providers are tried in order; only configured providers appear in /deposit/methods.
"""

from server.services.deposit.providers import (
    TelegramStarsProvider,
    OxaPayDepositProvider,
    TRC20ScanProvider,
    BEP20ScanProvider,
    BinancePayProvider,
    BinancePayTxProvider,
)

_PROVIDERS: list = [
    OxaPayDepositProvider(),   # Crypto via OxaPay              (requires OXAPAY_MERCHANT_KEY)
    TRC20ScanProvider(),       # Direct USDT TRC20               (requires DEPOSIT_TRC20_ADDRESS)
    BEP20ScanProvider(),       # Direct USDT BEP20 / BSC         (requires DEPOSIT_BEP20_ADDRESS)
    # Only ONE Binance option is exposed: "Binance Pay (Auto)" (Order ID verified
    # live against the Binance API).  The legacy merchant-API provider stays
    # importable so existing/older deposits keep resolving, but it is no longer
    # offered to users.
    BinancePayTxProvider(),    # Binance Pay (Auto)              (requires BINANCE_ACCOUNT_API_KEY + BINANCE_PAY_UID)
    TelegramStarsProvider(),   # Telegram Stars                  (requires BOT_USERNAME)
]


def get_all_providers() -> list:
    """All registered providers (regardless of configured status)."""
    return list(_PROVIDERS)


def get_configured_providers() -> list:
    """Providers that are fully configured and ready to use."""
    return [p for p in _PROVIDERS if p.is_configured()]


_LEGACY_PROVIDERS: list = [
    BinancePayProvider(),      # legacy: resolves old deposits only, never listed
]


def get_provider(method_id: str):
    """Return the provider for a given method_id, or None."""
    for p in _PROVIDERS:
        if p.method_id == method_id:
            return p
    for p in _LEGACY_PROVIDERS:
        if p.method_id == method_id:
            return p
    return None
