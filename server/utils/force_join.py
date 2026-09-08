"""Force-join gate for the bot.

Membership is checked live with Pyrogram on every gate/verify action.
"""
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import InlineKeyboardMarkup
from server.utils.bot_utils import Btn as InlineKeyboardButton
import config as _cfg


def _chat_ref(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


async def is_force_joined(client, user_id: int) -> bool:
    """Return True only when the user is currently a member of both targets."""
    if not getattr(_cfg, "FORCE_JOIN_ENABLED", False):
        return True

    targets = (
        getattr(_cfg, "FORCE_JOIN_CHANNEL_ID", ""),
        getattr(_cfg, "FORCE_JOIN_GROUP_ID", ""),
    )
    for raw in targets:
        chat = _chat_ref(raw)
        if not chat:
            return False
        try:
            member = await client.get_chat_member(chat, user_id)
            status = getattr(member, "status", None)
            if status in (
                ChatMemberStatus.OWNER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.MEMBER,
            ):
                continue
            if status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False)):
                continue
            return False
        except Exception:
            return False
    return True


def force_join_text() -> str:
    return (
        "![🔒](tg://emoji?id=6129550284290006595) **Join Required**\n\n"
        "Please join our channel and group to use the bot.\n"
        "After joining both, tap **Verify Membership**.\n\n"
        "![🔄](tg://emoji?id=6129792056589031358) Membership is checked live."
    )


def force_join_keyboard(callback_data: str = "forcejoin_verify") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "![📢](tg://emoji?id=6131886699254388574) Join Channel",
                url=getattr(_cfg, "FORCE_JOIN_CHANNEL_URL", ""),
            ),
            InlineKeyboardButton(
                "![👥](tg://emoji?id=5316979275461573049) Join Group",
                url=getattr(_cfg, "FORCE_JOIN_GROUP_URL", ""),
            ),
        ],
        [
            InlineKeyboardButton(
                "![✅](tg://emoji?id=6129492160497589882) Verify Membership",
                callback_data=callback_data,
            ),
        ],
    ])
