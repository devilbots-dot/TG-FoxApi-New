"""
accounts collection — legacy binding only.

This used to back a peer-to-peer account marketplace (list_account,
get_available_accounts, etc.) via an API router at /api/v1/accounts.
That router was never registered in server/core/api.py, so the whole
P2P listing flow was dead code and has been removed.

The live buy/sell model is the country-stock pool: session_admin.py +
sessiondb.py (bot-uploaded sessions) and countrydb.py (stock counts),
surfaced through server/plugins/bot/market.py and server/api/routes/orders.py.

The `accountsdb` collection binding is kept only because the admin
dashboard (server/api/routes/admin.py) still reads/writes it directly
for the "Accounts" panel (leftover marketplace data, if any).
"""

from server.core.mongo import collection

accountsdb = collection("accounts")
