

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
    '⚖️': "5316798307014556036",
    '⚠️': "6129939837823753679",
    '✅': "6129492160497589882",
    '✨': "6129801569941592173",
    '❌': "6129846551134084367",
    '❓': "6129472184604695207",
    '⬅️': "6129550284290006595",
    '🆕': "5316993667896981960",
    '🌐': "6296303781126604562",
    '🎁': "6129627894349045589",
    '🏆': "5316883252877738187",
    '👋': "6129627894349045589",
    '👤': "5256143829672672750",
    '👥': "5316979275461573049",
    '💰': "6129731974291527294",
    '💳': "6129731974291527294",
    '💵': "6129731974291527294",
    '💸': "6129731974291527294",
    '💼': "5316561753100792764",
    '📅': "6129574787078429498",
    '📊': "6129870783339567154",
    '📋': "6129579803600231171",
    '📌': "6131886699254388574",
    '📖': "5316561753100792764",
    '📤': "6131886699254388574",
    '📥': "6131886699254388574",
    '📦': "6131886699254388574",
    '🔄': "6129792056589031358",
    '🔑': "6129782440157256336",
    '🔔': "5316921740079675140",
    '🔗': "6129782440157256336",
    '🔴': "6129846551134084367",
    '🚫': "6129846551134084367",
    '🛒': "6131886699254388574",
    '🟢': "6129492160497589882",
    '🧾': "6129579803600231171",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Start plugin — Home panel, Profile, Transactions, Language, API Key, Cancel.
"""

import config
import math
from pyrogram import filters
from pyrogram.types import (
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    WebAppInfo,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from server.utils.bot_utils import Btn as InlineKeyboardButton
from server.utils.msg_builder import MsgBuilder
from server.utils.force_join import is_force_joined, force_join_keyboard, force_join_text

from server import bot, LOGGER
from server.core import memstore
from server.utils.bot_utils import DIV as _DIV, safe_edit as _safe_edit, is_admin as _is_admin
from server.utils.database import (
    add_served_user,
    is_served_user,
    get_user_lang,
    set_user_lang,
    get_api_key,
    get_balance,
    get_reserve_balance,
    get_rank,
    is_banned_user,
    get_user_stats,
    get_referral_stats,
    regenerate_api_key,
    get_user_deposits,
    get_user_withdrawals,
    get_user_transactions,
    get_buyer_orders,
)
from strings import get_string, LANGUAGES
from server.plugins.bot._shared_state import (
    wallet_state,
    market_pending,
    sell_state as _sell_state,
    sell_account_state as _sell_account_state,
    sell_session_state as _sell_session_state,
)

_log = LOGGER(__name__)

def _is_disabled_setting(value) -> bool:
    """Return True when an admin toggle value means disabled/off."""
    if isinstance(value, bool):
        return value is False
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "off", "disabled"}
    return not bool(value)


def _miniapp_enabled() -> bool:
    """Return the admin-controlled visibility flag for Mini App buttons."""
    value = memstore.settings.get("miniapp_enabled", True)
    return not _is_disabled_setting(value)


# ── Home Panel ────────────────────────────────────────────────────────────────

def _home_buttons(s: dict) -> InlineKeyboardMarkup:
    # Design contract: every bot-launched Mini App opens the verified same-server
    # React app at config.WEBAPP_URL; no legacy /webapp/ dashboard is exposed.
    rows = []
    if config.WEBAPP_URL and _miniapp_enabled():
        rows.append([
            # Telegram's native button serialization must remain untouched for
            # WebApp launch metadata and signed initData to be delivered.
            TelegramInlineKeyboardButton(
                s.get("btn_open_tgfox_app", "🦊 Open TgFox App"),
                web_app=WebAppInfo(url=config.WEBAPP_URL),
            ),
        ])

    rows.extend([
        [
            InlineKeyboardButton(s.get("btn_buy_account", "![🟢](tg://emoji?id=6129492160497589882) Buy Account"), callback_data="buy_page_1"),
            InlineKeyboardButton(s.get("btn_sell_account", "![🔴](tg://emoji?id=6129846551134084367) Sell Account"), callback_data="sell_account_start"),
        ],
        [
            InlineKeyboardButton(s.get("btn_sessions_menu", "![📦](tg://emoji?id=6131886699254388574) Sell / Buy Sessions"), callback_data="sessions_menu"),
        ],
        [
            InlineKeyboardButton(s.get("btn_home_deposit", "![💰](tg://emoji?id=6129731974291527294) Deposit"), callback_data="wallet_deposit"),
            InlineKeyboardButton(s.get("btn_home_withdraw_money", "![👤](tg://emoji?id=5316979275461573049) Withdraw"), callback_data="show_wallet"),
        ],
        [
            InlineKeyboardButton(s.get("btn_profile", "![👤](tg://emoji?id=5256143829672672750) Profile"), callback_data="show_profile"),
            InlineKeyboardButton(s.get("btn_extra", "![✨](tg://emoji?id=6129801569941592173) Extra"), callback_data="extra_menu"),
        ],
        [
            InlineKeyboardButton(s.get("btn_updates", "![🧾](tg://emoji?id=6129579803600231171) Purchase Logs"), url=config.UPDATES_CHANNEL),
            InlineKeyboardButton(s.get("btn_support", "![🔔](tg://emoji?id=5316921740079675140) Receive Updates"), url=config.SUPPORT_GROUP),
        ],
        [
            InlineKeyboardButton(s.get("btn_language", "![🌐](tg://emoji?id=5262470999399487110) Language"), callback_data="choose_lang"),
        ],
    ])
    return InlineKeyboardMarkup(rows)


def _home_caption(name: str, user_id: int, balance: float, rank: str, s: dict) -> str:
    return (
        f"> **🦊 {s.get('home_title', 'TG-Fox API')}**\n\n"
        f"{_pe('👋')}{s.get('home_welcome', 'Welcome back, {0}!').format(name)}\n\n"
        f"🆔 `{user_id}`  |  ![🏆](tg://emoji?id=5316883252877738187) **{rank}**\n"
        f"![💰](tg://emoji?id=6129731974291527294) {s.get('lbl_balance', 'Balance')}: **${balance:.2f}**\n\n"
        f"{_DIV}"
    )


async def _render_home(client, user_id: int, mention: str, lang: str = "en") -> tuple[str, InlineKeyboardMarkup]:
    s = get_string(lang)
    balance = await get_balance(user_id)
    rank = await get_rank(user_id)
    caption = _home_caption(mention, user_id, balance, rank, s)
    return caption, _home_buttons(s)


# ── Admin: adjust withdrawable reserve ───────────────────────────────────────
# Usage: /adjustreserve <user_id> <amount>
# Positive amount credits Balance + Reserve; negative amount debits both.
@bot.on_message(filters.command(["adjustreserve", "adjust_reserve"]) & filters.private)
async def adjust_reserve_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if len(args) != 2:
        await message.reply_text(
            "❌ Usage: `/adjustreserve <user_id> <amount>`\n\n"
            "Example: `/adjustreserve 123456789 5`\n"
            "Remove: `/adjustreserve 123456789 -2`"
        )
        return

    try:
        target_id = int(args[0])
        amount = float(args[1])
    except ValueError:
        await message.reply_text("❌ User ID and amount must be valid numbers.")
        return

    if target_id <= 0:
        await message.reply_text("❌ Invalid user ID.")
        return
    if not math.isfinite(amount) or amount == 0:
        await message.reply_text("❌ Amount must be a non-zero finite number.")
        return

    try:
        from server.utils.database import adjust_reserve_balance
        ok, new_balance, new_reserve = await adjust_reserve_balance(target_id, amount)
    except Exception as exc:
        _log.error("adjustreserve failed for %s: %s", target_id, exc, exc_info=True)
        await message.reply_text("❌ Adjustment failed. Nothing was changed.")
        return

    if not ok:
        if amount < 0:
            await message.reply_text(
                "❌ Adjustment failed. The user does not have enough Balance/Reserve "
                "for this deduction, or the user does not exist."
            )
        else:
            await message.reply_text("❌ User not found or adjustment could not be applied.")
        return

    sign = "+" if amount > 0 else ""
    await message.reply_text(
        "✅ **Reserve Balance Updated**\n\n"
        f"👤 User: `{target_id}`\n"
        f"💵 Adjustment: `{sign}${amount:.4f}`\n"
        f"💰 Balance: `${new_balance:.4f}`\n"
        f"📌 Reserve Balance: `${new_reserve:.4f}`"
    )


# ── /start ────────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("start") & filters.private)
async def start_pm(client, message: Message):
    try:
        user = message.from_user

        if await is_banned_user(user.id):
            s = get_string("en")
            await message.reply_text(s.get("banned_msg", "![🚫](tg://emoji?id=6129846551134084367) You are banned from using this bot."))
            return

        ref_code = message.command[1] if len(message.command) > 1 else None

        # ── Registration toggle gate ─────────────────────────────────────
        # If admin disabled new user registration, block brand-new users only.
        # Existing users continue to work normally.
        try:
            from server.utils.database.configdb import get_setting as _get_setting
            _reg_enabled = await _get_setting("registration_enabled")
        except Exception:
            _reg_enabled = True
        if _is_disabled_setting(_reg_enabled):
            try:
                _already = await is_served_user(user.id)
            except Exception:
                _already = True  # fail-open for existing users
            if not _already:
                _lang_guess = getattr(user, "language_code", "en") or "en"
                _lang_guess = _lang_guess.split("-")[0].lower()
                s = get_string(_lang_guess if _lang_guess in LANGUAGES else "en")
                _txt = s.get(
                    "registration_disabled_msg",
                    "![🚫](tg://emoji?id=6129846551134084367) **New user registration is currently disabled.**\n\n"
                    "Please try again after some time. Whenever new user registration is re-enabled, you will be able to start the bot.\n\n"
                    "If you have any doubt or issue, please contact support.",
                )
                _support_btn_label = s.get("btn_contact_support", "![🔔](tg://emoji?id=5316921740079675140) Contact Support")
                try:
                    _support_url = config.support_link()
                except Exception:
                    _support_url = config.SUPPORT_GROUP
                await message.reply_text(
                    _txt,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(_support_btn_label, url=_support_url)],
                    ]),
                )
                return

        await add_served_user(
            user_id=user.id,
            username=user.username,
            full_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
        )

        if ref_code and ref_code.startswith("ref_"):
            try:
                from server.utils.database import apply_referral
                await apply_referral(user.id, ref_code.replace("ref_", ""))
            except Exception as ref_exc:
                _log.warning("apply_referral failed for user %s ref=%s: %s", user.id, ref_code, ref_exc)

        # ── Force Join gate ────────────────────────────────────────────────
        if not await is_force_joined(client, user.id):
            _target = ref_code or ""
            _verify_cb = "forcejoin_verify" + (f":{_target}" if _target else "")
            await message.reply_text(
                force_join_text(),
                reply_markup=force_join_keyboard(_verify_cb),
            )
            return

        # Mini App seller control center uses this explicit deep-link to enter
        # the existing phone/OTP/2FA bot workflow. It is not a referral code.
        if ref_code == "sell":
            try:
                from server.plugins.bot.sell_account import begin_sell_account_flow
                sell_text, sell_keyboard = await begin_sell_account_flow(user.id)
                await message.reply_text(text=sell_text, reply_markup=sell_keyboard)
                return
            except Exception as sell_exc:
                _log.error("sell deep-link failed for user %s: %s", user.id, sell_exc, exc_info=True)

        # Deep-link: t.me/<bot>?start=avail_list → send the "available countries" summary.
        if ref_code == "avail_list":
            try:
                from pyrogram import enums as _enums
                from server.plugins.bot.market import _build_available_countries_message
                _msg = await _build_available_countries_message()
                await message.reply_text(_msg, parse_mode=_enums.ParseMode.HTML, disable_web_page_preview=True)
                return
            except Exception as _av_exc:
                _log.error("avail_list deep-link failed: %s", _av_exc, exc_info=True)

        lang = await get_user_lang(user.id)
        caption, buttons = await _render_home(client, user.id, user.mention, lang)
        await message.reply_text(text=caption, reply_markup=buttons)

        if config.LOG_GROUP_ID:
            try:
                username = f"@{user.username}" if user.username else "No Username"
                rank = await get_rank(user.id)
                await bot.send_message(
                    chat_id=config.LOG_GROUP_ID,
                    text=(
                        "![🆕](tg://emoji?id=5316993667896981960) **New User Started The Bot**\n\n"
                        f"![👤](tg://emoji?id=5316979275461573049) **Name:** {user.mention}\n"
                        f"🆔 **User ID:** `{user.id}`\n"
                        f"![🌐](tg://emoji?id=6296303781126604562) **Username:** {username}\n"
                        f"![🔗](tg://emoji?id=6129782440157256336) **Ref:** `{ref_code or 'None'}`\n"
                        f"![🏆](tg://emoji?id=5316883252877738187) **Rank:** {rank}"
                    ),
                )
            except Exception as e:
                _log.warning("Log group error: %s", e)

    except Exception as e:
        _log.error("start_pm: %s", e, exc_info=True)


# ── Force Join verification ─────────────────────────────────────────────────
@bot.on_callback_query(filters.regex(r"^forcejoin_verify(?::(.*))?$"))
async def forcejoin_verify_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        if not await is_force_joined(client, user_id):
            await cq.answer("Please join both the channel and group first.", show_alert=True)
            try:
                await _safe_edit(
                    cq,
                    force_join_text(),
                    force_join_keyboard(cq.data or "forcejoin_verify"),
                )
            except Exception:
                pass
            return

        # Verification succeeded: remove the gate message so the chat stays clean.
        try:
            if cq.message:
                await cq.message.delete()
        except Exception as exc:
            _log.debug("forcejoin gate message delete failed for %s: %s", user_id, exc)

        target = ""
        try:
            target = (cq.data or "").split(":", 1)[1]
        except IndexError:
            pass

        if target == "sell":
            try:
                from server.plugins.bot.sell_account import begin_sell_account_flow
                text, keyboard = await begin_sell_account_flow(user_id)
                await client.send_message(user_id, text=text, reply_markup=keyboard)
                await cq.answer("Verified successfully.")
                return
            except Exception as exc:
                _log.error("forcejoin sell deep-link failed for %s: %s", user_id, exc, exc_info=True)

        if target == "avail_list":
            try:
                from pyrogram import enums as _enums
                from server.plugins.bot.market import _build_available_countries_message
                text = await _build_available_countries_message()
                await client.send_message(
                    user_id,
                    text,
                    parse_mode=_enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                await cq.answer("Verified successfully.")
                return
            except Exception as exc:
                _log.error("forcejoin avail_list deep-link failed for %s: %s", user_id, exc, exc_info=True)

        lang = await get_user_lang(user_id)
        mention = cq.from_user.mention
        caption, buttons = await _render_home(client, user_id, mention, lang)
        await client.send_message(user_id, text=caption, reply_markup=buttons)
        await cq.answer("Verified successfully.")
    except Exception as exc:
        _log.error("forcejoin_verify_cb: %s", exc, exc_info=True)
        try:
            await cq.answer("Verification failed. Please try again.", show_alert=True)
        except Exception:
            pass


# ── Back to Home ──────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^(back_home|back_start)$"))
async def back_home_cb(client, cq: CallbackQuery):
    try:
        user = cq.from_user
        try:
            from server.plugins.bot._shared_state import clear_pending_flows
            clear_pending_flows(user.id)
            from server.plugins.bot.sell_account import _cleanup_state as _sa_cleanup
            await _sa_cleanup(user.id)
        except Exception:
            pass
        lang = await get_user_lang(user.id)
        caption, buttons = await _render_home(client, user.id, user.mention, lang)
        await _safe_edit(cq, caption, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("back_home_cb: %s", e, exc_info=True)


# ── Sessions submenu ──────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^sessions_menu$"))
async def sessions_menu_cb(client, cq: CallbackQuery):
    try:
        if not await is_force_joined(client, cq.from_user.id):
            await _safe_edit(cq, force_join_text(), force_join_keyboard("forcejoin_verify"))
            await cq.answer()
            return
        await _safe_edit(
            cq,
            "![📦](tg://emoji?id=6131886699254388574) **Sessions**\n\nChoose an option:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("![📥](tg://emoji?id=6131886699254388574) Buy Session", callback_data="buy_session_page_1")],
                [InlineKeyboardButton("![📤](tg://emoji?id=6131886699254388574) Sell Session", callback_data="sell_session_start")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_home")],
            ]),
        )
        await cq.answer()
    except Exception as e:
        _log.error("sessions_menu_cb: %s", e, exc_info=True)


# ── noop ──────────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^noop$"))
async def noop_cb(client, cq: CallbackQuery):
    await cq.answer()


# ── Extra submenu ─────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^extra_menu$"))
async def extra_menu_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        if not await is_force_joined(client, user_id):
            await _safe_edit(cq, force_join_text(), force_join_keyboard("forcejoin_verify"))
            await cq.answer()
            return
        lang = await get_user_lang(user_id)
        s = get_string(lang)
        text = (
            f"{_DIV}\n"
            f"![✨](tg://emoji?id=6129801569941592173) **{s.get('extra_title', 'EXTRA')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('extra_subtitle', 'Choose an option:')}"
        )
        # User Dashboard opens the same verified Mini App as the Telegram menu
        # and the /start home panel. Never route to the legacy /webapp/ dashboard.
        _dashboard_row = []
        try:
            import config as _cfg
            _webapp_url = (getattr(_cfg, "WEBAPP_URL", "") or "").strip()
        except Exception:
            _webapp_url = ""
        if _webapp_url and _miniapp_enabled():
            _dashboard_row = [[TelegramInlineKeyboardButton(
                s.get("btn_user_dashboard", "🎛 User Dashboard"),
                web_app=WebAppInfo(url=_webapp_url)
            )]]

        buttons = InlineKeyboardMarkup(_dashboard_row + [
            [InlineKeyboardButton(s.get("btn_apikey", "![🔑](tg://emoji?id=6129782440157256336) API Key"), callback_data="show_apikey")],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("extra_menu_cb: %s", e, exc_info=True)



# ── Profile ───────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^show_profile$"))
async def show_profile_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        from server.plugins.bot._shared_state import wallet_state as _wstate
        _wstate.pop(user_id, None)  # backing out of any in-progress deposit/withdraw clears its state
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        stats = await get_user_stats(user_id)
        ref = await get_referral_stats(user_id)

        joined = stats.get("joined_at")
        joined_str = joined.strftime("%d %b %Y") if joined else "N/A"
        username = cq.from_user.username
        uname_str = f"@{username}" if username else s.get("lbl_no_username", "No Username")

        ref_link = f"https://t.me/{(await client.get_me()).username}?start=ref_{ref.get('code', '')}"
        tap = s.get("lbl_tap_to_copy", "Tap to copy")

        b = MsgBuilder()
        D = _DIV

        # ── Section 1: Identity ───────────────────────────────────────────────
        b.t(f"{D}\n").bold(f"![👤](tg://emoji?id=5316979275461573049) {s.get('profile_title', 'YOUR PROFILE')}").t(f"\n{D}\n\n")
        _bq = b.bq_start()
        b.bold(f"🆔 {s.get('lbl_user_id', 'User ID')}: ").code(str(user_id)).t("\n")
        b.bold(f"![👤](tg://emoji?id=5316979275461573049) {s.get('lbl_username', 'Username')}: ").t(uname_str).t("\n")
        b.bold(f"![🏆](tg://emoji?id=5316883252877738187) {s.get('lbl_rank', 'Rank')}: ").code(str(stats.get('rank', 'VIP1'))).t("\n")
        b.bold(f"![📅](tg://emoji?id=6129574787078429498) {s.get('lbl_member_since', 'Member Since')}: ").t(joined_str)
        b.bq_end(_bq)

        b.t("\n\n")

        # ── Section 2: Wallet ─────────────────────────────────────────────────
        b.t(f"{D}\n").bold(f"![💰](tg://emoji?id=6129731974291527294) {s.get('wallet_title_section', 'WALLET')}").t(f"\n{D}\n\n")
        _bq = b.bq_start()
        b.bold(f"![💳](tg://emoji?id=6129731974291527294) {s.get('lbl_balance', 'Balance')}: ").code(f"${stats.get('balance', 0):.4f}").t("\n")
        b.bold("![💵](tg://emoji?id=6129731974291527294) Reserve Balance (Withdrawable): ").code(f"${stats.get('reserve_balance', 0):.4f}").t("\n")
        b.bold(f"![📥](tg://emoji?id=6131886699254388574) {s.get('lbl_total_deposited', 'Total Deposited')}: ").code(f"${stats.get('total_deposit', 0):.2f}").t("\n")
        b.bold(f"![💸](tg://emoji?id=6129731974291527294) {s.get('lbl_total_spent', 'Total Spent')}: ").code(f"${stats.get('total_spend', 0):.2f}").t("\n")
        b.bold(f"![💵](tg://emoji?id=6129731974291527294) {s.get('lbl_total_earned', 'Total Earned')}: ").code(f"${stats.get('total_earn', 0):.2f}")
        b.bq_end(_bq)

        b.t("\n\n")

        # ── Section 3: Trade Stats ────────────────────────────────────────────
        b.t(f"{D}\n").bold(f"![📊](tg://emoji?id=6129870783339567154) {s.get('trade_title', 'TRADE STATS')}").t(f"\n{D}\n\n")
        _bq = b.bq_start()
        b.bold(f"![🛒](tg://emoji?id=6131886699254388574) {s.get('lbl_accounts_bought', 'Accounts Bought')}: ").code(str(stats.get('total_account_buy', 0))).t("\n")
        b.bold(f"![💼](tg://emoji?id=5316561753100792764) {s.get('lbl_accounts_sold', 'Accounts Sold')}: ").code(str(stats.get('total_account_sell', 0)))
        b.bq_end(_bq)

        b.t("\n\n")

        # ── Section 4: Referrals ──────────────────────────────────────────────
        b.t(f"{D}\n").bold(f"![👥](tg://emoji?id=5316979275461573049) {s.get('ref_title_section', 'REFERRALS')}").t(f"\n{D}\n\n")
        _bq = b.bq_start()
        b.bold(f"![🔗](tg://emoji?id=6129782440157256336) {s.get('lbl_ref_link', 'Your Link')}: ").link(tap, ref_link).t("\n")
        b.bold(f"![👤](tg://emoji?id=5316979275461573049) {s.get('lbl_referred_users', 'Referred Users')}: ").code(str(ref.get('count', 0))).t("\n")
        b.bold(f"![🎁](tg://emoji?id=6129627894349045589) {s.get('lbl_bonus_earned', 'Bonus Earned')}: ").code(f"${ref.get('earnings', 0):.2f}")
        b.bq_end(_bq)

        text, entities = b.build()

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(s.get("btn_deposit", "![💰](tg://emoji?id=6129731974291527294) Deposit"), callback_data="wallet_deposit_profile"),
                InlineKeyboardButton(s.get("btn_withdraw", "![💸](tg://emoji?id=6129731974291527294) Withdraw"), callback_data="wallet_withdraw_profile"),
            ],
            [
                InlineKeyboardButton(s.get("btn_refer_earn", "![👥](tg://emoji?id=5316979275461573049) Refer & Earn"), callback_data="show_refer"),
                InlineKeyboardButton(s.get("btn_transactions", "📜 Transactions"), callback_data="show_transactions"),
            ],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")],
        ])
        await _safe_edit(cq, text, buttons, entities=entities)
        await cq.answer()
    except Exception as e:
        _log.error("show_profile_cb: %s", e, exc_info=True)


# ── Refer & Earn ──────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^show_refer$"))
async def show_refer_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        ref = await get_referral_stats(user_id)
        me = await client.get_me()
        ref_link = f"https://t.me/{me.username}?start=ref_{ref.get('code', '')}"
        ref_earnings = f"{ref.get('earnings', 0):.2f}"
        ref_count = ref.get('count', 0)

        text = (
            f"{_DIV}\n"
            f"![👥](tg://emoji?id=5316979275461573049) **{s.get('refer_title', 'REFER & EARN')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('refer_earn_line', '![🎁](tg://emoji?id=6129627894349045589) **Earn rewards for every friend you refer!**')}\n\n"
            f"{s.get('refer_link_lbl', '![🔗](tg://emoji?id=6129782440157256336) **Your referral link:**')}\n`{ref_link}`\n\n"
            f"{s.get('refer_total_lbl', '![👤](tg://emoji?id=5316979275461573049) **Total referred:** `{0}` users').format(ref_count)}\n"
            f"{s.get('refer_bonus_lbl', '![💰](tg://emoji?id=6129731974291527294) **Bonus earned:** `${0}`').format(ref_earnings)}\n\n"
            f"{_DIV}\n"
            f"{s.get('refer_tip', '![📌](tg://emoji?id=6131886699254388574) Share your link with friends. You earn a bonus when they make their first deposit!')}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back_profile", "![⬅️](tg://emoji?id=5258236805890710909) Back to Profile"), callback_data="show_profile")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("show_refer_cb: %s", e, exc_info=True)


# ── Transactions ──────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^show_transactions$"))
async def show_transactions_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        text = (
            f"{_DIV}\n"
            f"📜 **{s.get('tx_title', 'TRANSACTION HISTORY')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('tx_subtitle', 'View your complete financial history.\nChoose a category below 👇')}"
        )
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(s.get("btn_deposits", "![📥](tg://emoji?id=6131886699254388574) Deposits"), callback_data="tx_deposits"),
                InlineKeyboardButton(s.get("btn_withdrawals", "![📤](tg://emoji?id=6131886699254388574) Withdrawals"), callback_data="tx_withdrawals"),
            ],
            [
                InlineKeyboardButton(s.get("btn_purchases", "![🛒](tg://emoji?id=6131886699254388574) Purchases"), callback_data="tx_purchases"),
                InlineKeyboardButton(s.get("btn_all_history", "![📋](tg://emoji?id=6129579803600231171) All History"), callback_data="tx_all"),
            ],
            [InlineKeyboardButton(s.get("btn_back_profile", "![⬅️](tg://emoji?id=5258236805890710909) Back to Profile"), callback_data="show_profile")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("show_transactions_cb: %s", e, exc_info=True)


def _fmt_date(dt) -> str:
    try:
        return dt.strftime("%d %b %Y")
    except Exception:
        return "N/A"


@bot.on_callback_query(filters.regex("^tx_deposits$"))
async def tx_deposits_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        deps = await get_user_deposits(user_id)
        deps = (deps or [])[-15:]

        if not deps:
            history = s.get("tx_empty_dep", "📭 No deposits yet.")
        else:
            lines = []
            for d in reversed(deps):
                icon = {"completed": "![✅](tg://emoji?id=6129492160497589882)", "pending": "![⏳](tg://emoji?id=6129574787078429498)", "expired": "🕐", "failed": "![❌](tg://emoji?id=6129846551134084367)"}.get(
                    d.get("status", ""), "![❓](tg://emoji?id=6129472184604695207)"
                )
                lines.append(
                    f"{icon} `${d.get('amount', 0):.2f}` "
                    f"via **{d.get('method', 'N/A').upper()}** "
                    f"— {_fmt_date(d.get('created_at'))}"
                )
            history = "\n".join(lines)

        text = (
            f"{_DIV}\n"
            f"![📥](tg://emoji?id=6131886699254388574) **{s.get('tx_dep_title', 'DEPOSIT HISTORY')}**\n"
            f"{_DIV}\n\n"
            f"{history}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="show_transactions")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("tx_deposits_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^tx_withdrawals$"))
async def tx_withdrawals_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        wits = await get_user_withdrawals(user_id)
        wits = (wits or [])[-15:]

        if not wits:
            history = s.get("tx_empty_wit", "📭 No withdrawals yet.")
        else:
            lines = []
            for w in reversed(wits):
                icon = {"completed": "![✅](tg://emoji?id=6129492160497589882)", "pending": "![⏳](tg://emoji?id=6129574787078429498)", "processing": "![🔄](tg://emoji?id=6129792056589031358)", "rejected": "![❌](tg://emoji?id=6129846551134084367)"}.get(
                    w.get("status", ""), "![❓](tg://emoji?id=6129472184604695207)"
                )
                lines.append(
                    f"{icon} `${w.get('amount', 0):.2f}` "
                    f"via **{w.get('method', 'N/A')}** "
                    f"— {_fmt_date(w.get('created_at'))}"
                )
            history = "\n".join(lines)

        text = (
            f"{_DIV}\n"
            f"![📤](tg://emoji?id=6131886699254388574) **{s.get('tx_wit_title', 'WITHDRAWAL HISTORY')}**\n"
            f"{_DIV}\n\n"
            f"{history}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="show_transactions")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("tx_withdrawals_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^tx_purchases$"))
async def tx_purchases_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        orders = await get_buyer_orders(user_id)
        orders = (orders or [])[-15:]

        if not orders:
            history = s.get("tx_empty_pur", "📭 No purchases yet.")
        else:
            lines = []
            for o in reversed(orders):
                icon = {"completed": "![✅](tg://emoji?id=6129492160497589882)", "pending": "![⏳](tg://emoji?id=6129574787078429498)", "cancelled": "![❌](tg://emoji?id=6129846551134084367)", "refunded": "![🔄](tg://emoji?id=6129792056589031358)"}.get(
                    o.get("status", ""), "![❓](tg://emoji?id=6129472184604695207)"
                )
                cc = o.get("country_code", "??")
                name = o.get("country_name", cc)
                lines.append(
                    f"{icon} **{name}** `{cc}` — "
                    f"`${o.get('price_paid', 0):.2f}` "
                    f"— {_fmt_date(o.get('created_at'))}"
                )
            history = "\n".join(lines)

        text = (
            f"{_DIV}\n"
            f"![🛒](tg://emoji?id=6131886699254388574) **{s.get('tx_pur_title', 'PURCHASE HISTORY')}**\n"
            f"{_DIV}\n\n"
            f"{history}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="show_transactions")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("tx_purchases_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^tx_all$"))
async def tx_all_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        txns = await get_user_transactions(user_id)
        txns = (txns or [])[-15:]

        if not txns:
            history = s.get("tx_empty_all", "📭 No transactions yet.")
        else:
            type_icons = {
                "deposit": "![📥](tg://emoji?id=6131886699254388574)", "withdrawal": "![📤](tg://emoji?id=6131886699254388574)", "purchase": "![🛒](tg://emoji?id=6131886699254388574)",
                "sale": "![💼](tg://emoji?id=5316561753100792764)", "refund": "![🔄](tg://emoji?id=6129792056589031358)", "bonus": "![🎁](tg://emoji?id=6129627894349045589)",
                "fee": "![💸](tg://emoji?id=6129731974291527294)", "adjustment": "![⚖️](tg://emoji?id=5316798307014556036)",
            }
            lines = []
            for t in reversed(txns):
                txn_type = t.get("type", "")
                icon = type_icons.get(txn_type, "![💰](tg://emoji?id=6129731974291527294)")
                amount = t.get("amount", 0)
                sign = "+" if amount >= 0 else ""
                lines.append(
                    f"{icon} `{sign}${abs(amount):.2f}` "
                    f"**{txn_type.capitalize()}** "
                    f"— {_fmt_date(t.get('created_at'))}"
                )
            history = "\n".join(lines)

        text = (
            f"{_DIV}\n"
            f"![📋](tg://emoji?id=6129579803600231171) **{s.get('tx_all_title', 'COMPLETE HISTORY')}**\n"
            f"{_DIV}\n\n"
            f"{history}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="show_transactions")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("tx_all_cb: %s", e, exc_info=True)


# ── API Key ───────────────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^show_apikey$"))
async def show_apikey_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        api_key = await get_api_key(user_id)

        text = (
            f"{_DIV}\n"
            f"![🔑](tg://emoji?id=6129782440157256336) **{s.get('apikey_title', 'YOUR API KEY')}**\n"
            f"{_DIV}\n\n"
            f"`{api_key}`\n\n"
            f"{s.get('apikey_header_fmt', '![📌](tg://emoji?id=6131886699254388574) **Header format:**\n`X-Api-Key: tg_{user_id}_{secret}`')}\n\n"
            f"{s.get('apikey_docs', '![📖](tg://emoji?id=5316561753100792764) **Docs:** `/docs`')}\n\n"
            f"{s.get('apikey_warn', '![⚠️](tg://emoji?id=6129939837823753679) Never share your API key with anyone!')}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_regen_key", "![🔄](tg://emoji?id=6129792056589031358) Regenerate Key"), callback_data="regen_apikey")],
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer()
    except Exception as e:
        _log.error("show_apikey_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^regen_apikey$"))
async def regen_apikey_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        lang = await get_user_lang(user_id)
        s = get_string(lang)

        new_key = await regenerate_api_key(user_id)

        text = (
            f"{_DIV}\n"
            f"![✅](tg://emoji?id=6129492160497589882) **{s.get('apikey_regen_title', 'API KEY REGENERATED')}**\n"
            f"{_DIV}\n\n"
            f"`{new_key}`\n\n"
            f"{s.get('apikey_regen_warn', '![⚠️](tg://emoji?id=6129939837823753679) Your old key is now invalid.\nUpdate all your integrations.')}"
        )
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")],
        ])
        await _safe_edit(cq, text, buttons)
        await cq.answer(s.get("apikey_regen_alert", "![✅](tg://emoji?id=6129492160497589882) New key generated!"), show_alert=True)
    except Exception as e:
        _log.error("regen_apikey_cb: %s", e, exc_info=True)


# ── Language Selection ─────────────────────────────────────────────────────────

@bot.on_callback_query(filters.regex("^choose_lang$"))
async def choose_lang_cb(client, cq: CallbackQuery):
    try:
        user_id = cq.from_user.id
        current_lang = await get_user_lang(user_id)
        s = get_string(current_lang)

        lang_list = list(LANGUAGES.items())
        rows = []
        for i in range(0, len(lang_list), 2):
            row = []
            for code, name in lang_list[i:i + 2]:
                label = f"![✅](tg://emoji?id=6129492160497589882) {name}" if code == current_lang else name
                row.append(InlineKeyboardButton(label, callback_data=f"set_lang_{code}"))
            rows.append(row)
        rows.append([InlineKeyboardButton(s.get("btn_back", "![⬅️](tg://emoji?id=5258236805890710909) Back"), callback_data="back_home")])

        text = (
            f"{_DIV}\n"
            f"![🌐](tg://emoji?id=5262470999399487110) **{s.get('lang_title', 'SELECT LANGUAGE')}**\n"
            f"{_DIV}\n\n"
            f"{s.get('lang_subtitle', 'Choose your preferred language:')}"
        )
        await _safe_edit(cq, text, InlineKeyboardMarkup(rows))
        await cq.answer()
    except Exception as e:
        _log.error("choose_lang_cb: %s", e, exc_info=True)


@bot.on_callback_query(filters.regex("^set_lang_"))
async def set_lang_cb(client, cq: CallbackQuery):
    try:
        lang_code = cq.data.replace("set_lang_", "")
        if lang_code not in LANGUAGES:
            await cq.answer("Invalid language!", show_alert=True)
            return

        await set_user_lang(cq.from_user.id, lang_code)
        s = get_string(lang_code)

        user = cq.from_user
        caption, buttons = await _render_home(client, user.id, user.mention, lang_code)
        await _safe_edit(cq, caption, buttons)
        await cq.answer(s.get("lang_changed_alert", "![✅](tg://emoji?id=6129492160497589882) Language updated!"), show_alert=True)
    except Exception as e:
        _log.error("set_lang_cb: %s", e, exc_info=True)


# ── /cancel ────────────────────────────────────────────────────────────────────

@bot.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(client, message: Message):
    uid = message.from_user.id
    lang = await get_user_lang(uid)
    s = get_string(lang)

    cleared = False
    if uid in wallet_state:
        del wallet_state[uid]
        cleared = True
    if uid in market_pending:
        del market_pending[uid]
        cleared = True
    if uid in _sell_state:
        del _sell_state[uid]
        cleared = True

    # New sell account / sell session flows
    sa_state = _sell_account_state.pop(uid, None)
    if sa_state:
        cleared = True
        # Disconnect lingering Telethon client (if user cancelled mid-login)
        tg_client = sa_state.get("client")
        tmp_path   = sa_state.get("tmp_path")
        if tg_client:
            try:
                await tg_client.disconnect()
            except Exception:
                pass
        if tmp_path:
            try:
                from server.utils.sessions.telethon_client import cleanup_session_files
                await cleanup_session_files(tmp_path)
            except Exception:
                pass

    if _sell_session_state.pop(uid, None):
        cleared = True

    if cleared:
        await message.reply_text(s.get("cancel_success", "![✅](tg://emoji?id=6129492160497589882) Cancelled. Use /start to return to the main menu."))
    else:
        await message.reply_text(s.get("cancel_nothing", "Nothing to cancel."))
