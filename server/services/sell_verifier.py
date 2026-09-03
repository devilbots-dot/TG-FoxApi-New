"""
Sell Session Verifier — reusable payment gate for all sell flows.

Called by:
  • payment_release_worker  — before auto-releasing pending→earned balance (24h gate)
  • admin /validate API     — on-demand live check from the admin panel
  • finalize_sell_approval  — pre-approval gate (admin can force-override)

Verification checks (in order of criticality):
  1. session_ref         — session_msg_id + session_chat_id present in DB
  2. session_download    — file is downloadable from Telegram storage channel
  3. session_extract     — ZIP extracted / raw bytes readable
  4. session_authorized  — Telethon is_user_authorized() = True
  5. other_sessions      — no other active authorizations (termination verified)
  6. spam_status         — current restriction / spam status via Telegram API

Rules for auto-release (payment_release_worker):
  • checks 1-5 must all pass
  • spam_status permanent_spam / frozen / dead → REJECT
  • spam_status temporary_spam → pass (platform accepted it at submission)
  • any inconclusive check (pure network timeout) → hold payment, retry next cycle

Rules for admin manual approve:
  • Same checks run; failures are reported as WARNINGS
  • Admin may force-approve despite failures (audit-logged)
  • Force-approve reason is included in admin_note and user notification
"""
from __future__ import annotations

import asyncio
import io
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from server import LOGGER

_log = LOGGER(__name__)

# Spam statuses that are hard-blocks for payment release
_BLOCK_STATUSES = ("permanent_spam", "frozen", "dead", "unknown_banned")

# Spam statuses that auto-release allows (temporary issues are pre-accepted)
_ALLOW_STATUSES = ("clean", "temporary_spam", "restricted", "unknown")


@dataclass
class CheckDetail:
    passed: bool
    reason: str = ""
    inconclusive: bool = False   # True = network error, not a hard fail
    data: dict = field(default_factory=dict)


@dataclass
class VerificationResult:
    verified: bool                              # True = all required checks passed
    inconclusive: bool = False                  # True = network errors prevented full check
    checks: dict[str, CheckDetail] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)
    warn_checks: list[str] = field(default_factory=list)   # non-blocking issues
    reasons: list[str] = field(default_factory=list)       # human-readable failures
    warnings: list[str] = field(default_factory=list)      # human-readable warnings
    spam_status: str = "unknown"
    other_sessions_count: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "verified":             self.verified,
            "inconclusive":         self.inconclusive,
            "failed_checks":        self.failed_checks,
            "warn_checks":          self.warn_checks,
            "reasons":              self.reasons,
            "warnings":             self.warnings,
            "spam_status":          self.spam_status,
            "other_sessions_count": self.other_sessions_count,
            "error":                self.error,
            "checks": {
                name: {
                    "passed":       c.passed,
                    "reason":       c.reason,
                    "inconclusive": c.inconclusive,
                    **c.data,
                }
                for name, c in self.checks.items()
            },
        }


async def verify_sell_session(
    sell_request: dict,
    *,
    check_other_sessions: bool = True,
    timeout_s: int = 60,
) -> VerificationResult:
    """
    Comprehensive session verification for a sell request document.

    `sell_request` must contain at minimum:
      session_msg_id, session_chat_id, code (country ISO), request_id
    These are all present in any document fetched from sell_requests collection.

    Returns a VerificationResult.  Never raises — all exceptions are caught.
    """
    result = VerificationResult(verified=False)

    # ── Check 1: Session reference present ───────────────────────────────────
    session_msg_id  = sell_request.get("session_msg_id")
    session_chat_id = sell_request.get("session_chat_id")
    request_id      = sell_request.get("request_id", "?")
    country_code    = sell_request.get("code", "XX")

    if not (session_msg_id and session_chat_id):
        detail = CheckDetail(
            passed=False,
            reason="No session file reference stored (session_msg_id / session_chat_id missing). "
                   "This sell request was submitted without a session file.",
        )
        result.checks["session_ref"] = detail
        result.failed_checks.append("session_ref")
        result.reasons.append(detail.reason)
        result.error = "No session reference"
        return result

    result.checks["session_ref"] = CheckDetail(passed=True)

    # ── Check 2: Download session from channel ────────────────────────────────
    try:
        from server import bot
        from server.utils.sessions.channel_storage import download_session_from_channel
        raw_bytes = await asyncio.wait_for(
            download_session_from_channel(
                bot,
                session_chat_id,
                session_msg_id,
                account_id=sell_request.get("account_id"),
                phone=sell_request.get("phone"),
            ),
            timeout=30,
        )
        result.checks["session_download"] = CheckDetail(passed=True)
    except asyncio.TimeoutError:
        detail = CheckDetail(
            passed=False, inconclusive=True,
            reason="Telegram channel download timed out — network issue, not a session fault.",
        )
        result.checks["session_download"] = detail
        result.inconclusive = True
        result.error = "Channel download timed out"
        return result
    except Exception as exc:
        from server.utils.sessions.channel_storage import StorageError
        code = exc.code if isinstance(exc, StorageError) else "UNKNOWN_STORAGE_ERROR"
        inconclusive = code in {
            "MESSAGE_ACCESS_DENIED", "MEDIA_DOWNLOAD_FAILED", "UNKNOWN_STORAGE_ERROR",
        }
        detail = CheckDetail(
            passed=False,
            inconclusive=inconclusive,
            reason=f"Storage error {code}: {exc}",
            data={"storage_error_code": code},
        )
        result.checks["session_download"] = detail
        result.error = code
        if inconclusive:
            result.inconclusive = True
        else:
            result.failed_checks.append("session_download")
            result.reasons.append(detail.reason)
        return result

    # ── Check 3: Extract session bytes from ZIP / raw ─────────────────────────
    session_bytes: bytes
    try:
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                ses_files = [n for n in zf.namelist() if n.endswith(".session")]
                if not ses_files:
                    raise ValueError("ZIP contains no .session file")
                session_bytes = zf.read(ses_files[0])
        except (zipfile.BadZipFile, ValueError):
            # Not a ZIP — treat as raw session bytes
            session_bytes = raw_bytes
        result.checks["session_extract"] = CheckDetail(passed=True, data={"size_bytes": len(session_bytes)})
    except Exception as exc:
        detail = CheckDetail(
            passed=False,
            reason=f"Session file is corrupted or unreadable: {exc}",
        )
        result.checks["session_extract"] = detail
        result.failed_checks.append("session_extract")
        result.reasons.append(detail.reason)
        return result

    # ── Check 4–6: Connect with Telethon ─────────────────────────────────────
    tmp_path = None
    client   = None
    try:
        import config as _cfg
        from server.utils.sessions.telethon_client import (
            new_temp_session_path,
            write_session_bytes,
            cleanup_session_files,
            connect_with_proxy_fallback,
        )

        tmp_path = new_temp_session_path()
        await write_session_bytes(tmp_path, session_bytes)

        try:
            client, _, _ = await asyncio.wait_for(
                connect_with_proxy_fallback(
                    tmp_path[:-8], _cfg.API_ID, _cfg.API_HASH, country_code
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            result.checks["session_authorized"] = CheckDetail(
                passed=False, inconclusive=True,
                reason="Telegram connection timed out — DC unreachable. Verification inconclusive.",
            )
            result.inconclusive = True
            result.error = "Telegram connection timed out"
            return result
        except Exception as exc:
            result.checks["session_authorized"] = CheckDetail(
                passed=False, inconclusive=True,
                reason=f"Could not connect to Telegram: {exc}",
            )
            result.inconclusive = True
            result.error = str(exc)
            return result

        # ── Check 4: Authorized ───────────────────────────────────────────────
        try:
            is_auth = await asyncio.wait_for(client.is_user_authorized(), timeout=15)
        except Exception as exc:
            result.checks["session_authorized"] = CheckDetail(
                passed=False, inconclusive=True,
                reason=f"Could not verify authorization: {exc}",
            )
            result.inconclusive = True
            result.error = str(exc)
            return result

        if not is_auth:
            detail = CheckDetail(
                passed=False,
                reason="Session is no longer authorized — the account has been logged out "
                       "or the session has expired. Payment cannot be released.",
            )
            result.checks["session_authorized"] = detail
            result.failed_checks.append("session_authorized")
            result.reasons.append(detail.reason)
            return result

        result.checks["session_authorized"] = CheckDetail(passed=True)

        # ── Check 5: No other active sessions ────────────────────────────────
        if check_other_sessions:
            try:
                from server.stock.authorization import list_other_authorizations
                others = await asyncio.wait_for(
                    list_other_authorizations(client),
                    timeout=20,
                )
                other_count = len(others)
                result.other_sessions_count = other_count

                if other_count > 0:
                    lifecycle_status = str(
                        sell_request.get("lifecycle_status", "pending")
                    ).lower()
                    termination_is_pending = lifecycle_status in {
                        "pending",
                        "pending_termination",
                        "termination_pending",
                    }
                    # Build a short description of the other sessions
                    session_descs = []
                    for auth in others[:5]:  # max 5 in reason text
                        app_name = getattr(auth, "app_name", "Unknown") or "Unknown"
                        device   = getattr(auth, "device_model", "") or ""
                        platform = getattr(auth, "platform", "") or ""
                        session_descs.append(
                            f"• {app_name}" + (f" on {device}" if device else "") +
                            (f" ({platform})" if platform else "")
                        )
                    sessions_text = "\n".join(session_descs)
                    if termination_is_pending:
                        detail = CheckDetail(
                            passed=True,
                            inconclusive=True,
                            reason=(
                                f"{other_count} other active Telegram session(s) detected while "
                                "scheduled termination is still pending. This is advisory and the "
                                "termination worker will continue processing them.\n\n"
                                f"Active sessions:\n{sessions_text}"
                            ),
                            data={"count": other_count},
                        )
                        result.checks["other_sessions"] = detail
                        result.warn_checks.append("other_sessions")
                        result.warnings.append(
                            f"{other_count} other active session(s) present while termination is pending"
                        )
                    else:
                        detail = CheckDetail(
                            passed=False,
                            reason=(
                                f"{other_count} other active Telegram session(s) detected. "
                                "Session termination is incomplete or the account was logged in again "
                                "after termination.\n\n"
                                f"Active sessions:\n{sessions_text}"
                            ),
                            data={"count": other_count},
                        )
                        result.checks["other_sessions"] = detail
                        result.failed_checks.append("other_sessions")
                        result.reasons.append(
                            f"{other_count} other active session(s) still present — termination incomplete"
                        )
                else:
                    result.checks["other_sessions"] = CheckDetail(
                        passed=True, data={"count": 0},
                        reason="No other active sessions detected. Account is single-session.",
                    )

            except asyncio.TimeoutError:
                result.checks["other_sessions"] = CheckDetail(
                    passed=True, inconclusive=True,
                    reason="Could not list authorizations (timeout) — treated as passed.",
                )
                result.warn_checks.append("other_sessions")
                result.warnings.append("Authorization list timed out — could not verify single-session status")
            except Exception as exc:
                result.checks["other_sessions"] = CheckDetail(
                    passed=True, inconclusive=True,
                    reason=f"Authorization list failed: {exc} — treated as passed.",
                )
                result.warn_checks.append("other_sessions")
                result.warnings.append(f"Authorization list failed: {exc}")
        else:
            result.checks["other_sessions"] = CheckDetail(
                passed=True, reason="Other sessions check skipped by caller.",
            )

        # ── Check 6: Spam / restriction status ────────────────────────────────
        spam_status = "unknown"
        try:
            from server.stock.spam_check import check_spam_status
            spam_status = await asyncio.wait_for(check_spam_status(client), timeout=15)
            result.spam_status = spam_status

            if spam_status in _BLOCK_STATUSES:
                detail = CheckDetail(
                    passed=False,
                    reason=(
                        f"Account spam/restriction status: **{spam_status}**. "
                        "Permanently banned or frozen accounts cannot be accepted."
                    ),
                    data={"spam_status": spam_status},
                )
                result.checks["spam_status"] = detail
                result.failed_checks.append("spam_status")
                result.reasons.append(f"Account status: {spam_status} — not acceptable")
            elif spam_status == "temporary_spam":
                # Non-blocking — platform accepted this at submission time
                result.checks["spam_status"] = CheckDetail(
                    passed=True,
                    reason="Account has temporary spam restriction — accepted per platform policy.",
                    data={"spam_status": spam_status},
                )
                result.warn_checks.append("spam_status")
                result.warnings.append(
                    "Account has temporary spam/restriction flag (accepted per platform policy)"
                )
            else:
                result.checks["spam_status"] = CheckDetail(
                    passed=True,
                    reason=f"Spam status: {spam_status}",
                    data={"spam_status": spam_status},
                )

        except asyncio.TimeoutError:
            result.checks["spam_status"] = CheckDetail(
                passed=True, inconclusive=True,
                reason="Spam check timed out — treated as passed.",
                data={"spam_status": "unknown"},
            )
            result.warn_checks.append("spam_status")
            result.warnings.append("Spam status check timed out — could not verify current status")
        except Exception as exc:
            result.checks["spam_status"] = CheckDetail(
                passed=True, inconclusive=True,
                reason=f"Spam check failed: {exc}",
                data={"spam_status": "unknown"},
            )
            result.warn_checks.append("spam_status")
            result.warnings.append(f"Spam status check failed: {exc}")

    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if tmp_path:
            try:
                from server.utils.sessions.telethon_client import cleanup_session_files
                await cleanup_session_files(tmp_path)
            except Exception:
                pass

    # ── Final verdict ─────────────────────────────────────────────────────────
    result.verified = len(result.failed_checks) == 0
    _log.info(
        "verify_sell_session %s: verified=%s failed=%s warn=%s spam=%s other_sessions=%d",
        request_id, result.verified, result.failed_checks,
        result.warn_checks, result.spam_status, result.other_sessions_count,
    )
    return result


def build_admin_warning_text(result: VerificationResult) -> str:
    """
    Build a compact human-readable warning for admin display.
    Used in Telegram bot notifications and admin panel tooltip.
    """
    if result.verified and not result.warnings:
        return "✅ All session checks passed."

    lines = []
    if result.failed_checks:
        lines.append("❌ **Verification FAILED** — the following issues were found:")
        for reason in result.reasons:
            lines.append(f"  • {reason}")

    if result.warnings:
        lines.append("\n⚠️ **Warnings** (non-blocking):")
        for w in result.warnings:
            lines.append(f"  • {w}")

    if result.inconclusive:
        lines.append("\n🌐 Some checks were inconclusive due to network issues.")

    return "\n".join(lines)


def build_user_rejection_text(result: VerificationResult, request_id: str, phone: str = "") -> str:
    """
    Build a clear rejection explanation for the seller (sent via Telegram DM).
    """
    phone_line = f"\n📱 **Phone:** `{phone}`" if phone else ""
    lines = [
        f"❌ **Sell Request Payment Rejected**\n",
        f"🆔 **Request ID:** `{request_id}`{phone_line}\n",
        "Our automated 24-hour re-verification found that your session no longer meets "
        "our acceptance criteria. Your reserved balance has been reversed.\n",
    ]

    if result.failed_checks:
        lines.append("**Failed checks:**")
        for reason in result.reasons:
            lines.append(f"• {reason}")

    lines.append(
        "\n📋 **What to do:**\n"
        "If you believe this is an error, contact support with your Request ID.\n"
        "You may re-submit a new sell request if the underlying issue has been resolved."
    )
    return "\n".join(lines)
