"""
Fresh Telegram login flow: open a brand-new client, request a login code
via the account's own phone number, capture the OTP through an already-
connected client for the SAME account, and sign the new client in.

Used both to add an extra concurrent login to an already-stored account
(`generate_fresh_session`) and, inside the stock pipeline, to replace an
uploaded session with a brand-new one before storing it.
"""

from typing import Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from server import LOGGER
from server.stock.otp_capture import start_login_otp_listener, wait_for_login_otp
from server.stock.session_validation import read_account_info
from server.utils.sessions.telethon_client import (
    build_telethon_proxy,
    cleanup_session_files,
    connect_with_proxy_fallback,
    new_temp_session_path,
    read_session_bytes,
    write_session_bytes,
)

_log = LOGGER(__name__)


async def request_login_code(new_client, phone: str):
    """Ask Telegram to send a login code to `phone` on a fresh client."""
    return await new_client.send_code_request(phone)


async def sign_in_new_client(
    new_client,
    phone: str,
    otp: str,
    phone_code_hash: str,
    tfa_password: str = "",
    skip_empty_password_check: bool = False,
) -> tuple[str, Optional[Exception]]:
    """
    Sign a brand-new client in with a captured OTP, handling the 2FA
    password fallback Telegram requires for accounts that already have one.

    `skip_empty_password_check`: when False (default — used by
    generate_fresh_session), an empty `tfa_password` short-circuits to
    "password_required" without even attempting sign_in(password=""). When
    True (used by the main stock pipeline, which always has SOME value for
    admin_2fa_password by the time it reaches this point), the password
    sign-in is attempted regardless — matching each caller's original
    behavior exactly.

    Returns one of:
      ("ok",                None)
      ("password_required", None)        — 2FA needed, no password supplied
      ("password_rejected", <exception>) — 2FA needed, supplied one was rejected
      ("failed",            <exception>) — any other sign-in failure
    """
    try:
        await new_client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        return "ok", None
    except SessionPasswordNeededError:
        if not tfa_password and not skip_empty_password_check:
            return "password_required", None
        try:
            await new_client.sign_in(password=tfa_password)
            return "ok", None
        except Exception as exc:
            return "password_rejected", exc
    except Exception as exc:
        return "failed", exc


async def _safe_disconnect(client) -> None:
    if client is not None:
        try:
            await client.disconnect()
        except Exception:
            pass


async def generate_fresh_session(
    old_session_bytes: bytes,
    api_id: int,
    api_hash: str,
    country_code: str,
    tfa_password: str = "",
    otp_timeout: int = 120,
    terminate_others: bool = False,
) -> dict:
    """
    Create a brand-new, additionally-authorized session for the SAME account
    by performing a real phone-number login: request a login code, capture
    the OTP via the already-uploaded session, and sign a new blank client in
    with it — through the country-matched proxy.

    By default (`terminate_others=False`) this never touches any existing
    session. The old session (and any other device already logged in) stays
    exactly as active as before; the result is simply one more concurrent
    login, the same as opening Telegram Desktop alongside the phone app.
    This is what the admin `/gen_fresh_session` utility wants.

    When `terminate_others=True` (used when a stored session is actually
    being SOLD to a buyer), every other active login — including the old
    session this was generated from — is revoked right after the new client
    signs in, so only the buyer's brand-new session stays valid. Without
    this, a buyer purchasing a "session" unit would get access to an account
    the previous holder (seller, or anyone else with a copy of the old
    session) could still use — a dual-access fraud risk.

    Returns dict:
      success              bool
      phone                str | None
      user_id              int | None
      username             str | None
      first_name           str | None
      fresh_session_bytes  bytes | None   (the brand-new login's session)
      proxy_used           str            (proxy_id | "direct" | "none")
      needs_password       bool           (2FA password required but missing/wrong)
      terminated_others    bool           (only meaningful when terminate_others=True)
      error                str | None
    """
    result: dict = {
        "success":             False,
        "phone":               None,
        "user_id":             None,
        "username":            None,
        "first_name":          None,
        "fresh_session_bytes": None,
        "proxy_used":          "none",
        "needs_password":      False,
        "terminated_others":   False,
        "flood_wait_seconds":  None,
        "error":               None,
    }

    old_tmp_path = new_temp_session_path()
    new_tmp_path = new_temp_session_path()
    old_client = None
    new_client = None

    try:
        await write_session_bytes(old_tmp_path, old_session_bytes)
        old_session_path = old_tmp_path[:-8]
        new_session_path = new_tmp_path[:-8]

        # ── Step 1: connect OLD session (proxy-aware) ──────────────────────
        try:
            old_client, proxy_used, proxy_doc_used = await connect_with_proxy_fallback(
                old_session_path, api_id, api_hash, country_code
            )
            result["proxy_used"] = proxy_used
        except OSError as exc:
            result["error"] = f"Could not connect old session: {exc}"
            return result

        if not await old_client.is_user_authorized():
            result["error"] = "Old session not authorized (expired or invalid)"
            return result

        info = await read_account_info(old_client)
        result.update(info)
        phone = info["phone"]
        if not phone:
            result["error"] = "Could not read phone number from old session"
            return result

        # ── Step 2: OTP listener on the OLD client ─────────────────────────
        otp_future = start_login_otp_listener(old_client)

        # ── Step 3: new blank client, SAME proxy the old client used ──────
        proxy_tuple = build_telethon_proxy(proxy_doc_used) if proxy_doc_used else None
        new_client_kwargs: dict = {"api_id": api_id, "api_hash": api_hash}
        if proxy_tuple:
            new_client_kwargs["proxy"] = proxy_tuple
        new_client = TelegramClient(new_session_path, **new_client_kwargs)
        await new_client.connect()

        try:
            sent = await request_login_code(new_client, phone)
        except FloodWaitError as fwe:
            # Do NOT retry within this call — retrying immediately just
            # extends the wait. Surface the seconds so callers doing bulk
            # generation (buy-session qty > 1) can stop the batch instead of
            # hammering Telegram once per remaining unit.
            result["flood_wait_seconds"] = fwe.seconds
            result["error"] = f"FloodWait: must wait {fwe.seconds}s before requesting another code"
            return result
        except Exception as exc:
            result["error"] = f"send_code_request failed: {exc}"
            return result

        # ── Step 4: wait for OTP, then sign in the NEW client ──────────────
        otp = await wait_for_login_otp(otp_future, otp_timeout)
        if otp is None:
            result["error"] = "Timed out waiting for login code"
            return result

        outcome, exc = await sign_in_new_client(new_client, phone, otp, sent.phone_code_hash, tfa_password)
        if outcome == "password_required":
            result["needs_password"] = True
            result["error"] = "2FA password required"
            return result
        if outcome == "password_rejected":
            result["needs_password"] = True
            result["error"] = f"2FA sign-in failed: {exc}"
            return result
        if outcome == "failed":
            result["error"] = f"sign_in failed: {exc}"
            return result

        # ── Step 5: optionally terminate every other login on this account ──
        # Must happen BEFORE disconnecting new_client, since new_client is
        # the one issuing the ResetAuthorizationRequest calls.
        if terminate_others:
            try:
                from server.stock.authorization import list_other_authorizations, terminate_other_sessions
                others = await list_other_authorizations(new_client)
                if others:
                    term_result = await terminate_other_sessions(new_client, others, phone)
                    result["terminated_others"] = term_result.get("terminated_others", False)
            except Exception as exc:
                _log.warning("generate_fresh_session: terminate_others failed for %s: %s", phone, exc)

        # ── Step 6: flush + read the brand-new session bytes ───────────────
        await new_client.disconnect()
        new_client = None
        try:
            result["fresh_session_bytes"] = await read_session_bytes(new_tmp_path)
            result["success"] = True
        except Exception as exc:
            result["error"] = f"Could not read new session bytes: {exc}"

    except Exception as exc:
        _log.error("generate_fresh_session error: %s", exc)
        result["error"] = f"Unexpected error: {exc}"

    finally:
        await _safe_disconnect(old_client)
        await _safe_disconnect(new_client)
        await cleanup_session_files(old_tmp_path, new_tmp_path)

    return result
