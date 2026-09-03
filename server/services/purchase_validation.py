"""Pre-sale validation for inventory sessions.

This module keeps the purchase boundary small and explicit: only permanent
inventory-invalid outcomes are quarantined in BIN. Network/provider/transient
failures remain retryable and never silently become BIN records.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Optional

from server.logging import LOGGER

_log = LOGGER(__name__)


@dataclass(frozen=True)
class SessionValidation:
    valid: bool
    permanent_invalid: bool = False
    retryable: bool = False
    issue: str = ""
    reason: str = ""
    session_bytes: Optional[bytes] = None


def _extract_session_bytes(raw: bytes) -> bytes:
    if not raw:
        raise ValueError("empty_session_file")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            candidates = [n for n in zf.namelist() if n.endswith(".session")]
            if candidates:
                data = zf.read(candidates[0])
                if not data:
                    raise ValueError("empty_session_file")
                return data
    except zipfile.BadZipFile:
        pass
    if not raw:
        raise ValueError("empty_session_file")
    return raw


async def validate_inventory_session(
    bot_client,
    account: dict,
    country_code: str,
) -> SessionValidation:
    """Validate a reserved inventory account before it is sold.

    Permanent outcomes: missing/corrupt reference, unauthorized/revoked
    session, frozen/deactivated/banned account, permanent spam restriction.
    Retryable outcomes: Telegram download/connection timeout, FloodWait,
    temporary API outage, and unknown SpamBot response.
    """
    if not account.get("session_msg_id") or not account.get("session_chat_id"):
        return SessionValidation(
            valid=False,
            permanent_invalid=True,
            issue="missing_session_reference",
            reason="Stored Telegram session reference is missing.",
        )

    try:
        from server.utils.sessions.channel_storage import download_session_from_channel
        raw = await download_session_from_channel(
            bot_client,
            account["session_chat_id"],
            account["session_msg_id"],
        )
        session_bytes = _extract_session_bytes(raw)
    except zipfile.BadZipFile:
        return SessionValidation(
            valid=False,
            permanent_invalid=True,
            issue="corrupt_session_archive",
            reason="Stored session archive is corrupted.",
        )
    except ValueError as exc:
        return SessionValidation(
            valid=False,
            permanent_invalid=True,
            issue=str(exc),
            reason="Stored session file is empty or invalid.",
        )
    except Exception as exc:
        from server.utils.sessions.channel_storage import StorageError
        if isinstance(exc, StorageError):
            permanent_codes = {
                "MESSAGE_NOT_FOUND", "MESSAGE_MISMATCH", "MESSAGE_CHAT_MISMATCH",
                "MESSAGE_HAS_NO_DOCUMENT", "EMPTY_FILE", "INVALID_ARCHIVE",
                "SESSION_FILE_MISSING", "INVALID_SESSION_FILE",
            }
            if exc.code in permanent_codes:
                return SessionValidation(
                    valid=False,
                    permanent_invalid=True,
                    issue=exc.code,
                    reason=str(exc),
                )
        _log.warning("inventory session storage access/download failed for %s: %s", account.get("account_id"), exc)
        return SessionValidation(
            valid=False,
            retryable=True,
            issue=getattr(exc, "code", "session_storage_temporary_error"),
            reason="Telegram session storage is temporarily unavailable.",
        )

    tmp_path = None
    tg_client = None
    try:
        import config as cfg
        from server.utils.sessions.telethon_client import (
            new_temp_session_path,
            write_session_bytes,
            connect_with_proxy_fallback,
            cleanup_session_files,
        )
        tmp_path = new_temp_session_path()
        await write_session_bytes(tmp_path, session_bytes)
        tg_client, _, _ = await connect_with_proxy_fallback(
            tmp_path[:-8], cfg.API_ID, cfg.API_HASH, country_code,
        )
        try:
            authorized = await tg_client.is_user_authorized()
        except Exception as exc:
            msg = str(exc).lower()
            if any(token in msg for token in ("frozen", "deactivated", "banned", "revoked", "unauthorized")):
                return SessionValidation(
                    valid=False,
                    permanent_invalid=True,
                    issue="authorization_revoked",
                    reason="Telegram authorization is no longer valid.",
                    session_bytes=session_bytes,
                )
            return SessionValidation(
                valid=False,
                retryable=True,
                issue="authorization_check_temporary_error",
                reason="Telegram authorization check was temporarily unavailable.",
                session_bytes=session_bytes,
            )
        if not authorized:
            return SessionValidation(
                valid=False,
                permanent_invalid=True,
                issue="session_unauthorized",
                reason="Telegram session is no longer authorized.",
                session_bytes=session_bytes,
            )

        from server.stock.spam_check import check_spam_status
        status = await check_spam_status(tg_client)
        if status in {"frozen", "permanent_spam"}:
            return SessionValidation(
                valid=False,
                permanent_invalid=True,
                issue=status,
                reason=f"Telegram account has permanent status: {status}.",
                session_bytes=session_bytes,
            )
        if status == "unknown":
            return SessionValidation(
                valid=False,
                retryable=True,
                issue="spam_status_unknown",
                reason="Telegram restriction status could not be verified.",
                session_bytes=session_bytes,
            )
        return SessionValidation(
            valid=True,
            issue=status,
            reason="Session is authorized and usable.",
            session_bytes=session_bytes,
        )
    except Exception as exc:
        msg = str(exc).lower()
        if any(token in msg for token in ("frozen", "deactivated", "banned", "revoked", "unauthorized")):
            return SessionValidation(
                valid=False,
                permanent_invalid=True,
                issue="permanent_telegram_account_error",
                reason="Telegram account/session is permanently unavailable.",
                session_bytes=session_bytes,
            )
        _log.warning("inventory session validation temporary failure for %s: %s", account.get("account_id"), exc)
        return SessionValidation(
            valid=False,
            retryable=True,
            issue="session_validation_temporary_error",
            reason="Telegram validation is temporarily unavailable.",
            session_bytes=session_bytes,
        )
    finally:
        if tg_client is not None:
            try:
                await tg_client.disconnect()
            except Exception:
                pass
        if tmp_path:
            try:
                from server.utils.sessions.telethon_client import cleanup_session_files
                await cleanup_session_files(tmp_path)
            except Exception:
                pass
