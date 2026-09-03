"""
In-memory circular log buffer + async file-tail for live log streaming.

Usage:
  - attach_log_buffer() installs a handler on the root logger that fills BUFFER.
  - tail_log_file() is an async generator that yields new lines from log.txt.
  - SSE endpoint uses tail_log_file() to stream logs to the browser.
"""

import asyncio
import collections
import logging
import os
from datetime import datetime, timezone
from typing import AsyncGenerator

LOG_FILE   = "log.txt"
BUFFER_MAX = 500   # keep last N log lines in memory

# ── in-memory ring buffer ─────────────────────────────────────────────────────
BUFFER: collections.deque = collections.deque(maxlen=BUFFER_MAX)
_subscribers: list[asyncio.Queue] = []


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            BUFFER.append(line)
            for q in list(_subscribers):
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    pass
        except Exception:
            pass


def attach_log_buffer() -> None:
    """Call once at startup — installs the in-memory buffer handler."""
    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s %(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)


# ── SSE subscription ──────────────────────────────────────────────────────────

async def subscribe_logs() -> AsyncGenerator[str, None]:
    """Async generator — yields SSE-formatted log lines forever.

    First sends the current BUFFER contents, then streams new lines in real-time.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.append(q)
    try:
        # 1. Flush buffer history
        for line in list(BUFFER):
            yield f"data: {line}\n\n"

        # 2. Stream live lines
        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=15)
                yield f"data: {line}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"   # SSE heartbeat
    finally:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


# ── tail log file (fallback REST endpoint) ────────────────────────────────────

def read_log_tail(n: int = 300) -> list[str]:
    """Return the last *n* lines from log.txt (synchronous, fast)."""
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except Exception:
        return []
