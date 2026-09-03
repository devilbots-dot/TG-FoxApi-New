# Same-Server Production Integration

- [x] Trace existing `/webapp` routes, signed Telegram initData verification, and bot user lookup to confirm one shared user record.
- [x] Remove all frontend mock/demo data and fail closed outside a verified Telegram Mini App session.
- [x] Make `python3 -m server` build the `miniapp/` frontend and serve its static output from the same TgFox domain.
- [x] Define the stable Mini App URL and ensure existing bot Web App buttons point to it without creating a second account.
- [x] Validate build order, static route delivery, API authentication headers, and production startup without changing database data.
