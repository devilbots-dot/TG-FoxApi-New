"""
Buyer-side OTP retrieval.

After an account has already been SOLD, the buyer needs the Telegram
login code to actually sign into it themselves. This module connects to
the sold session purely to listen for that code and hand it back — it is
NOT part of the admin stock-upload pipeline (see server/stock/ for that).

Proxy fallback semantics:
  _CONN_ERROR sentinel  → actual TCP/proxy connection failure → retry with direct
  None                  → OTP timed out (session connected OK, no message came) → no retry
  ""   (empty str)      → session is not authorised → no retry
  str  (5-digit code)   → OTP received
"""

import asyncio
from typing import Optional

from telethon import TelegramClient

from server import LOGGER
from server.stock.otp_capture import start_login_otp_listener, wait_for_login_otp
from server.utils.sessions.telethon_client import (
    build_telethon_proxy,
    new_temp_session_path,
    write_session_bytes,
    cleanup_session_files,
)

_log = LOGGER(__name__)

# Sentinel: proxy/TCP connection failed; caller should retry with direct IP
_CONN_ERROR = object()


async def get_otp_from_session(
    session_bytes: bytes,
    api_id: int,
    api_hash: str,
    timeout: int = 120,
    proxy_doc: Optional[dict] = None,
) -> Optional[str]:
    """
    Connects to a Telethon session and waits for an OTP message from 777000.

    Proxy fallback rules:
      - If proxy_doc is given, connect through the proxy first.
      - Only fall back to direct (server IP) on a real connection failure
        (_CONN_ERROR sentinel). OTP timeout or unauthorised session are NOT
        retried — they mean the session itself is the problem, not the route.

    Returns:
      str   — 5-digit OTP
      None  — timed out (no Telegram login detected) OR session unauthorised
    """
    if proxy_doc:
        result = await _try_otp_connect(
            session_bytes=session_bytes,
            api_id=api_id,
            api_hash=api_hash,
            timeout=timeout,
            proxy_doc=proxy_doc,
        )
        if result is _CONN_ERROR:
            # Real network failure → log and fall through to direct attempt
            _log.warning(
                "Proxy %s:%s unreachable; retrying direct connection.",
                proxy_doc.get("host"), proxy_doc.get("port"),
            )
        else:
            # "" = unauthorised, None = timeout, str = OTP — all handled the same
            return result if result else None  # "" and None both become None

    # Direct connection (no proxy, or proxy already failed)
    result = await _try_otp_connect(
        session_bytes=session_bytes,
        api_id=api_id,
        api_hash=api_hash,
        timeout=timeout,
        proxy_doc=None,
    )
    if result is _CONN_ERROR:
        _log.error("Direct connection also failed — network issue on host.")
        return None
    return result if result else None  # "" → None, str → OTP code


async def _try_otp_connect(
    session_bytes: bytes,
    api_id: int,
    api_hash: str,
    timeout: int,
    proxy_doc: Optional[dict],
):
    """
    Attempt one Telethon connection (proxy or direct).

    Returns:
      str          — 5-digit OTP code received
      ""           — session not authorised (invalid session, no point retrying)
      None         — OTP not received within `timeout` (connection was fine)
      _CONN_ERROR  — TCP / proxy connection failed → caller may retry with different route
    """
    tmp_path = new_temp_session_path()
    proxy_tuple = build_telethon_proxy(proxy_doc) if proxy_doc else None
    proxy_id    = proxy_doc.get("proxy_id") if proxy_doc else None

    client = None
    try:
        await write_session_bytes(tmp_path, session_bytes)
        session_path = tmp_path[:-8]  # Telethon appends .session itself

        client_kwargs: dict = {"api_id": api_id, "api_hash": api_hash}
        if proxy_tuple:
            client_kwargs["proxy"] = proxy_tuple

        client = TelegramClient(session_path, **client_kwargs)
        otp_future = start_login_otp_listener(client)

        # ── Connection attempt ────────────────────────────────────────────
        try:
            await client.connect()
        except OSError as conn_err:
            _log.warning(
                "Connection error (proxy_id=%s host=%s): %s",
                proxy_id, proxy_doc.get("host") if proxy_doc else "direct", conn_err,
            )
            if proxy_id:
                from server.utils.database.proxydb import increment_proxy_fail
                await increment_proxy_fail(proxy_id)
            return _CONN_ERROR  # signal: try another route

        # ── Auth check ────────────────────────────────────────────────────
        if not await client.is_user_authorized():
            _log.warning("Session not authorised (proxy_id=%s) — skipping.", proxy_id)
            return ""  # session is invalid; no point retrying on a different proxy

        # ── Wait for OTP ──────────────────────────────────────────────────
        otp = await wait_for_login_otp(otp_future, timeout)
        if otp is not None:
            # Connection & auth were fine — reset fail counter
            if proxy_id:
                from server.utils.database.proxydb import reset_proxy_fails
                await reset_proxy_fails(proxy_id)
        return otp

    except Exception as exc:
        _log.error(
            "Unexpected error in _try_otp_connect (proxy_id=%s): %s", proxy_id, exc
        )
        if proxy_id:
            from server.utils.database.proxydb import increment_proxy_fail
            await increment_proxy_fail(proxy_id)
        return _CONN_ERROR

    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        await cleanup_session_files(tmp_path)
