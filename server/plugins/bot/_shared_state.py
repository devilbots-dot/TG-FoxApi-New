"""
Shared in-memory state for bot plugins.
Imported by wallet.py, market.py, start.py, sell_account.py, and sell_session.py
so that /cancel can clear all pending states from one place.
"""

wallet_state: dict[int, dict] = {}
market_pending: dict[int, dict] = {}

# Legacy sell flow (country grid → upload .session) — no longer triggered from
# home screen but kept so cancel / existing state clears cleanly.
sell_state: dict[int, dict] = {}

# New Sell Account flow (phone + OTP + 2FA → live login):
#   user_id → {
#     "step":             "await_phone" | "await_otp" | "await_2fa",
#     "phone":            str | None,
#     "country_code":     str | None,
#     "phone_code_hash":  str | None,
#     "client":           TelegramClient | None,   # live Telethon client
#     "tmp_path":         str | None,              # temp .session file path
#     "current_2fa":      str | None,              # seller's CURRENT 2FA (for rotation)
#     "otp_retries":      int,
#     "progress_msg_id":  int | None,
#   }
sell_account_state: dict[int, dict] = {}

# New Sell Session flow (upload ZIP / .session → admin pipeline):
#   user_id → {
#     "step":             "await_file" | "await_2fa",
#     "session_bytes":    bytes | None,            # stored if 2FA required
#     "progress_msg_id":  int | None,
#   }
sell_session_state: dict[int, dict] = {}

buy_auth_state: dict[int, dict] = {}  # buy-account handover flow: user_id → {"phone": ..., "order_id": ..., "old_client": ..., "otp_delivered": bool, ...}
buy_delivered_state: dict[str, dict] = {}  # order_id → same shape, once credentials are delivered
buy_session_pending: dict[int, bool] = {}  # buy-session direct-delivery flow: user_id → True while running
buy_search_pending: dict[int, dict] = {}  # buy search flow: user_id → {"mode": "account"|"session", "chat_id": int, "prompt_msg_id": int}


def clear_pending_flows(uid: int, *, keep: str | None = None) -> None:
    """
    Drop any in-progress conversational state for `uid`.
    Called whenever the user navigates away (Back to Home) or switches
    into a different flow, so a later plain-text message is not
    misinterpreted (e.g. TRC20 address treated as a phone number, or
    random chatter triggering an "Invalid phone number" reply).
    """
    buckets = {
        "wallet":       wallet_state,
        "sell_account": sell_account_state,
        "sell_session": sell_session_state,
        "market":       market_pending,
        "sell":         sell_state,
        "buy_auth":     buy_auth_state,
        "buy_search":   buy_search_pending,
    }
    for name, bucket in buckets.items():
        if name == keep:
            continue
        bucket.pop(uid, None)
