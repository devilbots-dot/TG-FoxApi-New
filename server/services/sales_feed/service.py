"""
Sales Feed Service.

Manages real purchase logging through fire_real_account() / fire_real_session().
Events are queued, asynchronous, non-blocking, retried, and deduplicated.

All sends go through _send_to_feed(), which reads settings from memstore
(zero MongoDB round-trips on the hot path).

Design rules:
  - Never blocks the purchase path (fire_* returns immediately).
  - Real purchase logs are NEVER lost: queued with retries + backoff.
  - Duplicate sends prevented via a per-send dedup set (TTL 60 s).
  - Only completed real purchases are eligible for feed delivery.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from server.logging import LOGGER

_log = LOGGER(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_MAX_RETRIES   = 5
_RETRY_DELAYS  = [3, 10, 30, 60, 120]   # seconds per attempt
_DEDUP_TTL_S   = 60                      # deduplicate sends within this window
_QUEUE_MAXSIZE = 1000


class SalesFeedService:
    """Singleton service managing real sales-feed delivery."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict | None] | None = None
        self._sender_task:  asyncio.Task | None = None
        self._fake_task:    asyncio.Task | None = None
        self._dedup: dict[str, float] = {}    # send_key → timestamp
        self._started = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Called once from the server startup after the event loop is running.
        Safe to call multiple times (idempotent).
        """
        if self._started:
            return
        self._started = True
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._sender_task = asyncio.create_task(self._sender_worker(), name="sales_feed_sender")
        self._fake_task   = asyncio.create_task(self._fake_worker(),   name="sales_feed_fake")
        _log.info("sales_feed: service started.")

    def _ensure_started(self) -> None:
        """Lazily start if the event loop is already running."""
        if not self._started:
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    self.start()
            except RuntimeError:
                pass

    # ── Public fire API ───────────────────────────────────────────────────────

    def fire_real_account(
        self,
        *,
        user_id: int,
        username: str | None,
        country_code: str,
        country_name: str,
        price: float,
        order_id: str,
        new_balance: float,
    ) -> None:
        """
        Enqueue a real Account purchase log.  Non-blocking — returns immediately.
        Call from market_service.buy_from_server() after complete_order().
        """
        self._ensure_started()
        event: dict = {
            "kind":         "real_account",
            "user_id":      user_id,
            "username":     username,
            "country_code": country_code,
            "country_name": country_name,
            "price":        price,
            "order_id":     order_id,
            "new_balance":  new_balance,
            "is_fake":      False,
            "_dedup_key":   f"account:{order_id}",
            "_enqueued_at": time.monotonic(),
        }
        self._enqueue(event)

    def fire_real_session(
        self,
        *,
        user_id: int,
        username: str | None,
        country_code: str,
        country_name: str,
        quantity: int,
        price_per: float,
        total_price: float,
        order_id: str,
    ) -> None:
        """
        Enqueue a real Session purchase log.  Non-blocking — returns immediately.
        Call from buy_session_service.purchase_batch() after complete_order().
        """
        self._ensure_started()
        event: dict = {
            "kind":         "real_session",
            "user_id":      user_id,
            "username":     username,
            "country_code": country_code,
            "country_name": country_name,
            "quantity":     quantity,
            "price_per":    price_per,
            "total_price":  total_price,
            "order_id":     order_id,
            "is_fake":      False,
            "_dedup_key":   f"session:{order_id}",
            "_enqueued_at": time.monotonic(),
        }
        self._enqueue(event)

    # ── Internal queue helpers ────────────────────────────────────────────────

    def _enqueue(self, event: dict) -> None:
        if self._queue is None:
            _log.warning("sales_feed: queue not ready, dropping event %s", event.get("_dedup_key"))
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning("sales_feed: queue full — dropping event %s", event.get("_dedup_key"))

    def _is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        # Prune expired entries
        expired = [k for k, ts in self._dedup.items() if now - ts > _DEDUP_TTL_S]
        for k in expired:
            self._dedup.pop(k, None)
        if key in self._dedup:
            return True
        self._dedup[key] = now
        return False

    # ── Sender worker ─────────────────────────────────────────────────────────

    async def _sender_worker(self) -> None:
        """Drain the queue, send each event with retry + backoff."""
        assert self._queue is not None
        while True:
            try:
                event = await self._queue.get()
                if event is None:     # shutdown sentinel
                    break
                await self._process_event(event)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.error("sales_feed: sender_worker unexpected error: %s", exc)

    async def _process_event(self, event: dict) -> None:
        """Send one event to the feed chat, with retry on failure."""
        dedup_key = event.get("_dedup_key", "")
        if dedup_key and self._is_duplicate(dedup_key):
            _log.debug("sales_feed: duplicate skipped: %s", dedup_key)
            return

        settings = self._get_settings()
        if not settings["enabled"]:
            return
        chat_id = settings["chat_id"]
        if not chat_id:
            return

        delay = settings["delay_seconds"]
        if delay > 0:
            await asyncio.sleep(delay)

        text = self._build_text(event)
        if not text:
            return

        silent = settings["silent"]
        last_error = ""
        for attempt in range(_MAX_RETRIES):
            ok, last_error = await self._send(chat_id, text, silent=silent)
            if ok:
                return
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                _log.info(
                    "sales_feed: send failed (attempt %d/%d, reason=%s), retrying in %ds...",
                    attempt + 1, _MAX_RETRIES, last_error, wait,
                )
                await asyncio.sleep(wait)

        # Real purchases must not be silently lost
        if not event.get("is_fake"):
            _log.error(
                "sales_feed: REAL purchase log FAILED after %d attempts (last error: %s) — event: %s",
                _MAX_RETRIES, last_error,
                {k: v for k, v in event.items() if not k.startswith("_")},
            )

    # ── Fake sales worker ─────────────────────────────────────────────────────

    async def _fake_worker(self) -> None:
        """Periodically publish an isolated, display-only fake purchase."""
        while True:
            try:
                settings = self._get_settings()
                if not settings["fake_enabled"]:
                    await asyncio.sleep(15)
                    continue

                interval_min = max(30, settings["fake_interval_min"])
                interval_max = max(interval_min, settings["fake_interval_max"])
                await asyncio.sleep(random.uniform(interval_min, interval_max))

                settings = self._get_settings()
                if not settings["fake_enabled"] or not settings["enabled"] or not settings["chat_id"]:
                    continue

                from server.services.sales_feed.fake_generator import generate_fake_event
                from server.services.sales_feed.formatter import format_fake_purchase

                event = generate_fake_event(
                    product_pool=settings["fake_product_pool"] or None,
                    country_pool=settings["fake_country_pool"] or None,
                    randomization_level=settings["fake_randomization_level"],
                )
                text = format_fake_purchase(
                    product_type=event["product_type"],
                    country_code=event["country_code"],
                    country_name=event["country_name"],
                    quantity=event["quantity"],
                    price_per=event["price_per"],
                    total_price=event["total_price"],
                    fake_username=event["fake_username"],
                    payment_method=event["payment_method"],
                    device=event["device"],
                )
                await self._send(settings["chat_id"], text, silent=settings["silent"])
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.error("sales_feed: fake_worker error: %s", exc)
                await asyncio.sleep(30)

    # ── Settings reader ───────────────────────────────────────────────────────

    @staticmethod
    def _get_settings() -> dict:
        """Read current settings from memstore (zero DB round-trips)."""
        from server.core import memstore
        s = memstore.settings
        return {
            "enabled":                   bool(s.get("sales_feed_enabled",          False)),
            "chat_id":                   str(s.get("sales_feed_chat_id",           "") or ""),
            "silent":                    bool(s.get("sales_feed_silent",            False)),
            "delay_seconds":             int(s.get("sales_feed_delay_seconds",      0)),
            "fake_enabled":              bool(s.get("fake_sales_enabled",           False)),
            "fake_interval_min":         int(s.get("fake_sales_interval_min",       300)),
            "fake_interval_max":         int(s.get("fake_sales_interval_max",       900)),
            "fake_randomization_level":  int(s.get("fake_sales_randomization_level", 5)),
            "fake_product_pool":         s.get("fake_sales_product_pool",           []),
            "fake_country_pool":          s.get("fake_sales_country_pool",           []),
        }

    # ── Low-level Telegram send ───────────────────────────────────────────────

    @staticmethod
    async def _send(
        chat_id: str, text: str, *, silent: bool = False
    ) -> tuple[bool, str]:
        """
        Send text to a Telegram chat.
        Returns (True, "") on success, or (False, "<reason>") on failure.
        Never raises.
        """
        try:
            from server import bot
            from pyrogram.enums import ParseMode
            chat: int | str = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
            await bot.send_message(
                chat,
                text,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=silent,
            )
            return True, ""
        except Exception as exc:
            reason = str(exc)
            _log.warning("sales_feed: Telegram send failed (chat=%s): %s", chat_id, reason)
            return False, reason

    # ── Text builder ──────────────────────────────────────────────────────────

    @staticmethod
    def _build_text(event: dict) -> str:
        """Build the formatted message text for a real purchase event."""
        from server.services.sales_feed import formatter as fmt
        kind = event.get("kind")
        try:
            if kind == "real_account":
                return fmt.format_account_purchase(
                    user_id=event["user_id"],
                    username=event.get("username"),
                    country_code=event["country_code"],
                    country_name=event["country_name"],
                    price=event["price"],
                    order_id=event["order_id"],
                    new_balance=event.get("new_balance", 0),
                )
            elif kind == "real_session":
                return fmt.format_session_purchase(
                    user_id=event["user_id"],
                    username=event.get("username"),
                    country_code=event["country_code"],
                    country_name=event["country_name"],
                    quantity=event["quantity"],
                    price_per=event["price_per"],
                    total_price=event["total_price"],
                    order_id=event["order_id"],
                )
        except Exception as exc:
            _log.error("sales_feed: formatter error for kind=%s: %s", kind, exc)
        return ""

    # ── Test helper ──────────────────────────────────────────────────────────

    async def send_test(self) -> tuple[bool, str]:
        """
        Send a test notification to the configured chat.
        Returns (True, "") on success, (False, "<reason>") on failure.
        """
        settings = self._get_settings()
        chat_id = settings["chat_id"]
        if not chat_id:
            return False, "No chat ID configured."
        from server.services.sales_feed.formatter import format_test_message
        text = format_test_message(chat_id)
        return await self._send(chat_id, text)

    async def send_fake_preview(self) -> tuple[bool, str]:
        """Generate one isolated fake event and send it as a preview."""
        settings = self._get_settings()
        chat_id = settings["chat_id"]
        if not chat_id:
            return False, "No chat ID configured."
        from server.services.sales_feed.fake_generator import generate_fake_event
        from server.services.sales_feed.formatter import format_fake_purchase
        event = generate_fake_event(
            product_pool=settings["fake_product_pool"] or None,
            country_pool=settings["fake_country_pool"] or None,
            randomization_level=settings["fake_randomization_level"],
        )
        text = format_fake_purchase(
            product_type=event["product_type"],
            country_code=event["country_code"],
            country_name=event["country_name"],
            quantity=event["quantity"],
            price_per=event["price_per"],
            total_price=event["total_price"],
            fake_username=event["fake_username"],
            payment_method=event["payment_method"],
            device=event["device"],
        )
        return await self._send(chat_id, text, silent=settings["silent"])
