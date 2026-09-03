"""
FastAPI dependencies — shared across all API routes.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, status

from server.utils.database import get_user_by_api_key


async def require_api_key(
    x_api_key: Optional[str] = Header(
        None,
        alias="X-Api-Key",
        description="Your API key in the format: tg_{user_id}_{secret}",
    ),
) -> dict:
    """
    FastAPI dependency — validates the X-Api-Key header and returns the user document.

    Raises:
        401  when the header is missing or the key format is wrong
        403  when the key is not found, revoked, or the account is banned
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Missing API key. Pass your key in the X-Api-Key header. "
                "Format: tg_{user_id}_{secret}. "
                "Get your key by sending /start to the bot."
            ),
        )

    if not x_api_key.startswith("tg_"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid API key format. Expected format: tg_{user_id}_{secret}"
            ),
        )

    user = await get_user_by_api_key(x_api_key)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "API key not found or has been revoked. "
                "Generate a new one via POST /api/v1/user/me/api-key/regenerate or via the bot."
            ),
        )

    if user.get("is_banned"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been suspended. Contact support for assistance.",
        )

    return user
