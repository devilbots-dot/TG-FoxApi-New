"""
Sell Ops — core DB operations for finalizing a sell request.

Both the manual admin override (server.plugins.bot.sell_admin) and the
automatic 24-hour recheck worker (server.services.sell_recheck) move money
through these SAME two atomic functions, so approval/rejection always
behaves identically no matter which path triggers it.
"""

from typing import Optional

from server import LOGGER
from server.utils.database.sellrequestdb import (
    approve_sell_request,
    reject_sell_request,
    get_sell_request,
)
from server.utils.database.userdb import move_pending_to_available, update_balance, clear_pending_balance
from server.utils.database.walletdb import log_transaction

_log = LOGGER(__name__)


async def finalize_sell_approval(
    request_id: str,
    final_price: float,
    note: str,
    *,
    notify_user: bool = True,
) -> Optional[dict]:
    """
    Atomically approve a pending sell request and move its pending_amount
    into the user's withdrawable balance.  Then add the account to stock so
    buyers can purchase it.

    Returns the PRE-update request doc (user_id, phone, code, pending_amount,
    etc.) on success, or None if the request was not found in "pending" state
    (already approved/rejected — a no-op, safe to call more than once).
    """
    doc = await approve_sell_request(request_id, final_price, admin_note=note)
    already_paid = False
    if not doc:
        # Self-heal: if the request was already marked paid by an earlier
        # partial run, still make sure downstream side-effects (users_sell_stock
        # upsert, ledger, notification) happen. Balance move is skipped so we
        # never double-pay the seller.
        existing = await get_sell_request(request_id)
        if not existing or existing.get("status") not in ("paid", "approved"):
            return None
        doc = existing
        already_paid = True
        _log.warning(
            "finalize_sell_approval: %s already status=%s — running self-heal "
            "(skipping balance move, still upserting users_sell_stock).",
            request_id, existing.get("status"),
        )

    user_id        = doc["user_id"]
    pending_amount = doc.get("pending_amount") or doc.get("offer_price", 0)

    # ── Move pending → available (atomic) ────────────────────────────────────
    # Skip in self-heal mode so we don't double-credit the seller.
    moved = True if already_paid else await move_pending_to_available(user_id, pending_amount)
    if not moved:
        _log.warning(
            "finalize_sell_approval: move_pending_to_available failed for user %s "
            "(pending=%.4f) — crediting balance directly and clearing pending.",
            user_id, pending_amount,
        )
        # Fallback: credit available balance directly …
        await update_balance(user_id, final_price)
        # … and clear the stuck pending balance so it never appears twice.
        try:
            await clear_pending_balance(user_id, pending_amount)
        except Exception as exc:
            _log.error(
                "finalize_sell_approval: clear_pending_balance fallback failed for user %s: %s",
                user_id, exc,
            )

    # ── Transaction ledger ────────────────────────────────────────────────────
    try:
        await log_transaction(
            user_id=user_id,
            txn_type="sale",
            amount=final_price,
            ref_id=request_id,
            note=f"Sell approved [{doc.get('code', '?')}] {doc.get('phone', '?')} — {note}",
        )
    except Exception as exc:
        _log.warning("finalize_sell_approval: log_transaction failed for %s: %s", request_id, exc)

    # ── Upsert into Users Sell Stock (holding area) ───────────────────────────
    # All approved accounts go to users_sell_stock so admin can see them,
    # run status checks, and promote to sell inventory when ready.
    # We use upsert_by_sell_id so that:
    #   • Auto-flow accounts (already inserted by termination worker with
    #     payment_status="pending") just get updated to payment_status="paid".
    #   • New manual approvals get freshly inserted.
    # Accounts with no session file (session_msg_id=None) are still inserted
    # with a visible warning — admin must handle them manually.
    session_msg_id  = doc.get("session_msg_id")
    session_chat_id = doc.get("session_chat_id")
    try:
        from server.utils.database.usersellstockdb import upsert_by_sell_id
        stock_id = await upsert_by_sell_id(
            sell_id          = request_id,
            user_id          = user_id,
            phone            = doc.get("phone", ""),
            country_code     = doc.get("code", "XX"),
            country_name     = doc.get("country_name", ""),
            session_msg_id   = session_msg_id,
            session_chat_id  = session_chat_id,
            tg_user_id       = doc.get("tg_user_id"),
            username         = doc.get("username"),
            first_name       = doc.get("first_name"),
            has_2fa          = doc.get("has_2fa", False),
            tfa_password_enc = doc.get("tfa_password_enc", ""),
            proxy_used       = doc.get("proxy_used", "none"),
            api_id_used      = doc.get("api_id_used"),
            sell_type        = doc.get("sell_type", "account"),
            spam_status      = doc.get("spam_status", "unknown"),
            payment_status   = "paid",
        )
        if not session_msg_id:
            _log.warning(
                "finalize_sell_approval: request %s has NO session_msg_id — "
                "added to users_sell_stock %s but session file is missing; admin action required.",
                request_id, stock_id,
            )
        else:
            _log.info(
                "finalize_sell_approval: upserted to users_sell_stock %s for request %s (phone %s)",
                stock_id, request_id, doc.get("phone"),
            )
    except Exception as exc:
        # Non-fatal — seller is already paid; admin can see in sell_requests view
        _log.error(
            "finalize_sell_approval: failed to upsert to users_sell_stock for request %s: %s",
            request_id, exc,
        )

    # ── Auto-transfer to inventory (config-driven) ────────────────────────────
    # When `auto_transfer_on_approval` is True in admin_config, sessions are
    # pushed into session_accounts immediately after approval — no manual
    # "Send to Inventory" step required.  Sessions without a session file
    # (session_msg_id=None) are silently skipped; the row stays visible in
    # users_sell_stock so the admin can handle it manually.
    if session_msg_id:
        try:
            from server.utils.database.configdb import get_setting as _cfg
            if await _cfg("auto_transfer_on_approval"):
                from server.admin.routes.user_sell_stock import _transfer_one
                uss_doc = {
                    "stock_id":        None,  # mark_transferred needs the real id; fetch below
                    "sell_id":         request_id,
                    "phone":           doc.get("phone", ""),
                    "country_code":    doc.get("code", "XX"),
                    "country_name":    doc.get("country_name", ""),
                    "session_msg_id":  session_msg_id,
                    "session_chat_id": session_chat_id,
                    "tg_user_id":      doc.get("tg_user_id"),
                    "username":        doc.get("username"),
                    "first_name":      doc.get("first_name"),
                    "has_2fa":         doc.get("has_2fa", False),
                    "tfa_password_enc": doc.get("tfa_password_enc", ""),
                    "proxy_used":      doc.get("proxy_used", "none"),
                    "api_id_used":     doc.get("api_id_used"),
                    "sell_type":       doc.get("sell_type", "account"),
                    "session_status":  "unknown",  # no live re-check on approval path
                }
                # Fetch the real stock_id from users_sell_stock (needed by _transfer_one
                # to call mark_transferred).
                try:
                    from server.utils.database.usersellstockdb import usersellstockdb as _ussdb
                    uss_row = await _ussdb.find_one({"sell_id": request_id}, {"stock_id": 1})
                    if uss_row:
                        uss_doc["stock_id"] = uss_row["stock_id"]
                except Exception:
                    pass
                if uss_doc.get("stock_id"):
                    result = await _transfer_one(uss_doc)
                    if result.get("ok"):
                        _log.info(
                            "finalize_sell_approval: auto-transferred %s → account_id=%s (phone %s)",
                            request_id, result.get("account_id"), doc.get("phone"),
                        )
                    else:
                        _log.warning(
                            "finalize_sell_approval: auto-transfer failed for %s: %s",
                            request_id, result.get("error"),
                        )
        except Exception as exc:
            # Non-fatal — seller is paid; admin can still transfer manually.
            _log.warning(
                "finalize_sell_approval: auto-transfer exception for %s: %s",
                request_id, exc,
            )

    # ── Audit log: sell approval ──────────────────────────────────────────────
    try:
        from server.utils.database.auditdb import log_action
        await log_action(
            "session", "sell_approved",
            actor="system",
            target=doc.get("phone", request_id),
            detail=f"request={request_id} user={user_id} amount={final_price:.4f} note={note}",
        )
    except Exception:
        pass

    # ── Broadcast live event to admin dashboard ────────────────────────────────
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type": "sell_approved",
            "request_id": request_id,
            "user_id": user_id,
            "amount": final_price,
        })
    except Exception:
        pass

    # ── Notify user via Telegram (best-effort) ────────────────────────────────
    # Skip when called from bulk-approval flow so a single consolidated DM can be sent.
    if notify_user:
        try:
            from server.utils.notifications import notify
            from server.utils.database.userdb import get_balance
            new_bal = await get_balance(user_id)
            await notify(
                user_id, "sell_approved",
                request_id=request_id,
                phone=doc.get("phone", ""),
                amount=final_price,
                note=note,
                new_balance=new_bal,
                country_name=doc.get("country_name", doc.get("code", "")),
            )
        except Exception as exc:
            _log.warning("finalize_sell_approval: user notification failed for %s: %s", request_id, exc)

    return doc


async def finalize_sell_rejection(
    request_id: str,
    note: str,
    *,
    notify_user: bool = True,
) -> Optional[dict]:
    """
    Atomically reject a pending sell request and reverse its pending_amount.

    Returns the PRE-update request doc on success, or None if the request
    was not found in "pending" state.
    """
    doc = await reject_sell_request(request_id, admin_note=note)
    if not doc:
        return None

    user_id        = doc["user_id"]
    pending_amount = doc.get("pending_amount") or doc.get("offer_price", 0)

    try:
        await clear_pending_balance(user_id, pending_amount)
    except Exception as exc:
        _log.error(
            "finalize_sell_rejection: clear_pending_balance failed for user %s request %s: %s",
            user_id, request_id, exc,
        )
        # Continue — status is already rejected; admin must correct balance manually.

    # ── Transaction ledger: record the reversal so the seller's history is accurate ──
    # Without this, the ledger shows a "sale" credit from submission with no
    # corresponding reversal — permanent inconsistency in the user's transaction history.
    try:
        await log_transaction(
            user_id=user_id,
            txn_type="sale_reversal",
            amount=-pending_amount,
            ref_id=request_id,
            note=f"Sell rejected [{doc.get('code', '?')}] {doc.get('phone', '?')} — {note}",
        )
    except Exception as exc:
        _log.warning("finalize_sell_rejection: log_transaction failed for %s: %s", request_id, exc)

    # ── Audit log ─────────────────────────────────────────────────────────────
    try:
        from server.utils.database.auditdb import log_action
        await log_action(
            "session", "sell_rejected",
            actor="system",
            target=doc.get("phone", request_id),
            detail=f"request={request_id} user={user_id} pending={pending_amount:.4f} note={note}",
        )
    except Exception:
        pass

    # Notify user via Telegram (best-effort) — skipped in bulk-reject flow.
    if notify_user:
        try:
            from server.utils.notifications import notify
            await notify(
                user_id, "sell_rejected",
                request_id=request_id,
                phone=doc.get("phone", ""),
                note=note,
            )
        except Exception as exc:
            _log.warning("finalize_sell_rejection: user notification failed for %s: %s", request_id, exc)

    # Broadcast live event to admin dashboard
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type": "sell_rejected",
            "request_id": request_id,
            "user_id": user_id,
        })
    except Exception:
        pass

    return doc
