"""
Persisting verified stock accounts.

Two distinct paths:

1. BATCH path (run_stock_batch)
   upload_session_and_build_record()
     → uploads to Telegram (per-account, unavoidable I/O)
     → builds a DB record dict in RAM (no Mongo write yet)
     → caller queues it in BatchContext.pending_inserts
   bulk_persist_batch(ctx)
     → insert_many(ordered=False) for all accounts in the batch
     → on partial failure: retries only the failed docs (not duplicates)
     → after max inline retries: enqueues survivors in memstore retry queue
     → memstore updated for every doc once it is inserted OR safely enqueued
     → returns only after persistence is confirmed or retry-queued (batch
        is never marked complete while data is in a limbo state)

2. RESUME path (admin supplies /2fa_pass or /set_country for a single
   paused account)
   save_verified_stock()
     → upload to Telegram + immediate add_session_account()
     → same as the old single-account path, unchanged

Failure handling in bulk_persist_batch
---------------------------------------
  BulkWriteError (partial)
    • code 11000 (duplicate key) → treated as already-inserted, no retry
    • other codes               → collected for retry
  Network / timeout errors      → full remaining list retried
  Retry schedule                → 2 s → 4 s → 8 s → 16 s (4 inline attempts)
  Last-resort                   → each failed doc enqueued in memstore
                                  sync_write (background worker, up to 10
                                  more attempts with 60 s cap)
  memstore                      → updated for ALL docs once they are either
                                  inserted or safely enqueued, so RAM counts
                                  stay accurate across retries
"""

import asyncio
import secrets
from datetime import datetime, timezone
from typing import Optional

from pymongo.errors import BulkWriteError

from server import LOGGER
from server.core import memstore
from server.stock.country_detection import detect_country
from server.utils.sessions.channel_storage import upload_session_to_channel

_log = LOGGER(__name__)

# ── Retry constants ───────────────────────────────────────────────────────────
_BULK_MAX_INLINE_ATTEMPTS = 4   # attempts before handing off to sync_write
_BULK_INITIAL_BACKOFF_S   = 2   # seconds; doubles each attempt, capped at 16
_BULK_BACKOFF_CAP_S       = 16
_DUPLICATE_KEY_CODE       = 11000


def _gen_account_id() -> str:
    return f"ACC-{secrets.token_hex(6).upper()}"


# ── Shared helpers ────────────────────────────────────────────────────────────

async def _resolve_country(
    phone: str,
    country_code_override: Optional[str] = None,
) -> tuple[str, str]:
    """
    Return (country_code, country_name).

    Resolution order:
      1. `country_code_override` when the admin supplied /set_country.
      2. detect_country() for the phone number.

    country_name is always looked up from memstore so it matches the
    admin's configured display name (never a raw phonenumbers string).
    memstore is already in RAM — zero Mongo round-trip.
    """
    if country_code_override:
        code = country_code_override.upper()
    else:
        result = await detect_country(phone)
        code   = result.get("iso_code", "XX")

    country_doc  = memstore.get_country(code)
    country_name = country_doc["country_name"] if country_doc else code
    return code, country_name


def _build_account_doc(
    phone: str,
    info: dict,
    session_msg_id: int,
    session_chat_id: int,
    country_code: str,
    country_name: str,
    account_id: Optional[str] = None,
) -> dict:
    """
    Build the full session_account document dict that will be inserted into
    MongoDB.  Pure data transformation — no I/O.
    """
    return {
        "account_id":          account_id or _gen_account_id(),
        "phone":               phone,
        "country_code":        country_code,
        "country_name":        country_name,
        "tg_user_id":          info.get("user_id"),
        "username":            info.get("username"),
        "first_name":          info.get("first_name"),
        "session_msg_id":      session_msg_id,
        "session_chat_id":     session_chat_id,
        "password":            "",   # plain-text 2FA password is never stored
        "tfa_password_enc":    info.get("tfa_password_enc", ""),
        "has_2fa":             info.get("has_2fa", False),
        "tfa_updated":         info.get("tfa_updated", False),
        "spam_status":         info.get("spam_status", "unknown"),
        "verified":            True,
        "verification_status": "verified",
        "login_time":          datetime.now(timezone.utc),
        "proxy_used":          info.get("proxy_used", "none"),
        "api_id_used":         info.get("api_id_used"),
        "terminated_others":   info.get("terminated_others", False),
        "sold":                False,
        "sold_to":             None,
        "uploaded_at":         datetime.now(timezone.utc),
        "sold_at":             None,
    }


# ── Batch path ────────────────────────────────────────────────────────────────

async def upload_session_and_build_record(
    bot_client,
    phone: str,
    info: dict,
    channel_id: int,
    country_code: str,
    country_name: str,
) -> dict:
    """
    Upload the final session bytes to the Telegram storage channel and
    build (but do NOT insert) the session_account document.

    Returns the fully-populated document dict ready for BatchContext.add_pending_insert().

    Raises RuntimeError if the pipeline reported success but produced no
    session bytes — we refuse to store a mismatched record.
    """
    upload_bytes = info.get("fresh_session_bytes")
    if not upload_bytes:
        raise RuntimeError(
            f"Pipeline reported success for {phone} but produced no final "
            f"session bytes — refusing to upload/store a mismatched session."
        )

    upload_ref = await upload_session_to_channel(bot_client, channel_id, phone, upload_bytes)
    account_id = _gen_account_id()
    doc        = _build_account_doc(
        phone, info, upload_ref.message_id, upload_ref.chat_id, country_code, country_name,
        account_id=account_id,
    )
    return doc


def _split_bulk_write_error(
    bwe: BulkWriteError,
    docs: list[dict],
) -> tuple[int, list[dict], list[dict]]:
    """
    Parse a BulkWriteError (ordered=False) into three buckets:

    Returns:
        newly_inserted  — count of docs that MongoDB accepted in this attempt
        to_retry        — docs that failed with a non-duplicate error (should retry)
        duplicates      — docs that failed with code 11000 (already in DB, skip)

    With ordered=False, MongoDB inserts every doc it can and reports errors
    for the rest. The error's ``index`` field is the position in the list
    that was passed to insert_many — i.e. into `docs`.
    """
    write_errors = bwe.details.get("writeErrors", [])
    error_indices: set[int] = set()
    duplicate_indices: set[int] = set()

    for err in write_errors:
        idx  = err["index"]
        code = err.get("code")
        error_indices.add(idx)
        if code == _DUPLICATE_KEY_CODE:
            duplicate_indices.add(idx)

    retry_indices = error_indices - duplicate_indices
    newly_inserted = len(docs) - len(error_indices)

    to_retry   = [docs[i] for i in sorted(retry_indices)]
    duplicates = [docs[i] for i in sorted(duplicate_indices)]
    return newly_inserted, to_retry, duplicates


def _enqueue_docs_for_background_retry(docs: list[dict]) -> None:
    """
    Hand off docs that exhausted all inline retry attempts to the memstore
    background retry worker (same exponential-backoff queue used for all
    async writes).  Each doc is enqueued as an independent upsert so a
    later partial success doesn't block the others.

    idempotency: we use update_one($setOnInsert) keyed on account_id, so
    re-running after a crash / duplicate never creates a second record.
    """
    from server.core.mongo import collection
    sessionaccountsdb = collection("session_accounts")

    for doc in docs:
        _doc = dict(doc)
        _aid = _doc.get("account_id")
        memstore.sync_write(
            lambda d=_doc, aid=_aid: sessionaccountsdb.update_one(
                {"account_id": aid},
                {"$setOnInsert": d},
                upsert=True,
            ),
            f"bulk_persist_retry:{_aid}",
        )


async def bulk_persist_batch(ctx) -> int:
    """
    Flush all pending session_account documents from BatchContext to MongoDB.

    Guarantee: this coroutine does NOT return until every document is either
    successfully inserted into MongoDB OR safely enqueued in the memstore
    background retry queue.  The batch is therefore never considered
    "complete" while any account record is in a limbo state.

    Failure handling
    ----------------
    BulkWriteError (partial failure with ordered=False):
      • code 11000 (duplicate key) → already in DB; treat as success, skip.
      • other error codes          → collected into a retry list.
    Any other exception (network, timeout, etc.):
      → entire remaining list is retried.

    Retry schedule (inline, this coroutine):
      attempt 1 → wait 2 s → attempt 2 → wait 4 s → attempt 3 →
      wait 8 s → attempt 4 (final inline attempt)
    After _BULK_MAX_INLINE_ATTEMPTS exhausted:
      → each remaining doc is handed to memstore.sync_write() (background
        worker, up to 10 more attempts, 60 s cap).

    memstore update
    ---------------
    Applied for ALL docs once they are inserted OR background-enqueued,
    so stock counts and the account index remain accurate throughout.
    Docs resolved as duplicates are NOT double-counted (they were already
    registered on their first insertion).

    Returns the count of docs inserted in this call (excluding duplicates
    and background-queued docs).
    """
    from server.core.mongo import collection
    sessionaccountsdb = collection("session_accounts")

    all_docs = ctx.pending_inserts
    if not all_docs:
        return 0

    remaining     = list(all_docs)   # docs still needing insertion
    total_inserted = 0               # successfully written in this call
    duplicate_aids: set[str] = set() # account_ids already in DB (skip memstore)
    backoff = _BULK_INITIAL_BACKOFF_S

    for attempt in range(1, _BULK_MAX_INLINE_ATTEMPTS + 1):
        if not remaining:
            break

        try:
            result = await sessionaccountsdb.insert_many(remaining, ordered=False)
            total_inserted += len(result.inserted_ids)
            remaining = []   # all done

        except BulkWriteError as bwe:
            newly_inserted, to_retry, duplicates = _split_bulk_write_error(bwe, remaining)
            total_inserted += newly_inserted

            dup_ids = {d.get("account_id") for d in duplicates if d.get("account_id")}
            duplicate_aids.update(dup_ids)

            if duplicates:
                _log.info(
                    "bulk_persist: %d duplicate(s) skipped (already in DB): %s",
                    len(duplicates),
                    [d.get("phone", "?") for d in duplicates],
                )
            if to_retry:
                _log.warning(
                    "bulk_persist: attempt %d/%d — %d doc(s) failed with "
                    "non-duplicate errors, will retry: phones=%s",
                    attempt, _BULK_MAX_INLINE_ATTEMPTS,
                    len(to_retry),
                    [d.get("phone", "?") for d in to_retry],
                )
            remaining = to_retry

        except Exception as exc:
            _log.warning(
                "bulk_persist: attempt %d/%d — full failure (%s): %d doc(s) "
                "remaining, will retry.",
                attempt, _BULK_MAX_INLINE_ATTEMPTS, exc, len(remaining),
            )
            # remaining stays as-is; we retry the whole list

        if remaining and attempt < _BULK_MAX_INLINE_ATTEMPTS:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BULK_BACKOFF_CAP_S)

    # Docs that survived all inline attempts → background retry queue
    if remaining:
        _log.error(
            "bulk_persist: %d doc(s) could not be inserted after %d attempts "
            "— handing off to background retry worker. phones=%s",
            len(remaining), _BULK_MAX_INLINE_ATTEMPTS,
            [d.get("phone", "?") for d in remaining],
        )
        _enqueue_docs_for_background_retry(remaining)

    # ── Update memstore ───────────────────────────────────────────────────────
    # All docs that were inserted OR background-queued get memstore entries.
    # Duplicates are excluded — they were counted on their original insertion.
    for doc in all_docs:
        aid = doc.get("account_id")
        if aid in duplicate_aids:
            continue
        cc   = doc.get("country_code", "XX")
        spam = doc.get("spam_status", "unknown")
        memstore.inc_stock(cc, spam)
        if aid:
            memstore.register_account(aid, cc, spam)

    return total_inserted


# ── Resume path (single account) ──────────────────────────────────────────────

async def save_verified_stock(
    bot_client,
    phone: str,
    info: dict,
    channel_id: int,
    country_code_override: Optional[str] = None,
) -> str:
    """
    Single-account save used by the resume flow (/2fa_pass, /set_country).
    Uploads to Telegram and writes to MongoDB immediately (no deferral).
    Returns the new account_id.
    """
    from server.utils.database.sessiondb import add_session_account

    real_phone   = info.get("phone") or phone
    country_code, country_name = await _resolve_country(real_phone, country_code_override)

    upload_bytes = info.get("fresh_session_bytes")
    if not upload_bytes:
        raise RuntimeError(
            f"Pipeline reported success for {real_phone} but produced no "
            f"final session bytes — refusing to store/upload a mismatched session."
        )

    upload_ref = await upload_session_to_channel(bot_client, channel_id, real_phone, upload_bytes)

    return await add_session_account(
        phone=real_phone,
        country_code=country_code,
        country_name=country_name,
        session_msg_id=upload_ref.message_id,
        session_chat_id=upload_ref.chat_id,
        password="",
        tg_user_id=info.get("user_id"),
        username=info.get("username"),
        first_name=info.get("first_name"),
        has_2fa=info.get("has_2fa", False),
        tfa_updated=info.get("tfa_updated", False),
        tfa_password_enc=info.get("tfa_password_enc", ""),
        spam_status=info.get("spam_status", "unknown"),
        verified=True,
        verification_status="verified",
        login_time=datetime.now(timezone.utc),
        proxy_used=info.get("proxy_used", "none"),
        api_id_used=info.get("api_id_used"),
        terminated_others=info.get("terminated_others", False),
    )
