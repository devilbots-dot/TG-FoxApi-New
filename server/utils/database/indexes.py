"""
MongoDB index creation — called once at bot startup after reinit().

All indexes use background=True so they don't block the event loop
during creation on an already-populated collection.

Rule: every field used in find(), sort(), count_documents(), or aggregate
$match MUST have a supporting index.
"""

from pymongo import ASCENDING, DESCENDING
from server.core.mongo import collection
from server import LOGGER

_log = LOGGER(__name__)


async def ensure_indexes():
    """Create all production indexes. Safe to call multiple times (idempotent)."""
    try:
        # ── users ─────────────────────────────────────────────────────────
        await collection("users").create_index(
            [("user_id", ASCENDING)], unique=True, background=True, name="idx_user_id"
        )
        await collection("users").create_index(
            [("api_key", ASCENDING)], unique=True, sparse=True, background=True, name="idx_api_key"
        )
        await collection("users").create_index(
            [("referral_code", ASCENDING)], unique=True, sparse=True, background=True, name="idx_referral_code"
        )
        await collection("users").create_index(
            [("is_banned", ASCENDING)], background=True, name="idx_is_banned"
        )
        await collection("users").create_index(
            [("joined_at", DESCENDING)], background=True, name="idx_joined_at"
        )

        # ── session_accounts ──────────────────────────────────────────────
        # Primary buy-flow query: country + unsold + clean spam_status + oldest first.
        # Must include spam_status because get_unsold_session_for_country uses clean_only=True
        # and country_service now filters on spam_status="clean" for stock counts.
        await collection("session_accounts").create_index(
            [("country_code", ASCENDING), ("sold", ASCENDING), ("inventory_state", ASCENDING), ("spam_status", ASCENDING), ("uploaded_at", ASCENDING)],
            background=True,
            name="idx_country_unsold_clean_oldest_v2",
        )
        # The legacy index (country+sold+uploaded_at) is intentionally retained.
        # The versioned name avoids a same-name/different-key-spec conflict in
        # existing production databases and does not drop any user data or index.
        await collection("session_accounts").create_index(
            [("account_id", ASCENDING)], unique=True, background=True, name="idx_acc_id"
        )
        await collection("session_accounts").create_index(
            [("phone", ASCENDING)], unique=True, background=True, name="idx_phone"
        )
        # Global clean-stock aggregation: sold + spam_status + country_code
        await collection("session_accounts").create_index(
            [("sold", ASCENDING), ("inventory_state", ASCENDING), ("spam_status", ASCENDING), ("country_code", ASCENDING)],
            background=True, name="idx_sold_clean_country"
        )
        await collection("session_accounts").create_index(
            [("inventory_state", ASCENDING), ("bin_moved_at", DESCENDING)],
            background=True, name="idx_bin_moved_at"
        )
        await collection("session_accounts").create_index(
            [("inventory_state", ASCENDING), ("bin_expires_at", ASCENDING)],
            background=True, name="idx_bin_expires_at"
        )
        await collection("session_accounts").create_index(
            [("sold_to", ASCENDING)], sparse=True, background=True, name="idx_sold_to"
        )
        # Standalone uploaded_at DESC — get_all_session_accounts() (admin
        # listing) sorts by uploaded_at DESC with no filter in the common
        # case ("show recent uploads"), which the compound indexes above
        # (all ASC uploaded_at, always paired with country_code/sold/spam
        # filters) don't serve efficiently. Without this, an unfiltered
        # listing falls back to an in-memory sort over the whole collection.
        await collection("session_accounts").create_index(
            [("uploaded_at", DESCENDING)], background=True, name="idx_uploaded_at_desc"
        )

        # ── orders ────────────────────────────────────────────────────────
        await collection("orders").create_index(
            [("order_id", ASCENDING)], unique=True, background=True, name="idx_order_id"
        )
        await collection("orders").create_index(
            [("buyer_id", ASCENDING)], background=True, name="idx_buyer_id"
        )
        await collection("orders").create_index(
            [("seller_id", ASCENDING)], background=True, name="idx_seller_id"
        )
        await collection("orders").create_index(
            [("status", ASCENDING)], background=True, name="idx_order_status"
        )
        await collection("orders").create_index(
            [("created_at", DESCENDING)], background=True, name="idx_order_created_at"
        )
        await collection("orders").create_index(
            [("buyer_id", ASCENDING), ("order_type", ASCENDING), ("created_at", DESCENDING)],
            background=True, name="idx_order_buyer_type_date"
        )

        # ── countries ─────────────────────────────────────────────────────
        await collection("countries").create_index(
            [("code", ASCENDING)], unique=True, background=True, name="idx_country_code"
        )
        await collection("countries").create_index(
            [("country_rank", ASCENDING)], background=True, name="idx_country_rank"
        )
        await collection("countries").create_index(
            [("temp_disable", ASCENDING)], background=True, name="idx_temp_disable"
        )
        await collection("countries").create_index(
            [("is_full", ASCENDING)], background=True, name="idx_is_full"
        )

        # ── deposits ──────────────────────────────────────────────────────
        await collection("deposits").create_index(
            [("deposit_id", ASCENDING)], unique=True, background=True, name="idx_deposit_id"
        )
        await collection("deposits").create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True, name="idx_deposit_user_date"
        )
        await collection("deposits").create_index(
            [("status", ASCENDING)], background=True, name="idx_deposit_status"
        )
        await collection("deposits").create_index(
            [("created_at", DESCENDING)], background=True, name="idx_deposit_created"
        )
        # Compound index for expire_overdue_deposits() — queries status+expires_at
        await collection("deposits").create_index(
            [("status", ASCENDING), ("expires_at", ASCENDING)],
            background=True, name="idx_deposit_status_expires"
        )

        # ── withdrawals (legacy names — kept for backward-compat) ────────────
        # The "new comprehensive" block below re-creates these with idx_wit_* names.
        # Drop the old ones first so we don't get "same key, different name" errors.
        for _old in ("idx_withdrawal_id", "idx_withdrawal_user_date",
                     "idx_withdrawal_status", "idx_withdrawal_created"):
            try:
                await collection("withdrawals").drop_index(_old)
            except Exception:
                pass  # already absent or never existed

        # ── transactions ──────────────────────────────────────────────────
        await collection("transactions").create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True, name="idx_txn_user_date"
        )
        await collection("transactions").create_index(
            [("ref_id", ASCENDING)], sparse=True, background=True, name="idx_txn_ref_id"
        )
        await collection("transactions").create_index(
            [("type", ASCENDING)], background=True, name="idx_txn_type"
        )
        # Legacy stale index from an old schema version — a unique
        # (ref_id, type) index, "sparse" but ref_id is stored as an
        # explicit `None` (not omitted) for ref_id-less types like
        # "adjustment", so sparse does NOT exempt it. Every 2nd+ manual
        # balance adjustment therefore hit DuplicateKeyError. No current
        # code path wants this constraint — drop it defensively on every
        # startup in case an environment (e.g. production) still has it.
        try:
            await collection("transactions").drop_index("idx_txn_ref_type_unique")
        except Exception:
            pass  # already absent — nothing to do

        # ── sell_requests ─────────────────────────────────────────────────
        await collection("sell_requests").create_index(
            [("request_id", ASCENDING)], unique=True, background=True, name="idx_req_id"
        )
        await collection("sell_requests").create_index(
            [("user_id", ASCENDING)], background=True, name="idx_req_user"
        )
        await collection("sell_requests").create_index(
            [("status", ASCENDING)], background=True, name="idx_req_status"
        )
        # submitted_at is used for sorting in get_pending_sell_requests /
        # get_all_sell_requests — without this index those queries do a full scan.
        # Drop legacy conflicting index (different direction, same name) before
        # creating the correct DESCENDING version so the startup warning disappears.
        try:
            await collection("sell_requests").drop_index("idx_req_submitted_at")
        except Exception:
            pass
        await collection("sell_requests").create_index(
            [("submitted_at", DESCENDING)], background=True, name="idx_req_submitted_at"
        )
        # Phone — duplicate detection (check_phone_has_active_sell)
        await collection("sell_requests").create_index(
            [("phone", ASCENDING)], background=True, name="idx_req_phone"
        )
        # Rate-limit query: user_id + submitted_at rolling window
        await collection("sell_requests").create_index(
            [("user_id", ASCENDING), ("submitted_at", DESCENDING)],
            background=True, name="idx_req_user_submitted"
        )
        # Pending count per user: user_id + status
        await collection("sell_requests").create_index(
            [("user_id", ASCENDING), ("status", ASCENDING)],
            background=True, name="idx_req_user_status"
        )

        # ── admin_audit_logs ──────────────────────────────────────────────
        # ts is queried/sorted in every audit log listing — must be indexed.
        await collection("admin_audit_logs").create_index(
            [("ts", DESCENDING)], background=True, name="idx_audit_ts"
        )
        await collection("admin_audit_logs").create_index(
            [("category", ASCENDING), ("ts", DESCENDING)], background=True, name="idx_audit_cat_ts"
        )
        await collection("admin_audit_logs").create_index(
            [("actor", ASCENDING)], background=True, name="idx_audit_actor"
        )

        # ── pending_2fa ───────────────────────────────────────────────────
        # idx_p2fa_phone and idx_p2fa_created already exist in production;
        # idx_p2fa_unique covers (chat_id, phone, source) — we just add chat_id
        # standalone for per-chat lookups. Skip re-creating already-present indexes.
        await collection("pending_2fa").create_index(
            [("chat_id", ASCENDING)], background=True, name="idx_p2fa_chat_id"
        )

        # ── users_sell_stock ──────────────────────────────────────────────
        # sell_id is the join key (upsert lookup) — must be unique
        await collection("users_sell_stock").create_index(
            [("sell_id", ASCENDING)], unique=True, background=True, name="idx_uss_sell_id"
        )
        # stock_id is the primary key used in admin actions
        await collection("users_sell_stock").create_index(
            [("stock_id", ASCENDING)], unique=True, background=True, name="idx_uss_stock_id"
        )
        # transferred + received_at — main admin panel listing (pending queue)
        await collection("users_sell_stock").create_index(
            [("transferred", ASCENDING), ("received_at", DESCENDING)],
            background=True, name="idx_uss_transferred_received"
        )
        # user_id — per-seller lookup
        await collection("users_sell_stock").create_index(
            [("user_id", ASCENDING)], background=True, name="idx_uss_user_id"
        )
        # Compound filter index: transferred + country_code (used by bulk transfer/approve)
        await collection("users_sell_stock").create_index(
            [("transferred", ASCENDING), ("country_code", ASCENDING)],
            background=True, name="idx_uss_transferred_country"
        )
        # payment_status filter (approve/reject flows)
        await collection("users_sell_stock").create_index(
            [("transferred", ASCENDING), ("payment_status", ASCENDING)],
            background=True, name="idx_uss_transferred_pay_status"
        )
        # session_status filter (Check Status + Send to Inventory filters)
        await collection("users_sell_stock").create_index(
            [("transferred", ASCENDING), ("session_status", ASCENDING)],
            background=True, name="idx_uss_transferred_sess_status"
        )

        # ── accounts (legacy P2P) ─────────────────────────────────────────
        await collection("accounts").create_index(
            [("account_id", ASCENDING)], unique=True, background=True, name="idx_p2p_acc_id"
        )
        await collection("accounts").create_index(
            [("country_code", ASCENDING), ("status", ASCENDING)],
            background=True, name="idx_p2p_country_status"
        )

        # ── proxies ───────────────────────────────────────────────────────
        await collection("proxies").create_index(
            [("proxy_id", ASCENDING)], unique=True, background=True, name="idx_proxy_id"
        )
        await collection("proxies").create_index(
            [("country_code", ASCENDING), ("is_active", ASCENDING), ("fail_count", ASCENDING)],
            background=True, name="idx_proxy_country_active"
        )

        # ── notifications ─────────────────────────────────────────────────────
        await collection("notifications").create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True, name="idx_notif_user_date"
        )
        await collection("notifications").create_index(
            [("created_at", DESCENDING)], background=True, name="idx_notif_created"
        )

        # ── webhooks ──────────────────────────────────────────────────────────
        await collection("webhooks").create_index(
            [("webhook_id", ASCENDING)], unique=True, background=True, name="idx_webhook_id"
        )
        await collection("webhooks").create_index(
            [("user_id", ASCENDING)], background=True, name="idx_webhook_user"
        )

        # ── background_task_logs ──────────────────────────────────────────────
        await collection("background_task_logs").create_index(
            [("worker_name", ASCENDING), ("started_at", DESCENDING)],
            background=True, name="idx_btl_worker_started"
        )

        # ── withdrawals ───────────────────────────────────────────────────────
        await collection("withdrawals").create_index(
            [("withdrawal_id", ASCENDING)], unique=True, background=True, name="idx_wit_id"
        )
        await collection("withdrawals").create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            background=True, name="idx_wit_user_created"
        )
        await collection("withdrawals").create_index(
            [("status", ASCENDING), ("created_at", ASCENDING)],
            background=True, name="idx_wit_status_created"
        )
        await collection("withdrawals").create_index(
            [("gateway_track_id", ASCENDING)],
            sparse=True, background=True, name="idx_wit_track_id"
        )
        await collection("withdrawals").create_index(
            [("user_id", ASCENDING), ("status", ASCENDING)],
            background=True, name="idx_wit_user_status"
        )
        await collection("withdrawals").create_index(
            [("user_id", ASCENDING), ("status", ASCENDING), ("completed_at", DESCENDING)],
            background=True, name="idx_wit_user_status_completed"
        )

        # ── withdrawal_logs ───────────────────────────────────────────────────
        await collection("withdrawal_logs").create_index(
            [("withdrawal_id", ASCENDING), ("created_at", ASCENDING)],
            background=True, name="idx_wlog_wit_created"
        )
        await collection("withdrawal_logs").create_index(
            [("log_id", ASCENDING)], unique=True, background=True, name="idx_wlog_id"
        )

        # ── gateway_requests / gateway_responses ──────────────────────────────
        await collection("gateway_requests").create_index(
            [("withdrawal_id", ASCENDING)], background=True, name="idx_gwreq_wit_id"
        )
        await collection("gateway_responses").create_index(
            [("withdrawal_id", ASCENDING)], background=True, name="idx_gwresp_wit_id"
        )

        _log.info("MongoDB indexes verified/created.")

    except Exception as exc:
        # Non-fatal — indexes improve performance but app can still run without them
        _log.warning("Index creation warning (non-fatal): %s", exc)
