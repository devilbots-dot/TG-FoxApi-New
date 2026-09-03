"""
Spam / freeze classification via the official @SpamBot — run once at
upload time (as part of the pipeline) and again, live, right before an
account is handed to a buyer (its stored status can go stale at any point
after upload).
"""

import re
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient

from server import LOGGER
from server.utils.sessions.telethon_client import (
    cleanup_session_files,
    connect_with_proxy_fallback,
    new_temp_session_path,
    write_session_bytes,
)

_log = LOGGER(__name__)

# Exact wording SpamBot sends for the harsh/permanent restriction (no lift
# date is ever given for this one — only a manual complaint can undo it,
# unlike genuinely temporary limits which always state a lift date/time).
# Normalized (lowercase, curly quotes -> straight, whitespace collapsed)
# so it still matches regardless of exact spacing/line-break differences.
_PERMANENT_SPAM_TEXT = (
    "i'm very sorry that you had to contact me. unfortunately, some actions "
    "can trigger a harsh response from our anti-spam systems. if you think "
    "your account was limited by mistake, you can submit a complaint to our "
    "moderators. while the account is limited, you will not be able to send "
    "messages to people who do not have your number in their phone contacts "
    "or add them to groups and channels. of course, when people contact you "
    "first, you can always reply to them."
)

# ── Exact-text SpamBot pattern set (kept verbatim so classification behaves
# identically to the reference detector) ────────────────────────────────────
_RE_GOOD_NEWS_EXACT = re.compile(
    r"good\s+news[,!]?\s+no\s+limits\s+are\s+currently\s+applied\s+to\s+your\s+account\.?.*"
    r"free\s+as\s+a\s+bird",
    re.I | re.S,
)
_RE_ALL_CLEAR = re.compile(
    r"(good\s+news|no\s+limits\s+are\s+currently\s+applied|free\s+as\s+a\s+bird|"
    r"are\s+free\s+to\s+go|are\s+not\s+limited|no\s+restrictions)",
    re.I,
)

# Permanent-spam replies from SpamBot always open with "Hello <name>," and
# then contain "Unfortunately," shortly after — this greeting+word combo on
# its own is a 100% reliable permanent-spam signal, independent of the exact
# restriction wording that follows (which can vary/rewrap).
_RE_HELLO_UNFORTUNATELY = re.compile(
    r"^\s*hello\b[^.\n]{0,80}[,!\n]\s*.{0,40}unfortunately\b",
    re.I | re.S,
)

# Temporary/normal spam wording: phone-number based limits that Telegram
# says can be eased by subscribing to Premium — always temporary, even if
# it also happens to mention the "harsh response" phrase.
_RE_TEMP_PHONE_SPAM = re.compile(
    r"(some\s+phone\s+numbers\s+may\s+trigger\s+a\s+harsh\s+response|"
    r"phone\s+numbers\s+may\s+trigger|"
    r"subscribe\s+to\s+telegram\s+premium|"
    r"get\s+less\s+strict\s+limits|"
    r"if\s+this\s+is\s+the\s+case\s+with\s+you)",
    re.I | re.S,
)

# Looser fallback pattern for the same harsh/no-lift-date restriction —
# catches minor wording variants that don't match `_PERMANENT_SPAM_TEXT`
# verbatim (SpamBot occasionally rewraps or re-punctuates this message).
_RE_HARD_PERM_SPAM = re.compile(
    r"(some\s+actions\s+can\s+trigger\s+a\s+harsh\s+response|"
    r"while\s+the\s+account\s+is\s+limited|"
    r"will\s+not\s+be\s+able\s+to\s+send\s+messages\s+to\s+people\s+who\s+do\s+not\s+have\s+your\s+number|"
    r"people\s+who\s+do\s+not\s+have\s+your\s+number\s+in\s+their\s+phone\s+contacts|"
    r"when\s+people\s+contact\s+you\s+first.*you\s+can\s+always\s+reply)",
    re.I | re.S,
)

# Dated SpamBot phrases: "...limited ... until <date>." /
# "The restriction will be lifted on <date>."
_RE_LIMITED = re.compile(
    r"(limited|restricted|can[\u2019'`]?t send|cannot send|"
    r"unable to (send|message|invite)|"
    r"restriction|some limitations|for a while|"
    r"messages to (people|users) who are not (in|on) your contact)",
    re.I,
)
_RE_EXPLICIT_PERM = re.compile(
    r"(permanent|forever|will not be lifted|"
    r"unrecoverable|will never|indefinite)",
    re.I,
)
_RE_FROZEN = re.compile(
    r"(frozen|deactivat|terminated|banned|"
    r"violation of the terms|account was blocked|"
    r"account has been (banned|blocked|deleted))",
    re.I,
)

# "until 3 Jan 2026", "restriction will be lifted on January 3, 2026", "until 2026-01-03"
_RE_UNTIL_DATE = re.compile(
    r"(?:until|till|lifted\s+on|restriction\s+will\s+be\s+lifted\s+on|expires\s+on)\s+"
    r"("
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}"          # 3 Jan 2026
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}"       # January 3, 2026
    r"|\d{4}-\d{2}-\d{2}"                       # 2026-01-03
    r")",
    re.I,
)
_DATE_FORMATS = [
    "%d %b %Y", "%d %B %Y",
    "%b %d %Y", "%b %d, %Y",
    "%B %d %Y", "%B %d, %Y",
    "%Y-%m-%d",
]
# A dated limit this far (or further) in the future is treated as effectively
# permanent rather than a genuine short-lived restriction.
_PERM_DAYS_THRESHOLD = 180


def _normalize_spam_text(raw: str) -> str:
    normalized = raw.lower()
    normalized = normalized.replace("\u2019", "'").replace("\u2018", "'")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _parse_until_date(text: str) -> Optional[datetime]:
    match = _RE_UNTIL_DATE.search(text)
    if not match:
        return None
    raw = re.sub(r",", "", match.group(1)).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt.replace(",", "")).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def check_spam_status(client) -> str:
    """
    Message the official @SpamBot from an already-connected, authorized
    Telethon client and parse its reply to detect whether the account is
    spam-limited or frozen.

    Exactly 5 possible statuses (nothing else is ever returned):
      "clean"          — SpamBot reports no limitations ("Good news, no limits")
      "temporary_spam" — temporary restriction with an automatic lift date
                          (e.g. can't send messages to new users for now)
      "permanent_spam" — the harsh/no-lift-date restriction ("harsh response
                          from our anti-spam systems" wording) — distinct from
                          "frozen".
      "frozen"         — permanent limitation / account marked for deletion
      "unknown"        — could not determine (SpamBot unreachable, unexpected reply, etc.)
    """
    try:
        entity = await client.get_entity("SpamBot")
        async with client.conversation(entity, timeout=15) as conv:
            await conv.send_message("/start")
            reply = await conv.get_response()
            text = (reply.raw_text or "")
    except Exception as exc:
        _log.warning("check_spam_status failed: %s", exc)
        return "unknown"

    return _classify_spambot_text(text)


def _classify_spambot_text(text: str) -> str:
    """
    Pure text -> status classifier, split out from check_spam_status() so it
    can be unit-tested without a live Telethon connection.

    Mirrors the reference detector's `analyse_spambot_text()` rules and
    decision order exactly (verdicts renamed to this system's 5 statuses:
    good->clean, spam->temporary_spam, permspam->permanent_spam,
    frozen->frozen, unknown->unknown):

      1. Frozen/deactivated/banned wording -> "frozen" (checked first — a
         frozen account is never merely "spammed").
      2. Exact "Good news ... free as a bird" all-clear -> "clean". This is
         not downgraded by a flaky target-privacy error.
      3. "Hello <name>, ... Unfortunately ..." greeting wording -> always
         "permanent_spam" — checked right after the all-clear check and
         before every other rule, since this greeting+word combo alone is a
         100% reliable permanent-spam signal regardless of what follows.
      4. Temp phone-number/Premium wording -> "temporary_spam" — checked
         before the harsh/permanent wording since both mention "harsh
         response", but the phone/Premium framing is always temporary.
      5. A dated limit ("... until <date>") combined with limited/restricted
         language -> "permanent_spam" if the date is >= 180 days out,
         otherwise "temporary_spam".
      6. Explicit permanence wording ("permanent", "forever", ...) ->
         "permanent_spam".
      7. The harsh/no-lift-date restriction wording (has no date) ->
         "permanent_spam".
      8. Generic limited/restricted language with no date or hard/permanent
         wording -> "temporary_spam".
      9. Looser all-clear phrasing -> "clean".
      10. Anything else -> "unknown".
    """
    if not text:
        return "unknown"

    clean = re.sub(r"\s+", " ", text.replace("\u2019", "'")).strip()

    if _RE_FROZEN.search(clean):
        return "frozen"

    until = _parse_until_date(clean)

    # Exact all-clear. This should not be downgraded by a flaky target privacy error.
    if _RE_GOOD_NEWS_EXACT.search(clean):
        return "clean"

    # "Hello <name>, ... Unfortunately ..." greeting -> always permanent_spam,
    # checked before every other rule (100% reliable signal on its own).
    if _RE_HELLO_UNFORTUNATELY.search(clean):
        return "permanent_spam"

    # The user-provided temp spam text must be checked BEFORE the generic
    # "harsh response" permanent wording because both contain "harsh response".
    if _RE_TEMP_PHONE_SPAM.search(clean):
        return "temporary_spam"

    # If a clear date exists, classify by date first. Some dated limits also
    # mention non-contact messaging, but they are temporary when the date is near.
    if until and _RE_LIMITED.search(clean):
        days = (until - datetime.now(timezone.utc)).days
        if days >= _PERM_DAYS_THRESHOLD:
            return "permanent_spam"
        return "temporary_spam"

    if _RE_EXPLICIT_PERM.search(clean):
        return "permanent_spam"

    # User-provided permanent spam wording has no date. Treat it as permanent_spam.
    normalized = _normalize_spam_text(clean)
    if _PERMANENT_SPAM_TEXT in normalized or _RE_HARD_PERM_SPAM.search(clean):
        return "permanent_spam"

    if _RE_LIMITED.search(clean):
        # limited-language but no date and no hard/permanent wording -> normal temp spam
        return "temporary_spam"

    if _RE_ALL_CLEAR.search(clean):
        return "clean"

    return "unknown"


async def verify_spam_status_live(
    session_bytes: bytes,
    api_id: int,
    api_hash: str,
    country_code: Optional[str] = None,
) -> str:
    """
    Connect a short-lived Telethon client from raw session bytes and run
    check_spam_status() on it — used to re-verify an account's real, CURRENT
    status right before it is handed to a buyer (the stored `spam_status` in
    the DB is only as fresh as the last upload-time check; Telegram can
    limit/freeze an account at any point afterwards).

    Returns the same 5-value status as check_spam_status(), or "unknown" if
    the session can't even be connected/authorized anymore.
    """
    tmp_path = new_temp_session_path()
    client = None
    try:
        await write_session_bytes(tmp_path, session_bytes)
        if country_code:
            client, _proxy_id, _proxy_doc = await connect_with_proxy_fallback(
                tmp_path[:-8], api_id, api_hash, country_code,
            )
        else:
            client = TelegramClient(tmp_path[:-8], api_id, api_hash)
            await client.connect()

        if not await client.is_user_authorized():
            return "unknown"

        return await check_spam_status(client)
    except Exception as exc:
        _log.warning("verify_spam_status_live failed: %s", exc)
        return "unknown"
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        await cleanup_session_files(tmp_path)
