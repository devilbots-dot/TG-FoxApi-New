# TgFoxApi Change Log

## 2026-08-25 — Backup-First Audit Phase

This phase created a verified non-destructive source backup and documentation deliverables. It did not perform a broad code rewrite, database migration, data deletion, or payment/provider operation.

| Change | Reason | Effect |
|---|---|---|
| Created timestamped backup archive outside repository | Preserve a recovery point before broader analysis/refactoring | Source/config state can be restored independently of Git |
| Created `BACKUP_README.md` and checksum manifest in backup directory | Document backup scope and integrity verification | Operators can validate archive before restore |
| Created `ANALYSIS_REPORT.md` | Capture architecture, testing baseline, risks, and remediation order | Owner can approve scope before refactoring |
| Created `ARCHITECTURE.md` | Document runtime topology, startup sequence, identity model, and boundaries | Shared technical reference |
| Created `API_DOCS.md` | Map public, Mini App, callback, health, and admin route surfaces | Supports client/integration review |
| Created `SETUP_GUIDE.md` | Document local and native Render deployment workflow | Reduces deployment ambiguity |
| Created `ENV_GUIDE.md` | Document variables without disclosing values | Safer operator onboarding |

## Recent Mini App Work Present in the Audited Baseline

The audit baseline includes prior committed work that integrated the React Mini App into the same Python service at `/app/`, added user-scoped wallet/order/deposit/withdrawal/community routes, introduced legacy path redirects, and added a direct API-key Mini App login fallback. Those changes remain subject to the risks and approval-gated remediation order described in `ANALYSIS_REPORT.md`.

## 2026-08-25 — Production Hardening Batch 1

This owner-approved batch removed synthetic user-facing behavior and tightened configuration/browser boundaries without deleting MongoDB records or performing a financial schema migration.

| Change | Reason | Effect |
|---|---|---|
| Removed fake-sales worker, generator, formatter, preview endpoint, admin controls, and active configuration defaults | Synthetic purchase activity is incompatible with the no-dummy-data policy | Sales feed now publishes only completed real purchase events |
| Removed fake-stock display behavior | Displayed availability must correspond to real unsold inventory | Buyer-visible stock is always the verified real count; legacy fake fields are ignored |
| Retired fake-stock mutation endpoints | Admin actions must not create misleading availability | Legacy endpoints return an explicit retirement response; bulk hide uses temporary disable instead |
| Replaced wildcard CORS default with explicit configured origins or same-origin-only mode | Browser clients must not receive broad cross-origin API access by default | Cross-origin access fails closed unless `ALLOWED_ORIGINS` is explicitly configured |
| Restricted MongoDB `custom_env` bootstrap values to a non-secret allowlist | Database-managed keys must not override deployment secrets | Provider keys, bot token, and database credentials remain host-environment controlled |
| Added 15-minute inactivity expiry for Mini App API-key session storage | Browser bearer credentials should not remain usable for an entire unattended tab session | Key is header-only, session-scoped, cleared on logout, and automatically cleared after idle timeout |
| Added regression tests and focused lint/build checks | Security behavior must remain enforceable in future changes | 20 regression tests, changed-file lint, dependency health, TypeScript, and production build pass |

## 2026-08-25 — Production Readiness Batch 2 (Input Boundaries)

| Change | Reason | Effect |
|---|---|---|
| Added shared `server.utils.money.parse_money` | Financial HTTP inputs must not enter services through binary float parsing | Deposit and withdrawal amounts now parse with `Decimal`, reject booleans/NaN/infinity/out-of-range values, and round predictably to four decimal places |
| Applied decimal validation to Mini App deposit creation | Provider requests must receive finite, minimum-compliant amounts | Invalid external values fail before provider/payment-record work begins |
| Applied decimal validation to Mini App withdrawal creation | Withdrawal service must receive finite, bounded amounts | Invalid, too-small, or too-large external values fail before balance reservation or payout workflow begins |
| Added boundary tests | Preserve numeric behavior while avoiding stored-data migration | 30 regression tests pass; existing MongoDB numeric fields remain untouched |
| Added scoped JSON request-size guard | Oversized JSON should not reach API/financial business handlers | `/api/*` and `/webapp/api/*` JSON bodies above 256 KiB are rejected before parsing; upload routes are unaffected |

## 2026-08-25 — Production Performance Batch 3 (Mini App Loading)

| Change | Reason | Effect |
|---|---|---|
| Lazy-loaded wallet, transaction, and community workspaces | These screens are not required for the first dashboard render | Production build now emits separate workspace chunks (approximately 15–29 kB each) and lowers initial JavaScript from approximately 652 kB to 594 kB before gzip |
| Added workspace loading fallback | Deferred code must retain a clear, non-demo user experience | Users see a secure workspace loading state while real API-backed modules load |

## 2026-08-25 — Production Performance Batch 3 (Catalog Read Coalescing)

| Change | Reason | Effect |
|---|---|---|
| Reused one per-request discount context for Mini App catalog previews | The catalog loop previously recalculated the same user's rank and discount settings for every country | `/webapp/api/countries` loads the user rank and the RAM-backed discount configuration once, then applies a consistent snapshot to all displayed countries |
| Preserved independent checkout pricing | Preview pricing must not become a financial source of truth | `/webapp/api/buy` continues to calculate its own server-side price immediately before purchase orchestration |
| Kept replay protection operation-specific | The current client generates a fresh `X-Request-Id` per request, so treating it as a generic durable idempotency key would not safely replay uncertain financial requests | Existing durable controls remain: atomic balance reservation/deduction, unique order/deposit identifiers, withdrawal processing claims, and successful-order accounting markers. No financial records or database schema were migrated. |

## 2026-08-25 — Production Performance Batch 4 (Profile Bootstrap)

| Change | Reason | Effect |
|---|---|---|
| Reused the Mini App profile endpoint's already-loaded canonical user document for preference serialization | `/webapp/api/me` previously fetched the same `users` document again only to read `webapp_prefs` | Removes one redundant MongoDB user lookup from each profile bootstrap while preserving the exact response schema and default themes |
| Parallelized independent purchase and seller-status counts | The two counts query unrelated collections and do not depend on one another | The profile endpoint waits for the slower count rather than their combined sequential latency; each count retains its independent safe failure fallback |
| Added regression coverage | Optimization must preserve canonical preference behavior and remain visible to future maintainers | Full suite now contains 34 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 5 (Order History Bootstrap)

| Change | Reason | Effect |
|---|---|---|
| Parallelized Mini App purchase and seller-request history reads | The dashboard loads `/webapp/api/record`, whose two history sources are independent MongoDB collections | Purchase and seller histories now load concurrently instead of serially; the endpoint latency is bounded by the slower query rather than their combined wait |
| Preserved per-source resilience and response contract | One history source being unavailable must not hide the other source's real data | Each source keeps its existing warning log and empty-list fallback; order, seller, status, and timestamp fields remain unchanged |
| Added regression coverage | The concurrency arrangement and client response shape must remain enforceable | Full suite now contains 35 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 6 (Seller Status Payload)

| Change | Reason | Effect |
|---|---|---|
| Added an explicit lightweight mode to user seller-request reads | Seller requests may contain encrypted session bytes needed only by internal retry workflows, while the Mini App displays summary metadata only | `/webapp/api/sell-requests` now projects out `session_bytes_enc` before serializing the real seller-status summary, reducing response/database payload without altering user-visible fields |
| Kept existing bot history behavior compatible | The bot's existing request-history formatter uses the same helper and must not unexpectedly lose fields | Full payload remains the default helper behavior; only the Mini App opts into the lightweight projection |
| Added regression coverage | Projection intent must remain preserved across future refactors | Full suite now contains 36 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 7 (API-Key Login Bootstrap)

| Change | Reason | Effect |
|---|---|---|
| Reused the verified profile returned by API-key login during the first dashboard refresh | The API-key login call already performs the authenticated `/webapp/api/me` identity request, but the next refresh immediately repeated it | A successful API-key login now uses that verified profile and only loads independent inventory/history data afterward, eliminating one authenticated profile request per login |
| Preserved fresh reads for normal refreshes | Post-purchase and initial app loads must still obtain current server state | First-load and subsequent refresh flows continue to request a fresh profile; only the just-verified login result is reused |
| Added regression coverage | Login flow performance must not weaken the API-key security contract | Full suite now contains 37 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 8 (Response Compression)

| Change | Reason | Effect |
|---|---|---|
| Enabled standard gzip response compression with a 1 KiB threshold | Mini App/API responses and static assets can be substantially larger than small control responses | Clients that advertise gzip support can receive compressed larger responses, while smaller responses avoid compression overhead |
| Preserved browser/API semantics | Compression is a transport concern and must not alter data, authentication, or write handling | No route contract, stored record, financial operation, request validation rule, or response body schema changed |
| Added regression coverage | Transfer optimization must remain intentionally configured | Full suite now contains 38 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 9 (Read Request Headers)

| Change | Reason | Effect |
|---|---|---|
| Restricted default JSON `Content-Type` to Mini App requests with JSON bodies | Read-only API requests do not send a body and should not advertise a JSON payload type unnecessarily | GET-style requests now send `Accept`, request correlation, and authentication headers without a forced JSON `Content-Type`; JSON writes retain their existing content type |
| Normalized request header construction | Caller-provided headers should be handled through the browser's standard `Headers` API before protected headers are applied | API-key/Telegram auth and `X-Request-Id` remain explicitly applied by the client; no endpoint, payload, or financial behavior changed |
| Added regression coverage | Header behavior affects cross-origin/browser efficiency and must remain deliberate | Full suite now contains 39 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 10 (Community Workspace Partial Loads)

| Change | Reason | Effect |
|---|---|---|
| Replaced all-or-nothing community workspace loading with independently settled reads | Referral, support, and seller-status data are independent real-data sources; one temporary outage should not hide the other available account information | The Mini App now renders every successful source and shows a clear non-demo notice only when one or more auxiliary reads fail |
| Added unmount-safe state handling | Deferred requests can resolve after users navigate away from a lazy-loaded workspace | The effect cleanup prevents late responses from updating unmounted workspace state |
| Added regression coverage | Partial availability behavior must remain intentional | Full suite now contains 40 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 11 (OTP Polling Lifecycle)

| Change | Reason | Effect |
|---|---|---|
| Stopped Mini App OTP polling after terminal states | A ready code, timeout, expired delivery, or unknown order cannot become a useful active polling target without a new purchase flow | The client stops its timer for terminal server outcomes, reducing unnecessary request traffic and repeated terminal responses |
| Prevented overlapping OTP requests and late state writes | Slow networks can allow an interval to start another request before the prior poll completes, and lazy workspace navigation can unmount the view | At most one OTP request runs at a time; cleanup disables late updates and cancels the timer on unmount |
| Added regression coverage | Delivery status handling must remain explicit and production-safe | Full suite now contains 41 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 12 (Wallet History Partial Loads)

| Change | Reason | Effect |
|---|---|---|
| Replaced all-or-nothing wallet workspace loading with independently settled reads | The verified wallet summary is essential, but deposit and withdrawal history are independent secondary reads | A successful verified balance snapshot remains required before rendering wallet controls; an isolated history outage now leaves the real balance and available history visible with a clear notice |
| Preserved financial-action boundaries | A partial history response must never substitute, cache, or estimate financial balances | Balance, reserved amount, deposit methods, withdrawal configuration, and all write requests continue to come directly from their existing server endpoints |
| Added regression coverage | Partial-read behavior around a financial workspace must stay explicit | Full suite now contains 42 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 13 (Activity Ledger Lifecycle)

| Change | Reason | Effect |
|---|---|---|
| Made the Activity workspace's initial ledger request unmount-safe | A user can leave a lazy-loaded workspace before the real transaction request resolves | Delayed ledger responses and errors no longer update state or show a toast after the workspace has unmounted |
| Preserved manual refresh behavior | A user-initiated refresh is intentionally active while the Activity workspace is open | Existing manual refresh and real server ledger source remain unchanged; no cached or fabricated transaction data was introduced |
| Added regression coverage | Async lifecycle safety should remain explicit during future UI refactors | Full suite now contains 43 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 14 (Wallet Load Lifecycle)

| Change | Reason | Effect |
|---|---|---|
| Made the Wallet workspace's initial summary/history reads unmount-safe | Users can navigate away from a lazy-loaded wallet workspace while real server reads are still in flight | Delayed wallet results and errors are ignored after cleanup, preventing late state updates or toasts on an unmounted workspace |
| Preserved financial freshness | Cleanup must not replace any live balance or transaction source with a cache | While open, the Wallet workspace still reads its summary, deposit history, and withdrawal history from the existing server endpoints; manual/action refresh behavior is unchanged |
| Retained regression coverage | Wallet lifecycle behavior must remain explicit because it presents financial information | Full suite remains at 43 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 15 (Rapid Submit Guards)

| Change | Reason | Effect |
|---|---|---|
| Added synchronous in-flight guards around Mini App purchase and wallet action entry points | UI disabled state is rendered asynchronously, so a very rapid double click can otherwise start another client request before the first render update completes | The browser now permits one active account purchase, deposit-instruction creation, withdrawal-address save, or withdrawal submission at a time |
| Preserved server-side financial controls | Client guards improve interaction reliability but must never be treated as the sole financial protection | Existing server-side atomic balance handling, operation-specific safeguards, validation, and provider workflows remain unchanged |
| Added regression coverage | Repeat-submit protection must remain present during future UI changes | Full suite now contains 44 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 16 (API-Key Account Read Reuse)

| Change | Reason | Effect |
|---|---|---|
| Reused the canonical account document already resolved by API-key authentication for Mini App profile and theme preference reads | An API-key request authenticated the full user document and then several routes immediately performed the same user lookup again | `/webapp/api/me` and theme preference/entitlement routes now reuse request-local internal auth context for API-key sessions, removing duplicate same-request account reads |
| Kept Telegram and financial behavior unchanged | Telegram initData contains Telegram identity rather than a canonical account document, and financial mutations require their existing server-side checks | Telegram-authenticated paths retain their existing account lookup; theme purchase balance deduction, entitlement checks, and all unrelated write flows remain unchanged |
| Added regression coverage and lint cleanup | Internal auth context must not alter public identity fields or response payloads | Full suite now contains 45 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Integrity Batch 17 (Retired Fake-Stock Admin UI)

| Change | Reason | Effect |
|---|---|---|
| Removed obsolete fake-stock and fake-mode controls from the country admin page | Runtime buyer stock is already real-inventory-only, but the admin UI still advertised fake stock, fake modes, and matching bulk edits | Operators now see verified stock counts, real availability controls, and accurate temporary-disable wording only |
| Hid the retired fake-stock setting without deleting legacy configuration | Existing deployments may still contain the old key in MongoDB settings | The obsolete setting is no longer rendered in the admin toggle UI; its stored value was not deleted or migrated |
| Clarified sales-feed bot admin help | Retired compatibility commands must not look like supported fake-sales configuration | Operator command documentation now describes a real completed-purchases feed and states legacy fake-sales commands are informational stubs only |
| Added regression coverage | No-dummy policy must include operator-facing UI as well as buyer runtime logic | Full suite now contains 46 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 18 (Fresh Wallet Snapshots)

| Change | Reason | Effect |
|---|---|---|
| Added a direct-Mongo wallet snapshot helper | Wallet overview reads previously assembled balance, reserved balance, and addresses through separate document lookups | One canonical projection now returns all three fresh fields together, reducing MongoDB round trips and avoiding mixed-time read windows |
| Adopted the snapshot for Mini App wallet, public wallet overview, and public user balance routes | These high-value client responses need current financial fields but do not need independent reads of the same user document | Affected endpoints preserve their existing response schemas, USD formatting, and address shape while reading a single fresh user document |
| Preserved financial safety boundaries | Financial values must not be served from a stale cache | The helper always queries MongoDB directly; writes, reservations, atomic deductions, provider workflows, and database records are unchanged |
| Added regression coverage and lint cleanup | One projection and expected rounding/address mapping need to stay explicit | Full suite now contains 47 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 19 (Public Financial Read Consistency)

| Change | Reason | Effect |
|---|---|---|
| Extended fresh wallet snapshots to the public wallet-balance endpoint | The endpoint still fetched balance and reserved balance separately after the initial snapshot pass | Available, reserved, and net-spendable values now originate from one direct MongoDB projection |
| Updated public profile financial fields to use the same snapshot | Profile statistics and reserved balance previously came from separate reads and could represent slightly different moments | Profile statistics and the fresh financial snapshot now load concurrently; balance, reserved balance, and net spendable use the single snapshot while profile statistics remain unchanged |
| Preserved financial safety boundaries | Read optimization must not turn financial data into a cache | No financial write, reservation, order, provider, response schema, or stored data behavior changed |
| Extended regression coverage | The complete public financial read surface needs explicit snapshot assertions | Full suite remains at 47 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Reliability Batch 20 (Lazy Workspace Error Isolation)

| Change | Reason | Effect |
|---|---|---|
| Added workspace-local error boundaries around lazy wallet, transaction, and community modules | A lazy chunk/module error could otherwise reach the global boundary and replace the entire Mini App dashboard | Each affected workspace now shows a focused recovery panel that returns the user to the dashboard; account data and completed actions remain untouched |
| Added reset-aware error-boundary support | Boundaries need to recover when navigation changes context | A changed reset key clears a previous boundary error, preventing stale fallback state from persisting across views |
| Removed raw stack traces from the user-facing global fallback | Browser stack traces are not useful user guidance and can expose implementation detail | The fallback now gives a generic recovery action while errors remain available through browser diagnostics |
| Added regression coverage | Lazy-error containment and safe copy must remain intentional | Full suite now contains 48 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 21 (Public Order Summary Counts)

| Change | Reason | Effect |
|---|---|---|
| Parallelized independent public order-history summary counts | Total, completed, and pending counts are unrelated read-only MongoDB aggregations that previously waited sequentially | The public order-history response now waits for the slowest count rather than the combined latency of all three counts |
| Preserved response contract and real order data | Summary values must remain sourced directly from real order records | Query predicates, pagination totals, summary field names, and order payloads are unchanged |
| Added regression coverage | Count coalescing needs to remain explicit during future API changes | Full suite now contains 49 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-25 — Production Performance Batch 22 (Public Order Page Coalescing)

| Change | Reason | Effect |
|---|---|---|
| Parallelized public order page-row retrieval with the pagination total count | The order-history endpoint waited for the total before beginning an independent sorted page query | The response now waits for the slower of the two read-only queries rather than their combined latency |
| Preserved filtering, sorting, serialization, and pagination contract | Order history must continue to reflect only real user orders in its documented order | The buyer filter, optional status filter, newest-first sort, 100-item limit, datetime serialization, and response schema are unchanged |
| Added regression coverage | Page/count coalescing should remain intentional in future API refactors | Full suite now contains 50 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-26 — Mini App Wallet UX Rebuild (Dedicated Deposit and Withdrawal Flows)

| Change | Reason | Effect |
|---|---|---|
| Rebuilt Wallet as a financial gateway with dedicated Deposit and Withdrawal pages | The previous workspace combined payment-provider selection, address saving, fee review, confirmation, and history into two compact cards | Users now enter separate, focused mobile-first flows from a verified balance overview, reducing decision density and making each financial step explicit |
| Added a three-step live deposit journey | A user needs a clear provider, amount, instruction, and confirmation sequence before sending funds | The Deposit page shows enabled live providers, provider network options, live minimums, quick amounts, an instruction-ready state, copy controls, provider link, expiry, and live deposit history |
| Added a three-step withdrawal journey | Network/address errors and unclear fees are costly in crypto transfers | The Withdrawal page requires network selection, saved-address verification, amount input, server-configured fee/net preview, spendable-balance checks, explicit irreversible-transfer confirmation, and live withdrawal history |
| Preserved real API and financial safety contracts | UI redesign must not invent balances, providers, statuses, or payment outcomes | All data uses existing `/wallet`, `/deposits`, `/deposit`, `/withdraw/address`, `/withdraw`, and `/withdrawals` Mini App APIs; existing client rapid-submit guards and server-side financial controls remain in effect |
| Kept new pages lazy-loaded | The richer financial UX should not slow first dashboard render | Deposit and Withdrawal pages ship as independent production chunks and load only when the user opens the relevant flow |
| Added regression coverage | Navigation, live API usage, in-flight safeguards, and amount/address checks must remain explicit | Full suite now contains 51 passing tests; no database migration, record deletion, live financial action, or user-session access occurred |

## 2026-08-26 — Manual USDT BEP20/TRC20 Transaction-Hash Verification

| Change | Reason | Effect |
|---|---|---|
| Replaced direct-address scan crediting for `bep20_scan` and `trc20_scan` with user-submitted transaction-hash verification | A static wallet address cannot safely identify which user paid; receipt status alone does not prove a USDT payment to the platform | Bot and Mini App now require the payer's hash/transaction ID, then verify the configured recipient, official USDT token contract, required amount, successful execution, instruction timing, and chain finality before crediting |
| Added durable network-qualified hash claims in `deposit_transaction_hashes` | The same on-chain payment must never be reused for a second user or deposit | MongoDB's default unique `_id` claim blocks cross-user and concurrent reuse; the original owner may safely retry the same hash while it remains pending |
| Preserved the existing atomic `confirm_deposit()` and `record_deposit(..., deposit_id=...)` credit guards | Hash uniqueness protects a transaction, but the wallet mutation remains a separate exactly-once boundary | Duplicate bot/Mini App requests, process retry, and stale recovery cannot issue a second balance credit for the same deposit |
| Added owner-scoped Mini App hash API and a dedicated mobile instruction step | Manual crypto confirmation must be explicit and usable from both product surfaces | `POST /webapp/api/deposit/{deposit_id}/transaction-hash` resolves the canonical owner, returns only safe status metadata, and powers a white/blue “Verify on-chain” input after BEP20/TRC20 instructions |
| Added bot hash-entry conversation state | Telegram users need a direct, clear path after they pay | The bot asks only BEP20/TRC20 users to send a hash, then reports pending, rejected, temporary-provider, or credited status without exposing full hashes in service logs |
| Added mocked adversarial coverage and release validation | Financial verification needs negative as well as happy-path assertions | Coverage now includes malformed identifiers, provider timeout, wrong USDT contract/recipient, insufficient confirmation, valid BEP20/TRC20 transfers, one-use claims, and no-double-credit retry behavior; full suite contains 59 passing tests |

## 2026-08-26 — Deposit Method Full-Page UX

| Change | Reason | Effect |
|---|---|---|
| Replaced the mixed deposit form with a method picker and focused payment page per enabled provider | Provider choice, amount entry, payment instructions, and verification controls previously accumulated in one scrolling workspace | Each provider now opens in its own method-first page; creating an instruction replaces the amount step rather than appending an instruction below it |
| Removed the Mini App OxaPay network chooser | OxaPay creates a hosted invoice and owns its available network routing inside that checkout | `/webapp/api/wallet` now returns no OxaPay network choices to the Mini App; the client creates the invoice without a network parameter and directs the user to OxaPay’s hosted checkout |
| Preserved distinct direct-USDT pages | BEP20 and TRC20 are separate manual on-chain verification methods, not an interchangeable network tab | Each direct method has its own instructions, receiving address, hash/transaction-ID input, confirmation state, and server verification action |
| Added a real Mini App Binance Pay Order ID action | The existing bot already supported manual Binance Order ID verification, but the Mini App did not expose the same verified capability | The owner-scoped `/webapp/api/deposit/{deposit_id}/binance-order` endpoint validates a live Binance Order ID with the existing one-use claim and atomic deposit-credit protections; its method page now shows the real Binance Pay ID and order input |
| Added regression and release validation | UI/provider separation must remain deliberate and real-data-backed | Full suite now contains 60 passing tests; Python compile/lint, dependency health, Mini App TypeScript, production build, and whitespace validation pass; no live payment, withdrawal, or wallet credit was performed |

## 2026-08-26 — Buy and Sell Account Full-Page UX

| Change | Reason | Effect |
|---|---|---|
| Replaced the compact Buy list/modal pattern with a dedicated country selection, review, and delivery journey | Country choice, verified-balance review, order creation, and OTP delivery need clear irreversible-action boundaries | The Buy Account page now selects from live inventory, shows a separate review with server-revalidated price/availability language, creates the real order, and tracks its real OTP terminal state without requiring the user to find a different page |
| Added a dedicated Sell Account control center | Seller request status was embedded inside a broader community workspace and did not clearly distinguish secure verification from status tracking | Users now see an explicit seller journey, per-request full detail page, real pending/review/release status, timestamps, quoted amount, and admin note where available |
| Added supported `start=sell` bot handoff | The previous Mini App seller link did not initiate the existing bot state machine | The new deep link starts the same bot phone/OTP/2FA selling flow used by the native Sell Account button; the Mini App never collects or requests a Telegram session itself |
| Moved seller status out of Community tools | One seller flow should have one unambiguous entry point | Community now focuses on real referral and support data; the dashboard and drawer expose the dedicated Sell Account page directly |
| Added regression and release validation | Real inventory/order/seller lifecycle boundaries must stay visible during future UX work | Full suite now contains 61 passing tests; Python compile/lint, dependency health, Mini App TypeScript, production build, and whitespace validation pass; no dummy inventory, account, order, seller request, or financial action was created |

## 2026-08-26 — Dashboard Command-Center UX

| Change | Reason | Effect |
|---|---|---|
| Extracted the dashboard into a lazy `DashboardWorkspace` | The dashboard had grown into a mixed inline screen and loaded alongside unrelated shell code | Dashboard now ships as its own focused chunk and is protected by the same local recovery boundary as the other dedicated workspaces |
| Replaced synthetic chart and derived “settled/pending/refund” values | Visual estimates and hard-coded chart heights do not belong in a financial/account control surface | The command center displays only verified profile balance, recorded purchase/sale counts, server totals, live country availability, real stock sum, real lowest listed price, and actual recent-order statuses |
| Rebuilt top-level actions around focused pages | Users should not have to scroll through mixed widgets to find their next workflow | Add funds, Buy account, Sell account, Withdraw, live market, and Activity actions each route directly to their dedicated page |
| Simplified the app shell after extraction | Old inline dashboard, buy modal, duplicate synthetic panels, and an inactive top-bar control created competing surfaces | Home is now a focused lazy-router shell; profile, drawer, API-key access, and workspace recovery remain available without duplicate dashboard logic |
| Added regression and release validation | Dashboard must remain real-data-only during future changes | Full suite now contains 62 passing tests; Python lint, dependency health, Mini App TypeScript, production build, and whitespace validation pass; no dummy financial, inventory, account, order, or seller data was created |

## 2026-08-26 — Wallet Summary Rendering Reliability

| Change | Reason | Effect |
|---|---|---|
| Replaced the implicit `busy || !wallet` return with explicit summary-card states | A wallet request failure or an interrupted response could leave the financial area visually blank and make the surrounding card/background appear broken | Wallet Center always renders a defined visual state: a white loading skeleton before the first response, a white unavailable/retry card after a failed summary request, or the verified live balance card after success |
| Preserved a verified wallet summary during refresh | Refreshing should not blank a previously received balance while new data is in flight | The existing summary remains visible with a small refreshing label until the new verified response arrives |
| Refused to estimate balance on a failed response | Financial UI must never substitute a placeholder amount for unavailable server data | The unavailable state explicitly states that no balance is estimated or displayed and offers a safe retry action |
| Added rendering regression coverage and release validation | Empty financial panels must not return in future UI changes | Full suite now contains 63 passing tests; dependency health, Mini App TypeScript, production build, and whitespace validation pass; no balance, payment, withdrawal, or database mutation was performed |

## 2026-08-26 — Admin Panel Operations Control Center

| Change | Reason | Effect |
|---|---|---|
| Rebuilt the shared admin shell as a white/blue TG FOX operations console | The existing panel was dark-first and presented a long ungrouped side navigation despite dozens of operational surfaces | All admin pages now inherit a clearer mobile-responsive shell with grouped navigation for command center, inventory/orders, payments/sellers, and system tools |
| Rebuilt the admin home page around real operational priorities | Existing dashboard mixed dense cards, charts, and tables without an explicit workflow hierarchy | The new command center provides routes into Session Accounts, Orders, Payments, and Seller Requests; displays only existing live stats/chart API responses and existing pending-work queues |
| Preserved existing workflow URLs and server behavior | UI work must not weaken access control or alter sensitive operational business logic | Inventory, order, payment, seller, users, logs, backups, and settings routes retain their existing endpoints; no financial/stock/seller state mutation was added or automatically triggered |
| Preserved authorization and CSRF enforcement | Admin actions remain consequential and need server-side security boundaries independent of the UI | Existing signed admin session validation and CSRF requirement for state-changing methods remain unchanged; the redesign adds no bypasses |
| Added regression and release validation | Shared shell changes can otherwise break security routes or hide real data flows | Full suite now contains 64 passing tests; Python syntax/lint, dependency health, Mini App TypeScript, production build, and whitespace validation pass; no destructive action, payment, withdrawal, database mutation, account operation, or notification was run |

## 2026-08-26 — Deep Admin Panel Connection Audit and Hardening

| Change | Reason | Effect |
|---|---|---|
| Mapped all rendered admin templates, route modules, navigation pages, API loaders, and mutation surfaces | A broad admin panel can silently accumulate disconnected pages or stale endpoint references | Regression coverage now verifies every non-base template is rendered by a route module, every admin route module is registered, and all 17 authenticated page paths exist in route source |
| Fixed client-side CSRF readiness race | A protected action clicked immediately after page load could run before asynchronous CSRF initialization finished | Non-GET helper calls now share and await CSRF loading; a missing secure session returns a safe retry failure before a mutation request is sent |
| Hardened shared client rendering and response parsing | Shared helpers are used by every admin page and must tolerate unexpected error shapes or untrusted display strings | Toasts use text nodes, unknown statuses are escaped, and non-JSON failed responses produce a safe consistent result object |
| Added explicit second confirmation and duplicate-submit lock for withdrawal approval | Payout approval is a high-consequence administrative action | The browser now requires a clear confirmation step and blocks rapid repeated clicks while the existing server approval route processes the request |
| Performed complete admin-package lint cleanup and validation | Dead imports obscure signal during operational code audits | All admin Python routes pass focused `F,E9` checks; the full suite now contains 67 passing tests and no production operation was invoked |

## 2026-08-26 — Live Admin Payments Reliability Fix

| Change | Reason | Effect |
|---|---|---|
| Diagnosed deployed Payments failure using authenticated read-only browser and live logs | Deposits page rendered a failed state and `NaN` count despite pending records | The real `500` came from a nested datetime at `extra.manual_chain_verification.checked_at` which the shallow serializer did not encode |
| Replaced shallow payment serialization with recursive JSON encoding | Provider/manual-chain evidence can contain nested datetimes and numeric types | Deposit, withdrawal, transaction, and export list paths now serialize nested evidence safely while removing only Mongo `_id` from the admin response |
| Corrected admin SSE subscriber lifecycle | Deployed logs showed stream “No response returned” errors | Event subscribers are now registered and removed inside the response generator lifetime rather than discarded immediately by an outer route `finally` |
| Hardened Payments list error states | A failed API response was treated as list data, producing a misleading `NaN` count | Deposits, withdrawals, and transactions now detect API failure payloads, reset their count, and render a clear retryable unavailable state |
| Performed live read-only page audit | Real data can expose failures not represented in test fixtures | Dashboard, Analytics, Sessions, Orders, Countries, Proxies, Sell Requests, Users, Settings, Tasks, BIN, Users Sell Stock, Sellers, Backup, Sales Feed, and Logs were navigated/read without invoking an operational action; full details are in `docs/LIVE_ADMIN_PANEL_AUDIT_2026-08-26.md` |
| Added focused regression coverage and release validation | Nested provider timestamps and SSE lifetimes must not regress | Full suite now contains 69 passing tests; admin syntax/lint, dependency health, Mini App TypeScript, production build, and whitespace validation pass |

## 2026-08-26 — Sales Feed Admin Visibility and Action Safety

| Change | Reason | Effect |
|---|---|---|
| Verified the deployed Sales Feed settings endpoint and DOM state in a read-only authenticated browser session | The compact configuration card appeared empty in browser annotation preview, creating the impression that feed data disappeared | The actual settings response, toggles, destination value, delay, and controls were present; the page is a configuration surface rather than a historical sales-post list |
| Added an explicit real-status summary | Configuration controls alone did not make delivery state and destination readiness visually obvious | The page now presents Delivery State, Destination readiness, and Publish Delay above editable settings using only loaded server values |
| Added explicit unavailable/retryable state | A failed settings read previously had no visible explanation and could leave the compact card ambiguous | Load failures now state that settings are temporarily unavailable and do not estimate delivery state or destination configuration |
| Replaced direct mutation fetches with shared admin helpers | Direct POST calls bypassed the shared CSRF/session/error handling used by the rest of the panel | Save and test requests now use the CSRF-aware admin client helper; the test notification additionally requires explicit browser confirmation before any message can be sent |
| Added regression coverage | Sales Feed status visibility and action safety need to remain intentional | Focused suite now contains 70 passing tests; no setting, Sales Feed configuration, or test notification was changed or sent during validation |

## 2026-08-26 — Sales Feed Real Completed-Purchase Activity

| Change | Reason | Effect |
|---|---|---|
| Rechecked the live deployed revision rather than assuming the prior fix had rolled out | The owner reported that the page still looked as though all feed content had disappeared | Live DOM confirmed the configuration summary was deployed and settings data was intact; the actual remaining product gap was the lack of visible recent feed-eligible activity |
| Added owner-protected read-only recent activity API | Sales Feed configuration needs operational context without manufacturing feed events | `/admin/api/sales-feed/recent` returns up to eight real completed orders only, with order, buyer, amount, fee, and completion time fields required for internal operational visibility |
| Added a Recent Completed Purchases panel | Settings alone can appear empty even when the Sales Feed is correctly configured | Administrators now see real completed-purchase activity and a clear no-real-sales-yet state directly beneath configuration; no synthetic sale, fake post, or delivery attempt is created |
| Completed regression and release validation | Read-only history and no-fake-sale rules must remain explicit | Full suite contains 70 passing tests; admin lint, dependency health, Mini App TypeScript, production build, and whitespace validation pass with no settings save or notification sent |

## Explicit Non-Changes

- No MongoDB collections or documents were deleted.
- No live payment or withdrawal was created.
- No user Telegram account/session was accessed.
- No production secret was written to documentation or source.
- No MongoDB collection or document was deleted; legacy fake-related fields may remain in documents but are not used for buyer-facing behavior.
- No live payment or withdrawal was created.
- No real on-chain transaction hash, user transaction record, wallet credit, or provider credential was used during automated verification tests.
