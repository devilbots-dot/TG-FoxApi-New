

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
    '❌': "6129846551134084367",
    '🌍': "6296303781126604562",
    '🌐': "6296303781126604562",
    '👍': "6129492160497589882",
    '💡': "6129700535130922338",
    '📊': "6129870783339567154",
    '📋': "6129579803600231171",
    '📥': "6131886699254388574",
    '📦': "6131886699254388574",
    '📱': "5330237710655306682",
    '🔁': "6129792056589031358",
    '🔍': "5316722951813346475",
    '🔐': "6129550284290006595",
    '🔑': "6129782440157256336",
    '🔴': "6129846551134084367",
    '🚀': "6129792056589031358",
    '🚫': "6129846551134084367",
    '🟠': "6129939837823753679",
    '🟡': "6129939837823753679",
    '🟢': "6129492160497589882",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Admin plugin — Production Session Processing Pipeline

Thin Telegram-facing layer: every actual pipeline step (proxy login,
verification, fresh session generation, 2FA, termination, persistence)
lives in `server.stock.*` / `server.utils.sessions.*`. This file only
handles Telegram I/O — commands, buttons, status-message formatting — and
the two admin-input "pause" states (missing/wrong 2FA password, unknown
country) via `PendingStockStore`.

Commands:
  Send .zip or .session file  → asks Enable/Disable 2FA for this batch FIRST,
                                 then runs the full pipeline (proxy login,
                                 fresh session, 2FA applied in the SAME
                                 connection — no reconnect afterwards).
  /sessions_stats             → per-country unsold counts
  /sessions_list [CC]         → list unsold sessions
  /2fa_pass <phone> <pwd>     → provide CURRENT 2FA password for a pending session
  /pending_2fa                → list sessions waiting for 2FA password
  /set_country <phone> <CC>   → provide the country (ISO alpha-2, e.g. IN/US/GB)
                                 for a session whose number couldn't be
                                 auto-detected — processing resumes right after
  /pending_country            → list sessions waiting for a manual country
  /gen_fresh_session <phone>  → generate an ADDITIONAL login for an existing
                                 account (does not touch any existing session)
  /set_2fa <batch_id> <pwd>   → give the new 2FA password for a batch that
                                 chose "Enable" (only needed once per batch,
                                 BEFORE that batch's accounts are processed)
"""

import asyncio
import uuid

from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup
from server.utils.bot_utils import Btn as InlineKeyboardButton

from server import bot, LOGGER
from server.utils.bot_utils import is_admin as _is_admin
from server.stock.batch_runner import DEFAULT_CONCURRENCY, StockAccountOutcome, run_stock_batch
from server.stock.pending_state import PendingStockStore
from server.stock.persistence import save_verified_stock
from server.stock.pipeline import process_uploaded_session

_log = LOGGER(__name__)

# ── Pending 2FA store ───────────────────────────────────────────────────────
# For accounts where we don't yet know the CURRENT password (no JSON/txt
# match) — resolved later via /2fa_pass. The batch's 2FA decision
# (enable/disable + new password) is already known by the time an account
# lands here (it's decided upfront, before processing starts), so it's
# carried on the entry and simply reused when /2fa_pass resolves it — same
# single-connection pipeline call, no separate reconnect for 2FA.
_pending_2fa = PendingStockStore()

# ── Pending country store ────────────────────────────────────────────────────
# For accounts where phone_to_country() couldn't resolve a real country at
# all (returned "XX") — this only happens for numbers with no recognizable
# calling code (garbage/incomplete input), since real Telegram numbers always
# carry a genuine one. Rather than silently storing "Unknown" and picking a
# proxy blind, the account is parked here and the admin is asked to supply
# the correct ISO alpha-2 code via /set_country — processing (proxy login,
# 2FA, etc., same single connection as everything else) resumes right after.
_pending_country = PendingStockStore()

# ── Background purge of abandoned pending entries ──────────────────────────
# Entries here can hold full session bytes for an entire batch. If an admin
# never resolves a 2FA/country prompt (e.g. abandons the batch), those bytes
# would otherwise sit in memory for the life of the process. Purge anything
# older than 48h once an hour — well past any realistic admin response time.
_PENDING_MAX_AGE_SECONDS = 48 * 60 * 60
_PENDING_PURGE_INTERVAL_SECONDS = 60 * 60


async def _purge_stale_pending_loop() -> None:
    while True:
        await asyncio.sleep(_PENDING_PURGE_INTERVAL_SECONDS)
        try:
            n1 = _pending_2fa.purge_stale(_PENDING_MAX_AGE_SECONDS)
            n2 = _pending_country.purge_stale(_PENDING_MAX_AGE_SECONDS)
            if n1 or n2:
                _log.info(
                    "Purged %d stale pending_2fa and %d stale pending_country entries.",
                    n1, n2,
                )
        except Exception as exc:
            _log.warning("Pending-entry purge loop error: %s", exc)


try:
    asyncio.get_running_loop().create_task(_purge_stale_pending_loop())
except RuntimeError:
    # Module imported outside a running loop (e.g. static analysis/tests) —
    # main() imports plugins after the loop is already running in production.
    pass

# ── Batches awaiting the admin's Enable/Disable decision ───────────────────
# Created right after a ZIP/session is extracted, BEFORE any account is
# touched. Processing only starts once the admin picks a button (Disable →
# starts immediately) or supplies a new password via /set_2fa (Enable).
# { batch_id: { "chat_id", "channel_id", "sessions" } }
_awaiting_decision: dict[str, dict] = {}


_SPAM_LABEL = {
    "clean": "![🟢](tg://emoji?id=6129492160497589882) clean", "temporary_spam": "![🟡](tg://emoji?id=6129939837823753679) temporary spam",
    "frozen": "![🔴](tg://emoji?id=6129846551134084367) frozen", "permanent_spam": "![🟠](tg://emoji?id=6129939837823753679) permanent spam",
    "unknown": "⚪ unknown",
}
_SPAM_ICON = {"clean": " ![🟢](tg://emoji?id=6129492160497589882)", "temporary_spam": " ![🟡](tg://emoji?id=6129939837823753679)", "frozen": " ![🔴](tg://emoji?id=6129846551134084367)", "permanent_spam": " ![🟠](tg://emoji?id=6129939837823753679)"}

# Terminal-outcome -> icon, for statuses that reach _process_2fa_pass /
# _process_set_country without matching any of the earlier explicit checks
# (e.g. rpc_error, network_error, invalid_session).
_OUTCOME_ICON = {
    "success":          "![✅](tg://emoji?id=6129492160497589882)",
    "partial_success":  "![⚠️](tg://emoji?id=6129939837823753679)",
    "spam_restricted":  "![🟡](tg://emoji?id=6129939837823753679)",
    "frozen":           "![🔴](tg://emoji?id=6129846551134084367)",
    "banned":           "![🚫](tg://emoji?id=6129846551134084367)",
    "invalid_session":  "![❌](tg://emoji?id=6129846551134084367)",
    "rpc_error":        "![❌](tg://emoji?id=6129846551134084367)",
    "network_error":    "![🌐](tg://emoji?id=6296303781126604562)",
    "twofa_failed":     "![🔐](tg://emoji?id=6129550284290006595)",
    "upload_failed":    "![⚠️](tg://emoji?id=6129939837823753679)",
}

# ── Conversational "awaiting free-text input" state ─────────────────────────
# Keyed by admin user_id. Lets the admin answer a pending prompt (batch
# password, per-account 2FA password, per-account country) by simply typing
# a plain-text reply instead of a slash command — the command forms below
# are kept as a fallback for backward compatibility, never removed.
# { user_id: {"type": "batch_password"|"2fa_pass"|"set_country", ...} }
_awaiting_text_input: dict[int, dict] = {}


def _format_outcome_line(o: StockAccountOutcome, batch_action: str) -> tuple[str | None, str | None]:
    """Render one StockAccountOutcome to (result_line, pending_line)."""
    info = o.info or {}
    if o.status == "duplicate":
        return f"![🔁](tg://emoji?id=6129792056589031358) `{o.phone}` — already in DB, skipped", None
    if o.status == "needs_country":
        return None, f"![🌍](tg://emoji?id=6296303781126604562) `{o.phone}` — country unknown → `/set_country {o.phone} <CC>`"
    if o.status == "frozen":
        return f"![🔴](tg://emoji?id=6129846551134084367) `{o.phone}` — account frozen (permanently limited), skipped", None
    if o.status == "permanent_spam":
        return f"![🟠](tg://emoji?id=6129939837823753679) `{o.phone}` — permanent spam restriction, skipped", None
    if o.status == "needs_2fa":
        reason = "wrong password — retry" if info.get("tfa_password_wrong") else "2FA enabled — password needed"
        return None, f"![🔐](tg://emoji?id=6129550284290006595) `{o.phone}` — {reason}"
    if o.status == "invalid":
        err = o.error or info.get("error") or "invalid or expired session"
        return f"![❌](tg://emoji?id=6129846551134084367) `{o.phone}` — {err}", None
    if o.status == "failed":
        return f"![⚠️](tg://emoji?id=6129939837823753679) `{o.phone}` — DB save failed: {o.error}", None
    if o.status == "verified":
        name = info.get("first_name") or o.phone
        tags = ""
        if info.get("tfa_updated"):           tags += " ![🔐](tg://emoji?id=6129550284290006595)"
        if info.get("terminated_others"):      tags += " ✂️"
        if info.get("used_fallback"):          tags += " ⚡fallback"
        if info.get("termination_incomplete"): tags += " ![⚠️](tg://emoji?id=6129939837823753679)sessions-remain"
        spam_icon = _SPAM_ICON.get(info.get("spam_status"), "")
        proxy = info.get("proxy_used", "none")
        proxy_tag = f" ![🌐](tg://emoji?id=6296303781126604562)`{proxy}`" if proxy not in ("direct", "none", None) else ""
        return f"![✅](tg://emoji?id=6129492160497589882) `{o.phone}` — {name}{tags}{spam_icon}{proxy_tag}", None
    # Catch-all: show the actual error/status instead of "unknown outcome"
    err = o.error or (info.get("error")) or o.status or "processing failed"
    return f"![⚠️](tg://emoji?id=6129939837823753679) `{o.phone}` — {err}", None


def _build_summary(outcomes: list[StockAccountOutcome], batch_action: str) -> str:
    stats: dict[str, int] = {
        "total": len(outcomes), "verified": 0, "invalid": 0, "duplicate": 0,
        "needs_2fa": 0, "needs_country": 0, "frozen": 0, "permanent_spam": 0, "failed": 0,
    }
    lines: list[str] = []
    pending_lines: list[str] = []
    for o in outcomes:
        stats[o.status] = stats.get(o.status, 0) + 1
        line, pending_line = _format_outcome_line(o, batch_action)
        if line:
            lines.append(line)
        if pending_line:
            pending_lines.append(pending_line)

    _SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━"
    action_label = "Enable 2FA" if batch_action == "enable" else "Disable 2FA"

    # ── Stats table — only show non-zero rows to keep it tight ───────────────
    stat_rows = [
        (f"![📥](tg://emoji?id=6131886699254388574)", "Total",           stats["total"],                       True),
        (f"![✅](tg://emoji?id=6129492160497589882)", "Verified",         stats["verified"],                    True),
        (f"![🔐](tg://emoji?id=6129550284290006595)", "Pending 2FA",      stats["needs_2fa"],                   False),
        (f"![🌍](tg://emoji?id=6296303781126604562)", "Pending country",  stats.get("needs_country", 0),        False),
        (f"![🔴](tg://emoji?id=6129846551134084367)", "Frozen",           stats.get("frozen", 0),               False),
        (f"![🟠](tg://emoji?id=6129939837823753679)", "Permanent spam",   stats.get("permanent_spam", 0),       False),
        (f"![🔁](tg://emoji?id=6129792056589031358)", "Duplicates",       stats["duplicate"],                   False),
        (f"![❌](tg://emoji?id=6129846551134084367)", "Invalid",          stats["invalid"],                     False),
        (f"![⚠️](tg://emoji?id=6129939837823753679)", "Save failed",      stats["failed"],                      False),
    ]
    stat_lines = [
        f"{icon} {label}:  **{count}**"
        for icon, label, count, always in stat_rows
        if always or count > 0
    ]

    parts = [
        f"**![📊](tg://emoji?id=6129870783339567154) Pipeline Report** — {action_label}",
        _SEP,
        "\n".join(stat_lines),
    ]

    # ── Per-account detail (resolved accounts first, pending at end) ──────────
    result_lines = lines[: 40 - len(pending_lines)] if pending_lines else lines[:40]
    detail_sections: list[str] = []
    if result_lines:
        detail_sections.append(_SEP + "\n" + "\n".join(result_lines))
        overflow = len(lines) - len(pending_lines) - len(result_lines)
        if overflow > 0:
            detail_sections[-1] += f"\n_…and {overflow} more_"
    if pending_lines:
        detail_sections.append(
            _SEP + "\n**![⏳](tg://emoji?id=6129574787078429498) Needs action**\n" + "\n".join(pending_lines)
        )

    # ── Action tips ───────────────────────────────────────────────────────────
    tips: list[str] = []
    if stats.get("needs_2fa"):
        tips.append(
            "**![🔐](tg://emoji?id=6129550284290006595) Resolve 2FA**\n"
            "Tap the 🔐 button below, or:\n"
            "`/pending_2fa` → `/2fa_pass <#> <password>`"
        )
    if stats.get("needs_country"):
        tips.append(
            "**![🌍](tg://emoji?id=6296303781126604562) Resolve country**\n"
            "Tap the 🌍 button below, or:\n"
            "`/pending_country` → `/set_country <#> <CC>`"
        )
    if tips:
        detail_sections.append(_SEP + "\n" + "\n\n".join(tips))

    return "\n".join(parts) + ("\n\n" + "\n\n".join(detail_sections) if detail_sections else "")



async def _register_pending(chat_id: int, outcomes: list[StockAccountOutcome]) -> None:
    """Stash needs_2fa / needs_country outcomes into the resumable stores."""
    for o in outcomes:
        if o.status == "needs_2fa" and o.pending_entry:
            _pending_2fa.add(chat_id, o.phone, o.pending_entry)
        elif o.status == "needs_country" and o.pending_entry:
            _pending_country.add(chat_id, o.phone, o.pending_entry)


_MAX_PENDING_BUTTONS_PER_KIND = 15


def _build_pending_buttons(outcomes: list[StockAccountOutcome]) -> InlineKeyboardMarkup | None:
    """
    One tap-to-answer button per pending account (2FA password / country),
    capped so a huge batch doesn't produce an unusable wall of buttons — the
    rest stay reachable via /pending_2fa and /pending_country as before.
    Tapping sets the conversational awaiting-input state so the admin can
    just type the answer as a normal message, no command needed.
    """
    pwd_phones = [o.phone for o in outcomes if o.status == "needs_2fa"][:_MAX_PENDING_BUTTONS_PER_KIND]
    cc_phones  = [o.phone for o in outcomes if o.status == "needs_country"][:_MAX_PENDING_BUTTONS_PER_KIND]
    rows = [[InlineKeyboardButton(f"![🔐](tg://emoji?id=6129550284290006595) {phone}", callback_data=f"askpwd_{phone}")] for phone in pwd_phones]
    rows += [[InlineKeyboardButton(f"![🌍](tg://emoji?id=6296303781126604562) {phone}", callback_data=f"askcc_{phone}")] for phone in cc_phones]
    return InlineKeyboardMarkup(rows) if rows else None


async def _send_batch_2fa_passwords(client, chat_id: int, outcomes: list[StockAccountOutcome],
                                     batch_action: str, batch_new_password: str | None) -> None:
    if not (batch_action == "enable" and batch_new_password):
        return
    for o in outcomes:
        if o.status == "verified" and o.info and o.info.get("tfa_updated"):
            try:
                await client.send_message(
                    chat_id, f"![📱](tg://emoji?id=5330237710655306682) `{o.phone}`\n![🔑](tg://emoji?id=6129782440157256336) Password: `{batch_new_password}`",
                )
            except Exception as exc:
                _log.warning("Failed to send password message for %s: %s", o.phone, exc)


# ── Upload handler ────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.document)
async def session_upload_handler(client, message: Message):
    user_id = message.from_user.id
    if not await _is_admin(user_id):
        return

    doc   = message.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".zip") or fname.endswith(".session")):
        return

    import config as _cfg
    channel_id = getattr(_cfg, "SESSION_CHANNEL_ID", None)
    if not channel_id:
        await message.reply_text(
            "![⚠️](tg://emoji?id=6129939837823753679) `SESSION_CHANNEL_ID` is not set.\nAdd it in Secrets and restart."
        )
        return

    status_msg = await message.reply_text("![📥](tg://emoji?id=6131886699254388574) Downloading…")

    try:
        buf = await client.download_media(message, in_memory=True)
        buf.seek(0)
        raw = buf.read()
    except Exception as exc:
        await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) Download failed: `{exc}`")
        return

    # ── Single .session file ────────────────────────────────────────────
    if fname.endswith(".session"):
        phone = (doc.file_name or "").replace(".session", "").strip()
        if not phone.startswith("+"):
            phone = "+" + phone
        sessions = [{
            "phone": phone, "session_bytes": raw,
            "password": "",
            "api_id_override": None, "api_hash_override": None,
        }]
    else:
        # ── Zip ─────────────────────────────────────────────────────────
        await status_msg.edit_text("![📦](tg://emoji?id=6131886699254388574) Extracting sessions from zip…")
        from server.stock.zip_extraction import extract_sessions_from_zip
        try:
            sessions = extract_sessions_from_zip(raw)
        except Exception as exc:
            await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) Failed to read zip: `{exc}`")
            return
        if not sessions:
            await status_msg.edit_text("![❌](tg://emoji?id=6129846551134084367) No `.session` files found inside the zip.")
            return

    json_count   = sum(1 for s in sessions if s.get("has_json"))
    batch_id     = uuid.uuid4().hex[:8]
    _awaiting_decision[batch_id] = {
        "chat_id":    message.chat.id,
        "channel_id": channel_id,
        "sessions":   sessions,
    }
    is_zip = fname.endswith(".zip")

    def _found_line() -> str:
        base = f"![🔍](tg://emoji?id=5316722951813346475) **{len(sessions)}** session(s) found"
        if is_zip and json_count:
            base += f" · {json_count} with JSON metadata"
        return base

    # ── Caption password: ask admin before applying (ZIP only) ───────────────
    caption_pwd  = (message.caption or "").strip()
    no_pwd_count = sum(1 for s in sessions if not s.get("password"))
    if is_zip and caption_pwd and no_pwd_count:
        short = caption_pwd[:30] + ("…" if len(caption_pwd) > 30 else "")
        _awaiting_decision[batch_id]["_caption_pwd"] = caption_pwd
        await status_msg.edit_text(
            f"{_found_line()}\n\n"
            f"![🔑](tg://emoji?id=6129782440157256336) Caption: `{short}`\n"
            f"![⚠️](tg://emoji?id=6129939837823753679) **{no_pwd_count}** account(s) have no JSON password.\n\n"
            f"Use this caption as the **current 2FA password** for those accounts?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("![✅](tg://emoji?id=6129492160497589882) Yes — use caption", callback_data=f"cap_yes_{batch_id}"),
                InlineKeyboardButton("![❌](tg://emoji?id=6129846551134084367) No — skip",         callback_data=f"cap_no_{batch_id}"),
            ]]),
        )
        return

    missing_pw_count = sum(1 for s in sessions if not s.get("password"))
    if missing_pw_count:
        await status_msg.edit_text(
            f"{_found_line()}\n\n"
            f"![⚠️](tg://emoji?id=6129939837823753679) **{missing_pw_count}** account(s) have no known current 2FA password.\n\n"
            "Do **all** of them share the **same** current 2FA password?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("![✅](tg://emoji?id=6129492160497589882) Yes — same password", callback_data=f"pwdall_yes_{batch_id}"),
                InlineKeyboardButton("![❌](tg://emoji?id=6129846551134084367) No — resolve later",  callback_data=f"pwdall_no_{batch_id}"),
            ]]),
        )
        return

    await status_msg.edit_text(f"{_found_line()}.")
    await _ask_2fa_decision(client, message.chat.id, batch_id, sessions, json_count, is_zip)


# ── Batch-level "same current password?" decision ────────────────────────────

async def _ask_2fa_decision(
    client, chat_id: int, batch_id: str, sessions: list[dict], json_count: int, is_zip: bool,
) -> None:
    """Ask the Enable/Disable 2FA question for a batch."""
    detail = f" · {json_count} with JSON" if is_zip and json_count else ""
    await client.send_message(
        chat_id,
        f"![🔐](tg://emoji?id=6129550284290006595) **{len(sessions)}** session(s) ready{detail}\n\n"
        "Set 2FA action for this entire batch:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Enable 2FA",  callback_data=f"zfa_en_{batch_id}"),
            InlineKeyboardButton("❌ Disable 2FA", callback_data=f"zfa_dis_{batch_id}"),
        ]]),
    )


# ── Caption password confirmation: Yes/No — asked before auto-applying ──────

@bot.on_callback_query(filters.regex(r"^cap_(yes|no)_([0-9a-f]+)$"))
async def cb_caption_password_decision(client, callback_query):
    """Admin decides whether the ZIP caption should be used as a common
    current 2FA password for sessions that have no JSON-matched password."""
    if not await _is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorized.", show_alert=True)
        return

    choice, batch_id = callback_query.matches[0].group(1), callback_query.matches[0].group(2)
    data = _awaiting_decision.get(batch_id)
    if not data:
        await callback_query.answer("This batch has expired or was already started.", show_alert=True)
        return

    await callback_query.answer()
    sessions   = data["sessions"]
    json_count = sum(1 for s in sessions if s.get("has_json"))

    if choice == "yes":
        caption_pwd = data.pop("_caption_pwd", "")
        filled = 0
        if caption_pwd:
            for s in sessions:
                if not s.get("password"):
                    s["password"] = caption_pwd
                    filled += 1
        await callback_query.message.edit_text(
            f"![✅](tg://emoji?id=6129492160497589882) Caption applied as current 2FA password "
            f"to **{filled}** account(s) that had no JSON match."
        )
    else:
        data.pop("_caption_pwd", None)
        await callback_query.message.edit_text(
            "![👍](tg://emoji?id=6129492160497589882) Caption skipped — accounts without a known "
            "password will be resolved individually after the batch runs."
        )

    # Re-evaluate missing passwords and continue the normal flow
    missing_pw_count = sum(1 for s in sessions if not s.get("password"))
    if missing_pw_count:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("![✅](tg://emoji?id=6129492160497589882) Yes", callback_data=f"pwdall_yes_{batch_id}"),
            InlineKeyboardButton("![❌](tg://emoji?id=6129846551134084367) No",  callback_data=f"pwdall_no_{batch_id}"),
        ]])
        await client.send_message(
            data["chat_id"],
            f"![⚠️](tg://emoji?id=6129939837823753679) **{missing_pw_count}** account(s) still have no known "
            "current 2FA password.\n\nDo **all** of them use the **same** current password?",
            reply_markup=buttons,
        )
        return

    await _ask_2fa_decision(client, data["chat_id"], batch_id, sessions, json_count, True)


@bot.on_callback_query(filters.regex(r"^pwdall_(yes|no)_([0-9a-f]+)$"))
async def cb_batch_password_decision(client, callback_query):
    if not await _is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorized.", show_alert=True)
        return

    choice, batch_id = callback_query.matches[0].group(1), callback_query.matches[0].group(2)
    data = _awaiting_decision.get(batch_id)
    if not data:
        await callback_query.answer("This batch has expired or was already started.", show_alert=True)
        return

    sessions   = data["sessions"]
    json_count = sum(1 for s in sessions if s.get("has_json"))

    if choice == "yes":
        await callback_query.answer()
        _awaiting_text_input[callback_query.from_user.id] = {
            "type": "batch_password", "batch_id": batch_id,
        }
        await callback_query.message.edit_text(
            "![🔑](tg://emoji?id=6129782440157256336) Send the current 2FA password for **all** these accounts now — "
            "just type it as a normal message, no command needed."
        )
    else:
        await callback_query.answer()
        await callback_query.message.edit_text(
            "![👍](tg://emoji?id=6129492160497589882) Got it — accounts without a known password will be collected into "
            "a pending list after this batch finishes; you'll be asked for each "
            "one individually (tap-to-answer buttons, no typing the phone number)."
        )
        await _ask_2fa_decision(client, data["chat_id"], batch_id, sessions, json_count, True)


async def _apply_batch_password_and_ask_2fa(client, chat_id: int, batch_id: str, password: str) -> None:
    data = _awaiting_decision.get(batch_id)
    if not data:
        await client.send_message(chat_id, "![⚠️](tg://emoji?id=6129939837823753679) This batch has expired or was already started.")
        return

    sessions = data["sessions"]
    filled = 0
    for s in sessions:
        if not s.get("password"):
            s["password"] = password
            filled += 1
    json_count = sum(1 for s in sessions if s.get("has_json"))

    await client.send_message(chat_id, f"![✅](tg://emoji?id=6129492160497589882) Applied that password to **{filled}** account(s).")
    await _ask_2fa_decision(client, chat_id, batch_id, sessions, json_count, True)


# ── Batch 2FA decision — asked BEFORE processing starts ─────────────────────

async def _start_batch_processing(
    client, chat_id: int, batch_id: str, batch_action: str, batch_new_password: str | None,
):
    """Kick off the actual pipeline run once the admin's Enable/Disable
    decision (and, for Enable, the new password) is known — never before."""
    data = _awaiting_decision.pop(batch_id, None)
    if not data:
        return None

    sessions   = data["sessions"]
    channel_id = data["channel_id"]

    from config import API_ID as _api_id, API_HASH as _api_hash

    status_msg = await client.send_message(
        chat_id,
        f"![🚀](tg://emoji?id=6129792056589031358) Starting production pipeline ({'enable' if batch_action == 'enable' else 'disable'} 2FA)…\n\n"
        f"_Proxy login → Verify → Fresh session → 2FA → Terminate others (all in one connection)_"
    )

    async def _on_progress(done: int, total: int) -> None:
        await status_msg.edit_text(
            f"![⏳](tg://emoji?id=6129574787078429498) Processing… {done}/{total} done (up to {DEFAULT_CONCURRENCY} in parallel)"
        )

    outcomes = await run_stock_batch(
        sessions, channel_id, _api_id, _api_hash, batch_action, batch_new_password,
        client, on_progress=_on_progress,
    )
    await _register_pending(chat_id, outcomes)
    await _send_batch_2fa_passwords(client, chat_id, outcomes, batch_action, batch_new_password)
    await status_msg.edit_text(
        _build_summary(outcomes, batch_action),
        reply_markup=_build_pending_buttons(outcomes),
    )
    return outcomes


@bot.on_callback_query(filters.regex(r"^zfa_(en|dis)_([0-9a-f]+)$"))
async def cb_batch_2fa_decision(client, callback_query):
    if not await _is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorized.", show_alert=True)
        return

    action, batch_id = callback_query.matches[0].group(1), callback_query.matches[0].group(2)
    data = _awaiting_decision.get(batch_id)
    if not data:
        await callback_query.answer("This batch has expired or was already started.", show_alert=True)
        return

    if action == "dis":
        await callback_query.answer("Disabling 2FA…")
        await callback_query.message.edit_text("![❌](tg://emoji?id=6129846551134084367) Disable selected — starting pipeline…")
        await _start_batch_processing(client, data["chat_id"], batch_id, "disable", None)
    else:
        await callback_query.answer()
        _awaiting_text_input[callback_query.from_user.id] = {
            "type": "batch_new_password", "batch_id": batch_id,
        }
        await callback_query.message.edit_text(
            "![🔑](tg://emoji?id=6129782440157256336) Enter the new 2FA password for this batch — just type it as a "
            "normal message (no command needed).\n\n"
            "Processing starts as soon as you send it — that same password "
            "gets set on every account in this batch."
        )


async def _apply_batch_new_password_and_start(client, chat_id: int, batch_id: str, new_password: str) -> None:
    data = _awaiting_decision.get(batch_id)
    if not data:
        await client.send_message(chat_id, f"![⚠️](tg://emoji?id=6129939837823753679) No such batch `{batch_id}` (expired or already started).")
        return
    await client.send_message(chat_id, "![✅](tg://emoji?id=6129492160497589882) Enable selected — starting pipeline…")
    # Use the batch's own chat_id (from when it was created), not necessarily
    # the chat this reply happened in — avoids cross-admin/cross-chat
    # mismatches if multiple admins/chats are active.
    await _start_batch_processing(client, data["chat_id"], batch_id, "enable", new_password)


@bot.on_message(filters.private & filters.command("set_2fa"))
async def cmd_set_2fa(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text("Usage: `/set_2fa <batch_id> <new_password>`")
        return

    batch_id     = args[0].strip()
    new_password = " ".join(args[1:]).strip()
    await _apply_batch_new_password_and_start(client, message.chat.id, batch_id, new_password)


# ── /2fa_pass — provide current password for a pending session ──────────────

async def _process_2fa_pass(client, chat_id: int, phone: str, admin_2fa_pass: str) -> None:
    """Shared implementation behind both `/2fa_pass` and the conversational
    tap-a-button-then-type-the-password flow."""
    entry = _pending_2fa.pop(chat_id, phone)
    if not entry:
        await client.send_message(
            chat_id, f"![⚠️](tg://emoji?id=6129939837823753679) No pending session for `{phone}` (already resolved or expired)."
        )
        return

    raw                 = entry["session_bytes"]
    channel_id          = entry["channel_id"]
    country_code        = entry["country_code"]
    api_id_override     = entry.get("api_id_override")
    api_hash_override   = entry.get("api_hash_override")
    batch_action        = entry.get("batch_action", "disable")
    batch_new_password  = entry.get("batch_new_password")

    status_msg = await client.send_message(chat_id, f"![🔐](tg://emoji?id=6129550284290006595) Processing `{phone}` with provided password…")

    from config import API_ID as _api_id, API_HASH as _api_hash
    from server.utils.database.sessiondb import get_session_by_phone

    try:
        info = await process_uploaded_session(
            raw, _api_id, _api_hash, country_code, admin_2fa_pass,
            old_api_id=api_id_override,
            old_api_hash=api_hash_override,
            batch_action=batch_action,
            batch_new_password=batch_new_password,
        )
    except Exception as exc:
        await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) Pipeline error: {exc}")
        return

    if info.get("tfa_password_wrong"):
        # Put back in pending so admin can retry
        _pending_2fa.add(chat_id, phone, entry)
        await status_msg.edit_text(
            f"![❌](tg://emoji?id=6129846551134084367) Wrong 2FA password for `{phone}`.\n"
            f"Try again — reply with the correct password, or `/2fa_pass {phone} <correct_password>`"
        )
        return

    if info.get("needs_admin_password"):
        _pending_2fa.add(chat_id, phone, entry)
        await status_msg.edit_text(
            f"![⚠️](tg://emoji?id=6129939837823753679) Still needs 2FA password for `{phone}`.\n"
            f"Reply with the password, or `/2fa_pass {phone} <password>`"
        )
        return

    if info.get("frozen"):
        await status_msg.edit_text(
            f"![🔴](tg://emoji?id=6129846551134084367) `{phone}` — frozen account (permanently limited by Telegram). "
            f"Skipped — no login/session changes were attempted."
        )
        return

    if not info.get("success"):
        await status_msg.edit_text(f"{_OUTCOME_ICON.get(info.get('outcome'), '![❌](tg://emoji?id=6129846551134084367)')} `{phone}` — {info.get('error') or 'unknown error'}")
        return

    existing = await get_session_by_phone(info.get("phone") or phone)
    if existing:
        await status_msg.edit_text(f"![🔁](tg://emoji?id=6129792056589031358) `{phone}` is already in the database.")
        return

    try:
        real_phone = info.get("phone") or phone
        acc_id = await save_verified_stock(client, real_phone, info, channel_id)
        name  = info.get("first_name") or real_phone
        proxy = info.get("proxy_used", "none")
        spam_label = _SPAM_LABEL.get(info.get("spam_status"), "⚪ unknown")

        await status_msg.edit_text(
            f"![✅](tg://emoji?id=6129492160497589882) **Session added!**\n"
            f"• Phone: `{real_phone}`\n"
            f"• Name: {name}\n"
            f"• 2FA: {'updated ![✅](tg://emoji?id=6129492160497589882)' if info.get('tfa_updated') else 'not changed'}\n"
            f"• Spam status: {spam_label}\n"
            f"• Proxy: `{proxy}`\n"
            f"• Other sessions terminated: {'yes ✂️' if info.get('terminated_others') else 'no'}\n"
            f"• Account ID: `{acc_id}`"
        )
    except Exception as exc:
        await status_msg.edit_text(f"![⚠️](tg://emoji?id=6129939837823753679) Save failed: {exc}")


@bot.on_message(filters.private & filters.command("2fa_pass"))
async def cmd_2fa_pass(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text(
            "Usage: `/2fa_pass <phone or #number> <current_password>`\n"
            "Example: `/2fa_pass +919876543210 myoldpass`\n"
            "Or just:  `/2fa_pass 1 myoldpass`  (number from `/pending_2fa`)\n\n"
            "Tip: tap the ![🔐](tg://emoji?id=6129550284290006595) button on the pipeline report instead, then just "
            "type the password — no command needed."
        )
        return

    chat_id = message.chat.id
    token   = args[0].strip()
    phone   = _pending_2fa.resolve_token(chat_id, token)
    admin_2fa_pass = " ".join(args[1:]).strip()

    if not phone:
        await message.reply_text(
            f"![⚠️](tg://emoji?id=6129939837823753679) No pending session for `{token}`.\n"
            "Use `/pending_2fa` to see all waiting accounts (with numbers)."
        )
        return

    await _process_2fa_pass(client, chat_id, phone, admin_2fa_pass)


# ── Tap-to-answer buttons on the pipeline report ─────────────────────────────

@bot.on_callback_query(filters.regex(r"^askpwd_(.+)$"))
async def cb_ask_pwd(client, callback_query):
    if not await _is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorized.", show_alert=True)
        return
    phone = callback_query.matches[0].group(1)
    chat_id = callback_query.message.chat.id
    if phone not in _pending_2fa.list_for_chat(chat_id):
        await callback_query.answer("Already resolved or expired.", show_alert=True)
        return
    await callback_query.answer()
    _awaiting_text_input[callback_query.from_user.id] = {"type": "2fa_pass", "phone": phone}
    await client.send_message(
        chat_id, f"![🔑](tg://emoji?id=6129782440157256336) Send the current 2FA password for `{phone}` now — plain text, no command."
    )


@bot.on_callback_query(filters.regex(r"^askcc_(.+)$"))
async def cb_ask_country(client, callback_query):
    if not await _is_admin(callback_query.from_user.id):
        await callback_query.answer("Not authorized.", show_alert=True)
        return
    phone = callback_query.matches[0].group(1)
    chat_id = callback_query.message.chat.id
    if phone not in _pending_country.list_for_chat(chat_id):
        await callback_query.answer("Already resolved or expired.", show_alert=True)
        return
    await callback_query.answer()
    _awaiting_text_input[callback_query.from_user.id] = {"type": "set_country", "phone": phone}
    await client.send_message(
        chat_id, f"![🌍](tg://emoji?id=6296303781126604562) Send the ISO country code (e.g. `IN`, `US`, `GB`) for `{phone}` now — plain text."
    )


# ── Conversational free-text answers (no slash command needed) ─────────────
# Only ever acts when the admin has an active awaiting-input state (set by a
# tap-to-answer button above); every other private text message passes
# through untouched, so it never interferes with anything else the bot does.

@bot.on_message(filters.private & filters.text, group=1)
async def admin_awaiting_input_handler(client, message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id or not await _is_admin(user_id):
        return

    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    pending = _awaiting_text_input.get(user_id)
    if not pending:
        return

    del _awaiting_text_input[user_id]
    chat_id = message.chat.id
    kind = pending.get("type")

    # Best-effort: remove the password/code from chat history once read.
    try:
        await message.delete()
    except Exception:
        pass

    if kind == "batch_password":
        await _apply_batch_password_and_ask_2fa(client, chat_id, pending["batch_id"], text)
    elif kind == "batch_new_password":
        await _apply_batch_new_password_and_start(client, chat_id, pending["batch_id"], text)
    elif kind == "2fa_pass":
        await _process_2fa_pass(client, chat_id, pending["phone"], text)
    elif kind == "set_country":
        resolved = _resolve_country_code(text)
        if not resolved:
            await client.send_message(
                chat_id,
                f"![⚠️](tg://emoji?id=6129939837823753679) `{text}` isn't a valid ISO alpha-2 country code (e.g. `IN`, `US`, `GB`). "
                "Try again, or use `/set_country`.",
            )
            # Re-arm so the admin can just try again without re-tapping the button.
            _awaiting_text_input[user_id] = pending
            return
        country_code, country_name = resolved
        await _process_set_country(client, chat_id, pending["phone"], country_code, country_name)


# ── /pending_2fa ──────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("pending_2fa"))
async def cmd_pending_2fa(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return
    pending = _pending_2fa.list_for_chat(message.chat.id)
    if not pending:
        await message.reply_text("![✅](tg://emoji?id=6129492160497589882) No pending 2FA sessions.")
        return
    lines = [f"![🔐](tg://emoji?id=6129550284290006595) **Pending 2FA ({len(pending)})**\n"]
    for idx, phone in enumerate(pending, 1):
        lines.append(f"**{idx}.** `{phone}` → `/2fa_pass {idx} <password>`")
    await message.reply_text("\n".join(lines))


# ── /pending_country ─────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("pending_country"))
async def cmd_pending_country(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return
    pending = _pending_country.list_for_chat(message.chat.id)
    if not pending:
        await message.reply_text("![✅](tg://emoji?id=6129492160497589882) No sessions waiting on a country.")
        return
    lines = [f"![🌍](tg://emoji?id=6296303781126604562) **Pending country ({len(pending)})**\n"]
    for idx, phone in enumerate(pending, 1):
        lines.append(f"**{idx}.** `{phone}` → `/set_country {idx} <CC>`")
    await message.reply_text("\n".join(lines))


# ── /set_country — provide the country for a session that couldn't auto-detect ──

def _resolve_country_code(cc_input: str) -> tuple[str, str] | None:
    import pycountry
    country = pycountry.countries.get(alpha_2=cc_input.strip().upper())
    if not country:
        return None
    return country.alpha_2, country.name


async def _process_set_country(client, chat_id: int, phone: str, country_code: str, country_name: str) -> None:
    """Shared implementation behind both `/set_country` and the conversational
    tap-a-button-then-type-the-code flow."""
    entry = _pending_country.pop(chat_id, phone)
    if not entry:
        await client.send_message(
            chat_id, f"![⚠️](tg://emoji?id=6129939837823753679) No pending session for `{phone}` (already resolved or expired)."
        )
        return

    raw                 = entry["session_bytes"]
    channel_id          = entry["channel_id"]
    admin_2fa_pass      = entry.get("admin_2fa_pass", "")
    api_id_override     = entry.get("api_id_override")
    api_hash_override   = entry.get("api_hash_override")
    batch_action        = entry.get("batch_action", "disable")
    batch_new_password  = entry.get("batch_new_password")

    status_msg = await client.send_message(
        chat_id, f"![🌍](tg://emoji?id=6296303781126604562) `{phone}` — using `{country_code}` ({country_name}), continuing pipeline…"
    )

    from config import API_ID as _api_id, API_HASH as _api_hash
    from server.utils.database.sessiondb import get_session_by_phone

    try:
        if await get_session_by_phone(phone):
            await status_msg.edit_text(f"![🔁](tg://emoji?id=6129792056589031358) `{phone}` is already in the database.")
            return
    except Exception as exc:
        _log.warning("Duplicate check error for %s: %s", phone, exc)

    try:
        info = await process_uploaded_session(
            raw, _api_id, _api_hash, country_code, admin_2fa_pass,
            old_api_id=api_id_override,
            old_api_hash=api_hash_override,
            batch_action=batch_action,
            batch_new_password=batch_new_password,
        )
    except Exception as exc:
        await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) Pipeline error: {exc}")
        return

    if info.get("needs_admin_password") or info.get("tfa_password_wrong"):
        # Country is now known — hand off to the 2FA-pending flow so the
        # admin resolves the password next, without re-detecting country.
        _pending_2fa.add(chat_id, phone, {
            "session_bytes":      raw,
            "channel_id":         channel_id,
            "country_code":       country_code,
            "api_id_override":    api_id_override,
            "api_hash_override":  api_hash_override,
            "batch_action":       batch_action,
            "batch_new_password": batch_new_password,
        })
        reason = "wrong 2FA password, retry" if info.get("tfa_password_wrong") else "has 2FA, need current password"
        await status_msg.edit_text(
            f"![🔐](tg://emoji?id=6129550284290006595) `{phone}` — {reason}.\nReply with the password, or `/2fa_pass {phone} <password>`"
        )
        return

    if info.get("frozen"):
        await status_msg.edit_text(
            f"![🔴](tg://emoji?id=6129846551134084367) `{phone}` — frozen account (permanently limited by Telegram). "
            f"Skipped — no login/session changes were attempted."
        )
        return

    if info.get("permanent_spam"):
        await status_msg.edit_text(
            f"![🟠](tg://emoji?id=6129939837823753679) `{phone}` — permanent spam restriction (no lift date). "
            f"Skipped — no login/session changes were attempted."
        )
        return

    if not info.get("success"):
        await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) `{phone}` — {info.get('error') or 'invalid session'}")
        return

    try:
        real_phone = info.get("phone") or phone
        acc_id = await save_verified_stock(
            client, real_phone, info, channel_id,
            country_code_override=country_code,
        )
        name  = info.get("first_name") or real_phone
        proxy = info.get("proxy_used", "none")
        spam_label = _SPAM_LABEL.get(info.get("spam_status"), "⚪ unknown")

        await status_msg.edit_text(
            f"![✅](tg://emoji?id=6129492160497589882) **Session added!**\n"
            f"• Phone: `{real_phone}`\n"
            f"• Country: {country_name} (`{country_code}`)\n"
            f"• Name: {name}\n"
            f"• 2FA: {'updated ![✅](tg://emoji?id=6129492160497589882)' if info.get('tfa_updated') else 'not changed'}\n"
            f"• Spam status: {spam_label}\n"
            f"• Proxy: `{proxy}`\n"
            f"• Other sessions terminated: {'yes ✂️' if info.get('terminated_others') else 'no'}\n"
            f"• Account ID: `{acc_id}`"
        )
    except Exception as exc:
        await status_msg.edit_text(f"![⚠️](tg://emoji?id=6129939837823753679) Save failed: {exc}")


@bot.on_message(filters.private & filters.command("set_country"))
async def cmd_set_country(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text(
            "Usage: `/set_country <phone or #number> <CC>`\n"
            "Example: `/set_country +881234567 IN`\n"
            "Or just:  `/set_country 1 IN`  (number from `/pending_country`)\n\n"
            "`<CC>` is the ISO alpha-2 country code (e.g. `IN`, `US`, `GB`).\n\n"
            "Tip: tap the ![🌍](tg://emoji?id=6296303781126604562) button on the pipeline report instead, then just "
            "type the code — no command needed."
        )
        return

    chat_id  = message.chat.id
    token    = args[0].strip()
    cc_input = args[1].strip().upper()

    resolved = _resolve_country_code(cc_input)
    if not resolved:
        await message.reply_text(
            f"![⚠️](tg://emoji?id=6129939837823753679) `{cc_input}` isn't a valid ISO alpha-2 country code.\n"
            "Use the 2-letter code, e.g. `IN`, `US`, `GB`, `NG`."
        )
        return
    country_code, country_name = resolved

    phone = _pending_country.resolve_token(chat_id, token)
    if not phone:
        await message.reply_text(
            f"![⚠️](tg://emoji?id=6129939837823753679) No pending session for `{token}`.\n"
            "Use `/pending_country` to see all waiting accounts (with numbers)."
        )
        return

    await _process_set_country(client, chat_id, phone, country_code, country_name)


# ── /gen_fresh_session — additional login, nothing terminated ─────────────

@bot.on_message(filters.private & filters.command("gen_fresh_session"))
async def cmd_gen_fresh_session(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if not args:
        await message.reply_text(
            "Usage: `/gen_fresh_session <phone>`\n"
            "Example: `/gen_fresh_session +919876543210`\n\n"
            "Generates an ADDITIONAL login for an already-uploaded, verified "
            "account. The existing session is left fully active — nothing is "
            "terminated."
        )
        return

    phone = args[0].strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    from config import API_ID as _api_id, API_HASH as _api_hash
    from server.utils.database.sessiondb import get_session_by_phone, add_extra_session
    from server.utils.sessions.channel_storage import download_session_from_channel, upload_session_to_channel
    from server.stock.session_generation import generate_fresh_session

    account = await get_session_by_phone(phone)
    if not account:
        await message.reply_text(f"![⚠️](tg://emoji?id=6129939837823753679) No stored account found for `{phone}`.")
        return

    status_msg = await message.reply_text(
        f"📡 Requesting a new login for `{phone}`…\n"
        f"_This will NOT affect the existing session._"
    )

    try:
        old_bytes = await download_session_from_channel(
            client, account["session_chat_id"], account["session_msg_id"]
        )
    except Exception as exc:
        await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) Could not fetch stored session: {exc}")
        return

    tfa_password = account.get("tfa_password_enc", "")

    try:
        await status_msg.edit_text(f"![⏳](tg://emoji?id=6129574787078429498) `{phone}` — waiting for login code (up to 120s)…")
        info = await generate_fresh_session(
            old_bytes, _api_id, _api_hash,
            account.get("country_code", "XX"),
            tfa_password=tfa_password,
        )
    except Exception as exc:
        await status_msg.edit_text(f"![❌](tg://emoji?id=6129846551134084367) Pipeline error: {exc}")
        return

    if not info.get("success"):
        if info.get("needs_password"):
            await status_msg.edit_text(
                f"![🔐](tg://emoji?id=6129550284290006595) `{phone}` — 2FA password missing/incorrect, could not "
                f"complete the new login.\nError: {info.get('error')}"
            )
        else:
            await status_msg.edit_text(
                f"![❌](tg://emoji?id=6129846551134084367) `{phone}` — {info.get('error') or 'failed to generate fresh session'}"
            )
        return

    try:
        upload_ref = await upload_session_to_channel(
            client, account["session_chat_id"], phone, info["fresh_session_bytes"]
        )
        msg_id = upload_ref.message_id
        await add_extra_session(phone, msg_id, upload_ref.chat_id)
    except Exception as exc:
        await status_msg.edit_text(f"![⚠️](tg://emoji?id=6129939837823753679) Login succeeded but upload/save failed: {exc}")
        return

    await status_msg.edit_text(
        f"![✅](tg://emoji?id=6129492160497589882) **Fresh session generated for `{phone}`**\n"
        f"• Name: {info.get('first_name') or phone}\n"
        f"• Proxy: `{info.get('proxy_used')}`\n"
        f"• Uploaded as an additional session (msg `{msg_id}`)\n"
        f"• Original session: untouched, still active ![✅](tg://emoji?id=6129492160497589882)"
    )


# ── /sessions_stats ─────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("sessions_stats"))
async def sessions_stats_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return
    from server.utils.database.sessiondb import get_session_stats
    stats = await get_session_stats()
    if not stats:
        await message.reply_text("![📊](tg://emoji?id=6129870783339567154) No session accounts in database.")
        return
    lines = ["![📊](tg://emoji?id=6129870783339567154) **Session Stock (Unsold)**\n"]
    for s in stats:
        unsold   = s["unsold"]
        sellable = s.get("sellable", unsold)
        clean_tag = f" ![✅](tg://emoji?id=6129492160497589882) {sellable} sellable" if sellable < unsold else " ![✅](tg://emoji?id=6129492160497589882) all clean"
        if sellable == 0 and unsold > 0:
            clean_tag = " ![⚠️](tg://emoji?id=6129939837823753679) none sellable (spam/unknown)"
        lines.append(
            f"• {s['country_name']} `{s['country_code']}` — **{unsold}** unsold{clean_tag}"
        )
    await message.reply_text("\n".join(lines))


# ── /sessions_list ──────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("sessions_list"))
async def sessions_list_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return
    from server.utils.database.sessiondb import get_all_session_accounts
    args     = message.command[1:]
    country  = args[0].upper() if args else None
    accounts = await get_all_session_accounts(country_code=country, sold=False, limit=50)
    if not accounts:
        await message.reply_text("![📋](tg://emoji?id=6129579803600231171) No unsold sessions found.")
        return
    lines = [f"![📋](tg://emoji?id=6129579803600231171) **Unsold Sessions ({len(accounts)})**\n"]
    for a in accounts[:30]:
        name  = a.get("first_name") or ""
        flags = ""
        if a.get("has_2fa"):           flags += " ![🔐](tg://emoji?id=6129550284290006595)"
        if a.get("tfa_updated"):       flags += " ![✅](tg://emoji?id=6129492160497589882)"
        if a.get("terminated_others"): flags += " ✂️"
        flags += _SPAM_ICON.get(a.get("spam_status"), "")
        proxy = a.get("proxy_used", "")
        proxy_str = f" ![🌐](tg://emoji?id=6296303781126604562)`{proxy}`" if proxy not in ("direct", "none", "", None) else ""
        lines.append(
            f"• `{a['account_id']}` `{a['phone']}` {a['country_name']}{flags}{proxy_str}"
            + (f" — {name}" if name else "")
        )
    if len(accounts) > 30:
        lines.append(f"…and {len(accounts) - 30} more")
    await message.reply_text("\n".join(lines))


# ── /spam_settings — view per-spam-status buy/sell toggles ──────────────────

@bot.on_message(filters.private & filters.command("spam_settings"))
async def cmd_spam_settings(client, message: Message):
    """Show the current buy/sell toggle for each spam status category."""
    if not await _is_admin(message.from_user.id):
        return
    from server.utils.database.configdb import get_setting
    _ROWS = [
        ("buy_enabled_clean",      "Buy  — Clean"),
        ("buy_enabled_temp_spam",  "Buy  — Temp Spam"),
        ("buy_enabled_perm_spam",  "Buy  — Perm Spam"),
        ("buy_enabled_frozen",     "Buy  — Frozen"),
        ("buy_enabled_unknown",    "Buy  — Unknown"),
        ("sell_enabled_clean",     "Sell — Clean"),
        ("sell_enabled_temp_spam", "Sell — Temp Spam"),
        ("sell_enabled_perm_spam", "Sell — Perm Spam"),
        ("sell_enabled_frozen",    "Sell — Frozen"),
        ("sell_enabled_unknown",   "Sell — Unknown"),
    ]
    lines = ["![📊](tg://emoji?id=6129870783339567154) **Spam Status Buy/Sell Toggles**\n"]
    for key, label in _ROWS:
        val  = await get_setting(key)
        icon = "![✅](tg://emoji?id=6129492160497589882)" if val else "![❌](tg://emoji?id=6129846551134084367)"
        lines.append(f"{icon} `{label}` — **{'enabled' if val else 'disabled'}**")
    lines += [
        "",
        "**Commands:**",
        "`/spam_buy <status> <on|off>` — toggle buying",
        "`/spam_sell <status> <on|off>` — toggle selling",
        "",
        "Statuses: `clean` `temp_spam` `perm_spam` `frozen` `unknown`",
    ]
    await message.reply_text("\n".join(lines))


# ── /spam_buy / /spam_sell — toggle buying or selling per spam status ────────

async def _set_spam_toggle(message: Message, side: str) -> None:
    args = message.command[1:]
    if len(args) < 2:
        await message.reply_text(
            f"Usage: `/{side}_spam <status> <on|off>`\n"
            f"Example: `/{side}_spam frozen on`\n\n"
            "Statuses: `clean` `temp_spam` `perm_spam` `frozen` `unknown`\n"
            "Use `/spam_settings` to see current values."
        )
        return

    _ALIAS: dict[str, str] = {
        "clean":         "clean",
        "temp_spam":     "temp_spam",
        "temp":          "temp_spam",
        "temporary_spam":"temp_spam",
        "perm_spam":     "perm_spam",
        "perm":          "perm_spam",
        "permanent_spam":"perm_spam",
        "frozen":        "frozen",
        "unknown":       "unknown",
    }
    status_input = args[0].lower().strip()
    status_key   = _ALIAS.get(status_input)
    if not status_key:
        await message.reply_text(
            f"![⚠️](tg://emoji?id=6129939837823753679) Unknown status `{status_input}`.\n"
            "Valid: `clean` `temp_spam` `perm_spam` `frozen` `unknown`"
        )
        return

    onoff = args[1].lower().strip()
    if onoff not in ("on", "off", "true", "false", "1", "0", "enable", "disable", "yes", "no"):
        await message.reply_text("![⚠️](tg://emoji?id=6129939837823753679) Use `on` or `off`.")
        return

    enabled     = onoff in ("on", "true", "1", "enable", "yes")
    setting_key = f"{side}_enabled_{status_key}"

    from server.utils.database.configdb import set_setting
    await set_setting(setting_key, enabled)

    icon         = "![✅](tg://emoji?id=6129492160497589882)" if enabled else "![❌](tg://emoji?id=6129846551134084367)"
    side_label   = "Buying" if side == "buy" else "Selling"
    status_label = status_input.replace("_", " ").title()
    await message.reply_text(
        f"{icon} **{side_label} `{status_label}`** accounts is now "
        f"**{'enabled ✅' if enabled else 'disabled ❌'}**.\n\n"
        f"Setting saved: `{setting_key}` = `{enabled}`\n"
        f"Use `/spam_settings` to confirm."
    )


@bot.on_message(filters.private & filters.command("spam_buy"))
async def cmd_spam_buy(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return
    await _set_spam_toggle(message, "buy")


@bot.on_message(filters.private & filters.command("spam_sell"))
async def cmd_spam_sell(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return
    await _set_spam_toggle(message, "sell")


# ── /encrypt — encode text with the substitution cipher ─────────────────────

@bot.on_message(filters.private & filters.command("encrypt"))
async def cmd_encrypt(client, message: Message):
    """
    /encrypt <text>

    Encodes any text using the custom substitution cipher.
    No keys, no database — pure character mapping. Fully reversible
    with /decrypt.

    Only accessible to admins.
    """
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if not args:
        await message.reply_text(
            f"{_pe('🔐')} **Encrypt Text**\n\n"
            "Usage: `/encrypt <text>`\n\n"
            "Applies the substitution cipher to your text. "
            "Use `/decrypt` on the result to get the original back.\n\n"
            "Example:\n`/encrypt ab1234`"
        )
        return

    plaintext = " ".join(args)
    from server.utils.sessions.crypto import subst_encrypt
    encoded = subst_encrypt(plaintext)

    await message.reply_text(
        f"{_pe('✅')} **Encrypted**\n\n"
        f"Input:  `{plaintext}`\n"
        f"Output: `{encoded}`"
    )


# ── /decrypt — decode text with the substitution cipher ─────────────────────

@bot.on_message(filters.private & filters.command("decrypt"))
async def cmd_decrypt(client, message: Message):
    """
    /decrypt <encoded_text>

    Decodes text that was encoded by the substitution cipher (e.g. the
    hint stored in Telegram's 2FA hint field, or output of /encrypt).
    No database lookup, no encryption keys — pure reverse character mapping.

    Only accessible to admins.
    """
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if not args:
        await message.reply_text(
            f"{_pe('🔑')} **Decrypt Text**\n\n"
            "Usage: `/decrypt <encoded_text>`\n\n"
            "Reverses the substitution cipher — works on the 2FA hint shown "
            "on the Telegram password screen or any output of `/encrypt`.\n\n"
            "Example:\n`/decrypt hm4917`"
        )
        return

    encoded = " ".join(args)
    from server.utils.sessions.crypto import subst_decrypt
    plaintext = subst_decrypt(encoded)

    await message.reply_text(
        f"{_pe('✅')} **Decrypted**\n\n"
        f"Input:  `{encoded}`\n"
        f"{_pe('🔑')} Output: `{plaintext}`"
    )
