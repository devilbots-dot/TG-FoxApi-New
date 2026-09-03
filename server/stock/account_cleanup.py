"""
Post-delivery account cleanup.

After a buyer receives an account (buy account or buy session), we connect
to the sold account and:
  1. Delete all private chat history (including Saved Messages).
  2. Leave / delete every group and channel.

This is always run in a background task — the delivery flow never waits on it.
"""

import asyncio

from server import LOGGER

_log = LOGGER(__name__)


async def cleanup_account_data(client, phone: str) -> dict:
    """
    On an already-connected, authorised Telethon client:
      • Delete all private / saved-message dialogs.
      • Leave all channels and supergroups.
      • Leave / delete all legacy groups.

    Returns a summary dict for logging.  Never raises.
    """
    deleted_chats = 0
    left_groups   = 0
    errors        = 0

    try:
        from telethon.tl.types import Channel, Chat, User

        _log.info("account_cleanup: starting for %s", phone)
        dialogs = await client.get_dialogs(limit=None)
        _log.info("account_cleanup: %s — %d dialogs to process", phone, len(dialogs))

        for dialog in dialogs:
            entity = dialog.entity
            try:
                if isinstance(entity, Channel):
                    # Broadcast channel or supergroup
                    from telethon.tl.functions.channels import LeaveChannelRequest
                    await client(LeaveChannelRequest(entity))
                    left_groups += 1
                    await asyncio.sleep(0.4)

                elif isinstance(entity, Chat):
                    # Legacy group — use delete_dialog which sends DeleteChatUser
                    try:
                        await client.delete_dialog(entity)
                    except Exception:
                        # Fallback: explicit leave
                        from telethon.tl.functions.messages import DeleteChatUserRequest
                        try:
                            await client(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
                        except Exception:
                            pass
                    left_groups += 1
                    await asyncio.sleep(0.4)

                elif isinstance(entity, User):
                    # Private chat or Saved Messages — delete from our side
                    await client.delete_dialog(entity)
                    deleted_chats += 1
                    await asyncio.sleep(0.15)

            except Exception as exc:
                _log.debug(
                    "account_cleanup: skip dialog %s (%s): %s",
                    getattr(entity, "id", "?"), type(entity).__name__, exc,
                )
                errors += 1

    except Exception as exc:
        _log.error("account_cleanup: fatal error for %s: %s", phone, exc)
        errors += 1

    summary = {
        "phone":         phone,
        "deleted_chats": deleted_chats,
        "left_groups":   left_groups,
        "errors":        errors,
    }
    _log.info("account_cleanup: done — %s", summary)
    return summary


async def cleanup_session_in_background(
    session_bytes: bytes,
    phone: str,
    country_code: str,
    api_id: int,
    api_hash: str,
) -> None:
    """
    Write session bytes to a temp file, connect via proxy, run cleanup,
    then disconnect and delete the temp file.

    Designed for asyncio.create_task() — never awaited by the caller.
    """
    from server.utils.sessions.telethon_client import (
        new_temp_session_path,
        write_session_bytes,
        cleanup_session_files,
        connect_with_proxy_fallback,
    )

    tmp_path = new_temp_session_path()
    client   = None
    try:
        await write_session_bytes(tmp_path, session_bytes)
        session_path = tmp_path[:-8]  # strip ".session" suffix

        client, _, _ = await connect_with_proxy_fallback(
            session_path, api_id, api_hash, country_code
        )
        await cleanup_account_data(client, phone)

    except Exception as exc:
        _log.error("cleanup_session_in_background %s: %s", phone, exc)
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        try:
            await cleanup_session_files(tmp_path)
        except Exception:
            pass
