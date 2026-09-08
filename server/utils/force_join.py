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


async def _is_member_or_pending_request(client, chat, user_id: int) -> bool:
    """Accept an actual member OR an active pending join request."""
    try:
        member = await client.get_chat_member(chat, user_id)
        status = getattr(member, "status", None)
        if status in (
            ChatMemberStatus.OWNER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.MEMBER,
        ):
            return True
        if status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False)):
            return True
    except Exception:
        pass

    # Private-group join requests are not returned by get_chat_member().
    # Search Telegram's pending-request list and verify the exact user ID.
    try:
        user = await client.get_users(user_id)
        queries = []
        if getattr(user, "username", None):
            queries.append(user.username)
        if getattr(user, "first_name", None):
            queries.append(user.first_name)
        if getattr(user, "last_name", None):
            queries.append(user.last_name)

        seen = set()
        for query in queries:
            query = str(query).strip()
            if not query or query.lower() in seen:
                continue
            seen.add(query.lower())
            async for req in client.get_chat_join_requests(chat, query=query, limit=100):
                if req.from_user and req.from_user.id == user_id:
                    return True
    except Exception:
        pass

    return False


async def is_force_joined(client, user_id: int) -> bool:
    """Return True when the user is a member of both targets or has a pending
    join request for the private target. Membership/request status is checked
    live on every call.
    """
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
        if await _is_member_or_pending_request(client, chat, user_id):
            continue
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
