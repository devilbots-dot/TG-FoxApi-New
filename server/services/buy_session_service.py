"""Atomic batch purchase primitives for the Telegram Buy Session flow.

This module deliberately does not use the OTP/fresh-session generator.  Stock
sessions are verified before they enter ``session_accounts`` and the buyer
receives the exact stored session bytes from that inventory record.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Iterable

from server.services.market_service import PLATFORM_SELLER_ID, MarketError
from server.utils.database.auditdb import log_action
from server.utils.database.countrydb import get_country
from server.utils.database.orderdb import (
    cancel_order,
    create_order,
    refund_order,
)
from server.utils.database.sessiondb import (
    get_unsold_session_for_country,
    mark_session_sold,
    revert_session_sold,
)
from server.utils.database.userdb import BalanceError, get_balance, record_purchase, update_balance
from server.utils.database.walletdb import log_transaction


MAX_BATCH_QUANTITY = 50  # hard cap: prevents resource exhaustion / oversized orders


async def purchase_batch(
    user_id: int,
    country_code: str,
    quantity: int,
    *,
    bot_client=None,
) -> dict:
    """Reserve exactly ``quantity`` inventory records and charge once.

    Inventory is claimed with the existing ``sold: false`` conditional update,
    so two buyers can never successfully reserve the same account.  If stock
    or balance is insufficient, every reservation made by this call is
    returned before the error is reported.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise MarketError("Quantity must be at least 1.", "invalid_quantity")
    if quantity > MAX_BATCH_QUANTITY:
        raise MarketError(
            f"Maximum batch quantity is {MAX_BATCH_QUANTITY} per order.",
            "quantity_too_large",
        )
    code = country_code.upper()
    country = await get_country(code)
    if not country:
        raise MarketError(f"Country '{code}' not found.", "country_not_found")
    if country.get("temp_disable"):
        raise MarketError("This country is temporarily unavailable.", "temp_disabled")

    price = round(float(country.get("price") or 0), 4)
    total = round(price * quantity, 4)
    balance = await get_balance(user_id)
    if balance < total:
        raise MarketError(
            f"Insufficient balance. Need ${total:.2f}, have ${balance:.2f}.",
            "insufficient_balance",
        )

    reserved: list[dict] = []
    order_id: str | None = None
    charged = False
    try:
        from server.services.inventory_service import reserve_valid_session
        for _ in range(quantity):
            reservation = await reserve_valid_session(
                bot_client=bot_client,
                user_id=user_id,
                country_code=code,
                exclude_account_ids=[a["account_id"] for a in reserved],
                max_attempts=25,
            )
            if not reservation.account:
                if reservation.retryable:
                    raise MarketError(
                        "Session validation is temporarily unavailable. Please try again shortly.",
                        "session_validation_temporary",
                    )
                raise MarketError(
                    "Not enough verified sessions are available right now.",
                    "out_of_stock",
                )
            reserved.append(reservation.account)

        order_id = await create_order(
            buyer_id=user_id,
            seller_id=PLATFORM_SELLER_ID,
            account_id=reserved[0]["account_id"],
            amount=total,
            quantity=quantity,
            account_ids=[a["account_id"] for a in reserved],
            order_type="buy_session",
        )

        try:
            await record_purchase(user_id, total, units=quantity)
            charged = True
        except BalanceError as exc:
            await cancel_order(order_id, "balance_insufficient_at_batch_deduction")
            raise MarketError(
                "Insufficient balance — your wallet changed during checkout.",
                "insufficient_balance",
            ) from exc

        # Keep the order pending until the ZIP is actually delivered to the
        # buyer.  Successful-sale stats/feed are finalized by the delivery
        # handler after Telegram accepts the document.
        await log_transaction(
            user_id=user_id,
            txn_type="purchase",
            amount=-total,
            ref_id=order_id,
            note=f"Bought {quantity} stored session(s) [{code}]",
        )
        await log_action(
            "order",
            "buy_session_reserved",
            actor=f"user:{user_id}",
            target=order_id,
            detail=f"country={code} quantity={quantity} amount={total:.4f}",
        )
        result = {
            "order_id": order_id,
            "country": country,
            "accounts": reserved,
            "quantity": quantity,
            "price_paid": total,
        }

        return result
    except Exception:
        # The caller also rolls back delivery failures.  This branch handles
        # reservation/payment failures before purchase_batch can return.
        if charged and order_id:
            # Refund the buyer — log explicitly on failure so admin can manually
            # correct rather than silently losing the user's money.
            try:
                await update_balance(user_id, total)
            except Exception as _refund_exc:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "CRITICAL: purchase_batch refund failed for user=%s amount=%.4f order=%s: %s "
                    "— manual balance correction required.",
                    user_id, total, order_id, _refund_exc,
                )
            try:
                await refund_order(order_id, "buy_session_batch_failed")
            except Exception as _ro_exc:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "purchase_batch: refund_order failed for order=%s: %s",
                    order_id, _ro_exc,
                )
        for account in reserved:
            try:
                await revert_session_sold(account["account_id"])
            except Exception:
                # Do not hide the original checkout error.  The failed account
                # remains sold and is visible to the reconciliation/admin tools.
                pass
        raise


async def rollback_batch_purchase(purchase: dict, user_id: int, reason: str) -> None:
    """Compensate a paid batch exactly once from the handler's failure path."""
    order_id = purchase.get("order_id")
    if order_id:
        refunded = await refund_order(order_id, reason)
        if refunded:
            amount = round(float(purchase.get("price_paid") or 0), 4)
            if amount:
                await update_balance(user_id, amount)
                await log_transaction(
                    user_id=user_id,
                    txn_type="refund",
                    amount=amount,
                    ref_id=order_id,
                    note=f"Buy-session batch rollback: {reason}",
                )
    for account in purchase.get("accounts", []):
        try:
            await revert_session_sold(account["account_id"])
        except Exception:
            pass
    await log_action(
        "order",
        "buy_session_rollback",
        actor=f"user:{user_id}",
        target=order_id,
        detail=reason,
        ok=False,
    )


def build_delivery_zip(items: Iterable[dict], filename: str) -> io.BytesIO:
    """Build and reopen a paired-session ZIP before it reaches Telegram."""
    item_list = list(items)
    if not item_list:
        raise ValueError("Cannot deliver an empty session batch.")

    seen: set[str] = set()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for item in item_list:
            phone = str(item.get("phone_digits") or "").replace("+", "").replace(" ", "")
            session_bytes = item.get("session_bytes")
            metadata = item.get("json_data")
            if not phone or phone in seen:
                raise ValueError("Duplicate or missing phone in delivery batch.")
            if not isinstance(session_bytes, (bytes, bytearray)) or not session_bytes:
                raise ValueError(f"Stored session is empty for {phone}.")
            if not isinstance(metadata, dict):
                raise ValueError(f"Metadata is missing for {phone}.")
            if str(metadata.get("session_file") or "") != phone:
                raise ValueError(f"Metadata does not match session for {phone}.")
            seen.add(phone)
            output.writestr(f"{phone}.session", bytes(session_bytes))
            output.writestr(
                f"{phone}.json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
            )

    archive.seek(0)
    with zipfile.ZipFile(archive, "r") as check:
        if check.testzip() is not None:
            raise ValueError("Generated delivery ZIP failed integrity validation.")
        names = set(check.namelist())
        for phone in seen:
            if {f"{phone}.session", f"{phone}.json"} - names:
                raise ValueError(f"Delivery pair is incomplete for {phone}.")
    archive.seek(0)
    archive.name = filename
    return archive