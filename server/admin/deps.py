"""
FastAPI dependencies for the admin panel.
"""

from fastapi import Request, HTTPException

from server.web.security import require_admin, verify_csrf

# Form-POST routes that are safe without a JS-sent CSRF token:
# logout only redirects and is already protected by the samesite=strict cookie.
_CSRF_EXEMPT_PATHS = {"/admin/auth/logout"}


def get_session_or_redirect(request: Request):
    """
    Returns the session dict if authenticated.
    Issues a 302 redirect to /adminlogin for unauthenticated page requests.
    """
    session = require_admin(request)
    if session is None:
        raise HTTPException(
            status_code=302,
            headers={"Location": "/adminlogin"},
        )
    return session


def require_session(request: Request):
    """
    Dependency: return session dict if authenticated.
    - Browser requests (Accept: text/html) → 302 redirect to /adminlogin
    - API/XHR requests → 401 JSON error

    For POST/PUT/PATCH/DELETE requests (excluding exempt paths), also verifies
    the X-CSRF-Token header against the token embedded in the signed session cookie.
    """
    session = require_admin(request)
    if session is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            raise HTTPException(
                status_code=302,
                headers={"Location": "/adminlogin"},
            )
        raise HTTPException(
            status_code=401,
            detail={"code": 401, "message": "Not authenticated"},
        )

    # CSRF check for all state-changing methods
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.url.path not in _CSRF_EXEMPT_PATHS:
            token = request.headers.get("X-CSRF-Token", "")
            if not verify_csrf(request, token):
                raise HTTPException(
                    status_code=403,
                    detail={"code": 403, "message": "CSRF token missing or invalid"},
                )

    return session
