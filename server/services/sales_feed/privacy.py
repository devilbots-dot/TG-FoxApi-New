"""
Privacy masking utilities.

All customer-facing data must be masked before appearing in any sales log.
Never expose phone numbers, emails, session IDs, wallet addresses, or any
credentials in full.
"""

from __future__ import annotations

import re


def mask_phone(phone: str) -> str:
    """
    Mask a phone number, showing country code + first 4 digits + XXXX.
    Example: +919579621234 → +91957962XXXX
    """
    if not phone:
        return "***"
    phone = str(phone).strip()
    # Remove spaces/dashes for counting
    digits_only = re.sub(r"[^\d+]", "", phone)
    if len(digits_only) <= 6:
        return "***"
    # Keep first 8 chars (with +), mask rest
    visible = digits_only[:-4]
    return visible + "X" * 4


def mask_email(email: str) -> str:
    """
    Mask an email address.
    Example: john.doe@gmail.com → jo**.*oe@gmail.com
    """
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return "*" * len(local) + "@" + domain
    # Show first 2 chars, mask middle, show last 1 char
    masked = local[:2] + "*" * max(1, len(local) - 3) + local[-1]
    return f"{masked}@{domain}"


def mask_session_id(session_id: str) -> str:
    """
    Mask a session / account ID.
    Show first 6 and last 4 characters.
    Example: ABCD1234EFGH5678 → ABCD12…5678
    """
    if not session_id:
        return "***"
    s = str(session_id).strip()
    if len(s) <= 10:
        return s[:3] + "…"
    return f"{s[:6]}…{s[-4:]}"


def mask_wallet(address: str) -> str:
    """
    Mask a crypto wallet address.
    Show first 10 and last 6 characters.
    Example: TQn5RPLJ7VChxxxxxxxxxxx → TQn5RPLJ7V…xxxxxx
    """
    if not address:
        return "***"
    a = str(address).strip()
    if len(a) <= 16:
        return a
    return f"{a[:10]}…{a[-6:]}"


def mask_user_id(user_id: int | str) -> str:
    """
    Partially mask a Telegram user ID.
    Example: 1234567890 → 1234***890
    """
    s = str(user_id).strip()
    if len(s) <= 6:
        return s[:2] + "***"
    return s[:4] + "***" + s[-3:]


def mask_username(username: str | None) -> str:
    """
    Mask a Telegram username, showing first 3 chars + ***.
    Example: johndoe → joh***
    """
    if not username:
        return "—"
    u = username.lstrip("@")
    if len(u) <= 3:
        return u + "***"
    return u[:3] + "***"
