"""Background task management routes — view status, trigger workers, edit intervals."""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from server.admin import templates
from server.admin.deps import require_session
from server.utils.database.auditdb import log_action
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Tasks"], include_in_schema=False)


@router.get("/admin/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/tasks.html", {"page": "tasks"})


@router.get("/admin/api/tasks")
async def list_tasks(_session=Depends(require_session)):
    """Return status of all registered background workers."""
    from server.services.workers import get_worker_status
    from server.utils.database.configdb import get_setting

    workers = await get_worker_status()
    # Return the related timing settings the workers actually read from, so the
    # admin sees "one source of truth" instead of hunting across pages.
    related = {
        "payment_hold_hours":            await get_setting("payment_hold_hours"),
        "order_timeout_minutes":         await get_setting("order_timeout_minutes"),
        "low_stock_threshold":           await get_setting("low_stock_threshold"),
        "termination_delay_minutes":     await get_setting("termination_delay_minutes"),
        "termination_retry_base_hours":  await get_setting("termination_retry_base_hours") or 24,
        "termination_retry_max_hours":   await get_setting("termination_retry_max_hours")  or 24 * 7,
    }
    return JSONResponse({"workers": workers, "related_settings": related})


@router.post("/admin/api/tasks/{name}/trigger")
async def trigger_task(name: str, request: Request, _session=Depends(require_session)):
    """Manually trigger a specific background worker's inner function."""
    from server.services.workers import trigger_worker
    ok = await trigger_worker(name)
    await log_action(
        "admin", "worker_triggered",
        ip=client_ip(request),
        target=name,
        ok=ok,
    )
    if not ok:
        return JSONResponse({"ok": False, "detail": f"Worker '{name}' not found or failed."}, status_code=404)
    return JSONResponse({"ok": True, "message": f"Worker '{name}' triggered successfully."})


@router.post("/admin/api/tasks/{name}/interval")
async def set_task_interval(name: str, request: Request, _session=Depends(require_session)):
    """Override a worker's tick interval (seconds). Send {"seconds": null} to reset to default."""
    from server.services.workers import set_worker_interval

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    raw = payload.get("seconds")
    seconds: int | None
    if raw is None or raw == "":
        seconds = None
    else:
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "detail": "seconds must be an integer or null"}, status_code=400)

    ok = await set_worker_interval(name, seconds)
    await log_action(
        "admin", "worker_interval_set",
        ip=client_ip(request),
        target=name,
        ok=ok,
        detail=f"seconds={seconds}",
    )
    if not ok:
        return JSONResponse({"ok": False, "detail": f"Worker '{name}' not found."}, status_code=404)
    return JSONResponse({"ok": True, "seconds": seconds})
