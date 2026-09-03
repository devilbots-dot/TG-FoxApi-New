"""
Reusable Telegram login-code (OTP) capture from the 777000 service account.

This single pair of functions replaces what used to be three separate
copies of the same inline `@client.on(events.NewMessage(from_users=777000))`
listener, duplicated across the stock pipeline, the fresh-session
generator, and the buyer-delivery OTP flow.

Split into "start" then "wait" so callers can register the listener
synchronously — matching Telegram's real timing, where the code can arrive
the instant it's requested — and only await the result once whatever
action triggered the code (e.g. send_code_request) has been sent.
"""

import asyncio
import re
from typing import Optional

from telethon import events


# Telegram login codes are 5 digits for most accounts, but newer accounts
# and some regions receive 6-digit codes. The regex matches both, and
# anchors with \b so it does not accidentally grab longer digit runs.
_OTP_RE = re.compile(r"\b(\d{5,6})\b")


def start_login_otp_listener(client) -> "asyncio.Future[str]":
    """
    Attach a listener for a 5- or 6-digit login code from Telegram's 777000
    service account on an already-connected `client`. Call this BEFORE
    triggering whatever action sends the code (e.g. send_code_request),
    so no message can arrive before the listener exists.

    The handler removes itself from the client once the future resolves,
    preventing a memory/handler leak if the client stays connected longer.

    Returns a Future that resolves to the code once received.
    """
    loop = asyncio.get_running_loop()
    otp_future: asyncio.Future = loop.create_future()

    @client.on(events.NewMessage(from_users=777000))
    async def _handler(event):
        match = _OTP_RE.search(event.raw_text)
        if match and not otp_future.done():
            otp_future.set_result(match.group(1))
            # Remove the handler immediately after resolving — avoids a
            # stale listener accumulating on long-lived client objects.
            client.remove_event_handler(_handler)

    return otp_future


async def wait_for_login_otp(otp_future: "asyncio.Future[str]", timeout: int) -> Optional[str]:
    """Await the future from start_login_otp_listener(); None on timeout."""
    try:
        return await asyncio.wait_for(asyncio.shield(otp_future), timeout=timeout)
    except asyncio.TimeoutError:
        # Cancel the underlying future so the handler stops waiting
        if not otp_future.done():
            otp_future.cancel()
        return None
