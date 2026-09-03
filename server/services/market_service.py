"""
Shared market logic used by BOTH the Telegram bot handlers and the public API.
Single source of truth — behaviour never diverges between bot and API integrators.

Terminology:
  BUY  = Server → User  : platform sells a session account to the user via OTP.
  SELL = User → Server  : user submits their account; admin reviews & pays.
"""

from server.utils.database.countrydb import get_country
from server.utils.database.userdb import (
    get_balance,
    record_purchase,
    record_successful_purchase,
    update_balance,
    BalanceError,
)
from server.utils.database.walletdb import log_transaction
from server.utils.database.orderdb import create_order, cancel_order, get_order, ordersdb
from server.utils.database.sellrequestdb import create_sell_request


class MarketError(Exception):
    """Expected business-rule failure (not found, out of stock, etc.)."""

    def __init__(self, message: str, code: str = "market_error"):
        super().__init__(message)
        self.message = message
        self.code = code


PLATFORM_SELLER_ID = 0  # pseudo-seller representing the platform


async def buy_from_server(
    user_id: int,
    code: str,
    account_id_override: str,
) -> dict:
    """
    BUY = Server → User (OTP session delivery).

    The caller must already have reserved a session account via
    mark_session_sold() and pass its account_id here.

    Returns a confirmation dict the caller can return directly to the user/API.
    Raises MarketError on any expected failure.
    """
    country = await get_country(code)
    if not country:
        raise MarketError(f"Country '{code.upper()}' not found.", "country_not_found")
    if country.get("temp_disable"):
        raise MarketError("This country is temporarily unavailable.", "temp_disabled")

    price = float(country.get("price", 0) or 0)
    # Keep checkout price aligned with the authenticated country listing. The
    # discount is recomputed server-side; no client-provided amount is trusted.
    try:
        from server.utils.pricing_discounts import apply_discounts
        price = round(float(await apply_discounts(user_id, code, price)), 4)
    except Exception:
        price = round(price, 4)

    # ── Balance check ─────────────────────────────────────────────────────
    balance = await get_balance(user_id)
    if balance < price:
        raise MarketError(
            f"Insufficient balance. Need ${price:.2f}, have ${balance:.2f}.",
            "insufficient_balance",
        )

    # ── Create order, deduct balance, log transaction ──────────────────────
    order_id = await create_order(
        buyer_id=user_id,
        seller_id=PLATFORM_SELLER_ID,
        account_id=account_id_override,
        amount=price,
    )

    # Atomic deduction — raises BalanceError if a concurrent request drained
    # the wallet between the pre-check above and this write.
    try:
        await record_purchase(user_id, price)
    except BalanceError:
        await cancel_order(order_id, "balance_insufficient_at_deduction")
        raise MarketError(
            f"Insufficient balance. Need ${price:.2f} — "
            "your wallet was depleted by a concurrent request.",
            "insufficient_balance",
        )

    # Keep the debit ledger at checkout for wallet history.  Successful-sale
    # statistics are deliberately recorded later by finalize_successful_purchase.
    try:
        await log_transaction(
            user_id=user_id,
            txn_type="purchase",
            amount=-price,
            ref_id=order_id,
            note=(
                f"Purchase initiated [{country['code']}] "
                f"{country.get('country_name', code)} — awaiting OTP delivery"
            ),
        )
    except Exception as exc:
        # Never leave a wallet debited when the order/ledger write failed.
        try:
            await update_balance(user_id, price)
            await cancel_order(order_id, "purchase_ledger_write_failed")
        except Exception as refund_exc:
            import logging
            logging.getLogger(__name__).error(
                "CRITICAL: purchase rollback failed order=%s user=%s: %s",
                order_id, user_id, refund_exc,
            )
        raise MarketError("Could not create the purchase ledger entry; balance was refunded.", "ledger_failure") from exc

    # The order remains pending until the buyer actually receives the OTP.
    # Spent/counters/sales-feed are finalized only by the delivery callback.
    new_balance = await get_balance(user_id)
    result = {
        "order_id":      order_id,
        "code":          country["code"],
        "country_name":  country.get("country_name", country["code"]),
        "price_paid":    price,
        "new_balance":   round(new_balance, 4),
    }

    return result


async def finalize_successful_purchase(
    order_id: str,
    *,
    country_code: str,
    country_name: str,
) -> bool:
    """Finalize successful-sale accounting after delivery, exactly once.

    The order must already be in ``completed`` + ``delivered`` state.  The
    durable order marker prevents duplicate Spent/counter updates and duplicate
    sale-feed notifications across callback retries and process restarts.
    """
    order = await get_order(order_id)
    if not order or order.get("status") != "completed":
        return False
    if order.get("delivery_status") != "delivered":
        return False

    buyer_id = int(order["buyer_id"])
    if not country_code or country_code == "XX":
        try:
            from server.utils.database.sessiondb import get_session_account
            account = await get_session_account(order.get("account_id", ""))
            country_code = (account or {}).get("country_code", "XX")
            country_name = (account or {}).get("country_name", country_code)
        except Exception:
            country_code = country_code or "XX"
            country_name = country_name or country_code

    accounted = await record_successful_purchase(
        buyer_id,
        order_id,
        float(order.get("amount") or 0),
        units=int(order.get("quantity") or 1),
    )

    # If accounting was already completed by an earlier retry, still inspect
    # the durable feed marker so a crash between accounting and enqueue can be
    # recovered without sending the sale notification twice.
    feed_claim = await ordersdb.update_one(
        {
            "order_id": order_id,
            "status": "completed",
            "delivery_status": "delivered",
            "successful_accounting": True,
            "sales_feed_queued": {"$ne": True},
        },
        {"$set": {"sales_feed_queued": True}},
    )
    if feed_claim.modified_count == 0:
        return accounted

    try:
        from server.services.sales_feed import sales_feed_service
        amount = float(order.get("amount") or 0)
        if order.get("order_type") == "buy_session":
            sales_feed_service.fire_real_session(
                user_id=buyer_id,
                username=None,
                country_code=country_code,
                country_name=country_name,
                quantity=int(order.get("quantity") or 1),
                price_per=round(amount / max(1, int(order.get("quantity") or 1)), 4),
                total_price=amount,
                order_id=order_id,
            )
        else:
            sales_feed_service.fire_real_account(
                user_id=buyer_id,
                username=None,
                country_code=country_code,
                country_name=country_name,
                price=amount,
                order_id=order_id,
                new_balance=round(await get_balance(buyer_id), 4),
            )
    except Exception:
        # The durable marker prevents duplicate accounting; the sales-feed
        # worker/log can reconcile the missed notification separately.
        return accounted
    return accounted


async def submit_sell_request(user_id: int, code: str, phone: str) -> dict:
    """
    SELL = User → Server.
    Creates a pending sell request. No balance/stock changes here —
    admin approves and sets the final payout price.
    """
    country = await get_country(code)
    if not country:
        raise MarketError(f"Country '{code.upper()}' not found.", "country_not_found")
    if country.get("is_full"):
        raise MarketError(
            "We are currently not accepting this country for selling.", "country_full"
        )

    request_id = await create_sell_request(
        user_id=user_id,
        code=country["code"],
        country_name=country.get("country_name", country["code"]),
        phone=phone,
        offer_price=country.get("price", 0),
    )

    return {
        "request_id":   request_id,
        "code":         country["code"],
        "country_name": country.get("country_name", country["code"]),
        "status":       "pending",
        "offer_price":  country.get("price", 0),
        "note":         "Submitted for admin review. Payout credited once approved.",
    }
