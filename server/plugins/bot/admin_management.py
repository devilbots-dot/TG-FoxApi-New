"""
Owner-only bot administration commands.

Commands:
  /addsudo <user_id>              Grant the user the existing admin/sudo access.
  /delsudo <user_id>              Revoke sudo access.
  /remsudo <user_id>              Alias for /delsudo.
  /listsudo                       Show all current sudo users.

The configured OWNER_ID remains the only owner. Sudo users inherit the
existing ``is_admin`` permissions used by the other admin plugins, but they
cannot add or remove sudo users.
"""

from __future__ import annotations

from pyrogram import filters
from pyrogram.types import Message

import config
from server import bot, LOGGER
from server.utils.database import add_sudo, get_sudoers, remove_sudo


_log = LOGGER(__name__)
_MAX_TELEGRAM_USER_ID = (1 << 63) - 1


def _parse_user_id(message: Message) -> int | None:
    """Read a Telegram user ID from the first command argument."""
    args = message.command[1:] if message.command else []
    if not args:
        return None
    raw = args[0].strip()
    if not raw.isdigit():
        return None
    user_id = int(raw)
    if user_id < 1 or user_id > _MAX_TELEGRAM_USER_ID:
        return None
    return user_id


async def _owner_only(message: Message) -> bool:
    """Return whether the sender is the configured owner."""
    user = message.from_user
    return bool(user and user.id == config.OWNER_ID)


def _usage(command: str) -> str:
    return (
        f"❌ **Usage:** `/{command} <user_id>`\n\n"
        "Example: `/"
        f"{command} 123456789`"
    )


@bot.on_message(filters.private & filters.command("addsudo"))
async def add_sudo_cmd(client, message: Message):
    if not await _owner_only(message):
        return

    user_id = _parse_user_id(message)
    if user_id is None:
        await message.reply_text(_usage("addsudo"))
        return
    if user_id == config.OWNER_ID:
        await message.reply_text("ℹ️ The owner already has full admin access.")
        return

    try:
        added = await add_sudo(user_id)
    except Exception:
        _log.exception("Failed to add sudo user %s", user_id)
        await message.reply_text("❌ Could not save sudo access. Please try again.")
        return

    if not added:
        await message.reply_text(f"ℹ️ `{user_id}` is already a sudo user.")
        return

    await message.reply_text(
        f"✅ Sudo access granted to `{user_id}`.\n"
        "They can now use the bot's admin commands."
    )


@bot.on_message(filters.private & filters.command(["delsudo", "remsudo"]))
async def remove_sudo_cmd(client, message: Message):
    if not await _owner_only(message):
        return

    command = message.command[0] if message.command else "delsudo"
    user_id = _parse_user_id(message)
    if user_id is None:
        await message.reply_text(_usage(command))
        return
    if user_id == config.OWNER_ID:
        await message.reply_text("❌ The configured owner cannot be removed.")
        return

    try:
        removed = await remove_sudo(user_id)
    except Exception:
        _log.exception("Failed to remove sudo user %s", user_id)
        await message.reply_text("❌ Could not remove sudo access. Please try again.")
        return

    if not removed:
        await message.reply_text(f"ℹ️ `{user_id}` is not in the sudo list.")
        return

    await message.reply_text(f"✅ Sudo access removed from `{user_id}`.")


@bot.on_message(filters.private & filters.command(["listsudo", "sudolist"]))
async def list_sudo_cmd(client, message: Message):
    if not await _owner_only(message):
        return

    try:
        sudoers = await get_sudoers()
    except Exception:
        _log.exception("Failed to load sudo users")
        await message.reply_text("❌ Could not load the sudo list. Please try again.")
        return

    if not sudoers:
        await message.reply_text(
            "👥 **Sudo users**\n\nNo sudo users have been added yet.\n"
            "Use `/addsudo <user_id>` to add one."
        )
        return

    lines = ["👥 **Sudo users**", ""]
    lines.extend(f"• `{user_id}`" for user_id in sudoers)
    lines.append("")
    lines.append("Remove access with `/delsudo <user_id>`.")
    await message.reply_text("\n".join(lines))