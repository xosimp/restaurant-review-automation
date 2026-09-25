"""nav.py — one address for every place and item an owner can be sent to
(Friction audit, 9/25/26: "Reply now", "Send now", "Answer it" and nearly
every alert named a MODULE, so web and iOS opened the module's top and the
owner hunted for the item).

A nav path is a short string the server puts on anything that sends the
owner somewhere — Home attention items, action_queue items, notifications,
push payloads, command-palette results — and that web (`cavNav(path)` in
templates/dashboard.html) and iOS (`NavPath` in Core/NavPath.swift) both
open the same way:

    <head>[/<rest>][?<key>=<value>&…]

  head   a module — home, reviews, labor, inventory, marketing, intel,
         account, dsr, ask, recs — or an item kind: review, schedule,
         person, invoice, order, issue, request, action, location.
  rest   a section of the module ("labor/schedule", "account/notifications",
         "inventory/invoices") or the item's id ("review/412",
         "schedule/88", "person/dana-k", "dsr/night/2026-09-24").
  query  a filter or a prefill ("reviews?filter=urgent", "ask?q=why…").

Unknown heads, sections or ids degrade: the client opens the module (or
Home) rather than failing, so a newer server never strands an older app.
"""
from urllib.parse import quote, urlencode

MODULES = ("home", "reviews", "labor", "inventory", "marketing", "intel", "account", "dsr", "ask", "recs")
ITEMS = ("review", "schedule", "person", "invoice", "order", "issue", "request", "action", "location")

# The module a head belongs to — what a client opens when it cannot focus
# the item itself (permission gating reads this too).
MODULE_OF = {"review": "reviews", "schedule": "labor", "person": "labor", "invoice": "inventory",
             "order": "inventory", "issue": "home", "request": "labor", "action": "home", "location": "home"}


def path(head, *rest, **query) -> str:
    """Build a nav path: path("review", 412) → "review/412";
    path("reviews", filter="urgent") → "reviews?filter=urgent"."""
    head = str(head or "home").strip().lower()
    parts = [head] + [quote(str(r), safe="-_.") for r in rest if r not in (None, "")]
    out = "/".join(parts)
    q = {k: v for k, v in query.items() if v not in (None, "")}
    if q:
        out += "?" + urlencode(q)
    return out


def module_of(nav_path) -> str:
    """The module a nav path lives in ("review/412" → "reviews")."""
    head = str(nav_path or "").split("?", 1)[0].split("/", 1)[0].strip().lower()
    if head in MODULES:
        return head
    return MODULE_OF.get(head, "home")


# ── the notification → nav map (the bell's rows; push payloads may use it) ──
# A row names WHERE its alert is about, not just the module: a review alert
# opens that review, a held schedule the schedule, a sign-in Security.
# Ask-type rows (briefs, the month's review) carry the question the alert
# stands for, so the bell opens Ask on it the way a push does on iOS.
_ALERT_NAV = {
    "labor_over": "labor", "labor_reminder": "labor", "coverage": "labor/schedule",
    "schedule_drafted": "labor/schedule", "schedule_publish_pending": "labor/schedule",
    "schedule_publish_held": "labor/schedule", "shift_request": "labor/requests",
    "food_waste": "inventory", "price_spike": "inventory/invoices", "critical_low": "inventory/order",
    "order_send_pending": "inventory/order", "order_send_held": "inventory/order",
    "order_send_voided": "inventory/order",
    "ai_visibility_drop": "intel", "competitor_move": "intel",
    "demand_opportunity": "marketing", "review_request_nudge": "reviews", "while_away": "reviews",
    "dsr": "dsr",
    # Issues render in Home's Needs attention card (iOS opens Home too).
    "issue": "issue", "issue_escalated": "issue",
    "login": "account/security", "staff_signin": "account/people",
    "connection_lost": "account/integrations", "data_source_down": "account/integrations",
    "data_source_restored": "account/integrations", "platform_alert": "home",
}
_ALERT_ASK = {
    "morning_brief": "Walk me through this morning's brief.",
    "daily_briefing": "Walk me through this morning's brief.",
    "intraday_pulse": "How is today going so far?",
    "closing_summary": "How did tonight go?",
    "weekly_review": "How did last week go?",
    "monthly_review": "How did last month go?",
    "outcome_achieved": "Which of my tracked changes just came through?",
    "milestone": "What milestone did we just reach?",
}
_URGENT_REVIEW_ALERTS = {"1star", "2star", "health", "neg_spike", "negative_trend", "rating_threshold"}


def for_notification(alert_type, review_id=None, ask_prompt=None) -> str:
    """The nav path one notification opens. A row about a review opens the
    review; an urgent one with no review id opens the urgent filter."""
    t = str(alert_type or "").strip()
    if t in _ALERT_ASK or ask_prompt:
        return path("ask", q=(ask_prompt or _ALERT_ASK.get(t)))
    if t in _ALERT_NAV:
        return _ALERT_NAV[t]
    if review_id not in (None, ""):
        return path("review", review_id)
    if t in _URGENT_REVIEW_ALERTS:
        return path("reviews", filter="urgent")
    return "reviews"


def request(kind, item_id) -> str:
    """A labor request's nav — "request/time_off-12", "request/shift_request-7"
    (`kind` is the action_queue kind, the id its row)."""
    return path("request", f"{kind}-{int(item_id)}")
