

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
    '❓': "6129472184604695207",
    '🌍': "6296303781126604562",
    '👤': "5316979275461573049",
    '💰': "6129731974291527294",
    '💵': "6129731974291527294",
    '📋': "6129579803600231171",
    '📝': "6129579803600231171",
    '📱': "5330237710655306682",
    '🚫': "6129846551134084367",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Sell Admin Plugin — Manual override for sell requests.

Every sell request submitted through the bot's live phone+OTP+2FA flow is
now handled AUTOMATICALLY: pending_balance is credited instantly when the
account passes a clean spam check + single-session guarantee, and
server.services.sell_recheck promotes it to withdrawable balance 24 hours
later if a live recheck still finds it clean. These commands are a manual
override for admins — to approve early, or to reject a request the
automatic recheck hasn't reached yet.

Commands (owner + sudoers only):
  /sell_list [pending|all]     — list sell requests (default: pending)
  /sell_approve <id> [price]   — approve now, ahead of the 24h auto recheck
  /sell_reject <id> <reason>   — reject a sell request early

Inline callbacks (usable in LOG_GROUP_ID):
  sela_<request_id>            — quick-approve at offer price
  selr_<request_id>            — inform admin to use /sell_reject command
"""

from pyrogram import filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from server.utils.bot_utils import Btn as InlineKeyboardButton

from server import bot, LOGGER
from server.utils.bot_utils import is_admin as _is_admin, DIV as _DIV

_log = LOGGER(__name__)


# ── /sell_list ────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("sell_list"))
async def sell_list_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    from server.utils.database import get_pending_sell_requests, get_all_sell_requests

    args = message.command[1:]
    mode = args[0].lower() if args else "pending"

    if mode == "all":
        reqs = await get_all_sell_requests(limit=50)
        title = "ALL SELL REQUESTS"
    else:
        reqs = await get_pending_sell_requests(limit=50)
        title = "PENDING SELL REQUESTS"

    if not reqs:
        await message.reply_text(f"![📋](tg://emoji?id=6129579803600231171) No {mode} sell requests.")
        return

    # Send each request as a separate message with approve/reject inline buttons
    header = f"![📋](tg://emoji?id=6129579803600231171) **{title} ({len(reqs)})**"
    await message.reply_text(header)

    for r in reqs:
        rid = r["request_id"]
        status = r.get("status", "?")
        lifecycle = r.get("lifecycle_status", "pending")
        spam = r.get("spam_status", "")
        spam_tag = f"\n![⚠️](tg://emoji?id=6129939837823753679) Spam: `{spam}`" if spam and spam not in ("clean", "unknown") else ""
        lc_tag = f"\n⏳ Lifecycle: `{lifecycle}`" if lifecycle != "pending" else ""
        pending_amt = r.get("pending_amount") or r.get("offer_price", 0)

        text = (
            f"![📋](tg://emoji?id=6129579803600231171) **Sell Request**\n\n"
            f"🆔 `{rid}`\n"
            f"![📱](tg://emoji?id=5330237710655306682) Phone: `{r.get('phone', '?')}`\n"
            f"![🌍](tg://emoji?id=6296303781126604562) Country: {r.get('country_name', r.get('code', '?'))}\n"
            f"![💵](tg://emoji?id=6129731974291527294) Price: `${r.get('offer_price', 0):g}` (pending: `${pending_amt:g}`)\n"
            f"📊 Status: `{status}`{lc_tag}{spam_tag}"
        )

        if status == "pending":
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"sela_{rid}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"selr_{rid}"),
                ]
            ])
        else:
            markup = None

        await message.reply_text(text, reply_markup=markup)


# ── /sell_approve ─────────────────────────────────────────────────────────────

@bot.on_message(filters.command("sell_approve"))
async def sell_approve_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if not args:
        await message.reply_text(
            "Usage: `/sell_approve <request_id> [price] [force]`\n\n"
            "Add `force` at the end to bypass session verification."
        )
        return

    # Strip optional "force" keyword anywhere in args
    force = any(a.lower() == "force" for a in args)
    args  = [a for a in args if a.lower() != "force"]

    request_id = args[0].upper()
    custom_price: float | None = None
    if len(args) >= 2:
        try:
            custom_price = float(args[1])
            if custom_price <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Invalid price. Must be a positive number.")
            return

    await _do_approve(client, message, request_id, custom_price, force=force)


async def _do_approve(
    client, origin, request_id: str,
    custom_price: float | None = None,
    force: bool = False,
):
    """
    Shared approval logic — called from the command, inline callback, and
    force-approve callback.  `origin` is either a Message or a CallbackQuery.

    When force=False (default), runs live Telethon session verification first.
    If verification hard-fails, blocks and sends a Force Approve button.
    When force=True, bypasses verification and approves immediately.
    """
    from server.utils.database import get_sell_request
    from server.services.sell_ops import finalize_sell_approval

    async def _reply(text, reply_markup=None):
        try:
            if isinstance(origin, CallbackQuery):
                return await origin.message.reply_text(text, reply_markup=reply_markup)
            return await origin.reply_text(text, reply_markup=reply_markup)
        except Exception as _e:
            _log.warning("_do_approve _reply failed: %s", _e)

    req = await get_sell_request(request_id)
    if not req:
        await _reply(f"![❌](tg://emoji?id=6129846551134084367) Sell request `{request_id}` not found.")
        return
    if req["status"] != "pending":
        await _reply(
            f"![⚠️](tg://emoji?id=6129939837823753679) Request `{request_id}` is already `{req['status']}`."
        )
        return

    final_price = custom_price if custom_price is not None else req.get("offer_price", 0)
    user_id     = req["user_id"]
    phone       = req.get("phone", "?")
    lifecycle   = req.get("lifecycle_status", "pending")
    spam_status = req.get("spam_status", "unknown")
    has_session = bool(req.get("session_msg_id"))

    # ── Session verification (unless admin force-approved) ────────────────────
    verif_line = ""
    if not force:
        await _reply(
            f"![⏳](tg://emoji?id=6129574787078429498) **Verifying session for `{request_id}`...**\n\n"
            f"![📱](tg://emoji?id=5330237710655306682) `{phone}` | Lifecycle: `{lifecycle}` | Spam: `{spam_status}`\n\n"
            f"Connecting to Telegram to check session integrity (~30s). Please wait."
        )
        try:
            from server.services.sell_verifier import verify_sell_session, build_admin_warning_text
            verification = await verify_sell_session(req)

            if verification.verified:
                other_s = verification.other_sessions_count
                verif_line = (
                    f"✅ Verification: Passed | Spam: `{verification.spam_status}` | "
                    f"Other sessions: `{other_s}`"
                )
                if verification.warnings:
                    verif_line += (
                        "\n![⚠️](tg://emoji?id=6129939837823753679) Warnings: "
                        + "; ".join(verification.warnings[:2])
                    )

            elif verification.inconclusive:
                verif_line = (
                    f"🌐 Verification: Inconclusive (network timeout) — proceeding with approval\n"
                    f"Spam (last known): `{spam_status}`"
                )

            else:
                # Hard fail — block and offer Force Approve
                fail_detail = build_admin_warning_text(verification)
                await _reply(
                    f"![❌](tg://emoji?id=6129846551134084367) "
                    f"**Session Validation FAILED for `{request_id}`**\n\n"
                    f"{fail_detail}\n\n"
                    f"![⚠️](tg://emoji?id=6129939837823753679) Seller will **NOT** be paid unless you force-approve.\n\n"
                    f"To force-approve: `/sell_approve {request_id} {final_price} force`\n"
                    f"Or tap the button below:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "![⚠️](tg://emoji?id=6129939837823753679) Force Approve",
                            callback_data=f"selaf_{request_id}",
                        ),
                        InlineKeyboardButton("✕ Cancel", callback_data=f"selc_{request_id}"),
                    ]]),
                )
                return

        except Exception as exc:
            _log.warning("_do_approve: verifier error for %s: %s", request_id, exc)
            verif_line = (
                f"![⚠️](tg://emoji?id=6129939837823753679) Verification error: `{exc}` — approving anyway"
            )
    else:
        verif_line = (
            "![⚠️](tg://emoji?id=6129939837823753679) FORCE APPROVED — session validation bypassed"
        )

    # ── Approve ───────────────────────────────────────────────────────────────
    note = (
        "Force-approved by admin (validation bypassed)"
        if force else
        "Manually approved by admin (session verification passed)"
    )
    doc = await finalize_sell_approval(request_id, final_price, note=note)
    if not doc:
        await _reply(
            f"![❌](tg://emoji?id=6129846551134084367) Could not approve `{request_id}` — "
            f"may already be processed."
        )
        return

    lifecycle_after = doc.get("lifecycle_status", "pending")
    terminated      = doc.get("terminated_others", False)
    term_str = (
        "✅ Done"
        if terminated else
        ("![⏳](tg://emoji?id=6129574787078429498) Pending (worker retries every 5 min)"
         if lifecycle_after == "pending_termination"
         else "—")
    )
    session_str = (
        "✅ Stored in channel"
        if has_session else
        "![❌](tg://emoji?id=6129846551134084367) Missing — no session file"
    )

    conf_text = (
        f"![✅](tg://emoji?id=6129492160497589882) **Sell request `{request_id}` approved.**\n\n"
        f"![👤](tg://emoji?id=5316979275461573049) User: `{user_id}`\n"
        f"![📱](tg://emoji?id=5330237710655306682) Phone: `{phone}`\n"
        f"![💵](tg://emoji?id=6129731974291527294) Paid: `${final_price:g}`\n"
        f"🔄 Lifecycle: `{lifecycle_after}`\n"
        f"📦 Session: {session_str}\n"
        f"🔚 Termination: {term_str}\n"
        f"{verif_line}"
    )
    try:
        if isinstance(origin, CallbackQuery):
            await origin.message.reply_text(conf_text)
            await origin.answer("![✅](tg://emoji?id=6129492160497589882) Approved!")
        else:
            await origin.reply_text(conf_text)
    except Exception as exc:
        _log.warning("sell_approve: admin notification failed: %s", exc)
    # Seller notification is handled by finalize_sell_approval → notify() — no duplicate here.


# ── /sell_reject ──────────────────────────────────────────────────────────────

@bot.on_message(filters.command("sell_reject"))
async def sell_reject_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text("Usage: `/sell_reject <request_id> <reason>`")
        return

    request_id = args[0].upper()
    reason = " ".join(args[1:])

    await _do_reject(client, message, request_id, reason)


async def _do_reject(client, origin, request_id: str, reason: str):
    """Shared rejection logic."""
    from server.utils.database import get_sell_request
    from server.services.sell_ops import finalize_sell_rejection

    req = await get_sell_request(request_id)
    if not req:
        _reply = getattr(origin, "reply_text", None) or origin.message.reply_text
        await _reply(f"![❌](tg://emoji?id=6129846551134084367) Sell request `{request_id}` not found.")
        return
    if req["status"] != "pending":
        _reply = getattr(origin, "reply_text", None) or origin.message.reply_text
        await _reply(f"![⚠️](tg://emoji?id=6129939837823753679) Request `{request_id}` is already `{req['status']}`.")
        return

    user_id = req["user_id"]

    # Atomically reject + reverse the pending balance held for this request
    doc = await finalize_sell_rejection(request_id, note=reason)
    if not doc:
        _reply = getattr(origin, "reply_text", None) or origin.message.reply_text
        await _reply(f"![❌](tg://emoji?id=6129846551134084367) Could not reject `{request_id}` — may already be processed.")
        return

    # Confirm to admin
    conf_text = (
        f"![❌](tg://emoji?id=6129846551134084367) **Sell request `{request_id}` rejected.**\n\n"
        f"![📱](tg://emoji?id=5330237710655306682) Phone: `{req.get('phone', '?')}`\n"
        f"![📝](tg://emoji?id=6129579803600231171) Reason: {reason}"
    )
    try:
        if isinstance(origin, CallbackQuery):
            await origin.message.reply_text(conf_text)
            await origin.answer("![❌](tg://emoji?id=6129846551134084367) Rejected.")
        else:
            await origin.reply_text(conf_text)
    except Exception as exc:
        _log.warning("sell_reject: admin notification failed: %s", exc)

    # Seller notification is handled by finalize_sell_rejection → notify() — no duplicate DM here.


# ── Inline callbacks (from LOG_GROUP_ID notification) ────────────────────────

@bot.on_callback_query(filters.regex(r"^sela_([A-Z0-9\-]+)$"))
async def sell_approve_inline_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("![🚫](tg://emoji?id=6129846551134084367) Admins only.", show_alert=True)
        return
    request_id = cq.matches[0].group(1)
    await cq.answer("![⏳](tg://emoji?id=6129574787078429498) Verifying session — please wait...")
    await _do_approve(client, cq, request_id, custom_price=None, force=False)


@bot.on_callback_query(filters.regex(r"^selaf_([A-Z0-9\-]+)$"))
async def sell_force_approve_inline_cb(client, cq: CallbackQuery):
    """Force approve — bypasses session verification. Used after validation failure."""
    if not await _is_admin(cq.from_user.id):
        await cq.answer("![🚫](tg://emoji?id=6129846551134084367) Admins only.", show_alert=True)
        return
    request_id = cq.matches[0].group(1)
    await cq.answer("![⚠️](tg://emoji?id=6129939837823753679) Force approving...")
    await _do_approve(client, cq, request_id, custom_price=None, force=True)


@bot.on_callback_query(filters.regex(r"^selc_([A-Z0-9\-]+)$"))
async def sell_cancel_approve_cb(client, cq: CallbackQuery):
    """Cancel an in-progress approval flow."""
    await cq.answer("Cancelled.")
    try:
        await cq.message.reply_text(
            f"❌ Approval cancelled for `{cq.matches[0].group(1)}`."
        )
    except Exception:
        pass


@bot.on_callback_query(filters.regex(r"^selr_([A-Z0-9\-]+)$"))
async def sell_reject_inline_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("![🚫](tg://emoji?id=6129846551134084367) Admins only.", show_alert=True)
        return
    request_id = cq.matches[0].group(1)
    await cq.answer(
        f"To reject with a reason, reply:\n/sell_reject {request_id} <reason>",
        show_alert=True,
    )


# ── /sell_settings ─────────────────────────────────────────────────────────────

@bot.on_message(filters.command("sell_settings"))
async def sell_settings_cmd(client, message: Message):
    """
    View or change sell timing settings.

    Usage:
      /sell_settings                              — show current settings
      /sell_settings delay <minutes>              — set termination delay (default 1)
      /sell_settings hold <hours>                 — set payment hold period (default 48)
    """
    if not await _is_admin(message.from_user.id):
        return

    from server.utils.database.configdb import get_setting, set_setting

    args = message.command[1:]

    # ── Write mode ────────────────────────────────────────────────────────────
    if len(args) >= 2:
        sub = args[0].lower()
        try:
            val = float(args[1])
            if val < 0:
                raise ValueError
        except ValueError:
            await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Value must be a positive number.")
            return

        if sub in ("delay", "termination_delay", "td"):
            val = int(val)
            await set_setting("termination_delay_minutes", val)
            await message.reply_text(
                f"![✅](tg://emoji?id=6129492160497589882) **Termination delay** set to `{val}` minute(s).\n\n"
                f"New sell requests will wait `{val}` min before bot tries to terminate other sessions."
            )
        elif sub in ("hold", "payment_hold", "ph"):
            val = int(val)
            await set_setting("payment_hold_hours", val)
            await message.reply_text(
                f"![✅](tg://emoji?id=6129492160497589882) **Payment hold** set to `{val}` hour(s).\n\n"
                f"After session termination, balance will release after `{val}h`."
            )
        else:
            await message.reply_text(
                "![❓](tg://emoji?id=6129472184604695207) Unknown setting.\n\n"
                "Use:\n"
                "• `/sell_settings delay <minutes>` — termination delay\n"
                "• `/sell_settings hold <hours>` — payment hold period"
            )
        return

    # ── Read mode ─────────────────────────────────────────────────────────────
    delay_min  = await get_setting("termination_delay_minutes") or 1
    hold_hours = await get_setting("payment_hold_hours") or 48

    text = (
        f"![📋](tg://emoji?id=6129579803600231171) **Sell Timing Settings**\n\n"
        f"⏱ **Termination Delay:** `{delay_min}` min\n"
        f"_(Time bot waits before terminating other sessions — gives seller time to logout manually)_\n\n"
        f"💰 **Payment Hold:** `{hold_hours}` hours\n"
        f"_(Time balance is held after termination before releasing to seller)_\n\n"
        f"**To change:**\n"
        f"`/sell_settings delay <minutes>` — e.g. `/sell_settings delay 5`\n"
        f"`/sell_settings hold <hours>` — e.g. `/sell_settings hold 24`"
    )
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ Set Delay", callback_data="sell_set_delay"),
            InlineKeyboardButton("💰 Set Hold", callback_data="sell_set_hold"),
        ]
    ])
    await message.reply_text(text, reply_markup=markup)


@bot.on_callback_query(filters.regex(r"^sell_set_(delay|hold)$"))
async def sell_settings_btn_cb(client, cq: CallbackQuery):
    if not await _is_admin(cq.from_user.id):
        await cq.answer("![🚫](tg://emoji?id=6129846551134084367) Admins only.", show_alert=True)
        return
    kind = cq.matches[0].group(1)
    if kind == "delay":
        await cq.answer(
            "Reply with:\n/sell_settings delay <minutes>\nExample: /sell_settings delay 2",
            show_alert=True,
        )
    else:
        await cq.answer(
            "Reply with:\n/sell_settings hold <hours>\nExample: /sell_settings hold 24",
            show_alert=True,
        )
