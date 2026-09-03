# TgFoxApi Full Repository Analysis Report

**Author:** Manus AI
**Audit baseline:** 2026-08-25
**Scope:** Current working tree after backup; no database, payment, Telegram-account, or production secret was read or modified during this audit.

> **Audit outcome:** TgFoxApi is a substantial Python Telegram commerce platform combining a Pyrogram bot, FastAPI, MongoDB, Telethon-based session/OTP workflows, payment providers, an admin panel, and a React Telegram Mini App. The core design is viable, but production readiness is constrained by several security, integrity, compliance, and maintainability risks listed below.

## 1. Backup and Validation Baseline

| Item | Result |
|---|---|
| Non-destructive source backup | Created at `/home/ubuntu/backups/backup_2026-08-25_original/` |
| Backup archive entries | 366 |
| Archive integrity manifest | Present; SHA-256 recorded in `BACKUP_README.md` |
| Python regression suite | 18 tests passed; 2 third-party deprecation warnings |
| Python dependency health | `pip check` reported no broken requirements |
| Mini App TypeScript check | Passed |
| Mini App production build | Passed; initial JavaScript bundle is approximately 652 kB and triggers Vite’s chunk-size warning |

The backup excludes only regenerable VCS metadata, Python caches, frontend dependency/build directories, and operational logs. Its scope, checksum, and restore instructions are in the backup directory’s `BACKUP_README.md`.

## 2. System Purpose and Business Flow

TgFoxApi is a Telegram-centered marketplace and control system for account/session inventory. Users interact through a bot or the same-server Mini App to view wallet balances, deposit funds, buy inventory, receive delivery/OTP information, request withdrawals, monitor activity, and review seller/referral information. Administrators manage inventory, countries, payments, users, proxies, logs, and backups through protected admin routes.

```text
Telegram user
  │
  ├─ /start → canonical MongoDB users.user_id profile + API key
  │              │
  │              ├─ Bot callbacks: buy / sell / deposit / withdraw / profile
  │              └─ Mini App: /app/
  │                    │
  │                    ├─ Telegram initData (intended path)
  │                    └─ X-Api-Key (current fallback path)
  │                         │
FastAPI /webapp/api/* or /api/v1/*
  │
  ├─ MongoDB: users, orders, sessions, deposits, transactions, withdrawals
  ├─ Payment providers and callbacks
  ├─ Inventory reservation / purchase service
  └─ OTP listener → Telegram storage channel → Telethon → owner-only result
```

## 3. Architecture Inventory

| Layer | Primary locations | Responsibility |
|---|---|---|
| Process bootstrap | `server/__main__.py` | Mongo initialization, cache load, plugin import, FastAPI and bot startup, Mini App menu registration, workers, shutdown |
| Configuration | `config.py`, `server/utils/database/envdb.py` | Environment validation, deployment URLs, provider controls, optional dynamic environment values |
| Bot | `server/core/bot.py`, `server/plugins/bot/` | Telegram lifecycle, user commands, callback actions, account/wallet/seller flows |
| Public API | `server/core/api.py`, `server/api/routes/` | Versioned API, API-key authentication, wallet, purchase, deposit, withdrawal, webhooks |
| WebApp API | `server/webapp/` | Same-domain Mini App static serving and owner-scoped account operations |
| Admin panel | `server/admin/` | Session-authenticated internal control panel; 126 route decorators were inventoried |
| Services | `server/services/`, `server/stock/` | Market transaction orchestration, providers, session validation, sales feed, background jobs |
| Persistence | `server/core/mongo.py`, `server/utils/database/` | Motor client, lazy collections, indexes, document operations |
| Mini App | `miniapp/client/` | React/Vite UI, authenticated HTTP client, wallet/order/community views |

## 4. Authentication and Authorization Map

| Surface | Current identity source | Authorization model | Audit assessment |
|---|---|---|---|
| Telegram bot | Telegram sender ID | Bot handlers use canonical `users.user_id` and admin/sudo checks | Appropriate primary identity boundary |
| Public `/api/v1/*` | `X-Api-Key` | `require_api_key` resolves the stored key owner and checks banned status | Functional; key storage/rotation controls need hardening |
| Mini App intended path | Telegram signed `initData` | HMAC verification, freshness check, user ID mapping | Correct architectural model; recent fixes need live verification |
| Mini App fallback | `X-Api-Key` | `require_webapp_user` adapts canonical key owner to user identity | Works without Telegram auth, but creates a bearer-key exposure trade-off |
| Admin panel | Session dependency | `require_session` across modular admin routes | Must remain tightly controlled because admin can affect inventory, balances, and dynamic configuration |
| Payment callbacks | Provider-specific webhook logic | Endpoint-specific verification | Requires provider-by-provider verification review before enabling real payouts |

## 5. Database Model Understanding

| Collection family | Ownership / key | Function |
|---|---|---|
| `users` | `user_id` | Canonical user profile, balance, rank, API key, referral, preferences, wallet addresses |
| `orders` | `buyer_id`, `order_id` | Account-purchase lifecycle and delivery state |
| `session_accounts` / accounts | account identifiers | Inventory/session storage references and availability state |
| `countries` | country code | Pricing, stock mode, purchase availability, network-related inventory metadata |
| `deposits`, `transactions`, `withdrawals` | `user_id` and transaction identifiers | Funding, ledger, and payout lifecycle |
| `sell_requests`, `users_sell_stock` | `user_id`, request/account references | Seller submissions, verification, payout holds, stock visibility |
| `settings`, `custom_env` | configuration keys | Runtime configuration and admin-adjustable values |
| audit/log collections | timestamps and entity references | Administrative and worker activity traceability |

The code uses a lazy Motor collection proxy so modules resolve to the active Mongo client after the startup-loop reinitialization. This avoids a prior multi-event-loop class of failure. Financial mutation paths intentionally bypass the RAM cache and invalidate user-cache entries after writes.

## 6. Public API Surface

The public product API is versioned under `/api/v1/` and uses `X-Api-Key`. The Mini App owner API is under `/webapp/api/`; it uses canonical user resolution via either signed Telegram data or the current API-key fallback. See `API_DOCS.md` for the detailed route table.

The service also exposes `/`, `/ping`, `/health`, `/docs`, and `/redoc`. Admin routes are intentionally excluded from the public OpenAPI document and are described by module in `API_DOCS.md`.

## 7. Findings

| Severity | Finding | Evidence | Why it matters | Required direction |
|---|---|---|---|---|
| **CRITICAL** | Synthetic sales-feed generator exists | `server/services/sales_feed/fake_generator.py` explicitly produces realistic fake purchase events | Conflicts directly with the owner’s no-dummy-data requirement and can mislead users if enabled | Remove feature or hard-disable it in production; retain only audited real purchase events |
| **HIGH** | Fake inventory mode exists | `countrydb.py` and `stock_display.py` support `stock_mode="fake"` and `fake_stock` | Users may see availability that is not backed by valid sessions | Remove/hard-disable fake stock and display only verified inventory |
| **HIGH** | CORS defaults to wildcard origin | `server/core/api.py` defaults `ALLOWED_ORIGINS` to `*` | Browser clients can invoke bearer-key endpoints from arbitrary origins; CORS is not a substitute for auth but is a valuable browser boundary | Require explicit production origins and fail closed in production |
| **HIGH** | API keys are bearer credentials used by Mini App fallback | API-key user lookup and `sessionStorage` client fallback | Any XSS, compromised device, or malicious browser context can replay a stored key during that tab session | Prefer fixed Telegram initData; if fallback remains, require short-lived scoped exchange tokens and explicit logout/rotation |
| **HIGH** | Runtime `custom_env` is loaded from MongoDB before normal config | `server/__main__.py` reads `custom_env` and writes `os.environ` | This is an extremely privileged configuration channel with potential to alter tokens, callback URLs, or payment behavior | Add strict allowlist, encryption at rest, approval/audit trail, and deny secret replacement via admin UI |
| **MEDIUM** | Monetary data uses floating-point values | `userdb.py`, wallet/service paths, provider and response formatting | Binary floating point can introduce rounding discrepancies in financial operations | Migrate ledger-critical values to integer minor units or Decimal with explicit serialization rules |
| **MEDIUM** | Core modules are oversized | `market.py` ~112 kB, `api/routes/admin.py` ~100 kB, `wallet.py` ~93 kB, workers ~54 kB | Large files increase review risk, coupling, and test gaps | Split by use case into focused controllers/services/repositories |
| **MEDIUM** | Background worker exits are logged but not restarted | `server/__main__.py` supervisor callback reports failure and tells operator to restart | A failed payment/order/cleanup worker can remain down until human intervention | Introduce bounded supervised restart/backoff and worker health metrics |
| **MEDIUM** | Test coverage is narrow relative to surface | 18 tests for public API, admin, bot, payment, and Mini App surface | Critical money and authorization paths may regress without detection | Build contract tests for owner isolation, provider callbacks, idempotency, and bot/Mini App journeys |
| **LOW** | Mini App initial bundle is large | Vite production output ~652 kB JS | Slower first paint on mobile Telegram clients | Lazy-load wallet, activity, and community workspaces; audit dependencies |
| **LOW** | Dependency warning | pnpm warns package-level configuration keys are ignored | Build remains functional, but dependency patch/override intent may not be enforced | Move pnpm config to supported workspace/config format and document it |

## 8. Reliability and Performance Assessment

The platform has useful reliability measures: same-event-loop FastAPI/Motor execution, lazy collections, memory caching for read-heavy entities, atomic balance deduction patterns, request IDs, structured API envelopes, delivery/OTP ownership checks, and background workers. The order route documents compensation after certain OTP setup failures, and the deployment build validates the Mini App output.

The next performance priorities should be query/index verification against actual production explain plans, real inventory count aggregation cost, caching boundaries for country/catalog data, Mini App code-splitting, and worker instrumentation. Optimization must not precede integrity work on wallet, reservation, payment-callback, and session-delivery paths.

## 9. Recommended Approval-Gated Remediation Order

1. Remove or permanently disable fake sales and fake stock paths, including admin controls and channel preview logic.
2. Restore Telegram signed-initData as the default identity mechanism and turn the API-key Mini App path into a short-lived, scoped exchange flow—or explicitly accept its bearer-key risk.
3. Fail closed on CORS production configuration and restrict dynamic environment keys to an allowlist.
4. Complete financial idempotency and owner-isolation test coverage before any UI expansion.
5. Introduce Decimal/minor-unit financial representation in a migration-safe, backward-compatible plan.
6. Split large bot/API modules only after contract tests lock down behavior.
7. Add worker restart supervision, readiness checks, and observable metrics.

## 10. Scope Boundary

>This report deliberately does **not** approve a broad rewrite yet. The requested rewrite affects money movement, session delivery, and Telegram authorization. The owner should approve the remediation sequence after reviewing this report; then changes should be made in small, tested, reversible batches.

## References

[1] [Telegram Mini Apps: validating received data](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)

[2] [FastAPI security documentation](https://fastapi.tiangolo.com/tutorial/security/)

[3] [OWASP CORS guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Origin_Resource_Sharing_Cheat_Sheet.html)
