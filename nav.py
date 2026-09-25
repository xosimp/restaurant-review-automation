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
