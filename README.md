# TG Account Market

A production Telegram account buying & selling platform combining a Pyrogram bot with a FastAPI REST API and MongoDB.

## Architecture

- **Bot**: Pyrogram (Telegram bot) — handles BUY/SELL flows, wallet, OTP delivery, admin commands
- **API**: FastAPI (uvicorn on port 5000 on Replit, configurable via `PORT`/`API_PORT`) — public REST API with X-Api-Key auth
- **Database**: MongoDB (Motor async driver) — users, orders, sessions, wallet, deposits, withdrawals
- **OTP**: Telethon — connects to `.session` files to intercept Telegram login codes
- **Payments**: OxaPay (webhook auto-confirm) + manual deposit approval

## How to Run

The bot and API start together with:
```
python3 -m server
```

Configured as the **"Start application"** workflow.

## Required Secrets (all set in Replit Secrets)

| Secret | Description |
|---|---|
| `API_ID` | Telegram API ID from https://my.telegram.org/apps |
| `API_HASH` | Telegram API Hash from https://my.telegram.org/apps |
| `BOT_TOKEN` | Bot token from @BotFather |
| `MONGO_DB_URI` | MongoDB Atlas connection string |
| `OWNER_ID` | Admin's Telegram user ID |
| `ADMIN_PASSWORD` | Password for the admin web panel (`/adminlogin`) |
| `SESSION_CHANNEL_ID` | Telegram channel ID where session files are stored |
| `SESSION_SECRET` | Fernet key for encrypting 2FA passwords (already set) |

## Optional Secrets

| Secret | Description |
|---|---|
| `LOG_GROUP_ID` | Telegram group/channel for bot startup logs |
| `ADMIN_SECRET_PATH` | Hidden query param to gate the admin login page |

## Key API Endpoints

- `GET /docs` — Interactive Swagger UI
- `GET /api/v1/user/me` — Your profile
- `GET /api/v1/wallet` — Wallet overview
- `POST /api/v1/wallet/deposit` — Create deposit
- `GET /api/v1/countries` — Browse available countries
- `POST /api/v1/orders` — Buy a session account
- `GET /api/v1/orders/{order_id}/otp` — Poll for OTP
- `GET /adminlogin` — Admin panel

## User Preferences

- Keep existing project structure — no restructuring
