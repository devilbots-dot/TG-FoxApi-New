"""
Placeholder module — withdrawal balance helpers were added directly to userdb.py.

The three functions that power the withdrawal balance lifecycle are in userdb.py:
    reserve_balance_atomic(user_id, amount)   → balance → reserved_balance
    finalize_reserved_balance(user_id, amount) → removes from reserved_balance (permanent deduct)
    release_reserved_balance(user_id, amount) → reserved_balance → balance (refund)

Import them from server.utils.database.userdb, not from here.
"""
