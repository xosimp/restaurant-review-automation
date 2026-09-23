"""The moat audit's implementation (Sep 2026).

What compounds here is the restaurant's own record: what it decided, what
was measured after, and which automations that record has earned. Every
test pins that a figure comes from a row someone wrote — never generated,
never re-proposed once declined, never scored against itself.
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant


# Imported here, before any fixture patches models.get_conn: a module first
# imported INSIDE a patched test binds the patched lambda (and its tmp DB)
# for the rest of the session — CLAUDE.md's bound-import hazard, seen live
# when recipes leaked one test's draft into the next file's.
import covers, decisions, food_cost_intelligence as fci, guest_marketing, home_brief, issues, labor, monthly_review  # noqa: E402
import ordering, outcomes, recipes, strategy_jobs, strategy_routes, time_off  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    for mod in (models, decisions, outcomes, issues, home_brief, monthly_review, fci, ordering, recipes, time_off,
                covers, strategy_jobs, labor):
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
    # home_dismissals.dismissed_at defaults to datetime('now'), which is UTC;
    # comparing against the local date failed every evening after 7pm Chicago.
    # Dated M/D/YY like every other date an owner (or Ask quoting one) reads.
    from datetime import datetime, timezone
    from time_utils import mdy
    assert f"({mdy(datetime.now(timezone.utc).date())})" in text
    assert datetime.now(timezone.utc).date().isoformat() not in text
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
    # the web twin carries the same hook — parity is asserted at the source
    import client_api
    web = client_api.mark_notification_opened.__wrapped__
    with app.test_request_context("/api/notifications/opened", method="POST", json={"type": "labor_over"}):
        web(_owner(rid))
    assert seen[-1] == (rid, "alert_labor_over")


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


# ── #5 time off: asked by the person, decided by a manager, honoured by the draft ──

def test_a_request_is_validated_and_decided_once(db_path):
    import time_off
    rid = _rid(db_path, module_labor=1)
    today = date(2026, 9, 21)
    row, err = time_off.request_time_off(rid, "Ana", "2026-10-03", "2026-10-05", reason="wedding", db_path=db_path, today=today)
    assert err is None and row["status"] == "pending" and row["reason"] == "wedding"
    assert time_off.request_time_off(rid, "Ana", "2026-10-04", "2026-10-04", db_path=db_path, today=today)[1] == \
        "You already have a request over those dates."
    assert time_off.request_time_off(rid, "Ana", "2026-09-01", "2026-09-02", db_path=db_path, today=today)[1] == "That date has passed."
    assert time_off.request_time_off(rid, "Ana", "2026-11-10", "2026-11-01", db_path=db_path, today=today)[1] == "The end date is before the start."
    assert time_off.request_time_off(rid, "", "2026-11-10", "2026-11-11", db_path=db_path, today=today)[1] == "No employee name on this session."
    assert time_off.pending(rid, db_path=db_path)[0]["id"] == row["id"]
    # nothing is a constraint until approved
    assert time_off.approved_in_window(rid, "2026-10-01", "2026-10-07", db_path=db_path) == {}
    dec = time_off.decide(rid, row["id"], True, decided_by=1, note="enjoy", db_path=db_path)
    assert dec["status"] == "approved" and dec["decision_note"] == "enjoy"
    assert time_off.decide(rid, row["id"], False, db_path=db_path) is None          # final
    assert time_off.decide(rid + 1, row["id"], False, db_path=db_path) is None      # not theirs
    # the draft's week reads only the overlapping days, by name
    assert time_off.approved_in_window(rid, "2026-10-05", "2026-10-11", db_path=db_path) == {"Ana": ["2026-10-05"]}
    assert time_off.mine(rid, "ana", db_path=db_path, today=today)[0]["status"] == "approved"


def test_time_off_routes_exist_for_managers_and_staff(db_path):
    import strategy_routes as sr, staff_routes
    paths = {p for p, _m, _f, _e in sr._ROUTES}
    assert "/labor/time-off" in paths and "/labor/time-off/<int:request_id>/decide" in paths
    src = open(staff_routes.__file__).read()
    assert '@staff_bp.route("/api/time-off", methods=["POST"])' in src and '@staff_bp.route("/api/time-off")' in src


# ── #4 covers: the figure that tells a lean day from a short one ──────────────

def test_covers_are_saved_and_read_by_the_analysis(db_path):
    import covers, labor
    rid = _rid(db_path, module_labor=1)
    out = covers.save(rid, covers.parse_csv("date,covers\n2026-09-14,180\n2026-09-15,60\nnope,x"), db_path=db_path)
    assert out["written"] == 2 and out["skipped"] == 1
    assert covers.by_date(rid, "2026-09-14", "2026-09-15", db_path=db_path) == {"2026-09-14": 180, "2026-09-15": 60}
    shifts = []
    # A strong day is 1.15x the median day (thresholds), so the week needs
    # its ordinary days for 9/14 to stand out.
    for day, sales in (("2026-09-14", 9000), ("2026-09-15", 3000), ("2026-09-16", 9000),
                       ("2026-09-17", 3000), ("2026-09-18", 3000)):
        shifts.append({"employee": "A", "role": "Server", "date": day, "day": "Mon", "scheduled_hours": 8,
                       "actual_hours": 8, "sales": sales, "shift_start": "10:00", "shift_end": "18:00"})
    plain = labor.analyse_shifts(shifts, hourly_rate=20, labor_target=30)
    assert plain["covers"] == {"days_with_covers": 0, "avg_sales_per_cover": None}
    with_c = labor.analyse_shifts(shifts, hourly_rate=20, labor_target=30, covers_by_date={"2026-09-14": 180, "2026-09-15": 60})
    assert with_c["covers"]["days_with_covers"] == 2 and with_c["covers"]["avg_sales_per_cover"] == 50.0
    lean = {d["date"]: d for d in with_c["understaffed_days"]}
    assert lean["9/14/26"]["covers"] == 180 and lean["9/14/26"]["sales_per_cover"] == 50.0 and lean["9/14/26"]["vs_avg_pct"] == 0.0
    assert "covers" not in lean["9/16/26"]
    # the prompt's refusal stands word for word without covers, and changes only with them
    assert "this system has no service-time, wait-time or cover-count data" in labor._covers_guidance(plain)
    g = labor._covers_guidance(with_c)
    assert "Cover counts ARE on file for 2 days" in g and "never assert that revenue was lost" in g


# ── #9 a comp/void pattern names a person only from the owner's own mapping ──

def test_the_approver_is_named_only_from_a_pos_id_the_owner_entered(db_path):
    import strategy_jobs
    rid = _rid(db_path, module_inventory=1)
    assert strategy_jobs._approver_name(rid, "8842", db_path) is None
    models.set_staff_contact(rid, "Jordan Lee", "j@x.com", "", db_path=db_path, pos_id="8842")
    assert strategy_jobs._approver_name(rid, "8842", db_path) == "Jordan Lee"
    assert strategy_jobs._approver_name(rid, "unrecorded", db_path) is None
    # a later save without a pos_id keeps it
    models.set_staff_contact(rid, "Jordan Lee", "jl@x.com", "", db_path=db_path)
    assert [c["pos_id"] for c in models.get_staff_contacts(rid, db_path=db_path)] == ["8842"]


# ── #3 a photographed recipe card becomes a draft, never a written recipe ────

def test_a_recipe_photo_becomes_a_pending_draft_with_unmatched_lines_named(db_path, monkeypatch):
    import recipes, inventory_ledger
    rid = _rid(db_path, module_inventory=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active) VALUES (?,?,?,1)", (rid, "Mozzarella", "lb"))
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active) VALUES (?,?,?,1)", (rid, "Flour", "lb"))
    conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, "Margherita Pizza"))
    conn.commit(); conn.close()
    monkeypatch.setattr(inventory_ledger, "list_ingredients", lambda r: [
        {"id": 1, "name": "Mozzarella", "unit": "lb"}, {"id": 2, "name": "Flour", "unit": "lb"}])
    monkeypatch.setattr(inventory_ledger, "list_menu_items_with_recipes", lambda r: [{"id": 7, "name": "Margherita Pizza"}])

    class _Blk:
        type = "text"
        text = json.dumps({"menu_item_name": "margherita", "note": None, "ingredients": [
            {"name": "Mozzarella", "qty": 0.25, "unit": "lb", "confidence": "high"},
            {"name": "San Marzano tomatoes", "qty": 0.5, "unit": "cup", "confidence": "medium"}]})

    class _Msg:
        stop_reason = "end_turn"
        content = [_Blk()]

    class _Client:
        pass
    import ai_utils
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda client, **kw: _Msg())
    draft = recipes.extract_from_image(rid, b"\xff\xd8fakejpeg", "image/jpeg", client=_Client(), db_path=db_path)
    assert draft["menu_item_id"] == 7 and draft["menu_item_matched"] is True
    assert draft["lines"] == [{"ingredient_id": 1, "name": "Mozzarella", "qty": 0.25, "unit": "lb", "confidence": "high"}]
    assert draft["unmatched"][0]["name"] == "San Marzano tomatoes"
    assert "not on your list: San Marzano tomatoes" in draft["note"]
    pending = recipes.list_drafts(rid, db_path=db_path)
    assert len(pending) == 1 and pending[0]["id"] == draft["id"]
    # nothing was written to the menu item
    conn = get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM recipe_drafts WHERE status='pending' AND restaurant_id=?", (rid,)).fetchone()[0] == 1
    conn.close()
    with pytest.raises(recipes.RecipePhotoError):
        recipes.extract_from_image(rid, b"%PDF-1.4 x", "application/pdf", client=_Client(), db_path=db_path)


# ── #21 a campaign's receipt is the guests Toast saw afterwards ──────────────

def test_recipients_are_kept_and_visits_matched_once_within_the_window(db_path, monkeypatch):
    import guest_marketing as gm, toast
    for mod in (gm,):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: models.get_conn(db_path))
        monkeypatch.setattr(mod, "DB_PATH", db_path)
    rid = _rid(db_path, module_marketing=1)
    update_restaurant(rid, {"toast_restaurant_guid": "guid-1"}, db_path=db_path)
    gm.init_guest_marketing(db_path=db_path)
    a = gm.add_guest_contact_manual(rid, "+13125550100", name="Ana", db_path=db_path)
    b = gm.add_guest_contact_manual(rid, "+13125550101", name="Ben", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE guest_contacts SET consent=1, consent_at='2026-09-01T10:00:00' WHERE restaurant_id=?", (rid,))
    conn.commit(); conn.close()
    monkeypatch.setattr(gm, "guest_sms_allowed_now", lambda r: True)
    monkeypatch.setattr(gm, "send_sms", lambda phone, msg, **kw: True)
    out = gm.send_campaign(rid, "Patio is open", db_path=db_path)
    assert out["ok"] and out["sent"] == 2
    conn = get_conn(db_path)
    camp_id = conn.execute("SELECT id FROM guest_campaigns WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    conn.execute("UPDATE guest_campaigns SET created_at='2026-09-15 12:00:00' WHERE id=?", (camp_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM guest_campaign_recipients WHERE campaign_id=?", (camp_id,)).fetchone()[0] == 2
    conn.close()
    fetched = []

    def fake_customers(r, day):
        fetched.append(day.isoformat())
        return [{"phone": "(312) 555-0100", "name": "Ana"}] if day.isoformat() in ("2026-09-16", "2026-09-18") else []
    # Attribution asks the connected provider (pos.fetch_order_customers), so
    # the fake is a provider that answers it — not a Toast column.
    import pos, types
    monkeypatch.setattr(pos, "PROVIDERS", {"toast": types.SimpleNamespace(
        is_connected=lambda r: r == rid, fetch_order_customers=fake_customers,
        sync_to_db=lambda r: {}, build_shifts_csv=lambda r, days=60: None)})
    res = gm.run_campaign_attribution(db_path=db_path, today=date(2026, 9, 19))
    assert res == {"campaigns_checked": 1, "visits_matched": 1}
    assert fetched == ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    hist = gm.campaign_history(rid, db_path=db_path)[0]
    assert hist["visits_matched"] == 1 and hist["attribution_through"] == "2026-09-18"
    # the next day reads only the day not yet read, and Ana is not counted twice
    fetched.clear()
    res = gm.run_campaign_attribution(db_path=db_path, today=date(2026, 9, 20))
    assert fetched == ["2026-09-19"] and res["visits_matched"] == 0
    assert gm.campaign_history(rid, db_path=db_path)[0]["visits_matched"] == 1


# ── #2 one diagnosis shape, computed not composed ────────────────────────────

def _analysis(**kw):
    base = {"total_sales": 50000, "total_labor_cost": 17500, "overall_labor_pct": 35.0, "labor_target": 30,
            # dow_summary is the real shape analyse_shifts emits: {weekday: float}
            "period_days": 21, "dow_summary": {"Monday": 41.0, "Friday": 27.0},
            "overtime_risk": [{"employee": "Ana", "hours": 44}],
            "role_summary": {"Server": {"labor_cost": 9000, "headcount": 6}, "Cook": {"labor_cost": 8500, "headcount": 4}}}
    base.update(kw)
    return base


def test_labor_diagnosis_names_the_biggest_driver_and_says_how_to_check(db_path):
    import labor
    # pinned against the source, not a fixture: the weekday map really is floats
    shifts = [{"employee": "A", "role": "Server", "date": "2026-09-14", "day": "Mon", "scheduled_hours": 8,
               "actual_hours": 8, "sales": 3000, "shift_start": "10:00", "shift_end": "18:00"}]
    real = labor.analyse_shifts(shifts, hourly_rate=20, labor_target=30)
    assert all(isinstance(v, (int, float)) for v in real["dow_summary"].values())
    d = labor.diagnose(_analysis())
    assert d["available"] and d["confidence"] == "high"
    assert d["cause"].startswith("Mondays run 41.0% labor against the 30% target")
    assert d["alternative_cause"].startswith("Overtime premium: 1 person-week")
    assert "Monday" in d["what_would_confirm"]
    metrics = [e["metric"] for e in d["operational_evidence"]]
    assert "labor % over the period" in metrics and "Monday labor %" in metrics
    # under target: no cause is manufactured
    calm = labor.diagnose(_analysis(overall_labor_pct=28.0, dow_summary={"Monday": 29.0}, overtime_risk=[]))
    assert calm["available"] and calm["cause"] is None and "nothing over target" in calm["summary"]
    # a short period is never high confidence
    assert labor.diagnose(_analysis(period_days=5))["confidence"] == "low"
    assert labor.diagnose({"total_sales": 0})["available"] is False


def test_marketing_diagnosis_compares_only_measured_campaigns(db_path, monkeypatch):
    import guest_marketing as gm
    rows = [
        {"id": 1, "sent_count": 40, "clicks": 8, "visits_matched": 6, "segment": "regulars", "segment_label": "Regulars", "created_at": "2026-09-01 12:00:00"},
        {"id": 2, "sent_count": 50, "clicks": 2, "visits_matched": 1, "segment": "lapsed", "segment_label": "Lapsed", "created_at": "2026-09-08 12:00:00"},
        {"id": 3, "sent_count": 5, "clicks": 5, "visits_matched": 5, "segment": "all", "segment_label": "Everyone", "created_at": "2026-09-10 12:00:00"},
    ]
    monkeypatch.setattr(gm, "campaign_history", lambda rid, limit=20, db_path=None: rows)
    d = gm.diagnose(1)
    assert d["available"] and "Regulars segment answered at 15.0 came back per 100 texts" in d["cause"]
    assert "Lapsed" in d["cause"] and d["confidence"] == "low"          # only two measured
    assert "Send the next campaign to the stronger segment" in d["what_would_confirm"]
    # taps stand in until attribution has run, and the summary says so
    for r in rows:
        r["visits_matched"] = None
    d2 = gm.diagnose(1)
    assert "taps" in d2["summary"] and "Toast check-ins have not been matched yet" in d2["summary"]
    monkeypatch.setattr(gm, "campaign_history", lambda rid, limit=20, db_path=None: rows[:1])
    assert gm.diagnose(1)["available"] is False


# ── #24 what the score hides: each question across the last checks ───────────

def test_query_history_lines_up_each_question_across_runs(db_path):
    models.init_ai_visibility_queries(db_path)
    rid = _rid(db_path)
    models.record_ai_visibility_queries(1, rid, [{"query": "best pizza near me", "kind": "near", "appeared": True},
                                                 {"query": "pizza open late", "kind": "hours", "appeared": False}], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE ai_visibility_query_runs SET created_at='2026-09-01 10:00:00' WHERE run_id=1")
    conn.commit(); conn.close()
    models.record_ai_visibility_queries(2, rid, [{"query": "best pizza near me", "kind": "near", "appeared": False},
                                                 {"query": "family dinner downtown", "kind": "occasion", "appeared": True}], db_path=db_path)
    out = models.ai_visibility_query_history(rid, db_path=db_path)
    assert [r["run_id"] for r in out["runs"]] == [1, 2]
    by = {q["query"]: q for q in out["queries"]}
    assert by["best pizza near me"]["appeared"] == [True, False] and by["best pizza near me"]["appearances"] == 1
    assert by["pizza open late"]["appeared"] == [False, None] and by["pizza open late"]["asked"] == 1
    assert by["family dinner downtown"]["appeared"] == [None, True]
    assert models.ai_visibility_query_history(rid + 9, db_path=db_path) == {"runs": [], "queries": []}


# ── #14 one monthly email per owner ─────────────────────────────────────────

def test_the_monthly_summary_goes_once_per_owner_across_locations(db_path, monkeypatch):
    import scheduler, emails
    a = _rid(db_path, module_labor=1)
    b = create_restaurant(Restaurant(name="Moat Co North", owner_email="M@x.com", module_reviews=1), db_path=db_path)
    c = create_restaurant(Restaurant(name="Other", owner_email="o@y.com", module_reviews=1), db_path=db_path)
    monkeypatch.setattr(scheduler, "local_due", lambda r, hour, claim_key=None, **k: True)
    monkeypatch.setattr(scheduler, "_push_month_ready", lambda r: None)
    singles, groups = [], []
    monkeypatch.setattr(emails, "send_monthly_summary_email", lambda **kw: singles.append(kw["restaurant_id"]))
    monkeypatch.setattr(emails, "send_monthly_group_summary_email", lambda to, owner, rs: groups.append((to, sorted(r.id for r in rs))))
    scheduler.run_monthly_summaries()
    assert singles == [c] and groups == [("m@x.com", sorted([a, b]))]


def test_the_group_email_is_one_message_logged_for_every_location(db_path, monkeypatch):
    import emails
    a = _rid(db_path)
    b = create_restaurant(Restaurant(name="Moat Co North", owner_email="m@x.com", module_reviews=1), db_path=db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "_monthly_review_sections", lambda rid, months=1: [f"<p>section for {rid}</p>"] if rid == a else [])
    delivered = []
    monkeypatch.setattr(emails, "deliver", lambda **kw: delivered.append(kw))
    logged = []
    monkeypatch.setattr(models, "log_email", lambda rid, et, to, subj, db_path=None, **k: logged.append((rid, et)))
    rs = [models.get_restaurant(a, db_path=db_path), models.get_restaurant(b, db_path=db_path)]
    emails.send_monthly_group_summary_email("m@x.com", "Mo Owner", rs)
    assert len(delivered) == 1 and delivered[0]["restaurant_id"] == a
    html = delivered[0]["payload"]["html"]
    assert f"section for {a}" in html and "Moat Co North" in html and "Not enough measured data" in html
    assert "across your 2 locations" in delivered[0]["payload"]["subject"]
    assert logged == [(b, "send_monthly_summary_email")]
    # a single restaurant never goes through the group path
    delivered.clear()
    emails.send_monthly_group_summary_email("m@x.com", "Mo", rs[:1])
    assert delivered == []


def test_the_labor_insight_routes_carry_the_diagnosis_and_stay_registered(db_path):
    """The helper was once inserted between the route decorators and the
    view, which re-pointed /api/labor-insight at the helper. Pinned here."""
    import client_api, mobile_api
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    by_rule = {r.rule: r.endpoint for r in app.url_map.iter_rules()}
    assert by_rule["/api/labor-insight"] == "client.labor_insight_api"
    assert callable(client_api._labor_diagnosis_safe) and not hasattr(client_api._labor_diagnosis_safe, "__wrapped__")
