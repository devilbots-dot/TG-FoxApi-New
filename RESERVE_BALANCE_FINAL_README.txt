FINAL Reserve Balance update

Replace each included file at the exact same path in the original project.

Logic:
1. balance = total wallet money.
2. reserve_balance = earned money still available for withdrawal/spending.
3. Sale/referral earnings increase balance + reserve_balance.
4. Purchases reduce balance and consume reserve_balance first; once reserve is zero, purchases consume deposit money.
5. Withdrawals are allowed only from reserve_balance. During a pending withdrawal, the amount is moved from balance + reserve_balance into reserved_balance. Failed/rejected withdrawals restore balance + reserve_balance. Completed withdrawals permanently consume reserved_balance.
6. total_earn remains lifetime earnings/statistics and is not used as the current withdrawable amount.
7. Legacy users without reserve_balance get a best-effort migration from lifetime earnings + referral earnings - total spend - total withdrawals, capped by current balance.

Keep a backup before replacing files. Python compile check passed.
