"""Adversarial tests for manual BEP20/TRC20 transaction-hash verification."""

from __future__ import annotations

import asyncio
import copy
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def _safe_env(monkeypatch):
    values = {
        "API_ID": "1",
        "API_HASH": "test-api-hash",
        "BOT_TOKEN": "123456:TEST",
        "MONGO_DB_URI": "mongodb://127.0.0.1:27017",
        "SESSION_SECRET": "test-session-secret",
        "OWNER_ID": "1",
        "ADMIN_PASSWORD": "test-admin-password",
        "BSCSCAN_API_KEY": "test-bscscan-key",
        "DEPOSIT_BEP20_MIN_CONFIRMATIONS": "12",
        "DEPOSIT_TRC20_MIN_CONFIRMATIONS": "19",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _bep20_transfer_log(recipient: str, amount: Decimal, *, contract: str | None = None):
    from server.services.deposit import manual_chain

    raw_value = int(amount * (Decimal(10) ** 18))
    return {
        "address": contract or manual_chain._BEP20_USDT_CONTRACT,
        "topics": [
            manual_chain._TRANSFER_TOPIC,
            "0x" + "00" * 32,
            "0x" + "00" * 12 + recipient.removeprefix("0x"),
        ],
        "data": hex(raw_value),
        "removed": False,
    }


def test_manual_hash_normalization_rejects_bad_or_cross_network_formats():
    from server.services.deposit.manual_chain import ManualChainError, normalize_transaction_hash

    assert normalize_transaction_hash("BEP20", "0x" + "Ab" * 32) == "0x" + "ab" * 32
    assert normalize_transaction_hash("TRC20", "0X" + "Cd" * 32) == "cd" * 32
    with pytest.raises(ManualChainError):
        normalize_transaction_hash("BEP20", "ab" * 32)
    with pytest.raises(ManualChainError):
        normalize_transaction_hash("TRC20", "not-a-transaction-id")


def test_bep20_verification_requires_successful_usdt_transfer_to_instruction_address(monkeypatch):
    from server.services.deposit import manual_chain

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipient = "0x" + "ab" * 20

    async def fake_get_json(_url, *, params, headers=None):
        action = params["action"]
        if action == "eth_getTransactionReceipt":
            return 200, {"result": {
                "status": "0x1",
                "blockNumber": "0x64",
                "logs": [_bep20_transfer_log(recipient, Decimal("15.25"))],
            }}
        if action == "eth_blockNumber":
            return 200, {"result": "0x70"}
        if action == "eth_getBlockByNumber":
            return 200, {"result": {"timestamp": hex(int((created + timedelta(seconds=1)).timestamp()))}}
        raise AssertionError(action)

    monkeypatch.setattr(manual_chain, "_get_json", fake_get_json)
    result = run(manual_chain._bep20_result("0x" + "1" * 64, recipient=recipient, expected_amount=Decimal("15.25"), created_at=created))

    assert result.state == "verified"
    assert result.confirmations == 13
    assert result.received_amount == Decimal("15.25")


def test_bep20_verification_rejects_wrong_token_and_waits_for_finality(monkeypatch):
    from server.services.deposit import manual_chain

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipient = "0x" + "cd" * 20

    async def wrong_token(_url, *, params, headers=None):
        action = params["action"]
        if action == "eth_getTransactionReceipt":
            return 200, {"result": {
                "status": "0x1",
                "blockNumber": "0x64",
                "logs": [_bep20_transfer_log(recipient, Decimal("10"), contract="0x" + "ef" * 20)],
            }}
        if action == "eth_blockNumber":
            return 200, {"result": "0x70"}
        return 200, {"result": {"timestamp": hex(int((created + timedelta(seconds=1)).timestamp()))}}

    monkeypatch.setattr(manual_chain, "_get_json", wrong_token)
    rejected = run(manual_chain._bep20_result("0x" + "2" * 64, recipient=recipient, expected_amount=Decimal("10"), created_at=created))
    assert (rejected.state, rejected.code) == ("rejected", "transfer_details_mismatch")

    async def one_confirmation(_url, *, params, headers=None):
        action = params["action"]
        if action == "eth_getTransactionReceipt":
            return 200, {"result": {
                "status": "0x1",
                "blockNumber": "0x64",
                "logs": [_bep20_transfer_log(recipient, Decimal("10"))],
            }}
        if action == "eth_blockNumber":
            return 200, {"result": "0x64"}
        return 200, {"result": {"timestamp": hex(int((created + timedelta(seconds=1)).timestamp()))}}

    monkeypatch.setattr(manual_chain, "_get_json", one_confirmation)
    pending = run(manual_chain._bep20_result("0x" + "3" * 64, recipient=recipient, expected_amount=Decimal("10"), created_at=created))
    assert (pending.state, pending.code, pending.confirmations) == ("pending", "awaiting_confirmations", 1)


def test_bep20_provider_timeout_is_retryable_not_not_found(monkeypatch):
    from server.services.deposit import manual_chain

    async def timed_out(_url, *, params, headers=None):
        return 0, None

    monkeypatch.setattr(manual_chain, "_get_json", timed_out)
    result = run(manual_chain._bep20_result(
        "0x" + "4" * 64,
        recipient="0x" + "ab" * 20,
        expected_amount=Decimal("10"),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    assert (result.state, result.code) == ("unavailable", "provider_unavailable")


def test_trc20_verification_requires_correct_recipient_contract_amount_and_status(monkeypatch):
    from server.services.deposit import manual_chain

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recipient = "TRecipientAddress1111111111111111111"

    async def valid_transaction(_url, *, params, headers=None):
        return 200, {
            "hash": "a" * 64,
            "revert": False,
            "contractRet": "SUCCESS",
            "confirmed": True,
            "confirmations": 19,
            "timestamp": int((created + timedelta(seconds=1)).timestamp() * 1000),
            "trc20TransferInfo": [{
                "contract_address": manual_chain._TRC20_USDT_CONTRACT,
                "to_address": recipient,
                "tokenType": "trc20",
                "type": "Transfer",
                "status": 0,
                "decimals": 6,
                "amount_str": "12500000",
            }],
        }

    monkeypatch.setattr(manual_chain, "_get_json", valid_transaction)
    verified = run(manual_chain._trc20_result("a" * 64, recipient=recipient, expected_amount=Decimal("12.5"), created_at=created))
    assert (verified.state, verified.confirmations, verified.received_amount) == ("verified", 19, Decimal("12.5"))

    async def wrong_recipient(_url, *, params, headers=None):
        payload = (await valid_transaction(_url, params=params, headers=headers))[1]
        payload["trc20TransferInfo"][0]["to_address"] = "TOtherRecipient111111111111111111111"
        return 200, payload

    monkeypatch.setattr(manual_chain, "_get_json", wrong_recipient)
    rejected = run(manual_chain._trc20_result("b" * 64, recipient=recipient, expected_amount=Decimal("12.5"), created_at=created))
    assert (rejected.state, rejected.code) == ("rejected", "transfer_details_mismatch")


def test_hash_claim_is_global_one_use_but_idempotent_for_same_deposit(monkeypatch):
    from server.services.deposit import manual_chain

    class FakeClaims:
        def __init__(self):
            self.docs = {}

        async def insert_one(self, doc):
            if doc["_id"] in self.docs:
                raise RuntimeError("E11000 duplicate key")
            self.docs[doc["_id"]] = copy.deepcopy(doc)

        async def find_one(self, query):
            return copy.deepcopy(self.docs.get(query["_id"]))

    claims = FakeClaims()
    monkeypatch.setattr(manual_chain, "_claims_collection", lambda: claims)
    tx_hash = "0x" + "d" * 64

    assert run(manual_chain._claim_hash("BEP20", tx_hash, "DEP-A", 7)) == (True, False)
    assert run(manual_chain._claim_hash("BEP20", tx_hash, "DEP-B", 8)) == (False, False)
    assert run(manual_chain._claim_hash("BEP20", tx_hash, "DEP-A", 7)) == (True, True)


def test_verified_hash_retry_cannot_credit_same_deposit_twice(monkeypatch):
    from server.services.deposit import manual_chain
    from server.utils.database import walletdb

    deposit = {
        "deposit_id": "DEP-ONCE",
        "user_id": 77,
        "method": "bep20_scan",
        "amount": 10.0,
        "address": "0x" + "aa" * 20,
        "status": "pending",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "extra": {},
    }
    calls = {"credit": 0}

    class FakeDeposits:
        async def update_one(self, query, update):
            if query.get("deposit_id") != deposit["deposit_id"]:
                return None
            for key, value in update.get("$set", {}).items():
                target = deposit
                parts = key.split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = value
            return None

    async def fake_get_deposit(deposit_id):
        return copy.deepcopy(deposit) if deposit_id == deposit["deposit_id"] else None

    async def fake_claim(*_args):
        return True, False

    async def fake_verify(*_args, **_kwargs):
        return manual_chain.ChainResult("verified", "verified", 16, Decimal("10"))

    async def fake_record(*_args, **_kwargs):
        return None

    async def fake_confirm(deposit_id, confirmed_by=None):
        assert deposit_id == deposit["deposit_id"]
        if deposit["status"] == "completed":
            return None
        calls["credit"] += 1
        deposit["status"] = "completed"
        deposit["balance_credited"] = True
        return copy.deepcopy(deposit)

    monkeypatch.setattr(walletdb, "depositsdb", FakeDeposits())
    monkeypatch.setattr(walletdb, "get_deposit", fake_get_deposit)
    monkeypatch.setattr(walletdb, "confirm_deposit", fake_confirm)
    monkeypatch.setattr(manual_chain, "_claim_hash", fake_claim)
    monkeypatch.setattr(manual_chain, "verify_manual_transaction", fake_verify)
    monkeypatch.setattr(manual_chain, "_record_claim_result", fake_record)

    tx_hash = "0x" + "e" * 64
    first = run(manual_chain.submit_transaction_hash("DEP-ONCE", 77, tx_hash))
    second = run(manual_chain.submit_transaction_hash("DEP-ONCE", 77, tx_hash))

    assert first["outcome"] == second["outcome"] == "completed"
    assert calls["credit"] == 1


def test_bot_miniapp_and_owner_scoped_api_use_manual_hash_contract():
    bot_source = (ROOT / "server/plugins/bot/wallet.py").read_text()
    route_source = (ROOT / "server/webapp/routes.py").read_text()
    api_source = (ROOT / "miniapp/client/src/lib/api.ts").read_text()
    ui_source = (ROOT / "miniapp/client/src/components/DepositWorkspace.tsx").read_text()
    walletdb_source = (ROOT / "server/utils/database/walletdb.py").read_text()

    assert '"step": "deposit_chain_hash"' in bot_source
    assert "submit_transaction_hash(dep_id, uid, text)" in bot_source
    assert "verify_transaction(address, amount, after=created_at)" not in bot_source
    assert '@router.post("/api/deposit/{deposit_id}/transaction-hash")' in route_source
    assert "submit_transaction_hash(deposit_id, int(user[\"id\"]), tx_hash)" in route_source
    assert "Depends(require_webapp_user)" in route_source
    assert "submitDepositTransactionHash" in api_source
    assert "transaction_hash: transactionHash" in api_source
    assert "Submit your {network} transaction hash" in ui_source
    assert "Verify on-chain" in ui_source
    assert "confirm_deposit before: deposit_id=%s status=%s balance_credited=%s document=%r" not in walletdb_source


def test_method_first_deposit_pages_hide_oxapay_network_picker_and_verify_binance_order_ids():
    route_source = (ROOT / "server/webapp/routes.py").read_text()
    api_source = (ROOT / "miniapp/client/src/lib/api.ts").read_text()
    ui_source = (ROOT / "miniapp/client/src/components/DepositWorkspace.tsx").read_text()

    assert 'if provider.method_id == "oxapay":' in route_source
    assert '@router.post("/api/deposit/{deposit_id}/binance-order")' in route_source
    assert "submitBinanceOrderId" in api_source
    assert 'body: JSON.stringify({ order_id: orderId })' in api_source
    assert 'type Screen = "methods" | "method";' in ui_source
    assert "Choose a payment method" in ui_source
    assert "No network selector here." in ui_source
    assert "OxaPay shows its available networks only inside the secure hosted checkout." in ui_source
    assert "tgfoxApi.createDeposit({ method: method.id, amount: value })" in ui_source
