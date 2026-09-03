"""
Background workers — long-running async tasks started at bot startup.

Two workers run indefinitely:

  termination_retry_worker()
      Every 60 minutes, find sell requests in PENDING_TERMINATION lifecycle
      whose retry_at has passed, re-connect the stored session and attempt
      to terminate all other authorizations.  On success → lifecycle moves
      to "completed" and a 48-hour payment release timer starts.
      On failure → retry_count is incremented and retry_at is pushed forward
      (exponential backoff, max 7-day retry window).

  payment_release_worker()
      Every 30 minutes, find sell requests in "completed" lifecycle whose
      payment_release_at has passed.  Atomically moves pending_balance to
      earned balance (available for withdrawal) and marks lifecycle as
      "payment_released".
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Optional

from server import LOGGER
from server.utils.common import utcnow

_log = LOGGER(__name__)

# Worker tick intervals — DEFAULTS. Admin can override any of these live from
# the Background Tasks page; the loops call `_effective_interval()` each cycle
# so a change in `admin_config` (RAM-first, no restart) takes effect on the
# very next tick. Related timing values (payment_hold_hours,
# order_timeout_minutes, termination_delay_minutes per-country) are also read
# from admin_config so one place controls everything.
_TERM_RETRY_INTERVAL_S = 30             # check every 30s; per-request retry_at still enforces the configured delay
_PAYMENT_CHECK_INTERVAL_S = 30 * 60     # check every 30 minutes

# How long to wait before the next termination retry (exponential: attempt × 24h, capped at 7d)
_RETRY_BASE_HOURS = 24
_RETRY_MAX_HOURS  = 7 * 24


# Mapping of worker name -> setting key + default interval (seconds). Used by
# `_effective_interval()` and by the admin API to expose/edit intervals.
_INTERVAL_DEFAULTS: dict[str, int] = {}


def _register_default_interval(name: str, default_s: int) -> None:
    _INTERVAL_DEFAULTS[name] = default_s


async def _effective_interval(name: str, default_s: int) -> int:
    """Read `worker_interval_<name>` from admin_config; fall back to default.

    Clamped to [5s, 24h] to guard against typos disabling a worker or DOSing it.
    """
    try:
        from server.utils.database.configdb import get_setting
        raw = await get_setting(f"worker_interval_{name}")
        if raw is None:
            return default_s
        value = int(raw)
    except Exception:
        return default_s
    if value < 5:
        value = 5
    if value > 24 * 3600:
        value = 24 * 3600
    return value


# ── Termination Retry Worker ──────────────────────────────────────────────────

async def termination_retry_worker() -> None:
    """
    Long-running background task.  Retries session termination for
    PENDING_TERMINATION sell requests.
    """
    _log.info("termination_retry_worker: started")
    await asyncio.sleep(5)  # Dependencies are initialized before workers are scheduled

    while True:
        try:
            await _run_termination_retries()
        except asyncio.CancelledError:
            _log.info("termination_retry_worker: cancelled")
            return
        except Exception as exc:
            _log.error("termination_retry_worker unhandled error: %s", exc, exc_info=True)

        await asyncio.sleep(await _effective_interval("termination_retry", _TERM_RETRY_INTERVAL_S))


async def _run_termination_retries() -> None:
    from server.utils.database.sellrequestdb import (
        get_pending_termination_due,
        update_sell_request_status,
        increment_retry_count,
    )

    due = await get_pending_termination_due(limit=20)
    if not due:
        return

    _log.info("termination_retry_worker: %d record(s) due for retry", len(due))

    for record in due:
        try:
            await _retry_one_termination(record)
        except Exception as exc:
            _log.error(
                "termination_retry_worker: error processing request %s: %s",
                record.get("request_id"), exc, exc_info=True,
            )


async def _retry_one_termination(record: dict) -> None:
    from server.utils.database.sellrequestdb import (
        update_sell_request_status,
        increment_retry_count,
    )
    from server.utils.sessions.crypto import decrypt_bytes
    from server.utils.sessions.telethon_client import (
        new_temp_session_path,
        write_session_bytes,
        cleanup_session_files,
        connect_with_proxy_fallback,
    )
    from server.stock.authorization import list_other_authorizations, terminate_other_sessions

    request_id  = record["request_id"]
    phone       = record.get("phone", "?")
    country     = record.get("code", "XX")
    retry_count = record.get("retry_count", 0)
    session_enc = record.get("session_bytes_enc")

    _log.info("Retrying termination for request %s phone %s (attempt #%d)", request_id, phone, retry_count + 1)

    if not session_enc:
        _log.warning("No session bytes stored for request %s — giving up", request_id)
        await update_sell_request_status(request_id, "pending", extra={"admin_note": "Retry abandoned: no session bytes"})
        return

    tmp_path: Optional[str] = None
    client = None
    try:
        # Decrypt and write session bytes
        try:
            session_bytes = decrypt_bytes(
                session_enc if isinstance(session_enc, bytes) else session_enc.encode()
            )
        except Exception:
            session_bytes = session_enc if isinstance(session_enc, bytes) else session_enc.encode()

        tmp_path = new_temp_session_path()
        await write_session_bytes(tmp_path, session_bytes)

        import config as _cfg
        client, _, _ = await connect_with_proxy_fallback(
            tmp_path[:-8], _cfg.API_ID, _cfg.API_HASH, country
        )

        if not await client.is_user_authorized():
            _log.warning("Session for request %s is no longer authorized — rejecting", request_id)
            await update_sell_request_status(
                request_id, "pending",
                extra={"admin_note": "Retry abandoned: session no longer authorized", "status": "rejected"},
            )
            return

        others = await list_other_authorizations(client)
        if not others:
            _log.info("No other sessions for request %s — termination complete", request_id)
            # Refresh session in channel with post-termination bytes before marking complete
            await _refresh_session_in_channel(client, tmp_path, record)
            client = None  # disconnect handled inside _refresh_session_in_channel
            await _mark_termination_complete(request_id, record)
            return

        term_result = await terminate_other_sessions(client, others, phone)
        if not term_result.get("termination_incomplete"):
            _log.info("Termination succeeded for request %s", request_id)
            # Refresh session in channel with post-termination bytes
            await _refresh_session_in_channel(client, tmp_path, record)
            client = None  # disconnect handled inside _refresh_session_in_channel
            await _mark_termination_complete(request_id, record)
        else:
            await _schedule_next_retry(request_id, retry_count)

    except Exception as exc:
        err = str(exc)
        if "FRESH_CHANGE_PHONE_FORBIDDEN" in err or "FreshChangePhone" in type(exc).__name__:
            _log.info("request %s still too new — rescheduling (attempt #%d)", request_id, retry_count + 1)
            await _schedule_next_retry(request_id, retry_count)
        else:
            _log.error("Termination retry error for request %s: %s", request_id, exc)
            await _schedule_next_retry(request_id, retry_count)

    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if tmp_path:
            try:
                await cleanup_session_files(tmp_path)
            except Exception:
                pass


async def _refresh_session_in_channel(client, tmp_path: Optional[str], record: dict) -> None:
    """
    After successful session termination, write a fresh session ZIP to the
    storage channel so future buyers receive post-termination bytes (not the
    stale pre-termination file uploaded at submission).

    Non-fatal — a failure here is logged but does not block the approval flow.
    """
    request_id      = record.get("request_id", "?")
    phone           = record.get("phone", "")
    country_code    = record.get("code", "XX")
    session_chat_id = record.get("session_chat_id")

    if not session_chat_id or not tmp_path:
        return  # nothing to do

    try:
        import asyncio as _asyncio
        from server.utils.sessions.telethon_client import read_session_bytes, cleanup_session_files

        # Disconnect client to flush the Telethon SQLite session to disk
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

        await _asyncio.sleep(0.5)  # brief pause to ensure flush
        fresh_bytes = await read_session_bytes(tmp_path)

        from server import bot as _bot
        from server.utils.sessions.channel_storage import upload_session_zip_to_channel
        upload_ref = await upload_session_zip_to_channel(
            _bot,
            session_chat_id,
            phone,
            fresh_bytes,
            tfa_password    = record.get("tfa_password_enc", ""),
            has_2fa         = record.get("has_2fa", False),
            spam_status     = record.get("spam_status", "unknown"),
            country_code    = country_code,
            country_name    = record.get("country_name", ""),
            sell_type       = record.get("sell_type", "account"),
        )

        new_msg_id = upload_ref.message_id
        new_chat_id = upload_ref.chat_id
        # Update both exact returned references in sell_requests.
        from server.utils.database.sellrequestdb import update_sell_request_status
        await update_sell_request_status(
            request_id,
            record.get("lifecycle_status", "pending_termination"),
            extra={"session_msg_id": new_msg_id, "session_chat_id": new_chat_id},
        )

        # Also update in users_sell_stock if the entry exists
        try:
            from server.utils.database.usersellstockdb import usersellstockdb as _ussdb
            await _ussdb.update_one(
                {"sell_id": request_id},
                {"$set": {
                    "session_msg_id": new_msg_id,
                    "session_chat_id": new_chat_id,
                }},
            )
        except Exception:
            pass

        _log.info(
            "_refresh_session_in_channel: refreshed session for %s → new msg_id=%s",
            request_id, new_msg_id,
        )

    except Exception as exc:
        _log.warning(
            "_refresh_session_in_channel: failed for %s — buyers may get stale session: %s",
            request_id, exc,
        )


async def _mark_termination_complete(request_id: str, record: dict) -> None:
    """Move the sell request to 'completed' and start the payment hold timer."""
    from server.utils.database.sellrequestdb import update_sell_request_status
    from server.utils.database.configdb import get_setting as _get_setting

    # Country-based AUTO-TERMINATION flow -> dedicated hold setting.
    # Falls back to the classic payment_hold_hours when not configured.
    hold_hours = int(
        await _get_setting("payment_hold_auto_term_hours")
        or await _get_setting("payment_hold_hours") or 48
    )
    payment_release_at = utcnow() + timedelta(hours=hold_hours)
    _log.info(
        "_mark_termination_complete: %s auto-termination hold = %dh (release at %s)",
        request_id, hold_hours, payment_release_at.isoformat(),
    )
    await update_sell_request_status(
        request_id,
        "completed",
        extra={
            "terminated_others":  True,
            "session_bytes_enc":  None,  # free memory
            "payment_release_at": payment_release_at,
        },
    )

    # ── Insert / update in Users Sell Stock so admin can see this session ─────
    # Auto-flow accounts (sell_account) bypass manual admin approval and would
    # otherwise never appear in the Users Sell Stock panel.  Upserting here
    # makes them visible immediately after termination completes.
    try:
        from server.utils.database.usersellstockdb import upsert_by_sell_id
        await upsert_by_sell_id(
            sell_id          = request_id,
            user_id          = record.get("user_id"),
            phone            = record.get("phone", ""),
            country_code     = record.get("code", "XX"),
            country_name     = record.get("country_name", ""),
            session_msg_id   = record.get("session_msg_id"),
            session_chat_id  = record.get("session_chat_id"),
            tg_user_id       = record.get("tg_user_id"),
            username         = record.get("username"),
            first_name       = record.get("first_name"),
            has_2fa          = record.get("has_2fa", False),
            tfa_password_enc = record.get("tfa_password_enc", ""),
            proxy_used       = record.get("proxy_used", "none"),
            api_id_used      = record.get("api_id_used"),
            sell_type        = record.get("sell_type", "account"),
            spam_status      = record.get("spam_status", "unknown"),
            payment_status   = "pending",   # payment still on hold — not yet released
        )
        _log.info("_mark_termination_complete: upserted %s into users_sell_stock (payment=pending)", request_id)
    except Exception as exc:
        _log.warning("_mark_termination_complete: users_sell_stock upsert failed for %s: %s", request_id, exc)

    # ── Broadcast to admin dashboard (SSE) ────────────────────────────────────
    try:
        from server.admin.routes.dashboard import broadcast_event
        await broadcast_event({
            "type":          "sell_termination_complete",
            "request_id":    request_id,
            "user_id":       record.get("user_id"),
            "hold_hours":    hold_hours,
        })
    except Exception:
        pass

    # Notify user
    try:
        from server import bot
        user_id = record.get("user_id")
        phone   = record.get("phone", "N/A")
        if user_id:
            await bot.send_message(
                user_id,
                f"✅ **Session Termination Complete**\n\n"
                f"📱 `{phone}`\n\n"
                f"All other sessions have been terminated successfully.\n"
                f"💰 Your pending balance will be released in ~{hold_hours} hours.\n\n"
                f"🆔 Request: `{request_id}`",
            )
    except Exception as notify_exc:
        _log.warning("Could not notify user for request %s: %s", request_id, notify_exc)


async def _schedule_next_retry(request_id: str, retry_count: int) -> None:
    """Back off and schedule the next retry attempt.

    Backoff hours come from admin_config (`termination_retry_base_hours`,
    `termination_retry_max_hours`) so the timing stays consistent with the
    values shown on the Settings / Background Tasks pages. Missing settings
    fall back to the module constants.
    """
    from server.utils.database.sellrequestdb import increment_retry_count
    from server.utils.database.configdb import get_setting

    try:
        base_h = int(await get_setting("termination_retry_base_hours") or _RETRY_BASE_HOURS)
        max_h  = int(await get_setting("termination_retry_max_hours")  or _RETRY_MAX_HOURS)
    except Exception:
        base_h, max_h = _RETRY_BASE_HOURS, _RETRY_MAX_HOURS

    hours = min(base_h * (retry_count + 1), max_h)
    next_retry = utcnow() + timedelta(hours=hours)
    await increment_retry_count(request_id, next_retry)
    _log.info(
        "Rescheduled retry for request %s: next attempt at %s (+%dh, base=%dh, max=%dh)",
        request_id, next_retry.isoformat(), hours, base_h, max_h,
    )


# ── Payment Release Worker ────────────────────────────────────────────────────

async def payment_release_worker() -> None:
    """
    Long-running background task.  Converts pending_balance to earned
    balance for sell requests that have passed their 48-hour hold.
    """
    _log.info("payment_release_worker: started")
    await asyncio.sleep(180)  # Give bot a moment to fully start

    while True:
        try:
            await _run_payment_releases()
        except asyncio.CancelledError:
            _log.info("payment_release_worker: cancelled")
            return
        except Exception as exc:
            _log.error("payment_release_worker unhandled error: %s", exc, exc_info=True)

        await asyncio.sleep(await _effective_interval("payment_release", _PAYMENT_CHECK_INTERVAL_S))


async def _run_payment_releases() -> None:
    from server.utils.database.sellrequestdb import (
        get_payment_release_due, mark_payment_released, reject_sell_request,
    )
    from server.utils.database.userdb import move_pending_to_available, clear_pending_balance
    from server.utils.database.walletdb import log_transaction

    due = await get_payment_release_due(limit=50)
    if not due:
        return

    _log.info("payment_release_worker: checking %d request(s) for 24h re-verification", len(due))

    for record in due:
        request_id     = record["request_id"]
        user_id        = record.get("user_id")
        pending_amount = record.get("pending_amount", 0.0)
        phone          = record.get("phone", "N/A")
        country        = record.get("country_name", "N/A")

        # ── 24-hour Session Re-verification ──────────────────────────────────
        # ALL checks must pass before pending balance is released to earned.
        # Inconclusive result (pure network timeout) = hold payment, retry next cycle.
        # Verification failure = reject the request, reverse pending balance, notify user.
        try:
            from server.services.sell_verifier import verify_sell_session, build_user_rejection_text
            _log.info("payment_release_worker: verifying session for request %s phone %s", request_id, phone)
            verification = await verify_sell_session(record)

            if verification.inconclusive:
                # Network error — hold this cycle, try again later (no status change)
                _log.warning(
                    "payment_release_worker: verification INCONCLUSIVE for %s — holding payment: %s",
                    request_id, verification.error,
                )
                try:
                    from server.utils.notifications import notify
                    await notify(
                        user_id, "sell_payment_held",
                        request_id=request_id, phone=phone,
                        reason="Temporary network issue during re-verification",
                        retry_hours=4,
                    )
                except Exception:
                    pass
                continue  # skip to next record; payment release_at unchanged → retried next run

            if not verification.verified:
                # Session failed verification — reject and reverse balance
                _log.warning(
                    "payment_release_worker: verification FAILED for %s — rejecting. Reasons: %s",
                    request_id, verification.reasons,
                )
                fail_note = (
                    "Auto-rejected at 24h re-verification: "
                    + "; ".join(verification.reasons)
                )
                try:
                    await reject_sell_request(request_id, admin_note=fail_note)
                except Exception as exc:
                    _log.error("payment_release: reject_sell_request failed for %s: %s", request_id, exc)

                try:
                    await clear_pending_balance(user_id, pending_amount)
                except Exception as exc:
                    _log.error("payment_release: clear_pending_balance failed for %s: %s", request_id, exc)

                try:
                    await log_transaction(
                        user_id=user_id,
                        txn_type="reversal",
                        amount=-pending_amount,
                        ref_id=request_id,
                        note=f"24h recheck failed — balance reversed. {'; '.join(verification.reasons[:2])}",
                    )
                except Exception:
                    pass

                try:
                    from server import bot as _bot
                    rejection_text = build_user_rejection_text(verification, request_id, phone)
                    await _bot.send_message(user_id, rejection_text)
                except Exception as exc:
                    _log.warning("payment_release: could not DM user %s: %s", user_id, exc)

                try:
                    from server.admin.routes.dashboard import broadcast_event
                    await broadcast_event({
                        "type": "sell_auto_rejected",
                        "request_id": request_id,
                        "user_id": user_id,
                        "reasons": verification.reasons,
                    })
                except Exception:
                    pass

                # Mark users_sell_stock entry as rejected so admin sees it
                try:
                    from server.utils.database.usersellstockdb import update_payment_status_by_sell_id
                    await update_payment_status_by_sell_id(request_id, "rejected")
                except Exception:
                    pass

                _log.info(
                    "payment_release_worker: auto-rejected %s for user %s (reversed $%.4f)",
                    request_id, user_id, pending_amount,
                )
                continue  # done with this record

            # Verification PASSED — release payment
            _log.info(
                "payment_release_worker: verification PASSED for %s (spam=%s, other_sessions=%d)",
                request_id, verification.spam_status, verification.other_sessions_count,
            )

        except Exception as exc:
            _log.error(
                "payment_release_worker: verification error for %s — holding payment: %s",
                request_id, exc, exc_info=True,
            )
            continue  # safer to hold than release unverified

        # ── Release payment (verification passed) ─────────────────────────────
        try:
            # Move pending → earned balance.  Do not mark the request paid if
            # the conditional balance update failed; keeping it due allows a
            # later worker cycle or admin reconciliation to recover safely.
            moved = await move_pending_to_available(user_id, pending_amount)
            if not moved:
                _log.error(
                    "payment_release_worker: pending balance move failed for %s "
                    "(user=%s amount=%.4f); request remains unreleased",
                    request_id, user_id, pending_amount,
                )
                continue

            # Mark as payment released in DB
            await mark_payment_released(request_id)

            # Audit log
            try:
                await log_transaction(
                    user_id=user_id,
                    txn_type="sale",
                    amount=pending_amount,
                    ref_id=request_id,
                    note=f"Auto-released after 24h hold + verification passed — {phone} [{country}]",
                )
            except Exception as txn_exc:
                _log.warning("payment_release log_transaction failed for %s: %s", request_id, txn_exc)

            # Notify user
            try:
                from server import bot
                await bot.send_message(
                    user_id,
                    f"💰 **Payment Released!**\n\n"
                    f"✅ Your pending balance of `${pending_amount:g}` from selling "
                    f"`{phone}` has been verified and released to your **available balance**.\n\n"
                    f"You can now withdraw it. 🎉\n\n"
                    f"🆔 Request: `{request_id}`",
                )
            except Exception as notify_exc:
                _log.warning("payment_release notify user %s failed: %s", user_id, notify_exc)

            # Update users_sell_stock to reflect that payment was released
            try:
                from server.utils.database.usersellstockdb import update_payment_status_by_sell_id
                await update_payment_status_by_sell_id(request_id, "paid")
            except Exception:
                pass

            # ── Auto-transfer to inventory after payment release ─────────────
            # When `auto_transfer_on_payment_release` is True, sessions that
            # passed live re-verification are pushed straight into
            # session_accounts so buyers can purchase them — no manual admin
            # "Send to Inventory" action required.
            try:
                from server.utils.database.configdb import get_setting as _cfg
                if await _cfg("auto_transfer_on_payment_release"):
                    from server.admin.routes.user_sell_stock import _transfer_one
                    from server.utils.database.usersellstockdb import usersellstockdb as _ussdb
                    uss_row = await _ussdb.find_one(
                        {"sell_id": request_id, "transferred": False},
                    )
                    if uss_row and uss_row.get("session_msg_id") and uss_row.get("session_chat_id"):
                        transfer_result = await _transfer_one(uss_row)
                        if transfer_result.get("ok"):
                            _log.info(
                                "payment_release_worker: auto-transferred %s → account_id=%s (phone %s)",
                                request_id, transfer_result.get("account_id"), record.get("phone"),
                            )
                        else:
                            _log.warning(
                                "payment_release_worker: auto-transfer failed for %s: %s",
                                request_id, transfer_result.get("error"),
                            )
            except Exception as at_exc:
                _log.warning(
                    "payment_release_worker: auto-transfer exception for %s: %s",
                    request_id, at_exc,
                )

            _log.info(
                "payment_release_worker: released $%.4f for user %s (request %s)",
                pending_amount, user_id, request_id,
            )

        except Exception as exc:
            _log.error(
                "payment_release_worker: failed to release request %s: %s",
                request_id, exc, exc_info=True,
            )


# ── Order Timeout Worker ──────────────────────────────────────────────────────

_ORDER_TIMEOUT_INTERVAL_S = 5 * 60   # check every 5 minutes
_ORDER_TIMEOUT_AGE_M      = 30       # cancel orders pending > 30 minutes


async def order_timeout_worker() -> None:
    """
    Cancel orders that have been in 'pending' status for too long.

    A pending order means the session was reserved and balance deducted but
    the OTP was never delivered (listener crashed, session invalid, etc.).
    After ORDER_TIMEOUT_AGE_M minutes we cancel + refund automatically.
    """
    _log.info("order_timeout_worker: started")
    await asyncio.sleep(60)

    while True:
        try:
            await _run_order_timeouts()
        except asyncio.CancelledError:
            _log.info("order_timeout_worker: cancelled")
            return
        except Exception as exc:
            _log.error("order_timeout_worker unhandled error: %s", exc, exc_info=True)
        await asyncio.sleep(await _effective_interval("order_timeout", _ORDER_TIMEOUT_INTERVAL_S))


async def _run_order_timeouts() -> None:
    from server.utils.database.orderdb import ordersdb, cancel_order, refund_order
    from server.utils.database.sessiondb import revert_session_sold
    from server.utils.database.userdb import update_balance
    from server.utils.database.walletdb import log_transaction
    from server.utils.database.configdb import get_setting

    timeout_minutes = int(await get_setting("order_timeout_minutes") or _ORDER_TIMEOUT_AGE_M)
    cutoff = utcnow() - timedelta(minutes=timeout_minutes)

    # ── Case 1: "pending" orders ─────────────────────────────────────────────
    # These arise when buy_from_server() was called but record_purchase() or
    # complete_order() never finished (e.g. a server crash between those two
    # awaits).  The account was already reserved via mark_session_sold(), so
    # we MUST call revert_session_sold() here — without it the account stays
    # locked as "sold" forever, silently shrinking the available stock pool.
    pending_orders = await ordersdb.find(
        {"status": "pending", "created_at": {"$lt": cutoff}},
        {"_id": 0},
    ).to_list(length=50)

    if pending_orders:
        _log.info("order_timeout_worker: timing out %d stale pending order(s)", len(pending_orders))
    for order in pending_orders:
        order_id   = order.get("order_id", "")
        buyer_id   = order.get("buyer_id")
        amount     = order.get("amount", 0.0)
        account_id = order.get("account_id")
        try:
            ok = await cancel_order(order_id, reason="Auto-cancelled: OTP delivery timeout")
            if ok:
                # Release the reserved session back to the unsold pool
                if account_id:
                    try:
                        await revert_session_sold(account_id)
                        _log.info(
                            "order_timeout_worker: reverted session %s for cancelled order %s",
                            account_id, order_id,
                        )
                    except Exception as exc:
                        _log.error(
                            "order_timeout_worker: revert_session_sold(%s) failed for order %s: %s",
                            account_id, order_id, exc,
                        )
                if buyer_id and amount > 0:
                    await update_balance(buyer_id, amount)
                    await log_transaction(
                        user_id=buyer_id,
                        txn_type="refund",
                        amount=amount,
                        ref_id=order_id,
                        note="Auto-refund: order timed out after OTP delivery failure",
                    )
                    try:
                        from server import bot
                        await bot.send_message(
                            buyer_id,
                            f"⚠️ **Order Timed Out**\n\n"
                            f"🆔 Order `{order_id}` was automatically cancelled because "
                            f"the OTP could not be delivered within {timeout_minutes} minutes.\n\n"
                            f"💰 `${amount:g}` has been refunded to your balance.\n"
                            f"Please try again or contact support.",
                        )
                    except Exception:
                        pass
            _log.info(
                "order_timeout_worker: cancelled pending order %s (refund $%.4f to user %s)",
                order_id, amount, buyer_id,
            )
        except Exception as exc:
            _log.error("order_timeout_worker: error cancelling order %s: %s", order_id, exc)

    # ── Case 2: "completed" orders never delivered ────────────────────────────
    # buy_from_server() calls complete_order() immediately after record_purchase(),
    # BEFORE OTP delivery.  If the process restarts between complete_order() and
    # mark_order_delivered() the order stays "completed" with delivered_at=None
    # and the in-memory _BUY_AUTH listener is lost — the buyer paid but never
    # received their credentials.
    #
    # Use a 2× timeout here to avoid false positives: delivery could succeed but
    # the DB write of delivered_at could lag.  We only recover orders that are
    # clearly abandoned (well past the listener window).
    undelivered_cutoff = utcnow() - timedelta(minutes=max(timeout_minutes * 2, 120))
    undelivered_orders = await ordersdb.find(
        {
            "status":       "completed",
            "delivered_at": None,
            "created_at":   {"$lt": undelivered_cutoff},
        },
        {"_id": 0},
    ).to_list(length=50)

    if undelivered_orders:
        _log.info(
            "order_timeout_worker: recovering %d completed-but-undelivered order(s)",
            len(undelivered_orders),
        )
    for order in undelivered_orders:
        order_id   = order.get("order_id", "")
        buyer_id   = order.get("buyer_id")
        amount     = order.get("amount", 0.0)
        account_id = order.get("account_id")
        try:
            ok = await refund_order(order_id, "Auto-refund: OTP delivery lost after process restart")
            if ok:
                # Release the session back to unsold pool so it can be sold again
                if account_id:
                    try:
                        await revert_session_sold(account_id)
                        _log.info(
                            "order_timeout_worker: reverted session %s for undelivered order %s",
                            account_id, order_id,
                        )
                    except Exception as exc:
                        _log.error(
                            "order_timeout_worker: revert_session_sold(%s) failed for undelivered order %s: %s",
                            account_id, order_id, exc,
                        )
                if buyer_id and amount > 0:
                    await update_balance(buyer_id, amount)
                    await log_transaction(
                        user_id=buyer_id,
                        txn_type="refund",
                        amount=amount,
                        ref_id=order_id,
                        note="Auto-refund: OTP delivery lost after server restart",
                    )
                    try:
                        from server import bot
                        await bot.send_message(
                            buyer_id,
                            f"⚠️ **Order Recovery**\n\n"
                            f"🆔 Order `{order_id}` was automatically refunded because "
                            f"the OTP delivery listener was lost (server restart).\n\n"
                            f"💰 `${amount:g}` has been refunded to your balance.\n"
                            f"Please try again or contact support.",
                        )
                    except Exception:
                        pass
            _log.info(
                "order_timeout_worker: refunded undelivered order %s ($%.4f to user %s)",
                order_id, amount, buyer_id,
            )
        except Exception as exc:
            _log.error("order_timeout_worker: error refunding undelivered order %s: %s", order_id, exc)


# ── Low Stock Alert Worker ────────────────────────────────────────────────────

_LOW_STOCK_INTERVAL_S = 30 * 60   # check every 30 minutes
_LOW_STOCK_THRESHOLD  = 5         # alert when clean stock drops to this level


async def low_stock_alert_worker() -> None:
    """Notify admin via Telegram when any country's clean stock falls below threshold."""
    _log.info("low_stock_alert_worker: started")
    await asyncio.sleep(300)  # wait 5 min after startup

    while True:
        try:
            await _run_low_stock_check()
        except asyncio.CancelledError:
            _log.info("low_stock_alert_worker: cancelled")
            return
        except Exception as exc:
            _log.error("low_stock_alert_worker unhandled error: %s", exc, exc_info=True)
        await asyncio.sleep(await _effective_interval("low_stock_alert", _LOW_STOCK_INTERVAL_S))


async def _run_low_stock_check() -> None:
    from server.core import memstore
    from server.utils.database.configdb import get_setting
    import config as _cfg  # noqa: PLC0415 — import inside function to avoid circular deps

    threshold = int(await get_setting("low_stock_threshold") or _LOW_STOCK_THRESHOLD)
    low_countries = []

    for cc, counts in memstore.stock_counts.items():
        clean = counts.get("clean", 0)
        country = memstore.get_country(cc)
        if not country:
            continue
        if country.get("temp_disable"):
            continue  # skip disabled countries
        if clean <= threshold:
            low_countries.append((country.get("country_name", cc), cc, clean))

    if not low_countries:
        return

    try:
        from server import bot
        lines = "\n".join(f"  • {name} ({cc}): {n} clean" for name, cc, n in low_countries)
        await bot.send_message(
            _cfg.OWNER_ID,
            f"⚠️ **Low Stock Alert**\n\n"
            f"The following countries have ≤{threshold} clean accounts in stock:\n\n"
            f"{lines}\n\n"
            f"Upload more sessions to prevent order failures.",
        )
        _log.info("low_stock_alert_worker: alerted admin about %d low-stock country(ies)", len(low_countries))
    except Exception as exc:
        _log.warning("low_stock_alert_worker: could not notify admin: %s", exc)


# ── ZIP Cleanup Worker ────────────────────────────────────────────────────────

_ZIP_CLEANUP_INTERVAL_S = 60 * 60   # hourly
_ZIP_MAX_AGE_HOURS      = 2


async def zip_cleanup_worker() -> None:
    """Remove temporary ZIP files older than ZIP_MAX_AGE_HOURS from /tmp."""
    _log.info("zip_cleanup_worker: started")
    await asyncio.sleep(120)

    while True:
        try:
            await _run_zip_cleanup()
        except asyncio.CancelledError:
            _log.info("zip_cleanup_worker: cancelled")
            return
        except Exception as exc:
            _log.error("zip_cleanup_worker unhandled error: %s", exc, exc_info=True)
        await asyncio.sleep(await _effective_interval("zip_cleanup", _ZIP_CLEANUP_INTERVAL_S))


async def _run_zip_cleanup() -> None:
    import os
    import glob
    cutoff = utcnow().timestamp() - (_ZIP_MAX_AGE_HOURS * 3600)
    patterns = ["/tmp/*.zip", "/tmp/tg_sessions/*", "/tmp/*.session"]
    removed = 0
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                mtime = await asyncio.to_thread(os.path.getmtime, path)
                if mtime < cutoff:
                    await asyncio.to_thread(os.remove, path)
                    removed += 1
            except Exception:
                pass
    if removed:
        _log.info("zip_cleanup_worker: removed %d stale temp file(s)", removed)


# ── BIN retention cleanup ─────────────────────────────────────────────────────
_BIN_CLEANUP_INTERVAL_S = 60 * 60  # check once per hour


async def bin_cleanup_worker() -> None:
    """Delete BIN records after their 30-day retention period."""
    _log.info("bin_cleanup_worker: started")
    await asyncio.sleep(120)
    while True:
        try:
            await _run_bin_cleanup()
        except asyncio.CancelledError:
            _log.info("bin_cleanup_worker: cancelled")
            return
        except Exception as exc:
            _log.error("bin_cleanup_worker unhandled error: %s", exc, exc_info=True)
        await asyncio.sleep(await _effective_interval("bin_cleanup", _BIN_CLEANUP_INTERVAL_S))


async def _run_bin_cleanup() -> None:
    from server.utils.database.sessiondb import cleanup_expired_bin_sessions
    deleted = await cleanup_expired_bin_sessions()
    if deleted:
        _log.info("bin_cleanup_worker: deleted %d expired BIN session(s)", deleted)


# ── Startup helper ────────────────────────────────────────────────────────────
# Registry for manually triggering workers from the admin panel
_WORKER_REGISTRY: dict[str, dict] = {}


def _register_worker(name: str, fn, interval_s: int, description: str) -> None:
    _WORKER_REGISTRY[name] = {
        "name":        name,
        "description": description,
        "interval_s":  interval_s,
        "fn":          fn,
        "task":        None,
        "last_run":    None,
        "run_count":   0,
        "error_count": 0,
    }


async def get_worker_status() -> list[dict]:
    """Return status snapshot for all registered workers (admin panel use).

    `interval_s` is the currently effective interval (admin override or
    default). `default_interval_s` is the code default so the UI can render
    a Reset button.
    """
    from server.utils.database.configdb import get_setting

    out = []
    for w in _WORKER_REGISTRY.values():
        task: asyncio.Task | None = w.get("task")
        default_s = w["interval_s"]
        try:
            override = await get_setting(f"worker_interval_{w['name']}")
            effective = int(override) if override is not None else default_s
        except Exception:
            effective = default_s
        out.append({
            "name":              w["name"],
            "description":       w["description"],
            "interval_s":        effective,
            "default_interval_s": default_s,
            "overridden":        override is not None if 'override' in locals() else False,
            "running":           task is not None and not task.done(),
            "last_run":          w["last_run"].isoformat() if w.get("last_run") else None,
            "run_count":         w["run_count"],
            "error_count":       w["error_count"],
        })
    return out


async def set_worker_interval(name: str, seconds: int | None) -> bool:
    """Set (or clear when `seconds is None`) a worker's tick interval override."""
    if name not in _WORKER_REGISTRY:
        return False
    from server.utils.database.configdb import set_setting
    key = f"worker_interval_{name}"
    if seconds is None:
        await set_setting(key, None)
        return True
    seconds = int(seconds)
    if seconds < 5:
        seconds = 5
    if seconds > 24 * 3600:
        seconds = 24 * 3600
    await set_setting(key, seconds)
    return True


async def trigger_worker(name: str) -> bool:
    """Manually trigger the inner function of a registered worker (admin use)."""
    entry = _WORKER_REGISTRY.get(name)
    if not entry:
        return False
    fn = entry["fn"]
    try:
        await fn()
        entry["last_run"]  = utcnow()
        entry["run_count"] += 1
        return True
    except Exception as exc:
        _log.error("Manual trigger of worker %s failed: %s", name, exc)
        entry["error_count"] += 1
        return False


def start_background_workers() -> list:
    """
    Create and schedule all background workers.

    Must be called after the event loop is running (i.e. inside an async
    context, after bot.start()).  Returns task list so the caller can cancel
    on shutdown.
    """
    loop = asyncio.get_running_loop()

    _register_worker("termination_retry",         _run_termination_retries,      _TERM_RETRY_INTERVAL_S,          "Retry session termination for pending sell requests")
    _register_worker("payment_release",           _run_payment_releases,         _PAYMENT_CHECK_INTERVAL_S,       "Release pending_balance after 48-hour hold")
    _register_worker("order_timeout",             _run_order_timeouts,           _ORDER_TIMEOUT_INTERVAL_S,       "Auto-cancel + refund stale pending orders")
    _register_worker("low_stock_alert",           _run_low_stock_check,          _LOW_STOCK_INTERVAL_S,           "Alert admin when country stock is low")
    _register_worker("zip_cleanup",               _run_zip_cleanup,              _ZIP_CLEANUP_INTERVAL_S,         "Remove stale temp ZIP and session files")
    _register_worker("withdrawal_verification",   _run_withdrawal_verification,  _WITHDRAWAL_VERIFY_INTERVAL_S,   "Poll gateway for pending withdrawal statuses")
    _register_worker("binance_pay_deposit_poll",  _run_binance_pay_poll,         _BINANCE_PAY_POLL_INTERVAL_S,    "Auto-confirm Binance Pay deposits (webhook fallback)")
    _register_worker("bin_cleanup",                _run_bin_cleanup,              _BIN_CLEANUP_INTERVAL_S,         "Delete BIN sessions after 30-day retention")

    t1 = loop.create_task(termination_retry_worker(),       name="termination_retry_worker")
    t2 = loop.create_task(payment_release_worker(),         name="payment_release_worker")
    t3 = loop.create_task(order_timeout_worker(),           name="order_timeout_worker")
    t4 = loop.create_task(low_stock_alert_worker(),         name="low_stock_alert_worker")
    t5 = loop.create_task(zip_cleanup_worker(),             name="zip_cleanup_worker")
    t6 = loop.create_task(withdrawal_verification_worker(), name="withdrawal_verification_worker")
    t7 = loop.create_task(binance_pay_deposit_poller(),     name="binance_pay_deposit_poller")
    t8 = loop.create_task(bin_cleanup_worker(),              name="bin_cleanup_worker")

    _WORKER_REGISTRY["termination_retry"]["task"]        = t1
    _WORKER_REGISTRY["payment_release"]["task"]          = t2
    _WORKER_REGISTRY["order_timeout"]["task"]            = t3
    _WORKER_REGISTRY["low_stock_alert"]["task"]          = t4
    _WORKER_REGISTRY["zip_cleanup"]["task"]              = t5
    _WORKER_REGISTRY["withdrawal_verification"]["task"]  = t6
    _WORKER_REGISTRY["binance_pay_deposit_poll"]["task"] = t7
    _WORKER_REGISTRY["bin_cleanup"]["task"]                = t8

    _log.info(
        "Background workers scheduled: termination_retry_worker, payment_release_worker, "
        "order_timeout_worker, low_stock_alert_worker, zip_cleanup_worker, "
        "withdrawal_verification_worker, binance_pay_deposit_poller, bin_cleanup_worker"
    )
    return [t1, t2, t3, t4, t5, t6, t7, t8]


# ── Binance Pay Deposit Poller ────────────────────────────────────────────────
# Fallback for missed webhooks — polls pending Binance Pay deposits every 2 min
# and auto-confirms / expires them without any user action.

_BINANCE_PAY_POLL_INTERVAL_S = 2 * 60   # 2 minutes


async def binance_pay_deposit_poller() -> None:
    """
    Long-running background task.  Scans all pending Binance Pay deposits and
    auto-confirms / expires them by querying the Binance Pay Merchant API.

    This is a webhook fallback — it ensures every paid deposit is credited
    even if the webhook was never delivered (e.g. server was down, Replit
    sleeping, or Binance retried past its limit).
    """
    _log.info("binance_pay_deposit_poller: started")
    await asyncio.sleep(30)  # brief delay to let the bot & DB fully initialise

    while True:
        try:
            await _run_binance_pay_poll()
        except asyncio.CancelledError:
            _log.info("binance_pay_deposit_poller: cancelled")
            return
        except Exception as exc:
            _log.error("binance_pay_deposit_poller unhandled error: %s", exc, exc_info=True)

        await asyncio.sleep(await _effective_interval("binance_pay_deposit_poll", _BINANCE_PAY_POLL_INTERVAL_S))


async def _run_binance_pay_poll() -> None:
    """
    Inner poll cycle — called by the worker loop and triggerable from the admin panel.
    """
    from os import getenv as _getenv
    api_key    = _getenv("BINANCE_PAY_API_KEY", "").strip()
    secret_key = _getenv("BINANCE_PAY_SECRET_KEY", "").strip()

    if not api_key or not secret_key:
        return   # provider not configured — nothing to do

    from server.utils.database.walletdb import depositsdb, confirm_deposit
    from server.services.deposit.providers.binance_pay import query_order, map_binance_status

    # Fetch all pending Binance Pay deposits (not yet in a final state)
    cursor = depositsdb.find(
        {"method": "binance_pay", "status": {"$in": ["pending", "confirming"]}},
        {"_id": 0, "deposit_id": 1, "user_id": 1, "amount": 1, "expires_at": 1},
    )
    pending = await cursor.to_list(length=200)

    if not pending:
        return

    _log.debug("binance_pay_deposit_poller: checking %d pending deposit(s)", len(pending))

    now = utcnow()

    for dep in pending:
        deposit_id = dep["deposit_id"]
        user_id    = dep.get("user_id")
        try:
            result = await query_order(deposit_id)
        except Exception as exc:
            _log.warning("binance_pay_deposit_poller: query_order(%s) raised: %s", deposit_id, exc)
            continue

        if not result.get("ok"):
            # API error — skip silently (will retry next cycle)
            continue

        b_status   = result.get("status", "")
        int_status = result.get("internal", "")

        if int_status == "completed":
            dep_doc = await confirm_deposit(deposit_id, confirmed_by=None)
            if dep_doc:
                _log.info(
                    "binance_pay_deposit_poller: credited $%.4f to user %s (deposit=%s binance=%s)",
                    dep_doc["amount"], user_id, deposit_id, b_status,
                )
                # Persist Binance transaction ID if present
                tx = (result.get("data") or {}).get("transactionId")
                if tx:
                    await depositsdb.update_one(
                        {"deposit_id": deposit_id},
                        {"$set": {"tx_hash": tx, "extra.tx_hash": tx,
                                  "extra.biz_status": b_status}},
                    )
                # Notify user
                try:
                    from server.utils.notifications import notify
                    await notify(
                        user_id, "deposit_confirmed",
                        deposit_id=deposit_id,
                        amount=dep_doc["amount"],
                        method="Binance Pay",
                    )
                except Exception as exc:
                    _log.warning(
                        "binance_pay_deposit_poller: notify failed for user %s: %s",
                        user_id, exc,
                    )

        elif int_status in ("expired", "failed"):
            # Mark expired / cancelled orders so they don't keep getting polled
            result_upd = await depositsdb.update_one(
                {"deposit_id": deposit_id, "status": {"$in": ["pending", "confirming"]}},
                {"$set": {"status": int_status,
                          "note": f"Binance Pay order {b_status}",
                          "extra.biz_status": b_status}},
            )
            if result_upd.modified_count:
                _log.info(
                    "binance_pay_deposit_poller: %s → %s (binance=%s)",
                    deposit_id, int_status, b_status,
                )

        else:
            # Still pending — check if our local expiry has passed
            expires_at = dep.get("expires_at")
            if expires_at and expires_at.tzinfo is None:
                from datetime import timezone as _tz
                expires_at = expires_at.replace(tzinfo=_tz.utc)
            if expires_at and now > expires_at:
                await depositsdb.update_one(
                    {"deposit_id": deposit_id, "status": {"$in": ["pending", "confirming"]}},
                    {"$set": {"status": "expired", "note": "Expired (local TTL exceeded)"}},
                )
                _log.info("binance_pay_deposit_poller: %s → expired (local TTL)", deposit_id)


# ── Withdrawal Verification Worker ────────────────────────────────────────────

import config as _config
_WITHDRAWAL_VERIFY_INTERVAL_S = _config.WITHDRAWAL_VERIFY_INTERVAL_S


async def withdrawal_verification_worker() -> None:
    """
    Long-running background task — polls OxaPay for pending withdrawal statuses.
    Also expires withdrawals that have been unresolved beyond WITHDRAWAL_EXPIRE_HOURS.
    """
    from server.services.withdrawal.workers import withdrawal_verification_worker as _wvw
    await _wvw()


async def _run_withdrawal_verification() -> None:
    """Inner function called by the generic worker loop (admin trigger support)."""
    from server.services.withdrawal.workers import _run_verification_cycle
    await _run_verification_cycle()
