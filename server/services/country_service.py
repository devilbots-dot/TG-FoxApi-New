"""
Country service — business logic between the DB layer and the API routes.

All DB calls go through countrydb / sessiondb; this layer owns the
transformation from raw Mongo docs to clean API dicts.
"""

from __future__ import annotations

from typing import List, Optional

from server.utils.database.countrydb import get_all_countries, get_country
from server.utils.database.sessiondb import count_unsold_by_country, get_unsold_counts_all_countries
from server.utils.stock_display import displayed_stock


def _to_country_out(doc: dict, session_stock: int = 0) -> dict:
    """Convert a raw Mongo country document → CountryOut-compatible dict."""
    return {
        "name":         doc.get("country_name", ""),
        "country_rank": doc.get("country_rank", 0),
        "dial_code":    doc.get("idc", ""),
        "code":         doc.get("code", ""),
        "price":        round(float(doc.get("price", 0)), 4),
        "stock":        session_stock,
    }


async def fetch_all_countries() -> List[dict]:
    """
    Return all purchasable countries sorted by country_rank (ascending).

    - Excludes temp_disabled countries (buy_only=True).
    - Excludes zero-stock countries (nothing to sell).
    - Session stock is fetched in ONE aggregation instead of N+1 per-country
      queries, making this O(2) DB round-trips regardless of catalogue size.
    """
    docs = await get_all_countries(buy_only=True)

    # Stock counts are maintained by memstore on every inventory mutation and
    # hydrated once at startup.  Reading them here avoids a Mongo aggregation
    # on every country-list request, which is the hottest public endpoint.
    session_counts = await get_unsold_counts_all_countries(clean_only=True)

    result = []
    for doc in docs:
        # Display may be real or per-country fake stock. Purchases still only
        # ever pull from real clean inventory, regardless of what's displayed.
        real_stock = session_counts.get(doc["code"], 0)
        display_stock = displayed_stock(doc, real_stock)
        out = _to_country_out(doc, display_stock)
        if out["stock"] > 0:          # hide countries with nothing to sell
            result.append(out)
    return result


async def fetch_country(code: str) -> Optional[dict]:
    """
    Return a single purchasable country by ISO alpha-2 code, or None.

    Returns None (→ 404) when:
      - Country does not exist.
      - Country is temp_disabled (not available for purchase).
    """
    doc = await get_country(code.upper())
    if doc is None or doc.get("temp_disable"):
        return None
    # Mirror sellable pool used by get_unsold_session_for_country
    real_stock = await count_unsold_by_country(doc["code"], clean_only=True)
    display_stock = displayed_stock(doc, real_stock)
    return _to_country_out(doc, display_stock)
