"""
Sales Feed & Fake Activity Service.

Public API:
    from server.services.sales_feed import sales_feed_service
    await sales_feed_service.fire_real_account(...)
    await sales_feed_service.fire_real_session(...)

The service is initialised lazily on first use (or call .start() explicitly).
"""

from server.services.sales_feed.service import SalesFeedService

sales_feed_service = SalesFeedService()

__all__ = ["sales_feed_service"]
