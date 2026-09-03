"""Telegram Mini-App routes and same-server static frontend delivery."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_BASE = Path(__file__).resolve().parent
_ROOT = _BASE.parent.parent
_MINIAPP_DIST = _ROOT / "miniapp" / "dist" / "public"
_MINIAPP_INDEX = _MINIAPP_DIST / "index.html"
templates = Jinja2Templates(directory=str(_BASE / "templates"))


def register_webapp(app: FastAPI) -> None:
    app.mount(
        "/webapp/static",
        StaticFiles(directory=str(_BASE / "static")),
        name="webapp_static",
    )

    # Compiled during the same deploy and served by this FastAPI process.
    app.mount(
        "/app/assets",
        StaticFiles(directory=str(_MINIAPP_DIST / "assets"), check_dir=False),
        name="miniapp_assets",
    )

    @app.get("/app", include_in_schema=False)
    @app.get("/app/", include_in_schema=False)
    @app.get("/app/{asset_path:path}", include_in_schema=False)
    async def miniapp_index(asset_path: str = ""):
        if not _MINIAPP_INDEX.is_file():
            return JSONResponse(
                {"detail": "Mini App build is unavailable. Run the deployment build step."},
                status_code=503,
            )
        if asset_path:
            requested = (_MINIAPP_DIST / asset_path).resolve()
            try:
                requested.relative_to(_MINIAPP_DIST.resolve())
            except ValueError:
                requested = _MINIAPP_INDEX
            if requested.is_file():
                return FileResponse(requested)
        return FileResponse(_MINIAPP_INDEX)

    from .routes import router as webapp_router
    app.include_router(webapp_router)
