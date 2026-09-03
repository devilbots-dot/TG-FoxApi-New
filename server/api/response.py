"""
API Response Builder — SaaS-grade consistent response envelope for all endpoints.

Every response follows this structure:

  Success:
    {
      "success": true,
      "message": "Human-readable description of what happened",
      "data": {...} | [...] | null,
      "meta": {"version": "1", "timestamp": "2026-08-01T08:00:00Z"},
      "pagination": {          // only for paginated list responses
        "page": 1,
        "limit": 20,
        "total": 100,
        "total_pages": 5,
        "has_next": true,
        "has_prev": false
      }
    }

  Error:
    {
      "success": false,
      "message": "What went wrong and how to fix it",
      "error": {
        "code": 400,
        "type": "validation_error",
        "message": "...",
        "details": [...]
      },
      "meta": {"version": "1", "timestamp": "2026-08-01T08:00:00Z"}
    }

Backward-compatibility: `status` field mirrors `success` for existing bot/consumer code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi.responses import JSONResponse

_API_VERSION = "1"

# Error type constants — use these for the `error_type` arg in err()
ERR_VALIDATION   = "validation_error"
ERR_AUTH         = "authentication_error"
ERR_FORBIDDEN    = "authorization_error"
ERR_NOT_FOUND    = "not_found"
ERR_CONFLICT     = "conflict"
ERR_RATE_LIMIT   = "rate_limit"
ERR_UNAVAILABLE  = "service_unavailable"
ERR_INTERNAL     = "internal_error"
ERR_PAYMENT      = "payment_error"
ERR_BUSINESS     = "business_rule_violation"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta() -> dict:
    return {"version": _API_VERSION, "timestamp": _now_iso()}


def _pagination_block(page: int, limit: int, total: int) -> dict:
    """Build a consistent pagination metadata block."""
    total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


# ── Success responses ─────────────────────────────────────────────────────────

def ok(
    data: Any = None,
    message: str = "Request completed successfully.",
    pagination: Optional[dict] = None,
    **extra: Any,
) -> dict:
    """
    Return a standard success response envelope.

    Usage:
        return ok(data={"balance": 100.0}, message="Balance retrieved.")
        return ok(data=[...], message="...", pagination=build_pagination(page, limit, total))
    """
    response: dict = {
        "success": True,
        "status": True,          # backward-compat for existing consumers
        "message": message,
        "data": data,
        "meta": _meta(),
    }
    if pagination is not None:
        response["pagination"] = pagination
    # Merge any extra top-level fields (backward compat with old ok(**kwargs) call sites)
    if extra:
        response.update(extra)
    return response


def paginate(
    data: list,
    page: int,
    limit: int,
    total: int,
    message: str = "Data retrieved successfully.",
    **extra: Any,
) -> dict:
    """
    Convenience wrapper for paginated list responses.

    Usage:
        return paginate(data=items, page=1, limit=20, total=150, message="Orders retrieved.")
    """
    return ok(
        data=data,
        message=message,
        pagination=_pagination_block(page, limit, total),
        **extra,
    )


def build_pagination(page: int, limit: int, total: int) -> dict:
    """Build a pagination block to pass into ok()."""
    return _pagination_block(page, limit, total)


# ── Error responses ───────────────────────────────────────────────────────────

def err(
    code: int,
    message: str,
    error_type: str = ERR_BUSINESS,
    details: Optional[list] = None,
) -> JSONResponse:
    """
    Return a JSON error response with a standard envelope.

    Usage:
        return err(400, "Insufficient balance. Top up your wallet.", ERR_PAYMENT)
        return err(404, "Order 'ORD-abc' not found.", ERR_NOT_FOUND)
        return err(422, "Validation failed.", ERR_VALIDATION, details=[{"field": "amount", "issue": "Must be > 0"}])
    """
    # Infer a sensible error_type from the HTTP code if not supplied explicitly
    if error_type == ERR_BUSINESS:
        if code == 401:
            error_type = ERR_AUTH
        elif code == 403:
            error_type = ERR_FORBIDDEN
        elif code == 404:
            error_type = ERR_NOT_FOUND
        elif code == 409:
            error_type = ERR_CONFLICT
        elif code == 422:
            error_type = ERR_VALIDATION
        elif code == 429:
            error_type = ERR_RATE_LIMIT
        elif code in (500, 502, 503):
            error_type = ERR_INTERNAL if code == 500 else ERR_UNAVAILABLE

    error_block: dict = {
        "code": code,
        "type": error_type,
        "message": message,
    }
    if details:
        error_block["details"] = details

    return JSONResponse(
        status_code=code,
        content={
            "success": False,
            "status": False,        # backward-compat
            "message": message,
            "error": error_block,
            "meta": _meta(),
        },
    )
