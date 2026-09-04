import asyncio

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from ..logging import LOGGER


class Api:
    def __init__(self):
        LOGGER(__name__).info("Creating FastAPI...")

        self.app = FastAPI(
            title="TG-Fox API",
            description=(
                "Professional Telegram Account Buying & Selling Platform API.\n\n"
                "**Authentication:** Pass your API key in the `X-Api-Key` header.\n"
                "Format: `tg_{your_user_id}_{secret}`\n\n"
                "Get your API key by sending /start to the bot."
            ),
            version="1.0.0",
            docs_url="/docs",
            redoc_url="/redoc",
        )

        from os import getenv
        import config as _config

        _allowed_origins_raw = [o.strip().rstrip("/") for o in (getenv("ALLOWED_ORIGINS") or "").split(",") if o.strip()]
        # Same-origin Mini App requests do not need CORS. Cross-origin access is
        # fail-closed unless an operator explicitly configures approved origins.
        _allowed_origins = _allowed_origins_raw or ([_config.WEBAPP_BASE_URL] if _config.WEBAPP_BASE_URL else [])
        if not _allowed_origins:
            LOGGER(__name__).warning("CORS cross-origin access is disabled: set ALLOWED_ORIGINS only for approved browser origins.")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=_allowed_origins,
            allow_credentials=bool(_allowed_origins),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )
        # Compress only meaningful payloads. Small JSON responses avoid the
        # overhead, while larger Mini App/API/static responses can transfer
        # efficiently when the client sends Accept-Encoding: gzip.
        self.app.add_middleware(GZipMiddleware, minimum_size=1024)

        self._server = None
        self._task = None
        self._register_routes()

    def _register_routes(self):
        from fastapi import Request
        from fastapi.exceptions import RequestValidationError
        from starlette.exceptions import HTTPException as StarletteHTTPException

        # ── Unified error envelope: {status, error: {code, message}} ──────────
        from starlette.responses import RedirectResponse as StarletteRedirect

        @self.app.exception_handler(StarletteHTTPException)
        async def http_exception_handler(request: Request, exc: StarletteHTTPException):
            # Pass redirect responses through unchanged (e.g. admin auth redirects).
            if exc.status_code in (301, 302, 303, 307, 308) and exc.headers:
                location = exc.headers.get("Location") or exc.headers.get("location")
                if location:
                    return StarletteRedirect(url=location, status_code=exc.status_code)
            from server.api.response import err as _err
            return _err(exc.status_code, str(exc.detail))

        @self.app.exception_handler(RequestValidationError)
        async def validation_exception_handler(request: Request, exc: RequestValidationError):
            from server.api.response import _now_iso, _API_VERSION, ERR_VALIDATION
            # Build human-readable field error list
            details = []
            for e in exc.errors():
                loc = " → ".join(str(l) for l in e.get("loc", []) if l != "body")
                details.append({"field": loc or "request", "issue": e.get("msg", "Invalid value")})
            first_msg = details[0]["issue"] if details else "Validation error"
            human = f"Validation failed: {first_msg}." if details else "Request validation failed."
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "status":  False,
                    "message": human,
                    "error": {
                        "code":    422,
                        "type":    ERR_VALIDATION,
                        "message": human,
                        "details": details,
                    },
                    "meta": {"version": _API_VERSION, "timestamp": _now_iso()},
                },
            )

        @self.app.exception_handler(Exception)
        async def unhandled_exception_handler(request: Request, exc: Exception):
            from server.logging import LOGGER as _LOGGER
            _LOGGER("server.api.unhandled").error(
                "Unhandled exception on %s %s: %s",
                request.method, request.url.path, exc, exc_info=True,
            )
            from server.api.response import err as _err
            return _err(500, "An unexpected error occurred. Our team has been notified.")

        # ── Request ID middleware ─────────────────────────────────────────────
        # Attaches X-Request-Id to every response so callers can correlate
        # errors in logs with specific requests. Accepts caller-supplied IDs
        # (useful for idempotency) and generates one if absent.
        import uuid

        @self.app.middleware("http")
        async def json_request_size_guard(request: Request, call_next):
            """Reject oversized JSON API payloads before parsing/business logic."""
            api_path = request.url.path.startswith(("/api/", "/webapp/api/"))
            content_type = request.headers.get("content-type", "").lower()
            content_length = request.headers.get("content-length")
            if api_path and "application/json" in content_type and content_length:
                try:
                    if int(content_length) > 256 * 1024:
                        return JSONResponse({"detail": "JSON request body is too large."}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)
            return await call_next(request)

        @self.app.middleware("http")
        async def request_id_middleware(request: Request, call_next):
            req_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
            try:
                response = await call_next(request)
            except RuntimeError as exc:
                # Starlette can raise this when an SSE client closes or
                # reconnects before call_next returns. That is a normal
                # browser disconnect, not an application failure.
                if str(exc) == "No response returned.":
                    return Response(status_code=204, headers={"X-Request-Id": req_id})
                raise
            response.headers["X-Request-Id"] = req_id
            return response

        from server.api.routes import (
            auth_router,
            user_router,
            wallet_router,
            deposit_router,
            countries_router,
            orders_router,
        )

        self.app.include_router(auth_router)
        self.app.include_router(user_router)
        self.app.include_router(wallet_router)
        self.app.include_router(deposit_router)
        self.app.include_router(countries_router)
        self.app.include_router(orders_router)

        # Withdrawal webhook (OxaPay payout callbacks)
        from server.api.routes.withdrawal_webhook import router as withdrawal_webhook_router
        self.app.include_router(withdrawal_webhook_router)

        # Deposit webhook (OxaPay merchant payment callbacks)
        from server.api.routes.deposit_webhook import router as deposit_webhook_router
        self.app.include_router(deposit_webhook_router)

        # Binance Pay webhook (automatic payment confirmation)
        from server.api.routes.binance_pay_webhook import router as binance_pay_webhook_router
        self.app.include_router(binance_pay_webhook_router)

        # New modular admin panel (replaces old admin_router)
        from server.admin import register_admin
        register_admin(self.app)

        from server.webapp import register_webapp
        register_webapp(self.app)

        @self.app.get("/", tags=["Health"])
        async def home():
            from server.api.response import ok as _ok
            return _ok(
                data={
                    "platform":    "TG-Fox API",
                    "version":     "1.0.0",
                    "api_version": "v1",
                    "docs":        "/docs",
                    "redoc":       "/redoc",
                    "description": (
                        "Professional Telegram Account Buying & Selling Platform API. "
                        "Authenticate via X-Api-Key header. "
                        "Get your key by sending /start to the bot."
                    ),
                    "authentication": {
                        "header":  "X-Api-Key",
                        "format":  "tg_{user_id}_{secret}",
                        "obtain":  "Send /start to the Telegram bot",
                        "rotate":  "POST /api/v1/user/me/api-key/regenerate",
                    },
                    "endpoints": {
                        "auth":            "POST /api/v1/auth/validate-key",
                        "profile":         "GET  /api/v1/user/me",
                        "balance":         "GET  /api/v1/user/balance",
                        "rank":            "GET  /api/v1/user/me/rank",
                        "referral":        "GET  /api/v1/user/me/referral",
                        "orders":          "GET  /api/v1/user/orders",
                        "transactions":    "GET  /api/v1/user/transactions",
                        "sessions_sold":   "GET  /api/v1/user/sessions/sold",
                        "sessions_bought": "GET  /api/v1/user/sessions/bought",
                        "wallet":          "GET  /api/v1/wallet",
                        "wallet_balance":  "GET  /api/v1/wallet/balance",
                        "deposit_methods": "GET  /api/v1/wallet/deposit/methods",
                        "deposit_create":  "POST /api/v1/wallet/deposit",
                        "deposit_status":  "GET  /api/v1/wallet/deposit/{deposit_id}",
                        "withdrawals":     "GET  /api/v1/wallet/withdrawals",
                        "withdraw":        "POST /api/v1/wallet/withdraw",
                        "countries":       "GET  /api/v1/countries",
                        "country_detail":  "GET  /api/v1/countries/{code}",
                        "buy":             "POST /api/v1/orders",
                        "otp":             "GET  /api/v1/orders/{order_id}/otp",
                        "admin_panel":     "/adminlogin",
                    },
                    "status": {
                        "health": "/health",
                        "ping":   "/ping",
                    },
                },
                message="TG-Fox API is running. See /docs for full API documentation.",
            )

        @self.app.get("/ping", tags=["Health"])
        async def ping():
            from server.api.response import ok as _ok
            return _ok(data={"pong": True}, message="pong")

        @self.app.get("/health", tags=["Health"])
        async def health():
            from server.api.response import ok as _ok
            return _ok(data={"healthy": True}, message="Service is healthy.")

    async def start(self):
        # Run uvicorn inside the SAME asyncio event loop as the bot (as a
        # background task) instead of a separate thread with its own loop.
        # This avoids "Future attached to a different loop" errors when API
        # request handlers call the shared Motor/MongoDB client.
        import config as _cfg
        LOGGER(__name__).info("Starting FastAPI on port %d...", _cfg.API_PORT)
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=_cfg.API_PORT,
            log_level="warning",
            access_log=False,
            loop="none",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())

    async def stop(self):
        LOGGER(__name__).info("Stopping FastAPI...")
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
