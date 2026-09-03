# TgFoxApi API Documentation

## Authentication

Public API routes under `/api/v1/*` require `X-Api-Key: tg_<user_id>_<secret>`. The Mini App routes under `/webapp/api/*` resolve a user through signed Telegram `initData` or the current direct API-key fallback. API keys are bearer credentials and must never be placed in URLs, logs, or shared messages.

## Health and Discovery

| Method | Path | Purpose | Authentication |
|---|---|---|---|
| GET | `/` | Service/API discovery metadata | None |
| GET | `/ping` | Liveness response | None |
| GET | `/health` | Basic health response | None |
| GET | `/docs` | OpenAPI UI | None |
| GET | `/redoc` | ReDoc UI | None |

## Public API v1

| Area | Methods and paths |
|---|---|
| Auth | `POST /api/v1/auth/validate-key` |
| User | `GET /api/v1/user/me`, `/balance`, `/me/rank`, `/me/referral`, `/orders`, `/orders/{order_id}`, `DELETE /orders/{order_id}`, `/transactions`, `/sessions/bought`, `/sessions/sold`; `POST /me/api-key/regenerate`; `PATCH /me/language`, `/me/referral-notifications` |
| Wallet | `GET /api/v1/wallet/`, `/balance`, `/address`, `/withdrawals`, `/withdrawals/{withdrawal_id}`, `/transactions`; `POST /address`, `/withdraw`, `/withdrawals/{withdrawal_id}/verify` |
| Deposits | `GET /api/v1/wallet/deposit/methods`, `/history`, `/deposit/{deposit_id}`, `/deposit/{deposit_id}/status`; `POST /api/v1/wallet/deposit` |
| Countries | `GET /api/v1/countries`, `/api/v1/countries/{code}` |
| Orders | `POST /api/v1/orders`, `GET /api/v1/orders/{order_id}/otp` |
| Provider callbacks | `POST /webhooks/deposit`, `/webhooks/withdrawal`, `/webhooks/binance_pay` |

All user-owned resource routes must query by the resolved canonical user ID, never an ID supplied in request body or query string.

## Mini App API

All paths below are prefixed `/webapp/api`.

| Area | Methods and paths |
|---|---|
| Profile and activity | `GET /me`, `/record` |
| Marketplace | `GET /countries`, `POST /buy`, `GET /order/{order_id}/otp` |
| Wallet and funding | `GET /wallet`, `POST /deposit`, `GET /deposits`, `GET /deposit/{deposit_id}`, `GET /transactions` |
| Withdrawal | `POST /withdraw/address`, `POST /withdraw`, `GET /withdrawals` |
| Account tools | `GET /referral`, `/support`, `/sell-requests` |
| Themes | `GET /themes`, `POST /theme/select`, `/theme/purchase` |

Legacy `/webapp/`, `/webapp/buy`, `/webapp/record`, and other non-API historic WebApp paths redirect to `/app/`. Invalid `/webapp/api/*` paths should remain API errors rather than browser redirects.

## Admin API

The internal admin panel contains 126 route decorators across modules for authentication, analytics, backups, BIN, countries, dashboard, logs, orders, payments, proxies, sales feeds, sellers, sessions, settings, tasks, user seller stock, and users. These routes are intentionally hidden from public OpenAPI and must retain the `require_session` dependency.

## Response Behavior

FastAPI exception middleware returns a structured error envelope for HTTP and validation errors. A request ID is attached to responses through `X-Request-Id`; callers should provide or retain it only for support correlation, never as authorization.

## Pagination and Financial Safety

List endpoints expose page/limit-style pagination where implemented. Financial operations require server-side amount validation, owner resolution, provider availability checks, and idempotent callback handling. Clients must not submit trusted prices, balances, or user IDs.
