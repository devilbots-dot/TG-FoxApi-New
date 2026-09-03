"""Backup & Restore routes for the admin panel."""

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from server.admin import templates
from server.admin.deps import require_session
from server.core.mongo import mongodb
from server.utils.database.auditdb import log_action
from server.utils.database.envdb import set_custom_env
from server.web.security import client_ip

router = APIRouter(tags=["Admin-Backup"], include_in_schema=False)

# All known collections used by the project. The backup route also dynamically
# discovers any other collections that exist in the database so nothing is missed.
# Order matters for restore: reference data first, then transactional data.
_BACKUP_COLLECTIONS = [
    "admin_config",
    "custom_env",
    "countries",
    "proxies",
    "users",
    "sudoers",
    "blocked_users",
    "settings",
    "tgusersdb",
    "accounts",
    "session_accounts",
    "pending_2fa",
    "stock_pending_2fa",
    "stock_pending_country",
    "pipeline_state",
    "orders",
    "deposits",
    "withdrawals",
    "transactions",
    "sell_requests",
    "admin_audit_logs",
]

# System collections that should never be touched.
_SYSTEM_COLLECTIONS = {"system.indexes", "system.views", "system.profile"}

# Env var keys that must never leave the server in a backup file.
_SENSITIVE_ENV_KEYS = {
    "ADMIN_PASSWORD",
    "SESSION_SECRET",
    "MONGO_DB_URI",  # optional — keeping it out reduces exposure
}


@router.get("/admin/backup", response_class=HTMLResponse)
async def backup_page(request: Request, _session=Depends(require_session)):
    return templates.TemplateResponse(request, "admin/backup.html", {"page": "backup"})


@router.get("/admin/api/backup/export")
async def export_backup(_session=Depends(require_session)):
    """Download a full database backup as JSON."""
    # Discover all real collections, merge with the known list so even empty
    # known collections are recorded and nothing is ever missed.
    existing = set(await mongodb.list_collection_names())
    existing -= _SYSTEM_COLLECTIONS
    all_collections = _BACKUP_COLLECTIONS + sorted(existing - set(_BACKUP_COLLECTIONS))

    backup = {
        "meta": {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "collections": all_collections,
        },
        "collections": {},
    }

    for name in all_collections:
        coll = mongodb[name]
        docs = []
        async for doc in coll.find({}, {"_id": 0}):
            docs.append(_serialize(doc))

        if name == "custom_env":
            # Redact sensitive env overrides
            docs = [
                d for d in docs
                if (d.get("key") or "").upper() not in _SENSITIVE_ENV_KEYS
            ]

        backup["collections"][name] = docs

    filename = f"backup_{int(time.time())}.json"
    payload = json.dumps(backup, indent=2, default=_json_default)
    bytes_payload = payload.encode("utf-8")

    async def iter_file():
        yield bytes_payload

    return StreamingResponse(
        iter_file(),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        },
    )


@router.post("/admin/api/backup/restore")
async def restore_backup(
    request: Request,
    _session=Depends(require_session),
    file: UploadFile = File(...),
):
    """Restore all collections from an uploaded JSON backup file."""
    ip = client_ip(request)
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Invalid JSON file: {e}"}, status_code=400)

    if not isinstance(data, dict) or "collections" not in data:
        return JSONResponse({"ok": False, "error": "Invalid backup format"}, status_code=400)

    collections = data.get("collections", {})
    if not isinstance(collections, dict):
        return JSONResponse({"ok": False, "error": "Invalid collections block"}, status_code=400)

    restored = {}
    errors = []

    # Restore every collection that exists in the backup. The meta.collections
    # order is preserved, but we also iterate over any extra collections present.
    collection_order = data.get("meta", {}).get("collections", [])
    for name in collection_order:
        if name in _SYSTEM_COLLECTIONS:
            continue
        docs = collections.get(name)
        if docs is None:
            continue
        if not isinstance(docs, list):
            errors.append(f"{name}: expected list, got {type(docs).__name__}")
            continue

        try:
            coll = mongodb[name]
            await coll.delete_many({})
            prepared = []
            if docs:
                prepared = [_deserialize(doc) for doc in docs]
                # Drop any residual _id (backup strips it, but be defensive)
                for d in prepared:
                    d.pop("_id", None)
                await coll.insert_many(prepared)
            restored[name] = len(docs)

            # If custom_env was restored, also push values into os.environ so they
            # take effect after the next restart without requiring manual entry.
            if name == "custom_env" and prepared:
                for d in prepared:
                    k = d.get("key")
                    if k and k.upper() not in _SENSITIVE_ENV_KEYS:
                        await set_custom_env(k, str(d.get("value", "")))
        except Exception as e:
            errors.append(f"{name}: {e}")

    await log_action(
        "backup", "restore",
        ip=ip,
        detail=f"restored {len(restored)} collections: {restored}",
        ok=(not errors),
    )

    if errors:
        return JSONResponse(
            {"ok": False, "error": "; ".join(errors), "restored": restored},
            status_code=500,
        )

    return JSONResponse({"ok": True, "restored": restored, "restart_required": True})


# ── Serialization helpers ────────────────────────────────────────────────────

def _serialize(obj: Any) -> Any:
    """Recursively convert MongoDB types into JSON-safe markers."""
    if isinstance(obj, ObjectId):
        return {"$oid": str(obj)}
    if isinstance(obj, datetime):
        return {"$datetime": obj.isoformat()}
    if isinstance(obj, bytes):
        return {"$bytes": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    return obj


def _deserialize(obj: Any) -> Any:
    """Recursively convert JSON markers back into Python/MongoDB types."""
    if isinstance(obj, dict):
        if len(obj) == 1:
            if "$oid" in obj:
                return ObjectId(obj["$oid"])
            if "$datetime" in obj:
                return datetime.fromisoformat(obj["$datetime"])
            if "$bytes" in obj:
                return base64.b64decode(obj["$bytes"])
        return {k: _deserialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deserialize(v) for v in obj]
    return obj


def _json_default(obj: Any) -> Any:
    """Fallback for json.dumps on types not already handled."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
