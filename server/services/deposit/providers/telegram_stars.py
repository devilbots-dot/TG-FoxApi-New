"""
Telegram Stars deposit provider.

No external credentials needed — uses Telegram's native Stars payment system.
The bot must have Stars payments enabled via @BotFather.

Configuration:
  DEPOSIT_STARS_PER_USD  — Stars-to-USD rate (default: 50 Stars = $1)
"""

from datetime import datetime, timezone, timedelta
from os import getenv
from typing import Optional

import config
from server.services.deposit.base import DepositProvider, PaymentDetails

DEPOSIT_TTL_MINUTES = 60
DEFAULT_STARS_PER_USD = 50


class TelegramStarsProvider(DepositProvider):
    method_id = "telegram_stars"
    method_name = "Telegram Stars"
    networks: list[str] = []

    def is_configured(self) -> bool:
        """Stars payments require BOT_USERNAME to build the payment deep-link."""
        return bool(config.BOT_USERNAME)

    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        rate = int(getenv("DEPOSIT_STARS_PER_USD", str(DEFAULT_STARS_PER_USD)))
        stars_needed = int(amount * rate)

        # Build a deep link to the bot's Stars invoice.
        # BOT_USERNAME is validated by is_configured(); if it reaches here
        # without a username the payment_url is None and the frontend surfaces
        # "method unavailable" rather than a broken link.
        bot_username = config.BOT_USERNAME
        payment_url = (
            f"https://t.me/{bot_username}?start=deposit_{deposit_id}"
            if bot_username else None
        )

        return PaymentDetails(
            currency="Stars",
            payment_url=payment_url,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=DEPOSIT_TTL_MINUTES),
            instructions=(
                f"Pay {stars_needed} Telegram Stars to complete this deposit. "
                f"Open the bot and use the payment link, or send /deposit to the bot."
            ),
            extra={"stars_required": stars_needed, "rate": f"{rate} Stars = $1"},
        )
