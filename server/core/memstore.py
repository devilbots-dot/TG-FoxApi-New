"""
In-memory store — RAM-first runtime architecture.

Startup : load() pulls all hot collections from MongoDB into RAM once.
Reads   : served entirely from RAM — zero MongoDB round-trips on the hot path.
Writes  : RAM updated immediately; MongoDB synced asynchronously via sync_write().
Retry   : failed Mongo writes queued and retried with exponential backoff.
Recovery: server restart → load() rebuilds RAM from MongoDB automatically.

Collections kept in RAM:
  admin_config     → settings dict
  countries        → _countries dict (code → doc)
  proxies          → _proxies dict  (proxy_id → doc)
  sudoers          → sudoers list
  users.is_banned  → banned set
  session_accounts → stock_counts + _account_index (lightweight metadata)
  users (hot)      → _user_cache (per-user, TTL-gated)

Collections kept MongoDB-only (financial integrity, atomicity required):
  transactions, orders, deposits, withdrawals, audit_logs, sell_requests
  balance fields (always written + read via MongoDB atomic ops)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Optional

_log = logging.getLogger("memstore")

# ── Retry queue ───────────────────────────────────────────────────────────────
_retry_queue: Optional[asyncio.Queue] = None
_retry_task:  Optional[asyncio.Task]  = None

_MAX_RETRIES  = 10
# Backoff delays per attempt (seconds): 2 4 8 16 32 then capped at 60
_RETRY_DELAYS = [2, 4, 8, 16, 32, 60, 60, 60, 60, 60]


# ── Settings (admin_config) ───────────────────────────────────────────────────
settings: dict[str, Any] = {}


# ── Countries ─────────────────────────────────────────────────────────────────
# Source of truth: _countries[code] = doc (code is always uppercase)
_countries: dict[str, dict] = {}


def get_countries(buy_only: bool = False, sell_only: bool = False) -> list[dict]:
    """Return a sorted-by-rank copy of countries, with optional filters."""
    out = []
    for doc in _countries.values():
        if buy_only  and doc.get("temp_disable"):
            continue
        if sell_only and doc.get("is_full"):
            continue
        out.append(dict(doc))
    out.sort(key=lambda d: d.get("country_rank", 9999))
    return out


def get_country(code: str) -> Optional[dict]:
    doc = _countries.get(code.upper())
    return dict(doc) if doc else None


def set_country(doc: dict) -> None:
    code = str(doc.get("code", "")).upper()
    if code:
        _countries[code] = dict(doc)


def del_country(code: str) -> None:
    _countries.pop(code.upper(), None)


def get_next_country_rank() -> int:
    if not _countries:
        return 1
    return max((d.get("country_rank", 0) for d in _countries.values()), default=0) + 1


# ── Proxies ───────────────────────────────────────────────────────────────────
_proxies: dict[str, dict] = {}   # {proxy_id: doc}


def get_proxy_for_country(country_code: str) -> Optional[dict]:
    """Best active proxy: lowest fail_count, then oldest. Falls back to wildcard *."""
    code = country_code.upper()

    def _best(pool: list[dict]) -> Optional[dict]:
        active = [p for p in pool if p.get("is_active")]
        if not active:
            return None
        active.sort(key=lambda p: (p.get("fail_count", 0), str(p.get("added_at", ""))))
        return dict(active[0])

    specific = [p for p in _proxies.values() if p.get("country_code") == code]
    result   = _best(specific)
    if result:
        return result
    wildcard = [p for p in _proxies.values() if p.get("country_code") == "*"]
    return _best(wildcard)


def get_proxies_for_country(country_code: str) -> list[dict]:
    code = country_code.upper()
    out  = [dict(p) for p in _proxies.values() if p.get("country_code") == code]
    out.sort(key=lambda p: str(p.get("added_at", "")), reverse=True)
    return out


def get_all_proxies() -> list[dict]:
    out = list(_proxies.values())
    out.sort(key=lambda p: (p.get("country_code", ""), str(p.get("added_at", ""))), reverse=False)
    return [dict(p) for p in out]


def set_proxy(doc: dict) -> None:
    pid = doc.get("proxy_id", "")
    if pid:
        _proxies[pid] = dict(doc)


def del_proxy(proxy_id: str) -> None:
    _proxies.pop(proxy_id, None)


def update_proxy_field(proxy_id: str, **kwargs: Any) -> None:
    if proxy_id in _proxies:
        _proxies[proxy_id].update(kwargs)


# ── Sudoers ───────────────────────────────────────────────────────────────────
sudoers: list[int] = []


# ── Banned users ──────────────────────────────────────────────────────────────
banned: set[int] = set()


# ── Stock counts ──────────────────────────────────────────────────────────────
# {country_code_upper: {"total": int, "clean": int, "sellable": int}}
# "sellable" mirrors the buyer pool: clean + temporary_spam + permanent_spam.
# Frozen/dead/unknown rows stay visible to admins but never create buyer stock.
stock_counts: dict[str, dict[str, int]] = {}

_SELLABLE_SPAM_STATUSES = frozenset({"clean", "temporary_spam", "permanent_spam"})

# Lightweight account index for maintaining stock_counts on sell/revert
# {account_id: {"cc": str, "spam": str}}
_account_index: dict[str, dict[str, str]] = {}


def get_stock_count(country_code: str, clean_only: bool = True) -> int:
    entry = stock_counts.get(country_code.upper(), {})
    return entry.get("sellable" if clean_only else "total", 0)


def inc_stock(country_code: str, spam_status: str) -> None:
    cc = country_code.upper()
    if cc not in stock_counts:
        stock_counts[cc] = {"total": 0, "clean": 0, "sellable": 0}
    else:
        stock_counts[cc].setdefault("total", 0)
        stock_counts[cc].setdefault("clean", 0)
        stock_counts[cc].setdefault("sellable", stock_counts[cc].get("clean", 0))
    stock_counts[cc]["total"] += 1
    if spam_status == "clean":
        stock_counts[cc]["clean"] += 1
    if spam_status in _SELLABLE_SPAM_STATUSES:
        stock_counts[cc]["sellable"] += 1


def dec_stock(country_code: str, spam_status: str) -> None:
    cc = country_code.upper()
    if cc not in stock_counts:
        return
    stock_counts[cc]["total"] = max(0, stock_counts[cc].get("total", 0) - 1)
    if spam_status == "clean":
        stock_counts[cc]["clean"] = max(0, stock_counts[cc].get("clean", 0) - 1)
    if spam_status in _SELLABLE_SPAM_STATUSES:
        stock_counts[cc]["sellable"] = max(0, stock_counts[cc].get("sellable", 0) - 1)


def register_account(account_id: str, country_code: str, spam_status: str) -> None:
    _account_index[account_id] = {
        "cc":   country_code.upper(),
        "spam": spam_status,
    }


def unregister_account(account_id: str) -> None:
    _account_index.pop(account_id, None)


def get_account_meta(account_id: str) -> Optional[dict[str, str]]:
    return _account_index.get(account_id)


# ── User cache ───────────────────────────────────────────────────────────────
# Per-user full doc cache. Financial fields (balance, etc.) are always
# read from MongoDB directly; the cache is used for display/profile reads.
# Invalidated completely on any write that changes the user doc.
_user_cache: dict[int, tuple[dict, float]] = {}   # {user_id: (doc, loaded_at)}
_USER_CACHE_TTL = 120.0  # seconds


def get_cached_user(user_id: int) -> Optional[dict]:
    entry = _user_cache.get(user_id)
    if entry and (time.monotonic() - entry[1]) < _USER_CACHE_TTL:
        return dict(entry[0])
    return None


def cache_user(doc: dict) -> None:
    uid = doc.get("user_id")
    if uid is not None:
        _user_cache[uid] = (dict(doc), time.monotonic())


def invalidate_user(user_id: int) -> None:
    _user_cache.pop(user_id, None)


# ── Write-through sync ───────────────────────────────────────────────────────

def sync_write(factory: Callable[[], Coroutine], description: str = "") -> None:
    """
    Schedule a MongoDB write without blocking the caller.
    RAM is already updated before calling this.
    On Mongo failure → queued for automatic retry with backoff.
    """
    asyncio.create_task(_do_sync(factory, description, attempt=0))


async def _do_sync(
    factory:     Callable[[], Coroutine],
    description: str,
    attempt:     int,
) -> None:
    try:
        await factory()
    except Exception as exc:
        _log.warning(
            "Mongo sync failed [%s] attempt %d: %s",
            description, attempt + 1, exc,
        )
        if attempt < _MAX_RETRIES and _retry_queue is not None:
            await _retry_queue.put((factory, description, attempt + 1))
        else:
            _log.error(
                "Mongo sync permanently failed [%s] after %d attempts — "
                "data may be out of sync. Restart to recover.",
                description, attempt + 1,
            )


async def _retry_worker() -> None:
    """Background worker: drains the retry queue with per-attempt backoff."""
    assert _retry_queue is not None
    while True:
        try:
            factory, description, attempt = await _retry_queue.get()
            delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
            _log.info("Retrying Mongo sync [%s] in %ds (attempt %d)...", description, delay, attempt)
            await asyncio.sleep(delay)
            await _do_sync(factory, description, attempt)
            _retry_queue.task_done()
        except asyncio.CancelledError:
            _log.info("memstore: retry worker cancelled.")
            break
        except Exception as exc:
            _log.error("memstore: retry worker unexpected error: %s", exc)


# ── Startup loader ────────────────────────────────────────────────────────────

async def load() -> None:
    """
    Pull all hot data from MongoDB into RAM.
    Must be called once at startup, after mongo.reinit().
    Subsequent server restarts automatically rebuild RAM from MongoDB — no
    manual intervention required.
    """
    global _retry_queue, _retry_task

    from server.core.mongo import collection  # imported here to avoid circular import

    _log.info("memstore: loading hot data from MongoDB...")
    t0 = time.monotonic()

    # Config defaults (kept in sync with configdb.DEFAULTS)
    _config_defaults: dict[str, Any] = {
        "maintenance_mode":           False,
        "deposits_enabled":           True,
        "withdrawals_enabled":        True,
        "sell_requests_enabled":      True,
        "registration_enabled":       True,
        "deposit_methods":            {},
        # Sell timing (kept in sync with configdb.DEFAULTS)
        "termination_delay_minutes":  1,
        "payment_hold_hours":         48,
        "payment_hold_auto_term_hours": 48,
        # Worker thresholds (kept in sync with configdb.DEFAULTS)
        "low_stock_threshold":        5,
        "order_timeout_minutes":      30,
        "platform_fee_percent":       5.0,
        # Session-selling gate
        "session_selling_enabled":    True,
        "session_selling_min_rank":   "VIP1",
        # Rank / country discounts (dict)
        "rank_discounts":             {},
        "country_discounts":          {},
        # Sales feed
        "sales_feed_enabled":           False,
        "sales_feed_chat_id":           "",
        "sales_feed_silent":            False,
        "sales_feed_delay_seconds":     0,
        # Fake sales and fake-stock visibility
        "fake_sales_enabled":              False,
        "fake_sales_interval_min":         300,
        "fake_sales_interval_max":         900,
        "fake_sales_randomization_level":  5,
        "fake_sales_product_pool":         ["account", "session"],
        "fake_sales_country_pool":         [],
        "hide_fake_stock_when_real_zero":  False,
        "miniapp_enabled":                 True,
    }

    def _coerce_loaded_setting(key: str, value: Any) -> Any:
        default = _config_defaults.get(key)
        if isinstance(default, bool):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            if value is None:
                return default
            return bool(value)
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, dict):
            return value if isinstance(value, dict) else default
        return value

    # ── Settings ───────────────────────────────────────────────────────────────
    settings.clear()
    settings.update(_config_defaults)
    async for doc in collection("admin_config").find({}):
        key = doc["key"]
        settings[key] = _coerce_loaded_setting(key, doc.get("value"))

    # ── Countries ───────────────────────────────────────────────────────────────
    _countries.clear()
    async for doc in collection("countries").find({}).sort("country_rank", 1):
        doc.pop("_id", None)
        code = str(doc.get("code", "")).upper()
        if code:
            _countries[code] = doc

    # ── Proxies ─────────────────────────────────────────────────────────────────
    _proxies.clear()
    async for doc in collection("proxies").find({}):
        doc.pop("_id", None)
        pid = doc.get("proxy_id", "")
        if pid:
            _proxies[pid] = doc

    # ── Sudoers ─────────────────────────────────────────────────────────
    sudoers.clear()
    sdoc = await collection("sudoers").find_one({"sudo": "sudo"})
    if sdoc:
        sudoers.extend(sdoc.get("sudoers", []))

    # ── Banned users ────────────────────────────────────────────────────────
    banned.clear()
    async for doc in collection("users").find({"is_banned": True}, {"user_id": 1}):
        uid = doc.get("user_id")
        if uid is not None:
            banned.add(uid)

    # ── Stock counts + account index ───────────────────────────────────────
    stock_counts.clear()
    _account_index.clear()

    pipeline = [
        {
            "$match": {"sold": False, "inventory_state": {"$ne": "bin"}}
        },
        {
            "$group": {
                "_id":   "$country_code",
                "total": {"$sum": 1},
                "clean": {"$sum": {"$cond": [{"$eq": ["$spam_status", "clean"]}, 1, 0]}},
                "sellable": {"$sum": {"$cond": [
                    {"$in": ["$spam_status", ["clean", "temporary_spam", "permanent_spam"]]},
                    1,
                    0,
                ]}},
            }
        },
    ]
    async for doc in collection("session_accounts").aggregate(pipeline):
        cc = doc.get("_id", "")
        if cc:
            stock_counts[cc.upper()] = {
                "total": doc.get("total", 0),
                "clean": doc.get("clean", 0),
                "sellable": doc.get("sellable", 0),
            }

    # Lightweight account index (only unsold accounts needed)
    async for doc in collection("session_accounts").find(
        {"sold": False, "inventory_state": {"$ne": "bin"}},
        {"account_id": 1, "country_code": 1, "spam_status": 1},
    ):
        aid = doc.get("account_id", "")
        cc  = str(doc.get("country_code", "XX")).upper()
        sp  = doc.get("spam_status", "unknown")
        if aid:
            _account_index[aid] = {"cc": cc, "spam": sp}

    elapsed = round(time.monotonic() - t0, 3)
    _log.info(
        "memstore: loaded — %d settings | %d countries | %d proxies | "
        "%d sudoers | %d banned | %d stock buckets | %d accounts indexed — %.3fs",
        len(settings), len(_countries), len(_proxies),
        len(sudoers), len(banned), len(stock_counts), len(_account_index),
        elapsed,
    )

    # ── Start retry worker ───────────────────────────────────────────────────
    if _retry_task is not None and not _retry_task.done():
        _retry_task.cancel()
    _retry_queue = asyncio.Queue()
    _retry_task  = asyncio.create_task(_retry_worker())
    _log.info("memstore: retry worker started.")
