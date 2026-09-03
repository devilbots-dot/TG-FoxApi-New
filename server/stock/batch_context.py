"""
BatchContext — in-memory execution context for one admin upload batch.

A single BatchContext is created per admin upload (whether a .zip or a
single .session file) and shared across all concurrent account pipelines
running within that batch.

Design goals
------------
* Zero per-account MongoDB reads during processing — everything needed
  is pre-loaded into RAM before the first account starts.
* Zero per-account MongoDB writes during processing — DB records are
  built in RAM and flushed in a single bulk_write at the very end.
* Intra-batch dedup — a phone claimed by one account in the batch cannot
  be double-counted by another account that races ahead.
* Channel uploads (Telegram) remain per-account — they are I/O against
  Telegram, not MongoDB, and the msg_id they return is needed to build
  the DB record.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


def _gen_batch_id() -> str:
    return f"BATCH-{secrets.token_hex(4).upper()}"


@dataclass
class BatchContext:
    """
    Lifetime: created once per upload call in run_stock_batch(), passed to
    every process_one_stock_session() call, and used by bulk_persist_batch()
    at the very end.

    Thread/task safety
    ------------------
    All mutable shared fields are protected by asyncio locks. The locks are
    created inside __post_init__ so they always belong to the running event
    loop (avoids the "Future attached to a different loop" error that would
    occur if they were default_factory'd at import time).
    """

    batch_id: str = field(default_factory=_gen_batch_id)

    # ── Pre-loaded before processing starts ──────────────────────────────────
    # Set of phone numbers already in sessiondb, scoped to exactly the phones
    # present in this batch (populated via a single $in query, not a full
    # collection scan).
    existing_phones: set[str] = field(default_factory=set)

    # ── Intra-batch dedup ─────────────────────────────────────────────────────
    # Phones that have already been "claimed" by an account that passed the
    # dedup gate in this batch. Prevents a race where two copies of the same
    # phone inside a single zip both make it past the duplicate check.
    batch_phones: set[str] = field(default_factory=set)

    # ── Pending DB inserts ───────────────────────────────────────────────────
    # Built up during processing; flushed in one insert_many at the end.
    # Each element is a fully-formed session_account document dict (same
    # schema as sessiondb.add_session_account would produce), ready to insert.
    pending_inserts: list[dict] = field(default_factory=list)

    # Locks — initialised in __post_init__ to bind to the running loop.
    _batch_phones_lock: asyncio.Lock = field(init=False)
    _pending_lock: asyncio.Lock      = field(init=False)

    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self):
        self._batch_phones_lock = asyncio.Lock()
        self._pending_lock      = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def claim_phone(self, phone: str) -> bool:
        """
        Atomically check dedup + claim a phone for this batch.
        Returns True  → phone is new (not in DB, not already claimed here).
        Returns False → duplicate; caller should return a "duplicate" outcome.

        On True, the phone is immediately added to batch_phones so no other
        concurrent pipeline can claim it before this one finishes.
        """
        async with self._batch_phones_lock:
            if phone in self.existing_phones or phone in self.batch_phones:
                return False
            self.batch_phones.add(phone)
            return True

    async def add_pending_insert(self, doc: dict) -> None:
        """Queue a fully-built session_account document for the bulk insert."""
        async with self._pending_lock:
            self.pending_inserts.append(doc)

    def elapsed(self) -> float:
        """Seconds elapsed since this batch context was created."""
        return time.monotonic() - self.started_at
