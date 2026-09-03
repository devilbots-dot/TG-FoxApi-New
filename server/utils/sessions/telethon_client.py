"""
Low-level Telethon connection primitives — "Telethon Operations" layer.

Shared by the stock pipeline (server/stock/) and the buyer-delivery OTP
flow (server/utils/sessions/delivery_otp.py). Neither of those own this
logic; both just call it. If a third consumer ever needs to open a
Telethon client from raw session bytes, it belongs here too — not a new
copy.

Responsibilities:
  - Build the proxy tuple Telethon expects from a proxydb document.
  - Manage temporary on-disk session files (Telethon requires a real file
    path, not raw bytes) with guaranteed cleanup.
  - Connect a TelegramClient using the country -> wildcard -> direct proxy
    fallback chain used everywhere a stock account is touched.
"""

import asyncio
import os
import uuid

from telethon import TelegramClient

from server import LOGGER

_log = LOGGER(__name__)

_TMP_DIR = "/tmp/tg_sessions"


def build_telethon_proxy(proxy_doc: dict) -> tuple:
    """
    Convert a proxydb document to the tuple format Telethon expects.

    Telethon proxy tuple:
      (socks.SOCKS5, host, port)                        — no auth
      (socks.SOCKS5, host, port, True, user, password)  — with auth
      (socks.HTTP,   host, port)
    """
    import socks

    ptype = proxy_doc.get("type", "socks5").lower()
    host  = proxy_doc["host"]
    port  = int(proxy_doc["port"])
    user  = proxy_doc.get("username", "")
    pwd   = proxy_doc.get("password", "")

    socks_type = socks.SOCKS5 if ptype == "socks5" else socks.HTTP

    if user:
        return (socks_type, host, port, True, user, pwd)
    return (socks_type, host, port)


def new_temp_session_path() -> str:
    """Reserve a fresh temp `.session` file path (caller writes the bytes)."""
    os.makedirs(_TMP_DIR, exist_ok=True)
    return os.path.join(_TMP_DIR, f"{uuid.uuid4().hex}.session")


def _write_session_bytes_sync(tmp_path: str, session_bytes: bytes) -> None:
    with open(tmp_path, "wb") as fh:
        fh.write(session_bytes)


async def write_session_bytes(tmp_path: str, session_bytes: bytes) -> None:
    """Write raw session bytes to a path returned by new_temp_session_path().

    Runs the blocking file write in a thread so it never stalls the event
    loop (this pipeline is called concurrently for batch uploads).
    """
    await asyncio.to_thread(_write_session_bytes_sync, tmp_path, session_bytes)


def _read_session_bytes_sync(tmp_path: str) -> bytes:
    with open(tmp_path, "rb") as fh:
        return fh.read()


async def read_session_bytes(tmp_path: str) -> bytes:
    """Read back a session file's bytes (e.g. after Telethon has written to it)."""
    return await asyncio.to_thread(_read_session_bytes_sync, tmp_path)


def _cleanup_session_files_sync(*tmp_paths: str) -> None:
    for tmp_path in tmp_paths:
        base = tmp_path[:-8] if tmp_path.endswith(".session") else tmp_path
        for ext in (".session", ".session-journal", ".session-wal", ".session-shm"):
            try:
                os.remove(base + ext)
            except OSError:
                pass


async def cleanup_session_files(*tmp_paths: str) -> None:
    """Delete a Telethon session file and its -journal/-wal/-shm siblings."""
    await asyncio.to_thread(_cleanup_session_files_sync, *tmp_paths)


def _sweep_stale_temp_sessions_sync(max_age_seconds: int) -> int:
    """
    Delete any leftover files in _TMP_DIR older than `max_age_seconds`.

    Every normal code path already cleans up its own temp session files via
    a try/finally (see session_generation.py, pipeline.py). This sweep only
    catches what those can't: files orphaned by a hard process crash/kill
    (e.g. OOM) where no Python `finally` ever gets to run. Safe to call on
    every boot — a session that's still mid-flight when the process is
    restarted is, by definition, already abandoned.
    """
    import time as _time

    if not os.path.isdir(_TMP_DIR):
        return 0
    cutoff = _time.time() - max_age_seconds
    removed = 0
    try:
        entries = os.listdir(_TMP_DIR)
    except OSError:
        return 0
    for name in entries:
        path = os.path.join(_TMP_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


async def sweep_stale_temp_sessions(max_age_seconds: int = 3600) -> int:
    """Startup housekeeping: remove temp session files older than an hour."""
    removed = await asyncio.to_thread(_sweep_stale_temp_sessions_sync, max_age_seconds)
    if removed:
        _log.info("Swept %d stale temp session file(s) from %s", removed, _TMP_DIR)
    return removed


async def connect_with_proxy_fallback(
    session_path: str,
    api_id: int,
    api_hash: str,
    country_code: str,
) -> tuple:
    """
    Connect a TelegramClient using the fallback chain:
      1. Country-specific proxy  (fewest failures)
      2. Wildcard ("*") proxy    (get_active_proxy_for_country handles this automatically)
      3. Direct server IP

    Returns: (TelegramClient, proxy_id_or_"direct", proxy_doc_or_None)
             proxy_doc is the EXACT proxy document actually used (or None for
             direct) so callers needing a second client can reuse the same
             route instead of re-querying the DB (which could pick a
             different proxy on a fresh lookup).
    Raises:  OSError if every attempt fails.
    """
    from server.utils.database.proxydb import (
        get_active_proxy_for_country,
        increment_proxy_fail,
        reset_proxy_fails,
    )

    proxy_doc = await get_active_proxy_for_country(country_code)

    # ── Attempt 1: via proxy ──────────────────────────────────────────────
    if proxy_doc:
        proxy_tuple = build_telethon_proxy(proxy_doc)
        proxy_id    = proxy_doc["proxy_id"]
        try:
            client = TelegramClient(session_path, api_id, api_hash, proxy=proxy_tuple)
            await client.connect()
            await reset_proxy_fails(proxy_id)
            _log.info(
                "Connected via proxy %s (%s:%s) for %s",
                proxy_id, proxy_doc["host"], proxy_doc["port"], country_code,
            )
            return client, proxy_id, proxy_doc
        except OSError as exc:
            _log.warning(
                "Proxy %s (%s:%s) failed for %s: %s — retrying direct.",
                proxy_id, proxy_doc["host"], proxy_doc["port"], country_code, exc,
            )
            await increment_proxy_fail(proxy_id)

    # ── Attempt 2: direct ─────────────────────────────────────────────────
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()   # raises OSError if this also fails
    _log.info("Connected direct (no proxy) for %s", country_code)
    return client, "direct", None
