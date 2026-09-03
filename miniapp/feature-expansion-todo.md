# Real User Control Center Expansion

- [x] Map every existing authenticated deposit, wallet, order, referral, support, and sell-account backend workflow.
- [x] Add secure Mini App API endpoints only where the current verified `/webapp/api/*` contract has a real backend capability to expose.
- [x] Implement live deposit instructions, deposit status, and wallet history without exposing sensitive payment keys.
- [x] Implement real account order history and OTP lifecycle visibility without exposing data outside the purchaser session.
- [x] Implement referral statistics, support shortcuts, and seller request status using canonical user data.
- [ ] Validate access control, financial idempotency, error states, and empty states without demo data.
- [ ] Commit and push the validated feature expansion to GitHub.

## Single-Container Deployment

- [x] Verify Python, Node, native-library, and Mini App build requirements for the production image.
- [x] Create a multi-stage Dockerfile that builds `miniapp/` and copies only compiled assets into the Python runtime image.
- [x] Add a safe Docker build context exclusion file and validate Dockerfile syntax/runtime command.
- [x] Commit the container configuration without including local secrets, logs, node_modules, build artifacts, or audit files.
- [ ] Push the container configuration to GitHub only after the owner requests it.

## Legacy Bot Mini App Routing

- [x] Locate every legacy Web App URL, inline dashboard button, and reply-keyboard entry point in the bot source.
- [x] Route each valid user-facing Mini App entry point to the canonical `WEBAPP_URL` (`/app/`) without exposing a legacy dashboard.
- [x] Validate startup menu registration and source-level inline-button URLs, then commit the routing correction.

## Render Mini App Build Recovery

- [x] Trace the missing `miniapp/dist/public` build output from Docker build stage through FastAPI static serving.
- [x] Correct the Docker build output copy path or Vite output configuration so `/app/` always has compiled assets in production.
- [x] Verify the container filesystem contract and `/app/` availability behavior.
- [ ] Commit and push the artifact-guard correction, then redeploy the existing Render service using Docker runtime.

## Native Render Deployment Without Docker

- [x] Verify the root Mini App build script generates `miniapp/dist/public` before the Python server starts.
- [x] Configure the native Render build command separately from the single runtime command `python3 -m server`.
- [x] Provide exact Render settings and post-deploy `/app/` retest steps without requiring a Docker runtime migration.

## Render Settings Guide

- [x] Create a root `RENDER_DEPLOY.md` that maps each visible Render setting field to its exact native deployment value.
- [x] Document environment variables, deploy verification, known error recovery, and security rules without embedding secrets.
- [x] Review the guide against `render.yaml`, then commit and push it to GitHub.

## Telegram Mini App Launch and Authentication Recovery

- [x] Map the observed 404, Mini App shell, and signed-user API failure to their exact launch and backend code paths.
- [x] Audit the browser initData capture, header transport, Telegram HMAC validation, and current bot-token runtime assumptions without logging secrets.
- [x] Remove or redirect remaining legacy Mini App entry paths and make valid Telegram authentication failures diagnosable without exposing sensitive information.
- [x] Run safe static, unsigned, and signed-fixture regression tests.
- [ ] Commit and push the repair with an exact Telegram retest procedure.

## Canonical User Dashboard Launch Contract

- [x] Capture the live response behavior for canonical and plausible malformed User Dashboard URLs.
- [x] Cross-check inline WebApp buttons, bot menu buttons, and backend initData validation against official Telegram Mini App documentation.
- [x] Replace custom button serialization for Mini App buttons with native Pyrogram WebApp buttons and redirect stale non-API legacy routes.
- [x] Add deterministic launch-contract and signed-user tests.
- [ ] Commit and push the canonical launch-contract repair.

## Bot Token and Signed Identity Recovery

- [x] Trace every runtime consumer of `BOT_TOKEN` and verify one canonical environment source is used for bot startup and Mini App HMAC validation.
- [x] Add startup-safe, non-secret logging that reports the authenticated Telegram bot identity and never prints token material.
- [x] Identify and repair the reversed Telegram HMAC key/message derivation that caused genuine initData signature failures.
- [x] Normalize `BOT_TOKEN` whitespace at configuration load and verify its public numeric prefix matches the bot identity returned by Telegram at startup.
- [x] Preserve all signed initData fields except `hash` in the HMAC data-check-string, including newer Telegram client fields such as `signature`.
- [x] Validate configuration guards and signed-initData regression cases, including current-client `signature` field handling.
- [ ] Commit and push the official HMAC and signature-field repair, then redeploy and retest from a fresh Telegram button.

## Live Render Authentication Verification

- [ ] Use secure logged-in Render dashboard access to verify the running deploy revision and native build settings without exposing account credentials.
- [ ] Correlate a fresh Mini App request with non-secret Render logs and determine whether the deployed authentication code accepts the signed user payload.
- [ ] Apply only necessary source/config changes, push them, and confirm the same Telegram user reaches the real dashboard without a 404 or verification failure.

## Evidence-Driven Signed Request Diagnostics

- [x] Add a versioned, non-secret diagnostic fingerprint for each failed initData verification: received-field names, HMAC branch, and request ID only.
- [x] Add controlled tests covering the exact query parsing and field-selection path used by live requests.
- [ ] Push instrumentation, correlate one fresh Telegram request with its Render log record, and make a single evidence-backed final repair.

## Direct Mini App API-Key Login

- [x] Reuse the existing canonical API-key validator to resolve a Mini App user without creating a second account system.
- [x] Add a dedicated API-key login screen and secure API header flow without putting the key in a URL, log, or synthetic demo state.
- [x] Ensure API-key authenticated Mini App routes expose only the key owner’s account, wallet, orders, and transactions.
- [x] Add safe login, invalid-key, logout, and cross-user isolation tests.
- [ ] Commit and push the production API-key login flow, then verify it from a fresh Mini App session.

## Backup-First Full Repository Audit

- [x] Create a timestamped, non-destructive repository backup outside the working tree with a backup manifest and integrity checksum.
- [x] Produce an exhaustive repository inventory, architecture/data-flow map, API surface map, dependency inventory, and environment-variable map.
- [x] Audit production risks across authentication, authorization, financial idempotency, Telegram integration, database behavior, performance, and error handling.
- [x] Write `ANALYSIS_REPORT.md`, `CHANGES_LOG.md`, `API_DOCS.md`, `SETUP_GUIDE.md`, `ENV_GUIDE.md`, and `ARCHITECTURE.md` without including secrets.
- [ ] Present the audit report and wait for owner approval before broad refactoring or destructive changes.

## Approved Production Hardening — Batch 1

- [x] Remove or hard-disable synthetic sales feed, fake sales preview, fake stock, and fake inventory display paths without deleting existing database records.
- [x] Require explicit production browser origins and constrain MongoDB dynamic environment overrides to a safe non-secret allowlist.
- [x] Limit Mini App API-key session persistence to a short inactivity window with automatic client-side clear and explicit logout.
- [x] Add regression tests for no-synthetic-data behavior, CORS/config fail-closed behavior, and API-key session expiry behavior.
- [x] Run full validation, update `CHANGES_LOG.md`, and deliver the next approval-gated remediation batch.

## Approved Production Readiness — Batch 2

- [x] Introduce shared decimal-safe validation at payment/order/withdrawal request boundaries without migrating stored MongoDB numeric fields.
- [x] Tighten request-size/error safeguards for exposed financial and API endpoints.
- [x] Add low-risk performance safeguards for repeated public inventory/API queries and Mini App loading.
- [x] Add focused regression coverage for numeric validation and request-boundary reliability behavior.
- [x] Run full validation and update technical documentation for the validated input-boundary batch.

## Approved Production Performance — Batch 3

- [x] Profile and document initial Mini App loading hotspots.
- [x] Add bounded, invalidation-safe backend caching or coalescing only for read-only public inventory data.
- [x] Assess duplicate-prone write replay behavior; retain existing operation-specific durable financial guards and do not misuse the per-request `X-Request-Id` as a generic idempotency key.
- [x] Lazy-load non-dashboard Mini App workspaces and preserve existing no-dummy-data error states.
- [x] Validate build output and update change log for the completed Mini App loading optimization.

## Approved Production Performance — Batch 4

- [x] Profile the Mini App dashboard bootstrap for redundant user reads and independently awaitable counts.
- [x] Reuse the loaded canonical profile document for preferences and parallelize independent read-only account counts.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Performance — Batch 5

- [x] Profile the initial Mini App order-history response for independent purchase and seller-request reads.
- [x] Parallelize those read-only histories while retaining current limits, sort order, response fields, and per-source failure isolation.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Performance — Batch 6

- [x] Profile Mini App seller-status data for encrypted retry-only session payloads that are not rendered to the user.
- [x] Add a Mini App-only lightweight projection while preserving default bot helper behavior and all real seller-status fields.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Performance — Batch 7

- [x] Profile the API-key Mini App login sequence for duplicate authenticated profile reads.
- [x] Reuse the just-verified login profile for the initial inventory/history refresh while preserving normal fresh refresh behavior.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Performance — Batch 8

- [x] Profile response-transfer overhead for larger Mini App/API/static payloads.
- [x] Enable standard gzip compression with a conservative threshold while preserving all route and data behavior.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Performance — Batch 9

- [x] Profile Mini App read requests for unnecessary JSON content-type headers.
- [x] Restrict default JSON content type to body-carrying requests while preserving API-key, Telegram, and request-ID header behavior.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Reliability — Batch 10

- [x] Profile independent community workspace reads for all-or-nothing client failure behavior.
- [x] Render every successful real-data source while surfacing a clear partial-availability notice for rejected reads.
- [x] Add unmount-safe handling, regression coverage, full validation, documentation, and the validated GitHub push.

## Approved Production Reliability — Batch 11

- [x] Profile the real OTP polling loop for terminal-state continuation and overlapping request risk.
- [x] Stop terminal polling, serialize active requests, normalize terminal HTTP outcomes, and clean up on workspace unmount.
- [x] Add regression coverage, run full validation, document the no-migration change, and push the validated pass.

## Approved Production Reliability — Batch 12

- [x] Profile remaining Mini App/API read and polling paths for safe latency, duplicate-work, or partial-availability improvements.
- [x] Implement only the highest-value evidence-backed change without changing financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 13

- [x] Profile remaining Mini App/API reads and client lifecycle paths for safe partial-availability, duplicate-work, or polling improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 14

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 15

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 16

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 17

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 18

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 19

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 20

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 21

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Production Reliability — Batch 22

- [x] Profile remaining Mini App/API read, refresh, and lifecycle paths for safe duplicate-work, partial-availability, or cancellation improvements.
- [x] Implement only the highest-value evidence-backed change without altering financial write semantics or stored data.
- [x] Add regressions, complete full validation, document the no-migration outcome, and push the validated pass.

## Approved Mini App Wallet UX Rebuild

- [x] Inspect existing deposit/withdrawal API response contracts, balance constraints, and current wallet workspace interaction states.
- [x] Define separate mobile-first deposit and withdrawal journeys with clear steps, amount/fee guidance, validation, pending states, and safe recovery messages.
- [x] Implement dedicated deposit and withdrawal workspace views with clean white/blue TG FOX styling and real API-backed data only.
- [x] Add navigation from the wallet overview, regression coverage, full validation, change-log documentation, and a validated GitHub push.

## Approved USDT BEP20/TRC20 Transaction-Hash Verification

- [x] Inspect existing bot, Mini App, deposit persistence, and BscScan/Tron verification paths for the two manual USDT methods.
- [x] Define one-use transaction-hash submission, recipient/asset/amount/status checks, and safe pending/failed/verified user states without blind crediting.
- [x] Add real API-backed transaction-hash submission UX to the bot and Mini App only for USDT BEP20 and USDT TRC20.
- [x] Add adversarial regression coverage for malformed, reused, wrong-network, wrong-recipient, wrong-amount, unconfirmed, and successful verification outcomes; validate and push.

## Approved Deposit Method Full-Page UX

- [x] Inspect enabled deposit provider metadata and identify which methods should retain a network selection versus presenting one direct method page.
- [x] Remove user-facing OxaPay network selection where the provider owns network/payment routing, without changing server-side provider validation.
- [x] Create separate, focused Mini App pages for each selected deposit method with method-specific amount, instructions, provider action, hash/order input where applicable, and status recovery.
- [x] Add method-first navigation, regression coverage, full validation, documentation, and a validated GitHub push.

## Approved Buy and Sell Account Full-Page UX

- [x] Inspect current Mini App buy flow, country/stock data, order/OTP states, seller submission data, and real server lifecycle endpoints.
- [x] Define focused buyer and seller page journeys that retain only real availability, pricing, balance, status, and recovery information.
- [x] Replace mixed Buy and Sell workspace interactions with dedicated full-page selection, review, submission, confirmation, and pending-status experiences.
- [x] Add regression coverage, full validation, documentation, and a validated GitHub push without creating any dummy account, order, sale, or inventory data.

## Approved Dashboard Command-Center UX

- [x] Inspect dashboard data sources, existing quick actions, current content hierarchy, and routes into focused Buy, Sell, Deposit, Withdrawal, and Activity pages.
- [x] Define a real-data-only dashboard command center with clear balance, live availability, recent activity, and action priorities—without synthetic charts, estimates, or filler states.
- [x] Rebuild the dashboard composition and action routing for a concise mobile-first command-center experience with no duplicated workflow surfaces.
- [x] Add regression coverage, full validation, documentation, and a validated GitHub push without creating or presenting dummy financial, inventory, account, order, or seller data.

## Approved Wallet Summary Rendering Reliability Fix

- [x] Inspect Wallet Center summary-card data loading, conditional rendering, CSS background, and client-side error paths causing the blank/dark presentation.
- [x] Implement explicit white loading, unavailable, and loaded balance-summary states that preserve real server data and never render an empty financial card.
- [x] Add regression coverage, full validation, documentation, and a validated GitHub push without creating or displaying dummy balance or financial data.

## Approved Admin Panel Full-Page Control-Center UX

- [x] Inspect current admin APIs, authorization guards, inventory/order/payment/seller operations, existing templates, and every destructive or externally consequential action.
- [x] Define separate focused admin pages with real operational data, role-aware navigation, explicit confirmation boundaries, and no synthetic metrics or placeholder controls.
- [x] Rebuild the admin panel shell and workflow pages while preserving existing server-side authorization and requiring explicit confirmation for sensitive state-changing operations.
- [x] Add regression coverage, full validation, documentation, and a validated GitHub push without running any destructive database, payment, withdrawal, account, or notification action.

## Approved Deep Admin Panel Connection Audit

- [x] Create a complete inventory of every admin template, route, API endpoint, client-side data loader, form, modal, and state-changing action.
- [x] Verify template-to-route and frontend-to-backend contracts, return shapes, authorization dependencies, CSRF behavior, error states, and stale/dead links across all admin pages.
- [x] Implement only confirmed connection, resilience, authorization, or UX defects; preserve existing operational semantics and require explicit confirmation for sensitive actions.
- [x] Add route-contract regression coverage and run complete non-destructive validation without sending payments, withdrawals, notifications, account actions, destructive database commands, or backups/restores.
- [x] Document findings, completion status, intended changes, validation results, and a verified GitHub push.

## Approved Live Authenticated Admin Panel Audit

- [x] Log in to the deployed admin panel without retaining credentials and inspect each operational page using read-only navigation/data loads only.
- [x] Capture and diagnose live Payments page, browser-console, and network failures without approving/rejecting/retrying any financial record.
- [x] Evaluate live admin UI/UX page hierarchy, responsiveness, loading/error states, and real-data readability; implement only verified non-destructive fixes.
- [x] Add regression coverage, run complete validation, safely re-check live read-only loading, document findings, and push the verified update.

## Approved Sales Feed Live Rendering Repair

- [x] Inspect deployed Sales Feed data request, browser console, DOM state, and rendered style without saving feed settings or sending a notification.
- [x] Identify and repair the confirmed data/contract/rendering issue while preserving the existing real-purchases-only policy and server-side authorization.
- [x] Add regression coverage and run complete non-destructive validation without changing feed configuration or sending any message.
- [x] Document the root cause and repair, commit, push, and safely re-check the deployed page after rollout.

## Approved Sales Feed Live Recheck and Final Repair

- [x] Inspect the currently deployed page revision, source markers, settings response, DOM layout, and browser errors without saving or sending anything.
- [x] Implement the confirmed remaining production rendering or deployment correction instead of assuming the previous local fix is live.
- [x] Run full non-destructive validation, confirm the repaired result in the deployed browser after rollout, and push the verified correction.
