"""
Terminating every other active Telegram authorization for an account, so
only the pipeline's final session stays logged in.
"""

import asyncio

from telethon.tl.functions.account import (
    GetAuthorizationsRequest as _GetAuths,
    ResetAuthorizationRequest as _ResetAuth,
)

from server import LOGGER

_log = LOGGER(__name__)


async def find_current_session_hash(client) -> int | None:
    """Return the authorization hash Telegram considers "current" for `client`."""
    auths = await client(_GetAuths())
    return next((a.hash for a in auths.authorizations if a.current), None)


async def list_other_authorizations(client, exclude_hash: int | None = None) -> list:
    """
    List every authorization visible to `client` that is not its own current
    one, and (if given) not `exclude_hash` either — used when termination is
    driven from a different, older client than the one that should stay alive.
    """
    auths = await client(_GetAuths())
    return [
        a for a in auths.authorizations
        if not a.current and (exclude_hash is None or a.hash != exclude_hash)
    ]


async def find_current_and_list_others(old_client, new_client) -> tuple[int | None, list]:
    """
    Fetch both clients' authorization lists CONCURRENTLY — the new client's
    (to find its own current-session hash, so it can be excluded) and the
    old client's (the list termination is actually driven from) — instead
    of two sequential round trips through the proxy. Only the local
    filtering (which needs both results) happens after they land.

    Returns (new_session_hash, others_to_terminate_from_old_client).
    """
    new_auths, old_auths = await asyncio.gather(
        new_client(_GetAuths()),
        old_client(_GetAuths()),
    )
    new_hash = next((a.hash for a in new_auths.authorizations if a.current), None)
    others = [
        a for a in old_auths.authorizations
        if not a.current and a.hash != new_hash
    ]
    return new_hash, others


async def terminate_other_sessions(terminator_client, others: list, phone: str) -> dict:
    """
    Terminate every authorization in `others` concurrently (fan-out, not a
    sequential round trip per device — that dominated processing time for
    accounts with many logged-in devices), using `terminator_client` to
    issue the ResetAuthorizationRequest calls.

    Returns {"terminated_others": bool, "termination_incomplete": bool}.
    `termination_incomplete` is True if any termination call failed — the
    caller should surface this so the admin knows more than one session may
    still be active, instead of silently claiming a clean single-session state.
    """
    async def _terminate_one(auth) -> bool:
        try:
            await terminator_client(_ResetAuth(hash=auth.hash))
            return True
        except Exception as exc:
            _log.warning("Could not terminate one authorization for %s: %s", phone, exc)
            return False

    term_results = await asyncio.gather(*(_terminate_one(a) for a in others))
    terminated = sum(1 for r in term_results if r)
    failed     = sum(1 for r in term_results if not r)

    if terminated:
        _log.info("Terminated %d other session(s) for %s", terminated, phone)
    if failed:
        _log.warning(
            "%d other session(s) for %s could NOT be terminated — "
            "account may still have more than one active session.",
            failed, phone,
        )

    return {"terminated_others": terminated > 0, "termination_incomplete": failed > 0}
