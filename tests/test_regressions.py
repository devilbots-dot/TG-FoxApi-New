import hashlib
import hmac
import io
import json
import os
import sys
import time
from urllib.parse import urlencode
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _safe_import_environment():
    """Provide only dummy values if a test imports the application package."""
    values = {
        "API_ID": "1",
        "API_HASH": "test-api-hash",
        "BOT_TOKEN": "123456:TEST",
        "MONGO_DB_URI": "mongodb://127.0.0.1:27017",
        "SESSION_SECRET": "test-session-secret",
        "OWNER_ID": "1",
        "ADMIN_PASSWORD": "test-admin-password",
    }
    for key, value in values.items():
        os.environ.setdefault(key, value)


def test_market_quantity_callback_resolves_delivery_helper():
    source = (ROOT / "server/plugins/bot/market.py").read_text()
    assert "async def _deliver_one_session(" in source
    assert "outcome = await _deliver_one_session(" in source


def test_config_support_links_use_environment_keys():
    source = (ROOT / "config.py").read_text()
    assert 'getenv("SUPPORT_GROUP"' in source
    assert 'getenv("UPDATES_CHANNEL"' in source
    assert 'getenv("https://t.me/TgFoxApi"' not in source


def test_bot_token_configuration_is_normalized_and_startup_identity_is_guarded():
    config_source = (ROOT / "config.py").read_text()
    bot_source = (ROOT / "server/core/bot.py").read_text()
    assert 'BOT_TOKEN = _require("BOT_TOKEN", str, "Get it from @BotFather on Telegram").strip()' in config_source
    assert 'BOT_TOKEN has an invalid format' in config_source
    assert 'token_bot_id = int(config.BOT_TOKEN.partition(":" )[0])' not in bot_source
    assert 'token_bot_id = int(config.BOT_TOKEN.partition(":")[0])' in bot_source
    assert 'if token_bot_id != self.id:' in bot_source


def _signed_webapp_init_data(bot_token: str, **overrides: str) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAEAAAEAAAAB",
        "user": json.dumps({"id": 42, "first_name": "Test"}, separators=(",", ":")),
    }
    fields.update(overrides)
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_webapp_init_data_accepts_valid_signed_fixture():
    from server.webapp.auth import verify_init_data

    init_data = _signed_webapp_init_data("123456:TEST")
    verified = verify_init_data(init_data, bot_token="123456:TEST")
    assert verified["user"]["id"] == 42


def test_webapp_init_data_matches_fixed_telegram_hmac_vector(monkeypatch):
    """Keep the Telegram key/message order pinned by a time-independent vector."""
    from server.webapp import auth

    monkeypatch.setattr(auth.time, "time", lambda: 1700000100)
    init_data = (
        "auth_date=1700000000&query_id=AAEAAAEAAAAB&"
        "user=%7B%22id%22%3A42%2C%22first_name%22%3A%22Test%22%7D&"
        "hash=4d76d859fa0d42e5626dae3c7cd878a45c9ae95551ca092dffd2924253a2ff16"
    )
    verified = auth.verify_init_data(init_data, bot_token="123456:TEST")
    assert verified["auth_date"] == "1700000000"
    assert verified["user"] == {"id": 42, "first_name": "Test"}


def test_webapp_init_data_includes_newer_signature_field_in_hmac_payload():
    from server.webapp.auth import verify_init_data

    init_data = _signed_webapp_init_data("123456:TEST", signature="telegram-ed25519-signature")
    verified = verify_init_data(init_data, bot_token="123456:TEST")
    assert verified["signature"] == "telegram-ed25519-signature"


def test_webapp_hmac_diagnostic_logs_only_safe_request_metadata(caplog):
    from fastapi import HTTPException
    from server.webapp.auth import verify_init_data

    caplog.set_level("WARNING", logger="server.webapp.auth")
    init_data = _signed_webapp_init_data("123456:TEST", signature="telegram-ed25519-signature")
    with pytest.raises(HTTPException, match="Telegram verification failed"):
        verify_init_data(init_data.replace("Test", "Other"), bot_token="123456:TEST", request_id="diag-request-123")
    message = caplog.messages[-1]
    assert "diag=tma-hmac-v5" in message
    assert "request_id=diag-request-123" in message
    assert "stage=hmac-mismatch" in message
    assert "fields=auth_date,hash,query_id,signature,user" in message
    assert "123456:TEST" not in message
    assert "telegram-ed25519-signature" not in message


def test_webapp_init_data_rejects_tampering_and_duplicate_fields():
    from fastapi import HTTPException
    from server.webapp.auth import verify_init_data

    valid = _signed_webapp_init_data("123456:TEST")
    with pytest.raises(HTTPException, match="Telegram verification failed"):
        verify_init_data(valid.replace("Test", "Other"), bot_token="123456:TEST")
    with pytest.raises(HTTPException, match="Malformed initData"):
        verify_init_data(f"{valid}&auth_date=1", bot_token="123456:TEST")


def test_webapp_init_data_rejects_reversed_hmac_key_message_order():
    from fastapi import HTTPException
    from server.webapp.auth import verify_init_data

    bot_token = "123456:TEST"
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAEAAAEAAAAB",
        "user": json.dumps({"id": 42, "first_name": "Test"}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    wrong_secret = hmac.new(bot_token.encode(), b"WebAppData", hashlib.sha256).digest()
    fields["hash"] = hmac.new(wrong_secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException, match="Telegram verification failed"):
        verify_init_data(urlencode(fields), bot_token=bot_token)


def test_legacy_webapp_pages_redirect_to_canonical_mini_app():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.webapp import register_webapp

    app = FastAPI()
    register_webapp(app)
    client = TestClient(app)
    for path in ("/webapp/", "/webapp/buy", "/webapp/record", "/webapp/app/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/app/"


def test_bot_webapp_buttons_use_native_telegram_serialization_and_canonical_url(monkeypatch):
    source = (ROOT / "server/plugins/bot/start.py").read_text()
    assert "InlineKeyboardButton as TelegramInlineKeyboardButton" in source
    assert "TelegramInlineKeyboardButton(" in source
    assert "web_app=WebAppInfo(url=config.WEBAPP_URL)" in source
    assert 'getattr(_cfg, "WEBAPP_URL", "")' in source
    assert 'f"{_webapp_url}/webapp/"' not in source

    from server.plugins.bot import start
    monkeypatch.setattr(start.config, "WEBAPP_URL", "https://example.test/app/")
    button = start._home_buttons({}).inline_keyboard[0][0]
    assert type(button).__name__ == "InlineKeyboardButton"
    assert button.web_app.url == "https://example.test/app/"


def test_webapp_api_key_login_reuses_canonical_user_and_precedes_telegram(monkeypatch):
    from server.webapp import auth

    calls = []

    async def fake_require_api_key(key):
        calls.append(key)
        return {"user_id": 77, "full_name": "Real Owner", "username": "owner", "language": "hi"}

    monkeypatch.setattr(auth, "require_api_key", fake_require_api_key)
    identity = asyncio_run(auth.require_webapp_user(
        x_api_key=" tg_77_real_secret ",
        x_telegram_init_data="must-not-be-evaluated",
        x_request_id="api-key-login-test",
    ))
    assert calls == ["tg_77_real_secret"]
    assert {key: identity[key] for key in ("id", "first_name", "username", "language_code", "auth_source")} == {
        "id": 77,
        "first_name": "Real",
        "username": "owner",
        "language_code": "hi",
        "auth_source": "api_key",
    }
    assert identity["_webapp_account_doc"]["user_id"] == 77


def test_miniapp_api_key_login_is_header_only_and_all_user_routes_accept_canonical_resolver():
    routes_source = (ROOT / "server/webapp/routes.py").read_text()
    client_source = (ROOT / "miniapp/client/src/lib/api.ts").read_text()
    assert "Depends(require_tg_user)" not in routes_source
    assert routes_source.count("Depends(require_webapp_user)") >= 19
    assert 'headers.set("X-Api-Key", apiKey)' in client_source
    assert "sessionStorage" in client_source
    assert "localStorage" not in client_source
    assert "clearStoredApiKey();" in client_source


def test_miniapp_read_requests_do_not_force_json_content_type():
    client_source = (ROOT / "miniapp/client/src/lib/api.ts").read_text()
    assert "const headers = new Headers(init?.headers);" in client_source
    assert 'if (typeof init?.body === "string" && !headers.has("Content-Type"))' in client_source
    assert 'headers.set("Content-Type", "application/json");' in client_source
    assert '"Content-Type": "application/json"' not in client_source


def test_miniapp_community_workspace_shows_partial_real_data_when_one_read_fails():
    source = (ROOT / "miniapp/client/src/components/CommunityWorkspace.tsx").read_text()
    assert "Promise.allSettled([tgfoxApi.referral(), tgfoxApi.support()])" in source
    assert 'if (referralResult.status === "fulfilled") setReferral(referralResult.value);' in source
    assert 'if (supportResult.status === "fulfilled") setSupport(supportResult.value);' in source
    assert "Available data is still shown." in source


def test_miniapp_otp_polling_stops_on_terminal_states_and_avoids_overlap():
    source = (ROOT / "miniapp/client/src/components/ActivityWorkspace.tsx").read_text()
    assert "let polling = false;" in source
    assert "if (!active || polling) return;" in source
    assert 'if (["ready", "timeout", "expired"].includes(result.otp_status)) stop();' in source
    assert '404: "unknown", 408: "timeout", 410: "expired"' in source
    assert "return () => { active = false; stop(); };" in source


def test_miniapp_wallet_keeps_verified_summary_when_history_read_fails():
    source = (ROOT / "miniapp/client/src/components/WalletWorkspace.tsx").read_text()
    assert "Promise.allSettled([" in source
    assert "const load = async (canUpdate: () => boolean = () => true)" in source
    assert "if (!canUpdate()) return;" in source
    assert 'if (summaryResult.status === "rejected") {' in source
    assert "setSummaryError(\"Verified wallet data is temporarily unavailable." in source
    assert "return;" in source
    assert 'if (depositResult.status === "fulfilled") setDeposits(depositResult.value.items);' in source
    assert 'if (withdrawalResult.status === "fulfilled") setWithdrawals(withdrawalResult.value.items);' in source
    assert "Verified balance data is still shown." in source
    assert "load(() => mounted.current)" in source
    assert "return () => { mounted.current = false; };" in source


def test_miniapp_activity_ledger_load_is_unmount_safe():
    source = (ROOT / "miniapp/client/src/components/ActivityWorkspace.tsx").read_text()
    assert "const load = async (canUpdate: () => boolean = () => true)" in source
    assert "if (canUpdate()) setLedger(data.items);" in source
    assert "load(() => active)" in source
    assert "return () => { active = false; };" in source


def test_miniapp_rapid_submit_guards_cover_purchase_and_wallet_actions():
    home_source = (ROOT / "miniapp/client/src/pages/Home.tsx").read_text()
    deposit_source = (ROOT / "miniapp/client/src/components/DepositWorkspace.tsx").read_text()
    buy_source = (ROOT / "miniapp/client/src/components/BuyAccountWorkspace.tsx").read_text()
    withdrawal_source = (ROOT / "miniapp/client/src/components/WithdrawalWorkspace.tsx").read_text()
    assert "BuyAccountWorkspace" in home_source
    assert "const inFlight = useRef(false);" in buy_source
    assert "if (!selected || inFlight.current) return;" in buy_source
    assert "inFlight.current = true;" in buy_source
    assert "inFlight.current = false;" in buy_source
    assert "const createInFlight = useRef(false);" in deposit_source
    assert "const verifyInFlight = useRef(false);" in deposit_source
    assert "const actionInFlight = useRef(false);" in withdrawal_source
    assert deposit_source.count("if (createInFlight.current) return;") == 1
    assert deposit_source.count("verifyInFlight.current") >= 4
    assert withdrawal_source.count("if (actionInFlight.current) return;") == 2
    assert deposit_source.count("createInFlight.current = true;") == 1
    assert deposit_source.count("verifyInFlight.current = true;") == 2
    assert withdrawal_source.count("actionInFlight.current = true;") == 2
    assert deposit_source.count("createInFlight.current = false;") == 1
    assert deposit_source.count("verifyInFlight.current = false;") == 2
    assert withdrawal_source.count("actionInFlight.current = false;") == 2


def test_lazy_workspaces_have_local_recovery_and_global_error_hides_stack():
    home_source = (ROOT / "miniapp/client/src/pages/Home.tsx").read_text()
    boundary_source = (ROOT / "miniapp/client/src/components/ErrorBoundary.tsx").read_text()
    assert home_source.count("<ErrorBoundary resetKey={view}") == 8
    assert "<WorkspaceRecovery onBack={() => navigate(\"dashboard\")} />" in home_source
    assert "<WorkspaceRecovery onBack={() => navigate(\"wallet\")} />" in home_source
    assert "Return to dashboard" in home_source
    assert "fallback?: ReactNode;" in boundary_source
    assert "resetKey?: unknown;" in boundary_source
    assert "componentDidCatch(error: Error)" in boundary_source
    assert "this.state.error?.stack" not in boundary_source


def test_miniapp_has_separate_live_deposit_and_withdrawal_workspaces():
    home_source = (ROOT / "miniapp/client/src/pages/Home.tsx").read_text()
    deposit_source = (ROOT / "miniapp/client/src/components/DepositWorkspace.tsx").read_text()
    withdrawal_source = (ROOT / "miniapp/client/src/components/WithdrawalWorkspace.tsx").read_text()
    wallet_source = (ROOT / "miniapp/client/src/components/WalletWorkspace.tsx").read_text()
    assert 'type View = "dashboard" | "buy" | "sell" | "wallet" | "deposit" | "withdraw"' in home_source
    assert "const DepositWorkspace = lazy(" in home_source
    assert "const WithdrawalWorkspace = lazy(" in home_source
    assert '<WalletWorkspace onOpenDeposit={() => navigate("deposit")} onOpenWithdrawal={() => navigate("withdraw")} />' in home_source
    assert "tgfoxApi.createDeposit(" in deposit_source
    assert "tgfoxApi.deposits()" in deposit_source
    assert 'type Screen = "methods" | "method";' in deposit_source
    assert "Choose a payment method" in deposit_source
    assert "No network selector here." in deposit_source
    assert "OxaPay shows its available networks only inside the secure hosted checkout." in deposit_source
    assert "tgfoxApi.submitBinanceOrderId(" in deposit_source
    assert "Submit your {network} transaction hash" in deposit_source
    assert "tgfoxApi.saveWithdrawalAddress(" in withdrawal_source
    assert "tgfoxApi.withdraw(" in withdrawal_source
    assert "Withdrawal amount exceeds your verified spendable balance." in withdrawal_source
    assert "Save this destination address before requesting withdrawal." in withdrawal_source
    assert "onOpenDeposit" in wallet_source
    assert "onOpenWithdrawal" in wallet_source


def test_miniapp_buy_sell_are_focused_real_data_pages_with_secure_bot_handoff():
    home_source = (ROOT / "miniapp/client/src/pages/Home.tsx").read_text()
    buy_source = (ROOT / "miniapp/client/src/components/BuyAccountWorkspace.tsx").read_text()
    dashboard_source = (ROOT / "miniapp/client/src/components/DashboardWorkspace.tsx").read_text()
    sell_source = (ROOT / "miniapp/client/src/components/SellAccountWorkspace.tsx").read_text()
    start_source = (ROOT / "server/plugins/bot/start.py").read_text()
    sell_bot_source = (ROOT / "server/plugins/bot/sell_account.py").read_text()

    assert "const BuyAccountWorkspace = lazy(" in home_source
    assert "const SellAccountWorkspace = lazy(" in home_source
    assert 'view === "buy" && <ErrorBoundary' in home_source
    assert 'view === "sell" && <ErrorBoundary' in home_source
    assert 'onNavigate("sell")' in dashboard_source
    assert 'type Screen = "countries" | "review" | "delivery";' in buy_source
    assert "tgfoxApi.buy(selected.code)" in buy_source
    assert "tgfoxApi.orderOtp(order.order_id)" in buy_source
    assert "The server rechecks country availability and final price" in buy_source
    assert "tgfoxApi.sellRequests()" in sell_source
    assert "https://t.me/${botUsername}?start=sell" in sell_source
    assert "This Mini App never asks you to paste a Telegram session." in sell_source
    assert 'if ref_code == "sell":' in start_source
    assert "begin_sell_account_flow(user.id)" in start_source
    assert "async def begin_sell_account_flow" in sell_bot_source


def test_dashboard_is_a_lazy_real_data_command_center_without_synthetic_metrics():
    home_source = (ROOT / "miniapp/client/src/pages/Home.tsx").read_text()
    dashboard_source = (ROOT / "miniapp/client/src/components/DashboardWorkspace.tsx").read_text()

    assert "const DashboardWorkspace = lazy(" in home_source
    assert 'view === "dashboard" && <ErrorBoundary' in home_source
    assert "<DashboardWorkspace profile={profile} countries={countries} records={records}" in home_source
    assert "SpendingPanel" not in home_source
    assert "[22, 38, 31, 64, 42, 82, 58]" not in home_source
    assert "Math.max(profile.buy_spent, 0.01)" not in home_source
    assert "profile.balance" in dashboard_source
    assert "profile.counts.purchases" in dashboard_source
    assert "profile.counts.sales" in dashboard_source
    assert "profile.buy_spent" in dashboard_source
    assert "profile.sell_earned" in dashboard_source
    assert "availableCountries.reduce" in dashboard_source
    assert "order.status" in dashboard_source
    assert 'onNavigate("buy")' in dashboard_source
    assert 'onNavigate("sell")' in dashboard_source
    assert 'onNavigate("wallet")' in dashboard_source
    assert 'onNavigate("transactions")' in dashboard_source


def test_wallet_summary_has_explicit_white_loading_unavailable_and_verified_states():
    source = (ROOT / "miniapp/client/src/components/WalletWorkspace.tsx").read_text()

    assert "const [summaryError, setSummaryError] = useState<string | null>(null);" in source
    assert "setSummaryError(\"Verified wallet data is temporarily unavailable." in source
    assert "if (summaryResult.status === \"rejected\")" in source
    assert "if (busy || !wallet) return <WalletLoading />;" not in source
    assert "WalletSummaryLoading" in source
    assert "WalletSummaryUnavailable" in source
    assert "WalletSummary wallet={wallet} refreshing={busy}" in source
    assert "!busy && !wallet" in source
    assert "Wallet summary is temporarily unavailable" in source
    assert "No balance is estimated or shown while this data is unavailable." in source
    assert "bg-white" in source


def test_admin_panel_is_a_grouped_real_data_control_center_with_existing_guards():
    base = (ROOT / "server/admin/templates/admin/base.html").read_text()
    dashboard = (ROOT / "server/admin/templates/admin/dashboard.html").read_text()
    css = (ROOT / "server/admin/static/admin/css/admin.css").read_text()
    deps = (ROOT / "server/admin/deps.py").read_text()

    assert '<html lang="en" data-theme="light">' in base
    assert "Command center" in base
    assert "Inventory & orders" in base
    assert "Payments & sellers" in base
    assert "System" in base
    for route in ["/admin/sessions", "/admin/orders", "/admin/payments", "/admin/sell-requests", "/admin/backup", "/admin/settings"]:
        assert f'href="{route}"' in base
    assert "admin-hero" in dashboard
    assert "/admin/api/stats" in dashboard
    assert "/admin/api/charts/orders" in dashboard
    assert "/admin/api/deposits?status=pending" in dashboard
    assert "/admin/api/sell-requests?status=pending" in dashboard
    assert "loadAll()" in dashboard
    assert "data-theme=\"light\"" in css
    assert ".admin-quick-grid" in css
    assert ".admin-attention-grid" in css
    assert "require_admin(request)" in deps
    assert "X-CSRF-Token" in deps


def test_every_admin_page_template_has_a_registered_page_route_and_every_router_is_mounted():
    import re

    template_dir = ROOT / "server/admin/templates/admin"
    rendered_templates = set()
    route_source = ""
    for source_path in (ROOT / "server/admin/routes").glob("*.py"):
        source = source_path.read_text()
        route_source += source
        rendered_templates.update(re.findall(r'"admin/([a-z_]+\.html)"', source))

    all_templates = {path.name for path in template_dir.glob("*.html")}
    assert all_templates - {"base.html"} == rendered_templates

    registration_source = (ROOT / "server/admin/routes/__init__.py").read_text()
    registered_modules = set(re.findall(r"from \.([a-z_]+) import router as", registration_source))
    route_modules = {path.stem for path in (ROOT / "server/admin/routes").glob("*.py")} - {"__init__"}
    assert route_modules == registered_modules
    for path in [
        "/admin", "/admin/analytics", "/admin/sessions", "/admin/bin",
        "/admin/countries", "/admin/proxies", "/admin/orders", "/admin/sell-requests",
        "/admin/payments", "/admin/sellers", "/admin/user-sell-stock", "/admin/users",
        "/admin/logs", "/admin/tasks", "/admin/backup", "/admin/sales-feed", "/admin/settings",
    ]:
        assert f'"{path}"' in route_source


def test_admin_client_waits_for_csrf_uses_light_default_and_avoids_untrusted_html_in_toasts():
    source = (ROOT / "server/admin/static/admin/js/admin.js").read_text()

    assert "localStorage.getItem(THEME_KEY) || 'light'" in source
    assert "if (method !== 'GET' && !_csrfToken) await loadCsrf();" in source
    assert "Secure admin session is not ready. Please retry." in source
    assert "let _csrfLoading = null;" in source
    assert "text.textContent = String(msg);" in source
    assert "function escapeHtml(value)" in source
    assert "return res.ok ? data : { ...data, ok: false, status: res.status };" in source


def test_admin_withdrawal_approval_requires_confirmation_and_blocks_duplicate_clicks():
    source = (ROOT / "server/admin/templates/admin/payments.html").read_text()

    assert "let _curTab = 'deposits', _dbt, _withdrawalApprovalInFlight = false;" in source
    assert "if (_withdrawalApprovalInFlight) return;" in source
    assert "Approve withdrawal ${id}? This confirms the payout record" in source
    assert "_withdrawalApprovalInFlight = true;" in source
    assert "button.disabled = true; button.textContent = 'Approving…';" in source
    assert "_withdrawalApprovalInFlight = false;" in source
    assert "button.disabled = false; button.textContent = '✓ Approve';" in source


def test_admin_payment_records_encode_nested_manual_chain_datetimes_and_live_sse_keeps_subscribers_registered():
    from datetime import datetime, timezone
    from decimal import Decimal
    from server.admin.routes.payments import _ser

    encoded = _ser({
        "_id": "private-object-id",
        "amount": Decimal("12.50"),
        "extra": {"manual_chain_verification": {"checked_at": datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)}},
    })
    assert "_id" not in encoded
    assert encoded["amount"] == 12.5
    assert encoded["extra"]["manual_chain_verification"]["checked_at"].endswith("+00:00")

    dashboard = (ROOT / "server/admin/routes/dashboard.py").read_text()
    assert "async def event_generator():\n        _LIVE_SUBS.add(queue)" in dashboard
    assert "finally:\n            _LIVE_SUBS.discard(queue)" in dashboard


def test_admin_order_list_serializes_delivery_and_termination_datetimes():
    from datetime import datetime, timezone
    from server.admin.routes.orders import _ser

    encoded = _ser({
        "created_at": datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc),
        "delivered_at": datetime(2026, 9, 4, 8, 1, tzinfo=timezone.utc),
        "platform_session_terminated_at": datetime(2026, 9, 4, 8, 2, tzinfo=timezone.utc),
        "delivery": {"events": [datetime(2026, 9, 4, 8, 3, tzinfo=timezone.utc)]},
    })
    assert encoded["created_at"].endswith("+00:00")
    assert encoded["delivered_at"].endswith("+00:00")
    assert encoded["platform_session_terminated_at"].endswith("+00:00")
    assert encoded["delivery"]["events"][0].endswith("+00:00")


def test_admin_sse_disconnect_is_not_logged_as_an_unhandled_no_response_error():
    source = (ROOT / "server/core/api.py").read_text()
    assert 'if str(exc) == "No response returned.":' in source
    assert "return Response(status_code=204" in source


def test_admin_payment_lists_show_retryable_error_without_nan_counts():
    source = (ROOT / "server/admin/templates/admin/payments.html").read_text()

    assert "if (data.ok === false) throw new Error(data.detail || data.message || 'Could not load deposits');" in source
    assert "if (data.ok === false) throw new Error(data.detail || data.message || 'Could not load withdrawals');" in source
    assert "if (data.ok === false) throw new Error(data.detail || data.message || 'Could not load transactions');" in source
    assert "Could not load deposits." in source
    assert "Could not load withdrawals." in source
    assert "Could not load transactions." in source


def test_api_key_miniapp_requests_reuse_authenticated_account_document():
    from server.webapp.routes import _account_doc_from_auth

    account = {"user_id": 42, "webapp_prefs": {"theme": "midnight_blue"}}
    assert _account_doc_from_auth({"_webapp_account_doc": account}, 42) is account
    assert _account_doc_from_auth({"_webapp_account_doc": account}, 99) == {}
    auth_source = (ROOT / "server/webapp/auth.py").read_text()
    routes_source = (ROOT / "server/webapp/routes.py").read_text()
    assert '"_webapp_account_doc": user' in auth_source
    assert "doc = _account_doc_from_auth(user, uid) or await _get_user_doc(uid)" in routes_source
    assert routes_source.count("_account_doc_from_auth(user, uid) or await _get_user_doc(uid)") == 4
    assert "async def _get_prefs" not in routes_source


def test_fresh_wallet_snapshot_coalesces_balance_reserved_and_addresses(monkeypatch):
    from server.utils.database import userdb

    captured = {}

    class FakeUsers:
        async def find_one(self, query, projection):
            captured["query"] = query
            captured["projection"] = projection
            return {
                "balance": 12.34567,
                "reserved_balance": 2.12555,
                "wallet_addresses": {"trc20": "T-ADDRESS", "bep20": "B-ADDRESS"},
            }

    monkeypatch.setattr(userdb, "usersdb", FakeUsers())
    snapshot = asyncio_run(userdb.get_wallet_snapshot(42))
    assert captured == {
        "query": {"user_id": 42},
        "projection": {"balance": 1, "reserved_balance": 1, "wallet_addresses": 1},
    }
    assert snapshot == {
        "balance": 12.3457,
        "reserved_balance": 2.1256,
        "addresses": {"trc20": "T-ADDRESS", "bep20": "B-ADDRESS"},
    }
    webapp_source = (ROOT / "server/webapp/routes.py").read_text()
    wallet_api_source = (ROOT / "server/api/routes/wallet.py").read_text()
    user_api_source = (ROOT / "server/api/routes/user.py").read_text()
    assert "snapshot = await get_wallet_snapshot(uid)" in webapp_source
    assert wallet_api_source.count("snapshot = await get_wallet_snapshot(user[\"user_id\"])" ) == 2
    assert "snapshot = await get_wallet_snapshot(user[\"user_id\"])" in user_api_source
    assert "stats, snapshot = await asyncio.gather(" in user_api_source


def test_public_order_history_summary_counts_are_parallelized():
    source = (ROOT / "server/api/routes/user.py").read_text()
    assert "total_all, total_completed, total_pending = await asyncio.gather(" in source
    assert 'ordersdb.count_documents({"buyer_id": user["user_id"]}),' in source
    assert 'ordersdb.count_documents({"buyer_id": user["user_id"], "status": "completed"}),' in source
    assert 'ordersdb.count_documents({"buyer_id": user["user_id"], "status": "pending"}),' in source


def test_public_order_history_page_and_total_are_parallelized():
    source = (ROOT / "server/api/routes/user.py").read_text()
    assert "async def _fetch_page() -> list[dict]:" in source
    assert "total, orders = await asyncio.gather(" in source
    assert "ordersdb.count_documents(query)," in source
    assert "_fetch_page()," in source
    assert "_serialize_dt(order, \"created_at\", \"completed_at\", \"cancelled_at\", \"delivered_at\", \"updated_at\")" in source


def test_miniapp_api_key_login_reuses_verified_profile_for_initial_refresh():
    source = (ROOT / "miniapp/client/src/pages/Home.tsx").read_text()
    assert "const refresh = async (verifiedProfile?: Profile)" in source
    assert "const me = verifiedProfile ?? await tgfoxApi.profile();" in source
    assert "const me = await tgfoxApi.login(apiKey); await refresh(me);" in source


def test_sales_feed_fake_activity_stays_isolated_from_real_accounting():
    service_source = (ROOT / "server/services/sales_feed/service.py").read_text()
    formatter_source = (ROOT / "server/services/sales_feed/formatter.py").read_text()
    stock_source = (ROOT / "server/utils/stock_display.py").read_text()
    bot_admin_source = (ROOT / "server/plugins/bot/sales_feed_admin.py").read_text()
    fake_source = (ROOT / "server/services/sales_feed/fake_generator.py").read_text()
    assert "_fake_worker" in service_source
    assert "send_fake_preview" in service_source
    assert "format_fake_purchase" in formatter_source
    assert "never creates an order" in fake_source
    assert "record_successful_purchase" not in fake_source
    assert "return max(0, real_stock)" in stock_source
    assert "fake_stock" in stock_source
    assert "send_fake_preview" in bot_admin_source


def test_admin_country_ui_keeps_fake_stock_controls_isolated_from_real_stock():
    country_template = (ROOT / "server/admin/templates/admin/countries.html").read_text()
    settings_template = (ROOT / "server/admin/templates/admin/settings.html").read_text()
    assert "Real / Fake Stock" in country_template
    assert "Fake Stock" in country_template
    assert "stock_mode" in country_template
    assert "hide_fake_stock_when_real_zero" in settings_template


def test_hardened_cors_custom_env_and_api_key_session_contracts():
    api_source = (ROOT / "server/core/api.py").read_text()
    bootstrap_source = (ROOT / "server/__main__.py").read_text()
    client_source = (ROOT / "miniapp/client/src/lib/api.ts").read_text()
    assert 'or ["*"]' not in api_source
    assert "CORS cross-origin access is disabled" in api_source
    assert "_CUSTOM_ENV_ALLOWLIST" in bootstrap_source
    assert "if k in _CUSTOM_ENV_ALLOWLIST" in bootstrap_source
    assert "BOT_TOKEN" not in bootstrap_source.split("_CUSTOM_ENV_ALLOWLIST", 1)[1].split("}", 1)[0]
    assert "API_KEY_IDLE_TIMEOUT_MS = 15 * 60 * 1000" in client_source
    assert "API_KEY_ACTIVITY_STORAGE" in client_source
    assert "Date.now() - lastActivity > API_KEY_IDLE_TIMEOUT_MS" in client_source


def test_json_api_request_size_guard_is_scoped_to_json_api_routes():
    api_source = (ROOT / "server/core/api.py").read_text()
    assert "async def json_request_size_guard" in api_source
    assert 'startswith(("/api/", "/webapp/api/"))' in api_source
    assert "256 * 1024" in api_source
    assert "JSON request body is too large." in api_source


def test_api_enables_conservative_response_compression():
    api_source = (ROOT / "server/core/api.py").read_text()
    assert "from fastapi.middleware.gzip import GZipMiddleware" in api_source
    assert "self.app.add_middleware(GZipMiddleware, minimum_size=1024)" in api_source


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.23456", 1.2346), ("0.20001", 0.2), (1, 1.0)],
)
def test_money_parser_rounds_external_amounts_with_decimal(raw, expected):
    from server.utils.money import parse_money

    assert parse_money(raw, field="deposit amount", minimum=0.2) == expected


@pytest.mark.parametrize("raw", [None, True, "NaN", "Infinity", "-1", "0.1999", "1000001"])
def test_money_parser_rejects_invalid_or_out_of_range_external_amounts(raw):
    from fastapi import HTTPException
    from server.utils.money import parse_money

    with pytest.raises(HTTPException):
        parse_money(raw, field="deposit amount", minimum=0.2)


def test_discount_context_reuses_one_rank_and_settings_snapshot(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from server.utils import pricing_discounts

    calls = []

    async def fake_rank(user_id):
        calls.append(("rank", user_id))
        return "VIP2"

    async def fake_rank_discount(rank):
        calls.append(("rank_discount", rank))
        return 5.0

    async def fake_setting(key):
        calls.append(("setting", key))
        return {
            "IN": {"percent": 10, "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            "US": {"percent": 25, "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(pricing_discounts, "get_user_rank", fake_rank)
    monkeypatch.setattr(pricing_discounts, "get_rank_discount", fake_rank_discount)
    monkeypatch.setattr(pricing_discounts, "get_setting", fake_setting)

    context = asyncio_run(pricing_discounts.get_discount_context(42))
    assert calls == [("rank", 42), ("rank_discount", "VIP2"), ("setting", "country_discounts")]
    assert pricing_discounts.apply_discount_context(100, "IN", *context) == 90.0
    assert pricing_discounts.apply_discount_context(100, "US", *context) == 95.0
    assert pricing_discounts.apply_discount_context(100, "BR", *context) == 95.0


def test_miniapp_catalog_uses_one_discount_context_per_response():
    source = (ROOT / "server/webapp/routes.py").read_text()
    catalog_source = source.split('@router.get("/api/countries")', 1)[1].split('@router.post("/api/buy")', 1)[0]
    assert "discount_context = await get_discount_context(uid)" in catalog_source
    assert "apply_discount_context(base, code, *discount_context)" in catalog_source
    assert "await apply_discounts" not in catalog_source


def test_miniapp_profile_reuses_loaded_user_document_and_parallelizes_counts():
    from server.webapp.routes import _prefs_from_doc

    source = (ROOT / "server/webapp/routes.py").read_text()
    profile_source = source.split('@router.get("/api/me")', 1)[1].split('@router.get("/api/record")', 1)[0]
    assert "prefs = _prefs_from_doc(doc)" in profile_source
    assert "prefs = await _get_prefs(uid)" not in profile_source
    assert "asyncio.gather(_purchase_count(), _sales_count())" in source
    assert _prefs_from_doc({"webapp_prefs": {"theme": "midnight_blue", "owned_themes": ["greenfield"]}}) == {
        "theme": "midnight_blue",
        "owned_themes": ["greenfield", "dark_gold", "midnight_blue", "neon_purple"],
    }


def test_miniapp_record_parallelizes_independent_purchase_and_sales_history_reads():
    source = (ROOT / "server/webapp/routes.py").read_text()
    record_source = source.split('@router.get("/api/record")', 1)[1].split('# ── Buy Account:', 1)[0]
    assert "async def _purchases()" in record_source
    assert "async def _sales()" in record_source
    assert "asyncio.gather(_purchases(), _sales())" in record_source
    assert '"purchases": purchases' in record_source
    assert '"sales": sales' in record_source


def test_miniapp_seller_status_excludes_retry_only_session_payload(monkeypatch):
    from server.utils.database import sellrequestdb

    captured = {}

    class Cursor:
        def __init__(self, include_session_payload):
            self.include_session_payload = include_session_payload

        def sort(self, *args):
            return self

        def limit(self, *args):
            return self

        async def _items(self):
            item = {"_id": "internal", "request_id": "SELL-TEST"}
            if self.include_session_payload:
                item["session_bytes_enc"] = b"retry-only"
            yield item

        def __aiter__(self):
            return self._items()

    class FakeRequests:
        def find(self, query, projection=None):
            captured["query"] = query
            captured["projection"] = projection
            return Cursor(include_session_payload=projection is None)

    monkeypatch.setattr(sellrequestdb, "sellrequestsdb", FakeRequests())
    docs = asyncio_run(sellrequestdb.get_user_sell_requests(42, include_session_payload=False))
    assert captured == {
        "query": {"user_id": 42},
        "projection": {"_id": 0, "session_bytes_enc": 0},
    }
    assert docs == [{"request_id": "SELL-TEST"}]

    route_source = (ROOT / "server/webapp/routes.py").read_text()
    seller_source = route_source.split('@router.get("/api/sell-requests")', 1)[1].split('# ── Themes', 1)[0]
    assert "include_session_payload=False" in seller_source


def test_delivery_zip_pairs_session_and_metadata():
    from server.services.buy_session_service import build_delivery_zip

    archive = build_delivery_zip(
        [
            {
                "phone_digits": "919999999999",
                "session_bytes": b"session-bytes",
                "json_data": {"session_file": "919999999999", "phone": "+919999999999"},
            }
        ],
        "delivery.zip",
    )
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == {"919999999999.session", "919999999999.json"}
        assert zf.read("919999999999.session") == b"session-bytes"


def test_delivery_zip_rejects_duplicate_phone():
    from server.services.buy_session_service import build_delivery_zip

    item = {
        "phone_digits": "919999999999",
        "session_bytes": b"session-bytes",
        "json_data": {"session_file": "919999999999"},
    }
    with pytest.raises(ValueError, match="Duplicate"):
        build_delivery_zip([item, item], "delivery.zip")


def test_storage_extracts_session_from_zip_and_rejects_unsafe_session_path():
    from server.utils.sessions.channel_storage import StorageError, extract_session_bytes

    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("account.session", b"sqlite-session")
    assert extract_session_bytes(good.getvalue()) == b"sqlite-session"

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as zf:
        zf.writestr("../account.session", b"bad")
    with pytest.raises(StorageError) as exc_info:
        extract_session_bytes(unsafe.getvalue())
    assert exc_info.value.code == "INVALID_ARCHIVE"


def test_transaction_whitelist_contains_all_current_reversal_types():
    from server.utils.database.walletdb import VALID_TXN_TYPES

    assert {"reversal", "sale_reversal", "referral"}.issubset(VALID_TXN_TYPES)


def test_withdrawal_retryable_submit_keeps_reserved_balance(monkeypatch):
    from server.services.withdrawal import service as service_module
    from server.services.withdrawal.models import PayoutResult
    from server.services.withdrawal.service import WithdrawalService

    calls = {"release": 0, "status": [], "events": []}

    class Provider:
        provider_id = "fake"

        async def submit_payout(self, **kwargs):
            return PayoutResult(
                success=False,
                internal_status="pending",
                message="temporary network outage",
            )

    async def fake_store(*args, **kwargs):
        return None

    async def fake_update(withdrawal_id, new_status, **kwargs):
        calls["status"].append(new_status)
        return True

    async def fake_event(*args, **kwargs):
        calls["events"].append(args[1] if len(args) > 1 else kwargs.get("event"))

    async def fake_get(*args, **kwargs):
        return {"withdrawal_id": "WIT-TEST", "user_id": 1, "amount": 10}

    async def fake_release(*args, **kwargs):
        calls["release"] += 1

    monkeypatch.setattr(service_module, "store_gateway_result", fake_store)
    monkeypatch.setattr(service_module, "update_withdrawal_status", fake_update)
    monkeypatch.setattr(service_module, "log_withdrawal_event", fake_event)
    monkeypatch.setattr(service_module, "get_withdrawal_record", fake_get)
    monkeypatch.setattr(service_module, "release_withdrawal", fake_release)

    svc = WithdrawalService(registry=None)
    notified = []

    async def fake_notify(*args, **kwargs):
        notified.append(True)

    svc._notify = fake_notify
    result = asyncio_run(svc._submit_to_gateway(
        wit_id="WIT-TEST",
        provider=Provider(),
        user_id=1,
        amount=9.9,
        currency="USDT",
        network="TRC20",
        address="TTEST",
        memo="",
    ))
    assert result.internal_status == "pending"
    assert calls["release"] == 0
    assert calls["status"] == ["pending"]
    assert notified


def test_admin_sales_feed_has_real_status_summary_safe_loading_and_guarded_mutations():
    source = (ROOT / "server/admin/templates/admin/sales_feed.html").read_text()
    route_source = (ROOT / "server/admin/routes/sales_feed.py").read_text()

    assert 'id="feed-summary-state"' in source
    assert 'id="feed-summary-destination"' in source
    assert 'id="feed-summary-delay"' in source
    assert 'id="feed-load-note"' in source
    assert "apiGET('/admin/api/sales-feed/settings')" in source
    assert "apiPOST('/admin/api/sales-feed/settings',payload)" in source
    assert "apiPOST('/admin/api/sales-feed/test',{})" in source
    assert "window.confirm('Send a real test notification" in source
    assert "Sales Feed settings are temporarily unavailable" in source
    assert 'id="feed-activity-body"' in source
    assert "apiGET('/admin/api/sales-feed/recent')" in source
    assert "Only real completed orders appear here." in source
    assert '@router.get("/admin/api/sales-feed/recent")' in route_source
    assert '"status": {"$in": ["completed", "delivered", "success", "paid"]}' in route_source
    assert "jsonable_encoder" in route_source


def test_bin_compatibility_normalises_legacy_records_without_moving_them():
    from server.utils.database.sessiondb import (
        _legacy_bin_marker_query,
        _normalise_bin_doc,
    )

    item = _normalise_bin_doc({
        "session_id": "legacy-42",
        "country": "in",
        "reason": "AUTH_KEY_INVALID",
        "detected_at": "2026-09-01T12:00:00+00:00",
    }, "bin_sessions")
    assert item["account_id"] == "legacy-42"
    assert item["country_code"] == "IN"
    assert item["bin_issue"] == "AUTH_KEY_INVALID"
    assert item["_bin_source"] == "bin_sessions"
    assert "$or" in _legacy_bin_marker_query()

    source = (ROOT / "server/utils/database/sessiondb.py").read_text()
    route_source = (ROOT / "server/admin/routes/bin.py").read_text()
    assert "list_collection_names" in source
    assert "inventory_state" in source
    assert "get_bin_session" in route_source
    assert "_SENSITIVE_BIN_FIELDS" in route_source


def asyncio_run(awaitable):
    import asyncio
    return asyncio.run(awaitable)


class _Result:
    modified_count = 1
    upserted_id = None


def test_record_deposit_uses_atomic_deposit_id_guard(monkeypatch):
    from server.utils.database import userdb

    captured = {}

    class FakeUsers:
        async def update_one(self, query, update, upsert=False):
            captured.update(query=query, update=update, upsert=upsert)
            return _Result()

    monkeypatch.setattr(userdb, "usersdb", FakeUsers())
    monkeypatch.setattr(userdb.memstore, "invalidate_user", lambda _uid: None)
    credited = asyncio_run(userdb.record_deposit(42, 5.0, deposit_id="DEP-TEST"))
    assert credited is True
    assert captured["query"]["credited_deposit_ids"] == {"$ne": "DEP-TEST"}
    assert captured["update"]["$addToSet"] == {"credited_deposit_ids": "DEP-TEST"}
