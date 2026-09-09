"""
users / sudoers / blocked_users / settings collections — RAM-first.

Hot reads from RAM:
  sudoers         → memstore.sudoers  (no Mongo round-trip)
  is_banned       → memstore.banned   (no Mongo round-trip)
  get_user        → memstore user cache (TTL=120s, fallback to Mongo)
  get_user_lang   → from cached user doc

Financial writes (balance, deductions, purchases) ALWAYS go directly to
MongoDB — atomicity and correctness require it. User cache is invalidated
after any balance mutation so the next read reflects the DB state.
"""

import secrets
from typing import Optional

from server.core.mongo import collection
from server.core import memstore
from server.utils.common import utcnow as _now

usersdb    = collection("users")
sudoersdb  = collection("sudoers")
blockeddb  = collection("blocked_users")
settingsdb = collection("settings")


class BalanceError(Exception):
    """Raised when an atomic balance deduction fails (insufficient funds)."""


def _generate_api_key(user_id: int) -> str:
    return f"tg_{user_id}_{secrets.token_hex(16)}"


def _generate_referral_code() -> str:
    return secrets.token_urlsafe(8).upper()


DEFAULT_PROFILE = {
    "balance":                 0.0,
    "reserve_balance":         0.0,
    "pending_balance":         0.0,
    "rank":                    "VIP1",
    "language":                "en",
    "api_key":                 None,
    "api_access":              True,
    "total_account_buy":       0,
    "total_account_sell":      0,
    "total_deposit":           0.0,
    "total_spend":             0.0,
    "total_earn":              0.0,
    "referral_code":           None,
    "referred_by":             None,
    "referral_count":          0,
    "referral_earnings":       0.0,
    "referral_notifications":  True,
    "disable_password":        False,
    "two_fa_enabled":          False,
    "two_fa_secret":           None,
    "is_banned":               False,
    "ban_reason":              None,
    "is_verified":             False,
    "joined_at":               None,
}

RANK_THRESHOLDS = {
    "VIP1":    {"min_spend": 0,     "min_buy": 0},
    "VIP2":    {"min_spend": 100,   "min_buy": 5},
    "VIP3":    {"min_spend": 500,   "min_buy": 20},
    "PREMIUM": {"min_spend": 2000,  "min_buy": 100},
    "DIAMOND": {"min_spend": 10000, "min_buy": 500},
}


# ─── User Registration ──────────────────────────────────────────────────────

async def is_served_user(user_id: int) -> bool:
    cached = memstore.get_cached_user(user_id)
    if cached is not None:
        return True
    return bool(await usersdb.find_one({"user_id": user_id}, {"_id": 1}))


async def add_served_user(user_id: int, username: str = None, full_name: str = None):
    now = _now()
    await usersdb.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                **DEFAULT_PROFILE,
                "user_id":       user_id,
                "api_key":       _generate_api_key(user_id),
                "referral_code": _generate_referral_code(),
                "joined_at":     now,
            },
            "$set": {
                "username":  username,
                "full_name": full_name,
            },
        },
        upsert=True,
    )
    # Invalidate so next get_user() fetches the fresh doc
    memstore.invalidate_user(user_id)


async def get_served_users(limit: int = 10_000) -> list:
    users = []
    async for user in usersdb.find({"user_id": {"$gt": 0}}).limit(limit):
        users.append(user)
    return users


async def get_user(user_id: int) -> Optional[dict]:
    cached = memstore.get_cached_user(user_id)
    if cached is not None:
        return cached
    doc = await usersdb.find_one({"user_id": user_id})
    if doc:
        memstore.cache_user(doc)
    return doc


async def get_total_users() -> int:
    return await usersdb.count_documents({"user_id": {"$gt": 0}})


# ─── Language ───────────────────────────────────────────────────────────────

async def get_user_lang(user_id: int) -> str:
    cached = memstore.get_cached_user(user_id)
    if cached is not None:
        return cached.get("language", "en")
    user = await usersdb.find_one({"user_id": user_id}, {"language": 1})
    return (user or {}).get("language", "en")


async def set_user_lang(user_id: int, lang: str):
    memstore.invalidate_user(user_id)
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"language": lang}},
        upsert=True,
    )


# ─── Balance & Wallet ───────────────────────────────────────────────────────
# All balance ops go directly to MongoDB — atomicity is required.
# User cache is invalidated after writes so the next read is fresh.

async def get_balance(user_id: int) -> float:
    # Always fetch from Mongo — balance must be current.
    user = await usersdb.find_one({"user_id": user_id}, {"balance": 1})
    return round(float((user or {}).get("balance", 0.0) or 0.0), 4)


async def _legacy_reserve_value(user: dict) -> float:
    """Best-effort migration value for users created before reserve_balance existed.

    Reserve represents earnings still present in the wallet.  Because older
    documents did not track which source (deposit vs earnings) each purchase
    consumed, the safest recoverable estimate is lifetime earnings/referrals
    minus lifetime spend/withdrawals, capped by the current wallet balance.
    """
    earned = float(user.get("total_earn") or 0.0) + float(user.get("referral_earnings") or 0.0)
    spent = float(user.get("total_spend") or 0.0)
    withdrawn = float(user.get("total_withdrawal") or 0.0)
    balance = float(user.get("balance") or 0.0)
    return max(0.0, min(balance, earned - spent - withdrawn))


async def ensure_reserve_balance(user_id: int) -> float:
    """Ensure legacy users get a persistent reserve_balance field."""
    user = await usersdb.find_one(
        {"user_id": user_id},
        {"reserve_balance": 1, "balance": 1, "total_earn": 1,
         "referral_earnings": 1, "total_spend": 1, "total_withdrawal": 1},
    ) or {}
    if "reserve_balance" in user:
        return round(max(0.0, float(user.get("reserve_balance") or 0.0)), 4)
    value = round(await _legacy_reserve_value(user), 4)
    result = await usersdb.update_one(
        {"user_id": user_id, "reserve_balance": {"$exists": False}},
        {"$set": {"reserve_balance": value}},
    )
    if result.modified_count:
        memstore.invalidate_user(user_id)
    return value


async def get_reserve_balance(user_id: int) -> float:
    """Return earned money that is still withdrawable/spendable."""
    return await ensure_reserve_balance(user_id)


async def get_wallet_snapshot(user_id: int) -> dict:
    """Return fresh wallet fields from one canonical MongoDB document read."""
    user = await usersdb.find_one(
        {"user_id": user_id},
        {"balance": 1, "reserve_balance": 1, "reserved_balance": 1,
         "total_earn": 1, "referral_earnings": 1, "total_spend": 1,
         "total_withdrawal": 1, "wallet_addresses": 1},
    ) or {}
    reserve = user.get("reserve_balance")
    if reserve is None:
        reserve = await _legacy_reserve_value(user)
    addresses = user.get("wallet_addresses") or {}
    return {
        "balance": round(float(user.get("balance") or 0.0), 4),
        "reserve_balance": round(max(0.0, float(reserve or 0.0)), 4),
        "reserved_balance": round(float(user.get("reserved_balance") or 0.0), 4),
        "addresses": {"trc20": addresses.get("trc20"), "bep20": addresses.get("bep20")},
    }


async def update_balance(user_id: int, amount: float):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$inc": {"balance": round(amount, 4)}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


async def adjust_reserve_balance(user_id: int, amount: float) -> tuple[bool, float, float]:
    """Atomically adjust a user's withdrawable earned reserve and total balance.

    Positive ``amount`` credits both balances. Negative ``amount`` debits both
    balances, but only when both have enough funds. This keeps reserve_balance
    and balance consistent for admin adjustments.

    Returns: (ok, new_balance, new_reserve_balance).
    """
    try:
        user_id = int(user_id)
        amount = round(float(amount), 4)
    except (TypeError, ValueError):
        return False, 0.0, 0.0

    if not math.isfinite(amount) or amount == 0:
        return False, 0.0, 0.0

    await ensure_reserve_balance(user_id)

    if amount > 0:
        doc = await usersdb.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"balance": amount, "reserve_balance": amount}},
            projection={"balance": 1, "reserve_balance": 1},
            return_document=ReturnDocument.AFTER,
        )
    else:
        debit = abs(amount)
        doc = await usersdb.find_one_and_update(
            {
                "user_id": user_id,
                "balance": {"$gte": debit},
                "reserve_balance": {"$gte": debit},
            },
            {"$inc": {"balance": -debit, "reserve_balance": -debit}},
            projection={"balance": 1, "reserve_balance": 1},
            return_document=ReturnDocument.AFTER,
        )

    if not doc:
        return False, 0.0, 0.0

    memstore.invalidate_user(user_id)
    return (
        True,
        round(float(doc.get("balance") or 0.0), 4),
        round(max(0.0, float(doc.get("reserve_balance") or 0.0)), 4),
    )


async def set_balance(user_id: int, amount: float):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"balance": round(amount, 4)}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


async def deduct_balance_atomic(user_id: int, amount: float) -> bool:
    """Spend from total balance and consume earned reserve first.

    The total ``balance`` is reduced by the purchase amount.  ``reserve_balance``
    is reduced by the same amount up to zero, so earned funds are consumed before
    deposit funds.  This preserves the meaning: reserve = earned money still
    remaining after spending/withdrawals.
    """
    amount = round(float(amount), 4)
    if amount <= 0:
        return True
    # Initialise legacy documents before the atomic purchase deduction.
    await ensure_reserve_balance(user_id)
    result = await usersdb.update_one(
        {"user_id": user_id, "balance": {"$gte": amount}},
        [{"$set": {
            "balance": {"$round": [{"$subtract": ["$balance", amount]}, 4]},
            "reserve_balance": {"$round": [{"$max": [0.0, {"$subtract": [{"$ifNull": ["$reserve_balance", 0.0]}, amount]}]}, 4]},
        }}],
    )
    if result.modified_count > 0:
        memstore.invalidate_user(user_id)
    return result.modified_count > 0


# ─── Withdrawal Balance Reservation ──────────────────────────────────────────
# Three-step lifecycle:
#   1. reserve_balance_atomic   — move from balance → reserved_balance
#   2. finalize_reserved_balance — remove from reserved_balance (permanent deduction)
#   3. release_reserved_balance  — move reserved_balance → balance (refund)

async def reserve_earned_balance_atomic(user_id: int, amount: float) -> bool:
    """Lock an earned amount for withdrawal. Removes it from both spendable
    balance and withdrawable reserve while keeping a separate temporary lock.
    """
    amount = round(float(amount), 4)
    if amount <= 0:
        return False
    await ensure_reserve_balance(user_id)
    result = await usersdb.update_one(
        {"user_id": user_id, "balance": {"$gte": amount}, "reserve_balance": {"$gte": amount}},
        {"$inc": {
            "balance": -amount,
            "reserve_balance": -amount,
            "reserved_balance": amount,
        }},
    )
    if result.modified_count > 0:
        memstore.invalidate_user(user_id)
    return result.modified_count > 0


async def reserve_balance_atomic(user_id: int, amount: float) -> bool:
    """Backward-compatible alias: withdrawals now reserve earned balance only."""
    return await reserve_earned_balance_atomic(user_id, amount)


async def finalize_reserved_balance(user_id: int, amount: float) -> bool:
    """Permanently consume a withdrawal amount already locked in reserved_balance."""
    amount = round(float(amount), 4)
    result = await usersdb.update_one(
        {"user_id": user_id, "reserved_balance": {"$gte": amount}},
        {"$inc": {"reserved_balance": -amount, "total_withdrawal": amount}},
    )
    if result.modified_count > 0:
        memstore.invalidate_user(user_id)
    return result.modified_count > 0


async def release_reserved_balance(user_id: int, amount: float) -> None:
    """Return a failed/cancelled withdrawal to both balance and reserve."""
    amount = round(float(amount), 4)
    await usersdb.update_one(
        {"user_id": user_id, "reserved_balance": {"$gte": amount}},
        {"$inc": {
            "reserved_balance": -amount,
            "balance": amount,
            "reserve_balance": amount,
        }},
    )
    memstore.invalidate_user(user_id)


async def get_reserved_balance(user_id: int) -> float:
    """Return the amount currently reserved for pending withdrawals."""
    user = await usersdb.find_one({"user_id": user_id}, {"reserved_balance": 1})
    return round((user or {}).get("reserved_balance", 0.0), 4)


async def record_deposit(
    user_id: int,
    amount: float,
    *,
    deposit_id: str | None = None,
) -> bool:
    """Credit a deposit exactly once when a durable deposit ID is available.

    The conditional ``$ne`` predicate and ``$inc`` happen atomically in Mongo.
    Legacy callers may omit ``deposit_id`` and retain the historical behavior;
    all current deposit-confirmation paths pass the provider deposit ID.
    """
    query = {"user_id": user_id}
    update = {"$inc": {"balance": amount, "total_deposit": amount}}
    if deposit_id:
        query["credited_deposit_ids"] = {"$ne": deposit_id}
        update["$addToSet"] = {"credited_deposit_ids": deposit_id}

    result = await usersdb.update_one(query, update, upsert=True)
    credited = bool(result.modified_count or result.upserted_id is not None)
    if credited:
        memstore.invalidate_user(user_id)
    return credited


# ─── Wallet Addresses ────────────────────────────────────────────────────────

SUPPORTED_NETWORKS = ["TRC20", "BEP20"]


async def get_wallet_addresses(user_id: int) -> dict:
    cached = memstore.get_cached_user(user_id)
    addrs  = (cached or {}).get("wallet_addresses")
    if addrs is None:
        user  = await usersdb.find_one({"user_id": user_id}, {"wallet_addresses": 1})
        addrs = (user or {}).get("wallet_addresses", {})
    return {
        "trc20": addrs.get("trc20"),
        "bep20": addrs.get("bep20"),
    }


async def set_wallet_address(user_id: int, network: str, address: str) -> None:
    network = network.lower()
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {f"wallet_addresses.{network}": address}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


async def record_purchase(user_id: int, amount: float, *, units: int = 1):
    """Charge the wallet. Earned reserve is consumed first, then deposits."""
    ok = await deduct_balance_atomic(user_id, amount)
    if not ok:
        raise BalanceError(
            f"Balance fell below ${float(amount):.4f} between pre-check and deduction "
            "(concurrent purchase likely drained the wallet)."
        )


async def record_successful_purchase(
    user_id: int,
    order_id: str,
    amount: float,
    *,
    units: int = 1,
) -> bool:
    """Record successful-sale statistics exactly once after delivery.

    The order document is the durable idempotency gate.  A duplicate callback,
    poll, retry, or worker replay sees ``successful_accounting=True`` and does
    not increment the buyer statistics again.
    """
    from server.utils.database.orderdb import ordersdb

    units = max(1, int(units))
    value = round(float(amount), 4)
    order = await ordersdb.find_one({
        "order_id": order_id,
        "buyer_id": user_id,
        "status": "completed",
        "delivery_status": "delivered",
    })
    if not order:
        return False

    # This update is the idempotency boundary.  Mongo applies the predicate
    # and both updates atomically, so concurrent callbacks can never increment
    # Spent/counters twice for the same order.  It also recovers safely if the
    # process crashes after this update but before the order marker is written.
    result = await usersdb.update_one(
        {"user_id": user_id, "successful_purchase_ids": {"$ne": order_id}},
        {
            "$inc": {"total_spend": value, "total_account_buy": units},
            "$addToSet": {"successful_purchase_ids": order_id},
        },
    )
    buyer_exists = await usersdb.find_one({"user_id": user_id}, {"user_id": 1})
    if buyer_exists is None:
        await ordersdb.update_one(
            {"order_id": order_id},
            {"$set": {"successful_accounting_error": "buyer_not_found"}},
        )
        raise RuntimeError(f"Buyer {user_id} not found while recording order {order_id}")

    await ordersdb.update_one(
        {"order_id": order_id, "successful_accounting": {"$ne": True}},
        {"$set": {"successful_accounting": True}},
    )
    if result.modified_count == 0:
        return False
    memstore.invalidate_user(user_id)
    await _update_rank(user_id)
    return True


async def record_sale(user_id: int, amount: float):
    await usersdb.update_one(
        {"user_id": user_id},
        {
            "$inc": {
                "balance":            round(amount, 4),
                "reserve_balance":    round(amount, 4),
                "total_earn":         round(amount, 4),
                "total_account_sell": 1,
            }
        },
    )
    memstore.invalidate_user(user_id)


# ─── Pending Balance (Sell flow) ─────────────────────────────────────────────

async def get_pending_balance(user_id: int) -> float:
    user = await usersdb.find_one({"user_id": user_id}, {"pending_balance": 1})
    return round((user or {}).get("pending_balance", 0.0), 4)


async def credit_pending_balance(user_id: int, amount: float) -> None:
    await usersdb.update_one(
        {"user_id": user_id},
        {"$inc": {"pending_balance": round(amount, 4)}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


async def move_pending_to_available(user_id: int, amount: float) -> bool:
    result = await usersdb.update_one(
        {"user_id": user_id, "pending_balance": {"$gte": round(amount, 4)}},
        {
            "$inc": {
                "pending_balance":    -round(amount, 4),
                "balance":             round(amount, 4),
                "reserve_balance":    round(amount, 4),
                "total_earn":          round(amount, 4),
                "total_account_sell":  1,
            }
        },
    )
    if result.modified_count > 0:
        memstore.invalidate_user(user_id)
    return result.modified_count > 0


async def clear_pending_balance(user_id: int, amount: float) -> None:
    """Atomically deduct pending_balance, clamping to 0 via an aggregation pipeline update."""
    await usersdb.update_one(
        {"user_id": user_id},
        [
            {
                "$set": {
                    "pending_balance": {
                        "$max": [
                            0.0,
                            {"$subtract": ["$pending_balance", round(amount, 4)]},
                        ]
                    }
                }
            }
        ],
    )
    memstore.invalidate_user(user_id)


# ─── Rank System ────────────────────────────────────────────────────────────

async def _update_rank(user_id: int):
    await usersdb.update_one(
        {"user_id": user_id},
        [
            {
                "$set": {
                    "rank": {
                        "$switch": {
                            "branches": [
                                {
                                    "case": {
                                        "$and": [
                                            {"$gte": ["$total_spend",       10000]},
                                            {"$gte": ["$total_account_buy", 500]},
                                        ]
                                    },
                                    "then": "DIAMOND",
                                },
                                {
                                    "case": {
                                        "$and": [
                                            {"$gte": ["$total_spend",       2000]},
                                            {"$gte": ["$total_account_buy", 100]},
                                        ]
                                    },
                                    "then": "PREMIUM",
                                },
                                {
                                    "case": {
                                        "$and": [
                                            {"$gte": ["$total_spend",       500]},
                                            {"$gte": ["$total_account_buy", 20]},
                                        ]
                                    },
                                    "then": "VIP3",
                                },
                                {
                                    "case": {
                                        "$and": [
                                            {"$gte": ["$total_spend",       100]},
                                            {"$gte": ["$total_account_buy", 5]},
                                        ]
                                    },
                                    "then": "VIP2",
                                },
                            ],
                            "default": "VIP1",
                        }
                    }
                }
            }
        ],
    )
    memstore.invalidate_user(user_id)


async def get_rank(user_id: int) -> str:
    cached = memstore.get_cached_user(user_id)
    if cached:
        return cached.get("rank", "VIP1")
    user = await usersdb.find_one({"user_id": user_id}, {"rank": 1})
    return (user or {}).get("rank", "VIP1")


async def set_rank(user_id: int, rank: str):
    if rank not in RANK_THRESHOLDS:
        raise ValueError(f"Invalid rank '{rank}'. Valid ranks: {list(RANK_THRESHOLDS)}")
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"rank": rank}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


# ─── API Key ────────────────────────────────────────────────────────────────

async def get_api_key(user_id: int) -> Optional[str]:
    cached = memstore.get_cached_user(user_id)
    if cached:
        return cached.get("api_key")
    user = await usersdb.find_one({"user_id": user_id}, {"api_key": 1})
    return (user or {}).get("api_key")


async def regenerate_api_key(user_id: int) -> str:
    new_key = _generate_api_key(user_id)
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"api_key": new_key}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)
    return new_key


async def get_user_by_api_key(api_key: str) -> Optional[dict]:
    # API key lookups must always go to Mongo (can't index all keys in RAM)
    return await usersdb.find_one({"api_key": api_key, "api_access": True})


async def set_api_access(user_id: int, status: bool):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"api_access": status}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


# ─── Referral System ────────────────────────────────────────────────────────

async def get_referral_code(user_id: int) -> Optional[str]:
    cached = memstore.get_cached_user(user_id)
    if cached:
        return cached.get("referral_code")
    user = await usersdb.find_one({"user_id": user_id}, {"referral_code": 1})
    return (user or {}).get("referral_code")


async def get_user_by_referral(code: str) -> Optional[dict]:
    return await usersdb.find_one({"referral_code": code})


async def apply_referral(user_id: int, referral_code: str, bonus: float = 0.005) -> bool:
    # Validate the referral code and ensure it doesn't point back at the same user
    referrer = await usersdb.find_one({"referral_code": referral_code}, {"user_id": 1})
    if not referrer or referrer["user_id"] == user_id:
        return False
    referrer_id = referrer["user_id"]

    # Atomically claim the referral slot: only succeeds if referred_by is still
    # None in the DB at write time, preventing a TOCTOU double-claim race where
    # two concurrent calls both read referred_by=None and both proceed.
    result = await usersdb.update_one(
        {"user_id": user_id, "referred_by": None},
        {"$set": {"referred_by": referrer_id}},
    )
    if result.modified_count == 0:
        # Either the user doesn't exist or was already referred
        return False

    # Credit the referrer — best-effort; if this fails the slot is already
    # claimed so the user won't get double-credited on retry.
    await usersdb.update_one(
        {"user_id": referrer_id},
        {
            "$inc": {
                "referral_count":    1,
                "referral_earnings": bonus,
                "balance":           bonus,
                "reserve_balance":  bonus,
            }
        },
    )
    memstore.invalidate_user(user_id)
    memstore.invalidate_user(referrer_id)
    return True


async def get_referral_stats(user_id: int) -> dict:
    cached = memstore.get_cached_user(user_id)
    src    = cached or await usersdb.find_one(
        {"user_id": user_id},
        {"referral_count": 1, "referral_earnings": 1, "referral_code": 1},
    )
    if not src:
        return {"count": 0, "earnings": 0.0, "code": None}
    return {
        "count":    src.get("referral_count", 0),
        "earnings": src.get("referral_earnings", 0.0),
        "code":     src.get("referral_code"),
    }


# ─── Settings / Notifications ───────────────────────────────────────────────

async def set_referral_notifications(user_id: int, status: bool):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"referral_notifications": status}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


async def set_disable_password(user_id: int, status: bool):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"disable_password": status}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


# ─── 2FA ────────────────────────────────────────────────────────────────────

async def enable_2fa(user_id: int, secret: str):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"two_fa_enabled": True, "two_fa_secret": secret}},
        upsert=True,
    )
    memstore.invalidate_user(user_id)


async def disable_2fa(user_id: int):
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"two_fa_enabled": False, "two_fa_secret": None}},
    )
    memstore.invalidate_user(user_id)


async def get_2fa_secret(user_id: int) -> Optional[str]:
    cached = memstore.get_cached_user(user_id)
    if cached and cached.get("two_fa_enabled"):
        return cached.get("two_fa_secret")
    user = await usersdb.find_one(
        {"user_id": user_id, "two_fa_enabled": True},
        {"two_fa_secret": 1},
    )
    return (user or {}).get("two_fa_secret")


# ─── Admin: Ban / Unban ─────────────────────────────────────────────────────
# RAM-first: is_banned() is a hot-path O(1) set lookup.

async def is_banned_user(user_id: int) -> bool:
    return user_id in memstore.banned


async def is_banned(user_id: int) -> bool:
    return user_id in memstore.banned


async def add_banned_user(user_id: int, reason: str = "No reason provided"):
    memstore.banned.add(user_id)
    memstore.invalidate_user(user_id)
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": True, "ban_reason": reason}},
        upsert=True,
    )


async def remove_banned_user(user_id: int):
    memstore.banned.discard(user_id)
    memstore.invalidate_user(user_id)
    await usersdb.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": False, "ban_reason": None}},
    )


async def get_banned_users() -> list:
    result = []
    async for user in usersdb.find({"is_banned": True}).limit(1000):
        result.append(user)
    return result


async def get_banned_count() -> int:
    return len(memstore.banned)


async def get_gbanned() -> list:
    return await get_banned_users()


# ─── Admin: Sudo ────────────────────────────────────────────────────────────
# RAM-first: get_sudoers() is served from memstore.sudoers (O(1) list copy).

async def get_sudoers() -> list:
    return list(memstore.sudoers)


async def add_sudo(user_id: int) -> bool:
    if user_id in memstore.sudoers:
        return False
    memstore.sudoers.append(user_id)
    await sudoersdb.update_one(
        {"sudo": "sudo"},
        {"$set": {"sudoers": list(memstore.sudoers)}},
        upsert=True,
    )
    return True


async def remove_sudo(user_id: int) -> bool:
    if user_id not in memstore.sudoers:
        return False
    memstore.sudoers.remove(user_id)
    await sudoersdb.update_one(
        {"sudo": "sudo"},
        {"$set": {"sudoers": list(memstore.sudoers)}},
        upsert=True,
    )
    return True


# ─── Stats ──────────────────────────────────────────────────────────────────

async def get_user_stats(user_id: int) -> dict:
    user = await get_user(user_id)
    if not user:
        return {}
    return {
        "user_id":            user["user_id"],
        "username":           user.get("username"),
        "full_name":          user.get("full_name"),
        "balance":            user.get("balance", 0.0),
        "reserve_balance":    user.get("reserve_balance", 0.0),
        "rank":               user.get("rank", "VIP1"),
        "total_account_buy":  user.get("total_account_buy", 0),
        "total_account_sell": user.get("total_account_sell", 0),
        "total_deposit":      user.get("total_deposit", 0.0),
        "total_spend":        user.get("total_spend", 0.0),
        "total_earn":         user.get("total_earn", 0.0),
        "referral_count":     user.get("referral_count", 0),
        "referral_earnings":  user.get("referral_earnings", 0.0),
        "joined_at":          user.get("joined_at"),
        "is_verified":        user.get("is_verified", False),
        "api_access":         user.get("api_access", True),
    }


# NOTE: is_on_off() / toggle_feature() were dead code that pointed at a
# "settings" collection instead of the canonical "admin_config" collection.
# All feature flags are now managed through configdb.get_setting() /
# configdb.set_setting() which use the "admin_config" collection + memstore.
