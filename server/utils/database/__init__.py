from .userdb import (
    # Registration
    is_served_user,
    add_served_user,
    get_served_users,
    get_user,
    get_total_users,
    get_user_stats,
    # Language
    get_user_lang,
    set_user_lang,
    # Balance & Wallet
    get_balance,
    get_reserve_balance,
    get_wallet_snapshot,
    update_balance,
    set_balance,
    deduct_balance_atomic,
    reserve_earned_balance_atomic,
    reserve_balance_atomic,
    finalize_reserved_balance,
    release_reserved_balance,
    record_deposit,
    record_purchase,
    record_sale,
    # Pending balance (sell flow)
    get_pending_balance,
    credit_pending_balance,
    move_pending_to_available,
    clear_pending_balance,
    # Wallet addresses
    get_wallet_addresses,
    set_wallet_address,
    SUPPORTED_NETWORKS,
    # Rank
    get_rank,
    set_rank,
    # API Key
    get_api_key,
    regenerate_api_key,
    get_user_by_api_key,
    set_api_access,
    # Referral
    get_referral_code,
    get_user_by_referral,
    apply_referral,
    get_referral_stats,
    # Settings
    set_referral_notifications,
    set_disable_password,
    # 2FA
    enable_2fa,
    disable_2fa,
    get_2fa_secret,
    # Ban
    is_banned_user,
    is_banned,
    add_banned_user,
    remove_banned_user,
    get_banned_users,
    get_banned_count,
    get_gbanned,
    # Sudo
    get_sudoers,
    add_sudo,
    remove_sudo,
)

from .orderdb import (
    create_order,
    complete_order,
    cancel_order,
    dispute_order,
    refund_order,
    get_order,
    get_buyer_orders,
    get_seller_orders,
    get_all_orders,
    count_orders,
    get_total_volume,
    get_total_fees_collected,
    PLATFORM_FEE_PERCENT,
)

from .walletdb import (
    # Deposits
    create_deposit_v2,
    confirm_deposit,
    reject_deposit,
    expire_overdue_deposits,
    get_deposit,
    get_user_deposits,
    get_user_deposits_paginated,
    count_user_deposits,
    get_pending_deposits,
    DEPOSIT_STATUSES,
    # Withdrawals
    create_withdrawal,
    claim_withdrawal_for_processing,
    complete_withdrawal,
    revert_withdrawal_to_pending,
    approve_withdrawal,
    reject_withdrawal,
    get_withdrawal,
    get_user_withdrawals,
    get_pending_withdrawals,
    # Transactions
    log_transaction,
    get_user_transactions,
    get_all_transactions,
    VALID_TXN_TYPES,
)

from .countrydb import (
    get_all_countries,
    get_country,
    upsert_country,
    set_country_field,
    country_exists,
    get_country_price,
    delete_country,
    delete_all_countries,
    get_next_rank,
)

from .sellrequestdb import (
    create_sell_request,
    get_sell_request,
    get_user_sell_requests,
    get_pending_sell_requests,
    get_all_sell_requests,
    approve_sell_request,
    reject_sell_request,
    count_pending_sell_requests,
    check_phone_has_active_sell,
    check_phone_in_stock,
    get_user_recent_sell_count,
    get_user_pending_sell_count,
    SELL_RATE_LIMIT_HOURS,
    SELL_RATE_LIMIT_MAX,
    SELL_MAX_PENDING,
    # Extended lifecycle
    update_sell_request_status,
    get_pending_termination_due,
    increment_retry_count,
    get_payment_release_due,
    mark_payment_released,
)

from .sessiondb import (
    add_session_account,
    get_session_account,
    get_unsold_session_for_country,
    mark_session_sold,
    delete_session_account,
    get_all_session_accounts,
    count_unsold_by_country,
    get_session_stats,
)

from .proxydb import (
    add_proxy,
    get_active_proxy_for_country,
    get_proxies_for_country,
    list_all_proxies,
    delete_proxy,
    toggle_proxy,
    increment_proxy_fail,
    reset_proxy_fails,
)


from .configdb import (
    get_setting,
    get_all_settings,
    set_setting,
)
