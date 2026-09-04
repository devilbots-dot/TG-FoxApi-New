"""Telegram WebApp initData verification.

Verifies the HMAC-SHA256 signature Telegram attaches to every WebApp payload.
Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hmac
import hashlib
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

import logging

from fastapi import Header, HTTPException

import config as _cfg
from server.api.deps import require_api_key

_log = logging.getLogger(__name__)


_MAX_AGE_SECONDS = 24 * 60 * 60  # 24h — Telegram recommends re-validating after this
_MAX_FUTURE_SKEW_SECONDS = 5 * 60
_DIAGNOSTIC_VERSION = "tma-hmac-v5"


def _compute_secret(bot_token: str) -> bytes:
    # Telegram's official algorithm is:
    # secret_key = HMAC-SHA256(key="WebAppData", message=bot_token).
    # Reversing these arguments rejects every genuine initData payload.
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _auth_reject(detail: str, *, stage: str, request_id: str = "", field_names: tuple[str, ...] = ()) -> None:
    """Reject without logging raw initData, hashes, tokens, or user values."""
    _log.warning(
        "WebApp initData rejected diag=%s request_id=%s stage=%s fields=%s",
        _DIAGNOSTIC_VERSION,
        request_id or "client-missing",
        stage,
        ",".join(field_names) or "none",
    )
    raise HTTPException(status_code=401, detail=detail)


def verify_init_data(init_data: str, bot_token: Optional[str] = None, request_id: str = "") -> dict:
    """Verify initData and return parsed fields (with `user` decoded).

    Raises HTTPException(401) on any failure.
    """
    if not init_data:
        _auth_reject("Missing initData — open this panel from the bot button (not a browser).", stage="missing", request_id=request_id)

    token = bot_token or _cfg.BOT_TOKEN
    try:
        parsed_pairs = parse_qsl(init_data, strict_parsing=True, keep_blank_values=True)
    except Exception:
        _auth_reject("Malformed initData", stage="parse", request_id=request_id)

    keys = [key for key, _ in parsed_pairs]
    field_names = tuple(sorted(set(keys)))
    if len(keys) != len(set(keys)):
        _auth_reject("Malformed initData", stage="duplicate-field", request_id=request_id, field_names=field_names)
    pairs = dict(parsed_pairs)

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        _auth_reject("Missing hash", stage="missing-hash", request_id=request_id, field_names=field_names)

    # Only `hash` is excluded for bot-token HMAC validation. Telegram's
    # data-check-string is built from every other received field, including
    # the newer `signature` field when it is present.
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
    secret = _compute_secret(token)
    calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc, received_hash):
        _auth_reject(
            "Telegram verification failed. Close and reopen this Mini App from the bot.",
            stage="hmac-mismatch",
            request_id=request_id,
            field_names=field_names,
        )

    # Freshness check
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    now = time.time()
    if auth_date <= 0 or auth_date > now + _MAX_FUTURE_SKEW_SECONDS or (now - auth_date) > _MAX_AGE_SECONDS:
        _auth_reject("Session expired — reopen the Mini App from the bot.", stage="freshness", request_id=request_id, field_names=field_names)

    user_raw = pairs.get("user")
    if user_raw:
        try:
            pairs["user"] = json.loads(user_raw)
        except Exception:
            _auth_reject("Malformed user field", stage="user-json", request_id=request_id, field_names=field_names)
    return pairs


async def require_tg_user(
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    x_request_id: str = Header(default="", alias="X-Request-Id"),
) -> dict:
    """FastAPI dependency — returns the verified Telegram `user` dict."""
    data = verify_init_data(x_telegram_init_data, request_id=x_request_id)
    user = data.get("user") or {}
    if not user.get("id"):
        _auth_reject("No user in initData", stage="missing-user", request_id=x_request_id, field_names=tuple(sorted(data.keys())))
    return user


def _api_key_identity(user: dict) -> dict:
    """Adapt the existing canonical API-key user record to Mini App identity."""
    full_name = str(user.get("full_name") or "").strip()
    username = str(user.get("username") or "").strip()
    return {
        "id": int(user["user_id"]),
        "first_name": full_name.split()[0] if full_name else username,
        "username": username,
        "language_code": str(user.get("language") or "en"),
        "auth_source": "api_key",
        # This canonical record was just resolved by require_api_key for this
        # request. It is internal dependency context, never a response field.
        "_webapp_account_doc": user,
    }


async def require_webapp_user(
    x_api_key: str = Header(default="", alias="X-Api-Key"),
    x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data"),
    x_request_id: str = Header(default="", alias="X-Request-Id"),
) -> dict:
    """Resolve one canonical user through a supplied API key or Telegram initData.

    API-key login is intentionally header-only. It never appears in URLs, logs,
    cookies, response payloads, or MongoDB session data.
    """
    if x_api_key.strip():
        return _api_key_identity(await require_api_key(x_api_key.strip()))
    return await require_tg_user(x_telegram_init_data, x_request_id)
