# Live Admin Panel Read-Only Audit

**Date:** 2026-08-26
**Target:** Deployed TG FOX Admin Panel
**Safety boundary:** Browser login and read-only page/API inspection only. No approval, rejection, verification, retry, export, notification, backup/restore, balance, stock, or account action was invoked.

## Confirmed Live Findings

| Surface | Observed behavior | Root cause or result |
|---|---|---|
| Login and dashboard | Authenticated admin session opened successfully; dashboard stats, charts, and navigation rendered after loading | Dashboard base read path is operational; live counters are server-sourced |
| Payments — Deposits tab | Initial request ended in a failed empty state with a `NaN` counter | Live logs identified a `500`: a nested `checked_at` timestamp inside `extra.manual_chain_verification` was not JSON serializable by the shallow deposit serializer |
| Live Events stream | Deployed logs contained repeated `No response returned` errors for `/admin/api/events/stream` | Subscriber was discarded by an outer route `finally` before the streaming generator lifetime; corrected by moving queue registration/removal into the generator lifetime |
| Payments error presentation | Failed list payload was treated as ordinary list data, causing the visible `NaN` count | Client now detects `ok === false`, clears the count, and renders a clear retryable unavailable state |
| Analytics | Live chart/rank API data rendered successfully after initial loading | The empty sales-history panel currently says only “No data”; a future UI pass can replace empty chart canvases with a clearer explanatory empty state, but no loading failure was observed |
| Session Accounts | Filter controls and inventory list loaded successfully from the live server | Read-only inventory browsing worked; action buttons were intentionally not opened or invoked |
| Orders | Filter controls, real order rows, status badges, and pagination loaded successfully | Read-only order management data path is operational; no order action was opened or invoked |
| Countries & Pricing | Country rows, verified stock counts, buy/sell prices, status labels, and explanatory copy loaded successfully | Real country configuration read path is operational; pricing and stock controls were not opened or invoked |
| Proxies | Proxy inventory and active-status data loaded successfully | Read-only proxy health presentation is operational; test, toggle, add, universal assignment, and delete controls were not invoked |
| Sell Requests | Seller request filters and an accurate empty state loaded successfully | No approval, rejection, validation, or seller payout workflow action was invoked |
| Users | User list, real balances/ranks/statuses, filters, and pagination loaded successfully | The dense raw display-name column can reduce scanability; no balance, rank, ban, notification, or access control action was opened or invoked |
| Settings | Feature toggles, masked environment-variable status, sell timing, worker thresholds, and deposit-method configuration loaded successfully | The page is data-dense on desktop and would benefit from progressive disclosure/section anchors; no setting, secret, restart, or toggle was changed |
| Background Tasks | Worker status, linked timing values, intervals, and trigger controls loaded successfully | The task rows are operational but visually crowded; no interval, reset, or trigger action was invoked |
| BIN Sessions | Initial page load showed “Failed to load BIN sessions,” while the read-only endpoint returned a valid empty paginated list | A safe in-page read-only reload rendered “No BIN sessions found”; the live failure was transient rather than a persistent response-shape mismatch. Restore/delete actions were not invoked |
| Users Sell Stock | Seller-upload inventory filters and an accurate empty state loaded successfully | Bulk action controls are visible and understandable but visually dense; no status check, approval, rejection, or transfer was invoked |
| Sellers | Seller profile page renders a targeted Telegram User ID lookup form successfully | This is intentionally not a general seller list; no profile lookup was submitted |
| Backup & Restore | Backup/restore page renders clear export/import boundaries and destructive restore warning | No file export, upload, restore, or deletion was invoked |
| Sales Feed | Feed configuration and real-purchases-only policy rendered successfully | Rendered field labels and controls have visible dark-on-white contrast; the browser annotation preview makes the compact card appear visually sparser than its actual DOM state. No feed setting was saved and no test notification was sent |

## Verified Local Fixes Pending Deployment

The deposit serializer now uses recursive JSON encoding for nested verification evidence and numeric values. The SSE generator owns its subscription lifecycle for the complete response stream. Payments list tabs explicitly distinguish error payloads from list responses.

## Deployment Recheck Note

Immediately after the GitHub push, the deployed Payments page still rendered the previous `NaN`/failed state. This indicates the hosted instance had not yet picked up the new commit at the time of the read-only check; it does not alter the local regression result. A browser refresh after Render completes its automatic deployment is required to verify the live fix.

## Sales Feed Follow-Up

The page is not missing its controls or its read-only settings loader. The deployed script calls `/admin/api/sales-feed/settings`; the parent `/admin/api/sales-feed` route is intentionally absent and returns `404`.

The actual settings endpoint returned a successful live configuration response. The enabled toggle, silent toggle, destination field, delay field, and save button were present with normal visible geometry and dark-on-white contrast. The browser annotation preview made the compact configuration surface look much emptier than the rendered DOM state. The page is a **configuration page**, not a published-sales history page, so it intentionally does not list historical feed posts.

## Final Sales Feed Repair

The deployed repair marker was confirmed in the Sales Feed DOM: the live page displayed the new delivery state, destination readiness, and publish delay summary values. The remaining product gap was not a missing setting; it was the absence of a clear completed-purchase activity surface, which made the page look like the actual feed had disappeared.

The final repair adds an authenticated, read-only `/admin/api/sales-feed/recent` endpoint that reads only real `completed` orders and exposes a short operational history to the Sales Feed page. The page now differentiates its two responsibilities: current configuration and the recent real purchases eligible for feed visibility. It does not create, simulate, publish, retry, or modify any sales-feed item.

### Deployment Recheck Status

Immediately after the `5edd629` GitHub push, two cache-busted, authenticated GET checks still returned the preceding deployed Sales Feed template: the configuration summary was visible, but the new **Recent Completed Purchases** card was not yet present. This is recorded as a pending Render rollout rather than a successful live deployment confirmation. No configuration save, test notification, or other state-changing control was used during the recheck.

After the automatic rollout settled, a further cache-busted authenticated GET check rendered the **Recent Completed Purchases** card with a truthful `0 recent` empty state. A separate browser-session GET request to `/admin/api/sales-feed/recent` returned HTTP `200` and `{ "items": [] }`, confirming the empty state came from the deployed real completed-orders query. No order, setting, notification, or other production data was changed.

During the owner's subsequent live browser demonstration, the same deployed page again loaded the card after its brief loading state and rendered `0 recent` with the message that only real completed orders appear. This was another read-only page load; no action control was selected.

> No live database content, transaction hash, credential, or record identifier is included in this audit document.
