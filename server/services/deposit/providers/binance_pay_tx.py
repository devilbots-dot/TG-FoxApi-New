"""
Binance Pay Transaction-Verify Deposit Provider.

Flow:
  1. User sends USDT to the merchant's Binance Pay ID.
  2. User comes back, taps "Verify Payment", and enters their Binance Order ID.
  3. The bot queries GET /sapi/v1/pay/transactions (standard Binance account API)
     to confirm the matching payment was received.

Configuration (Replit Secrets):
  BINANCE_ACCOUNT_API_KEY    — standard Binance account API key (not Pay Merchant key)
  BINANCE_ACCOUNT_SECRET_KEY — standard Binance account API secret
  BINANCE_PAY_UID            — merchant's Binance Pay UID shown to buyers

Optional:
  DEPOSIT_SCAN_TTL       — deposit lifetime in minutes (default: 60)
  BINANCE_PROXY_SERVER   — proxy host/IP used for every Binance API call
                           (may include a scheme: http://, https://, socks5://, socks5h://, socks4://)
  BINANCE_PROXY_PORT     — proxy port
  BINANCE_PROXY_USERNAME — proxy username (optional)
  BINANCE_PROXY_PASSWORD — proxy password (optional)

API docs:
  https://developers.binance.com/en/docs/catalog/investment-and-services-pay/api/rest-api/~
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from os import getenv
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

# PROXY CHANGE: aiohttp cannot speak SOCKS natively, so SOCKS5/SOCKS4 support is
# provided by aiohttp_socks. The import is optional — if the package is missing we
# fall back gracefully (and log a clear error) instead of crashing at import time.
try:  # pragma: no cover - trivial import guard
    from aiohttp_socks import ProxyConnector  # type: ignore
except Exception:  # ImportError or any packaging issue
    ProxyConnector = None  # type: ignore[assignment]

from server.logging import LOGGER
from server.services.deposit.base import DepositProvider, PaymentDetails

_log = LOGGER(__name__)

_BASE_URL = "https://api.binance.com"
_TIMEOUT  = aiohttp.ClientTimeout(total=30, connect=10)


def _sign(secret: str, query_string: str) -> str:
    """HMAC-SHA256 signature for standard Binance REST API."""
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class BinancePayTxProvider(DepositProvider):
    """
    Manual Binance Pay deposit — user sends USDT, then verifies with Order ID.

    No payment link is created; the provider just stores a pending deposit record
    and waits for the user to submit their Binance Pay Order ID for verification.
    """

    method_id   = "binance_pay_tx"
    method_name = "Binance Pay (Auto)"
    networks: list[str] = []

    def is_configured(self) -> bool:
        api_key    = getenv("BINANCE_ACCOUNT_API_KEY", "").strip()
        secret_key = getenv("BINANCE_ACCOUNT_SECRET_KEY", "").strip()
        pay_uid    = getenv("BINANCE_PAY_UID", "").strip()
        return bool(api_key and secret_key and pay_uid)

    async def create_payment(
        self,
        deposit_id: str,
        amount: float,
        user_id: int,
        network: Optional[str] = None,
    ) -> PaymentDetails:
        pay_uid = getenv("BINANCE_PAY_UID", "").strip()
        ttl     = int(getenv("DEPOSIT_SCAN_TTL", "60"))

        instructions = (
            f"Send exactly **${amount:.2f} USDT** to Binance Pay ID:\n"
            f"`{pay_uid}`\n\n"
            f"After sending, tap **Verify Payment** and enter your Binance Order ID.\n"
            f"The payment is verified automatically with Binance and credited instantly."
        )

        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl)

        return PaymentDetails(
            currency     = "USDT",
            instructions = instructions,
            expires_at   = expires_at,
            extra={
                "provider":    "binance_pay_tx",
                "pay_uid":     pay_uid,
                "ttl_minutes": ttl,
            },
        )


# ── Proxy support ─────────────────────────────────────────────────────────────
# PROXY CHANGE: the whole proxy layer below was reworked so that *every* Binance
# API call goes through exactly one code path (`_binance_session()`), supporting
# both HTTP/HTTPS proxies (aiohttp `proxy=`/`proxy_auth=`) and SOCKS proxies
# (aiohttp_socks `ProxyConnector`). Credentials are never logged.

_SOCKS_SCHEMES = ("socks5h", "socks5", "socks4a", "socks4")


def _parse_proxy_env() -> Optional[dict[str, Any]]:
    """
    PROXY CHANGE: single source of truth for proxy configuration.

    Reads the (unchanged) BINANCE_PROXY_* env vars and returns a normalised dict:
        {"scheme", "host", "port", "user", "password", "url", "is_socks", "safe_url"}
    or None when no proxy is configured.

    `safe_url` never contains credentials and is the only form that is logged.
    """
    host = getenv("BINANCE_PROXY_SERVER", "").strip()
    if not host:
        return None

    port = getenv("BINANCE_PROXY_PORT", "").strip()
    user = getenv("BINANCE_PROXY_USERNAME", "").strip()
    pwd  = getenv("BINANCE_PROXY_PASSWORD", "").strip()

    # Scheme may be embedded in BINANCE_PROXY_SERVER (e.g. "socks5://1.2.3.4").
    if "://" in host:
        scheme, _, hostpart = host.partition("://")
        scheme = scheme.strip().lower()
    else:
        scheme, hostpart = "http", host

    # Strip any credentials accidentally embedded in the host part so they are
    # never logged; they are re-applied explicitly below.
    if "@" in hostpart:
        creds, _, hostpart = hostpart.rpartition("@")
        embedded_user, _, embedded_pwd = creds.partition(":")
        user = user or embedded_user
        pwd  = pwd or embedded_pwd

    hostpart = hostpart.strip().strip("/")
    if port and ":" not in hostpart:
        hostpart = f"{hostpart}:{port}"

    host_only, _, port_str = hostpart.partition(":")
    try:
        port_num = int(port_str) if port_str else (1080 if scheme.startswith("socks") else 8080)
    except ValueError:
        port_num = 1080 if scheme.startswith("socks") else 8080

    return {
        "scheme":   scheme,
        "host":     host_only,
        "port":     port_num,
        "user":     user or None,
        "password": pwd or None,
        # Full URL including credentials — for connection use only, never logged.
        "url":      f"{scheme}://{user}:{pwd}@{hostpart}" if user else f"{scheme}://{hostpart}",
        "is_socks": scheme in _SOCKS_SCHEMES,
        # Credential-free form, safe for logs.
        "safe_url": f"{scheme}://{hostpart}",
    }


def _proxy_config() -> tuple[Optional[str], Optional[aiohttp.BasicAuth]]:
    """
    Backward-compatible helper kept for any external callers.

    Returns (proxy_url, proxy_auth) for HTTP/HTTPS proxies, and (None, None)
    when no proxy is configured or when a SOCKS proxy is configured (SOCKS is
    handled by a connector, not by aiohttp's `proxy=` parameter).
    """
    cfg = _parse_proxy_env()
    if not cfg or cfg["is_socks"]:
        return None, None
    auth = aiohttp.BasicAuth(cfg["user"], cfg["password"] or "") if cfg["user"] else None
    return cfg["safe_url"] if not cfg["user"] else f"{cfg['scheme']}://{cfg['host']}:{cfg['port']}", auth


@asynccontextmanager
async def _binance_session():
    """
    PROXY CHANGE: the one and only place where an aiohttp session for Binance is
    created. Yields `(session, request_kwargs)`:

      * No proxy configured  → plain session, empty kwargs (identical to before).
      * HTTP/HTTPS proxy     → plain session + {"proxy": ..., "proxy_auth": ...}.
      * SOCKS4/SOCKS5 proxy  → session built on aiohttp_socks.ProxyConnector,
                               empty kwargs (aiohttp's `proxy=` cannot do SOCKS).

    Callers must pass `**request_kwargs` to every request so the proxy is always
    applied. Timeout handling (`_TIMEOUT`) is unchanged.
    """
    cfg = _parse_proxy_env()

    if not cfg:
        _log.info("binance_pay_tx: no proxy configured — direct connection to Binance API")
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            yield session, {}
        return

    if cfg["is_socks"]:
        if ProxyConnector is None:
            # Fail loudly rather than silently bypassing the proxy.
            raise RuntimeError(
                "SOCKS proxy configured but 'aiohttp_socks' is not installed. "
                "Install it with: pip install aiohttp_socks"
            )
        _log.info(
            "binance_pay_tx: using SOCKS proxy %s (auth=%s) for Binance API calls",
            cfg["safe_url"], "yes" if cfg["user"] else "no",
        )
        connector = ProxyConnector.from_url(cfg["url"])  # credentials embedded, never logged
        async with aiohttp.ClientSession(timeout=_TIMEOUT, connector=connector) as session:
            yield session, {}
        return

    # HTTP / HTTPS proxy — aiohttp's native support.
    _log.info(
        "binance_pay_tx: using HTTP proxy %s (auth=%s) for Binance API calls",
        cfg["safe_url"], "yes" if cfg["user"] else "no",
    )
    proxy_auth = aiohttp.BasicAuth(cfg["user"], cfg["password"] or "") if cfg["user"] else None
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        yield session, {"proxy": cfg["safe_url"], "proxy_auth": proxy_auth}


# ── Duplicate Order ID protection ─────────────────────────────────────────────

def _orders_collection():
    from server.core.mongo import collection
    return collection("binance_pay_orders")


async def is_order_id_used(order_id: str) -> Optional[dict]:
    """Return the existing claim document when this Order ID was already used."""
    oid = (order_id or "").strip()
    if not oid:
        return None
    try:
        doc = await _orders_collection().find_one({"_id": oid})
        if doc:
            return doc
    except Exception as exc:
        _log.warning("binance_pay_tx: duplicate lookup failed for %s: %s", oid, exc)
    # Fallback: scan deposits that already recorded this order id
    try:
        from server.utils.database.walletdb import depositsdb
        dep = await depositsdb.find_one({"extra.binance_order_id": oid})
        if dep:
            return {"_id": oid, "deposit_id": dep.get("deposit_id"),
                    "user_id": dep.get("user_id")}
    except Exception:
        pass
    return None


async def claim_order_id(order_id: str, deposit_id: str, user_id: int,
                         amount: float) -> bool:
    """
    Atomically reserve an Order ID. Returns False when it was already used
    (by this or any other user) — the caller must then refuse to credit.
    """
    oid = (order_id or "").strip()
    if not oid:
        return False
    from datetime import datetime as _dt
    try:
        await _orders_collection().insert_one({
            "_id":        oid,
            "deposit_id": deposit_id,
            "user_id":    user_id,
            "amount":     float(amount or 0.0),
            "claimed_at": _dt.now(timezone.utc),
        })
        return True
    except Exception as exc:
        if "duplicate key" in str(exc).lower() or "E11000" in str(exc):
            _log.warning(
                "binance_pay_tx: duplicate Order ID %s rejected (deposit=%s user=%s)",
                oid, deposit_id, user_id,
            )
            return False
        # Storage problem — fail closed only if the id already exists elsewhere
        _log.error("binance_pay_tx: claim_order_id error for %s: %s", oid, exc)
        return await is_order_id_used(oid) is None


async def release_order_id(order_id: str) -> None:
    """Undo a claim (used when crediting failed after the claim)."""
    oid = (order_id or "").strip()
    if not oid:
        return
    try:
        await _orders_collection().delete_one({"_id": oid})
    except Exception:
        pass


# ── Transaction lookup ────────────────────────────────────────────────────────

async def verify_by_order_id(
    order_id: str,
    expected_amount: float,
    deposit_id: str,
) -> dict:
    """
    Query GET /sapi/v1/pay/transactions and look for a transaction whose
    orderId matches `order_id` and whose amount matches `expected_amount`.

    Returns:
      ok          — True if a matching confirmed transaction was found
      tx          — the matched transaction dict (when ok=True)
      error       — error message (when ok=False)
      not_found   — True when API worked but order ID was not in the list
    """
    # ── Duplicate protection: an Order ID may only ever be used once ──────────
    used = await is_order_id_used(order_id)
    if used and str(used.get("deposit_id") or "") != str(deposit_id):
        return {
            "ok": False,
            "duplicate": True,
            "error": "This Binance Order ID has already been used for another deposit.",
        }

    api_key = getenv("BINANCE_ACCOUNT_API_KEY", "").strip()
    secret  = getenv("BINANCE_ACCOUNT_SECRET_KEY", "").strip()

    if not api_key or not secret:
        return {"ok": False, "error": "BINANCE_ACCOUNT_API_KEY / BINANCE_ACCOUNT_SECRET_KEY not configured"}

    # Fetch last 100 transactions (max allowed per call)
    params = {
        "limit":     100,
        "timestamp": int(time.time() * 1000),
    }
    query_string = urlencode(params)
    signature    = _sign(secret, query_string)
    url          = f"{_BASE_URL}/sapi/v1/pay/transactions?{query_string}&signature={signature}"
    headers      = {"X-MBX-APIKEY": api_key}

    try:
        # PROXY CHANGE: session + per-request proxy kwargs come from the shared
        # helper, so HTTP and SOCKS proxies are both honoured here and in any
        # future Binance call. URL, headers, params and parsing are unchanged.
        async with _binance_session() as (session, request_kwargs):
            async with session.get(url, headers=headers, **request_kwargs) as resp:
                raw_text = await resp.text()
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    return {"ok": False, "error": f"Non-JSON response (HTTP {resp.status})"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Request timed out"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        # PROXY CHANGE: misconfigured/unavailable SOCKS support surfaces as a
        # normal error result instead of an unhandled exception.
        _log.error("binance_pay_tx: proxy configuration error: %s", exc)
        return {"ok": False, "error": str(exc)}

    # API-level error
    # Binance Pay /sapi/v1/pay/transactions returns "000000" on success;
    # some endpoints return integer 0. Accept both as success.
    _SUCCESS_CODES = {"0", "000000"}
    raw_code = data.get("code")
    if raw_code is not None and str(raw_code) not in _SUCCESS_CODES:
        msg = data.get("msg") or data.get("message") or f"code={raw_code}"
        return {"ok": False, "error": msg}

    transactions = data.get("data") or data.get("rows") or []

    # Search for matching order ID
    for tx in transactions:
        if str(tx.get("orderId", "")).strip() == order_id.strip():
            # Verify amount — allow ±1 cent tolerance for rounding
            try:
                tx_amount = float(tx.get("amount", 0))
            except (TypeError, ValueError):
                tx_amount = 0.0

            tx_status = str(tx.get("transactionStatus") or tx.get("status") or "").upper()
            if tx_status and tx_status not in ("S", "SUCCESS", "PAY_SUCCESS", "0", "COMPLETED"):
                return {
                    "ok": False,
                    "error": f"Order ID found but its Binance status is `{tx_status}` (not completed).",
                }

            if abs(tx_amount) < expected_amount * 0.99:
                return {
                    "ok":    False,
                    "error": (
                        f"Order ID found but amount mismatch: "
                        f"expected ${expected_amount:.2f}, got ${abs(tx_amount):.2f} USDT."
                    ),
                }

            _log.info(
                "binance_pay_tx: order %s matched for deposit %s (amount=%.4f)",
                order_id, deposit_id, tx_amount,
            )
            return {"ok": True, "tx": tx}

    return {"ok": False, "not_found": True, "error": f"Order ID `{order_id}` not found in your recent transactions."}
