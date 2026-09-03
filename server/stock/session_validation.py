"""
Session authorization + identity checks — the "is this account real and
usable" gate every uploaded session goes through before anything else
touches it.

check_duplicate_stock() accepts an optional BatchContext. When one is
supplied the check is a pure RAM lookup (no MongoDB round-trip):
  • existing_phones  — phones already in sessiondb (pre-loaded once per batch)
  • batch_phones     — phones claimed by another account in this same batch

When no context is supplied (resume / single-account path) it falls back
to a direct MongoDB query — same as the original behaviour.
"""

from typing import TYPE_CHECKING, Optional

from server import LOGGER

if TYPE_CHECKING:
    from server.stock.batch_context import BatchContext

_log = LOGGER(__name__)


async def validate_session_authorized(client) -> bool:
    """True if the connected Telethon client's session is still logged in."""
    return await client.is_user_authorized()


async def read_account_info(client) -> dict:
    """Read the identity fields Telegram exposes for the logged-in account."""
    me = await client.get_me()
    phone = f"+{me.phone}" if me and me.phone else None
    return {
        "phone":      phone,
        "user_id":    me.id if me else None,
        "username":   me.username if me else None,
        "first_name": me.first_name if me else None,
    }


async def check_duplicate_stock(
    phone: str,
    ctx: Optional["BatchContext"] = None,
) -> bool:
    """
    True if this phone number is already taken (either in the DB or by
    another account earlier in this same batch).

    Batch path  (ctx supplied):
      Pure in-memory check — no MongoDB round-trip.
      ctx.claim_phone() atomically checks both sets AND marks the phone as
      taken, so concurrent pipelines in the same batch cannot both pass.
      Returns True (duplicate) when claim_phone returns False.

    Resume path (ctx is None):
      Direct MongoDB query — same as the original single-account behaviour.
      DB errors are treated as "not a duplicate" to avoid blocking a whole
      batch on a transient failure.
    """
    if ctx is not None:
        # claim_phone: True  → phone is new; False → duplicate
        claimed = await ctx.claim_phone(phone)
        return not claimed

    # Fallback: single-account / resume path
    from server.utils.database.sessiondb import get_session_by_phone
    try:
        return bool(await get_session_by_phone(phone))
    except Exception as exc:
        _log.warning("Duplicate check error for %s: %s", phone, exc)
        return False
