FINAL RESERVE BALANCE + ADMIN ADJUSTMENT UPDATE

Replace these files at the SAME paths in your GitHub project:

server/plugins/bot/start.py
server/utils/database/userdb.py
server/utils/database/__init__.py

Admin command added:
/adjustreserve <user_id> <amount>

Examples:
/adjustreserve 123456789 5
/adjustreserve 123456789 -2

Alias also supported:
/adjust_reserve <user_id> <amount>

Security:
- Only the existing bot admin check (OWNER_ID + sudoers) can use this command.
- Positive adjustment increases BOTH balance and reserve_balance.
- Negative adjustment decreases BOTH, only if sufficient funds exist.
- Zero/invalid/infinite amounts are rejected.
- If an adjustment fails, no balance is changed.

The reserve balance remains the withdrawable earned balance. Purchases consume reserve first, then ordinary deposit balance. Withdrawals can use reserve only.

IMPORTANT: Keep a backup of the current GitHub files before replacing them.
