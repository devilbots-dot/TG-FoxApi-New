"""
process_uploaded_session(): the per-account orchestrator for the admin
stock-upload pipeline — the single place that sequences every step from
"here's an uploaded .session file" to "here's the final, verified session
ready to be stored". See server/stock/__init__.py for the full stage list.

Flow:
  1.  Select best proxy for country (country -> wildcard -> direct).
  2.  Connect the UPLOADED session via Telethon (proxy-aware). This
      connection stays open for the ENTIRE pipeline — never reconnected.
  3.  Auth check + read account info + 2FA status.
        - 2FA exists, no password given -> needs_admin_password=True (pause)
  4.  Spam / freeze check via @SpamBot on the uploaded client.
        - "frozen" -> short-circuit, nothing else done.
        - "temporary_spam" / "permanent_spam" -> recorded as metadata, pipeline continues.
  5.  Apply the batch's 2FA decision on the UPLOADED client (same connection).
  6.  Terminate ALL other active authorizations from the UPLOADED client.
      The uploaded session itself is still connected after this step —
      it terminates everyone ELSE, not itself.
  7.  Generate a brand-new fresh session while the uploaded client is still live:
        a. Register an OTP listener on the uploaded client.
        b. Connect a blank new Telethon client (same proxy).
        c. Request a login code for the phone number.
        d. Capture the OTP that arrives on the uploaded client.
        e. Sign the new client in (+ current 2FA password if needed).
      On any failure -> FALLBACK: the uploaded session itself becomes final.
  8.  Disconnect the new client -> flush SQLite -> read its session bytes.
  9.  Self-logout the uploaded client (removes it from the account completely).
 10.  Delete all temp files.

Net result:
  • Success path  — exactly ONE session survives: the brand-new one.
  • Fallback path — exactly ONE session survives: the uploaded one
                    (used_fallback=True).
"""

import asyncio
import time
from typing import Optional

from telethon import TelegramClient
from telethon.errors import AuthKeyUnregisteredError, UserDeactivatedBanError, UserDeactivatedError

from server import LOGGER
from server.stock.authorization import (
    list_other_authorizations,
    terminate_other_sessions,
)
from server.stock.otp_capture import start_login_otp_listener, wait_for_login_otp
from server.stock.session_generation import request_login_code, sign_in_new_client
from server.stock.session_validation import read_account_info, validate_session_authorized
from server.stock.spam_check import check_spam_status
from server.stock.two_factor import WrongTwoFactorPassword, apply_batch_2fa_decision, get_2fa_status
from server.utils.sessions.telethon_client import (
    build_telethon_proxy,
    cleanup_session_files,
    connect_with_proxy_fallback,
    new_temp_session_path,
    read_session_bytes,
    write_session_bytes,
)

_log = LOGGER(__name__)

# Spam/limit statuses that must NOT, on their own, stop any pipeline step.
_NON_BLOCKING_SPAM_STATUSES = {"temporary_spam", "permanent_spam"}

# Terminal outcome classifications for admin-facing reporting.
OUTCOME_SUCCESS          = "success"
OUTCOME_PARTIAL_SUCCESS  = "partial_success"
OUTCOME_SPAM_RESTRICTED  = "spam_restricted"
OUTCOME_FROZEN           = "frozen"
OUTCOME_BANNED           = "banned"
OUTCOME_INVALID_SESSION  = "invalid_session"
OUTCOME_RPC_ERROR        = "rpc_error"
OUTCOME_NETWORK_ERROR    = "network_error"
OUTCOME_TWOFA_FAILED     = "twofa_failed"
OUTCOME_UPLOAD_FAILED    = "upload_failed"


def _blank_pipeline_result(api_id: int) -> dict:
    return {
        "success":                False,
        "phone":                  None,
        "user_id":                None,
        "username":               None,
        "first_name":             None,
        "has_2fa":                False,
        "tfa_updated":            False,
        "tfa_apply_failed":       False,
        "tfa_password_enc":       "",
        "spam_status":            "unknown",
        "fresh_session_bytes":    None,
        "used_fallback":          False,
        "proxy_used":             "none",
        "terminated_others":      False,
        "termination_incomplete": False,
        "api_id_used":            api_id,
        "needs_admin_password":   False,
        "tfa_password_wrong":     False,
        "frozen":                 False,
        "permanent_spam":         False,
        "banned":                 False,
        "outcome":                None,
        "stage_timings":          {},
        "total_time":             0.0,
        "error":                  None,
    }


def _classify_outcome(result: dict) -> str:
    """
    Compute the final terminal classification from everything the pipeline
    observed. Only called for TERMINAL results (never for pause states).
    """
    if result.get("banned"):
        return OUTCOME_BANNED
    if result.get("frozen"):
        return OUTCOME_FROZEN
    if not result.get("success"):
        return result.get("outcome") or OUTCOME_RPC_ERROR
    if result.get("used_fallback") or result.get("termination_incomplete") or result.get("tfa_apply_failed"):
        return OUTCOME_PARTIAL_SUCCESS
    if result.get("spam_status") in _NON_BLOCKING_SPAM_STATUSES:
        return OUTCOME_SPAM_RESTRICTED
    return OUTCOME_SUCCESS


async def process_uploaded_session(
    session_bytes: bytes,
    api_id: int,
    api_hash: str,
    country_code: str,
    admin_2fa_password: str = "",
    otp_timeout: int = 120,
    old_api_id: Optional[int] = None,
    old_api_hash: Optional[str] = None,
    batch_action: str = "disable",
    batch_new_password: Optional[str] = None,
) -> dict:
    """
    Production-grade pipeline for a single admin-uploaded .session file.
    See module docstring for the full stage-by-stage description.

    Result keys:
      success              bool
      phone                str | None
      user_id              int | None
      username             str | None
      first_name           str | None
      has_2fa              bool   (original state before our update)
      tfa_updated          bool
      tfa_apply_failed     bool
      tfa_password_enc     str    (Fernet-encrypted new password; empty if not changed)
      spam_status          str    ("clean" | "temporary_spam" | "permanent_spam" | "frozen" | "unknown")
      fresh_session_bytes  bytes | None
      used_fallback        bool
      proxy_used           str    (proxy_id | "direct" | "none")
      terminated_others    bool
      termination_incomplete bool
      api_id_used          int
      needs_admin_password bool
      tfa_password_wrong   bool
      frozen               bool
      permanent_spam       bool
      banned               bool
      outcome              str | None
      stage_timings        dict[str, float]
      total_time           float
      error                str | None
    """
    result = _blank_pipeline_result(api_id)
    timings: dict[str, float] = {}
    pipeline_start = time.monotonic()

    old_tmp_path = new_temp_session_path()
    new_tmp_path = new_temp_session_path()
    old_client   = None
    new_client   = None

    def _finish(outcome: Optional[str] = None) -> dict:
        result["stage_timings"] = timings
        result["total_time"]    = round(time.monotonic() - pipeline_start, 3)
        if outcome is not None:
            result["outcome"] = outcome
        elif not result.get("needs_admin_password") and not result.get("tfa_password_wrong"):
            result["outcome"] = _classify_outcome(result)
        return result

    try:
        await write_session_bytes(old_tmp_path, session_bytes)
        old_session_path = old_tmp_path[:-8]
        new_session_path = new_tmp_path[:-8]

        # ── Step 1-2: proxy-aware connect (UPLOADED session) ─────────────────
        # Use the account's own api_id/api_hash when provided (JSON override)
        # so the existing session key is not invalidated by a credentials mismatch.
        connect_api_id   = old_api_id or api_id
        connect_api_hash = old_api_hash or api_hash

        t0 = time.monotonic()
        try:
            old_client, proxy_used, proxy_doc_used = await connect_with_proxy_fallback(
                old_session_path, connect_api_id, connect_api_hash, country_code
            )
            result["proxy_used"] = proxy_used
        except OSError as exc:
            result["error"] = f"All connection attempts failed: {exc}"
            return _finish(OUTCOME_NETWORK_ERROR)
        finally:
            timings["connect_old"] = round(time.monotonic() - t0, 3)

        try:
            # ── Step 3: auth check + account info + 2FA status ───────────────
            t0 = time.monotonic()
            try:
                if not await validate_session_authorized(old_client):
                    result["error"] = "Session not authorized (expired or invalid)"
                    return _finish(OUTCOME_INVALID_SESSION)

                info = await read_account_info(old_client)
                result.update(info)
                phone = info["phone"]
                if not phone:
                    result["error"] = "Could not read phone number from uploaded session"
                    return _finish(OUTCOME_INVALID_SESSION)

            except UserDeactivatedBanError as exc:
                result["banned"] = True
                result["error"]  = f"Account is banned by Telegram: {exc}"
                return _finish(OUTCOME_BANNED)
            except (UserDeactivatedError, AuthKeyUnregisteredError) as exc:
                result["error"] = f"Account deleted or authorization revoked: {exc}"
                return _finish(OUTCOME_INVALID_SESSION)
            finally:
                timings["validate_and_info"] = round(time.monotonic() - t0, 3)

            t0 = time.monotonic()
            original_has_2fa = await get_2fa_status(old_client)
            timings["2fa_status"] = round(time.monotonic() - t0, 3)
            result["has_2fa"] = original_has_2fa

            if original_has_2fa and not admin_2fa_password:
                result["needs_admin_password"] = True
                return _finish()  # pause — admin must provide current password

            # ── Step 4: spam / freeze check on UPLOADED client ───────────────
            t0 = time.monotonic()
            spam_result = await check_spam_status(old_client)
            timings["spam_check"] = round(time.monotonic() - t0, 3)

            result["spam_status"] = spam_result if not isinstance(spam_result, Exception) else "unknown"
            if isinstance(spam_result, Exception):
                _log.warning("Spam check raised unexpectedly for %s: %s", phone, spam_result)

            # Spam status — recorded as metadata only. NO status skips any step.
            # frozen / permanent_spam / temporary_spam are all just labels;
            # the pipeline continues regardless. Only an actual RPC rejection
            # from Telegram stops a specific step.
            if result["spam_status"] == "frozen":
                result["frozen"] = True
                _log.info(
                    "Account %s is frozen — pipeline continuing anyway (spam status "
                    "does not skip any step).", phone,
                )

            if result["spam_status"] == "permanent_spam":
                result["permanent_spam"] = True
                _log.info(
                    "Account %s has a permanent spam restriction — continuing with the "
                    "full pipeline (spam status alone does not block any step).", phone,
                )

            # ── Step 5: apply the batch's 2FA decision on UPLOADED client ────
            # Done here, BEFORE termination and fresh session, so 2FA is already
            # in the desired state when the new session is created.
            t0 = time.monotonic()
            try:
                result.update(await apply_batch_2fa_decision(
                    old_client, phone, original_has_2fa, admin_2fa_password,
                    batch_action, batch_new_password,
                ))
            except WrongTwoFactorPassword:
                result["tfa_password_wrong"] = True
                return _finish()  # wrong current password — pause
            finally:
                timings["apply_2fa"] = round(time.monotonic() - t0, 3)

            # ── Step 6: terminate ALL other active authorizations ─────────────
            # The UPLOADED client is still live. It terminates every OTHER
            # session (other devices, other logins) but NOT itself — so this
            # connection stays open for the OTP step that follows.
            # After this step only the uploaded session is active on the account.
            t0 = time.monotonic()
            try:
                others = await list_other_authorizations(old_client)
                result.update(await terminate_other_sessions(old_client, others, phone))
            except Exception as exc:
                result["termination_incomplete"] = True
                _log.warning("Could not check/terminate other sessions for %s: %s", phone, exc)
            timings["terminate_others"] = round(time.monotonic() - t0, 3)

            # ── Step 7: generate a brand-new fresh session ────────────────────
            # The uploaded client is STILL connected (self-logout comes last).
            # OTP from Telegram arrives on THIS uploaded session — which is why
            # we keep it live until the new session is successfully signed in.
            proxy_tuple = build_telethon_proxy(proxy_doc_used) if proxy_doc_used else None
            new_client_kwargs: dict = {"api_id": api_id, "api_hash": api_hash}
            if proxy_tuple:
                new_client_kwargs["proxy"] = proxy_tuple
            new_client = TelegramClient(new_session_path, **new_client_kwargs)

            fresh_ok       = True
            fallback_error = None

            t0 = time.monotonic()
            try:
                await new_client.connect()

                otp_future = start_login_otp_listener(old_client)

                try:
                    sent = await request_login_code(new_client, phone)
                except Exception as exc:
                    raise RuntimeError(f"send_code_request failed: {exc}")

                otp = await wait_for_login_otp(otp_future, otp_timeout)
                if otp is None:
                    raise RuntimeError("Timed out waiting for login code from Telegram")

                outcome, sign_in_exc = await sign_in_new_client(
                    new_client, phone, otp, sent.phone_code_hash,
                    admin_2fa_password, skip_empty_password_check=True,
                )
                if outcome == "password_rejected":
                    exc_name = type(sign_in_exc).__name__
                    if "PasswordHashInvalid" in exc_name or "PASSWORD_HASH_INVALID" in str(sign_in_exc):
                        result["tfa_password_wrong"] = True
                        return _finish()  # wrong password — pause, not a fallback
                    raise RuntimeError(f"2FA sign-in failed: {sign_in_exc}")
                elif outcome != "ok":
                    raise RuntimeError(f"sign_in failed: {sign_in_exc}")

            except Exception as exc:
                fresh_ok       = False
                fallback_error = str(exc)
                try:
                    await new_client.disconnect()
                except Exception:
                    pass
                new_client = None
                _log.warning(
                    "Fresh session generation failed for %s (%s) — keeping uploaded session.",
                    phone, fallback_error,
                )
            finally:
                timings["fresh_login_attempt"] = round(time.monotonic() - t0, 3)

            result["used_fallback"] = not fresh_ok
            if not fresh_ok:
                result["error"] = (
                    f"Fresh session generation failed, kept uploaded session: {fallback_error}"
                )

            # ── Step 8: disconnect final client -> flush -> read bytes ─────────
            # Success  → disconnect new_client, read new session bytes.
            # Fallback → new_client is already gone; read old session bytes instead
            #            (uploaded session stays as the final session, NOT logged out).
            t0 = time.monotonic()
            if fresh_ok:
                await new_client.disconnect()
                new_client = None
                final_tmp_path = new_tmp_path
            else:
                # Fallback: old client IS the final session.
                # Disconnect it now to flush SQLite, then read its bytes.
                await old_client.disconnect()
                old_client     = None
                final_tmp_path = old_tmp_path

            try:
                result["fresh_session_bytes"] = await read_session_bytes(final_tmp_path)
                result["success"]             = True
            except Exception as exc:
                result["error"] = f"Could not read final session bytes: {exc}"
                timings["finalize"] = round(time.monotonic() - t0, 3)
                return _finish(OUTCOME_UPLOAD_FAILED)
            timings["finalize"] = round(time.monotonic() - t0, 3)

            # ── Step 9: self-logout the UPLOADED session ──────────────────────
            # Only done on the success path — the new session is now the sole
            # active one. The uploaded session removes ITSELF from the account.
            # On fallback the uploaded session IS the final one, so we must NOT
            # log it out (old_client is already None in that case).
            if fresh_ok and old_client is not None:
                t0 = time.monotonic()
                try:
                    await old_client.log_out()
                except Exception as exc:
                    _log.warning(
                        "Self-logout of uploaded session failed for %s (%s) — "
                        "disconnecting without logout.",
                        phone, exc,
                    )
                    try:
                        await old_client.disconnect()
                    except Exception:
                        pass
                finally:
                    old_client = None
                    timings["self_logout"] = round(time.monotonic() - t0, 3)

        except Exception as exc:
            _log.error(
                "process_uploaded_session inner error (%s): %s",
                result.get("phone", "unknown"), exc,
            )
            result["error"] = str(exc)
            return _finish(OUTCOME_RPC_ERROR)

    except Exception as exc:
        result["error"] = f"Unexpected pipeline error: {exc}"
        return _finish(OUTCOME_RPC_ERROR)

    finally:
        # Safety net — disconnect any client still open after an unexpected exit.
        for client in (old_client, new_client):
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        # Step 10: delete all temp session files.
        await cleanup_session_files(old_tmp_path, new_tmp_path)

    return _finish()
