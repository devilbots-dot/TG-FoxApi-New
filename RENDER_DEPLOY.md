# TgFox Render Deployment Guide

This guide deploys the **Python bot, FastAPI API, and Telegram Mini App from one Render web service**. It does **not** require Docker for the normal deployment. Render runs one build command before deployment, then starts the complete service with only `python3 -m server`.[1]

> **Do not paste secrets, bot tokens, MongoDB URLs, payment keys, or the Render deploy-hook URL into GitHub, screenshots, or chat.** Keep existing secret values in Render's Environment page.

## 1. Open the Correct Render Settings Screen

In Render, open the **Tg-FoxApi4** web service and select **Settings**. The fields shown in the settings screen must use the following exact values.

| Render field | What to enter | Why |
|---|---|---|
| **Root Directory** | Leave **blank** | The repository root contains `build_miniapp.sh`, `requirements.txt`, and `server/`. |
| **Build Command** | `bash build_miniapp.sh && pip install -r requirements.txt` | Runs once per deploy. It builds `miniapp/dist/public`, then installs Python packages. |
| **Pre-Deploy Command** | Leave **blank** | Do not run database migrations or duplicate build steps here. |
| **Start Command** | `python3 -m server` | This is the only runtime command. It starts the bot, FastAPI, and serves the compiled Mini App at `/app/`. |
| **Auto-Deploy** | `On Commit` | New commits to the selected branch automatically deploy. |
| **Build Filters** | Leave **blank** | Changes anywhere in this repository should be able to trigger a deploy. |
| **Git Credentials** | Do not change | This is already the connected repository identity. |
| **Deploy Hook** | Do not share or modify | Treat this URL as a secret. It is not needed for ordinary GitHub auto-deploys. |

> The **Build Command** and **Start Command** are different by design. React/TypeScript must be compiled once before the server starts, but the running service still uses only `python3 -m server`.[1]

## 2. Confirm the Service Runtime

Use the **Python** native runtime for this existing service. Do not select Docker for this deployment path. Render's native runtime includes Node.js and pnpm, so `build_miniapp.sh` can compile the Mini App before Python starts.[2]

## 3. Environment Variables

Open **Environment** in the Render side menu. Keep the existing bot, database, encryption, and payment settings unchanged. Add or verify the following public URL variable.

| Variable | Exact value | Notes |
|---|---|---|
| `WEBAPP_BASE_URL` | `https://tg-foxapi4.onrender.com` | Do **not** add `/app/` here. The server adds `/app/` automatically. |
| `PORT` | Do not set manually | Render provides it automatically. TgFox reads it first. |

If the associated payment providers are enabled, verify these existing callback values use the same public domain:

| Optional callback variable | Expected URL |
|---|---|
| `DEPOSIT_WEBHOOK_URL` | `https://tg-foxapi4.onrender.com/webhooks/deposit` |
| `WITHDRAWAL_CALLBACK_URL` | `https://tg-foxapi4.onrender.com/webhooks/withdrawal` |
| `BINANCE_PAY_WEBHOOK_URL` | `https://tg-foxapi4.onrender.com/webhooks/binance_pay` |

These callback URLs are for payment-provider notifications. They are separate from the Telegram Mini App URL.

## 4. Deploy the Latest Code

After saving the settings, open **Manual Deploy** and select **Clear build cache & deploy latest commit**. This is important after changing the Build Command because the previous deployment did not generate the Mini App output.

The selected Git branch must be `main`. The deployment should execute the build command first, produce `miniapp/dist/public/index.html`, and only then run `python3 -m server`.[1]

## 5. What Success Looks Like

The logs should show Mini App build output before the Python server startup. After startup, the relevant bot log is:

```text
Telegram Mini App menu configured: https://tg-foxapi4.onrender.com/app/
```

Then test this address in a browser:

```text
https://tg-foxapi4.onrender.com/app/
```

It must no longer return:

```json
{"detail":"Mini App build is unavailable. Run the deployment build step."}
```

For full wallet, order, OTP, and account data, open the Mini App from the Telegram bot because Telegram supplies the signed user identity. After the new deployment is live, send a **new** `/start` message and press **Open TgFox App**. Old chat messages can retain old Web App button URLs, so do not use those historical buttons.

## 6. Quick Recovery Table

| Problem | Meaning | Exact fix |
|---|---|---|
| `Mini App build is unavailable` at `/app/` | The pre-deploy build did not create `miniapp/dist/public`. | Recheck the exact Build Command, save it, then use **Clear build cache & deploy latest commit**. |
| `node` or `pnpm` error in Build Logs | Native build toolchain did not run as expected. | Keep runtime as Python, leave Root Directory blank, and share only the non-secret Build Log error for investigation. |
| Dark legacy dashboard opens in Telegram | An old historic inline button is being opened. | Send a new `/start` after the latest deploy and use **Open TgFox App** or the bot menu button. |
| `/app/` works but wallet data does not show in a normal browser | This is expected outside Telegram. | Open `/app/` from the bot; signed `initData` is required for protected user data. |

## 7. Deployment Contract

```text
Build once per deploy:
bash build_miniapp.sh && pip install -r requirements.txt

Run continuously after deploy:
python3 -m server

Public Mini App route:
https://tg-foxapi4.onrender.com/app/
```

## References

[1] [Render Web Services documentation](https://render.com/docs/web-services) describes separate build and start commands for native web services.

[2] [Render Native Runtimes documentation](https://render.com/docs/native-runtimes) lists Node.js and pnpm among the tools available in native runtimes.
