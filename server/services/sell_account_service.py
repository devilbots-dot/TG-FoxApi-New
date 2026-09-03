"""
Sell Account Service — Live phone/OTP/2FA login flow (User → Server).

The seller never uploads a file. Instead:
  1. Bot creates a blank Telethon session for the phone number.
  2. Bot calls send_code_request(phone) — Telegram sends the OTP to the user's device.
  3. User types the OTP into the bot chat.
  4. Bot signs in. If 2FA needed, asks for current password.
  5. Bot runs the full production pipeline on the now-live session:
       spam check → pricing → 2FA rotation → session termination
  6. If "session too new" prevents termination → PENDING_TERMINATION status,
     retry after 48 hours via the background worker.
  7. On success → session uploaded to storage channel, sell request created,
     pending_balance credited.

All Telethon I/O is fully async. No blocking calls.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import phonenumbers

from server import LOGGER
from server.utils.sessions.telethon_client import (
    new_temp_session_path,
    cleanup_session_files,
    read_session_bytes,
    build_telethon_proxy,
)

_log = LOGGER(__name__)

# How long (seconds) to keep the Telethon client alive while waiting for OTP
# from the user (they may need time to switch apps and type the code).
OTP_WAIT_TIMEOUT = 300  # 5 minutes


# ── Phone number validation ───────────────────────────────────────────────────

def validate_phone(phone_raw: str) -> tuple[bool, str, str]:
    """
    Validate and normalise a phone number in international format.

    Returns (is_valid, normalised_phone, country_code_iso2).
    On failure: (False, "", "").
    """
    try:
        phone_raw = phone_raw.strip()
        if not phone_raw.startswith("+"):
            phone_raw = "+" + phone_raw

        parsed = phonenumbers.parse(phone_raw, None)
        if not phonenumbers.is_valid_number(parsed):
            return False, "", ""

        normalised = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )
        region = phonenumbers.region_code_for_number(parsed) or ""
        return True, normalised, region.upper()
    except Exception as exc:
        _log.debug("validate_phone(%r): %s", phone_raw, exc)
        return False, "", ""


# ── Telethon login initiation ─────────────────────────────────────────────────

async def start_login(
    phone: str,
    country_code: str,
    api_id: int,
    api_hash: str,
) -> tuple[object, str, str, Optional[dict]]:
    """
    Create a blank Telethon session and request a login code from Telegram.

    Returns (client, tmp_path, phone_code_hash, proxy_doc).
    Raises on connection failure.

    The caller is responsible for disconnecting `client` and cleaning up
    `tmp_path` in all exit paths (success, failure, and cancellation).
    """
    from server.utils.database.proxydb import get_active_proxy_for_country
    from telethon import TelegramClient

    tmp_path = new_temp_session_path()
    session_path = tmp_path[:-8]  # strip ".session"

    proxy_doc = await get_active_proxy_for_country(country_code)

    if proxy_doc:
        proxy_tuple = build_telethon_proxy(proxy_doc)
        client = TelegramClient(session_path, api_id, api_hash, proxy=proxy_tuple)
    else:
        client = TelegramClient(session_path, api_id, api_hash)

    await client.connect()

    sent = await client.send_code_request(phone)
    phone_code_hash = sent.phone_code_hash

    return client, tmp_path, phone_code_hash, proxy_doc


async def submit_otp(
    client,
    phone: str,
    otp: str,
    phone_code_hash: str,
) -> tuple[str, Optional[Exception]]:
    """
    Attempt to sign in with the OTP code.

    Returns ("ok", None)          on success.
    Returns ("2fa_required", None) when 2FA password is needed.
    Returns ("wrong_otp", exc)    for a wrong code.
    Returns ("expired_otp", exc)  for an expired code.
    Returns ("failed", exc)       for any other error.
    """
    from telethon.errors import (
        SessionPasswordNeededError,
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
    )

    try:
        await client.sign_in(phone=phone, code=otp, phone_code_hash=phone_code_hash)
        return "ok", None
    except SessionPasswordNeededError:
        return "2fa_required", None
    except PhoneCodeExpiredError as exc:
        return "expired_otp", exc
    except PhoneCodeInvalidError as exc:
        return "wrong_otp", exc
    except Exception as exc:
        return "failed", exc


# ── Pre-OTP eligibility validation ────────────────────────────────────────────

async def validate_phone_eligibility(
    user_id: int,
    phone: str,
    country_code: str,
) -> tuple[bool, str]:
    """
    Run all pre-OTP eligibility checks. Returns (ok, user_facing_error_message).

    Call this BEFORE start_login() so Telegram send_code_request calls are never
    wasted on a phone/country that would be rejected anyway.

    Check order (as specified in Phase 3):
      1.  Global maintenance mode
      2.  Global selling enabled
      3.  Country exists in our system
      4.  Country sell price configured (> 0)
      5.  Country not temporarily disabled
      6.  Country quota not full
      7.  Rate limit  — max submissions per rolling 24h window
      8.  Pending cap — max simultaneous pending requests
      9.  Phone not already in an active sell request
      10. Phone not already in our stock
    """
    from server.core import memstore
    from server.utils.database.configdb import get_setting
    from server.utils.database.sellrequestdb import (
        check_phone_has_active_sell,
        check_phone_in_stock,
        get_user_recent_sell_count,
        get_user_pending_sell_count,
        SELL_RATE_LIMIT_HOURS,
        SELL_RATE_LIMIT_MAX,
        SELL_MAX_PENDING,
    )

    # 1. Maintenance mode
    if await get_setting("maintenance_mode"):
        return False, "🔧 The platform is currently under maintenance. Please try again later."

    # 2. Selling globally enabled
    if not await get_setting("sell_requests_enabled"):
        return False, "⛔ Selling is currently disabled. Please check back later."

    # 3. Country exists in our system
    country = memstore.get_country(country_code)
    if not country:
        return False, (
            "![❌](tg://emoji?id=6129846551134084367) This country is not supported.\n"
            "We do not accept accounts from this region."
        )

    # 4. Sell price configured
    if not country.get("sell_price", 0):
        return False, (
            "![❌](tg://emoji?id=6129846551134084367) No sell price is set for this country.\n"
            "We are not currently accepting accounts from this region."
        )

    # 5. Country not temporarily disabled
    if country.get("temp_disable"):
        return False, (
            "![⏸](tg://emoji?id=6129574787078429498) Selling for this country is temporarily paused.\n"
            "Please try again later."
        )

    # 6. Country quota not full
    if country.get("is_full"):
        return False, (
            "![📦](tg://emoji?id=6129579803600231171) We have reached our quota for this country.\n"
            "We are not accepting more accounts right now."
        )

    # 7-10. Run the four independent DB checks in parallel to halve round-trips.
    recent, pending, has_active_sell, in_stock = await asyncio.gather(
        get_user_recent_sell_count(user_id, SELL_RATE_LIMIT_HOURS),
        get_user_pending_sell_count(user_id),
        check_phone_has_active_sell(phone),
        check_phone_in_stock(phone),
    )

    # 7. Rate limit: max N submissions per rolling window
    if recent >= SELL_RATE_LIMIT_MAX:
        return False, (
            f"![⏱](tg://emoji?id=6129574787078429498) You have reached the limit of "
            f"{SELL_RATE_LIMIT_MAX} submissions per {SELL_RATE_LIMIT_HOURS} hours.\n"
            "Please try again later."
        )

    # 8. Pending cap: max M simultaneous pending requests
    if pending >= SELL_MAX_PENDING:
        return False, (
            f"![⏳](tg://emoji?id=6129574787078429498) You already have {SELL_MAX_PENDING} "
            "pending sell requests.\nPlease wait for existing ones to be reviewed."
        )

    # 9. Phone not already in an active sell request
    if has_active_sell:
        return False, (
            "![🔁](tg://emoji?id=6129792056589031358) This phone number already has an active "
            "sell request awaiting review."
        )

    # 10. Phone not already in our stock
    if in_stock:
        return False, (
            "![📦](tg://emoji?id=6129579803600231171) This phone number is already in our system.\n"
            "Duplicate accounts are not accepted."
        )

    return True, ""


async def submit_2fa(client, password: str) -> tuple[str, Optional[Exception]]:
    """
    Submit a 2FA password on an already-connected, OTP-verified client.

    Returns ("ok", None) on success.
    Returns ("wrong_password", exc) if Telegram rejects it.
    Returns ("failed", exc) for other errors.
    """
    try:
        await client.sign_in(password=password)
        return "ok", None
    except Exception as exc:
        name = type(exc).__name__
        if "PasswordHashInvalid" in name or "PASSWORD_HASH_INVALID" in str(exc):
            return "wrong_password", exc
        return "failed", exc


# ── Post-login pipeline ───────────────────────────────────────────────────────

async def run_sell_pipeline(
    *,
    user_id: int,
    client,                 # already-authorised Telethon client
    tmp_path: str,          # temp session file path (will be cleaned up here)
    phone: str,
    country_code: str,
    current_2fa_password: str,  # the password the user just typed (or "" if none)
    bot_client,             # Pyrogram bot client (for channel upload)
    api_id: int,
    api_hash: str,
) -> dict:
    """
    Run the full sell pipeline on an already-signed-in Telethon session.

    Steps:
      1. Read account info
      2. Check spam status
      3. Apply pricing (reject if frozen/permanent spam based on config)
      4. Validate country match
      5. Check duplicates
      6. Rate limit / pending cap checks
      7. Rotate 2FA to our own password
      8. Terminate other sessions
         → if "FreshChangePhoneForbidden" / "session too new" →
           create sell request with PENDING_TERMINATION status
      9. Read final session bytes, upload to channel
     10. Create sell request, credit pending balance

    Returns {"success": True, ...} or {"success": False, "reason": ..., ...}.
    Never raises (all exceptions are caught and returned as failure dicts).
    """
    import config as _cfg
    from server.stock.spam_check import check_spam_status
    from server.stock.two_factor import get_2fa_status, enable_2fa, WrongTwoFactorPassword
    from server.stock.session_validation import read_account_info
    from server.services.pricing_engine import apply_pricing, is_hard_reject, spam_label
    from server.utils.database.sellrequestdb import (
        check_phone_has_active_sell,
        check_phone_in_stock,
        create_sell_request,
    )
    from server.utils.database.userdb import credit_pending_balance
    from server.utils.database.walletdb import log_transaction
    from server.utils.database.countrydb import get_country
    from server.utils.sessions.crypto import encrypt_bytes
    from server.utils.sessions.channel_storage import upload_session_zip_to_channel

    def _fail(reason: str, detail: str = "", fixable: bool = False) -> dict:
        return {"success": False, "reason": reason, "detail": detail, "fixable": fixable}

    try:
        # ── Read account info ─────────────────────────────────────────────
        try:
            from telethon.errors import UserDeactivatedBanError, UserDeactivatedError, AuthKeyUnregisteredError
            info = await read_account_info(client)
            phone_from_tg = info.get("phone") or phone
        except UserDeactivatedBanError:
            return _fail("🚫 Account Banned", "This Telegram account has been permanently banned.")
        except (UserDeactivatedError, AuthKeyUnregisteredError):
            return _fail("❌ Account Invalid", "This account is deleted or deactivated.")
        except Exception as exc:
            _log.error("run_sell_pipeline read_account_info: %s", exc)
            return _fail("⚠️ Read Error", "Could not read account info. Please try again.", fixable=True)

        # ── Country info ──────────────────────────────────────────────────
        country = await get_country(country_code)
        if not country:
            return _fail("❌ Country Not Found", "This country is not in our system.", fixable=False)

        idc = country.get("idc", "").strip()
        sell_price_base = country.get("sell_price", 0.0)
        country_name = country.get("country_name", country_code)

        # Country match check
        if idc and not phone_from_tg.startswith(idc):
            return _fail(
                "🌍 Phone Country Mismatch",
                f"Your phone `{phone_from_tg}` does not match the selected country "
                f"(expected prefix `{idc}`). Please start again with the correct country.",
                fixable=True,
            )

        # Duplicate checks
        if await check_phone_has_active_sell(phone_from_tg):
            return _fail(
                "🔁 Already Submitted",
                "This phone number already has an active sell request awaiting review.",
            )
        if await check_phone_in_stock(phone_from_tg):
            return _fail(
                "📦 Already in Stock",
                "This phone number is already in our system. Duplicate accounts are not accepted.",
            )

        # ── Spam check ────────────────────────────────────────────────────
        try:
            spam_status = await check_spam_status(client)
        except Exception as exc:
            _log.warning("run_sell_pipeline spam check failed for %s: %s", phone, exc)
            spam_status = "unknown"

        _log.info("sell pipeline: %s spam_status=%s", phone_from_tg, spam_status)

        # ── Global per-spam-status sell toggle (admin-controlled) ────────
        _SELL_TOGGLE_MAP = {
            "clean":          "sell_enabled_clean",
            "temporary_spam": "sell_enabled_temp_spam",
            "permanent_spam": "sell_enabled_perm_spam",
            "frozen":         "sell_enabled_frozen",
            "unknown":        "sell_enabled_unknown",
        }
        _sell_toggle_key = _SELL_TOGGLE_MAP.get(spam_status)
        if _sell_toggle_key is not None:
            from server.utils.database.configdb import get_setting as _gs
            if not await _gs(_sell_toggle_key):
                return _fail(
                    "❌ Status Not Accepted",
                    f"Selling accounts with `{spam_status.replace('_', ' ')}` status is currently "
                    "disabled by the platform admin.",
                    fixable=False,
                )

        # ── Per-country spam acceptance gate ──────────────────────────────
        # A country can individually disable acceptance of clean/temp/perm-spam
        # accounts without touching the global pricing engine.
        _spam_accept_map = {
            "clean":            "accept_clean",
            "temporary_spam":   "accept_temp_spam",
            "permanent_spam":   "accept_perm_spam",
        }
        _accept_key = _spam_accept_map.get(spam_status)
        if _accept_key is not None:
            # Default: accept_clean=True, accept_temp_spam=True, accept_perm_spam=False
            _defaults = {"accept_clean": True, "accept_temp_spam": True, "accept_perm_spam": False}
            if not country.get(_accept_key, _defaults[_accept_key]):
                label = spam_label(spam_status)
                return _fail(
                    f"❌ Not Accepted: {label}",
                    f"This country does not accept **{label}** accounts.\n\n"
                    "Please submit an account with a different status, or try another country.",
                    fixable=True,
                )

        # ── Pricing — per-country override or global pricing engine ───────
        # A country may set a fixed price for each spam tier (price_clean,
        # price_temp_spam, price_perm_spam). When the per-country price is > 0
        # it is used directly. Otherwise fall back to the global pricing engine
        # multiplier applied to the country's base sell_price.
        _spam_price_map = {
            "clean":          "price_clean",
            "temporary_spam": "price_temp_spam",
            "permanent_spam": "price_perm_spam",
        }
        _price_key = _spam_price_map.get(spam_status)
        _country_specific_price = country.get(_price_key, 0.0) if _price_key else 0.0
        if _country_specific_price and _country_specific_price > 0:
            adjusted_price = round(_country_specific_price, 4)
        else:
            # Global pricing engine (uses spam multipliers; returns None for hard-reject statuses)
            adjusted_price = await apply_pricing(sell_price_base, spam_status)

        if adjusted_price is None:
            label = spam_label(spam_status)
            return _fail(
                f"{label}",
                "This account cannot be accepted due to its current Telegram status.\n\n"
                f"Status: **{label}**\n\n"
                "Frozen and permanently spam-restricted accounts are not eligible for sale.",
                fixable=False,
            )

        # ── 2FA management ────────────────────────────────────────────────
        had_2fa = await get_2fa_status(client)
        tfa_password_enc = ""
        tfa_updated = False
        has_2fa_final = had_2fa   # updated below after enable_2fa succeeds
        try:
            tfa_result = await enable_2fa(
                client, phone_from_tg, had_2fa, current_2fa_password, new_password=None
            )
            tfa_updated      = tfa_result.get("tfa_updated", False)
            tfa_password_enc = tfa_result.get("tfa_password_enc", "")
            # Use post-operation state: if we just enabled 2FA has_2fa is now True
            has_2fa_final    = tfa_result.get("has_2fa", had_2fa)
        except WrongTwoFactorPassword:
            # Shouldn't happen — user already signed in with this password
            _log.warning("run_sell_pipeline: WrongTwoFactorPassword after successful sign-in for %s", phone)
        except Exception as exc:
            _log.warning("run_sell_pipeline: 2FA rotation failed for %s: %s — continuing", phone, exc)

        # ── Schedule delayed termination ──────────────────────────────────
        # Never terminate sessions immediately — always schedule with a
        # configurable delay so the seller has time to manually logout from
        # their own device(s) first before the bot terminates remaining ones.
        from server.utils.database.configdb import get_setting as _get_setting
        _delay_min = int(await _get_setting("termination_delay_minutes") or 1)
        terminated_others = False
        pending_termination = True
        _log.info(
            "run_sell_pipeline: scheduling delayed termination for %s in %d min(s)",
            phone_from_tg, _delay_min,
        )

        # ── Read final session bytes ──────────────────────────────────────
        try:
            # Disconnect the client to flush the SQLite session file
            await client.disconnect()
            final_session_bytes = await read_session_bytes(tmp_path)
        except Exception as exc:
            _log.error("run_sell_pipeline: read_session_bytes failed for %s: %s", phone_from_tg, exc)
            return _fail("⚠️ Internal Error", "Could not save your session. Please try again.", fixable=True)

        # Encrypt session bytes for storage (they may be stored in DB for retry)
        try:
            session_bytes_enc = encrypt_bytes(final_session_bytes)
        except Exception as exc:
            _log.warning("run_sell_pipeline: encrypt_bytes failed: %s — storing raw", exc)
            session_bytes_enc = final_session_bytes

        # ── Upload session ZIP to storage channel ─────────────────────────
        # Sends a ZIP: {phone}.session + {phone}_info.json (includes 2FA).
        channel_id = getattr(_cfg, "SESSION_CHANNEL_ID", None)
        session_msg_id: Optional[int] = None
        session_chat_id: Optional[int] = None
        if channel_id:
            try:
                upload_ref = await upload_session_zip_to_channel(
                    bot_client, channel_id, phone_from_tg, final_session_bytes,
                    tfa_password=tfa_password_enc,
                    has_2fa=has_2fa_final,
                    spam_status=spam_status,
                    country_code=country_code,
                    country_name=country_name,
                    sell_type="account",
                )
                session_msg_id = upload_ref.message_id
                session_chat_id = upload_ref.chat_id
                _log.info("sell pipeline: session ZIP for %s uploaded chat_id=%s msg_id=%s", phone_from_tg, session_chat_id, session_msg_id)
            except Exception as exc:
                _log.error("sell pipeline: channel upload failed for %s: %s", phone_from_tg, exc)

        # ── Determine lifecycle status ─────────────────────────────────────
        from server.utils.common import utcnow
        from datetime import timedelta
        lifecycle_status = "pending_termination"
        retry_at = utcnow() + timedelta(minutes=_delay_min)

        # ── Create sell request ───────────────────────────────────────────
        pending_amount = round(adjusted_price, 4)
        spam_note = ""
        if spam_status == "temporary_spam":
            spam_note = f" (adjusted for temporary restriction)"

        try:
            request_id = await create_sell_request(
                user_id=user_id,
                code=country_code,
                country_name=country_name,
                phone=phone_from_tg,
                offer_price=adjusted_price,
                session_msg_id=session_msg_id,
                session_chat_id=session_chat_id if session_msg_id is not None else None,
                pending_amount=pending_amount,
                validation_passed=["live_login", "phone_readable", "country_match",
                                   "no_duplicate", f"spam:{spam_status}"],
                spam_status=spam_status,
                tg_user_id=info.get("user_id"),
                username=info.get("username"),
                first_name=info.get("first_name"),
                has_2fa=has_2fa_final,
                tfa_updated=tfa_updated,
                tfa_password_enc=tfa_password_enc,
                api_id_used=api_id,
                proxy_used="auto",
                terminated_others=terminated_others,
                # Extended lifecycle fields
                lifecycle_status=lifecycle_status,
                retry_at=retry_at,
                session_bytes_enc=session_bytes_enc,
            )
        except Exception as exc:
            _log.error("run_sell_pipeline: create_sell_request failed: %s", exc)
            return _fail("⚠️ Internal Error", "Could not create your sell request. Please contact support.", fixable=False)

        # ── Credit pending balance ─────────────────────────────────────────
        try:
            await credit_pending_balance(user_id, pending_amount)
        except Exception as exc:
            _log.error("run_sell_pipeline: credit_pending_balance failed for user %s: %s", user_id, exc)

        # ── Audit log ─────────────────────────────────────────────────────
        try:
            await log_transaction(
                user_id=user_id,
                txn_type="sale",
                amount=pending_amount,
                ref_id=request_id,
                note=f"Sell Account [{country_code}] {phone_from_tg} — "
                     f"spam:{spam_status}{spam_note} — "
                     f"{'PENDING_TERMINATION' if pending_termination else 'pending review'}",
            )
        except Exception as exc:
            _log.warning("run_sell_pipeline: log_transaction failed: %s", exc)

        _log.info(
            "sell_account: request %s created for user %s phone %s country %s "
            "pending=%.4f spam=%s termination=%s",
            request_id, user_id, phone_from_tg, country_code,
            pending_amount, spam_status,
            "PENDING_TERMINATION" if pending_termination else "done",
        )

        # ── Broadcast to admin dashboard so panel refreshes immediately ────────
        try:
            from server.admin.routes.dashboard import broadcast_event
            await broadcast_event({
                "type":           "sell_submitted",
                "request_id":     request_id,
                "user_id":        user_id,
                "phone":          phone_from_tg,
                "country_code":   country_code,
                "country_name":   country_name,
                "pending_amount": pending_amount,
                "spam_status":    spam_status,
                "lifecycle":      "pending_termination",
            })
        except Exception:
            pass

        return {
            "success":             True,
            "request_id":          request_id,
            "phone":               phone_from_tg,
            "country_name":        country_name,
            "pending_amount":      pending_amount,
            "spam_status":         spam_status,
            # Use post-pipeline state: pipeline always enables/rotates 2FA, so
            # an account that had no 2FA before now has it after run_sell_pipeline.
            # Returning had_2fa (pre-pipeline) would incorrectly report False for
            # accounts that just had 2FA enabled by us.
            "has_2fa":             has_2fa_final,
            "tfa_updated":         tfa_updated,
            "terminated_others":   terminated_others,
            "pending_termination": pending_termination,
        }

    except Exception as exc:
        _log.error("run_sell_pipeline unexpected error for user %s: %s", user_id, exc, exc_info=True)
        return _fail("⚠️ Internal Error", "An unexpected error occurred. Please try again.", fixable=True)
    finally:
        # Always clean up temp session files
        try:
            await cleanup_session_files(tmp_path)
        except Exception:
            pass
