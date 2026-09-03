# TgFoxApi Environment Guide

This guide documents variable purpose without exposing values. Required secrets must be configured in the host environment, not committed to source control.

## Required Startup Variables

| Variable | Purpose | Sensitivity |
|---|---|---|
| `API_ID` | Telegram application numeric ID | Sensitive configuration |
| `API_HASH` | Telegram application secret | Secret |
| `BOT_TOKEN` | Telegram bot authentication and Mini App HMAC key | Secret |
| `MONGO_DB_URI` | MongoDB connection string | Secret |
| `SESSION_SECRET` | Session/2FA encryption material | Secret |
| `OWNER_ID` | Telegram numeric owner ID | Sensitive configuration |
| `ADMIN_PASSWORD` | Admin panel bootstrap credential | Secret |

## Runtime and Public URLs

| Variable | Purpose |
|---|---|
| `PORT` / `API_PORT` | HTTP bind port; platform `PORT` has priority |
| `WEBAPP_BASE_URL` | Explicit public HTTPS service origin used to derive `/app/` |
| `RENDER_EXTERNAL_URL` | Render fallback public origin |
| `REPLIT_DEV_DOMAIN` | Replit deployment/development fallback origin |
| `ALLOWED_ORIGINS` | Comma-separated explicit browser origins; must be set in production |
| `BOT_USERNAME` | Bot username without `@`, used for links/deep links |

## Telegram and Support

| Variable | Purpose |
|---|---|
| `LOG_GROUP_ID` | Optional operational log group |
| `SESSION_CHANNEL_ID` | Telegram storage channel for account/session material |
| `SUPPORT_CHANNEL` | Support channel URL |
| `SUPPORT_GROUP` | Support group URL |
| `UPDATES_CHANNEL` | Updates channel URL |
| `SUPPORT_USER_ID` | Optional direct Telegram support contact ID |

## Payment and Withdrawal Controls

| Variable family | Purpose |
|---|---|
| `OXAPAY_*` | OxaPay payout/provider configuration, base URL, network aliases, sandbox mode |
| `BINANCE_*` | Binance account/Pay verification credentials, UID, optional proxy |
| `DEPOSIT_WEBHOOK_URL` | Public deposit callback URL |
| `WITHDRAWAL_CALLBACK_URL` | Public withdrawal callback URL |
| `BINANCE_PAY_WEBHOOK_URL` | Public Binance Pay callback URL |
| `WITHDRAWAL_*` | Amount limits, fees, gateway mode, verification interval, expiry |

## Database and Worker Tuning

| Variable | Purpose |
|---|---|
| `MONGO_MAX_POOL_SIZE` | Motor maximum MongoDB connection pool size |
| `MONGO_MIN_POOL_SIZE` | Motor warm connection pool size |
| Feature/provider settings | Stored server settings determine enabled payment, feed, inventory, and worker behavior |

## Security Rules

> A secret shared in chat should be treated as exposed and rotated through its issuing provider. Do not echo or reuse it in source code.

The `custom_env` MongoDB feature can alter environment values at startup. Treat access to its admin controls as equivalent to privileged deployment access. A future hardening phase should restrict keys to a reviewed allowlist and store confidential values using suitable encryption and audit controls.
