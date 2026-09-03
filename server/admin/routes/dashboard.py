"""Dashboard page + stats API + live event stream."""

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from server.admin import templates
from server.admin.deps import require_session
from server.core.mongo import mongodb

router = APIRouter(tags=["Admin-Dashboard"], include_in_schema=False)

# Shared live event queue for SSE (broadcast to all admin clients).
_LIVE_SUBS = set()

# ── Stats cache — avoids hammering MongoDB on every 10-second SSE heartbeat ──
# Shared between /admin/api/stats (REST) and the SSE event generator.
# TTL: 30 s.  A build takes ~10–15 DB round-trips; caching to 30 s drops
# steady-state load from ~120/min to ~2/min per admin session.
_stats_cache: dict = {"data": None, "expires": 0.0}
_STATS_CACHE_TTL_S = 30


@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/dashboard.html", {"page": "dashboard"})


async def _build_stats(*, force: bool = False) -> dict:
    """
    Build and cache the full stats payload.

    Session counts that can be served from the in-memory stock index are read
    from memstore (zero MongoDB round-trips).  All other counts are cached for
    _STATS_CACHE_TTL_S seconds so the SSE heartbeat doesn't hammer the DB.

    Pass force=True to bypass the cache (used after known-mutating events).
    """
    now_ts = time.monotonic()
    if not force and _stats_cache["data"] and now_ts < _stats_cache["expires"]:
        return _stats_cache["data"]

    # ── Session stats — served from RAM where possible ────────────────────────
    from server.core import memstore as _ms
    sc = _ms.stock_counts  # {cc: {"total": int, "clean": int, "sellable": int}}
    total_unsold = sum(v.get("total", 0) for v in sc.values())
    clean_unsold = sum(v.get("clean", 0) for v in sc.values())

    # Sold count and spam breakdown still need one aggregate (or count) query.
    # We run a single $facet aggregation so it's ONE round-trip instead of 8.
    sa = mongodb.session_accounts
    session_facet = await sa.aggregate([
        {"$facet": {
            "total":      [{"$count": "n"}],
            "temp_spam":  [{"$match": {"spam_status": "temporary_spam"}}, {"$count": "n"}],
            "perm_spam":  [{"$match": {"spam_status": "permanent_spam"}}, {"$count": "n"}],
            "frozen":     [{"$match": {"spam_status": "frozen"}},         {"$count": "n"}],
            "unknown":    [{"$match": {"spam_status": "unknown"}},         {"$count": "n"}],
        }},
    ]).to_list(length=1)
    sf = session_facet[0] if session_facet else {}

    def _n(key: str) -> int:
        rows = sf.get(key) or []
        return rows[0]["n"] if rows else 0

    total_sessions   = _n("total")
    sold_sessions    = max(0, total_sessions - total_unsold)
    temp_spam_sess   = _n("temp_spam")
    perm_spam_sess   = _n("perm_spam")
    frozen_sess      = _n("frozen")
    unknown_sess     = _n("unknown")

    # ── Orders — one $facet (4 counts + today + revenue in one trip) ──────────
    orders   = mongodb.orders
    now_utc  = datetime.now(timezone.utc)
    today_s  = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    order_facet = await orders.aggregate([
        {"$facet": {
            "total":     [{"$count": "n"}],
            "pending":   [{"$match": {"status": "pending"}},   {"$count": "n"}],
            "completed": [{"$match": {"status": "completed"}}, {"$count": "n"}],
            "cancelled": [{"$match": {"status": "cancelled"}}, {"$count": "n"}],
            "refunded":  [{"$match": {"status": "refunded"}},  {"$count": "n"}],
            "today":     [{"$match": {"created_at": {"$gte": today_s}}}, {"$count": "n"}],
            "revenue_total": [
                {"$match": {"status": "completed"}},
                {"$group": {"_id": None, "s": {"$sum": "$amount"}}},
            ],
            "revenue_today": [
                {"$match": {"status": "completed", "completed_at": {"$gte": today_s}}},
                {"$group": {"_id": None, "s": {"$sum": "$amount"}}},
            ],
        }},
    ]).to_list(length=1)
    of = order_facet[0] if order_facet else {}

    def _on(key: str) -> int:
        rows = of.get(key) or []
        return rows[0]["n"] if rows else 0

    def _osum(key: str) -> float:
        rows = of.get(key) or []
        return round(rows[0]["s"], 2) if rows else 0.0

    # ── Users ─────────────────────────────────────────────────────────────────
    users = mongodb.users
    user_facet = await users.aggregate([
        {"$facet": {
            "total":  [{"$match": {"user_id": {"$gt": 0}}}, {"$count": "n"}],
            "banned": [{"$match": {"is_banned": True}},     {"$count": "n"}],
            "today":  [{"$match": {"joined_at": {"$gte": today_s}}}, {"$count": "n"}],
        }},
    ]).to_list(length=1)
    uf = user_facet[0] if user_facet else {}

    def _un(key: str) -> int:
        rows = uf.get(key) or []
        return rows[0]["n"] if rows else 0

    # ── Payments — deposits + withdrawals — one $facet each ───────────────────
    deposits    = mongodb.deposits
    withdrawals = mongodb.withdrawals

    dep_facet = await deposits.aggregate([
        {"$facet": {
            "pending":   [{"$match": {"status": "pending"}},   {"$count": "n"}],
            "completed": [{"$match": {"status": "completed"}}, {"$count": "n"}],
            "volume":    [
                {"$match": {"status": "completed"}},
                {"$group": {"_id": None, "s": {"$sum": "$amount"}}},
            ],
        }},
    ]).to_list(length=1)
    df = dep_facet[0] if dep_facet else {}

    def _dn(key: str) -> int:
        rows = df.get(key) or []
        return rows[0]["n"] if rows else 0

    def _dsum(key: str) -> float:
        rows = df.get(key) or []
        return round(rows[0]["s"], 2) if rows else 0.0

    pending_withdrawals = await withdrawals.count_documents({"status": "pending"})

    # ── Sell requests + user sell stock ───────────────────────────────────────
    from server.utils.database.sellrequestdb import count_pending_sell_requests
    from server.utils.database.usersellstockdb import count_user_sell_stock
    pending_sell_requests       = await count_pending_sell_requests()
    pending_user_sell_stock     = await count_user_sell_stock(transferred=False)
    transferred_user_sell_stock = await count_user_sell_stock(transferred=True)

    # ── Proxies + countries — cheap; kept as individual counts ───────────────
    proxies  = mongodb.proxies
    prx_facet = await proxies.aggregate([
        {"$facet": {
            "active": [{"$match": {"is_active": True}},  {"$count": "n"}],
            "dead":   [{"$match": {"is_active": False}}, {"$count": "n"}],
            "total":  [{"$count": "n"}],
        }},
    ]).to_list(length=1)
    pf = prx_facet[0] if prx_facet else {}

    def _pn(key: str) -> int:
        rows = pf.get(key) or []
        return rows[0]["n"] if rows else 0

    total_countries  = await mongodb.countries.count_documents({})
    active_countries = await mongodb.countries.count_documents({"temp_disable": False})

    result = {
        "sessions": {
            "total":          total_sessions,
            "sold":           sold_sessions,
            "unsold":         total_unsold,
            "clean":          clean_unsold,
            "temporary_spam": temp_spam_sess,
            "permanent_spam": perm_spam_sess,
            "frozen":         frozen_sess,
            "unknown":        unknown_sess,
        },
        "orders": {
            "total":     _on("total"),
            "pending":   _on("pending"),
            "completed": _on("completed"),
            "cancelled": _on("cancelled"),
            "refunded":  _on("refunded"),
            "today":     _on("today"),
        },
        "revenue": {
            "total": _osum("revenue_total"),
            "today": _osum("revenue_today"),
        },
        "users": {
            "total":  _un("total"),
            "banned": _un("banned"),
            "today":  _un("today"),
        },
        "payments": {
            "pending_deposits":         _dn("pending"),
            "pending_withdrawals":      pending_withdrawals,
            "total_deposits_completed": _dn("completed"),
            "total_deposited":          _dsum("volume"),
            "pending_sell_requests":    pending_sell_requests,
        },
        "user_sell_stock": {
            "pending":     pending_user_sell_stock,
            "transferred": transferred_user_sell_stock,
            "total":       pending_user_sell_stock + transferred_user_sell_stock,
        },
        "proxies": {
            "total":  _pn("total"),
            "active": _pn("active"),
            "dead":   _pn("dead"),
        },
        "countries": {
            "total":  total_countries,
            "active": active_countries,
        },
    }

    _stats_cache["data"]    = result
    _stats_cache["expires"] = time.monotonic() + _STATS_CACHE_TTL_S
    return result


def invalidate_stats_cache() -> None:
    """Force the next _build_stats() call to bypass the cache."""
    _stats_cache["expires"] = 0.0


@router.get("/admin/api/stats")
async def admin_stats(_session=Depends(require_session)):
    """REST endpoint — always returns fresh stats (bypasses cache for direct API hits)."""
    data = await _build_stats(force=True)
    return JSONResponse(data)


# Period presets: (days_span, bucket_days, label_fmt)
_PERIOD_PRESETS = {
    "1d":  (1,   None, "%H:00"),   # hourly
    "1w":  (7,   1,    "%b %d"),
    "14d": (14,  1,    "%b %d"),
    "1m":  (30,  1,    "%b %d"),
    "3m":  (90,  7,    "%b %d"),
    "6m":  (180, 7,    "%b %d"),
    "9m":  (270, 14,   "%b %d"),
}


def _period_buckets(period: str):
    """Return list of (label, start, end) buckets for the given period."""
    period = period if period in _PERIOD_PRESETS else "14d"
    days, bucket_days, fmt = _PERIOD_PRESETS[period]
    now = datetime.now(timezone.utc)
    buckets = []
    if period == "1d":
        # 24 hourly buckets ending at current hour
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        for i in range(23, -1, -1):
            start = hour_start - timedelta(hours=i)
            end = start + timedelta(hours=1)
            buckets.append((start.strftime(fmt), start, end))
        return buckets
    # daily/multi-day buckets
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_buckets = max(1, days // bucket_days)
    for i in range(total_buckets - 1, -1, -1):
        start = today_start - timedelta(days=(i + 1) * bucket_days - 1) - timedelta(days=0)
        # simpler: compute start from end
        end = today_start + timedelta(days=1) - timedelta(days=i * bucket_days)
        start = end - timedelta(days=bucket_days)
        buckets.append((start.strftime(fmt), start, end))
    return buckets


@router.get("/admin/api/charts/orders")
async def chart_orders(period: str = "14d", _session=Depends(require_session)):
    """Order counts per bucket for the selected period."""
    orders = mongodb.orders
    labels, completed_data, cancelled_data = [], [], []
    for label, start, end in _period_buckets(period):
        labels.append(label)
        completed_data.append(await orders.count_documents({
            "status": "completed", "completed_at": {"$gte": start, "$lt": end}
        }))
        cancelled_data.append(await orders.count_documents({
            "status": "cancelled", "cancelled_at": {"$gte": start, "$lt": end}
        }))
    return JSONResponse({"labels": labels, "completed": completed_data, "cancelled": cancelled_data, "period": period})


@router.get("/admin/api/charts/revenue")
async def chart_revenue(period: str = "14d", _session=Depends(require_session)):
    """Revenue per bucket for the selected period."""
    orders = mongodb.orders
    labels, revenue_data = [], []
    for label, start, end in _period_buckets(period):
        labels.append(label)
        pipeline = [
            {"$match": {"status": "completed", "completed_at": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ]
        result = await orders.aggregate(pipeline).to_list(length=1)
        revenue_data.append(round(result[0]["total"], 2) if result else 0)
    return JSONResponse({"labels": labels, "revenue": revenue_data, "period": period})


@router.get("/admin/api/charts/sessions-by-country")
async def chart_sessions_by_country(_session=Depends(require_session)):
    """Top 10 countries by unsold session count — served from memstore (no DB query)."""
    from server.core import memstore as _ms
    rows = sorted(
        [
            {"cc": cc, "count": v.get("total", 0)}
            for cc, v in _ms.stock_counts.items()
            if v.get("total", 0) > 0
        ],
        key=lambda r: r["count"],
        reverse=True,
    )[:10]
    # Enrich with country name from memstore countries
    labels, data = [], []
    for row in rows:
        c = _ms.get_country(row["cc"])
        labels.append((c or {}).get("country_name") or row["cc"])
        data.append(row["count"])
    return JSONResponse({"labels": labels, "data": data})


@router.get("/admin/api/events/stream")
async def admin_event_stream(_session=Depends(require_session)):
    """SSE stream broadcasting live stats and alerts to the admin dashboard."""
    queue = asyncio.Queue()
    async def event_generator():
        _LIVE_SUBS.add(queue)
        try:
            # Send initial stats immediately (fresh — first load)
            stats = await _build_stats(force=True)
            yield f"data: {json.dumps({'type': 'stats', 'stats': stats})}\n\n"
            last_alert_check = datetime.now(timezone.utc)
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=10)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat: send stats every 10 seconds, but use cache so
                    # we avoid 20+ DB round-trips per connected admin session.
                    stats = await _build_stats()  # cache-served
                    yield f"data: {json.dumps({'type': 'stats', 'stats': stats})}\n\n"
                    # Alert check every 30 seconds
                    now = datetime.now(timezone.utc)
                    if (now - last_alert_check).total_seconds() >= 30:
                        last_alert_check = now
                        pending_d  = await mongodb.deposits.count_documents({"status": "pending", "created_at": {"$gte": now - timedelta(seconds=35)}})
                        pending_w  = await mongodb.withdrawals.count_documents({"status": "pending", "created_at": {"$gte": now - timedelta(seconds=35)}})
                        pending_sr = await mongodb.sell_requests.count_documents({"status": "pending", "submitted_at": {"$gte": now - timedelta(seconds=35)}})
                        if pending_d or pending_w or pending_sr:
                            yield f"data: {json.dumps({'type': 'alert', 'pending_deposits': pending_d, 'pending_withdrawals': pending_w, 'pending_sell_requests': pending_sr})}\n\n"
        finally:
            _LIVE_SUBS.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


async def broadcast_event(payload: dict) -> None:
    """
    Push a live event to every connected admin client.

    Also invalidates the stats cache so the next heartbeat returns fresh data
    after a known-mutating event (sale, approval, transfer, etc.).
    """
    invalidate_stats_cache()
    dead = []
    for q in list(_LIVE_SUBS):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _LIVE_SUBS.discard(q)
