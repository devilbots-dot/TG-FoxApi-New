"""Telegram storage-channel I/O with legacy-safe diagnostics.

Existing Mongo records are the source of truth for legacy downloads:
``session_chat_id`` and ``session_msg_id`` are always used together.  New
uploads return the exact chat/message reference returned by Telegram so callers
can persist both values without guessing.
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    """Safe, categorized storage failure that retains the original exception."""

    def __init__(self, code: str, message: str, *, cause: BaseException | None = None, context: dict | None = None):
        self.code = code
        self.context = context or {}
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
MESSAGE_ACCESS_DENIED = "MESSAGE_ACCESS_DENIED"
MESSAGE_MISMATCH = "MESSAGE_MISMATCH"
MESSAGE_CHAT_MISMATCH = "MESSAGE_CHAT_MISMATCH"
MESSAGE_HAS_NO_DOCUMENT = "MESSAGE_HAS_NO_DOCUMENT"
MEDIA_DOWNLOAD_FAILED = "MEDIA_DOWNLOAD_FAILED"
EMPTY_FILE = "EMPTY_FILE"
INVALID_ARCHIVE = "INVALID_ARCHIVE"
SESSION_FILE_MISSING = "SESSION_FILE_MISSING"
INVALID_SESSION_FILE = "INVALID_SESSION_FILE"
UNKNOWN_STORAGE_ERROR = "UNKNOWN_STORAGE_ERROR"


@dataclass(frozen=True)
class TelegramStorageRef:
    """Exact Telegram reference returned by ``send_document``."""

    chat_id: int | str
    message_id: int

    def __int__(self) -> int:
        return int(self.message_id)

    def __str__(self) -> str:
        return str(self.message_id)


def _safe_context(chat_id: Any, msg_id: Any, *, account_id: str | None = None, phone: str | None = None) -> dict:
    context = {"session_chat_id": str(chat_id), "session_msg_id": str(msg_id)}
    if account_id:
        context["account_id"] = account_id
    if phone:
        context["phone"] = phone
    return context


def _is_access_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return any(token in name or token in text for token in (
        "forbidden", "access", "permission", "private", "channelprivate",
        "peeridinvalid", "usernotparticipant", "chatadminrequired",
    ))


def _same_id(left: Any, right: Any) -> bool:
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _document_name(document: Any) -> str | None:
    name = getattr(document, "file_name", None)
    if name:
        return str(name)
    return None


def _validate_message(message: Any, chat_id: int, msg_id: int, context: dict) -> None:
    if message is None:
        raise StorageError(MESSAGE_NOT_FOUND, "Stored Telegram message does not exist.", context=context)

    if isinstance(message, (list, tuple)):
        if not message:
            raise StorageError(MESSAGE_NOT_FOUND, "Stored Telegram message lookup returned no message.", context=context)
        message = message[0]

    actual_msg_id = getattr(message, "id", None)
    if not _same_id(actual_msg_id, msg_id):
        raise StorageError(
            MESSAGE_MISMATCH,
            "Telegram returned a different message than the stored reference.",
            context={**context, "actual_message_id": str(actual_msg_id)},
        )

    actual_chat = getattr(getattr(message, "chat", None), "id", None)
    if not _same_id(actual_chat, chat_id):
        raise StorageError(
            MESSAGE_CHAT_MISMATCH,
            "Telegram message belongs to a different chat than the stored reference.",
            context={**context, "actual_chat_id": str(actual_chat)},
        )

    if getattr(message, "document", None) is None:
        raise StorageError(
            MESSAGE_HAS_NO_DOCUMENT,
            "Stored Telegram message does not contain a document.",
            context=context,
        )


def _validate_downloaded_object(downloaded: Any, context: dict) -> bytes:
    if downloaded is None:
        raise StorageError(MEDIA_DOWNLOAD_FAILED, "Telegram returned no downloadable media.", context=context)
    if isinstance(downloaded, (bytes, bytearray)):
        raw = bytes(downloaded)
    elif hasattr(downloaded, "read"):
        try:
            downloaded.seek(0)
            raw = downloaded.read()
        except Exception as exc:
            raise StorageError(MEDIA_DOWNLOAD_FAILED, "Downloaded media could not be read.", cause=exc, context=context) from exc
    elif isinstance(downloaded, (str, os.PathLike)):
        path = Path(downloaded)
        if not path.exists() or not path.is_file():
            raise StorageError(MEDIA_DOWNLOAD_FAILED, "Telegram returned a missing media file.", context={**context, "download_path": str(path)})
        try:
            raw = path.read_bytes()
        except Exception as exc:
            raise StorageError(MEDIA_DOWNLOAD_FAILED, "Downloaded media file could not be read.", cause=exc, context=context) from exc
    else:
        raise StorageError(MEDIA_DOWNLOAD_FAILED, "Telegram returned an unsupported media object.", context=context)
    if not raw:
        raise StorageError(EMPTY_FILE, "Downloaded Telegram media is empty.", context=context)
    return raw


def _safe_archive_member(name: str) -> bool:
    path = Path(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and path.name == name


def extract_session_bytes(raw: bytes) -> bytes:
    """Validate a ZIP/raw storage object and return non-empty session bytes."""
    if not raw:
        raise StorageError(EMPTY_FILE, "Stored media is empty.")

    if not raw.startswith(b"PK"):
        return raw

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.testzip() is not None:
                raise StorageError(INVALID_ARCHIVE, "Stored ZIP archive failed integrity validation.")
            session_names = [
                name for name in archive.namelist()
                if name.lower().endswith(".session") and _safe_archive_member(name)
            ]
            unsafe_session_names = [
                name for name in archive.namelist()
                if name.lower().endswith(".session") and not _safe_archive_member(name)
            ]
            if unsafe_session_names:
                raise StorageError(INVALID_ARCHIVE, "Stored ZIP contains an unsafe session path.")
            if not session_names:
                raise StorageError(SESSION_FILE_MISSING, "Stored ZIP does not contain a .session file.")
            session_bytes = archive.read(session_names[0])
            if not session_bytes:
                raise StorageError(EMPTY_FILE, "Stored .session file inside ZIP is empty.")
            return session_bytes
    except StorageError:
        raise
    except zipfile.BadZipFile as exc:
        raise StorageError(INVALID_ARCHIVE, "Stored media starts like a ZIP but is corrupt.", cause=exc) from exc
    except OSError as exc:
        raise StorageError(INVALID_ARCHIVE, "Stored ZIP archive could not be read.", cause=exc) from exc


async def upload_session_to_channel(
    bot_client,
    channel_id: int,
    phone: str,
    session_bytes: bytes,
) -> TelegramStorageRef:
    """Upload a raw .session document and return exact chat/message IDs."""
    if not session_bytes:
        raise StorageError(EMPTY_FILE, "Refusing to upload an empty session file.")
    filename = f"{phone}.session"
    buf = io.BytesIO(session_bytes)
    buf.name = filename
    try:
        message = await bot_client.send_document(
            chat_id=channel_id,
            document=buf,
            caption=f"📦 Session: `{phone}`",
            file_name=filename,
        )
    except Exception as exc:
        raise StorageError(MEDIA_DOWNLOAD_FAILED, "Telegram storage upload failed.", cause=exc, context={"channel_id": str(channel_id)}) from exc
    actual_chat_id = getattr(getattr(message, "chat", None), "id", None)
    actual_msg_id = getattr(message, "id", None)
    if actual_chat_id is None or actual_msg_id is None:
        raise StorageError(UNKNOWN_STORAGE_ERROR, "Telegram upload returned an incomplete message reference.", context={"channel_id": str(channel_id)})
    return TelegramStorageRef(actual_chat_id, int(actual_msg_id))


async def upload_session_zip_to_channel(
    bot_client,
    channel_id: int,
    phone: str,
    session_bytes: bytes,
    *,
    tfa_password: str = "",
    has_2fa: bool = False,
    spam_status: str = "unknown",
    country_code: str = "",
    country_name: str = "",
    sell_type: str = "account",
) -> TelegramStorageRef:
    """Build/upload a self-contained ZIP and return exact chat/message IDs."""
    if not session_bytes:
        raise StorageError(EMPTY_FILE, "Refusing to upload an empty session file.")
    safe_phone = re.sub(r"[^0-9A-Za-z_+.-]", "", phone.replace("+", "").replace(" ", ""))
    session_filename = f"{safe_phone}.session"
    json_filename = f"{safe_phone}_info.json"
    zip_filename = f"{safe_phone}.zip"
    info = {
        "phone": phone,
        "country_code": country_code,
        "country_name": country_name,
        "sell_type": sell_type,
        "spam_status": spam_status,
        "has_2fa": has_2fa,
        "tfa_password": tfa_password if tfa_password else None,
        "hint": "Managed" if tfa_password else None,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(session_filename, session_bytes)
        archive.writestr(json_filename, json.dumps(info, ensure_ascii=False, indent=2))
    zip_buf.seek(0)
    zip_buf.name = zip_filename
    caption_lines = [
        f"📦 **Session ZIP** — `{phone}`",
        f"🌍 {country_name or country_code}  |  🔰 {sell_type}  |  🛡 spam:`{spam_status}`",
    ]
    if has_2fa:
        caption_lines.append("🔐 2FA: set (password stored inside the protected archive)")
    else:
        caption_lines.append("🔓 2FA: not set")
    try:
        message = await bot_client.send_document(
            chat_id=channel_id,
            document=zip_buf,
            caption="\n".join(caption_lines),
            file_name=zip_filename,
        )
    except Exception as exc:
        raise StorageError(UNKNOWN_STORAGE_ERROR, "Telegram storage ZIP upload failed.", cause=exc, context={"channel_id": str(channel_id)}) from exc
    actual_chat_id = getattr(getattr(message, "chat", None), "id", None)
    actual_msg_id = getattr(message, "id", None)
    if actual_chat_id is None or actual_msg_id is None:
        raise StorageError(UNKNOWN_STORAGE_ERROR, "Telegram ZIP upload returned an incomplete message reference.", context={"channel_id": str(channel_id)})
    return TelegramStorageRef(actual_chat_id, int(actual_msg_id))


async def diagnose_session_message(
    bot_client,
    chat_id: int,
    msg_id: int,
    *,
    account_id: str | None = None,
    phone: str | None = None,
) -> dict:
    """Read-only diagnostic for one stored Telegram message reference."""
    context = _safe_context(chat_id, msg_id, account_id=account_id, phone=phone)
    result = {
        "account_id": account_id,
        "phone": phone,
        "db_chat_id": chat_id,
        "db_message_id": msg_id,
        "message_exists": False,
        "actual_chat_id": None,
        "actual_message_id": None,
        "document_exists": False,
        "file_name": None,
        "file_size": None,
        "download_status": "not_attempted",
        "file_validation_status": "not_attempted",
        "error_code": None,
        "error": None,
    }
    try:
        message = await bot_client.get_messages(chat_id, msg_id)
    except Exception as exc:
        code = MESSAGE_ACCESS_DENIED if _is_access_error(exc) else UNKNOWN_STORAGE_ERROR
        result.update(error_code=code, error="Telegram message lookup failed.")
        return result
    result["message_exists"] = message is not None
    result["actual_message_id"] = getattr(message, "id", None)
    result["actual_chat_id"] = getattr(getattr(message, "chat", None), "id", None)
    try:
        _validate_message(message, chat_id, msg_id, context)
    except StorageError as exc:
        result.update(error_code=exc.code, error=str(exc))
        return result
    document = getattr(message, "document", None)
    result.update(
        document_exists=True,
        file_name=_document_name(document),
        file_size=getattr(document, "file_size", None),
    )
    try:
        downloaded = await bot_client.download_media(message, in_memory=True)
        raw = _validate_downloaded_object(downloaded, context)
        result["download_status"] = "ok"
        result["downloaded_bytes"] = len(raw)
        extract_session_bytes(raw)
        result["file_validation_status"] = "ok"
    except StorageError as exc:
        result.update(error_code=exc.code, error=str(exc))
    except Exception:
        result.update(error_code=UNKNOWN_STORAGE_ERROR, error="Unexpected storage diagnostic error.")
    return result


async def download_session_from_channel(
    bot_client,
    chat_id: int,
    msg_id: int,
    *,
    account_id: str | None = None,
    phone: str | None = None,
) -> bytes:
    """Download exactly the Mongo-referenced message and return session bytes."""
    context = _safe_context(chat_id, msg_id, account_id=account_id, phone=phone)
    try:
        if not chat_id:
            raise StorageError(MESSAGE_ACCESS_DENIED, "Stored Telegram chat reference is empty.", context=context)
        if not isinstance(msg_id, int) or msg_id <= 0:
            raise StorageError(MESSAGE_MISMATCH, "Stored Telegram message ID is invalid.", context=context)
        try:
            message = await bot_client.get_messages(chat_id, msg_id)
        except Exception as exc:
            code = MESSAGE_ACCESS_DENIED if _is_access_error(exc) else UNKNOWN_STORAGE_ERROR
            detail = "Current Telegram client cannot access the stored chat." if code == MESSAGE_ACCESS_DENIED else "Telegram message lookup failed."
            raise StorageError(code, detail, cause=exc, context=context) from exc
        _validate_message(message, chat_id, msg_id, context)
        document = getattr(message, "document", None)
        try:
            downloaded = await bot_client.download_media(message, in_memory=True)
        except Exception as exc:
            raise StorageError(MEDIA_DOWNLOAD_FAILED, "Telegram document exists but media download failed.", cause=exc, context={**context, "file_name": _document_name(document)}) from exc
        raw = _validate_downloaded_object(downloaded, context)
        try:
            return extract_session_bytes(raw)
        except StorageError as exc:
            exc.context.update(context)
            raise
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(UNKNOWN_STORAGE_ERROR, "Unexpected Telegram storage error.", cause=exc, context=context) from exc
