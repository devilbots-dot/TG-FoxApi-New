"""
Generic store for stock accounts paused mid-pipeline on some piece of admin
input (current 2FA password, manual country code, etc.) — replaces what
used to be two near-identical raw dicts plus duplicated resolver functions.
"""

import time


class PendingStockStore:
    """
    Per-chat storage of {phone: entry_dict}, with lookup by exact phone, by
    the 1-based index shown in a `/pending_*` listing, or by a loosely
    normalized phone (missing "+", stray spaces) — whichever is easier for
    the admin to type back.

    Entries hold raw session bytes for potentially large batches; if an
    admin abandons a batch mid-way (never runs `/2fa_pass` or `/set_country`
    for some accounts), those entries would otherwise sit in memory forever.
    Each entry is stamped with `_added_at` so `purge_stale()` can be called
    periodically to reclaim genuinely abandoned ones.
    """

    def __init__(self):
        self._by_chat: dict[int, dict[str, dict]] = {}

    def add(self, chat_id: int, phone: str, entry: dict) -> None:
        entry = dict(entry)
        entry["_added_at"] = time.monotonic()
        self._by_chat.setdefault(chat_id, {})[phone] = entry

    def purge_stale(self, max_age_seconds: float) -> int:
        """Drop entries older than `max_age_seconds`. Returns count removed."""
        now = time.monotonic()
        removed = 0
        for chat_id in list(self._by_chat.keys()):
            pending = self._by_chat[chat_id]
            for phone in list(pending.keys()):
                added_at = pending[phone].get("_added_at", now)
                if now - added_at > max_age_seconds:
                    del pending[phone]
                    removed += 1
            if not pending:
                del self._by_chat[chat_id]
        return removed

    def list_for_chat(self, chat_id: int) -> dict[str, dict]:
        return self._by_chat.get(chat_id, {})

    def pop(self, chat_id: int, phone: str) -> dict | None:
        return self._by_chat.get(chat_id, {}).pop(phone, None)

    def resolve_token(self, chat_id: int, token: str) -> str | None:
        """Resolve an admin-supplied token (exact phone, list index, or a
        loosely-normalized phone) to the exact phone key stored for this chat."""
        pending = self._by_chat.get(chat_id, {})
        if not pending:
            return None

        token = token.strip()
        if token in pending:
            return token

        if token.isdigit():
            idx = int(token) - 1
            keys = list(pending.keys())
            if 0 <= idx < len(keys):
                return keys[idx]

        normalized = "+" + token.lstrip("+").replace(" ", "")
        if normalized in pending:
            return normalized

        return None
