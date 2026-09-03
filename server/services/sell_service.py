"""
Sell Service — Live Session Pipeline.

The seller never touches a .session file: they type phone + OTP (+ current
2FA password if the account has one) directly in chat (see market.py's sell
flow). Once that live login succeeds, this module runs it through the SAME
production-grade pipeline used for admin stock uploads before creating a
sell request for admin review:
  1. Rate limit: max SELL_RATE_LIMIT_MAX submissions per SELL_RATE_LIMIT_HOURS hours.
  2. Pending cap: max SELL_MAX_PENDING simultaneous pending requests per user.
  3. Full pipeline (server.stock.pipeline.process_uploaded_session): spam/freeze
     check + ALWAYS set/rotate the account's 2FA to a fresh password we control,
     then mint one more brand-new session for storage.
  4. Phone duplicate: same phone cannot have an active pending sell request or
     already be in stock.
  5. Phone country match: the account's phone must belong to the selected country.
  6. On success: upload the pipeline's final session to the storage channel,
     create sell request (still pending admin review), credit pending_balance.

All Telethon I/O is async and fully proxy-aware (reuses existing proxy infrastructure).
"""

from typing import Optional

from server import LOGGER
from server.utils.database.sellrequestdb import (
    check_phone_has_active_sell,
    check_phone_in_stock,
    get_user_recent_sell_count,
    get_user_pending_sell_count,
    create_sell_request,
    SELL_RATE_LIMIT_HOURS,
    SELL_RATE_LIMIT_MAX,
    SELL_MAX_PENDING,
)
from server.utils.database.userdb import credit_pending_balance
from server.utils.database.walletdb import log_transaction
from server.utils.database.countrydb import get_country

_log = LOGGER(__name__)

# ── Reason codes returned on failure ─────────────────────────────────────────
# Each maps to a specific user-facing explanation in _REJECTION_MESSAGES below.

RC_RATE_LIMITED      = "rate_limited"
RC_TOO_MANY_PENDING  = "too_many_pending"
RC_DUPLICATE_ACTIVE  = "duplicate_active"
RC_DUPLICATE_STOCK   = "duplicate_stock"
RC_INVALID_SESSION   = "invalid_session"
RC_BANNED            = "banned"
RC_WRONG_COUNTRY     = "wrong_country"
RC_FROZEN            = "frozen"
RC_SPAM_RESTRICTED   = "spam_restricted"
RC_CONNECT_ERROR     = "connect_error"
RC_INTERNAL          = "internal_error"

_REJECTION_MESSAGES: dict[str, dict] = {
    RC_RATE_LIMITED: {
        "title":   "⏱ Too Many Submissions",
        "detail":  (
            f"You have reached the submission limit of {SELL_RATE_LIMIT_MAX} accounts "
            f"per {SELL_RATE_LIMIT_HOURS} hours.\n\n"
            "✅ **What to do:** Wait a few hours and try again."
        ),
        "fixable": False,
    },
    RC_TOO_MANY_PENDING: {
        "title":   "⏳ Too Many Pending Requests",
        "detail":  (
            f"You already have {SELL_MAX_PENDING} or more sell requests awaiting review.\n\n"
            "✅ **What to do:** Wait for your existing requests to be reviewed before submitting more."
        ),
        "fixable": False,
    },
    RC_DUPLICATE_ACTIVE: {
        "title":   "🔁 Already Submitted",
        "detail":  (
            "This phone number already has an active sell request awaiting review.\n\n"
            "✅ **What to do:** Check 'My Sell Requests' for its current status.\n"
            "If it was rejected, you may resubmit after the review is complete."
        ),
        "fixable": False,
    },
    RC_DUPLICATE_STOCK: {
        "title":   "📦 Already in Our Stock",
        "detail":  (
            "This phone number is already in our system.\n\n"
            "❌ **Why it happened:** An account with this number was previously submitted and accepted.\n"
            "Duplicate accounts are not accepted."
        ),
        "fixable": False,
    },
    RC_INVALID_SESSION: {
        "title":   "❌ Session Invalid or Expired",
        "detail":  (
            "Your session file could not be authenticated with Telegram.\n\n"
            "❌ **Why it happened:** The session file may be:\n"
            "• Expired (you logged out on Telegram)\n"
            "• Created with incorrect API credentials\n"
            "• Corrupted or incorrectly generated\n\n"
            "✅ **What to do:** Re-generate your session file using the provided code snippet and try again."
        ),
        "fixable": True,
    },
    RC_BANNED: {
        "title":   "🚫 Account Banned",
        "detail":  (
            "This Telegram account has been permanently banned by Telegram.\n\n"
            "❌ **Why it happened:** The account violated Telegram's Terms of Service.\n"
            "Banned accounts cannot be sold on this platform."
        ),
        "fixable": False,
    },
    RC_WRONG_COUNTRY: {
        "title":   "🌍 Wrong Country",
        "detail":  (
            "The phone number in your session does not match the selected country.\n\n"
            "✅ **What to do:** Go back and select the correct country that matches your phone number's prefix."
        ),
        "fixable": True,
    },
    RC_FROZEN: {
        "title":   "🧊 Account Frozen",
        "detail":  (
            "This account has been frozen (permanently limited) by Telegram.\n\n"
            "❌ **Why it happened:** Telegram has placed the most severe limitation on this account.\n"
            "Frozen accounts cannot be logged into and are not accepted on this platform."
        ),
        "fixable": False,
    },
    RC_SPAM_RESTRICTED: {
        "title":   "🚫 Account Spam Restricted",
        "detail":  (
            "This account has been flagged for spam by Telegram.\n\n"
            "❌ **Why it happened:** The account sent spam messages or was mass-reported.\n"
            "Spam-restricted accounts are not accepted on this platform."
        ),
        "fixable": False,
    },
    RC_CONNECT_ERROR: {
        "title":   "🌐 Connection Failed",
        "detail":  (
            "We couldn't connect to Telegram to validate your session.\n\n"
            "✅ **What to do:** This is a temporary network issue. Please try again in a few minutes."
        ),
        "fixable": True,
    },
    RC_INTERNAL: {
        "title":   "⚠️ Internal Error",
        "detail":  (
            "An unexpected error occurred while processing your session.\n\n"
            "✅ **What to do:** Please try again. If the problem persists, contact support."
        ),
        "fixable": True,
    },
}


def get_rejection_message(reason_code: str) -> dict:
    """Return the user-facing explanation dict for a rejection reason code."""
    return _REJECTION_MESSAGES.get(reason_code, _REJECTION_MESSAGES[RC_INTERNAL])


async def validate_and_submit_sell_live(
    user_id: int,
    country_code: str,
    session_bytes: bytes,
    current_2fa_password: str,
    bot_client,       # Pyrogram bot client (for channel upload)
    channel_id: int,
    api_id: int,
    api_hash: str,
    otp_timeout: int = 120,
) -> dict:
    """
    Full sell pipeline for a LIVE, already-authorized session captured by the
    bot itself (seller only typed phone + OTP + current 2FA if any — no
    .session file changes hands). Runs the same production-grade pipeline
    used for admin stock uploads (`process_uploaded_session`): spam/freeze
    check, then ALWAYS set/rotate the account's 2FA password to a fresh one
    we control (so a future buyer's delivered `.json` has a working twoFA),
    then generate one more brand-new session to hand off cleanly.

    `current_2fa_password` is the password the seller just typed to sign in
    (empty string if the account had no 2FA) — passed straight through as
    the pipeline's `admin_2fa_password` so it never re-prompts.

    Returns on success:
      {"success": True, "request_id": str, "phone": str, "pending_amount": float,
       "country_name": str, "spam_status": str, "has_2fa": bool, "tfa_updated": bool}

    Returns on failure:
      {"success": False, "reason_code": str, "reason": str, "detail": str, "fixable": bool}
    """
    def _fail(reason_code: str, extra: str = "") -> dict:
        msg = get_rejection_message(reason_code)
        detail = msg["detail"]
        if extra:
            detail = f"{detail}\n\n_Technical detail: {extra}_"
        return {
            "success":     False,
            "reason_code": reason_code,
            "reason":      msg["title"],
            "detail":      detail,
            "fixable":     msg["fixable"],
        }

    # ── Step 1: Rate limit ────────────────────────────────────────────────────
    recent_count = await get_user_recent_sell_count(user_id, SELL_RATE_LIMIT_HOURS)
    if recent_count >= SELL_RATE_LIMIT_MAX:
        _log.info("sell rate limit: user %s has %d submissions in last %dh", user_id, recent_count, SELL_RATE_LIMIT_HOURS)
        return _fail(RC_RATE_LIMITED)

    # ── Step 2: Max pending cap ───────────────────────────────────────────────
    pending_count = await get_user_pending_sell_count(user_id)
    if pending_count >= SELL_MAX_PENDING:
        _log.info("sell pending cap: user %s already has %d pending", user_id, pending_count)
        return _fail(RC_TOO_MANY_PENDING)

    # ── Step 3: Country info ──────────────────────────────────────────────────
    country = await get_country(country_code)
    if not country:
        return _fail(RC_INTERNAL, f"country {country_code!r} not found")

    idc = country.get("idc", "").strip()  # e.g. "+91"
    sell_price = country.get("sell_price", 0.0)
    country_name = country.get("country_name", country_code)

    # ── Step 4: Run the full pipeline (spam check + always rotate 2FA) ───────
    from server.stock.pipeline import process_uploaded_session

    result = await process_uploaded_session(
        session_bytes, api_id, api_hash, country_code,
        admin_2fa_password=current_2fa_password,
        otp_timeout=otp_timeout,
        batch_action="enable",       # always set/rotate to a fresh password we control
        batch_new_password=None,     # let it auto-generate a strong one
    )

    if result.get("banned"):
        return _fail(RC_BANNED, result.get("error") or "")
    if result.get("frozen"):
        return _fail(RC_FROZEN, result.get("error") or "")
    if not result.get("success"):
        reason = RC_CONNECT_ERROR if result.get("outcome") == "network_error" else RC_INVALID_SESSION
        return _fail(reason, result.get("error") or "")

    phone = result.get("phone") or ""
    if not phone:
        return _fail(RC_INVALID_SESSION, "could not read phone from session")

    # ── Step 5: Phone duplicate checks ────────────────────────────────────────
    if await check_phone_has_active_sell(phone):
        return _fail(RC_DUPLICATE_ACTIVE)
    if await check_phone_in_stock(phone):
        return _fail(RC_DUPLICATE_STOCK)

    # ── Step 6: Country match ─────────────────────────────────────────────────
    if idc and not phone.startswith(idc):
        _log.info(
            "sell country mismatch: user %s phone %s doesn't match IDC %s for %s",
            user_id, phone, idc, country_code,
        )
        return _fail(RC_WRONG_COUNTRY)

    # ── Step 7: Spam status ────────────────────────────────────────────────────
    spam_status = result.get("spam_status", "unknown")
    if result.get("permanent_spam"):
        return _fail(RC_SPAM_RESTRICTED)
    # temporary_spam: accepted but noted; admin decides on final review.

    # ── Step 8: Upload the pipeline's FINAL session ZIP to the storage channel ─
    # ZIP contains: {phone}.session + {phone}_info.json with 2FA password.
    from server.utils.sessions.channel_storage import upload_session_zip_to_channel

    fresh_bytes = result.get("fresh_session_bytes") or session_bytes
    plain_tfa   = result.get("tfa_password_enc", "")
    session_msg_id: Optional[int] = None
    session_chat_id: Optional[int] = None
    try:
        upload_ref = await upload_session_zip_to_channel(
            bot_client, channel_id, phone, fresh_bytes,
            tfa_password=plain_tfa,
            has_2fa=result.get("has_2fa", False),
            spam_status=spam_status,
            country_code=country_code,
            country_name=country_name,
            sell_type="account",
        )
        session_msg_id = upload_ref.message_id
        session_chat_id = upload_ref.chat_id
        _log.info("sell_service: session ZIP for %s uploaded chat_id=%s msg_id=%s", phone, session_chat_id, session_msg_id)
    except Exception as exc:
        _log.error("sell_service: channel upload failed for %s: %s", phone, exc)
        # Non-fatal — request still created so user gets credited.

    # ── Step 9: Create sell request + credit pending_balance ──────────────────
    pending_amount = round(sell_price, 4)
    checks_passed = ["authorized", "phone_readable", "no_duplicate", "country_match", f"spam_check:{spam_status}"]
    try:
        request_id = await create_sell_request(
            user_id=user_id,
            code=country_code,
            country_name=country_name,
            phone=phone,
            offer_price=sell_price,
            session_msg_id=session_msg_id,
            session_chat_id=session_chat_id if session_msg_id is not None else None,
            pending_amount=pending_amount,
            validation_passed=checks_passed,
            spam_status=spam_status,
            tg_user_id=result.get("user_id"),
            username=result.get("username"),
            first_name=result.get("first_name"),
            has_2fa=result.get("has_2fa", False),
            tfa_updated=result.get("tfa_updated", False),
            tfa_password_enc=result.get("tfa_password_enc", ""),
            api_id_used=result.get("api_id_used") or api_id,
            proxy_used=result.get("proxy_used", "none"),
            terminated_others=result.get("terminated_others", False),
        )
    except Exception as exc:
        _log.error("sell_service: create_sell_request failed for user %s: %s", user_id, exc)
        return _fail(RC_INTERNAL, str(exc))

    # Credit pending balance
    try:
        await credit_pending_balance(user_id, pending_amount)
    except Exception as exc:
        _log.error("sell_service: credit_pending_balance failed for user %s: %s", user_id, exc)
        # Not a hard failure — request is created; admin can manually credit if needed

    # Log transaction for audit trail
    try:
        await log_transaction(
            user_id=user_id,
            txn_type="sale",
            amount=pending_amount,
            ref_id=request_id,
            note=f"Sell submission [{country_code}] {phone} — pending admin review",
        )
    except Exception as exc:
        _log.warning("sell_service: log_transaction failed for %s: %s", request_id, exc)

    _log.info(
        "sell_service: request %s created for user %s phone %s country %s pending=%.4f",
        request_id, user_id, phone, country_code, pending_amount,
    )

    # ── Broadcast to admin dashboard so panel refreshes immediately ───────────
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type":           "sell_submitted",
            "request_id":     request_id,
            "user_id":        user_id,
            "phone":          phone,
            "country_code":   country_code,
            "country_name":   country_name,
            "pending_amount": pending_amount,
            "spam_status":    spam_status,
            "lifecycle":      "pending",
        })
    except Exception:
        pass

    return {
        "success":        True,
        "request_id":     request_id,
        "phone":          phone,
        "pending_amount": pending_amount,
        "country_name":   country_name,
        "spam_status":    spam_status,
        "has_2fa":        result.get("has_2fa", False),
        "tfa_updated":    result.get("tfa_updated", False),
    }


async def validate_and_submit_sell(
    user_id: int,
    session_bytes: bytes,
    bot_client,
    channel_id: int,
    api_id: int,
    api_hash: str,
    current_2fa_password: str = "",
    sell_type: str = "session",
) -> dict:
    """
    Validate and submit a user-uploaded .session file for the Sell Session flow.

    This is the file-upload path (user provides raw .session bytes from a ZIP
    or a standalone .session file).  Reuses the same production pipeline as
    validate_and_submit_sell_live but without a country pre-selection — the
    country is auto-detected from the phone number read out of the session.

    Returns the same shape as validate_and_submit_sell_live.
    If the session has 2FA and current_2fa_password is empty/wrong, returns
    {"needs_2fa": True, ...} so the caller can prompt the user interactively.
    """
    def _fail(reason_code: str, extra: str = "", needs_2fa: bool = False) -> dict:
        msg = get_rejection_message(reason_code)
        detail = msg["detail"]
        if extra:
            detail = f"{detail}\n\n_Technical detail: {extra}_"
        return {
            "success":     False,
            "needs_2fa":   needs_2fa,
            "reason_code": reason_code,
            "reason":      msg["title"],
            "detail":      detail,
            "fixable":     msg["fixable"],
        }

    # ── Rate limit / pending cap ──────────────────────────────────────────────
    recent_count = await get_user_recent_sell_count(user_id, SELL_RATE_LIMIT_HOURS)
    if recent_count >= SELL_RATE_LIMIT_MAX:
        return _fail(RC_RATE_LIMITED)

    pending_count = await get_user_pending_sell_count(user_id)
    if pending_count >= SELL_MAX_PENDING:
        return _fail(RC_TOO_MANY_PENDING)

    # ── Run pipeline ──────────────────────────────────────────────────────────
    from server.stock.pipeline import process_uploaded_session

    result = await process_uploaded_session(
        session_bytes, api_id, api_hash, "XX",  # country auto-detected from phone
        admin_2fa_password=current_2fa_password,
        otp_timeout=120,
        batch_action="enable",
        batch_new_password=None,
    )

    if result.get("needs_admin_password"):
        return _fail(RC_INVALID_SESSION, needs_2fa=True)

    if result.get("banned"):
        return _fail(RC_BANNED, result.get("error") or "")
    if result.get("frozen"):
        return _fail(RC_FROZEN, result.get("error") or "")
    if not result.get("success"):
        reason = RC_CONNECT_ERROR if result.get("outcome") == "network_error" else RC_INVALID_SESSION
        return _fail(reason, result.get("error") or "")

    phone = result.get("phone") or ""
    if not phone:
        return _fail(RC_INVALID_SESSION, "could not read phone from session")

    # Duplicate checks
    if await check_phone_has_active_sell(phone):
        return _fail(RC_DUPLICATE_ACTIVE)
    if await check_phone_in_stock(phone):
        return _fail(RC_DUPLICATE_STOCK)

    # Detect country from phone
    country_code, country_name, sell_price = "XX", "Unknown", 0.0
    spam_status = result.get("spam_status", "unknown")

    try:
        import phonenumbers as _pn
        parsed = _pn.parse(phone, None)
        region = _pn.region_code_for_number(parsed) or "XX"
        country_code = region.upper()
    except Exception:
        pass

    country = await get_country(country_code) if country_code != "XX" else None
    if country:
        country_name = country.get("country_name", country_code)
        sell_price   = country.get("sell_price", 0.0)
    else:
        # Try fallback: look up all countries and match by phone prefix
        from server.utils.database.countrydb import get_all_countries
        all_countries = await get_all_countries()
        for c in all_countries:
            idc = c.get("idc", "").strip()
            if idc and phone.startswith(idc):
                country = c
                country_code = c.get("code", "XX")
                country_name = c.get("country_name", country_code)
                sell_price   = c.get("sell_price", 0.0)
                break

    # ── Global per-spam-status sell toggle (admin-controlled) ────────────────
    _SELL_TOGGLE = {
        "clean":          "sell_enabled_clean",
        "temporary_spam": "sell_enabled_temp_spam",
        "permanent_spam": "sell_enabled_perm_spam",
        "frozen":         "sell_enabled_frozen",
        "unknown":        "sell_enabled_unknown",
    }
    _sell_key = _SELL_TOGGLE.get(spam_status)
    if _sell_key is not None:
        from server.utils.database.configdb import get_setting as _gs
        if not await _gs(_sell_key):
            return _fail(
                RC_FROZEN if spam_status == "frozen" else RC_SPAM_RESTRICTED,
                f"Selling {spam_status} accounts is currently disabled by admin",
            )

    # ── Per-country spam acceptance gate ─────────────────────────────────────
    if country:
        _spam_accept_map = {
            "clean":          "accept_clean",
            "temporary_spam": "accept_temp_spam",
            "permanent_spam": "accept_perm_spam",
        }
        _accept_key = _spam_accept_map.get(spam_status)
        if _accept_key is not None:
            _defaults = {"accept_clean": True, "accept_temp_spam": True, "accept_perm_spam": False}
            if not country.get(_accept_key, _defaults[_accept_key]):
                return _fail(RC_SPAM_RESTRICTED, f"Country {country_code} does not accept {spam_status} accounts")

    # ── Pricing: per-country override → global engine fallback ───────────────
    from server.services.pricing_engine import apply_pricing
    _spam_price_map = {
        "clean":          "price_clean",
        "temporary_spam": "price_temp_spam",
        "permanent_spam": "price_perm_spam",
    }
    _price_key = _spam_price_map.get(spam_status)
    _country_price = (country.get(_price_key, 0.0) if (country and _price_key) else 0.0) or 0.0
    if _country_price > 0:
        adjusted_price = round(_country_price, 4)
    else:
        adjusted_price = await apply_pricing(sell_price, spam_status)

    if adjusted_price is None:
        return _fail(RC_FROZEN if spam_status == "frozen" else RC_SPAM_RESTRICTED)

    # Upload session ZIP to channel (session file + info JSON with 2FA)
    from server.utils.sessions.channel_storage import upload_session_zip_to_channel
    fresh_bytes  = result.get("fresh_session_bytes") or session_bytes
    plain_tfa    = result.get("tfa_password_enc", "")
    session_msg_id: Optional[int] = None
    session_chat_id: Optional[int] = None
    try:
        upload_ref = await upload_session_zip_to_channel(
            bot_client, channel_id, phone, fresh_bytes,
            tfa_password=plain_tfa,
            has_2fa=result.get("has_2fa", False),
            spam_status=spam_status,
            country_code=country_code,
            country_name=country_name,
            sell_type=sell_type,
        )
        session_msg_id = upload_ref.message_id
        session_chat_id = upload_ref.chat_id
    except Exception as exc:
        _log.error("validate_and_submit_sell: channel upload failed for %s: %s", phone, exc)

    # Create sell request
    pending_amount = round(adjusted_price, 4)
    checks_passed = ["authorized", "phone_readable", "no_duplicate", f"spam_check:{spam_status}"]
    try:
        request_id = await create_sell_request(
            user_id=user_id,
            code=country_code,
            country_name=country_name,
            phone=phone,
            offer_price=adjusted_price,
            session_msg_id=session_msg_id,
            session_chat_id=session_chat_id if session_msg_id is not None else None,
            pending_amount=pending_amount,
            validation_passed=checks_passed,
            spam_status=spam_status,
            tg_user_id=result.get("user_id"),
            username=result.get("username"),
            first_name=result.get("first_name"),
            has_2fa=result.get("has_2fa", False),
            tfa_updated=result.get("tfa_updated", False),
            tfa_password_enc=result.get("tfa_password_enc", ""),
            api_id_used=api_id,
            proxy_used=result.get("proxy_used", "none"),
            terminated_others=result.get("terminated_others", False),
            sell_type=sell_type,
        )
    except Exception as exc:
        _log.error("validate_and_submit_sell: create_sell_request failed: %s", exc)
        return _fail(RC_INTERNAL, str(exc))

    try:
        await credit_pending_balance(user_id, pending_amount)
    except Exception as exc:
        _log.error("validate_and_submit_sell: credit_pending_balance failed: %s", exc)

    try:
        await log_transaction(
            user_id=user_id,
            txn_type="sale",
            amount=pending_amount,
            ref_id=request_id,
            note=f"Sell Session [{country_code}] {phone} — pending admin review",
        )
    except Exception as exc:
        _log.warning("validate_and_submit_sell: log_transaction failed: %s", exc)

    _log.info(
        "validate_and_submit_sell: request %s user %s phone %s pending=%.4f",
        request_id, user_id, phone, pending_amount,
    )

    # ── Broadcast to admin dashboard ──────────────────────────────────────────
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type":           "sell_submitted",
            "request_id":     request_id,
            "user_id":        user_id,
            "phone":          phone,
            "country_code":   country_code,
            "country_name":   country_name,
            "pending_amount": pending_amount,
            "spam_status":    spam_status,
            "lifecycle":      "pending",
        })
    except Exception:
        pass

    return {
        "success":        True,
        "request_id":     request_id,
        "phone":          phone,
        "pending_amount": pending_amount,
        "country_name":   country_name,
        "spam_status":    spam_status,
        "has_2fa":        result.get("has_2fa", False),
        "tfa_updated":    result.get("tfa_updated", False),
        "needs_2fa":      False,
    }
