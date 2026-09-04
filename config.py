import re
import sys
from os import getenv
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()


# ── Required env var checker ──────────────────────────────────────────────────

def _require(key: str, cast=str, hint: str = ""):
    val = getenv(key)
    if val is None or val.strip() == "":
        msg = f"[FATAL] Required secret '{key}' is not set."
        if hint:
            msg += f" {hint}"
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    try:
        return cast(val)
    except (ValueError, TypeError):
        print(
            f"[FATAL] Secret '{key}' has an invalid value '{val}'. Expected {cast.__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)


# ── Core Telegram credentials ────────────────────────────────────────────────
API_ID   = _require("API_ID",   int,  "Get it from https://my.telegram.org/apps")
API_HASH = _require("API_HASH", str,  "Get it from https://my.telegram.org/apps")
BOT_TOKEN = _require("BOT_TOKEN", str, "Get it from @BotFather on Telegram").strip()
if ":" not in BOT_TOKEN or not BOT_TOKEN.partition(":")[0].isdigit():
    print("[FATAL] BOT_TOKEN has an invalid format. Set the current token from @BotFather.", file=sys.stderr)
    raise SystemExit(1)

# ── MongoDB ──────────────────────────────────────────────────────────────────
MONGO_DB_URI = _require("MONGO_DB_URI", str, "Get it from https://cloud.mongodb.com")

# ── Session encryption ────────────────────────────────────────────────────────
_require("SESSION_SECRET", str, "Fernet key for encrypting 2FA passwords.")

# ── Owner ────────────────────────────────────────────────────────────────────
OWNER_ID = _require("OWNER_ID", int, "Your Telegram user ID. Get it from @MissRose_Bot via /id")

# ── API server ────────────────────────────────────────────────────────────────
# Port the uvicorn server binds on. Render/Heroku/Railway inject $PORT
# automatically — honour it first, then fall back to API_PORT, then 1470.
_api_port_raw = (getenv("PORT") or getenv("API_PORT") or "1470").strip()
try:
    API_PORT = int(_api_port_raw)
except (ValueError, TypeError):
    print(f"[FATAL] API_PORT must be an integer. Got: {_api_port_raw!r}", file=sys.stderr)
    raise SystemExit(1)

# ── Optional — Log group / Session channel ────────────────────────────────────
def _optional_int(key: str) -> "int | None":
    val = getenv(key)
    if not val or not val.strip():
        return None
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        print(
            f"[FATAL] Optional secret '{key}' must be an integer. Got: {val!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)

LOG_GROUP_ID       = _optional_int("LOG_GROUP_ID")
SESSION_CHANNEL_ID = _optional_int("SESSION_CHANNEL_ID")

# ── Optional bot identity ─────────────────────────────────────────────────────
# Required by the Telegram Stars deposit provider to build payment deep-links.
# Set to your bot's username WITHOUT the @ prefix.
BOT_USERNAME: str = getenv("BOT_USERNAME", "")

# ── Support links ────────────────────────────────────────────────────────────
SUPPORT_CHANNEL  = getenv("SUPPORT_CHANNEL",  "https://t.me/TgFoxApi")
SUPPORT_GROUP    = getenv("SUPPORT_GROUP",    "https://t.me/TgFoxApi")
UPDATES_CHANNEL  = getenv("UPDATES_CHANNEL",  "https://t.me/TgFoxSells")

# ── Support UID ───────────────────────────────────────────────────────────────
# Telegram user ID that "Contact Support" inline buttons should open.
# Set env var SUPPORT_USER_ID to the numeric UID (e.g. 123456789).
try:
    SUPPORT_USER_ID: int = int(getenv("SUPPORT_USER_ID", "0") or "0")
except (TypeError, ValueError):
    SUPPORT_USER_ID = 0


def support_link() -> str:
    """Return a tg://user link for the configured support UID; fallback to SUPPORT_GROUP."""
    if SUPPORT_USER_ID:
        return f"tg://user?id={SUPPORT_USER_ID}"
    return SUPPORT_GROUP

# ── OxaPay Payout (Withdrawal) ────────────────────────────────────────────────
# Payout API key from OxaPay dashboard → Payout Service → Generate Payout API Key
OXAPAY_PAYOUT_API_KEY: str = getenv("OXAPAY_PAYOUT_API_KEY", "")

# Base URL for OxaPay v1 API (override for sandbox/mock servers if needed)
OXAPAY_BASE_URL: str = getenv("OXAPAY_BASE_URL", "https://api.oxapay.com/v1")

# Sandbox mode — set OXAPAY_SANDBOX=false in production to send real payouts
OXAPAY_SANDBOX: bool = getenv("OXAPAY_SANDBOX", "false").lower() not in ("false", "0", "no", "off")


def _build_webhook_url(env_key: str, path: str) -> str:
    """
    Build a publicly-accessible HTTPS webhook URL.
    Priority:
      1. REPLIT_DEV_DOMAIN (always used when available — ensures correct domain
         on every restart even if an old value is stored in env/secrets)
      2. Explicit env var (e.g. WITHDRAWAL_CALLBACK_URL) — used as fallback
         when not running on Replit (e.g. custom VPS domain)
    Always normalises double-slashes that can appear when env vars are copy-pasted.
    """
    import re as _re
    dev_domain = getenv("REPLIT_DEV_DOMAIN", "").strip()
    if dev_domain:
        url = f"https://{dev_domain}{path}"
    else:
        url = getenv(env_key, "").strip()
    # Normalise: collapse any // that is NOT right after the scheme
    return _re.sub(r"(?<!:)//+", "/", url)


# Publicly accessible HTTPS URL where OxaPay POSTs payout status callbacks.
# e.g. https://your-repl.replit.app/webhooks/withdrawal
WITHDRAWAL_CALLBACK_URL: str = _build_webhook_url("WITHDRAWAL_CALLBACK_URL", "/webhooks/withdrawal")

# Publicly accessible HTTPS URL where OxaPay POSTs deposit status callbacks.
# e.g. https://your-repl.replit.app/webhooks/deposit
DEPOSIT_WEBHOOK_URL: str = _build_webhook_url("DEPOSIT_WEBHOOK_URL", "/webhooks/deposit")

# Publicly accessible HTTPS URL where Binance Pay POSTs payment callbacks.
# e.g. https://your-repl.replit.app/webhooks/binance_pay
BINANCE_PAY_WEBHOOK_URL: str = _build_webhook_url("BINANCE_PAY_WEBHOOK_URL", "/webhooks/binance_pay")

# ── Binance Pay (Auto) — Order ID verification ───────────────────────────────────
# Standard Binance account API key + secret (NOT the Pay Merchant API key).
# Used to query GET /sapi/v1/pay/transactions to verify user-submitted Order IDs.
BINANCE_ACCOUNT_API_KEY:    str = getenv("BINANCE_ACCOUNT_API_KEY",    "")
BINANCE_ACCOUNT_SECRET_KEY: str = getenv("BINANCE_ACCOUNT_SECRET_KEY", "")

# Your Binance Pay UID — displayed to buyers so they know where to send USDT.
BINANCE_PAY_UID: str = getenv("BINANCE_PAY_UID", "")

# Optional proxy for every Binance API call (useful when the host IP is blocked).
BINANCE_PROXY_SERVER:   str = getenv("BINANCE_PROXY_SERVER",   "")
BINANCE_PROXY_PORT:     str = getenv("BINANCE_PROXY_PORT",     "")
BINANCE_PROXY_USERNAME: str = getenv("BINANCE_PROXY_USERNAME", "")
BINANCE_PROXY_PASSWORD: str = getenv("BINANCE_PROXY_PASSWORD", "")

# Withdrawal limits (USD)
WITHDRAWAL_MIN_AMOUNT: float  = float(getenv("WITHDRAWAL_MIN_AMOUNT",  "1.0"))
WITHDRAWAL_MAX_AMOUNT: float  = float(getenv("WITHDRAWAL_MAX_AMOUNT",  "10000.0"))
WITHDRAWAL_DAILY_LIMIT: float = float(getenv("WITHDRAWAL_DAILY_LIMIT", "1000.0"))
WITHDRAWAL_MONTHLY_LIMIT: float = float(getenv("WITHDRAWAL_MONTHLY_LIMIT", "10000.0"))
WITHDRAWAL_MAX_PENDING: int   = int(getenv("WITHDRAWAL_MAX_PENDING", "3"))

# Withdrawal fee: percentage (e.g. "1.0" = 1%) + fixed (e.g. "0.5" = $0.50)
WITHDRAWAL_FEE_PERCENT: float = float(getenv("WITHDRAWAL_FEE_PERCENT", "0.10"))
WITHDRAWAL_FEE_FIXED:   float = float(getenv("WITHDRAWAL_FEE_FIXED",   "0.0"))

# Gateway mode: "auto" = submit to gateway immediately; "manual" = admin approves
WITHDRAWAL_GATEWAY: str = getenv("WITHDRAWAL_GATEWAY", "auto")

# OxaPay network aliases — how to translate our network names to OxaPay's names
# TRC20 → TRX, BEP20 → BSC (configurable via env)
OXAPAY_NETWORK_TRC20: str = getenv("OXAPAY_NETWORK_TRC20", "TRX")
OXAPAY_NETWORK_BEP20: str = getenv("OXAPAY_NETWORK_BEP20", "BSC")

# Currency to send (USDT by default — the platform currency)
OXAPAY_WITHDRAW_CURRENCY: str = getenv("OXAPAY_WITHDRAW_CURRENCY", "USDT")

# Verification worker: how often to poll OxaPay for pending withdrawal status (seconds)
WITHDRAWAL_VERIFY_INTERVAL_S: int = int(getenv("WITHDRAWAL_VERIFY_INTERVAL_S", "300"))

# How many hours before an unresolved withdrawal is marked expired
WITHDRAWAL_EXPIRE_HOURS: float = float(getenv("WITHDRAWAL_EXPIRE_HOURS", "72"))

START_IMG_URL = getenv(
    "START_IMG_URL",
    "https://image2url.com/r2/default/images/1773908941970-6c273f0b-3d1c-4e9f-a07b-67a2c81e300d.jpg",
)


# ── Validate URLs ────────────────────────────────────────────────────────────

def _check_url(val: str, key: str):
    if val and not re.match(r"(?:http|https)://", val):
        print(
            f"[FATAL] '{key}' must start with https://. Got: {val}",
            file=sys.stderr,
        )
        raise SystemExit(1)




# WebApp mini-app public URL. Render exposes RENDER_EXTERNAL_URL automatically;
# WEBAPP_BASE_URL remains an explicit custom-domain override. The Mini App is
# served by this same Python process at /app/ and does not use a second account.
WEBAPP_BASE_URL = (
    getenv("WEBAPP_BASE_URL", "")
    or getenv("RENDER_EXTERNAL_URL", "")
    or getenv("REPLIT_DEV_DOMAIN", "")
).strip()
if WEBAPP_BASE_URL and not WEBAPP_BASE_URL.startswith(("http://", "https://")):
    WEBAPP_BASE_URL = f"https://{WEBAPP_BASE_URL}"
WEBAPP_BASE_URL = WEBAPP_BASE_URL.rstrip("/")
# The Python server serves the Mini App at the root `/app/` path. Older
# deployments sometimes stored `/webapp` or `/app` in WEBAPP_BASE_URL, which
# produced invalid URLs such as `/webapp/app/` and led to a 404 page.
if WEBAPP_BASE_URL:
    _webapp_parts = urlsplit(WEBAPP_BASE_URL)
    if _webapp_parts.path not in ("", "/") or _webapp_parts.query or _webapp_parts.fragment:
        print(
            "[WARN] WEBAPP_BASE_URL should be an origin; ignoring its path/query "
            "because the Mini App is served at /app/.",
            file=sys.stderr,
        )
        WEBAPP_BASE_URL = urlunsplit(
            (_webapp_parts.scheme, _webapp_parts.netloc, "", "", "")
        ).rstrip("/")
WEBAPP_URL = f"{WEBAPP_BASE_URL}/app/" if WEBAPP_BASE_URL else ""
if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    print(
        "[FATAL] WEBAPP_BASE_URL must resolve to a public HTTPS URL for Telegram Mini Apps.",
        file=sys.stderr,
    )
    raise SystemExit(1)

_check_url(SUPPORT_CHANNEL, "SUPPORT_CHANNEL")
_check_url(SUPPORT_GROUP,   "SUPPORT_GROUP")
_check_url(UPDATES_CHANNEL, "UPDATES_CHANNEL")
