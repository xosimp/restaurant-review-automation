"""
ask_cavnar_tools.py — the tools Ask Cavnar can actually call.

Two kinds, and the split is the whole safety design:

  READ tools run immediately. They only ever look at this restaurant's own
  data, so the worst case of the model calling one it didn't need is a
  wasted round trip.

  WRITE tools never execute here. The model "calling" one produces a
  PROPOSAL, which the client renders as a confirm card; only an explicit
  user tap fires the real route. That line exists because misreading
  "cancel the salmon order" as "send the salmon order" would email a
  supplier — an assistant that asks first is strictly better than one that
  is usually right.

Everything is scoped by restaurant_id supplied by the caller from the
authenticated session, never by the model. A tool cannot be pointed at
another restaurant even if the model asks it to.
"""
import json
import logging

log = logging.getLogger(__name__)

# Ceiling on rows any single read tool returns. The model pays for every
# token of this, and 20 reviews is plenty to answer "what are people
# complaining about" without burying the actual question.
_MAX_ROWS = 20


# ── Read tools ──────────────────────────────────────────────────────────────

def _read_reviews(restaurant_id, sentiment=None, urgency=None, search=None,
                  needs_response=None, limit=10):
    """Individual reviews — the gap that made "which reviews mention the
    patio?" unanswerable, since the context snapshot only ever carried
    aggregate counts."""
    from models import get_reviews_data
    filter_by = "all"
    if urgency == "high":
        filter_by = "urgent"
    elif sentiment in ("positive", "neutral", "negative"):
        filter_by = sentiment
    rows = get_reviews_data(restaurant_id, filter_by=filter_by, search=(search or ""))
    if needs_response is True:
        rows = [r for r in rows if not (r.get("draft_response") or "").strip()]
    out = []
    for r in rows[:min(int(limit or 10), _MAX_ROWS)]:
        out.append({
            "id": r.get("id"),
            "author": r.get("author"),
            "rating": r.get("rating"),
            "platform": r.get("platform"),
            "date": r.get("review_date"),
            "text": (r.get("text") or "")[:400],
            "sentiment": r.get("sentiment"),
            "urgency": r.get("urgency"),
            "has_draft": bool((r.get("draft_response") or "").strip()),
            "status": r.get("response_status"),
        })
    return {"count": len(out), "reviews": out}


def _read_menu_margins(restaurant_id, limit=10):
    import inventory_ledger
    data = inventory_ledger.menu_profitability(restaurant_id)
    n = min(int(limit or 10), _MAX_ROWS)
    return {
        "average_food_cost_pct": data.get("average_food_cost_pct"),
        "worst": data.get("worst"),
        "best": data.get("best"),
        "priced": data.get("priced", [])[:n],
        "unpriced_count": len(data.get("unpriced", [])),
        "unmapped_count": len(data.get("unmapped", [])),
    }


def _read_order_draft(restaurant_id):
    """What would be ordered, grouped by supplier — without sending it."""
    from inventory import build_supplier_orders
    draft = build_supplier_orders(restaurant_id)
    return {
        "total_cost": draft.get("total_cost"),
        "item_count": draft.get("item_count"),
        "groups": [
            {"supplier_name": g.get("supplier_name"),
             "supplier_email": g.get("supplier_email"),
             "total_cost": g.get("total_cost"),
             "items": g.get("items", [])[:_MAX_ROWS]}
            for g in (draft.get("groups") or [])
        ],
        "unassigned": [i.get("item") for i in (draft.get("unassigned") or [])][:_MAX_ROWS],
    }


def _read_schedule(restaurant_id):
    """The most recently generated schedule, plus who has actually opened
    the link it was sent with."""
    from models import get_conn, get_schedule_share_status
    from labor import employees_in_schedule
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, week_start, week_end, hours_scheduled, hours_budget, schedule_csv "
            "FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 1",
            (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "schedule_id": row["id"],
        "week_start": row["week_start"],
        "week_end": row["week_end"],
        "hours_scheduled": row["hours_scheduled"],
        "hours_budget": row["hours_budget"],
        "employees": employees_in_schedule(row["schedule_csv"] or ""),
        "share_status": get_schedule_share_status(restaurant_id, row["id"]),
    }


def _read_staff_availability(restaurant_id):
    from models import get_staff_availability
    return {"availability": get_staff_availability(restaurant_id)[:_MAX_ROWS]}


def _read_email_history(restaurant_id, limit=10):
    from models import get_email_log_for_client
    return {"emails": get_email_log_for_client(restaurant_id, limit=min(int(limit or 10), _MAX_ROWS))}


# ── Tool registry ───────────────────────────────────────────────────────────
# `kind` drives everything: "read" executes, "write" only ever proposes.

TOOLS = [
    {
        "kind": "read",
        "fn": _read_reviews,
        "spec": {
            "name": "read_reviews",
            "description": (
                "Read this restaurant's individual reviews. Use whenever the owner asks "
                "about what specific reviewers said, wants examples, or asks which reviews "
                "mention a topic. The data snapshot only has totals, so call this for "
                "anything about actual review content."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                    "urgency": {"type": "string", "enum": ["high"]},
                    "search": {"type": "string", "description": "Keyword to match in review text, e.g. 'patio'."},
                    "needs_response": {"type": "boolean", "description": "Only reviews with no draft written yet."},
                    "limit": {"type": "integer", "description": "Max reviews to return (default 10, max 20)."},
                },
            },
        },
    },
    {
        "kind": "read",
        "fn": _read_menu_margins,
        "spec": {
            "name": "read_menu_margins",
            "description": "Plate cost, sell price and margin per dish. Use for questions about which dishes make or lose money.",
            "input_schema": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Max dishes (default 10, max 20)."}}},
        },
    },
    {
        "kind": "read",
        "fn": _read_order_draft,
        "spec": {
            "name": "read_order_draft",
            "description": "This week's suggested supplier order, grouped by supplier, with costs. Read-only — does not send anything.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    {
        "kind": "read",
        "fn": _read_schedule,
        "spec": {
            "name": "read_schedule",
            "description": "The latest generated staff schedule: week, hours vs budget, who is on it, and who has opened their link.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    {
        "kind": "read",
        "fn": _read_staff_availability,
        "spec": {
            "name": "read_staff_availability",
            "description": "Days staff have said they cannot work, submitted through their own schedule link.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    {
        "kind": "read",
        "fn": _read_email_history,
        "spec": {
            "name": "read_email_history",
            "description": "Recent email Cavnar AI sent for this restaurant and whether it was delivered or failed.",
            "input_schema": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Max rows (default 10, max 20)."}}},
        },
    },

    # ── Write tools: proposal only ──────────────────────────────────────────
    # Each carries the route the client calls on confirm. Nothing here runs
    # server-side from a model decision.
    {
        "kind": "write",
        "confirm": True,
        "route": {"web": "/api/food-cost/send-order", "mobile": "/mobile/api/food-cost/send-order", "method": "POST"},
        "summary": "Email the suggested order to {supplier}",
        "spec": {
            "name": "send_supplier_order",
            "description": (
                "Propose emailing the suggested order to suppliers. This does NOT send — it asks "
                "the owner to confirm first. Call read_order_draft first so you can tell them what "
                "they are about to send."
            ),
            "input_schema": {"type": "object", "properties": {
                "supplier_email": {"type": "string", "description": "Send to just this supplier. Omit to send to every supplier with items."}}},
        },
    },
    {
        "kind": "write",
        "confirm": True,
        "route": {"web": "/api/labor/publish-schedule", "mobile": "/mobile/api/labor/publish-schedule", "method": "POST"},
        "summary": "Send the current schedule to staff",
        "spec": {
            "name": "publish_schedule",
            "description": "Propose sending each employee their own shifts by email. Does NOT send — the owner confirms first.",
            "input_schema": {"type": "object", "properties": {
                "schedule_id": {"type": "integer", "description": "Defaults to the most recent schedule."}}},
        },
    },
    {
        "kind": "write",
        "confirm": True,
        "route": {"web": "/api/reviews/approve-all", "mobile": "/mobile/api/reviews/approve-all", "method": "POST"},
        "summary": "Approve and post all drafted review replies",
        "spec": {
            "name": "approve_all_reviews",
            "description": "Propose approving every review reply that already has a draft awaiting approval. Posts publicly, so the owner confirms first.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    {
        "kind": "write",
        "confirm": True,
        "route": {"web": "/api/guest-campaign/send", "mobile": "/mobile/api/guest-campaign/send", "method": "POST"},
        "summary": "Text the guest club",
        "spec": {
            "name": "send_guest_campaign",
            "description": (
                "Propose texting the consented guest list. Does NOT send — the owner confirms first. "
                "Always include the exact message you are proposing so they can read it before agreeing."
            ),
            "input_schema": {"type": "object", "required": ["message"], "properties": {
                "message": {"type": "string", "description": "The exact SMS body to send."}}},
        },
    },
    {
        "kind": "write",
        "confirm": True,
        "route": {"web": "/api/generate-schedule", "mobile": "/mobile/api/labor/generate-schedule", "method": "GET"},
        "summary": "Generate next week's schedule",
        "spec": {
            "name": "generate_schedule",
            "description": "Propose building an optimised schedule for next week. Takes a minute and replaces the current draft, so the owner confirms first.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
]

_BY_NAME = {t["spec"]["name"]: t for t in TOOLS}


def tool_specs():
    """The `tools` array passed to the API — specs only, no internals."""
    return [t["spec"] for t in TOOLS]


def is_write_tool(name):
    tool = _BY_NAME.get(name)
    return bool(tool and tool["kind"] == "write")


def run_read_tool(name, restaurant_id, tool_input):
    """Execute a read tool. Returns a JSON string for the tool_result block.

    Errors come back as content rather than raising: a tool that fails
    should let the model say "I couldn't pull that up", not collapse the
    whole conversation.
    """
    tool = _BY_NAME.get(name)
    if not tool or tool["kind"] != "read":
        return json.dumps({"error": f"unknown read tool: {name}"})
    kwargs = {k: v for k, v in (tool_input or {}).items() if v is not None}
    try:
        return json.dumps(tool["fn"](restaurant_id, **kwargs), default=str)
    except TypeError as e:
        log.warning("ask_cavnar tool %s bad args %r: %s", name, tool_input, e)
        return json.dumps({"error": f"invalid arguments for {name}"})
    except Exception as e:
        log.warning("ask_cavnar tool %s failed: %s", name, e)
        return json.dumps({"error": f"could not read {name}"})


def build_proposal(name, tool_input):
    """Turn a write-tool call into the confirm card the client renders.

    `route` is what the client posts to on confirm — the same authenticated
    endpoint the button in the UI already uses, so a proposal can never
    reach anything the user couldn't already do themselves.
    """
    tool = _BY_NAME.get(name)
    if not tool or tool["kind"] != "write":
        return None
    args = {k: v for k, v in (tool_input or {}).items() if v is not None}
    summary = tool["summary"]
    if "{supplier}" in summary:
        summary = summary.replace("{supplier}", args.get("supplier_email") or "every supplier")
    return {
        "action": name,
        "summary": summary,
        "route": tool["route"],
        "body": args,
        "requires_confirmation": True,
    }
