import sys

from pyrogram import Client, errors
from pyrogram.enums import ChatMemberStatus, ParseMode

import config
from ..logging import LOGGER


class Bot(Client):
    def __init__(self):
        LOGGER(__name__).info("Starting Bot...")
        super().__init__(
            name="Bot",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            in_memory=True,
            max_concurrent_transmissions=7,
        )

    async def start(self):
        await super().start()
        self.id = self.me.id
        self.name = self.me.first_name
        self.username = self.me.username
        self.mention = self.me.mention

        # The public numeric prefix of every Telegram bot token is that bot's
        # ID. Checking it against getMe prevents a pasted/truncated Render
        # secret from leaving the Mini App HMAC verifier in an unknown state.
        token_bot_id = int(config.BOT_TOKEN.partition(":")[0])
        if token_bot_id != self.id:
            LOGGER(__name__).critical(
                "BOT_TOKEN bot-ID prefix (%s) does not match authenticated bot ID (%s). Refusing to start Mini App authentication.",
                token_bot_id,
                self.id,
            )
            raise RuntimeError("BOT_TOKEN identity mismatch")

        # Ensure all MongoDB indexes exist (idempotent — safe to re-run)
        try:
            from server.utils.database.indexes import ensure_indexes
            await ensure_indexes()
        except Exception as _idx_err:
            LOGGER(__name__).warning("Index setup warning: %s", _idx_err)

        if config.LOG_GROUP_ID:
            try:
                await self.send_message(
                    chat_id=config.LOG_GROUP_ID,
                    text=f"<u><b>» {self.mention} ʙᴏᴛ sᴛᴀʀᴛᴇᴅ :</b><u>\n\nɪᴅ : <code>{self.id}</code>\nɴᴀᴍᴇ : {self.name}\nᴜsᴇʀɴᴀᴍᴇ : @{self.username}",
                )
            except (errors.ChannelInvalid, errors.PeerIdInvalid):
                LOGGER(__name__).warning(
                    "Bot could not access the log group/channel. Continuing without logging."
                )
            except Exception as ex:
                LOGGER(__name__).warning(
                    f"Bot could not access the log group/channel. Reason: {type(ex).__name__}. Continuing without logging."
                )
            else:
                a = await self.get_chat_member(config.LOG_GROUP_ID, self.id)
                if a.status != ChatMemberStatus.ADMINISTRATOR:
                    LOGGER(__name__).warning(
                        "Bot is not admin in log group/channel. Log messages may fail."
                    )
        else:
            LOGGER(__name__).info("LOG_GROUP_ID not set. Skipping log group setup.")

        # ── Verify session storage channel (critical for session uploads) ────────
        if config.SESSION_CHANNEL_ID:
            try:
                chat = await self.get_chat(config.SESSION_CHANNEL_ID)
                LOGGER(__name__).info(
                    "Session channel verified: %s (id=%s, type=%s)",
                    chat.title,
                    chat.id,
                    chat.type.value if hasattr(chat.type, "value") else chat.type,
                )
            except (errors.ChannelInvalid, errors.PeerIdInvalid):
                LOGGER(__name__).error(
                    "SESSION_CHANNEL_ID (%s) is invalid or the bot is not a member of the channel. "
                    "Add the bot as an admin to the channel and restart.",
                    config.SESSION_CHANNEL_ID,
                )
                sys.exit(1)
            except Exception as ex:
                LOGGER(__name__).warning(
                    "Could not verify SESSION_CHANNEL_ID (%s): %s. Session uploads may fail.",
                    config.SESSION_CHANNEL_ID,
                    type(ex).__name__,
                )
            else:
                # Actually try uploading a throwaway test file so we catch
                # "channel exists but bot can't post/upload here" cases too
                # (e.g. not an admin, no post-messages permission) — get_chat
                # succeeding does NOT guarantee upload rights.
                import io

                test_file = io.BytesIO(
                    f"session-channel-verification-{self.id}".encode()
                )
                test_file.name = "session_channel_test.txt"
                try:
                    test_msg = await self.send_document(
                        chat_id=config.SESSION_CHANNEL_ID,
                        document=test_file,
                        caption=(
                            "✅ Session channel upload check — startup verification.\n"
                            f"Channel: {chat.title} (id={chat.id})\n"
                            "This file is left here on purpose so you can confirm it "
                            "arrived; delete it manually whenever you like."
                        ),
                    )
                    LOGGER(__name__).info(
                        "Session channel upload check passed: test file sent to %s "
                        "'%s' (message_id=%s). Left in the channel — check it there.",
                        config.SESSION_CHANNEL_ID,
                        chat.title,
                        test_msg.id,
                    )
                except Exception as ex:
                    # Any failure to actually upload to the channel is fatal —
                    # session uploads are core functionality, so don't let the
                    # server start in a broken state.
                    LOGGER(__name__).error(
                        "SESSION_CHANNEL_ID (%s) exists but the bot could not upload a "
                        "test file there: %s: %s. Add the bot as an admin with 'Post "
                        "Messages' permission and restart.",
                        config.SESSION_CHANNEL_ID,
                        type(ex).__name__,
                        ex,
                    )
                    sys.exit(1)
        else:
            LOGGER(__name__).warning(
                "SESSION_CHANNEL_ID not set. Session upload/download features will not work."
            )

        LOGGER(__name__).info(f"Bot Started as {self.name}")
        LOGGER(__name__).info(
            "Mini App identity verification is bound to the authenticated BOT_TOKEN identity: @%s (id=%s).",
            self.username or "unknown",
            self.id,
        )

    async def stop(self):
        await super().stop()
