"""
New admin panel — clean modular architecture.

Mounts at /admin (pages) and /admin/api (JSON APIs).
Auth: cookie-based sessions via server/web/security.py (HttpOnly, CSRF, SameSite=strict).
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

_BASE = Path(__file__).parent

templates = Jinja2Templates(directory=str(_BASE / "templates"))


def register_admin(app: FastAPI) -> None:
    """Mount the admin static files and include all admin routers."""
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(_BASE / "static")),
        name="admin_static",
    )

    from .routes import include_admin_routers
    include_admin_routers(app)
