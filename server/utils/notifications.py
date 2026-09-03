"""
Centralized notification dispatcher.

All user-facing Telegram messages that originate from server-side events
(admin actions, background workers, payment webhooks) must go through here.
This module is the single source of truth for every notification template.

Usage:
    from server.utils.notifications import notify
    await notify(user_id, "withdrawal_approved", amount=50.0, tx_hash="0xabc…")

If the bot is not yet started (or the user has blocked the bot) the exception
is caught and logged — it is NEVER re-raised, so callers don't need try/except.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.logging import LOGGER

_log = LOGGER(__name__)

# ── Template registry ─────────────────────────────────────────────────────────
# Each key is an event name; each value is a function that returns the message
# text given the keyword arguments passed to notify().
# Templates use Telegram HTML — no premium / custom emoji markup is used here,
# only standard unicode emoji, so messages always render as icons (never as a
# raw emoji-id or plain text) in any Telegram client.

_TEMPLATES: dict[str, Any] = {}

# Shared visual divider used to give every notification a consistent,
# professional card-like header.
_DIV = "━━━━━━━━━━━━━━━━━━━━"


# ── Presentation helpers ──────────────────────────────────────────────────────
# These only shape the LOOK of a message. No business logic lives here.

def _esc(value: Any) -> str:
    """Escape text so Telegram HTML parse_mode can never break."""
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _b(text: Any) -> str:
    """Bold label (HTML)."""
    return f"<b>{_esc(text)}</b>"


def _q(value: Any) -> str:
    """Wrap any value in quotes (monospace) safely."""
    return f"<code>{_esc(value)}</code>"


def _wrap(text: str) -> str:
    """
    Put the WHOLE message inside a single Telegram blockquote.

    No `expandable` attribute is used, so the quote is always fully shown —
    Telegram never collapses it behind a "Show more" tap.
    """
    body = text.strip("\n")
    return f"<blockquote>{body}</blockquote>"


def _head(icon: str, title: str, suffix: str = "") -> str:
    """Card header: divider, bold uppercase title, divider."""
    tail = f" {suffix}" if suffix else ""
    return f"{_DIV}\n{icon} {_b(title.upper())}{tail}\n{_DIV}\n\n"


def _row(icon: str, label: str, value: Any) -> str:
    """One aligned detail row with the value in quotes."""
    return f"{icon} {_b(label)} {_q(value)}\n"


def _note(icon: str, label: str, value: Any) -> str:
    """Free-text row (reason / note) — also quoted, as requested."""
    return f"{icon} {_b(label)} {_q(value)}\n"


def _foot(*lines: str) -> str:
    """Closing block, separated by a divider."""
    body = "\n".join(l for l in lines if l)
    return f"\n{_DIV}\n{body}"


def _short_addr(wallet_address: str) -> str:
    return (
        f"{wallet_address[:14]}…{wallet_address[-8:]}"
        if len(wallet_address) > 22 else wallet_address
    )


def _t(name: str):
    """Decorator: register a template function."""
    def dec(fn):
        _TEMPLATES[name] = fn
        return fn
    return dec


# ── Withdrawal templates ──────────────────────────────────────────────────────

@_t("withdrawal_submitted")
def _withdrawal_submitted(*, withdrawal_id: str, amount: float, network: str,
                           wallet_address: str, **_) -> str:
    return (
        _head("📤", "Withdrawal Submitted")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + _row("🌐", "Network", network)
        + _row("📬", "Address", _short_addr(wallet_address))
        + _row("📊", "Status", "Under Review")
        + _foot(
            "⏳ Your request is queued for review.",
            "🔔 You will be notified as soon as it is processed.",
        )
    )


@_t("withdrawal_approved")
def _withdrawal_approved(*, withdrawal_id: str, amount: float, network: str,
                          wallet_address: str, tx_hash: str = "", **_) -> str:
    tx_row = (
        _row("🔗", "TX Hash", tx_hash)
        if tx_hash and tx_hash != "manual" else ""
    )
    return (
        _head("✅", "Withdrawal Completed")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + _row("🌐", "Network", network)
        + _row("📬", "Address", _short_addr(wallet_address))
        + tx_row
        + _row("📊", "Status", "Paid")
        + _foot(
            "🚀 Your funds are on their way.",
            "👉 Please check your wallet in a few minutes.",
        )
    )


@_t("withdrawal_rejected")
def _withdrawal_rejected(*, withdrawal_id: str, amount: float, network: str = "",
                          wallet_address: str = "", note: str = "", **_) -> str:
    return (
        _head("❌", "Withdrawal Rejected")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + (_row("🌐", "Network", network) if network else "")
        + (_note("📝", "Reason", note) if note else "")
        + _row("📊", "Status", "Rejected")
        + _foot(
            "💰 Your balance has been refunded automatically.",
            "🆘 If you believe this is an error, please contact support.",
        )
    )


@_t("withdrawal_failed")
def _withdrawal_failed(*, withdrawal_id: str, amount: float, reason: str = "", **_) -> str:
    return (
        _head("⚠️", "Withdrawal Failed")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + (_note("⚠️", "Reason", reason) if reason else "")
        + _row("📊", "Status", "Failed")
        + _foot(
            "💰 Your balance has been refunded automatically.",
            "🔁 Please try again, or contact support if the issue persists.",
        )
    )


@_t("withdrawal_processing")
def _withdrawal_processing(*, withdrawal_id: str, amount: float, network: str,
                            track_id: str = "", **_) -> str:
    return (
        _head("⚙️", "Withdrawal Processing")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + _row("🌐", "Network", network)
        + (_row("🔢", "Track ID", track_id) if track_id else "")
        + _row("📊", "Status", "Processing")
        + _foot(
            "⚙️ Your withdrawal is being processed by the payment gateway.",
            "🔔 You will be notified once it is confirmed on the blockchain.",
        )
    )


@_t("withdrawal_confirming")
def _withdrawal_confirming(*, withdrawal_id: str, amount: float, network: str,
                            tx_hash: str = "", **_) -> str:
    return (
        _head("🔗", "Withdrawal Confirming")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + _row("🌐", "Network", network)
        + (_row("🔗", "TX Hash", tx_hash) if tx_hash else "")
        + _row("📊", "Status", "Confirming On Chain")
        + _foot(
            "🔍 Transaction detected on-chain, waiting for confirmations.",
            "⏱️ This usually takes only a few minutes.",
        )
    )


@_t("withdrawal_expired")
def _withdrawal_expired(*, withdrawal_id: str, amount: float, **_) -> str:
    return (
        _head("⏰", "Withdrawal Expired")
        + _row("🆔", "Request ID", withdrawal_id)
        + _row("💵", "Amount", f"${amount:.2f} USDT")
        + _row("📊", "Status", "Expired")
        + _foot(
            "💰 Your balance has been refunded.",
            "ℹ️ The request was not processed within the allowed time window.",
            "🔁 You may submit a new withdrawal anytime.",
        )
    )


# ── Deposit templates ─────────────────────────────────────────────────────────

@_t("deposit_confirmed")
def _deposit_confirmed(*, deposit_id: str, amount: float, method: str = "", **_) -> str:
    return (
        _head("✅", "Deposit Confirmed")
        + _row("🆔", "Deposit ID", deposit_id)
        + _row("💵", "Amount", f"${amount:.2f}")
        + (_row("🏦", "Method", method) if method else "")
        + _row("📊", "Status", "Credited")
        + _foot(
            "💰 Your balance has been updated successfully.",
            "🛒 Happy trading!",
        )
    )


@_t("deposit_rejected")
def _deposit_rejected(*, deposit_id: str, amount: float, note: str = "", **_) -> str:
    return (
        _head("❌", "Deposit Rejected")
        + _row("🆔", "Deposit ID", deposit_id)
        + _row("💵", "Amount", f"${amount:.2f}")
        + (_note("📝", "Reason", note) if note else "")
        + _row("📊", "Status", "Rejected")
        + _foot("🆘 Please contact support if you believe this is an error.")
    )


# ── Sell request templates ────────────────────────────────────────────────────

@_t("sell_approved")
def _sell_approved(*, request_id: str, phone: str = "", amount: float = 0.0,
                   note: str = "", new_balance: float = 0.0,
                   country_name: str = "", **_) -> str:
    return (
        _head("✅", "Sell Request Approved")
        + _row("🆔", "Request ID", request_id)
        + (_row("🌍", "Country", country_name) if country_name else "")
        + (_row("📱", "Phone", phone) if phone else "")
        + _row("💵", "Amount Credited", f"${amount:.2f}")
        + (_row("💰", "New Balance", f"${new_balance:.2f}") if new_balance else "")
        + (_note("📝", "Note", note) if note else "")
        + _row("📊", "Status", "Approved")
        + _foot(
            "💰 Your earnings are now in your available balance.",
            "🏧 Withdraw anytime from the /wallet menu.",
        )
    )


@_t("sell_rejected")
def _sell_rejected(*, request_id: str, phone: str = "", note: str = "", **_) -> str:
    return (
        _head("❌", "Sell Request Rejected")
        + _row("🆔", "Request ID", request_id)
        + (_row("📱", "Phone", phone) if phone else "")
        + (_note("📝", "Reason", note) if note else "")
        + _row("📊", "Status", "Rejected")
        + _foot(
            "🔄 Your reserved balance has been reversed.",
            "🔁 You may submit a new sell request at any time.",
        )
    )


# ── Bulk sell templates (admin bulk approve/reject from Users Sell Stock) ────

_ACC_STATUS_LABEL = {
    "clean":          "✅ Clean",
    "temporary_spam": "⚠️ Temp Spam",
    "permanent_spam": "🚫 Perm Spam",
    "frozen":         "🧊 Frozen",
    "restricted":     "🔒 Restricted",
    "dead":           "💀 Dead",
    "unknown":        "❓ Unknown",
}


@_t("sell_bulk_approved")
def _sell_bulk_approved(*, items: list = None, total_credited: float = 0.0,
                        new_balance: float = 0.0, note: str = "", **_) -> str:
    items = items or []
    lines = []
    for i, it in enumerate(items, 1):
        status = _ACC_STATUS_LABEL.get(it.get("status", "unknown"), it.get("status", "?"))
        country = it.get("country_name") or it.get("country_code", "?")
        amt = "+${:.2f}".format(it.get("amount", 0) or 0)
        idx = "{:02d}".format(i)
        lines.append(
            _q(idx) + " " + _q(it.get("sell_id", "?")) + " • " + str(country)
            + " • " + _q(it.get("phone", "?")) + " • " + status + " • " + _q(amt)
        )
    body = "\n".join(lines) if lines else "_(no items)_"
    return (
        _head("✅", "Sell Requests Approved", f"({len(items)})")
        + f"{body}\n\n"
        + _row("💵", "Total Credited", f"${total_credited:.2f}")
        + _row("💰", "New Balance", f"${new_balance:.2f}")
        + (_note("📝", "Note", note) if note else "")
        + _foot(
            "💰 Your earnings are now in your available balance.",
            "🏧 Withdraw anytime from the /wallet menu.",
        )
    )


@_t("sell_bulk_rejected")
def _sell_bulk_rejected(*, items: list = None, note: str = "", **_) -> str:
    items = items or []
    lines = []
    for i, it in enumerate(items, 1):
        country = it.get("country_name") or it.get("country_code", "?")
        idx = "{:02d}".format(i)
        lines.append(
            _q(idx) + " " + _q(it.get("sell_id", "?")) + " • " + str(country)
            + " • " + _q(it.get("phone", "?"))
        )
    body = "\n".join(lines) if lines else "_(no items)_"
    return (
        _head("❌", "Sell Requests Rejected", f"({len(items)})")
        + f"{body}\n\n"
        + (_note("📝", "Reason", note) if note else "")
        + _foot(
            "🔄 Reserved balance for these requests has been reversed.",
            "🔁 You may submit new sell requests at any time.",
        )
    )


# ── Order templates ───────────────────────────────────────────────────────────

@_t("order_cancelled_refund")
def _order_cancelled_refund(*, order_id: str, amount: float, reason: str = "", **_) -> str:
    return (
        _head("🔄", "Order Cancelled & Refunded")
        + _row("🆔", "Order ID", order_id)
        + _row("💵", "Refunded", f"${amount:.2f}")
        + _note("📝", "Reason", reason or "Admin cancellation")
        + _row("📊", "Status", "Refunded")
        + _foot("💰 Your balance has been fully restored.")
    )


@_t("balance_adjusted")
def _balance_adjusted(*, amount: float, mode: str = "add", note: str = "", **_) -> str:
    verb = "credited to" if mode == "add" else "adjusted in"
    action = "added to" if amount > 0 else "deducted from"
    return (
        _head("💰", "Balance Adjustment")
        + _row("🔧", "Action", f"{action.title()} your balance")
        + _row("💵", "Amount", f"${abs(amount):.2f}")
        + (_note("📝", "Note", note) if note else "")
        + _foot("✅ Your current balance has been updated.")
    )


@_t("account_banned")
def _account_banned(*, reason: str = "", **_) -> str:
    return (
        _head("🚫", "Account Suspended")
        + _row("🏷️", "Service", "TG-Fox API")
        + _row("📊", "Status", "Suspended")
        + (_note("📝", "Reason", reason) if reason else "")
        + _foot(
            "🔒 Access to the marketplace is currently disabled.",
            "🆘 If you believe this is a mistake, please contact support.",
        )
    )


@_t("account_unbanned")
def _account_unbanned(**_) -> str:
    return (
        _head("✅", "Account Reinstated")
        + _row("🏷️", "Service", "TG-Fox API")
        + _row("📊", "Status", "Active")
        + _foot(
            "🔓 Your suspension has been lifted.",
            "🛒 You may now use the marketplace again.",
        )
    )


# ── Sell auto-recheck templates ───────────────────────────────────────────────

@_t("sell_auto_rejected")
def _sell_auto_rejected(
    *, request_id: str, phone: str = "", reasons: list = None,
    warnings: list = None, note: str = "", **_
) -> str:
    reason_lines = ""
    if reasons:
        reason_lines = "\n" + _b("Failed checks:") + "\n" + "\n".join(f"• {_q(r)}" for r in reasons) + "\n"
    warn_lines = ""
    if warnings:
        warn_lines = "\n⚠️ " + _b("Warnings:") + "\n" + "\n".join(f"• {_q(w)}" for w in warnings) + "\n"
    return (
        _head("❌", "Payment Verification Failed")
        + _row("🆔", "Request ID", request_id)
        + (_row("📱", "Phone", phone) if phone else "")
        + _row("📊", "Status", "Rejected")
        + reason_lines
        + warn_lines
        + (_note("📝", "Note", note) if note else "")
        + _foot(
            "🔍 Our automated 24-hour re-verification found that your session "
            "no longer meets acceptance criteria.",
            "🔄 Your reserved pending balance has been reversed.",
            "",
            "📋 " + _b("What happens next"),
            "🆘 If you believe this is an error, contact support with your Request ID.",
            "🔁 You may re-submit a new sell request once the issue is resolved.",
        )
    )


@_t("sell_payment_held")
def _sell_payment_held(
    *, request_id: str, phone: str = "", reason: str = "", retry_hours: int = 24, **_
) -> str:
    return (
        _head("⏳", "Payment Verification Pending")
        + _row("🆔", "Request ID", request_id)
        + (_row("📱", "Phone", phone) if phone else "")
        + (_note("📝", "Reason", reason) if reason else "")
        + _row("🕒", "Re-check In", f"~{retry_hours} hours")
        + _row("📊", "Status", "On Hold")
        + _foot(
            "🌐 Verification hit a temporary network issue.",
            "✅ No action is needed from you.",
            "🔔 We will notify you as soon as the check completes.",
        )
    )


@_t("sell_force_approved")
def _sell_force_approved(
    *, request_id: str, phone: str = "", amount: float = 0.0,
    new_balance: float = 0.0, warnings: list = None, **_
) -> str:
    warn_lines = ""
    if warnings:
        warn_lines = (
            "\n⚠️ " + _b("Admin force-approved despite these warnings:") + "\n"
            + "\n".join(f"• {_q(w)}" for w in warnings) + "\n"
        )
    return (
        _head("✅", "Sell Request Approved", "(Admin Override)")
        + _row("🆔", "Request ID", request_id)
        + (_row("📱", "Phone", phone) if phone else "")
        + _row("💵", "Amount Credited", f"${amount:.2f}")
        + (_row("💰", "New Balance", f"${new_balance:.2f}") if new_balance else "")
        + _row("📊", "Status", "Approved")
        + warn_lines
        + _foot("💰 Your earnings have been credited to your available balance.")
    )


# ── Admin broadcast ───────────────────────────────────────────────────────────

@_t("admin_message")
def _admin_message(*, message: str, **_) -> str:
    return (
        _head("📢", "Message From Admin")
        + f"{_esc(message)}\n"
        + _foot("🦊 TG-Fox API • Official Announcement")
    )


# ── Transport ─────────────────────────────────────────────────────────────────
# Different Telegram libraries expect a different `parse_mode` value:
#   • aiogram / python-telegram-bot → the string "HTML"
#   • Pyrogram v2                   → enums.ParseMode.HTML  (a plain "HTML"
#                                     string raises: Invalid parse mode "HTML")
#   • Telethon                      → the lowercase string "html"
# We resolve it ONCE, lazily, and cache it. If every mode is rejected we fall
# back to sending the message as plain text (tags stripped) so the user still
# gets the notification instead of nothing.

import re as _re

_PARSE_MODE: Any = None          # cached resolved parse mode
_PARSE_MODE_RESOLVED = False


def _html_parse_mode() -> Any:
    """Best-effort HTML parse mode for whichever bot library is installed."""
    try:
        from pyrogram import enums  # type: ignore
        return enums.ParseMode.HTML
    except Exception:
        pass
    return "HTML"


_TAG_RE = _re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Strip HTML tags / unescape entities for a plain-text fallback send."""
    out = _TAG_RE.sub("", text)
    return (
        out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


async def _send(user_id: int, html_text: str) -> None:
    """
    Send an HTML message, auto-detecting the parse mode the bot library wants.
    Raises only if the message could not be delivered at all.
    """
    global _PARSE_MODE, _PARSE_MODE_RESOLVED

    from server import bot

    candidates: list[Any]
    if _PARSE_MODE_RESOLVED:
        candidates = [_PARSE_MODE]
    else:
        candidates = [_html_parse_mode(), "HTML", "html"]

    last_exc: Exception | None = None
    for mode in candidates:
        try:
            await bot.send_message(user_id, html_text, parse_mode=mode)
            _PARSE_MODE, _PARSE_MODE_RESOLVED = mode, True
            return
        except Exception as exc:
            msg = str(exc).lower()
            # Only keep trying when the failure is about the parse mode itself.
            if "parse mode" not in msg and "parse_mode" not in msg:
                raise
            last_exc = exc

    # Every HTML mode was rejected → deliver as plain text rather than nothing.
    _log.warning("send: HTML parse mode unsupported (%s) — sending plain text", last_exc)
    await bot.send_message(user_id, _plain(html_text))


# ── Dispatcher ────────────────────────────────────────────────────────────────

async def notify(user_id: int, event: str, **kwargs) -> bool:
    """
    Send a notification to a user by event name.

    Returns True if sent successfully, False on any error.
    Never raises — failures are logged and swallowed.
    """
    template_fn = _TEMPLATES.get(event)
    if not template_fn:
        _log.warning("notify: unknown event '%s' — no template registered", event)
        return False

    try:
        text = template_fn(**kwargs)
    except Exception as exc:
        _log.error("notify: template '%s' raised: %s", event, exc)
        return False

    try:
        await _send(user_id, _wrap(text))
        return True
    except Exception as exc:
        # User may have blocked the bot — don't crash the caller
        _log.warning("notify: could not DM user %s (event=%s): %s", user_id, event, exc)
        return False


async def notify_many(user_ids: list[int], event: str, **kwargs) -> dict[int, bool]:
    """Send the same notification to multiple users concurrently."""
    results = await asyncio.gather(
        *(notify(uid, event, **kwargs) for uid in user_ids),
        return_exceptions=True,
    )
    return {uid: (r is True) for uid, r in zip(user_ids, results)}


async def send_raw(user_id: int, text: str) -> bool:
    """Send a raw pre-composed message to a user. Used by admin 'Send Message' UI."""
    try:
        await _send(user_id, _wrap(_esc(text)))
        return True
    except Exception as exc:
        _log.warning("send_raw: could not DM user %s: %s", user_id, exc)
        return False
