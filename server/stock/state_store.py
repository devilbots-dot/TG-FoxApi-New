"""
Persistent per-account pipeline state machine + checkpointing.

Two jobs:
  1. Observability — every account processed through the stock pipeline gets
     a persisted state + timeline in MongoDB (`pipeline_state` collection),
     so an admin (or a future debugging session) can always see exactly
     which stage an account reached, not just its final outcome.
  2. Crash safety — the pipeline's single most dangerous window is between
     "the brand-new session's bytes have been read" and "the uploaded
     session has logged itself out". If the process dies in that window
     without a checkpoint, the account is stranded: the old session may
     already be gone and the new one only ever existed in RAM. We persist
     the finished session bytes (encrypted) to this collection BEFORE the
     self-logout call, closing that gap.

Nothing here ever raises into the pipeline — every write is best-effort
(logged on failure) so a MongoDB hiccup can never break account processing,
which must keep working even if checkpointing itself is degraded.
"""

import time
from datetime import datetime, timezone
from typing import Optional

from server import LOGGER
from server.core.mongo import collection
from server.stock.pipeline_states import (
    COMPLETED,
    FAILED,
    FINAL_SESSION_UPLOADED,
    NEEDS_2FA,
    NEEDS_COUNTRY,
    RESUMABLE_FROM_SCRATCH,
    UPLOADED,
)
from server.utils.sessions.crypto import decrypt_bytes, encrypt_bytes

_log = LOGGER(__name__)

_COLLECTION = "pipeline_state"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def init_state(
    phone: str,
    batch_id: str,
    chat_id: int,
    channel_id: int,
    session_bytes: bytes,
    country_code: Optional[str],
    admin_2fa_pass: str,
    api_id_override: Optional[int],
    api_hash_override: Optional[str],
    batch_action: str,
    batch_new_password: Optional[str],
) -> None:
    """Create (or replace) the checkpoint doc for an account about to be
    processed. Called once at the start of every pipeline run — both fresh
    uploads and resumed pending items."""
    doc = {
        "_id":                     phone,
        "batch_id":                batch_id,
        "chat_id":                 chat_id,
        "channel_id":              channel_id,
        "session_bytes_enc":       encrypt_bytes(session_bytes),
        "final_session_bytes_enc": None,
        "final_info":              None,
        "country_code":            country_code,
        "admin_2fa_pass_enc":      admin_2fa_pass or "",
        "api_id_override":         api_id_override,
        "api_hash_override":       api_hash_override,
        "batch_action":            batch_action,
        "batch_new_password":      batch_new_password,
        "state":                   UPLOADED,
        "timeline":                [{"state": UPLOADED, "at": _now()}],
        "error":                   None,
        "created_at":              _now(),
        "updated_at":              _now(),
    }
    try:
        await collection(_COLLECTION).replace_one({"_id": phone}, doc, upsert=True)
    except Exception as exc:
        _log.warning("state_store.init_state failed for %s: %s", phone, exc)


async def advance(phone: str, state: str) -> None:
    """Move an account to a new state and append it to its timeline."""
    try:
        await collection(_COLLECTION).update_one(
            {"_id": phone},
            {
                "$set":  {"state": state, "updated_at": _now()},
                "$push": {"timeline": {"state": state, "at": _now()}},
            },
        )
    except Exception as exc:
        _log.warning("state_store.advance(%s) failed for %s: %s", state, phone, exc)


async def save_final_session(phone: str, session_bytes: bytes, final_info: dict) -> None:
    """
    THE critical checkpoint. Must be called after the final session bytes
    are read but BEFORE the uploaded session is asked to log itself out —
    once this write succeeds, the account can always be finished purely
    from this document even if the process crashes immediately after.
    """
    # final_info may contain non-JSON-safe values (e.g. raw bytes) — keep
    # only the plain, storable fields needed to finish the DB record later.
    safe_info = {
        k: v for k, v in final_info.items()
        if k != "fresh_session_bytes" and isinstance(v, (str, int, float, bool, type(None), dict, list))
    }
    try:
        await collection(_COLLECTION).update_one(
            {"_id": phone},
            {
                "$set": {
                    "final_session_bytes_enc": encrypt_bytes(session_bytes),
                    "final_info":              safe_info,
                    "updated_at":              _now(),
                },
                "$push": {"timeline": {"state": FINAL_SESSION_UPLOADED, "at": _now()}},
            },
        )
    except Exception as exc:
        # This is the one checkpoint where a failure genuinely matters —
        # log loudly, but still never raise into the pipeline.
        _log.error("state_store.save_final_session FAILED for %s — crash-safety checkpoint lost: %s", phone, exc)


async def mark_completed(phone: str) -> None:
    """Account fully persisted to session_accounts — drop its checkpoint
    doc, it no longer needs to exist."""
    try:
        await collection(_COLLECTION).delete_one({"_id": phone})
    except Exception as exc:
        _log.warning("state_store.mark_completed cleanup failed for %s: %s", phone, exc)


async def mark_failed(phone: str, error: str) -> None:
    try:
        await collection(_COLLECTION).update_one(
            {"_id": phone},
            {
                "$set":  {"state": FAILED, "error": error, "updated_at": _now()},
                "$push": {"timeline": {"state": FAILED, "at": _now()}},
            },
        )
    except Exception as exc:
        _log.warning("state_store.mark_failed failed for %s: %s", phone, exc)


async def mark_needs_2fa(phone: str) -> None:
    await advance(phone, NEEDS_2FA)


async def mark_needs_country(phone: str) -> None:
    await advance(phone, NEEDS_COUNTRY)


async def clear(phone: str) -> None:
    try:
        await collection(_COLLECTION).delete_one({"_id": phone})
    except Exception as exc:
        _log.warning("state_store.clear failed for %s: %s", phone, exc)


async def get_timeline(phone: str) -> Optional[dict]:
    """Read-only lookup for the /account_state admin command."""
    try:
        return await collection(_COLLECTION).find_one({"_id": phone})
    except Exception as exc:
        _log.warning("state_store.get_timeline failed for %s: %s", phone, exc)
        return None


def decrypt_session(enc: bytes) -> bytes:
    return decrypt_bytes(enc)


def decrypt_admin_pass(enc: str) -> str:
    return enc or ""


async def resume_incomplete() -> dict:
    """
    Called once at bot startup. For every account whose last known state is
    not COMPLETED/FAILED:

      - state == FINAL_SESSION_UPLOADED: the finished session was already
        checkpointed before any crash — finish the DB save directly from
        the checkpoint, no Telegram calls needed at all.
      - state in RESUMABLE_FROM_SCRATCH (anything before the self-logout
        point, including the two NEEDS_* pause states): the originally
        uploaded session is still guaranteed intact (never logged out) —
        safe to just re-run the whole pipeline for that account.
      - anything else (state == COMPLETED/FAILED — shouldn't normally be
        queried here, or an unrecognized state): left untouched.

    Returns a summary dict for startup logging. Never raises — this must
    never prevent the bot itself from starting.
    """
    summary = {"finalized_from_checkpoint": 0, "rerun_from_scratch": 0, "skipped": 0, "errors": 0}
    try:
        docs = [doc async for doc in collection(_COLLECTION).find({})]
    except Exception as exc:
        _log.warning("resume_incomplete: could not query %s: %s", _COLLECTION, exc)
        return summary

    for doc in docs:
        phone = doc["_id"]
        state = doc.get("state")
        try:
            if state == FINAL_SESSION_UPLOADED and doc.get("final_session_bytes_enc"):
                await _finish_from_checkpoint(doc)
                summary["finalized_from_checkpoint"] += 1
            elif state in RESUMABLE_FROM_SCRATCH and doc.get("session_bytes_enc"):
                await _rerun_from_scratch(doc)
                summary["rerun_from_scratch"] += 1
            else:
                summary["skipped"] += 1
        except Exception as exc:
            summary["errors"] += 1
            _log.error("resume_incomplete: failed to resume %s (state=%s): %s", phone, state, exc)

    if any(summary.values()):
        _log.info(
            "Pipeline resume-on-restart: %d finalized from checkpoint, %d re-run from scratch, "
            "%d needing admin input left as-is, %d errors.",
            summary["finalized_from_checkpoint"], summary["rerun_from_scratch"],
            summary["skipped"], summary["errors"],
        )
    return summary


async def _finish_from_checkpoint(doc: dict) -> None:
    """Recover an account whose final session was checkpointed but whose
    channel-upload + DB-save never completed (process died right after)."""
    from server.stock.persistence import upload_session_and_build_record
    from server.core import memstore
    from server.utils.database.sessiondb import add_session_account, get_session_by_phone

    phone        = doc["_id"]
    channel_id   = doc["channel_id"]
    country_code = doc.get("country_code")
    final_info   = dict(doc.get("final_info") or {})
    final_info["fresh_session_bytes"] = decrypt_bytes(doc["final_session_bytes_enc"])

    real_phone = final_info.get("phone") or phone
    if await get_session_by_phone(real_phone):
        _log.info("resume: %s already saved — dropping stale checkpoint.", real_phone)
        await clear(phone)
        return

    country_doc  = memstore.get_country(country_code) if country_code else None
    country_name = country_doc["country_name"] if country_doc else (country_code or "unknown")

    from server import bot as _bot_module  # the running Client instance
    doc_built = await upload_session_and_build_record(
        _bot_module.bot, real_phone, final_info, channel_id, country_code, country_name,
    )
    await add_session_account(doc_built)
    await clear(phone)
    _log.info("resume: finalized %s from crash-safety checkpoint (no Telegram calls needed).", real_phone)


async def _rerun_from_scratch(doc: dict) -> None:
    """The uploaded session was never logged out — safe to just process it
    again from the beginning with the exact same inputs."""
    from server.stock.pipeline import process_uploaded_session
    from server.stock.persistence import save_verified_stock
    from server.utils.database.sessiondb import get_session_by_phone
    import config

    phone = doc["_id"]
    if await get_session_by_phone(phone):
        await clear(phone)
        return

    session_bytes  = decrypt_bytes(doc["session_bytes_enc"])
    admin_2fa_pass = decrypt_admin_pass(doc.get("admin_2fa_pass_enc") or "")
    country_code   = doc.get("country_code") or "XX"

    if country_code == "XX":
        # Still needs a country — nothing to auto-resume, just re-register
        # the pending entry so /pending_country and the tap buttons work
        # again after the restart. Handled by session_admin's own rehydrate.
        return

    from server import bot as _bot_module

    info = await process_uploaded_session(
        session_bytes, config.API_ID, config.API_HASH, country_code, admin_2fa_pass,
        old_api_id=doc.get("api_id_override"),
        old_api_hash=doc.get("api_hash_override"),
        batch_action=doc.get("batch_action", "disable"),
        batch_new_password=doc.get("batch_new_password"),
        state_key=phone,
    )

    if info.get("needs_admin_password") or info.get("tfa_password_wrong") or not info.get("success"):
        # Still can't finish automatically — leave the checkpoint in place,
        # it will show up under /pending_2fa etc via rehydrate.
        return

    real_phone = info.get("phone") or phone
    channel_id = doc["channel_id"]
    await save_verified_stock(_bot_module.bot, real_phone, info, channel_id, country_code_override=country_code)
    await clear(phone)
    _log.info("resume: %s reprocessed from scratch successfully after restart.", real_phone)
