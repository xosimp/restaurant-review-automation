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



def _read_shifts(restaurant_id, employee=None, limit=20):
    """The actual roster and logged shifts.

    Without this the assistant could see aggregate labour percentages and
    an (often empty) schedule_history, but not who actually works here —
    so it told restaurants with three weeks of shift data on file that they
    had "no staff added" and refused to act.
    """
    from labor import load_shifts, analyse_shifts
    from models import get_client_data

    # Deliberately NOT load_shifts_for_restaurant: that silently falls back
    # to a built-in sample roster when a restaurant has no CSV, which would
    # hand the model invented employees to discuss as if they were real
    # staff. Read the stored CSV directly and report honestly when absent.
    stored = get_client_data(restaurant_id) or {}
    csv_text = stored.get("shifts_csv")
    if not csv_text:
        return {"has_data": False, "employees": [], "shifts": [],
                "note": "No shift data uploaded or synced for this restaurant yet."}
    shifts = load_shifts(csv_string=csv_text) or []
    if employee:
        needle = employee.strip().lower()
        shifts = [s for s in shifts if needle in (s.get("employee") or "").lower()]
    if not shifts:
        return {"has_data": False, "employees": [], "shifts": []}
    try:
        analysis = analyse_shifts(shifts)
    except Exception:
        analysis = {}
    roster = analysis.get("employee_hours") or {}
    return {
        "has_data": True,
        "shift_count": len(shifts),
        "date_range": analysis.get("date_range"),
        "employees": [
            {"name": name, "scheduled_hours": v.get("scheduled"),
             "actual_hours": v.get("actual"), "shifts": v.get("shifts")}
            for name, v in list(roster.items())[:_MAX_ROWS]
        ],
        "overtime_risk": (analysis.get("overtime_risk") or [])[:_MAX_ROWS],
        "role_summary": analysis.get("role_summary"),
        "recent_shifts": [
            {"date": s.get("date"), "day": s.get("day"), "employee": s.get("employee"),
             "role": s.get("role"), "start": s.get("shift_start"), "end": s.get("shift_end"),
             "scheduled_hours": s.get("scheduled_hours")}
            for s in shifts[-min(int(limit or 20), _MAX_ROWS):]
        ],
    }


def _read_food_cost(restaurant_id):
    """Item-level detail behind the food-cost totals: what's low, what's
    being wasted, and which prices are moving."""
    from inventory import (load_inventory_for_restaurant, analyse_inventory,
                           compute_item_trends, build_price_watch)
    # Returns (items, is_live) — is_live is False when it fell back to the
    # built-in sample set, and the model must be told so it never quotes
    # sample stock levels as this restaurant's real ones.
    items, is_live = load_inventory_for_restaurant(restaurant_id)
    items = items or []
    if not is_live:
        return {"is_live": False,
                "note": "No real inventory data yet — nothing here reflects this restaurant. "
                        "Say so rather than quoting these numbers."}
    a = analyse_inventory(items)
    try:
        watch = build_price_watch(compute_item_trends(restaurant_id, items))
    except Exception:
        watch = []
    return {
        "is_live": True,
        "weekly_waste_cost": a.get("total_waste_cost_week"),
        "monthly_waste_projection": a.get("monthly_waste_projection"),
        "inventory_value": a.get("total_stock_value"),
        "critical_low": (a.get("critical_low") or [])[:_MAX_ROWS],
        "reorder_soon": (a.get("reorder_soon") or [])[:_MAX_ROWS],
        "waste_items": (a.get("waste_items") or [])[:_MAX_ROWS],
        "overstock": (a.get("overstock") or [])[:_MAX_ROWS],
        "price_watch": watch[:_MAX_ROWS],
    }


def _read_competitors(restaurant_id, limit=5):
    """The competitor set behind the intel summary — ratings, review counts,
    and what their own reviewers actually say."""
    import json as _json
    from models import get_restaurant
    from competitor_intel_format import extract_recs
    r = get_restaurant(restaurant_id)
    raw = getattr(r, "competitor_intel", None)
    if not raw:
        return {"has_data": False}
    try:
        blob = _json.loads(raw)
    except Exception:
        return {"has_data": True, "recommendations": extract_recs(raw if isinstance(raw, str) else "")}
    out = []
    for c in (blob.get("competitors") or [])[:min(int(limit or 5), _MAX_ROWS)]:
        out.append({
            "name": c.get("name"),
            "rating": c.get("rating"),
            "review_count": c.get("review_count"),
            "address": c.get("vicinity"),
            # Trimmed hard: five competitors x five full reviews would
            # dominate the context window on its own.
            "sample_reviews": [
                {"rating": rv.get("rating"), "when": rv.get("time"),
                 "text": (rv.get("text") or "")[:300]}
                for rv in (c.get("reviews") or [])[:3]
            ],
        })
    return {
        "has_data": True,
        "updated_at": getattr(r, "competitor_updated_at", None),
        "competitors": out,
        "recommendations": extract_recs(blob.get("insight", "") or ""),
    }


def _read_marketing_posts(restaurant_id, limit=10):
    """What has actually been published, and how it performed."""
    from models import get_conn
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT content_type, topic, post_platform, reach, impressions, engaged, created_at "
            "FROM marketing_content_log WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, min(int(limit or 10), _MAX_ROWS))
        ).fetchall()
    finally:
        conn.close()
    posts = [dict(r) for r in rows]
    published = [p for p in posts if p.get("post_platform")]
    return {
        "total_logged": len(posts),
        "published_count": len(published),
        "posts": posts,
    }


def _read_guest_club(restaurant_id):
    """Who is actually reachable by text, and who is only on file.

    The distinction matters: only a guest who opted in themselves can be
    texted, so "how big is my list" and "how many can I message" are
    different numbers.
    """
    from guest_marketing import get_guest_contacts
    from models import get_conn
    contacts = get_guest_contacts(restaurant_id)
    consented = [c for c in contacts if c["consent"] and not c["unsubscribed"]]
    conn = get_conn()
    try:
        campaigns = conn.execute(
            "SELECT message, sent_count, failed_count, created_at FROM guest_campaigns "
            "WHERE restaurant_id=? ORDER BY id DESC LIMIT 5", (restaurant_id,)
        ).fetchall()
    finally:
        conn.close()
    return {
        "total_on_file": len(contacts),
        "textable": len(consented),
        "unsubscribed": sum(1 for c in contacts if c["unsubscribed"]),
        "awaiting_optin": sum(1 for c in contacts if not c["consent"] and not c["unsubscribed"]),
        "recent_campaigns": [
            {"message": (dict(c)["message"] or "")[:120], "sent": dict(c)["sent_count"],
             "failed": dict(c)["failed_count"], "when": dict(c)["created_at"]}
            for c in campaigns
        ],
    }


# ── Direct actions ──────────────────────────────────────────────────────────
# Settings are reversible, private to the account, and visible in the UI the
# moment they change — so unlike anything that leaves the building, these run
# without a confirmation card.

_SETTABLE = {
    "auto_approve": "_do_auto_approve",
    "marketing_opt_out": "_do_marketing_opt_out",
    "login_notify": "_do_login_notify",
    "data_retention": "_do_data_retention",
}


def _apply_setting(restaurant_id, setting=None, value=None, **extra):
    """Change one account setting. Routed through client_api's own _do_*
    handlers so the assistant and the settings screen can never diverge on
    what a setting means or how it validates."""
    import client_api
    handler_name = _SETTABLE.get(setting)
    if not handler_name:
        return {"ok": False, "error": f"'{setting}' is not a setting I can change.",
                "settable": sorted(_SETTABLE)}
    handler = getattr(client_api, handler_name, None)
    if handler is None:
        return {"ok": False, "error": f"no handler for {setting}"}
    payload = dict(extra)
    if setting == "auto_approve":
        payload.setdefault("enabled", bool(value))
    elif setting == "marketing_opt_out":
        payload.setdefault("opted_out", bool(value))
    elif setting == "login_notify":
        payload.setdefault("enabled", bool(value))
    elif setting == "data_retention":
        # The handler speaks months (0 = keep everything, else 6/12/24/36),
        # and validates the value itself.
        payload.setdefault("months", value)
    result, status = handler(restaurant_id, payload)
    return {"ok": status == 200 and result.get("ok", False), "setting": setting, "result": result}


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

    {
        "kind": "read",
        "fn": _read_shifts,
        "spec": {
            "name": "read_shifts",
            "description": (
                "The staff roster and logged shifts — who works here, their hours, who is "
                "near overtime. Call this for any question about specific people or hours, "
                "and before saying a restaurant has no staff or no shift data."
            ),
            "input_schema": {"type": "object", "properties": {
                "employee": {"type": "string", "description": "Filter to one person by name."},
                "limit": {"type": "integer", "description": "Max recent shifts (default 20, max 20)."}}},
        },
    },
    {
        "kind": "read",
        "fn": _read_food_cost,
        "spec": {
            "name": "read_food_cost",
            "description": "Item-level food cost: what's critically low, what's being wasted, what's overstocked, and which supplier prices are rising.",
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    {
        "kind": "read",
        "fn": _read_competitors,
        "spec": {
            "name": "read_competitors",
            "description": "The competitor set: names, ratings, review counts and sample reviews from each, plus the recommendations drawn from them.",
            "input_schema": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Max competitors (default 5)."}}},
        },
    },
    {
        "kind": "read",
        "fn": _read_marketing_posts,
        "spec": {
            "name": "read_marketing_posts",
            "description": "Marketing content history — what was generated, what was actually published, and its reach/engagement.",
            "input_schema": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Max posts (default 10, max 20)."}}},
        },
    },
    {
        "kind": "read",
        "fn": _read_guest_club,
        "spec": {
            "name": "read_guest_club",
            "description": (
                "Guest text club make-up: how many are on file, how many can actually be "
                "texted (opted in themselves), how many unsubscribed, and recent campaigns. "
                "Call before proposing a campaign so you can say who it reaches."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
    },
    {
        # Reversible, account-private, and visible in the UI the moment it
        # changes — so this one runs rather than proposing.
        "kind": "action",
        "fn": _apply_setting,
        "spec": {
            "name": "change_setting",
            "description": (
                "Change an account setting directly (no confirmation needed): auto_approve, "
                "marketing_opt_out, login_notify, data_retention. Say what you changed. "
                "Only these four — anything else needs the settings screen."
            ),
            "input_schema": {"type": "object", "required": ["setting"], "properties": {
                "setting": {"type": "string", "enum": ["auto_approve", "marketing_opt_out",
                                                        "login_notify", "data_retention"]},
                "value": {"description": "true/false for the toggles; for data_retention one of 0 (keep everything), 6, 12, 24 or 36 months."}}},
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
        "route": {"web": "/api/regenerate-draft/{review_id}", "mobile": "/mobile/api/reviews/{review_id}/regenerate-draft", "method": "POST"},
        "summary": "Draft a reply to review #{review_id}",
        "spec": {
            "name": "draft_review_reply",
            "description": (
                "Propose writing (or rewriting) the AI reply for one review. Use read_reviews "
                "first to find the review id. Does not post anything — it only drafts, and the "
                "owner confirms."
            ),
            "input_schema": {"type": "object", "required": ["review_id"], "properties": {
                "review_id": {"type": "integer", "description": "From read_reviews."}}},
        },
    },
    {
        "kind": "write",
        "confirm": True,
        "route": {"web": "/approve/{review_id}", "mobile": "/mobile/api/reviews/{review_id}/approve", "method": "POST"},
        "summary": "Approve and post the reply to review #{review_id}",
        "spec": {
            "name": "approve_review",
            "description": (
                "Propose approving one review's drafted reply so it posts publicly. Use "
                "read_reviews to find the id and check it has a draft. Posts publicly, so the "
                "owner confirms first."
            ),
            "input_schema": {"type": "object", "required": ["review_id"], "properties": {
                "review_id": {"type": "integer", "description": "From read_reviews."}}},
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
    """True only for tools that must be confirmed before anything happens.
    An "action" tool is a direct write and deliberately not in this set."""
    tool = _BY_NAME.get(name)
    return bool(tool and tool["kind"] == "write")


def is_action_tool(name):
    tool = _BY_NAME.get(name)
    return bool(tool and tool["kind"] == "action")


def run_read_tool(name, restaurant_id, tool_input):
    """Execute a read tool. Returns a JSON string for the tool_result block.

    Errors come back as content rather than raising: a tool that fails
    should let the model say "I couldn't pull that up", not collapse the
    whole conversation.
    """
    tool = _BY_NAME.get(name)
    # "action" tools run here too — they are direct writes with no
    # confirmation step, which is the whole point of the separate kind.
    if not tool or tool["kind"] not in ("read", "action"):
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

    # Per-review actions address one row, so the id belongs in the path, not
    # the body. Substituted here (and stripped from the body) so the client
    # posts to a real URL rather than one containing a literal placeholder.
    route = dict(tool["route"])
    if "{review_id}" in route.get("web", "") or "{review_id}" in route.get("mobile", ""):
        try:
            review_id = int(args.get("review_id"))
        except (TypeError, ValueError):
            return None
        route["web"] = route["web"].replace("{review_id}", str(review_id))
        route["mobile"] = route["mobile"].replace("{review_id}", str(review_id))
        summary = summary.replace("{review_id}", str(review_id))
        args = {k: v for k, v in args.items() if k != "review_id"}

    return {
        "action": name,
        "summary": summary,
        "route": route,
        "body": args,
        "requires_confirmation": True,
    }
