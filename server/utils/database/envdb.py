"""Custom environment variable storage in MongoDB.

Admin can add/edit/delete custom key=value pairs.
On startup they are loaded into os.environ so the app can use them.
System secrets (MONGO_DB_URI, BOT_TOKEN, etc.) are never stored here —
they live in Replit Secrets and are read-only from this panel.
"""

import os
from typing import Optional

from server.core.mongo import collection
from server.utils.common import utcnow as _now

envdb = collection("custom_env")


async def get_all_custom_env() -> list[dict]:
    docs = []
    async for d in envdb.find({}, {"_id": 0}).sort("key", 1):
        docs.append(d)
    return docs


async def set_custom_env(key: str, value: str) -> None:
    key = key.strip().upper()
    await envdb.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value, "updated_at": _now()}},
        upsert=True,
    )
    os.environ[key] = value


async def delete_custom_env(key: str) -> bool:
    key = key.strip().upper()
    result = await envdb.delete_one({"key": key})
    if result.deleted_count:
        os.environ.pop(key, None)
        return True
    return False


async def load_custom_env_into_os() -> int:
    """Call once at startup — load all stored custom vars into os.environ."""
    count = 0
    async for d in envdb.find({}, {"_id": 0}):
        k = d.get("key", "")
        v = d.get("value", "")
        if k:
            os.environ[k] = v
            count += 1
    return count
