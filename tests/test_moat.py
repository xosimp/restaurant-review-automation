"""The moat audit's implementation (Sep 2026).

What compounds here is the restaurant's own record: what it decided, what
was measured after, and which automations that record has earned. Every
test pins that a figure comes from a row someone wrote — never generated,
never re-proposed once declined, never scored against itself.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    import decisions, outcomes, issues, home_brief, monthly_review, food_cost_intelligence as fci, ordering
    for mod in (models, decisions, outcomes, issues, home_brief, monthly_review, fci, ordering):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import auth
    auth.init_auth(db_path=db_path)


def _rid(db_path, **kw):
    kw.setdefault("module_reviews", 1)
    return create_restaurant(Restaurant(name="Moat Co", owner_email="m@x.com", **kw), db_path=db_path)


def _owner(rid, uid=1):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "owner", "username": "o", "email": "m@x.com"}


# ── #1 decision records ───────────────────────────────────────────────────────

def test_history_joins_the_four_tables_into_one_record_per_decision(db_path):
    import decisions, home_brief, outcomes, issues
    rid = _rid(db_path, module_labor=1)
    # the owner said "not for us" twice, with a reason the second time
    home_brief.dismiss(rid, "trim_day:Monday", kind="recommendation", user_id=1)
    home_brief.dismiss(rid, "trim_day:Monday", kind="not_for_us", user_id=1,
                       reason="Monday is our delivery day", title="Trim Monday")
    # a recommendation they acted on, being measured
    outcomes.record(rid, "home", "cut_waste:Salmon", "Cut salmon waste", "weekly_waste", db_path=db_path)
    # an issue, resolved with a note
    issue, _ = issues.create_issue(rid, "plan", "Post the schedule by Thursday", source_key="plan:2026-W38:0",
                                   notify=False, db_path=db_path)
    issues.resolve(rid, issue["id"], note="Done Wednesday", db_path=db_path)
    # a proposal from Ask, dismissed
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ask_cavnar_actions (restaurant_id, action, summary, outcome) VALUES (?,?,?,?)",
                 (rid, "draft_campaign", "Text regulars about the patio", "dismissed"))
    conn.commit(); conn.close()

    rows = decisions.history(rid, db_path=db_path)
    by_key = {r["key"]: r for r in rows}
    d = by_key["trim_day:Monday"]
    assert d["answer"] == "not for us" and d["times_hidden"] == 2 and d["title"] == "Trim Monday"
    assert d["reason"] == "Monday is our delivery day"
    o = by_key["cut_waste:Salmon"]
    assert o["answer"] == "tracking" and o["outcome"]["metric"] == "weekly_waste" and o["outcome"]["status"] == "tracking"
    i = by_key["plan:2026-W38:0"]
    # resolving an issue answers the card that raised it (issues._resolve → home_brief.dismiss "done")
    assert i["answer"] == "done" and i["issue"]["note"] == "Done Wednesday" and i["kind"] == "plan"
    p = next(r for r in rows if r["kind"] == "proposal")
    assert p["answer"] == "dismissed" and p["title"] == "Text regulars about the patio"


def test_the_context_section_is_empty_for_an_empty_restaurant_and_dated_otherwise(db_path):
    import decisions, home_brief
    rid = _rid(db_path)
    assert decisions.context(rid, db_path=db_path) == ""
    home_brief.dismiss(rid, "add_brunch", kind="not_for_us", reason="no Sunday staff", title="Add brunch")
    text = decisions.context(rid, db_path=db_path)
    assert text.startswith("WHAT THIS RESTAURANT HAS DECIDED BEFORE")
    assert "Add brunch: not for us" in text and "because: no Sunday staff" in text
    assert date.today().isoformat() in text
    assert "Do not re-propose something marked 'not for us'" in text


def test_ask_reads_decisions_as_a_tool_and_as_context(db_path, monkeypatch):
    import ask_cavnar, ask_cavnar_tools, home_brief
    names = [t["spec"]["name"] for t in ask_cavnar_tools.TOOLS]
    assert "read_decisions" in names
    tool = next(t for t in ask_cavnar_tools.TOOLS if t["spec"]["name"] == "read_decisions")
    assert tool["kind"] == "read"
    rid = _rid(db_path)
    home_brief.dismiss(rid, "raise_prices", kind="done", title="Raise prices")
    out = tool["fn"](rid, limit=5)
    assert out["count"] == 1 and out["decisions"][0]["answer"] == "done"
    assert ask_cavnar._decisions_context in ask_cavnar.build_context.__globals__.values() or hasattr(ask_cavnar, "_decisions_context")
    assert "Raise prices: done" in ask_cavnar._decisions_context(rid)


# ── #10 an opened alert starts a measurement ──────────────────────────────────

def test_an_opened_alert_with_a_metric_starts_one_observed_tracker(db_path):
    import outcomes
    rid = _rid(db_path, module_labor=1)
    t = outcomes.observe(rid, "alert_labor_over", user_id=1, db_path=db_path)
    assert t and t["metric"] == "labor_pct" and t["source"] == "observed"
    assert t["title"].startswith("Read the labor-over-target alert")
    # a second open the same month, or while one is in flight, adds nothing
    assert outcomes.observe(rid, "alert_labor_over", user_id=1, db_path=db_path) is None
    # an alert with no metric behind it is never tracked
    assert outcomes.observe(rid, "alert_new_review", user_id=1, db_path=db_path) is None
    assert "alert_new_review" not in outcomes.OBSERVED_ACTIONS
    for k, (metric, _t) in outcomes.ALERT_METRICS.items():
        import metrics
        assert metrics.known(metric), k


def test_the_phone_route_wires_the_open_to_the_tracker(db_path, monkeypatch):
    import mobile_api, outcomes
    seen = []
    monkeypatch.setattr(outcomes, "observe", lambda rid, action, **kw: seen.append((rid, action)))
    monkeypatch.setattr(models, "record_notification_open", lambda *a, **k: None)
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    rid = _rid(db_path)
    inner = mobile_api.mobile_mark_notification_opened.__wrapped__ if hasattr(
        mobile_api.mobile_mark_notification_opened, "__wrapped__") else None
    assert inner is not None, "route body must be reachable without a token"
    with app.test_request_context("/mobile/api/notifications/opened", method="POST", json={"type": "food_waste"}):
        inner(_owner(rid))
    with app.test_request_context("/mobile/api/notifications/opened", method="POST", json={"type": "new_review"}):
        inner(_owner(rid))
    assert seen == [(rid, "alert_food_waste")]


# ── #12 the weekly plan is scored in the monthly review ───────────────────────

def test_the_review_counts_plan_actions_filed_and_done_last_month(db_path, monkeypatch):
    import monthly_review, issues
    rid = _rid(db_path, module_labor=1)
    today = date(2026, 10, 3)
    conn = get_conn(db_path)
    for i, status in enumerate(("resolved", "resolved", "open")):
        conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, created_at) "
                     "VALUES (?,?,?,?,?,?)", (rid, "plan", f"plan:2026-W37:{i}", f"Action {i}", status, "2026-09-14 12:00:00"))
    # a plan from the month before does not count, nor does a non-plan issue
    conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, created_at) "
                 "VALUES (?,?,?,?,?,?)", (rid, "plan", "plan:2026-W33:0", "Old", "resolved", "2026-08-14 12:00:00"))
    conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, created_at) "
                 "VALUES (?,?,?,?,?,?)", (rid, "callout", "c:1", "Callout", "resolved", "2026-09-14 12:00:00"))
    conn.commit(); conn.close()
    review = monthly_review.build(rid, today=today, db_path=db_path)
    assert review["plan"] == {"filed": 3, "done": 2}
    assert "You did 2 of the 3 actions the Monday plans filed last month." in monthly_review.lines(review)
    # no plans filed → no line, no zero
    rid2 = _rid(db_path)
    r2 = monthly_review.build(rid2, today=today, db_path=db_path)
    assert r2["plan"] is None
    assert not any("Monday plans" in l for l in monthly_review.lines(r2))


# ── #16 the profitability projection is frozen once and scored ────────────────

def test_the_projection_is_frozen_once_mid_month_never_on_render(db_path, monkeypatch):
    import food_cost_intelligence as fci
    rid = _rid(db_path, module_inventory=1)
    calls = []
    monkeypatch.setattr(fci, "profitability_projection", lambda r, db_path=None: (calls.append(1) or {
        "available": True, "prime_cost_pct": 61.5, "days_elapsed": 15}))
    assert fci.record_profitability_forecast(rid, db_path=db_path, today=date(2026, 9, 10)) == {
        "recorded": False, "reason": "before the 15th"}
    assert calls == []
    out = fci.record_profitability_forecast(rid, db_path=db_path, today=date(2026, 9, 15))
    assert out["recorded"] and out["horizon_end"] == "2026-09-30" and out["predicted"] == 61.5
    # the next night: already frozen — the later, more accurate reading never replaces it
    monkeypatch.setattr(fci, "profitability_projection", lambda r, db_path=None: {
        "available": True, "prime_cost_pct": 58.0, "days_elapsed": 28})
    assert fci.record_profitability_forecast(rid, db_path=db_path, today=date(2026, 9, 28))["reason"] == "already frozen this month"
    conn = get_conn(db_path)
    row = conn.execute("SELECT predicted, actual FROM forecast_log WHERE restaurant_id=? AND kind='profitability_month'",
                       (rid,)).fetchone()
    conn.close()
    assert row["predicted"] == 61.5 and row["actual"] is None


def test_a_closed_month_is_scored_from_measured_food_and_labor(db_path, monkeypatch):
    import food_cost_intelligence as fci, metrics
    rid = _rid(db_path, module_inventory=1)
    fci.record_forecast(rid, "profitability_month", "2026-08-31", 60.0, basis="test", db_path=db_path)
    vals = {"food_cost_pct": (32.0, "measured"), "labor_pct": (26.0, "measured")}
    monkeypatch.setattr(metrics, "measure", lambda r, key, s, e, db: vals[key])
    monkeypatch.setattr(fci, "load_waste_history", lambda *a, **k: [], raising=False)
    import waste_trend
    monkeypatch.setattr(waste_trend, "load_waste_history", lambda *a, **k: ([], None))
    out = fci.score_forecasts(rid, db_path=db_path)
    assert out["scored"] == 1
    conn = get_conn(db_path)
    row = conn.execute("SELECT actual, error_pct FROM forecast_log WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["actual"] == 58.0 and row["error_pct"] is not None
    # a month whose labor cannot be measured stays unscored — never scored against zero
    fci.record_forecast(rid, "profitability_month", "2026-07-31", 60.0, basis="test", db_path=db_path)
    monkeypatch.setattr(metrics, "measure", lambda r, key, s, e, db: (None, "no data") if key == "labor_pct" else (32.0, ""))
    assert fci.score_forecasts(rid, db_path=db_path)["scored"] == 0


# ── #13 / #17 the memory profile is the owner's to write; trust has one page ──

def test_the_owner_can_add_a_fact_and_it_is_logged(db_path, monkeypatch):
    import strategy_routes as sr
    rid = _rid(db_path)
    monkeypatch.setattr(sr, "_body", lambda: {"fact": "We close Mondays", "kind": "context"})
    payload, status = sr._do_memory_add(_owner(rid))
    assert status == 200 and payload["ok"]
    facts = [f["fact"] for f in models.get_ask_memory(rid, db_path=db_path)]
    assert "We close Mondays" in facts
    conn = get_conn(db_path)
    ev = conn.execute("SELECT event_type FROM activity_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
    conn.close()
    assert ev and ev["event_type"] == "memory_added"
    monkeypatch.setattr(sr, "_body", lambda: {"fact": "   "})
    payload, status = sr._do_memory_add(_owner(rid))
    assert status == 400


def test_the_trust_page_reads_every_earned_automation_from_the_record(db_path, monkeypatch):
    import strategy_routes as sr
    rid = _rid(db_path, module_inventory=1, module_labor=1)
    update_restaurant(rid, {"auto_publish_schedule": 1}, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, supplier_name, supplier_email, is_active) VALUES (?,?,?,?,1)",
                 (rid, "Flour", "Mill Co", "orders@mill.co"))
    conn.commit(); conn.close()
    payload, status = sr._do_trust(_owner(rid))
    assert status == 200 and payload["ok"]
    assert payload["schedule"] == {"enabled": True, "unedited_in_a_row": 0, "needed": models.SCHEDULE_PUBLISH_TRUST_MIN}
    assert payload["auto_approve"]["enabled"] is False and set(payload["auto_approve"]["bands"]) == {"3", "4", "5"}
    sup = payload["suppliers"]
    assert len(sup) == 1 and sup[0]["name"] == "Mill Co" and sup[0]["trusted"] is False and sup[0]["needed"] == 3
    # a login without Food Cost sees no supplier record at all
    from permissions import has_permission
    monkeypatch.setattr(sr, "_sees_food", lambda u: False)
    payload, _ = sr._do_trust(_owner(rid))
    assert payload["suppliers"] == [] and "orders_enabled" not in payload


def test_the_new_routes_exist_on_web_and_phone(db_path):
    import strategy_routes as sr
    paths = {p for p, _m, _f, _e in sr._ROUTES}
    for p in ("/account/memory/add", "/account/trust", "/decisions"):
        assert p in paths, p
    assert "memory_added" in models.ACCOUNT_EVENT_TYPES
