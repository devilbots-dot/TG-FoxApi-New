"""
Sales Feed, Fake Sales, and Mini App — Bot admin commands.

Commands (owner + sudo only):
  /admin sales on|off           Enable/disable the real sales feed
  /setlogchat <chat_id>         Set the destination chat for the sales feed
  /salesstatus                  Show current sales feed configuration
  /testsaleslog                 Send a test notification to the feed chat
  /fakesales on|off|status|test Enable/check/test isolated fake sales
  /setfakeinterval <min> [max]  Set fake sales interval in seconds
  /miniapp on|off|status        Show/hide Mini App buttons and menu entry
"""

from __future__ import annotations

import config
from pyrogram import filters
from pyrogram.types import Message, MenuButtonDefault, MenuButtonWebApp, WebAppInfo

from server import bot, LOGGER
from server.core import memstore
from server.utils.database.configdb import set_setting

_log = LOGGER(__name__)


# ── Auth guard ────────────────────────────────────────────────────────────────

async def _is_admin(message: Message) -> bool:
    user = message.from_user
    if not user:
        return False
    return user.id == config.OWNER_ID or user.id in memstore.sudoers


# ── /admin sales on|off ───────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("admin"))
async def admin_sales_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    args = message.command[1:]
    if not args or args[0].lower() != "sales":
        return   # not for us — other admin handlers may pick it up
    if len(args) < 2:
        await message.reply_text(
            "❓ **Usage:** `/admin sales on` or `/admin sales off`"
        )
        return
    action = args[1].lower()
    if action not in ("on", "off"):
        await message.reply_text(
            "❓ **Usage:** `/admin sales on` or `/admin sales off`"
        )
        return
    enabled = action == "on"
    await set_setting("sales_feed_enabled", enabled)
    status = "✅ **enabled**" if enabled else "❌ **disabled**"
    await message.reply_text(f"📡 Sales feed {status}.")


# ── /setlogchat <chat_id> ─────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("setlogchat"))
async def set_log_chat_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    args = message.command[1:]
    if not args:
        current = str(memstore.settings.get("sales_feed_chat_id") or "")
        if current:
            await message.reply_text(
                f"📬 **Current sales feed chat:** `{current}`\n\n"
                "To change it: `/setlogchat <chat_id>`"
            )
        else:
            await message.reply_text(
                "📬 No sales feed chat configured yet.\n\n"
                "Usage: `/setlogchat <chat_id>`\n"
                "Tip: Forward any message from your target chat to @userinfobot to get the ID."
            )
        return
    chat_id = args[0].strip()
    if not chat_id.lstrip("-").isdigit():
        await message.reply_text(
            "❌ Invalid chat ID. Must be a numeric ID (e.g. `-1001234567890`).\n"
            "Tip: Forward a message from the target chat to @userinfobot to find its ID."
        )
        return
    await set_setting("sales_feed_chat_id", chat_id)
    await message.reply_text(
        f"✅ **Sales feed chat set to:** `{chat_id}`\n\n"
        "Use `/testsaleslog` to verify the connection."
    )


# ── /salesstatus ──────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("salesstatus"))
async def sales_status_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    s = memstore.settings
    enabled        = bool(s.get("sales_feed_enabled", False))
    chat_id        = str(s.get("sales_feed_chat_id") or "")
    silent         = bool(s.get("sales_feed_silent", False))
    delay          = int(s.get("sales_feed_delay_seconds", 0))

    text = (
        "📡 **Sales Feed Status**\n\n"
        f"🔌 **Feed enabled:**   {'✅ Yes' if enabled else '❌ No'}\n"
        f"📬 **Chat ID:**        `{chat_id or '— not set —'}`\n"
        f"🔇 **Silent mode:**    {'✅ Yes' if silent else '❌ No'}\n"
        f"⏱ **Notify delay:**   `{delay}s`\n\n"
        "✅ **Data policy:** only completed real purchases are published.\n\n"
        "_Use the admin panel at /admin/sales-feed for full configuration._"
    )
    await message.reply_text(text)


# ── /testsaleslog ─────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("testsaleslog"))
async def test_sales_log_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    chat_id = str(memstore.settings.get("sales_feed_chat_id") or "")
    if not chat_id:
        await message.reply_text(
            "⚠️ No sales feed chat configured.\n"
            "Set one first: `/setlogchat <chat_id>`"
        )
        return
    await message.reply_text("⏳ Sending test notification…")
    from server.services.sales_feed import sales_feed_service
    ok, reason = await sales_feed_service.send_test()
    if ok:
        await message.reply_text(f"✅ Test notification sent to `{chat_id}`.")
    else:
        await message.reply_text(
            f"❌ Failed to send to `{chat_id}`.\n"
            f"{reason or 'Check that the bot is a member of the target chat and has permission to send messages.'}"
        )


# ── /fakesales on|off|status|test ────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("fakesales"))
async def fake_sales_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    args = message.command[1:]
    if not args:
        await message.reply_text(
            "❓ **Usage:**\n"
            "`/fakesales on` — enable fake sales\n"
            "`/fakesales off` — disable fake sales\n"
            "`/fakesales status` — show current settings\n"
            "`/fakesales test` — send one fake sale immediately"
        )
        return

    action = args[0].lower()
    if action == "on":
        await set_setting("fake_sales_enabled", True)
        await message.reply_text("✅ **Fake sales enabled.**")
    elif action == "off":
        await set_setting("fake_sales_enabled", False)
        await message.reply_text("❌ **Fake sales disabled.**")
    elif action == "status":
        s = memstore.settings
        enabled = bool(s.get("fake_sales_enabled", False))
        int_min = int(s.get("fake_sales_interval_min", 300))
        int_max = int(s.get("fake_sales_interval_max", 900))
        rand_level = int(s.get("fake_sales_randomization_level", 5))
        prod_pool = s.get("fake_sales_product_pool") or ["account", "session"]
        await message.reply_text(
            "🎭 **Fake Sales Status**\n\n"
            f"🤖 **Enabled:**        {'✅ Yes' if enabled else '❌ No'}\n"
            f"⏰ **Interval:**       `{int_min}–{int_max}s`\n"
            f"🎲 **Randomization:** `{rand_level}/10`\n"
            f"📦 **Product pool:**  `{', '.join(prod_pool)}`"
        )
    elif action == "test":
        if not str(memstore.settings.get("sales_feed_chat_id") or ""):
            await message.reply_text("⚠️ No sales feed chat configured.\nSet one first: `/setlogchat <chat_id>`")
            return
        await message.reply_text("⏳ Sending fake sale preview…")
        from server.services.sales_feed import sales_feed_service
        ok, reason = await sales_feed_service.send_fake_preview()
        await message.reply_text("✅ Fake sale preview sent." if ok else f"❌ Failed: {reason}")
    else:
        await message.reply_text("❓ Unknown action. Use: `on`, `off`, `status`, or `test`.")


# ── /setfakeinterval <min> [max] ──────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("setfakeinterval"))
async def set_fake_interval_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    args = message.command[1:]
    if not args:
        await message.reply_text("❓ **Usage:** `/setfakeinterval <min_seconds> [max_seconds]`\n\nExample: `/setfakeinterval 120 600`\nMin: 30 seconds.")
        return
    try:
        int_min = int(args[0])
        int_max = int(args[1]) if len(args) > 1 else int_min * 2
    except ValueError:
        await message.reply_text("❌ Invalid numbers. Both values must be integers (seconds).")
        return
    if int_min < 30:
        await message.reply_text("❌ Minimum interval is 30 seconds.")
        return
    int_max = max(int_min, int_max)
    await set_setting("fake_sales_interval_min", int_min)
    await set_setting("fake_sales_interval_max", int_max)
    await message.reply_text(f"✅ **Fake sales interval updated.**\n\n⏰ Interval: `{int_min}–{int_max}s`")


# ── /miniapp on|off|status ───────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("miniapp"))
async def miniapp_cmd(client, message: Message):
    if not await _is_admin(message):
        return
    args = message.command[1:]
    enabled = bool(memstore.settings.get("miniapp_enabled", True))
    if not args or args[0].lower() == "status":
        await message.reply_text(f"🦊 Mini App is {'✅ enabled' if enabled else '❌ disabled'}.\nUsage: `/miniapp on` or `/miniapp off`")
        return
    action = args[0].lower()
    if action not in ("on", "off"):
        await message.reply_text("❓ **Usage:** `/miniapp on`, `/miniapp off`, or `/miniapp status`")
        return
    enabled = action == "on"
    await set_setting("miniapp_enabled", enabled)
    try:
        if enabled and config.WEBAPP_URL:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Open TgFox",
                    web_app=WebAppInfo(url=config.WEBAPP_URL),
                )
            )
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as exc:
        _log.warning("Mini App menu update failed: %s", exc)
        await message.reply_text(f"⚠️ Setting saved, but Telegram menu update failed: `{exc}`")
        return
    await message.reply_text(f"🦊 Mini App {'✅ enabled' if enabled else '❌ disabled'}. The Start menu will {'show' if enabled else 'hide'} its button.")
