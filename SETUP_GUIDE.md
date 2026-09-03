# TgFoxApi Setup Guide

## Prerequisites

The production stack requires Python 3.12, Node.js with Corepack/pnpm for the Mini App build, MongoDB access, a Telegram bot token and app credentials, and any payment-provider credentials required by enabled payment methods.

> Never paste production values into documentation, Git commits, screenshots, or chat. Keep secrets in the host’s protected environment-variable store.

## Local Development

Create a private environment file from the required variables listed in `ENV_GUIDE.md`. Install Python dependencies, then build the Mini App.

```bash
pip install -r requirements.txt
bash build_miniapp.sh
python3 -m server
```

The single runtime command starts the Pyrogram bot and FastAPI in the same asyncio process. The compiled Mini App is served from `/app/`.

## Native Render Deployment

Use the Python runtime and leave **Root Directory** blank.

| Render field | Required value |
|---|---|
| Build Command | `bash build_miniapp.sh && pip install -r requirements.txt` |
| Start Command | `python3 -m server` |
| Auto Deploy | `On Commit` |
| `WEBAPP_BASE_URL` | Public service origin, for example `https://your-service.onrender.com` |

Do not append `/app/` to `WEBAPP_BASE_URL`. The application derives and registers the final Telegram menu URL as `<origin>/app/`.

After changing the build command, use **Clear build cache & deploy latest commit** once. Verify `/health`, `/app/`, and startup logs before asking users to open the Mini App.

## Docker Alternative

The repository contains a multi-stage Dockerfile, but native Render deployment is the recommended current path. If Docker is selected, it must build the Mini App artifact and copy `miniapp/dist/public` into the Python runtime image before `python3 -m server` starts.

## Safe Verification Checklist

| Check | Expected outcome |
|---|---|
| `pytest -q` | All regression tests pass |
| `python3 -m pip check` | No broken packages |
| `cd miniapp && pnpm check && pnpm build` | Type and production build pass |
| `GET /health` | Health response |
| `GET /app/` | Mini App shell loads |
| Bot startup | Bot identity, session channel check, and Mini App menu registration are logged without secrets |

## Operational Constraints

Do not delete MongoDB data during setup. Do not enable a payment provider until its callback URL, webhook verification, and sandbox/production mode are deliberately reviewed. Do not enable synthetic sales or fake stock modes; they conflict with the current owner policy requiring real data only.
