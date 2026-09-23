"""
dsr — the nightly End-of-Day Closeout & Daily Sales Report.

One pipeline, one fact snapshot, many views (docs/plans/DSR_ENGINE_PLAN.md):

  trigger ─► collect FACTS (deterministic, per block, each with a status and
             a source) ─► one AI narrative that reads only the facts ─► every
             view (owner, manager, Erik's weekly grid, email, push, brief, Ask)
             renders from that same snapshot.

This module is the contract every part shares. A BLOCK is a dict:

    {"status": ready | awaiting | unavailable | not_connected,
     "source": "rpower" | "toast" | "cavnar" | ... | None,
     "reason": owner-facing sentence when not ready ("Awaiting POS synchronization"),
     "metrics": {key: number or None},   # None = not measured. NEVER 0 for unknown.
     "detail": {...}}                     # lists and structures the views render

A report's facts are {"schema", "restaurant_id", "business_date", "fiscal",
"blocks": {name: block}, "missing": [reason, ...]}. Metrics flatten into the
searchable `dsr_metrics` table as "<block>.<key>".

The rule that governs everything here: never fabricate. A block that cannot
be measured says why; an estimate is labelled an estimate in its detail.
"""

SCHEMA_VERSION = 1

# Block statuses.
READY = "ready"
AWAITING = "awaiting"            # the data will come (POS still settling, sync pending)
UNAVAILABLE = "unavailable"      # connected, but this night can't be measured
NOT_CONNECTED = "not_connected"  # the integration isn't set up
STATUSES = (READY, AWAITING, UNAVAILABLE, NOT_CONNECTED)

# Report stages, in order of a normal night. `provisional` and `failed` are
# terminal alternatives to `final`.
STAGES = ("scheduled", "awaiting_close", "collecting", "writing", "final", "provisional", "failed")
TERMINAL_STAGES = ("final", "provisional", "failed")

# The blocks, in the order a report shows them.
BLOCKS = ("sales", "labor", "food", "reviews", "marketing", "intel", "closeout")

# Owner-facing sentences for a block that isn't ready, by block.
MISSING_TEXT = {
    "sales": "Awaiting POS synchronization",
    "labor": "Labor data unavailable",
    "food": "Inventory data unavailable",
    "reviews": "Review sync delayed",
    "marketing": "Marketing data unavailable",
    "intel": "Local data unavailable",
    "closeout": "No manager closeout filed",
}

# Erik's six sales categories (his DSR's CURRENT block). A restaurant's POS
# departments map onto these through dsr_category_map; anything unmapped is
# shown as "Unmapped", never guessed into a category.
DEFAULT_CATEGORIES = ("Food", "Liquor", "Beer", "Wine", "Retail", "NA Beverage")
UNMAPPED = "Unmapped"


def block(status, source=None, reason=None, metrics=None, detail=None, block_name=None):
    """A validated block. Metrics must be numbers or None; a status other
    than ready must carry a reason (defaulting to the block's MISSING_TEXT)."""
    if status not in STATUSES:
        raise ValueError(f"unknown block status {status!r}")
    clean = {}
    for k, v in (metrics or {}).items():
        if v is None or isinstance(v, bool):
            clean[str(k)] = None if v is None else int(v)
        elif isinstance(v, (int, float)):
            clean[str(k)] = round(float(v), 4)
        else:
            raise ValueError(f"metric {k!r} must be a number or None, got {type(v).__name__}")
    if status != READY and not reason:
        reason = MISSING_TEXT.get(block_name or "", "Data unavailable")
    return {"status": status, "source": source, "reason": reason if status != READY else None,
            "metrics": clean, "detail": detail or {}}


def missing_reasons(blocks: dict) -> list:
    """The owner-facing sentences for every block that isn't ready, in
    report order."""
    out = []
    for name in BLOCKS:
        b = (blocks or {}).get(name)
        if b and b.get("status") != READY and b.get("reason"):
            out.append(b["reason"])
    return out


from dsr.store import init_dsr  # noqa: E402,F401  (facade: models.init_db calls it)
