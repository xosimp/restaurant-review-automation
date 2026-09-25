"""
command_center.py — the server half of the Command Center (Friction audit
9/25/26, items 14 and 48; U4 §5): the ⌘K palette on the web and the command
sheet on iOS read the same three things from here, through the same routes
(strategy_routes, web and mobile twins):

  registry(user)             GET  /api/command/registry
      The commands this login may run at this location: places to go
      (modules, sections, the daily report, locations), the confirmable
      actions (Ask's own write tools, through the same permission test Ask
      uses), and Ask itself. The client never lists a command that would be
      refused — and the server still refuses it (every route keeps its own
      decorators; this list only hides).

  search(user, q)            GET  /api/command/search?q=
      One ranked, permission-filtered search across places, locations,
      reviews, people, open issues, schedules, report nights and the
      caller's own Ask chats. A review's text is public-written: returned
      for display only and never handed to a model from here.

  propose(user, action, args)  POST /api/command/propose
      The SAME confirm card Ask renders (ask_cavnar_tools.build_proposal),
      built with no model call, with PROPOSAL_DENYLIST and the link refusal
      applied to the palette's arguments exactly as to a model's. Logged to
      ask_cavnar_actions as "proposed", so confirm and dismiss go through
      Ask's existing /api/ask-cavnar/action and the audit trail is one.

  reopen(user, proposal_id)  GET  /api/ask-cavnar/proposals/<id>
      A stored, unanswered Ask proposal rebuilt from its row — "Open it" on
      Home's Still open used to re-ask the model for a card it had already
      made (U4-20). Rebuilt through build_proposal, so an order's draft hash
      is re-read from the order as it stands now.

Nothing here decides anything a person hasn't: no model call, no write
beyond the proposal's audit row. Free text in the palette goes to Ask's own
stream unchanged, so the answer checker (response_validation) and the Data
Health readiness gate still stand in front of every model answer.
"""
import json
import re

# ── the places a command can go ──────────────────────────────────────────────
# (id, label, keywords, nav, module_key). module_key is the product module
# (auth._MODULE_PREFIXES / permissions.MODULE_VIEW_PERMISSIONS) the place
# belongs to; None is every console login's (Home, Account).
_PLACES = (
    ("go:home", "Home", ("home", "brief", "today", "dashboard", "attention"), "home", None),
    ("go:reviews", "Reviews", ("reviews", "inbox", "replies", "google", "ratings"), "reviews", "reviews"),
    ("go:reviews/urgent", "Reviews › Urgent", ("urgent", "1 star", "low star", "unanswered"),
     "reviews?filter=urgent", "reviews"),
    ("go:reviews/pending", "Reviews › Waiting on a reply", ("drafts", "drafted", "approve", "pending"),
     "reviews?filter=pending", "reviews"),
    ("go:labor", "Labor", ("labor", "staff", "shifts", "payroll", "hours"), "labor", "labor"),
    ("go:labor/schedule", "Labor › Schedule", ("schedule", "week", "shifts", "publish", "send schedule"),
     "labor/schedule", "labor"),
    ("go:labor/requests", "Labor › Requests", ("time off", "requests", "swap", "drop", "pto"),
     "labor/requests", "labor"),
    ("go:labor/team", "Labor › Team", ("team", "roster", "ratings", "employees", "people"), "labor/team", "labor"),
    ("go:inventory", "Food Cost", ("food cost", "inventory", "stock", "margins", "menu"), "inventory", "inventory"),
    ("go:inventory/invoices", "Food Cost › Invoices", ("invoices", "scan", "prices", "costs"),
     "inventory/invoices", "inventory"),
    ("go:inventory/order", "Food Cost › Order", ("order", "suppliers", "reorder", "low stock"),
     "inventory/order", "inventory"),
    ("go:inventory/count", "Food Cost › Count", ("count", "count sheet", "stock take"), "inventory/count", "inventory"),
    ("go:marketing", "Marketing", ("marketing", "posts", "instagram", "facebook", "content"), "marketing", "marketing"),
    ("go:marketing/guests", "Marketing › Guest club", ("guests", "guest club", "texts", "sms", "win back"),
     "marketing/guests", "marketing"),
    ("go:intel", "Intel", ("intel", "competitors", "ai visibility", "market"), "intel", "intel"),
    ("go:recs", "My recommendation record", ("recommendations", "record", "track record"), "recs", None),
    ("go:account", "Account", ("account", "settings"), "account", None),
)
# Account's rail (templates/dashboard.html SECTIONS), each an address of its
# own: "account/notifications". People and Billing are the account holder's.
_ACCOUNT_SECTIONS = (
    ("restaurant", "Restaurant profile", ("profile", "voice", "hours", "contact"), False),
    # Its own section since the density round (9/25/26): the nightly
    # report's calendar and POS departments. Owner-only, like the page.
    ("report", "Daily report settings", ("daily report", "dsr", "fiscal", "periods", "pos departments"), True),
    ("people", "People", ("people", "team", "logins", "staff", "pins", "invite"), True),
    ("billing", "Billing", ("billing", "subscription", "invoice", "card", "plan"), True),
    ("notifications", "Notifications", ("notifications", "alerts", "digest", "texts", "email"), False),
    ("automation", "Automation, AI & memory", ("automation", "auto approve", "auto-approve", "autopilot",
                                                "memory", "remember", "decisions", "trust"), False),
    ("integrations", "Integrations", ("integrations", "connect", "pos", "toast", "square", "google"), False),
    ("security", "Security", ("security", "password", "two factor", "2fa", "sessions"), False),
    ("data", "Data", ("data", "export", "retention", "download"), False),
    ("support", "Support", ("support", "help", "contact"), False),
)
_DSR_PLACES = (
    ("go:dsr", "Last night's report", ("dsr", "daily sales", "last night", "report", "sales"), "dsr"),
    ("go:dsr/week", "This week's sales grid", ("week", "weekly sales", "grid"), "dsr/week"),
)

# ── the confirmable actions ──────────────────────────────────────────────────
# Ask's write tools a palette can run without arguments it would have to
# invent. Each is proposed (propose()), never run: the card names what goes
# out, and only a click or ⌘Enter on the card confirms it.
# tier (DESIGN_SYSTEM.md §10, the confirm/undo policy): 2 goes outside the
# restaurant (post, publish, text, email, order), 1 changes data here.
_ACTIONS = (
    ("approve_all_reviews", "Publish the drafted replies", ("publish", "approve all", "replies", "post replies"), 2),
    ("publish_schedule", "Send the schedule to staff", ("send schedule", "publish schedule", "text staff"), 2),
    ("send_supplier_order", "Send the supplier order", ("send order", "order", "suppliers", "email order"), 2),
    ("generate_schedule", "Generate next week's schedule", ("generate schedule", "build schedule", "new week"), 1),
    ("refresh_competitors", "Refresh competitor data", ("refresh competitors", "competitors", "intel"), 1),
)
# Tier of every write tool, for propose() of one a search result offers.
TIER = {"approve_review": 2, "retract_review_reply": 2, "send_guest_campaign": 2, "publish_instagram_post": 2,
        "publish_facebook_post": 2, "send_review_request": 2, "create_issue": 2, "draft_review_reply": 1,
        "set_auto_approve": 1, "set_data_retention": 1,
        # A shift-request answer emails the employee; a time-off answer
        # changes who can be scheduled here.
        "decide_shift_request": 2, "decide_time_off": 1}
TIER.update({a[0]: a[3] for a in _ACTIONS})

SEARCH_LIMIT = 20
_PER_SOURCE = 5
QUERY_MAX = 80


def _extra_permission(action):
    """The permission a route checks beyond the module, if any — so the
    registry hides what the route would refuse (client_api's publish checks
    SCHEDULE_PUBLISH, generate SCHEDULE_DRAFT, social posts MARKETING_APPROVE)."""
    from permissions import SCHEDULE_PUBLISH, SCHEDULE_DRAFT, MARKETING_APPROVE
    # Deciding a request is the Labor decide routes' SCHEDULE_DRAFT
    # (strategy_routes._may_draft) - Home's Approve / Deny open this card.
    return {"publish_schedule": SCHEDULE_PUBLISH, "generate_schedule": SCHEDULE_DRAFT,
            "decide_time_off": SCHEDULE_DRAFT, "decide_shift_request": SCHEDULE_DRAFT,
            "publish_instagram_post": MARKETING_APPROVE, "publish_facebook_post": MARKETING_APPROVE,
            "send_guest_campaign": MARKETING_APPROVE}.get(action)


def _restaurant(user):
    from models import get_restaurant
    return get_restaurant(user["restaurant_id"])


def _view(user, restaurant):
    import ask_cavnar_tools
    return ask_cavnar_tools.viewer_restaurant(restaurant, user)


def _sees_module(user, restaurant, view, key):
    """Sold to this restaurant AND readable by this login. Intel has no flag
    of its own: all four modules (models.is_full_tier) plus a listing."""
    if key is None:
        return True
    from permissions import MODULE_VIEW_PERMISSIONS, has_permission
    perm = MODULE_VIEW_PERMISSIONS.get(key)
    if perm and not user.get("is_admin") and not has_permission(user, perm):
        return False
    if key == "intel":
        from models import is_full_tier
        return bool(is_full_tier(restaurant) and getattr(restaurant, "google_place_id", None))
    return bool(getattr(view, "module_" + key, 0))


def _sees_dsr(user, restaurant):
    try:
        from dsr import access
        return bool(getattr(restaurant, "dsr_enabled", 1)) and access.view_for(user) is not None
    except Exception:
        return False


def action_allowed(user, action, restaurant=None, view=None):
    """The one test behind the listed actions and propose(): a write tool,
    allowed for this login by Ask's own tool_allowed, plus the route's own
    extra permission."""
    import ask_cavnar_tools
    from permissions import has_permission
    if not ask_cavnar_tools.is_write_tool(action):
        return False
    restaurant = restaurant or _restaurant(user)
    view = view or _view(user, restaurant)
    if not ask_cavnar_tools.tool_allowed(action, view):
        return False
    if action == "refresh_competitors" and not _sees_module(user, restaurant, view, "intel"):
        return False
    need = _extra_permission(action)
    return not need or bool(user.get("is_admin")) or has_permission(user, need)


def _locations(user):
    """[{id, name, active}] this login may switch to — none without
    LOCATION_SWITCH (client_api._do_group_locations, the header's list)."""
    try:
        import client_api
        payload, _ = client_api._do_group_locations(user)
        return payload.get("locations") or []
    except Exception:
        return []


def _cmd(cid, label, keywords, kind, tier, nav=None, action=None, args=None):
    return {"id": cid, "label": label, "keywords": list(keywords), "kind": kind, "tier": tier,
            "nav": nav, "action": action, "args": args}


def registry(user, _locs=None):
    """{ok, commands:[{id, label, keywords, kind, tier, nav, action, args}]}.
    `_locs`: the switchable locations when the caller has them already
    (search reads them once per keystroke, not twice)."""
    from permissions import is_principal
    restaurant = _restaurant(user)
    if restaurant is None:
        return {"ok": False, "error": "Restaurant not found."}, 404
    view = _view(user, restaurant)
    out = []
    for cid, label, kw, nav, key in _PLACES:
        if _sees_module(user, restaurant, view, key):
            out.append(_cmd(cid, label, kw, "nav", 0, nav=nav))
    principal = is_principal(user)
    for sec, label, kw, owner_only in _ACCOUNT_SECTIONS:
        if owner_only and not principal:
            continue
        out.append(_cmd("go:account/" + sec, "Account › " + label, kw, "nav", 0, nav="account/" + sec))
    if _sees_dsr(user, restaurant):
        for cid, label, kw, nav in _DSR_PLACES:
            out.append(_cmd(cid, label, kw, "nav", 0, nav=nav))
    for loc in (_locations(user) if _locs is None else _locs):
        if loc.get("active"):
            continue
        out.append(_cmd(f"location:{loc['id']}", f"Switch to {loc['name']}", ("switch", "location", loc["name"]),
                        "nav", 0, nav=f"location/{int(loc['id'])}"))
    for action, label, kw, tier in _ACTIONS:
        if action_allowed(user, action, restaurant, view):
            out.append(_cmd("action:" + action, label, kw, "action", tier, action=action, args={}))
    out.append(_cmd("ask", "Ask Cavnar AI", ("ask", "question", "why", "how"), "ask", 0, nav="ask"))
    return {"ok": True, "commands": out}, 200


# ── search ───────────────────────────────────────────────────────────────────

def _score(q, *fields):
    """0 = no match; higher is better: a field that starts with the query,
    then a word inside one that does, then a substring."""
    best = 0
    for f in fields:
        f = (f or "").lower()
        if not f:
            continue
        if f.startswith(q):
            best = max(best, 3)
        elif re.search(r"\b" + re.escape(q), f):
            best = max(best, 2)
        elif q in f:
            best = max(best, 1)
    return best


def _snippet(text, n=90):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _like(q):
    """A LIKE pattern that matches `q` literally: a typed % or _ was a
    wildcard, so "%" listed every review (re-audit F1-15)."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _search_reviews(rid, q):
    import models
    conn = models.get_conn()
    try:
        like = _like(q)
        rows = conn.execute(
            "SELECT id, author, rating, text, response_status FROM reviews WHERE restaurant_id=? "
            "AND deleted_at IS NULL AND (LOWER(author) LIKE ? ESCAPE '\\' OR LOWER(text) LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(NULLIF(review_date,''), fetched_at) DESC, id DESC LIMIT ?",
            (rid, like, like, _PER_SOURCE)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        state = {"drafted": "reply drafted", "pending": "no reply yet"}.get(r["response_status"] or "", "")
        out.append((_score(q, r["author"]) + 1, {
            "type": "review", "id": r["id"],
            "title": f"{int(r['rating'] or 0)}★ · {r['author'] or 'A guest'}",
            # Public-written text: shown, never sent to a model from here.
            "subtitle": _snippet(r["text"]) + (f" · {state}" if state else ""),
            "nav": f"review/{int(r['id'])}"}))
    return out


def _people(rid):
    """[{key, name, role}] — people.list_people (the one person record) when
    it exists, else the roster names."""
    try:
        import people
        return [{"key": p.get("key"), "name": p.get("name"), "role": p.get("role")}
                for p in (people.list_people(rid) or []) if p.get("name")]
    except Exception:
        pass
    try:
        from staff_roster import roster_names_for_restaurant
        out = []
        for name, role in roster_names_for_restaurant(rid):
            out.append({"key": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name, "role": role})
        return out
    except Exception:
        return []


def _search_people(rid, q):
    import nav
    out = []
    for p in _people(rid):
        s = _score(q, p["name"], p.get("role"))
        if s:
            out.append((s, {"type": "person", "id": p["key"], "title": p["name"],
                            "subtitle": p.get("role") or "Team", "nav": nav.path("person", p["key"])}))
    out.sort(key=lambda x: -x[0])
    return out[:_PER_SOURCE]


def _search_issues(user, rid, q):
    import issues
    out = []
    for i in issues.list_issues(rid, status="unresolved", limit=30, sees_loss=issues.viewer_sees_loss(user)):
        s = _score(q, i.get("title"), i.get("assignee_name"))
        if s:
            out.append((s, {"type": "issue", "id": i["id"], "title": i["title"],
                            "subtitle": (f"{i['assignee_name']} has it" if i.get("assignee_name") else "Unassigned"),
                            "nav": f"issue/{int(i['id'])}"}))
    return out[:_PER_SOURCE]


def _search_schedules(rid, q):
    import models
    from time_utils import mdy
    conn = models.get_conn()
    try:
        rows = conn.execute("SELECT id, week_start, published_at FROM schedule_history WHERE restaurant_id=? "
                            "AND superseded_by IS NULL ORDER BY week_start DESC, id DESC LIMIT 12",
                            (rid,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    out = []
    for r in rows:
        label = f"Week of {mdy(r['week_start'])}"
        s = _score(q, label, "schedule", "week")
        if s:
            out.append((s, {"type": "schedule", "id": r["id"], "title": label,
                            "subtitle": "Sent to staff" if r["published_at"] else "Drafted, not sent",
                            "nav": f"schedule/{int(r['id'])}"}))
    return out[:_PER_SOURCE]


def _search_dsr(rid, q):
    from dsr import store
    from time_utils import mdy
    out = []
    for r in store.list_reports(rid, limit=14):
        d = str(r.get("business_date") or "")[:10]
        if not d:
            continue
        label = f"Daily report · {mdy(d)}"
        s = _score(q, label, "report", "dsr", "night")
        if s:
            out.append((s, {"type": "dsr", "id": d, "title": label, "subtitle": "The nightly sales report",
                            "nav": f"dsr/night/{d}"}))
    return out[:_PER_SOURCE]


def _search_chats(user, rid, q):
    from models import list_ask_conversations
    out = []
    for c in list_ask_conversations(rid, viewer_id=user.get("id")):
        s = _score(q, c.get("title"))
        if s:
            out.append((s, {"type": "chat", "id": c["id"], "title": c.get("title") or "A chat with Ask",
                            "subtitle": "Ask Cavnar AI chat", "nav": f"ask?conversation={int(c['id'])}"}))
    return out[:_PER_SOURCE]


def search(user, q):
    """{ok, results:[{type, id, title, subtitle, nav, location:{id,name}}]}.
    Every result belongs to the active location (one location per command,
    U4 §5.4) except a location to switch to."""
    q = " ".join(str(q or "").lower().split())[:QUERY_MAX]
    if len(q) < 2:
        return {"ok": True, "results": []}, 200
    restaurant = _restaurant(user)
    if restaurant is None:
        return {"ok": False, "error": "Restaurant not found."}, 404
    rid = user["restaurant_id"]
    view = _view(user, restaurant)
    here = {"id": rid, "name": getattr(restaurant, "location_name", None) or restaurant.name}
    scored = []
    locs = _locations(user)
    cmds, _ = registry(user, _locs=locs)
    for c in cmds.get("commands") or []:
        if c["kind"] != "nav" or c["id"].startswith("location:"):
            continue
        s = _score(q, c["label"], *c["keywords"])
        if s:
            scored.append((s + 1, {"type": "place", "id": c["id"], "title": c["label"], "subtitle": "Go to",
                                   "nav": c["nav"]}))
    for loc in locs:
        s = _score(q, loc.get("name"))
        if s and not loc.get("active"):
            scored.append((s + 1, {"type": "location", "id": loc["id"], "title": f"Switch to {loc['name']}",
                                   "subtitle": "Location", "nav": f"location/{int(loc['id'])}",
                                   "location": {"id": loc["id"], "name": loc["name"]}}))
    sources = []
    if _sees_module(user, restaurant, view, "reviews"):
        sources.append(lambda: _search_reviews(rid, q))
    if _sees_module(user, restaurant, view, "labor"):
        sources.append(lambda: _search_people(rid, q))
        sources.append(lambda: _search_schedules(rid, q))
    sources.append(lambda: _search_issues(user, rid, q))
    if _sees_dsr(user, restaurant):
        sources.append(lambda: _search_dsr(rid, q))
    sources.append(lambda: _search_chats(user, rid, q))
    for fn in sources:
        try:
            scored.extend(fn())
        except Exception as e:
            print(f"[command] a search source failed rid={rid}: {e}")
    scored.sort(key=lambda x: -x[0])
    results = []
    for _s, r in scored[:SEARCH_LIMIT]:
        r.setdefault("location", here)
        results.append(r)
    return {"ok": True, "results": results}, 200


# ── propose / reopen ─────────────────────────────────────────────────────────

def propose(user, action, args):
    """{ok, proposal} — Ask's confirm card for `action`, logged as proposed."""
    import ask_cavnar_tools
    from models import log_ask_action
    action = str(action or "").strip()[:60]
    if not isinstance(args, dict):
        args = {}
    if not ask_cavnar_tools.is_write_tool(action):
        return {"ok": False, "error": "That isn't an action Cavnar AI can propose."}, 400
    if not action_allowed(user, action):
        return {"ok": False, "error": "That action isn't available to this login."}, 403
    rid = user["restaurant_id"]
    refusal = ask_cavnar_tools.proposal_refusal(action, args, rid)
    if refusal:
        return {"ok": False, "error": refusal}, 400
    p = ask_cavnar_tools.build_proposal(action, args, restaurant_id=rid)
    if not p:
        return {"ok": False, "error": "That needs more detail before it can be proposed."}, 400
    p["tier"] = TIER.get(action, 2)
    p["surface"] = "command"
    # surface='command': a palette or Home card the owner may simply close
    # is not something Cavnar proposed and nobody answered, so it never
    # becomes a Still-open item (action_queue, re-audit F1-7), and its
    # answer is not written into an unrelated Ask chat.
    p["proposal_id"] = log_ask_action(rid, action, summary=p.get("summary"), body=p.get("body"),
                                      outcome="proposed", user_id=user.get("id"),
                                      surface="command", target=p.get("target") or None)
    return {"ok": True, "proposal": p}, 200


def _stored_args(action, row):
    """The proposal's arguments from its row. A per-review proposal's id
    lives in its summary (build_proposal moves it into the route and out of
    the body — "…review #412")."""
    import ask_cavnar_tools
    try:
        args = json.loads(row.get("body") or "{}") or {}
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    # The path ids the card was built for (build_proposal's `target`,
    # stored on the row): a request decision, the invoice or the order it
    # named - never the newest one at reopen time (re-audit F1-8).
    try:
        target = json.loads(row.get("target") or "{}") or {}
    except (TypeError, ValueError):
        target = {}
    if isinstance(target, dict):
        for k in ("review_id", "request_id", "import_id", "po_id"):
            if target.get(k) is not None:
                try:
                    args[k] = int(target[k])
                except (TypeError, ValueError):
                    pass
    tool = ask_cavnar_tools._BY_NAME.get(action) or {}
    if "review_id" not in args and "{review_id}" in str((tool.get("route") or {}).get("web", "")):
        # An older row (no target): the id lives in the summary.
        m = re.search(r"#(\d+)", row.get("summary") or "")
        if m:
            args["review_id"] = int(m.group(1))
    return args


def reopen(user, proposal_id):
    """{ok, proposal} — a stored proposal's card again, with no model call.
    Scoped like Ask's action route: this restaurant, and another login's
    proposal only for an account holder. A settled one says how it ended."""
    import ask_cavnar_tools
    from models import get_ask_proposal
    from permissions import is_principal
    rid = user["restaurant_id"]
    try:
        pid = int(proposal_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "That proposal wasn't found."}, 404
    row = get_ask_proposal(rid, pid)
    if not row:
        return {"ok": False, "error": "That proposal wasn't found."}, 404
    owner_of = row.get("user_id")
    if owner_of is not None and user.get("id") is not None and owner_of != user.get("id") \
            and not is_principal(user):
        return {"ok": False, "error": "That proposal wasn't found."}, 404
    if row.get("settled"):
        return {"ok": True, "proposal": None,
                "settled": {"outcome": row["settled"]["outcome"], "at": row["settled"]["created_at"]},
                "summary": row.get("summary")}, 200
    action = row["action"]
    if not action_allowed(user, action):
        return {"ok": False, "error": "That action isn't available to this login any more."}, 403
    args = _stored_args(action, row)
    p = ask_cavnar_tools.build_proposal(action, args, restaurant_id=rid)
    if not p:
        return {"ok": False, "error": "That proposal can't be rebuilt — ask Cavnar AI again."}, 409
    p["proposal_id"] = pid
    p["tier"] = TIER.get(action, 2)
    p["surface"] = "reopened"
    return {"ok": True, "proposal": p}, 200


# ── one request: the confirmed action settles its own proposal ───────────────
# A confirm card used to be two unlinked requests: the action's route, then a
# fire-and-forget POST to /api/ask-cavnar/action. A lost second request left
# replies posted and the proposal "never confirmed" in Still open, where
# "Open it" could confirm it again (re-audit F1-9). The web card now names
# its proposal on the action request itself (this header), and the answer
# is recorded in the same request, only when the route said ok.
PROPOSAL_HEADER = "X-Cavnar-Proposal"
CONVERSATION_HEADER = "X-Cavnar-Conversation"


def _route_matches(action, path):
    """Whether `path` is the route the proposal's tool posts to (web or
    mobile), placeholders standing for one path segment - a header on any
    other request settles nothing."""
    import ask_cavnar_tools
    tool = ask_cavnar_tools._BY_NAME.get(action) or {}
    for surface in ("web", "mobile"):
        tmpl = str((tool.get("route") or {}).get(surface) or "")
        if not tmpl:
            continue
        rx = "^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(tmpl)) + "$"
        if re.match(rx, path or ""):
            return True
    return False


def settle_confirmed(response):
    """after_app_request: when a POST carrying PROPOSAL_HEADER succeeded
    (200, ok, not a started job, not a publish gate), record the proposal
    confirmed - scoped exactly like /api/ask-cavnar/action - and say so in
    the response (`proposal_settled`), so the client sends no second
    request. Anything unexpected leaves the response untouched and the
    client records the answer as before."""
    try:
        from flask import request
        raw = request.headers.get(PROPOSAL_HEADER)
        if not raw or request.method != "POST" or response.status_code != 200:
            return response
        if response.direct_passthrough or not response.is_json:
            return response
        pid = int(raw)
        data = response.get_json(silent=True)
        if not isinstance(data, dict) or data.get("ok") is not True or data.get("job_id") or data.get("needs_ack"):
            return response
        from auth import get_current_user
        user = get_current_user()
        if not user or not user.get("restaurant_id"):
            return response
        from models import get_ask_proposal
        prop = get_ask_proposal(user["restaurant_id"], pid)
        if not prop or prop.get("settled") or not _route_matches(prop["action"], request.path):
            return response
        import client_api
        payload, status = client_api._do_record_ask_action(
            user["restaurant_id"], user.get("id"),
            {"action": prop["action"], "outcome": "confirmed", "proposal_id": pid,
             "conversation_id": request.headers.get(CONVERSATION_HEADER)}, user=user)
        if status == 200 and payload.get("ok"):
            data["proposal_settled"] = True
            response.set_data(json.dumps(data))
    except Exception as e:
        print(f"[command] proposal not settled on its own request: {e}")
    return response
