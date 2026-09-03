"""
Shared response envelopes — used by all API modules.

Standard envelope:
  Success  → { "success": true,  "message": "...", "data": {...}, "meta": {...}, "pagination": {...} }
  Error    → { "success": false, "message": "...", "error": {"code": N, "type": "...", "message": "..."}, "meta": {...} }

Backward-compat: `status` field mirrors `success` for existing consumers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

_API_VERSION = "1"


# ── Meta ─────────────────────────────────────────────────────────────────────

class MetaOut(BaseModel):
    version: str = _API_VERSION
    timestamp: datetime

    model_config = {"json_schema_extra": {"example": {"version": "1", "timestamp": "2026-08-01T08:00:00Z"}}}


# ── Pagination ────────────────────────────────────────────────────────────────

class PaginationOut(BaseModel):
    page: int
    limit: int
    total: int
    total_pages: int
    has_next: bool
    has_prev: bool

    model_config = {
        "json_schema_extra": {
            "example": {
                "page": 1, "limit": 20, "total": 100,
                "total_pages": 5, "has_next": True, "has_prev": False,
            }
        }
    }


# ── Error ─────────────────────────────────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: int
    type: str = "error"
    message: str
    details: Optional[List[dict]] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "code": 404, "type": "not_found",
                "message": "Country not found",
            }
        }
    }


class ErrorResponse(BaseModel):
    success: bool = False
    status:  bool = False          # backward-compat alias
    message: str
    error:   ErrorDetail
    meta:    Optional[dict] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "success": False, "status": False,
                "message": "Country not found.",
                "error": {"code": 404, "type": "not_found", "message": "Country not found"},
                "meta":  {"version": "1", "timestamp": "2026-08-01T08:00:00Z"},
            }
        }
    }


# ── Success — single object ────────────────────────────────────────────────────

class DataResponse(BaseModel, Generic[T]):
    """
    Single-resource response:
    {
      "success": true,
      "message": "...",
      "data": {...},
      "meta": {...}
    }
    """
    success: bool = True
    status:  bool = True            # backward-compat alias
    message: str  = "Request completed successfully."
    data:    T
    meta:    Optional[dict] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "success": True, "status": True,
                "message": "Country retrieved successfully.",
                "data": {"code": "UZ", "name": "Uzbekistan 🇺🇿", "price": 0.80},
                "meta": {"version": "1", "timestamp": "2026-08-01T08:00:00Z"},
            }
        }
    }


# ── Success — list of objects ──────────────────────────────────────────────────

class ListResponse(BaseModel, Generic[T]):
    """
    List response without pagination:
    {
      "success": true,
      "message": "...",
      "count": N,
      "data": [...],
      "meta": {...}
    }
    """
    success: bool = True
    status:  bool = True            # backward-compat alias
    message: str  = "Data retrieved successfully."
    count:   int
    data:    List[T]
    meta:    Optional[dict] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "success": True, "status": True,
                "message": "Countries retrieved successfully.",
                "count": 3,
                "data": [{"code": "UZ"}, {"code": "IN"}, {"code": "RU"}],
                "meta": {"version": "1", "timestamp": "2026-08-01T08:00:00Z"},
            }
        }
    }


# ── Success — paginated list ────────────────────────────────────────────────────

class PagedResponse(BaseModel, Generic[T]):
    """
    Paginated list response:
    {
      "success": true,
      "message": "...",
      "data": [...],
      "pagination": {"page": 1, "limit": 20, "total": 100, ...},
      "meta": {...}
    }
    """
    success:    bool = True
    status:     bool = True         # backward-compat alias
    message:    str  = "Data retrieved successfully."
    data:       List[T]
    pagination: PaginationOut
    meta:       Optional[dict] = None
