"""
review_common.py — the sentences the weekly and monthly reviews share.

weekly_review.py and monthly_review.py each carried their own _fmt,
headline and cost_of_waiting; two were byte-identical and the third
differed only by the word "week"/"month". One copy here; each review
module keeps thin wrappers under its old names so callers and tests
(which patch monthly_review.headline) are unchanged.
"""


def fmt(value, unit):
    if value is None:
        return "—"
    if unit == "$":
        return f"${value:,.0f}"
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "★":
        return f"{value:.2f}★"
    return f"{value:g}"


def headline(review, period: str) -> str:
    """One sentence naming the week or month, from the metrics that moved."""
    moved = [m for m in review["metrics"] if m["verdict"] in ("improved", "worsened")]
    if not moved:
        measured = [m for m in review["metrics"] if m["value"] is not None]
        return (f"A steady {period} — nothing moved beyond normal variation."
                if measured else f"Not enough data synced last {period} to read it.")
    better = [m for m in moved if m["verdict"] == "improved"]
    worse = [m for m in moved if m["verdict"] == "worsened"]
    if better and not worse:
        return f"A better {period}: {better[0]['label'].lower()} improved."
    if worse and not better:
        return f"{worse[0]['label']} went the wrong way last {period}."
    return f"{better[0]['label']} improved; {worse[0]['label'].lower()} went the other way."


def cost_of_waiting(review) -> str:
    """What another period of this costs, in dollars, or "" when nothing
    measured moved far enough to put a number on.

    No email in this product answered "what happens if I ignore this" — the
    one sentence that separates an advisor from a dashboard. It is not a
    new measurement: monthly_dollars is already computed per metric, and
    this states the consequence of leaving it where it is.

    Only ever said about a metric that WORSENED, and only when there is a
    dollar figure behind it. "Nothing got worse" needs no warning, and a
    warning without a number is the generic nudge this is meant to replace.
    """
    worse = [m for m in review["metrics"]
             if m["verdict"] == "worsened" and m.get("monthly_dollars")]
    if not worse:
        return ""
    lead = max(worse, key=lambda m: abs(m["monthly_dollars"]))
    return (f"If {lead['label'].lower()} stays where it is, that is about "
            f"${abs(lead['monthly_dollars']):,.0f} a month — roughly "
            f"${abs(lead['monthly_dollars']) * 12:,.0f} over a year.")


# ── the ledger, for both reviews ─────────────────────────────────────────────

def silenced(restaurant_id, db_path=None) -> set:
    """Keys an answer on any surface is silencing (rec_ledger) — a priority
    the owner said no to on Home is not a priority in the weekly email."""
    try:
        import rec_ledger
        from models import DB_PATH
        return rec_ledger.silenced_keys(restaurant_id, db_path=db_path or DB_PATH)
    except Exception:
        return set()


def present(restaurant_id, priorities, fix_first, db_path=None, surface="weekly_email"):
    """Record the priorities and the one thing as shown in the periodic
    email. Only outside a web request: inside one, the review is being
    VIEWED on Home or previewed, and Home records its own impressions."""
    try:
        from flask import has_request_context
        if has_request_context():
            return
        import rec_ledger
        from models import DB_PATH
        items = [{"key": p["key"], "module": "home", "title": p.get("label"), "position": i + 1,
                  "dollar_value": p.get("monthly")} for i, p in enumerate(priorities or []) if p.get("key")]
        if fix_first and fix_first.get("key"):
            items.insert(0, {"key": fix_first["key"], "module": "home", "title": fix_first.get("what"),
                             "position": 0, "dollar_value": fix_first.get("dollars_monthly")})
        if items:
            rec_ledger.present_many(restaurant_id, items, surface, db_path=db_path or DB_PATH)
    except Exception as e:
        print(f"[review] impressions not recorded: {e}")
