"""
Proxy management — RAM-first.

Reads  : served from memstore._proxies (zero Mongo round-trips).
Writes : RAM updated immediately; MongoDB synced async via sync_write().
         Exception: fail_count increments use sync_write (non-critical,
         best-effort). delete/toggle use immediate await (admin-critical).
"""

import asyncio
import secrets
import time as _time
from typing import Optional, List

from server.core.mongo import collection
from server.core import memstore
from server.utils.common import utcnow as _now

proxiesdb = collection("proxies")


def _gen_id() -> str:
    return f"PRX-{secrets.token_hex(5).upper()}"


# ── Read helpers (all from RAM) ───────────────────────────────────────────────

async def get_active_proxy_for_country(country_code: str) -> Optional[dict]:
    return memstore.get_proxy_for_country(country_code)


async def get_proxies_for_country(country_code: str) -> List[dict]:
    return memstore.get_proxies_for_country(country_code)


async def list_all_proxies() -> List[dict]:
    return memstore.get_all_proxies()


# ── Write helpers (RAM first, Mongo async) ────────────────────────────────────

async def add_proxy(
    country_code: str,
    host: str,
    port: int,
    proxy_type: str = "socks5",
    username: str = "",
    password: str = "",
) -> str:
    proxy_id = _gen_id()
    doc = {
        "proxy_id":     proxy_id,
        "country_code": country_code.upper(),
        "host":         host.strip(),
        "port":         int(port),
        "type":         proxy_type.lower(),
        "username":     username,
        "password":     password,
        "is_active":    True,
        "added_at":     _now(),
        "fail_count":   0,
    }
    memstore.set_proxy(doc)

    _doc = dict(doc)
    memstore.sync_write(
        lambda: proxiesdb.insert_one(_doc),
        f"add_proxy:{proxy_id}",
    )
    return proxy_id


async def delete_proxy(proxy_id: str) -> bool:
    memstore.del_proxy(proxy_id)
    r = await proxiesdb.delete_one({"proxy_id": proxy_id})
    return r.deleted_count > 0


async def toggle_proxy(proxy_id: str, is_active: bool) -> bool:
    memstore.update_proxy_field(proxy_id, is_active=is_active)
    r = await proxiesdb.update_one(
        {"proxy_id": proxy_id},
        {"$set": {"is_active": is_active}},
    )
    return r.modified_count > 0


async def increment_proxy_fail(proxy_id: str) -> None:
    """Deprioritise a proxy that just failed. Best-effort async sync."""
    if proxy_id in memstore._proxies:
        memstore._proxies[proxy_id]["fail_count"] = (
            memstore._proxies[proxy_id].get("fail_count", 0) + 1
        )
    _pid = proxy_id
    memstore.sync_write(
        lambda: proxiesdb.update_one({"proxy_id": _pid}, {"$inc": {"fail_count": 1}}),
        f"proxy_fail:{proxy_id}",
    )


async def reset_proxy_fails(proxy_id: str) -> None:
    memstore.update_proxy_field(proxy_id, fail_count=0)
    _pid = proxy_id
    memstore.sync_write(
        lambda: proxiesdb.update_one({"proxy_id": _pid}, {"$set": {"fail_count": 0}}),
        f"proxy_reset:{proxy_id}",
    )


async def test_proxy_connection(
    host: str,
    port: int,
    proxy_type: str = "socks5",
    username: str = "",
    password: str = "",
    timeout: int = 8,
) -> dict:
    """
    Verify a proxy actually works.
    SOCKS5: open a TCP connection to api.telegram.org:443 through the proxy.
    HTTP:   open a raw TCP connection to the proxy host:port.
    Returns {"ok": True, "latency_ms": int} or {"ok": False, "detail": str}.
    """
    proxy_type = (proxy_type or "socks5").lower()
    start = _time.monotonic()
    try:
        if proxy_type == "socks5":
            import socks
            s = socks.socksocket()
            s.set_proxy(
                socks.SOCKS5,
                host.strip(),
                int(port),
                username=username or None,
                password=password or None,
            )
            s.settimeout(timeout)
            s.connect(("api.telegram.org", 443))
            s.close()
        elif proxy_type == "http":
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host.strip(), int(port)),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
        else:
            return {"ok": False, "detail": f"Unsupported proxy type: {proxy_type}"}

        elapsed = round((_time.monotonic() - start) * 1000)
        return {"ok": True, "latency_ms": elapsed}
    except Exception as e:
        return {"ok": False, "detail": str(e)}
