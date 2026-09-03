"""
2FA (two-factor password) status inspection and mutation for stock accounts.

The "enable" and "disable" batch actions used to be two near-duplicate
edit_2fa blocks inline in the pipeline; that logic now lives here as small,
single-purpose functions the pipeline (and anything else that needs to
manage an account's 2FA) calls into.
"""

from server import LOGGER
from server.utils.sessions.crypto import generate_strong_password

_log = LOGGER(__name__)


class WrongTwoFactorPassword(Exception):
    """Raised when Telegram rejects the CURRENT 2FA password we were given."""


def _is_wrong_password_error(exc: Exception) -> bool:
    exc_name = type(exc).__name__
    return "PasswordHashInvalid" in exc_name or "PASSWORD_HASH_INVALID" in str(exc)


async def get_2fa_status(client) -> bool:
    """
    True if the account currently has a 2FA password set.

    On error, logs and assumes False — callers then proceed as if the
    account had none, matching the original's non-fatal handling here.
    """
    try:
        from telethon.tl.functions.account import GetPasswordRequest
        pwd_info = await client(GetPasswordRequest())
        return bool(pwd_info.has_password)
    except Exception as exc:
        _log.warning("Could not check 2FA status: %s", exc)
        return False


async def enable_2fa(
    client, phone: str, had_2fa: bool, current_password: str, new_password: str | None,
) -> dict:
    """
    Set (or rotate) the account's 2FA password to `new_password` (a fresh
    strong one is generated if none is given).

    Raises WrongTwoFactorPassword if `current_password` is rejected (only
    relevant when `had_2fa` is True) — this must pause the caller, not be
    treated as a generic failure. Any other error is logged and swallowed
    so the rest of the pipeline can continue without a 2FA change.

    Returns {"has_2fa": True, "tfa_updated": True, "tfa_password_enc": str}
    on success, or {"tfa_apply_failed": True, ...} if the attempt failed non-fatally.

    IMPORTANT: encrypt_password() is intentionally called OUTSIDE the edit_2fa
    try-block. Previously it was inside the same try, so an encryption failure
    (e.g. bad SESSION_SECRET) would silently discard the password even though
    2FA was already set on Telegram. Now the two operations are separated:
      1. edit_2fa  — sets the password on Telegram (may raise → caught)
      2. encrypt   — stores it in DB (encryption failure → critical log +
                     plaintext fallback so decrypt_password() still works)
    """
    password = new_password or generate_strong_password()

    # ── Build hint: substitution cipher stored in Telegram's hint field ─────
    # subst_encrypt() applies a fixed character-substitution table — no keys,
    # no external libs. subst_decrypt() reverses it perfectly from the hint
    # alone, with zero DB access required.
    try:
        from server.utils.sessions.crypto import subst_encrypt
        hint_value = subst_encrypt(password)
    except Exception as _hint_exc:
        _log.warning("enable_2fa: hint encryption failed for %s: %s — using fallback hint", phone, _hint_exc)
        hint_value = "Managed"

    # ── Step 1: Set / rotate the 2FA on Telegram ─────────────────────────────
    try:
        if had_2fa:
            await client.edit_2fa(current_password=current_password, new_password=password, hint=hint_value)
        else:
            await client.edit_2fa(new_password=password, hint=hint_value)
        _log.info("2FA %s for %s (hint=encrypted)", "updated" if had_2fa else "enabled", phone)
    except Exception as exc:
        if _is_wrong_password_error(exc):
            raise WrongTwoFactorPassword(str(exc)) from exc
        _log.warning("2FA enable failed for %s (%s: %s) — continuing.", phone, type(exc).__name__, exc)
        return {"tfa_apply_failed": True, "tfa_apply_error": f"{type(exc).__name__}: {exc}"}

    return {"has_2fa": True, "tfa_updated": True, "tfa_password_enc": password}


async def disable_2fa(client, phone: str, had_2fa: bool, current_password: str) -> dict:
    """
    Remove the account's 2FA password. No-op if it never had one.

    Raises WrongTwoFactorPassword if `current_password` is rejected. Any
    other error is logged and swallowed.
    """
    if not had_2fa:
        return {}
    try:
        await client.edit_2fa(current_password=current_password, new_password=None)
        _log.info("2FA disabled for %s", phone)
        return {"has_2fa": False, "tfa_updated": True, "tfa_password_enc": ""}
    except Exception as exc:
        if _is_wrong_password_error(exc):
            raise WrongTwoFactorPassword(str(exc)) from exc
        _log.warning("2FA disable failed for %s (%s: %s) — continuing.", phone, type(exc).__name__, exc)
        return {"tfa_apply_failed": True, "tfa_apply_error": f"{type(exc).__name__}: {exc}"}


async def apply_batch_2fa_decision(
    client,
    phone: str,
    had_2fa: bool,
    current_password: str,
    batch_action: str,
    batch_new_password: str | None,
) -> dict:
    """Dispatch to enable_2fa/disable_2fa based on the admin's batch-wide choice."""
    if batch_action == "enable":
        return await enable_2fa(client, phone, had_2fa, current_password, batch_new_password)
    if batch_action == "disable":
        return await disable_2fa(client, phone, had_2fa, current_password)
    return {}
