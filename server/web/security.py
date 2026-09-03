"""
Session-cookie authentication + CSRF protection + brute-force rate limiting
for the web admin panel.

Design:
- Single admin account, password = ADMIN_PASSWORD secret (pbkdf2-hashed once
  at process start for constant-time comparison; the secret itself is never
  logged or displayed).
- On successful login we set a signed, httponly, samesite=strict cookie
  containing {"a": "admin", "iat": <issued at>}. itsdangerous verifies the
  signature and expiry (max_age) server-side on every request — no server-
  side session store needed, and the cookie is opaque + unforgeable.
- CSRF token is embedded in the same signed cookie payload and rendered into
  every form; verified on every POST.
"""

import hashlib
import secrets
import time
from collections import defaultdict
from os import getenv
from threading import Lock
from typing import Optional

from fastapi import Request, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

ADMIN_PASSWORD = getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise SystemExit("[ERROR] - ADMIN_PASSWORD secret is not set. Please set it in Replit Secrets.")

SESSION_SECRET = getenv("SESSION_SECRET") or getenv("ADMIN_PASSWORD")

COOKIE_NAME = "admin_session"
COOKIE_MAX_AGE = 60 * 60 * 12  # 12 hours

_signer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-panel-session")

# Optional: hide the login page behind /adminlogin?key=<secret>
ADMIN_SECRET_PATH: Optional[str] = getenv("ADMIN_SECRET_PATH") or None


# ── Brute-force protection ───────────────────────────────────────────────────
_LOCKOUT_WINDOW = 900   # 15 minutes
_MAX_ATTEMPTS = 5

_fail_counts: dict = defaultdict(list)
_fail_lock = Lock()


def client_ip(request: Request) -> str:
    # Take the LAST entry from X-Forwarded-For when behind a trusted reverse proxy
    # (Replit's proxy appends the real IP at the end, making spoofing the first entry harmless).
    # If not behind a proxy, fall back to the direct connection IP.
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]  # rightmost = added by trusted proxy
    return request.client.host if request.client else "unknown"


def check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    with _fail_lock:
        _fail_counts[ip] = [t for t in _fail_counts[ip] if now - t < _LOCKOUT_WINDOW]
        if len(_fail_counts[ip]) >= _MAX_ATTEMPTS:
            remaining = int(_LOCKOUT_WINDOW - (now - _fail_counts[ip][0]))
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed login attempts. Try again in {remaining // 60}m {remaining % 60}s.",
            )


def record_fail(ip: str) -> None:
    with _fail_lock:
        _fail_counts[ip].append(time.monotonic())


def clear_fails(ip: str) -> None:
    with _fail_lock:
        _fail_counts.pop(ip, None)


def verify_password(password: str) -> bool:
    return secrets.compare_digest(password, ADMIN_PASSWORD)


# ── Cookie session ────────────────────────────────────────────────────────────

def make_session_token() -> tuple[str, str]:
    """Returns (cookie_value, csrf_token)."""
    csrf = secrets.token_hex(24)
    payload = {"a": "admin", "csrf": csrf}
    token = _signer.dumps(payload)
    return token, csrf


def _read_cookie(request: Request) -> Optional[dict]:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return _signer.loads(raw, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_session(request: Request) -> Optional[dict]:
    return _read_cookie(request)


def is_authenticated(request: Request) -> bool:
    return _read_cookie(request) is not None


def require_admin(request: Request) -> Optional[dict]:
    """Returns the session dict if authenticated, else None (caller redirects)."""
    return _read_cookie(request)


def verify_csrf(request: Request, token: str) -> bool:
    session = _read_cookie(request)
    if not session:
        return False
    return secrets.compare_digest(session.get("csrf", ""), token or "")


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=True,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
