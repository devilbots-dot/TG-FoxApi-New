"""
Users Sell Stock admin routes.

/admin/user-sell-stock            — HTML page
/admin/api/user-sell-stock        — list (GET, filterable)
/admin/api/user-sell-stock/check-status  — bulk Telethon status check (POST)
/admin/api/user-sell-stock/transfer      — send to Sell Inventory (POST)
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from server import LOGGER
from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.usersellstockdb import (
    list_user_sell_stock,
    get_untransferred_by_ids,
    get_untransferred_by_query,
    update_session_status,
    mark_transferred,
    count_user_sell_stock,
)

router = APIRouter(tags=["Admin-UserSellStock"], include_in_schema=False)
_log = LOGGER(__name__)


# ── HTML page ─────────────────────────────────────────────────────────────────

@router.get("/admin/user-sell-stock", response_class=HTMLResponse)
async def user_sell_stock_page(request: Request, _session=Depends(require_session)):
    total_pending = await count_user_sell_stock(transferred=False)
    return templates.TemplateResponse(
        request,
        "admin/user_sell_stock.html",
        {"page": "user_sell_stock", "total_pending": total_pending},
    )


# ── List API ──────────────────────────────────────────────────────────────────

@router.get("/admin/api/user-sell-stock")
async def api_list_user_sell_stock(
    request: Request,
    _session=Depends(require_session),
    user_id: Optional[int] = None,
    sell_id: Optional[str] = None,
    stock_id: Optional[str] = None,
    country: Optional[str] = None,
    payment_status: Optional[str] = None,
    session_status: Optional[str] = None,
    transferred: Optional[str] = None,   # "0", "1", or None for all
    page: int = 1,
    limit: int = 50,
):
    transferred_bool: Optional[bool] = None
    if transferred == "0":
        transferred_bool = False
    elif transferred == "1":
        transferred_bool = True

    items, total = await list_user_sell_stock(
        user_id=user_id,
        sell_id=sell_id,
        stock_id=stock_id,
        country_code=country,
        payment_status=payment_status,
        session_status=session_status,
        transferred=transferred_bool,
        page=page,
        limit=limit,
    )

    # Serialize datetime fields — json.dumps cannot handle Python datetime objects
    _DATETIME_KEYS = ("received_at", "transferred_at", "status_checked_at")
    for d in items:
        for k in _DATETIME_KEYS:
            v = d.get(k)
            if v is not None and hasattr(v, "isoformat"):
                d[k] = v.isoformat()

    return JSONResponse({
        "items": items,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    })


# ── Check Session Status ──────────────────────────────────────────────────────

class CheckStatusRequest(BaseModel):
    stock_ids: list[str] = []          # empty = check all matching filters
    user_id: Optional[int] = None
    sell_id: Optional[str] = None
    country: Optional[str] = None
    payment_status: Optional[str] = None
    session_status_filter: Optional[str] = None


@router.post("/admin/api/user-sell-stock/check-status")
async def api_check_status(
    body: CheckStatusRequest,
    _session=Depends(require_session),
):
    """
    Check Telethon status for one or more users_sell_stock records.

    Downloads the session ZIP from the Telegram storage channel,
    connects with Telethon, and updates session_status in the DB.
    Works in a fan-out so multiple sessions are checked concurrently.
    """
    if body.stock_ids:
        docs = await get_untransferred_by_ids(body.stock_ids)
    else:
        docs = await get_untransferred_by_query(
            user_id=body.user_id,
            sell_id=body.sell_id,
            country_code=body.country,
            payment_status=body.payment_status,
            session_status=body.session_status_filter,
            limit=50,
        )

    if not docs:
        return JSONResponse({"ok": True, "checked": 0, "results": []})

    results = await asyncio.gather(
        *(_check_one(doc) for doc in docs),
        return_exceptions=True,
    )

    out = []
    for doc, res in zip(docs, results):
        if isinstance(res, Exception):
            out.append({"stock_id": doc["stock_id"], "status": "error", "error": str(res)})
        else:
            out.append(res)

    return JSONResponse({"ok": True, "checked": len(docs), "results": out})


async def _check_one(doc: dict) -> dict:
    """Download session from channel, connect with Telethon, detect status
    using the canonical `check_spam_status` (same detector used by the
    sell pipeline and live buy-check — @SpamBot /start reply classifier)."""
    stock_id        = doc["stock_id"]
    session_msg_id  = doc.get("session_msg_id")
    session_chat_id = doc.get("session_chat_id")
    country_code    = doc.get("country_code", "XX")
    phone           = doc.get("phone", "?")

    if not session_msg_id or not session_chat_id:
        await update_session_status(stock_id, "dead")
        return {"stock_id": stock_id, "status": "dead", "reason": "no_session_ref"}

    tmp_path = None
    client   = None
    try:
        from server import bot
        from server.utils.sessions.channel_storage import download_session_from_channel
        from server.utils.sessions.telethon_client import (
            new_temp_session_path,
            write_session_bytes,
            connect_with_proxy_fallback,
        )
        from server.stock.spam_check import check_spam_status
        import config as _cfg
        import io, zipfile

        raw = await download_session_from_channel(bot, session_chat_id, session_msg_id)

        # Handle ZIP archives (uploaded as ZIP by channel_storage)
        session_bytes: bytes = raw
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                session_files = [n for n in zf.namelist() if n.endswith(".session")]
                if session_files:
                    session_bytes = zf.read(session_files[0])
        except Exception:
            pass  # not a ZIP — treat as raw session bytes

        tmp_path = new_temp_session_path()
        await write_session_bytes(tmp_path, session_bytes)

        client, _, _ = await connect_with_proxy_fallback(
            tmp_path[:-8], _cfg.API_ID, _cfg.API_HASH, country_code
        )

        # Authorization check (dead / frozen)
        try:
            authorized = await client.is_user_authorized()
        except Exception as e:
            err = str(e).lower()
            if "frozen" in err:
                await update_session_status(stock_id, "frozen")
                return {"stock_id": stock_id, "status": "frozen", "phone": phone}
            if "deactivated" in err or "banned" in err:
                await update_session_status(stock_id, "permanent_spam")
                return {"stock_id": stock_id, "status": "permanent_spam", "phone": phone}
            await update_session_status(stock_id, "dead")
            return {"stock_id": stock_id, "status": "dead", "phone": phone, "error": str(e)}

        if not authorized:
            await update_session_status(stock_id, "dead")
            return {"stock_id": stock_id, "status": "dead", "phone": phone}

        # Canonical @SpamBot classifier — returns one of:
        # "clean" | "temporary_spam" | "permanent_spam" | "frozen" | "unknown"
        status = await check_spam_status(client)

        await update_session_status(stock_id, status)
        return {"stock_id": stock_id, "status": status, "phone": phone}

    except Exception as exc:
        _log.warning("check_one failed for stock_id=%s: %s", stock_id, exc)
        await update_session_status(stock_id, "unknown")
        return {"stock_id": stock_id, "status": "unknown", "error": str(exc), "phone": phone}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if tmp_path:
            try:
                from server.utils.sessions.telethon_client import cleanup_session_files
                await cleanup_session_files(tmp_path)
            except Exception:
                pass

# ── Transfer to Sell Inventory ────────────────────────────────────────────────

class TransferRequest(BaseModel):
    stock_ids: list[str] = []          # empty = transfer all matching filters
    user_id: Optional[int] = None
    sell_id: Optional[str] = None
    country: Optional[str] = None
    payment_status: Optional[str] = None
    session_status_filter: Optional[str] = None


@router.post("/admin/api/user-sell-stock/transfer")
async def api_transfer_to_inventory(
    body: TransferRequest,
    _session=Depends(require_session),
):
    """
    Move selected sessions from users_sell_stock into session_accounts
    (the sell inventory) so buyers can purchase them.

    Sessions are routed to their own country's inventory automatically.
    """
    if body.stock_ids:
        docs = await get_untransferred_by_ids(body.stock_ids)
    else:
        docs = await get_untransferred_by_query(
            user_id=body.user_id,
            sell_id=body.sell_id,
            country_code=body.country,
            payment_status=body.payment_status,
            session_status=body.session_status_filter,
            limit=200,
        )

    if not docs:
        return JSONResponse({"ok": True, "transferred": 0, "failed": 0, "results": []})

    results = await asyncio.gather(
        *(_transfer_one(doc) for doc in docs),
        return_exceptions=True,
    )

    transferred = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    failed = len(results) - transferred

    out = []
    for doc, res in zip(docs, results):
        if isinstance(res, Exception):
            out.append({"stock_id": doc["stock_id"], "ok": False, "error": str(res)})
        else:
            out.append(res)

    # Broadcast to admin dashboard
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type": "user_sell_stock_transferred",
            "transferred": transferred,
            "failed": failed,
        })
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "transferred": transferred,
        "failed": failed,
        "results": out,
    })


async def _transfer_one(doc: dict) -> dict:
    """Insert one users_sell_stock record into session_accounts and mark transferred.

    Defensive: uses .get() with fallbacks for every field, treats a duplicate
    phone in session_accounts as an idempotent success (marks the row transferred),
    and logs the full doc keys on any failure so silent transfers are traceable.
    """
    stock_id = doc.get("stock_id")
    phone    = doc.get("phone") or "?"
    cc       = (doc.get("country_code") or "").upper() or "XX"
    cname    = doc.get("country_name") or cc

    if not stock_id:
        return {"stock_id": None, "ok": False, "error": "missing_stock_id", "phone": phone}

    # Admin-triggered "Send to Inventory" is an explicit override — do not gate
    # on payment/approval status or session-status verification. The only hard
    # requirement is that we actually have the session file to hand off.
    if not doc.get("session_msg_id") or not doc.get("session_chat_id"):
        _log.warning("Transfer skipped stock_id=%s phone=%s: session_file_missing (msg=%s chat=%s)",
                     stock_id, phone, doc.get("session_msg_id"), doc.get("session_chat_id"))
        # Do NOT mark as transferred — keep the row visible in the pending list
        # so the admin can investigate and retry once the session file is recovered.
        # (Previously this silently marked the row done, losing the account forever.)
        return {"stock_id": stock_id, "ok": False, "error": "session_file_missing", "phone": phone}

    if not doc.get("phone"):
        _log.warning("Transfer skipped stock_id=%s: phone_missing", stock_id)
        return {"stock_id": stock_id, "ok": False, "error": "phone_missing", "phone": phone}

    mapped_status = _map_session_status(doc.get("session_status", "unknown"))

    try:
        from server.utils.database.sessiondb import add_session_account, get_session_by_phone
        try:
            account_id = await add_session_account(
                phone            = phone,
                country_code     = cc,
                country_name     = cname,
                session_msg_id   = doc["session_msg_id"],
                session_chat_id  = doc["session_chat_id"],
                tg_user_id       = doc.get("tg_user_id"),
                username         = doc.get("username"),
                first_name       = doc.get("first_name"),
                has_2fa          = doc.get("has_2fa", False),
                tfa_updated      = bool(doc.get("tfa_password_enc")),
                tfa_password_enc = doc.get("tfa_password_enc", ""),
                spam_status      = mapped_status,
                verified         = True,
                verification_status = "verified",
                proxy_used       = doc.get("proxy_used", "none"),
                api_id_used      = doc.get("api_id_used"),
            )
        except Exception as insert_exc:
            # Duplicate phone in session_accounts → treat as already-transferred success
            # so the admin action clears the row from the pending queue instead of
            # silently failing forever.
            err = str(insert_exc).lower()
            if "duplicate" in err or "e11000" in err:
                existing = None
                try:
                    existing = await get_session_by_phone(phone)
                except Exception:
                    existing = None
                account_id = (existing or {}).get("account_id") or "existing"
                _log.warning("Transfer duplicate for stock_id=%s phone=%s → reusing account_id=%s",
                             stock_id, phone, account_id)
            else:
                raise

        await mark_transferred(stock_id)

        # A manually transferred real account must be visible in the buyer menu.
        try:
            from server.utils.database.countrydb import set_country_stock_mode
            await set_country_stock_mode(cc, "real")
        except Exception as exc:
            _log.warning("Could not switch %s to real stock mode: %s", cc, exc)

        # ── Audit log: stock movement ─────────────────────────────────────────
        try:
            from server.utils.database.auditdb import log_action
            sell_id = doc.get("sell_id", "?")
            await log_action(
                "session", "stock_transfer",
                actor="system",
                target=phone,
                detail=f"USS {stock_id} → ACC {account_id} ({cc}) sell={sell_id}",
            )
        except Exception:
            pass

        _log.info("Transferred USS stock_id=%s → account_id=%s (%s / %s)",
                  stock_id, account_id, phone, cc)
        return {"stock_id": stock_id, "ok": True, "account_id": account_id, "phone": phone}
    except Exception as exc:
        _log.error("Transfer failed for stock_id=%s phone=%s cc=%s keys=%s: %s",
                   stock_id, phone, cc, sorted(list(doc.keys())), exc, exc_info=True)
        return {"stock_id": stock_id, "ok": False, "error": str(exc) or exc.__class__.__name__, "phone": phone}


def _map_session_status(session_status: str) -> str:
    """Map users_sell_stock session_status to session_accounts spam_status."""
    mapping = {
        "clean":          "clean",
        "temporary_spam": "temporary_spam",
        "permanent_spam": "permanent_spam",
        "frozen":         "frozen",
        "restricted":     "temporary_spam",
        "dead":           "permanent_spam",
        "unknown":        "unknown",
    }
    return mapping.get(session_status, "unknown")


# ── Bulk Approve / Reject (with pre-approval session verification) ────────────
# Groups multi-row results per seller so users receive ONE consolidated Telegram
# DM instead of one message per account. Approval never transfers inventory;
# Send to Inventory remains a separate explicit admin action.

class BulkApproveRequest(BaseModel):
    stock_ids: list[str] = []          # empty = approve all matching filters
    user_id: Optional[int] = None
    sell_id: Optional[str] = None
    country: Optional[str] = None
    payment_status: Optional[str] = None
    session_status_filter: Optional[str] = None
    force: bool = False                # bypass session verification
    auto_transfer: bool = False        # admin must explicitly Send to Inventory
    note: str = "Bulk approved by admin (session verification passed)"


class BulkRejectRequest(BaseModel):
    stock_ids: list[str] = []
    user_id: Optional[int] = None
    sell_id: Optional[str] = None
    country: Optional[str] = None
    payment_status: Optional[str] = None
    session_status_filter: Optional[str] = None
    note: str = "Rejected by admin"


async def _load_stock_docs(body) -> list[dict]:
    if body.stock_ids:
        return await get_untransferred_by_ids(body.stock_ids)
    return await get_untransferred_by_query(
        user_id=body.user_id,
        sell_id=body.sell_id,
        country_code=body.country,
        payment_status=body.payment_status,
        session_status=body.session_status_filter,
        limit=200,
    )


@router.post("/admin/api/user-sell-stock/approve")
async def api_bulk_approve(
    body: BulkApproveRequest,
    _session=Depends(require_session),
):
    """
    Bulk-approve sell requests directly from the Users Sell Stock panel.

    Flow (per stock row):
      1. Load linked sell_request. Skip if not pending.
      2. Run live session verification via verify_sell_session (unless force=true).
         • verified   → approve
         • inconclusive → approve (network-only failure, allow release)
         • hard-fail  → skip with reason (admin can re-run with force=true)
      3. Call finalize_sell_approval with notify_user=False (we send our own
         consolidated DM below).
      4. Optionally transfer the account to the Sell Inventory so buyers see it.

    Response groups per user with a single consolidated summary.
    """
    from server.utils.database.sellrequestdb import get_sell_request
    from server.services.sell_ops import finalize_sell_approval
    from server.services.sell_verifier import verify_sell_session
    from server.utils.database.userdb import get_balance
    from server.utils.notifications import notify

    docs = await _load_stock_docs(body)
    if not docs:
        return JSONResponse({"ok": True, "approved": 0, "skipped": 0, "transferred": 0, "results": []})

    # A single-row panel approval uses the exact same user notification as the
    # Telegram log-channel button. Multi-row actions suppress per-account DMs
    # and send one consolidated list per seller below.
    consolidated_notification = len(docs) > 1

    results: list[dict] = []
    # user_id → list of items for consolidated DM
    per_user: dict[int, list[dict]] = {}
    # user_id → running total credited
    per_user_total: dict[int, float] = {}

    async def _process_one(doc: dict) -> dict:
        stock_id = doc["stock_id"]
        sell_id  = doc.get("sell_id")
        phone    = doc.get("phone", "?")
        if not sell_id:
            return {"stock_id": stock_id, "phone": phone, "ok": False,
                    "reason": "no_sell_id_linked"}

        req = await get_sell_request(sell_id)
        if not req:
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": "sell_request_not_found"}

        if req.get("status") != "pending":
            # Already paid or rejected — nothing to do here (self-heal happens
            # elsewhere). Still auto-transfer below if requested.
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": f"already_{req.get('status','?')}",
                    "final_price": req.get("offer_price", 0),
                    "user_id": req.get("user_id"),
                    "country_code": req.get("code"),
                    "country_name": req.get("country_name")}

        # ── Verification gate ──────────────────────────────────────────────
        spam_after_check = req.get("spam_status", "unknown")
        if not body.force:
            try:
                v = await verify_sell_session(req)
                spam_after_check = v.spam_status or spam_after_check
                if not v.verified and not v.inconclusive:
                    return {
                        "stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                        "ok": False,
                        "reason": "verification_failed",
                        "failed_checks": v.failed_checks,
                        "verifier_reasons": v.reasons[:3],
                    }
            except Exception as exc:
                _log.warning("bulk_approve verify failed for %s: %s", sell_id, exc)
                # Treat verifier crash as inconclusive — allow approval to proceed
                # so a transient error doesn't block admin action.

        final_price = req.get("offer_price", 0)
        note = body.note if not body.force else f"{body.note} (force)"
        approved = await finalize_sell_approval(
            sell_id,
            final_price,
            note=note,
            notify_user=not consolidated_notification,
        )
        if not approved:
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": "finalize_failed"}

        # Do not report a successful panel approval until the canonical
        # sell_requests row is actually paid. This prevents a green UI result
        # for a partial/failed state transition.
        fresh_req = await get_sell_request(sell_id)
        if not fresh_req or fresh_req.get("status") != "paid":
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": "approval_state_not_persisted"}

        return {
            "stock_id":     stock_id,
            "sell_id":      sell_id,
            "phone":        phone,
            "user_id":      approved["user_id"],
            "country_code": approved.get("code"),
            "country_name": approved.get("country_name"),
            "final_price":  final_price,
            "status":       spam_after_check,
            "ok":           True,
        }

    proc_results = await asyncio.gather(*(_process_one(d) for d in docs), return_exceptions=True)

    approved_count = 0
    for r in proc_results:
        if isinstance(r, Exception):
            results.append({"ok": False, "reason": f"exception: {r}"})
            continue
        results.append(r)
        if r.get("ok"):
            approved_count += 1
            uid = r["user_id"]
            per_user.setdefault(uid, []).append({
                "sell_id":       r["sell_id"],
                "phone":         r["phone"],
                "country_code":  r.get("country_code"),
                "country_name":  r.get("country_name"),
                "status":        r.get("status", "unknown"),
                "amount":        r["final_price"],
            })
            per_user_total[uid] = per_user_total.get(uid, 0.0) + float(r["final_price"])

    # ── Auto-transfer approved stocks to Sell Inventory ──────────────────────
    transferred_count = 0
    if body.auto_transfer:
        approved_stock_ids = [r["stock_id"] for r in results
                              if isinstance(r, dict) and r.get("ok") and r.get("stock_id")]
        if approved_stock_ids:
            fresh_docs = await get_untransferred_by_ids(approved_stock_ids)
            xfer = await asyncio.gather(*(_transfer_one(d) for d in fresh_docs),
                                        return_exceptions=True)
            for r in xfer:
                if isinstance(r, dict) and r.get("ok"):
                    transferred_count += 1

    # ── Send one consolidated DM per seller ──────────────────────────────────
    async def _dm_one(uid: int, items: list[dict]):
        try:
            new_bal = await get_balance(uid)
            await notify(
                uid, "sell_bulk_approved",
                items=items,
                total_credited=per_user_total.get(uid, 0.0),
                new_balance=new_bal,
                note=body.note,
            )
        except Exception as exc:
            _log.warning("bulk_approve DM failed for user %s: %s", uid, exc)

    if consolidated_notification:
        await asyncio.gather(*(_dm_one(uid, items) for uid, items in per_user.items()))

    # Dashboard broadcast
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type": "sell_bulk_approved",
            "approved": approved_count,
            "transferred": transferred_count,
            "users_notified": len(per_user),
        })
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "approved": approved_count,
        "skipped":  len(results) - approved_count,
        "transferred": transferred_count,
        "users_notified": len(per_user),
        "results": results,
    })


@router.post("/admin/api/user-sell-stock/reject")
async def api_bulk_reject(
    body: BulkRejectRequest,
    _session=Depends(require_session),
):
    """Bulk-reject sell requests with a single consolidated DM per seller."""
    from server.utils.database.sellrequestdb import get_sell_request
    from server.services.sell_ops import finalize_sell_rejection
    from server.utils.notifications import notify

    docs = await _load_stock_docs(body)
    if not docs:
        return JSONResponse({"ok": True, "rejected": 0, "skipped": 0, "results": []})

    results: list[dict] = []
    per_user: dict[int, list[dict]] = {}

    async def _process_one(doc: dict) -> dict:
        stock_id = doc["stock_id"]
        sell_id  = doc.get("sell_id")
        phone    = doc.get("phone", "?")
        if not sell_id:
            return {"stock_id": stock_id, "phone": phone, "ok": False,
                    "reason": "no_sell_id_linked"}

        req = await get_sell_request(sell_id)
        if not req:
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": "sell_request_not_found"}
        if req.get("status") != "pending":
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": f"already_{req.get('status','?')}"}

        rejected = await finalize_sell_rejection(sell_id, note=body.note, notify_user=False)
        if not rejected:
            return {"stock_id": stock_id, "sell_id": sell_id, "phone": phone,
                    "ok": False, "reason": "finalize_failed"}

        return {
            "stock_id":     stock_id,
            "sell_id":      sell_id,
            "phone":        phone,
            "user_id":      rejected["user_id"],
            "country_code": rejected.get("code"),
            "country_name": rejected.get("country_name"),
            "ok":           True,
        }

    proc_results = await asyncio.gather(*(_process_one(d) for d in docs), return_exceptions=True)

    rejected_count = 0
    for r in proc_results:
        if isinstance(r, Exception):
            results.append({"ok": False, "reason": f"exception: {r}"})
            continue
        results.append(r)
        if r.get("ok"):
            rejected_count += 1
            uid = r["user_id"]
            per_user.setdefault(uid, []).append({
                "sell_id":      r["sell_id"],
                "phone":        r["phone"],
                "country_code": r.get("country_code"),
                "country_name": r.get("country_name"),
            })

    async def _dm_one(uid: int, items: list[dict]):
        try:
            await notify(uid, "sell_bulk_rejected", items=items, note=body.note)
        except Exception as exc:
            _log.warning("bulk_reject DM failed for user %s: %s", uid, exc)

    await asyncio.gather(*(_dm_one(uid, items) for uid, items in per_user.items()))

    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type": "sell_bulk_rejected",
            "rejected": rejected_count,
            "users_notified": len(per_user),
        })
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "rejected": rejected_count,
        "skipped":  len(results) - rejected_count,
        "users_notified": len(per_user),
        "results": results,
    })
