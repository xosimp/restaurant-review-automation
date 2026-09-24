"""
strategy_routes.py — the HTTP surface for the strategic-foundation modules:
issues, goals, outcome tracking, the dish scorecard and reprice suggestions,
invoice scanning, the demand forecast, loss signals, the morning brief and
the nightly DSR (dsr/: the report, its progress, Close day).

Every route exists twice, web (/api/...) and mobile (/mobile/api/...), and
both halves call ONE shared body — the web/mobile-twin rule in CLAUDE.md. The
bodies take (current_user) and return (payload, status).

PERMISSIONS
  * Module gating comes from the path, as everywhere else: /food-cost/... is
    the Food Cost module plus FOOD_COST_VIEW (so a manager never sees margins
    or invoice prices), /labor/... is Labor. See auth._MODULE_PREFIXES.
  * Loss signals need LOSS_VIEW: owners hold it, and an owner may grant it
    to a manager (permission_grants) — a signal can name the approving
    manager, so it is a deliberate per-person choice. Issue routing and the
    brief's send settings are principal-only (TEAM_INVITE). The brief itself
    is built per viewer from what that login may see.
  * Goals and outcomes on food-cost metrics are hidden from a login without
    FOOD_COST_VIEW, the same line the Food Cost tab draws.

The public issue page (/i/<token>) lives here too, on its own blueprint: the
token IS the credential, there is no session and no CSRF cookie to carry.
"""
from datetime import date

from flask import Blueprint, Response, jsonify, request, render_template, abort

from auth import login_required, mobile_login_required

strategy_bp = Blueprint("strategy", __name__)
strategy_mobile_bp = Blueprint("strategy_mobile", __name__, url_prefix="/mobile/api")
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(strategy_bp)
_json_object_guard(strategy_mobile_bp)
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


def _limited(u, bucket, max_calls, window_secs):
    """Per-restaurant limiter for routes that text a phone or run a paid
    model. The daily AI budget stops runaway spend; this stops one screen
    (or one script) from texting a manager twenty times in a minute."""
    from ai_utils import ai_rate_limited
    return ai_rate_limited(f"{bucket}:{_rid(u)}", max_calls=max_calls, window_secs=window_secs)


_SLOW_DOWN = ({"ok": False, "error": "That's a lot in a short time — wait a few minutes and try again."}, 429)


# ── issues ────────────────────────────────────────────────────────────────────

# Home's open-issues list (web hbCoverButtons, iOS HomeDay) shows the first
# four unresolved issues, and on a coverage issue up to two suggested covers
# nobody has been asked yet.
HOME_ISSUES_SHOWN = 4
COVERS_SHOWN = 2


def askable_covers(issue) -> list:
    """The suggested covers a client shows on a coverage issue: not yet
    asked, at most COVERS_SHOWN — the same rule both Home clients apply."""
    if not issue or issue.get("kind") != "coverage" or issue.get("status") == "resolved":
        return []
    meta = issue.get("meta") or {}
    asked = {str(a.get("name") or "").strip().lower() for a in (meta.get("asked") or []) if isinstance(a, dict)}
    return [c for c in (meta.get("covers") or [])
            if isinstance(c, dict) and c.get("name") and str(c["name"]).strip().lower() not in asked][:COVERS_SHOWN]


def present_covers(rid, issue_rows, surface, user_id=None) -> dict:
    """A coverage issue's suggested covers are a recommendation ("cover:
    <date>:<person>", intraday.cover_key) — presented where they are
    RENDERED, never when the issue is filed (re-audit C3). Each issue shown
    with covers gains `cover_rec_key` and `cover_answerable` (asking one of
    them is the yes, intraday.ask_to_cover). Never raises."""
    try:
        import intraday
        import rec_delivery
        items = []
        for i, issue in enumerate(issue_rows or []):
            covers = askable_covers(issue)
            if not covers:
                continue
            key = intraday.cover_key(issue)
            issue["cover_rec_key"] = key
            issue["cover_answerable"] = rec_delivery.answerable(key)
            who = " or ".join(str(c["name"]) for c in covers)
            missing = (issue.get("meta") or {}).get("missing") or "the missing shift"
            items.append({"key": key, "module": "labor", "kind": "cover", "position": i,
                          "title": f"Ask {who} to cover {missing}"[:200]})
        return rec_delivery.present_now(rid, surface, items, user_id=user_id)
    except Exception as e:
        print(f"[issues] covers not presented rid={rid}: {e}")
        return {}


def _do_issues_list(u):
    import issues
    status = request.args.get("status") or "unresolved"
    # A loss issue names the manager who approved the comps; only a login
    # with LOSS_VIEW reads it (re-audit A-8). Web and mobile share this body.
    loss = issues.viewer_sees_loss(u)
    rows = issues.list_issues(_rid(u), status=status, sees_loss=loss)
    if status == "unresolved":
        # What Home renders from this list: its covers are shown there.
        present_covers(_rid(u), rows[:HOME_ISSUES_SHOWN], "home", user_id=u.get("id"))
    return {"ok": True, "issues": rows, "summary": issues.summary(_rid(u), sees_loss=loss)}, 200


def _do_issue_create(u):
    import issues
    b = _body()
    title = (b.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "Give the issue a title."}, 400
    contact = b.get("assignee_contact_id")
    if _limited(u, "issue_text", 20, 3600):
        return _SLOW_DOWN
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
    if _limited(u, "issue_text", 20, 3600):
        return _SLOW_DOWN
    try:
        out = issues.reassign(_rid(u), issue_id, int(_body().get("contact_id")))
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e) or "Pick a contact."}, 400
    if not out:
        return {"ok": False, "error": "Issue not found or already resolved."}, 404
    return {"ok": True, "issue": out}, 200


def _do_issue_ask_cover(u, issue_id):
    """One tap on a coverage issue's suggested cover: text (or email) that
    person the cover request. Nothing else changes — the schedule moves only
    when the manager decides who is on (intraday.ask_to_cover)."""
    import intraday
    from permissions import has_permission, LABOR_VIEW
    if not has_permission(u, LABOR_VIEW):
        return _forbidden("Only someone who can see Labor can ask a teammate to cover.")
    name = (_body().get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "Who should be asked?"}, 400
    if _limited(u, "issue_text", 20, 3600):
        return _SLOW_DOWN
    out = intraday.ask_to_cover(_rid(u), issue_id, name, user_id=u.get("id"))
    return out, (200 if out.get("ok") else 400)


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
    ids = None
    raw_ids = request.args.get("ids")
    if raw_ids:
        try:
            ids = [int(x) for x in raw_ids.split(",") if x.strip()][:200]
        except ValueError:
            return {"ok": False, "error": "ids must be comma-separated numbers"}, 400
    rows = outcomes.list_outcomes(_rid(u), status=request.args.get("status") or None,
                                  limit=200 if ids else 50, ids=ids)
    live = {p["id"]: p for p in outcomes.progress(_rid(u))}
    # The recommendation each result measures, so a check-in is only offered
    # where the server has one to attach it to (rec-ROI #21).
    keys = outcomes.checkin_keys(_rid(u), [r["id"] for r in rows])
    out = []
    for r in rows:
        if not _metric_visible(u, r.get("metric")):
            continue
        if r["id"] in live:
            r = {**r, **{k: v for k, v in live[r["id"]].items() if k.startswith("interim")}}
        out.append({**r, "summary": outcomes.summarise(r) if r.get("status") != "tracking" else None,
                    "checkin_key": keys.get(r["id"])})
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
    source_key = b.get("source_key") or f"manual:{title.lower()[:80]}"
    try:
        # One tracker per metric (rec-ROI #3): a second Track on a number
        # already being measured is answered, not started.
        res = outcomes.start(_rid(u), b.get("source") or "manual", source_key, title, b["metric"],
                             user_id=u.get("id"), window_days=wd, module=b.get("module"), gate="metric")
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    o = res.get("outcome") or {}
    # "Track this" on a recommendation is the owner acting on it: the
    # ledger records it as accepted (which also quiets the card while it is
    # measured), so the tracker's verdict lands on a taken episode. Taken
    # even when another tracker already measures the number — the owner
    # still took it; it just is not measured on its own.
    if (b.get("source") or "") == "recommendation" and b.get("source_key"):
        try:
            import rec_ledger as _rl
            surface = b.get("surface") if b.get("surface") in _rl.SURFACES else "home"
            meta = ({"tracking": o.get("id"), "metric": o.get("metric")} if o else
                    {"metric": b["metric"], "tracker_refused": (res["tracker_refused"].get("in_flight") or {}).get("id")})
            _rl.record(_rid(u), str(b["source_key"]), "accepted", surface=surface, user_id=u.get("id"),
                       role=u.get("role"), meta=meta,
                       source_ref=f"track:{o.get('id')}" if o else f"track-refused:{source_key}")
            import home_brief as _hb
            _hb.invalidate(_rid(u))
        except Exception as _tx:
            print(f"[outcomes] tracked recommendation not recorded in the ledger: {_tx}")
    if not res["ok"]:
        # ok stays true: the answer was taken. Both clients show `warning`.
        refused = res["tracker_refused"]
        return {"ok": True, "tracker_refused": refused, "warning": refused["reason"],
                "message": refused["reason"]}, 200
    # A tracker whose BASELINE could not be measured will come back "unknown"
    # when its window closes, 28 days from now, having told the owner
    # nothing. Still allowed — it is their change to track, and the data may
    # arrive tomorrow — but say so at the moment they press the button
    # rather than a month later.
    warning = None
    if o.get("baseline_value") is None:
        warning = (f"Tracking started, but {(o.get('metric_label') or o['metric']).lower()} "
                   f"can't be read right now ({o.get('baseline_detail') or 'no data'}), so "
                   f"there may be nothing to compare against.")
    return {"ok": True, "outcome": o, "tracker": res["tracker"], "warning": warning}, 200


def _do_outcome_abandon(u, outcome_id):
    import outcomes
    outcomes.abandon(_rid(u), outcome_id)
    return {"ok": True}, 200


def _do_value(u):
    """What Cavnar AI has been worth, and what is still on the table.

    Three figures that are never summed into each other (see
    value_delivered.py), plus the all-time biggest measured win and — where
    a sales audit is linked to the account — what that audit estimated
    against what has since been measured.

    Food-cost dollars are filtered the same way goals and outcomes are: a
    manager without FOOD_COST_VIEW sees the labor and reviews halves and
    not the margin ones.
    """
    import value_delivered
    rid = _rid(u)
    # Filtered BEFORE anything is summed. An earlier version of this route
    # stripped by_module and the opportunity items but left the headline
    # total whole, which handed a manager without FOOD_COST_VIEW the margin
    # dollars straight back by subtraction.
    denied = set() if _metric_visible(u, "food_cost_pct") else {"inventory"}
    out = value_delivered.breakdown(rid, denied_modules=denied)
    # Every stated rate, beside the four figures (never one of them) — the
    # same object delivered.rates carries, so a surface prices a reply from
    # the server's REPLY_RATE, not its own copy (rec-ROI #12).
    out["rates"] = value_delivered.rates()
    # Distinct work since sign-up. Needs no button and carries no dollars,
    # so it is not filtered by module permission — a manager may know the
    # product drafted 200 replies.
    try:
        led = value_delivered.ledger(rid)
        out["ledger"] = dict(led, lines=value_delivered.ledger_lines(led))
    except Exception as e:
        import ops
        ops.capture(e, job="value_ledger", context=f"restaurant_id={rid}")

    # The audit comparison is owner-level: it carries whole-business dollars
    # and what Will quoted at the table.
    try:
        from permissions import has_permission, TEAM_INVITE
        if has_permission(u, TEAM_INVITE):
            import promise
            out["promise"] = promise.compare(rid)
    except Exception as e:
        import ops
        ops.capture(e, job="value_promise", context=f"restaurant_id={rid}")
    return {"ok": True, **out}, 200


def _do_metrics(u):
    import metrics
    keys = [k for k in ("labor_pct", "overtime_hours", "food_cost_pct", "sales", "avg_rating", "weekly_waste",
                        "response_hours")
            if _metric_visible(u, k)]
    return {"ok": True, "metrics": [{"key": k, **metrics.describe(k)} for k in keys]}, 200


# ── food cost: menu + invoices ────────────────────────────────────────────────

def _do_dish_scorecard(u):
    import menu_intelligence
    return {"ok": True, **menu_intelligence.dish_scorecard(_rid(u))}, 200


def _do_reprice(u):
    # Each suggestion carries its rec_ledger key ("reprice:<dish>"), is
    # logged as shown, and one already answered is left out (audit #26).
    import menu_intelligence
    return {"ok": True, **menu_intelligence.presented_suggestions(_rid(u), surface="food",
                                                                   user_id=u.get("id"))}, 200


def _do_invoice_scan(u):
    import invoices
    f = request.files.get("file")
    if not f:
        return {"ok": False, "error": "Attach a photo or PDF of the invoice."}, 400
    if _limited(u, "invoice_scan", 10, 600):
        return _SLOW_DOWN
    data = f.read()
    media_type = (f.mimetype or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    try:
        out = invoices.scan(_rid(u), data, media_type, user_id=u.get("id"))
        # Supplier memory (ordering.py): a supplier whose scans the owner
        # has applied unchanged three times gets its clean lines applied on
        # scan; flagged lines still wait.
        try:
            import ordering
            out = ordering.auto_apply_if_trusted(_rid(u), out, user_id=u.get("id"))
        except Exception as _ae:
            import ops
            ops.capture(_ae, job="invoice_auto_apply", context=f"restaurant_id={_rid(u)}")
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


# ── the action queue ──────────────────────────────────────────────────────────

def _do_actions(u):
    """Everything still open for this login, most pressing first."""
    import action_queue
    return {"ok": True, **action_queue.items(_rid(u), viewer=u, today=_local_today(u))}, 200


def _do_action_snooze(u):
    import action_queue
    b = _body()
    key = (b.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "Which item?"}, 400
    out = action_queue.snooze(_rid(u), key, days=b.get("days") or action_queue.SNOOZE_DAYS,
                              user_id=u.get("id"), today=_local_today(u))
    return {"ok": True, **out}, 200


# ── close-out ─────────────────────────────────────────────────────────────────

def _do_closeout_get(u):
    """Tonight's close-out (or the last one filed), and the questions to
    answer — the four quick lines and the six the Daily Sales Report adds
    (`dsr_fields`), with what each is called. Any console login may read
    and write it: the person who closes is usually not the owner."""
    import closeout
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    day = closeout.business_date_for(r)
    return {"ok": True, "business_date": day.isoformat(),
            "closeout": closeout.get(_rid(u), day.isoformat()),
            "previous": closeout.latest(_rid(u), before=(day.isoformat())),
            "fields": list(closeout.FIELDS), "dsr_fields": list(closeout.DSR_FIELDS),
            "labels": dict(closeout.LABELS)}, 200


def _do_closeout_save(u):
    import closeout
    from models import get_restaurant
    b = _body()
    try:
        entry = closeout.save(_rid(u), b, user_id=u.get("id"),
                              submitted_by=(u.get("username") or "").title() or None,
                              restaurant=get_restaurant(_rid(u)))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "closeout": entry}, 200


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
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule.")
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


def _do_auto_publish_get(u):
    from models import get_restaurant, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN
    r = get_restaurant(_rid(u))
    trust = schedule_publish_trust(_rid(u))
    return {"ok": True, "enabled": bool(getattr(r, "auto_publish_schedule", 0)),
            "trust": trust, "needed": SCHEDULE_PUBLISH_TRUST_MIN,
            "armed": bool(getattr(r, "auto_publish_schedule", 0)) and trust >= SCHEDULE_PUBLISH_TRUST_MIN}, 200


def _may_publish(u):
    from permissions import has_permission, SCHEDULE_PUBLISH
    return bool(u.get("is_admin")) or has_permission(u, SCHEDULE_PUBLISH)


def _flag(v) -> bool:
    """A JSON boolean as a boolean: bool("false") is True, so a client that
    sends strings would have switched things ON by asking for off."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _do_auto_publish_set(u):
    # Auto-publish emails every employee on Friday. It is publishing, and
    # takes the publish permission — a teammate login could switch it on.
    if not _may_publish(u):
        return _forbidden("Only someone who can send the schedule to staff can turn on auto-publish.")
    from models import update_restaurant
    from client_api import log_account_event
    b = _body()
    if "enabled" not in b:
        return {"ok": False, "error": "Nothing to change."}, 400
    b["enabled"] = _flag(b["enabled"])
    update_restaurant(_rid(u), {"auto_publish_schedule": 1 if b["enabled"] else 0})
    log_account_event(_rid(u), "auto_publish_changed", current_user=u, detail="on" if b["enabled"] else "off")
    return _do_auto_publish_get(u)


def _do_auto_order_get(u):
    from models import get_restaurant
    import ordering
    r = get_restaurant(_rid(u))
    # The record per supplier, so the switch says what it would do.
    suppliers = []
    try:
        from models import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("SELECT DISTINCT supplier_name, supplier_email FROM ingredients WHERE restaurant_id=? "
                                "AND is_active=1 AND supplier_email IS NOT NULL AND supplier_email != ''", (_rid(u),)).fetchall()
        finally:
            conn.close()
        for row in rows:
            t = ordering.supplier_trust(_rid(u), row["supplier_email"])
            suppliers.append({"name": row["supplier_name"] or row["supplier_email"], "email": row["supplier_email"],
                              "orders": t["orders"], "median_total": t["median_total"], "trusted": t["trusted"],
                              "needed": max(0, ordering.ORDER_TRUST_MIN - (t["orders"] - t.get("edited", 0)))})
    except Exception:
        pass
    # An automatic order waits for a count from the last week
    # (ordering.COUNT_FRESH_DAYS); the switch says when that is holding it.
    try:
        fresh = ordering.count_freshness(_rid(u))
    except Exception:
        fresh = None
    return {"ok": True, "enabled": bool(getattr(r, "auto_order_trusted", 0)), "suppliers": suppliers,
            "undo_minutes": ordering.ORDER_UNDO_MINUTES, "count_freshness": fresh,
            "count_fresh_days": ordering.COUNT_FRESH_DAYS}, 200


def _do_auto_order_set(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can change ordering.")
    from models import update_restaurant
    from client_api import log_account_event
    b = _body()
    if "enabled" not in b:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), {"auto_order_trusted": 1 if b["enabled"] else 0})
    log_account_event(_rid(u), "auto_order_changed", current_user=u, detail="on" if b["enabled"] else "off")
    return _do_auto_order_get(u)


# ── #7 the count sheet, pre-filled ───────────────────────────────────────────
# Counts were typed from scratch (a CSV column) while the ledger already
# held the theoretical on-hand — last recount + receiving − depletion. The
# sheet opens filled with that number; the owner corrects, not types.

def _do_count_sheet_get(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can count.")
    import inventory_ledger
    rows = inventory_ledger.list_ingredients(_rid(u))
    out = [{"ingredient_id": r["id"], "name": r["name"], "unit": r.get("unit") or "",
            "category": r.get("category") or "", "expected": r.get("current_stock"),
            "par_level": r.get("par_level"), "last_recount_at": r.get("last_recount_at"),
            "avg_daily_usage": r.get("avg_daily_usage"),
            # The web's Suppliers block reads these; the order draft groups by them.
            "supplier_name": r.get("supplier_name") or "",
            "supplier_email": r.get("supplier_email") or ""} for r in rows]
    return {"ok": True, "items": out, "count": len(out)}, 200


def _do_count_sheet_save(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can count.")
    import inventory_ledger
    from client_api import log_account_event
    b = _body()
    items = b.get("items")
    if not isinstance(items, list) or not items:
        return {"ok": False, "error": "Nothing counted."}, 400
    if len(items) > 500:
        return {"ok": False, "error": "That is more items than one count holds."}, 400
    # The date is the count's place in the ledger, so it has to be one.
    # It went straight to record_recount unchecked: "9/21/26" — the
    # product's own display format — sorts after every ISO date and sat
    # inside every future 7-day window, and a future date did the same
    # (MOD-FC-15).
    day = str(b.get("date") or "").strip() or None
    if day:
        from datetime import date as _date
        try:
            parsed = _date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "error": "Pick the count date from the calendar."}, 400
        if len(day) != 10 or parsed > _local_today(u):
            return {"ok": False, "error": "A count can't be dated in the future."
                    if parsed > _local_today(u) else "Pick the count date from the calendar."}, 400
        day = parsed.isoformat()
    written, skipped = 0, []
    for it in items:
        try:
            ing_id = int(it.get("ingredient_id"))
            qty = float(it.get("counted"))
        except (TypeError, ValueError, AttributeError):
            skipped.append(it)
            continue
        if qty < 0 or qty > 1e7:
            skipped.append(it)
            continue
        try:
            inventory_ledger.record_recount(_rid(u), ing_id, qty, event_date=day, source="count_sheet")
            written += 1
        except Exception:
            skipped.append(it)
    if written:
        log_account_event(_rid(u), "inventory_counted", current_user=u, detail=f"{written} items")
    return {"ok": written > 0, "written": written, "skipped": len(skipped),
            "error": None if written else "No usable lines — each needs an ingredient and a number."}, (200 if written else 400)


def _do_recipe_drafts(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can review recipes.")
    import recipes
    # `missing` is what the "draft them now" button offers; `ingredients` is
    # whether a draft can be attempted at all (it may only use what is on
    # the list). Both best-effort: the drafts themselves are the point.
    missing = ingredients = 0
    try:
        import inventory_ledger
        missing = len(recipes.missing_recipes(_rid(u)))
        ingredients = len([i for i in (inventory_ledger.list_ingredients(_rid(u)) or []) if i.get("name")])
    except Exception:
        pass
    return {"ok": True, "drafts": recipes.list_drafts(_rid(u)), "missing": missing,
            "ingredients": ingredients}, 200


def _do_recipe_draft_now(u):
    """The owner pastes the menu (or asks for the POS dishes that have no
    recipe) and Cavnar drafts recipes from the ingredient list — the
    accept gate is unchanged. Bounded to RECIPE_DRAFT_LIMIT model calls a
    request, so the response arrives inside the worker's timeout; the
    reply says how many are left and the button offers the next batch."""
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can draft recipes.")
    import recipes
    from client_api import log_account_event
    if _limited(u, "recipe_draft", 6, 600):
        return _SLOW_DOWN
    text = _body().get("menu") or ""
    if len(text) > 20_000:
        return {"ok": False, "error": "That is more than a menu — paste the dishes, one a line."}, 400
    try:
        if text.strip():
            out = recipes.draft_from_menu(_rid(u), text, user_id=u.get("id"), limit=recipes.RECIPE_DRAFT_LIMIT)
        else:
            out = {"ok": True, **recipes.draft_missing(_rid(u), limit=recipes.RECIPE_DRAFT_LIMIT)}
        if out.get("ok"):
            out["remaining"] = len(recipes.missing_recipes(_rid(u)))
    except Exception as e:
        import ops
        ops.capture(e, job="recipe_draft_now", context=f"restaurant_id={_rid(u)}")
        return {"ok": False, "error": "Couldn't draft just now — try again in a minute."}, 500
    if out.get("drafted"):
        log_account_event(_rid(u), "recipes_drafted", current_user=u, detail=f"{out['drafted']} drafts")
    return out, (200 if out.get("ok") else 400)


def _do_recipe_draft_accept(u, draft_id):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can review recipes.")
    import recipes
    from client_api import log_account_event
    out = recipes.accept(_rid(u), int(draft_id), lines=_body().get("lines"), user_id=u.get("id"))
    if out.get("ok"):
        log_account_event(_rid(u), "recipe_accepted", current_user=u, detail=f"draft #{draft_id}, {out['written']} lines")
    return out, (200 if out.get("ok") else 409)


def _do_recipe_draft_reject(u, draft_id):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can review recipes.")
    import recipes
    out = recipes.reject(_rid(u), int(draft_id), user_id=u.get("id"))
    return out, (200 if out.get("ok") else 409)


def _do_recipes_import(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can import recipes.")
    import recipes
    from client_api import log_account_event
    text = _body().get("csv") or ""
    if len(text) > 400_000:
        return {"ok": False, "error": "That file is too large for one import."}, 400
    out = recipes.import_csv(_rid(u), text)
    if out.get("ok"):
        log_account_event(_rid(u), "recipes_imported", current_user=u, detail=f"{out['written']} lines")
    return out, (200 if out.get("ok") else 400)


def _do_weekly_plan_get(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "enabled": bool(getattr(r, "weekly_plan_enabled", 0))}, 200


def _do_weekly_plan_set(u):
    if not _principal(u):
        return _forbidden("Only the account owner can turn the weekly plan on.")
    from models import update_restaurant
    from client_api import log_account_event
    b = _body()
    if "enabled" not in b:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), {"weekly_plan_enabled": 1 if b["enabled"] else 0})
    log_account_event(_rid(u), "weekly_plan_changed", current_user=u, detail="on" if b["enabled"] else "off")
    return _do_weekly_plan_get(u)


SEND_DELAY_CHOICES = (0, 5, 15)


def _do_send_delay_get(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "minutes": int(getattr(r, "send_delay_minutes", 0) or 0), "choices": list(SEND_DELAY_CHOICES)}, 200


def _do_send_delay_set(u):
    if not _principal(u):
        return _forbidden("Only the account owner can change this.")
    from models import update_restaurant
    from client_api import log_account_event
    try:
        minutes = int(_body().get("minutes"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "minutes must be a number"}, 400
    if minutes not in SEND_DELAY_CHOICES:
        return {"ok": False, "error": f"Pick one of {', '.join(str(c) for c in SEND_DELAY_CHOICES)} minutes."}, 400
    update_restaurant(_rid(u), {"send_delay_minutes": minutes})
    log_account_event(_rid(u), "send_delay_changed", current_user=u, detail=f"{minutes} min")
    return _do_send_delay_get(u)


def _do_trust(u):
    """Why Cavnar AI stopped asking — every earned automation and the
    record behind it, in one place (moat audit #17)."""
    from models import get_restaurant, auto_approve_trust, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN, AUTO_APPROVE_TRUST_MIN
    import ordering
    r = get_restaurant(_rid(u))
    out = {"ok": True, "auto_approve": {"enabled": bool(getattr(r, "auto_approve_earned", 0)),
                                        "min_approved": AUTO_APPROVE_TRUST_MIN,
                                        "bands": {str(k): v for k, v in auto_approve_trust(_rid(u)).items()}},
           "schedule": {"enabled": bool(getattr(r, "auto_publish_schedule", 0)),
                        "unedited_in_a_row": schedule_publish_trust(_rid(u)), "needed": SCHEDULE_PUBLISH_TRUST_MIN},
           "suppliers": [], "invoices": []}
    if _sees_food(u):
        try:
            from models import get_conn
            conn = get_conn()
            try:
                rows = conn.execute("SELECT DISTINCT supplier_name, supplier_email FROM ingredients WHERE restaurant_id=? "
                                    "AND is_active=1 AND supplier_email IS NOT NULL AND supplier_email != ''", (_rid(u),)).fetchall()
                inv = conn.execute("SELECT DISTINCT supplier FROM invoice_imports WHERE restaurant_id=? AND supplier IS NOT NULL",
                                   (_rid(u),)).fetchall()
            finally:
                conn.close()
            out["orders_enabled"] = bool(getattr(r, "auto_order_trusted", 0))
            for row in rows:
                t = ordering.supplier_trust(_rid(u), row["supplier_email"])
                out["suppliers"].append({"name": row["supplier_name"] or row["supplier_email"], **t,
                                         "needed": max(0, ordering.ORDER_TRUST_MIN - (t["orders"] - t.get("edited", 0)))})
            for row in inv:
                t = ordering.invoice_trust(_rid(u), row["supplier"])
                out["invoices"].append({"supplier": row["supplier"], **t,
                                        "needed": max(0, ordering.INVOICE_TRUST_MIN - t["full_accepts"])})
        except Exception:
            pass
    return out, 200


def _do_memory_add(u):
    """The owner adds a fact directly — the profile is theirs to write, not
    only the assistant's to keep."""
    from models import remember_ask_fact
    from client_api import log_account_event
    b = _body()
    fact = (b.get("fact") or "").strip()[:300]
    kind = (b.get("kind") or "context").strip()
    if not fact:
        return {"ok": False, "error": "Write the fact first."}, 400
    if kind not in ("goal", "context", "preference", "followup"):
        kind = "context"
    try:
        saved = remember_ask_fact(_rid(u), fact, kind=kind, source="Account", user_id=u.get("id"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    log_account_event(_rid(u), "memory_added", current_user=u, detail=fact[:120])
    return {"ok": True, "fact": saved.get("fact") if isinstance(saved, dict) else fact}, 200


def _do_decisions(u):
    import decisions
    import issues
    # Loss issues name the manager who approved the comps (re-audit A-8).
    rows = decisions.history(_rid(u), limit=40, sees_loss=issues.viewer_sees_loss(u))
    if not _sees_food(u):
        rows = [r for r in rows if not ((r.get("outcome") or {}).get("metric") or "").startswith(("food_cost", "weekly_waste"))]
    return {"ok": True, "decisions": rows}, 200


def _sees_labor(u):
    from permissions import has_permission, LABOR_VIEW
    return bool(u.get("is_admin")) or has_permission(u, LABOR_VIEW)


def _do_time_off_list(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can review time off.")
    import time_off
    rows = time_off.recent(_rid(u))
    return {"ok": True, "requests": rows, "pending": sum(1 for r in rows if r["status"] == "pending")}, 200


def _do_time_off_decide(u, request_id):
    # Deciding time off changes who can be scheduled — the same power as
    # deciding a shift request, not a view-only labor login's (SCHED-22).
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not decide time off.")
    import time_off
    from client_api import log_account_event
    b = _body()
    decision = (b.get("decision") or "").strip().lower()
    if decision not in ("approve", "deny"):
        return {"ok": False, "error": "decision must be approve or deny."}, 400
    row = time_off.decide(_rid(u), request_id, decision == "approve", decided_by=u.get("id"), note=b.get("note"))
    if not row:
        return {"ok": False, "error": "That request was already answered, or is not yours."}, 404
    from time_utils import mdy_range
    # The account log is owner-facing: M/D/YY, not the stored ISO (A-25).
    log_account_event(_rid(u), "time_off_decided", current_user=u,
                      detail=f"{row['employee_name']} {mdy_range(row['start_date'], row['end_date'])}: {row['status']}")
    out = {"ok": True, "request": row}
    if row["status"] == "approved":
        # A week staff already have may put them on those days: name each
        # shift so the manager covers it (SCHED-22 / MOD-EMP-8).
        try:
            conflicts = time_off.published_conflicts(_rid(u), row["employee_name"], row["start_date"], row["end_date"])
        except Exception:
            conflicts = []
        out["conflicts"] = conflicts
        if conflicts:
            from time_utils import mdy
            shifts = ", ".join(f"{(c.get('day') or '')[:3]} {mdy(c['date'])} {c['shift_start']}".strip() for c in conflicts[:4])
            out["warning"] = (f"{row['employee_name']} is still on the published schedule for {shifts}"
                              + (f" and {len(conflicts) - 4} more" if len(conflicts) > 4 else "")
                              + " — cover or move those shifts.")
    return out, 200


def _do_covers_get(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see covers.")
    import covers
    return {"ok": True, "days": covers.recent(_rid(u))}, 200


def _do_covers_save(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can enter covers.")
    import covers
    from client_api import log_account_event
    b = _body()
    rows = b.get("rows")
    if not isinstance(rows, list):
        rows = covers.parse_csv(b.get("csv") or "")
    if not rows:
        return {"ok": False, "error": "Send rows of date and covers, or a CSV with those two columns."}, 400
    out = covers.save(_rid(u), rows, source="manual")
    if out["written"]:
        log_account_event(_rid(u), "covers_imported", current_user=u, detail=f"{out['written']} days")
    return {"ok": True, **out}, 200


# ── Scheduling: the roster, per-person facts, pairings, dated signals, rules,
#    versions, requests. One body each, served to web and iOS alike. ────────

def _may_rate(u):
    from permissions import has_permission, TEAM_RATE
    return bool(u.get("is_admin")) or has_permission(u, TEAM_RATE)


def _may_draft(u):
    from permissions import has_permission, SCHEDULE_DRAFT
    return bool(u.get("is_admin")) or has_permission(u, SCHEDULE_DRAFT)


def _who(u):
    return (u.get("username") or u.get("email") or "").strip()[:120] or None


def _do_roster_get(u):
    """Everyone on the roster, active and deactivated, with the facts the
    schedule respects about them and their Operational Score."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see the roster.")
    import staff_settings as _ss
    from models import get_operational_scores, get_leader_flags
    rid = _rid(u)
    scores = get_operational_scores(rid)
    closers = get_leader_flags(rid)
    rel = {}
    try:
        rel = _ss.reliability(rid)
    except Exception:
        rel = {}
    out = []
    for e in _ss.roster(rid, include_inactive=True):
        out.append({**e, "score": scores.get(e["name"]), "can_close": bool(closers.get(e["name"])),
                    "reliability": rel.get(e["name"])})
    suggested = []
    try:
        import schedule_intel as _si
        suggested = _si.chemistry_suggestions_shown(rid, user_id=u.get("id"))
    except Exception:
        suggested = []
    roster_low = {e["name"].strip().lower() for e in out}
    off_roster = sorted(n for n in scores if n.strip().lower() not in roster_low) if roster_low else []
    return {"ok": True, "roster": out, "pairs": _ss.pairs(rid), "suggested_pairs": suggested,
            "ratings_off_roster": off_roster,
            "choices": {"employment_type": list(_ss.EMPLOYMENT_TYPES), "daypart": list(_ss.DAYPART_CHOICES),
                        "days": list(_ss.DAYS), "certifications": list(_ss.CERTIFICATIONS)}, "can_edit": _may_rate(u)}, 200


def _do_staff_settings_set(u):
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change their settings.")
    import staff_settings as _ss
    from client_api import log_account_event
    b = _body()
    if b.get("experienced") is not None:
        # "Experienced" changes how every shift is scored; a name nobody is
        # scheduled under would change it for no one real.
        _nm = b.get("employee_name") or b.get("name")
        _roster = {e["name"].strip().lower() for e in _ss.roster(_rid(u), include_inactive=True)}
        if not isinstance(_nm, str) or _nm.strip().lower() not in _roster:
            return {"ok": False, "error": "That name isn't on the roster."}, 400
    try:
        row = _ss.upsert(_rid(u), b.get("employee_name") or b.get("name"),
                         active=b.get("active"), employment_type=b.get("employment_type"),
                         min_hours=b.get("min_hours"), max_hours=b.get("max_hours"),
                         daypart_availability=b.get("daypart_availability"), is_minor=b.get("is_minor"),
                         time_windows=b.get("time_windows"), certifications=b.get("certifications"),
                         preferred_dayparts=b.get("preferred_dayparts"), desired_hours=b.get("desired_hours"),
                         experienced=b.get("experienced"), updated_by=_who(u))
    except _ss.StaffSettingsError as e:
        return {"ok": False, "error": str(e)}, 400
    changed = [k for k in ("active", "employment_type", "min_hours", "max_hours", "daypart_availability", "is_minor",
                           "time_windows", "certifications", "preferred_dayparts", "desired_hours",
                           "experienced") if k in b]
    log_account_event(_rid(u), "staff_settings_changed", current_user=u,
                      detail=f"{row['employee_name']}: {', '.join(changed) or 'no change'}")
    return {"ok": True, "settings": row}, 200


def _do_staff_pairs_set(u):
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change pairings.")
    import staff_settings as _ss
    b = _body()
    try:
        row = _ss.set_pair(_rid(u), b.get("a"), b.get("b"), (b.get("kind") or "").strip().lower(),
                           note=b.get("note"), created_by=_who(u))
    except _ss.StaffSettingsError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "pair": row, "pairs": _ss.pairs(_rid(u))}, 200


def _do_staff_pair_delete(u, pair_id):
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change pairings.")
    import staff_settings as _ss
    if not _ss.delete_pair(_rid(u), pair_id):
        return {"ok": False, "error": "That pairing is not yours, or is already gone."}, 404
    return {"ok": True, "pairs": _ss.pairs(_rid(u))}, 200


def _do_demand_signals_get(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import demand_signals as _ds
    from datetime import date, timedelta
    start = (request.args.get("start") or date.today().isoformat())[:10]
    end = (request.args.get("end") or (date.today() + timedelta(days=60)).isoformat())[:10]
    return {"ok": True, "signals": _ds.upcoming(_rid(u), start, end)}, 200


def _do_demand_signals_save(u):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule's inputs.")
    import demand_signals as _ds
    from client_api import log_account_event
    b = _body()
    rows = b.get("rows")
    if not isinstance(rows, list):
        rows = _ds.parse_reservations_csv(b.get("csv") or "")
    if not rows:
        return {"ok": False, "error": "Send rows (date, kind, label, covers or lift) or a CSV of date,covers."}, 400
    out = _ds.save(_rid(u), rows, source=(b.get("source") or "manual"), created_by=_who(u))
    if out["written"]:
        log_account_event(_rid(u), "demand_signals_saved", current_user=u, detail=f"{out['written']} dates")
    return {"ok": True, **out}, 200


def _do_demand_signal_delete(u, signal_id):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule's inputs.")
    import demand_signals as _ds
    if not _ds.delete(_rid(u), signal_id):
        return {"ok": False, "error": "Not found."}, 404
    return {"ok": True}, 200


def _cross_training_defaults(restaurant_id) -> dict:
    """{role: default percent} for the roles this restaurant schedules, so
    the rules screen shows what a blank field means."""
    import shift_quality as _sq
    roles = set()
    try:
        import staff_settings as _ss
        roles |= {(e.get("role") or "").strip() for e in (_ss.roster(restaurant_id) or [])}
    except Exception:
        pass
    roles.discard("")
    return {r: int(round(_sq.cross_training_target_for(r) * 100)) for r in sorted(roles)}


def _do_compliance_get(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see the rules.")
    import schedule_rules as _sr
    import compliance_packs
    import reservation_feeds
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    rules = _sr.compliance(r)
    pack = rules.pop("_pack", None)
    return {"ok": True, "rules": rules, "defaults": dict(_sr.DEFAULTS),
            "role_floors": _sr.role_floors(r), "can_edit": _principal(u),
            "jurisdiction": getattr(r, "jurisdiction", None), "pack": pack, "packs": compliance_packs.available(),
            "role_arrivals": _sr._load_json(getattr(r, "role_arrival_json", None), {}),
            "role_close_mins": _sr._load_json(getattr(r, "role_close_min_json", None), {}),
            "manager_rule_unusable": bool(rules.get("manager_on_duty")) and not _keyholders(_rid(u)),
            "role_requirements": _sr._load_json(getattr(r, "role_requirements_json", None), {}),
            "foh_roles": _sr._load_json(getattr(r, "foh_roles_json", None), []) or ["Server"],
            "patio_roles": _sr._load_json(getattr(r, "patio_roles_json", None), []),
            # Cross-training target per role, whole percents; a role left
            # out uses the default shown beside it (shift_quality).
            "role_cross_training": _sr._load_json(getattr(r, "role_cross_training_json", None), {}),
            "cross_training_defaults": _cross_training_defaults(_rid(u)),
            "cross_training_default": int(round(__import__("shift_quality").CROSS_TRAINING_DEFAULT * 100)),
            "trim_to_budget": bool(int(getattr(r, "trim_to_budget", 1) or 0)),
            "closures": _sr.closures(r),
            "certifications": list(__import__("staff_settings").CERTIFICATIONS),
            "reservation_feed": reservation_feeds.status(r), "reservation_providers": reservation_feeds.available()}, 200


def _do_compliance_set(u):
    if not _principal(u):
        return _forbidden("Only the account owner can change the scheduling rules.")
    import schedule_rules as _sr
    from client_api import log_account_event
    b = _body()
    out = {}
    if isinstance(b.get("rules"), dict):
        out["rules"] = _sr.save_compliance(_rid(u), b["rules"])
    if "role_floors" in b:
        floors = b.get("role_floors") if isinstance(b.get("role_floors"), dict) else {}
        out["role_floors"] = _sr.save_role_floors(_rid(u), floors)
    if "closed_weekdays" in b or "closed_dates" in b:
        cw = b.get("closed_weekdays") if isinstance(b.get("closed_weekdays"), list) else None
        cd = b.get("closed_dates") if isinstance(b.get("closed_dates"), list) else None
        try:
            out["closures"] = _sr.save_closures(_rid(u), closed_weekdays=cw, closed_dates=cd)
        except ValueError as e:
            return {"ok": False, "error": str(e)}, 400
    import json as _j
    from models import update_restaurant
    settings = {}
    if "jurisdiction" in b:
        import compliance_packs
        code = (b.get("jurisdiction") or "").strip().upper()
        if code and code not in compliance_packs.PACKS:
            return {"ok": False, "error": f"No rule pack for {code}."}, 400
        settings["jurisdiction"] = code or None
        out["jurisdiction"] = code or None
    if "role_arrivals" in b and isinstance(b.get("role_arrivals"), dict):
        clean = {}
        for k, v in b["role_arrivals"].items():
            try:
                clean[str(k).strip()[:60]] = max(-240, min(240, int(v)))
            except (TypeError, ValueError):
                continue
        settings["role_arrival_json"] = _j.dumps(clean) if clean else None
        out["role_arrivals"] = clean
    if "role_close_mins" in b and isinstance(b.get("role_close_mins"), dict):
        clean = {}
        for k, v in b["role_close_mins"].items():
            try:
                clean[str(k).strip()[:60]] = max(0, min(240, int(v)))
            except (TypeError, ValueError):
                continue
        settings["role_close_min_json"] = _j.dumps(clean) if clean else None
        out["role_close_mins"] = clean
    if "role_requirements" in b and isinstance(b.get("role_requirements"), dict):
        clean = {str(k).strip()[:60]: sorted({str(x).strip().lower()[:40] for x in (v or []) if str(x).strip()})
                 for k, v in b["role_requirements"].items() if str(k).strip()}
        clean = {k: v for k, v in clean.items() if v}
        settings["role_requirements_json"] = _j.dumps(clean) if clean else None
        out["role_requirements"] = clean
    for key, col in (("foh_roles", "foh_roles_json"), ("patio_roles", "patio_roles_json")):
        if key in b and isinstance(b.get(key), list):
            clean = sorted({str(x).strip()[:60] for x in b[key] if str(x).strip()})
            settings[col] = _j.dumps(clean) if clean else None
            out[key] = clean
    if "role_cross_training" in b and isinstance(b.get("role_cross_training"), dict):
        clean = {}
        for k, v in b["role_cross_training"].items():
            if not str(k).strip() or v is None or v == "":
                continue
            try:
                clean[str(k).strip()[:60]] = int(max(0, min(100, round(float(v)))))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"The cross-training target for {str(k)[:60]} must be a percent from 0 to 100."}, 400
        settings["role_cross_training_json"] = _j.dumps(clean) if clean else None
        out["role_cross_training"] = clean
    if "trim_to_budget" in b:
        settings["trim_to_budget"] = 1 if b.get("trim_to_budget") else 0
        out["trim_to_budget"] = bool(b.get("trim_to_budget"))
    if "reservation_provider" in b or "reservation_api_key" in b:
        import reservation_feeds
        prov = (b.get("reservation_provider") or "").strip().lower()
        if prov and prov not in reservation_feeds.PROVIDERS:
            return {"ok": False, "error": f"{prov} is not a reservation system this build knows."}, 400
        settings["reservation_provider"] = prov or None
        if "reservation_api_key" in b:
            settings["reservation_api_key"] = (b.get("reservation_api_key") or "").strip()[:200] or None
        out["reservation_provider"] = prov or None
    if settings:
        update_restaurant(_rid(u), settings)
        if "reservation_provider" in settings:
            import reservation_feeds
            from models import get_restaurant
            out["reservation_feed"] = reservation_feeds.status(get_restaurant(_rid(u)))
    if not out:
        return {"ok": False, "error": "Send rules, role_floors, or a setting."}, 400
    log_account_event(_rid(u), "schedule_rules_changed", current_user=u, detail=", ".join(out))
    return {"ok": True, **out}, 200


def _do_schedule_versions(u, history_id):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see schedules.")
    import schedule_versions as _sv
    return {"ok": True, "versions": _sv.list_versions(_rid(u), history_id),
            "draft_vs_published": _sv.draft_vs_published(_rid(u), history_id)}, 200


def _rows_from_body(b):
    """The rows in a request, or None when there are none worth reading. A
    row whose date is not YYYY-MM-DD cannot be judged by any rule and made
    the rule load fail with a 500; such a body is refused (None) instead."""
    from datetime import datetime as _dt
    cols = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")
    raw = b.get("rows")
    if not isinstance(raw, list) or not raw or len(raw) > 2000:
        return None
    rows = [{c: str(r.get(c) or "")[:200] for c in cols} for r in raw if isinstance(r, dict)]
    for r in rows:
        try:
            _dt.strptime(r["date"].strip(), "%Y-%m-%d")
        except ValueError:
            return None
    return rows or None


def _do_schedule_violations(u):
    """Every rule the rows on screen break — for the review panel after
    an edit, so the owner never publishes on a stale verdict."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can check a schedule.")
    import schedule_rules as _sr
    from schedule_engine import quality_inputs_from_db
    b = _body()
    rows = _rows_from_body(b)
    if not rows:
        return {"ok": False, "error": "rows required"}, 400
    inputs = quality_inputs_from_db(_rid(u), week_rows=rows)
    c = inputs.get("constraints")
    if c is None:
        return {"ok": True, "violations": [], "review": _sr.summarize([])}, 200
    if inputs.get("roster"):
        c.active = {n.lower() for n in inputs["roster"]}
        c.roster_names = list(inputs["roster"])
    viols = _sr.violations(rows, c)
    out = {"ok": True, "violations": viols, "review": _sr.summarize(viols),
           "pending_time_off": inputs.get("pending_time_off") or {}}
    # Who the rows on screen push into overtime, with a same-role person who
    # has room and what moving the shift saves — the one-tap move (#27).
    try:
        import schedule_learning as _sl
        from models import get_role_rates as _grr, get_restaurant as _gr
        out["overtime_moves"] = [f for f in _sl.price_overtime_moves(
            _sl.overtime_forecast(rows, constraints=c), _grr(_rid(u)),
            getattr(_gr(_rid(u)), "hourly_rate", None) or None) if f.get("candidate")]
        import rec_ledger as _rl
        for f in out["overtime_moves"]:
            f["rec_key"] = _rl.rec_key("overtime_move", f"{f['employee']}:{f['candidate'].get('date')}")
        _rl.present_many(_rid(u), [dict(key=f["rec_key"], module="schedule", kind="overtime_move", title=f["text"][:160],
                                        dollar_value=f["candidate"].get("saves"), cavnar_completes=True)
                                   for f in out["overtime_moves"]], "schedule_review", user_id=u.get("id"))
    except Exception as e:
        print(f"[schedule] overtime moves unavailable: {e}")
        out["overtime_moves"] = []
    # What the edit moves in hours and overtime-priced dollars, when the
    # page sends the rows it started from.
    base = _rows_from_body({"rows": b.get("baseline_rows")}) if isinstance(b.get("baseline_rows"), list) else None
    if base is not None:
        try:
            import schedule_economics as _econ
            from models import get_role_rates
            rates = get_role_rates(_rid(u))
            out["cost"] = _econ.cost_delta(base, rows, rates, (rates or {}).get("_default"),
                                           ceiling=c.compliance.get("weekly_hours_ceiling") or 40)
        except Exception:
            out["cost"] = None
    return out, 200


def _do_schedule_apply_fixes(u):
    """Put somebody legal on every row that breaks a hard rule, re-score,
    and hand the rows back — the owner still decides whether to save."""
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule.")
    import schedule_rules as _sr
    import shift_quality as _sq
    from schedule_engine import quality_inputs_from_db, _quality_signals, _score_schedule_quality
    rows = _rows_from_body(_body())
    if not rows:
        return {"ok": False, "error": "rows required"}, 400
    inputs = quality_inputs_from_db(_rid(u), week_rows=rows)
    c = inputs.get("constraints")
    if c is None:
        return {"ok": False, "error": "The rules could not be loaded for this week."}, 500
    if inputs.get("roster"):
        c.active = {n.lower() for n in inputs["roster"]}
        c.roster_names = list(inputs["roster"])
    viols = _sr.violations(rows, c)
    signals, weights = _quality_signals(_rid(u), inputs)
    out = _sq.apply_fixes(rows, _sr.fixable(viols), profiles=inputs.get("shift_profiles") or None,
                          rule_constraints=c,
                          weights=weights, **signals)
    fixed_rows = out["rows"]
    after = _sr.violations(fixed_rows, c)
    quality, what_if = _score_schedule_quality(_rid(u), fixed_rows, inputs)
    from schedule_engine import present_quality
    present_quality(_rid(u), quality, user_id=u.get("id"))
    return {"ok": True, "rows": fixed_rows, "fixes": out["fixes"], "unfixed": out["unfixed"],
            "violations": after, "review": _sr.summarize(after), "quality": quality, "what_if": what_if}, 200


def _do_schedule_optimize(u):
    """Run the Shift Quality repair loop over the week on screen and hand
    back the improved rows with every change and why. Nothing is saved: the
    owner decides, exactly as with apply-fixes."""
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule.")
    import schedule_optimizer as _opt
    from schedule_engine import quality_inputs_from_db, _quality_signals, _score_schedule_quality
    body = _body()
    rows = _rows_from_body(body)
    if not rows:
        return {"ok": False, "error": "rows required"}, 400
    targets = body.get("daily_target_hours") if isinstance(body.get("daily_target_hours"), dict) else None
    try:
        targets = {str(k): float(v) for k, v in (targets or {}).items()} or None
    except (TypeError, ValueError):
        return {"ok": False, "error": "daily_target_hours must map dates to hours."}, 400
    # The week's own hours budget, from the saved draft when the client names
    # it: without it the search had no ceiling and could add hours past the
    # labor budget the generation respected.
    budget = None
    hid = body.get("history_id")
    if hid is not None:
        try:
            hid = int(hid)
        except (TypeError, ValueError):
            return {"ok": False, "error": "history_id must be a number."}, 400
        from models import get_conn as _gc_opt
        _c = _gc_opt()
        try:
            _h = _c.execute("SELECT hours_budget FROM schedule_history WHERE id=? AND restaurant_id=?",
                            (hid, _rid(u))).fetchone()
        finally:
            _c.close()
        if _h is None:
            return {"ok": False, "error": "That week is gone — reload the schedule."}, 404
        budget = float(_h["hours_budget"] or 0) or None
        if not targets:
            from schedule_engine import stored_daily_targets
            targets = stored_daily_targets(_rid(u), hid) or None
    elif body.get("hours_budget") not in (None, ""):
        try:
            budget = float(body.get("hours_budget"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "hours_budget must be a number."}, 400
    inputs = quality_inputs_from_db(_rid(u), daily_target_hours=targets, week_rows=rows)
    c = inputs.get("constraints")
    if c is None:
        return {"ok": False, "error": "The rules could not be loaded for this week."}, 500
    if inputs.get("roster"):
        c.active = {n.lower() for n in inputs["roster"]}
        c.roster_names = list(inputs["roster"])
    signals, weights = _quality_signals(_rid(u), inputs)
    from models import get_restaurant as _gr_opt
    _r_opt = _gr_opt(_rid(u))
    res = _opt.optimize(rows, inputs, signals=signals, weights=weights, constraints=c,
                        max_seconds=12.0, max_evaluations=600,
                        max_server_overlap=getattr(_r_opt, "section_count", None),
                        hours_budget=(budget if int(getattr(_r_opt, "trim_to_budget", 1) or 0) else None))
    quality, what_if = _score_schedule_quality(_rid(u), res["rows"], inputs)
    from schedule_engine import present_quality
    present_quality(_rid(u), quality, user_id=u.get("id"))
    summary = _opt.summary(res, signals)
    # A proposal with changes is a recommendation: kept on Save, set aside
    # on Discard (the page reports which to /recs/event).
    rec_key = None
    if summary.get("changes"):
        import rec_ledger as _rl
        from datetime import datetime as _dt
        rec_key = _rl.rec_key("optimizer", f"{hid or 'draft'}:{_dt.utcnow().strftime('%Y%m%d%H%M%S')}")
        _rl.present(_rid(u), rec_key, "schedule", "schedule_review", kind="optimizer",
                    title=f"{len(summary['changes'])} changes from Improve with Cavnar", user_id=u.get("id"),
                    cavnar_completes=True)
    return {"ok": True, "rows": res["rows"], "optimizer": summary, "rec_key": rec_key,
            "quality": quality, "what_if": what_if}, 200


def _do_calibration_apply(u):
    """Apply the suggested Shift Quality weights (schedule_learning.
    calibrate_weights) — the owner's decision, one tap, recorded and
    reversible (the previous weights are kept in the change record)."""
    if not _principal(u):
        return _forbidden("Only the account owner can change how schedules are scored.")
    import json as _j
    import schedule_learning as _sl
    from models import update_restaurant, get_quality_weights, record_capability_change
    cal = _sl.calibrate_weights(_rid(u))
    if not cal.get("ready") or not cal.get("suggested_weights"):
        return {"ok": False, "error": cal.get("reason") or "There is no suggestion to apply yet."}, 400
    before = get_quality_weights(_rid(u)) or {}
    after = dict(before)
    after.update({k: float(v) for k, v in cal["suggested_weights"].items()})
    update_restaurant(_rid(u), {"quality_weights_json": _j.dumps(after)})
    record_capability_change(_rid(u), "quality_weights_applied", subject="weights", before=_j.dumps(before),
                             after=_j.dumps(after), changed_by=_who(u))
    import rec_ledger as _rl
    _rl.record(_rid(u), "calibration:weights", "accepted", surface="labor", user_id=u.get("id"), role=u.get("role"))
    return {"ok": True, "weights": after}, 200


# The metric a module's recommendation is measured against when the owner
# taps Track and the recommendation carries no metric of its own — the
# module's natural number, in the order to try. The first one this
# restaurant can actually measure is used; with none, no tracker starts and
# the answer says so (M-8). Intel has no metric of its own, and neither has
# Marketing: a post has no honest metric (outcomes.OBSERVED_ACTIONS says so
# of post_published), and "marketing": ("sales",) started a sales tracker
# that read every unrelated thing that moved sales as the post working —
# and credited it to Labor (rec-ROI #11). Track on either answers "nothing
# to measure it against yet". A marketing recommendation that DOES carry a
# metric (a slow-day text aimed at one weekday's sales) is measured on it.
REC_TRACK_METRICS = {
    "reviews": ("avg_rating",),
    "food": ("food_cost_pct", "weekly_waste"),
    "labor": ("labor_pct",),
}


def _rec_title(rid, key):
    """The words the recommendation was shown with (rec_instances.title)."""
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute("SELECT title FROM rec_instances WHERE restaurant_id=? AND key=? "
                               "ORDER BY created_at DESC, rowid DESC LIMIT 1", (rid, key)).fetchone()
        finally:
            conn.close()
        return (row["title"] if row and row["title"] else None)
    except Exception as e:
        print(f"[recs] title lookup failed for {rid} {key}: {e}")
        return None


def _start_rec_tracker(rid, key, module, user_id, event="accepted", body_metric=None):
    """A real before-and-after tracker (outcomes.start) for an answered
    recommendation (rec-ROI #18). Returns (result, metric description):
    result is {"tracker": ...} when one started, {"tracker_refused": ...}
    when one could not, or None when nothing was attempted.

    Which metric: the one the recommendation CARRIES (a DSR action's kind,
    #39 — authoritative, so "reorder" measures nothing; else the metric it
    was presented with). Track ("accepted") with none falls back to the
    module's natural number (REC_TRACK_METRICS); Done ("completed") with
    none starts nothing — Done never promised a measurement.

    Automatic, so the family gate (#18): nothing starts while anything in
    the metric's family is already being measured, and the reply says what
    is and until when."""
    import metrics
    import outcomes
    carried, authoritative = outcomes.metric_for_rec(rid, key, body_metric=body_metric)
    if carried:
        candidates = (carried,)
    elif authoritative or event != "accepted":
        return ({"tracker_refused": outcomes.no_metric_reply()} if event == "accepted" else None), None
    else:
        candidates = REC_TRACK_METRICS.get(module or "", ())
    if not candidates:
        return {"tracker_refused": outcomes.no_metric_reply()}, None
    from datetime import date as _date, timedelta as _td
    unreadable = None
    for metric in candidates:
        try:
            got = metrics.trailing(rid, metric, end=(_date.today() - _td(days=1)).isoformat())
        except Exception as e:
            print(f"[recs] {metric} not measurable for {rid}: {e}")
            continue
        if got["value"] is None:
            unreadable = unreadable or (metric, got.get("detail"))
            continue
        title = _rec_title(rid, key) or key
        try:
            res = outcomes.start(rid, "recommendation", key, title, metric, user_id=user_id,
                                 module=module, gate="family")
        except Exception as e:
            print(f"[recs] tracker failed for {rid} {key}: {e}")
            return None, None
        return res, metrics.describe(metric)
    if unreadable:
        return {"tracker_refused": outcomes.not_measurable_reply(*unreadable)}, None
    return {"tracker_refused": outcomes.no_metric_reply()}, None


def _do_rec_event(u):
    """An owner's response to any recommendation, from any client: opened,
    evidence viewed, accepted, dismissed (hide / not_for_us / done),
    snoozed, completed. The one door into rec_ledger for web and iOS.

    What each answer does, and the sentence the client shows for it (M-8,
    H-10) — both clients show `message` rather than their own promise:
      completed  — "Done": silenced for SILENCE_DAYS["done"] (it said "won't
                   suggest it again" and came back after 14 days);
      dismissed  — "Not for us": silenced; the module's insight prompt is
                   told not to suggest the same thing in other words;
      accepted   — "Track": a real outcomes tracker on the module's metric
                   when one can be measured, quiet for its window; otherwise
                   no tracker, and the message says it is only hidden.

    `reason_code` (rec_ledger.REASON_CODES: already_doing, doesnt_fit,
    too_costly, bad_timing, dont_trust_data, other) and an optional free
    `reason` ride on the answer's meta; an unknown code is a 400. Events
    the server writes where they happen (implemented, superseded, checkin,
    abandoned, outcome, expired, shown) are refused."""
    import rec_ledger as _rl
    from datetime import datetime as _dt, timedelta as _td
    b = _body()
    key = b.get("key")
    event = b.get("event")
    # implemented / superseded / checkin / abandoned / outcome / expired /
    # shown are written where they happen, never posted by a client.
    if not isinstance(key, str) or not key.strip() or event not in _rl.EVENTS or event in _rl.SERVER_ONLY_EVENTS:
        return {"ok": False, "error": "key and a response are required"}, 400
    code = b.get("reason_code")
    if code not in (None, "") and code not in _rl.REASON_CODES:
        return {"ok": False, "error": "reason_code must be one of " + ", ".join(_rl.REASON_CODES)}, 400
    surface = b.get("surface") if b.get("surface") in _rl.SURFACES else "unknown"
    meta = {}
    silence = None
    until = None
    if code:
        meta["reason_code"] = code
    if event in ("dismissed", "snoozed"):
        reason = b.get("reason")
        if isinstance(reason, str) and reason.strip():
            meta["reason"] = reason.strip()[:200]
    if event == "dismissed":
        kind = b.get("kind") if b.get("kind") in _rl.SILENCE_DAYS else "hide"
        meta["kind"] = kind
    if event == "snoozed":
        try:
            days = max(1, min(30, int(b.get("days") or 1)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "days must be a number"}, 400
        until = (_dt.utcnow() + _td(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(b.get("module"), str):
        meta["module"] = b["module"][:20]
    module = meta.get("module") or (surface if surface in REC_TRACK_METRICS else None)
    message = None
    tracking = None
    started = None
    if event in ("completed", "accepted"):
        # Accept/Done on a recommendation that carries a metric starts its
        # tracker (rec-ROI #18, #39), under the family gate. `body_metric`
        # lets a client name the metric a line was shown with.
        started, info = _start_rec_tracker(_rid(u), key.strip(), module, u.get("id"), event=event,
                                           body_metric=b.get("metric") if isinstance(b.get("metric"), str) else None)
    tracker = (started or {}).get("tracker")
    refused = (started or {}).get("tracker_refused")
    if tracker:
        meta["tracker_id"] = tracker.get("id")
    if event == "completed":
        silence = _rl.SILENCE_DAYS["done"]
        message = "Done \u2014 Cavnar won\u2019t suggest it again"
        if tracker:
            message += f". Now {tracker['label_text']}"
        elif refused and refused.get("code") == "in_flight":
            message += f". {refused['reason']}"
    elif event == "dismissed" and meta.get("kind") == "not_for_us":
        message = "Noted \u2014 it won\u2019t come back"
    elif event == "accepted":
        if tracker:
            window = int(tracker.get("window_days") or info["default_window_days"])
            # Not re-asked while its outcome is being measured.
            silence = max(_rl.ACCEPTED_QUIET_DAYS, window)
            tracking = {"metric": tracker["metric"], "label": tracker["label"], "window_days": window,
                        "evaluate_on": tracker.get("evaluate_on")}
            message = (f"Tracking \u2014 Cavnar will compare {tracker['label'].lower()} over the next "
                       f"{window} days with the {window} before")
        elif refused and refused.get("code") == "in_flight":
            message = (f"Noted \u2014 hidden for {_rl.ACCEPTED_QUIET_DAYS} days. {refused['reason']}")
        else:
            message = (f"Noted \u2014 hidden for {_rl.ACCEPTED_QUIET_DAYS} days. There is nothing "
                       "here Cavnar can measure it against yet")
    ok = _rl.record(_rid(u), key.strip(), event, surface=surface, user_id=u.get("id"), role=u.get("role"),
                    meta=meta or None, silence_days=silence, snooze_until=until)
    out = {"ok": True, "recorded": ok}
    if message:
        out["message"] = message
    if tracking:
        out["tracking"] = tracking
    if tracker:
        out["tracker"] = tracker
    elif refused:
        out["tracker_refused"] = refused
    return out, 200


# ── the owner's own recommendation record (rec_learning) ────────────────────
#
# What they followed, what it did, and the timeline — restaurant-scoped and
# redacted per viewer (a manager never sees a loss, a food-cost or an
# owner-only recommendation: rec_learning.viewer_sees). Contracts in
# API_REFERENCE.md → "Recommendation record".

def _do_recs_summary(u):
    import rec_learning
    raw = request.args.get("days") or "30"
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = None
    if days not in rec_learning.SUMMARY_WINDOWS:
        return {"ok": False, "error": "days must be 30, 90 or 180"}, 400
    return rec_learning.summary(_rid(u), days=days, viewer=u), 200


def _do_recs_timeline(u):
    import rec_learning
    raw = request.args.get("limit")
    try:
        limit = int(raw) if raw not in (None, "") else 30
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit must be a number"}, 400
    before = request.args.get("before") or None
    if before and rec_learning._cursor(before) is None:
        return {"ok": False, "error": "before must be a timestamp or a next_before cursor"}, 400
    return rec_learning.timeline(_rid(u), limit=limit, before=before, viewer=u), 200


def _do_recs_what_worked(u):
    """GET /recs/what-worked?days=90|180 — "What worked for you" in
    sentences, built without a model from the ledger summary, the stored
    results and the measured days (owner_report.what_worked, rec-ROI #28).
    Redacted for this login exactly as /recs/summary is; any other `days`
    is a 400."""
    import owner_report
    raw = request.args.get("days") or str(owner_report.DEFAULT_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = None
    if days not in owner_report.WINDOWS:
        return {"ok": False, "error": "days must be 90 or 180"}, 400
    return owner_report.what_worked(_rid(u), days=days, viewer=u), 200


def _do_recs_checkin(u):
    """"Did you make this change? Did anything else change?" — one answer
    per tap, recorded as a `checkin` on the recommendation's latest episode
    (rec_ledger.checkin documents the meta the outcome evaluation reads).
    {key, did_it: yes|no|partly, conditions_changed: bool, note?}"""
    import rec_ledger as _rl
    import rec_learning
    b = _body()
    key = b.get("key")
    did_it = b.get("did_it")
    changed = b.get("conditions_changed", False)
    note = b.get("note")
    if not isinstance(key, str) or not key.strip():
        return {"ok": False, "error": "key is required"}, 400
    if did_it not in _rl.CHECKIN_ANSWERS:
        return {"ok": False, "error": "did_it must be yes, no or partly"}, 400
    if not isinstance(changed, bool):
        return {"ok": False, "error": "conditions_changed must be true or false"}, 400
    if note is not None and not isinstance(note, str):
        return {"ok": False, "error": "note must be text"}, 400
    ep = rec_learning.episode_for(_rid(u), key.strip())
    # Another restaurant's key, or one this login may not see, is simply not
    # found — its existence is not confirmed either way.
    if ep is None or not rec_learning.viewer_sees(u, ep):
        return {"ok": False, "error": "No such recommendation."}, 404
    surface = b.get("surface") if b.get("surface") in _rl.SURFACES else "unknown"
    out = _rl.checkin(_rid(u), key.strip(), did_it, conditions_changed=changed, note=note, user_id=u.get("id"),
                      role=u.get("role"), surface=surface)
    if out is None:
        return {"ok": False, "error": "No such recommendation."}, 404
    if out.get("tracker_id"):
        # The answer changes the result it is about: "no" stops it counting,
        # "conditions changed" caps its grade (outcomes.apply_checkin).
        try:
            import outcomes
            outcomes.apply_checkin(_rid(u), out["tracker_id"], did_it, changed)
        except Exception as e:
            import ops
            ops.capture(e, job="outcome_checkin", context=f"restaurant_id={_rid(u)}")
    try:
        import home_brief as _hb
        _hb.invalidate(_rid(u))
    except Exception as e:
        print(f"[recs] home cache not cleared after check-in: {e}")
    return {"ok": True, "recorded": out["recorded"],
            "checkin": {k: out[k] for k in ("did_it", "conditions_changed", "note", "tracker_id", "attribution")}}, 200


def _do_standby_ask(u):
    """One tap from the draft's standby line: ask the named person, by
    email, whether they can be on call that day. Asked once per person and
    day — a second tap says so instead of sending again."""
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change the schedule.")
    import re as _re
    import rec_ledger as _rl
    import shift_requests as _sreq
    from time_utils import mdy as _mdy
    b = _body()
    day = b.get("date") if isinstance(b.get("date"), str) else ""
    who = (b.get("employee") or "").strip() if isinstance(b.get("employee"), str) else ""
    if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not who:
        return {"ok": False, "error": "date and employee are required"}, 400
    import models as _m
    book = _sreq._contacts(_rid(u), _m.DB_PATH)
    contact = book.get(who.lower())
    if contact is None:
        return {"ok": False, "error": f"{who} isn't on the roster."}, 404
    if not contact.get("email"):
        return {"ok": False, "error": f"There's no email on file for {who} — ask them directly."}, 409
    key = _rl.rec_key("standby", f"{day}:{who.lower()}")
    ref = f"ask:{day}:{who.lower()}"
    # "Already asked" means an ask that went out. The acceptance is written
    # only after the email is sent: written first, a failed send left it in
    # place and every retry said "already asked" and sent nothing.
    if _rl.recorded(_rid(u), key, "accepted", ref):
        return {"ok": True, "sent": 0, "already": True, "message": f"{who} was already asked about {_mdy(day)}."}, 200
    from datetime import date as _date
    wd = _date.fromisoformat(day).strftime("%A")
    when = f"{wd} {_mdy(day)}"
    shift = "–".join(x for x in (b.get("shift_start"), b.get("shift_end")) if isinstance(x, str) and x)
    shift = f" ({shift})" if shift else ""
    lines = [f"Could you be on call {when}{shift}?",
             "You're not on the schedule that day. If somebody can't make it, your manager may call you in.",
             "Reply to your manager to say yes or no."]
    sent = _sreq._email_staff(_rid(u), [contact.get("employee_name") or who], "Could you be on call?", lines,
                              _m.DB_PATH)
    if not sent:
        return {"ok": False, "error": f"The email to {who} didn't go out — try again or ask them directly."}, 502
    _rl.record(_rid(u), key, "accepted", surface="schedule_review", user_id=u.get("id"), role=u.get("role"),
               meta={"module": "schedule"}, source_ref=ref)
    return {"ok": True, "sent": sent, "message": f"Asked {who} to be on call {when}."}, 200


def _do_shift_requests_list(u):
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see shift requests.")
    import shift_requests as _sq_req
    return {"ok": True, "requests": _sq_req.for_manager(_rid(u)), "open": _sq_req.open_shifts(_rid(u))}, 200


def _do_shift_request_decide(u, request_id):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not decide shift requests.")
    import shift_requests as _sq_req
    from client_api import log_account_event
    b = _body()
    decision = (b.get("decision") or "").strip().lower()
    if decision not in ("approve", "deny"):
        return {"ok": False, "error": "decision must be approve or deny."}, 400
    try:
        row = _sq_req.decide(_rid(u), request_id, decision == "approve", decided_by=_who(u),
                             replacement=(b.get("replacement") or "").strip() or None)
    except _sq_req.ShiftRequestError as e:
        return {"ok": False, "error": str(e)}, 400
    if not row:
        return {"ok": False, "error": "That request was already answered, or is not yours."}, 404
    log_account_event(_rid(u), "shift_request_decided", current_user=u,
                      detail=f"{row['employee_name']} {row['date']} {row['shift_start']}: {row['status']}")
    return {"ok": True, "request": row}, 200


def _keyholders(rid) -> bool:
    """Whether anybody can satisfy a manager-on-duty rule here."""
    try:
        import schedule_rules as _sr
        c = _sr.build_constraints(rid, [], [])
        return bool(c.keyholders)
    except Exception:
        return False


def _do_learned_patterns(u):
    """What the draft has learned from the manager's edits, with the
    owner's say: each one can be dismissed or restored."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import schedule_versions as _sv
    import schedule_intel as _si
    rid = _rid(u)
    dismissed = _si.dismissed_patterns(rid)
    out = []
    for p in _sv.learned_patterns(rid, min_repeats=1):
        key = _si.pattern_key(p)
        out.append({**p, "key": key, "active": p["times"] >= 2 and key not in dismissed, "dismissed": key in dismissed})
    return {"ok": True, "patterns": out, "can_edit": _may_draft(u)}, 200


def _do_learned_pattern_set(u):
    if not _may_draft(u):
        return _forbidden("Your login can view labor but not change what the draft learns.")
    import schedule_intel as _si
    b = _body()
    key = (b.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "key required"}, 400
    if b.get("dismissed", True):
        _si.dismiss_pattern(_rid(u), key, actor=_who(u))
    else:
        _si.restore_pattern(_rid(u), key)
    return {"ok": True, "key": key, "dismissed": bool(b.get("dismissed", True))}, 200


def _do_recommendation_event(u):
    """The owner accepted or dismissed a recommendation — the ledger that
    decides which kinds keep being shown."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can do this.")
    import schedule_intel as _si
    import rec_ledger as _rl
    b = _body()
    action = (b.get("action") or "").strip().lower()
    if action not in ("accepted", "dismissed", "restored"):
        return {"ok": False, "error": "action is accepted, dismissed or restored"}, 400
    kind = (b.get("kind") or "other")[:60] if isinstance(b.get("kind"), str) else "other"
    text = b.get("key") if isinstance(b.get("key"), str) else ""
    _si.record_recommendation(_rid(u), kind, text, action, actor=_who(u))
    # The same answer in the one trail every surface reads: "Not for us"
    # keeps this recommendation off the draft from now on, on any device.
    rkey = _si.schedule_rec_key(kind, text)
    started = None
    if action == "accepted":
        _rl.record(_rid(u), rkey, "accepted", surface="schedule_review", user_id=u.get("id"), role=u.get("role"))
        # An accepted "Trim about Nh…" is measured like Home's trim_day: the
        # labor % after against before (outcomes). The other kinds are read
        # against what the night itself recorded (schedule_intel.
        # measure_accepted_recommendations, Mondays). One tracker per metric
        # (rec-ROI #3): a second trim while labor % is measured is answered.
        if kind == "hours":
            try:
                import outcomes as _oc
                started = _oc.start(_rid(u), "schedule", rkey, text[:160] or "Trim the schedule", "labor_pct",
                                    user_id=u.get("id"), module="labor", gate="metric")
            except Exception as _ox:
                print(f"[schedule] could not start the outcome tracker: {_ox}")
    elif action == "dismissed":
        _rl.record(_rid(u), rkey, "dismissed", surface="schedule_review", user_id=u.get("id"), role=u.get("role"),
                   meta={"kind": "not_for_us"})
    out = {"ok": True, "suppressed_kinds": sorted(_si.suppressed_kinds(_rid(u)))}
    if started:
        out.update({k: started[k] for k in ("tracker", "tracker_refused") if k in started})
    return out, 200



# When the drafts have been going out nearly untouched, auto-publish is
# worth offering (the owner still has the undo window). Three published
# weeks in a row, each with at most this many edits and at least this share
# of rows untouched, and the latest draft at or above the quality bar.
AUTO_PUBLISH_WEEKS = 3
AUTO_PUBLISH_MAX_CHANGES = 3
AUTO_PUBLISH_MIN_UNCHANGED = 0.95
AUTO_PUBLISH_MIN_SCORE = 85


def _auto_publish_offer(rid) -> dict:
    """Offer auto-publish only when it would actually run: the offer and the
    Friday job read ONE rule (models.schedule_publish_trust — consecutive
    recent drafts sent unedited with no coverage or no-show issue) plus the
    latest draft's quality. The offer used to accept light edits while the
    job stopped at any edit, so an owner who accepted it got "armed: false"
    and nothing ever published."""
    import json as _json
    from models import get_restaurant, get_conn, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN
    r = get_restaurant(rid)
    if r is not None and int(getattr(r, "auto_publish_schedule", 0) or 0):
        return {"eligible": False, "reason": "Auto-publish is already on."}
    trust = schedule_publish_trust(rid)
    # A week only counts as having run clean when something was watching it
    # (schedule_intel.watched_dates); say what is missing instead of a
    # "0 of 3" that no amount of good weeks would move.
    import schedule_intel as _si_ap
    missing = _si_ap.coverage_watch_missing(rid)
    if trust < SCHEDULE_PUBLISH_TRUST_MIN and missing:
        return {"eligible": False, "trust": trust, "needed": SCHEDULE_PUBLISH_TRUST_MIN, "missing": missing,
                "reason": ("Auto-publish needs proof that published weeks ran clean, and Cavnar can't watch "
                           "a shift yet: " + "; ".join(missing) + ".")}
    if trust < SCHEDULE_PUBLISH_TRUST_MIN:
        return {"eligible": False, "trust": trust, "needed": SCHEDULE_PUBLISH_TRUST_MIN,
                "reason": (f"{trust} of the {SCHEDULE_PUBLISH_TRUST_MIN} drafts in a row it needs went out unedited "
                           "with no coverage or no-show issue.")}
    conn = get_conn()
    try:
        latest = conn.execute("SELECT quality_json FROM schedule_history WHERE restaurant_id=? AND quality_json IS NOT NULL "
                              "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
    finally:
        conn.close()
    try:
        score = (_json.loads(latest["quality_json"]) or {}).get("score") if latest else None
    except Exception:
        score = None
    if score is None or score < AUTO_PUBLISH_MIN_SCORE:
        return {"eligible": False, "reason": f"The latest draft scored {score}, under {AUTO_PUBLISH_MIN_SCORE}."}
    return {"eligible": True, "score": score, "trust": trust,
            "reason": (f"Your last {trust} drafts went out unedited with no coverage or no-show issue, and the latest "
                       f"scored {score}. Auto-publish can send next week's on Friday, with time to undo.")}


def _do_ratings_unmatched(u):
    """Operational Scores stored under a name that is not on the roster
    judge nobody: a rating for "Kim Tran" and a roster "Kim T." never meet
    (Gia Mia: 17 ratings, none matched). Each is listed with the roster name
    it most likely means."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import staff_settings as _ss
    from models import get_operational_scores
    # Deactivated people are still the people their ratings belong to: left
    # out, a former employee's rating was offered to whoever shared a first
    # name and moved onto them.
    everyone = _ss.roster(_rid(u), include_inactive=True)
    active = [e["name"] for e in everyone if e.get("active", True)]
    known = {e["name"].strip().lower() for e in everyone}
    scores = {n: v for n, v in (get_operational_scores(_rid(u)) or {}).items() if n.strip().lower() not in known}
    return {"ok": True, "unmatched": rating_name_suggestions(scores, active)}, 200


def rating_name_suggestions(scores: dict, roster: list) -> list:
    """[{rated, score, suggestion, candidates}] for every rated name not on
    the roster. A suggestion is offered only when one roster name clearly
    fits: same first name and matching last initial, or a close spelling."""
    import difflib
    on = {n.strip().lower() for n in roster}
    out = []
    for rated, sc in sorted((scores or {}).items()):
        if rated.strip().lower() in on:
            continue
        parts = rated.strip().split()
        first = parts[0].lower() if parts else ""
        last_initial = parts[-1][0].lower() if len(parts) > 1 and parts[-1] else ""
        by_initial = [n for n in roster if n.strip().split() and n.strip().split()[0].lower() == first
                      and (not last_initial or (len(n.strip().split()) > 1 and n.strip().split()[-1][:1].lower() == last_initial))]
        close = difflib.get_close_matches(rated, roster, n=3, cutoff=0.75)
        candidates = list(dict.fromkeys(by_initial + close))
        suggestion = candidates[0] if len(by_initial) == 1 or (not by_initial and len(close) == 1) else None
        out.append({"rated": rated, "score": sc, "suggestion": suggestion, "candidates": candidates[:4]})
    return out


def _do_ratings_match(u):
    """Move every rating stored under one name onto a roster name."""
    if not _may_rate(u):
        return _forbidden("Your login can view the team but not change ratings.")
    import staff_settings as _ss
    from models import rename_capability_holder
    from client_api import log_account_event
    b = _body()
    if not isinstance(b.get("rated"), str) or not isinstance(b.get("roster_name"), str):
        return {"ok": False, "error": "Pick a name from the roster."}, 400
    rated, target = b["rated"].strip(), b["roster_name"].strip()
    everyone = _ss.roster(_rid(u), include_inactive=True)
    roster = {e["name"] for e in everyone if e.get("active", True)}
    if not rated or target not in roster:
        return {"ok": False, "error": "Pick a name from the roster."}, 400
    if rated.lower() != target.lower() and rated.lower() in {e["name"].strip().lower() for e in everyone}:
        return {"ok": False, "error": f"{rated} is on your roster (deactivated) — their rating stays theirs."}, 409
    moved = rename_capability_holder(_rid(u), rated, target)
    if moved is None:
        return {"ok": False, "error": f"{target} already has a rating. Remove one of them first."}, 409
    log_account_event(_rid(u), "rating_matched", current_user=u, detail=f"{rated} → {target}")
    return {"ok": True, "moved": moved}, 200


def _roster_roles(rid) -> dict:
    """{name: role} for the active roster — who a rotation plans for."""
    import staff_settings as _staff
    return {e["name"]: e.get("role") or "" for e in _staff.roster(rid) or [] if e.get("name")}


def _starting_points(rid) -> dict:
    """The borrowed starting headcount (intelligence.staffing), shaped for
    the screens — or why there is none."""
    from intelligence import staffing as _staffing
    return _staffing.payload(_staffing.starting_headcount(rid, roster_roles=_roster_roles(rid)))


def _present_calibration(u, cal):
    """A ready weight suggestion is shown on this screen with its own Apply:
    presented under "calibration:weights" (the key the Apply records as
    accepted — a bookkeeping prefix, so it stays out of acceptance figures)
    with `rec_key` and `answerable` (false: Apply is its answer, not Done /
    Not for us). Never fails the route."""
    try:
        if isinstance(cal, dict) and cal.get("ready") and cal.get("suggested_weights"):
            import rec_ledger
            import rec_delivery
            rec_ledger.present(_rid(u), "calibration:weights", "schedule", "labor",
                               title="Suggested Shift Quality weights", user_id=u.get("id"))
            cal = dict(cal, rec_key="calibration:weights", answerable=rec_delivery.answerable("calibration:weights"))
    except Exception as e:
        print(f"[schedule] calibration not presented rid={_rid(u)}: {e}")
    return cal


def _do_schedule_intel(u):
    """The record behind the draft: outcomes by daypart, the rotation
    ledger, what staff keep dropping and claiming, who could hold a
    station, and pairs the record suggests."""
    if not _sees_labor(u):
        return _forbidden("Only someone who can see labor can see this.")
    import schedule_intel as _si
    import schedule_economics as _econ
    import schedule_learning as _sl
    import schedule_versions as _sv
    rid = _rid(u)
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default
    mentored = _safe(lambda: _si.mentoring(rid), {})
    return {"ok": True,
            "outcomes": _safe(lambda: _si.outcomes_by_daypart(rid), {}),
            "ledger": _safe(lambda: _si.fairness_ledger(rid), {}),
            "behaviour": _safe(lambda: _si.behaviour_preferences(rid), {}),
            "could_hold": _si.could_hold(mentored), "mentored": mentored,
            "suggested_pairs": _safe(lambda: _si.chemistry_suggestions_shown(rid, user_id=u.get("id")), []),
            "splh": _safe(lambda: _econ.splh_by_daypart(rid), {}),
            "revenue": _safe(lambda: _econ.projected_weekly_revenue(rid), {}),
            # What the engine is learning: how much of each draft survives to
            # the published week, how the quality dimensions tracked real
            # outcomes (suggested weights, never applied), and no-show rates
            # by weekday.
            "draft_acceptance": _safe(lambda: _sv.acceptance(rid), {"available": False}),
            "weight_calibration": _present_calibration(u, _safe(lambda: _sl.calibrate_weights(rid), {"ready": False})),
            # Schedule learning (#42, #46, #48, #43): how well the edit
            # predictor would have done on past drafts, the multi-week
            # rotation, the sales-per-labor-hour targets, and — for a
            # restaurant with no history — a borrowed starting headcount or
            # why there is none.
            "edit_prediction": _safe(lambda: _sl.edit_prediction_summary(rid), {"ready": False}),
            "rotation": _safe(lambda: _si.rotation_plan(rid, roster_roles=_roster_roles(rid)), {}),
            "splh_objective": _safe(lambda: _econ.splh_objective(rid), {"available": False}),
            "starting_points": _safe(lambda: _starting_points(rid), {"available": False}),
            "attendance_by_weekday": _safe(lambda: _sl.attendance_by_weekday(rid), {}),
            "auto_publish_offer": (_safe(lambda: _auto_publish_offer(rid), {"eligible": False}) if _may_publish(u)
                                   else {"eligible": False, "reason": "Only someone who can send the schedule can turn this on."}),
            "suppressed_recommendation_kinds": sorted(_safe(lambda: _si.suppressed_kinds(rid), set()))}, 200


def _do_reservation_sync(u):
    if not _principal(u):
        return _forbidden("Only the account owner can sync the reservation feed.")
    import reservation_feeds
    res = reservation_feeds.sync(_rid(u))
    return {"ok": not res.get("error"), **res}, (200 if not res.get("error") else 400)


def _do_recipe_scan(u):
    if not _sees_food(u):
        return _forbidden("Only someone who can see food cost can add recipes.")
    import recipes
    f = request.files.get("file")
    if not f:
        return {"ok": False, "error": "Attach a photo of the recipe card."}, 400
    if _limited(u, "recipe_scan", 10, 600):
        return _SLOW_DOWN
    data = f.read()
    media_type = (f.mimetype or "").lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    try:
        draft = recipes.extract_from_image(_rid(u), data, media_type, user_id=u.get("id"))
    except (recipes.RecipePhotoError, ValueError) as e:
        return {"ok": False, "error": str(e)}, 400
    except Exception as e:
        from ai_utils import AIBudgetExceeded
        if isinstance(e, AIBudgetExceeded):
            return {"ok": False, "error": str(e)}, 429
        import ops
        ops.capture(e, job="recipe_scan", context=f"restaurant_id={_rid(u)}")
        return {"ok": False, "error": "The card couldn't be read right now. Try again shortly."}, 502
    return {"ok": True, "draft": draft}, 200


def _do_post_tags(u, post_id):
    """Correct what a post was about. Only a menu item of this restaurant,
    only a known occasion or kind; anything else is refused, not guessed."""
    import marketing_tags
    from models import get_conn
    b = _body()
    overrides = {}
    if "menu_item_id" in b:
        try:
            overrides["menu_item_id"] = int(b["menu_item_id"]) if b["menu_item_id"] not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            return {"ok": False, "error": "menu_item_id must be a number."}, 400
    if "occasion" in b:
        occ = (b.get("occasion") or "").strip().lower() or None
        if occ and occ not in marketing_tags.OCCASIONS:
            return {"ok": False, "error": f"occasion must be one of {', '.join(marketing_tags.OCCASIONS)}."}, 400
        overrides["occasion"] = occ
    if "post_kind" in b:
        kind = (b.get("post_kind") or "").strip().lower() or None
        if kind and kind not in marketing_tags.KINDS:
            return {"ok": False, "error": f"post_kind must be one of {', '.join(marketing_tags.KINDS)}."}, 400
        overrides["post_kind"] = kind
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, topic FROM marketing_content_log WHERE id=? AND restaurant_id=?", (int(post_id), _rid(u))).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "Post not found."}, 404
    # An explicit "no item" must clear inference too, so it is passed as a
    # sentinel the tagger treats as set.
    tags = marketing_tags.tag_row(row["id"], _rid(u), row["topic"], overrides=overrides, clear_item=("menu_item_id" in overrides and overrides["menu_item_id"] is None))
    tags["label"] = marketing_tags.label(tags)
    return {"ok": True, "tags": tags}, 200


def _do_ai_visibility_queries(u):
    from models import ai_visibility_query_history
    return {"ok": True, **ai_visibility_query_history(_rid(u))}, 200


def _do_marketing_diagnosis(u):
    import guest_marketing
    return {"ok": True, "diagnosis": guest_marketing.diagnose(_rid(u))}, 200


def _do_memory_list(u):
    """What Ask Cavnar remembers about this restaurant, with who added it —
    so the owner can read and correct the memory that shapes every answer."""
    from models import get_ask_memory
    return {"ok": True, "facts": get_ask_memory(_rid(u))}, 200


def _do_memory_forget(u):
    from models import forget_ask_fact
    from client_api import log_account_event
    fact = (_body().get("fact") or "").strip()
    if not fact:
        return {"ok": False, "error": "Which fact?"}, 400
    ok = forget_ask_fact(_rid(u), fact)
    if ok:
        log_account_event(_rid(u), "memory_forgotten", current_user=u, detail=fact[:120])
    return ({"ok": True} if ok else {"ok": False, "error": "No fact like that."}), (200 if ok else 404)


def _do_delayed_pending(u):
    import delayed
    return {"ok": True, "actions": delayed.pending(_rid(u))}, 200


def _do_delayed_cancel(u, action_id):
    import delayed
    from client_api import log_account_event
    ok = delayed.cancel(_rid(u), int(action_id), actor=u)
    if ok:
        log_account_event(_rid(u), "delayed_action_cancelled", current_user=u, detail=f"#{action_id}")
    return ({"ok": True} if ok else {"ok": False, "error": "That already went out, or was already undone."}), (200 if ok else 409)


# ── principal-only ────────────────────────────────────────────────────────────

def _loss_viewer(u):
    from permissions import has_permission, LOSS_VIEW
    return has_permission(u, LOSS_VIEW)


def _do_loss_signals(u):
    if not _loss_viewer(u):
        return _forbidden("Comps & voids are visible to the owner, or to a manager the owner has "
                          "given access.")
    import loss_detection
    sig = loss_detection.signals(_rid(u))
    _present_loss_flags(u, sig)
    return {"ok": True, **sig}, 200


def _present_loss_flags(u, sig):
    """A comp/void flag is a recommendation — review these tickets — and
    only this login's view (LOSS_VIEW, checked by the caller) ever sees it:
    presented on "home", the surface both Homes show it on (#41), with
    `rec_key` / `answerable` on each flag, and a flag the owner already
    answered (or whose issue was resolved — the same key) left out of
    `flagged`. Never fails the route."""
    try:
        import rec_delivery
        import rec_ledger
        flagged = [f for f in (sig or {}).get("flagged") or [] if f.get("key")]
        for f in flagged:
            f["rec_key"] = f["key"]
            f["answerable"] = rec_delivery.answerable(f["key"])
        ids = rec_ledger.present_many(_rid(u), [{"key": f["key"], "module": "ops", "title": f.get("headline"),
                                                 "position": i} for i, f in enumerate(flagged)],
                                      "home", user_id=u.get("id")) if flagged else {}
        if ids:
            sig["flagged"] = [f for f in sig.get("flagged") or []
                              if not (f.get("key") in ids and ids[f["key"]] is None)]
    except Exception as e:
        print(f"[loss] presentation failed rid={_rid(u)}: {e}")


def _do_cross_module(u):
    """What two modules saw that neither could see alone.

    `business_intelligence.correlations()` has existed since audit #15 and
    had exactly one consumer — `executive_brief`, which had no route. So the
    single finding this platform can produce that no single-module tool can
    ("your Friday complaints fall on your leanest Friday") reached an owner
    only as one compressed line in the morning brief, or if the Ask model
    happened to choose the tool. This is that surface.

    Returns [] often and deliberately. `correlations` cannot manufacture a
    link — both sides must already have cleared their own module's floor —
    so an empty list means the modules genuinely do not agree on anything,
    which is the common and correct outcome.
    """
    import business_intelligence as bi
    from models import get_restaurant
    from ask_cavnar_tools import viewer_restaurant
    # Built from what THIS login may see, the same way the brief is. A
    # manager without FOOD_COST_VIEW must not read a margin link.
    r = viewer_restaurant(get_restaurant(_rid(u)), u)
    brief = bi.executive_brief(_rid(u), restaurant=r)
    # Every card that carries a question carries the question to ask. The
    # web phrased this in JS and iOS had no affordance at all; one string
    # from here means both surfaces ask Cavnar the same thing.
    links = [dict(l, ask=f"Tell me more about this: {l.get('headline', '')}")
             for l in (brief.get("links") or [])]
    fix_first, links = _present_cross_module(u, brief.get("fix_first"), links)
    return {"ok": True,
            "links": links,
            "fix_first": fix_first,
            "modules_consulted": brief.get("modules_consulted") or [],
            "modules_off": brief.get("modules_off") or [],
            "unanswered": brief.get("unanswered") or []}, 200


def _present_cross_module(u, fix_first, links):
    """Home's "one thing" and its "What connects" links, into rec_ledger on
    the surface that shows them (#26): their keys existed (the one thing's
    own key, business_intelligence.link_key) and this route — the one both
    Homes read — never presented them, so the page that leads with them was
    the one place they were never counted.

    Each carries the contract fields: `rec_key`, and `answerable` — true
    where Done / Not for us / Track apply (rec_delivery.answerable; the owed
    replies and the money ranking are not). A link the owner has already
    answered is left out, as every other surface leaves an answered
    recommendation out; the one thing is already filtered
    (pick_one_thing). Returns (fix_first, links). Never fails the route."""
    import business_intelligence as bi
    import rec_delivery
    try:
        ff = dict(fix_first) if fix_first else None
        items, seen = [], set()
        if ff and ff.get("key"):
            ff["rec_key"] = ff["key"]
            ff["answerable"] = rec_delivery.answerable(ff["key"])
            items.append({"key": ff["key"], "module": "home", "title": ff.get("what"), "position": 0,
                          "dollar_value": ff.get("dollars_monthly"),
                          "evidence_sources": ff.get("modules") or None,
                          "cross_module": len(ff.get("modules") or []) > 1})
            seen.add(ff["key"])
        out_links = []
        for l in links or []:
            key = bi.link_key(l)
            l = dict(l, rec_key=key, answerable=rec_delivery.answerable(key))
            out_links.append(l)
            if key not in seen:
                seen.add(key)
                items.append({"key": key, "module": "home", "title": l.get("headline"), "position": len(items),
                              "evidence_sources": l.get("modules") or None, "cross_module": True})
        items = rec_delivery.only_presentable(items)
        ids = {}
        if items:
            import rec_ledger
            ids = rec_ledger.present_many(_rid(u), items, "home", user_id=u.get("id")) or {}
        out_links = [l for l in out_links if not (l["rec_key"] in ids and ids[l["rec_key"]] is None)]
        if ff and ff.get("key") in ids and ids[ff["key"]] is None:
            ff = None           # answered between the pick and the showing
        return ff, out_links
    except Exception as e:
        print(f"[cross-module] presentation failed rid={_rid(u)}: {e}")
        return fix_first, links


def _do_ask_feedback(u):
    """POST /ask-cavnar/feedback {message_id | turn_id, helpful: bool,
    note?} — "Was this useful?" on one Ask answer (ROI audit #48). The
    answer must be this restaurant's and this login's (models.
    record_ask_feedback refuses anything else with a 404, never a write);
    rating it again replaces the rating. Returns the rating and the
    restaurant's running tally."""
    b = _body()
    raw = b.get("message_id", b.get("turn_id"))
    if isinstance(raw, bool):
        raw = None
    try:
        mid = int(raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Which answer? Send its message_id."}, 400
    helpful = b.get("helpful")
    if not isinstance(helpful, bool):
        return {"ok": False, "error": "helpful must be true or false"}, 400
    note = b.get("note")
    if note is not None and not isinstance(note, str):
        return {"ok": False, "error": "note must be text"}, 400
    from models import record_ask_feedback, ask_feedback_summary
    row = record_ask_feedback(_rid(u), mid, helpful, note, user_id=u.get("id"))
    if row is None:
        return {"ok": False, "error": "That answer isn't in your Ask history."}, 404
    try:
        import ask_cavnar
        ask_cavnar.invalidate_context(_rid(u))
    except Exception as e:
        print(f"[ask] context not refreshed after feedback rid={_rid(u)}: {e}")
    return {"ok": True, "feedback": row, "summary": ask_feedback_summary(_rid(u))}, 200


def _do_good_news(u):
    """Records, streaks and complaints that stopped.

    The counterpart to /loss-signals: everything else in this product looks
    for trouble. Filtered by the same rule as goals and outcomes — a manager
    without FOOD_COST_VIEW is not congratulated on a margin record.
    """
    import good_news
    from ask_cavnar_tools import viewer_restaurant
    from models import get_restaurant
    # Every module this login may not see, not just food cost. An earlier
    # version denied {"inventory"} alone, which left a manager without the
    # Labor permission reading sales and labor-percentage records — the
    # same class of leak the ROI audit found in /api/value, where the
    # breakdown was filtered and the headline total was not.
    #
    # viewer_restaurant is the single answer to "what may this login see",
    # and it fails closed: a role lookup that errors hides everything.
    view = viewer_restaurant(get_restaurant(_rid(u)), u)
    denied = set(getattr(view, "_ask_denied", frozenset()))
    items = good_news.all_good_news(_rid(u), today=_local_today(u),
                                    restaurant=view, denied_modules=denied)
    items = [dict(i, ask=f"What's behind this: {i.get('headline', '')}") for i in items]
    return {"ok": True, "items": items, "caveat": good_news.CAVEAT}, 200


# ── self-serve pause ──────────────────────────────────────────────────────────
# The only path off the product was an email to Will asking to cancel. A
# pause is the smaller decision an owner can make for themselves: Stripe
# collection stops (pause_collection, behaviour "void" — nothing is invoiced
# and nothing accrues), the account moves to the product's existing "paused"
# state, and Stripe resumes collection on the date chosen. Data keeps
# flowing while paused — reviews are fetched on reviews_live, not billing —
# so the day they come back the brief is current, not a month stale.

PAUSE_DAYS = (14, 30, 60)


def _stripe_client():
    import os
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    import stripe as _stripe
    import config as _config
    _stripe = _config.stripe_api(key)
    return _stripe


def _active_subscription(stripe_mod, customer_id):
    for status in ("active", "trialing", "past_due"):
        subs = stripe_mod.Subscription.list(customer=customer_id, status=status, limit=3)
        if subs.data:
            return subs.data[0]
    return None


def _do_pause(u):
    if not _principal(u):
        return _forbidden("Only the account owner can pause the subscription.")
    from datetime import timedelta
    from models import get_restaurant, update_restaurant
    from client_api import log_account_event
    b = _body()
    try:
        days = int(b.get("days") or 30)
    except (TypeError, ValueError):
        return {"ok": False, "error": "days must be a whole number"}, 400
    if days not in PAUSE_DAYS:
        return {"ok": False, "error": f"Pick {', '.join(str(d) for d in PAUSE_DAYS[:-1])} or {PAUSE_DAYS[-1]} days."}, 400
    rid = _rid(u)
    r = get_restaurant(rid)
    if not r:
        return {"ok": False, "error": "Restaurant not found"}, 404
    if (r.billing_status or "").lower() == "paused":
        return {"ok": False, "error": "Already paused."}, 409
    # In the restaurant's own clock, so "paused until 10/20" is their
    # 10/20 — and Stripe resumes at that local moment, not a UTC one.
    from time_utils import restaurant_now
    resumes = restaurant_now(r) + timedelta(days=days)
    try:
        stripe_mod = _stripe_client()
    except Exception as e:
        # A key without the library (or the reverse) must not pause the
        # product while Stripe keeps collecting.
        import ops
        ops.capture(e, job="pause_subscription", context=f"restaurant_id={rid}")
        return {"ok": False, "error": "Stripe isn't reachable from this server — nothing changed. Reply to Will."}, 502
    if stripe_mod and r.stripe_customer_id:
        try:
            sub = _active_subscription(stripe_mod, r.stripe_customer_id)
            if not sub:
                return {"ok": False, "error": "No active subscription to pause — reply to Will."}, 409
            stripe_mod.Subscription.modify(
                sub.id, pause_collection={"behavior": "void", "resumes_at": int(resumes.timestamp())})
        except Exception as e:
            import ops
            ops.capture(e, job="pause_subscription", context=f"restaurant_id={rid}")
            return {"ok": False, "error": "Stripe did not accept the pause — nothing changed. Reply to Will."}, 502
    else:
        # No Stripe on this account (a trial, or a manually billed client):
        # the product pauses; there is no collection to stop.
        pass
    # Every location billed together pauses together, the way the webhook
    # moves siblings — one owner, one subscription, one state.
    try:
        from webhook_routes import _sibling_restaurant_ids
        rids = _sibling_restaurant_ids(rid)
    except Exception:
        rids = [rid]
    for _r in rids:
        update_restaurant(_r, {"billing_status": "paused", "paused_until": resumes.date().isoformat()})
    log_account_event(rid, "subscription_paused", current_user=u, detail=f"{days} days, resumes {resumes.date().isoformat()}")
    try:
        import emails as _emails
        _emails.deliver(email_type="pause_notice_ops", restaurant_id=rid, payload={
            "from": _emails.sender("ops"), "to": [_emails._from_email()],
            "subject": f"Paused: {r.name} — {days} days, resumes {resumes.date().isoformat()}",
            "preheader": "A client paused their own subscription.",
            "html": _emails._branded_email(f"<p>{r.name} paused for {days} days from the app. Stripe resumes "
                                           f"collection on {resumes.date().isoformat()}.</p>")})
    except Exception as e:
        import ops
        ops.capture(e, job="pause_notice", context=f"restaurant_id={rid}")
    return {"ok": True, "paused_until": resumes.date().isoformat(), "days": days}, 200


def _do_resume(u):
    if not _principal(u):
        return _forbidden("Only the account owner can resume the subscription.")
    from models import get_restaurant, update_restaurant
    from client_api import log_account_event
    rid = _rid(u)
    r = get_restaurant(rid)
    if not r or (r.billing_status or "").lower() != "paused":
        return {"ok": False, "error": "Not paused."}, 409
    try:
        stripe_mod = _stripe_client()
    except Exception as e:
        import ops
        ops.capture(e, job="resume_subscription", context=f"restaurant_id={rid}")
        return {"ok": False, "error": "Stripe isn't reachable from this server — reply to Will."}, 502
    if stripe_mod and r.stripe_customer_id:
        try:
            sub = _active_subscription(stripe_mod, r.stripe_customer_id)
            if sub:
                stripe_mod.Subscription.modify(sub.id, pause_collection="")
        except Exception as e:
            import ops
            ops.capture(e, job="resume_subscription", context=f"restaurant_id={rid}")
            return {"ok": False, "error": "Stripe did not accept the resume — reply to Will."}, 502
    try:
        from webhook_routes import _sibling_restaurant_ids
        rids = _sibling_restaurant_ids(rid)
    except Exception:
        rids = [rid]
    for _r in rids:
        update_restaurant(_r, {"billing_status": "active", "paused_until": None})
    log_account_event(rid, "subscription_resumed", current_user=u)
    return {"ok": True}, 200


def _do_pause_status(u):
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    return {"ok": True, "paused": (getattr(r, "billing_status", "") or "").lower() == "paused",
            "paused_until": getattr(r, "paused_until", None), "days": list(PAUSE_DAYS),
            "can_pause": _principal(u)}, 200


def _do_activity(u):
    """What Cavnar AI has been doing for this restaurant — the activity feed
    (activity.py). Filtered by what this login may see, the same way the
    brief and good news are."""
    import activity
    from ask_cavnar_tools import viewer_restaurant
    from models import get_restaurant
    view = viewer_restaurant(get_restaurant(_rid(u)), u)
    denied = set(getattr(view, "_ask_denied", frozenset()))
    return activity.feed(_rid(u), restaurant=view, denied=denied), 200


def _do_monthly_review(u):
    """Last month, read the way an owner reads a P&L — the same build the
    monthly email sends on the 1st, so the screen and the email agree.
    Metrics this login may not see are left out the way goals are."""
    import monthly_review
    from models import get_restaurant
    rid = _rid(u)
    review = monthly_review.build(rid, today=_local_today(u), restaurant=get_restaurant(rid))
    review["metrics"] = [m for m in review["metrics"] if _metric_visible(u, m.get("key"))]
    review["results"] = [r for r in (review.get("results") or []) if _metric_visible(u, r.get("metric"))]
    review["goals"] = [g for g in (review.get("goals") or []) if _metric_visible(u, g.get("metric"))]
    # The priorities, the one thing and prime cost carry food-cost dollars
    # too; a login that cannot see food cost does not get them here either.
    if not _sees_food(u):
        review["priorities"] = [p for p in (review.get("priorities") or [])
                                if (p.get("key") or "") != "money:food_cost"]
        ff = review.get("fix_first") or {}
        if "food_cost" in (ff.get("modules") or []):
            review["fix_first"] = None
        review["prime_cost"] = None
    for m in review["metrics"]:
        m["ask"] = f"What moved my {m['label'].lower()} in {review['month']}?"
    return {"ok": True, "review": review, "headline": monthly_review.headline(review),
            "ask": f"Walk me through {review['month']}",
            "yoy": {m["key"]: monthly_review.yoy_clause(m) for m in review["metrics"]}}, 200


def _do_milestones(u):
    """Moments worth marking, newest first, plus anything not yet shown.

    `unseen` drives the one-time in-app moment; the client marks it seen so
    the promise that it appears once is kept across devices — which the
    localStorage flag it replaces could not do.
    """
    import milestones
    # A savings milestone's body carries whole-business measured dollars,
    # which include food-cost results — so it is owner-level for the same
    # reason /api/value's promise block is. A goal milestone names the
    # metric it was set on, which may be one this login cannot see.
    #
    # An anniversary and "every review answered" carry neither, so a manager
    # still gets the moments that are genuinely theirs rather than an empty
    # screen. Filtering by kind rather than blocking the route is the
    # difference between a permission boundary and a wall.
    money_kinds = {"savings", "goal"}
    allowed = None if _principal(u) else (lambda m: m.get("kind") not in money_kinds)

    def _visible(rows):
        return rows if allowed is None else [m for m in rows if allowed(m)]

    return {"ok": True,
            "items": _visible(milestones.recent(_rid(u), limit=10)),
            "unseen": _visible(milestones.recent(_rid(u), limit=3, unseen_only=True))}, 200


def _do_milestone_seen(u):
    import milestones
    key = (_body().get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "Which milestone?"}, 400
    return {"ok": True, "marked": milestones.mark_seen(_rid(u), key)}, 200


def _do_morning_brief(u):
    """This login's own brief — built from what THEY may see, the same way
    it is delivered to them. The send settings are the restaurant's, so only
    the owner may change them (can_edit)."""
    import morning_brief
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    brief = morning_brief.build(_rid(u), restaurant=r, today=_local_today(u), viewer=u)
    # K6: Home's "Before service" card renders these lines, so a load that
    # names itself a Home view (?view=home, web and iOS Home) records them on
    # "home" — once a day per key. Every other reader (the settings screens)
    # records nothing: they show no lines.
    if request.args.get("view") == "home":
        morning_brief.present_on_home(_rid(u), brief, user_id=u.get("id"))
    return {"ok": True,
            "brief": brief,
            "settings": {"enabled": bool(getattr(r, "morning_brief_enabled", 1)),
                         "hour": int(getattr(r, "morning_brief_hour", 7) or 7),
                         "hold_alerts": bool(getattr(r, "alert_hold_during_service", 1)),
                         "briefing_level": getattr(r, "briefing_level", None) or "normal",
                         "preshift_nudge_hour": int(getattr(r, "preshift_nudge_hour", 0) or 0)},
            "can_edit": _principal(u)}, 200


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
    if "hold_alerts" in b:
        fields["alert_hold_during_service"] = 1 if b["hold_alerts"] else 0
    if "briefing_level" in b:
        level = str(b["briefing_level"] or "").lower()
        if level not in ("calm", "normal", "all"):
            return {"ok": False, "error": "briefing_level must be calm, normal or all"}, 400
        fields["briefing_level"] = level
    if "preshift_nudge_hour" in b:
        try:
            nudge = int(b["preshift_nudge_hour"] or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "preshift_nudge_hour must be a whole number"}, 400
        if nudge and not (12 <= nudge <= 20):
            return {"ok": False, "error": "Pick an hour between noon and 8pm, or off."}, 400
        fields["preshift_nudge_hour"] = nudge
    if not fields:
        return {"ok": False, "error": "Nothing to change."}, 400
    update_restaurant(_rid(u), fields)
    return {"ok": True, **fields}, 200


# ── registration: every body on both blueprints ───────────────────────────────

def _idempotent(body, route):
    """Answer a repeated request (same restaurant, route and Idempotency-Key)
    from the first one's result. The key is derived from the uploaded file
    on iOS, so tapping Scan again after a timeout no longer pays for a
    second model read of the same invoice (CLIENT-21). A request still in
    flight is answered 409 rather than started twice; a failure (5xx) is
    not remembered, so a real retry runs."""
    import json as _json

    def wrapped(u, **kw):
        key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
        if not key:
            return body(u, **kw)
        rid = _rid(u)
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute("DELETE FROM idempotent_responses WHERE created_at < datetime('now','-7 days')")
            row = conn.execute("SELECT status, payload_json, created_at >= datetime('now','-10 minutes') AS fresh "
                               "FROM idempotent_responses WHERE restaurant_id=? AND route=? AND idem_key=?",
                               (rid, route, key)).fetchone()
            if row and row["payload_json"] is not None:
                conn.commit()
                return _json.loads(row["payload_json"]), int(row["status"] or 200)
            if row and row["fresh"]:
                conn.commit()
                return {"ok": False, "in_progress": True,
                        "error": "That scan is still being read — check back in a moment."}, 409
            conn.execute("INSERT OR REPLACE INTO idempotent_responses (restaurant_id, route, idem_key) VALUES (?,?,?)",
                         (rid, route, key))
            conn.commit()
        finally:
            conn.close()
        try:
            payload, status = body(u, **kw)
        except BaseException:
            _forget(rid, route, key)
            raise
        if status >= 500:
            _forget(rid, route, key)
        else:
            conn = get_conn()
            try:
                conn.execute("UPDATE idempotent_responses SET status=?, payload_json=? "
                             "WHERE restaurant_id=? AND route=? AND idem_key=?",
                             (status, _json.dumps(payload, default=str), rid, route, key))
                conn.commit()
            finally:
                conn.close()
        return payload, status
    wrapped.__name__ = body.__name__
    return wrapped


# ── the nightly DSR (dsr/) ────────────────────────────────────────────────────
#
# Any console login may read the night's report and press Close day, as with
# the close-out it grows from: the person who closes is usually not the
# owner. WHAT they read is decided by dsr.access.view_for — the Owner DSR for
# an owner login at this location, the Manager DSR for every other console
# login — and every payload is built from the one stored snapshot. A report
# is always this session's restaurant's (_rid): a manager's session is their
# location, so no id or date in a request can reach another tenant's night.

def _dsr_view(u):
    from dsr import access
    return access.view_for(u)


def _dsr_day(raw):
    try:
        return date.fromisoformat(str(raw or "")[:10]) if len(str(raw or "")) == 10 else None
    except ValueError:
        return None


_NO_DSR = ({"ok": False, "error": "The daily report isn't part of your access."}, 403)


def _do_dsr_list(u):
    from dsr import access, store
    if _dsr_view(u) is None:
        return _NO_DSR
    try:
        limit = max(1, min(90, int(request.args.get("limit") or 30)))
    except ValueError:
        return {"ok": False, "error": "limit must be a number"}, 400
    before = _dsr_day(request.args.get("before")) if request.args.get("before") else None
    rows = store.list_reports(_rid(u), limit=limit, before=before)
    import closeout
    from models import get_restaurant
    r = get_restaurant(_rid(u))
    tonight = closeout.business_date_for(r) if r else None
    return {"ok": True, "view": _dsr_view(u), "enabled": bool(getattr(r, "dsr_enabled", 1)) if r else False,
            "tonight": tonight.isoformat() if tonight else None,
            "reports": [access.summary(row, u) for row in rows]}, 200


def _do_dsr_get(u, day):
    from dsr import access, store
    from models import get_restaurant
    if _dsr_view(u) is None:
        return _NO_DSR
    d = _dsr_day(day)
    if d is None:
        return {"ok": False, "error": "The date must be YYYY-MM-DD."}, 400
    raw = request.args.get("version")
    try:
        version = int(raw) if raw else None
    except ValueError:
        return {"ok": False, "error": "version must be a number"}, 400
    report = store.get_report(_rid(u), d, version=version)
    if not report:
        return {"ok": False, "error": "There's no report for that night yet."}, 404
    payload = access.render(report, u, restaurant=get_restaurant(_rid(u)), versions=store.versions(_rid(u), d))
    _dsr_present_view(u, d, payload)
    return {"ok": True, **payload}, 200


# A report read this many days after its night is history, not advice: its
# actions are not presented again (that would open a fresh episode for last
# month's "tomorrow"). The same window dsr.deliver announces a night within.
DSR_VIEW_PRESENT_DAYS = 3


def _dsr_present_view(u, day, payload):
    """The report view is where the actions are SEEN: present what this
    login's view shows on the "dsr" surface (the narrative no longer
    presents at generation — dsr.narrative.ledger_items), and give every
    action the contract fields: `rec_key` (its dsr_action key),
    `answerable` (Done / Not for us / Track apply) and `answered` (the owner
    already answered it — the report keeps the line, the controls go).
    Never fails the view."""
    try:
        import rec_delivery
        from dsr import deliver as _dsr_deliver
        acts = ((payload or {}).get("narrative") or {}).get("actions_tomorrow") or []
        if not acts:
            return
        recent = (_local_today(u) - day).days <= DSR_VIEW_PRESENT_DAYS
        ids = _dsr_deliver.present_shown(_rid(u), payload, "dsr", user_id=u.get("id")) if recent else {}
        silenced = set()
        if not recent:
            import rec_ledger
            silenced = rec_ledger.silenced_keys(_rid(u))
        for a in acts:
            if not isinstance(a, dict) or not a.get("key"):
                continue
            answered = (a["key"] in ids and ids[a["key"]] is None) if recent else a["key"] in silenced
            a["rec_key"] = a["key"]
            a["answered"] = bool(answered)
            a["answerable"] = rec_delivery.answerable(a["key"]) and not answered
    except Exception as e:
        print(f"[dsr] view presentation failed rid={_rid(u)}: {e}")


def _do_dsr_status(u, day):
    from dsr import access, store
    from models import get_restaurant
    from time_utils import mdy
    if _dsr_view(u) is None:
        return _NO_DSR
    d = _dsr_day(day)
    if d is None:
        return {"ok": False, "error": "The date must be YYYY-MM-DD."}, 400
    report = store.get_report(_rid(u), d)
    if not report:
        # Nothing started yet is an answer the progressive screen shows, not an error.
        return {"ok": True, "view": _dsr_view(u), "exists": False, "business_date": d.isoformat(),
                "label": mdy(d), "status": None}, 200
    return {"ok": True, "view": _dsr_view(u), "exists": True,
            **access.checklist(report, u, restaurant=get_restaurant(_rid(u)))}, 200


DSR_OWNER_DAYS = 7      # an owner may run or re-run any of the last seven business dates


def _do_dsr_close(u):
    """Close day, now: the night runs on a background thread and the app
    follows /dsr/<date>/status. A manager: tonight's business date, or the
    one before (a close pressed after the late-close window rolled over).
    An owner: any of the last DSR_OWNER_DAYS business dates — the "generate
    one if the automation failed" path. `rerun` re-runs a finished night as
    a new version — the owner's call only."""
    from datetime import timedelta
    from dsr import pipeline
    from models import get_restaurant
    from time_utils import mdy
    if _dsr_view(u) is None:
        return _NO_DSR
    r = get_restaurant(_rid(u))
    if not r or not getattr(r, "dsr_enabled", 1):
        return {"ok": False, "error": "The daily report is switched off for this location."}, 409
    import closeout
    from dsr import access
    today = closeout.business_date_for(r)
    body = _body()
    raw = body.get("date")
    d = _dsr_day(raw) if raw else today
    rerun = bool(body.get("rerun"))
    owner = _dsr_view(u) == access.OWNER
    if rerun and not owner:
        return {"ok": False, "error": "Only the owner can re-run a finished night."}, 403
    earliest = today - timedelta(days=(DSR_OWNER_DAYS - 1) if owner else 1)
    if d is None or not (earliest <= d <= today):
        if owner:
            return {"ok": False, "error": f"Only the last {DSR_OWNER_DAYS} nights ({mdy(earliest)} – {mdy(today)}) "
                                          "can be run here."}, 400
        return {"ok": False, "error": f"Only tonight ({mdy(today)}) or the night before can be closed here."}, 400
    if _limited(u, "dsr_close", 6, 600):
        return _SLOW_DOWN
    out = pipeline.start_manual(r, d, rerun=rerun)
    return {"ok": True, "business_date": d.isoformat(), "label": mdy(d), **out}, (202 if out.get("started") else 200)


def _do_dsr_week(u):
    """Erik's weekly grid: the restaurant's week holding ?date= (default:
    the latest report's night, else today), with period to date."""
    from dsr import access, rollup
    from models import get_restaurant
    if _dsr_view(u) is None:
        return _NO_DSR
    d = _dsr_grid_day(u)
    if d is None:
        return {"ok": False, "error": "The date must be YYYY-MM-DD."}, 400
    r = get_restaurant(_rid(u))
    return {"ok": True, "view": _dsr_view(u), "week": access.redact_grid(rollup.week(r, d), u)}, 200


def _do_dsr_period(u):
    from dsr import access, rollup
    from models import get_restaurant
    if _dsr_view(u) is None:
        return _NO_DSR
    d = _dsr_grid_day(u)
    if d is None:
        return {"ok": False, "error": "The date must be YYYY-MM-DD."}, 400
    grid = rollup.period(get_restaurant(_rid(u)), d)
    if grid is None:
        return {"ok": False, "error": "Set your fiscal calendar to see periods."}, 409
    return {"ok": True, "view": _dsr_view(u), "period": access.redact_grid(grid, u)}, 200


def _do_dsr_history_import(u):
    """Last Year from the owner's old DSR workbooks (dsr.history_import):
    multipart field "file", .xlsx or .csv. Owner view only — it writes the
    figures every Last Year column reads. Always this session's restaurant."""
    import os
    from dsr import access, history_import, store
    if _dsr_view(u) != access.OWNER:
        return {"ok": False, "error": "Only the owner can import last year's reports."}, 403
    f = request.files.get("file")
    if not f or not f.filename:
        return {"ok": False, "error": "Attach a .xlsx or .csv file.", "imported": 0, "skipped": 0, "errors": []}, 400
    if _limited(u, "dsr_history_import", 10, 600):
        return _SLOW_DOWN
    data = f.read(history_import.MAX_BYTES + 1)
    name = os.path.basename(f.filename.replace("\\", "/"))[:200]
    try:
        parsed = history_import.parse(name, data, today=_local_today(u))
    except history_import.HistoryImportError as e:
        return {"ok": False, "error": str(e), "imported": 0, "skipped": 0, "errors": [str(e)]}, 400
    imported = store.import_history(_rid(u), parsed["rows"], source_file=name, imported_by=u.get("id"))
    return {"ok": True, "imported": imported, "skipped": parsed["skipped"], "errors": parsed["errors"]}, 200


def _do_dsr_history_template(u):
    """The import template: its header row only."""
    from dsr import history_import
    if _dsr_view(u) is None:
        return _NO_DSR
    resp = Response(history_import.template_csv(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = 'attachment; filename="dsr-last-year-template.csv"'
    return resp, 200


def _dsr_grid_day(u):
    from dsr import store
    raw = request.args.get("date")
    if raw:
        return _dsr_day(raw)
    latest = store.list_reports(_rid(u), limit=1)
    if latest:
        return _dsr_day(latest[0]["business_date"])
    import closeout
    from models import get_restaurant
    return closeout.business_date_for(get_restaurant(_rid(u)))


def _do_dsr_week_xlsx(u):
    """The week's grid as an .xlsx in Erik's layout, from the same payload
    the screen renders (so the manager's file has no budget columns)."""
    from dsr import access, rollup, xlsx
    from models import get_restaurant
    if _dsr_view(u) is None:
        return _NO_DSR
    d = _dsr_grid_day(u)
    if d is None:
        return {"ok": False, "error": "The date must be YYYY-MM-DD."}, 400
    r = get_restaurant(_rid(u))
    name = getattr(r, "name", "") or ""
    grid = access.redact_grid(rollup.week(r, d), u)
    resp = Response(xlsx.week_workbook(grid, name),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp.headers["Content-Disposition"] = f'attachment; filename="{xlsx.filename(grid, name)}"'
    return resp, 200


_NOT_OWNER_DSR = ({"ok": False, "error": "Only the owner can change the daily report's settings."}, 403)


def _dsr_owner_only(u):
    """None when this login is the owner view; else the refusal to return."""
    from dsr import access
    view = _dsr_view(u)
    if view == access.OWNER:
        return None
    return _NOT_OWNER_DSR if view else _NO_DSR


def _dsr_money(v, label):
    """(figure, error): a non-negative number, or None to clear it."""
    if v is None or v == "":
        return None, None
    if isinstance(v, bool):
        return None, f"{label} must be a number."
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None, f"{label} must be a number."
    if f != f or f < 0 or f > 10_000_000:
        return None, f"{label} must be between $0 and $10,000,000."
    return round(f, 2), None


def _do_dsr_budget(u):
    """The owner's budget for a night — {date, gross, net}, or {days: [...]}
    for a week at once. A blank figure clears it; nothing is carried into a
    night the owner didn't enter."""
    from dsr import store
    from time_utils import mdy
    refused = _dsr_owner_only(u)
    if refused:
        return refused
    body = _body()
    days = body.get("days") if isinstance(body.get("days"), list) else [body]
    if not days or len(days) > 14:
        return {"ok": False, "error": "Send one night, or up to 14 in days."}, 400
    clean = []
    for row in days:
        if not isinstance(row, dict):
            return {"ok": False, "error": "Each night must be {date, gross, net}."}, 400
        d = _dsr_day(row.get("date"))
        if d is None:
            return {"ok": False, "error": "The date must be YYYY-MM-DD."}, 400
        gross, err = _dsr_money(row.get("gross"), "Gross")
        if err:
            return {"ok": False, "error": err}, 400
        net, err = _dsr_money(row.get("net"), "Net")
        if err:
            return {"ok": False, "error": err}, 400
        clean.append((d, gross, net))
    for d, gross, net in clean:
        store.set_budget(_rid(u), d, gross=gross, net=net, updated_by=u.get("id"))
    return {"ok": True, "saved": [{"date": d.isoformat(), "label": mdy(d), "gross": g, "net": n}
                                  for d, g, n in clean]}, 200


def _do_dsr_category(u):
    """Map a POS department to a DSR category — {pos_name, category}. One of
    Erik's six (matched case-insensitively) or the owner's own label; never
    "Unmapped", which is what no mapping means."""
    import dsr as _dsr
    from dsr import store
    refused = _dsr_owner_only(u)
    if refused:
        return refused
    body = _body()
    name = body.get("pos_name") if isinstance(body.get("pos_name"), str) else ""
    cat = body.get("category") if isinstance(body.get("category"), str) else ""
    name, cat = name.strip(), " ".join(cat.split())
    if not name or not cat:
        return {"ok": False, "error": "Pick a POS department and a category."}, 400
    if len(name) > 120 or len(cat) > 60:
        return {"ok": False, "error": "That name is too long."}, 400
    if cat.lower() == _dsr.UNMAPPED.lower():
        return {"ok": False, "error": "Pick one of your categories."}, 400
    cat = {c.lower(): c for c in _dsr.DEFAULT_CATEGORIES}.get(cat.lower(), cat)
    store.set_category(_rid(u), name, cat)
    return {"ok": True, "pos_name": name, "category": cat,
            "note": f"Nights already reported keep their split; from the next report on, {name} counts as {cat}."}, 200


def _dsr_unmapped(rid):
    """(departments, business_date): what the latest report couldn't place,
    with its dollars — straight from that night's Sales detail."""
    from dsr import store
    latest = store.list_reports(rid, limit=1)
    if not latest:
        return [], None
    sales = ((latest[0].get("facts") or {}).get("blocks") or {}).get("sales") or {}
    rows = (sales.get("detail") or {}).get("unmapped") or []
    return ([{"department": x.get("department"), "net": x.get("net")} for x in rows
             if isinstance(x, dict) and x.get("department")], latest[0]["business_date"])


def _dsr_settings_payload(r):
    import closeout
    from dsr import fiscal
    from time_utils import mdy
    hour = getattr(r, "dsr_deadline_hour", None)
    years = []
    for start, lengths in fiscal.listed_years(r):
        pos = fiscal.position(r, start)
        years.append({"start": start.isoformat(), "start_label": mdy(start.isoformat()), "lengths": lengths,
                      "weeks": sum(lengths), "fiscal_year": pos["fiscal_year"]})
    return {"fiscal_week_start_dow": getattr(r, "fiscal_week_start_dow", None),
            "fiscal_year_start": getattr(r, "fiscal_year_start", None) or None,
            "fiscal_period_scheme": getattr(r, "fiscal_period_scheme", None) or "4x13",
            "fiscal_years": years,
            "dsr_enabled": bool(getattr(r, "dsr_enabled", 1)),
            "dsr_notify": bool(getattr(r, "dsr_notify", 0)),
            "dsr_gross_basis": getattr(r, "dsr_gross_basis", None) or "items",
            "dsr_deadline_hour": 4 if hour is None else int(hour),
            "calendar_label": fiscal.label(r, closeout.business_date_for(r))}


def _do_dsr_settings_get(u):
    """The owner's DSR settings: the fiscal calendar, the switch and the
    deadline, the category map and what the latest night left unmapped."""
    import dsr as _dsr
    from dsr import store
    from models import get_restaurant
    from time_utils import mdy
    refused = _dsr_owner_only(u)
    if refused:
        return refused
    r = get_restaurant(_rid(u))
    unmapped, as_of = _dsr_unmapped(_rid(u))
    mapping = store.category_map(_rid(u))
    return {"ok": True, "can_edit": True, "settings": _dsr_settings_payload(r),
            "categories": list(_dsr.DEFAULT_CATEGORIES),
            "category_map": [{"pos_name": k, "category": v} for k, v in sorted(mapping.items())],
            "unmapped": unmapped, "unmapped_as_of": mdy(as_of) if as_of else None}, 200


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _do_dsr_settings_set(u):
    """Any of fiscal_week_start_dow (0=Mon … 6=Sun, or null), fiscal_year_start
    (YYYY-MM-DD, or "" to clear), fiscal_period_scheme ("4x13" | "445" | "454" |
    "544" | the period lengths, comma-separated), fiscal_years (the years that
    differ, [{start, lengths}], or [] / null to clear), dsr_enabled, dsr_notify
    (email + push the finished report), dsr_gross_basis ("items" | "all"),
    dsr_deadline_hour (0–11, local). Every column is in update_restaurant's
    whitelist. A value of the wrong type is a 400 with the reason, never a
    500."""
    import json
    from dsr import fiscal
    from models import get_restaurant, update_restaurant
    from time_utils import mdy
    refused = _dsr_owner_only(u)
    if refused:
        return refused
    body = _body()
    if not isinstance(body, dict):
        return {"ok": False, "error": "Send the settings as a JSON object."}, 400
    fields = {}
    if "fiscal_week_start_dow" in body:
        v = body.get("fiscal_week_start_dow")
        if v is None or v == "":
            fields["fiscal_week_start_dow"] = None
        else:
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = -1
            if isinstance(body.get("fiscal_week_start_dow"), bool) or not 0 <= v <= 6:
                return {"ok": False, "error": "Pick a day for your week to start."}, 400
            fields["fiscal_week_start_dow"] = v
    if "fiscal_year_start" in body:
        raw = body.get("fiscal_year_start")
        if raw in (None, ""):
            fields["fiscal_year_start"] = None
        else:
            d = _dsr_day(raw)
            if d is None:
                return {"ok": False, "error": "The year start must be a date."}, 400
            fields["fiscal_year_start"] = d.isoformat()
    if "fiscal_period_scheme" in body:
        raw = body.get("fiscal_period_scheme")
        if isinstance(raw, list) and all(isinstance(n, int) and not isinstance(n, bool) for n in raw):
            raw = ",".join(str(n) for n in raw)
        scheme = raw.replace(" ", "") if isinstance(raw, str) else ""
        if not fiscal.valid_scheme(scheme):
            return {"ok": False, "error": "Pick 13 four-week periods, 4-4-5, 4-5-4 or 5-4-4, or list your "
                                          "periods' lengths in weeks (12 or 13 of 4 or 5, totalling 52 or 53)."}, 400
        fields["fiscal_period_scheme"] = scheme
    if "fiscal_years" in body:
        raw = body.get("fiscal_years")
        if raw is not None and not isinstance(raw, list):
            return {"ok": False, "error": "List each year as a start date and its period lengths."}, 400
        rows, err = fiscal.parse_years(raw)
        if err:
            return {"ok": False, "error": err}, 400
        fields["fiscal_years_json"] = (json.dumps([{"start": s.isoformat(), "lengths": n} for s, n in rows])
                                       if rows else None)
    if "dsr_enabled" in body:
        fields["dsr_enabled"] = 1 if body.get("dsr_enabled") else 0
    if "dsr_notify" in body:
        fields["dsr_notify"] = 1 if body.get("dsr_notify") else 0
    if "dsr_gross_basis" in body:
        from dsr.block_sales import GROSS_BASES
        # A list or an object is unhashable: `in GROSS_BASES` raised and the
        # route answered 500 (re-audit D16). Only a known string is a basis.
        basis = body.get("dsr_gross_basis")
        if not isinstance(basis, str) or basis not in GROSS_BASES:
            return {"ok": False, "error": "Gross is either items only or everything rung (items, tax and voids)."}, 400
        fields["dsr_gross_basis"] = basis
    if "dsr_deadline_hour" in body:
        raw = body.get("dsr_deadline_hour")
        try:
            h = -1 if isinstance(raw, bool) else int(raw)
        except (TypeError, ValueError):
            h = -1
        if not 0 <= h <= 11:
            return {"ok": False, "error": "Pick an hour between midnight and 11am."}, 400
        fields["dsr_deadline_hour"] = h
    if not fields:
        return {"ok": False, "error": "Nothing to change."}, 400
    # Periods are whole weeks: a year start that isn't the week's first day
    # would split a week across two periods.
    r = get_restaurant(_rid(u))
    dow = fields["fiscal_week_start_dow"] if "fiscal_week_start_dow" in fields else getattr(r, "fiscal_week_start_dow", None)
    ys = fields["fiscal_year_start"] if "fiscal_year_start" in fields else getattr(r, "fiscal_year_start", None)
    want = 0 if dow is None else int(dow)
    if ys:
        if date.fromisoformat(str(ys)[:10]).weekday() != want:
            return {"ok": False, "error": f"Period 1 has to start on a {_WEEKDAYS[want]}, the first day of your week."}, 400
    listed = (fiscal.parse_years(fields["fiscal_years_json"])[0] if "fiscal_years_json" in fields
              else fiscal.listed_years(r))
    for start, _lengths in listed:
        if start.weekday() != want:
            return {"ok": False, "error": f"The year starting {mdy(start)} has to start on a "
                                          f"{_WEEKDAYS[want]}, the first day of your week."}, 400
    update_restaurant(_rid(u), fields)
    return {"ok": True, "settings": _dsr_settings_payload(get_restaurant(_rid(u)))}, 200


def _forget(rid, route, key):
    from models import get_conn
    conn = get_conn()
    try:
        conn.execute("DELETE FROM idempotent_responses WHERE restaurant_id=? AND route=? AND idem_key=?",
                     (rid, route, key))
        conn.commit()
    finally:
        conn.close()


_ROUTES = [
    # (path, methods, body, endpoint)
    ("/issues", ["GET"], _do_issues_list, "issues_list"),
    ("/issues", ["POST"], _do_issue_create, "issue_create"),
    ("/issues/<int:issue_id>/resolve", ["POST"], _do_issue_resolve, "issue_resolve"),
    ("/issues/<int:issue_id>/reassign", ["POST"], _do_issue_reassign, "issue_reassign"),
    ("/issues/<int:issue_id>/ask-cover", ["POST"], _do_issue_ask_cover, "issue_ask_cover"),
    ("/issues/routing", ["GET"], _do_routing_get, "issue_routing_get"),
    ("/issues/routing", ["POST"], _do_routing_set, "issue_routing_set"),
    ("/goals", ["GET"], _do_goals_list, "goals_list"),
    ("/goals", ["POST"], _do_goal_set, "goal_set"),
    ("/goals/<int:goal_id>/end", ["POST"], _do_goal_end, "goal_end"),
    ("/outcomes", ["GET"], _do_outcomes_list, "outcomes_list"),
    ("/outcomes", ["POST"], _do_outcome_record, "outcome_record"),
    ("/outcomes/<int:outcome_id>/abandon", ["POST"], _do_outcome_abandon, "outcome_abandon"),
    ("/metrics", ["GET"], _do_metrics, "metrics_list"),
    ("/value", ["GET"], _do_value, "value_summary"),
    ("/food-cost/dish-scorecard", ["GET"], _do_dish_scorecard, "dish_scorecard"),
    ("/food-cost/reprice", ["GET"], _do_reprice, "reprice"),
    ("/food-cost/invoices", ["GET"], _do_invoice_list, "invoice_list"),
    ("/food-cost/invoices", ["POST"], _idempotent(_do_invoice_scan, "invoice_scan"), "invoice_scan"),
    ("/food-cost/invoices/<int:import_id>", ["GET"], _do_invoice_get, "invoice_get"),
    ("/food-cost/invoices/<int:import_id>/apply", ["POST"], _do_invoice_apply, "invoice_apply"),
    ("/actions", ["GET"], _do_actions, "actions_list"),
    ("/actions/snooze", ["POST"], _do_action_snooze, "actions_snooze"),
    ("/closeout", ["GET"], _do_closeout_get, "closeout_get"),
    ("/closeout", ["POST"], _do_closeout_save, "closeout_save"),
    ("/labor/demand", ["GET"], _do_demand, "demand"),
    ("/labor/auto-draft", ["GET"], _do_auto_draft_get, "auto_draft_get"),
    ("/labor/auto-draft", ["POST"], _do_auto_draft_set, "auto_draft_set"),
    ("/loss-signals", ["GET"], _do_loss_signals, "loss_signals"),
    ("/cross-module", ["GET"], _do_cross_module, "cross_module"),
    ("/good-news", ["GET"], _do_good_news, "good_news"),
    ("/milestones", ["GET"], _do_milestones, "milestones_list"),
    ("/monthly-review", ["GET"], _do_monthly_review, "monthly_review"),
    ("/activity", ["GET"], _do_activity, "activity"),
    ("/labor/auto-publish", ["GET"], _do_auto_publish_get, "auto_publish_get"),
    ("/labor/auto-publish", ["POST"], _do_auto_publish_set, "auto_publish_set"),
    ("/food-cost/recipe-drafts", ["GET"], _do_recipe_drafts, "recipe_drafts"),
    ("/food-cost/recipe-drafts/<int:draft_id>/accept", ["POST"], _do_recipe_draft_accept, "recipe_draft_accept"),
    ("/food-cost/recipe-drafts/<int:draft_id>/reject", ["POST"], _do_recipe_draft_reject, "recipe_draft_reject"),
    ("/food-cost/recipes/import", ["POST"], _do_recipes_import, "recipes_import"),
    ("/food-cost/recipes/draft", ["POST"], _do_recipe_draft_now, "recipe_draft_now"),
    ("/labor/weekly-plan", ["GET"], _do_weekly_plan_get, "weekly_plan_get"),
    ("/labor/weekly-plan", ["POST"], _do_weekly_plan_set, "weekly_plan_set"),
    ("/account/send-delay", ["GET"], _do_send_delay_get, "send_delay_get"),
    ("/account/send-delay", ["POST"], _do_send_delay_set, "send_delay_set"),
    ("/food-cost/count-sheet", ["GET"], _do_count_sheet_get, "count_sheet_get"),
    ("/food-cost/count-sheet", ["POST"], _do_count_sheet_save, "count_sheet_save"),
    ("/food-cost/auto-order", ["GET"], _do_auto_order_get, "auto_order_get"),
    ("/food-cost/auto-order", ["POST"], _do_auto_order_set, "auto_order_set"),
    ("/account/memory", ["GET"], _do_memory_list, "memory_list"),
    ("/account/memory/add", ["POST"], _do_memory_add, "memory_add"),
    ("/marketing/posts/<int:post_id>/tags", ["POST"], _do_post_tags, "post_tags"),
    ("/intel/ai-visibility/queries", ["GET"], _do_ai_visibility_queries, "ai_visibility_queries"),
    ("/marketing/diagnosis", ["GET"], _do_marketing_diagnosis, "marketing_diagnosis"),
    ("/labor/time-off", ["GET"], _do_time_off_list, "time_off_list"),
    ("/labor/time-off/<int:request_id>/decide", ["POST"], _do_time_off_decide, "time_off_decide"),
    ("/labor/covers", ["GET"], _do_covers_get, "covers_get"),
    ("/labor/covers", ["POST"], _do_covers_save, "covers_save"),
    ("/labor/roster", ["GET"], _do_roster_get, "roster_get"),
    ("/labor/staff-settings", ["POST"], _do_staff_settings_set, "staff_settings_set"),
    ("/labor/staff-pairs", ["POST"], _do_staff_pairs_set, "staff_pairs_set"),
    ("/labor/staff-pairs/<int:pair_id>", ["DELETE"], _do_staff_pair_delete, "staff_pair_delete"),
    ("/labor/demand-signals", ["GET"], _do_demand_signals_get, "demand_signals_get"),
    ("/labor/demand-signals", ["POST"], _do_demand_signals_save, "demand_signals_save"),
    ("/labor/demand-signals/<int:signal_id>", ["DELETE"], _do_demand_signal_delete, "demand_signal_delete"),
    ("/labor/rules", ["GET"], _do_compliance_get, "schedule_rules_get"),
    ("/labor/rules", ["POST"], _do_compliance_set, "schedule_rules_set"),
    ("/labor/schedule-history/<int:history_id>/versions", ["GET"], _do_schedule_versions, "schedule_versions"),
    ("/labor/schedule/violations", ["POST"], _do_schedule_violations, "schedule_violations"),
    ("/labor/schedule/apply-fixes", ["POST"], _do_schedule_apply_fixes, "schedule_apply_fixes"),
    ("/labor/schedule/optimize", ["POST"], _do_schedule_optimize, "schedule_optimize"),
    ("/labor/ratings/unmatched", ["GET"], _do_ratings_unmatched, "ratings_unmatched"),
    ("/labor/ratings/match", ["POST"], _do_ratings_match, "ratings_match"),
    ("/recs/event", ["POST"], _do_rec_event, "rec_event"),
    ("/recs/summary", ["GET"], _do_recs_summary, "recs_summary"),
    ("/recs/timeline", ["GET"], _do_recs_timeline, "recs_timeline"),
    ("/recs/what-worked", ["GET"], _do_recs_what_worked, "recs_what_worked"),
    ("/recs/checkin", ["POST"], _do_recs_checkin, "recs_checkin"),
    ("/labor/quality/calibration/apply", ["POST"], _do_calibration_apply, "calibration_apply"),
    ("/labor/shift-requests", ["GET"], _do_shift_requests_list, "shift_requests_list"),
    ("/labor/shift-requests/<int:request_id>/decide", ["POST"], _do_shift_request_decide, "shift_request_decide"),
    ("/labor/learned-patterns", ["GET"], _do_learned_patterns, "learned_patterns"),
    ("/labor/learned-patterns", ["POST"], _do_learned_pattern_set, "learned_pattern_set"),
    ("/labor/schedule/recommendation", ["POST"], _do_recommendation_event, "schedule_recommendation_event"),
    ("/labor/standby/ask", ["POST"], _do_standby_ask, "labor_standby_ask"),
    ("/labor/intel", ["GET"], _do_schedule_intel, "schedule_intel"),
    ("/labor/reservations/sync", ["POST"], _do_reservation_sync, "reservation_sync"),
    ("/food-cost/recipes/scan", ["POST"], _idempotent(_do_recipe_scan, "recipe_scan"), "recipe_scan"),
    ("/account/trust", ["GET"], _do_trust, "trust"),
    ("/decisions", ["GET"], _do_decisions, "decisions"),
    ("/account/memory/forget", ["POST"], _do_memory_forget, "memory_forget"),
    ("/actions/pending", ["GET"], _do_delayed_pending, "delayed_pending"),
    ("/actions/<int:action_id>/cancel", ["POST"], _do_delayed_cancel, "delayed_cancel"),
    ("/account/pause", ["GET"], _do_pause_status, "pause_status"),
    ("/account/pause", ["POST"], _do_pause, "pause"),
    ("/account/resume", ["POST"], _do_resume, "resume"),
    ("/milestones/seen", ["POST"], _do_milestone_seen, "milestone_seen"),
    ("/morning-brief", ["GET"], _do_morning_brief, "morning_brief"),
    ("/morning-brief/settings", ["POST"], _do_morning_brief_settings, "morning_brief_settings"),
    ("/dsr", ["GET"], _do_dsr_list, "dsr_list"),
    ("/dsr/close", ["POST"], _do_dsr_close, "dsr_close"),
    ("/dsr/week", ["GET"], _do_dsr_week, "dsr_week"),
    ("/dsr/period", ["GET"], _do_dsr_period, "dsr_period"),
    ("/dsr/history/import", ["POST"], _do_dsr_history_import, "dsr_history_import"),
    ("/dsr/history/template.csv", ["GET"], _do_dsr_history_template, "dsr_history_template"),
    ("/dsr/week.xlsx", ["GET"], _do_dsr_week_xlsx, "dsr_week_xlsx"),
    ("/dsr/budget", ["POST"], _do_dsr_budget, "dsr_budget"),
    ("/dsr/category", ["POST"], _do_dsr_category, "dsr_category"),
    ("/dsr/settings", ["GET"], _do_dsr_settings_get, "dsr_settings_get"),
    ("/dsr/settings", ["POST"], _do_dsr_settings_set, "dsr_settings_set"),
    ("/ask-cavnar/feedback", ["POST"], _do_ask_feedback, "ask_feedback"),
    ("/dsr/<day>", ["GET"], _do_dsr_get, "dsr_get"),
    ("/dsr/<day>/status", ["GET"], _do_dsr_status, "dsr_status"),
]


def _wrap(body, decorator):
    def view(current_user, **kw):
        payload, status = body(current_user, **kw)
        # A body that builds its own response (a file download) is passed
        # through; every other body returns a dict.
        resp = payload if isinstance(payload, Response) else jsonify(**payload)
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
        # The page is presented only when a PERSON acted on it: its GET
        # stays free of side effects (a messaging app's link preview fetches
        # it), so the covers it showed are recorded on the post that proves
        # someone had it open — once a day, like every showing.
        seen = issues.by_token(token)
        if seen:
            present_covers(seen["restaurant_id"], [seen], "issue_sms")
        if action == "ack":
            issue = issues.acknowledge(token)
            done = "ack"
        elif action == "resolve":
            issue = issues.resolve_by_token(
                token, note=(request.form.get("note") or "").strip()[:500] or None)
            done = "resolve"
        elif action == "ask_cover":
            # The manager the no-show was texted to, asking a suggested cover
            # from the same page — one tap; the link is their authority.
            import intraday
            from ai_utils import ai_rate_limited
            held = issues.by_token(token)
            if not held:
                abort(404)
            if ai_rate_limited(f"issue_text:{held['restaurant_id']}", max_calls=20, window_secs=3600):
                abort(429)
            asked = intraday.ask_to_cover(held["restaurant_id"], held["id"],
                                          (request.form.get("name") or "").strip()[:80], surface="issue_sms")
            issue = issues.by_token(token)
            done = {"asked": asked}
        else:
            abort(400)
    else:
        issue = issues.by_token(token)
    if not issue:
        abort(404)
    resp = render_template("issue.html", issue=issue, token=token, done=done)
    return resp, 200, {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                       "X-Robots-Tag": "noindex"}
