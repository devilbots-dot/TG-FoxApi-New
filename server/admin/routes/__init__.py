"""Register all admin routers onto the FastAPI app."""

from fastapi import FastAPI


def include_admin_routers(app: FastAPI) -> None:
    from .auth import router as auth_router
    from .dashboard import router as dashboard_router
    from .sessions import router as sessions_router
    from .orders import router as orders_router
    from .users import router as users_router
    from .countries import router as countries_router
    from .proxies import router as proxies_router
    from .payments import router as payments_router
    from .logs import router as logs_router
    from .analytics import router as analytics_router
    from .settings import router as settings_router
    from .backup import router as backup_router
    from .tasks import router as tasks_router
    from .sellers import router as sellers_router
    from .user_sell_stock import router as user_sell_stock_router
    from .sales_feed import router as sales_feed_router
    from .bin import router as bin_router

    for r in [
        auth_router,
        dashboard_router,
        sessions_router,
        orders_router,
        users_router,
        countries_router,
        proxies_router,
        payments_router,
        logs_router,
        analytics_router,
        settings_router,
        backup_router,
        tasks_router,
        sellers_router,
        user_sell_stock_router,
        sales_feed_router,
        bin_router,
    ]:
        app.include_router(r)
