"""
Withdrawal Admin Plugin — Inline Telegram admin panel for withdrawal management.

Commands (owner + sudoers only):
  /witlist [pending|processing|completed|failed|all]
             — paginated list of withdrawals with inline action buttons
  /witapprove <id> [tx_hash]   — approve a manual withdrawal
  /witreject  <id> <reason>    — reject a withdrawal with reason

Inline callbacks:
  witadm_view_<id>    — view full withdrawal details
  witadm_approve_<id> — approve (manual gateway mode)
  witadm_reject_<id>  — start rejection flow (asks for reason text)
  witadm_refresh_<id> — force-poll gateway for latest status
  witadm_list_<page>  — paginate withdrawal list
"""

from __future__ import annotations

import asyncio
from typing import Optional

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from server.utils.bot_utils import Btn as InlineKeyboardButton

from server import bot, LOGGER
from server.utils.bot_utils import is_admin as _is_admin, DIV as _DIV
from server.utils.withdrawal_statuses import WithdrawalStatus

_log = LOGGER(__name__)

# In-memory state for admin rejection flows:  uid → {"step": "await_reason", "wit_id": str}
_ADMIN_STATE: dict[int, dict] = {}

# ── Status emoji map ──────────────────────────────────────────────────────────

_STATUS_EMOJI = {
    "pending":              "⏳",
    "processing":           "⚙️",
    "waiting_confirmation": "🔗",
    "completed":            "✅",
    "failed":               "❌",
    "cancelled":            "❌",
    "rejected":             "❌",
    "expired":              "⏰",
    "unknown":              "❓",
}

_STATUS_LABEL = {
    "pending":              "Pending",
    "processing":           "Processing",
    "waiting_confirmation": "Awaiting Confirmation",
    "completed":            "Completed",
    "failed":               "Failed",
    "cancelled":            "Cancelled",
    "rejected":             "Rejected",
    "expired":              "Expired",
    "unknown":              "Unknown",
}

# Predefined rejection reasons for quick selection
_REJECTION_REASONS = [
    "Invalid wallet address",
    "Incorrect network selected",
    "Suspicious activity detected",
    "KYC verification required",
    "Duplicate request",
    "Payment provider rejected",
    "Security review failed",
    "Insufficient verification",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_wit_summary(wit: dict) -> str:
    """One-line summary for list view."""
    status    = wit.get("status", "unknown")
    emoji     = _STATUS_EMOJI.get(status, "❓")
    wit_id    = wit.get("withdrawal_id", "?")
    amount    = wit.get("amount", 0.0)
    network   = wit.get("network", "?")
    user_id   = wit.get("user_id", "?")
    short_id  = wit_id[-12:] if len(wit_id) > 12 else wit_id
    return f"{emoji} `{short_id}` | ${amount:.2f} {network} | UID:{user_id} | {_STATUS_LABEL.get(status, status)}"


def _fmt_wit_detail(wit: dict) -> str:
    """Full details block for a single withdrawal."""
    status      = wit.get("status", "unknown")
    emoji       = _STATUS_EMOJI.get(status, "❓")
    wit_id      = wit.get("withdrawal_id", "?")
    user_id     = wit.get("user_id", "?")
    amount      = wit.get("amount", 0.0)
    fee         = wit.get("fee_amount", 0.0)
    net_amount  = wit.get("net_amount", amount)
    currency    = wit.get("currency", "USDT")
    network     = wit.get("network", "?")
    address     = wit.get("wallet_address", "?")
    gateway     = wit.get("gateway", "manual")
    track_id    = wit.get("gateway_track_id") or "—"
    tx_hash     = wit.get("gateway_tx_hash") or "—"
    reason      = wit.get("reason") or "—"
    created_at  = wit.get("created_at")
    updated_at  = wit.get("updated_at")
    completed_at = wit.get("completed_at")
    mode        = wit.get("mode", "manual")
    admin_note  = wit.get("admin_note") or "—"

    def _dt(dt) -> str:
        if dt is None:
            return "—"
        try:
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return str(dt)

    fee_line = f"\n💸 **Fee:** `${fee:.4f}` | **Net:** `${net_amount:.4f} {currency}`" if fee > 0 else f"\n💵 **Net:** `${net_amount:.4f} {currency}`"
    reason_line = f"\n📝 **Reason:** {reason}" if reason != "—" else ""
    note_line = f"\n📌 **Admin Note:** {admin_note}" if admin_note != "—" else ""
    tx_line = f"\n🔗 **TX Hash:** `{tx_hash}`" if tx_hash != "—" else ""
    track_line = f"\n🔢 **Track ID:** `{track_id}`" if track_id != "—" else ""

    return (
        f"{_DIV}\n"
        f"📤 **WITHDRAWAL DETAILS**\n"
        f"{_DIV}\n\n"
        f"🆔 **ID:** `{wit_id}`\n"
        f"👤 **User ID:** `{user_id}`\n"
        f"📊 **Status:** {emoji} {_STATUS_LABEL.get(status, status)}\n"
        f"🌐 **Network:** `{network}` ({mode.upper()})\n"
        f"📬 **Address:** `{address}`\n"
        f"💵 **Amount:** `${amount:.4f} USD`"
        f"{fee_line}\n"
        f"🏦 **Gateway:** `{gateway}`"
        f"{track_line}"
        f"{tx_line}"
        f"{reason_line}"
        f"{note_line}\n"
        f"{_DIV}\n"
        f"🕐 **Created:** {_dt(created_at)}\n"
        f"🔄 **Updated:** {_dt(updated_at)}\n"
        f"✅ **Completed:** {_dt(completed_at)}"
    )


def _wit_action_markup(wit: dict, page: int = 0) -> InlineKeyboardMarkup:
    """Inline buttons for a single withdrawal detail view."""
    wit_id  = wit.get("withdrawal_id", "")
    status  = wit.get("status", "unknown")
    rows    = []

    # Action buttons based on status
    if status in WithdrawalStatus.IN_FLIGHT:
        action_row = []
        action_row.append(InlineKeyboardButton(
            "✅ Approve", callback_data=f"witadm_approve_{wit_id}"
        ))
        action_row.append(InlineKeyboardButton(
            "❌ Reject", callback_data=f"witadm_reject_{wit_id}"
        ))
        rows.append(action_row)
        rows.append([InlineKeyboardButton(
            "🔄 Refresh Status", callback_data=f"witadm_refresh_{wit_id}"
        )])
    elif status in WithdrawalStatus.FINAL:
        rows.append([InlineKeyboardButton(
            "🔄 Refresh", callback_data=f"witadm_refresh_{wit_id}"
        )])

    rows.append([InlineKeyboardButton(
        f"⬅️ Back to List", callback_data=f"witadm_list_{page}"
    )])
    return InlineKeyboardMarkup(rows)


def _wit_list_markup(wits: list[dict], page: int, total: int, limit: int = 10) -> InlineKeyboardMarkup:
    """Inline buttons for the withdrawal list."""
    rows = []
    for w in wits:
        wit_id    = w.get("withdrawal_id", "")
        short_id  = wit_id[-10:] if len(wit_id) > 10 else wit_id
        status    = w.get("status", "unknown")
        emoji     = _STATUS_EMOJI.get(status, "❓")
        amount    = w.get("amount", 0.0)
        network   = w.get("network", "?")
        rows.append([InlineKeyboardButton(
            f"{emoji} {short_id} · ${amount:.2f} {network}",
            callback_data=f"witadm_view_{wit_id}_{page}"
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"witadm_list_{page - 1}"))
    total_pages = max(1, (total + limit - 1) // limit)
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="witadm_noop"))
    if (page + 1) * limit < total:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"witadm_list_{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"witadm_list_{page}")])
    return InlineKeyboardMarkup(rows)


async def _send_withdrawal_list(
    target,               # Message or CallbackQuery
    page: int = 0,
    status_filter: Optional[str] = None,
    as_edit: bool = False,
):
    """Fetch and display a paginated withdrawal list."""
    limit = 10
    from server.utils.database.withdrawaldb import get_all_withdrawals
    total, wits = await get_all_withdrawals(
        status=status_filter,
        page=page + 1,
        limit=limit,
    )

    if not wits:
        text = (
            f"{_DIV}\n"
            f"📤 **WITHDRAWAL PANEL**\n"
            f"{_DIV}\n\n"
            f"📭 No withdrawals found"
            + (f" with status `{status_filter}`" if status_filter else "")
            + ".\n\n"
            f"Total: `0`"
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="witadm_list_0")]])
    else:
        filter_label = f" [{status_filter.upper()}]" if status_filter else " [ALL]"
        text = (
            f"{_DIV}\n"
            f"📤 **WITHDRAWAL PANEL{filter_label}**\n"
            f"{_DIV}\n\n"
            f"📊 **Total:** `{total}` | Page `{page + 1}`\n\n"
        )
        for w in wits:
            text += _fmt_wit_summary(w) + "\n"
        text += f"\n_Tap a row to manage it._"
        markup = _wit_list_markup(wits, page, total, limit)

    if as_edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except Exception:
            await target.message.reply_text(text, reply_markup=markup)
        await target.answer()
    else:
        msg_target = target.message if isinstance(target, CallbackQuery) else target
        await msg_target.reply_text(text, reply_markup=markup)


# ── /witlist ──────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("witlist") & filters.private)
async def witlist_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    status_filter = args[0].lower() if args else None
    valid_statuses = list(WithdrawalStatus.IN_FLIGHT | WithdrawalStatus.FINAL) + ["all"]
    if status_filter == "all":
        status_filter = None
    elif status_filter and status_filter not in valid_statuses:
        await message.reply_text(
            f"❌ Invalid status. Valid options: `pending`, `processing`, `waiting_confirmation`, "
            f"`completed`, `failed`, `rejected`, `cancelled`, `expired`, `all`"
        )
        return

    await _send_withdrawal_list(message, page=0, status_filter=status_filter)


# ── Inline: paginate list ─────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^witadm_list_\d+$"))
async def witadm_list_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return
    page = int(cq.data.split("_")[-1])
    await _send_withdrawal_list(cq, page=page, as_edit=True)


@bot.on_callback_query(filters.regex(r"^witadm_noop$"))
async def witadm_noop_cb(client, cq: CallbackQuery):
    await cq.answer()


# ── Inline: view details ──────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^witadm_view_WIT-[A-F0-9]+_\d+$"))
async def witadm_view_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return

    parts   = cq.data.split("_")
    # format: witadm_view_WIT-XXXXXXXX_<page>
    # parts: ["witadm", "view", "WIT-XXXXXXXX", "<page>"]
    wit_id  = parts[2]
    page    = int(parts[3]) if len(parts) > 3 else 0

    from server.utils.database.withdrawaldb import get_withdrawal_record
    wit = await get_withdrawal_record(wit_id)
    if not wit:
        await cq.answer("❌ Withdrawal not found.", show_alert=True)
        return

    text   = _fmt_wit_detail(wit)
    markup = _wit_action_markup(wit, page=page)
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cq.message.reply_text(text, reply_markup=markup)
    await cq.answer()


# ── Inline: approve ───────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^witadm_approve_WIT-[A-F0-9]+$"))
async def witadm_approve_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return

    wit_id = cq.data[len("witadm_approve_"):]
    await cq.answer("⏳ Processing approval…")

    try:
        from server.services.withdrawal import get_withdrawal_service
        svc    = get_withdrawal_service()
        result = await svc.admin_approve(
            withdrawal_id=wit_id,
            admin_note=f"Approved by admin {cq.from_user.id}",
        )
    except Exception as exc:
        _log.error("witadm_approve_cb error for %s: %s", wit_id, exc, exc_info=True)
        await cq.answer("❌ Error approving withdrawal. Check logs.", show_alert=True)
        return

    if not result["ok"]:
        await cq.answer(f"❌ {result.get('error', 'Unknown error')}", show_alert=True)
        return

    # Refresh the view
    from server.utils.database.withdrawaldb import get_withdrawal_record
    wit  = await get_withdrawal_record(wit_id)
    text = f"✅ **Approved successfully!**\n\n{_fmt_wit_detail(wit)}" if wit else f"✅ Withdrawal `{wit_id}` approved."
    markup = _wit_action_markup(wit, page=0) if wit else InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="witadm_list_0")
    ]])
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


# ── Inline: reject (start flow) ───────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^witadm_reject_WIT-[A-F0-9]+$"))
async def witadm_reject_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return

    wit_id = cq.data[len("witadm_reject_"):]
    uid    = cq.from_user.id

    # Build quick-reason markup
    reason_buttons = []
    for i, reason in enumerate(_REJECTION_REASONS):
        reason_buttons.append([InlineKeyboardButton(
            f"📝 {reason}", callback_data=f"witadm_reason_{wit_id}_{i}"
        )])
    reason_buttons.append([InlineKeyboardButton(
        "✏️ Custom Reason", callback_data=f"witadm_reason_custom_{wit_id}"
    )])
    reason_buttons.append([InlineKeyboardButton(
        "❌ Cancel", callback_data=f"witadm_reject_cancel_{wit_id}"
    )])

    try:
        await cq.message.edit_text(
            f"{_DIV}\n"
            f"❌ **REJECT WITHDRAWAL**\n"
            f"{_DIV}\n\n"
            f"🆔 `{wit_id}`\n\n"
            f"**Select rejection reason:**",
            reply_markup=InlineKeyboardMarkup(reason_buttons),
        )
    except Exception:
        pass
    await cq.answer()


@bot.on_callback_query(filters.regex(r"^witadm_reject_cancel_WIT-[A-F0-9]+$"))
async def witadm_reject_cancel_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return
    wit_id = cq.data[len("witadm_reject_cancel_"):]
    _ADMIN_STATE.pop(cq.from_user.id, None)

    from server.utils.database.withdrawaldb import get_withdrawal_record
    wit = await get_withdrawal_record(wit_id)
    if wit:
        try:
            await cq.message.edit_text(_fmt_wit_detail(wit), reply_markup=_wit_action_markup(wit))
        except Exception:
            pass
    await cq.answer("Cancelled.")


@bot.on_callback_query(filters.regex(r"^witadm_reason_WIT-[A-F0-9]+_\d+$"))
async def witadm_reason_preset_cb(client, cq: CallbackQuery):
    """Admin selected a preset rejection reason."""
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return

    # format: witadm_reason_WIT-XXXX_N
    parts  = cq.data.split("_")
    wit_id = parts[2]
    idx    = int(parts[3])
    reason = _REJECTION_REASONS[idx] if 0 <= idx < len(_REJECTION_REASONS) else "Rejected by admin"

    await _do_reject(cq, wit_id, reason)


@bot.on_callback_query(filters.regex(r"^witadm_reason_custom_WIT-[A-F0-9]+$"))
async def witadm_reason_custom_cb(client, cq: CallbackQuery):
    """Admin wants to type a custom rejection reason."""
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return

    wit_id = cq.data[len("witadm_reason_custom_"):]
    uid    = cq.from_user.id

    _ADMIN_STATE[uid] = {"step": "await_reject_reason", "wit_id": wit_id, "msg_id": cq.message.id, "chat_id": cq.message.chat.id}

    try:
        await cq.message.edit_text(
            f"{_DIV}\n"
            f"❌ **REJECT WITHDRAWAL**\n"
            f"{_DIV}\n\n"
            f"🆔 `{wit_id}`\n\n"
            f"Type your **rejection reason** and send it.\n"
            f"_(Or send /cancel to abort)_",
        )
    except Exception:
        pass
    await cq.answer("Type the rejection reason.")


# ── Inline: refresh status ────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^witadm_refresh_WIT-[A-F0-9]+$"))
async def witadm_refresh_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("❌ Admin only.", show_alert=True)
        return

    wit_id = cq.data[len("witadm_refresh_"):]
    await cq.answer("🔄 Refreshing…")

    try:
        from server.services.withdrawal import get_withdrawal_service
        svc    = get_withdrawal_service()
        result = await svc.verify_withdrawal(wit_id)
        status = result.get("status", "unknown")
        emoji  = _STATUS_EMOJI.get(status, "❓")
        label  = _STATUS_LABEL.get(status, status)
    except Exception as exc:
        _log.error("witadm_refresh_cb error for %s: %s", wit_id, exc)
        await cq.answer("❌ Refresh failed. See logs.", show_alert=True)
        return

    from server.utils.database.withdrawaldb import get_withdrawal_record
    wit = await get_withdrawal_record(wit_id)
    if wit:
        try:
            await cq.message.edit_text(
                f"🔄 **Refreshed** — {emoji} {label}\n\n{_fmt_wit_detail(wit)}",
                reply_markup=_wit_action_markup(wit),
            )
        except Exception:
            pass


# ── /witapprove command ───────────────────────────────────────────────────────

@bot.on_message(filters.command("witapprove") & filters.private)
async def witapprove_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if not args:
        await message.reply_text("Usage: `/witapprove <withdrawal_id> [tx_hash]`")
        return

    wit_id  = args[0].upper()
    tx_hash = args[1] if len(args) > 1 else ""

    msg = await message.reply_text(f"⏳ Approving `{wit_id}`…")
    try:
        from server.services.withdrawal import get_withdrawal_service
        svc    = get_withdrawal_service()
        result = await svc.admin_approve(
            withdrawal_id=wit_id,
            tx_hash=tx_hash,
            admin_note=f"Approved by admin {message.from_user.id} via command",
        )
    except Exception as exc:
        _log.error("witapprove_cmd error: %s", exc, exc_info=True)
        await msg.edit_text(f"❌ Error: `{exc}`")
        return

    if result["ok"]:
        await msg.edit_text(f"✅ **Approved!** `{wit_id}` → status: `{result.get('status')}`")
    else:
        await msg.edit_text(f"❌ Failed: {result.get('error')}")


# ── /witreject command ────────────────────────────────────────────────────────

@bot.on_message(filters.command("witreject") & filters.private)
async def witreject_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text("Usage: `/witreject <withdrawal_id> <reason>`")
        return

    wit_id = args[0].upper()
    reason = " ".join(args[1:])

    msg = await message.reply_text(f"⏳ Rejecting `{wit_id}`…")
    try:
        from server.services.withdrawal import get_withdrawal_service
        svc    = get_withdrawal_service()
        result = await svc.admin_reject(withdrawal_id=wit_id, reason=reason)
    except Exception as exc:
        _log.error("witreject_cmd error: %s", exc, exc_info=True)
        await msg.edit_text(f"❌ Error: `{exc}`")
        return

    if result["ok"]:
        await msg.edit_text(f"✅ **Rejected!** `{wit_id}` — reason: _{reason}_")
    else:
        await msg.edit_text(f"❌ Failed: {result.get('error')}")


# ── Text handler for admin rejection reason ───────────────────────────────────

@bot.on_message(filters.private & filters.text & ~filters.command([
    "start", "cancel", "witlist", "witapprove", "witreject",
    "deposit", "withdraw", "setwallet",
]))
async def witadm_text_handler(client, message: Message):
    uid   = message.from_user.id
    state = _ADMIN_STATE.get(uid)
    if not state or state.get("step") != "await_reject_reason":
        await message.continue_propagation()
        return

    if not await _is_admin(uid):
        _ADMIN_STATE.pop(uid, None)
        await message.continue_propagation()
        return

    wit_id = state["wit_id"]
    reason = message.text.strip()

    if len(reason) < 3:
        await message.reply_text("❌ Reason is too short. Please provide a meaningful rejection reason.")
        return

    _ADMIN_STATE.pop(uid, None)

    try:
        await message.delete()
    except Exception:
        pass

    msg = await message.reply_text(f"⏳ Rejecting `{wit_id}` with reason: _{reason}_…")

    await _do_reject_from_message(msg, wit_id, reason)


# ── Internal rejection helpers ────────────────────────────────────────────────

async def _do_reject(cq: CallbackQuery, wit_id: str, reason: str) -> None:
    """Perform rejection and update the callback message."""
    await cq.answer("⏳ Processing rejection…")
    try:
        from server.services.withdrawal import get_withdrawal_service
        svc    = get_withdrawal_service()
        result = await svc.admin_reject(withdrawal_id=wit_id, reason=reason)
    except Exception as exc:
        _log.error("_do_reject error for %s: %s", wit_id, exc, exc_info=True)
        await cq.answer("❌ Rejection failed. See logs.", show_alert=True)
        return

    if not result["ok"]:
        await cq.answer(f"❌ {result.get('error', 'Unknown error')}", show_alert=True)
        return

    from server.utils.database.withdrawaldb import get_withdrawal_record
    wit  = await get_withdrawal_record(wit_id)
    text = (
        f"✅ **Rejected successfully!**\n"
        f"📝 **Reason:** {reason}\n\n"
        + (_fmt_wit_detail(wit) if wit else f"🆔 `{wit_id}`")
    )
    markup = _wit_action_markup(wit, page=0) if wit else InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="witadm_list_0")
    ]])
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


async def _do_reject_from_message(msg, wit_id: str, reason: str) -> None:
    """Perform rejection from a message context."""
    try:
        from server.services.withdrawal import get_withdrawal_service
        svc    = get_withdrawal_service()
        result = await svc.admin_reject(withdrawal_id=wit_id, reason=reason)
    except Exception as exc:
        _log.error("_do_reject_from_message error for %s: %s", wit_id, exc, exc_info=True)
        await msg.edit_text(f"❌ Error: `{exc}`")
        return

    if result["ok"]:
        await msg.edit_text(
            f"✅ **Rejected!** `{wit_id}`\n📝 **Reason:** _{reason}_",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 View List", callback_data="witadm_list_0")
            ]]),
        )
    else:
        await msg.edit_text(f"❌ Failed: {result.get('error')}")
