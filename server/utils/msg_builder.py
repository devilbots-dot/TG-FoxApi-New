"""
Shared message builder — constructs Telegram text + entity lists with
correct UTF-16 offsets. Works with pyrofork (layer 220+) which supports
MessageEntityBlockquote and MessageEntityCustomEmoji.

Custom emoji syntax
-------------------
Any string passed to .t(), .bold(), .code(), or .link() may contain one or
more ``![alt](tg://emoji?id=ID)`` tokens.  _append_parsed() strips the
markdown, keeps the alt character as the visible glyph in the raw text, and
adds a MessageEntityCustomEmoji entity so Telegram renders the animated
premium sticker.  Overlapping entities (e.g. a bold span that also covers a
custom-emoji position) are legal and handled correctly.
"""

import re as _re

from pyrogram import enums
from pyrogram.types import MessageEntity

_EMOJI_RE = _re.compile(r'!\[([^\]]+)\]\(tg://emoji\?id=(\d+)\)')


class MsgBuilder:
    """Build a Telegram message string + entity list with correct UTF-16 offsets."""

    def __init__(self):
        self._parts: list[str] = []
        self._utf16_offset: int = 0
        self._entities: list = []

    @staticmethod
    def _u16len(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    # ── Internal: append text, parsing premium-emoji markdown ─────────────────

    def _append_parsed(
        self,
        text: str,
        outer_type=None,
        **outer_kwargs,
    ) -> "MsgBuilder":
        """
        Scan *text* for ``![alt](tg://emoji?id=ID)`` tokens.

        For each token:
          • The alt character(s) are written into the message text.
          • A ``MessageEntityCustomEmoji`` entity is added at that position.

        Plain segments between tokens are written verbatim.

        If *outer_type* is given (e.g. BOLD, CODE, TEXT_LINK), a single
        entity of that type is added spanning the full processed range,
        so bold/code formatting correctly covers both the emoji glyphs and
        the surrounding text.
        """
        outer_start = self._utf16_offset
        cursor = 0

        for m in _EMOJI_RE.finditer(text):
            # ── plain segment before this token ──────────────────────────────
            before = text[cursor:m.start()]
            if before:
                self._parts.append(before)
                self._utf16_offset += self._u16len(before)

            # ── custom emoji token ────────────────────────────────────────────
            alt      = m.group(1)          # e.g. "🛒"
            emoji_id = int(m.group(2))     # the numeric ID (must be int for TL serialization)

            emoji_off = self._utf16_offset
            emoji_len = self._u16len(alt)
            self._parts.append(alt)
            self._utf16_offset += emoji_len
            self._entities.append(
                MessageEntity(
                    type=enums.MessageEntityType.CUSTOM_EMOJI,
                    offset=emoji_off,
                    length=emoji_len,
                    custom_emoji_id=emoji_id,
                )
            )
            cursor = m.end()

        # ── tail after last token (or the whole string if no tokens) ─────────
        tail = text[cursor:]
        if tail:
            self._parts.append(tail)
            self._utf16_offset += self._u16len(tail)

        # ── outer formatting entity (bold / code / link / …) ─────────────────
        if outer_type is not None:
            span_len = self._utf16_offset - outer_start
            if span_len > 0:
                self._entities.append(
                    MessageEntity(
                        type=outer_type,
                        offset=outer_start,
                        length=span_len,
                        **outer_kwargs,
                    )
                )

        return self

    # ── Public API ─────────────────────────────────────────────────────────────

    def t(self, text: str) -> "MsgBuilder":
        """Plain text (premium-emoji tokens are expanded)."""
        return self._append_parsed(text)

    def bold(self, text: str) -> "MsgBuilder":
        """Bold text; premium-emoji tokens inside are expanded and rendered."""
        return self._append_parsed(text, enums.MessageEntityType.BOLD)

    def code(self, text: str) -> "MsgBuilder":
        """Inline code; premium-emoji tokens inside are expanded."""
        return self._append_parsed(text, enums.MessageEntityType.CODE)

    def link(self, label: str, url: str) -> "MsgBuilder":
        """Clickable hyperlink; premium-emoji tokens in the label are expanded."""
        return self._append_parsed(
            label, enums.MessageEntityType.TEXT_LINK, url=url
        )

    def bq_start(self) -> int:
        """Mark the start of a blockquote; returns start offset."""
        return self._utf16_offset

    def bq_end(self, start: int) -> "MsgBuilder":
        """Close blockquote that started at `start`."""
        length = self._utf16_offset - start
        if length > 0:
            self._entities.append(
                MessageEntity(
                    type=enums.MessageEntityType.BLOCKQUOTE,
                    offset=start,
                    length=length,
                )
            )
        return self

    def build(self) -> tuple[str, list]:
        return "".join(self._parts), self._entities
