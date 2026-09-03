"""Safe inventory reservation and pre-sale session validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from server.logging import LOGGER
from server.services.purchase_validation import SessionValidation, validate_inventory_session
from server.utils.database.sessiondb import (
    get_unsold_session_for_country,
    mark_session_sold,
    revert_session_sold,
    move_session_to_bin,
)

_log = LOGGER(__name__)


@dataclass(frozen=True)
class ReservationResult:
    account: Optional[dict]
    error: Optional[str] = None
    retryable: bool = False


async def _notify_bin(bot_client, account: dict, validation: SessionValidation, order_id: str | None = None) -> None:
    """Send a redacted invalid-session alert to the configured logs group."""
    try:
        import config
        chat_id = getattr(config, "LOG_GROUP_ID", None)
        if not chat_id or bot_client is None:
            return
        await bot_client.send_message(
            chat_id,
            "⚠️ **INVALID SESSION DETECTED**\n\n"
            f"Account: `{account.get('account_id', '?')}`\n"
            f"Country: `{account.get('country_code', '?')}`\n"
            f"Issue: `{validation.issue or 'invalid_session'}`\n"
            f"Action: **Moved to BIN**\n"
            f"Time: `{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}`"
            + (f"\nOrder: `{order_id}`" if order_id else ""),
        )
    except Exception as exc:
        _log.warning("Could not notify logs group about BIN account %s: %s", account.get("account_id"), exc)


async def reserve_valid_session(
    *,
    bot_client,
    user_id: int,
    country_code: str,
    exclude_account_ids: Optional[list[str]] = None,
    max_attempts: int = 25,
    order_id: str | None = None,
) -> ReservationResult:
    """Reserve one live-valid account, quarantining permanent failures.

    Every loop iteration either atomically claims a new account, moves a
    permanent-invalid account to BIN, or releases a temporary validation claim.
    The bounded attempt count prevents infinite retries and the exclusion list
    prevents selecting the same account twice in one purchase.
    """
    excluded = set(exclude_account_ids or [])
    attempts = 0
    while attempts < max(1, max_attempts):
        attempts += 1
        account = await get_unsold_session_for_country(
            country_code,
            exclude_account_ids=list(excluded),
        )
        if not account:
            return ReservationResult(None, "out_of_stock")
        account_id = account.get("account_id")
        if not account_id or account_id in excluded:
            continue
        excluded.add(account_id)

        if not await mark_session_sold(account_id, user_id):
            continue

        validation = await validate_inventory_session(bot_client, account, country_code)
        if validation.valid:
            return ReservationResult(account)

        if validation.permanent_invalid:
            moved = await move_session_to_bin(
                account_id,
                validation.reason,
                issue=validation.issue,
                order_id=order_id,
            )
            if moved:
                await _notify_bin(bot_client, account, validation, order_id)
                continue
            # Never leave an invalid account claimed if BIN persistence failed.
            await revert_session_sold(account_id)
            return ReservationResult(None, "bin_persistence_failed")

        # Temporary network/provider/Telegram problem: do not BIN it and do not
        # sell it. Release the claim, then stop so a platform-wide outage does
        # not fan out into repeated validation attempts.
        await revert_session_sold(account_id)
        return ReservationResult(None, validation.reason or "temporary_validation_error", retryable=True)

    return ReservationResult(None, "validation_attempt_limit_reached", retryable=True)
