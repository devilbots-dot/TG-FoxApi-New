"""
MongoDB client management.

Architecture:
  - `reinit()` must be called inside the running event loop (in __main__) so
    the Motor client is bound to the correct asyncio loop.
  - `collection(name)` returns a _LazyCollection proxy that always delegates
    to the current `mongodb` object — no manual patching of db module vars needed.
"""

from motor.motor_asyncio import AsyncIOMotorClient

from config import MONGO_DB_URI
from ..logging import LOGGER

_log = LOGGER(__name__)

# Pool sized for concurrent multi-admin stock batches (each account pipeline
# does several DB round trips) plus regular user traffic on top. Defaults are
# generous headroom over Motor's own default (100) without requiring a
# dedicated Atlas tier; override via MONGO_MAX_POOL_SIZE if the cluster's own
# connection limit is lower. minPoolSize keeps a few warm connections ready
# so the very first requests after a burst don't pay a fresh-connection cost.
import os as _os
_MONGO_MAX_POOL_SIZE = int(_os.getenv("MONGO_MAX_POOL_SIZE", "200"))
_MONGO_MIN_POOL_SIZE = int(_os.getenv("MONGO_MIN_POOL_SIZE", "10"))

_log.info("Connecting to your Mongo Database...")
try:
    _client = AsyncIOMotorClient(
        MONGO_DB_URI,
        maxPoolSize=_MONGO_MAX_POOL_SIZE,
        minPoolSize=_MONGO_MIN_POOL_SIZE,
    )
    mongodb = _client.Apiserverdb
    _log.info("Connected to your Mongo Database.")
except Exception as e:
    # Log only the exception type, never str(e) — pymongo/Motor error messages
    # can echo back the connection string (including the embedded credentials)
    # verbatim, which would otherwise land in plaintext in the log file.
    _log.error("Failed to connect to your Mongo Database (%s). Check MONGO_DB_URI.", type(e).__name__)
    raise SystemExit(1)


def reinit() -> None:
    """
    Recreate the Motor client inside the already-running event loop.

    Call this as the very first thing inside async main() to ensure all
    MongoDB operations run on the correct asyncio loop.  After this call,
    every _LazyCollection proxy automatically routes through the new client
    — no manual re-patching of database module variables is needed.
    """
    global _client, mongodb
    _log.info("Re-initialising MongoDB client inside running event loop...")
    old_client = _client
    _client = AsyncIOMotorClient(
        MONGO_DB_URI,
        maxPoolSize=_MONGO_MAX_POOL_SIZE,
        minPoolSize=_MONGO_MIN_POOL_SIZE,
    )
    mongodb = _client.Apiserverdb
    if old_client is not None:
        # Close the loop-less client created at import time so its socket
        # pool doesn't linger — reinit() only ever runs once at startup, but
        # leaving the old client open would otherwise leak a connection.
        old_client.close()
    _log.info("MongoDB client re-initialised.")


class _LazyCollection:
    """
    Transparent proxy to a MongoDB collection.

    Always resolves through the *current* `mongodb` object so that after
    `reinit()` rebinds the client, all subsequent database calls use the
    new connection — without any manual variable patching.

    Usage (in database modules):
        usersdb = collection("users")
        await usersdb.find_one({"user_id": 123})   # works exactly as before
    """
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def __getattr__(self, item: str):
        name = object.__getattribute__(self, "_name")
        return getattr(getattr(mongodb, name), item)

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_name")
        return f"<_LazyCollection '{name}'>"


def collection(name: str) -> _LazyCollection:
    """Return a lazy proxy to the named collection in the current database."""
    return _LazyCollection(name)
