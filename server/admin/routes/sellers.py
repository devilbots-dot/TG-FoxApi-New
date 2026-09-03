"""Admin: Seller search & profile routes."""

from datetime import datetime

from fastapi import APIRouter, Request, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.sellrequestdb import get_seller_profile
from server.utils.database.userdb import get_user

router = APIRouter(tags=["Admin-Sellers"], include_in_schema=False)


def _iso(dt):
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt or ""


@router.get("/admin/sellers", response_class=HTMLResponse)
async def sellers_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/sellers.html", {"page": "sellers"})


@router.get("/admin/api/sellers/{user_id}/profile")
async def seller_profile_api(user_id: int, _session=Depends(require_session)):
    """Full sell history + stats for one user. Used by the seller profile page."""
    try:
        profile = await get_seller_profile(user_id) or {}
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "detail": f"profile lookup failed: {exc}"},
            status_code=500,
        )

    # Guarantee the fields the frontend reads always exist so the UI never
    # crashes on `data.recent.length` or `data.total`.
    profile.setdefault("total", 0)
    profile.setdefault("pending", 0)
    profile.setdefault("paid", 0)
    profile.setdefault("rejected", 0)
    profile.setdefault("total_earned", 0.0)
    profile.setdefault("total_pending", 0.0)
    profile.setdefault("spam_breakdown", {})
    profile.setdefault("country_breakdown", {})
    profile.setdefault("sell_type_breakdown", {})
    profile.setdefault("recent", [])

    # Attach basic user doc (balance, ban status, username) if available
    try:
        user_doc = await get_user(user_id)
        if user_doc:
            user_doc.pop("_id", None)
            profile["user_doc"] = {
                "username":        user_doc.get("username"),
                "first_name":      user_doc.get("first_name"),
                "balance":         user_doc.get("balance", 0.0),
                "pending_balance": user_doc.get("pending_balance", 0.0),
                "is_banned":       user_doc.get("is_banned", False),
                "is_sudo":         user_doc.get("is_sudo", False),
                "joined_at":       _iso(user_doc.get("joined_at")),
            }
        else:
            profile["user_doc"] = None
    except Exception:
        profile["user_doc"] = None

    # jsonable_encoder walks the whole tree and converts datetime/ObjectId etc.
    return JSONResponse(jsonable_encoder(profile))
