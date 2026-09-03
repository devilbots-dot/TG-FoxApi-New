"""Login / logout routes for the admin panel."""

import secrets as _secrets
from typing import Optional

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from server.web.security import (
    client_ip,
    check_rate_limit,
    record_fail,
    clear_fails,
    verify_password,
    make_session_token,
    set_session_cookie,
    clear_session_cookie,
    require_admin,
    ADMIN_SECRET_PATH,
)
from server.utils.database.auditdb import log_action
from server.admin import templates
from server.admin.deps import require_session

router = APIRouter(tags=["Admin-Auth"], include_in_schema=False)


@router.get("/adminlogin", response_class=HTMLResponse)
async def login_page(request: Request, key: Optional[str] = None):
    # Optional secret path gating
    if ADMIN_SECRET_PATH and not _secrets.compare_digest(key or "", ADMIN_SECRET_PATH):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    if require_admin(request):
        return RedirectResponse("/admin", status_code=302)

    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/admin/auth/login")
async def do_login(request: Request, password: str = Form(...)):
    ip = client_ip(request)
    check_rate_limit(ip)

    if not verify_password(password):
        record_fail(ip)
        await log_action("login", "login_failed", ip=ip, detail="Wrong password", ok=False)
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Invalid password. Try again."},
            status_code=401,
        )

    clear_fails(ip)
    token, csrf = make_session_token()
    await log_action("login", "login_success", ip=ip, detail="Admin logged in", ok=True)

    response = RedirectResponse("/admin", status_code=302)
    set_session_cookie(response, token)
    return response


@router.get("/admin/api/csrf")
async def get_csrf_token(request: Request, session=Depends(require_session)):
    """Return the CSRF token embedded in the signed session cookie."""
    return {"csrf": session.get("csrf", "")}


@router.post("/admin/auth/logout")
async def do_logout(request: Request, _session=Depends(require_session)):
    ip = client_ip(request)
    await log_action("login", "logout", ip=ip, detail="Admin logged out", ok=True)
    response = RedirectResponse("/adminlogin", status_code=302)
    clear_session_cookie(response)
    return response
