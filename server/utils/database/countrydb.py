"""
countries collection — RAM-first.

Reads  : served from memstore._countries (zero Mongo round-trips).
Writes : RAM updated immediately, MongoDB synced async via sync_write().
"""

from typing import Optional, List

from pymongo import UpdateOne

from server.core.mongo import collection
from server.core import memstore
from server.utils.common import utcnow as _now

countriesdb = collection("countries")


# ── Read helpers (all from RAM) ──────────────────────────────────────────────

async def get_all_countries(
    buy_only: bool = False,
    sell_only: bool = False,
) -> List[dict]:
    """Return countries sorted by rank — served from RAM, no Mongo call."""
    return memstore.get_countries(buy_only=buy_only, sell_only=sell_only)


async def get_country(code: str) -> Optional[dict]:
    return memstore.get_country(code)


async def country_exists(code: str) -> bool:
    return memstore.get_country(code) is not None


async def get_country_price(code: str) -> Optional[float]:
    doc = memstore.get_country(code)
    if doc is None:
        return None
    return round(doc.get("price", 0.0), 4)


async def get_next_rank() -> int:
    return memstore.get_next_country_rank()


# ── Write helpers (RAM first, Mongo async) ────────────────────────────────────

async def upsert_country(
    code: str,
    country_name: str,
    country_rank: int,
    idc: str,
    price: float = 0.0,
    sell_price: float = 0.0,
    temp_disable: bool = False,
    is_full: bool = False,
    fake_stock: int = 0,
    stock_mode: str = "real",
    # ── per-country spam acceptance ────────────────────────────────────
    accept_clean: bool = True,
    accept_temp_spam: bool = True,
    accept_perm_spam: bool = False,
    # ── per-country spam pricing (0 = fall back to global pricing engine) ──
    price_clean: float = 0.0,
    price_temp_spam: float = 0.0,
    price_perm_spam: float = 0.0,
    termination_delay_hours: int = 24,
) -> None:
    code = code.upper()
    if stock_mode not in ("real", "fake"):
        stock_mode = "real"

    doc = {
        "code":             code,
        "country_name":     country_name,
        "country_rank":     country_rank,
        "idc":              idc,
        "price":            round(price, 4),
        "sell_price":       round(sell_price, 4),
        "temp_disable":     temp_disable,
        "is_full":          is_full,
        "fake_stock":       max(0, int(fake_stock)),
        "stock_mode":       stock_mode,
        # spam config
        "accept_clean":     accept_clean,
        "accept_temp_spam": accept_temp_spam,
        "accept_perm_spam": accept_perm_spam,
        "price_clean":      round(max(0.0, price_clean), 4),
        "price_temp_spam":  round(max(0.0, price_temp_spam), 4),
        "price_perm_spam":  round(max(0.0, price_perm_spam), 4),
        "termination_delay_hours": max(0, int(termination_delay_hours)),
        "updated_at":       _now(),
    }
    memstore.set_country(doc)

    _code, _doc = code, dict(doc)
    memstore.sync_write(
        lambda: countriesdb.update_one(
            {"code": _code},
            {"$set": _doc},
            upsert=True,
        ),
        f"upsert_country:{code}",
    )


async def reorder_country_rank(code: str, new_rank: int) -> None:
    """
    Move country `code` to `new_rank` and auto-fix all other ranks
    sequentially. Entirely in-memory reorder; bulk-writes only the
    changed ranks to MongoDB.
    """
    code = code.upper()
    all_countries = memstore.get_countries()   # sorted by current rank, from RAM
    if not all_countries:
        return

    target = None
    rest   = []
    for c in all_countries:
        if c["code"] == code:
            target = c
        else:
            rest.append(c)

    if target is None:
        return

    total    = len(all_countries)
    new_rank = max(1, min(new_rank, total))
    rest.insert(new_rank - 1, target)

    now = _now()
    ops = []
    for i, c in enumerate(rest):
        desired = i + 1
        if c.get("country_rank") != desired:
            updated = dict(c)
            updated["country_rank"] = desired
            updated["updated_at"]   = now
            memstore.set_country(updated)
            ops.append(UpdateOne(
                {"code": c["code"]},
                {"$set": {"country_rank": desired, "updated_at": now}},
            ))

    if ops:
        _ops = list(ops)
        memstore.sync_write(
            lambda: countriesdb.bulk_write(_ops, ordered=False),
            f"reorder_country_rank:{code}",
        )


async def set_country_field(code: str, field: str, value) -> None:
    code = code.upper()
    existing = memstore.get_country(code)
    if existing:
        existing[field]       = value
        existing["updated_at"] = _now()
        memstore.set_country(existing)

    _code, _field, _value, _now_ts = code, field, value, _now()
    memstore.sync_write(
        lambda: countriesdb.update_one(
            {"code": _code},
            {"$set": {_field: _value, "updated_at": _now_ts}},
        ),
        f"set_country_field:{code}.{field}",
    )


async def delete_country(code: str) -> bool:
    code = code.upper()
    memstore.del_country(code)
    r = await countriesdb.delete_one({"code": code})
    return r.deleted_count > 0


async def delete_all_countries() -> int:
    memstore._countries.clear()
    r = await countriesdb.delete_many({})
    return r.deleted_count


async def set_country_stock_mode(code: str, mode: str) -> None:
    if mode not in ("real", "fake"):
        raise ValueError("mode must be 'real' or 'fake'")
    await set_country_field(code, "stock_mode", mode)


async def bulk_hide_real_stock_except(keep_codes: List[str]) -> int:
    keep = [c.upper() for c in keep_codes]
    now  = _now()
    count = 0
    for c_code, doc in list(memstore._countries.items()):
        if c_code not in keep:
            doc["stock_mode"] = "fake"
            doc["fake_stock"] = 0
            doc["updated_at"] = now
            memstore.set_country(doc)
            count += 1

    _keep, _now_ts = keep, now
    memstore.sync_write(
        lambda: countriesdb.update_many(
            {"code": {"$nin": _keep}},
            {"$set": {"stock_mode": "fake", "fake_stock": 0, "updated_at": _now_ts}},
        ),
        "bulk_hide_real_stock",
    )
    return count

async def bulk_update_countries(codes: List[str], fields: dict) -> int:
    """Apply the same `fields` dict to every country in `codes`.
    Only whitelisted fields are accepted. Returns count of affected countries.
    RAM updated immediately; Mongo synced async.
    """
    allowed = {
        "price", "sell_price", "temp_disable", "is_full",
        "stock_mode", "fake_stock", "termination_delay_hours",
        "accept_clean", "accept_temp_spam", "accept_perm_spam",
        "price_clean", "price_temp_spam", "price_perm_spam",
    }
    clean = {k: v for k, v in (fields or {}).items() if k in allowed}
    if not clean or not codes:
        return 0
    codes = [c.upper() for c in codes]
    now = _now()
    count = 0
    for code in codes:
        doc = memstore.get_country(code)
        if not doc:
            continue
        for k, v in clean.items():
            if k == "price" or k == "sell_price" or k.startswith("price_"):
                doc[k] = round(float(v), 4)
            elif k == "fake_stock":
                doc[k] = max(0, int(v))
            elif k == "termination_delay_hours":
                doc[k] = max(0, int(v))
            elif k in ("temp_disable", "is_full", "accept_clean", "accept_temp_spam", "accept_perm_spam"):
                doc[k] = bool(v)
            elif k == "stock_mode":
                doc[k] = v if v in ("real", "fake") else doc.get("stock_mode", "real")
            else:
                doc[k] = v
        doc["updated_at"] = now
        memstore.set_country(doc)
        count += 1

    _codes, _clean, _now_ts = codes, clean, now
    memstore.sync_write(
        lambda: countriesdb.update_many(
            {"code": {"$in": _codes}},
            {"$set": {**_clean, "updated_at": _now_ts}},
        ),
        "bulk_update_countries",
    )
    return count
