import asyncio
import importlib
import os
import sys

from pyrogram import idle


# ── Bootstrap: load custom env vars from MongoDB before config.py reads them ───
# Admins can add/edit/delete env vars in the admin panel. They are stored in
# the custom_env collection and applied on the next restart.

# Dynamic MongoDB configuration is intentionally restricted to non-secret,
# operator-facing presentation values. Deployment secrets and provider keys
# must stay in the host environment / secret manager.
_CUSTOM_ENV_ALLOWLIST = {
    "SUPPORT_CHANNEL",
    "SUPPORT_GROUP",
    "SUPPORT_USER_ID",
    "UPDATES_CHANNEL",
    "START_IMG_URL",
}

def _load_custom_env_from_db() -> None:
    mongo_uri = os.environ.get("MONGO_DB_URI")
    if not mongo_uri:
        return
    try:
        from pymongo import MongoClient
        client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=3000,
            connectTimeoutMS=3000,
            socketTimeoutMS=3000,
        )
        db = client.get_database("Apiserverdb")
        for doc in db.custom_env.find({}, {"_id": 0, "key": 1, "value": 1}):
            k = (doc.get("key") or "").strip()
            if k in _CUSTOM_ENV_ALLOWLIST:
                os.environ[k] = str(doc.get("value") or "")
            elif k:
                print(f"[bootstrap] Ignored non-allowlisted custom environment key: {k}", file=sys.stderr)
        client.close()
    except Exception as exc:
        # Do not fail startup because of this; just log to stderr.
        print(f"[bootstrap] Could not load custom env vars: {exc}", file=sys.stderr)


_load_custom_env_from_db()

from pyrogram.errors import FloodWait

import config
from server import LOGGER, bot, api
from server.plugins import ALL_MODULES


async def _wait_for_api(host: str, port: int, attempts: int = 20, delay: float = 1.0) -> bool:
    """
    Async TCP probe — returns True as soon as the port accepts connections.
    Replaces the old blocking urllib.request.urlopen health check which
    stalled the asyncio event loop for up to ~20 seconds on startup.
    """
    for _ in range(attempts):
        await asyncio.sleep(delay)
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return True
        except (ConnectionRefusedError, OSError):
            pass
    return False


async def main():
    # ── Step 1: Recreate the Motor client inside the running event loop ──────
    # Motor must be initialised after the loop is running to avoid the
    # "Future attached to a different loop" error.
    #
    # After reinit(), all _LazyCollection proxies in the db modules
    # automatically route through the new client — no manual patching of
    # db module variables is needed.
    from server.core import mongo
    mongo.reinit()

    # ── Step 0: Sweep temp session files orphaned by a previous hard crash ───
    from server.utils.sessions.telethon_client import sweep_stale_temp_sessions
    await sweep_stale_temp_sessions()

    # ── Step 1b: Load in-memory store from MongoDB ────────────────────────────
    # Pulls all hot data (settings, countries, proxies, sudoers, banned users,
    # stock counts) into RAM so runtime reads never touch MongoDB.
    # Also starts the background retry worker for failed async writes.
    from server.core import memstore
    await memstore.load()

    # ── Step 1c: Backfill users_sell_stock from already-approved sell_requests
    # Self-heals databases created by older code that forgot to populate
    # users_sell_stock, so previously-paid sellers become visible in the
    # admin "Seller Stocks" / "Seller Lookup" panels on the next boot.
    try:
        from server.utils.database.usersellstockdb import backfill_from_sell_requests
        n = await backfill_from_sell_requests()
        if n:
            LOGGER("server").info(
                "users_sell_stock backfill: inserted %d missing row(s) from sell_requests.", n,
            )
    except Exception as exc:
        LOGGER("server").warning("users_sell_stock backfill skipped: %s", exc)

    # ── Step 2: Load plugins ──────────────────────────────────────────────────
    LOGGER("server").info("Loading plugins...")
    for module in ALL_MODULES:
        importlib.import_module("server.plugins" + module)
        LOGGER("server").info("Loaded: server.plugins%s", module)
    LOGGER("server").info("Total %d plugin(s) loaded.", len(ALL_MODULES))

    # ── Step 3: Start API and verify it comes up ──────────────────────────────
    await api.start()
    port = config.API_PORT
    up = await _wait_for_api("127.0.0.1", port, attempts=20, delay=1.0)
    if up:
        LOGGER("server").info("API health check passed (port %d is up).", port)
    else:
        LOGGER("server").warning(
            "API did not respond on port %d after startup — "
            "HTTP API may be down.",
            port,
        )

    # ── Log active webhook URLs (auto-resolved from REPLIT_DEV_DOMAIN) ────────
    LOGGER("server").info("Deposit  webhook URL : %s", config.DEPOSIT_WEBHOOK_URL or "(not set)")
    LOGGER("server").info("Withdrawal callback URL: %s", config.WITHDRAWAL_CALLBACK_URL or "(not set)")

    # ── Step 4: Start bot (with Telegram flood-wait retry) ────────────────────
    # The API is already up, so the admin panel stays accessible even if
    # Telegram temporarily rate-limits the bot login.
    bot_started = False
    while not bot_started:
        try:
            await bot.start()
            bot_started = True
        except FloodWait as exc:
            wait = getattr(exc, "value", 60)
            LOGGER("server").warning(
                "Telegram flood wait (%ss): bot login delayed, admin panel still online. Retrying in %ss...",
                wait, wait,
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            LOGGER("server").warning("Bot start failed: %s. Retrying in 60s...", exc)
            await asyncio.sleep(60)

    LOGGER("server").info("Bot is now running and listening for messages...")

    # Register the same-domain Mini App as the bot's default Telegram menu.
    # Telegram signs initData for this bot; WebApp routes map its user.id to the
    # same Mongo user record the bot already uses.
    if config.WEBAPP_URL and bool(memstore.settings.get("miniapp_enabled", True)):
        try:
            from pyrogram.types import MenuButtonWebApp, WebAppInfo

            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Open TgFox",
                    web_app=WebAppInfo(url=config.WEBAPP_URL),
                )
            )
            LOGGER("server").info("Telegram Mini App menu configured: %s", config.WEBAPP_URL)
        except Exception as exc:
            LOGGER("server").warning("Mini App menu setup skipped: %s", exc)
    elif not bool(memstore.settings.get("miniapp_enabled", True)):
        try:
            from pyrogram.types import MenuButtonDefault
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
            LOGGER("server").info("Telegram Mini App menu disabled by admin setting.")
        except Exception as exc:
            LOGGER("server").warning("Mini App menu disable skipped: %s", exc)
    else:
        LOGGER("server").warning(
            "Mini App menu not configured: set WEBAPP_BASE_URL or provide RENDER_EXTERNAL_URL."
        )

    # ── Step 5: Start background workers ─────────────────────────────────────
    # These run indefinitely and handle:
    #   • Retrying session termination for PENDING_TERMINATION sell requests
    #   • Releasing pending_balance → earned balance after 48-hour holds
    from server.services.workers import start_background_workers
    _worker_tasks = start_background_workers()

    # ── Supervisor: detect and log unexpected worker exits ────────────────────
    _log = LOGGER("server")

    def _make_worker_done_cb(task_name: str):
        def _on_done(task: asyncio.Task):
            if task.cancelled():
                return  # intentional shutdown — no action needed
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                _log.critical(
                    "Worker '%s' exited with unhandled exception: %s — "
                    "restart the app to restore this worker.",
                    task_name, exc, exc_info=exc,
                )
            else:
                _log.warning(
                    "Worker '%s' exited unexpectedly (no exception) — "
                    "restart the app to restore this worker.",
                    task_name,
                )
        return _on_done

    for _t in _worker_tasks:
        _t.add_done_callback(_make_worker_done_cb(_t.get_name()))

    # ── Step 6: Start sales feed service ─────────────────────────────────────
    from server.services.sales_feed import sales_feed_service
    sales_feed_service.start()
    LOGGER("server").info("Sales feed service started.")

    await idle()

    for task in _worker_tasks:
        task.cancel()
        try:
            await task
        except Exception:
            pass

    await bot.stop()
    await api.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
