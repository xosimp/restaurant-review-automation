"""change_log — a lasting, attributed history of settings, targets, prices,
menu, roster and hours changes (memory audit 9/29/26: change_log). SKELETON:
workstream M7 owns this module and replaces these bodies; the signature is
the contract every writer calls.
"""


def record(restaurant_id, entity, field, before, after, actor_user_id=None, source=None, db_path=None):
    """One change: entity ("restaurant" | "menu_item" | "price" | "roster" |
    "hours" | ...), field, before/after (JSON-able), who and through what
    (source: "owner" | "manager" | "admin" | "import" | "system"). Never
    raises into the caller's write."""
    return None
