

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
    '⚠️': "6129939837823753679",
    '✅': "6129492160497589882",
    '✏️': "6129579803600231171",
    '❌': "6129846551134084367",
    '➕': "6129492160497589882",
    '⬅️': "6129550284290006595",
    '⭐': "6129492160497589882",
    '🌐': "6296303781126604562",
    '🏦': "5316705578670636235",
    '💰': "6129731974291527294",
    '💳': "6129731974291527294",
    '💵': "6129731974291527294",
    '📌': "6131886699254388574",
    '📝': "6129579803600231171",
    '📤': "6131886699254388574",
    '📥': "6131886699254388574",
    '🔑': "6129782440157256336",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Wallet plugin — Deposit, Withdraw, Address Management.
"""

import secrets as _sec

from pyrogram import filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from server.utils.bot_utils import Btn as InlineKeyboardButton

import config
from server import bot, LOGGER
from server.utils.database import get_balance, get_reserve_balance, get_user_lang
from server.utils.database.userdb import get_wallet_addresses, set_wallet_address
from server.utils.database.walletdb import create_deposit_v2
from server.services.withdrawal.models import FeeCalculation
from server.plugins.bot._shared_state import wallet_state as _STATE
from server.utils.bot_utils import DIV as _DIV, safe_edit as _safe_edit
from server.utils.constants import MIN_DEPOSIT, MIN_WITHDRAWAL
from server.utils.validation import validate_wallet_address as _validate_address
from strings import get_string

_log = LOGGER(__name__)

def _setting_is_enabled(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


async def _feature_enabled(key: str, default: bool = True) -> bool:
    try:
        from server.utils.database.configdb import get_setting
        return _setting_is_enabled(await get_setting(key), default=default)
    except Exception as exc:
        _log.warning("feature toggle %s read failed: %s", key, exc)
        return default


def _support_button(s: dict) -> InlineKeyboardMarkup:
    try:
        url = config.support_link()
    except Exception:
        url = config.SUPPORT_GROUP
    return InlineKeyboardMarkup([[InlineKeyboardButton(s.get("btn_contact_support", "![🔔](tg://emoji?id=5316921740079675140) Contact Support"), url=url)]])


async def _answer_feature_blocked(cq: CallbackQuery, s: dict, key: str, fallback: str):
    text = s.get(key, fallback)
    try:
        if cq.message:
            await cq.message.edit_text(text, reply_markup=_support_button(s))
    except Exception:
        try:
            if cq.message:
                await cq.message.reply_text(text, reply_markup=_support_button(s))
        except Exception:
            pass
    await cq.answer(s.get("feature_disabled_alert", "This feature is currently disabled."), show_alert=True)


async def _reply_feature_blocked(message: Message, s: dict, key: str, fallback: str):
    await message.reply_text(s.get(key, fallback), reply_markup=_support_button(s))

# Emoji + display label for each provider
_METHOD_META: dict[str, tuple[str, str]] = {
    "telegram_stars":  ("![⭐](tg://emoji?id=6129492160497589882)", "Telegram Stars"),
    "trc20_scan":      ("🔹", "USDT (TRC20)"),
    "bep20_scan":      ("🔷", "USDT (BEP20)"),
    "binance_pay":     ("![💰](tg://emoji?id=6129731974291527294)", "Binance Pay (Auto)"),
    "binance_pay_tx":  ("![💰](tg://emoji?id=6129731974291527294)", "Binance Pay (Auto)"),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _edit_by_ref(client, chat_id: int, msg_id: int, text: str, markup=None):
    """Edit a message by its chat_id/msg_id reference stored in state."""
    try:
        await client.edit_message_text(chat_id, msg_id, text, reply_markup=markup)
    except Exception as e:
        _log.debug("_edit_by_ref failed (chat=%s msg=%s): %s", chat_id, msg_id, e)


def _wallet_main_buttons(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(s.get("btn_deposits", "![📥](tg://emoji?id=6131886699254388574) Deposit"), callback_data="wallet_deposit"),
            InlineKeyboardButton(s.get("btn_withdrawals", "![📤](tg://emoji?id=6131886699254388574) Withdraw"), callback_data="wallet_withdraw"),
        ],
        [InlineKeyboardButton(s.get("btn_manage_addr", "![🔑](tg://emoji?id=6129782440157256336) Manage Addresses"), callback_data="wallet_addr_menu")],
        [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")],
    ])


def _addr_buttons(addrs: dict, s: dict) -> InlineKeyboardMarkup:
    trc = addrs.get("trc20")
    bep = addrs.get("bep20")
    trc_label = f"![✏️](tg://emoji?id=6129579803600231171) TRC20: {trc[:10]}…" if trc else s.get("btn_add_trc", "![➕](tg://emoji?id=6129492160497589882) Add TRC20 Address")
    bep_label = f"![✏️](tg://emoji?id=6129579803600231171) BEP20: {bep[:10]}…" if bep else s.get("btn_add_bep", "![➕](tg://emoji?id=6129492160497589882) Add BEP20 Address")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(trc_label, callback_data="wallet_set_trc20")],
        [InlineKeyboardButton(bep_label, callback_data="wallet_set_bep20")],
        [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="show_wallet")],
    ])


async def _get_active_providers():
    """Return configured providers that are also admin-enabled."""
    from server.services.deposit.registry import get_configured_providers
    from server.utils.database.configdb import get_setting
    configured = get_configured_providers()
    enabled_map = await get_setting("deposit_methods") or {}
    # A method not present in the map is ON by default
    return [p for p in configured if enabled_map.get(p.method_id, True)]


def _method_picker_markup(providers, s: dict, back_to: str = "show_wallet") -> InlineKeyboardMarkup:
    rows = []
    for p in providers:
        emoji, label = _METHOD_META.get(p.method_id, ("![💳](tg://emoji?id=6129731974291527294)", p.method_name))
        rows.append([InlineKeyboardButton(f"{emoji} {label}", callback_data=f"dep_method_{p.method_id}")])
    rows.append([InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=back_to)])
    return InlineKeyboardMarkup(rows)


def _method_picker_text(s: dict) -> str:
    return (
        f"{_DIV}\n"
        f"![📥](tg://emoji?id=6131886699254388574) **{s.get('dep_choose_title', 'SELECT DEPOSIT METHOD')}**\n"
        f"{_DIV}\n\n"
        f"{s.get('dep_choose_body', 'Choose your preferred payment method below:')}\n\n"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_min_deposit', 'Minimum deposit')}:** `${MIN_DEPOSIT:.2f} USD`"
    )


async def _process_deposit(client, uid: int, fallback_msg, state: dict, s: dict):
    """
    Generate the payment via the chosen provider and edit the stored
    message with the result (or error). Called after amount is collected.
    """
    from server.services.deposit.registry import get_provider

    data      = state["data"]
    method_id = data["method"]
    amount    = data["amount"]
    network   = data.get("network")
    chat_id   = data.get("chat_id", uid)
    msg_id    = data.get("msg_id")

    provider = get_provider(method_id)
    emoji, label = _METHOD_META.get(method_id, ("![💳](tg://emoji?id=6129731974291527294)", provider.method_name if provider else method_id))
    dep_id = f"DEP-{_sec.token_hex(8).upper()}"

    back_target = data.get("origin", "show_wallet")
    back_btn = InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"), callback_data=back_target)

    async def _edit(text, markup=None):
        if msg_id:
            await _edit_by_ref(client, chat_id, msg_id, text, markup)
        else:
            try:
                await fallback_msg.edit_text(text, reply_markup=markup)
            except Exception:
                await fallback_msg.reply_text(text, reply_markup=markup)

    try:
        if await _feature_enabled("maintenance_mode", default=False):
            _STATE.pop(uid, None)
            await _edit(s.get("maintenance_mode_msg", "🔧 The platform is currently under maintenance. Please try again later."), _support_button(s))
            return
        if not await _feature_enabled("deposits_enabled", default=True):
            _STATE.pop(uid, None)
            await _edit(s.get("deposits_disabled_msg", "🚫 New deposits are currently disabled. Please try again later or contact support."), _support_button(s))
            return

        details = await provider.create_payment(dep_id, amount, uid, network=network)

        await create_deposit_v2(
            deposit_id=dep_id,
            user_id=uid,
            amount=amount,
            method=method_id,
            currency=details.currency,
            address=details.address,
            payment_url=details.payment_url,
            qr_url=details.qr_url,
            memo=details.memo,
            phone=details.phone,
            instructions=details.instructions,
            expires_at=details.expires_at,
            extra=details.extra,
        )
        _STATE.pop(uid, None)

        # ── Build result message based on what the provider returned ─────────

        if details.payment_url:
            # OxaPay, Binance Pay, Telegram Stars — redirect to payment page
            ttl = details.extra.get("ttl_minutes", 60)
            pay_btn    = InlineKeyboardButton(s.get("btn_pay_now", "![💳](tg://emoji?id=6129731974291527294) Pay Now →"), url=details.payment_url)
            verify_btn = InlineKeyboardButton(s.get("btn_verify_payment", "🔍 Verify Payment"), callback_data=f"dep_verify_{dep_id}")
            # Binance Pay: show Verify button so user can poll order status
            if method_id == "binance_pay":
                markup = InlineKeyboardMarkup([[pay_btn], [verify_btn], [back_btn]])
            else:
                markup = InlineKeyboardMarkup([[pay_btn], [verify_btn], [back_btn]])

            text = (
                f"{_DIV}\n"
                f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_ready_title', 'PAYMENT READY')}**\n"
                f"{_DIV}\n\n"
                f"{emoji} **{s.get('lbl_method', 'Method')}:** {label}\n"
                f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${amount:.2f} USD`\n"
                f"🪙 **{s.get('lbl_currency', 'Currency')}:** `{details.currency}`\n"
                f"⏱ **{s.get('lbl_expires', 'Expires in')}:** `{ttl} {s.get('lbl_minutes', 'minutes')}`\n\n"
                f"👆 {s.get('dep_tap_pay', 'Tap **Pay Now** to open the payment page.')}\n"
                f"![✅](tg://emoji?id=6129492160497589882) {s.get('dep_auto_credit', 'Your balance is credited automatically once payment is confirmed.')}"
            )

        elif details.address:
            # TRC20 / BEP20 direct — show wallet address + Verify button
            ttl        = details.extra.get("ttl_minutes", 60)
            verify_btn = InlineKeyboardButton(s.get("btn_verify_payment", "🔍 Verify Payment"), callback_data=f"dep_verify_{dep_id}")
            markup     = InlineKeyboardMarkup([[verify_btn], [back_btn]])

            text = (
                f"{_DIV}\n"
                f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_ready_title', 'PAYMENT READY')}**\n"
                f"{_DIV}\n\n"
                f"{emoji} **{s.get('lbl_method', 'Method')}:** {label}\n"
                f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${amount:.2f} USD`\n"
                f"🪙 **{s.get('lbl_currency', 'Currency')}:** `{details.currency}`\n"
                f"⏱ **{s.get('lbl_expires', 'Expires in')}:** `{ttl} {s.get('lbl_minutes', 'minutes')}`\n\n"
                f"📬 **Send EXACTLY `${amount:.2f} USDT` to this address:**\n"
                f"`{details.address}`\n\n"
                f"![⚠️](tg://emoji?id=6129939837823753679) Send the **exact** amount shown. "
                f"After sending, tap **Verify Payment** to confirm.\n"
                f"![✅](tg://emoji?id=6129492160497589882) Your balance is credited automatically once matched on-chain."
            )

        elif method_id == "binance_pay_tx":
            # Manual Binance Pay — show UID + instructions + "Enter Order ID" verify button
            ttl        = details.extra.get("ttl_minutes", 60)
            pay_uid    = details.extra.get("pay_uid", "—")
            verify_btn = InlineKeyboardButton(
                s.get("btn_verify_payment", "🔍 Verify Payment"),
                callback_data=f"dep_verify_{dep_id}",
            )
            markup = InlineKeyboardMarkup([[verify_btn], [back_btn]])
            text = (
                f"{_DIV}\n"
                f"![💰](tg://emoji?id=6129731974291527294) **{s.get('dep_ready_title', 'PAYMENT READY')}**\n"
                f"{_DIV}\n\n"
                f"![💰](tg://emoji?id=6129731974291527294) **{s.get('lbl_method', 'Method')}:** {s.get('binance_pay_auto_label', 'Binance Pay (Auto)')}\n"
                f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${amount:.2f} USDT`\n"
                f"⏱ **{s.get('lbl_expires', 'Expires in')}:** `{ttl} {s.get('lbl_minutes', 'minutes')}`\n\n"
                f"📌 **Send to Binance Pay ID:**\n`{pay_uid}`\n\n"
                f"![⚠️](tg://emoji?id=6129939837823753679) Send the **exact** amount shown above.\n"
                f"After sending, tap **Verify Payment** and enter your Binance **Order ID**."
            )

        else:
            markup = InlineKeyboardMarkup([[back_btn]])
            text = (
                f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_created_title', 'DEPOSIT CREATED')}**\n\n"
                f"🆔 `{dep_id}`\n\n"
                f"{details.instructions or ''}"
            )

        await _edit(text, markup)

    except Exception as exc:
        _log.error("deposit %s error for uid %s: %s", method_id, uid, exc, exc_info=True)
        _STATE.pop(uid, None)
        err_text = s.get("err_payment_fail", "![❌](tg://emoji?id=6129846551134084367) Failed to generate payment. Please try again or contact support.")
        await _edit(err_text, InlineKeyboardMarkup([[back_btn]]))


# ── show_wallet ───────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^show_wallet$"))
async def show_wallet_cb(client, cq: CallbackQuery):
    try:
        uid = cq.from_user.id
        _STATE.pop(uid, None)  # backing out of any in-progress deposit/withdraw clears its state
        lang = await get_user_lang(uid)
        s = get_string(lang)

        from server.utils.database.userdb import get_pending_balance
        balance = await get_balance(uid)
        reserve_balance = await get_reserve_balance(uid)
        pending_balance = await get_pending_balance(uid)
        addrs = await get_wallet_addresses(uid)

        trc = addrs.get("trc20") or s.get("trc_not_set", "Not set ![⚠️](tg://emoji?id=6129939837823753679)")
        bep = addrs.get("bep20") or s.get("bep_not_set", "Not set ![⚠️](tg://emoji?id=6129939837823753679)")

        pending_line = (
            f"![⏳](tg://emoji?id=6129574787078429498) **Pending Balance:** `${pending_balance:.4f} USD`\n"
            if pending_balance > 0 else ""
        )

        text = (
            f"{_DIV}\n"
            f"![💳](tg://emoji?id=6129731974291527294) **{s.get('wallet_main_title', 'YOUR WALLET')}**\n"
            f"{_DIV}\n\n"
            f"![💰](tg://emoji?id=6129731974291527294) **{s.get('lbl_balance', 'Balance')}:** `${balance:.4f} USD`\n"
            f"![💵](tg://emoji?id=6129731974291527294) **Reserve Balance (Withdrawable):** `${reserve_balance:.4f} USD`\n"
            f"{pending_line}\n"
            f"{_DIV}\n"
            f"![🏦](tg://emoji?id=5316705578670636235) **{s.get('wallet_addr_section', 'WITHDRAWAL ADDRESSES (USDT)')}**\n"
            f"{_DIV}\n\n"
            f"🔹 **TRC20:** `{trc}`\n"
            f"🔹 **BEP20:** `{bep}`\n\n"
            f"{s.get('wallet_addr_tip', 'Set your addresses below to enable withdrawals.\nDeposits are processed via Telegram Stars.')}"
        )
        await _safe_edit(cq, text, _wallet_main_buttons(s))
        await cq.answer()
    except Exception as e:
        _log.error("show_wallet_cb: %s", e, exc_info=True)


# ── Address Management ────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^wallet_addr_menu$"))
async def wallet_addr_menu_cb(client, cq: CallbackQuery):
    try:
        uid = cq.from_user.id
        lang = await get_user_lang(uid)
        s = get_string(lang)

        addrs = await get_wallet_addresses(uid)
        text = (
            f"{_DIV}\n"
            f"![🔑](tg://emoji?id=6129782440157256336) **{s.get('wallet_manage_title', 'WITHDRAWAL ADDRESSES')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('wallet_manage_body', 'Set your USDT wallet addresses for withdrawals.')}"
        )
        await _safe_edit(cq, text, _addr_buttons(addrs, s))
        await cq.answer()
    except Exception as e:
        _log.error("wallet_addr_menu_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^wallet_set_(trc20|bep20)$"))
async def wallet_set_addr_cb(client, cq: CallbackQuery):
    try:
        network = "TRC20" if "trc20" in cq.data else "BEP20"
        uid = cq.from_user.id
        lang = await get_user_lang(uid)
        s = get_string(lang)

        try:
            from server.plugins.bot._shared_state import clear_pending_flows
            clear_pending_flows(uid, keep="wallet")
            from server.plugins.bot.sell_account import _cleanup_state as _sa_cleanup
            await _sa_cleanup(uid)
        except Exception:
            pass
        _STATE[uid] = {"step": f"set_{network.lower()}", "data": {"network": network}}

        example = "`TXhA...abcd` (34 chars)" if network == "TRC20" else "`0x1234...abcd` (42 chars)"
        title = s.get("set_addr_title", "SET {0} ADDRESS").format(network)
        body = s.get("set_addr_body", "Send your USDT **{0}** wallet address.\n\nExample: {1}\n\n![⚠️](tg://emoji?id=6129939837823753679) Only send a valid USDT address!\nType /cancel to abort.").format(network, example)

        await cq.message.reply_text(f"![📝](tg://emoji?id=6129579803600231171) **{title}**\n\n{body}")
        await cq.answer()
    except Exception as e:
        _log.error("wallet_set_addr_cb: %s", e, exc_info=True)


@bot.on_message(filters.command("setwallet") & filters.private)
async def setwallet_cmd(client, message: Message):
    uid = message.from_user.id
    lang = await get_user_lang(uid)
    s = get_string(lang)
    addrs = await get_wallet_addresses(uid)
    await message.reply_text(
        f"![🔑](tg://emoji?id=6129782440157256336) **{s.get('wallet_manage_title', 'WITHDRAWAL ADDRESSES')}**\n\n"
        f"{s.get('wallet_addr_tip', 'Set your addresses below to enable withdrawals.')}",
        reply_markup=_addr_buttons(addrs, s),
    )


# ── Deposit — Method Picker ───────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wallet_deposit(_profile)?$"))
async def wallet_deposit_cb(client, cq: CallbackQuery):
    try:
        uid = cq.from_user.id
        lang = await get_user_lang(uid)
        s = get_string(lang)

        origin = "show_profile" if cq.data.endswith("_profile") else "show_wallet"

        if await _feature_enabled("maintenance_mode", default=False):
            await _answer_feature_blocked(cq, s, "maintenance_mode_msg", "🔧 The platform is currently under maintenance. Please try again later.")
            return
        if not await _feature_enabled("deposits_enabled", default=True):
            await _answer_feature_blocked(cq, s, "deposits_disabled_msg", "🚫 New deposits are currently disabled. Please try again later or contact support.")
            return

        providers = await _get_active_providers()
        if not providers:
            await cq.answer(
                s.get("no_deposit_methods", "![❌](tg://emoji?id=6129846551134084367) No deposit methods are available. Contact admin."),
                show_alert=True,
            )
            return

        try:
            from server.plugins.bot._shared_state import clear_pending_flows
            clear_pending_flows(uid, keep="wallet")
            from server.plugins.bot.sell_account import _cleanup_state as _sa_cleanup
            await _sa_cleanup(uid)
        except Exception:
            pass
        _STATE[uid] = {"step": "dep_picker", "data": {"origin": origin}}
        await _safe_edit(cq, _method_picker_text(s), _method_picker_markup(providers, s, back_to=origin))
        await cq.answer()
    except Exception as e:
        _log.error("wallet_deposit_cb: %s", e, exc_info=True)


@bot.on_message(filters.command("deposit") & filters.private)
async def deposit_cmd(client, message: Message):
    uid = message.from_user.id
    lang = await get_user_lang(uid)
    s = get_string(lang)

    if await _feature_enabled("maintenance_mode", default=False):
        await _reply_feature_blocked(message, s, "maintenance_mode_msg", "🔧 The platform is currently under maintenance. Please try again later.")
        return
    if not await _feature_enabled("deposits_enabled", default=True):
        await _reply_feature_blocked(message, s, "deposits_disabled_msg", "🚫 New deposits are currently disabled. Please try again later or contact support.")
        return

    providers = await _get_active_providers()
    if not providers:
        await message.reply_text(s.get("no_deposit_methods", "![❌](tg://emoji?id=6129846551134084367) No deposit methods are available. Contact admin."))
        return

    await message.reply_text(_method_picker_text(s), reply_markup=_method_picker_markup(providers, s))


# ── Deposit — Method Selected ─────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^dep_method_\w+$"))
async def dep_method_cb(client, cq: CallbackQuery):
    try:
        uid = cq.from_user.id
        method_id = cq.data[len("dep_method_"):]
        lang = await get_user_lang(uid)
        s = get_string(lang)

        if await _feature_enabled("maintenance_mode", default=False):
            await _answer_feature_blocked(cq, s, "maintenance_mode_msg", "🔧 The platform is currently under maintenance. Please try again later.")
            _STATE.pop(uid, None)
            return
        if not await _feature_enabled("deposits_enabled", default=True):
            await _answer_feature_blocked(cq, s, "deposits_disabled_msg", "🚫 New deposits are currently disabled. Please try again later or contact support.")
            _STATE.pop(uid, None)
            return

        from server.services.deposit.registry import get_provider
        provider = get_provider(method_id)
        if not provider or not provider.is_configured():
            await cq.answer(s.get("method_unavailable", "![❌](tg://emoji?id=6129846551134084367) This method is currently unavailable."), show_alert=True)
            return

        emoji, label = _METHOD_META.get(method_id, ("![💳](tg://emoji?id=6129731974291527294)", provider.method_name))

        prev_state = _STATE.get(uid) or {}
        origin = prev_state.get("data", {}).get("origin", "show_wallet")

        # Store method + reference to THIS message so we can edit it when the
        # user sends their amount as a text reply.
        _STATE[uid] = {
            "step": "deposit_amount",
            "data": {
                "method":   method_id,
                "chat_id":  cq.message.chat.id,
                "msg_id":   cq.message.id,
                "origin":   origin,
            },
        }

        text = (
            f"{_DIV}\n"
            f"{emoji} **{label}**\n"
            f"{_DIV}\n\n"
            f"{s.get('dep_enter_amount', 'Enter the amount **(in USD)** you want to deposit:')}\n\n"
            f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_min_deposit', 'Minimum')}:** `${MIN_DEPOSIT:.2f} USD`\n\n"
            f"_{s.get('dep_type_cancel', 'Type /cancel to abort.')}_"
        )
        cancel_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="dep_back_methods"),
        ]])
        await _safe_edit(cq, text, cancel_markup)
        await cq.answer()
    except Exception as e:
        _log.error("dep_method_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex(r"^dep_verify_DEP-[A-F0-9]+$"))
async def dep_verify_cb(client, cq: CallbackQuery):
    """
    'Verify Payment' button — user manually polls OxaPay for latest status.
    Works for OxaPay crypto deposits. Idempotent — safe to tap multiple times.
    """
    try:
        uid = cq.from_user.id
        dep_id = cq.data[len("dep_verify_"):]
        lang = await get_user_lang(uid)
        s = get_string(lang)

        await cq.answer(s.get("verifying_payment", "🔍 Checking payment status…"))

        from server.utils.database.walletdb import get_deposit, confirm_deposit
        dep = await get_deposit(dep_id)

        if not dep or dep.get("user_id") != uid:
            await cq.answer(s.get("err_deposit_not_found", "❌ Deposit not found."), show_alert=True)
            return

        current_status = dep.get("status", "pending")

        # ── Already completed ─────────────────────────────────────────────────
        if current_status == "completed":
            await cq.answer(
                s.get("dep_already_credited", "✅ Payment already confirmed! Your balance was credited."),
                show_alert=True,
            )
            return

        # ── Already in a final state ──────────────────────────────────────────
        if current_status in ("expired", "failed", "rejected"):
            label = {"expired": "⏰ Expired", "failed": "❌ Failed", "rejected": "❌ Rejected"}.get(current_status, current_status)
            await cq.answer(
                s.get("dep_final_state", "This deposit is {0}. Please create a new one.").format(label),
                show_alert=True,
            )
            return

        method = dep.get("method", "")

        # ── Manual blockchain verification (TRC20 / BEP20) ───────────────────
        if method in ("trc20_scan", "bep20_scan"):
            network = "TRC20" if method == "trc20_scan" else "BEP20"
            _STATE[uid] = {
                "step": "deposit_chain_hash",
                "data": {
                    "dep_id": dep_id,
                    "network": network,
                    "chat_id": cq.message.chat.id if cq.message else uid,
                    "msg_id": cq.message.id if cq.message else None,
                },
            }
            try:
                await cq.message.edit_text(
                    f"{_DIV}\n"
                    f"![🔐](tg://emoji?id=6129782440157256336) **Submit Your {network} Transaction Hash**\n"
                    f"{_DIV}\n\n"
                    f"After sending **${float(dep.get('amount') or 0):.2f} USDT** to the shown address, "
                    f"send the transaction hash / transaction ID here.\n\n"
                    f"We verify the real on-chain transfer, USDT token, receiving address, amount, and confirmations before crediting. "
                    f"A transaction hash can only be used once.\n\n"
                    f"_Send only the hash, not an explorer link. Type /cancel to go back._",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                            callback_data="show_wallet",
                        )
                    ]]),
                )
            except Exception:
                pass
            await cq.answer()
            return

        # ── Binance Pay (Auto) — prompt user to enter their Order ID ─────────
        if method == "binance_pay_tx":
            # Set state so the next message is treated as the Order ID input
            _STATE[uid] = {
                "step": "dep_bptx_order_id",
                "data": {
                    "dep_id":  dep_id,
                    "amount":  dep.get("amount", 0.0),
                    "chat_id": cq.message.chat.id if cq.message else uid,
                    "msg_id":  cq.message.id if cq.message else None,
                },
            }
            try:
                await cq.message.edit_text(
                    f"{_DIV}\n"
                    f"![🔑](tg://emoji?id=6129782440157256336) **Enter Your Binance Order ID**\n"
                    f"{_DIV}\n\n"
                    f"Please type the **Order ID** from your Binance Pay transaction.\n\n"
                    f"You can find it in:\n"
                    f"**Binance App → Pay → Transaction History → Order ID**\n\n"
                    f"_Type /cancel to go back._",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                            callback_data="show_wallet",
                        )
                    ]]),
                )
            except Exception:
                pass
            await cq.answer()
            return

        # ── Binance Pay — query order status via Merchant API ────────────────
        if method == "binance_pay":
            from server.services.deposit.providers.binance_pay import query_order
            result = await query_order(dep_id)

            if not result["ok"]:
                _log.warning("dep_verify: Binance Pay query failed for %s: %s", dep_id, result.get("error"))
                await cq.answer(
                    s.get("dep_verify_error", "⚠️ Could not reach Binance Pay. Try again in a moment."),
                    show_alert=True,
                )
                return

            b_status   = result["status"]      # e.g. "PAID", "PENDING"
            int_status = result["internal"]    # e.g. "completed", "pending"

            _log.info("dep_verify: Binance %s status=%s internal=%s", dep_id, b_status, int_status)

            if int_status == "completed":
                dep_doc = await confirm_deposit(dep_id, confirmed_by=None)
                if dep_doc:
                    _log.info("dep_verify: Binance Pay credited $%.4f to user %s", dep_doc["amount"], uid)
                    # Store transaction id if present
                    tx = result.get("data", {}).get("transactionId")
                    if tx:
                        from server.utils.database.walletdb import depositsdb as _ddb
                        await _ddb.update_one(
                            {"deposit_id": dep_id},
                            {"$set": {"tx_hash": tx, "extra.tx_hash": tx}},
                        )
                    try:
                        from server.utils.notifications import notify
                        await notify(uid, "deposit_confirmed",
                                     deposit_id=dep_id,
                                     amount=dep_doc["amount"],
                                     method="binance_pay")
                    except Exception:
                        pass
                    try:
                        await cq.message.edit_text(
                            f"{_DIV}\n"
                            f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_verified_title', 'PAYMENT CONFIRMED!')}**\n"
                            f"{_DIV}\n\n"
                            f"![💰](tg://emoji?id=6129731974291527294) **{s.get('lbl_method', 'Method')}:** Binance Pay\n"
                            f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${dep_doc['amount']:.2f} USD`\n"
                            f"🆔 **{s.get('lbl_deposit_id', 'Deposit ID')}:** `{dep_id}`\n\n"
                            f"![✅](tg://emoji?id=6129492160497589882) {s.get('dep_balance_credited', 'Your balance has been credited!')}",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton(
                                    s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                                    callback_data="show_wallet",
                                )
                            ]]),
                        )
                    except Exception:
                        pass
                else:
                    await cq.answer(
                        s.get("dep_already_credited", "✅ Payment already confirmed! Your balance was credited."),
                        show_alert=True,
                    )
                return

            # Map Binance Pay statuses to user messages
            status_msg = {
                "pending":  s.get("dep_status_pending",    "⏳ Waiting for payment on Binance Pay."),
                "expired":  s.get("dep_status_expired",    "⏰ Order expired. Please create a new deposit."),
                "failed":   s.get("dep_status_failed",     "❌ Order failed or was cancelled."),
            }.get(int_status, f"ℹ️ Order status: {b_status}")

            await cq.answer(status_msg, show_alert=True)
            return

        # ── Non-OxaPay / Telegram Stars fallback ─────────────────────────────
        if method != "oxapay":
            await cq.answer(
                s.get("dep_verify_stars", "⭐ This payment is verified automatically. Please wait."),
                show_alert=True,
            )
            return

        track_id = (dep.get("extra") or {}).get("track_id")
        if not track_id:
            await cq.answer(
                s.get("dep_no_track_id", "⚠️ Cannot verify — payment link not yet generated."),
                show_alert=True,
            )
            return

        from os import getenv
        merchant_key = getenv("OXAPAY_MERCHANT_KEY", "")
        if not merchant_key:
            await cq.answer("⚠️ Merchant key not configured. Contact admin.", show_alert=True)
            return

        from server.services.deposit.providers.oxapay import inquiry_payment
        result = await inquiry_payment(merchant_key, track_id)

        if not result["ok"]:
            _log.warning("dep_verify: inquiry failed for %s: %s", dep_id, result.get("error"))
            await cq.answer(
                s.get("dep_verify_error", "⚠️ Could not reach payment gateway. Try again in a moment."),
                show_alert=True,
            )
            return

        oxapay_status = result["status"]
        internal_status = result["internal"]

        _log.info("dep_verify: %s OxaPay=%s internal=%s", dep_id, oxapay_status, internal_status)

        # ── Payment confirmed via inquiry ─────────────────────────────────────
        if internal_status == "completed":
            dep_doc = await confirm_deposit(dep_id, confirmed_by=None)
            if dep_doc:
                _log.info("dep_verify: credited $%.4f to user %s via manual verify", dep_doc["amount"], uid)
                try:
                    from server.utils.notifications import notify
                    await notify(uid, "deposit_confirmed",
                                 deposit_id=dep_id,
                                 amount=dep_doc["amount"],
                                 method="oxapay")
                except Exception:
                    pass
                # Update the message — remove Verify button, show success
                try:
                    back_target = "show_wallet"
                    await cq.message.edit_text(
                        f"{_DIV}\n"
                        f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_verified_title', 'PAYMENT CONFIRMED!')}**\n"
                        f"{_DIV}\n\n"
                        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${dep_doc['amount']:.2f} USD`\n"
                        f"🆔 **{s.get('lbl_deposit_id', 'Deposit ID')}:** `{dep_id}`\n\n"
                        f"![✅](tg://emoji?id=6129492160497589882) {s.get('dep_balance_credited', 'Your balance has been credited!')}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"), callback_data=back_target)
                        ]]),
                    )
                except Exception:
                    pass
            else:
                # Already credited by webhook concurrently — that's fine
                await cq.answer(
                    s.get("dep_already_credited", "✅ Payment already confirmed! Your balance was credited."),
                    show_alert=True,
                )
            return

        # ── In-flight status update ───────────────────────────────────────────
        if internal_status == "confirming" and current_status == "pending":
            from server.utils.database.walletdb import depositsdb
            await depositsdb.update_one(
                {"deposit_id": dep_id, "status": "pending"},
                {"$set": {"status": "confirming", "extra.oxapay_status": oxapay_status}},
            )

        # ── Show current status to user ───────────────────────────────────────
        status_emoji = {
            "pending":    "⏳",
            "confirming": "🔗",
            "expired":    "⏰",
            "failed":     "❌",
        }.get(internal_status, "ℹ️")

        status_label = {
            "pending":    s.get("dep_status_pending",    "Waiting for payment"),
            "confirming": s.get("dep_status_confirming", "Payment received — confirming on blockchain"),
            "expired":    s.get("dep_status_expired",    "Invoice expired"),
            "failed":     s.get("dep_status_failed",     "Payment failed"),
        }.get(internal_status, oxapay_status)

        await cq.answer(
            f"{status_emoji} {status_label}",
            show_alert=True,
        )

    except Exception as e:
        _log.error("dep_verify_cb: %s", e, exc_info=True)
        try:
            await cq.answer("⚠️ Verification failed. Try again.", show_alert=True)
        except Exception:
            pass


@bot.on_callback_query(filters.regex("^dep_back_methods$"))
async def dep_back_methods_cb(client, cq: CallbackQuery):
    """Go back to the method picker from the amount-entry screen."""
    try:
        uid = cq.from_user.id
        prev_state = _STATE.pop(uid, None) or {}
        origin = prev_state.get("data", {}).get("origin", "show_wallet")
        lang = await get_user_lang(uid)
        s = get_string(lang)
        providers = await _get_active_providers()
        _STATE[uid] = {"step": "dep_picker", "data": {"origin": origin}}
        await _safe_edit(cq, _method_picker_text(s), _method_picker_markup(providers, s, back_to=origin))
        await cq.answer()
    except Exception as e:
        _log.error("dep_back_methods_cb: %s", e, exc_info=True)


# ── Deposit — Network Selection (Crypto provider) ─────────────────────────────

@bot.on_callback_query(filters.regex(r"^dep_net_\w+$"))
async def dep_network_cb(client, cq: CallbackQuery):
    try:
        uid = cq.from_user.id
        lang = await get_user_lang(uid)
        s = get_string(lang)

        state = _STATE.get(uid)
        if not state or state.get("step") != "deposit_network":
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        network = cq.data[len("dep_net_"):]
        state["data"]["network"] = network
        state["step"] = "deposit_confirming"

        await cq.answer(s.get("generating_link", "![⏳](tg://emoji?id=6129574787078429498) Generating payment…"))

        # Show "generating" state in the message while we work
        chat_id = state["data"].get("chat_id", uid)
        msg_id  = state["data"].get("msg_id")
        await _edit_by_ref(client, chat_id, msg_id, s.get("generating_link", "![⏳](tg://emoji?id=6129574787078429498) Generating payment…"))

        await _process_deposit(client, uid, cq.message, state, s)
    except Exception as e:
        _log.error("dep_network_cb: %s", e, exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# WITHDRAWAL FLOW
#
# Full inline-button flow (no plain-text YES/NO confirmations):
#   1. Network picker  (TRC20 / BEP20)
#   2. Address entry   (type new or tap "Use Saved")
#   3. Amount entry    (type amount in USD)
#   4. Review screen   (full details — Confirm / Edit Amount / Edit Address / Cancel)
#   5. Submit          → success message with "Check Status" button
#
# All steps live inside a single message that is edited in-place.
# User text messages (address, amount) are deleted for a clean inbox.
# ═══════════════════════════════════════════════════════════════════════════════

# ── Text helpers ──────────────────────────────────────────────────────────────

def _withdraw_network_text(s: dict) -> str:
    return (
        f"{_DIV}\n"
        f"![📤](tg://emoji?id=6131886699254388574) **{s.get('wallet_withdraw_title', 'WITHDRAWAL REQUEST')}**\n"
        f"{_DIV}\n\n"
        f"![🌐](tg://emoji?id=6296303781126604562) **{s.get('wit_select_network', 'Select Network')}**\n\n"
        f"{s.get('wit_network_body', 'Choose the USDT withdrawal network:')}\n\n"
        f"🪙 **{s.get('lbl_currency', 'Currency')}:** `USDT`\n"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_min_withdrawal', 'Minimum')}:** `${MIN_WITHDRAWAL:.2f} USD`"
    )


def _withdraw_network_markup(s: dict, back_to: str = "show_wallet") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔹 TRC20", callback_data="wit_net_TRC20"),
            InlineKeyboardButton("🔷 BEP20", callback_data="wit_net_BEP20"),
        ],
        [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data=back_to)],
    ])


def _withdraw_address_text(network: str, saved_addr, s: dict) -> str:
    example = "`TXhA…abcd` (34 chars)" if network == "TRC20" else "`0x1234…abcd` (42 chars)"
    saved_line = (
        f"\n\n💾 **{s.get('lbl_saved_address', 'Saved Address')}:** `{saved_addr}`\n"
        f"_↑ Tap **Use Saved** or type a new one below._"
    ) if saved_addr else ""
    return (
        f"{_DIV}\n"
        f"![📤](tg://emoji?id=6131886699254388574) **{s.get('wallet_withdraw_title', 'WITHDRAWAL REQUEST')}**\n"
        f"{_DIV}\n\n"
        f"![🌐](tg://emoji?id=6296303781126604562) **{s.get('lbl_network', 'Network')}:** `{network} (USDT)`\n\n"
        f"📬 **{s.get('wit_enter_address', 'Enter your USDT wallet address:')}**"
        f"{saved_line}\n\n"
        f"_Example: {example}_\n"
        f"_{s.get('dep_type_cancel', 'Type /cancel to abort.')}_"
    )


def _withdraw_address_markup(saved_addr, s: dict) -> InlineKeyboardMarkup:
    rows = []
    if saved_addr:
        short = f"{saved_addr[:8]}…{saved_addr[-4:]}"
        rows.append([InlineKeyboardButton(
            f"![✅](tg://emoji?id=6129492160497589882) {s.get('wit_use_saved', 'Use Saved')}: {short}",
            callback_data="wit_use_saved_addr"
        )])
    rows.append([InlineKeyboardButton(
        s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"),
        callback_data="wit_back_to_network"
    )])
    return InlineKeyboardMarkup(rows)


def _withdraw_amount_text(network: str, address: str, s: dict) -> str:
    short_addr = f"{address[:12]}…{address[-6:]}" if len(address) > 18 else address
    return (
        f"{_DIV}\n"
        f"![📤](tg://emoji?id=6131886699254388574) **{s.get('wallet_withdraw_title', 'WITHDRAWAL REQUEST')}**\n"
        f"{_DIV}\n\n"
        f"![🌐](tg://emoji?id=6296303781126604562) **{s.get('lbl_network', 'Network')}:** `{network}`\n"
        f"📬 **{s.get('lbl_address', 'Address')}:** `{short_addr}`\n\n"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('wit_enter_amount', 'Enter the amount (USD) to withdraw:')}**\n\n"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_min_withdrawal', 'Minimum')}:** `${MIN_WITHDRAWAL:.2f} USD`\n"
        f"_{s.get('dep_type_cancel', 'Type /cancel to abort.')}_"
    )


def _withdraw_amount_markup(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"),
            callback_data="wit_back_to_address"
        ),
    ]])


def _withdraw_review_text(data: dict, balance: float, reserve_balance: float, s: dict) -> str:
    """Full confirmation screen shown before the user commits to the withdrawal."""
    amount     = data["amount"]
    network    = data["network"]
    address    = data["address"]
    fee_amount = data.get("fee_amount", 0.0)
    net_amount = data.get("net_amount", amount)
    remaining  = max(0.0, balance - amount)
    short_addr = f"{address[:14]}…{address[-8:]}" if len(address) > 22 else address

    fee_line = (
        f"![💸](tg://emoji?id=6129574787078429498) **{s.get('lbl_fee', 'Network Fee')}:** `${fee_amount:.4f} USD`\n"
    ) if fee_amount > 0 else ""

    return (
        f"{_DIV}\n"
        f"![📤](tg://emoji?id=6131886699254388574) **{s.get('wallet_confirm_title', 'CONFIRM WITHDRAWAL')}**\n"
        f"{_DIV}\n\n"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${amount:.2f} USD`\n"
        f"🪙 **{s.get('lbl_currency', 'Currency')}:** `USDT`\n"
        f"![🌐](tg://emoji?id=6296303781126604562) **{s.get('lbl_network', 'Network')}:** `{network}`\n"
        f"📬 **{s.get('lbl_address', 'Address')}:** `{short_addr}`\n"
        f"{fee_line}"
        f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_net_amount', 'You Receive')}:** `${net_amount:.4f} USDT`\n"
        f"{_DIV}\n"
        f"![💰](tg://emoji?id=6129731974291527294) **{s.get('lbl_balance', 'Current Balance')}:** `${balance:.4f} USD`\n"
        f"![💵](tg://emoji?id=6129731974291527294) **Reserve Balance (Withdrawable):** `${reserve_balance:.4f} USD`\n"
        f"![⏳](tg://emoji?id=6129574787078429498) **{s.get('lbl_after_withdrawal', 'After Withdrawal')}:** `${remaining:.4f} USD`\n"
        f"{_DIV}\n\n"
        f"![⚠️](tg://emoji?id=6129939837823753679) {s.get('wit_confirm_warning', 'Please verify all details carefully. Withdrawals **cannot** be reversed.')}"
    )


def _withdraw_review_markup(s: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"![✅](tg://emoji?id=6129492160497589882) {s.get('btn_confirm_withdrawal', 'Confirm Withdrawal')}",
            callback_data="wit_confirm"
        )],
        [
            InlineKeyboardButton(
                f"![✏️](tg://emoji?id=6129579803600231171) {s.get('btn_edit_amount', 'Edit Amount')}",
                callback_data="wit_edit_amount"
            ),
            InlineKeyboardButton(
                f"![✏️](tg://emoji?id=6129579803600231171) {s.get('btn_edit_address', 'Edit Address')}",
                callback_data="wit_edit_address"
            ),
        ],
        [InlineKeyboardButton(
            f"![❌](tg://emoji?id=6129846551134084367) {s.get('btn_cancel', 'Cancel')}",
            callback_data="wit_cancel"
        )],
    ])


def _wit_status_label(status: str, s: dict) -> str:
    """Human-readable status label."""
    return {
        "pending":              s.get("wit_status_pending",    "⏳ Pending review"),
        "processing":           s.get("wit_status_processing", "⚙️ Processing"),
        "waiting_confirmation": s.get("wit_status_confirming", "🔗 Awaiting blockchain confirmation"),
        "completed":            s.get("wit_status_done",       "✅ Completed"),
        "failed":               s.get("wit_status_failed",     "❌ Failed"),
        "cancelled":            s.get("wit_status_cancelled",  "❌ Cancelled"),
        "rejected":             s.get("wit_status_rejected",   "❌ Rejected"),
        "expired":              s.get("wit_status_expired",    "⏰ Expired"),
    }.get(status, f"Status: {status}")


# ── Withdraw — Start ──────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wallet_withdraw(_profile)?$"))
async def wallet_withdraw_cb(client, cq: CallbackQuery):
    try:
        uid    = cq.from_user.id
        lang   = await get_user_lang(uid)
        s      = get_string(lang)
        origin = "show_profile" if cq.data.endswith("_profile") else "show_wallet"

        if await _feature_enabled("maintenance_mode", default=False):
            await _answer_feature_blocked(cq, s, "maintenance_mode_msg", "🔧 Maintenance mode active.")
            return
        if not await _feature_enabled("withdrawals_enabled", default=True):
            await _answer_feature_blocked(cq, s, "withdrawals_disabled_msg", "🚫 Withdrawals are currently disabled.")
            return

        try:
            from server.plugins.bot._shared_state import clear_pending_flows
            clear_pending_flows(uid, keep="wallet")
            from server.plugins.bot.sell_account import _cleanup_state as _sa_cleanup
            await _sa_cleanup(uid)
        except Exception:
            pass

        _STATE[uid] = {
            "step": "withdraw_pick_network",
            "data": {
                "origin":  origin,
                "chat_id": cq.message.chat.id,
                "msg_id":  cq.message.id,
            },
        }
        await _safe_edit(cq, _withdraw_network_text(s), _withdraw_network_markup(s, back_to=origin))
        await cq.answer()
    except Exception as e:
        _log.error("wallet_withdraw_cb: %s", e, exc_info=True)


@bot.on_message(filters.command("withdraw") & filters.private)
async def withdraw_cmd(client, message: Message):
    uid  = message.from_user.id
    lang = await get_user_lang(uid)
    s    = get_string(lang)

    if await _feature_enabled("maintenance_mode", default=False):
        await _reply_feature_blocked(message, s, "maintenance_mode_msg", "🔧 Maintenance mode active.")
        return
    if not await _feature_enabled("withdrawals_enabled", default=True):
        await _reply_feature_blocked(message, s, "withdrawals_disabled_msg", "🚫 Withdrawals are currently disabled.")
        return

    try:
        from server.plugins.bot._shared_state import clear_pending_flows
        clear_pending_flows(uid, keep="wallet")
    except Exception:
        pass

    sent = await message.reply_text(
        _withdraw_network_text(s),
        reply_markup=_withdraw_network_markup(s),
    )
    _STATE[uid] = {
        "step": "withdraw_pick_network",
        "data": {
            "origin":  "show_wallet",
            "chat_id": sent.chat.id,
            "msg_id":  sent.id,
        },
    }


# ── Network selection ─────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_net_(TRC20|BEP20)$"))
async def wit_network_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        if await _feature_enabled("maintenance_mode", default=False):
            await _answer_feature_blocked(cq, s, "maintenance_mode_msg", "🔧 Maintenance mode active.")
            _STATE.pop(uid, None)
            return
        if not await _feature_enabled("withdrawals_enabled", default=True):
            await _answer_feature_blocked(cq, s, "withdrawals_disabled_msg", "🚫 Withdrawals disabled.")
            _STATE.pop(uid, None)
            return

        state = _STATE.get(uid)
        if not state or state.get("step") != "withdraw_pick_network":
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        network                  = cq.data.replace("wit_net_", "")   # "TRC20" or "BEP20"
        state["data"]["network"] = network
        state["step"]            = "withdraw_enter_address"
        state["data"]["msg_id"]  = cq.message.id
        state["data"]["chat_id"] = cq.message.chat.id

        addrs                        = await get_wallet_addresses(uid)
        saved_addr                   = addrs.get(network.lower())
        state["data"]["saved_addr"]  = saved_addr

        await _safe_edit(
            cq,
            _withdraw_address_text(network, saved_addr, s),
            _withdraw_address_markup(saved_addr, s),
        )
        await cq.answer()
    except Exception as e:
        _log.error("wit_network_cb: %s", e, exc_info=True)


# ── Use saved address ─────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_use_saved_addr$"))
async def wit_use_saved_addr_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        state = _STATE.get(uid)
        if not state or state.get("step") != "withdraw_enter_address":
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        saved_addr = state["data"].get("saved_addr")
        if not saved_addr:
            await cq.answer(
                s.get("err_no_saved_addr", "❌ No saved address. Please type your address."),
                show_alert=True,
            )
            return

        network = state["data"]["network"]
        if _validate_address(network, saved_addr):
            await cq.answer(
                s.get("err_addr_invalid", "❌ Saved address appears invalid. Please type a new one."),
                show_alert=True,
            )
            return

        state["data"]["address"] = saved_addr
        state["step"]            = "withdraw_enter_amount"
        state["data"]["msg_id"]  = cq.message.id

        await _safe_edit(
            cq,
            _withdraw_amount_text(network, saved_addr, s),
            _withdraw_amount_markup(s),
        )
        await cq.answer()
    except Exception as e:
        _log.error("wit_use_saved_addr_cb: %s", e, exc_info=True)


# ── Back navigation ───────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_back_to_network$"))
async def wit_back_to_network_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        state = _STATE.get(uid)
        if not state:
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        origin = state["data"].get("origin", "show_wallet")
        state["step"] = "withdraw_pick_network"
        for k in ("network", "address", "amount", "fee_amount", "net_amount"):
            state["data"].pop(k, None)
        state["data"]["msg_id"] = cq.message.id

        await _safe_edit(cq, _withdraw_network_text(s), _withdraw_network_markup(s, back_to=origin))
        await cq.answer()
    except Exception as e:
        _log.error("wit_back_to_network_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex(r"^wit_back_to_address$"))
async def wit_back_to_address_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        state = _STATE.get(uid)
        if not state:
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        network    = state["data"].get("network", "TRC20")
        saved_addr = state["data"].get("saved_addr")
        state["step"] = "withdraw_enter_address"
        for k in ("address", "amount", "fee_amount", "net_amount"):
            state["data"].pop(k, None)
        state["data"]["msg_id"] = cq.message.id

        await _safe_edit(
            cq,
            _withdraw_address_text(network, saved_addr, s),
            _withdraw_address_markup(saved_addr, s),
        )
        await cq.answer()
    except Exception as e:
        _log.error("wit_back_to_address_cb: %s", e, exc_info=True)


# ── Edit amount / address from the review screen ──────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_edit_amount$"))
async def wit_edit_amount_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        state = _STATE.get(uid)
        if not state or state.get("step") not in ("withdraw_review", "wit_submitting"):
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        state["step"] = "withdraw_enter_amount"
        for k in ("amount", "fee_amount", "net_amount"):
            state["data"].pop(k, None)
        state["data"]["msg_id"] = cq.message.id

        await _safe_edit(
            cq,
            _withdraw_amount_text(state["data"]["network"], state["data"]["address"], s),
            _withdraw_amount_markup(s),
        )
        await cq.answer()
    except Exception as e:
        _log.error("wit_edit_amount_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex(r"^wit_edit_address$"))
async def wit_edit_address_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        state = _STATE.get(uid)
        if not state or state.get("step") not in ("withdraw_review", "wit_submitting"):
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        network    = state["data"].get("network", "TRC20")
        saved_addr = state["data"].get("saved_addr")
        state["step"] = "withdraw_enter_address"
        for k in ("address", "amount", "fee_amount", "net_amount"):
            state["data"].pop(k, None)
        state["data"]["msg_id"] = cq.message.id

        await _safe_edit(
            cq,
            _withdraw_address_text(network, saved_addr, s),
            _withdraw_address_markup(saved_addr, s),
        )
        await cq.answer()
    except Exception as e:
        _log.error("wit_edit_address_cb: %s", e, exc_info=True)


# ── Cancel ────────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_cancel$"))
async def wit_cancel_cb(client, cq: CallbackQuery):
    try:
        uid  = cq.from_user.id
        lang = await get_user_lang(uid)
        s    = get_string(lang)

        state  = _STATE.pop(uid, None)
        origin = (state or {}).get("data", {}).get("origin", "show_wallet")

        await _safe_edit(
            cq,
            f"{_DIV}\n"
            f"![❌](tg://emoji?id=6129846551134084367) **{s.get('wit_cancelled_title', 'WITHDRAWAL CANCELLED')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('wit_cancelled_body', 'Your withdrawal request was cancelled. Your balance was not affected.')}",
            InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                    callback_data=origin
                )
            ]]),
        )
        await cq.answer(s.get("wit_cancelled_alert", "Withdrawal cancelled."))
    except Exception as e:
        _log.error("wit_cancel_cb: %s", e, exc_info=True)


# ── Confirm withdrawal (submit) ───────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_confirm$"))
async def wit_confirm_cb(client, cq: CallbackQuery):
    uid  = cq.from_user.id
    lang = await get_user_lang(uid)
    s    = get_string(lang)
    try:
        state = _STATE.get(uid)
        if not state or state.get("step") != "withdraw_review":
            await cq.answer(s.get("err_session_exp", "Session expired. Start again."), show_alert=True)
            return

        # Anti-double-click guard
        state["step"] = "wit_submitting"
        await cq.answer(s.get("wit_processing_alert", "⏳ Processing…"))

        data    = state["data"]
        amount  = data["amount"]
        network = data["network"]
        address = data["address"]
        origin  = data.get("origin", "show_wallet")
        chat_id = data.get("chat_id", uid)
        msg_id  = data.get("msg_id")

        # Show in-progress message while the gateway call runs
        await _edit_by_ref(
            client, chat_id, msg_id,
            f"{_DIV}\n"
            f"![⏳](tg://emoji?id=6129574787078429498) **{s.get('wit_processing_title', 'PROCESSING WITHDRAWAL')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('wit_processing_body', 'Submitting your withdrawal request… Please wait.')}",
        )

        try:
            from server.services.withdrawal import get_withdrawal_service
            svc    = get_withdrawal_service()
            result = await svc.create_withdrawal(
                user_id=uid,
                amount=amount,
                network=network,
                wallet_address=address,
            )
        except Exception as exc:
            _log.error("wit_confirm_cb: create_withdrawal raised for user %s: %s", uid, exc, exc_info=True)
            _STATE.pop(uid, None)
            await _edit_by_ref(
                client, chat_id, msg_id,
                s.get("err_unexpected", "![❌](tg://emoji?id=6129846551134084367) Unexpected error. Please try again."),
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    s.get("btn_back_wallet", "Back to Wallet"), callback_data=origin
                )]]),
            )
            return

        _STATE.pop(uid, None)

        if not result["ok"]:
            error = result.get("error", "Unknown error.")
            if "balance" in error.lower() or "insufficient" in error.lower():
                reserve_balance = await get_reserve_balance(uid)
                err_text = (
                    s.get(
                        "wallet_insufficient",
                        "![❌](tg://emoji?id=6129846551134084367) **Insufficient balance.**\n\n"
                        "Available: `${0}`\nRequired: `${1}`"
                    ).format(f"{reserve_balance:.4f}", f"{amount:.2f}")
                )
            else:
                err_text = f"![❌](tg://emoji?id=6129846551134084367) {error}"
            await _edit_by_ref(
                client, chat_id, msg_id,
                err_text,
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    s.get("btn_back_wallet", "Back to Wallet"), callback_data=origin
                )]]),
            )
            return

        # ── Success ───────────────────────────────────────────────────────────
        wit_id    = result["withdrawal_id"]
        status    = result.get("status", "pending")
        net_amt   = result.get("net_amount", amount)
        fee       = result.get("fee", 0.0)
        net       = result.get("network", network)
        short_adr = f"{address[:14]}…{address[-8:]}" if len(address) > 22 else address

        status_lbl = _wit_status_label(status, s)
        if fee > 0:
            receive_line = (
                f"\n![💸](tg://emoji?id=6129574787078429498) **{s.get('lbl_fee', 'Fee')}:** `${fee:.4f}`"
                f" → **{s.get('lbl_net_amount', 'You Receive')}:** `${net_amt:.4f} USDT`"
            )
        else:
            receive_line = (
                f"\n![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_net_amount', 'You Receive')}:** `${net_amt:.4f} USDT`"
            )

        success_text = (
            f"{_DIV}\n"
            f"![✅](tg://emoji?id=6129492160497589882) **{s.get('wallet_success_title', 'WITHDRAWAL SUBMITTED!')}**\n"
            f"{_DIV}\n\n"
            f"🆔 **{s.get('lbl_id', 'ID')}:** `{wit_id}`\n"
            f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${amount:.2f} USD`"
            f"{receive_line}\n"
            f"![🌐](tg://emoji?id=6296303781126604562) **{s.get('lbl_network', 'Network')}:** `{net}`\n"
            f"📬 **{s.get('lbl_address', 'Address')}:** `{short_adr}`\n\n"
            f"📊 **{s.get('lbl_status', 'Status')}:** {status_lbl}\n\n"
            f"_{s.get('wit_notify_tip', 'You will be notified when the status changes.')}_"
        )
        await _edit_by_ref(
            client, chat_id, msg_id,
            success_text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🔄 {s.get('btn_check_status', 'Check Status')}",
                    callback_data=f"wit_status_{wit_id}"
                )],
                [InlineKeyboardButton(
                    s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                    callback_data=origin
                )],
            ]),
        )

    except Exception as e:
        _log.error("wit_confirm_cb: %s", e, exc_info=True)
        _STATE.pop(uid, None)
        try:
            await cq.answer(s.get("err_unexpected", "Unexpected error. Please try again."), show_alert=True)
        except Exception:
            pass


# ── Live status check button ──────────────────────────────────────────────────

@bot.on_callback_query(filters.regex(r"^wit_status_WIT-[A-F0-9]+$"))
async def wit_status_cb(client, cq: CallbackQuery):
    try:
        uid    = cq.from_user.id
        lang   = await get_user_lang(uid)
        s      = get_string(lang)
        wit_id = cq.data[len("wit_status_"):]

        await cq.answer(s.get("checking_status", "🔍 Checking status…"))

        from server.utils.database.withdrawaldb import get_withdrawal_record
        from server.utils.withdrawal_statuses import WithdrawalStatus

        wit = await get_withdrawal_record(wit_id)
        if not wit or wit.get("user_id") != uid:
            await cq.answer(s.get("err_not_found", "❌ Withdrawal not found."), show_alert=True)
            return

        status = wit.get("status", "unknown")

        # For in-flight withdrawals that have a gateway track_id, try to refresh
        if status in WithdrawalStatus.IN_FLIGHT and wit.get("gateway_track_id"):
            try:
                from server.services.withdrawal import get_withdrawal_service
                svc    = get_withdrawal_service()
                result = await svc.verify_withdrawal(wit_id)
                status = result.get("status", status)
                wit    = await get_withdrawal_record(wit_id) or wit
            except Exception as ex:
                _log.warning("wit_status_cb verify failed: %s", ex)

        status_lbl = _wit_status_label(status, s)
        amount     = wit.get("amount", 0)
        network    = wit.get("network", "")
        address    = wit.get("wallet_address", "")
        tx_hash    = wit.get("gateway_tx_hash") or ""
        reason     = wit.get("reason") or ""
        net_amount = wit.get("net_amount", amount)
        fee_amount = wit.get("fee_amount", 0.0)
        short_adr  = f"{address[:14]}…{address[-8:]}" if len(address) > 22 else address

        is_final = status in WithdrawalStatus.FINAL

        # Build updated message text
        if fee_amount and fee_amount > 0:
            receive_line = (
                f"\n![💸](tg://emoji?id=6129574787078429498) **{s.get('lbl_fee', 'Fee')}:** `${fee_amount:.4f}`"
                f" → **{s.get('lbl_net_amount', 'You Receive')}:** `${net_amount:.4f} USDT`"
            )
        else:
            receive_line = (
                f"\n![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_net_amount', 'You Receive')}:** `${net_amount:.4f} USDT`"
            )

        extra_lines = ""
        if tx_hash:
            short_tx = f"{tx_hash[:20]}…{tx_hash[-8:]}" if len(tx_hash) > 28 else tx_hash
            extra_lines += f"\n🔗 **TX:** `{short_tx}`"
        if reason and is_final:
            extra_lines += f"\n📝 **{s.get('lbl_reason', 'Reason')}:** {reason}"

        title_emoji = "✅" if not is_final else ("❌" if status in {"failed", "rejected"} else "ℹ️")
        updated_text = (
            f"{_DIV}\n"
            f"![{title_emoji}](tg://emoji?id=6129492160497589882) **{s.get('wallet_success_title', 'WITHDRAWAL SUBMITTED!')}**\n"
            f"{_DIV}\n\n"
            f"🆔 **{s.get('lbl_id', 'ID')}:** `{wit_id}`\n"
            f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${amount:.2f} USD`"
            f"{receive_line}\n"
            f"![🌐](tg://emoji?id=6296303781126604562) **{s.get('lbl_network', 'Network')}:** `{network}`\n"
            f"📬 **{s.get('lbl_address', 'Address')}:** `{short_adr}`\n"
            f"{extra_lines}\n"
            f"📊 **{s.get('lbl_status', 'Status')}:** {status_lbl}\n\n"
            f"_{s.get('wit_notify_tip', 'You will be notified when the status changes.')}_"
        )

        # Build buttons — hide CHECK STATUS if withdrawal is already in a final state
        buttons = []
        if not is_final:
            buttons.append([InlineKeyboardButton(
                f"🔄 {s.get('btn_check_status', 'Check Status')}",
                callback_data=f"wit_status_{wit_id}",
            )])
        buttons.append([InlineKeyboardButton(
            s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
            callback_data="show_wallet",
        )])

        try:
            await cq.message.edit_text(
                updated_text,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception:
            # Message unchanged (same content) — silently ignore Telegram's "not modified" error
            pass

    except Exception as e:
        _log.error("wit_status_cb: %s", e, exc_info=True)
        try:
            await cq.answer("⚠️ Could not fetch status. Try again.", show_alert=True)
        except Exception:
            pass


# ── Text input state machine ──────────────────────────────────────────────────

@bot.on_message(
    filters.private & filters.text
    & ~filters.command(["start", "cancel", "deposit", "withdraw", "setwallet",
                        "sessions_stats", "sessions_list", "witlist", "witapprove", "witreject",
                        "pending_2fa", "2fa_pass", "set_2fa",
                        "gen_fresh_session", "add_proxy", "list_proxies",
                        "del_proxy", "proxy_login"])
)
async def wallet_text_handler(client, message: Message):
    uid   = message.from_user.id
    state = _STATE.get(uid)
    if not state:
        # No wallet flow in progress — pass to other handlers
        await message.continue_propagation()
        return

    lang = await get_user_lang(uid)
    s    = get_string(lang)
    step = state["step"]
    text = message.text.strip()

    # ── Feature guard for deposit steps ──────────────────────────────────────
    if step.startswith("deposit"):
        if await _feature_enabled("maintenance_mode", default=False):
            _STATE.pop(uid, None)
            await _reply_feature_blocked(message, s, "maintenance_mode_msg", "🔧 Maintenance mode active.")
            return
        if not await _feature_enabled("deposits_enabled", default=True):
            _STATE.pop(uid, None)
            await _reply_feature_blocked(message, s, "deposits_disabled_msg", "🚫 Deposits are currently disabled.")
            return

    # ── Feature guard for withdrawal steps ───────────────────────────────────
    if step.startswith("withdraw"):
        if await _feature_enabled("maintenance_mode", default=False):
            _STATE.pop(uid, None)
            await _reply_feature_blocked(message, s, "maintenance_mode_msg", "🔧 Maintenance mode active.")
            return
        if not await _feature_enabled("withdrawals_enabled", default=True):
            _STATE.pop(uid, None)
            await _reply_feature_blocked(message, s, "withdrawals_disabled_msg", "🚫 Withdrawals are currently disabled.")
            return

    # ── Set TRC20 / BEP20 address ─────────────────────────────────────────────
    if step in ("set_trc20", "set_bep20"):
        network = state["data"]["network"]
        err_key = _validate_address(network, text)
        if err_key:
            err_msg = (
                s.get("err_invalid_trc20",
                      "![❌](tg://emoji?id=6129846551134084367) Invalid TRC20 address. Must start with `T` and be 34 characters.")
                if network == "TRC20"
                else s.get("err_invalid_bep20",
                           "![❌](tg://emoji?id=6129846551134084367) Invalid BEP20 address. Must start with `0x` and be 42 characters.")
            )
            await message.reply_text(f"{err_msg}\n\n_{s.get('dep_type_cancel', 'Type /cancel to abort.')}_")
            return

        await set_wallet_address(uid, network, text)
        del _STATE[uid]

        title = s.get("addr_saved_title", "{0} ADDRESS SAVED!").format(network)
        body  = s.get("addr_saved_body", "You can now withdraw USDT to this address.")
        await message.reply_text(
            f"![✅](tg://emoji?id=6129492160497589882) **{title}**\n\n`{text}`\n\n{body}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                    callback_data="show_wallet"
                ),
            ]]),
        )

    # ── Manual USDT chain transaction-hash entry ─────────────────────────────
    elif step == "deposit_chain_hash":
        dep_id = state["data"]["dep_id"]
        network = state["data"]["network"]
        chat_id = state["data"].get("chat_id", uid)
        msg_id = state["data"].get("msg_id")

        try:
            await message.delete()
        except Exception:
            pass

        await _edit_by_ref(
            client, chat_id, msg_id,
            s.get("verifying_payment", "![⏳](tg://emoji?id=6129574787078429498) Verifying payment…"),
        )

        from server.services.deposit.manual_chain import ManualChainError, submit_transaction_hash

        try:
            result = await submit_transaction_hash(dep_id, uid, text)
        except LookupError:
            _STATE.pop(uid, None)
            await _edit_by_ref(
                client, chat_id, msg_id,
                "![❌](tg://emoji?id=6129846551134084367) **Deposit Not Found**\n\n"
                "Create a new payment instruction from your wallet.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"), callback_data="show_wallet"),
                ]]),
            )
            return
        except ManualChainError as exc:
            message_text = str(exc)
            if "expired" in message_text.lower():
                _STATE.pop(uid, None)
            await _edit_by_ref(
                client, chat_id, msg_id,
                f"![❌](tg://emoji?id=6129846551134084367) **Transaction Hash Not Accepted**\n\n"
                f"{message_text}\n\n"
                "_Send a valid transaction hash, or type /cancel to return to your wallet._",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"), callback_data="show_wallet"),
                ]]),
            )
            return
        except RuntimeError:
            await _edit_by_ref(
                client, chat_id, msg_id,
                "![⏳](tg://emoji?id=6129574787078429498) **Verification Temporarily Unavailable**\n\n"
                "Your transaction hash was reserved safely. Please send the same hash again in a minute to retry verification.",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Try Again", callback_data=f"dep_verify_{dep_id}"),
                    InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back"), callback_data="show_wallet"),
                ]]),
            )
            _STATE.pop(uid, None)
            return

        outcome = result.get("outcome")
        verification = result.get("verification") or {}
        if outcome == "completed":
            _STATE.pop(uid, None)
            dep_doc = result.get("deposit") or {}
            try:
                from server.utils.notifications import notify
                await notify(uid, "deposit_confirmed", deposit_id=dep_id, amount=dep_doc.get("amount", 0), method=network)
            except Exception:
                pass
            await _edit_by_ref(
                client, chat_id, msg_id,
                f"{_DIV}\n"
                f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_verified_title', 'PAYMENT CONFIRMED!')}**\n"
                f"{_DIV}\n\n"
                f"🔹 **Network:** `{network}`\n"
                f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${float(dep_doc.get('amount') or 0):.2f} USD`\n"
                f"🆔 **{s.get('lbl_deposit_id', 'Deposit ID')}:** `{dep_id}`\n"
                f"⛓️ **Confirmations:** `{verification.get('confirmations') or 'verified'}`\n\n"
                f"![✅](tg://emoji?id=6129492160497589882) {s.get('dep_balance_credited', 'Your balance has been credited!')}",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"), callback_data="show_wallet"),
                ]]),
            )
            return

        _STATE.pop(uid, None)
        if outcome == "pending":
            code = verification.get("code")
            detail = (
                "The transaction is still propagating or waiting for final confirmations."
                if code in {"transaction_not_found", "awaiting_confirmations"}
                else "The verification provider is temporarily unavailable."
            )
            title = "Verification Pending"
        else:
            detail = "This transaction does not match the selected USDT network, receiving address, token, amount, or success status."
            title = "Transaction Rejected"
        await _edit_by_ref(
            client, chat_id, msg_id,
            f"![⏳](tg://emoji?id=6129574787078429498) **{title}**\n\n"
            f"{detail}\n\n"
            "Use **Try Again** to submit or recheck a transaction hash. Your balance has not been credited.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Try Again", callback_data=f"dep_verify_{dep_id}"),
                InlineKeyboardButton(s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back"), callback_data="show_wallet"),
            ]]),
        )

    # ── Binance Pay (Auto) — Order ID entry ──────────────────────────────────
    elif step == "dep_bptx_order_id":
        order_id = text.strip()
        dep_id   = state["data"]["dep_id"]
        amount   = state["data"]["amount"]
        chat_id  = state["data"].get("chat_id", uid)
        msg_id   = state["data"].get("msg_id")

        if not order_id or len(order_id) < 5:
            await message.reply_text(
                "![❌](tg://emoji?id=6129846551134084367) Please enter a valid Binance Order ID.\n\n"
                "_Type /cancel to go back._"
            )
            return

        try:
            await message.delete()
        except Exception:
            pass

        await _edit_by_ref(
            client, chat_id, msg_id,
            s.get("verifying_payment", "![⏳](tg://emoji?id=6129574787078429498) Verifying payment…"),
        )

        from server.services.deposit.providers.binance_pay_tx import verify_by_order_id
        result = await verify_by_order_id(order_id, amount, dep_id)

        if not result["ok"]:
            err = result.get("error", "Verification failed.")
            _log.warning("dep_bptx_order_id: verification failed uid=%s dep=%s err=%s", uid, dep_id, err)
            await _edit_by_ref(
                client, chat_id, msg_id,
                f"![❌](tg://emoji?id=6129846551134084367) **Verification Failed**\n\n"
                f"{err}\n\n"
                f"_Check your Order ID and try again, or contact support._",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        s.get("btn_verify_payment", "🔍 Try Again"),
                        callback_data=f"dep_verify_{dep_id}",
                    ),
                    InlineKeyboardButton(
                        s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back"),
                        callback_data="show_wallet",
                    ),
                ]]),
            )
            _STATE.pop(uid, None)
            return

        # ── Duplicate protection — claim the Order ID before crediting ────────
        from server.services.deposit.providers.binance_pay_tx import (
            claim_order_id, release_order_id,
        )
        if not await claim_order_id(order_id, dep_id, uid, amount):
            _log.warning("dep_bptx: duplicate Order ID %s blocked (uid=%s dep=%s)", order_id, uid, dep_id)
            await _edit_by_ref(
                client, chat_id, msg_id,
                f"![❌](tg://emoji?id=6129846551134084367) **{s.get('dep_dup_order_title', 'Order ID Already Used')}**\n\n"
                f"{s.get('dep_dup_order_body', 'This Binance Order ID has already been used for another deposit. Each Order ID can only be used once.')}\n\n"
                f"_{s.get('dep_dup_order_hint', 'Please make a new payment or contact support.')}_",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                        callback_data="show_wallet",
                    )
                ]]),
            )
            _STATE.pop(uid, None)
            return

        # ── Payment verified — credit balance ─────────────────────────────────
        from server.utils.database.walletdb import confirm_deposit, depositsdb as _ddb
        try:
            dep_doc = await confirm_deposit(dep_id, confirmed_by=None)
        except Exception:
            await release_order_id(order_id)
            raise
        if not dep_doc:
            await release_order_id(order_id)
        _STATE.pop(uid, None)

        if dep_doc:
            _log.info(
                "dep_bptx: credited $%.4f to user %s (order_id=%s dep=%s)",
                dep_doc["amount"], uid, order_id, dep_id,
            )
            tx_data = result.get("tx", {})
            tx_id   = str(tx_data.get("orderId") or order_id)
            await _ddb.update_one(
                {"deposit_id": dep_id},
                {"$set": {
                    "tx_hash":              tx_id,
                    "extra.tx_hash":        tx_id,
                    "extra.binance_order_id": order_id,
                }},
            )
            try:
                from server.utils.notifications import notify
                await notify(uid, "deposit_confirmed",
                             deposit_id=dep_id,
                             amount=dep_doc["amount"],
                             method="binance_pay_tx")
            except Exception:
                pass
            await _edit_by_ref(
                client, chat_id, msg_id,
                f"{_DIV}\n"
                f"![✅](tg://emoji?id=6129492160497589882) **{s.get('dep_verified_title', 'PAYMENT CONFIRMED!')}**\n"
                f"{_DIV}\n\n"
                f"![💰](tg://emoji?id=6129731974291527294) **{s.get('lbl_method', 'Method')}:** {s.get('binance_pay_auto_label', 'Binance Pay (Auto)')}\n"
                f"![💵](tg://emoji?id=6129731974291527294) **{s.get('lbl_amount', 'Amount')}:** `${dep_doc['amount']:.2f} USD`\n"
                f"🆔 **{s.get('lbl_deposit_id', 'Deposit ID')}:** `{dep_id}`\n"
                f"🔗 **Order ID:** `{order_id}`\n\n"
                f"![✅](tg://emoji?id=6129492160497589882) {s.get('dep_balance_credited', 'Your balance has been credited!')}",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                        callback_data="show_wallet",
                    )
                ]]),
            )
        else:
            await _edit_by_ref(
                client, chat_id, msg_id,
                s.get("dep_already_credited", "![✅](tg://emoji?id=6129492160497589882) Payment already confirmed! Your balance was credited."),
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        s.get("btn_back_wallet", "![💳](tg://emoji?id=6129731974291527294) Back to Wallet"),
                        callback_data="show_wallet",
                    )
                ]]),
            )

    # ── Deposit amount ────────────────────────────────────────────────────────
    elif step == "deposit_amount":
        try:
            amount = float(text)
        except ValueError:
            await message.reply_text(
                s.get("err_invalid_amount",
                      "![❌](tg://emoji?id=6129846551134084367) Please enter a valid number (e.g. `10` or `25.50`)")
            )
            return

        if amount < MIN_DEPOSIT:
            await message.reply_text(
                s.get("err_min_deposit",
                      "![❌](tg://emoji?id=6129846551134084367) Minimum deposit is ${0}. Try again or /cancel.")
                .format(f"{MIN_DEPOSIT:.2f}")
            )
            return

        state["data"]["amount"] = amount

        try:
            await message.delete()
        except Exception:
            pass

        chat_id = state["data"].get("chat_id", uid)
        msg_id  = state["data"].get("msg_id")
        await _edit_by_ref(
            client, chat_id, msg_id,
            s.get("generating_link", "![⏳](tg://emoji?id=6129574787078429498) Generating payment…")
        )
        await _process_deposit(client, uid, message, state, s)

    # ── Withdrawal address entry ──────────────────────────────────────────────
    elif step == "withdraw_enter_address":
        network = state["data"].get("network", "TRC20")
        err_key = _validate_address(network, text)
        if err_key:
            err_msg = (
                s.get("err_invalid_trc20",
                      "![❌](tg://emoji?id=6129846551134084367) Invalid TRC20 address. Must start with `T` and be 34 characters.")
                if network == "TRC20"
                else s.get("err_invalid_bep20",
                           "![❌](tg://emoji?id=6129846551134084367) Invalid BEP20 address. Must start with `0x` and be 42 characters.")
            )
            await message.reply_text(f"{err_msg}\n\n_{s.get('dep_type_cancel', 'Type /cancel to abort.')}_")
            return

        state["data"]["address"] = text
        state["step"]            = "withdraw_enter_amount"

        # Delete user message for cleaner UX
        try:
            await message.delete()
        except Exception:
            pass

        # Edit the stored bot message
        chat_id = state["data"].get("chat_id", uid)
        msg_id  = state["data"].get("msg_id")
        await _edit_by_ref(
            client, chat_id, msg_id,
            _withdraw_amount_text(network, text, s),
            _withdraw_amount_markup(s),
        )

    # ── Withdrawal amount entry ───────────────────────────────────────────────
    elif step == "withdraw_enter_amount":
        try:
            amount = round(float(text), 2)
        except ValueError:
            await message.reply_text(
                s.get("err_invalid_amount",
                      "![❌](tg://emoji?id=6129846551134084367) Please enter a valid number (e.g. `10` or `25.50`)")
            )
            return

        if amount < MIN_WITHDRAWAL:
            await message.reply_text(
                s.get("err_min_withdraw",
                      "![❌](tg://emoji?id=6129846551134084367) Minimum withdrawal is ${0}. Try again or /cancel.")
                .format(f"{MIN_WITHDRAWAL:.2f}")
            )
            return

        import config as _cfg
        if amount > _cfg.WITHDRAWAL_MAX_AMOUNT:
            await message.reply_text(
                s.get("err_max_withdraw",
                      "![❌](tg://emoji?id=6129846551134084367) Maximum withdrawal is ${0}. Try again or /cancel.")
                .format(f"{_cfg.WITHDRAWAL_MAX_AMOUNT:.2f}")
            )
            return

        balance = await get_balance(uid)
        reserve_balance = await get_reserve_balance(uid)
        if amount > reserve_balance:
            await message.reply_text(
                s.get("wallet_insufficient",
                      "![❌](tg://emoji?id=6129846551134084367) **Insufficient withdrawable balance.**\n\n"
                      "Available: `${0}`\nRequested: `${1}`")
                .format(f"{reserve_balance:.4f}", f"{amount:.2f}")
            )
            return

        # Calculate fee preview
        fee_calc = FeeCalculation.calculate(
            amount,
            fee_percent=_cfg.WITHDRAWAL_FEE_PERCENT,
            fee_fixed=_cfg.WITHDRAWAL_FEE_FIXED,
        )

        state["data"]["amount"]     = amount
        state["data"]["fee_amount"] = fee_calc.fee_amount
        state["data"]["net_amount"] = fee_calc.net_amount
        state["step"]               = "withdraw_review"

        # Delete user message
        try:
            await message.delete()
        except Exception:
            pass

        # Edit stored message to show review screen
        chat_id = state["data"].get("chat_id", uid)
        msg_id  = state["data"].get("msg_id")
        await _edit_by_ref(
            client, chat_id, msg_id,
            _withdraw_review_text(state["data"], balance, await get_reserve_balance(uid), s),
            _withdraw_review_markup(s),
        )

    else:
        # Unknown step — pass to other handlers
        await message.continue_propagation()
