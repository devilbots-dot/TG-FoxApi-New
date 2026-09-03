

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
    '✅': "6129492160497589882",
    '❌': "6129846551134084367",
    '🌐': "6296303781126604562",
    '🔐': "6129550284290006595",
    '🔴': "6129846551134084367",
    '🟢': "6129492160497589882",
}


def _pe(emoji):
    """Return premium-emoji markdown for `emoji`, or the raw emoji as fallback."""
    _eid = PREMIUM_EMOJIS.get(emoji)
    return f"![{emoji}](tg://emoji?id={_eid})" if _eid else emoji


"""
Proxy Admin Plugin — manage per-country Telethon proxies via bot.

Commands (owner / sudoers only):
  /add_proxy <CC> <host> <port> [type] [user] [pass]
      CC = ISO country code or * (wildcard for all countries)
      type = socks5 (default) | http
      Examples:
        /add_proxy IN 1.2.3.4 1080
        /add_proxy RU 1.2.3.4 1080 socks5
        /add_proxy * 5.5.5.5 3128 http
        /add_proxy UZ 9.9.9.9 1080 socks5 myuser mypass

  /list_proxies [CC]
      List all proxies, or those for one country.

  /del_proxy <proxy_id>
      Delete a proxy by its ID.

  /proxy_login <CC> <on|off>
      Enable or disable proxy-based login for a country.
      When enabled, OTP listener uses this country's proxy (or wildcard).
      Falls back to direct connection if no proxy available or proxy fails.

  /proxy_login
      Show which countries currently have proxy_login_enabled = True.
"""

from pyrogram import filters
from pyrogram.types import Message, InlineKeyboardMarkup
from server.utils.bot_utils import Btn as InlineKeyboardButton

from server import bot, LOGGER
from server.utils.bot_utils import is_admin as _is_admin
from server.utils.database.proxydb import test_proxy_connection

_log = LOGGER(__name__)


# ── /add_proxy ────────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("add_proxy"))
async def add_proxy_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]  # drop command itself
    # Minimum: CC host port
    if len(args) < 3:
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) **Usage:**\n"
            "`/add_proxy <CC> <host> <port> [type] [user] [pass]`\n\n"
            "**CC** = ISO country code or `*` (wildcard)\n"
            "**type** = `socks5` (default) | `http`\n\n"
            "**Examples:**\n"
            "`/add_proxy IN 1.2.3.4 1080`\n"
            "`/add_proxy RU 1.2.3.4 1080 socks5 myuser mypass`\n"
            "`/add_proxy * 5.5.5.5 3128 http`"
        )
        return

    cc = args[0].upper()
    host = args[1]
    try:
        port = int(args[2])
    except ValueError:
        await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Port must be a number.")
        return

    proxy_type = args[3].lower() if len(args) > 3 else "socks5"
    if proxy_type not in ("socks5", "http"):
        await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Proxy type must be `socks5` or `http`.")
        return

    username = args[4] if len(args) > 4 else ""
    password = args[5] if len(args) > 5 else ""

    # Test before saving — reject fake or dead proxies immediately.
    test = await test_proxy_connection(
        host=host, port=port, proxy_type=proxy_type,
        username=username, password=password,
    )
    if not test["ok"]:
        await message.reply_text(
            f"![❌](tg://emoji?id=6129846551134084367) **Proxy test failed** — not added.\n"
            f"• Server: `{proxy_type.upper()} {host}:{port}`\n"
            f"• Error: `{test['detail']}`\n\n"
            f"Check the host, port, type and auth, then try again."
        )
        return

    from server.utils.database.proxydb import add_proxy
    proxy_id = await add_proxy(
        country_code=cc,
        host=host,
        port=port,
        proxy_type=proxy_type,
        username=username,
        password=password,
    )

    cc_display = "All countries (wildcard)" if cc == "*" else cc
    auth_line = f"\n• Auth: `{username}:{password}`" if username else ""
    await message.reply_text(
        f"![✅](tg://emoji?id=6129492160497589882) **Proxy added!** (tested `{test['latency_ms']}ms`)\n"
        f"• ID: `{proxy_id}`\n"
        f"• Country: `{cc_display}`\n"
        f"• Server: `{proxy_type.upper()} {host}:{port}`"
        f"{auth_line}\n\n"
        f"Enable proxy login with:\n"
        f"`/proxy_login {cc} on`"
    )


# ── /list_proxies ─────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("list_proxies"))
async def list_proxies_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]

    if args:
        cc = args[0].upper()
        from server.utils.database.proxydb import get_proxies_for_country
        proxies = await get_proxies_for_country(cc)
        header = f"![🌐](tg://emoji?id=6296303781126604562) **Proxies for `{cc}`** ({len(proxies)} total)\n"
    else:
        from server.utils.database.proxydb import list_all_proxies
        proxies = await list_all_proxies()
        header = f"![🌐](tg://emoji?id=6296303781126604562) **All Proxies** ({len(proxies)} total)\n"

    if not proxies:
        await message.reply_text(f"{header}\nNo proxies found.")
        return

    lines = [header]
    for p in proxies:
        status = "![🟢](tg://emoji?id=6129492160497589882)" if p["is_active"] else "![🔴](tg://emoji?id=6129846551134084367)"
        auth = f" ![🔐](tg://emoji?id=6129550284290006595)" if p.get("username") else ""
        fail = f" ![❌](tg://emoji?id=6129846551134084367){p['fail_count']}" if p.get("fail_count", 0) > 0 else ""
        lines.append(
            f"{status} `{p['proxy_id']}` `{p['country_code']}` "
            f"{p['type'].upper()} `{p['host']}:{p['port']}`{auth}{fail}"
        )

    await message.reply_text("\n".join(lines))


# ── /del_proxy ────────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("del_proxy"))
async def del_proxy_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]
    if not args:
        await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Usage: `/del_proxy <proxy_id>`")
        return

    proxy_id = args[0].upper()
    from server.utils.database.proxydb import delete_proxy
    deleted = await delete_proxy(proxy_id)

    if deleted:
        await message.reply_text(f"![✅](tg://emoji?id=6129492160497589882) Proxy `{proxy_id}` deleted.")
    else:
        await message.reply_text(f"![❌](tg://emoji?id=6129846551134084367) Proxy `{proxy_id}` not found.")


# ── /proxy_login ──────────────────────────────────────────────────────────────

@bot.on_message(filters.private & filters.command("proxy_login"))
async def proxy_login_cmd(client, message: Message):
    if not await _is_admin(message.from_user.id):
        return

    args = message.command[1:]

    # No args → show status of all countries
    if not args:
        from server.utils.database.countrydb import get_all_countries
        countries = await get_all_countries()
        enabled = [c for c in countries if c.get("proxy_login_enabled")]
        if not enabled:
            await message.reply_text(
                "📡 **Proxy Login Status**\n\nNo countries have proxy login enabled.\n\n"
                "Enable with: `/proxy_login <CC> on`"
            )
            return
        lines = ["📡 **Proxy Login Enabled Countries:**\n"]
        for c in enabled:
            lines.append(f"• `{c['code']}` — {c.get('country_name', c['code'])}")
        await message.reply_text("\n".join(lines))
        return

    if len(args) < 2:
        await message.reply_text(
            "![❌](tg://emoji?id=6129846551134084367) **Usage:**\n"
            "`/proxy_login <CC> on|off`\n\n"
            "**Example:**\n"
            "`/proxy_login IN on`\n"
            "`/proxy_login RU off`"
        )
        return

    cc = args[0].upper()
    toggle = args[1].lower()
    if toggle not in ("on", "off"):
        await message.reply_text("![❌](tg://emoji?id=6129846551134084367) Use `on` or `off`.")
        return

    from server.utils.database.countrydb import get_country, set_country_field
    country = await get_country(cc)
    if not country:
        await message.reply_text(f"![❌](tg://emoji?id=6129846551134084367) Country `{cc}` not found in database.")
        return

    enabled = toggle == "on"
    await set_country_field(cc, "proxy_login_enabled", enabled)

    emoji = "![✅](tg://emoji?id=6129492160497589882)" if enabled else "![❌](tg://emoji?id=6129846551134084367)"
    status_text = "enabled" if enabled else "disabled"
    await message.reply_text(
        f"{emoji} Proxy login **{status_text}** for `{cc}` — {country.get('country_name', cc)}\n\n"
        + (
            f"OTP listener will now route through your configured proxy for `{cc}`.\n"
            f"Falls back to server IP if proxy is unavailable."
            if enabled else
            f"OTP listener will connect directly (server IP) for `{cc}`."
        )
    )
