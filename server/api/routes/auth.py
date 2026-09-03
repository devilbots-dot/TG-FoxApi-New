"""
Auth API — API key validation and health probe.

POST /api/v1/auth/validate-key
  Validate a key and return the associated user profile.
  Useful for third-party apps to verify a user-provided key before use.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header

from server.api.response import ok, err, ERR_AUTH, ERR_FORBIDDEN
from server.utils.database.userdb import get_user_by_api_key, get_user_stats

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@router.post(
    "/validate-key",
    summary="Validate an API key",
)
async def validate_key(
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
):
    """
    Validates an API key and returns the associated user profile on success.

    Use this endpoint when your application receives a user-supplied API key
    and wants to verify it is valid before storing or using it.

    Returns **401** when the key is missing or invalid.
    Returns **403** when the account is banned.
    Returns full user profile on success.
    """
    if not x_api_key:
        return err(
            401,
            "No API key provided. Pass it in the X-Api-Key request header.",
            ERR_AUTH,
        )

    if not x_api_key.startswith("tg_"):
        return err(
            401,
            "Invalid API key format. Expected format: tg_{user_id}_{secret}",
            ERR_AUTH,
        )

    user = await get_user_by_api_key(x_api_key)
    if not user:
        return err(
            401,
            "API key not found or has been revoked. "
            "Generate a new key via the bot or POST /api/v1/user/me/api-key/regenerate.",
            ERR_AUTH,
        )

    if user.get("is_banned"):
        return err(
            403,
            "This account has been suspended. Contact support for assistance.",
            ERR_FORBIDDEN,
        )

    stats = await get_user_stats(user["user_id"]) or {}
    joined_at = stats.get("joined_at")

    return ok(
        data={
            "valid":      True,
            "user_id":    stats.get("user_id"),
            "username":   stats.get("username"),
            "full_name":  stats.get("full_name"),
            "rank":       stats.get("rank", "VIP1"),
            "balance":    round(float(stats.get("balance", 0)), 4),
            "currency":   "USD",
            "api_access": stats.get("api_access", True),
            "is_verified": stats.get("is_verified", False),
            "joined_at":  joined_at.isoformat() if joined_at else None,
        },
        message="API key is valid.",
    )
