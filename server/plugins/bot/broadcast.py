"""
Admin Broadcast Plugin

Commands:
  /broadcast   — Start a broadcast to all users (owner + sudoers only)
  /bcancel     — Cancel an in-progress broadcast setup

Flow A — reply to an existing message:
  Reply to any message with /broadcast → skip straight to confirmation

Flow B — send a new message:
  1. Admin sends /broadcast (no reply)
  2. Bot asks admin to send the message to broadcast
  3. Admin sends ANY message type (text, photo, video, audio, document,
     sticker, GIF, voice, animation — with any caption, formatting,
     and inline buttons)
  4. Bot echoes a preview and shows user count + confirmation buttons
  5. Admin confirms → broadcast runs with live progress updates
  6. Final summary: sent / blocked / failed / total

Notes:
  - Uses Message.copy() so the message is delivered identical to the original
    (no forward tag, preserves caption entities and inline reply_markup)
  - Streams user IDs from MongoDB in batches — does not load all users at once
  - Handles FloodWait automatically, skips blocked/deactivated accounts
  - Only one broadcast can run per admin at a time
"""

from __future__ import annotations

import asyncio

from pyrogram import filters
from pyrogram.errors import (
    FloodWait,
    InputUserDeactivated,
    PeerIdInvalid,
    UserDeactivatedBan,
    UserIsBlocked,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from server import bot, LOGGER
from server.utils.bot_utils import is_admin
from server.utils.database import get_total_users
from server.utils.database.userdb import usersdb

_log = LOGGER(__name__)

# Per-admin state: admin_id → {"step": "await_msg"|"confirm"|"running", "msg": Message|None}
_state: dict[int, dict] = {}

_PROGRESS_INTERVAL = 50    # edit progress message every N users
_SEND_DELAY        = 0.05  # 20 msgs/s — safely under Telegram's 30/s limit


# ─── /broadcast ──────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("broadcast"))
async def broadcast_cmd(client, message: Message):
    if not await is_admin(message.from_user.id):
        return

    uid = message.from_user.id

    if _state.get(uid, {}).get("step") == "running":
        await message.reply_text(
            "⚠️ **Broadcast already running.**\n\n"
            "Wait for the current broadcast to finish before starting a new one."
        )
        return

    # ── Flow A: /broadcast sent as a reply → use that message directly ────────
    if message.reply_to_message:
        source_msg = message.reply_to_message
        await _show_confirmation(message, source_msg, uid)
        return

    # ── Flow B: no reply → ask admin to send the message ─────────────────────
    _state[uid] = {"step": "await_msg", "msg": None}

    await message.reply_text(
        "📢 **Broadcast — Step 1 of 2**\n\n"
        "Send me the message you want to broadcast to **all users**.\n\n"
        "Supports **everything**:\n"
        "• Text with formatting\n"
        "• Photo / Video / Audio / Document / Voice / Sticker / GIF\n"
        "• Caption + inline buttons\n"
        "• Polls & any other message type\n\n"
        "📌 The message will be delivered **exactly** as you send it.\n\n"
        "💡 **Tip:** You can also reply to any existing message with /broadcast\n"
        "   to skip this step.\n\n"
        "Send /bcancel to abort."
    )


# ─── /bcancel ────────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("bcancel"))
async def broadcast_cancel_cmd(client, message: Message):
    if not await is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    st  = _state.pop(uid, None)

    if st and st["step"] != "running":
        await message.reply_text("✅ Broadcast setup cancelled.")
    elif st and st["step"] == "running":
        await message.reply_text(
            "⚠️ A broadcast is currently **running** and cannot be cancelled mid-flight.\n"
            "Wait for it to complete."
        )
        _state[uid] = st  # put it back
    else:
        await message.reply_text("ℹ️ No active broadcast to cancel.")


# ─── Shared confirmation helper ──────────────────────────────────────────────

async def _show_confirmation(reply_to: Message, source_msg: Message, uid: int) -> None:
    """Store state and send the Step-2 confirmation card."""
    total = await get_total_users()
    _state[uid] = {"step": "confirm", "msg": source_msg}

    # Echo the message back as a live preview
    try:
        await source_msg.copy(reply_to.chat.id)
    except Exception as exc:
        _log.warning("Could not preview broadcast message: %s", exc)

    # Rough ETA at _SEND_DELAY seconds/msg
    eta_s            = max(1, int(total * _SEND_DELAY))
    eta_m, eta_s_rem = divmod(eta_s, 60)
    eta_h, eta_m     = divmod(eta_m, 60)
    if eta_h:
        eta_str = f"{eta_h}h {eta_m}m"
    elif eta_m:
        eta_str = f"{eta_m}m {eta_s_rem}s"
    else:
        eta_str = f"{eta_s_rem}s"

    await reply_to.reply_text(
        f"📋 **Broadcast — Confirm**\n\n"
        f"👆 Preview of your message is above.\n\n"
        f"👥 Recipients:     **{total:,}** users\n"
        f"⏱ Estimated time: **~{eta_str}**\n\n"
        "Ready to send to everyone?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Broadcast", callback_data="bc:confirm"),
                InlineKeyboardButton("❌ Cancel",            callback_data="bc:cancel"),
            ]
        ]),
    )


# ─── Receive the message to broadcast (Flow B) ───────────────────────────────

@bot.on_message(filters.private, group=10)
async def broadcast_receive(client, message: Message):
    """
    Catch-all in group 10 (lower priority than command handlers in group 0).
    Only activates when the admin is in the "await_msg" step.
    """
    if not message.from_user:
        return

    uid   = message.from_user.id
    state = _state.get(uid)

    if not state or state["step"] != "await_msg":
        return
    if not await is_admin(uid):
        return

    # Ignore commands — /bcancel is already handled above
    if message.text and message.text.startswith("/"):
        return

    await _show_confirmation(message, message, uid)


# ─── Inline button callbacks ──────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^bc:(confirm|cancel)$"))
async def broadcast_callback(client, cq: CallbackQuery):
    if not await is_admin(cq.from_user.id):
        await cq.answer("❌ Admins only.", show_alert=True)
        return

    uid    = cq.from_user.id
    action = cq.data.split(":")[1]

    if action == "cancel":
        _state.pop(uid, None)
        await cq.message.edit_text("❌ **Broadcast cancelled.**")
        await cq.answer("Cancelled.")
        return

    # ── confirm ──
    state = _state.get(uid)
    if not state or state["step"] != "confirm":
        await cq.answer("⚠️ Session expired. Run /broadcast again.", show_alert=True)
        return

    source_msg: Message = state["msg"]
    _state[uid] = {"step": "running", "msg": source_msg}

    await cq.message.edit_text(
        "📡 **Broadcast started!**\n\n"
        "⏳ Building user list…\n\n"
        "Progress will appear here."
    )
    await cq.answer("🚀 Broadcast started!")

    # Fire and forget — doesn't block the event loop
    asyncio.create_task(
        _run_broadcast(client, uid, source_msg, cq.message)
    )


# ─── Core broadcast loop ──────────────────────────────────────────────────────

async def _run_broadcast(
    client,
    admin_id: int,
    source_msg: Message,
    status_msg: Message,
) -> None:
    sent = failed = blocked = total = 0

    try:
        async for doc in usersdb.find({"user_id": {"$gt": 0}}, {"user_id": 1}):
            user_id = doc["user_id"]
            total  += 1

            try:
                await source_msg.copy(user_id)
                sent += 1

            except FloodWait as e:
                wait = e.value + 3
                _log.warning("FloodWait %ds during broadcast — sleeping", wait)
                try:
                    await status_msg.edit_text(
                        _progress_text(sent, blocked, failed, total)
                        + f"\n\n⏸ **FloodWait {wait}s…**"
                    )
                except Exception as _e:
                    _log.debug("Broadcast: could not update flood-wait status msg: %s", _e)
                await asyncio.sleep(wait)
                # Retry once after the flood wait
                try:
                    await source_msg.copy(user_id)
                    sent += 1
                except Exception:
                    failed += 1

            except (
                UserIsBlocked,
                InputUserDeactivated,
                PeerIdInvalid,
                UserDeactivatedBan,
            ):
                blocked += 1

            except Exception as exc:
                _log.debug("Broadcast to %s failed: %s", user_id, exc)
                failed += 1

            # Live progress update
            if total % _PROGRESS_INTERVAL == 0:
                try:
                    await status_msg.edit_text(_progress_text(sent, blocked, failed, total))
                except Exception:
                    pass

            await asyncio.sleep(_SEND_DELAY)

    except Exception as exc:
        _log.exception("Broadcast loop crashed: %s", exc)

    finally:
        _state.pop(admin_id, None)

    # Final report
    try:
        await status_msg.edit_text(_final_text(sent, blocked, failed, total))
    except Exception:
        pass

    _log.info(
        "Broadcast complete — sent=%d blocked=%d failed=%d total=%d",
        sent, blocked, failed, total,
    )


# ─── Text helpers ─────────────────────────────────────────────────────────────

def _progress_bar(done: int, total: int, width: int = 10) -> str:
    pct   = done / max(total, 1)
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled)


def _progress_text(sent: int, blocked: int, failed: int, done: int) -> str:
    bar = _progress_bar(done, done)  # indeterminate (total unknown mid-stream)
    return (
        f"📡 **Broadcasting…**\n\n"
        f"`[{bar}]`\n\n"
        f"✅ Sent:     **{sent:,}**\n"
        f"🚫 Blocked:  **{blocked:,}**\n"
        f"❌ Failed:   **{failed:,}**\n"
        f"📊 Processed: **{done:,}**"
    )


def _final_text(sent: int, blocked: int, failed: int, total: int) -> str:
    rate = int((sent / max(total, 1)) * 100)
    bar  = _progress_bar(sent, total)
    return (
        f"✅ **Broadcast Complete!**\n\n"
        f"`[{bar}]` {rate}%\n\n"
        f"✅ Sent:       **{sent:,}**\n"
        f"🚫 Blocked:    **{blocked:,}**\n"
        f"❌ Failed:     **{failed:,}**\n"
        f"📊 Total Users: **{total:,}**\n\n"
        f"📈 Delivery rate: **{rate}%**"
    )
