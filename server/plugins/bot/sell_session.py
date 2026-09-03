

# ─────────────────────────────────────────────────────────────────────────────
# PREMIUM EMOJIS (Telegram 9.4+ custom emoji entities).
#
# Rendered via pyrogram's default Markdown syntax:  ![e](tg://emoji?id=ID)
# Clients on Telegram 9.4+ show the premium emoji; older clients / non-premium
# users see the plain unicode fallback automatically — nothing else to do.
#
# Change any ID here to swap what the users see across this whole file.
# `_pe("✅")` also works if you want to compose new strings by hand.
# ─────────────────────────────────────────────────────────────────────────────
PREMIUM_EMOJIS = {
    '⏳': "6129574787078429498",
    '⚠️': "6129939837823753679",
    '✅': "6129492160497589882",
    '❌': "6129846551134084367",
    '⬅️': "6129550284290006595",
    '🌍': "6296303781126604562",
    '👤': "5316979275461573049",
    '💰': "6129731974291527294",
    '💵': "6129731974291527294",
    '📋': "6129579803600231171",
    '📦': "6131886699254388574",
    '📱': "5330237710655306682",
    '🔄': "6129792056589031358",
    '🔍': "5316722951813346475",
    '🔐': "6129550284290006595",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Sell Session Plugin — User uploads ZIP / .session file → admin pipeline.
"""

import asyncio
import io

from pyrogram import filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from server.utils.bot_utils import Btn as InlineKeyboardButton

import config as _cfg
from server import bot, LOGGER
from server.utils.bot_utils import safe_edit as _safe_edit
from server.utils.database import get_user_lang, is_banned_user
from server.utils.pricing_discounts import rank_index, get_user_rank, get_user_total_spend, get_required_spend_for
from server.utils.database.configdb import get_setting
from server.plugins.bot._shared_state import sell_session_state as _SS_STATE
from strings import get_string

_log = LOGGER(__name__)

_MAX_ZIP_SIZE = 50 * 1024 * 1024   # 50 MB
_MAX_SES_SIZE = 512 * 1024         # 512 KB


def _cancel_btn():
    return [InlineKeyboardButton("![❌](tg://emoji?id=5348567359764317130) Cancel", callback_data="sell_session_cancel")]


def _back_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("![⬅️](tg://emoji?id=5258236805890710909) Back", callback_data="back_home")]])


# ── Entry ─────────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^sell_session_start$"))
async def sell_session_start_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        if await is_banned_user(user_id):
            await cq.answer("You are banned.", show_alert=True)
            return

        # Session-selling gate: master toggle + rank threshold
        try:
            selling_on = await get_setting("session_selling_enabled")
        except Exception:
            selling_on = True
        try:
            min_rank = str(await get_setting("session_selling_min_rank") or "").strip().upper()
        except Exception:
            min_rank = ""

        support_btn = None
        try:
            sup_url = _cfg.support_link() if hasattr(_cfg, "support_link") else None
            if sup_url:
                support_btn = InlineKeyboardButton("Contact Support", url=sup_url)
        except Exception:
            support_btn = None

        # Localised strings
        try:
            _lang = await get_user_lang(user_id) or "en"
        except Exception:
            _lang = "en"
        try:
            _s = get_string(_lang)
        except Exception:
            _s = {}

        _back_lbl = _s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back")
        if support_btn is not None:
            try:
                support_btn = InlineKeyboardButton(
                    _s.get("btn_contact_support", "Contact Support"),
                    url=support_btn.url,
                )
            except Exception:
                pass

        if selling_on is False:
            rows = []
            if support_btn:
                rows.append([support_btn])
            rows.append([InlineKeyboardButton(_back_lbl, callback_data="back_home")])
            _txt = _s.get(
                "sell_disabled_msg",
                "🛑 **Session selling is currently disabled.**\n\n"
                "Please try again later. If you have any doubt or issue, contact support.",
            )
            # Append rank status + progress so user knows what's required
            try:
                _user_rank = await get_user_rank(user_id)
            except Exception:
                _user_rank = ""
            try:
                _spent = await get_user_total_spend(user_id)
            except Exception:
                _spent = 0.0
            _required = 0.0
            if min_rank:
                try:
                    _required = await get_required_spend_for(min_rank)
                except Exception:
                    _required = 0.0
            _needed = max(0.0, _required - _spent)
            _rank_line = (
                f"\n\n👤 Your rank: **{_user_rank or 'N/A'}** — spent `${_spent:g}`"
            )
            if min_rank:
                _rank_line += (
                    f"\n🔒 Minimum rank to sell: **{min_rank}** (needs `${_required:g}` spent)"
                    f"\n💰 Spend **`${_needed:g}`** more to reach **{min_rank}**"
                )
            _txt = f"{_txt}{_rank_line}"
            await _safe_edit(cq, _txt, InlineKeyboardMarkup(rows))
            await cq.answer()
            return

        if min_rank:
            try:
                user_rank = await get_user_rank(user_id)
            except Exception:
                user_rank = ""
            u_idx = rank_index(user_rank)
            m_idx = rank_index(min_rank)
            if m_idx > 0 and (u_idx < 0 or u_idx < m_idx):
                try:
                    _spent = await get_user_total_spend(user_id)
                except Exception:
                    _spent = 0.0
                try:
                    _required = await get_required_spend_for(min_rank)
                except Exception:
                    _required = 0.0
                _needed = max(0.0, _required - _spent)

                rows = []
                if support_btn:
                    rows.append([support_btn])
                rows.append([InlineKeyboardButton(_back_lbl, callback_data="back_home")])

                _tpl = _s.get(
                    "sell_rank_locked_msg",
                    "🔒 **Session selling is rank-locked.**\n\n"
                    "• Minimum rank: **{min_rank}** (needs `${required_spend:g}` spent)\n"
                    "• Your rank: **{current_rank}** — you've spent `${current_spend}`\n"
                    "• Spend **`${needed_spend:g}`** more to unlock\n\n"
                    "If you have any doubt or issue, contact support.",
                )
                try:
                    _txt = _tpl.format(
                        min_rank=min_rank,
                        current_rank=user_rank or "N/A",
                        current_spend=_spent,
                        required_spend=_required,
                        needed_spend=_needed,
                    )
                except Exception:
                    _txt = (
                        f"🔒 **Session selling is rank-locked.**\n\n"
                        f"Minimum rank required: **{min_rank}** (needs `${_required:g}` spent)\n"
                        f"Your rank: **{user_rank or 'N/A'}** — spent `${_spent:g}`\n"
                        f"Spend `${_needed:g}` more to unlock."
                    )

                await _safe_edit(cq, _txt, InlineKeyboardMarkup(rows))
                await cq.answer()
                return

        _SS_STATE[user_id] = {"step": "await_file", "session_bytes": None}

        await _safe_edit(
            cq,
            "![📦](tg://emoji?id=6131886699254388574) **Send your session file** as a document.\n\nAccepted: `.session` or `.zip`",
            InlineKeyboardMarkup([_cancel_btn()]),
        )
        await cq.answer()
    except Exception as exc:
        _log.error("sell_session_start_cb: %s", exc, exc_info=True)


@bot.on_callback_query(filters.regex("^sell_session_cancel$"))
async def sell_session_cancel_cb(client, cq: CallbackQuery):
    try:
        _SS_STATE.pop(cq.from_user.id, None)
        await _safe_edit(cq, "![❌](tg://emoji?id=5348567359764317130) Cancelled.", _back_btn())
        await cq.answer()
    except Exception as exc:
        _log.error("sell_session_cancel_cb: %s", exc, exc_info=True)


# ── Document handler ─────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.document)
async def sell_session_document_handler(client, message: Message):
    user_id = message.from_user.id
    state = _SS_STATE.get(user_id)
    if not state or state.get("step") != "await_file":
        await message.continue_propagation()
        return

    doc   = message.document
    fname = (doc.file_name or "").lower()
    is_zip     = fname.endswith(".zip")
    is_session = fname.endswith(".session")

    if not (is_zip or is_session):
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) Wrong file type. Send a `.session` or `.zip` file.",
            reply_markup=InlineKeyboardMarkup([_cancel_btn()]),
        )
        return

    max_size = _MAX_ZIP_SIZE if is_zip else _MAX_SES_SIZE
    if doc.file_size and doc.file_size > max_size:
        limit = "50 MB" if is_zip else "512 KB"
        await message.reply_text(
            f"![❌](tg://emoji?id=6129846551134084367) File too large. Max size: {limit}",
            reply_markup=InlineKeyboardMarkup([_cancel_btn()]),
        )
        return

    _SS_STATE.pop(user_id, None)
    progress = await message.reply_text("![🔍](tg://emoji?id=5316722951813346475) Validating… please wait.")

    try:
        buf = await client.download_media(message, in_memory=True)
        buf.seek(0)
        file_bytes = buf.read()
    except Exception as exc:
        _log.error("sell_session download user %s: %s", user_id, exc)
        await progress.edit_text(
            "![❌](tg://emoji?id=6129846551134084367) Could not download your file. Please try again.",
            reply_markup=InlineKeyboardMarkup([_cancel_btn()]),
        )
        return

    if is_zip:
        await _process_zip(client, message, progress, user_id, file_bytes)
    else:
        await _process_session(client, message, progress, user_id, file_bytes)


# ── Text handler: 2FA ──────────────────────────────────────────────────

@bot.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "help"]))
async def sell_session_text_handler(client, message: Message):
    user_id = message.from_user.id
    state = _SS_STATE.get(user_id)
    if not state or state.get("step") != "await_2fa":
        await message.continue_propagation()
        return

    password      = (message.text or "").strip()
    session_bytes = state.get("session_bytes")
    _SS_STATE.pop(user_id, None)

    if not session_bytes:
        await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Session data lost — please upload your file again.", reply_markup=_back_btn())
        return

    progress = await message.reply_text("![🔐](tg://emoji?id=6129550284290006595) Verifying 2FA… please wait.")
    await _run_session_pipeline(client, message, progress, user_id, session_bytes, current_2fa=password)


# ── Internals ────────────────────────────────────────────────────────────────

async def _process_zip(client, message, progress, user_id, zip_bytes):
    from server.stock.zip_extraction import extract_sessions_from_zip

    try:
        # Run in a thread pool so large ZIPs don't block the event loop
        sessions = await asyncio.to_thread(extract_sessions_from_zip, zip_bytes)
    except Exception as exc:
        _log.error("_process_zip user %s: %s", user_id, exc)
        await progress.edit_text("![❌](tg://emoji?id=6129846551134084367) Invalid ZIP. Make sure it contains valid `.session` files.", reply_markup=_back_btn())
        return

    if not sessions:
        await progress.edit_text("![❌](tg://emoji?id=6129846551134084367) No `.session` files found in the ZIP.", reply_markup=_back_btn())
        return

    if len(sessions) == 1:
        entry = sessions[0]
        # Pass the JSON-matched password (if any) so the pipeline doesn't
        # need to prompt for 2FA unnecessarily
        await _process_session(client, message, progress, user_id,
                               entry["session_bytes"], current_2fa=entry.get("password", ""))
        return

    # Multiple sessions — process all sequentially
    results = []
    for entry in sessions:
        label = entry.get("phone") or entry.get("filename", "?")
        r = await _run_session_pipeline_return(
            client, user_id, entry["session_bytes"],
            current_2fa=entry.get("password", ""),
        )
        results.append((label, r))

    ok    = sum(1 for _, r in results if r.get("success"))
    fail  = len(results) - ok
    total = sum(r.get("pending_amount", 0) for _, r in results if r.get("success"))

    lines = [f"![📦](tg://emoji?id=6131886699254388574) **ZIP Result** — ![✅](tg://emoji?id=6129492160497589882) {ok} accepted  ![❌](tg://emoji?id=6129846551134084367) {fail} rejected"]
    if total:
        lines.append(f"![💰](tg://emoji?id=6129731974291527294) Total pending: `${total:g}`")
    lines.append("")
    for label, r in results:
        icon = "![✅](tg://emoji?id=6129492160497589882)" if r.get("success") else "![❌](tg://emoji?id=6129846551134084367)"
        note = f"${r.get('pending_amount', 0):g}" if r.get("success") else r.get("reason", "Error")
        lines.append(f"{icon} `{label}` — {note}")

    await progress.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("![📋](tg://emoji?id=6129579803600231171) My Requests", callback_data="sell_my_requests")],
            [InlineKeyboardButton("![⬅️](tg://emoji?id=5258236805890710909) Back", callback_data="back_home")],
        ]),
    )


async def _process_session(client, message, progress, user_id, session_bytes, current_2fa=""):
    result = await _run_session_pipeline_return(client, user_id, session_bytes, current_2fa)
    if result.get("needs_2fa"):
        _SS_STATE[user_id] = {"step": "await_2fa", "session_bytes": session_bytes}
        await progress.edit_text(
            "![🔐](tg://emoji?id=6129550284290006595) This session has 2FA enabled.\n\nEnter your **current 2FA password**:",
            reply_markup=InlineKeyboardMarkup([_cancel_btn()]),
        )
        return
    await _render_result(client, progress, user_id, result)


async def _run_session_pipeline(client, message, progress, user_id, session_bytes, current_2fa=""):
    result = await _run_session_pipeline_return(client, user_id, session_bytes, current_2fa)
    if result.get("needs_2fa"):
        _SS_STATE[user_id] = {"step": "await_2fa", "session_bytes": session_bytes}
        await progress.edit_text(
            "![❌](tg://emoji?id=6129846551134084367) Wrong 2FA password. Please upload your file again.",
            reply_markup=_back_btn(),
        )
        return
    await _render_result(client, progress, user_id, result)


async def _render_result(client, progress, user_id, result):
    if result["success"]:
        phone          = result["phone"]
        pending_amount = result["pending_amount"]
        request_id     = result["request_id"]
        country_name   = result["country_name"]
        spam_status    = result.get("spam_status", "unknown")

        spam_line = "\n![⚠️](tg://emoji?id=6129939837823753679) Temporary restriction — price adjusted." if spam_status == "temporary_spam" else ""

        text = (
            f"![✅](tg://emoji?id=6129492160497589882) **Session Submitted**\n\n"
            f"![📱](tg://emoji?id=5330237710655306682) `{phone}`\n"
            f"![🌍](tg://emoji?id=6296303781126604562) {country_name}\n"
            f"![💰](tg://emoji?id=6129731974291527294) +`${pending_amount:g}` pending\n"
            f"![📋](tg://emoji?id=6129579803600231171) ![⏳](tg://emoji?id=6129574787078429498) Awaiting review"
            f"{spam_line}\n\n"
            f"ID: `{request_id}`"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("![📋](tg://emoji?id=6129579803600231171) My Requests", callback_data="sell_my_requests")],
            [InlineKeyboardButton("![⬅️](tg://emoji?id=5258236805890710909) Back", callback_data="back_home")],
        ])
        await progress.edit_text(text, reply_markup=buttons)

        # Admin log
        try:
            log_group = getattr(_cfg, "LOG_GROUP_ID", None)
            if log_group:
                from server.utils.database import get_user
                user_doc = await get_user(user_id)
                uname = f"@{user_doc['username']}" if user_doc and user_doc.get("username") else f"ID:{user_id}"
                await client.send_message(
                    log_group,
                    f"![📦](tg://emoji?id=6131886699254388574) **New Sell Session**\n\n"
                    f"![👤](tg://emoji?id=5316979275461573049) {uname} (`{user_id}`)\n"
                    f"![📱](tg://emoji?id=5330237710655306682) `{phone}` — {country_name}\n"
                    f"![💵](tg://emoji?id=6129731974291527294) `${pending_amount:g}` | Spam: `{spam_status}`\n"
                    f"🆔 `{request_id}`",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("![✅](tg://emoji?id=6129492160497589882) Approve", callback_data=f"sela_{request_id}"),
                        InlineKeyboardButton("![❌](tg://emoji?id=6129846551134084367) Reject",  callback_data=f"selr_{request_id}"),
                    ]]),
                )
        except Exception as exc:
            _log.warning("sell_session log group notify failed: %s", exc)

    else:
        reason  = result["reason"]
        detail  = result["detail"]
        fixable = result.get("fixable", False)
        btn_rows = []
        if fixable:
            btn_rows.append([InlineKeyboardButton("![🔄](tg://emoji?id=6129792056589031358) Try Again", callback_data="sell_session_start")])
        btn_rows.append([InlineKeyboardButton("![⬅️](tg://emoji?id=5258236805890710909) Back", callback_data="back_home")])
        await progress.edit_text(f"{reason}\n\n{detail}", reply_markup=InlineKeyboardMarkup(btn_rows))


async def _run_session_pipeline_return(client, user_id: int, session_bytes: bytes, current_2fa: str = "") -> dict:
    try:
        from server.services.sell_service import validate_and_submit_sell
        channel_id = getattr(_cfg, "SESSION_CHANNEL_ID", None)
        if not channel_id:
            return {"success": False, "reason": "![⚠️](tg://emoji?id=6129939837823753679) Platform Error", "detail": "Contact admin.", "fixable": False}
        return await validate_and_submit_sell(
            user_id=user_id,
            session_bytes=session_bytes,
            bot_client=client,
            channel_id=channel_id,
            api_id=_cfg.API_ID,
            api_hash=_cfg.API_HASH,
            current_2fa_password=current_2fa,
            sell_type="session",
        )
    except Exception as exc:
        _log.error("_run_session_pipeline_return user %s: %s", user_id, exc, exc_info=True)
        return {"success": False, "reason": "![⚠️](tg://emoji?id=6129939837823753679) Internal Error", "detail": "Unexpected error. Please try again.", "fixable": True}
