# Manual USDT Transaction-Hash Verification Reference

This reference documents the external data required by the BEP20 and TRC20 manual-deposit verifier. It intentionally contains no credentials, wallet addresses, transaction hashes, or user data.

| Network | Authoritative transaction data required | Acceptance rule |
| --- | --- | --- |
| **BEP20 / BNB Smart Chain** | An Etherscan API V2 receipt request for `chainid=56`, `module=proxy`, `action=eth_getTransactionReceipt`, and the submitted transaction hash. | The receipt must be mined, have `status == 0x1`, and contain an ERC-20 `Transfer` log from the official BSC USDT contract whose recipient topic is the configured deposit address and whose raw amount meets the requested deposit amount. The block must meet the configured finality threshold. |
| **TRC20 / TRON** | A TronScan transaction-info request by submitted transaction hash. | The transaction must be confirmed, non-reverted, report `contractRet == SUCCESS`, and include a successful `trc20TransferInfo` item for the official TRON USDT contract whose receiver is the configured deposit address and whose raw amount meets the requested deposit amount. The confirmation count must meet the configured finality threshold. |

The provider must treat a missing transaction, temporary upstream error, or insufficient confirmations as a **retryable pending** state. A transfer with the wrong network, token contract, recipient, amount, or execution result is **rejected for that deposit** and must never credit the wallet.

> A receipt execution status alone proves only that an EVM transaction executed. The verifier must inspect the ERC-20 `Transfer` event log to prove that the configured USDT token transferred the required value to the platform address.

## Durable State Machine

The implementation uses a dedicated `deposit_transaction_hashes` collection. Its `_id` is the normalized `network:transaction-hash`, which MongoDB uniquely protects by default. This deliberately avoids a migration or a new unique index over legacy deposit records.

| Step | Durable action | Result |
| --- | --- | --- |
| 1. Authenticate and locate | The API or bot resolves the canonical user, then loads only that user's eligible `pending` manual-chain deposit. | Another user cannot inspect, submit for, or credit the deposit. |
| 2. Validate and claim | The service normalizes strict BSC/TRON hash syntax and inserts the network-qualified claim. Replaying the same hash is accepted only by the same deposit and user; any other deposit receives a conflict. | A transaction hash is globally one-use across manual deposits, including concurrent submissions. |
| 3. Verify from chain | The provider retrieves the real transaction by hash and verifies finality, execution status, configured recipient, official USDT contract, and the exact required raw token value. | A claimed identifier is not payment proof by itself. |
| 4. Record outcome | Retryable states retain the same claim for safe retry. A final mismatch records a failed deposit and never credits it. A valid transfer stores verification metadata before crediting. | A user may safely retry while a submitted transaction is still propagating or awaiting confirmations. |
| 5. Credit exactly once | `confirm_deposit()` retains the existing atomic `balance_credited="crediting"` claim and calls `userdb.record_deposit(..., deposit_id=...)`, which has the atomic credited-deposit-ID guard. | A process crash, duplicate callback, bot/Mini App race, or retry cannot issue a second balance credit. |

The hash claim is intentionally not released after a transient error or mismatch. A real on-chain transaction must never become available to a different deposit after it has been submitted once; the original user may retry the same pending claim without resending it.

The verifier uses configurable finality thresholds, with conservative defaults of `12` BSC blocks and `19` TRON confirmations. Operators may raise these values through `DEPOSIT_BEP20_MIN_CONFIRMATIONS` and `DEPOSIT_TRC20_MIN_CONFIRMATIONS` without code changes.

## Sources

1. [Etherscan API V2 — `eth_getTransactionReceipt`](https://docs.etherscan.io/api-reference/endpoint/ethgettransactionreceipt): the endpoint requires `chainid`, `module=proxy`, `action=eth_getTransactionReceipt`, and `txhash`; its receipt response includes `status` and event `logs`.
2. [Etherscan API V2 supported chains](https://docs.etherscan.io/supported-chains): BNB Smart Chain uses chain ID `56`.
3. [TronScan API — Get Transaction Details by Hash](https://docs.tronscan.org/en/api/transactions-and-transfers/transaction-info): responses include `confirmed`, `revert`, `confirmations`, `contractRet`, and `trc20TransferInfo` fields including sender, receiver, contract, raw amount, decimals, and transfer status.
4. [TronScan API Keys](https://docs.tronscan.org/en/api/api-keys): requests use the `TRON-PRO-API-KEY` request header; the provider uses this documented header with the existing `TRONSCAN_API_KEY` configuration when it is available.
