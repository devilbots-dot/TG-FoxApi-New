"""
WithdrawalService — orchestrates the full withdrawal lifecycle.

Responsibilities:
  • Validate the request (limits, balance, address, network, currency)
  • Reserve user balance (atomic, prevents double-spend)
  • Create the withdrawal record in MongoDB
  • Submit the payout to the configured gateway (if mode=auto)
  • Handle every gateway response state
  • Release or finalize balance on final status
  • Log every action for the audit trail
"""

from __future__ import annotations

import asyncio
from typing import Optional

import config
from server.logging import LOGGER
from server.services.withdrawal.models import (
    FeeCalculation,
    PayoutResult,
    VerifyResult,
    WithdrawalLimits,
)
from server.utils.withdrawal_statuses import WithdrawalStatus
from server.services.withdrawal.registry import WithdrawalRegistry
from server.utils.database.withdrawaldb import (
    create_withdrawal_record,
    get_withdrawal_record,
    get_user_active_withdrawal_count,
    get_user_daily_withdrawal_total,
    get_user_monthly_withdrawal_total,
    update_withdrawal_status,
    store_gateway_result,
    finalize_withdrawal,
    release_withdrawal,
    log_withdrawal_event,
    get_pending_unsubmitted,
    get_in_flight_withdrawals,
)
from server.utils.database.userdb import (
    reserve_earned_balance_atomic,
    release_reserved_balance,
    finalize_reserved_balance,
    get_balance,
    get_wallet_addresses,
    SUPPORTED_NETWORKS,
)
from server.utils.validation import validate_wallet_address

_log = LOGGER(__name__)


class WithdrawalService:
    """
    High-level orchestrator for the withdrawal (payout) flow.

    Usage:
        svc = WithdrawalService(registry)
        result = await svc.create_withdrawal(user_id=…, amount=…, network=…)
    """

    def __init__(self, registry: WithdrawalRegistry) -> None:
        self._registry = registry

    # ── Public: create withdrawal ─────────────────────────────────────────────

    async def create_withdrawal(
        self,
        *,
        user_id: int,
        amount: float,
        network: str,
        wallet_address: Optional[str] = None,
        memo: str = "",
    ) -> dict:
        """
        Full withdrawal creation flow.

        Returns a result dict:
          { "ok": True, "withdrawal_id": "WIT-...", "status": "processing", ... }
          { "ok": False, "error": "..." }
        """
        network = network.upper()

        # ── 1. Network + currency validation ─────────────────────────────────
        if network not in SUPPORTED_NETWORKS:
            return {"ok": False, "error": f"Unsupported network '{network}'. Supported: {', '.join(SUPPORTED_NETWORKS)}"}

        currency = config.OXAPAY_WITHDRAW_CURRENCY  # e.g. "USDT"

        # ── 2. Amount validation ──────────────────────────────────────────────
        limits = self._limits()
        if amount < limits.min_amount:
            return {"ok": False, "error": f"Minimum withdrawal is ${limits.min_amount:.2f}."}
        if amount > limits.max_amount:
            return {"ok": False, "error": f"Maximum withdrawal is ${limits.max_amount:.2f}."}

        # ── 3. Address resolution + validation ────────────────────────────────
        if not wallet_address:
            addresses = await get_wallet_addresses(user_id)
            wallet_address = addresses.get(network.lower())
        if not wallet_address:
            return {"ok": False, "error": f"No {network} address on file. Set one first via /wallet/address."}

        addr_err = validate_wallet_address(network, wallet_address)
        if addr_err:
            return {"ok": False, "error": f"Invalid {network} address: {addr_err}"}

        # ── 4. Balance check ──────────────────────────────────────────────────
        reserve_balance = await get_reserve_balance(user_id)
        if reserve_balance < amount:
            return {"ok": False, "error": f"Insufficient withdrawable balance. You have ${reserve_balance:.2f}, need ${amount:.2f}."}

        # ── 5. Daily limit ────────────────────────────────────────────────────
        daily_total = await get_user_daily_withdrawal_total(user_id)
        if daily_total + amount > limits.daily_limit:
            remaining = max(0.0, limits.daily_limit - daily_total)
            return {"ok": False, "error": f"Daily withdrawal limit exceeded. Remaining today: ${remaining:.2f}."}

        # ── 6. Monthly limit ──────────────────────────────────────────────────
        monthly_total = await get_user_monthly_withdrawal_total(user_id)
        if monthly_total + amount > limits.monthly_limit:
            remaining = max(0.0, limits.monthly_limit - monthly_total)
            return {"ok": False, "error": f"Monthly withdrawal limit exceeded. Remaining this month: ${remaining:.2f}."}

        # ── 7. Max pending withdrawals ────────────────────────────────────────
        active_count = await get_user_active_withdrawal_count(user_id)
        if active_count >= limits.max_pending:
            return {"ok": False, "error": f"You already have {active_count} pending withdrawal(s). Wait for them to complete."}

        # ── 8. Fee calculation ────────────────────────────────────────────────
        fee_calc = FeeCalculation.calculate(
            amount,
            fee_percent=config.WITHDRAWAL_FEE_PERCENT,
            fee_fixed=config.WITHDRAWAL_FEE_FIXED,
        )
        if fee_calc.net_amount <= 0:
            return {"ok": False, "error": "Amount too small after fees."}

        # ── 9. Reserve balance ────────────────────────────────────────────────
        reserved = await reserve_earned_balance_atomic(user_id, amount)
        if not reserved:
            # Re-read for accurate error message
            balance = await get_balance(user_id)
            return {"ok": False, "error": f"Insufficient withdrawable balance. Available: ${await get_reserve_balance(user_id):.2f}."}

        # ── 10. Determine mode + provider ─────────────────────────────────────
        mode = config.WITHDRAWAL_GATEWAY  # "auto" or "manual"
        provider = self._registry.default() if mode == "auto" else None

        # If auto mode but no configured provider, fall back to manual
        if mode == "auto" and provider is None:
            mode = "manual"
            _log.warning("No withdrawal provider configured — falling back to manual mode")

        # ── 11. Create DB record ──────────────────────────────────────────────
        try:
            wit_id = await create_withdrawal_record(
                user_id=user_id,
                amount=amount,
                currency=currency,
                network=network,
                wallet_address=wallet_address,
                fee_amount=fee_calc.fee_amount,
                net_amount=fee_calc.net_amount,
                mode=mode,
                gateway=provider.provider_id if provider else "manual",
                memo=memo,
            )
        except Exception as exc:
            # Record creation failed — release reserved balance immediately
            await release_reserved_balance(user_id, amount)
            _log.error("create_withdrawal_record failed for user %s: %s", user_id, exc)
            return {"ok": False, "error": "Failed to create withdrawal record. Balance refunded."}

        await log_withdrawal_event(wit_id, "created", {
            "user_id": user_id, "amount": amount, "network": network,
            "wallet_address": wallet_address, "mode": mode,
        })

        # Notify user that request was received
        await self._notify(user_id, "withdrawal_submitted", wit={
            "withdrawal_id": wit_id,
            "amount":        amount,
            "network":       network,
            "wallet_address": wallet_address,
        })

        # ── 12. Submit to gateway (if auto) ───────────────────────────────────
        if mode == "auto" and provider is not None:
            result = await self._submit_to_gateway(
                wit_id=wit_id,
                provider=provider,
                user_id=user_id,
                amount=fee_calc.net_amount,
                currency=currency,
                network=network,
                address=wallet_address,
                memo=memo,
            )
            return {
                "ok": True,
                "withdrawal_id": wit_id,
                "status": result.internal_status,
                "gateway": provider.provider_id,
                "track_id": result.track_id,
                "amount": amount,
                "fee": fee_calc.fee_amount,
                "net_amount": fee_calc.net_amount,
                "network": network,
                "wallet_address": wallet_address,
                "message": result.message or "Withdrawal submitted to gateway.",
            }

        # Manual mode — waiting for admin
        return {
            "ok": True,
            "withdrawal_id": wit_id,
            "status": WithdrawalStatus.PENDING,
            "gateway": "manual",
            "track_id": None,
            "amount": amount,
            "fee": fee_calc.fee_amount,
            "net_amount": fee_calc.net_amount,
            "network": network,
            "wallet_address": wallet_address,
            "message": "Withdrawal submitted. Pending admin approval.",
        }

    # ── Public: verify single withdrawal ─────────────────────────────────────

    async def admin_retry(self, withdrawal_id: str, admin_note: str = "") -> dict:
        """
        Admin-triggered re-submission of a failed or stuck withdrawal to the gateway.
        Resets the record to pending, clears old track_id, then submits fresh.
        Only valid for auto-mode withdrawals with a configured gateway.
        """
        wit = await get_withdrawal_record(withdrawal_id)
        if not wit:
            return {"ok": False, "error": "Withdrawal not found."}

        if wit.get("status") in WithdrawalStatus.FINAL and wit.get("status") not in {
            WithdrawalStatus.FAILED
        }:
            return {"ok": False, "error": f"Cannot retry — withdrawal is in final state: {wit['status']}."}

        provider_id = wit.get("gateway", "oxapay")
        if provider_id == "manual":
            return {"ok": False, "error": "Manual-mode withdrawal — use approve/reject instead of retry."}

        provider = self._registry.get(provider_id)
        if not provider or not provider.is_configured():
            return {"ok": False, "error": f"Provider '{provider_id}' not configured."}

        # Reset to pending, clear old track_id so submit runs fresh
        await update_withdrawal_status(withdrawal_id, WithdrawalStatus.PENDING,
                                       gateway_track_id="", gateway_status="")
        await log_withdrawal_event(withdrawal_id, "admin_retry", {
            "note": admin_note, "prev_status": wit.get("status")
        })

        result = await self._submit_to_gateway(
            wit_id=withdrawal_id,
            provider=provider,
            user_id=wit["user_id"],
            amount=wit.get("net_amount", wit.get("amount", 0)),
            currency=wit.get("currency", "USDT"),
            network=wit.get("network", "TRC20"),
            address=wit.get("wallet_address", ""),
            memo=wit.get("memo", ""),
        )

        new_wit = await get_withdrawal_record(withdrawal_id)
        return {
            "ok":           True,
            "withdrawal_id": withdrawal_id,
            "status":        new_wit.get("status") if new_wit else result.internal_status,
            "track_id":      result.track_id,
            "message":       result.message or "Re-submitted to gateway.",
        }

    async def verify_withdrawal(self, withdrawal_id: str) -> dict:
        """
        Force-poll the gateway for a single withdrawal's current status.
        Returns updated status info.
        """
        wit = await get_withdrawal_record(withdrawal_id)
        if not wit:
            return {"ok": False, "error": "Withdrawal not found."}

        track_id = wit.get("gateway_track_id")
        provider_id = wit.get("gateway", "oxapay")

        if not track_id:
            # Auto-mode stuck without track_id — try to submit
            if provider_id != "manual":
                provider = self._registry.get(provider_id)
                if provider and provider.is_configured():
                    await self._retry_unsubmitted(wit)
                    refreshed = await get_withdrawal_record(withdrawal_id)
                    return {
                        "ok": True,
                        "withdrawal_id": withdrawal_id,
                        "status": refreshed.get("status") if refreshed else wit.get("status"),
                        "message": "Re-submitted to gateway.",
                    }
            return {
                "ok": True,
                "withdrawal_id": withdrawal_id,
                "status": wit.get("status"),
                "message": "Pending admin approval (manual mode).",
            }

        if wit.get("status") in WithdrawalStatus.FINAL:
            return {
                "ok": True,
                "withdrawal_id": withdrawal_id,
                "status": wit.get("status"),
                "message": "Withdrawal already in final state.",
            }

        provider = self._registry.get(provider_id)
        if not provider:
            return {"ok": False, "error": f"Unknown provider '{provider_id}'."}

        result = await provider.verify_payout(track_id)
        await self._apply_verify_result(wit, result)

        return {
            "ok": True,
            "withdrawal_id": withdrawal_id,
            "status": result.internal_status if result.found else wit.get("status"),
            "gateway_status": result.gateway_status,
            "tx_hash": result.tx_hash,
            "message": result.message,
        }

    # ── Public: admin approve (manual mode) ───────────────────────────────────

    async def admin_approve(
        self,
        withdrawal_id: str,
        tx_hash: str = "",
        admin_note: str = "",
    ) -> dict:
        """Admin manually marks a withdrawal as completed."""
        wit = await get_withdrawal_record(withdrawal_id)
        if not wit:
            return {"ok": False, "error": "Withdrawal not found."}
        if wit.get("status") in WithdrawalStatus.FINAL:
            return {"ok": False, "error": f"Withdrawal already in final state: {wit['status']}."}

        user_id = wit["user_id"]
        amount  = wit["amount"]

        await finalize_withdrawal(
            withdrawal_id=withdrawal_id,
            user_id=user_id,
            amount=amount,
            tx_hash=tx_hash or "manual",
            admin_note=admin_note,
        )
        await log_withdrawal_event(withdrawal_id, "admin_approved", {
            "tx_hash": tx_hash, "note": admin_note
        })
        await self._notify(user_id, "withdrawal_approved", wit=wit, tx_hash=tx_hash or "manual")
        return {"ok": True, "withdrawal_id": withdrawal_id, "status": WithdrawalStatus.COMPLETED}

    # ── Public: admin reject ──────────────────────────────────────────────────

    async def admin_reject(
        self,
        withdrawal_id: str,
        reason: str = "",
    ) -> dict:
        """Admin rejects a withdrawal and releases the reserved balance."""
        wit = await get_withdrawal_record(withdrawal_id)
        if not wit:
            return {"ok": False, "error": "Withdrawal not found."}
        if wit.get("status") in WithdrawalStatus.FINAL:
            return {"ok": False, "error": f"Withdrawal already in final state: {wit['status']}."}

        user_id = wit["user_id"]
        amount  = wit["amount"]

        await release_withdrawal(
            withdrawal_id=withdrawal_id,
            user_id=user_id,
            amount=amount,
            new_status=WithdrawalStatus.REJECTED,
            reason=reason,
        )
        await log_withdrawal_event(withdrawal_id, "admin_rejected", {"reason": reason})
        await self._notify(user_id, "withdrawal_rejected", wit=wit, note=reason)
        return {"ok": True, "withdrawal_id": withdrawal_id, "status": WithdrawalStatus.REJECTED}

    # ── Public: background verification sweep ─────────────────────────────────

    async def verify_pending_withdrawals(self) -> dict:
        """
        Background worker: poll OxaPay for all in-flight withdrawals.
        Returns counts of verified/updated records.
        """
        in_flight = await get_in_flight_withdrawals(limit=50)
        stats = {"checked": 0, "updated": 0, "errors": 0}

        for wit in in_flight:
            track_id    = wit.get("gateway_track_id")
            provider_id = wit.get("gateway", "oxapay")

            if not track_id:
                # Pending/unsubmitted — try to submit
                await self._retry_unsubmitted(wit)
                stats["checked"] += 1
                continue

            provider = self._registry.get(provider_id)
            if not provider:
                stats["errors"] += 1
                continue

            try:
                result = await provider.verify_payout(track_id)
                changed = await self._apply_verify_result(wit, result)
                stats["checked"] += 1
                if changed:
                    stats["updated"] += 1
            except Exception as exc:
                _log.error("verify_pending error for %s: %s", wit.get("withdrawal_id"), exc)
                stats["errors"] += 1
                await asyncio.sleep(0.1)  # brief pause on error

        return stats

    # ── Public: expire stale withdrawals ─────────────────────────────────────

    async def expire_stale_withdrawals(self) -> int:
        """Mark withdrawals older than WITHDRAWAL_EXPIRE_HOURS as expired."""
        from server.utils.common import utcnow
        from datetime import timedelta
        from server.utils.database.withdrawaldb import get_expirable_withdrawals

        cutoff = utcnow() - timedelta(hours=config.WITHDRAWAL_EXPIRE_HOURS)
        expirable = await get_expirable_withdrawals(before=cutoff)
        count = 0
        for wit in expirable:
            await release_withdrawal(
                withdrawal_id=wit["withdrawal_id"],
                user_id=wit["user_id"],
                amount=wit["amount"],
                new_status=WithdrawalStatus.EXPIRED,
                reason=f"No resolution within {config.WITHDRAWAL_EXPIRE_HOURS}h",
            )
            await log_withdrawal_event(wit["withdrawal_id"], "expired", {})
            await self._notify(wit["user_id"], "withdrawal_failed",
                               wit=wit, reason="Expired — no resolution")
            count += 1
        return count

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _submit_to_gateway(
        self,
        *,
        wit_id: str,
        provider,
        user_id: int,
        amount: float,
        currency: str,
        network: str,
        address: str,
        memo: str,
    ) -> PayoutResult:
        callback_url = config.WITHDRAWAL_CALLBACK_URL

        try:
            result = await provider.submit_payout(
                withdrawal_id=wit_id,
                address=address,
                currency=currency,
                network=network,
                amount=amount,
                callback_url=callback_url,
                memo=memo,
            )
        except Exception as exc:
            _log.error("Gateway submit raised for %s: %s", wit_id, exc)
            result = PayoutResult(
                success=False,
                message=str(exc),
                internal_status=WithdrawalStatus.FAILED,
            )

        # Persist gateway request/response
        await store_gateway_result(wit_id, result)

        if result.success:
            await update_withdrawal_status(
                wit_id,
                new_status=result.internal_status,
                gateway_track_id=result.track_id,
                gateway_status=result.gateway_status,
            )
            await log_withdrawal_event(wit_id, "gateway_accepted", {
                "track_id": result.track_id, "gateway_status": result.gateway_status
            })

            # If gateway immediately confirms — finalize balance
            if result.internal_status == WithdrawalStatus.COMPLETED:
                wit = await get_withdrawal_record(wit_id)
                if wit:
                    await finalize_withdrawal(
                        withdrawal_id=wit_id,
                        user_id=user_id,
                        amount=wit["amount"],
                        tx_hash=None,
                    )
                    await self._notify(user_id, "withdrawal_approved", wit=wit)
        else:
            # Transport failures are explicitly retryable.  OxaPay returns
            # success=False with internal_status=pending for timeouts and
            # network errors; releasing the user's reserved balance here would
            # allow a later retry to pay out without a valid reservation.
            if result.internal_status in {
                WithdrawalStatus.PENDING,
                WithdrawalStatus.PROCESSING,
                WithdrawalStatus.WAITING_CONFIRMATION,
            }:
                await update_withdrawal_status(
                    wit_id,
                    new_status=WithdrawalStatus.PENDING,
                    reason=result.message or "Gateway request will be retried",
                )
                await log_withdrawal_event(wit_id, "gateway_retryable_failure", {
                    "message": result.message,
                    "error_type": result.error_type,
                    "error_key": result.error_key,
                })
                _log.warning(
                    "Withdrawal %s submission is retryable; reserved balance retained: %s",
                    wit_id, result.message,
                )
                wit = await get_withdrawal_record(wit_id)
                if wit:
                    await self._notify(user_id, "withdrawal_submitted", wit=wit)
                return result

            # Check if this is a gateway-side failure (not user's fault).
            # These errors mean OUR gateway account has an issue — fall back to
            # manual review instead of rejecting the user's withdrawal.
            _GATEWAY_SIDE_ERRORS = {
                "amount_exceeds_balance",  # OxaPay payout account has insufficient funds
                "insufficient_balance",
                "account_suspended",
                "service_unavailable",
            }
            is_gateway_side = result.error_key in _GATEWAY_SIDE_ERRORS

            if is_gateway_side:
                # Keep balance reserved — switch to manual mode for admin review
                await update_withdrawal_status(
                    wit_id,
                    new_status=WithdrawalStatus.PENDING,
                    gateway=("manual"),
                    reason=f"Auto-gateway unavailable ({result.error_key}); queued for manual review",
                )
                await log_withdrawal_event(wit_id, "gateway_fallback_manual", {
                    "message": result.message,
                    "error_type": result.error_type,
                    "error_key": result.error_key,
                })
                wit = await get_withdrawal_record(wit_id)
                if wit:
                    await self._notify(user_id, "withdrawal_submitted", wit=wit)
                _log.warning(
                    "Withdrawal %s fell back to manual mode due to gateway error: %s",
                    wit_id, result.error_key,
                )
            else:
                # Genuine gateway rejection — release balance and notify user
                await release_withdrawal(
                    withdrawal_id=wit_id,
                    user_id=user_id,
                    amount=(await get_withdrawal_record(wit_id) or {}).get("amount", 0),
                    new_status=result.internal_status,
                    reason=result.message,
                )
                await log_withdrawal_event(wit_id, "gateway_rejected", {
                    "message": result.message,
                    "error_type": result.error_type,
                    "error_key": result.error_key,
                })
                wit = await get_withdrawal_record(wit_id)
                if wit:
                    await self._notify(user_id, "withdrawal_rejected", wit=wit, note=result.message)

        return result

    async def _apply_verify_result(self, wit: dict, result: VerifyResult) -> bool:
        """Apply a VerifyResult to the withdrawal record. Returns True if status changed."""
        wit_id  = wit["withdrawal_id"]
        user_id = wit["user_id"]
        old_status = wit.get("status")

        # Update verification metadata
        await log_withdrawal_event(wit_id, "verified", {
            "gateway_status": result.gateway_status,
            "internal_status": result.internal_status,
            "tx_hash": result.tx_hash,
            "found": result.found,
        })

        if not result.found:
            return False

        new_status = result.internal_status
        if new_status == old_status:
            return False

        if new_status == WithdrawalStatus.COMPLETED:
            amount = wit.get("amount", 0)
            await finalize_withdrawal(
                withdrawal_id=wit_id,
                user_id=user_id,
                amount=amount,
                tx_hash=result.tx_hash,
                gateway_fee=result.fee,
            )
            await log_withdrawal_event(wit_id, "completed", {"tx_hash": result.tx_hash})
            await self._notify(user_id, "withdrawal_approved", wit=wit, tx_hash=result.tx_hash or "")
            return True

        elif new_status in WithdrawalStatus.RELEASE:
            amount = wit.get("amount", 0)
            await release_withdrawal(
                withdrawal_id=wit_id,
                user_id=user_id,
                amount=amount,
                new_status=new_status,
                reason=result.message,
                gateway_status=result.gateway_status,
            )
            await log_withdrawal_event(wit_id, f"status_{new_status}", {"reason": result.message})
            await self._notify(user_id, "withdrawal_rejected", wit=wit, note=result.message)
            return True

        else:
            # In-flight status update (processing → waiting_confirmation, etc.)
            await update_withdrawal_status(
                wit_id,
                new_status=new_status,
                gateway_status=result.gateway_status,
                tx_hash=result.tx_hash,
            )
            return True

    async def _retry_unsubmitted(self, wit: dict) -> None:
        """Retry gateway submission for a withdrawal stuck in 'pending' with no track_id."""
        wit_id = wit.get("withdrawal_id", "?")
        provider_id = wit.get("gateway", "oxapay")
        if provider_id == "manual":
            return  # manual mode — admin handles it
        provider = self._registry.get(provider_id)
        if not provider or not provider.is_configured():
            return

        _log.info("Retrying unsubmitted withdrawal %s", wit_id)
        await self._submit_to_gateway(
            wit_id=wit_id,
            provider=provider,
            user_id=wit["user_id"],
            amount=wit.get("net_amount", wit.get("amount", 0)),
            currency=wit.get("currency", "USDT"),
            network=wit.get("network", "TRC20"),
            address=wit.get("wallet_address", ""),
            memo=wit.get("memo", ""),
        )

    async def _notify(self, user_id: int, event: str, *, wit: dict, **kwargs) -> None:
        """Send a Telegram notification; never raises."""
        try:
            from server.utils.notifications import notify
            await notify(
                user_id, event,
                withdrawal_id=wit.get("withdrawal_id", ""),
                amount=wit.get("amount", 0.0),
                network=wit.get("network", ""),
                wallet_address=wit.get("wallet_address", ""),
                **kwargs,
            )
        except Exception as exc:
            _log.warning("Notification '%s' failed for user %s: %s", event, user_id, exc)

    @staticmethod
    def _limits() -> WithdrawalLimits:
        return WithdrawalLimits(
            min_amount=config.WITHDRAWAL_MIN_AMOUNT,
            max_amount=config.WITHDRAWAL_MAX_AMOUNT,
            daily_limit=config.WITHDRAWAL_DAILY_LIMIT,
            monthly_limit=config.WITHDRAWAL_MONTHLY_LIMIT,
            max_pending=config.WITHDRAWAL_MAX_PENDING,
        )
