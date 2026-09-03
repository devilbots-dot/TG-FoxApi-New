"""
Concurrent batch processing of a whole admin upload (a .zip's worth of
.session files, or a single .session file treated as a batch of one).

In-memory execution model
--------------------------
  1. Pre-load  — before any account starts, one $in query fetches which
                 phones in this batch already exist in MongoDB.  All
                 subsequent duplicate checks are pure RAM lookups.
  2. Process   — every account runs its full pipeline (Telegram I/O only)
                 and uploads its session to the storage channel.  No MongoDB
                 reads or writes happen during this phase.
  3. Build     — each successful account builds its DB document in RAM and
                 queues it in BatchContext.pending_inserts.
  4. Bulk-persist — after all accounts finish, one insert_many flushes the
                 entire batch to MongoDB, followed by a single memstore pass.

Country lookups are served from memstore (already in RAM — zero Mongo
round-trips even for the per-account country resolution step).

This is the single implementation shared by the bot upload handler
(server/plugins/bot/session_admin.py) and the REST admin API
(server/api/routes/admin.py).
"""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from server import LOGGER
from server.stock.batch_context import BatchContext
from server.stock.country_detection import detect_country
from server.stock.persistence import (
    bulk_persist_batch,
    save_verified_stock,
    upload_session_and_build_record,
)
from server.stock.pipeline import process_uploaded_session
from server.stock.session_validation import check_duplicate_stock

_log = LOGGER(__name__)

DEFAULT_CONCURRENCY = max(1, int(os.getenv("SESSION_PIPELINE_CONCURRENCY", "200")))
GLOBAL_CONCURRENCY  = max(1, int(os.getenv("SESSION_PIPELINE_GLOBAL_CONCURRENCY", "200")))
_global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)


@dataclass
class StockAccountOutcome:
    """
    Structured result for one account in a batch — not pre-formatted,
    so bot and API callers can each render it for their own surface.

    status values:
      "verified" | "invalid" | "duplicate" | "needs_2fa" | "needs_country" |
      "frozen"   | "failed"
    """
    index:         int
    phone:         str
    status:        str
    info:          Optional[dict] = None
    account_id:    Optional[str]  = None
    error:         Optional[str]  = None
    pending_entry: Optional[dict] = field(default=None)


# ── Pre-load ──────────────────────────────────────────────────────────────────

async def _preload_existing_phones(phones: list[str]) -> set[str]:
    """
    Single MongoDB $in query scoped to exactly the phones in this batch.
    Returns the subset of those phones that are already in sessiondb.

    Scoping to the batch keeps the query tiny regardless of how large the
    session_accounts collection grows.
    """
    from server.core.mongo import collection
    sessionaccountsdb = collection("session_accounts")
    existing: set[str] = set()
    try:
        async for doc in sessionaccountsdb.find(
            {"phone": {"$in": phones}},
            {"phone": 1, "_id": 0},
        ):
            existing.add(doc["phone"])
    except Exception as exc:
        _log.warning("pre-load existing phones failed (%s) — dedup will be best-effort", exc)
    return existing


# ── Per-account worker ────────────────────────────────────────────────────────

async def process_one_stock_session(
    index: int,
    session_entry: dict,
    channel_id: int,
    api_id: int,
    api_hash: str,
    batch_action: str,
    batch_new_password: Optional[str],
    bot_client,
    ctx: BatchContext,
) -> StockAccountOutcome:
    """
    Run one uploaded session through the full pipeline, using ctx for all
    state that would otherwise require a MongoDB round-trip.

    session_entry keys:
        phone, session_bytes, password,
        api_id_override (int | None), api_hash_override (str | None)
    """
    phone             = session_entry["phone"]
    raw               = session_entry["session_bytes"]
    admin_2fa_pass    = session_entry.get("password", "")
    api_id_override   = session_entry.get("api_id_override")
    api_hash_override = session_entry.get("api_hash_override")

    # ── Duplicate check — pure RAM (ctx.claim_phone) ──────────────────────
    if await check_duplicate_stock(phone, ctx=ctx):
        return StockAccountOutcome(index, phone, "duplicate")

    # ── Country detection — served from memstore, falls back to phonenumbers lib
    _country = await detect_country(phone)
    country_code = _country.get("iso_code", "XX")
    if country_code == "XX":
        pending_entry = {
            "session_bytes":      raw,
            "channel_id":         channel_id,
            "admin_2fa_pass":     admin_2fa_pass,
            "api_id_override":    api_id_override,
            "api_hash_override":  api_hash_override,
            "batch_action":       batch_action,
            "batch_new_password": batch_new_password,
        }
        return StockAccountOutcome(index, phone, "needs_country", pending_entry=pending_entry)

    # ── Full Telegram pipeline ─────────────────────────────────────────────
    try:
        info = await process_uploaded_session(
            raw, api_id, api_hash, country_code, admin_2fa_pass,
            old_api_id=api_id_override,
            old_api_hash=api_hash_override,
            batch_action=batch_action,
            batch_new_password=batch_new_password,
        )
    except Exception as exc:
        return StockAccountOutcome(index, phone, "invalid", error=f"pipeline error: {exc}")

    if info.get("frozen"):
        return StockAccountOutcome(index, phone, "frozen", info=info)

    pending_entry = {
        "session_bytes":      raw,
        "channel_id":         channel_id,
        "country_code":       country_code,
        "api_id_override":    api_id_override,
        "api_hash_override":  api_hash_override,
        "batch_action":       batch_action,
        "batch_new_password": batch_new_password,
    }

    if info.get("needs_admin_password"):
        return StockAccountOutcome(index, phone, "needs_2fa", info=info, pending_entry=pending_entry)

    if info.get("tfa_password_wrong"):
        return StockAccountOutcome(index, phone, "needs_2fa", info=info, pending_entry=pending_entry)

    if not info.get("success"):
        return StockAccountOutcome(
            index, phone, "invalid",
            info=info, error=info.get("error") or "invalid session",
        )

    # ── Channel upload (Telegram I/O) + build DB record in RAM ───────────
    real_phone = info.get("phone") or phone
    try:
        from server.core import memstore
        country_doc  = memstore.get_country(country_code)
        country_name = country_doc["country_name"] if country_doc else country_code

        doc = await upload_session_and_build_record(
            bot_client, real_phone, info, channel_id,
            country_code, country_name,
        )
    except Exception as exc:
        info["outcome"] = "upload_failed"
        return StockAccountOutcome(
            index, phone, "failed",
            info=info, error=f"channel upload failed: {exc}",
        )

    # Queue for bulk insert — no MongoDB write yet
    await ctx.add_pending_insert(doc)

    account_id = doc["account_id"]
    return StockAccountOutcome(index, real_phone, "verified", info=info, account_id=account_id)


# ── Batch orchestrator ────────────────────────────────────────────────────────

async def run_stock_batch(
    sessions: list[dict],
    channel_id: int,
    api_id: int,
    api_hash: str,
    batch_action: str,
    batch_new_password: Optional[str],
    bot_client,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> list[StockAccountOutcome]:
    """
    Process a whole batch, up to `concurrency` accounts at once.

    Execution model:
      1. Pre-load  — one $in query for all phones in this batch.
      2. Process   — concurrent Telegram pipelines + channel uploads.
                     No per-account MongoDB I/O.
      3. Bulk-persist — single insert_many for all verified accounts.

    `on_progress(done_count, total)` is awaited after each account finishes.
    Returns outcomes sorted by original submission order.
    """
    total = len(sessions)

    # ── Step 1: Pre-load ─────────────────────────────────────────────────────
    all_phones      = [s["phone"] for s in sessions]
    existing_phones = await _preload_existing_phones(all_phones)

    ctx = BatchContext(existing_phones=existing_phones)
    _log.info(
        "Batch %s started — %d accounts, %d already in DB, concurrency=%d",
        ctx.batch_id, total, len(existing_phones), concurrency,
    )

    sem            = asyncio.Semaphore(max(1, concurrency))
    done_count     = 0
    progress_lock  = asyncio.Lock()

    # ── Step 2: Process ──────────────────────────────────────────────────────
    async def _run(i: int, entry: dict) -> StockAccountOutcome:
        nonlocal done_count
        async with sem, _global_sem:
            try:
                return await process_one_stock_session(
                    i, entry, channel_id, api_id, api_hash,
                    batch_action, batch_new_password, bot_client, ctx,
                )
            finally:
                if on_progress is not None:
                    async with progress_lock:
                        done_count += 1
                        try:
                            await on_progress(done_count, total)
                        except Exception:
                            pass

    outcomes = await asyncio.gather(*(_run(i, s) for i, s in enumerate(sessions, 1)))
    outcomes = sorted(outcomes, key=lambda o: o.index)

    # ── Step 3: Bulk-persist ─────────────────────────────────────────────────
    inserted = await bulk_persist_batch(ctx)
    _log.info(
        "Batch %s complete — %d verified, %d inserted to DB in %.2fs",
        ctx.batch_id, inserted, inserted, ctx.elapsed(),
    )

    return outcomes


# ── Benchmark helper ─────────────────────────────────────────────────────────

def build_batch_benchmark(outcomes: list[StockAccountOutcome], wall_time: float) -> dict:
    """
    Aggregate per-account stage_timings/total_time into a batch-level
    performance report.  Returns {} if no account carried timing data.
    """
    per_account_totals: list[float] = []
    stage_totals: dict[str, list[float]] = {}

    for o in outcomes:
        info  = o.info or {}
        total = info.get("total_time")
        if total:
            per_account_totals.append(total)
        for stage, secs in (info.get("stage_timings") or {}).items():
            stage_totals.setdefault(stage, []).append(secs)

    if not per_account_totals:
        return {}

    n               = len(per_account_totals)
    avg_per_account = sum(per_account_totals) / n
    stage_avgs      = {
        s: sum(v) / len(v) for s, v in stage_totals.items() if v
    }
    slowest_stage   = max(stage_avgs.items(), key=lambda kv: kv[1]) if stage_avgs else None
    fastest_stage   = min(stage_avgs.items(), key=lambda kv: kv[1]) if stage_avgs else None
    throughput      = (len(outcomes) / wall_time) * 60 if wall_time > 0 else 0.0

    return {
        "accounts_timed":     n,
        "avg_per_account":    round(avg_per_account, 2),
        "slowest_account":    round(max(per_account_totals), 2),
        "fastest_account":    round(min(per_account_totals), 2),
        "slowest_stage":      slowest_stage,
        "fastest_stage":      fastest_stage,
        "stage_avgs":         {k: round(v, 2) for k, v in stage_avgs.items()},
        "total_batch_time":   round(wall_time, 2),
        "throughput_per_min": round(throughput, 2),
    }
