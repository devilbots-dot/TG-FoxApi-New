"""
Shared bot plugin utilities.

Centralises helpers that were previously duplicated across start.py,
market.py, wallet.py, session_admin.py, and proxy_admin.py.
"""

import re as _re
import struct as _struct
from typing import Optional

from pyrogram import enums
from pyrogram.types import CallbackQuery, InlineKeyboardButton as _IKB

# ── TL-level icon_custom_emoji_id support ────────────────────────────────────
#
# Telegram MTProto layer 223+ added two new TL constructors:
#
#   keyboardButtonStyle#4fdd3430
#       flags:#
#       bg_primary:flags.0?true   bg_danger:flags.1?true   bg_success:flags.2?true
#       icon:flags.3?long                          ← the emoji ID
#
#   keyboardButtonCallback#e62bc960   ← updated ID (was 0x35bbdb6b at layer 220)
#       flags:#
#       requires_password:flags.0?true
#       style:flags.10?KeyboardButtonStyle
#       text:string   data:bytes
#
# Pyrofork 2.3.69 ships layer 220 and doesn't expose these constructors.
# We hand-craft the TL bytes and return a thin _TLRaw wrapper whose
# .write() is called by pyrogram's Vector() serialiser when building
# the ReplyInlineMarkup payload.


def _tl_str(s: str) -> bytes:
    """TL string encoding (length-prefixed, 4-byte aligned)."""
    b = s.encode("utf-8")
    n = len(b)
    head = bytes([n]) if n <= 253 else b"\xfe" + _struct.pack("<I", n)[:3]
    total = len(head) + n
    return head + b + b"\x00" * ((-total) % 4)


def _tl_bytes_enc(data: bytes) -> bytes:
    """TL bytes encoding (length-prefixed, 4-byte aligned)."""
    n = len(data)
    head = bytes([n]) if n <= 253 else b"\xfe" + _struct.pack("<I", n)[:3]
    total = len(head) + n
    return head + data + b"\x00" * ((-total) % 4)


# keyboardButtonStyle flag bits
_STYLE_BG_PRIMARY = 1 << 0   # highlighted / accent (blue tint)
_STYLE_BG_DANGER  = 1 << 1   # red background
_STYLE_BG_SUCCESS = 1 << 2   # green background
_STYLE_ICON       = 1 << 3   # custom emoji icon (icon:flags.3?long)


def _tl_style(icon_id: int = 0, color_flags: int = 0) -> bytes:
    """Serialize keyboardButtonStyle#4fdd3430.

    Args:
        icon_id:     Custom emoji ID (0 = no icon).
        color_flags: OR of _STYLE_BG_* constants (0 = no colour tint).
    """
    flags = color_flags
    if icon_id:
        flags |= _STYLE_ICON
    buf = _struct.pack("<I", 0x4FDD3430) + _struct.pack("<I", flags)
    if icon_id:
        buf += _struct.pack("<q", icon_id)
    return buf


def _tl_callback_with_style(
    text: str,
    data: bytes,
    icon_id: int = 0,
    color_flags: int = 0,
    requires_password: bool = False,
) -> bytes:
    """Full TL bytes for keyboardButtonCallback#e62bc960 with style.

    Bot API 9.4 (Feb 2026): keyboardButtonStyle carries both the
    icon_custom_emoji_id and the button colour (primary/success/danger).
    """
    btn_flags = 1 << 10       # bit 10 = style present
    if requires_password:
        btn_flags |= 1 << 0
    return (
        _struct.pack("<I", 0xE62BC960)
        + _struct.pack("<I", btn_flags)
        + _tl_style(icon_id, color_flags)
        + _tl_str(text)
        + _tl_bytes_enc(data)
    )


class _TLRaw:
    """Lightweight TLObject substitute that holds pre-serialized TL bytes.

    pyrogram's Vector.__new__ calls i.write() (no args) on each button raw
    type collected by InlineKeyboardMarkup.write() → KeyboardButtonRow.
    Returning a _TLRaw instance from InlineKeyboardButton.write() is enough
    to inject our hand-crafted bytes into the outgoing MTProto payload.
    """

    __slots__ = ("_b",)

    def __init__(self, b: bytes) -> None:
        self._b = b

    def write(self, *_) -> bytes:          # noqa: D401
        return self._b


class _BtnWithIcon(_IKB):
    """InlineKeyboardButton subclass that injects icon_custom_emoji_id."""

    async def write(self, client):         # type: ignore[override]
        return self._tl_raw                # set by Btn() after construction


# ── Semantic icon + colour map ────────────────────────────────────────────────
# Maps lowercase text keywords → (icon_id, color_flag).
# Bot API 9.4 (Feb 2026): keyboardButtonStyle carries BOTH the custom emoji
# icon AND the native button colour (primary/success/danger) in one object.
# Tested in insertion order — more-specific phrases first.
#
# color_flag values: 0 = no tint | _STYLE_BG_PRIMARY | _STYLE_BG_SUCCESS | _STYLE_BG_DANGER

_ICON: dict[str, tuple[int, int]] = {
    # ── Buy  (primary action → PRIMARY tint) ─────────────────────────────────
    "buy now":           (6028346797368283073, _STYLE_BG_PRIMARY),
    "buy account":       (6028346797368283073, _STYLE_BG_PRIMARY),
    "buy session":       (5409150592188690356, _STYLE_BG_PRIMARY),
    "buy":               (6028346797368283073, _STYLE_BG_PRIMARY),
    # ── Sell  (neutral — no tint) ────────────────────────────────────────────
    "sell account":      (6028338546736107668, 0),
    "sell session":      (6039630677182254664, 0),
    "sell / buy session":      (5409150592188690356, 0),
    "start selling":     (6028338546736107668, 0),
    "sell another":      (6028338546736107668, 0),
    "sell":              (6028338546736107668, 0),
    # ── Approve / yes / positive  (SUCCESS = green) ───────────────────────────
    "approve":           (5767199127775481841, _STYLE_BG_SUCCESS),
    "enable":            (5767199127775481841, _STYLE_BG_SUCCESS),
    "yes":               (5767199127775481841, _STYLE_BG_SUCCESS),
    # ── Reject / cancel / negative  (DANGER = red) ────────────────────────────
    "reject":            (5773677501825945508, _STYLE_BG_DANGER),
    "disable":           (5773677501825945508, _STYLE_BG_DANGER),
    "cancel":            (5773677501825945508, _STYLE_BG_DANGER),
    "logout":            (6030764546128352351, _STYLE_BG_DANGER),
    "no":                (5773677501825945508, _STYLE_BG_DANGER),
    # ── Navigation  (no tint) ────────────────────────────────────────────────
    "back to market":    (6035162669948867129, 0),
    "back to wallet":    (6035162669948867129, 0),
    "back to profil":    (6035162669948867129, 0),
    "back":              (6035162669948867129, 0),
    "prev":              (6035162669948867129, 0),
    "next":              (5767356727305441799, 0),
    "→":                 (5767356727305441799, 0),
    # ── Wallet & finance ─────────────────────────────────────────────────────
    "manage address":    (6034969813032374911, 0),
    "pay now":           (5942734685976138521, _STYLE_BG_PRIMARY),
    "pay":               (5942734685976138521, _STYLE_BG_PRIMARY),
    "deposit":           (5256186332669035163, 0),
    "withdraw":          (6030466823290360017, 0),
    "wallet":            (6032636795387121097, 0),
    # ── Transactions & history ───────────────────────────────────────────────
    "all history":       (6039381989985882045, 0),
    "transaction":       (6039636621416993073, 0),
    "purchase log":      (6039636621416993073, 0),
    "purchase":          (6041919344995209164, 0),
    "withdrawals":       (6030466823290360017, 0),
    "deposits":          (5904258298764334001, 0),
    # ── Profile & social ─────────────────────────────────────────────────────
    "refer":             (5776233299424843260, 0),
    "profile":           (5256143829672672750, 0),
    # ── Keys & security ──────────────────────────────────────────────────────
    "api key":           (6034969813032374911, 0),
    "regenerate":        (6037364759811068375, 0),
    "regen":             (6037364759811068375, 0),
    "new otp":           (6037364759811068375, 0),
    "get new":           (6037364759811068375, 0),
    "2fa":               (5938473438468378529, 0),
    "password":          (5938473438468378529, 0),
    "otp":               (5922272602784534896, 0),
    # ── Sessions ─────────────────────────────────────────────────────────────
    "sessions":          (6039522349517115015, 0),
    "session":           (6039522349517115015, 0),
    # ── Market & lists ───────────────────────────────────────────────────────
    "price list":        (6039381989985882045, 0),
    "view all":          (6037397706505195857, 0),
    "my request":        (5881806211195605908, 0),
    "request":           (5881806211195605908, 0),
    "market":            (5922272602784534896, 0),
    "view":              (6037397706505195857, 0),
    # ── Language ─────────────────────────────────────────────────────────────
    "language":          (5262470999399487110, 0),
    # ── Notifications & support ──────────────────────────────────────────────
    "receive update":    (6039486778597970865, 0),
    "update":            (6039486778597970865, 0),
    "support":           (5937999673510858217, 0),
    "log":               (6039636621416993073, 0),
    # ── Extra / misc ─────────────────────────────────────────────────────────
    "extra":             (6037533152593842454, 0),
}


def _icon_for(text: str) -> tuple[int, int] | None:
    """Return (icon_id, color_flag) for the first matching keyword, or None."""
    t = text.lower()
    for kw, entry in _ICON.items():
        if kw in t:
            return entry
    return None


# ── Callback-data → icon map ─────────────────────────────────────────────────
# Because button labels are translated per user language, keyword matching on
# the label text won't find "buy"/"sell"/"deposit" etc. in Arabic, Chinese,
# Russian, Farsi, Bengali, etc. We fall back to matching the stable ASCII
# callback_data so translated buttons keep their premium custom-emoji icons.
_CB_ICON: dict[str, tuple[int, int]] = {
    "buy_page":              (5359805631320571519, _STYLE_BG_PRIMARY),
    "buy_view_all":          (6037397706505195857, 0),
    "buy_session_page":      (5904258298764334001, _STYLE_BG_PRIMARY),
    "buy_search":            (5316977222467206948, 0),
    "sell_account_start":    (6028338546736107668, 0),
    "sell_session_start":    (6039630677182254664, 0),
    "sell_my_requests":      (5881806211195605908, 0),
    "sell_page":             (6028338546736107668, 0),
    "sell_session_cancel":   (5773677501825945508, _STYLE_BG_DANGER),
    "sessions_menu":         (5409150592188690356, 0),
    "wallet_deposit":        (5256186332669035163, 0),
    "wallet_withdraw":       (6030466823290360017, 0),
    "show_wallet":           (5443127283898405358, 0),
    "wallet_addr_menu":      (6034969813032374911, 0),
    "wallet_set_bep20":      (6034969813032374911, 0),
    "wallet_set_trc20":      (6034969813032374911, 0),
    "show_profile":          (5256143829672672750, 0),
    "show_refer":            (5776233299424843260, 0),
    "show_transactions":     (6039381989985882045, 0),
    "show_apikey":           (6034969813032374911, 0),
    "regen_apikey":          (6037364759811068375, 0),
    "tx_all":                (6039381989985882045, 0),
    "tx_deposits":           (5904258298764334001, 0),
    "tx_withdrawals":        (6030466823290360017, 0),
    "tx_purchases":          (6041919344995209164, 0),
    "extra_menu":            (6037533152593842454, 0),
    "choose_lang":           (5262470999399487110, 0),
    "back_home":             (6035162669948867129, 0),
    "dep_back_methods":      (6035162669948867129, 0),
}


def _icon_for_callback(cb) -> tuple[int, int] | None:
    """Return (icon_id, color_flag) matching the callback_data prefix."""
    if not cb:
        return None
    s = cb.decode("utf-8", "ignore") if isinstance(cb, (bytes, bytearray)) else str(cb)
    for key in sorted(_CB_ICON.keys(), key=len, reverse=True):
        if s == key or s.startswith(key + "_") or s.startswith(key):
            return _CB_ICON[key]
    return None


# ── Inline button helper ──────────────────────────────────────────────────────

_EMOJI_MD = _re.compile(r'!\[([^\]]+)\]\(tg://emoji\?id=\d+\)\s*')

# Matches leading Unicode emoji / symbol characters (supplementary planes,
# Misc Symbols, Dingbats, variation selectors, ZWJ) so we can strip them
# from button labels when a TL-injected custom icon will replace them.
_PLAIN_EMOJI_START = _re.compile(
    r'^[\u2000-\u32FF\uFE00-\uFE0F\U0001F000-\U0001FFFF\U00010000-\U0010FFFF]+\s*'
)

# Matches ![emoji](tg://emoji?id=ID) markdown and converts it to the HTML
# <tg-emoji> tag so it renders correctly when parse_mode=HTML is used.
_EMOJI_MD_TO_HTML = _re.compile(r'!\[([^\]]+)\]\(tg://emoji\?id=(\d+)\)')


def md_emoji_to_html(text: str) -> str:
    """Convert ``![emoji](tg://emoji?id=ID)`` markdown to HTML ``<tg-emoji>`` tags."""
    return _EMOJI_MD_TO_HTML.sub(
        lambda m: f'<tg-emoji emoji-id="{m.group(2)}">{m.group(1)}</tg-emoji>',
        text,
    )


# ── Small-caps unicode font for English button labels ────────────────────────
# Maps A-Z / a-z → small-caps unicode letters. Digits, symbols, emojis,
# whitespace, and non-Latin scripts pass through unchanged.
_SMALLCAPS_MAP = str.maketrans({
    'a': 'ᴀ', 'b': 'ʙ', 'c': 'ᴄ', 'd': 'ᴅ', 'e': 'ᴇ', 'f': 'ꜰ', 'g': 'ɢ',
    'h': 'ʜ', 'i': 'ɪ', 'j': 'ᴊ', 'k': 'ᴋ', 'l': 'ʟ', 'm': 'ᴍ', 'n': 'ɴ',
    'o': 'ᴏ', 'p': 'ᴘ', 'q': 'ǫ', 'r': 'ʀ', 's': 'ꜱ', 't': 'ᴛ', 'u': 'ᴜ',
    'v': 'ᴠ', 'w': 'ᴡ', 'x': 'x', 'y': 'ʏ', 'z': 'ᴢ',
    'A': 'ᴀ', 'B': 'ʙ', 'C': 'ᴄ', 'D': 'ᴅ', 'E': 'ᴇ', 'F': 'ꜰ', 'G': 'ɢ',
    'H': 'ʜ', 'I': 'ɪ', 'J': 'ᴊ', 'K': 'ᴋ', 'L': 'ʟ', 'M': 'ᴍ', 'N': 'ɴ',
    'O': 'ᴏ', 'P': 'ᴘ', 'Q': 'ǫ', 'R': 'ʀ', 'S': 'ꜱ', 'T': 'ᴛ', 'U': 'ᴜ',
    'V': 'ᴠ', 'W': 'ᴡ', 'X': 'x', 'Y': 'ʏ', 'Z': 'ᴢ',
})


def _to_smallcaps(text: str) -> str:
    """Convert ASCII Latin letters in ``text`` to small-caps unicode."""
    return text.translate(_SMALLCAPS_MAP) if text else text




def Btn(text: str, **kwargs) -> _IKB:
    """
    Drop-in replacement for InlineKeyboardButton.

    1. Strips ``![emoji](tg://emoji?id=ID)`` premium-emoji markdown AND any
       leading plain Unicode emoji characters from the button label (both
       render incorrectly or duplicate inside Telegram button text).
    2. For callback_data buttons whose label matches a known semantic keyword,
       automatically injects icon_custom_emoji_id via the updated
       keyboardButtonCallback#E62BC960 + keyboardButtonStyle#4FDD3430
       MTProto constructors (Telegram layer 223+). No background colour tint
       is applied — only the custom icon.

    Emoji priority rule:
    • Custom icon found  → strip BOTH the ![...] markdown AND any leading
      plain emoji char. The TL-injected custom icon is the sole visual icon.
    • No custom icon     → keep the alt-text emoji character as a plain-text
      fallback so older clients / non-premium sessions still see something.

    Usage is identical to InlineKeyboardButton:
        Btn("Some Label", callback_data="cb_data")
    """
    # Version that keeps the alt-text emoji char (e.g. "👤 Profile")
    with_emoji = _EMOJI_MD.sub(lambda m: m.group(1) + " ", text).strip()
    # Version that strips markdown entirely (e.g. "Profile"),
    # then also strips any leading plain-Unicode emoji chars that language
    # strings or default labels may have (e.g. "🔑 API Key" → "API Key").
    no_emoji = _PLAIN_EMOJI_START.sub("", _EMOJI_MD.sub("", text).strip()).strip()

    # Keyword lookup MUST use the raw ASCII label — small-caps unicode letters
    # would never match the plain-ASCII keywords in _ICON. Apply the small-caps
    # transform only to the visible label passed to Telegram.
    with_emoji_display = _to_smallcaps(with_emoji)
    no_emoji_display = _to_smallcaps(no_emoji)

    cb = kwargs.get("callback_data")
    if cb is not None:
        # Match against the raw (pre-smallcaps) with-emoji label first,
        # then fall back to the stable ASCII callback_data so translated
        # labels (Arabic/Chinese/Russian/etc.) still get premium icons.
        match = _icon_for(with_emoji) or _icon_for_callback(cb)
        if match:
            icon_id, _color_flags = match
            # No background colour tint — custom icon only.
            color_flags = 0
            data = cb.encode("utf-8") if isinstance(cb, str) else cb
            raw_bytes = _tl_callback_with_style(
                no_emoji_display, data,
                icon_id=icon_id,
                color_flags=color_flags,
                requires_password=bool(kwargs.get("requires_password", False)),
            )
            btn = _BtnWithIcon(no_emoji_display, **kwargs)
            btn._tl_raw = _TLRaw(raw_bytes)   # type: ignore[attr-defined]
            return btn

    # No custom icon: keep the alt-text emoji as plain-text fallback
    return _IKB(with_emoji_display, **kwargs)


from server.logging import LOGGER

_log = LOGGER(__name__)

# ── Visual constants ──────────────────────────────────────────────────────────

DIV = "─" * 20


# ── Flag emoji from ISO country code ─────────────────────────────────────────

def flag(cc: str) -> str:
    """Convert a 2-letter ISO country code to its regional indicator flag emoji."""
    try:
        return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper()[:2])
    except Exception:
        return ""


# ── Safe message edit (inline keyboard update) ────────────────────────────────

async def safe_edit(
    cq: CallbackQuery,
    text: str,
    markup=None,
    parse_mode=None,
    entities=None,
) -> None:
    """
    Edit the message behind a CallbackQuery, with fallback to reply.

    Supports photo messages (edit_caption), plain messages (edit_text),
    entity-aware rendering (parse_mode=DISABLED + entities), and arbitrary
    parse_mode.  Falls back to reply_text if editing fails (e.g. the
    message is too old to edit or was already deleted).
    """
    kwargs: dict = {"reply_markup": markup}
    if entities is not None:
        kwargs["entities"] = entities
        kwargs["parse_mode"] = enums.ParseMode.DISABLED
    elif parse_mode is not None:
        kwargs["parse_mode"] = parse_mode

    try:
        if cq.message.photo:
            # edit_caption uses "caption" not "text" and "caption_entities" not "entities"
            cap_kwargs: dict = {"caption": text, "reply_markup": markup}
            if entities is not None:
                cap_kwargs["caption_entities"] = entities
                cap_kwargs["parse_mode"] = enums.ParseMode.DISABLED
            elif parse_mode is not None:
                cap_kwargs["parse_mode"] = parse_mode
            await cq.message.edit_caption(**cap_kwargs)
        else:
            await cq.message.edit_text(text=text, **kwargs)
    except Exception:
        try:
            await cq.message.reply_text(text=text, **kwargs)
        except Exception as exc:
            _log.error("safe_edit failed: %s", exc, exc_info=True)


# ── Admin authorisation helper ────────────────────────────────────────────────

async def is_admin(user_id: int) -> bool:
    """
    Return True if user_id is the bot owner or in the sudoers list.

    Defensive: on any DB error, falls back to owner-only check so the
    bot never locks out the owner due to a transient DB failure.
    """
    from config import OWNER_ID
    if user_id == OWNER_ID:
        return True
    try:
        from server.utils.database import get_sudoers
        sudoers = await get_sudoers()
        return user_id in sudoers
    except Exception as exc:
        _log.error("is_admin DB check failed for user %s: %s", user_id, exc)
        return False

