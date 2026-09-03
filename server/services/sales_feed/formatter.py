"""Sales-feed message formatter for completed real purchases only."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Literal

from server.services.sales_feed.privacy import mask_username


def _now_str() -> str:
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %I:%M %p IST")


def _flag(country_code: str) -> str:
    """Convert a two-letter country code to its regional indicator flag."""
    code = country_code.upper().strip()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 0x1F1A5) + chr(ord(code[1]) + 0x1F1A5)


def _mask_user_id(user_id: int) -> str:
    value = str(user_id)
    return value if len(value) < 4 else f"{value[:2]} TG Fox Api {value[-2:]}"


def _fake_masked_user_id() -> str:
    return f"{random.randint(10, 99)} TG Fox Api {random.randint(10, 99)}"


def format_account_purchase(
    *,
    user_id: int,
    username: str | None,
    country_code: str,
    country_name: str,
    price: float,
    order_id: str,
    new_balance: float,
) -> str:
    """Format a completed, real account purchase notification."""
    del new_balance  # retained in the stable call contract; never published.
    flag = _flag(country_code)
    uid_masked = _mask_user_id(user_id)
    uname_masked = mask_username(username)
    buyer_line = f"👤 **Buyer:** @{uname_masked}" if uname_masked else f"👤 **Buyer ID:** `{uid_masked}`"
    return "\n".join([
        "╔════════════════════╗",
        "      🛒 **NEW PURCHASE**",
        "╚════════════════════╝",
        "",
        f"🌍 **Country:** {flag} {country_name} (`{country_code.upper()}`)",
        f"💵 **Amount Paid:** `${price:.2f} USDT`",
        "📦 **Type:** Account (OTP Delivery)",
        "",
        buyer_line,
        f"🆔 **Order:** `#{order_id[-8:]}`",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"🕐 **Time:** `{_now_str()}`",
        "",
        "**🛒 [Buy Now](https://t.me/TGfoxapirobot)**",
    ])


def format_session_purchase(
    *,
    user_id: int,
    username: str | None,
    country_code: str,
    country_name: str,
    quantity: int,
    price_per: float,
    total_price: float,
    order_id: str,
) -> str:
    """Format a completed, real session purchase notification."""
    flag = _flag(country_code)
    uid_masked = _mask_user_id(user_id)
    uname_masked = mask_username(username)
    buyer_line = f"👤 **Buyer:** @{uname_masked}" if uname_masked else f"👤 **Buyer ID:** `{uid_masked}`"
    return "\n".join([
        "╔════════════════════╗",
        "      📲 **NEW PURCHASE**",
        "╚════════════════════╝",
        "",
        f"🌍 **Country:** {flag} {country_name} (`{country_code.upper()}`)",
        f"📦 **Quantity:** `{quantity}x` session{'s' if quantity != 1 else ''}",
        f"💵 **Price/Unit:** `${price_per:.2f} USDT`",
        f"💰 **Total Paid:** `${total_price:.2f} USDT`",
        "📱 **Type:** Session File (Ready-Made)",
        "",
        buyer_line,
        f"🆔 **Order:** `#{order_id[-8:]}`",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"🕐 **Time:** `{_now_str()}`",
        "",
        "**🛒 [Buy Now](https://t.me/TGfoxapirobot)**",
    ])


def format_fake_purchase(
    *,
    product_type: Literal["account", "session"],
    country_code: str,
    country_name: str,
    quantity: int,
    price_per: float,
    total_price: float,
    fake_username: str,
    payment_method: str,
    device: str,
) -> str:
    """Format a synthetic display-only purchase like the legacy feed."""
    del fake_username, payment_method, device
    flag = _flag(country_code)
    time_str = _now_str()
    uid_masked = _fake_masked_user_id()
    if product_type == "session":
        lines = [
            "╔════════════════════╗",
            "      📲 **NEW PURCHASE**",
            "╚════════════════════╝",
            "",
            f"🌍 **Country:** {flag} {country_name} (`{country_code.upper()}`)",
            f"📦 **Quantity:** `{quantity}x` session{'s' if quantity != 1 else ''}",
            f"💵 **Price/Unit:** `${price_per:.2f} USDT`",
            f"💰 **Total Paid:** `${total_price:.2f} USDT`",
            "📱 **Type:** Session File (Ready-Made)",
        ]
    else:
        lines = [
            "╔════════════════════╗",
            "      🛒 **NEW PURCHASE**",
            "╚════════════════════╝",
            "",
            f"🌍 **Country:** {flag} {country_name} (`{country_code.upper()}`)",
            f"💵 **Amount Paid:** `${total_price:.2f} USDT`",
            "📦 **Type:** Account (OTP Delivery)",
        ]
    lines += [
        "",
        f"👤 **Buyer ID:** `{uid_masked}`",
        f"🆔 **Order:** `#{''.join(filter(str.isdigit, time_str))[-8:]}`",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"🕐 **Time:** `{time_str}`",
        "",
        "**🛒 [Buy Now](https://t.me/TGfoxapirobot)**",
    ]
    return "\n".join(lines)


def format_test_message(chat_id: str) -> str:
    return (
        "✅ **Sales Feed Test**\n\n"
        "📡 Real-sales feed is active and connected.\n"
        f"📬 **Destination Chat:** `{chat_id}`\n"
        f"🕐 **Time:** {_now_str()}\n\n"
        "_This is a test notification from the admin panel._"
    )
