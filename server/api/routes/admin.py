import secrets
import time
from collections import defaultdict
from os import getenv
from threading import Lock
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
ADMIN_PASSWORD = getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise SystemExit("[ERROR] - ADMIN_PASSWORD secret is not set. Please set it in Replit Secrets.")

# Optional: set ADMIN_SECRET_PATH in Replit Secrets to hide the login page
# behind /adminlogin?key=<your-secret>.  Leave unset to keep the old URL.
ADMIN_SECRET_PATH: Optional[str] = getenv("ADMIN_SECRET_PATH") or None

_sessions: set = set()

router = APIRouter(tags=["Admin"])
_security = HTTPBearer(auto_error=False)


def _auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security)):
    if not credentials or credentials.credentials not in _sessions:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.credentials


# ── Brute-force / rate-limit protection ─────────────────────────────────────
_LOCKOUT_WINDOW = 900          # 15 minutes
_MAX_ATTEMPTS   = 5            # attempts before lockout

_fail_counts: dict = defaultdict(list)   # ip -> [monotonic timestamps]
_fail_lock   = Lock()


def _client_ip(request: Request) -> str:
    """Best-effort client IP, respecting reverse-proxy X-Forwarded-For."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown")


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    with _fail_lock:
        _fail_counts[ip] = [t for t in _fail_counts[ip] if now - t < _LOCKOUT_WINDOW]
        if len(_fail_counts[ip]) >= _MAX_ATTEMPTS:
            remaining = int(_LOCKOUT_WINDOW - (now - _fail_counts[ip][0]))
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Too many failed login attempts. "
                    f"Try again in {remaining // 60}m {remaining % 60}s."
                ),
            )


def _record_fail(ip: str) -> None:
    with _fail_lock:
        _fail_counts[ip].append(time.monotonic())


def _clear_fails(ip: str) -> None:
    with _fail_lock:
        _fail_counts.pop(ip, None)


# ── HTML Page ────────────────────────────────────────────────────────────────

@router.get("/adminlogin", response_class=HTMLResponse, include_in_schema=False)
async def admin_panel(request: Request, key: Optional[str] = None):
    """
    Serve the admin login UI.

    If ADMIN_SECRET_PATH is configured, the page is only served when the
    correct ?key=<secret> query parameter is present; otherwise 404.
    This prevents random internet users from even discovering the panel.
    """
    if ADMIN_SECRET_PATH and not secrets.compare_digest(key or "", ADMIN_SECRET_PATH):
        raise HTTPException(status_code=404)
    return HTMLResponse(content=_HTML)


# ── Auth ─────────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    password: str

@router.post("/admin/api/login", include_in_schema=False)
async def admin_login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    _check_rate_limit(ip)                        # raises 429 if over limit

    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        _record_fail(ip)                         # count this failure
        raise HTTPException(status_code=401, detail="Wrong password")

    _clear_fails(ip)                             # successful login resets counter
    token = secrets.token_hex(32)
    _sessions.add(token)
    return {"token": token}

@router.post("/admin/api/logout", include_in_schema=False)
async def admin_logout(tok: str = Depends(_auth)):
    _sessions.discard(tok)
    return {"ok": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/admin/api/stats", include_in_schema=False)
async def admin_stats(_: str = Depends(_auth)):
    from server.utils.database.userdb import usersdb
    from server.utils.database.accountdb import accountsdb
    from server.utils.database.orderdb import ordersdb, get_total_volume, get_total_fees_collected
    from server.utils.database.walletdb import depositsdb, withdrawalsdb
    from server.utils.database.sellrequestdb import sellrequestsdb
    return {
        "total_users":          await usersdb.count_documents({}),
        "banned_users":         await usersdb.count_documents({"is_banned": True}),
        "available_accounts":   await accountsdb.count_documents({"status": "available"}),
        "sold_accounts":        await accountsdb.count_documents({"status": "sold"}),
        "total_orders":         await ordersdb.count_documents({}),
        "pending_orders":       await ordersdb.count_documents({"status": "pending"}),
        "completed_orders":     await ordersdb.count_documents({"status": "completed"}),
        "disputed_orders":      await ordersdb.count_documents({"status": "disputed"}),
        "volume":               await get_total_volume(),
        "fees":                 await get_total_fees_collected(),
        "pending_deposits":     await depositsdb.count_documents({"status": "pending"}),
        "pending_withdrawals":  await withdrawalsdb.count_documents({"status": "pending"}),
        "pending_sell_requests": await sellrequestsdb.count_documents({"status": "pending"}),
    }


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/admin/api/users", include_in_schema=False)
async def admin_users(_: str = Depends(_auth)):
    from server.utils.database.userdb import usersdb
    users = []
    async for u in usersdb.find({}).sort("joined_at", -1).limit(500):
        u.pop("_id", None)
        u.pop("two_fa_secret", None)
        u.pop("api_key", None)
        for f in ["joined_at"]:
            if u.get(f):
                u[f] = u[f].isoformat()
        users.append(u)
    return users


class BanIn(BaseModel):
    user_id: int
    reason: str = ""

@router.post("/admin/api/users/ban", include_in_schema=False)
async def admin_ban(body: BanIn, _: str = Depends(_auth)):
    from server.utils.database.userdb import usersdb
    await usersdb.update_one({"user_id": body.user_id}, {"$set": {"is_banned": True, "ban_reason": body.reason}})
    return {"ok": True}

@router.post("/admin/api/users/unban", include_in_schema=False)
async def admin_unban(body: BanIn, _: str = Depends(_auth)):
    from server.utils.database.userdb import usersdb
    await usersdb.update_one({"user_id": body.user_id}, {"$set": {"is_banned": False, "ban_reason": None}})
    return {"ok": True}


class BalanceIn(BaseModel):
    user_id: int
    amount: float

@router.post("/admin/api/users/balance", include_in_schema=False)
async def admin_balance(body: BalanceIn, _: str = Depends(_auth)):
    from server.utils.database.userdb import set_balance
    await set_balance(body.user_id, body.amount)
    return {"ok": True}


class RankIn(BaseModel):
    user_id: int
    rank: str

@router.post("/admin/api/users/rank", include_in_schema=False)
async def admin_rank(body: RankIn, _: str = Depends(_auth)):
    from server.utils.database.userdb import set_rank
    await set_rank(body.user_id, body.rank)
    return {"ok": True}


# ── Accounts ──────────────────────────────────────────────────────────────────

@router.get("/admin/api/accounts", include_in_schema=False)
async def admin_accounts(_: str = Depends(_auth)):
    from server.utils.database.accountdb import accountsdb
    acc = []
    async for a in accountsdb.find({}).sort("listed_at", -1).limit(500):
        a.pop("_id", None)
        for f in ["listed_at", "sold_at"]:
            if a.get(f):
                a[f] = a[f].isoformat()
        acc.append(a)
    return acc


class AccRemoveIn(BaseModel):
    account_id: str

@router.post("/admin/api/accounts/remove", include_in_schema=False)
async def admin_remove_acc(body: AccRemoveIn, _: str = Depends(_auth)):
    from server.utils.database.accountdb import accountsdb
    await accountsdb.update_one({"account_id": body.account_id}, {"$set": {"status": "removed"}})
    return {"ok": True}


# ── Orders ────────────────────────────────────────────────────────────────────

@router.get("/admin/api/orders", include_in_schema=False)
async def admin_orders(_: str = Depends(_auth)):
    from server.utils.database.orderdb import ordersdb
    orders = []
    async for o in ordersdb.find({}).sort("created_at", -1).limit(500):
        o.pop("_id", None)
        for f in ["created_at", "completed_at", "cancelled_at"]:
            if o.get(f):
                o[f] = o[f].isoformat()
        orders.append(o)
    return orders


class OrderActionIn(BaseModel):
    order_id: str
    reason: str = ""

@router.post("/admin/api/orders/complete", include_in_schema=False)
async def admin_order_complete(body: OrderActionIn, _: str = Depends(_auth)):
    from server.utils.database.orderdb import complete_order
    return {"ok": await complete_order(body.order_id)}

@router.post("/admin/api/orders/cancel", include_in_schema=False)
async def admin_order_cancel(body: OrderActionIn, _: str = Depends(_auth)):
    from server.utils.database.orderdb import cancel_order
    return {"ok": await cancel_order(body.order_id, body.reason)}

@router.post("/admin/api/orders/refund", include_in_schema=False)
async def admin_order_refund(body: OrderActionIn, _: str = Depends(_auth)):
    """
    Refund an order: change status to 'refunded' AND credit user's balance back.

    Two-phase idempotency pattern (mirrors confirm_deposit):
      Phase 1 — atomic status flip: pending/disputed → refunded, balance_refunded=False
      Phase 2 — credit balance + log, then set balance_refunded=True

    If the process crashes between Phase 1 and Phase 2, re-calling this endpoint
    with the same order_id detects {status: "refunded", balance_refunded: False}
    and safely re-applies Phase 2 only — no double-refund risk.
    """
    from server.utils.database.orderdb import ordersdb
    from server.utils.database.userdb import update_balance
    from server.utils.database.walletdb import log_transaction
    from datetime import datetime, timezone

    # Phase 1: atomically claim the refund from a refundable state
    order = await ordersdb.find_one_and_update(
        {"order_id": body.order_id, "status": {"$in": ["pending", "disputed"]}},
        {"$set": {
            "status":          "refunded",
            "refund_reason":   body.reason,
            "cancelled_at":    datetime.now(timezone.utc),
            "balance_refunded": False,  # Phase 2 will flip this
        }},
        # return_document=False → original doc before update
    )

    if order is None:
        # Not in a refundable state — check if it's a recoverable "refunded but uncredited" case
        order = await ordersdb.find_one(
            {"order_id": body.order_id, "status": "refunded", "balance_refunded": False}
        )
        if order is None:
            return {"ok": False, "error": "Order not found, not in a refundable state, or already fully refunded."}
        # Phase 1 done but Phase 2 interrupted — fall through to re-apply credit

    # Phase 2: credit buyer's balance and log
    await update_balance(order["buyer_id"], order["amount"])
    await log_transaction(
        user_id=order["buyer_id"],
        txn_type="refund",
        amount=order["amount"],
        ref_id=order["order_id"],
        note=f"Order refunded — {body.reason}" if body.reason else "Order refunded by admin",
    )

    # Mark Phase 2 complete
    await ordersdb.update_one(
        {"order_id": body.order_id},
        {"$set": {"balance_refunded": True}},
    )
    return {"ok": True, "refunded_amount": order["amount"], "buyer_id": order["buyer_id"]}


# ── Deposits ──────────────────────────────────────────────────────────────────

@router.get("/admin/api/deposits", include_in_schema=False)
async def admin_deposits(_: str = Depends(_auth)):
    from server.utils.database.walletdb import depositsdb
    deps = []
    async for d in depositsdb.find({}).sort("created_at", -1).limit(500):
        d.pop("_id", None)
        for f in ["created_at", "confirmed_at"]:
            if d.get(f):
                d[f] = d[f].isoformat()
        deps.append(d)
    return deps


class DepositActionIn(BaseModel):
    deposit_id: str
    note: str = ""

@router.post("/admin/api/deposits/approve", include_in_schema=False)
async def admin_deposit_approve(body: DepositActionIn, _: str = Depends(_auth)):
    # confirm_deposit now atomically confirms + credits user balance internally.
    from server.utils.database.walletdb import confirm_deposit
    dep = await confirm_deposit(body.deposit_id, confirmed_by=0)
    return {"ok": bool(dep)}

@router.post("/admin/api/deposits/reject", include_in_schema=False)
async def admin_deposit_reject(body: DepositActionIn, _: str = Depends(_auth)):
    from server.utils.database.walletdb import reject_deposit
    return {"ok": await reject_deposit(body.deposit_id, body.note)}


# ── Withdrawals ───────────────────────────────────────────────────────────────

@router.get("/admin/api/withdrawals", include_in_schema=False)
async def admin_withdrawals(_: str = Depends(_auth)):
    from server.utils.database.walletdb import withdrawalsdb
    wits = []
    async for w in withdrawalsdb.find({}).sort("created_at", -1).limit(500):
        w.pop("_id", None)
        for f in ["created_at", "processed_at"]:
            if w.get(f):
                w[f] = w[f].isoformat()
        wits.append(w)
    return wits


class WitApproveIn(BaseModel):
    withdrawal_id: str
    tx_hash: Optional[str] = None  # must be provided by admin after manual transfer

@router.post("/admin/api/withdrawals/approve", include_in_schema=False)
async def admin_wit_approve(body: WitApproveIn, _: str = Depends(_auth)):
    """
    Approve a withdrawal.

    State machine: pending → processing → completed (or reverted to pending on failure).

    - Atomically claims the withdrawal (pending → processing) first.
      Any concurrent duplicate approve call gets a 404 at this step.
    - tx_hash must be provided → mark completed immediately (manual approval).
    - Balance was pre-deducted when the user created the withdrawal — never deduct here.
    """
    from server.utils.database.walletdb import (
        claim_withdrawal_for_processing,
        complete_withdrawal,
        revert_withdrawal_to_pending,
    )

    # Step 1 — Atomically claim the withdrawal. Prevents double-payout.
    wit = await claim_withdrawal_for_processing(body.withdrawal_id, processed_by=0)
    if not wit:
        return {"ok": False, "error": "Withdrawal not found or already being processed."}

    tx_hash = body.tx_hash

    # tx_hash provided: complete immediately
    if tx_hash:
        await complete_withdrawal(body.withdrawal_id, tx_hash)
        return {"ok": True, "tx_hash": tx_hash}

    # No tx_hash — revert so admin can provide it
    await revert_withdrawal_to_pending(body.withdrawal_id)
    return {
        "ok": False,
        "error": "tx_hash is required. Send funds manually then re-approve with tx_hash.",
        "wallet_address": wit.get("wallet_address"),
        "network": wit.get("method"),
        "amount": wit.get("amount"),
    }


class WitRejectIn(BaseModel):
    withdrawal_id: str
    note: str = ""

@router.post("/admin/api/withdrawals/reject", include_in_schema=False)
async def admin_wit_reject(body: WitRejectIn, _: str = Depends(_auth)):
    """
    Reject a withdrawal and refund the balance (was pre-deducted at creation).
    """
    from server.utils.database.walletdb import reject_withdrawal, log_transaction
    from server.utils.database.userdb import update_balance

    wit = await reject_withdrawal(body.withdrawal_id, body.note)
    if wit:
        # Refund the pre-deducted balance
        await update_balance(wit["user_id"], wit["amount"])
        # Log the refund so the ledger is accurate
        await log_transaction(
            user_id=wit["user_id"],
            txn_type="refund",
            amount=wit["amount"],
            ref_id=wit["withdrawal_id"],
            note=f"Withdrawal rejected — refunded via {wit.get('method', '')}",
        )
    return {"ok": bool(wit)}


# ── Countries ─────────────────────────────────────────────────────────────────

@router.get("/admin/api/countries", include_in_schema=False)
async def admin_get_countries(_: str = Depends(_auth)):
    from server.utils.database.countrydb import get_all_countries
    return await get_all_countries()


class CountryUpsertIn(BaseModel):
    code: str
    country_name: str
    country_rank: int
    idc: str
    price: float = 0.0
    temp_disable: bool = False
    is_full: bool = False

@router.post("/admin/api/countries/upsert", include_in_schema=False)
async def admin_upsert_country(body: CountryUpsertIn, _: str = Depends(_auth)):
    from server.utils.database.countrydb import upsert_country
    await upsert_country(**body.dict())
    return {"ok": True}


class CountryDeleteIn(BaseModel):
    code: str

@router.post("/admin/api/countries/delete", include_in_schema=False)
async def admin_delete_country(body: CountryDeleteIn, _: str = Depends(_auth)):
    from server.utils.database.countrydb import delete_country
    ok = await delete_country(body.code)
    return {"ok": ok}


@router.post("/admin/api/countries/delete-all", include_in_schema=False)
async def admin_delete_all_countries(_: str = Depends(_auth)):
    from server.utils.database.countrydb import delete_all_countries
    count = await delete_all_countries()
    return {"ok": True, "deleted": count}


@router.get("/admin/api/countries/next-rank", include_in_schema=False)
async def admin_next_rank(_: str = Depends(_auth)):
    from server.utils.database.countrydb import get_next_rank
    return {"ok": True, "next_rank": await get_next_rank()}


class CountryToggleIn(BaseModel):
    code: str
    value: bool

@router.post("/admin/api/countries/toggle-temp-disable", include_in_schema=False)
async def admin_toggle_temp_disable(body: CountryToggleIn, _: str = Depends(_auth)):
    from server.utils.database.countrydb import set_country_field
    await set_country_field(body.code, "temp_disable", body.value)
    return {"ok": True}

@router.post("/admin/api/countries/toggle-full", include_in_schema=False)
async def admin_toggle_full(body: CountryToggleIn, _: str = Depends(_auth)):
    from server.utils.database.countrydb import set_country_field
    await set_country_field(body.code, "is_full", body.value)
    return {"ok": True}


class CountryPriceIn(BaseModel):
    code: str
    price: float

@router.post("/admin/api/countries/price", include_in_schema=False)
async def admin_country_price(body: CountryPriceIn, _: str = Depends(_auth)):
    from server.utils.database.countrydb import set_country_field
    await set_country_field(body.code, "price", round(body.price, 4))
    return {"ok": True}


class CountryRankIn(BaseModel):
    code: str
    country_rank: int

@router.post("/admin/api/countries/rank", include_in_schema=False)
async def admin_country_rank(body: CountryRankIn, _: str = Depends(_auth)):
    from server.utils.database.countrydb import set_country_field
    await set_country_field(body.code, "country_rank", body.country_rank)
    return {"ok": True}


# ── Sell Requests (User -> Server submissions awaiting admin review) ─────────

@router.get("/admin/api/sell-requests", include_in_schema=False)
async def admin_get_sell_requests(status: Optional[str] = None, _: str = Depends(_auth)):
    from server.utils.database.sellrequestdb import get_all_sell_requests
    return await get_all_sell_requests(status=status, limit=300)


class SellApproveIn(BaseModel):
    request_id: str
    final_price: float
    note: str = ""

@router.post("/admin/api/sell-requests/approve", include_in_schema=False)
async def admin_approve_sell_request(body: SellApproveIn, _: str = Depends(_auth)):
    from server.utils.database.sellrequestdb import approve_sell_request, get_sell_request
    from server.utils.database.userdb import record_sale
    from server.utils.database.walletdb import log_transaction

    req = await get_sell_request(body.request_id)
    if not req or req.get("status") != "pending":
        return {"ok": False}

    doc = await approve_sell_request(body.request_id, body.final_price, body.note)
    if not doc:
        return {"ok": False}

    # Move pending_balance → available; fall back to direct credit if inconsistent
    from server.utils.database.userdb import move_pending_to_available
    pending_amount = req.get("pending_amount") or req.get("offer_price", body.final_price)
    moved = await move_pending_to_available(req["user_id"], pending_amount)
    if not moved:
        await record_sale(req["user_id"], body.final_price)
    await log_transaction(
        user_id=req["user_id"],
        txn_type="sale",
        amount=body.final_price,
        note=f"Sold account — {req.get('country_name', req['code'])} ({req['code']})",
    )
    return {"ok": True}


class SellRejectIn(BaseModel):
    request_id: str
    note: str = ""

@router.post("/admin/api/sell-requests/reject", include_in_schema=False)
async def admin_reject_sell_request(body: SellRejectIn, _: str = Depends(_auth)):
    from server.utils.database.sellrequestdb import reject_sell_request
    ok = await reject_sell_request(body.request_id, body.note)
    return {"ok": ok}


# ── Session Accounts (zip-uploaded .session files) ───────────────────────────

@router.get("/admin/api/sessions", include_in_schema=False)
async def admin_get_sessions(
    country: Optional[str] = None,
    sold: Optional[bool] = None,
    _: str = Depends(_auth),
):
    from server.utils.database.sessiondb import get_all_session_accounts
    return await get_all_session_accounts(country_code=country, sold=sold, limit=300)


@router.get("/admin/api/sessions/stats", include_in_schema=False)
async def admin_session_stats(_: str = Depends(_auth)):
    from server.utils.database.sessiondb import get_session_stats
    return await get_session_stats()


class SessionDeleteIn(BaseModel):
    account_id: str

@router.post("/admin/api/sessions/delete", include_in_schema=False)
async def admin_delete_session(body: SessionDeleteIn, _: str = Depends(_auth)):
    from server.utils.database.sessiondb import delete_session_account
    ok = await delete_session_account(body.account_id)
    return {"ok": ok}


from fastapi import UploadFile, File, Form

@router.post("/admin/api/sessions/upload", include_in_schema=False)
async def admin_upload_sessions(
    file: UploadFile = File(...),
    _: str = Depends(_auth),
):
    """
    Upload a .zip or .session file through the full production pipeline
    (see server.stock.pipeline.process_uploaded_session for the exact
    step-by-step flow: proxy login, verification, fresh session, 2FA,
    termination of every other active session, then persistence).

    2FA is left disabled by default for this endpoint (no batch decision UI
    like the bot has) — sessions with an existing 2FA whose current password
    is unknown or wrong are returned with status "needs_2fa"/"tfa_password_wrong".
    Re-upload that .session file via this endpoint with its password set in
    the accompanying JSON/txt metadata (zip) or simply retry once you know it.

    Accounts whose phone number's country can't be auto-detected are
    returned with status "needs_country" rather than paused for manual input
    — this endpoint has no interactive resume flow (unlike the bot's
    /set_country), so they are simply reported and must be re-uploaded once
    the number itself is corrected.
    """
    import config as _cfg
    from server import bot as _bot

    channel_id = getattr(_cfg, "SESSION_CHANNEL_ID", None)
    if not channel_id:
        return {"ok": False, "error": "SESSION_CHANNEL_ID not configured"}

    raw   = await file.read()
    fname = (file.filename or "").lower()

    from server.stock.batch_runner import run_stock_batch
    from server.stock.zip_extraction import extract_sessions_from_zip

    # ── Build sessions list ───────────────────────────────────────────────
    if fname.endswith(".session"):
        phone = (file.filename or "").replace(".session", "").strip()
        if not phone.startswith("+"):
            phone = "+" + phone
        sessions = [{
            "phone": phone, "session_bytes": raw, "password": "",
            "api_id_override": None, "api_hash_override": None,
        }]

    elif fname.endswith(".zip"):
        try:
            sessions = extract_sessions_from_zip(raw)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if not sessions:
            return {"ok": False, "error": "No .session files found in the zip"}
    else:
        return {"ok": False, "error": "Only .zip or .session files accepted"}

    # ── Run pipeline (shared with the bot's upload handler) ───────────────
    outcomes = await run_stock_batch(
        sessions, channel_id, _cfg.API_ID, _cfg.API_HASH,
        batch_action="disable", batch_new_password=None, bot_client=_bot,
    )

    stats = {
        "total":         len(outcomes),
        "verified":      0,
        "invalid":       0,
        "duplicate":     0,
        "needs_2fa":     0,
        "needs_country": 0,
        "frozen":        0,
        "permanent_spam": 0,
        "failed":        0,
    }
    results = []
    for o in outcomes:
        stats[o.status] = stats.get(o.status, 0) + 1
        info = o.info or {}
        entry: dict = {"phone": o.phone, "ok": o.status == "verified", "status": o.status}

        if o.status == "duplicate":
            pass
        elif o.status == "needs_country":
            entry["message"] = "Country could not be auto-detected — correct the number and re-upload"
        elif o.status == "needs_2fa":
            entry["user_id"]  = info.get("user_id")
            entry["username"] = info.get("username")
            entry["status"]   = "tfa_password_wrong" if info.get("tfa_password_wrong") else "needs_2fa"
            entry["message"]  = (
                "Wrong current 2FA password — re-upload with correct password"
                if info.get("tfa_password_wrong")
                else "Re-upload with correct current 2FA password"
            )
        elif o.status in ("frozen", "permanent_spam"):
            entry["error"] = info.get("error")
        elif o.status == "invalid":
            entry["error"] = o.error or info.get("error")
        elif o.status == "failed":
            entry["status"] = "save_error"
            entry["error"]  = o.error
        elif o.status == "verified":
            entry.update({
                "account_id":        o.account_id,
                "user_id":           info.get("user_id"),
                "username":          info.get("username"),
                "first_name":        info.get("first_name"),
                "has_2fa":           info.get("has_2fa", False),
                "tfa_updated":       info.get("tfa_updated", False),
                "proxy_used":        info.get("proxy_used", "none"),
                "terminated_others": info.get("terminated_others", False),
            })

        results.append(entry)

    return {"ok": True, "summary": stats, "results": results}


# ── Mobile HTML SPA ───────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<meta name="theme-color" content="#0a0a16"/>
<meta name="mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<title>Admin — TG Market</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0a0a16;--card:#12122a;--card2:#181830;--border:#1e1e3c;
  --accent:#7c3aed;--accent2:#3b82f6;--glow:rgba(124,58,237,.2);
  --green:#10b981;--yellow:#f59e0b;--red:#ef4444;--cyan:#06b6d4;
  --text:#e8eaf6;--muted:#64748b;--muted2:#94a3b8;
  --nav-h:64px;--top-h:56px;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);
  font-family:-apple-system,'Segoe UI',sans-serif;font-size:14px}

/* ─── LOGIN ──────────────────────────────────────────────── */
#login{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  height:100vh;padding:24px;
  background:radial-gradient(ellipse at 50% 20%,rgba(124,58,237,.18) 0%,transparent 65%);
}
.login-box{width:100%;max-width:360px;text-align:center}
.login-icon{font-size:56px;margin-bottom:20px;line-height:1}
.login-box h1{font-size:22px;font-weight:800;letter-spacing:-.3px;margin-bottom:6px}
.login-box p{color:var(--muted);font-size:13px;margin-bottom:32px}
.login-box input{
  width:100%;padding:16px;background:var(--card);
  border:1.5px solid var(--border);border-radius:14px;
  color:var(--text);font-size:16px;outline:none;
  transition:.2s;margin-bottom:14px;letter-spacing:2px;text-align:center;
}
.login-box input:focus{border-color:var(--accent);box-shadow:0 0 0 4px var(--glow)}
.btn-login{
  width:100%;padding:16px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  border:none;border-radius:14px;color:#fff;font-size:16px;font-weight:700;
  cursor:pointer;transition:.15s;letter-spacing:.3px;
}
.btn-login:active{transform:scale(.97)}
#login-err{color:var(--red);font-size:13px;margin-top:10px;min-height:18px}

/* ─── APP SHELL ──────────────────────────────────────────── */
#app{display:none;flex-direction:column;height:100vh}

/* top bar */
#topbar{
  height:var(--top-h);background:var(--card);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;flex-shrink:0;position:sticky;top:0;z-index:50;
}
#topbar h2{font-size:17px;font-weight:700}
.topbar-right{display:flex;align-items:center;gap:10px}
.icon-btn{
  width:38px;height:38px;border-radius:10px;border:1px solid var(--border);
  background:transparent;color:var(--muted2);font-size:16px;
  display:flex;align-items:center;justify-content:center;cursor:pointer;
}
.icon-btn:active{background:var(--card2)}
.admin-pill{background:rgba(124,58,237,.15);color:var(--accent);
  padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700;
  border:1px solid rgba(124,58,237,.3)}

/* scroll area */
#content{
  flex:1;overflow-y:auto;overflow-x:hidden;
  padding:16px 16px calc(var(--nav-h) + 16px);
  -webkit-overflow-scrolling:touch;
}

/* bottom nav */
#bottom-nav{
  height:var(--nav-h);background:var(--card);
  border-top:1px solid var(--border);
  display:flex;align-items:stretch;
  position:fixed;bottom:0;left:0;right:0;z-index:100;
  overflow-x:auto;-webkit-overflow-scrolling:touch;
  scrollbar-width:none;
}
#bottom-nav::-webkit-scrollbar{display:none}
.nav-tab{
  min-width:58px;flex:1;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:3px;cursor:pointer;
  transition:.15s;border:none;background:transparent;color:var(--muted);
  padding:6px 4px;position:relative;
}
.nav-tab:active{background:rgba(255,255,255,.04)}
.nav-tab.active{color:var(--accent)}
.nav-tab .tab-icon{font-size:20px;line-height:1}
.nav-tab .tab-label{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.nav-dot{
  position:absolute;top:6px;right:calc(50% - 14px);
  width:8px;height:8px;border-radius:50%;
  background:var(--red);border:2px solid var(--card);display:none;
}
.nav-dot.warn{background:var(--yellow)}

/* ─── STAT CARDS ─────────────────────────────────────────── */
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.s-card{
  background:var(--card);border:1px solid var(--border);border-radius:14px;
  padding:14px;position:relative;overflow:hidden;
}
.s-card::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent2))}
.s-card.g::after{background:var(--green)}
.s-card.y::after{background:var(--yellow)}
.s-card.r::after{background:var(--red)}
.s-card.c::after{background:var(--cyan)}
.s-card .s-icon{font-size:22px;margin-bottom:8px}
.s-card .s-val{font-size:26px;font-weight:800;line-height:1;margin-bottom:4px}
.s-card .s-lbl{font-size:11px;color:var(--muted);font-weight:500}
.s-card .s-sub{font-size:10px;color:var(--muted);margin-top:4px}

/* ─── SECTION HEADER ─────────────────────────────────────── */
.sec-hdr{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:12px;
}
.sec-hdr h3{font-size:16px;font-weight:700}
.count-pill{background:var(--card2);color:var(--muted2);
  font-size:11px;padding:3px 8px;border-radius:8px;font-weight:600}

/* ─── SEARCH / FILTER BAR ────────────────────────────────── */
.filter-bar{
  display:flex;gap:8px;margin-bottom:12px;
  position:sticky;top:0;z-index:10;
  background:var(--bg);padding:8px 0 4px;margin-top:-8px;
}
.search-inp{
  flex:1;background:var(--card);border:1.5px solid var(--border);
  border-radius:10px;padding:10px 14px;color:var(--text);
  font-size:14px;outline:none;
}
.search-inp:focus{border-color:var(--accent)}
.filter-sel{
  background:var(--card);border:1.5px solid var(--border);
  border-radius:10px;padding:10px 10px;color:var(--text);
  font-size:12px;outline:none;cursor:pointer;min-width:90px;
}

/* ─── ITEM CARDS ─────────────────────────────────────────── */
.item-list{display:flex;flex-direction:column;gap:10px}
.item-card{
  background:var(--card);border:1px solid var(--border);
  border-radius:14px;padding:14px;
}
.item-card.pending{border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.04)}
.item-card.disputed{border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.04)}
.item-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.item-row:last-child{margin-bottom:0}
.item-id{font-family:monospace;font-size:11px;color:var(--muted2);
  background:var(--card2);padding:2px 8px;border-radius:6px;letter-spacing:.3px}
.item-name{font-weight:700;font-size:15px}
.item-sub{font-size:12px;color:var(--muted2);margin-top:2px}
.item-amount{font-size:18px;font-weight:800}
.item-amount.green{color:var(--green)}
.item-amount.red{color:var(--red)}
.item-amount.yellow{color:var(--yellow)}
.avatar{
  width:42px;height:42px;border-radius:12px;
  display:flex;align-items:center;justify-content:center;
  font-size:18px;flex-shrink:0;margin-right:12px;
  background:rgba(124,58,237,.15);
}
.item-main{display:flex;align-items:center;margin-bottom:10px}
.item-info{flex:1;min-width:0}
.item-info .name{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item-info .sub{font-size:12px;color:var(--muted);margin-top:2px}
.item-actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}

/* ─── BADGES ─────────────────────────────────────────────── */
.badge{display:inline-flex;align-items:center;padding:3px 9px;
  border-radius:8px;font-size:11px;font-weight:700;letter-spacing:.2px}
.b-green{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.2)}
.b-red{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.b-yellow{background:rgba(245,158,11,.15);color:var(--yellow);border:1px solid rgba(245,158,11,.2)}
.b-blue{background:rgba(59,130,246,.15);color:var(--accent2);border:1px solid rgba(59,130,246,.2)}
.b-purple{background:rgba(124,58,237,.15);color:var(--accent);border:1px solid rgba(124,58,237,.2)}
.b-gray{background:rgba(100,116,139,.1);color:var(--muted2);border:1px solid rgba(100,116,139,.2)}
.b-cyan{background:rgba(6,182,212,.15);color:var(--cyan);border:1px solid rgba(6,182,212,.2)}

/* ─── BUTTONS ────────────────────────────────────────────── */
.btn{
  display:inline-flex;align-items:center;justify-content:center;
  gap:5px;border:none;border-radius:10px;font-size:13px;font-weight:700;
  cursor:pointer;padding:10px 16px;transition:.15s;white-space:nowrap;
}
.btn:active{transform:scale(.95)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}
.btn-green{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3)}
.btn-red{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3)}
.btn-yellow{background:rgba(245,158,11,.15);color:var(--yellow);border:1px solid rgba(245,158,11,.3)}
.btn-blue{background:rgba(59,130,246,.15);color:var(--accent2);border:1px solid rgba(59,130,246,.3)}
.btn-ghost{background:transparent;color:var(--muted2);border:1px solid var(--border)}
.btn-block{width:100%;padding:14px}
.btn-sm{padding:8px 12px;font-size:12px;border-radius:8px}

/* ─── INFO ROWS ──────────────────────────────────────────── */
.info-row{
  display:flex;justify-content:space-between;align-items:center;
  padding:10px 0;border-bottom:1px solid rgba(255,255,255,.05);
  font-size:13px;
}
.info-row:last-child{border-bottom:none}
.info-row .lbl{color:var(--muted);font-weight:500}
.info-row .val{font-weight:600;text-align:right;max-width:60%;
  word-break:break-all;font-size:12px}
.info-row .val.mono{font-family:monospace;font-size:11px}

/* ─── BOTTOM SHEET ───────────────────────────────────────── */
.sheet-bg{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.65);backdrop-filter:blur(3px);
  z-index:200;align-items:flex-end;
}
.sheet-bg.open{display:flex}
.sheet{
  background:var(--card);border-radius:20px 20px 0 0;
  width:100%;padding:20px 20px 36px;
  transform:translateY(100%);transition:transform .3s cubic-bezier(.4,0,.2,1);
}
.sheet-bg.open .sheet{transform:translateY(0)}
.sheet-handle{width:36px;height:4px;background:var(--border);
  border-radius:2px;margin:0 auto 20px;display:block}
.sheet h3{font-size:17px;font-weight:800;margin-bottom:4px}
.sheet .sheet-sub{font-size:13px;color:var(--muted);margin-bottom:20px}
.sheet label{display:block;font-size:12px;color:var(--muted2);
  font-weight:600;margin-bottom:6px;margin-top:14px}
.sheet label:first-of-type{margin-top:0}
.sheet input,.sheet select,.sheet textarea{
  width:100%;background:var(--bg);border:1.5px solid var(--border);
  border-radius:10px;padding:12px 14px;color:var(--text);
  font-size:15px;outline:none;
}
.sheet input:focus,.sheet select:focus{border-color:var(--accent)}
.sheet-actions{display:flex;flex-direction:column;gap:10px;margin-top:20px}

/* ─── TOAST ──────────────────────────────────────────────── */
#toasts{
  position:fixed;top:calc(var(--top-h) + 10px);left:12px;right:12px;
  display:flex;flex-direction:column;gap:8px;z-index:999;pointer-events:none;
}
.toast{
  padding:12px 16px;border-radius:12px;font-size:13px;font-weight:600;
  display:flex;align-items:center;gap:8px;pointer-events:auto;
  animation:toastIn .25s ease;
  box-shadow:0 8px 32px rgba(0,0,0,.5);
}
.t-ok{background:#0a2e1f;border:1px solid rgba(16,185,129,.4);color:var(--green)}
.t-err{background:#2e0a0a;border:1px solid rgba(239,68,68,.4);color:var(--red)}
.t-info{background:#0a1a2e;border:1px solid rgba(6,182,212,.4);color:var(--cyan)}
@keyframes toastIn{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:none}}

/* ─── EMPTY / LOADER ─────────────────────────────────────── */
.empty{text-align:center;padding:48px 20px;color:var(--muted)}
.empty .ei{font-size:44px;margin-bottom:12px}
.loader-wrap{text-align:center;padding:48px 20px;color:var(--muted)}
.spin{display:inline-block;width:22px;height:22px;border:2px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;
  animation:spin .7s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}

/* ─── QUICK ACTION CARDS ─────────────────────────────────── */
.qa-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.qa-card{
  background:var(--card);border:1px solid var(--border);border-radius:14px;
  padding:16px 14px;text-align:center;cursor:pointer;transition:.15s;
}
.qa-card:active{transform:scale(.97);background:var(--card2)}
.qa-card .qa-icon{font-size:28px;margin-bottom:8px}
.qa-card .qa-lbl{font-size:12px;font-weight:700;color:var(--muted2)}
.qa-card .qa-val{font-size:20px;font-weight:800;margin-bottom:2px}
.qa-card.alert{border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.06)}
.qa-card.danger{border-color:rgba(239,68,68,.4);background:rgba(239,68,68,.06)}

/* ─── SCROLLBAR ──────────────────────────────────────────── */
::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
</style>
</head>
<body>

<!-- ═══ LOGIN ═══════════════════════════════════════════════════════════════ -->
<div id="login">
  <div class="login-box">
    <div class="login-icon">🤖</div>
    <h1>TG Market Admin</h1>
    <p>Enter password to continue</p>
    <input type="password" id="pw" placeholder="••••••••"
      autocomplete="current-password"
      onkeydown="if(event.key==='Enter')doLogin()"/>
    <button class="btn-login" onclick="doLogin()">🔐 Login</button>
    <div id="login-err"></div>
  </div>
</div>

<!-- ═══ APP ══════════════════════════════════════════════════════════════════ -->
<div id="app">

  <!-- Top Bar -->
  <div id="topbar">
    <h2 id="page-title">📊 Dashboard</h2>
    <div class="topbar-right">
      <span class="admin-pill">Admin</span>
      <button class="icon-btn" onclick="refreshCurrent()" title="Refresh">🔄</button>
      <button class="icon-btn" onclick="doLogout()" title="Logout">🚪</button>
    </div>
  </div>

  <!-- Scroll Content -->
  <div id="content">
    <div class="loader-wrap"><span class="spin"></span>Loading...</div>
  </div>

  <!-- Bottom Navigation -->
  <nav id="bottom-nav">
    <button class="nav-tab active" id="tab-dashboard" onclick="goto('dashboard')">
      <span class="tab-icon">📊</span>
      <span class="tab-label">Home</span>
    </button>
    <button class="nav-tab" id="tab-users" onclick="goto('users')">
      <span class="tab-icon">👥</span>
      <span class="tab-label">Users</span>
    </button>
    <button class="nav-tab" id="tab-accounts" onclick="goto('accounts')">
      <span class="tab-icon">📱</span>
      <span class="tab-label">Accounts</span>
    </button>
    <button class="nav-tab" id="tab-orders" onclick="goto('orders')">
      <span class="tab-icon">🛒</span>
      <span class="tab-label">Orders</span>
    </button>
    <button class="nav-tab" id="tab-deposits" onclick="goto('deposits')">
      <div class="nav-dot warn" id="dot-deposits"></div>
      <span class="tab-icon">📥</span>
      <span class="tab-label">Deposit</span>
    </button>
    <button class="nav-tab" id="tab-withdrawals" onclick="goto('withdrawals')">
      <div class="nav-dot" id="dot-withdrawals"></div>
      <span class="tab-icon">📤</span>
      <span class="tab-label">Withdraw</span>
    </button>
    <button class="nav-tab" id="tab-countries" onclick="goto('countries')">
      <span class="tab-icon">🌍</span>
      <span class="tab-label">Countries</span>
    </button>
    <button class="nav-tab" id="tab-sellrequests" onclick="goto('sellrequests')">
      <div class="nav-dot warn" id="dot-sellrequests"></div>
      <span class="tab-icon">📮</span>
      <span class="tab-label">Sell Reqs</span>
    </button>
    <button class="nav-tab" id="tab-sessions" onclick="goto('sessions')">
      <span class="tab-icon">📱</span>
      <span class="tab-label">Sessions</span>
    </button>
  </nav>
</div>

<!-- ═══ SHEETS ════════════════════════════════════════════════════════════════ -->

<!-- Ban Sheet -->
<div class="sheet-bg" id="sh-ban" onclick="closeSheet('sh-ban')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3>🚫 Ban User</h3>
    <p class="sheet-sub">User will be blocked from using the bot.</p>
    <input type="hidden" id="ban-uid"/>
    <label>Reason (optional)</label>
    <input type="text" id="ban-reason" placeholder="e.g. Spam, fraud..."/>
    <div class="sheet-actions">
      <button class="btn btn-red btn-block" onclick="confirmBan()">Ban User</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-ban')">Cancel</button>
    </div>
  </div>
</div>

<!-- Balance Sheet -->
<div class="sheet-bg" id="sh-balance" onclick="closeSheet('sh-balance')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3>💰 Set Balance</h3>
    <p class="sheet-sub">Override user balance directly.</p>
    <input type="hidden" id="bal-uid"/>
    <label>New Balance (USD)</label>
    <input type="number" id="bal-amount" placeholder="0.00" step="0.01" min="0" inputmode="decimal"/>
    <div class="sheet-actions">
      <button class="btn btn-primary btn-block" onclick="confirmBalance()">Set Balance</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-balance')">Cancel</button>
    </div>
  </div>
</div>

<!-- Rank Sheet -->
<div class="sheet-bg" id="sh-rank" onclick="closeSheet('sh-rank')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3>🏆 Set Rank</h3>
    <p class="sheet-sub">Manually assign a rank to this user.</p>
    <input type="hidden" id="rank-uid"/>
    <label>Select Rank</label>
    <select id="rank-val">
      <option>VIP1</option><option>VIP2</option><option>VIP3</option>
      <option>PREMIUM</option><option>DIAMOND</option>
    </select>
    <div class="sheet-actions">
      <button class="btn btn-primary btn-block" onclick="confirmRank()">Set Rank</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-rank')">Cancel</button>
    </div>
  </div>
</div>

<!-- Reject Sheet -->
<div class="sheet-bg" id="sh-reject" onclick="closeSheet('sh-reject')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="rej-title">❌ Reject</h3>
    <p class="sheet-sub">Provide a reason for rejection.</p>
    <input type="hidden" id="rej-id"/>
    <input type="hidden" id="rej-type"/>
    <label>Reason (optional)</label>
    <input type="text" id="rej-reason" placeholder="Optional..."/>
    <div class="sheet-actions">
      <button class="btn btn-red btn-block" onclick="confirmReject()">Reject</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-reject')">Cancel</button>
    </div>
  </div>
</div>

<!-- TX Hash Sheet -->
<div class="sheet-bg" id="sh-txhash" onclick="closeSheet('sh-txhash')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3>✅ Approve Withdrawal</h3>
    <p class="sheet-sub">Enter blockchain TX hash to confirm payment sent.</p>
    <input type="hidden" id="txh-id"/>
    <label>Transaction Hash</label>
    <input type="text" id="txh-val" placeholder="0x... or txid..."/>
    <div class="sheet-actions">
      <button class="btn btn-green btn-block" onclick="confirmApproveWit()">Approve & Send</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-txhash')">Cancel</button>
    </div>
  </div>
</div>

<!-- Sell Request Approve Sheet -->
<div class="sheet-bg" id="sh-sellapprove" onclick="closeSheet('sh-sellapprove')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="sellapp-title">✅ Approve Sell Request</h3>
    <p class="sheet-sub">Set the final payout price for this account.</p>
    <input type="hidden" id="sellapp-id"/>
    <label>Final Price ($)</label>
    <input type="number" id="sellapp-price" placeholder="e.g. 1.20" step="0.01" min="0" inputmode="decimal"/>
    <label>Note (optional)</label>
    <input type="text" id="sellapp-note" placeholder="Optional..."/>
    <div class="sheet-actions">
      <button class="btn btn-green btn-block" onclick="confirmApproveSell()">Approve & Pay</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-sellapprove')">Cancel</button>
    </div>
  </div>
</div>

<!-- Order Action Sheet -->
<div class="sheet-bg" id="sh-order" onclick="closeSheet('sh-order')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="oa-title">Order Action</h3>
    <p class="sheet-sub" id="oa-desc">Confirm this action.</p>
    <input type="hidden" id="oa-id"/>
    <input type="hidden" id="oa-action"/>
    <label>Reason (optional)</label>
    <input type="text" id="oa-reason" placeholder="Optional..."/>
    <div class="sheet-actions">
      <button class="btn btn-primary btn-block" id="oa-btn" onclick="confirmOrderAction()">Confirm</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-order')">Cancel</button>
    </div>
  </div>
</div>

<!-- Country Buy Price Sheet (what user pays to buy from us) -->
<div class="sheet-bg" id="sh-cprice" onclick="closeSheet('sh-cprice')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="cprice-title">🟢 Buy Price — User Pays Us</h3>
    <p class="sheet-sub">User buys account FROM us → user pays this amount. Keep this HIGHER than Sell Price.</p>
    <input type="hidden" id="cprice-code"/>
    <label>Buy Price — User Pays (USD)</label>
    <input type="number" id="cprice-val" placeholder="e.g. 2.50" step="0.01" min="0" inputmode="decimal"/>
    <div class="sheet-actions">
      <button class="btn btn-primary btn-block" onclick="confirmCPrice()">Set Buy Price</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-cprice')">Cancel</button>
    </div>
  </div>
</div>

<!-- Country Sell Price Sheet (what we pay user when they sell to us) -->
<div class="sheet-bg" id="sh-cbprice" onclick="closeSheet('sh-cbprice')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="cbprice-title">🔴 Sell Price — We Pay User</h3>
    <p class="sheet-sub">User sells account TO us → we pay this amount. Keep this LOWER than Buy Price.</p>
    <input type="hidden" id="cbprice-code"/>
    <label>Sell Price — We Pay (USD)</label>
    <input type="number" id="cbprice-val" placeholder="e.g. 0.40" step="0.01" min="0" inputmode="decimal"/>
    <div class="sheet-actions">
      <button class="btn btn-block" style="background:var(--purple,#7c3aed);color:#fff" onclick="confirmCSellPrice()">Set Sell Price</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-cbprice')">Cancel</button>
    </div>
  </div>
</div>

<!-- Country Rank Sheet -->
<div class="sheet-bg" id="sh-crank" onclick="closeSheet('sh-crank')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="crank-title">🏆 Set Rank</h3>
    <p class="sheet-sub">Lower number = shown higher in the list. Set to 1 to bring to top.</p>
    <input type="hidden" id="crank-code"/>
    <label>Rank</label>
    <input type="number" id="crank-val" placeholder="e.g. 1" step="1" min="1" inputmode="numeric"/>
    <div class="sheet-actions">
      <button class="btn btn-primary btn-block" onclick="confirmCRank()">Set Rank</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-crank')">Cancel</button>
    </div>
  </div>
</div>

<!-- Country Add/Edit Sheet -->
<div class="sheet-bg" id="sh-cadd" onclick="closeSheet('sh-cadd')">
  <div class="sheet" onclick="event.stopPropagation()">
    <span class="sheet-handle"></span>
    <h3 id="cadd-title">🌍 Add Country</h3>
    <p class="sheet-sub">Create or update a country's full record.</p>
    <input type="hidden" id="cadd-editing"/>
    <label>Country Name (with flag)</label>
    <input type="text" id="cadd-name" placeholder="e.g. Uzbekistan 🇺🇿"/>
    <label>ISO Code</label>
    <input type="text" id="cadd-code" placeholder="e.g. UZ" style="text-transform:uppercase"/>
    <label>Dialing Code</label>
    <input type="text" id="cadd-idc" placeholder="e.g. +998"/>
    <label>Rank (lower = higher up)</label>
    <input type="number" id="cadd-rank" placeholder="e.g. 40" step="1" min="1" inputmode="numeric"/>
    <label>Price (USD)</label>
    <input type="number" id="cadd-price" placeholder="e.g. 0.80" step="0.01" min="0" inputmode="decimal"/>
    <div class="sheet-actions">
      <button class="btn btn-primary btn-block" onclick="confirmCAdd()">Save Country</button>
      <button class="btn btn-ghost btn-block" onclick="closeSheet('sh-cadd')">Cancel</button>
    </div>
  </div>
</div>

<!-- Toast container -->
<div id="toasts"></div>

<script>
let _token = localStorage.getItem('admin_token') || '';
let _cur = 'dashboard';

// ─── UTILS ────────────────────────────────────────────────────────────────────

function toast(msg, type='ok') {
  const cls = {ok:'t-ok', err:'t-err', info:'t-info'};
  const ico = {ok:'✅', err:'❌', info:'ℹ️'};
  const el = document.createElement('div');
  el.className = 'toast ' + (cls[type]||'t-info');
  el.innerHTML = ico[type] + ' ' + msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, method='GET', body=null) {
  const opts = {
    method,
    headers: {'Content-Type':'application/json','Authorization':'Bearer '+_token}
  };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.detail || 'Request failed');
  return j;
}

function fmtDate(s) {
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'2-digit'});
}
function fmtDt(s) {
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'}) + ' ' +
         d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});
}
function trunc(s, n=20) { return s&&s.length>n ? s.slice(0,n)+'…' : (s||'—'); }
function $$(id){ return document.getElementById(id); }

function openSheet(id) {
  $$('sh-'+id).classList.add('open');
}
function closeSheet(id) {
  $$(id).classList.remove('open');
}

function statusBadge(s) {
  const m = {
    available:'b-green',sold:'b-purple',pending:'b-yellow',
    removed:'b-gray',disputed:'b-red',
    completed:'b-green',cancelled:'b-gray',refunded:'b-blue',
    fresh:'b-green',aged:'b-blue',premium:'b-purple',aged_premium:'b-cyan',
    rejected:'b-red',
  };
  return `<span class="badge ${m[s]||'b-gray'}">${s}</span>`;
}

function rankBadge(r) {
  const m = {VIP1:'b-gray',VIP2:'b-blue',VIP3:'b-cyan',PREMIUM:'b-purple',DIAMOND:'b-yellow'};
  return `<span class="badge ${m[r]||'b-gray'}">🏆 ${r}</span>`;
}

// ─── AUTH ──────────────────────────────────────────────────────────────────────

async function doLogin() {
  const pw = $$('pw').value;
  $$('login-err').textContent = '';
  try {
    const r = await fetch('/admin/api/login',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password:pw})
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail||'Wrong password');
    _token = j.token;
    localStorage.setItem('admin_token', _token);
    $$('login').style.display = 'none';
    $$('app').style.display = 'flex';
    goto('dashboard');
  } catch(e) {
    $$('login-err').textContent = e.message;
  }
}

async function doLogout() {
  try { await api('/admin/api/logout','POST'); } catch(_) {}
  localStorage.removeItem('admin_token');
  location.reload();
}

// ─── NAV ──────────────────────────────────────────────────────────────────────

const pageTitles = {
  dashboard:'📊 Dashboard', users:'👥 Users', accounts:'📱 Accounts',
  orders:'🛒 Orders', deposits:'📥 Deposits', withdrawals:'📤 Withdrawals',
  countries:'🌍 Countries', sellrequests:'📮 Sell Requests'
};

function goto(page) {
  _cur = page;
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  const tab = $$('tab-'+page);
  if (tab) tab.classList.add('active');
  $$('page-title').textContent = pageTitles[page]||page;
  loadPage(page);
}

function refreshCurrent() { loadPage(_cur); }

async function loadPage(page) {
  const c = $$('content');
  c.innerHTML = '<div class="loader-wrap"><span class="spin"></span>Loading...</div>';
  try {
    if (page==='dashboard')   await renderDashboard();
    else if (page==='users')       await renderUsers();
    else if (page==='accounts')    await renderAccounts();
    else if (page==='orders')      await renderOrders();
    else if (page==='deposits')    await renderDeposits();
    else if (page==='withdrawals') await renderWithdrawals();
    else if (page==='countries')   await renderCountries();
    else if (page==='sellrequests') await renderSellRequests();
  } catch(e) {
    c.innerHTML = `<div class="empty"><div class="ei">⚠️</div><p>${e.message}</p></div>`;
  }
}

// ─── DASHBOARD ────────────────────────────────────────────────────────────────

async function renderDashboard() {
  const s = await api('/admin/api/stats');

  // update nav dots
  $$('dot-deposits').style.display  = s.pending_deposits > 0  ? 'block' : 'none';
  $$('dot-withdrawals').style.display = s.pending_withdrawals > 0 ? 'block' : 'none';
  $$('dot-sellrequests').style.display = s.pending_sell_requests > 0 ? 'block' : 'none';

  $$('content').innerHTML = `
  <div class="stats-grid">
    <div class="s-card">
      <div class="s-icon">👥</div>
      <div class="s-val">${s.total_users}</div>
      <div class="s-lbl">Total Users</div>
      <div class="s-sub">${s.banned_users} banned</div>
    </div>
    <div class="s-card g">
      <div class="s-icon">📱</div>
      <div class="s-val">${s.available_accounts}</div>
      <div class="s-lbl">Available</div>
      <div class="s-sub">${s.sold_accounts} sold</div>
    </div>
    <div class="s-card c">
      <div class="s-icon">🛒</div>
      <div class="s-val">${s.total_orders}</div>
      <div class="s-lbl">Total Orders</div>
      <div class="s-sub">${s.completed_orders} done</div>
    </div>
    <div class="s-card r">
      <div class="s-icon">⚡</div>
      <div class="s-val">${s.disputed_orders}</div>
      <div class="s-lbl">Disputes</div>
      <div class="s-sub">Needs review</div>
    </div>
    <div class="s-card">
      <div class="s-icon">💰</div>
      <div class="s-val">$${Number(s.volume).toFixed(0)}</div>
      <div class="s-lbl">Total Volume</div>
      <div class="s-sub">Completed</div>
    </div>
    <div class="s-card g">
      <div class="s-icon">🏦</div>
      <div class="s-val">$${Number(s.fees).toFixed(0)}</div>
      <div class="s-lbl">Platform Fees</div>
      <div class="s-sub">5% per order</div>
    </div>
  </div>

  <div class="sec-hdr"><h3>⚡ Action Needed</h3></div>
  <div class="qa-grid">
    <div class="qa-card ${s.pending_deposits>0?'alert':''}" onclick="goto('deposits')">
      <div class="qa-icon">📥</div>
      <div class="qa-val" style="color:var(--yellow)">${s.pending_deposits}</div>
      <div class="qa-lbl">Pending Deposits</div>
    </div>
    <div class="qa-card ${s.pending_withdrawals>0?'danger':''}" onclick="goto('withdrawals')">
      <div class="qa-icon">📤</div>
      <div class="qa-val" style="color:var(--red)">${s.pending_withdrawals}</div>
      <div class="qa-lbl">Pending Withdrawals</div>
    </div>
    <div class="qa-card ${s.pending_orders>0?'alert':''}" onclick="goto('orders')">
      <div class="qa-icon">🛒</div>
      <div class="qa-val" style="color:var(--yellow)">${s.pending_orders}</div>
      <div class="qa-lbl">Pending Orders</div>
    </div>
    <div class="qa-card ${s.disputed_orders>0?'danger':''}" onclick="goto('orders')">
      <div class="qa-icon">🔥</div>
      <div class="qa-val" style="color:var(--red)">${s.disputed_orders}</div>
      <div class="qa-lbl">Disputes</div>
    </div>
    <div class="qa-card ${s.pending_sell_requests>0?'alert':''}" onclick="goto('sellrequests')">
      <div class="qa-icon">📮</div>
      <div class="qa-val" style="color:var(--yellow)">${s.pending_sell_requests}</div>
      <div class="qa-lbl">Sell Requests</div>
    </div>
  </div>
  `;
}

// ─── USERS ────────────────────────────────────────────────────────────────────

let _users = [], _uFilt = '';
async function renderUsers() {
  _users = await api('/admin/api/users');
  _uFilt = '';
  renderUsersList(_users);
}

function renderUsersList(list) {
  $$('content').innerHTML = `
  <div class="filter-bar">
    <input class="search-inp" placeholder="🔍 Search user..." value="${_uFilt}"
      oninput="filterUsers(this.value)"/>
    <select class="filter-sel" onchange="filterUsersByStatus(this.value)">
      <option value="">All</option>
      <option value="active">Active</option>
      <option value="banned">Banned</option>
    </select>
  </div>
  <div class="sec-hdr">
    <h3>Users</h3>
    <span class="count-pill">${list.length}</span>
  </div>
  <div class="item-list">
    ${list.length ? list.map(u => `
    <div class="item-card">
      <div class="item-main">
        <div class="avatar">${u.is_banned?'🚫':'👤'}</div>
        <div class="item-info">
          <div class="name">${u.full_name || u.username || 'Unknown'}</div>
          <div class="sub">${u.username?'@'+u.username+' · ':''}ID: ${u.user_id}</div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-size:16px;font-weight:800;color:var(--green)">$${Number(u.balance||0).toFixed(2)}</div>
          <div style="margin-top:4px">${rankBadge(u.rank||'VIP1')}</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        ${u.is_banned
          ? '<span class="badge b-red">🚫 Banned</span>'
          : '<span class="badge b-green">✓ Active</span>'}
        <span style="font-size:11px;color:var(--muted)">Joined ${fmtDate(u.joined_at)}</span>
        <span style="font-size:11px;color:var(--muted)">Buys: ${u.total_account_buy||0}</span>
      </div>
      <div class="item-actions">
        ${u.is_banned
          ? `<button class="btn btn-green btn-sm" onclick="unbanUser(${u.user_id})">✓ Unban</button>`
          : `<button class="btn btn-red btn-sm" onclick="openBan(${u.user_id})">🚫 Ban</button>`}
        <button class="btn btn-blue btn-sm" onclick="openBalance(${u.user_id},${u.balance||0})">💰 Balance</button>
        <button class="btn btn-yellow btn-sm" onclick="openRank(${u.user_id},'${u.rank||'VIP1'}')">🏆 Rank</button>
      </div>
    </div>
    `).join('') : '<div class="empty"><div class="ei">👤</div><p>No users found</p></div>'}
  </div>`;
}

function filterUsers(q) {
  _uFilt = q.toLowerCase();
  const f = _users.filter(u =>
    String(u.user_id).includes(_uFilt) ||
    (u.username||'').toLowerCase().includes(_uFilt) ||
    (u.full_name||'').toLowerCase().includes(_uFilt)
  );
  renderUsersList(f);
  $$('content').querySelector('.search-inp').value = q;
}
function filterUsersByStatus(s) {
  const f = s==='banned' ? _users.filter(u=>u.is_banned)
    : s==='active' ? _users.filter(u=>!u.is_banned) : _users;
  renderUsersList(f);
}

function openBan(uid) {
  $$('ban-uid').value = uid;
  $$('ban-reason').value = '';
  openSheet('ban');
}
async function confirmBan() {
  const uid = +$$('ban-uid').value;
  const reason = $$('ban-reason').value;
  try {
    await api('/admin/api/users/ban','POST',{user_id:uid,reason});
    closeSheet('sh-ban');
    toast('User banned');
    renderUsers();
  } catch(e) { toast(e.message,'err'); }
}
async function unbanUser(uid) {
  try {
    await api('/admin/api/users/unban','POST',{user_id:uid,reason:''});
    toast('User unbanned');
    renderUsers();
  } catch(e) { toast(e.message,'err'); }
}
function openBalance(uid, cur) {
  $$('bal-uid').value = uid;
  $$('bal-amount').value = cur;
  openSheet('balance');
}
async function confirmBalance() {
  const uid = +$$('bal-uid').value;
  const amount = +$$('bal-amount').value;
  try {
    await api('/admin/api/users/balance','POST',{user_id:uid,amount});
    closeSheet('sh-balance');
    toast('Balance set to $'+amount.toFixed(2));
    renderUsers();
  } catch(e) { toast(e.message,'err'); }
}
function openRank(uid, cur) {
  $$('rank-uid').value = uid;
  $$('rank-val').value = cur;
  openSheet('rank');
}
async function confirmRank() {
  const uid = +$$('rank-uid').value;
  const rank = $$('rank-val').value;
  try {
    await api('/admin/api/users/rank','POST',{user_id:uid,rank});
    closeSheet('sh-rank');
    toast('Rank set to '+rank);
    renderUsers();
  } catch(e) { toast(e.message,'err'); }
}

// ─── ACCOUNTS ─────────────────────────────────────────────────────────────────

let _accounts = [];
async function renderAccounts() {
  _accounts = await api('/admin/api/accounts');
  renderAccountsList(_accounts);
}

function renderAccountsList(list) {
  $$('content').innerHTML = `
  <div class="filter-bar">
    <input class="search-inp" placeholder="🔍 ID, country..."
      oninput="filterAccounts(this.value)"/>
    <select class="filter-sel" onchange="filterAccByStatus(this.value)">
      <option value="">All</option>
      <option value="available">Available</option>
      <option value="sold">Sold</option>
      <option value="pending">Pending</option>
      <option value="removed">Removed</option>
    </select>
  </div>
  <div class="sec-hdr"><h3>Accounts</h3><span class="count-pill">${list.length}</span></div>
  <div class="item-list">
    ${list.length ? list.map(a => `
    <div class="item-card">
      <div class="item-row">
        <span class="item-id">${a.account_id}</span>
        ${statusBadge(a.status)}
      </div>
      <div class="item-row">
        <div>
          <div style="font-weight:700;font-size:15px">🌐 ${a.country||'—'} (${a.country_code||'?'})</div>
          <div style="font-size:12px;color:var(--muted);margin-top:3px">
            ${statusBadge(a.category||'fresh')} · ${a.age_days||0} days old
          </div>
        </div>
        <div class="item-amount green">$${Number(a.price||0).toFixed(2)}</div>
      </div>
      <div class="info-row">
        <span class="lbl">Seller ID</span>
        <span class="val mono">${a.seller_id}</span>
      </div>
      <div class="info-row">
        <span class="lbl">Listed</span>
        <span class="val">${fmtDate(a.listed_at)}</span>
      </div>
      ${a.status==='sold' ? `<div class="info-row"><span class="lbl">Sold</span><span class="val">${fmtDate(a.sold_at)}</span></div>` : ''}
      ${a.status !== 'removed' ? `
      <div style="margin-top:10px">
        <button class="btn btn-red btn-sm" onclick="removeAccount('${a.account_id}')">🗑 Remove</button>
      </div>` : `<div style="margin-top:8px"><span class="badge b-gray">Removed</span></div>`}
    </div>
    `).join('') : '<div class="empty"><div class="ei">📱</div><p>No accounts</p></div>'}
  </div>`;
}

function filterAccounts(q) {
  q = q.toLowerCase();
  renderAccountsList(_accounts.filter(a =>
    a.account_id.toLowerCase().includes(q) ||
    (a.country||'').toLowerCase().includes(q) ||
    (a.country_code||'').toLowerCase().includes(q)
  ));
}
function filterAccByStatus(s) {
  renderAccountsList(s ? _accounts.filter(a=>a.status===s) : _accounts);
}
async function removeAccount(id) {
  if (!confirm('Remove this account?')) return;
  try {
    await api('/admin/api/accounts/remove','POST',{account_id:id});
    toast('Account removed');
    renderAccounts();
  } catch(e) { toast(e.message,'err'); }
}

// ─── ORDERS ───────────────────────────────────────────────────────────────────

let _orders = [];
async function renderOrders() {
  _orders = await api('/admin/api/orders');
  renderOrdersList(_orders);
}

function renderOrdersList(list) {
  $$('content').innerHTML = `
  <div class="filter-bar">
    <input class="search-inp" placeholder="🔍 Order ID, user..."
      oninput="filterOrders(this.value)"/>
    <select class="filter-sel" onchange="filterOrdByStatus(this.value)">
      <option value="">All</option>
      <option value="pending">Pending</option>
      <option value="completed">Completed</option>
      <option value="disputed">Disputed</option>
      <option value="cancelled">Cancelled</option>
      <option value="refunded">Refunded</option>
    </select>
  </div>
  <div class="sec-hdr"><h3>Orders</h3><span class="count-pill">${list.length}</span></div>
  <div class="item-list">
    ${list.length ? list.map(o => `
    <div class="item-card ${o.status==='disputed'?'disputed':o.status==='pending'?'pending':''}">
      <div class="item-row">
        <span class="item-id">${o.order_id}</span>
        ${statusBadge(o.status)}
      </div>
      <div class="item-row" style="margin-top:8px">
        <div>
          <div style="font-size:12px;color:var(--muted)">Buyer → Seller</div>
          <div style="font-weight:600;font-size:13px;font-family:monospace">${o.buyer_id} → ${o.seller_id}</div>
        </div>
        <div style="text-align:right">
          <div class="item-amount green">$${Number(o.amount||0).toFixed(2)}</div>
          <div style="font-size:11px;color:var(--yellow)">Fee: $${Number(o.fee||0).toFixed(2)}</div>
        </div>
      </div>
      <div class="info-row">
        <span class="lbl">Account</span>
        <span class="val mono">${o.account_id}</span>
      </div>
      <div class="info-row">
        <span class="lbl">Created</span>
        <span class="val">${fmtDt(o.created_at)}</span>
      </div>
      ${(o.status==='pending'||o.status==='disputed') ? `
      <div class="item-actions" style="margin-top:10px">
        ${o.status==='pending' ? `
        <button class="btn btn-green btn-sm" onclick="openOA('${o.order_id}','complete')">✅ Complete</button>
        <button class="btn btn-ghost btn-sm" onclick="openOA('${o.order_id}','cancel')">Cancel</button>
        ` : ''}
        <button class="btn btn-yellow btn-sm" onclick="openOA('${o.order_id}','refund')">💸 Refund</button>
      </div>` : ''}
    </div>
    `).join('') : '<div class="empty"><div class="ei">🛒</div><p>No orders</p></div>'}
  </div>`;
}

function filterOrders(q) {
  q = q.toLowerCase();
  renderOrdersList(_orders.filter(o =>
    o.order_id.toLowerCase().includes(q) ||
    String(o.buyer_id).includes(q) ||
    String(o.seller_id).includes(q)
  ));
}
function filterOrdByStatus(s) {
  renderOrdersList(s ? _orders.filter(o=>o.status===s) : _orders);
}

const oaCfg = {
  complete:{t:'✅ Complete Order',d:'Mark as completed. Seller receives payment.',c:'btn-green'},
  cancel:  {t:'❌ Cancel Order',  d:'Cancel this pending order.',c:'btn-red'},
  refund:  {t:'💸 Refund Order',  d:'Refund buyer. Balance restored.',c:'btn-yellow'},
};
function openOA(id, action) {
  const cfg = oaCfg[action];
  $$('oa-id').value = id; $$('oa-action').value = action;
  $$('oa-title').textContent = cfg.t; $$('oa-desc').textContent = cfg.d;
  $$('oa-btn').className = `btn ${cfg.c} btn-block`;
  $$('oa-reason').value = '';
  openSheet('order');
}
async function confirmOrderAction() {
  const id = $$('oa-id').value;
  const action = $$('oa-action').value;
  const reason = $$('oa-reason').value;
  try {
    const r = await api(`/admin/api/orders/${action}`,'POST',{order_id:id,reason});
    if (!r.ok) throw new Error('Could not apply — order may have changed');
    closeSheet('sh-order');
    toast('Order updated');
    renderOrders();
  } catch(e) { toast(e.message,'err'); }
}

// ─── DEPOSITS ─────────────────────────────────────────────────────────────────

let _deposits = [];
async function renderDeposits() {
  _deposits = await api('/admin/api/deposits');
  renderDepList(_deposits.slice().sort((a,b)=>a.status==='pending'?-1:1));
}

function renderDepList(list) {
  const pending = list.filter(d=>d.status==='pending').length;
  $$('content').innerHTML = `
  <div class="filter-bar">
    <input class="search-inp" placeholder="🔍 Deposit ID, user..."
      oninput="filterDeps(this.value)"/>
    <select class="filter-sel" onchange="filterDepByStatus(this.value)">
      <option value="pending">Pending</option>
      <option value="">All</option>
      <option value="completed">Completed</option>
      <option value="rejected">Rejected</option>
    </select>
  </div>
  <div class="sec-hdr">
    <h3>Deposits</h3>
    <div style="display:flex;gap:6px;align-items:center">
      ${pending ? `<span class="badge b-yellow">⚠️ ${pending} Pending</span>` : ''}
      <span class="count-pill">${list.length}</span>
    </div>
  </div>
  <div class="item-list">
    ${list.length ? list.map(d => `
    <div class="item-card ${d.status==='pending'?'pending':''}">
      <div class="item-row">
        <span class="item-id">${d.deposit_id}</span>
        ${statusBadge(d.status)}
      </div>
      <div class="item-row" style="margin-top:8px">
        <div>
          <div style="font-size:12px;color:var(--muted)">User ID</div>
          <div style="font-weight:700;font-family:monospace">${d.user_id}</div>
        </div>
        <div class="item-amount green">$${Number(d.amount||0).toFixed(2)}</div>
      </div>
      <div class="info-row">
        <span class="lbl">Method</span>
        <span class="val">${d.method||'—'}</span>
      </div>
      ${d.tx_hash ? `<div class="info-row"><span class="lbl">TX Hash</span><span class="val mono">${trunc(d.tx_hash,22)}</span></div>` : ''}
      <div class="info-row">
        <span class="lbl">Time</span>
        <span class="val">${fmtDt(d.created_at)}</span>
      </div>
      ${d.status==='pending' ? `
      <div class="item-actions" style="margin-top:10px">
        <button class="btn btn-green btn-sm" onclick="approveDeposit('${d.deposit_id}')">✅ Approve</button>
        <button class="btn btn-red btn-sm" onclick="openReject('${d.deposit_id}','deposit')">❌ Reject</button>
      </div>` : ''}
    </div>
    `).join('') : '<div class="empty"><div class="ei">📥</div><p>No deposits</p></div>'}
  </div>`;
}

function filterDeps(q) {
  q=q.toLowerCase();
  renderDepList(_deposits.filter(d=>
    d.deposit_id.toLowerCase().includes(q)||String(d.user_id).includes(q)
  ));
}
function filterDepByStatus(s) {
  const f = s ? _deposits.filter(d=>d.status===s) : _deposits;
  renderDepList(f.slice().sort((a,b)=>a.status==='pending'?-1:1));
}
async function approveDeposit(id) {
  try {
    const r = await api('/admin/api/deposits/approve','POST',{deposit_id:id});
    if (!r.ok) throw new Error('Already processed or not found');
    toast('Deposit approved — balance credited');
    renderDeposits();
  } catch(e) { toast(e.message,'err'); }
}

// ─── WITHDRAWALS ──────────────────────────────────────────────────────────────

let _withdrawals = [];
async function renderWithdrawals() {
  _withdrawals = await api('/admin/api/withdrawals');
  renderWitList(_withdrawals.slice().sort((a,b)=>(a.status==='pending'||a.status==='processing')?-1:1));
}

function renderWitList(list) {
  const pending = list.filter(w=>w.status==='pending'||w.status==='processing').length;
  $$('content').innerHTML = `
  <div class="filter-bar">
    <input class="search-inp" placeholder="🔍 Withdrawal ID, user..."
      oninput="filterWits(this.value)"/>
    <select class="filter-sel" onchange="filterWitByStatus(this.value)">
      <option value="pending">Pending</option>
      <option value="processing">Processing</option>
      <option value="">All</option>
      <option value="completed">Completed</option>
      <option value="rejected">Rejected</option>
    </select>
  </div>
  <div class="sec-hdr">
    <h3>Withdrawals</h3>
    <div style="display:flex;gap:6px;align-items:center">
      ${pending ? `<span class="badge b-red">🔴 ${pending} Pending</span>` : ''}
      <span class="count-pill">${list.length}</span>
    </div>
  </div>
  <div class="item-list">
    ${list.length ? list.map(w => `
    <div class="item-card ${(w.status==='pending'||w.status==='processing')?'pending':''}">
      <div class="item-row">
        <span class="item-id">${w.withdrawal_id}</span>
        ${statusBadge(w.status)}
      </div>
      <div class="item-row" style="margin-top:8px">
        <div>
          <div style="font-size:12px;color:var(--muted)">User ID</div>
          <div style="font-weight:700;font-family:monospace">${w.user_id}</div>
        </div>
        <div class="item-amount red">-$${Number(w.amount||0).toFixed(2)}</div>
      </div>
      <div class="info-row">
        <span class="lbl">Method</span>
        <span class="val">${w.method||'—'}</span>
      </div>
      <div class="info-row">
        <span class="lbl">Wallet</span>
        <span class="val mono" title="${w.wallet_address||''}">${trunc(w.wallet_address||'—',22)}</span>
      </div>
      ${w.tx_hash ? `<div class="info-row"><span class="lbl">TX Hash</span><span class="val mono">${trunc(w.tx_hash,22)}</span></div>` : ''}
      <div class="info-row">
        <span class="lbl">Time</span>
        <span class="val">${fmtDt(w.created_at)}</span>
      </div>
      ${w.status==='pending' ? `
      <div class="item-actions" style="margin-top:10px">
        <button class="btn btn-green btn-sm" onclick="openApproveWit('${w.withdrawal_id}')">✅ Approve</button>
        <button class="btn btn-red btn-sm" onclick="openReject('${w.withdrawal_id}','withdrawal')">❌ Reject</button>
      </div>` : w.status==='processing' ? `
      <div class="item-actions" style="margin-top:10px">
        <span class="badge b-yellow">⏳ Processing payout…</span>
      </div>` : ''}
    </div>
    `).join('') : '<div class="empty"><div class="ei">📤</div><p>No withdrawals</p></div>'}
  </div>`;
}

function filterWits(q) {
  q=q.toLowerCase();
  renderWitList(_withdrawals.filter(w=>
    w.withdrawal_id.toLowerCase().includes(q)||String(w.user_id).includes(q)
  ));
}
function filterWitByStatus(s) {
  const f = s ? _withdrawals.filter(w=>w.status===s) : _withdrawals;
  renderWitList(f.slice().sort((a,b)=>a.status==='pending'?-1:1));
}

function openApproveWit(id) {
  $$('txh-id').value = id; $$('txh-val').value = '';
  openSheet('txhash');
}
async function confirmApproveWit() {
  const id = $('txh-id').value;
  const txHash = $('txh-val').value.trim();
  // tx_hash is optional when OXAPAY_PAYOUT_KEY is configured (auto-payout)
  try {
    const body = {withdrawal_id:id};
    if (txHash) body.tx_hash = txHash;
    const r = await api('/admin/api/withdrawals/approve','POST',body);
    if (!r.ok) throw new Error(r.error || 'Already processed or not found');
    closeSheet('sh-txhash');
    toast('Withdrawal approved');
    renderWithdrawals();
  } catch(e) { toast(e.message,'err'); }
}

// ─── SHARED REJECT ────────────────────────────────────────────────────────────

function openReject(id, type) {
  $$('rej-id').value = id; $$('rej-type').value = type;
  $$('rej-title').textContent = type==='deposit' ? '❌ Reject Deposit'
    : type==='withdrawal' ? '❌ Reject Withdrawal' : '❌ Reject Sell Request';
  $$('rej-reason').value = '';
  openSheet('reject');
}
async function confirmReject() {
  const id = $$('rej-id').value;
  const type = $$('rej-type').value;
  const note = $$('rej-reason').value;
  if (type==='sell') {
    try {
      const r = await api('/admin/api/sell-requests/reject','POST',{request_id:id, note});
      if (!r.ok) throw new Error('Already processed or not found');
      closeSheet('sh-reject');
      toast('Sell request rejected');
      renderSellRequests();
    } catch(e) { toast(e.message,'err'); }
    return;
  }
  try {
    await api(`/admin/api/${type}s/reject`,'POST',
      {[type==='deposit'?'deposit_id':'withdrawal_id']:id, note});
    closeSheet('sh-reject');
    toast(`${type} rejected`);
    type==='deposit' ? renderDeposits() : renderWithdrawals();
  } catch(e) { toast(e.message,'err'); }
}

// ─── SELL REQUESTS ────────────────────────────────────────────────────────────

let _sellReqs = [];
async function renderSellRequests() {
  _sellReqs = await api('/admin/api/sell-requests');
  renderSellReqList(_sellReqs.slice().sort((a,b)=>a.status==='pending'?-1:1));
}

function renderSellReqList(list) {
  const pending = list.filter(r=>r.status==='pending').length;
  $$('content').innerHTML = `
  <div class="filter-bar">
    <input class="search-inp" placeholder="🔍 Request ID, user, phone..."
      oninput="filterSellReqs(this.value)"/>
    <select class="filter-sel" onchange="filterSellReqByStatus(this.value)">
      <option value="pending">Pending</option>
      <option value="">All</option>
      <option value="paid">Paid</option>
      <option value="rejected">Rejected</option>
    </select>
  </div>
  <div class="sec-hdr">
    <h3>Sell Requests</h3>
    <div style="display:flex;gap:6px;align-items:center">
      ${pending ? `<span class="badge b-yellow">⚠️ ${pending} Pending</span>` : ''}
      <span class="count-pill">${list.length}</span>
    </div>
  </div>
  <div class="item-list">
    ${list.length ? list.map(r => `
    <div class="item-card ${r.status==='pending'?'pending':''}">
      <div class="item-row">
        <span class="item-id">${r.request_id}</span>
        ${statusBadge(r.status)}
      </div>
      <div class="item-row" style="margin-top:8px">
        <div>
          <div style="font-size:12px;color:var(--muted)">User ID</div>
          <div style="font-weight:700;font-family:monospace">${r.user_id}</div>
        </div>
        <div class="item-amount green">$${Number(r.final_price ?? r.offer_price ?? 0).toFixed(2)}</div>
      </div>
      <div class="info-row">
        <span class="lbl">Country</span>
        <span class="val">${r.country_name || r.code}</span>
      </div>
      <div class="info-row">
        <span class="lbl">Phone</span>
        <span class="val mono">${r.phone}</span>
      </div>
      <div class="info-row">
        <span class="lbl">Offer Price</span>
        <span class="val">$${Number(r.offer_price||0).toFixed(2)}</span>
      </div>
      ${r.admin_note ? `<div class="info-row"><span class="lbl">Note</span><span class="val">${r.admin_note}</span></div>` : ''}
      <div class="info-row">
        <span class="lbl">Submitted</span>
        <span class="val">${fmtDt(r.submitted_at)}</span>
      </div>
      ${r.status==='pending' ? `
      <div class="item-actions" style="margin-top:10px">
        <button class="btn btn-green btn-sm" onclick="openApproveSell('${r.request_id}',${r.offer_price||0})">✅ Approve</button>
        <button class="btn btn-red btn-sm" onclick="openReject('${r.request_id}','sell')">❌ Reject</button>
      </div>` : ''}
    </div>
    `).join('') : '<div class="empty"><div class="ei">📮</div><p>No sell requests</p></div>'}
  </div>`;
}

function filterSellReqs(q) {
  q=q.toLowerCase();
  renderSellReqList(_sellReqs.filter(r=>
    r.request_id.toLowerCase().includes(q)||String(r.user_id).includes(q)||(r.phone||'').includes(q)
  ));
}
function filterSellReqByStatus(s) {
  const f = s ? _sellReqs.filter(r=>r.status===s) : _sellReqs;
  renderSellReqList(f.slice().sort((a,b)=>a.status==='pending'?-1:1));
}
function openApproveSell(id, offerPrice) {
  $$('sellapp-id').value = id;
  $$('sellapp-title').textContent = `✅ Approve — ${id}`;
  $$('sellapp-price').value = offerPrice || '';
  $$('sellapp-note').value = '';
  openSheet('sellapprove');
}
async function confirmApproveSell() {
  const id = $$('sellapp-id').value;
  const price = parseFloat($$('sellapp-price').value);
  const note = $$('sellapp-note').value;
  if (isNaN(price) || price < 0) { toast('Enter a valid price','err'); return; }
  try {
    const r = await api('/admin/api/sell-requests/approve','POST',{request_id:id, final_price:price, note});
    if (!r.ok) throw new Error('Already processed or not found');
    closeSheet('sh-sellapprove');
    toast('Sell request approved — user paid');
    renderSellRequests();
  } catch(e) { toast(e.message,'err'); }
}

// ─── COUNTRIES ────────────────────────────────────────────────────────────────

let _countries = [], _cFilt = '', _cBuyFilt = '', _cSellFilt = '';

async function renderCountries() {
  _countries = await api('/admin/api/countries');
  _cFilt = ''; _cBuyFilt = ''; _cSellFilt = '';
  renderCountriesList(_countries);
}

function applyCountryFilters() {
  let list = _countries;
  if (_cFilt) {
    const q = _cFilt.toLowerCase();
    list = list.filter(c =>
      c.country_name.toLowerCase().includes(q) ||
      c.code.toLowerCase().includes(q)
    );
  }
  if (_cBuyFilt  !== '') list = list.filter(c => c.temp_disable === (_cBuyFilt  === 'true'));
  if (_cSellFilt !== '') list = list.filter(c => c.is_full      === (_cSellFilt === 'true'));
  return list;
}

function renderCountriesList(list) {
  const total = _countries.length;
  const buyOn  = _countries.filter(c=>!c.temp_disable).length;
  const sellOn = _countries.filter(c=>!c.is_full).length;
  $('content').innerHTML = `
  <div class="filter-bar" style="flex-wrap:wrap;gap:6px">
    <input class="search-inp" placeholder="🔍 Country name or code..."
      value="${_cFilt}"
      oninput="_cFilt=this.value;renderCountriesList(applyCountryFilters())"
      style="min-width:140px"/>
    <select class="filter-sel" style="min-width:95px"
      onchange="_cBuyFilt=this.value;renderCountriesList(applyCountryFilters())">
      <option value=""     ${_cBuyFilt===''     ?'selected':''}>All Buy</option>
      <option value="false" ${_cBuyFilt==='false' ?'selected':''}>✅ Buy ON</option>
      <option value="true"${_cBuyFilt==='true'?'selected':''}>⏸ Buy Disabled</option>
    </select>
    <select class="filter-sel" style="min-width:95px"
      onchange="_cSellFilt=this.value;renderCountriesList(applyCountryFilters())">
      <option value=""     ${_cSellFilt===''     ?'selected':''}>All Sell</option>
      <option value="false" ${_cSellFilt==='false' ?'selected':''}>✅ Sell ON</option>
      <option value="true"${_cSellFilt==='true'?'selected':''}>🈵 Full</option>
    </select>
  </div>

  <div class="sec-hdr">
    <h3>Countries</h3>
    <div style="display:flex;gap:6px;align-items:center">
      <span class="badge b-blue">${buyOn} Buy</span>
      <span class="badge b-green">${sellOn} Sell</span>
      <span class="count-pill">${list.length}/${total}</span>
    </div>
  </div>

  <div style="margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn btn-primary btn-sm" onclick="openCAdd()">
      ➕ Add Country
    </button>
    <button class="btn btn-red btn-sm" onclick="confirmDeleteAll()">
      🗑️ Delete All
    </button>
  </div>

  <div class="item-list">
    ${list.length ? list.map(c => `
    <div class="item-card" id="ccard-${c.code}">
      <div class="item-row">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="badge b-purple" style="font-family:monospace">#${c.country_rank}</span>
          <div>
            <div style="font-weight:700;font-size:15px">${c.country_name}</div>
            <div style="font-size:11px;color:var(--muted);font-family:monospace">${c.code} · ${c.idc}</div>
          </div>
        </div>
        <div style="text-align:right;flex-shrink:0">
          <div style="font-size:15px;font-weight:800;color:var(--green)">🟢 Buy ${Number(c.price||0).toFixed(2)}</div>
          <div style="font-size:13px;font-weight:700;color:var(--purple,#7c3aed)">🔴 Sell ${Number(c.sell_price||0).toFixed(2)}</div>
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">
        ${!c.temp_disable
          ? '<span class="badge b-blue">✅ Buy ON</span>'
          : '<span class="badge b-gray">⏸ Buy Disabled</span>'}
        ${!c.is_full
          ? '<span class="badge b-green">✅ Sell ON</span>'
          : '<span class="badge b-gray">🈵 Sell Full</span>'}
      </div>
      <div class="item-actions">
        <button class="btn btn-sm ${!c.temp_disable?'btn-red':'btn-blue'}"
          onclick="toggleTempDisable('${c.code}',${!c.temp_disable})">
          ${!c.temp_disable ? '⏸ Disable Buy' : '▶ Enable Buy'}
        </button>
        <button class="btn btn-sm ${!c.is_full?'btn-red':'btn-green'}"
          onclick="toggleFull('${c.code}',${!c.is_full})">
          ${!c.is_full ? '🈵 Mark Full' : '▶ Enable Sell'}
        </button>
        <button class="btn btn-yellow btn-sm"
          onclick="openCPrice('${c.code}','${c.country_name}',${c.price||0})">
          💰 Sell Price
        </button>
        <button class="btn btn-sm" style="background:var(--purple,#7c3aed);color:#fff"
          onclick="openCSellPrice('${c.code}','${c.country_name}',${c.sell_price||0})">
          💸 Sell Price
        </button>
        <button class="btn btn-ghost btn-sm"
          onclick="openCRank('${c.code}','${c.country_name}',${c.country_rank||1})">
          🏆 Rank
        </button>
        <button class="btn btn-ghost btn-sm" onclick="openCAdd('${c.code}')">
          ✏️ Edit
        </button>
        <button class="btn btn-red btn-sm" onclick="confirmDeleteCountry('${c.code}')">
          🗑️ Delete
        </button>
      </div>
    </div>
    `).join('') : '<div class="empty"><div class="ei">🌍</div><p>No countries match</p></div>'}
  </div>`;
}

async function toggleTempDisable(code, val) {
  try {
    await api('/admin/api/countries/toggle-temp-disable','POST',{code,value:val});
    const c = _countries.find(c=>c.code===code);
    if (c) c.temp_disable = val;
    renderCountriesList(applyCountryFilters());
    toast(`${code} Buy ${val?'disabled':'enabled'}`);
  } catch(e) { toast(e.message,'err'); }
}

async function toggleFull(code, val) {
  try {
    await api('/admin/api/countries/toggle-full','POST',{code,value:val});
    const c = _countries.find(c=>c.code===code);
    if (c) c.is_full = val;
    renderCountriesList(applyCountryFilters());
    toast(`${code} Sell ${val?'marked full':'enabled'}`);
  } catch(e) { toast(e.message,'err'); }
}

function openCPrice(code, name, cur) {
  $('cprice-code').value = code;
  $('cprice-title').textContent = `🟢 Buy Price (User Pays Us) — ${code} ${name}`;
  $('cprice-val').value = cur;
  openSheet('cprice');
}
async function confirmCPrice() {
  const code  = $('cprice-code').value;
  const price = parseFloat($('cprice-val').value);
  if (isNaN(price) || price < 0) { toast('Enter a valid price','err'); return; }
  try {
    await api('/admin/api/countries/price','POST',{code,price});
    const c = _countries.find(c=>c.code===code);
    if (c) c.price = price;
    closeSheet('sh-cprice');
    renderCountriesList(applyCountryFilters());
    toast(`${code} buy price → ${price.toFixed(2)}`);
  } catch(e) { toast(e.message,'err'); }
}

function openCSellPrice(code, name, cur) {
  $('cbprice-code').value = code;
  $('cbprice-title').textContent = `🔴 Sell Price (We Pay User) — ${code} ${name}`;
  $('cbprice-val').value = cur;
  openSheet('cbprice');
}
async function confirmCSellPrice() {
  const code       = $('cbprice-code').value;
  const sell_price = parseFloat($('cbprice-val').value);
  if (isNaN(sell_price) || sell_price < 0) { toast('Enter a valid price','err'); return; }
  try {
    await api(`/admin/api/countries/${code}/sell-price`,'PATCH',{sell_price});
    const c = _countries.find(c=>c.code===code);
    if (c) c.sell_price = sell_price;
    closeSheet('sh-cbprice');
    renderCountriesList(applyCountryFilters());
    toast(`${code} sell price → ${sell_price.toFixed(2)}`);
  } catch(e) { toast(e.message,'err'); }
}

function openCRank(code, name, cur) {
  $('crank-code').value = code;
  $('crank-title').textContent = `🏆 Rank — ${code} ${name}`;
  $('crank-val').value = cur;
  openSheet('crank');
}
async function confirmCRank() {
  const code = $('crank-code').value;
  const rank = parseInt($('crank-val').value, 10);
  if (isNaN(rank) || rank < 1) { toast('Enter a valid rank','err'); return; }
  try {
    await api('/admin/api/countries/rank','POST',{code,country_rank:rank});
    closeSheet('sh-crank');
    toast(`${code} rank → ${rank}`);
    await renderCountries();
  } catch(e) { toast(e.message,'err'); }
}

function openCAdd(code) {
  const c = code ? _countries.find(c=>c.code===code) : null;
  $('cadd-editing').value = code || '';
  $('cadd-title').textContent = c ? `✏️ Edit — ${c.code}` : '🌍 Add Country';
  $('cadd-name').value  = c ? c.country_name : '';
  $('cadd-code').value  = c ? c.code : '';
  $('cadd-idc').value   = c ? c.idc  : '';
  $('cadd-rank').value  = c ? c.country_rank : '';
  $('cadd-price').value = c ? c.price : '';
  openSheet('cadd');
  if (!c) {
    api('/admin/api/countries/next-rank').then(r => { $('cadd-rank').value = r.next_rank; }).catch(()=>{});
  }
}
async function confirmCAdd() {
  const editing = $('cadd-editing').value;
  const name  = $('cadd-name').value.trim();
  const code  = $('cadd-code').value.trim().toUpperCase();
  const idc   = $('cadd-idc').value.trim();
  const rank  = parseInt($('cadd-rank').value, 10);
  const price = parseFloat($('cadd-price').value);
  if (!name || !code || !idc) { toast('Fill in name, code, and dialing code','err'); return; }
  if (isNaN(rank) || rank < 1) { toast('Enter a valid rank','err'); return; }
  if (isNaN(price) || price < 0) { toast('Enter a valid price','err'); return; }
  const existing = _countries.find(c=>c.code===code);
  try {
    await api('/admin/api/countries/upsert','POST',{
      code, country_name:name, country_rank:rank, idc, price,
      temp_disable: existing ? existing.temp_disable : false,
      is_full: existing ? existing.is_full : false,
    });
    closeSheet('sh-cadd');
    toast(`${code} saved`);
    await renderCountries();
  } catch(e) { toast(e.message,'err'); }
}

async function confirmDeleteCountry(code) {
  if (!confirm(`Delete ${code}? This cannot be undone.`)) return;
  try {
    await api('/admin/api/countries/delete','POST',{code});
    toast(`${code} deleted`);
    await renderCountries();
  } catch(e) { toast(e.message,'err'); }
}

async function confirmDeleteAll() {
  if (!confirm('Delete ALL countries? This cannot be undone.')) return;
  try {
    const r = await api('/admin/api/countries/delete-all','POST');
    toast(`Deleted ${r.deleted} countries`);
    await renderCountries();
  } catch(e) { toast(e.message,'err'); }
}

// ─── INIT ─────────────────────────────────────────────────────────────────────

// Auto-login check
(async () => {
  if (_token) {
    try {
      await api('/admin/api/stats');
      $$('login').style.display = 'none';
      $$('app').style.display = 'flex';
      goto('dashboard');
    } catch(_) {
      localStorage.removeItem('admin_token');
      _token = '';
    }
  }
})();
</script>
</body>
</html>"""
