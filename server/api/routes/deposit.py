"""
Deposit API — provider-based, extensible deposit system.

Endpoints
─────────
  GET  /api/v1/wallet/deposit/methods          → list available methods
  POST /api/v1/wallet/deposit                  → create deposit (body: {method, network?, amount})
  GET  /api/v1/wallet/deposit/{deposit_id}     → check deposit status
  GET  /api/v1/wallet/deposits                 → paginated deposit history

Authentication: X-Api-Key header (tg_{user_id}_{secret})
"""

import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from server.api.deps import require_api_key
from server.api.response import ok, err
from server.api.schemas.deposit import (
    CreateDepositIn,
    DepositMethodsOut,
    DepositMethodOut,
    CreateDepositOut,
    DepositStatusOut,
    DepositListOut,
    DepositDetailOut,
)
from server.services.deposit import get_configured_providers, get_provider
from server.utils.database.walletdb import (
    create_deposit_v2,
    get_deposit,
    get_user_deposits_paginated,
    count_user_deposits,
    expire_overdue_deposits,
    confirm_deposit,
    depositsdb,
)
from server.logging import LOGGER
from server.utils.constants import MIN_DEPOSIT
from server.utils.common import utcnow

router = APIRouter(prefix="/api/v1/wallet", tags=["Deposits"])
_log = LOGGER(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _map_status(raw: str) -> str:
    """Normalise internal DB status to the public API status vocabulary."""
    return {"rejected": "failed"}.get(raw, raw)


def _doc_to_detail(doc: dict) -> DepositDetailOut:
    """Convert a raw MongoDB deposit document → DepositDetailOut (with derived fields)."""
    from server.api.schemas.deposit import enrich_deposit
    detail = DepositDetailOut(
        deposit_id=doc["deposit_id"],
        status=_map_status(doc.get("status", "pending")),
        amount=doc["amount"],
        method=doc["method"],
        currency=doc.get("currency"),
        network=doc.get("network"),
        address=doc.get("address"),
        payment_url=doc.get("payment_url"),
        qr_url=doc.get("qr_url"),
        memo=doc.get("memo"),
        phone=doc.get("phone"),
        instructions=doc.get("instructions"),
        expires_at=doc.get("expires_at"),
        created_at=doc.get("created_at"),
        extra=doc.get("extra") or None,
    )
    return enrich_deposit(detail)


# ══════════════════════════════════════════════════════════════════════════════
# 1.  GET /deposit/methods
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/deposit/methods",
    summary="List available deposit methods",
    response_model=DepositMethodsOut,
)
async def list_deposit_methods(_user=Depends(require_api_key)):
    """
    Returns all configured deposit methods.

    A method only appears when its credentials/config are present on the server.
    The `networks` field is only included for methods that support multiple networks.
    """
    providers = get_configured_providers()
    data = []
    for p in providers:
        entry = DepositMethodOut(id=p.method_id, name=p.method_name)
        nets = p.networks if hasattr(p, "networks") else []
        if callable(nets):
            nets = nets()
        if nets:
            entry.networks = list(nets)
        data.append(entry)

    return DepositMethodsOut(data=data)


# ══════════════════════════════════════════════════════════════════════════════
# 2.  POST /deposit
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/deposit",
    summary="Create a deposit",
    response_model=CreateDepositOut,
)
async def create_deposit_request(
    body: CreateDepositIn,
    user=Depends(require_api_key),
):
    """
    Initiates a deposit using the specified payment method.

    **Body:**
    ```json
    {"method": "oxapay", "amount": 10}
    ```

    Returns payment details (address / link) and a `deposit_id` to track status.
    Poll `GET /api/v1/wallet/deposit/{deposit_id}` for updates.

    OxaPay deposits are confirmed automatically via webhook — no admin action needed.
    """
    # ── Validate amount ───────────────────────────────────────────────────────
    if body.amount < MIN_DEPOSIT:
        return err(400, f"Minimum deposit amount is ${MIN_DEPOSIT:.2f}.")

    # ── Resolve provider ──────────────────────────────────────────────────────
    provider = get_provider(body.method)
    if not provider:
        return err(400, f"Unknown payment method '{body.method}'.")
    if not provider.is_configured():
        return err(503, f"Method '{body.method}' is not available. Contact support.")

    # ── Pre-generate deposit ID ───────────────────────────────────────────────
    # The same dep_id must be given to both the payment provider (as order_id)
    # AND saved to the DB — so the webhook can look up the record by order_id.
    dep_id = f"DEP-{secrets.token_hex(8).upper()}"

    # ── Generate payment details via provider ─────────────────────────────────
    try:
        payment = await provider.create_payment(
            deposit_id=dep_id,
            amount=body.amount,
            user_id=user["user_id"],
            network=body.network,
        )
    except ValueError as exc:
        return err(400, str(exc))
    except RuntimeError as exc:
        _log.error("Deposit provider error for user %s method %s: %s", user["user_id"], body.method, exc)
        return err(503, str(exc))

    # ── Persist deposit record (same dep_id given to provider) ────────────────
    await create_deposit_v2(
        deposit_id=dep_id,
        user_id=user["user_id"],
        amount=body.amount,
        method=body.method,
        network=body.network,
        currency=payment.currency,
        address=payment.address,
        payment_url=payment.payment_url,
        qr_url=payment.qr_url,
        memo=payment.memo,
        phone=payment.phone,
        instructions=payment.instructions,
        expires_at=payment.expires_at,
        extra=payment.extra,
    )

    _log.info(
        "Deposit %s created: user=%s method=%s amount=%.4f",
        dep_id, user["user_id"], body.method, body.amount,
    )

    doc = await get_deposit(dep_id)
    return CreateDepositOut(data=_doc_to_detail(doc))


# ══════════════════════════════════════════════════════════════════════════════
# 3.  GET /deposit/{deposit_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/deposit/{deposit_id}",
    summary="Check deposit status",
    response_model=DepositStatusOut,
)
async def get_deposit_status(deposit_id: str, user=Depends(require_api_key)):
    """
    Returns the current state of a deposit.

    Possible states: `pending` | `confirming` | `completed` | `expired` | `failed`

    Expired deposits are detected lazily on every poll.
    """
    doc = await get_deposit(deposit_id)
    if not doc or doc["user_id"] != user["user_id"]:
        return err(404, f"Deposit '{deposit_id}' not found.")

    # Expire only the requested deposit instead of issuing a collection-wide
    # update on every status poll. The background worker remains responsible
    # for bulk cleanup.
    expires_at = doc.get("expires_at")
    if (
        doc.get("status") == "pending"
        and expires_at is not None
        and expires_at <= utcnow()
    ):
        await depositsdb.update_one(
            {
                "deposit_id": deposit_id,
                "user_id": user["user_id"],
                "status": "pending",
                "expires_at": {"$lte": utcnow()},
            },
            {"$set": {"status": "expired"}},
        )
        doc = await get_deposit(deposit_id)

    return DepositStatusOut(data=_doc_to_detail(doc))


# ══════════════════════════════════════════════════════════════════════════════
# 4.  GET /deposits
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/deposits",
    summary="Deposit history (paginated)",
    response_model=DepositListOut,
)
async def deposit_history(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    user=Depends(require_api_key),
):
    """
    Returns your deposit history, newest first.

    Supports pagination via `?page=1&limit=20`.
    """
    total = await count_user_deposits(user["user_id"])
    docs = await get_user_deposits_paginated(user["user_id"], page=page, limit=limit)

    return DepositListOut(
        page=page,
        limit=limit,
        total=total,
        data=[_doc_to_detail(d) for d in docs],
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5.  POST /deposit/{deposit_id}/verify  — Binance Pay Order ID verification
# ══════════════════════════════════════════════════════════════════════════════

class VerifyDepositIn(BaseModel):
    order_id: str


@router.post(
    "/deposit/{deposit_id}/verify",
    summary="Verify a Binance Pay deposit by Order ID",
)
async def verify_binance_deposit(
    deposit_id: str,
    body: VerifyDepositIn,
    user=Depends(require_api_key),
):
    """
    Verify a `binance_pay_tx` deposit by submitting the Binance Pay Order ID.

    1. Looks up the pending deposit (must belong to the authenticated user).
    2. Calls the Binance API to confirm the Order ID exists and the amount matches.
    3. Credits the balance if verification succeeds.

    **Body:**
    ```json
    {"order_id": "445608650991075328"}
    ```

    Returns `status: "completed"` and the credited amount on success.
    """
    doc = await get_deposit(deposit_id)
    if not doc or doc["user_id"] != user["user_id"]:
        return err(404, f"Deposit '{deposit_id}' not found.")

    if doc.get("method") != "binance_pay_tx":
        return err(400, "This endpoint is only for Binance Pay (Order ID) deposits.")

    status = doc.get("status", "pending")
    if status == "completed":
        return ok(
            data={"deposit_id": deposit_id, "status": "completed", "amount": doc["amount"]},
            message="Deposit already confirmed.",
        )
    if status in ("expired", "failed"):
        return err(400, f"Deposit is {status} and cannot be verified.")

    order_id = (body.order_id or "").strip()
    if not order_id:
        return err(400, "order_id is required.")

    amount = float(doc.get("amount", 0))

    from server.services.deposit.providers.binance_pay_tx import (
        verify_by_order_id,
        claim_order_id,
        release_order_id,
    )

    # ── Call Binance API ──────────────────────────────────────────────────────
    result = await verify_by_order_id(order_id, amount, deposit_id)
    if not result["ok"]:
        if result.get("duplicate"):
            return err(409, result.get("error", "Order ID already used."))
        return err(400, result.get("error", "Verification failed."))

    # ── Duplicate protection — atomically claim the Order ID ─────────────────
    claimed = await claim_order_id(order_id, deposit_id, user["user_id"], amount)
    if not claimed:
        return err(409, "This Binance Order ID has already been used for another deposit.")

    # ── Credit balance ────────────────────────────────────────────────────────
    try:
        dep_doc = await confirm_deposit(deposit_id, confirmed_by=None)
    except Exception as exc:
        await release_order_id(order_id)
        _log.error("confirm_deposit failed for %s: %s", deposit_id, exc)
        return err(500, "Failed to credit balance. Please contact support.")

    if not dep_doc:
        # Already confirmed (race condition) — still a success
        return ok(
            data={"deposit_id": deposit_id, "status": "completed", "amount": amount},
            message="Deposit already confirmed.",
        )

    # Store order ID on the deposit record
    tx_data = result.get("tx", {})
    tx_id   = str(tx_data.get("orderId") or order_id)
    await depositsdb.update_one(
        {"deposit_id": deposit_id},
        {"$set": {
            "tx_hash":               tx_id,
            "extra.tx_hash":         tx_id,
            "extra.binance_order_id": order_id,
        }},
    )

    _log.info(
        "API binance_pay_tx: credited $%.4f to user %s (order_id=%s dep=%s)",
        dep_doc["amount"], user["user_id"], order_id, deposit_id,
    )

    return ok(
        data={
            "deposit_id": deposit_id,
            "status":     "completed",
            "amount":     dep_doc["amount"],
            "order_id":   order_id,
        },
        message="Payment verified and balance credited.",
    )
