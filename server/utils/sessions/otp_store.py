"""
Shared in-memory OTP store — used by BOTH the bot plugin and the REST API.

Lifecycle:
  1. POST /api/v1/orders (or POST /numbers/buy)
       → otp_store.create(phone, account_id, password, owner_id, order_id)
       + background task starts Telethon OTP listener
  2. Listener fires     → otp_store.set_otp(phone, otp_code)
  3. Poll by phone      → otp_store.get(phone)
     Poll by order_id   → otp_store.get_by_order_id(order_id)
  4. Either side calls  → otp_store.clear(phone) when done or timed out

State machine per entry:
  waiting  → ready (OTP received) or timeout (expires_at passed)
  ready    → auto-cleared after READY_TTL_SECONDS to prevent stale index

Timeout tombstones:
  When an entry times out it is evicted from _store, but its order_id is
  kept in _timeout_tombstones for TOMBSTONE_TTL_SECONDS.  This ensures
  that a client which polls *again* after the first 408 response receives
  another 408 (not a confusing 404 "order not found").
"""

import time
from typing import Optional

# phone (str, with leading +) → state dict
_store: dict[str, dict] = {}

# order_id → phone  (reverse index; kept strictly in sync with _store)
_order_index: dict[str, str] = {}

# order_id → eviction_time  (tombstone for timed-out orders)
# Ensures re-polls after timeout return 408, not 404.
_timeout_tombstones: dict[str, float] = {}

OTP_TIMEOUT_SECONDS  = 180   # 3 min — no OTP received
READY_TTL_SECONDS    = 600   # 10 min — auto-clear after OTP is delivered
TOMBSTONE_TTL_SECONDS = 300  # 5 min — keep timeout tombstone for re-polls


def create(
    phone: str,
    account_id: str,
    password: str = "",
    timeout: int = OTP_TIMEOUT_SECONDS,
    owner_id: int = 0,
    order_id: str = "",
):
    """Register a pending OTP slot, replacing any existing entry for this phone."""
    key = phone if phone.startswith("+") else f"+{phone}"

    # ── Clean up any prior entry for this phone ───────────────────────────
    prior = _store.get(key)
    if prior and prior.get("order_id"):
        _order_index.pop(prior["order_id"], None)

    now = time.time()
    _store[key] = {
        "account_id":  account_id,
        "password":    password,
        "otp":         None,
        "created_at":  now,
        "expires_at":  now + timeout,
        "done":        False,
        "done_at":     None,   # timestamp when OTP was set (for READY_TTL)
        "owner_id":    owner_id,
        "order_id":    order_id,
    }
    if order_id:
        _order_index[order_id] = key
        # Remove any stale tombstone for this order_id (re-use after retry)
        _timeout_tombstones.pop(order_id, None)


def set_otp(phone: str, otp: str):
    """Called by the Telethon listener when OTP arrives."""
    key = phone if phone.startswith("+") else f"+{phone}"
    if key in _store:
        _store[key]["otp"]     = otp
        _store[key]["done"]    = True
        _store[key]["done_at"] = time.time()


def _resolve(key: str) -> Optional[dict]:
    """
    Core state machine — returns one of:
      None                     → entry unknown (never created or already cleared)
      {"status": "waiting"}    → listener running, no OTP yet
      {"status": "ready", ...} → OTP received; auto-clears after READY_TTL_SECONDS
      {"status": "timeout"}    → expired while waiting; entry removed
      {"status": "expired"}    → ready TTL elapsed; OTP was set but not polled in time
    """
    state = _store.get(key)
    if not state:
        return None

    now = time.time()

    if state["done"]:
        # Auto-clear ready state after READY_TTL to prevent stale index.
        # Return "expired" (not "timeout") — OTP WAS received, just too late to poll.
        if now - (state["done_at"] or now) > READY_TTL_SECONDS:
            _evict(key)
            return {"status": "expired"}
        return {
            "status":   "ready",
            "otp":      state["otp"],
            "password": state["password"],
            "order_id": state["order_id"],
        }

    if now > state["expires_at"]:
        order_id = state.get("order_id", "")
        _evict(key)
        # Plant a tombstone so re-polls get 408 instead of 404
        if order_id:
            _timeout_tombstones[order_id] = now
        return {"status": "timeout"}

    return {"status": "waiting"}


def _evict(key: str):
    """Remove entry from both _store and _order_index."""
    state = _store.pop(key, None)
    if state and state.get("order_id"):
        _order_index.pop(state["order_id"], None)


def _purge_stale_tombstones():
    """Evict tombstones older than TOMBSTONE_TTL_SECONDS (called lazily)."""
    cutoff = time.time() - TOMBSTONE_TTL_SECONDS
    stale = [oid for oid, t in _timeout_tombstones.items() if t < cutoff]
    for oid in stale:
        _timeout_tombstones.pop(oid, None)


def get(phone: str) -> Optional[dict]:
    """Look up OTP state by phone number."""
    key = phone if phone.startswith("+") else f"+{phone}"
    return _resolve(key)


def get_by_order_id(order_id: str) -> Optional[dict]:
    """
    Look up OTP state by order_id instead of phone number.

    If the entry has already timed out and been evicted, the tombstone
    mechanism returns {"status": "timeout"} so callers can still return
    a proper 408 instead of a confusing 404.
    """
    key = _order_index.get(order_id)
    if key:
        return _resolve(key)

    # Entry not in active store — check tombstone (timeout within last 5 min)
    if order_id in _timeout_tombstones:
        _purge_stale_tombstones()
        if order_id in _timeout_tombstones:  # still there after purge
            return {"status": "timeout"}

    return None


def get_phone_by_order_id(order_id: str) -> Optional[str]:
    """Return the phone (with +) registered for this order_id, or None."""
    return _order_index.get(order_id)


def clear(phone: str):
    key = phone if phone.startswith("+") else f"+{phone}"
    _evict(key)


def is_pending(phone: str) -> bool:
    key = phone if phone.startswith("+") else f"+{phone}"
    return key in _store and not _store[key]["done"]


def get_owner_id(phone: str) -> Optional[int]:
    """Return the user_id that purchased this number, or None if not found."""
    key = phone if phone.startswith("+") else f"+{phone}"
    state = _store.get(key)
    return state.get("owner_id") if state else None
