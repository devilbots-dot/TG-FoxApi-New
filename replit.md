# TG Account Market

A production Telegram account buying & selling platform combining a Pyrogram bot with a FastAPI REST API and MongoDB.

## Architecture

- **Bot**: Pyrogram/Pyrofork (Telegram bot) — handles BUY/SELL flows, wallet, OTP delivery, admin commands
- **API**: FastAPI (uvicorn on port 5000) — public REST API with X-Api-Key auth
- **Database**: MongoDB (Motor async driver) — users, orders, sessions, wallet, deposits, withdrawals
- **OTP**: Telethon — connects to `.session` files to intercept Telegram login codes
- **Payments**: OxaPay (webhook auto-confirm) + manual deposit approval

## How to Run

```
python3 -m server
```

Configured as the **"Start application"** workflow (port 5000).

## Key URLs

- `/docs` — Interactive Swagger UI
- `/adminlogin` — Admin panel

## Required Secrets (set in Replit Secrets)

| Secret | Description |
|---|---|
| `API_ID` | Telegram API ID from https://my.telegram.org/apps |
| `API_HASH` | Telegram API Hash from https://my.telegram.org/apps |
| `BOT_TOKEN` | Bot token from @BotFather |
| `MONGO_DB_URI` | MongoDB Atlas connection string |
| `OWNER_ID` | Admin's Telegram user ID |
| `ADMIN_PASSWORD` | Password for the admin web panel (`/adminlogin`) |
| `SESSION_CHANNEL_ID` | Telegram channel ID where session files are stored |
| `SESSION_SECRET` | Fernet key for encrypting 2FA passwords |

## Deposit Methods

Five methods supported. A method only appears in the bot when its secret is set.

| Method | Secret(s) needed | Confirmation |
|---|---|---|
| **OxaPay** (crypto via gateway) | `OXAPAY_MERCHANT_KEY` ✅ set | Automatic via webhook |
| **USDT TRC20** (direct to wallet) | `DEPOSIT_TRC20_ADDRESS` ✅ set | User taps "Verify Payment" → TronGrid scan |
| **USDT BEP20** (direct to wallet) | `DEPOSIT_BEP20_ADDRESS` | User taps "Verify Payment" → BSCScan |
| **Binance Pay** (UID-based) | `BINANCE_PAY_UID` | Admin confirms manually in `/admin/payments` |
| **Telegram Stars** | `BOT_USERNAME` | Automatic |

### OxaPay sandbox → production
OxaPay defaults to **sandbox mode** (test payments, no real money).  
To go live, add to Replit Secrets:
```
OXAPAY_SANDBOX = false
```

### Optional deposit secrets
| Secret | Description |
|---|---|
| `DEPOSIT_SCAN_TTL` | TRC20/BEP20/Binance deposit lifetime in minutes (default: 60) |
| `TRONGRID_API_KEY` | TronGrid API key for higher rate limits (free at trongrid.io) |
| `BSCSCAN_API_KEY` | BSCScan API key for higher rate limits (free at bscscan.com) |

## Optional Secrets

| Secret | Description |
|---|---|
| `LOG_GROUP_ID` | Telegram group/channel for bot startup logs |
| `ADMIN_SECRET_PATH` | Hidden query param to gate the admin login page |
| `OXAPAY_PAYOUT_API_KEY` | OxaPay payout API key (enables automatic withdrawals) |
| `WITHDRAWAL_CALLBACK_URL` | Public HTTPS URL for OxaPay webhooks (e.g. `https://yourapp.replit.app/webhooks/withdrawal`) |

## Withdrawal System (OxaPay)

The platform has a production-grade, multi-gateway withdrawal system with full lifecycle management.

### How it works

1. User requests withdrawal → **balance is reserved** (moved to `reserved_balance`), not immediately deducted
2. If `WITHDRAWAL_GATEWAY=auto` and OxaPay key is configured → payout submitted automatically
3. If `WITHDRAWAL_GATEWAY=manual` (or no key) → queued for admin approval at `/admin/payments`
4. OxaPay sends webhook callbacks to `/webhooks/withdrawal` (HMAC-SHA512 validated)
5. Background worker (`withdrawal_verification_worker`, every 5 min) polls in-flight withdrawals
6. On completion → `finalize_reserved_balance` (permanent deduct + `total_withdrawal` stat)
7. On failure/rejection → `release_reserved_balance` (funds returned to spendable balance)

### Withdrawal env vars (set in Replit Secrets or config.py)

| Env var | Default | Description |
|---|---|---|
| `OXAPAY_PAYOUT_API_KEY` | _(empty)_ | OxaPay payout key — enables auto mode |
| `OXAPAY_BASE_URL` | `https://api.oxapay.com/v1` | OxaPay API base |
| `WITHDRAWAL_CALLBACK_URL` | _(empty)_ | Webhook callback URL sent to OxaPay |
| `WITHDRAWAL_GATEWAY` | `auto` | `auto` (submit immediately) or `manual` (admin approval) |
| `WITHDRAWAL_MIN_AMOUNT` | `5.0` | Minimum withdrawal in USD |
| `WITHDRAWAL_MAX_AMOUNT` | `10000.0` | Maximum single withdrawal in USD |
| `WITHDRAWAL_DAILY_LIMIT` | `1000.0` | Per-user daily limit in USD |
| `WITHDRAWAL_MONTHLY_LIMIT` | `10000.0` | Per-user monthly limit in USD |
| `WITHDRAWAL_FEE_PERCENT` | `0.0` | Fee as percentage of amount |
| `WITHDRAWAL_FEE_FIXED` | `0.0` | Fixed fee in USD |
| `WITHDRAWAL_VERIFY_INTERVAL_S` | `300` | Background polling interval (seconds) |
| `WITHDRAWAL_EXPIRE_HOURS` | `72` | Hours before unresolved withdrawal expires |
| `OXAPAY_NETWORK_TRC20` | `TRX` | OxaPay network ID for TRC20 |
| `OXAPAY_NETWORK_BEP20` | `BSC` | OxaPay network ID for BEP20 |

### Key files

| File | Purpose |
|---|---|
| `server/services/withdrawal/` | Core service package |
| `server/services/withdrawal/service.py` | `WithdrawalService` orchestrator |
| `server/services/withdrawal/providers/oxapay.py` | OxaPay integration |
| `server/utils/database/withdrawaldb.py` | DB layer (withdrawals, logs, gateway records) |
| `server/utils/withdrawal_statuses.py` | `WithdrawalStatus` constants (import from here to avoid circular imports) |
| `server/api/routes/withdrawal_webhook.py` | `POST /webhooks/withdrawal` — HMAC-validated callbacks |
| `server/api/routes/wallet.py` | User-facing wallet REST API |
| `server/admin/routes/payments.py` | Admin payment management UI/API |

### MongoDB collections added

- `withdrawals` — withdrawal records
- `withdrawal_logs` — event log per withdrawal
- `gateway_requests` / `gateway_responses` — raw OxaPay payloads for audit

## User Preferences

- Keep existing project structure — no restructuring
