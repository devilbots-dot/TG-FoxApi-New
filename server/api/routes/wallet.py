"""
Wallet API — balance, addresses, deposits, withdrawals, transactions.

Authentication: X-Api-Key: tg_{user_id}_{secret}

Endpoints
─────────
  GET  /api/v1/wallet/                         → full wallet overview
  GET  /api/v1/wallet/balance                  → balance + reserved breakdown
  GET  /api/v1/wallet/address                  → saved withdrawal addresses
  POST /api/v1/wallet/address                  → set / update address
  POST /api/v1/wallet/withdraw                 → request withdrawal
  GET  /api/v1/wallet/withdrawals              → paginated withdrawal history
  GET  /api/v1/wallet/withdrawals/{id}         → single withdrawal detail
  POST /api/v1/wallet/withdrawals/{id}/verify  → force-poll gateway for latest status
  GET  /api/v1/wallet/transactions             → paginated transaction ledger

Response envelope (all endpoints):
  Success  → { "success": true, "message": "...", "data": {...}, "meta": {...}, "pagination": {...} }
  Error    → { "success": false, "message": "...", "error": {"code": N, "type": "...", "message": "..."}, "meta": {...} }
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from server.api.deps import require_api_key
from server.api.response import ok, err, paginate, ERR_NOT_FOUND
from server.api.schemas.wallet import SetAddressIn, WithdrawIn
from server.logging import LOGGER
from server.utils.database.userdb import (
    get_wallet_addresses,
    get_wallet_snapshot,
    set_wallet_address,
    SUPPORTED_NETWORKS,
)
from server.utils.database.withdrawaldb import (
    get_withdrawal_record,
    get_user_withdrawals,
    count_user_withdrawals,
)
from server.utils.database.walletdb import get_user_transactions
from server.services.deposit import get_configured_providers
from server.services.withdrawal import get_withdrawal_service
from server.utils.constants import MIN_DEPOSIT
from server.utils.database.configdb import get_setting
import config

_log = LOGGER(__name__)
router = APIRouter(prefix="/api/v1/wallet", tags=["Wallet"])


# ── Helpers ───────────────────────────────────────────────────────────────────

_DT_FIELDS = (
    "created_at", "confirmed_at", "processed_at", "expires_at",
    "updated_at", "completed_at", "webhook_received_at", "last_verified_at",
)

_INTERNAL_FIELDS = (
    "gateway_request", "gateway_response", "webhook_payload",
    "raw_request", "raw_response",
)


def _clean(doc: dict) -> dict:
    """Sanitise a MongoDB document for JSON: remove _id and internal fields, serialise datetimes."""
    doc.pop("_id", None)
    for field in _INTERNAL_FIELDS:
        doc.pop(field, None)
    for k in _DT_FIELDS:
        v = doc.get(k)
        if v is not None:
            try:
                doc[k] = v.isoformat()
            except AttributeError:
                pass
    return doc


# Public-facing fields for a withdrawal record
_WITHDRAWAL_KEEP = {
    "withdrawal_id", "status", "amount", "fee_amount", "net_amount",
    "currency", "network", "wallet_address", "gateway",
    "gateway_track_id", "gateway_tx_hash", "gateway_status",
    "reason", "note", "created_at", "updated_at", "completed_at",
}


def _withdrawal_view(doc: dict) -> dict:
    """Public-facing view of a withdrawal record — strips internal gateway payloads."""
    doc = _clean(dict(doc))
    view = {k: v for k, v in doc.items() if k in _WITHDRAWAL_KEEP}
    # Human-readable status label
    view["status_label"] = _STATUS_LABELS.get(view.get("status", ""), view.get("status", ""))
    view["is_final"] = view.get("status") in {"completed", "failed", "rejected", "cancelled"}
    return view


_STATUS_LABELS = {
    "pending":               "Pending Review",
    "processing":            "Processing",
    "waiting_confirmation":  "Waiting Blockchain Confirmation",
    "completed":             "Completed",
    "failed":                "Failed",
    "rejected":              "Rejected",
    "cancelled":             "Cancelled",
    "manual":                "Under Manual Review",
}


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/", summary="Full wallet overview")
async def wallet_overview(user=Depends(require_api_key)):
    """
    Returns a complete wallet snapshot in a single call:
    - Balance (available, reserved, net spendable)
    - Saved withdrawal addresses
    - Available deposit methods
    - Supported withdrawal networks
    - All platform limits and fees

    Designed to power a complete wallet screen without additional API calls.
    """
    snapshot = await get_wallet_snapshot(user["user_id"])

    balance  = float(snapshot["balance"])
    res_f    = float(snapshot["reserved_balance"])
    net      = max(0.0, balance - res_f)

    deposit_methods = []
    for p in get_configured_providers():
        method = {"id": p.method_id, "name": p.method_name, "min_amount": MIN_DEPOSIT}
        nets = p.networks if hasattr(p, "networks") else []
        if callable(nets):
            nets = nets()
        if nets:
            method["networks"] = list(nets)
        deposit_methods.append(method)

    return ok(
        data={
            # Balance
            "balance": {
                "available":   round(balance, 4),
                "reserved":    round(res_f, 4),
                "net_spendable": round(net, 4),
                "currency":    "USD",
                "formatted": {
                    "available":   f"${balance:.2f}",
                    "reserved":    f"${res_f:.2f}",
                    "net_spendable": f"${net:.2f}",
                },
            },
            # Addresses
            "addresses": snapshot["addresses"],

            # Deposit
            "deposit": {
                "methods":    deposit_methods,
                "min_amount": MIN_DEPOSIT,
                "currency":   "USD",
            },

            # Withdrawal
            "withdrawal": {
                "supported_networks":  SUPPORTED_NETWORKS,
                "min_amount":          config.WITHDRAWAL_MIN_AMOUNT,
                "max_amount":          config.WITHDRAWAL_MAX_AMOUNT,
                "fee_percent":         config.WITHDRAWAL_FEE_PERCENT,
                "fee_fixed":           config.WITHDRAWAL_FEE_FIXED,
                "daily_limit":         config.WITHDRAWAL_DAILY_LIMIT,
                "monthly_limit":       config.WITHDRAWAL_MONTHLY_LIMIT,
                "currency":            "USD",
            },
        },
        message="Wallet overview retrieved successfully.",
    )


@router.get("/balance", summary="Get balance breakdown")
async def get_wallet_balance(user=Depends(require_api_key)):
    """Returns the available, reserved, and net spendable balance."""
    snapshot = await get_wallet_snapshot(user["user_id"])
    balance = float(snapshot["balance"])
    res_f   = float(snapshot["reserved_balance"])
    net     = max(0.0, balance - res_f)

    return ok(
        data={
            "available":       round(balance, 4),
            "reserved":        round(res_f, 4),
            "net_spendable":   round(net, 4),
            "currency":        "USD",
            "formatted": {
                "available":     f"${balance:.2f}",
                "reserved":      f"${res_f:.2f}",
                "net_spendable": f"${net:.2f}",
            },
        },
        message="Balance retrieved successfully.",
    )


# ── Withdrawal Addresses ──────────────────────────────────────────────────────

@router.get("/address", summary="Get saved withdrawal addresses")
async def get_addresses(user=Depends(require_api_key)):
    """Returns your saved withdrawal addresses organised by network."""
    addrs = await get_wallet_addresses(user["user_id"])
    return ok(
        data={
            "addresses":         addrs,
            "supported_networks": SUPPORTED_NETWORKS,
            "total_saved":       sum(1 for v in addrs.values() if v),
        },
        message="Withdrawal addresses retrieved successfully.",
    )


@router.post("/address", summary="Set or update a withdrawal address")
async def set_address(body: SetAddressIn, user=Depends(require_api_key)):
    """
    Save or update a withdrawal address for a given network.

    Supported networks: TRC20 (USDT on Tron), BEP20 (USDT on BSC).
    The address is validated for format before saving.
    """
    network = body.network.upper()
    address = body.address.strip()

    if network not in SUPPORTED_NETWORKS:
        return err(400, f"Unsupported network '{network}'. Supported: {', '.join(SUPPORTED_NETWORKS)}")

    from server.utils.validation import validate_wallet_address
    addr_err = validate_wallet_address(network, address)
    if addr_err:
        return err(400, f"Invalid {network} address: {addr_err}")

    await set_wallet_address(user["user_id"], network, address)
    _log.info("Address set: user=%s network=%s address=%s…", user["user_id"], network, address[:10])

    return ok(
        data={
            "network":    network,
            "address":    address,
            "is_default": True,
        },
        message=f"{network} withdrawal address saved successfully.",
    )


# ── Withdrawals ───────────────────────────────────────────────────────────────

@router.post("/withdraw", summary="Request a USDT withdrawal")
async def request_withdrawal(body: WithdrawIn, user=Depends(require_api_key)):
    """
    Request a USDT withdrawal to your saved wallet address.

    Flow:
      1. Validates amount is within limits, balance is sufficient, address is saved
      2. Reserves balance to prevent double-spend
      3. Submits payout to gateway (if configured) or queues for admin approval
      4. Returns withdrawal ID and current status for tracking

    Poll `GET /api/v1/wallet/withdrawals/{withdrawal_id}` for status updates,
    or enable webhook callbacks on your account for push notifications.
    """
    withdrawals_enabled = await get_setting("withdrawals_enabled")
    if not withdrawals_enabled:
        return err(503, "Withdrawals are temporarily disabled by the platform. Please try again later.")

    network = body.network.upper()

    svc    = get_withdrawal_service()
    result = await svc.create_withdrawal(
        user_id=user["user_id"],
        amount=body.amount,
        network=network,
    )

    if not result["ok"]:
        return err(400, result["error"])

    return ok(
        data={
            "withdrawal_id":   result["withdrawal_id"],
            "status":          result["status"],
            "status_label":    _STATUS_LABELS.get(result["status"], result["status"]),
            "amount":          result["amount"],
            "fee":             result.get("fee", 0.0),
            "net_amount":      result.get("net_amount", result["amount"]),
            "network":         result["network"],
            "currency":        "USD",
            "wallet_address":  result["wallet_address"],
            "gateway":         result.get("gateway", "manual"),
            "track_id":        result.get("track_id"),
            "poll_url":        f"/api/v1/wallet/withdrawals/{result['withdrawal_id']}",
        },
        message=result.get("message", "Withdrawal request submitted. You will be notified when it is processed."),
    )


@router.get("/withdrawals", summary="Withdrawal history")
async def my_withdrawals(
    page:   int = Query(1,  ge=1,                description="Page number (1-based)"),
    limit:  int = Query(20, ge=1, le=100,        description="Items per page"),
    status: str = Query(None, description="Filter by status: pending | processing | completed | failed | cancelled"),
    network: str = Query(None, description="Filter by network: TRC20 | BEP20"),
    user=Depends(require_api_key),
):
    """Returns your withdrawal history, newest first, with rich status metadata."""
    _net = network.upper() if network else None

    # Fetch page + all summary counts concurrently
    withdrawals, total, total_all, total_completed, total_pending = await asyncio.gather(
        get_user_withdrawals(
            user["user_id"], limit=limit, page=page,
            status=status or None, network=_net,
        ),
        count_user_withdrawals(user["user_id"], status=status or None, network=_net),
        count_user_withdrawals(user["user_id"]),
        count_user_withdrawals(user["user_id"], status="completed"),
        count_user_withdrawals(user["user_id"], status="pending"),
    )

    items = [_withdrawal_view(w) for w in withdrawals]

    return paginate(
        data=items,
        page=page,
        limit=limit,
        total=total,
        message="Withdrawal history retrieved successfully.",
        summary={
            "total_withdrawals": total_all,
            "completed_count":   total_completed,
            "pending_count":     total_pending,
        },
    )


@router.get("/withdrawals/{withdrawal_id}", summary="Single withdrawal detail")
async def get_withdrawal_status(withdrawal_id: str, user=Depends(require_api_key)):
    """
    Returns the current state and full details of a specific withdrawal.
    Includes gateway tracking ID and blockchain transaction hash when available.
    """
    wit = await get_withdrawal_record(withdrawal_id)
    if not wit or wit.get("user_id") != user["user_id"]:
        return err(404, f"Withdrawal '{withdrawal_id}' not found.", ERR_NOT_FOUND)

    view = _withdrawal_view(wit)

    return ok(
        data=view,
        message="Withdrawal details retrieved successfully.",
    )


@router.post("/withdrawals/{withdrawal_id}/verify", summary="Force-verify withdrawal status")
async def verify_withdrawal(withdrawal_id: str, user=Depends(require_api_key)):
    """
    Force-poll the payment gateway for the latest withdrawal status.

    Use this when the gateway webhook was missed or delayed — it will
    sync the current gateway state into the platform and return the result.
    """
    wit = await get_withdrawal_record(withdrawal_id)
    if not wit or wit.get("user_id") != user["user_id"]:
        return err(404, f"Withdrawal '{withdrawal_id}' not found.", ERR_NOT_FOUND)

    svc    = get_withdrawal_service()
    result = await svc.verify_withdrawal(withdrawal_id)

    if not result["ok"]:
        return err(400, result["error"])

    return ok(
        data={
            "withdrawal_id":   withdrawal_id,
            "status":          result.get("status"),
            "status_label":    _STATUS_LABELS.get(result.get("status", ""), ""),
            "gateway_status":  result.get("gateway_status"),
            "tx_hash":         result.get("tx_hash"),
            "is_final":        result.get("status") in {"completed", "failed", "rejected", "cancelled"},
        },
        message=result.get("message", "Gateway status verified and synced."),
    )


# ── Transactions ──────────────────────────────────────────────────────────────

@router.get("/transactions", summary="Full transaction ledger")
async def my_transactions(
    page:  int = Query(1,  ge=1,         description="Page number (1-based)"),
    limit: int = Query(30, ge=1, le=100, description="Items per page"),
    txn_type: str = Query(None, description="Filter: deposit | withdrawal | purchase | sale | refund | bonus | adjustment"),
    user=Depends(require_api_key),
):
    """
    Returns your full transaction ledger (all credits and debits), newest first.

    Each entry records the type, amount, direction (+/-), reference ID,
    note, and timestamp — suitable for a detailed transaction screen or CSV export.
    """
    from server.utils.database.walletdb import transactionsdb

    valid_types = {
        "deposit", "withdrawal", "purchase", "sale",
        "refund", "bonus", "adjustment", "referral",
    }
    if txn_type and txn_type not in valid_types:
        return err(400, f"Invalid type '{txn_type}'. Allowed: {', '.join(sorted(valid_types))}")

    query: dict = {"user_id": user["user_id"]}
    if txn_type:
        query["type"] = txn_type

    txns, total = await asyncio.gather(
        get_user_transactions(user["user_id"], limit=limit, page=page, txn_type=txn_type or None),
        transactionsdb.count_documents(query),
    )
    items = [_clean(t) for t in txns]

    return paginate(
        data=items,
        page=page,
        limit=limit,
        total=total,
        message="Transaction ledger retrieved successfully.",
    )
