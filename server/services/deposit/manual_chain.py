"""Secure manual USDT transaction-hash verification for BEP20 and TRC20.

Each submitted transaction hash is globally claimed once in a dedicated MongoDB
collection before any balance operation. A claim is never payment proof on its
own: the verifier still requires a successful, final USDT transfer to the
payment instruction's address for at least the expected amount.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from os import getenv
from typing import Any, Literal

import aiohttp

from server.logging import LOGGER

_log = LOGGER(__name__)

_BEP20_METHOD = "bep20_scan"
_TRC20_METHOD = "trc20_scan"
_MANUAL_METHODS = {_BEP20_METHOD: "BEP20", _TRC20_METHOD: "TRC20"}

_BEP20_USDT_CONTRACT = "0x55d398326f99059ff775485246999027b3197955"
_TRC20_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_BEP20_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TRC20_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_ETHERSCAN_V2_API = "https://api.etherscan.io/v2/api"
_TRONSCAN_TRANSACTION_INFO_API = "https://apilist.tronscanapi.com/api/transaction-info"
_TIMEOUT = aiohttp.ClientTimeout(total=18, connect=8)


class ManualChainError(ValueError):
    """Expected user-facing manual-chain verification error."""


@dataclass(frozen=True)
class ChainResult:
    """Normalized provider outcome with no secret or raw provider payload."""

    state: Literal["verified", "pending", "rejected", "unavailable"]
    code: str
    confirmations: int | None = None
    received_amount: Decimal | None = None

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "code": self.code,
            "confirmations": self.confirmations,
            "received_amount": float(self.received_amount) if self.received_amount is not None else None,
        }


def is_manual_chain_method(method: str) -> bool:
    return str(method or "").strip() in _MANUAL_METHODS


def network_for_method(method: str) -> str:
    try:
        return _MANUAL_METHODS[str(method or "").strip()]
    except KeyError as exc:
        raise ManualChainError("This deposit does not use manual transaction-hash verification.") from exc


def normalize_transaction_hash(network: str, value: str) -> str:
    """Validate and canonicalize a hash without accepting explorer URLs or IDs."""
    raw = str(value or "").strip()
    if network == "BEP20":
        if not _BEP20_HASH_RE.fullmatch(raw):
            raise ManualChainError("Enter a valid BSC transaction hash beginning with 0x.")
        return raw.lower()
    if network == "TRC20":
        raw = raw[2:] if raw.lower().startswith("0x") else raw
        if not _TRC20_HASH_RE.fullmatch(raw):
            raise ManualChainError("Enter a valid TRON transaction ID containing 64 hexadecimal characters.")
        return raw.lower()
    raise ManualChainError("Unsupported manual deposit network.")


def _display_hash(tx_hash: str) -> str:
    """Avoid logging or exposing a full user transaction hash in service logs."""
    return f"{tx_hash[:10]}…{tx_hash[-6:]}" if len(tx_hash) > 18 else "[redacted]"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _expected_amount(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ManualChainError("Deposit amount is invalid. Create a new payment instruction.") from exc
    if amount <= 0:
        raise ManualChainError("Deposit amount is invalid. Create a new payment instruction.")
    return amount


async def _get_json(url: str, *, params: dict[str, str], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any] | None]:
    """Fetch JSON with a bounded timeout and no transaction payload logging."""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, params=params, headers=headers or {}) as response:
                if response.status != 200:
                    return response.status, None
                payload = await response.json(content_type=None)
                return response.status, payload if isinstance(payload, dict) else None
    except (asyncio.TimeoutError, aiohttp.ClientError, ValueError):
        return 0, None


def _hex_int(value: Any) -> int | None:
    try:
        raw = str(value or "")
        return int(raw, 16) if raw.startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return None


def _bep20_recipient_from_topic(topic: Any) -> str:
    raw = str(topic or "").lower()
    if not raw.startswith("0x") or len(raw) != 66:
        return ""
    return f"0x{raw[-40:]}"


async def _bep20_result(
    tx_hash: str,
    *,
    recipient: str,
    expected_amount: Decimal,
    created_at: datetime,
) -> ChainResult:
    api_key = getenv("BSCSCAN_API_KEY", "").strip()
    if not api_key:
        return ChainResult("unavailable", "provider_unavailable")

    common = {"chainid": "56", "module": "proxy", "apikey": api_key}
    receipt_status, receipt_data = await _get_json(
        _ETHERSCAN_V2_API,
        params={**common, "action": "eth_getTransactionReceipt", "txhash": tx_hash},
    )
    if receipt_status != 200 or receipt_data is None:
        return ChainResult("unavailable", "provider_unavailable")
    receipt = (receipt_data or {}).get("result")
    if not isinstance(receipt, dict):
        if receipt_data and receipt_data.get("error"):
            return ChainResult("unavailable", "provider_unavailable")
        return ChainResult("pending", "transaction_not_found")
    if str(receipt.get("status") or "").lower() != "0x1":
        return ChainResult("rejected", "transaction_failed")

    block_number = _hex_int(receipt.get("blockNumber"))
    if block_number is None:
        return ChainResult("pending", "transaction_not_mined")
    _, head_data = await _get_json(
        _ETHERSCAN_V2_API,
        params={**common, "action": "eth_blockNumber"},
    )
    head_number = _hex_int((head_data or {}).get("result"))
    if head_number is None or head_number < block_number:
        return ChainResult("unavailable", "provider_unavailable")

    _, block_data = await _get_json(
        _ETHERSCAN_V2_API,
        params={**common, "action": "eth_getBlockByNumber", "tag": hex(block_number), "boolean": "false"},
    )
    block = (block_data or {}).get("result")
    block_timestamp = _hex_int(block.get("timestamp")) if isinstance(block, dict) else None
    if block_timestamp is None:
        return ChainResult("unavailable", "provider_unavailable")
    if datetime.fromtimestamp(block_timestamp, tz=timezone.utc) < created_at:
        return ChainResult("rejected", "transaction_predates_instruction")

    expected_recipient = recipient.lower()
    matching_amount: Decimal | None = None
    for event in receipt.get("logs") or []:
        if not isinstance(event, dict) or event.get("removed"):
            continue
        topics = event.get("topics") or []
        if (
            str(event.get("address") or "").lower() != _BEP20_USDT_CONTRACT
            or len(topics) < 3
            or str(topics[0] or "").lower() != _TRANSFER_TOPIC
            or _bep20_recipient_from_topic(topics[2]) != expected_recipient
        ):
            continue
        raw_amount = _hex_int(event.get("data"))
        if raw_amount is None:
            continue
        amount = Decimal(raw_amount) / (Decimal(10) ** 18)
        if amount >= expected_amount:
            matching_amount = amount
            break

    if matching_amount is None:
        return ChainResult("rejected", "transfer_details_mismatch")
    confirmations = head_number - block_number + 1
    minimum = _positive_env_int("DEPOSIT_BEP20_MIN_CONFIRMATIONS", 12)
    if confirmations < minimum:
        return ChainResult("pending", "awaiting_confirmations", confirmations, matching_amount)
    return ChainResult("verified", "verified", confirmations, matching_amount)


async def _trc20_result(
    tx_hash: str,
    *,
    recipient: str,
    expected_amount: Decimal,
    created_at: datetime,
) -> ChainResult:
    headers = {"Accept": "application/json"}
    api_key = getenv("TRONSCAN_API_KEY", "").strip()
    if api_key:
        headers["TRON-PRO-API-KEY"] = api_key
    transaction_status, transaction = await _get_json(
        _TRONSCAN_TRANSACTION_INFO_API,
        params={"hash": tx_hash},
        headers=headers,
    )
    if transaction_status != 200 or transaction is None:
        return ChainResult("unavailable", "provider_unavailable")
    if not transaction.get("hash"):
        return ChainResult("pending", "transaction_not_found")
    if transaction.get("revert") or str(transaction.get("contractRet") or "").upper() != "SUCCESS":
        return ChainResult("rejected", "transaction_failed")
    if not transaction.get("confirmed"):
        return ChainResult("pending", "awaiting_confirmations", int(transaction.get("confirmations") or 0))

    timestamp_ms = _hex_int(transaction.get("timestamp"))
    if timestamp_ms is None:
        return ChainResult("unavailable", "provider_unavailable")
    if datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc) < created_at:
        return ChainResult("rejected", "transaction_predates_instruction")

    transfers: list[Any] = []
    for key in ("trc20TransferInfo", "transfersAllList"):
        value = transaction.get(key)
        if isinstance(value, list):
            transfers.extend(value)
    primary = transaction.get("tokenTransferInfo")
    if isinstance(primary, dict):
        transfers.append(primary)

    matching_amount: Decimal | None = None
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        try:
            transfer_status = int(transfer.get("status"))
        except (TypeError, ValueError):
            continue
        if (
            str(transfer.get("contract_address") or "") != _TRC20_USDT_CONTRACT
            or str(transfer.get("to_address") or "") != recipient
            or str(transfer.get("tokenType") or transfer.get("tokenType2") or "").lower() != "trc20"
            or str(transfer.get("type") or "").lower() != "transfer"
            or transfer_status != 0
        ):
            continue
        try:
            decimals = int(transfer.get("decimals"))
            raw_amount = int(str(transfer.get("amount_str")))
        except (TypeError, ValueError):
            continue
        if decimals != 6:
            continue
        amount = Decimal(raw_amount) / (Decimal(10) ** decimals)
        if amount >= expected_amount:
            matching_amount = amount
            break

    if matching_amount is None:
        return ChainResult("rejected", "transfer_details_mismatch")
    confirmations = int(transaction.get("confirmations") or 0)
    minimum = _positive_env_int("DEPOSIT_TRC20_MIN_CONFIRMATIONS", 19)
    if confirmations < minimum:
        return ChainResult("pending", "awaiting_confirmations", confirmations, matching_amount)
    return ChainResult("verified", "verified", confirmations, matching_amount)


async def verify_manual_transaction(
    method: str,
    tx_hash: str,
    *,
    recipient: str,
    expected_amount: Any,
    created_at: Any,
) -> ChainResult:
    """Verify one normalized hash against an immutable payment instruction."""
    network = network_for_method(method)
    expected = _expected_amount(expected_amount)
    created = _utc(created_at)
    if not recipient:
        return ChainResult("unavailable", "instruction_incomplete")
    if network == "BEP20":
        return await _bep20_result(tx_hash, recipient=recipient, expected_amount=expected, created_at=created)
    return await _trc20_result(tx_hash, recipient=recipient, expected_amount=expected, created_at=created)


def _claims_collection():
    from server.core.mongo import collection

    return collection("deposit_transaction_hashes")


async def _claim_hash(network: str, tx_hash: str, deposit_id: str, user_id: int) -> tuple[bool, bool]:
    """Insert the global claim; returns ``(accepted, already_same_deposit)``."""
    claim_id = f"{network}:{tx_hash}"
    now = datetime.now(timezone.utc)
    try:
        await _claims_collection().insert_one({
            "_id": claim_id,
            "network": network,
            "tx_hash": tx_hash,
            "deposit_id": deposit_id,
            "user_id": int(user_id),
            "state": "claimed",
            "claimed_at": now,
            "checked_at": None,
        })
        return True, False
    except Exception as exc:
        existing = await _claims_collection().find_one({"_id": claim_id})
        if existing is not None:
            same_deposit = (
                str(existing.get("deposit_id") or "") == str(deposit_id)
                and int(existing.get("user_id") or 0) == int(user_id)
            )
            return same_deposit, same_deposit
        _log.error("manual_chain: hash claim storage failed (%s): %s", _display_hash(tx_hash), type(exc).__name__)
        raise RuntimeError("Unable to reserve this transaction hash. Please retry shortly.") from exc


async def _record_claim_result(network: str, tx_hash: str, result: ChainResult, deposit_id: str) -> None:
    await _claims_collection().update_one(
        {"_id": f"{network}:{tx_hash}", "deposit_id": deposit_id},
        {"$set": {
            "state": result.state,
            "last_code": result.code,
            "confirmations": result.confirmations,
            "checked_at": datetime.now(timezone.utc),
        }},
    )


def _verification_snapshot(network: str, tx_hash: str, result: ChainResult) -> dict[str, Any]:
    return {
        "network": network,
        "submitted_hash": tx_hash,
        "state": result.state,
        "code": result.code,
        "confirmations": result.confirmations,
        "received_amount": str(result.received_amount) if result.received_amount is not None else None,
        "checked_at": datetime.now(timezone.utc),
    }


async def submit_transaction_hash(deposit_id: str, user_id: int, submitted_hash: str) -> dict[str, Any]:
    """Claim, verify, and safely credit a user-owned manual-chain deposit.

    The return value is deliberately safe for bot and Mini App presentation. It
    carries no provider payload and never exposes other users' claims.
    """
    from server.utils.database.walletdb import confirm_deposit, depositsdb, get_deposit

    deposit = await get_deposit(deposit_id)
    if not deposit or int(deposit.get("user_id") or 0) != int(user_id):
        raise LookupError("Deposit not found.")
    method = str(deposit.get("method") or "")
    network = network_for_method(method)
    status = str(deposit.get("status") or "pending")
    if status == "completed":
        return {"outcome": "completed", "deposit": deposit, "verification": {"state": "verified", "code": "already_completed"}}
    if status != "pending":
        raise ManualChainError(f"This deposit is {status} and cannot accept a transaction hash.")
    expires_at = deposit.get("expires_at")
    if expires_at and _utc(expires_at) < datetime.now(timezone.utc):
        await depositsdb.update_one(
            {"deposit_id": deposit_id, "user_id": int(user_id), "status": "pending"},
            {"$set": {"status": "expired", "note": "Payment instruction expired"}},
        )
        raise ManualChainError("This payment instruction has expired. Create a new deposit before paying.")

    tx_hash = normalize_transaction_hash(network, submitted_hash)
    claimed, same_deposit = await _claim_hash(network, tx_hash, deposit_id, int(user_id))
    if not claimed:
        raise ManualChainError("This transaction hash has already been submitted for another deposit.")

    result = await verify_manual_transaction(
        method,
        tx_hash,
        recipient=str(deposit.get("address") or "").strip(),
        expected_amount=deposit.get("amount"),
        created_at=deposit.get("created_at"),
    )
    await _record_claim_result(network, tx_hash, result, deposit_id)
    snapshot = _verification_snapshot(network, tx_hash, result)

    if result.state == "unavailable":
        await depositsdb.update_one(
            {"deposit_id": deposit_id, "user_id": int(user_id), "status": "pending"},
            {"$set": {"extra.manual_chain_verification": snapshot}},
        )
        return {"outcome": "pending", "deposit": await get_deposit(deposit_id) or deposit, "verification": result.public()}

    if result.state in {"pending", "rejected"}:
        # A rejected submitted hash stays globally reserved, but the payment
        # instruction remains pending so the owner may submit a different hash.
        await depositsdb.update_one(
            {"deposit_id": deposit_id, "user_id": int(user_id), "status": "pending"},
            {"$set": {
                "submitted_tx_hash": tx_hash,
                "extra.manual_chain_verification": snapshot,
            }},
        )
        return {"outcome": result.state, "deposit": await get_deposit(deposit_id) or deposit, "verification": result.public()}

    # Store verified evidence before the balance mutation. If the process dies
    # after this write, the same owner can retry and confirm_deposit remains
    # idempotent through its existing durable crediting claim.
    await depositsdb.update_one(
        {"deposit_id": deposit_id, "user_id": int(user_id), "status": "pending"},
        {"$set": {
            "tx_hash": tx_hash,
            "submitted_tx_hash": tx_hash,
            "extra.tx_hash": tx_hash,
            "extra.manual_chain_verification": snapshot,
        }},
    )
    try:
        credited = await confirm_deposit(deposit_id, confirmed_by=None)
    except Exception:
        _log.exception("manual_chain: credit finalization failed for deposit=%s hash=%s", deposit_id, _display_hash(tx_hash))
        raise

    final_deposit = await get_deposit(deposit_id) or deposit
    if credited or final_deposit.get("status") == "completed":
        await _record_claim_result(network, tx_hash, ChainResult("verified", "completed", result.confirmations, result.received_amount), deposit_id)
        return {"outcome": "completed", "deposit": final_deposit, "verification": result.public(), "same_submission": same_deposit}
    return {"outcome": "pending", "deposit": final_deposit, "verification": result.public(), "same_submission": same_deposit}
