

# ─────────────────────────────────────────────────────────────────────────────
# PREMIUM EMOJIS (Telegram 9.4+ custom emoji entities).
#
# Rendered via pyrogram's default Markdown syntax:  ![e](tg://emoji?id=ID)
# Clients on Telegram 9.4+ show the premium emoji; older clients / non-premium
# users see the plain unicode fallback automatically — nothing else to do.
#
# Change any ID here to swap what the users see across this whole file.
# `_pe("✅")` also works if you want to compose new strings by hand.
# ─────────────────────────────────────────────────────────────────────────────
PREMIUM_EMOJIS = {
    '⏳': "6129574787078429498",
    '☎️': "5330237710655306682",
    '⚠️': "6129939837823753679",
    '⚡': "6129792056589031358",
    '✅': "6129492160497589882",
    '❌': "6129846551134084367",
    '❓': "6129472184604695207",
    '➡️': "6129792056589031358",
    '⬅️': "6129550284290006595",
    '🌍': "6296303781126604562",
    '👤': "5316979275461573049",
    '💰': "6129731974291527294",
    '💵': "6129731974291527294",
    '💼': "5316561753100792764",
    '📄': "6129579803600231171",
    '📋': "6129579803600231171",
    '📌': "6131886699254388574",
    '📞': "5330237710655306682",
    '📤': "6131886699254388574",
    '📦': "6131886699254388574",
    '📱': "5330237710655306682",
    '📲': "5330237710655306682",
    '🔄': "6129792056589031358",
    '🔍': "5316722951813346475",
    '🔐': "6129550284290006595",
    '🔴': "6129846551134084367",
    '🚫': "6129846551134084367",
    '🛒': "6131886699254388574",
    '🟢': "6129492160497589882",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Market plugin — Buy and Sell flows.

BUY ACCOUNT flow (Server → User — buyer logs in on their OWN device):
  1. User browses paginated country grid (buy_page_1)
  2. Selects country → country detail page (buy/back/cancel)
  3. Confirms → balance deducted, order created → bot hands over the phone
     number and tells the buyer to log in with it in their own Telegram app
  4. Buyer enters that number in Telegram → Telegram sends the login code to
     the account; the bot's still-active old session captures it and relays
     the code (+ 2FA password, if any) to the buyer in one clean message
  5. Buyer finishes logging in on their own device — the bot detects the new
     session appearing and revokes every other session (including its own),
     so the buyer ends up as the sole owner. No backend sign-in by the bot.

BUY SESSION flow (Server → User — buyer gets a ready-made session file):
  1. User browses paginated country grid (buy_session_page_1)
  2. Selects country → asked for quantity
  3. Bot reserves existing verified inventory sessions and delivers
     the .session + .json files zipped, in one message

SELL flow (User → Server):
  1. User picks country, uploads .session file
  2. Bot validates, creates sell request for admin review
"""

import asyncio
import math
import os
from html import escape as _he

from pyrogram import enums, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from server.utils.bot_utils import Btn as InlineKeyboardButton
from telethon import TelegramClient

from server import bot, LOGGER
from server.utils.database import get_balance, get_all_countries, get_country, get_user_lang
from server.utils.database.userdb import update_balance
from server.utils.database.orderdb import refund_order
from server.utils.database.walletdb import log_transaction
from server.services.market_service import buy_from_server, MarketError
from server.plugins.bot._shared_state import (
    market_pending as _PENDING,
    sell_state as _SELL_STATE,
    sell_account_state as _SA_STATE,
    buy_auth_state as _BUY_AUTH,
    buy_delivered_state as _BUY_DELIVERED,
    buy_session_pending as _BUY_SESSION_PENDING,
    buy_search_pending as _BUY_SEARCH,
)
import config
from server.utils.bot_utils import DIV as _DIV, flag as _flag, safe_edit as _safe_edit, md_emoji_to_html as _md_emoji_to_html
from server.utils.constants import PAGE_SIZE
from server.utils.stock_display import displayed_stock
from server.utils.sessions.telethon_client import (
    connect_with_proxy_fallback,
    new_temp_session_path,
    write_session_bytes,
    cleanup_session_files,
)
from strings import get_string

_log = LOGGER(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _back_market_btn(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s.get("btn_back_to_market", "![⬅️](tg://emoji?id=5258236805890710909) Back to Market"), callback_data="back_home")],
    ])


def _country_label(c: dict) -> str:
    code = c.get("code", "??")
    idc = c.get("idc", "").lstrip("+")
    price = c.get("price", 0)
    idc_str = f"+{idc}" if idc else ""
    return f"{_flag(code)} {code}{idc_str} | {price:g}$"


def _sell_country_label(c: dict) -> str:
    code = c.get("code", "??")
    idc = c.get("idc", "").lstrip("+")
    sell_price = c.get("sell_price", 0)
    idc_str = f"+{idc}" if idc else ""
    # When the country is marked full by admin, show "Full" in place of the price.
    if c.get("is_full"):
        return f"{_flag(code)} {code}{idc_str} | Full"
    return f"{_flag(code)} {code}{idc_str} | {sell_price:g}$"


async def _build_available_countries_message() -> str:
    """
    Build a single expandable-blockquote message listing every country
    currently open for selling, with per-status prices and live stock counts.
    Rendered with HTML parse mode so Telegram shows a collapsible quote.
    """
    from server.core import memstore as _memstore
    try:
        from server.utils.database.configdb import get_setting as _get_setting
        hold_hours = int(await _get_setting("payment_hold_hours") or 48)
    except Exception:
        hold_hours = 48
    hold_seconds = hold_hours * 3600

    countries = await get_all_countries(sell_only=True) or []
    # Stable order: full countries last, others by name
    countries.sort(key=lambda c: (bool(c.get("is_full")), (c.get("country_name") or c.get("code") or "").lower()))

    lines: list[str] = []
    for c in countries:
        code = (c.get("code") or "??").upper()
        idc = c.get("idc") or ""
        if idc and not idc.startswith("+"):
            idc = f"+{idc}"
        name = c.get("country_name") or code
        base = float(c.get("sell_price") or 0.0)

        def _p(key: str) -> float:
            v = float(c.get(key) or 0.0)
            return v if v > 0 else base

        clean = _p("price_clean") if c.get("accept_clean") else 0.0
        temp  = _p("price_temp_spam") if c.get("accept_temp_spam") else 0.0
        perm  = _p("price_perm_spam") if c.get("accept_perm_spam") else 0.0

        try:
            stock = _memstore.get_stock_count(code, clean_only=False)
        except Exception:
            stock = 0

        if c.get("is_full"):
            price_str = "Full"
        else:
            price_str = (
                f"Free:${clean:g}|New:${clean:g}|"
                f"Spam:${temp:g}|Perm:${perm:g}|{stock}|{hold_seconds}s"
            )

        lines.append(f"{_flag(code)} {idc} {_he(name)}\n{price_str}")

    body = "\n\n".join(lines) if lines else "No countries available right now."
    header = f"🌍 <b>Available Countries</b> : ({len(countries)}):"
    return f"{header}\n\n<blockquote expandable>{body}</blockquote>"


async def _refund_buy_auth(user_id: int, state: dict) -> None:
    """
    Full cleanup when a buy-auth flow is aborted or fails.
    Reverts session stock, credits balance back, marks order refunded.
    Disconnects Telethon client and removes temp files.
    """
    # Restore session to unsold stock
    try:
        from server.utils.database.sessiondb import revert_session_sold
        await revert_session_sold(state["account_id"])
    except Exception as exc:
        _log.error("_refund_buy_auth revert_session_sold(%s): %s", state.get("account_id"), exc)

    # Credit balance back
    price = state.get("price", 0)
    order_id = state.get("order_id", "")
    if price > 0:
        try:
            await update_balance(user_id, price)
        except Exception as exc:
            _log.error("_refund_buy_auth update_balance(%s, +%s): %s", user_id, price, exc)
        try:
            if order_id:
                await refund_order(order_id, "buyer_cancelled_otp_flow")
                await log_transaction(
                    user_id=user_id,
                    txn_type="refund",
                    amount=price,
                    ref_id=order_id,
                    note="Buy cancelled — OTP/2FA flow aborted",
                )
        except Exception as exc:
            _log.error("_refund_buy_auth log_transaction(%s): %s", user_id, exc)

    # Disconnect Telethon client
    tg_client: TelegramClient | None = state.get("tg_client")
    if tg_client:
        try:
            await tg_client.disconnect()
        except Exception:
            pass

    # Disconnect + clean up the OLD (already-authorized) session client used
    # to auto-capture the login code — only present for the auto-OTP flow.
    old_client = state.get("old_client")
    if old_client:
        try:
            await old_client.disconnect()
        except Exception:
            pass
    old_tmp_path = state.get("old_tmp_path")
    if old_tmp_path:
        try:
            await cleanup_session_files(old_tmp_path)
        except Exception:
            pass

    # Delete temp session file
    session_path = state.get("session_path", "")
    if session_path:
        try:
            session_file = session_path + ".session"
            if os.path.exists(session_file):
                os.unlink(session_file)
            tmpdir = os.path.dirname(session_path)
            if os.path.isdir(tmpdir):
                os.rmdir(tmpdir)
        except Exception:
            pass

    _BUY_AUTH.pop(user_id, None)


# ── Order message rendering (single message, edited through its lifecycle) ───

_OTP_REFRESH_WINDOW = 3600  # seconds an order stays "live" for a Get New OTP retry after delivery (60 min)


def _order_details_block(state: dict) -> str:
    return (
        f"> 🆔 Order ID: `#{state['order_id']}`\n"
        f"> ![🌍](tg://emoji?id=6296303781126604562) Country: {state['country_name']}\n"
        f"> ![☎️](tg://emoji?id=5330237710655306682) Dial Code: `{state['dial_code']}`\n"
    )


def _order_text_waiting(state: dict) -> str:
    return (
        f"![✅](tg://emoji?id=6129492160497589882) **Order Created Successfully**\n\n"
        f"{_order_details_block(state)}"
        f"> ![📱](tg://emoji?id=5330237710655306682) Login Number: `{state['phone']}`\n"
        f"> ![💰](tg://emoji?id=6129731974291527294) Price: `${state['price']:g}`\n"
        f"> ![📦](tg://emoji?id=6131886699254388574) Status: Waiting for Telegram Login"
    )


def _order_text_ready(state: dict, otp: str) -> str:
    tfa_password = state.get("tfa_password") or ""
    tfa_line = f"`{tfa_password}`" if tfa_password else "Not Enabled"
    return (
        f"![✅](tg://emoji?id=6129492160497589882) **Login Credentials Ready**\n\n"
        f"{_order_details_block(state)}"
        f"> ![📱](tg://emoji?id=5330237710655306682) Phone: `{state['phone']}`\n"
        f"> 🔢 Login Code: `{otp}`\n"
        f"> ![🔐](tg://emoji?id=6129550284290006595) 2FA Password: {tfa_line}"
    )


def _order_text_failed(state: dict, reason: str) -> str:
    return (
        f"![❌](tg://emoji?id=6129846551134084367) **Order Failed**\n\n"
        f"{_order_details_block(state)}"
        f"> ![📱](tg://emoji?id=5330237710655306682) Login Number: `{state['phone']}`\n"
        f"> ![📦](tg://emoji?id=6131886699254388574) Status: Refunded\n\n"
        f"{reason}"
    )


def _order_text_completed(state: dict) -> str:
    return (
        f"![✅](tg://emoji?id=6129492160497589882) **Order Completed**\n\n"
        f"{_order_details_block(state)}"
        f"> ![📱](tg://emoji?id=5330237710655306682) Phone: `{state['phone']}`\n"
        f"> ![📦](tg://emoji?id=6131886699254388574) Status: Completed"
    )


def _order_text_loggedout(state: dict) -> str:
    return (
        f"![✅](tg://emoji?id=6129492160497589882) **Order Completed — Session Logged Out**\n\n"
        f"{_order_details_block(state)}"
        f"> ![📱](tg://emoji?id=5330237710655306682) Phone: `{state['phone']}`\n"
        f"> ![📦](tg://emoji?id=6131886699254388574) Status: Logged Out (by you)\n\n"
        f"The old session has been terminated. Only your own login remains active on this account."
    )


def _order_text_logout_failed(state: dict, reason: str) -> str:
    return (
        f"![⚠️](tg://emoji?id=6129939837823753679) **Logout Failed**\n\n"
        f"{_order_details_block(state)}"
        f"> ![📱](tg://emoji?id=5330237710655306682) Phone: `{state['phone']}`\n"
        f"> ![📦](tg://emoji?id=6131886699254388574) Status: Logged In\n\n"
        f"{reason}"
    )


def _order_ready_buttons(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("![🔄](tg://emoji?id=6129792056589031358) Get New OTP", callback_data=f"buy_otp_refresh_{order_id}")],
        [InlineKeyboardButton("🚪 Logout This Session", callback_data=f"buy_logout_{order_id}")],
    ])


async def _edit_order_message(message: Message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception as exc:
        _log.warning("_edit_order_message: %s", exc)


async def _finalize_buy_order(order_id: str, message: Message) -> None:
    """
    Closes out a delivered order once the Get New OTP window has elapsed:
    1. Delete all chats and leave all channels/groups on the sold account.
    2. Disconnect the old (seller-side) session.
    3. Clean up temp files.
    4. Edit the order message to its final "Completed" state.
    """
    state = _BUY_DELIVERED.get(order_id)
    if state is None:
        return
    old_client   = state.get("old_client")
    old_tmp_path = state.get("old_tmp_path")
    phone        = state.get("phone", "?")

    if old_client:
        # Wipe all chats / leave all groups before disconnecting
        try:
            from server.stock.account_cleanup import cleanup_account_data
            await cleanup_account_data(old_client, phone)
        except Exception as exc:
            _log.warning("_finalize_buy_order: cleanup failed for order %s (%s): %s", order_id, phone, exc)
        try:
            await old_client.disconnect()
        except Exception:
            pass
    if old_tmp_path:
        try:
            await cleanup_session_files(old_tmp_path)
        except Exception:
            pass
    await _edit_order_message(message, _order_text_completed(state))
    _BUY_DELIVERED.pop(order_id, None)


async def _schedule_finalize(order_id: str, message: Message, state: dict) -> None:
    """(Re)schedules the auto-finalize timer, cancelling any prior one."""
    old_task = state.get("finalize_task")
    if old_task and not old_task.done():
        old_task.cancel()

    async def _wait_then_finalize():
        try:
            await asyncio.sleep(_OTP_REFRESH_WINDOW)
        except asyncio.CancelledError:
            return
        await _finalize_buy_order(order_id, message)

    state["finalize_task"] = asyncio.create_task(_wait_then_finalize())


async def _run_buy_account_handover(bot_client, message: Message, user_id: int, otp_future) -> None:
    """
    Background task (detached from the callback-query handler so a dispatcher
    worker isn't held for the wait): the buyer logs in on their OWN device
    with the phone number they were just given. This task waits for
    Telegram's login code — captured via the account's OLD still-authorized
    session (`old_client`) the instant the buyer requests it from their
    app — then edits the single order message in place with the delivered
    credentials. No new message is ever sent; the order message is the only
    source of truth throughout its lifecycle.
    """
    from server.stock.otp_capture import wait_for_login_otp
    from server.utils.database.orderdb import mark_order_delivered
    from server.utils.database.auditdb import log_action

    state = _BUY_AUTH.get(user_id)
    if state is None:
        return
    phone = state["phone"]

    try:
        otp = await wait_for_login_otp(otp_future, timeout=300)
    except Exception as exc:
        _log.error("_run_buy_account_handover wait_for_login_otp(%s): %s", phone, exc, exc_info=True)
        otp = None

    # Buyer may have been refunded (or the flow otherwise cleared) while we waited.
    if _BUY_AUTH.get(user_id) is not state:
        return

    if not otp:
        old_client = state.get("old_client")
        old_tmp_path = state.get("old_tmp_path")
        if old_client:
            try:
                await old_client.disconnect()
            except Exception:
                pass
        if old_tmp_path:
            try:
                await cleanup_session_files(old_tmp_path)
            except Exception:
                pass
        await _refund_buy_auth(user_id, state)
        await _edit_order_message(
            message,
            _order_text_failed(
                state,
                "We did not detect a login attempt in time. Your balance has been refunded — please try again.",
            ),
        )
        return

    order_id = state.get("order_id", "")
    await _edit_order_message(message, _order_text_ready(state, otp), _order_ready_buttons(order_id))

    # Credentials are delivered — free up the "one order at a time" lock so
    # the buyer can start a new purchase right away. The delivered order
    # keeps living independently (Get New OTP / auto-finalize) in its own
    # registry, keyed by order_id instead of user_id.
    _BUY_AUTH.pop(user_id, None)
    _BUY_DELIVERED[order_id] = state

    try:
        await mark_order_delivered(order_id)
    except Exception as exc:
        _log.error("_run_buy_account_handover mark_order_delivered(%s): %s", order_id, exc, exc_info=True)

    # OTP was actually received and shown to the buyer.  Only now can this
    # order become a successful sale and affect Spent/buy counters/sales feed.
    try:
        from server.services.market_service import finalize_successful_purchase
        finalized = await finalize_successful_purchase(
            order_id,
            country_code=state.get("country_code", "XX"),
            country_name=state.get("country_name", state.get("country_code", "XX")),
        )
        if not finalized:
            _log.warning("Successful OTP delivery did not finalize accounting for order %s", order_id)
    except Exception as exc:
        _log.error("_run_buy_account_handover finalize accounting failed for %s: %s", order_id, exc, exc_info=True)

    try:
        await log_action(
            "order", "account_delivered",
            actor=str(user_id), target=order_id,
            detail=f"phone={phone}",
        )
    except Exception as exc:
        _log.error("_run_buy_account_handover log_action(%s): %s", order_id, exc, exc_info=True)

    # Keep the old session alive for a limited window in case the buyer needs
    # a fresh code (![🔄](tg://emoji?id=6129792056589031358) Get New OTP), then finalize the order automatically.
    await _schedule_finalize(order_id, message, state)


# ── BUY — "![🔄](tg://emoji?id=6129792056589031358) Get New OTP" (re-uses the same order, same message) ─────────────

@bot.on_callback_query(filters.regex(r"^buy_otp_refresh_(.+)$"))
async def buy_otp_refresh_cb(client, cq: CallbackQuery):
    from server.stock.otp_capture import start_login_otp_listener, wait_for_login_otp
    from server.utils.database.auditdb import log_action

    user_id = cq.from_user.id
    order_id = cq.matches[0].group(1)
    state = _BUY_DELIVERED.get(order_id)
    if state is None:
        await cq.answer("This order is no longer active.", show_alert=True)
        return
    if state.get("user_id") != user_id:
        # Defensive — should never trigger since only the buyer sees this message.
        await cq.answer("This order does not belong to you.", show_alert=True)
        return
    if state.get("otp_refresh_in_progress"):
        await cq.answer("Already requesting a new code — please wait…")
        return

    old_client = state.get("old_client")
    old_tmp_path = state.get("old_tmp_path")
    phone = state["phone"]
    if not old_client:
        await cq.answer("This order is no longer active.", show_alert=True)
        return

    state["otp_refresh_in_progress"] = True
    await cq.answer("![🔄](tg://emoji?id=6129792056589031358) Requesting a new login code…")

    try:
        # Telethon's `is_connected` is a method — calling it as a bare
        # attribute leaks a bound-method object that's always truthy, so
        # a silently-dropped socket never triggered a reconnect and the
        # OTP listener sat on a dead client. Call it, and reconnect on
        # any failure so the 60-min Get-New-OTP window actually works.
        connected = False
        try:
            connected = bool(old_client.is_connected())
        except Exception:
            connected = False
        if not connected:
            import config as _cfg
            try:
                await old_client.disconnect()
            except Exception:
                pass
            old_client, _pid, _pdoc = await connect_with_proxy_fallback(
                old_tmp_path[:-8], _cfg.API_ID, _cfg.API_HASH, state["country_code"],
            )
            state["old_client"] = old_client

        # Re-verify the session is still authorised on the seller side —
        # if the seller (or Telegram) killed it in the meantime, no code
        # will ever arrive and we should tell the buyer instead of just
        # timing out silently.
        try:
            authorised = await old_client.is_user_authorized()
        except Exception:
            authorised = True  # be permissive; treat unknown as "try anyway"
        if not authorised:
            otp = None
            _log.warning("buy_otp_refresh_cb(%s): old session no longer authorised.", phone)
        else:
            otp_future = start_login_otp_listener(old_client)
            otp = await wait_for_login_otp(otp_future, timeout=300)
    except Exception as exc:
        _log.error("buy_otp_refresh_cb(%s): %s", phone, exc, exc_info=True)
        otp = None
    finally:
        state["otp_refresh_in_progress"] = False

    if _BUY_DELIVERED.get(order_id) is not state:
        return

    if not otp:
        try:
            await cq.message.reply_text("![⚠️](tg://emoji?id=6129939837823753679) We could not detect a new login attempt in time. Try again with the button below.")
        except Exception:
            pass
        return

    await _edit_order_message(cq.message, _order_text_ready(state, otp), _order_ready_buttons(order_id))
    try:
        await log_action(
            "order", "account_otp_refreshed",
            actor=str(user_id), target=order_id,
            detail=f"phone={phone}",
        )
    except Exception as exc:
        _log.error("buy_otp_refresh_cb log_action: %s", exc, exc_info=True)

    await _schedule_finalize(order_id, cq.message, state)


# ── BUY — "🚪 Logout This Session" (buyer terminates the old/seller session) ──

@bot.on_callback_query(filters.regex(r"^buy_logout_(.+)$"))
async def buy_logout_cb(client, cq: CallbackQuery):
    """
    Lets the buyer terminate the OLD (seller-side) session themselves once
    they've logged in on their own device — closing the dual-access window
    on demand instead of waiting for the auto-finalize timer, which only
    disconnects (does not invalidate) the session.
    """
    from server.utils.database.auditdb import log_action

    user_id = cq.from_user.id
    order_id = cq.matches[0].group(1)
    state = _BUY_DELIVERED.get(order_id)
    if state is None:
        await cq.answer("This order is no longer active.", show_alert=True)
        return
    if state.get("user_id") != user_id:
        await cq.answer("This order does not belong to you.", show_alert=True)
        return
    if state.get("otp_refresh_in_progress"):
        await cq.answer("A new code request is in progress — please wait a moment then try again.")
        return
    if state.get("logout_in_progress"):
        await cq.answer("Already logging out — please wait…")
        return

    old_client = state.get("old_client")
    old_tmp_path = state.get("old_tmp_path")
    phone = state["phone"]
    if not old_client:
        await cq.answer("This session is no longer active.", show_alert=True)
        return

    state["logout_in_progress"] = True
    await cq.answer("🚪 Logging out the old session…")

    try:
        # Telethon's `is_connected` is a method — calling it as a bare attribute
        # returns the bound method (always truthy). Always call it with ().
        connected = False
        try:
            connected = bool(old_client.is_connected())
        except Exception:
            connected = False
        if not connected:
            import config as _cfg
            try:
                await old_client.disconnect()
            except Exception:
                pass
            old_client, _pid, _pdoc = await connect_with_proxy_fallback(
                old_tmp_path[:-8], _cfg.API_ID, _cfg.API_HASH, state["country_code"],
            )
            state["old_client"] = old_client
        await old_client.log_out()  # invalidates this session on Telegram's servers, then disconnects
        logout_ok = True
        logout_err = None
    except Exception as exc:
        logout_ok = False
        logout_err = str(exc)
        _log.error("buy_logout_cb log_out(%s): %s", phone, exc, exc_info=True)
        try:
            if old_client.is_connected():
                await old_client.disconnect()
        except Exception:
            pass
    finally:
        state["logout_in_progress"] = False

    if _BUY_DELIVERED.get(order_id) is not state:
        return

    if not logout_ok:
        await _edit_order_message(
            cq.message,
            _order_text_logout_failed(
                state,
                f"We could not log out the old session automatically ({logout_err}). "
                f"You can try again with the button below.",
            ),
            _order_ready_buttons(order_id),
        )
        return

    # Cancel the pending auto-finalize timer — logout already finished the job.
    finalize_task = state.get("finalize_task")
    if finalize_task and not finalize_task.done():
        finalize_task.cancel()

    if old_tmp_path:
        try:
            await cleanup_session_files(old_tmp_path)
        except Exception:
            pass

    try:
        await log_action(
            "order", "account_session_logged_out",
            actor=str(user_id), target=order_id,
            detail=f"phone={phone}",
        )
    except Exception as exc:
        _log.error("buy_logout_cb log_action: %s", exc, exc_info=True)

    await _edit_order_message(cq.message, _order_text_loggedout(state))
    _BUY_DELIVERED.pop(order_id, None)


# ── Market Root ───────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^show_market$"))
async def show_market_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        balance = await get_balance(user_id)
        text = (
            f"{_DIV}\n"
            f"![🛒](tg://emoji?id=6131886699254388574) **{s.get('market_title', 'MARKETPLACE')}**\n"
            f"{_DIV}\n\n"
            f"![💰](tg://emoji?id=6129731974291527294) **{s.get('market_balance_lbl', 'Your Balance')}:** `${balance:.2f}`\n\n"
            f"{s.get('market_tagline', 'Buy and sell verified Telegram accounts with instant OTP delivery.')}\n\n"
            "Choose an action below 👇"
        )
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(s.get("btn_buy_account", "![🟢](tg://emoji?id=6129492160497589882) Buy Account"), callback_data="buy_page_1"),
                InlineKeyboardButton(s.get("btn_sell_account", "![🔴](tg://emoji?id=6129846551134084367) Sell Account"), callback_data="sell_page_1"),
            ],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("show_market_cb: %s", e, exc_info=True)


# ── BUY — Country Grid ────────────────────────────────────────────────────────

async def _build_buy_grid(page: int, s: dict, mode: str = "account") -> tuple[int, int, int, list]:
    from server.utils.database.sessiondb import get_unsold_counts_all_countries

    sel_prefix = "buy_session_sel_" if mode == "session" else "buy_sel_"
    page_prefix = "buy_session_page_" if mode == "session" else "buy_page_"

    countries = await get_all_countries(buy_only=True)
    session_counts = await get_unsold_counts_all_countries()
    available = []
    for c in countries:
        real_stock = session_counts.get(c["code"], 0)
        stock = displayed_stock(c, real_stock)
        if stock > 0:
            c["_total_stock"] = stock
            available.append(c)

    total = len(available)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    page_items = available[start:start + PAGE_SIZE]

    rows = []
    for i in range(0, len(page_items), 2):
        row = []
        for c in page_items[i:i + 2]:
            row.append(InlineKeyboardButton(
                _country_label(c),
                callback_data=f"{sel_prefix}{c['code']}_{page}",
            ))
        rows.append(row)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(s.get("btn_prev", "![⬅️](tg://emoji?id=6129550284290006595) Prev"), callback_data=f"{page_prefix}{page - 1}"))
    nav.append(InlineKeyboardButton(f"![📄](tg://emoji?id=6129579803600231171) {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(s.get("btn_next", "Next ![➡️](tg://emoji?id=6129792056589031358)"), callback_data=f"{page_prefix}{page + 1}"))
    if nav:
        rows.append(nav)

    search_btn = InlineKeyboardButton(
        s.get("btn_search_country", "![🔍](tg://emoji?id=5316977222467206948) Search"),
        callback_data=("buy_search_sess" if mode == "session" else "buy_search_acc"),
    )
    if mode != "session":
        rows.append([
            InlineKeyboardButton(s.get("btn_view_all", "![📋](tg://emoji?id=6129579803600231171) View All"), callback_data="buy_view_all"),
            search_btn,
        ])
    else:
        rows.append([search_btn])
    rows.append([InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")])
    return page, total_pages, total, rows


@bot.on_callback_query(filters.regex(r"^(?:buy|buy_session)_page_(\d+)$"))
async def buy_page_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        mode = "session" if cq.data.startswith("buy_session_page_") else "account"
        page = int(cq.matches[0].group(1))
        balance = await get_balance(user_id)
        page, total_pages, total, rows = await _build_buy_grid(page, s, mode=mode)

        title = s.get("buy_session_title", "BUY SESSION") if mode == "session" else s.get("buy_title", "BUY ACCOUNT")

        if total == 0:
            text = (
                f"{_DIV}\n"
                f"![🛒](tg://emoji?id=6131886699254388574) **{title}**\n"
                f"{_DIV}\n\n"
                f"![💰](tg://emoji?id=6129731974291527294) **{s.get('market_balance_lbl', 'Your Balance')}:** `${balance:.2f}`\n\n"
                f"{s.get('buy_no_stock', '![🚫](tg://emoji?id=6129846551134084367) No countries available right now. Check back later!')}"
            )
            buttons = _back_market_btn(s)
        else:
            page_info = s.get("buy_page_info", "Page {0}/{1} — {2} countries").format(page, total_pages, total)
            hint = (
                s.get("buy_session_hint", "Click a country to view details 👇")
                if mode == "session"
                else s.get("buy_hint", "Click a country to view details 👇")
            )
            text = (
                f"{_DIV}\n"
                f"![🛒](tg://emoji?id=6131886699254388574) **{title}**\n"
                f"{_DIV}\n\n"
                f"![💰](tg://emoji?id=6129731974291527294) **{s.get('market_balance_lbl', 'Your Balance')}:** `${balance:.2f}`\n"
                f"![📄](tg://emoji?id=6129579803600231171) {page_info}\n\n"
                f"{hint}\n"
                f"{_DIV}"
            )
            buttons = InlineKeyboardMarkup(rows)

        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("buy_page_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^buy_view_all$"))
async def buy_view_all_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        from server.utils.database.sessiondb import get_unsold_counts_all_countries
        countries = await get_all_countries(buy_only=True)
        session_counts = await get_unsold_counts_all_countries()

        entries = []
        for c in countries:
            real_stock = session_counts.get(c["code"], 0)
            stock = displayed_stock(c, real_stock)
            if stock > 0:
                entries.append((c, stock))

        title = s.get("buy_view_all_title", "All Available Countries")
        if not entries:
            html_text = _md_emoji_to_html(f"<b>![📋](tg://emoji?id=6129579803600231171) {_he(title)}</b>\n{_he(_DIV)}\n\n{_he(s.get('buy_view_all_empty', '![🚫](tg://emoji?id=6129846551134084367) No countries available right now.'))}")
        else:
            CHUNK = 20
            parts = [_md_emoji_to_html(f"<b>![📋](tg://emoji?id=6129579803600231171) {_he(title)}</b>\n{_he(_DIV)}\n")]
            for i in range(0, len(entries), CHUNK):
                chunk_lines = []
                for c, total_stock in entries[i:i + CHUNK]:
                    name = _he(c.get("country_name", c["code"]))
                    code = _he(c["code"])
                    price = c.get("price", 0)
                    chunk_lines.append(
                        f"<b>{name}</b> <code>{code}</code> — <code>${price:g}</code> | {total_stock}"
                    )
                parts.append("<blockquote>" + "\n".join(chunk_lines) + "</blockquote>")
            html_text = "\n".join(parts)

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="buy_page_1")],
        ])
        await _safe_edit(cq, html_text, buttons, parse_mode=enums.ParseMode.HTML)
        await cq.answer()
    except Exception as e:
        _log.error("buy_view_all_cb: %s", e, exc_info=True)


# ── BUY — Search country by name / ISO / dial code ───────────────────────────

_BUY_SEARCH_PROMPT = (
    "![🔍](tg://emoji?id=5316977222467206948) **Search Country**\n\n"
    "Send the country **name**, **ISO code**, or **dial code**.\n\n"
    "Examples: `USA`, `US`, `+1`, `India`, `IN`, `91`\n\n"
    "Type /cancel to abort."
)


@bot.on_callback_query(filters.regex(r"^buy_search_(acc|sess)$"))
async def buy_search_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        mode = "session" if cq.matches[0].group(1) == "sess" else "account"
        chat_id = cq.message.chat.id
        try:
            await cq.message.delete()
        except Exception:
            pass
        prompt = await client.send_message(chat_id, _BUY_SEARCH_PROMPT)
        _BUY_SEARCH[user_id] = {
            "mode": mode,
            "chat_id": chat_id,
            "prompt_msg_id": prompt.id,
        }
        await cq.answer()
    except Exception as e:
        _log.error("buy_search_cb: %s", e, exc_info=True)


def _match_country_search(query: str, countries: list) -> dict | None:
    q = (query or "").strip()
    if not q:
        return None
    q_up = q.upper().lstrip("+").replace(" ", "")
    for c in countries:
        if (c.get("code") or "").upper() == q_up:
            return c
    for c in countries:
        if (c.get("idc") or "").lstrip("+") == q_up:
            return c
    for c in countries:
        if (c.get("country_name") or "").upper() == q.upper():
            return c
    for c in countries:
        name = (c.get("country_name") or "").upper()
        if name and q.upper() in name:
            return c
    return None


@bot.on_message(filters.private & filters.text & ~filters.command(["start", "cancel", "help"]))
async def buy_search_text_handler(client, message: Message):
    user_id = message.from_user.id
    ctx = _BUY_SEARCH.get(user_id)
    if not ctx:
        await message.continue_propagation()
        return

    from server.utils.database.sessiondb import get_unsold_counts_all_countries

    mode = ctx.get("mode", "account")
    lang = await get_user_lang(user_id)
    s = get_string(lang)

    query = (message.text or "").strip()
    countries = await get_all_countries(buy_only=True)
    session_counts = await get_unsold_counts_all_countries()

    available = []
    for c in countries:
        real_stock = session_counts.get(c["code"], 0)
        stock = displayed_stock(c, real_stock)
        if stock > 0 and not c.get("temp_disable"):
            available.append(c)

    match = _match_country_search(query, available)

    try:
        await client.delete_messages(ctx["chat_id"], ctx["prompt_msg_id"])
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass
    _BUY_SEARCH.pop(user_id, None)

    back_cb = "buy_session_page_1" if mode == "session" else "buy_page_1"
    search_cb = "buy_search_sess" if mode == "session" else "buy_search_acc"

    if not match:
        text = (
            "![❌](tg://emoji?id=6129846551134084367) **No match found.**\n\n"
            f"No country matching `{_he(query)[:64]}` is currently in stock."
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_search_again", "![🔍](tg://emoji?id=5316977222467206948) Search Again"), callback_data=search_cb)],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=back_cb)],
        ])
        await client.send_message(ctx["chat_id"], text, reply_markup=buttons)
        return

    code = match["code"]
    country_name = match.get("country_name", code)
    sel_cb = f"buy_session_sel_{code}_1" if mode == "session" else f"buy_sel_{code}_1"
    text = (
        f"![✅](tg://emoji?id=6129492160497589882) **Found:** {_flag(code)} {country_name}\n\n"
        "Tap **Open** to view details."
    )
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(s.get("btn_open", "![➡️](tg://emoji?id=6129792056589031358) Open"), callback_data=sel_cb)],
        [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=back_cb)],
    ])
    await client.send_message(ctx["chat_id"], text, reply_markup=buttons)


@bot.on_callback_query(filters.regex(r"^buy_sel_([A-Za-z]{2,3})_(\d+)$"))
async def buy_select_country_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        code = cq.matches[0].group(1).upper()
        page = int(cq.matches[0].group(2))

        from server.utils.database.sessiondb import count_unsold_by_country

        country = await get_country(code)
        if not country:
            await cq.answer(s.get("err_country_not_found", "![❌](tg://emoji?id=6129846551134084367) Country not found."), show_alert=True)
            return
        if country.get("temp_disable"):
            await cq.answer(s.get("sell_country_full", "![🚫](tg://emoji?id=6129846551134084367) Temporarily unavailable."), show_alert=True)
            return

        real_stock = await count_unsold_by_country(code)
        stock = displayed_stock(country, real_stock)
        idc = country.get("idc", "").lstrip("+")
        idc_str = f"+{idc}" if idc else "N/A"
        country_name = country.get("country_name", code)

        text = (
            f"{_DIV}\n"
            f"![🛒](tg://emoji?id=6131886699254388574) **{s.get('buy_country_title', 'BUY ACCOUNT — {0}').format(country_name.upper())}**\n"
            f"{_DIV}\n\n"
            f"{_flag(code)} **{s.get('buy_country_lbl', 'Country')}:** {country_name}\n"
            f"![📞](tg://emoji?id=5330237710655306682) **{s.get('buy_phone_code_lbl', 'Phone Code')}:** `{idc_str}`\n"
            f"![💵](tg://emoji?id=6129731974291527294) **{s.get('buy_price_lbl', 'Price')}:** `${country.get('price', 0):g}`\n"
            f"![📦](tg://emoji?id=6131886699254388574) **{s.get('buy_stock_lbl', 'Available')}:** `{stock}`\n"
            f"![⚡](tg://emoji?id=6129792056589031358) **{s.get('buy_delivery_lbl', 'Delivery')}:** {s.get('buy_delivery_val', 'Instant OTP')}\n\n"
            f"{_DIV}\n"
            f"{s.get('buy_confirm_hint', 'Press **Buy Now** to proceed. Your balance will be deducted upon confirmation.')}"
        )
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=f"buy_page_{page}"),
                InlineKeyboardButton(s.get("btn_buy_now", "![✅](tg://emoji?id=6129492160497589882) Buy Now"), callback_data=f"buy_confirm_{code}_{page}"),
            ],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("buy_select_country_cb: %s", e, exc_info=True)


# ── BUY — Confirm: reserve account, deduct balance, start OTP flow ────────────

@bot.on_callback_query(filters.regex(r"^buy_confirm_([A-Za-z]{2,3})_(\d+)$"))
async def buy_confirm_cb(client, cq: CallbackQuery):
    user_id = cq.from_user.id
    lang = await get_user_lang(user_id)
    s = get_string(lang)

    # Block if another buy auth is already in progress for this user
    if user_id in _BUY_AUTH:
        await cq.answer(
            s.get("buy_auth_already_active", "![⏳](tg://emoji?id=6129574787078429498) You already have an order in progress. Please wait for it to finish."),
            show_alert=True,
        )
        return

    code = cq.matches[0].group(1).upper()
    page = int(cq.matches[0].group(2))
    order_id = None

    try:
        import config as _cfg
        from server.services.inventory_service import reserve_valid_session
        from server.utils.database.sessiondb import revert_session_sold

        country = await get_country(code)
        if not country:
            await cq.answer(s.get("err_country_not_found", "![❌](tg://emoji?id=6129846551134084367) Country not found."), show_alert=True)
            return

        price = country.get("price", 0)
        balance = await get_balance(user_id)
        if balance < price:
            await cq.answer(
                s.get("wallet_insufficient", "![❌](tg://emoji?id=6129846551134084367) Insufficient balance.\n\nYour balance: ${0}\nRequired: ${1}").format(
                    f"{balance:.2f}", f"{price:g}"
                ),
                show_alert=True,
            )
            return

        # ── Reserve and live-validate account from stock ───────────────────────
        reservation = await reserve_valid_session(
            bot_client=client,
            user_id=user_id,
            country_code=code,
            max_attempts=25,
        )
        if not reservation.account:
            if reservation.retryable:
                await cq.answer(
                    "Session validation is temporarily unavailable. Please try again shortly.",
                    show_alert=True,
                )
            else:
                await cq.answer(
                    s.get("buy_no_clean_account", "![⚠️](tg://emoji?id=6129939837823753679) No valid account available right now."),
                    show_alert=True,
                )
            return
        session_account = reservation.account

        # ── Deduct balance and create order ───────────────────────────────────
        try:
            result = await buy_from_server(
                user_id, code,
                account_id_override=session_account["account_id"],
            )
        except MarketError as me:
            await revert_session_sold(session_account["account_id"])
            await cq.answer(f"![❌](tg://emoji?id=6129846551134084367) {me.message}", show_alert=True)
            return

        order_id = result["order_id"]
        phone = session_account["phone"]

        # ── Must have a stored session — that's what auto-captures the OTP ─────
        if not session_account.get("session_msg_id") or not session_account.get("session_chat_id"):
            temp_state = {
                "account_id": session_account["account_id"],
                "order_id": order_id,
                "price": price,
            }
            await _refund_buy_auth(user_id, temp_state)
            await cq.answer(
                s.get("buy_session_no_source", "![⚠️](tg://emoji?id=6129939837823753679) Stored session file is missing — refunded."),
                show_alert=True,
            )
            return

        # ── Acknowledge so Telegram doesn't timeout the callback ───────────────
        await cq.answer(s.get("buy_otp_requesting", "📨 Preparing your account…"))

        stored_tfa_password = (
            session_account.get("tfa_password_enc", "")
            or session_account.get("password", "")
        )

        # ── Connect the OLD authorized session ─────────────────────────────────
        # This stays connected and listens for Telegram's login-code message
        # once the BUYER enters this phone number into their own Telegram app
        # — the bot never signs in on its own behalf.
        from server.utils.sessions.channel_storage import download_session_from_channel
        from server.stock.otp_capture import start_login_otp_listener

        old_tmp_path = new_temp_session_path()
        old_client = None
        try:
            old_session_bytes = await download_session_from_channel(
                client, session_account["session_chat_id"], session_account["session_msg_id"],
                account_id=session_account.get("account_id"), phone=phone,
            )
            await write_session_bytes(old_tmp_path, old_session_bytes)
            old_client, _proxy_id, _proxy_doc_used = await connect_with_proxy_fallback(
                old_tmp_path[:-8], _cfg.API_ID, _cfg.API_HASH, code,
            )
            if not await old_client.is_user_authorized():
                raise RuntimeError("stored session not authorized")
        except Exception as exc:
            storage_code = getattr(exc, "code", "UNKNOWN_STORAGE_ERROR")
            storage_context = getattr(exc, "context", {})
            _log.error(
                "buy_confirm_cb storage/session preparation failed account=%s phone=%s "
                "country=%s code=%s context=%s error=%s",
                session_account.get("account_id"), phone, code,
                storage_code, storage_context, exc,
            )
            if old_client:
                try:
                    await old_client.disconnect()
                except Exception:
                    pass
            await cleanup_session_files(old_tmp_path)
            temp_state = {
                "account_id": session_account["account_id"],
                "order_id": order_id,
                "price": price,
            }
            await _refund_buy_auth(user_id, temp_state)
            await _safe_edit(
                cq,
                s.get("buy_session_no_source", "![⚠️](tg://emoji?id=6129939837823753679) Stored session file is invalid — refunded."),
                _back_market_btn(s),
            )
            return

        # ── Live spam check before handing the account over ─────────────────────
        from server.stock.spam_check import check_spam_status
        try:
            spam_status = await check_spam_status(old_client)
        except Exception as exc:
            _log.warning("buy_confirm_cb spam check failed for %s: %s", phone, exc)
            spam_status = "unknown"

        # Check per-spam-status buy toggle (admin-configurable)
        _BUY_TOGGLE = {
            "clean":          "buy_enabled_clean",
            "temporary_spam": "buy_enabled_temp_spam",
            "permanent_spam": "buy_enabled_perm_spam",
            "frozen":         "buy_enabled_frozen",
            "unknown":        "buy_enabled_unknown",
        }
        _buy_key = _BUY_TOGGLE.get(spam_status)
        _buy_blocked = False
        if _buy_key is not None:
            from server.utils.database.configdb import get_setting as _gs
            _buy_blocked = not (await _gs(_buy_key))

        if _buy_blocked:
            try:
                await old_client.disconnect()
            except Exception:
                pass
            await cleanup_session_files(old_tmp_path)
            temp_state = {
                "account_id": session_account["account_id"],
                "order_id": order_id,
                "price": price,
            }
            await _refund_buy_auth(user_id, temp_state)
            await _safe_edit(
                cq,
                s.get("buy_account_banned", "![❌](tg://emoji?id=6129846551134084367) This account has been banned by Telegram. Your purchase has been refunded."),
                _back_market_btn(s),
            )
            return

        otp_future = start_login_otp_listener(old_client)

        idc = country.get("idc", "").lstrip("+")
        dial_code = f"+{idc}" if idc else "N/A"

        # ── Store handover state — the buyer logs in themselves from here ──────
        _BUY_AUTH[user_id] = {
            "user_id": user_id,
            "phone": phone,
            "account_id": session_account["account_id"],
            "order_id": order_id,
            "country_code": code,
            "country_name": country.get("country_name", code),
            "dial_code": dial_code,
            "price": price,
            "old_client": old_client,
            "old_tmp_path": old_tmp_path,
            "lang": lang,
            "tfa_password": stored_tfa_password,
            "otp_refresh_in_progress": False,
        }
        state = _BUY_AUTH[user_id]

        spam_note = (
            f"\n\n![⚠️](tg://emoji?id=6129939837823753679) _{s.get('sell_spam_flag_note', 'This account has a temporary spam restriction. It will clear in a few days.')}_"
            if spam_status == "temporary_spam" else ""
        )
        handover_text = _order_text_waiting(state) + spam_note

        try:
            await _safe_edit(cq, handover_text, None)
        except Exception:
            # The order lock is already held — don't leave it stuck just
            # because the message edit itself failed; the background task
            # below will still try to edit it again once the OTP arrives.
            pass

        asyncio.create_task(_run_buy_account_handover(client, cq.message, user_id, otp_future))

    except Exception as e:
        _log.error("buy_confirm_cb: %s", e, exc_info=True)
        # If we got far enough to reserve a "purchase in progress" lock for
        # this user but crashed before the background handover task could
        # take over, that lock would otherwise never clear — auto-refund and
        # release it so the buyer isn't permanently blocked from buying again.
        stuck_state = _BUY_AUTH.get(user_id)
        if stuck_state is not None and order_id is not None and stuck_state.get("order_id") == order_id:
            try:
                await _refund_buy_auth(user_id, stuck_state)
            except Exception as exc2:
                _log.error("buy_confirm_cb stuck-state cleanup failed for %s: %s", user_id, exc2, exc_info=True)


# ── BUY SESSION — country select, then ask quantity ───────────────────────────

@bot.on_callback_query(filters.regex(r"^buy_session_sel_([A-Za-z]{2,3})_(\d+)$"))
async def buy_session_select_country_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        code = cq.matches[0].group(1).upper()
        page = int(cq.matches[0].group(2))

        from server.utils.database.sessiondb import count_unsold_by_country

        country = await get_country(code)
        if not country:
            await cq.answer(s.get("err_country_not_found", "![❌](tg://emoji?id=6129846551134084367) Country not found."), show_alert=True)
            return
        if country.get("temp_disable"):
            await cq.answer(s.get("sell_country_full", "![🚫](tg://emoji?id=6129846551134084367) Temporarily unavailable."), show_alert=True)
            return

        real_stock = await count_unsold_by_country(code)
        display_stock = displayed_stock(country, real_stock)

        text, buttons = _build_qty_keypad_view(
            country=country,
            code=code,
            page=page,
            entered="",
            display_stock=display_stock,
            s=s,
        )
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("buy_session_select_country_cb: %s", e, exc_info=True)


# ── BUY SESSION — calculator-style quantity keypad (UI only) ──────────────────
#
# Purely a presentation layer: the BUY key simply fires the existing
# `buy_session_qty_<code>_<page>_<qty>` callback, so the checkout → confirm →
# purchase backend flow below is untouched.

def _build_qty_keypad_view(*, country: dict, code: str, page: int, entered: str,
                           display_stock: int, s: dict):
    """Return (text, InlineKeyboardMarkup) for the quantity keypad screen."""
    idc = str(country.get("idc", "") or "").lstrip("+")
    idc_str = f"+{idc}" if idc else "N/A"
    country_name = country.get("country_name", code)
    price = float(country.get("price", 0) or 0)

    qty = int(entered) if entered.isdigit() else 0
    total = round(price * qty, 4)

    text = (
        f"{_DIV}\n"
        f"![🛒](tg://emoji?id=6131886699254388574) **{s.get('buy_session_country_title', 'BUY SESSION — {0}').format(str(country_name).upper())}**\n"
        f"{_DIV}\n\n"
        f"{_flag(code)} **{s.get('buy_country_lbl', 'Country')}:** {country_name}\n"
        f"![📞](tg://emoji?id=5330237710655306682) **{s.get('buy_phone_code_lbl', 'Key Code')}:** `{idc_str}`\n"
        f"![📦](tg://emoji?id=6131886699254388574) **{s.get('buy_stock_lbl', 'Available')}:** `{display_stock}`\n"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('buy_price_lbl', 'Price')}:** `${price:g}` {s.get('buy_session_per_unit', 'each')}\n\n"
        f"{_DIV}\n"
        f"`  SES {qty}   |   {total:.2f} USD  `\n"
        f"{_DIV}\n"
        f"{s.get('buy_session_qty_hint', 'Enter quantity using the keypad below.')}"
    )

    if display_stock <= 0:
        rows = [[InlineKeyboardButton(
            s.get("buy_session_out_of_stock_btn", "Out of stock"),
            callback_data=f"buy_session_page_{page}",
        )]]
        rows.append([InlineKeyboardButton(
            s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"),
            callback_data=f"buy_session_page_{page}",
        )])
        return text, InlineKeyboardMarkup(rows)

    def key(label: str, action: str):
        return InlineKeyboardButton(
            label, callback_data=f"bsk_{code}_{page}_{entered}_{action}"
        )

    rows = [
        [key("1", "1"), key("2", "2"), key("3", "3")],
        [key("4", "4"), key("5", "5"), key("6", "6")],
        [key("7", "7"), key("8", "8"), key("9", "9")],
        [
            InlineKeyboardButton(
                s.get("buy_session_buy_btn", "BUY"),
                callback_data=f"buy_session_qty_{code}_{page}_{qty}",
            ),
            key("0", "0"),
            key("⌫", "bk"),
        ],
        [InlineKeyboardButton(
            s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"),
            callback_data=f"buy_session_page_{page}",
        )],
    ]
    return text, InlineKeyboardMarkup(rows)


@bot.on_callback_query(filters.regex(r"^bsk_([A-Za-z]{2,3})_(\d+)_(\d*)_(\d|bk)$"))
async def buy_session_keypad_cb(client, cq: CallbackQuery):
    """Keypad digit / backspace press — re-renders the same screen."""
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        code    = cq.matches[0].group(1).upper()
        page    = int(cq.matches[0].group(2))
        entered = cq.matches[0].group(3) or ""
        action  = cq.matches[0].group(4)

        from server.utils.database.sessiondb import count_unsold_by_country

        country = await get_country(code)
        if not country or country.get("temp_disable"):
            await cq.answer(
                s.get("sell_country_full", "![🚫](tg://emoji?id=6129846551134084367) Temporarily unavailable."),
                show_alert=True,
            )
            return

        real_stock = await count_unsold_by_country(code)
        display_stock = displayed_stock(country, real_stock)

        if action == "bk":
            new_entered = entered[:-1]
        else:
            candidate = (entered + action).lstrip("0")
            if not candidate:
                new_entered = ""
            elif len(candidate) > 6:
                await cq.answer(
                    s.get("buy_session_qty_too_big", "Quantity is too large."),
                    show_alert=True,
                )
                return
            elif int(candidate) > max(display_stock, 0):
                await cq.answer(
                    s.get(
                        "buy_session_qty_over_stock",
                        "![⚠️](tg://emoji?id=6129939837823753679) Only {0} session(s) available.",
                    ).format(display_stock),
                    show_alert=True,
                )
                return
            else:
                new_entered = candidate

        if new_entered == entered:
            await cq.answer()
            return

        text, buttons = _build_qty_keypad_view(
            country=country,
            code=code,
            page=page,
            entered=new_entered,
            display_stock=display_stock,
            s=s,
        )
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as exc:
        _log.error("buy_session_keypad_cb: %s", exc, exc_info=True)
        try:
            await cq.answer()
        except Exception:
            pass


def _progress_block(stage: str, filled: int, total: int = 10) -> str:
    """Render a simple animated-looking progress bar block (UI only)."""
    filled = max(0, min(filled, total))
    bar = "█" * filled + "░" * (total - filled)
    pct = int(filled * 100 / total)
    return (
        f"{_DIV}\n"
        f"![⏳](tg://emoji?id=6129574787078429498) **PROCESSING…**\n"
        f"{_DIV}\n\n"
        f"{stage}\n"
        f"`{bar}` `{pct}%`"
    )


def _build_delivery_json(
    *,
    phone_digits: str,
    country: dict,
    fresh_info: dict,
    tfa_password: str,
    api_id_used: int,
    register_time: int,
) -> dict:
    """
    Build the buyer-facing `<phone>.json` metadata document delivered
    alongside the fresh `.session` file for a "Buy Session" purchase.
    Field set/shape is fixed by product spec — do not rename keys.
    """
    import secrets as _secrets
    import time as _time

    tz_offset = int(country.get("tz_offset", 0) or 0)

    return {
        "session_file":     phone_digits,
        "phone":             phone_digits,
        "register_time":     register_time,
        "app_id":            api_id_used,
        "app_hash":          config.API_HASH,
        "sdk":               "SDK 33",
        "app_version":       "12.8.1 (69162)",
        "device":            "samsungSM-G970F",
        "last_check_time":   _time.time(),
        "first_name":        fresh_info.get("first_name") or "",
        "last_name":         "",
        "sex":               "1",
        "lang_pack":         "android",
        "system_lang_pack":  "en-gb",
        "twoFA":             tfa_password or "",
        "device_token":      _secrets.token_urlsafe(42),
        "device_secret":     _secrets.token_urlsafe(150),
        "perf_cat":          2,
        "tz_offset":         tz_offset,
        "id":                fresh_info.get("user_id"),
        "trust":             False,
        "premium":           bool(fresh_info.get("premium", False)),
    }


async def _deliver_one_session(client, user_id: int, code: str, s: dict, country: "dict | None" = None) -> dict:
    """
    Reserve + pay for ONE session account, run a live spam check, and
    generate a brand-new fresh session (auto OTP capture — no buyer
    interaction). Does NOT send anything itself — the caller batches every
    successful unit's session bytes + json metadata into a single combined
    zip and sends that once, so a multi-quantity buy delivers ONE file.

    Returns on success:
      {"ok": True, "phone": ..., "phone_digits": ..., "fresh_bytes": ...,
       "json_data": ..., "spam_status": ...}
    Returns on failure:
      {"ok": False, "reason": "<user-facing text>"}
    On any failure after balance deduction, the unit is refunded.
    """
    import time as _time

    import config as _cfg
    from server.utils.database.sessiondb import (
        get_unsold_session_for_country,
        mark_session_sold,
        revert_session_sold,
    )
    from server.utils.sessions.channel_storage import download_session_from_channel
    from server.stock.session_generation import generate_fresh_session
    from server.stock.spam_check import verify_spam_status_live

    session_account = await get_unsold_session_for_country(code)
    if not session_account:
        return {"ok": False, "reason": s.get("buy_no_clean_account", "![⚠️](tg://emoji?id=6129939837823753679) No account available right now.")}
    if not await mark_session_sold(session_account["account_id"], user_id):
        return {"ok": False, "reason": s.get("buy_no_clean_account", "![⚠️](tg://emoji?id=6129939837823753679) No account available right now.")}

    account_id = session_account["account_id"]

    try:
        result = await buy_from_server(user_id, code, account_id_override=account_id)
    except MarketError as me:
        await revert_session_sold(account_id)
        return {"ok": False, "reason": f"![❌](tg://emoji?id=6129846551134084367) {me.message}"}

    phone = session_account["phone"]

    price_paid = result["price_paid"]

    async def _refund_unit(reason: str) -> dict:
        # Restore account to unsold inventory FIRST — before any balance ops
        # that might fail — so the account is never permanently lost from stock.
        try:
            await revert_session_sold(account_id)
        except Exception as exc:
            _log.error("_deliver_one_session revert_session_sold(%s): %s", account_id, exc)
        try:
            await update_balance(user_id, price_paid)
            await log_transaction(
                user_id, "refund", price_paid,
                ref_id=result["order_id"], note=f"Buy-session refund ({phone}): {reason}",
            )
        except Exception as exc:
            _log.error("_deliver_one_session refund failed for %s: %s", phone, exc)
        try:
            from server.utils.database.orderdb import refund_order
            await refund_order(result["order_id"], reason)
        except Exception:
            pass
        return {"ok": False, "reason": reason}

    try:
        if not session_account.get("session_msg_id") or not session_account.get("session_chat_id"):
            return await _refund_unit(s.get("buy_session_no_source", "![⚠️](tg://emoji?id=6129939837823753679) Stored session file is missing — refunded."))

        old_session_bytes = await download_session_from_channel(
            client, session_account["session_chat_id"], session_account["session_msg_id"],
            account_id=session_account.get("account_id"), phone=phone,
        )
    except Exception as exc:
        _log.error(
            "_deliver_one_session storage failure account=%s phone=%s country=%s code=%s context=%s error=%s",
            session_account.get("account_id"), phone, code,
            getattr(exc, "code", "UNKNOWN_STORAGE_ERROR"),
            getattr(exc, "context", {}), exc,
        )
        return await _refund_unit(s.get("buy_session_no_source", "![⚠️](tg://emoji?id=6129939837823753679) Stored session is unavailable — refunded."))

    tfa_password = session_account.get("tfa_password_enc", "") or session_account.get("password", "")

    fresh = await generate_fresh_session(
        old_session_bytes, _cfg.API_ID, _cfg.API_HASH, code,
        tfa_password=tfa_password, otp_timeout=90,
        terminate_others=True,
    )
    if not fresh.get("success"):
        _log.warning("_deliver_one_session fresh-session failed for %s: %s", phone, fresh.get("error"))
        flood_wait_seconds = fresh.get("flood_wait_seconds")
        if flood_wait_seconds:
            flood_msg = s.get("buy_session_flood_wait", "![⏳](tg://emoji?id=6129574787078429498) Telegram rate limit hit — refunded.")
            try:
                flood_msg = flood_msg.format(flood_wait_seconds)
            except (IndexError, KeyError):
                pass
            refunded = await _refund_unit(flood_msg)
            refunded["flood_wait_seconds"] = flood_wait_seconds
            return refunded
        return await _refund_unit(
            s.get("buy_session_gen_failed", "![⚠️](tg://emoji?id=6129939837823753679) Could not generate a fresh session for this account — refunded.")
        )

    fresh_bytes = fresh["fresh_session_bytes"]

    spam_status = await verify_spam_status_live(fresh_bytes, _cfg.API_ID, _cfg.API_HASH, code)
    if spam_status not in ("clean", "temporary_spam"):
        return await _refund_unit(
            s.get("buy_session_spam_flagged", "![⚠️](tg://emoji?id=6129939837823753679) This account is currently restricted by Telegram — refunded.")
        )

    phone_digits = phone.lstrip("+")
    login_time = session_account.get("login_time")
    register_time = int(login_time.timestamp()) if hasattr(login_time, "timestamp") else int(_time.time())

    # Use the pre-fetched country if the caller supplied it — saves one DB round-trip
    # per unit on multi-unit purchases; fall back to fetching if not provided.
    _country_info = country if country is not None else (await get_country(code) or {})
    json_data = _build_delivery_json(
        phone_digits=phone_digits,
        country=_country_info,
        fresh_info=fresh,
        tfa_password=tfa_password,
        api_id_used=session_account.get("api_id_used") or _cfg.API_ID,
        register_time=register_time,
    )

    return {
        "ok":           True,
        "phone":        phone,
        "phone_digits": phone_digits,
        "fresh_bytes":  fresh_bytes,
        "json_data":    json_data,
        "spam_status":  spam_status,
    }


@bot.on_callback_query(filters.regex(r"^buy_session_qty_legacy_([A-Za-z]{2,3})_(\d+)_(\d+)$"))
async def _legacy_buy_session_qty_cb(client, cq: CallbackQuery):
    user_id = cq.from_user.id
    lang = await get_user_lang(user_id)
    s = get_string(lang)

    if _BUY_SESSION_PENDING.get(user_id):
        await cq.answer(
            s.get("buy_session_already_active", "![⏳](tg://emoji?id=6129574787078429498) A previous session purchase is still processing. Please wait."),
            show_alert=True,
        )
        return

    # Acquire per-user lock BEFORE the first await so two rapid taps cannot
    # both slip past the check above while an await is in progress (TOCTOU).
    # The finally block always clears it regardless of how the try exits.
    _BUY_SESSION_PENDING[user_id] = True

    code = cq.matches[0].group(1).upper()
    page = int(cq.matches[0].group(2))
    qty = int(cq.matches[0].group(3))

    try:
        country = await get_country(code)
        if not country:
            await cq.answer(s.get("err_country_not_found", "![❌](tg://emoji?id=6129846551134084367) Country not found."), show_alert=True)
            return
        if country.get("temp_disable"):
            await cq.answer(s.get("sell_country_full", "![🚫](tg://emoji?id=6129846551134084367) Temporarily unavailable."), show_alert=True)
            return

        price = country.get("price", 0)
        total_cost = price * qty
        balance = await get_balance(user_id)
        if balance < total_cost:
            await cq.answer(
                s.get("wallet_insufficient", "![❌](tg://emoji?id=6129846551134084367) Insufficient balance.\n\nYour balance: ${0}\nRequired: ${1}").format(
                    f"{balance:.2f}", f"{total_cost:g}"
                ),
                show_alert=True,
            )
            return

        await cq.answer(s.get("buy_session_processing", "![⏳](tg://emoji?id=6129574787078429498) Processing your purchase…"))
        await _safe_edit(
            cq,
            f"{_DIV}\n"
            f"![⏳](tg://emoji?id=6129574787078429498) **{s.get('buy_session_processing_title', 'PROCESSING…')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('buy_session_processing_body', 'Generating fresh sessions and checking spam status for {0} account(s). This can take a minute…').format(qty)}",
            None,
        )

        delivered_items, failed_reasons = [], []
        flood_wait_hit = None
        for _ in range(qty):
            # Pass the already-fetched country dict to avoid a redundant DB call per unit.
            outcome = await _deliver_one_session(client, user_id, code, s, country=country)
            if outcome["ok"]:
                delivered_items.append(outcome)
            else:
                failed_reasons.append(outcome["reason"])
                # Telegram rate-limited a login attempt: every further unit in
                # this batch would hit the exact same limit and just waste the
                # remaining reservations on refunds. Stop the batch immediately
                # instead of looping through the rest of `qty`.
                if outcome.get("flood_wait_seconds"):
                    flood_wait_hit = outcome["flood_wait_seconds"]
                    break

        delivered = len(delivered_items)
        spam_flagged = sum(1 for it in delivered_items if it["spam_status"] == "temporary_spam")

        # ── Pack every delivered unit's .session + .json into ONE zip ──────────
        if delivered_items:
            import io
            import json as _json
            import zipfile

            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in delivered_items:
                    zf.writestr(f"{item['phone_digits']}.session", item["fresh_bytes"])
                    zf.writestr(f"{item['phone_digits']}.json", _json.dumps(item["json_data"], indent=2))
            zip_buf.seek(0)
            zip_buf.name = f"buy_session_{code}_{delivered}.zip"

            spam_note = (
                f"\n![⚠️](tg://emoji?id=6129939837823753679) {spam_flagged} {s.get('sell_spam_flag_note', 'account(s) have a temporary spam restriction.')}"
                if spam_flagged else ""
            )
            try:
                await client.send_document(
                    chat_id=user_id,
                    document=zip_buf,
                    file_name=zip_buf.name,
                    caption=(
                        f"![✅](tg://emoji?id=6129492160497589882) **{s.get('buy_session_delivered_title', 'Session(s) delivered')}**\n"
                        f"![📦](tg://emoji?id=6131886699254388574) `{delivered}` {s.get('buy_session_accounts_lbl', 'account(s)')}{spam_note}"
                    ),
                )
            except Exception as exc:
                _log.error("buy_session_qty_cb zip delivery failed: %s", exc)
                failed_reasons.append(s.get("buy_session_delivery_failed", "![⚠️](tg://emoji?id=6129939837823753679) Could not deliver the zip file."))

        balance_now = await get_balance(user_id)
        # Use ✅ if at least one unit was delivered, ❌ if all failed.
        done_icon = "![✅](tg://emoji?id=6129492160497589882)" if delivered > 0 else "![❌](tg://emoji?id=6129846551134084367)"
        done_title = (
            s.get("buy_session_done_title", "BUY SESSION COMPLETE")
            if delivered > 0
            else s.get("buy_session_failed_title", "PURCHASE FAILED")
        )
        lines = [
            _DIV,
            f"{done_icon} **{done_title}**",
            _DIV,
            "",
            f"![📦](tg://emoji?id=6131886699254388574) **{s.get('buy_session_delivered_lbl', 'Delivered')}:** `{delivered}/{qty}`",
            f"![💰](tg://emoji?id=6129731974291527294) **{s.get('market_balance_lbl', 'Your Balance')}:** `${balance_now:.2f}`",
        ]
        if failed_reasons:
            lines.append("")
            lines.append(f"![⚠️](tg://emoji?id=6129939837823753679) **{s.get('buy_session_issues_lbl', 'Issues')}:**")
            for reason in failed_reasons:
                lines.append(f"• {reason}")
        if flood_wait_hit:
            remaining_untried = qty - delivered - len(failed_reasons)
            lines.append("")
            lines.append(
                s.get(
                    "buy_session_flood_wait_batch_note",
                    "![⏳](tg://emoji?id=6129574787078429498) Telegram asked us to slow down. The remaining {0} unit(s) were not attempted and were "
                    "not charged — please try again in about {1} seconds.",
                ).format(remaining_untried, flood_wait_hit)
            )
        # Edit the original "Processing…" message in place — no extra message sent.
        # The zip file (if any) is already delivered above as a separate document.
        await _safe_edit(cq, "\n".join(lines), _back_market_btn(s))
    except Exception as e:
        _log.error("buy_session_qty_cb: %s", e, exc_info=True)
        try:
            await _safe_edit(
                cq,
                s.get("buy_error_generic", "![❌](tg://emoji?id=6129846551134084367) Something went wrong. Please try again or contact support."),
                _back_market_btn(s),
            )
        except Exception:
            pass
    finally:
        _BUY_SESSION_PENDING.pop(user_id, None)


@bot.on_callback_query(filters.regex(r"^buy_session_qty_([A-Za-z]{2,3})_(\d+)_(\d+)$"))
async def buy_session_qty_cb(client, cq: CallbackQuery):
    """Show a live checkout summary; the confirm callback performs the purchase."""
    user_id = cq.from_user.id
    lang = await get_user_lang(user_id)
    s = get_string(lang)
    code = cq.matches[0].group(1).upper()
    page = int(cq.matches[0].group(2))
    qty = int(cq.matches[0].group(3))

    try:
        from server.utils.database.sessiondb import count_unsold_by_country

        country = await get_country(code)
        if not country or country.get("temp_disable"):
            await cq.answer(
                s.get("buy_no_clean_account", "![⚠️](tg://emoji?id=6129939837823753679) No account available right now."),
                show_alert=True,
            )
            return
        live_stock = await count_unsold_by_country(code)
        price = round(float(country.get("price") or 0), 4)
        if qty < 1:
            await cq.answer("Quantity must be at least 1.", show_alert=True)
            return
        if qty > live_stock:
            await cq.answer(
                f"Only {min(live_stock, 100)} session(s) are available right now.",
                show_alert=True,
            )
            return
        total = round(price * qty, 4)
        balance = await get_balance(user_id)
        if balance < total:
            await cq.answer(
                s.get("wallet_insufficient", "![❌](tg://emoji?id=6129846551134084367) Insufficient balance.\n\nYour balance: ${0}\nRequired: ${1}").format(
                    f"{balance:.2f}", f"{total:.2f}"
                ),
                show_alert=True,
            )
            return

        text = (
            f"{_DIV}\n"
            f"![🛒](tg://emoji?id=6131886699254388574) **{s.get('buy_session_confirm_title', 'CONFIRM SESSION PURCHASE')}**\n"
            f"{_DIV}\n\n"
            f"{_flag(code)} **{s.get('buy_country_lbl', 'Country')}:** {country.get('country_name', code)}\n"
            f"![📦](tg://emoji?id=6131886699254388574) **Quantity:** `{qty}`\n"
            f"![💵](tg://emoji?id=6129731974291527294) **Price:** `${price:.2f}` each\n"
            f"![💰](tg://emoji?id=6129731974291527294) **Total:** `${total:.2f}`\n"
            f"![📦](tg://emoji?id=6131886699254388574) **Available now:** `{live_stock}`\n"
            f"![⚡](tg://emoji?id=6129792056589031358) **Delivery:** stored verified `.session` + matching `.json`\n\n"
            f"Your balance after purchase: `${balance - total:.2f}`"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                s.get("btn_confirm", "✅ Confirm"),
                callback_data=f"buy_session_confirm_{code}_{page}_{qty}",
            )],
            [InlineKeyboardButton(
                s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"),
                callback_data=f"buy_session_sel_{code}_{page}",
            )],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as exc:
        _log.error("buy_session_qty_cb: %s", exc, exc_info=True)
        await cq.answer(
            s.get("buy_error_generic", "![❌](tg://emoji?id=6129846551134084367) Something went wrong. Please try again."),
            show_alert=True,
        )


@bot.on_callback_query(filters.regex(r"^buy_session_confirm_([A-Za-z]{2,3})_(\d+)_(\d+)$"))
async def buy_session_confirm_cb(client, cq: CallbackQuery):
    """Reserve, pay, validate, and deliver one complete stored-session batch."""
    user_id = cq.from_user.id
    lang = await get_user_lang(user_id)
    s = get_string(lang)
    code = cq.matches[0].group(1).upper()
    page = int(cq.matches[0].group(2))
    qty = int(cq.matches[0].group(3))

    if _BUY_SESSION_PENDING.get(user_id):
        await cq.answer(
            s.get("buy_session_already_active", "![⏳](tg://emoji?id=6129574787078429498) A previous session purchase is still processing. Please wait."),
            show_alert=True,
        )
        return
    _BUY_SESSION_PENDING[user_id] = True
    purchase = None
    delivery_sent = False
    try:
        from server.services.buy_session_service import (
            build_delivery_zip,
            purchase_batch,
            rollback_batch_purchase,
        )
        from server.utils.database.orderdb import claim_order_delivery, finish_order_delivery
        from server.utils.sessions.channel_storage import download_session_from_channel
        from server.utils.sessions.crypto import decrypt_password

        await cq.answer(s.get("buy_session_processing", "![⏳](tg://emoji?id=6129574787078429498) Processing your purchase…"))
        await _safe_edit(
            cq,
            _progress_block(s.get("buy_progress_prepare", "Preparing Inventory…"), 4),
            None,
        )

        purchase = await purchase_batch(user_id, code, qty, bot_client=client)
        try:
            await _safe_edit(
                cq,
                _progress_block(s.get("buy_progress_check", "Checking Accounts…"), 6),
                None,
            )
        except Exception:
            pass
        if not await claim_order_delivery(purchase["order_id"]):
            raise RuntimeError("This order has already been delivered or is being processed.")

        items = []
        country = purchase["country"]
        for account in purchase["accounts"]:
            if not account.get("session_msg_id") or not account.get("session_chat_id"):
                raise ValueError("A reserved inventory session has no stored source.")
            stored_bytes = await download_session_from_channel(
                client,
                account["session_chat_id"],
                account["session_msg_id"],
                account_id=account.get("account_id"), phone=account.get("phone"),
            )
            phone_digits = str(account.get("phone") or "").replace("+", "").replace(" ", "")
            encrypted_tfa = account.get("tfa_password_enc", "") or account.get("password", "")
            try:
                tfa_password = decrypt_password(encrypted_tfa)
            except Exception:
                tfa_password = encrypted_tfa
            login_time = account.get("login_time")
            register_time = (
                int(login_time.timestamp())
                if hasattr(login_time, "timestamp")
                else 0
            )
            json_data = _build_delivery_json(
                phone_digits=phone_digits,
                country=country,
                fresh_info={
                    "first_name": account.get("first_name") or "",
                    "user_id": account.get("tg_user_id"),
                    "premium": bool(account.get("premium", False)),
                },
                tfa_password=tfa_password,
                api_id_used=account.get("api_id_used") or config.API_ID,
                register_time=register_time,
            )
            items.append({
                "phone_digits": phone_digits,
                "session_bytes": stored_bytes,
                "json_data": json_data,
            })

        try:
            await _safe_edit(
                cq,
                _progress_block(s.get("buy_progress_generate", "Generating Sessions…"), 8),
                None,
            )
        except Exception:
            pass

        zip_buf = build_delivery_zip(
            items,
            f"buy_session_{code}_{qty}.zip",
        )
        try:
            await _safe_edit(
                cq,
                _progress_block(s.get("buy_progress_finalize", "Finalizing…"), 10),
                None,
            )
        except Exception:
            pass
        await client.send_document(
            chat_id=user_id,
            document=zip_buf,
            file_name=zip_buf.name,
            caption=(
                f"![✅](tg://emoji?id=6129492160497589882) "
                f"**{s.get('buy_session_delivered_title', 'Session(s) delivered')}**\n"
                f"![📦](tg://emoji?id=6131886699254388574) `{qty}` "
                f"{s.get('buy_session_accounts_lbl', 'account(s)')}"
            ),
        )
        delivery_sent = True
        # Telegram delivery succeeded.  Do not compensate/refund if the
        # bookkeeping write alone fails: retrying would duplicate the ZIP.
        try:
            if not await finish_order_delivery(purchase["order_id"]):
                _log.warning(
                    "Buy-session delivery sent but order finalization was not claimed: %s",
                    purchase["order_id"],
                )
        except Exception as state_exc:
            _log.error(
                "Buy-session delivery sent but order finalization failed for %s: %s",
                purchase["order_id"],
                state_exc,
                exc_info=True,
            )

        # Telegram accepted the ZIP, so this is the successful-sale boundary.
        try:
            from server.services.market_service import finalize_successful_purchase
            finalized = await finalize_successful_purchase(
                purchase["order_id"],
                country_code=code,
                country_name=country.get("country_name", code),
            )
            if not finalized:
                _log.warning(
                    "Delivered batch did not finalize accounting for order %s",
                    purchase["order_id"],
                )
        except Exception as accounting_exc:
            _log.error(
                "Buy-session accounting finalization failed for %s: %s",
                purchase["order_id"], accounting_exc, exc_info=True,
            )

        # Fire-and-forget background cleanup for every delivered session:
        # connect to each sold account and delete all chats + leave all groups.
        try:
            import config as _cleanup_cfg
            from server.stock.account_cleanup import cleanup_session_in_background
            for _item in items:
                _phone_raw = _item.get("phone_digits", "?")
                _sbytes    = _item.get("session_bytes")
                if _sbytes:
                    asyncio.create_task(
                        cleanup_session_in_background(
                            session_bytes=_sbytes,
                            phone=_phone_raw,
                            country_code=code,
                            api_id=_cleanup_cfg.API_ID,
                            api_hash=_cleanup_cfg.API_HASH,
                        ),
                        name=f"cleanup_{_phone_raw}",
                    )
        except Exception as _ce:
            _log.warning("buy_session_confirm_cb: could not schedule cleanup tasks: %s", _ce)

        balance_now = await get_balance(user_id)
        await _safe_edit(
            cq,
            f"{_DIV}\n"
            f"![✅](tg://emoji?id=6129492160497589882) **{s.get('buy_session_done_title', 'BUY SESSION COMPLETE')}**\n"
            f"{_DIV}\n\n"
            f"![📦](tg://emoji?id=6131886699254388574) **Delivered:** `{qty}/{qty}`\n"
            f"![💰](tg://emoji?id=6129731974291527294) **{s.get('market_balance_lbl', 'Your Balance')}:** `${balance_now:.2f}`",
            _back_market_btn(s),
        )
    except Exception as exc:
        _log.error(
            "buy_session_confirm_cb failed code=%s context=%s error=%s",
            getattr(exc, "code", "UNKNOWN_STORAGE_ERROR"),
            getattr(exc, "context", {}), exc, exc_info=True,
        )
        if purchase and not delivery_sent:
            try:
                await rollback_batch_purchase(purchase, user_id, str(exc))
            except Exception as rollback_exc:
                _log.error("Buy-session rollback failed: %s", rollback_exc, exc_info=True)
        elif delivery_sent:
            _log.error(
                "Buy-session post-delivery error; keeping paid order and inventory sold: %s",
                exc,
                exc_info=True,
            )
        await _safe_edit(
            cq,
            s.get(
                "buy_session_delivery_failed",
                "![⚠️](tg://emoji?id=6129939837823753679) Delivery failed. Your balance and reserved inventory were restored.",
            ),
            _back_market_btn(s),
        )
    finally:
        _BUY_SESSION_PENDING.pop(user_id, None)


# ── SELL — Country Grid ───────────────────────────────────────────────────────

async def _build_sell_grid(page: int, s: dict) -> tuple[int, int, int, list]:
    countries = await get_all_countries(sell_only=True)
    total = len(countries)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    page_items = countries[start:start + PAGE_SIZE]

    rows = []
    for i in range(0, len(page_items), 2):
        row = []
        for c in page_items[i:i + 2]:
            row.append(InlineKeyboardButton(
                _sell_country_label(c),
                callback_data=f"sell_sel_{c['code']}_{page}",
            ))
        rows.append(row)

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(s.get("btn_prev", "![⬅️](tg://emoji?id=6129550284290006595) Prev"), callback_data=f"sell_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"![📄](tg://emoji?id=6129579803600231171) {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(s.get("btn_next", "Next ![➡️](tg://emoji?id=6129792056589031358)"), callback_data=f"sell_page_{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="sell_account_start")])
    return page, total_pages, total, rows



@bot.on_message(filters.command(["available", "avail"]) & filters.private)
async def available_countries_cmd(client, message: Message):
    try:
        msg = await _build_available_countries_message()
        await message.reply_text(msg, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        _log.error("available_countries_cmd: %s", e, exc_info=True)



@bot.on_callback_query(filters.regex(r"^sell_page_(\d+)$"))
async def sell_page_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        # User is leaving the country detail page — drop any armed
        # sell-account phone state so numbers typed later on other pages
        # aren't misread as a phone submission.
        _SA_STATE.pop(user_id, None)

        page = int(cq.matches[0].group(1))
        page, total_pages, total, rows = await _build_sell_grid(page, s)

        if total == 0:
            text = (
                f"{_DIV}\n"
                f"![💼](tg://emoji?id=5316561753100792764) **{s.get('sell_title', 'SELL ACCOUNT')}**\n"
                f"{_DIV}\n\n"
                f"{s.get('sell_no_countries', '![🚫](tg://emoji?id=6129846551134084367) No countries accepting accounts right now. Check back later!')}"
            )
            buttons = _back_market_btn(s)
        else:
            page_info = s.get("sell_page_info", "Page {0}/{1} — {2} countries").format(page, total_pages, total)
            # Deep-link back into the same bot with a start payload that triggers
            # the "available countries" summary message.
            try:
                _bot_username = (client.me.username if getattr(client, "me", None) else None) or (await client.get_me()).username
            except Exception:
                _bot_username = ""
            _avail_link = (
                f"**[__Click me to get available country list__](https://t.me/{_bot_username}?start=avail_list)**"
                if _bot_username else
                f"**__Send /available to get the country list__**"
            )
            text = (
                f"{_DIV}\n"
                f"![💼](tg://emoji?id=5316561753100792764) **{s.get('sell_title', 'SELL ACCOUNT')}**\n"
                f"{_DIV}\n\n"
                f"![📄](tg://emoji?id=6129579803600231171) {page_info}\n\n"
                f"{s.get('sell_hint', 'Select a country to view the sell price 👇')}\n\n"
                f"{_avail_link}\n"
                f"{_DIV}"
            )
            buttons = InlineKeyboardMarkup(rows)

        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("sell_page_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex(r"^sell_sel_([A-Za-z]{2,3})_(\d+)$"))
async def sell_select_country_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        code = cq.matches[0].group(1).upper()
        page = int(cq.matches[0].group(2))

        country = await get_country(code)
        if not country:
            await cq.answer(s.get("err_country_not_found", "![❌](tg://emoji?id=6129846551134084367) Country not found."), show_alert=True)
            return
        if country.get("is_full"):
            await cq.answer(s.get("sell_country_full", "![🚫](tg://emoji?id=6129846551134084367) This country is not accepting accounts right now."), show_alert=True)
            return

        idc = country.get("idc", "").strip() or "N/A"
        sell_price = country.get("sell_price", 0)
        country_name = country.get("country_name", code)

        # ── Build per-status "We Pay" price list ──────────────────────────
        # Only show statuses the country actually accepts. If price for an
        # accepted status is 0, fall back to the country's base sell_price.
        _price_rows = []
        _status_defs = [
            ("accept_clean",     "price_clean",     "![🟢](tg://emoji?id=6129492160497589882) Clean / Good News"),
            ("accept_temp_spam", "price_temp_spam", "![🟡](tg://emoji?id=6129574787078429498) Temporary Spam"),
            ("accept_perm_spam", "price_perm_spam", "![🟠](tg://emoji?id=6131886699254388574) Permanent Spam"),
        ]
        for accept_key, price_key, label in _status_defs:
            if not country.get(accept_key):
                continue
            p = float(country.get(price_key) or 0.0)
            if p <= 0:
                p = float(sell_price or 0.0)
            _price_rows.append(f"  {label} — `\"${p:g}\"`")
        if not _price_rows:
            _price_rows.append(f"  `\"${sell_price:g}\"`")
        _we_pay_block = "\n".join(_price_rows)

        # Termination delay timer from admin settings (minutes)
        try:
            from server.utils.database.configdb import get_setting as _get_setting
            _term_minutes = int(await _get_setting("termination_delay_minutes") or 1)
        except Exception:
            _term_minutes = 1

        text = (
            f"{_DIV}\n"
            f"![💼](tg://emoji?id=5316561753100792764) **{s.get('sell_country_title', 'SELL ACCOUNT — {0}').format(country_name.upper())}**\n"
            f"{_DIV}\n"
            f"{_flag(code)} **{s.get('sell_country_lbl', 'Country')}:** {country_name}\n"
            f"![📞](tg://emoji?id=5330237710655306682) **{s.get('sell_phone_code_lbl', 'Phone Code')}:** `{idc}`\n"
            f"![💵](tg://emoji?id=6129731974291527294) **{s.get('sell_we_pay_lbl', 'We Pay')}:**\n"
            f"{_we_pay_block}\n"
            f"{_DIV}\n"
            f"**{s.get('sell_requirements_title', '![✅](tg://emoji?id=6129492160497589882) Requirements')}**\n"
            f"> ***{s.get('sell_req_prefix', 'Phone number must start with `{0}`').format(idc)}***\n"
            f"> ***{s.get('sell_req_country_match', 'Account country must match `{0}`').format(country_name)}***\n"
            f"{_DIV}\n"
            f"_{s.get('sell_after_note', 'After you submit the account on the bot, please terminate it from your own device — payment processing will continue after that.')}_\n"
            f"__ _{s.get('sell_termination_time', '⏱ Termination time: ~{0} minutes').format(_term_minutes)}_ __"
        )
        # Arm the sell-account phone state so a phone number typed in chat
        # while the user is on this country detail page is captured by
        # sell_account_text_handler and routed into the live login flow —
        # matches the "Sell Now" button's callback below.
        try:
            from server.plugins.bot._shared_state import clear_pending_flows as _cpf
            _cpf(user_id, keep="sell_account")
        except Exception:
            pass
        _SA_STATE[user_id] = {
            "step":            "await_phone",
            "phone":           None,
            "country_code":    None,
            "phone_code_hash": None,
            "client":          None,
            "tmp_path":        None,
            "current_2fa":     None,
            "otp_retries":     0,
        }

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(s.get("btn_sell_now", "![📤](tg://emoji?id=6131886699254388574) Sell Now"), callback_data="sell_account_start"),
                InlineKeyboardButton(s.get("btn_my_sell_record", "![📋](tg://emoji?id=6129579803600231171) My Sell Record"), callback_data="sell_my_requests"),
            ],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=f"sell_page_{page}")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("sell_select_country_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^sell_my_requests$"))
async def sell_my_requests_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        from server.utils.database import get_user_sell_requests
        reqs = await get_user_sell_requests(user_id)
        reqs = (reqs or [])[-10:]

        if not reqs:
            history = s.get("sell_empty_requests", "📭 You have no sell requests yet.")
        else:
            lines = []
            for r in reqs:
                icon = {"pending": "![⏳](tg://emoji?id=6129574787078429498)", "approved": "![✅](tg://emoji?id=6129492160497589882)", "rejected": "![❌](tg://emoji?id=6129846551134084367)", "paid": "![💰](tg://emoji?id=6129731974291527294)"}.get(r.get("status", ""), "![❓](tg://emoji?id=6129472184604695207)")
                amount = r.get("final_price") or r.get("offer_price", 0)
                lines.append(
                    f"{icon} `{r.get('code', '??')}` "
                    f"— `{r.get('phone', 'N/A')}` "
                    f"— **${amount:g}** "
                    f"— {r.get('status', 'N/A').capitalize()}"
                )
            history = "\n".join(lines)

        text = (
            f"{_DIV}\n"
            f"![💼](tg://emoji?id=5316561753100792764) **{s.get('sell_my_requests_title', 'MY SELL REQUESTS')}**\n"
            f"{_DIV}\n\n"
            f"{history}\n\n"
            f"_{s.get('sell_requests_footer', 'Last 10 requests shown')}_"
        )
        await _safe_edit(cq, text, _back_market_btn(s))
        await cq.answer()
    except Exception as e:
        _log.error("sell_my_requests_cb: %s", e, exc_info=True)


# ── SELL — Init flow ──────────────────────────────────────────────────────────

_TELETHON_SNIPPET = (
    "```python\n"
    "from telethon.sync import TelegramClient\n\n"
    "api_id   = YOUR_API_ID    # from my.telegram.org\n"
    "api_hash = 'YOUR_API_HASH'\n\n"
    "with TelegramClient('my_session', api_id, api_hash) as client:\n"
    "    print('Session file created!')\n"
    "```"
)


@bot.on_callback_query(filters.regex(r"^sell_start_([A-Za-z]{2,3})_(\d+)$"))
async def sell_init_cb(client, cq: CallbackQuery):
    """
    Redirect to the new phone-based Sell Account flow.
    The country grid is now for browsing prices only; the actual sell flow
    starts with a phone number — country is detected automatically.
    """
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        text = (
            f"{_DIV}\n"
            f"![📱](tg://emoji?id=5330237710655306682) **{s.get('sell_acc_title', 'SELL ACCOUNT')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('sell_acc_redir_body', '![📲](tg://emoji?id=5330237710655306682) To sell this account, use the **Sell Account** button from the home screen and enter your phone number.')}\n\n"
            f"_{s.get('sell_acc_redir_note', 'Your country is auto-detected from your phone number — no need to select it manually.')}_"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_sell_account_start", "![📱](tg://emoji?id=5330237710655306682) Sell Account"), callback_data="sell_account_start")],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=f"sell_page_1")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("sell_init_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^sell_cancel$"))
async def sell_cancel_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        _SELL_STATE.pop(user_id, None)
        text = (
            f"{_DIV}\n"
            f"![❌](tg://emoji?id=6129846551134084367) **{s.get('sell_cancelled_title', 'SELL CANCELLED')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('sell_cancelled_body', 'No account was submitted. You can start again anytime.')}"
        )
        await _safe_edit(cq, text, _back_market_btn(s))
        await cq.answer()
    except Exception as e:
        _log.error("sell_cancel_cb: %s", e, exc_info=True)


# ── SELL — Document upload handler (.session file) ────────────────────────────

@bot.on_message(filters.private & filters.document)
async def sell_document_handler(client, message: Message):
    user_id = message.from_user.id
    state = _SELL_STATE.get(user_id)
    if not state or state.get("step") != "upload_session":
        await message.continue_propagation()  # Let admin handler process it
        return

    lang = await get_user_lang(user_id)
    s = get_string(lang)

    doc = message.document
    if not doc or not (doc.file_name or "").endswith(".session"):
        await message.reply_text(s.get("sell_wrong_file",
            "![❌](tg://emoji?id=6129846551134084367) **Wrong file type.**\n\n"
            "Please send a `.session` file (the file generated by Telethon).\n\n"
            "_Still waiting for your file…_ Type /cancel to abort."
        ))
        return

    _MAX_SESSION_BYTES = 512 * 1024  # 512 KB sanity cap
    if doc.file_size and doc.file_size > _MAX_SESSION_BYTES:
        await message.reply_text(s.get("sell_file_too_large",
            "![❌](tg://emoji?id=6129846551134084367) **File too large.**\n\n"
            "A valid Telethon session file is under 100 KB.\n"
            "Make sure you are sending the correct file.\n\n"
            "_Still waiting…_ Type /cancel to abort."
        ))
        return

    code = state["code"]
    _SELL_STATE.pop(user_id, None)

    progress_msg = await message.reply_text(
        f"{_DIV}\n"
        f"![🔍](tg://emoji?id=5316722951813346475) **{s.get('sell_validating_title', 'VALIDATING YOUR SESSION')}**\n"
        f"{_DIV}\n\n"
        f"{s.get('sell_validating_body', '![⏳](tg://emoji?id=6129574787078429498) Connecting to Telegram… Please wait up to 30 seconds.')}"
    )

    try:
        import config as _cfg
        from server.services.sell_service import validate_and_submit_sell

        channel_id = getattr(_cfg, "SESSION_CHANNEL_ID", None)
        if not channel_id:
            await progress_msg.edit_text(s.get("sell_no_channel", "![⚠️](tg://emoji?id=6129939837823753679) Platform is not fully configured. Please contact admin."))
            return

        try:
            buf = await client.download_media(message, in_memory=True)
            buf.seek(0)
            session_bytes = buf.read()
        except Exception as exc:
            _log.error("sell_document_handler download for user %s: %s", user_id, exc)
            await progress_msg.edit_text(s.get("sell_download_error",
                "![❌](tg://emoji?id=6129846551134084367) **Could not read your file.**\n\nPlease send the file again, or type /cancel."
            ))
            _SELL_STATE[user_id] = state  # Restore state for retry
            return

        result = await validate_and_submit_sell(
            user_id=user_id,
            country_code=code,
            session_bytes=session_bytes,
            bot_client=client,
            channel_id=channel_id,
            api_id=_cfg.API_ID,
            api_hash=_cfg.API_HASH,
        )

        if result["success"]:
            phone = result["phone"]
            pending_amount = result["pending_amount"]
            request_id = result["request_id"]
            country_name = result["country_name"]
            spam_note = ""
            if result.get("spam_status") == "temporary_spam":
                spam_note = f"\n\n{s.get('sell_spam_flag_note', '_![⚠️](tg://emoji?id=6129939837823753679) This account has a temporary spam flag. Admin will make the final decision._')}"

            text = (
                f"{_DIV}\n"
                f"![✅](tg://emoji?id=6129492160497589882) **{s.get('sell_success_title', 'SESSION SUBMITTED! ![✅](tg://emoji?id=6129492160497589882)')}**\n"
                f"{_DIV}\n\n"
                f"🆔 **{s.get('sell_request_id_lbl', 'Request ID')}:** `{request_id}`\n"
                f"![🌍](tg://emoji?id=6296303781126604562) **{s.get('sell_country_name_lbl', 'Country')}:** {country_name}\n"
                f"![📱](tg://emoji?id=5330237710655306682) **{s.get('otp_phone_lbl', 'Phone')}:** `{phone}`\n"
                f"![💰](tg://emoji?id=6129731974291527294) **{s.get('sell_pending_balance_lbl', 'Pending Balance')}:** +`${pending_amount:g}`\n"
                f"![📋](tg://emoji?id=6129579803600231171) **{s.get('sell_status_lbl', 'Status')}:** {s.get('sell_awaiting_review', '![⏳](tg://emoji?id=6129574787078429498) Awaiting admin review')}"
                f"{spam_note}\n\n"
                f"{_DIV}\n"
                f"{s.get('sell_success_note', 'Your pending balance will be credited once admin approves your request.')}\n\n"
                f"![📋](tg://emoji?id=6129579803600231171) {s.get('sell_track_hint', 'Track your request in **My Sell Requests**.')}"
            )
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton(s.get("btn_my_requests", "![📋](tg://emoji?id=6129579803600231171) My Requests"), callback_data="sell_my_requests")],
                [InlineKeyboardButton(s.get("btn_back_to_market", "![⬅️](tg://emoji?id=5258236805890710909) Back to Market"), callback_data="back_home")],
            ])
            await progress_msg.edit_text(text, reply_markup=buttons)

            if _cfg.LOG_GROUP_ID:
                try:
                    from server.utils.database import get_user
                    user_doc = await get_user(user_id)
                    uname = f"@{user_doc['username']}" if user_doc and user_doc.get("username") else f"ID:{user_id}"
                    await client.send_message(
                        _cfg.LOG_GROUP_ID,
                        f"![💼](tg://emoji?id=5316561753100792764) **NEW SELL REQUEST**\n\n"
                        f"![👤](tg://emoji?id=5316979275461573049) User: {uname} (`{user_id}`)\n"
                        f"![📱](tg://emoji?id=5330237710655306682) Phone: `{phone}`\n"
                        f"![🌍](tg://emoji?id=6296303781126604562) Country: {country_name}\n"
                        f"![💵](tg://emoji?id=6129731974291527294) Offer: `${pending_amount:g}`\n"
                        f"🆔 Request: `{request_id}`\n\n"
                        f"Use `/sell_approve {request_id}` or `/sell_reject {request_id} <reason>`",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("![✅](tg://emoji?id=6129492160497589882) Approve", callback_data=f"sela_{request_id}"),
                            InlineKeyboardButton("![❌](tg://emoji?id=6129846551134084367) Reject",  callback_data=f"selr_{request_id}"),
                        ]]),
                    )
                except Exception as notify_exc:
                    _log.warning("sell notify LOG_GROUP failed: %s", notify_exc)

        else:
            reason = result["reason"]
            detail = result["detail"]
            fixable = result.get("fixable", False)
            retry_hint = f"\n\n![✅](tg://emoji?id=6129492160497589882) _{s.get('cancel_success', 'You can try again.')}_" if fixable else ""

            text = (
                f"{_DIV}\n"
                f"![❌](tg://emoji?id=6129846551134084367) **{reason}**\n"
                f"{_DIV}\n\n"
                f"{detail}{retry_hint}"
            )
            btn_rows = []
            if fixable:
                btn_rows.append([InlineKeyboardButton(s.get("btn_sell_another", "![💼](tg://emoji?id=5316561753100792764) Sell Another"), callback_data=f"sell_sel_{code}_1")])
            btn_rows.append([InlineKeyboardButton(s.get("btn_back_to_market", "![⬅️](tg://emoji?id=5258236805890710909) Back to Market"), callback_data="back_home")])
            await progress_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(btn_rows))

    except Exception as e:
        _log.error("sell_document_handler: %s", e, exc_info=True)
        try:
            await progress_msg.edit_text(s.get("sell_error_generic",
                "![❌](tg://emoji?id=6129846551134084367) An unexpected error occurred. Please try again or contact support."
            ))
        except Exception:
            pass
