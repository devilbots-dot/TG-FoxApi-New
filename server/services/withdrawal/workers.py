"""
Withdrawal background workers.

  withdrawal_verification_worker()
      Every WITHDRAWAL_VERIFY_INTERVAL_S seconds:
        • Poll OxaPay for all in-flight withdrawal statuses
        • Apply status transitions + balance changes
        • Expire stale withdrawals beyond WITHDRAWAL_EXPIRE_HOURS

Integrated into server/services/workers.py via start_background_workers().
"""

from __future__ import annotations

import asyncio

import config
from server.logging import LOGGER

_log = LOGGER(__name__)


async def withdrawal_verification_worker() -> None:
    """Long-running background task for withdrawal status verification."""
    _log.info("withdrawal_verification_worker: started (interval=%ds)", config.WITHDRAWAL_VERIFY_INTERVAL_S)
    await asyncio.sleep(15)  # allow bot + API to initialize first

    while True:
        try:
            await _run_verification_cycle()
        except asyncio.CancelledError:
            _log.info("withdrawal_verification_worker: cancelled")
            return
        except Exception as exc:
            _log.error("withdrawal_verification_worker unhandled error: %s", exc, exc_info=True)

        await asyncio.sleep(config.WITHDRAWAL_VERIFY_INTERVAL_S)


async def _run_verification_cycle() -> None:
    """One verification sweep: poll in-flight withdrawals + expire stale ones."""
    try:
        from server.services.withdrawal import get_withdrawal_service
        svc = get_withdrawal_service()

        # Verify in-flight withdrawals
        stats = await svc.verify_pending_withdrawals()
        if stats["checked"] > 0:
            _log.info(
                "withdrawal_verification_worker: checked=%d updated=%d errors=%d",
                stats["checked"], stats["updated"], stats["errors"],
            )

        # Expire stale withdrawals
        expired = await svc.expire_stale_withdrawals()
        if expired > 0:
            _log.info("withdrawal_verification_worker: expired %d stale withdrawal(s)", expired)

    except Exception as exc:
        _log.error("_run_verification_cycle error: %s", exc, exc_info=True)
