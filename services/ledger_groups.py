"""Which ledgers are cash or bank ledgers (they take Contra vouchers).

A ledger is cash/bank when its group, or any group above it, is one of Tally's reserved
cash/bank groups, e.g. Bank Accounts > Current Accounts > "ICICI Current".
"""

CASH_BANK_GROUPS = {"cash-in-hand", "bank accounts", "bank od a/c", "bank occ a/c"}


def is_cash_bank_group(group, group_parents=None):
    """True if `group` is a cash/bank group or sits under one. group_parents: {group: parent}."""
    parents = {k.lower(): (v or "").lower() for k, v in (group_parents or {}).items()}
    seen = set()
    current = (group or "").strip().lower()
    while current and current not in seen:
        if current in CASH_BANK_GROUPS:
            return True
        seen.add(current)
        current = parents.get(current, "")
    return False
