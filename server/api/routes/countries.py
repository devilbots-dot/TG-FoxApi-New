"""
Countries API — browse available countries with pricing and stock information.

Endpoints
─────────
  GET /api/v1/countries          — list all active countries with stock
  GET /api/v1/countries/{code}   — single country by ISO alpha-2 code

Authentication: X-Api-Key header (tg_{user_id}_{secret})

Response envelope (all endpoints):
  Success  → { "success": true, "message": "...", "data": {...}, "meta": {...} }
  Error    → { "success": false, "message": "...", "error": {"code": N, "type": "...", "message": "..."}, "meta": {...} }
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from typing import Optional

from server.api.deps import require_api_key
from server.api.response import ok, err, ERR_NOT_FOUND
from server.api.schemas.common import DataResponse, ErrorResponse, ListResponse
from server.api.schemas.countries import CountryOut
from server.logging import LOGGER
from server.services.country_service import fetch_all_countries, fetch_country

router = APIRouter(
    prefix="/api/v1/countries",
    tags=["Countries"],
    dependencies=[Depends(require_api_key)],
)

_log = LOGGER(__name__)

# ISO alpha-2: exactly two ASCII letters
_CODE_RE = re.compile(r"^[A-Za-z]{2}$")


# ── GET /api/v1/countries ─────────────────────────────────────────────────────

@router.get(
    "/",
    summary="List all available countries",
    description=(
        "Returns every country currently available on the platform, sorted by `country_rank` "
        "(ascending — lowest rank = shown first in the UI).\n\n"
        "`stock` is the number of unsold OTP session accounts ready for purchase.\n\n"
        "Countries with `temp_disable: true` or zero stock are still returned "
        "so clients can show an 'out of stock' state rather than hiding the country."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Missing or malformed API key"},
        403: {"model": ErrorResponse, "description": "Invalid or banned API key"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def list_countries(
    in_stock_only: bool = Query(
        False,
        alias="in_stock",
        description="When true, only return countries with at least 1 account in stock.",
    ),
    min_stock: Optional[int] = Query(
        None,
        ge=0,
        description="Only return countries with stock >= this value.",
    ),
):
    """
    Returns all platform countries with pricing, stock levels, and availability flags.

    Designed to power a country-selection screen without additional API calls.
    Each country includes the price, real-time stock count, dial code, and availability flags.
    """
    try:
        countries = await fetch_all_countries()
    except Exception as exc:
        _log.exception("list_countries failed: %s", exc)
        return err(500, "Failed to retrieve countries. Please try again.")

    # Optional filtering
    if in_stock_only:
        countries = [c for c in countries if c.get("stock", 0) > 0]
    if min_stock is not None:
        countries = [c for c in countries if c.get("stock", 0) >= min_stock]

    items = [CountryOut(**c) for c in countries]

    # Aggregate stats useful for dashboard and analytics
    total_stock = sum(c.get("stock", 0) for c in countries)
    in_stock_count = sum(1 for c in countries if c.get("stock", 0) > 0)
    prices = [float(c.get("price", 0)) for c in countries if c.get("price")]
    min_price = min(prices) if prices else None
    max_price = max(prices) if prices else None

    return ok(
        data=items,
        message=f"{len(items)} {'country' if len(items) == 1 else 'countries'} retrieved successfully.",
        summary={
            "total_countries": len(items),
            "countries_in_stock": in_stock_count,
            "total_stock": total_stock,
            "price_range": {
                "min": round(min_price, 4) if min_price is not None else None,
                "max": round(max_price, 4) if max_price is not None else None,
                "currency": "USD",
            },
        },
    )


# ── GET /api/v1/countries/{code} ──────────────────────────────────────────────

@router.get(
    "/{code}",
    summary="Get a single country by code",
    description=(
        "Returns full details for one country identified by its "
        "**ISO 3166-1 alpha-2 code** (e.g. `UZ`, `IN`, `RU`). "
        "The code is case-insensitive.\n\n"
        "Returns **404** when the country does not exist in the platform catalogue."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid country code format"},
        401: {"model": ErrorResponse, "description": "Missing or malformed API key"},
        403: {"model": ErrorResponse, "description": "Invalid or banned API key"},
        404: {"model": ErrorResponse, "description": "Country not found"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def get_country_detail(
    code: str = Path(
        ...,
        description="ISO 3166-1 alpha-2 country code (e.g. `UZ`, `IN`, `RU`)",
        min_length=2,
        max_length=2,
    ),
) -> dict:
    """
    Returns full pricing and stock details for a single country.

    Use this to confirm availability and price before placing an order,
    or to show a country detail screen on your frontend.
    """
    code = code.upper()
    if not _CODE_RE.match(code):
        return err(
            400,
            f"'{code}' is not a valid ISO alpha-2 country code. "
            "Expected exactly 2 letters (A-Z), e.g. IN, UZ, RU.",
        )

    try:
        country = await fetch_country(code)
    except Exception as exc:
        _log.exception("get_country_detail failed for code=%s: %s", code, exc)
        return err(500, "Failed to retrieve country. Please try again.")

    if country is None:
        return err(
            404,
            f"Country '{code}' not found in the platform catalogue. "
            "Use GET /api/v1/countries for the full list.",
            ERR_NOT_FOUND,
        )

    item = CountryOut(**country)
    available = item.stock > 0 and not country.get("temp_disable", False)

    return ok(
        data={
            **item.model_dump(),
            "available":    available,
            "temp_disabled": bool(country.get("temp_disable", False)),
            "buy_url":      "/api/v1/orders",
            "buy_body":     {"country_code": code},
        },
        message=f"Country '{code}' retrieved successfully.",
    )
