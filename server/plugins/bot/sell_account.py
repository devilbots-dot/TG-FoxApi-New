"""
Sell Account Plugin — Live phone + OTP + 2FA login flow.

User flow:
  1. sell_account_start  → prompts for phone number
  2. _handle_phone       → validates format + all pre-OTP eligibility checks
                           → sends OTP via Telegram
  3. _handle_otp         → verifies OTP code
  4. _handle_2fa         → (if 2FA required) verifies current 2FA password
  5. _run_pipeline       → backend: spam check, pricing, 2FA rotation,
                           session termination, DB save, balance credit
"""

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from server.utils.bot_utils import Btn as InlineKeyboardButton

import config as _cfg
from server import bot, LOGGER
from server.utils.bot_utils import safe_edit as _safe_edit
from server.utils.database import is_banned_user
from server.plugins.bot._shared_state import sell_account_state as _SA_STATE

_log = LOGGER(__name__)
_MAX_OTP_RETRIES = 3


# ── Button helpers ─────────────────────────────────────────────────────────────
# All helpers return a list[InlineKeyboardButton] (a single keyboard row).
# Wrap in InlineKeyboardMarkup([...rows...]) at the call site.

def _back_row() -> list:
    """A single 'Back' button row that returns to the home menu."""
    return [InlineKeyboardButton(
        "![⬅️](tg://emoji?id=5258236805890710909) Back",
        callback_data="back_home",
    )]


def _cancel_row() -> list:
    """A single 'Cancel' button row that returns to the home menu mid-flow."""
    return [InlineKeyboardButton(
        "![❌](tg://emoji?id=5348567359764317130) Cancel",
        callback_data="back_home",
    )]


def _mk(rows: list) -> InlineKeyboardMarkup:
    """Shorthand: wrap a list of rows into InlineKeyboardMarkup."""
    return InlineKeyboardMarkup(rows)


# ── State cleanup ──────────────────────────────────────────────────────────────

async def _cleanup_state(user_id: int) -> None:
    """
    Pop and clean up any in-progress sell state for this user.
    Disconnects the Telethon client and removes temp session files.
    Safe to call even when no state exists.
    """
    state = _SA_STATE.pop(user_id, None)
    if not state:
        return

    client = state.get("client")
    tmp_path = state.get("tmp_path")

    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

    if tmp_path:
        try:
            from server.utils.sessions.telethon_client import cleanup_session_files
            await cleanup_session_files(tmp_path)
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────

async def begin_sell_account_flow(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Initialize the existing seller phone/OTP/2FA flow for a trusted entry."""
    await _cleanup_state(user_id)
    try:
        from server.plugins.bot._shared_state import clear_pending_flows
        clear_pending_flows(user_id, keep="sell_account")
    except Exception:
        pass

    _SA_STATE[user_id] = {
        "step":            "await_phone",
        "phone":           None,
        "country_code":    None,
        "phone_code_hash": None,
        "client":          None,
        "tmp_path":        None,
        "current_2fa":     None,
        "otp_retries":     0,
    }
    text = (
        "![📱](tg://emoji?id=5330237710655306682) **Enter your phone number** in international format.\n\n"
        "Example: `+447911123456`"
    )
    keyboard = _mk([
        [
            InlineKeyboardButton(
                "![🌍](tg://emoji?id=6296303781126604562) View Prices",
                callback_data="sell_page_1",
            ),
            InlineKeyboardButton(
                "![📤](tg://emoji?id=6131886699254388574) Sell Now",
                callback_data="sell_account_start",
            ),
        ],
        _cancel_row(),
    ])
    return text, keyboard

@bot.on_callback_query(filters.regex("^sell_account_start$"))
async def sell_account_start_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id

        if await is_banned_user(user_id):
            await cq.answer("You are banned.", show_alert=True)
            return

        text, keyboard = await begin_sell_account_flow(user_id)
        await _safe_edit(
            cq,
            text,
            keyboard,
        )
        await cq.answer()

    except Exception as exc:
        _log.error("sell_account_start_cb: %s", exc, exc_info=True)


# ── Message router ─────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "help"]))
async def sell_account_text_handler(client, message: Message):
    user_id = message.from_user.id
    state = _SA_STATE.get(user_id)
    if not state:
        await message.continue_propagation()
        return

    step = state.get("step")
    if step == "await_phone":
        await _handle_phone(client, message, state)
    elif step == "await_otp":
        await _handle_otp(client, message, state)
    elif step == "await_2fa":
        await _handle_2fa(client, message, state)
    else:
        await message.continue_propagation()


# ── Step 1: Phone ──────────────────────────────────────────────────────────────

async def _handle_phone(client, message: Message, state: dict):
    user_id = message.from_user.id
    from server.services.sell_account_service import validate_phone, validate_phone_eligibility, start_login

    raw = (message.text or "").strip()

    # If the text is clearly not a phone number attempt, let it fall through
    # to other handlers (wallet address, deposit amount, etc.) instead of
    # replying "Invalid phone number". Users normally type +<digits> here.
    _digits = "".join(ch for ch in raw if ch.isdigit())
    if (not raw.startswith("+")) or any(ch.isalpha() for ch in raw) or len(_digits) < 6 or len(raw) > 20:
        await message.continue_propagation()
        return

    # ── 1a. Format validation ──────────────────────────────────────────────────
    valid, phone, country_code = validate_phone(raw)
    if not valid:
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) Invalid phone number.\n\n"
            "Use international format, e.g. `+447911123456`",
            reply_markup=_mk([_cancel_row()]),
        )
        return

    # ── 1b. Pre-OTP eligibility checks (all before sending the code) ───────────
    # Checks: maintenance, selling enabled, country supported, price set,
    # country not disabled/full, rate limit, pending cap, duplicate.
    ok, err_msg = await validate_phone_eligibility(user_id, phone, country_code)
    if not ok:
        await message.reply_text(err_msg, reply_markup=_mk([_back_row()]))
        await _cleanup_state(user_id)
        return

    # ── 1c. Send OTP via Telegram ──────────────────────────────────────────────
    progress = await message.reply_text(
        f"📡 Sending login code to `{phone}`…"
    )

    try:
        tg_client, tmp_path, phone_code_hash, _proxy = await start_login(
            phone, country_code, _cfg.API_ID, _cfg.API_HASH,
        )
    except Exception as exc:
        err = str(exc).lower()
        if "flood" in err:
            msg = (
                "![⏳](tg://emoji?id=6129574787078429498) Telegram is rate-limiting this number.\n"
                "Please wait a few minutes and try again."
            )
        elif "invalid" in err and "phone" in err:
            msg = "![❌](tg://emoji?id=6129846551134084367) Telegram does not recognise this number."
        elif "phone_number_banned" in err:
            msg = "![❌](tg://emoji?id=6129846551134084367) This number is banned on Telegram."
        else:
            _log.warning("start_login failed for %s: %s", phone, exc)
            msg = "![❌](tg://emoji?id=6129846551134084367) Could not send the login code. Please try again."

        await progress.edit_text(msg, reply_markup=_mk([_cancel_row()]))
        return

    # ── 1d. Advance state ──────────────────────────────────────────────────────
    state.update({
        "step":            "await_otp",
        "phone":           phone,
        "country_code":    country_code,
        "phone_code_hash": phone_code_hash,
        "client":          tg_client,
        "tmp_path":        tmp_path,
        "otp_retries":     0,
    })

    await progress.edit_text(
        f"![🔑](tg://emoji?id=6129782440157256336) Login code sent to `{phone}`.\n\n"
        "Enter the code below (check your Telegram app):",
        reply_markup=_mk([_cancel_row()]),
    )


# ── Step 2: OTP ────────────────────────────────────────────────────────────────

async def _handle_otp(client, message: Message, state: dict):
    user_id = message.from_user.id
    from server.services.sell_account_service import submit_otp

    otp = (message.text or "").strip().replace(" ", "")
    status, exc = await submit_otp(
        state["client"], state["phone"], otp, state["phone_code_hash"],
    )

    if status == "wrong_otp":
        state["otp_retries"] = state.get("otp_retries", 0) + 1
        left = _MAX_OTP_RETRIES - state["otp_retries"]
        if left <= 0:
            await message.reply_text(
                "![❌](tg://emoji?id=6129846551134084367) Too many wrong codes. Please start over.",
                reply_markup=_mk([_back_row()]),
            )
            await _cleanup_state(user_id)
            return
        await message.reply_text(
            f"![❌](tg://emoji?id=6129846551134084367) Wrong code. {left} attempt(s) left.",
            reply_markup=_mk([_cancel_row()]),
        )
        return

    if status == "expired_otp":
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) This code has expired. Please start over and request a new one.",
            reply_markup=_mk([_back_row()]),
        )
        await _cleanup_state(user_id)
        return

    if status == "failed":
        _log.error("_handle_otp: %s — %s", state["phone"], exc)
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) Sign-in failed. Please try again.",
            reply_markup=_mk([_cancel_row()]),
        )
        return

    if status == "2fa_required":
        state["step"] = "await_2fa"
        await message.reply_text(
            "![🔐](tg://emoji?id=6129550284290006595) This account has **2FA** enabled.\n\n"
            "Enter your current 2FA password:",
            reply_markup=_mk([_cancel_row()]),
        )
        return

    # OTP accepted, no 2FA — run pipeline
    state["current_2fa"] = ""
    await _run_pipeline_and_reply(client, message, state, user_id)


# ── Step 3: 2FA ────────────────────────────────────────────────────────────────

async def _handle_2fa(client, message: Message, state: dict):
    user_id = message.from_user.id
    from server.services.sell_account_service import submit_2fa

    password = (message.text or "").strip()
    status, exc = await submit_2fa(state["client"], password)

    if status == "wrong_password":
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) Wrong 2FA password. Please try again.",
            reply_markup=_mk([_cancel_row()]),
        )
        return

    if status == "failed":
        _log.error("_handle_2fa: user %s — %s", user_id, exc)
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) 2FA verification failed. Please try again.",
            reply_markup=_mk([_cancel_row()]),
        )
        return

    state["current_2fa"] = password
    await _run_pipeline_and_reply(client, message, state, user_id)


# ── Pipeline ───────────────────────────────────────────────────────────────────

async def _run_pipeline_and_reply(bot_client, message: Message, state: dict, user_id: int):
    phone        = state["phone"]
    country_code = state["country_code"]
    tg_client    = state["client"]
    tmp_path     = state["tmp_path"]
    current_2fa  = state.get("current_2fa", "")

    # Clear state immediately — pipeline owns the client from here
    _SA_STATE.pop(user_id, None)

    progress = await message.reply_text(
        f"![⚙️](tg://emoji?id=5316798307014556036) Processing `{phone}`… please wait."
    )

    from server.services.sell_account_service import run_sell_pipeline
    result = await run_sell_pipeline(
        user_id=user_id,
        client=tg_client,
        tmp_path=tmp_path,
        phone=phone,
        country_code=country_code,
        current_2fa_password=current_2fa,
        bot_client=bot_client,
        api_id=_cfg.API_ID,
        api_hash=_cfg.API_HASH,
    )

    if result["success"]:
        pending_amount = result["pending_amount"]
        request_id     = result["request_id"]
        country_name   = result["country_name"]
        spam_status    = result["spam_status"]
        pending_term   = result.get("pending_termination", False)

        status_line = (
            "🕐 Termination pending (48h)"
            if pending_term else
            "![⏳](tg://emoji?id=6129574787078429498) Awaiting review"
        )
        spam_line = (
            "\n![⚠️](tg://emoji?id=6129939837823753679) Temporary restriction — price adjusted."
            if spam_status == "temporary_spam" else ""
        )

        text = (
            f"![✅](tg://emoji?id=6129492160497589882) **Account Submitted**\n\n"
            f"![📱](tg://emoji?id=5330237710655306682) `{result['phone']}`\n"
            f"![🌍](tg://emoji?id=6296303781126604562) {country_name}\n"
            f"![💰](tg://emoji?id=6129731974291527294) +`${pending_amount:g}` pending\n"
            f"![📋](tg://emoji?id=6129579803600231171) {status_line}"
            f"{spam_line}\n\n"
            f"ID: `{request_id}`"
        )
        await progress.edit_text(
            text,
            reply_markup=_mk([
                [InlineKeyboardButton(
                    "![📋](tg://emoji?id=6129579803600231171) My Requests",
                    callback_data="sell_my_requests",
                )],
                _back_row(),
            ]),
        )

        # Admin log group notification
        try:
            log_group = getattr(_cfg, "LOG_GROUP_ID", None)
            if log_group:
                from server.utils.database import get_user
                user_doc = await get_user(user_id)
                uname = (
                    f"@{user_doc['username']}"
                    if user_doc and user_doc.get("username")
                    else f"ID:{user_id}"
                )
                await bot_client.send_message(
                    log_group,
                    f"{'🕐' if pending_term else '![💼](tg://emoji?id=5316561753100792764)'} "
                    f"**New Sell Account**\n\n"
                    f"![👤](tg://emoji?id=5316979275461573049) {uname} (`{user_id}`)\n"
                    f"![📱](tg://emoji?id=5330237710655306682) `{result['phone']}` — {country_name}\n"
                    f"![💵](tg://emoji?id=6129731974291527294) `${pending_amount:g}` | "
                    f"Spam: `{spam_status}`\n"
                    f"🆔 `{request_id}`",
                    reply_markup=_mk([[
                        InlineKeyboardButton(
                            "![✅](tg://emoji?id=6129492160497589882) Approve",
                            callback_data=f"sela_{request_id}",
                        ),
                        InlineKeyboardButton(
                            "![❌](tg://emoji?id=6129846551134084367) Reject",
                            callback_data=f"selr_{request_id}",
                        ),
                    ]]),
                )
        except Exception as exc:
            _log.warning("sell_account log group notify failed: %s", exc)

    else:
        reason  = result["reason"]
        detail  = result["detail"]
        fixable = result.get("fixable", False)
        rows = []
        if fixable:
            rows.append([InlineKeyboardButton(
                "![🔄](tg://emoji?id=6129792056589031358) Try Again",
                callback_data="sell_account_start",
            )])
        rows.append(_back_row())
        await progress.edit_text(
            f"{reason}\n\n{detail}",
            reply_markup=_mk(rows),
        )
