# TgFox Telegram Mini App

This Mini App is a part of the main **Tg-FoxApi** service. The deployment builds the React frontend from `miniapp/` and the same `python3 -m server` process serves it at `/app/`; there is no second frontend service, no separate user account, and no external frontend API host.

## Shared Telegram identity

Telegram opens the Mini App with signed `initData`. Every `/webapp/api/*` call sends that value in `X-Telegram-Init-Data`; the Python server validates Telegram's HMAC signature and uses the signed `user.id` to query the existing bot user's MongoDB record. The bot and Mini App therefore show the same balance, ranks, inventory purchases, orders, and account state.

The Mini App intentionally fails closed outside Telegram. It never uses preview balances, sample countries, synthetic purchases, or fake OTP data.

## Routes

| User-facing route | Purpose |
|---|---|
| `/app/` | React Telegram Mini App served by the Python API service |
| `/webapp/api/me` | Verified current bot-user profile and wallet state |
| `/webapp/api/countries` | Live country inventory and current pricing |
| `/webapp/api/buy` | Authenticated live purchase |
| `/webapp/api/order/{order_id}/otp` | Authenticated OTP state for the purchaser |
| `/webapp/api/record` | Current user's purchase and sale history |

## Deployment

Docker is optional and is not required for the normal deployment. For an existing Render Python service, use **Python** runtime with the following two separate commands:

```text
Build Command: bash build_miniapp.sh && pip install -r requirements.txt
Start Command: python3 -m server
```

The build command runs once during every deploy and produces `miniapp/dist/public`. The start command remains only `python3 -m server`; that single Python process serves the bot, API, and compiled Mini App at `/app/`. The included `render.yaml` expresses this native workflow. The one required public setting is `WEBAPP_BASE_URL`, for example `https://your-service.onrender.com`.

On every successful server startup, TgFox registers `${WEBAPP_BASE_URL}/app/` as the bot's Telegram Mini App menu button. On Render, `RENDER_EXTERNAL_URL` is also accepted as the automatic URL source when `WEBAPP_BASE_URL` is not explicitly set.

## Local validation

```bash
bash build_miniapp.sh
python3 -m server
```

Open the generated `/app/` URL from the TgFox Telegram bot. Browser-only visits correctly show the verification-required screen because there is no signed Telegram identity.
