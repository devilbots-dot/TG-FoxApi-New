"""
Stock processing — everything that happens after an admin uploads a .zip
or .session file, up until it becomes sellable stock (or is set aside for
admin input, or is discarded as invalid).

This package is the single home for that pipeline. Telegram handlers
(server/plugins/bot/session_admin.py) and the REST admin API
(server/api/routes/admin.py) are thin callers of it — they format output
for their own surface, but never re-implement any of the steps below.

Modules, in roughly the order a session moves through them:
  zip_extraction      — parsing admin-uploaded .zip archives into raw
                         sessions + matched password/API credentials
  country_detection   — phone number -> proxy country
  session_validation  — auth check, reading account identity, duplicate detection
  two_factor          — 2FA status inspection + enable/disable/rotate
  spam_check          — @SpamBot status + frozen/restricted classification
  otp_capture         — reusable "wait for the 777000 login code" listener
  session_generation  — fresh login (request code, capture OTP, sign in)
  authorization       — terminating every other active session
  persistence         — uploading the final session + saving its DB record
  pipeline            — process_uploaded_session(): the per-account orchestrator
  pending_state       — generic store for accounts paused on admin input
  batch_runner        — concurrent processing of a whole upload batch

Telethon connection primitives (proxy building, temp session files) and
storage-channel I/O live one level up, in server/utils/sessions/ — they
are reused outside the stock pipeline too (buyer-delivery OTP flow), so
they are not stock-specific.
"""
