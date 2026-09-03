"""
Formal state list for a single account's journey through the stock-upload
pipeline, plus the two "waiting on admin input" pause states. Persisted
per-account (server/stock/state_store.py) so every account always has an
exact, inspectable stage and timeline — not just an in-memory outcome.

Order matters: RESUMABLE_ORDER is used by state_store to decide, on bot
restart, whether an account's original uploaded session is still safe to
reuse (anything before FINAL_SESSION_UPLOADED) or whether the already
-finalized session must be recovered from its checkpoint instead of
re-running Telegram calls (FINAL_SESSION_UPLOADED itself).
"""

UPLOADED                    = "UPLOADED"
COUNTRY_DETECTED            = "COUNTRY_DETECTED"
CONNECTED                   = "CONNECTED"
VALIDATED                   = "VALIDATED"
SPAM_CHECKED                = "SPAM_CHECKED"
TWOFA_VERIFIED              = "2FA_VERIFIED"
TWOFA_APPLIED               = "2FA_APPLIED"
OTHER_SESSIONS_TERMINATED   = "OTHER_SESSIONS_TERMINATED"
NEW_SESSION_CREATED         = "NEW_SESSION_CREATED"
FINAL_SESSION_UPLOADED      = "FINAL_SESSION_UPLOADED"
COMPLETED                   = "COMPLETED"
FAILED                      = "FAILED"

# Pause states — waiting on a human, not a crash. Never auto-retried;
# surfaced via /pending_2fa, /pending_country and the tap-to-answer buttons.
NEEDS_2FA                   = "NEEDS_2FA"
NEEDS_COUNTRY                = "NEEDS_COUNTRY"

# Terminal states — an account here is done, one way or another.
TERMINAL_STATES = {COMPLETED, FAILED}

# Every state before FINAL_SESSION_UPLOADED happens strictly before the
# uploaded session is asked to log itself out — so at any of those stages
# the original uploaded session is still fully intact and it is always safe
# to just restart the whole pipeline for that account from scratch.
RESUMABLE_FROM_SCRATCH = {
    UPLOADED, COUNTRY_DETECTED, CONNECTED, VALIDATED, SPAM_CHECKED,
    TWOFA_VERIFIED, TWOFA_APPLIED, OTHER_SESSIONS_TERMINATED, NEW_SESSION_CREATED,
    NEEDS_2FA, NEEDS_COUNTRY,
}

ORDER = [
    UPLOADED, COUNTRY_DETECTED, CONNECTED, VALIDATED, SPAM_CHECKED,
    TWOFA_VERIFIED, TWOFA_APPLIED, OTHER_SESSIONS_TERMINATED, NEW_SESSION_CREATED,
    FINAL_SESSION_UPLOADED, COMPLETED,
]
