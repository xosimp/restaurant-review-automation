"""
strategy_routes.py — the HTTP surface for the strategic-foundation modules:
issues, goals, outcome tracking, the dish scorecard and reprice suggestions,
invoice scanning, the demand forecast, loss signals and the morning brief.

Every route exists twice, web (/api/...) and mobile (/mobile/api/...), and
both halves call ONE shared body — the web/mobile-twin rule in CLAUDE.md. The
bodies take (current_user) and return (payload, status).

PERMISSIONS
  * Module gating comes from the path, as everywhere else: /food-cost/... is
    the Food Cost module plus FOOD_COST_VIEW (so a manager never sees margins
    or invoice prices), /labor/... is Labor. See auth._MODULE_PREFIXES.
  * Loss signals, issue routing and the morning brief are PRINCIPAL-only —
    the logins that administer the account (owner/client, TEAM_INVITE).
    Loss signals name approving managers; showing them to a manager is
    exactly wrong. Routing decides who gets texted.
  * Goals and outcomes on food-cost metrics are hidden from a login without
    FOOD_COST_VIEW, the same line the Food Cost tab draws.

The public issue page (/i/<token>) lives here too, on its own blueprint: the
token IS the credential, there is no session and no CSRF cookie to carry.
"""
from datetime import date

from flask import Blueprint, jsonify, request, render_template, abort

from auth import login_required, mobile_login_required

strategy_bp = Blueprint("strategy", __name__)
strategy_mobile_bp = Blueprint("strategy_mobile", __name__, url_prefix="/mobile/api")
issue_link_bp = Blueprint("issue_link", __name__)

# Metrics a login without FOOD_COST_VIEW may not see goals/outcomes for.
_FOOD_METRICS = {"food_cost_pct", "weekly_waste"}


def _rid(u):
    return u["restaurant_id"]


def _principal(u):
    from permissions import has_permission, TEAM_INVITE
    return bool(u.get("is_admin")) or has_permission(u, TEAM_INVITE)


def _sees_food(u):
    from permissions import has_permission, FOOD_COST_VIEW
    return bool(u.get("is_admin")) or has_permission(u, FOOD_COST_VIEW)


def _metric_visible(u, metric):
    base = (metric or "").split(":", 1)[0]
    return base not in _FOOD_METRICS or _sees_food(u)


def _local_today(u):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(_rid(u), naive=True).date()


def _body():
    return request.get_json(silent=True) or {}


def _forbidden(msg="Only the account owner can see this."):
    return {"ok": False, "error": msg}, 403


# ── issues ────────────────────────────────────────────────────────────────────

def _do_issues_list(u):
    import issues
    status = request.args.get("status") or "unresolved"
    return {"ok": True, "issues": issues.list_issues(_rid(u), status=status),
            "summary": issues.summary(_rid(u))}, 200


def _do_issue_create(u):
    import issues
    b = _body()
    title = (b.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "Give the issue a title."}, 400
    contact = b.get("assignee_contact_id")
    try:
        issue, _token = issues.create_issue(
            _rid(u), "manual", title, detail=(b.get("detail") or "").strip() or None,
            severity=b.get("severity") or "normal",
            assignee_contact_id=int(contact) if contact not in (None, "") else None,
            created_by=u.get("id"))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "issue": issue}, 200


def _do_issue_resolve(u, issue_id):
    import issues
    out = issues.resolve(_rid(u), issue_id, note=(_body().get("note") or "").strip() or None)
    if not out:
        return {"ok": False, "error": "Issue not found."}, 404
    return {"ok": True, "issue": out}, 200


def _do_issue_reassign(u, issue_id):
    import issues
    try:
        out = issues.reassign(_rid(u), issue_id, int(_body().get("contact_id")))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e) or "Pick a contact."}, 400
    if not out:
        return {"ok": False, "error": "Issue not found or already resolved."}, 404
    return {"ok": True, "issue": out}, 200


def _do_routing_get(u):
    if not _principal(u):
        return _forbidden()
    import issues
    from notify import get_alert_contacts
    contacts = [{"id": c["id"], "name": c["name"], "sms_consent": bool(c.get("sms_consent"))}
                for c in (get_alert_contacts(_rid(u)) or [])]
    return {"ok": True, "routing": issues.get_routing(_rid(u)), "contacts": contacts}, 200


def _do_routing_set(u):
    if not _principal(u):
        return _forbidden()
    import issues
    b = _body()
    role = b.get("role")
    if role not in ("manager", "escalation"):
        return {"ok": False, "error": "role must be manager or escalation"}, 400
    try:
        if b.get("contact_id") in (None, ""):
            issues.clear_routing(_rid(u), role)
        else:
            issues.set_routing(_rid(u), role, int(b["contact_id"]),
                               escalate_after_minutes=b.get("escalate_after_minutes"))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "routing": issues.get_routing(_rid(u))}, 200


# ── goals & outcomes ──────────────────────────────────────────────────────────

def _do_goals_list(u):
    import goals
    return {"ok": True, "goals": [g for g in goals.progress(_rid(u))
                                  if _metric_visible(u, g.get("metric"))]}, 200


def _do_goal_set(u):
    import goals
    b = _body()
    if not _metric_visible(u, b.get("metric")):
        return _forbidden("Food cost goals are for logins that can see food cost.")
    try:
        g = goals.set_goal(_rid(u), b.get("metric"), b.get("target"), deadline=b.get("deadline"),
                           note=b.get("note"), user_id=u.get("id"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "goal": g}, 200


def _do_goal_end(u, goal_id):
    import goals
    goals.end_goal(_rid(u), goal_id)
    return {"ok": True}, 200


def _do_outcomes_list(u):
    import outcomes
    rows = outcomes.list_outcomes(_rid(u), status=request.args.get("status") or None)
    live = {p["id"]: p for p in outcomes.progress(_rid(u))}
    out = []
    for r in rows:
        if not _metric_visible(u, r.get("metric")):
            continue
        if r["id"] in live:
            r = {**r, **{k: v for k, v in live[r["id"]].items() if k.startswith("interim")}}
        out.append({**r, "summary": outcomes.summarise(r) if r.get("status") != "tracking" else None})
    return {"ok": True, "outcomes": out, "caveat": outcomes.CAUSATION_CAVEAT}, 200


def _do_outcome_record(u):
    import outcomes
    b = _body()
    title = (b.get("title") or "").strip()
    if not title or not b.get("metric"):
        return {"ok": False, "error": "A title and a metric are required."}, 400
    if not _metric_visible(u, b.get("metric")):
        return _forbidden("Food cost tracking is for logins that can see food cost.")
    wd = b.get("window_days")
    if wd not in (None, ""):
        try:
            wd = int(wd)
        except (TypeError, ValueError):
            return {"ok": False, "error": "window_days must be a whole number"}, 400
        if not 7 <= wd <= 180:
            return {"ok": False, "error": "Track for between 7 and 180 days."}, 400
    else:
        wd = None
    try:
        o = outcomes.record(_rid(u), b.get("source") or "manual",
                            b.get("source_key") or f"manual:{title.lower()[:80]}",
                            title, b["metric"], user_id=u.get("id"),
                            window_days=wd)
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "outcome": o}, 200


def _do_outcome_abandon(u, outcome_id):
    import outcomes
    outcomes.abandon(_rid(u), outcome_id)
    return {"ok": True}, 200


def _do_metrics(u):
    import metrics
    keys = [k for k in ("labor_pct", "food_cost_pct", "sales", "avg_rating", "weekly_waste")
            if _metric_visible(u, k)]
    return {"ok": True, "metrics": [{"key": k, **metrics.describe(k)} for k in keys]}, 200


# ── food cost: menu + invoices ────────────────────────────────────────────────

def _do_dish_scorecard(u):
    import menu_intelligence
    return {"ok": True, **menu_intelligence.dish_scorecard(_rid(u))}, 200


def _do_reprice(u):
    import menu_intelligence
    return {"ok": True, **menu_intelligence.reprice_suggestions(_rid(u))}, 200


def _do_invoice_scan(u):
    import invoices
    f = request.files.get("file")
    if not f:
        return {"ok": False, "error": "Attach a photo or PDF of the invoice."}, 400
    data = f.read()
    media_type = (f.mimetype or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    try:
        out = invoices.scan(_rid(u), data, media_type, user_id=u.get("id"))
    except invoices.InvoiceError as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        from ai_utils import AIBudgetExceeded
        if isinstance(e, AIBudgetExceeded):
            return {"ok": False, "error": str(e)}, 429
        import ops
        ops.capture(e, job="invoice_scan", context=f"restaurant_id={_rid(u)}")
        return {"ok": False, "error": "The invoice couldn't be read right now. Try again shortly."}, 502
    return {"ok": True, "invoice": out}, 200


def _do_invoice_list(u):
    import invoices
    return {"ok": True, "invoices": invoices.list_imports(_rid(u))}, 200


def _do_invoice_get(u, import_id):
    import invoices
    out = invoices.get_import(_rid(u), import_id)
    if not out:
        return {"ok": False, "error": "Invoice not found."}, 404
    return {"ok": True, "invoice": out}, 200


def _do_invoice_apply(u, import_id):
    import invoices
    sel = _body().get("lines")
    if not isinstance(sel, list) or not sel:
        return {"ok": False, "error": "Pick at least one line to update."}, 400
    out = invoices.apply(_rid(u), import_id, sel, user_id=u.get("id"))
    return out, (200 if out.get("ok") else 409)


# ── labor: demand ─────────────────────────────────────────────────────────────

def _do_demand(u):
    import demand
    raw = (request.args.get("day") or "").strip()
    try:
        day = date.fromisoformat(raw) if raw else _local_today(u)
    except ValueError:
        return {"ok": False, "error": "day must be YYYY-MM-DD"}, 400
    return {"ok": True, "day": day.isoformat(), "forecast": demand.forecast_day(_rid(u), day),
            "slow_days": demand.slow_days(_rid(u)), "prep": demand.prep_list(_rid(u), day)}, 200


def _do_auto_draft_get(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "enabled": bool(getattr(r, "auto_draft_schedule", 0)),
            "external_tool": getattr(r, "external_scheduling_tool", None) or ""}, 200


def _do_auto_draft_set(u):
    from models import update_restaurant
    b = _body()
    fields = {}
    if "enabled" in b:
        fields["auto_draft_schedule"] = 1 if b["enabled"] else 0
    if "external_tool" in b:
        fields["external_scheduling_tool"] = (str(b["external_tool"] or "").strip()[:60]) or None
    if not fields:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), fields)
    return _do_auto_draft_get(u)


# ── principal-only ────────────────────────────────────────────────────────────

def _do_loss_signals(u):
    if not _principal(u):
        return _forbidden()
    import loss_detection
    return {"ok": True, **loss_detection.signals(_rid(u))}, 200


def _do_morning_brief(u):
    if not _principal(u):
        return _forbidden()
    import morning_brief
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "brief": morning_brief.build(_rid(u), restaurant=r, today=_local_today(u)),
            "settings": {"enabled": bool(getattr(r, "morning_brief_enabled", 1)),
                         "hour": int(getattr(r, "morning_brief_hour", 7) or 7)}}, 200


def _do_morning_brief_settings(u):
    if not _principal(u):
        return _forbidden()
    from models import update_restaurant
    import morning_brief
    b = _body()
    fields = {}
    if "enabled" in b:
        fields["morning_brief_enabled"] = 1 if b["enabled"] else 0
    if "hour" in b:
        try:
            hour = int(b["hour"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "hour must be a whole number"}, 400
        if not (4 <= hour < morning_brief.LATEST_SEND_HOUR):
            return {"ok": False, "error": f"Pick an hour between 4 and "
                                          f"{morning_brief.LATEST_SEND_HOUR - 1}."}, 400
        fields["morning_brief_hour"] = hour
    if not fields:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), fields)
    return {"ok": True, **fields}, 200


# ── registration: every body on both blueprints ───────────────────────────────

_ROUTES = [
    # (path, methods, body, endpoint)
    ("/issues", ["GET"], _do_issues_list, "issues_list"),
    ("/issues", ["POST"], _do_issue_create, "issue_create"),
    ("/issues/<int:issue_id>/resolve", ["POST"], _do_issue_resolve, "issue_resolve"),
    ("/issues/<int:issue_id>/reassign", ["POST"], _do_issue_reassign, "issue_reassign"),
    ("/issues/routing", ["GET"], _do_routing_get, "issue_routing_get"),
    ("/issues/routing", ["POST"], _do_routing_set, "issue_routing_set"),
    ("/goals", ["GET"], _do_goals_list, "goals_list"),
    ("/goals", ["POST"], _do_goal_set, "goal_set"),
    ("/goals/<int:goal_id>/end", ["POST"], _do_goal_end, "goal_end"),
    ("/outcomes", ["GET"], _do_outcomes_list, "outcomes_list"),
    ("/outcomes", ["POST"], _do_outcome_record, "outcome_record"),
    ("/outcomes/<int:outcome_id>/abandon", ["POST"], _do_outcome_abandon, "outcome_abandon"),
    ("/metrics", ["GET"], _do_metrics, "metrics_list"),
    ("/food-cost/dish-scorecard", ["GET"], _do_dish_scorecard, "dish_scorecard"),
    ("/food-cost/reprice", ["GET"], _do_reprice, "reprice"),
    ("/food-cost/invoices", ["GET"], _do_invoice_list, "invoice_list"),
    ("/food-cost/invoices", ["POST"], _do_invoice_scan, "invoice_scan"),
    ("/food-cost/invoices/<int:import_id>", ["GET"], _do_invoice_get, "invoice_get"),
    ("/food-cost/invoices/<int:import_id>/apply", ["POST"], _do_invoice_apply, "invoice_apply"),
    ("/labor/demand", ["GET"], _do_demand, "demand"),
    ("/labor/auto-draft", ["GET"], _do_auto_draft_get, "auto_draft_get"),
    ("/labor/auto-draft", ["POST"], _do_auto_draft_set, "auto_draft_set"),
    ("/loss-signals", ["GET"], _do_loss_signals, "loss_signals"),
    ("/morning-brief", ["GET"], _do_morning_brief, "morning_brief"),
    ("/morning-brief/settings", ["POST"], _do_morning_brief_settings, "morning_brief_settings"),
]


def _wrap(body, decorator):
    def view(current_user, **kw):
        payload, status = body(current_user, **kw)
        resp = jsonify(**payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp, status
    view.__name__ = body.__name__
    return decorator(view)


for _path, _methods, _body_fn, _ep in _ROUTES:
    strategy_bp.add_url_rule("/api" + _path, endpoint=_ep, methods=_methods,
                             view_func=_wrap(_body_fn, login_required))
    strategy_mobile_bp.add_url_rule(_path, endpoint=_ep, methods=_methods,
                                    view_func=_wrap(_body_fn, mobile_login_required))


# ── the public issue link ─────────────────────────────────────────────────────

@issue_link_bp.route("/i/<token>", methods=["GET", "POST"])
def issue_page(token):
    """What the assignee's text message opens.

    GET has NO side effect. Messaging apps fetch links to build previews
    (iMessage does it the moment the text lands), so "opening the link
    acknowledges it" would acknowledge every issue before a human saw it,
    and the escalation it exists to trigger would never fire. The assignee
    taps "I'm on it" (action=ack) or "Mark resolved" (action=resolve).
    """
    import issues
    done = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "ack":
            issue = issues.acknowledge(token)
            done = "ack"
        elif action == "resolve":
            issue = issues.resolve_by_token(
                token, note=(request.form.get("note") or "").strip()[:500] or None)
            done = "resolve"
        else:
            abort(400)
    else:
        issue = issues.by_token(token)
    if not issue:
        abort(404)
    resp = render_template("issue.html", issue=issue, token=token, done=done)
    return resp, 200, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                       "X-Robots-Tag": "noindex"}
