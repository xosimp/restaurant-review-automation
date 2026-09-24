"""Re-audit group C — recommendation coverage and delivery (C1–C15, K5, K6).

The rule these pin: every recommendation SHOWN is recorded in rec_ledger
once per surface per day at actual DELIVERY — only the keys a surface
rendered or sent, only when the send succeeded (a push: when a phone took
it), never when something was built — and every payload carrying one says
whether it can be answered. Each test failed against the code before the
fix it names. No model, network, email, SMS or push is reached.
"""
import json
import sqlite3
from datetime import date, datetime

import pytest
from flask import Flask

import auth
import business_intelligence as bi
import client_api
import emails
import mobile_api
import models
import morning_brief
import rec_delivery
import rec_ledger
import strategy_routes
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import action_queue, admin_routes, intraday, push, strategy_jobs, webhooks
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (morning_brief, bi, push, action_queue, strategy_jobs, intraday, admin_routes, auth, webhooks):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    client_api._insight_cache.clear()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called")))
    yield
    client_api._insight_cache.clear()


def _rid(db_path, **kw):
    fields = dict(name="Gia Mia", owner_email="o@x.test", owner_name="Sam Owner", module_reviews=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _user(rid, uid=7, role="owner"):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": role,
            "is_admin": 0, "email": "o@x.test"}


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _shown(db_path, rid, surface=None):
    q = "SELECT key, surface FROM rec_events WHERE restaurant_id=? AND event='shown'"
    args = [rid]
    if surface:
        q += " AND surface=?"
        args.append(surface)
    return sorted((r["key"], r["surface"]) for r in _q(db_path, q, args))


def _events(db_path, rid, event):
    return _q(db_path, "SELECT key, surface, user_id, restaurant_id FROM rec_events WHERE restaurant_id=? "
                       "AND event=?", (rid, event))


class _Sent:
    def __init__(self, ok=True):
        self.ok, self.error, self.status_code, self.attempts = ok, (None if ok else "boom"), None, 1


class _RunNow:
    """A push pool that runs each delivery at once, in the caller's thread."""
    def submit(self, fn, *a):
        fn(*a)


def _apns(monkeypatch, ok=True):
    """Every APNs call answers `ok`; returns the (alert_type, title, body) sent."""
    import push
    sent = []
    monkeypatch.setattr(push, "_push_executor", lambda: _RunNow())

    def _deliver(token_row, alert_type, title, body, data, db_path=None):
        sent.append((alert_type, title, body))
        return {"ok": ok, "status": 200 if ok else 410, "attempts": 1, "error": None if ok else "Unregistered"}
    monkeypatch.setattr(push, "_deliver", _deliver)
    return sent


def _device(db_path, rid, uid, tok="tok"):
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO device_tokens (restaurant_id, user_id, apns_token) VALUES (?,?,?)", (rid, uid, tok * 20))
    c.commit(); c.close()


@pytest.fixture
def http(monkeypatch, db_path):
    rid = _rid(db_path, module_labor=1, module_marketing=1, module_inventory=1)
    user = _user(rid)
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.add_template_filter(lambda d: "9/21/26", "format_date")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    app.register_blueprint(strategy_routes.issue_link_bp)
    c = app.test_client()
    c.rid, c.user = rid, user
    return c


_BEARER = {"Authorization": "Bearer t"}


# ── C1 schedule quality recommendations: shown when served, not when built ──

def _quality_stub(monkeypatch, recs):
    import schedule_engine as se
    import shift_quality as sq
    monkeypatch.setattr(se, "_quality_signals", lambda r, result, **extra: ({}, None))
    monkeypatch.setattr(sq, "score_rows", lambda rows, **k: {"checked": False, "recommendations": list(recs)})


_ROW = {"date": "2026-09-25", "day": "Friday", "employee": "Ana", "role": "Server",
        "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": 6}


def test_c1_scoring_a_schedule_records_nothing(db_path, monkeypatch):
    """The nightly auto-draft scores with nobody looking: no ledger row and no
    schedule_intel showing (which decides which kinds go quiet)."""
    import schedule_engine as se
    rid = _rid(db_path)
    _quality_stub(monkeypatch, ["Fill the gap on Friday night with a second server."])
    quality, _w = se._score_schedule_quality(rid, [dict(_ROW)], {})
    assert quality["recommendation_items"][0]["key"].startswith("schedule_coverage:")
    assert _shown(db_path, rid) == []
    assert _q(db_path, "SELECT * FROM schedule_recommendation_events") == []


def test_c1_the_status_poll_presents_the_recommendations_it_serves(db_path, monkeypatch):
    import schedule_engine as se
    rid = _rid(db_path)
    _quality_stub(monkeypatch, ["Fill the gap on Friday night with a second server.",
                                "Pair Ana with Ben on Saturday night."])
    quality, _w = se._score_schedule_quality(rid, [dict(_ROW)], {})
    result = client_api.present_schedule_result(rid, {"quality": quality}, user_id=7)
    items = result["quality"]["recommendation_items"]
    assert [k for k, _ in _shown(db_path, rid, "schedule_review")] == sorted(i["key"] for i in items)
    assert all(i["rec_key"] == i["key"] and i["answerable"] is True for i in items)
    assert {r["user_id"] for r in _events(db_path, rid, "shown")} == {7}
    assert len(_q(db_path, "SELECT * FROM schedule_recommendation_events WHERE action='shown'")) == 2


def test_c1_a_rescore_and_a_reopened_week_present_what_they_serve(http, db_path, monkeypatch):
    import labor
    import schedule_engine as se
    _quality_stub(monkeypatch, ["Fill the gap on Friday night with a second server."])
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {"is_live": True})
    monkeypatch.setattr(se, "quality_inputs_from_db", lambda *a, **k: {})
    body = http.post("/api/labor/schedule/score", json={"rows": [dict(_ROW)]}).get_json()
    assert body["ok"] and body["quality"]["recommendation_items"][0]["answerable"] is True
    assert len(_shown(db_path, http.rid, "schedule_review")) == 1
    # A stored week reopened (the history detail both clients load).
    quality, _w = se._score_schedule_quality(http.rid, [dict(_ROW)], {})
    quality["recommendation_items"][0]["key"] = "schedule_hours:trim about 12h"
    hid = models.save_schedule_history(http.rid, "2026-09-21", "2026-09-27", 40, 40, 30,
                                       "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n",
                                       [], quality=quality, db_path=db_path)
    assert http.get(f"/mobile/api/labor/schedule-history/{hid}", headers=_BEARER).get_json()["ok"]
    assert ("schedule_hours:trim about 12h", "schedule_review") in _shown(db_path, http.rid)


def test_c1_both_clients_show_at_most_five_and_that_is_what_is_served(db_path, monkeypatch):
    import schedule_engine as se
    rid = _rid(db_path)
    _quality_stub(monkeypatch, [f"Trim about {n}h from the Tuesday lunch." for n in range(2, 8)])
    quality, _w = se._score_schedule_quality(rid, [dict(_ROW)], {})
    assert len(quality["recommendations"]) == se.SHOWN_RECOMMENDATIONS == 5
    assert len(quality["recommendation_items"]) == 5


# ── C2 push-only jobs: nobody's phone, nothing sent and nothing recorded ────

def _job_world(db_path, monkeypatch):
    import notify
    from auth import create_user
    rid = _rid(db_path, module_labor=1, module_marketing=1)
    uid = create_user(rid, "owner1", "o@x.test", "pw-Very-Long-123!", db_path=db_path, role="owner")
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    return rid, uid


def test_c2_a_pulse_with_no_deliverable_phone_is_not_sent(db_path, monkeypatch):
    import intraday, push, scheduler, strategy_jobs
    rid, uid = _job_world(db_path, monkeypatch)
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(intraday, "pulse", lambda *a, **k: {
        "available": True, "off": True, "direction": "behind", "pct": -25.0, "net_sales": 900.0,
        "typical": 1200.0, "hour": 16, "samples": 6, "weekday": "Wednesday"})
    monkeypatch.setattr(strategy_jobs, "staffing_move", lambda *a, **k: {
        "text": "Letting Ana go saves 2h.", "dollars": 30, "key": "pulse_cut:2026-09-23:ana"})
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append(k))
    assert strategy_jobs.run_pre_dinner_pulse(db_path=db_path) == {"sent": 0}
    assert fired == [] and _shown(db_path, rid) == []
    # With a phone: sent, and the staffing move says it can't be answered there.
    _device(db_path, rid, uid)
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    assert strategy_jobs.run_pre_dinner_pulse(db_path=db_path) == {"sent": 1}
    data = fired[0]["data"]
    assert data["answerable"] is False and "rec_key" not in data
    assert data["staffing_move"]["answerable"] is False and fired[0]["user_ids"] == {uid}


def test_c2_a_quiet_night_with_no_deliverable_phone_keeps_its_week(db_path, monkeypatch):
    import demand, ops, push, strategy_jobs, time_utils
    rid, uid = _job_world(db_path, monkeypatch)
    monkeypatch.setattr(demand, "quiet_night_ahead", lambda *a, **k: {
        "available": True, "date": "2026-09-25", "weekday": "Thursday", "typical_sales": 1200.0,
        "below_average_pct": 22.0, "samples": 8})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 23, 11, 0))
    monkeypatch.setattr(strategy_jobs, "_draft_quiet_night_fill", lambda *a, **k: {})
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append(k))
    assert strategy_jobs.run_demand_opportunity(db_path=db_path) == {"sent": 0}
    assert fired == [] and not ops.period_claimed(f"demand_opportunity:{rid}", "2026-W39")
    _device(db_path, rid, uid)
    assert strategy_jobs.run_demand_opportunity(db_path=db_path) == {"sent": 1}
    assert fired[0]["data"]["answerable"] is False and "rec_key" not in fired[0]["data"]
    assert _shown(db_path, rid) == []


def test_c2_the_delivered_hook_runs_once_and_only_when_a_phone_took_it(db_path, monkeypatch):
    import push
    rid, uid = _job_world(db_path, monkeypatch)
    _device(db_path, rid, uid, "a")
    _device(db_path, rid, uid, "b")
    calls = []
    _apns(monkeypatch, ok=False)
    assert push.fire_push(rid, "labor_over", "t", "b", db_path=db_path, on_delivered=lambda: calls.append(1)) == 2
    assert calls == []
    _apns(monkeypatch, ok=True)
    assert push.fire_push(rid, "labor_over", "t", "b", db_path=db_path, on_delivered=lambda: calls.append(1)) == 2
    assert calls == [1], "two phones took it: shown once"
    assert push.fire_push(rid, "labor_over", "t", "b", db_path=db_path, user_ids={999}) == 0


def test_c2_an_alert_push_is_presented_only_when_delivered(db_path, monkeypatch):
    import notify
    rid, uid = _job_world(db_path, monkeypatch)
    _device(db_path, rid, uid)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE restaurants SET urgent_via_sms=0, urgent_via_email=0 WHERE id=?", (rid,))
    c.commit(); c.close()
    _apns(monkeypatch, ok=False)
    notify.deliver_alert(rid, "labor_over", "Labor 34%", "Labor over target", "<p>x</p>", db_path=db_path,
                         recs=[notify.alert_rec("labor_over", subject="2026-09-14")])
    assert _shown(db_path, rid) == []
    _apns(monkeypatch, ok=True)
    notify.deliver_alert(rid, "labor_over", "Labor 34%", "Labor over target", "<p>x</p>", db_path=db_path,
                         recs=[notify.alert_rec("labor_over", subject="2026-09-15")])
    assert _shown(db_path, rid) == [("labor_over:2026-09-15", "alert_push")]


# ── C3 a coverage issue's covers: shown where they are rendered ─────────────

def _coverage_issue(db_path, rid, covers=("Ana", "Bo", "Cy")):
    import issues
    issue, _ = issues.create_issue(rid, "coverage", "Ben hasn't clocked in", severity="high",
                                   source_key="coverage:2026-09-24:ben", notify=False,
                                   meta={"missing": "Ben", "role": "Server", "shift_start": "17:00",
                                         "covers": [{"name": n, "score": 1.0} for n in covers]},
                                   db_path=db_path)
    return issue


def test_c3_filing_a_coverage_issue_presents_nothing(db_path, monkeypatch):
    import intraday, issues, labor_replacements, notify, strategy_jobs
    rid = _rid(db_path, module_labor=1)
    c = sqlite3.connect(db_path)
    cid = c.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                    (rid, "Mgr", "+15555550100")).lastrowid
    c.commit(); c.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    monkeypatch.setattr(strategy_jobs, "_open_now", lambda r, local: True)
    monkeypatch.setattr(intraday, "coverage_gaps", lambda *a, **k: {
        "available": True, "scheduled_rows": [], "arrived_keys": [],
        "missing": [{"employee": "Ben", "role": "Server", "shift_start": "17:00", "minutes_late": 20}]})
    monkeypatch.setattr(issues, "resolve_coverage", lambda *a, **k: None)
    monkeypatch.setattr(labor_replacements, "for_gap", lambda *a, **k: [{"name": "Ana", "score": 0.9}])
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: False)            # Twilio refuses
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    assert _q(db_path, "SELECT * FROM rec_events WHERE restaurant_id=?", (rid,)) == []


def test_c3_homes_issue_list_presents_the_covers_it_shows(http, db_path):
    import intraday
    issue = _coverage_issue(db_path, http.rid)
    web = http.get("/api/issues?status=unresolved").get_json()
    row = web["issues"][0]
    assert row["cover_rec_key"] == intraday.cover_key(issue) == "cover:2026-09-24:ben"
    assert row["cover_answerable"] is True
    assert _shown(db_path, http.rid) == [("cover:2026-09-24:ben", "home")]
    # Every other status reads the list without showing covers.
    http.get("/api/issues?status=all")
    assert len(_events(db_path, http.rid, "shown")) == 1


def test_c3_the_issue_page_presents_its_covers_only_when_a_person_acts(http, db_path):
    import issues
    issue = _coverage_issue(db_path, http.rid)
    c = sqlite3.connect(db_path)
    cid = c.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                    (http.rid, "Mgr", "+15555550100")).lastrowid
    c.commit(); c.close()
    token = issues._mint_link(issue["id"], cid, db_path=db_path)
    assert http.get(f"/i/{token}").status_code == 200          # a link preview: no side effect
    assert _shown(db_path, http.rid) == []
    http.post(f"/i/{token}", data={"action": "ack"})
    assert _shown(db_path, http.rid) == [("cover:2026-09-24:ben", "issue_sms")]


# ── C4 what is recorded is what was delivered ───────────────────────────────

_LINES = [
    {"key": "schedule", "tone": "action", "rec": "schedule:next-week", "text": "Next week's schedule isn't built yet.",
     "ask": "a"},
    {"key": "stock", "tone": "bad", "rec": "stock_low:Salmon", "text": "Salmon is below par.", "ask": "b"},
    {"key": "slow_day", "tone": "neutral", "rec": "slow_day:Tuesday", "text": "Tuesdays run 20% under.", "ask": "c"},
    {"key": "reviews", "tone": "good", "rec": "top_issue:service", "text": "Service complaints fell.", "ask": "d"},
]


def test_c4_the_brief_push_presents_the_two_lines_it_shows_once_delivered(db_path, monkeypatch):
    rid, uid = _job_world(db_path, monkeypatch)
    _device(db_path, rid, uid)
    monkeypatch.setattr(morning_brief, "build", lambda *a, **k: {"restaurant_id": rid, "date": "2026-09-23",
                                                                 "lines": [dict(l) for l in _LINES]})
    sent = _apns(monkeypatch, ok=False)
    morning_brief.deliver(rid, db_path=db_path)
    assert len(sent) == 1 and _shown(db_path, rid) == [], "a push no phone took showed nothing"
    sent = _apns(monkeypatch, ok=True)
    morning_brief.deliver(rid, db_path=db_path)
    body = sent[0][2]
    assert "Next week's schedule" in body and "Salmon" in body and "Tuesdays" not in body
    assert _shown(db_path, rid) == [("schedule:next-week", "brief_push"), ("stock_low:Salmon", "brief_push")]


def test_c4_the_running_low_line_presents_only_the_items_it_names(db_path, monkeypatch):
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(morning_brief, "_critical_low", lambda *a, **k: ["Salmon", "Chicken", "Basil", "Cream",
                                                                          "Lemons", "Flour"])
    b = morning_brief.build(rid, viewer=None, db_path=db_path)
    line = next(l for l in b["lines"] if l["key"] == "stock")
    assert line["text"] == "Running low: Salmon, Chicken, Basil and 3 more."
    assert line["rec_keys"] == ["stock_low:Salmon", "stock_low:Chicken", "stock_low:Basil"]
    morning_brief._present(rid, {"lines": [line]}, "brief_email", db_path=db_path)
    assert [k for k, _ in _shown(db_path, rid)] == sorted(line["rec_keys"])


def test_c4_the_combined_morning_text_presents_only_the_item_it_names(db_path, monkeypatch):
    import notify
    rid, uid = _job_world(db_path, monkeypatch)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE restaurants SET urgent_via_sms=1, urgent_via_email=1 WHERE id=?", (rid,))
    c.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
              (rid, "Own", "+15555550100"))
    c.commit(); c.close()
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    monkeypatch.setattr(notify, "_daily_alert_suppressed", lambda *a, **k: False)
    sms = []
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, use_case="alert": (sms.append(msg), True)[1])
    monkeypatch.setattr(notify, "_email_alert", lambda *a, **k: True)
    notify.begin_daily_batch()
    notify.raise_alert(rid, "labor_over", "Labor 34%", "Labor over target", lines=["Labor ran 34%."],
                       recs=[notify.alert_rec("labor_over", subject="2026-09-14")], db_path=db_path)
    notify.raise_alert(rid, "food_waste", "Waste $620", "Food waste up", lines=["Waste hit $620."], db_path=db_path)
    notify.flush_daily_batch(db_path=db_path)
    assert len(sms) == 1 and "First: Waste hit $620." in sms[0]
    assert _shown(db_path, rid, "alert_sms") == [("food_waste", "alert_sms")]
    assert _shown(db_path, rid, "alert_email") == [("food_waste", "alert_email"),
                                                   ("labor_over:2026-09-14", "alert_email")]


def test_c4_homes_critically_low_card_presents_the_items_it_names(db_path, monkeypatch):
    import home_brief, inventory
    rid = _rid(db_path, module_inventory=1)
    crit = [{"item": n} for n in ("Salmon", "Chicken", "Basil", "Cream", "Lemons", "Flour")]
    monkeypatch.setattr(inventory, "analysis_for", lambda *a, **k: (
        [{"item": "x"}], True, {"critical_low": crit, "reorder_soon": [], "waste_items": []}))
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    card = next(a for a in p["attention"] if a["key"] == "critical_low")
    assert len(card["rec_keys"]) == home_brief.CRITICAL_LOW_NAMED == 4
    assert not [k for k, _ in _shown(db_path, rid, "home") if k in ("stock_low:Lemons", "stock_low:Flour")]


def test_c4_a_reviews_read_presents_the_one_diagnosis_both_clients_render(http, db_path):
    diags = [{"category": c, "cause": f"cause {c}", "recommended_action": f"Do the {c} thing",
              "confidence": "medium"} for c in ("slow_service", "food_quality", "cleanliness")]
    client_api._cache_set("review-insight:" + str(http.rid), {
        "insight": "This week: 4.1 stars.\nDo today: Brief the line on ticket times.",
        "figures_verified": True, "names_verified": True, "diagnoses": diags, "diagnosis": diags[0]})
    web = http.get("/api/review-insight").get_json()
    shown = [k for k, _ in _shown(db_path, http.rid) if k.startswith("diag_review:")]
    assert shown == ["diag_review:slow_service"]
    assert web["diagnosis"]["answerable"] is True and web["diagnoses"][1]["rec_key"] == "diag_review:food_quality"
    assert web["recs"][0]["rec_key"] == web["recs"][0]["key"] and web["recs"][0]["answerable"] is True


def test_c4_the_phone_calendar_presents_the_day_it_shows(http, db_path, monkeypatch):
    import marketing, time_utils
    ideas = [{"day": d, "angle": f"{d} idea", "type": "instagram_post", "iso_date": f"2026-09-{21 + i}"}
             for i, d in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday"))]
    monkeypatch.setattr(marketing, "get_cached_calendar", lambda *a, **k: [dict(i) for i in ideas])
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda rid, naive=False: datetime(2026, 9, 23, 9, 0))
    phone = http.get("/mobile/api/marketing", headers=_BEARER).get_json()
    keys = [i["rec_key"] for i in phone["calendar"]]
    assert all(i["answerable"] for i in phone["calendar"])
    assert _shown(db_path, http.rid, "marketing") == [(keys[2], "marketing")], "Wednesday: today's card"
    seen = http.post("/mobile/api/marketing/calendar/seen", json={"rec_key": keys[0]}, headers=_BEARER)
    assert seen.status_code == 200 and seen.get_json()["answerable"] is True
    assert sorted(k for k, _ in _shown(db_path, http.rid, "marketing")) == sorted([keys[0], keys[2]])
    assert http.post("/mobile/api/marketing/calendar/seen", json={"rec_key": "content_idea:nope"},
                     headers=_BEARER).status_code == 404


# ── C5 / K5 the modules grid and Ask's opening build Home without recording ──

def test_c5_the_modules_grid_records_nothing(http, db_path, monkeypatch):
    import home_brief
    monkeypatch.setattr(home_brief, "build_home_brief",
                        lambda *a, **k: pytest.fail("the modules grid must not build Home"))
    for path, headers in (("/mobile/api/home/modules", _BEARER), ("/api/home/modules", {})):
        body = http.get(path, headers=headers).get_json()
        assert body["ok"] and {m["key"] for m in body["modules"]} >= {"reviews", "labor"}
    assert _q(db_path, "SELECT * FROM rec_events") == []


def test_c5_asks_opening_fallback_builds_home_without_presenting(db_path, monkeypatch):
    import home_brief
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(morning_brief, "build", lambda *a, **k: {"lines": []})
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/ask-cavnar/opening"):
        resp = mobile_api.mobile_ask_opening.__wrapped__(current_user=_user(rid))
    assert resp[0].get_json()["ok"] and _shown(db_path, rid) == []
    # ...and it cached nothing, so the next real Home still presents.
    p, _ = home_brief.build_home_brief(_user(rid))
    assert p["ok"] and _shown(db_path, rid, "home")


# ── C6 one Labor read for both twins ────────────────────────────────────────

def test_c6_the_web_and_the_phone_share_one_labor_read(http, db_path, monkeypatch):
    import labor
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {
        "is_live": True, "overall_labor_pct": 34.0, "labor_target": 30, "dow_summary": {}})
    reads = iter(["Labor ran hot.\n\nRecommendations:\n1. Trim one server from Tuesday dinner.\n"
                  "2. Move Ana's Saturday close to Friday.",
                  "Labor ran hot.\n\nRecommendations:\n1. Send one host home early on Tuesdays.\n"
                  "2. Shift Ana's close to Thursday."])
    monkeypatch.setattr(labor, "labor_note", lambda *a, **k: next(reads))
    web = http.get("/api/labor-insight").get_json()
    phone = http.get("/mobile/api/labor/insight", headers=_BEARER).get_json()
    assert phone["insight_rec_keys"] == [i["rec_key"] for i in web["rec_items"]]
    assert not _q(db_path, "SELECT * FROM rec_instances WHERE status='superseded'")


# ── C7 the queue's tasks are not recommendations ────────────────────────────

def test_c7_a_teammates_request_is_listed_but_never_presented(db_path):
    import action_queue
    rid = _rid(db_path, module_labor=1)
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, kind, "
              "status) VALUES (?,1,'Ben','2099-01-01','17:00','drop','pending')", (rid,))
    c.commit(); c.close()
    out = action_queue.items(rid, db_path=db_path, today=date(2026, 9, 24))
    task = next(i for i in out["items"] if i["key"].startswith("shift_request:"))
    rec = next(i for i in out["items"] if i["key"] == "schedule:next-week")
    assert task["answerable"] is False and "rec_key" not in task
    assert rec["rec_key"] == "schedule:next-week" and rec["answerable"] is True
    assert _shown(db_path, rid) == [("schedule:next-week", "queue")]
    action_queue.snooze(rid, task["key"], db_path=db_path)
    assert not _q(db_path, "SELECT * FROM rec_events WHERE key=?", (task["key"],))
    assert action_queue.is_task(task["key"]) and not action_queue.is_task("schedule:next-week")


def test_c7_building_the_week_implements_next_weeks_schedule(db_path):
    import schedule_engine as se
    rid = _rid(db_path, module_labor=1)
    rec_ledger.present(rid, "schedule:next-week", "labor", "brief_email", db_path=db_path)
    assert se.mark_next_week_built(rid, 41) == 1
    assert se.mark_next_week_built(rid, 41) == 0, "once per generation"
    assert _q(db_path, "SELECT status FROM rec_instances WHERE key='schedule:next-week'")[0]["status"] == "implemented"


# ── C8 keys nothing can answer are not presented ────────────────────────────

def test_c8_email_and_push_only_keys_are_not_presented_or_linked():
    for key in ("pulse_cut:2026-09-23:ana", "quiet_night:2026-09-25", "digest_move:ab12", "monthly_move:first_post"):
        assert not rec_delivery.presentable(key) and not rec_delivery.answerable(key)
        assert rec_delivery.link("https://x.test/", key, "digest") == "https://x.test/"
    with rec_delivery.collect() as shown:
        rec_delivery.stage(1, "monthly_email", [{"key": "monthly_move:approve_replies"}])
    assert shown.groups == []


# ── C9 / K6 Home's "Before service" lines ───────────────────────────────────

def test_k6_the_brief_served_to_home_records_its_lines(http, db_path, monkeypatch):
    monkeypatch.setattr(morning_brief, "build", lambda *a, **k: {"restaurant_id": http.rid, "date": "2026-09-24",
                                                                 "lines": [dict(l, rec_key=l["rec"], answerable=True)
                                                                           for l in _LINES] + [
        {"key": "fix_first", "tone": "action", "rec": "cut_waste:Salmon", "text": "If you only do one thing",
         "ask": "x"},
        {"key": "money", "tone": "neutral", "rec": "money:food_cost", "text": "Biggest", "ask": "y"}]})
    body = http.get("/api/morning-brief").get_json()
    assert body["ok"] and _shown(db_path, http.rid) == [], "a settings read shows no lines"
    body = http.get("/api/morning-brief?view=home").get_json()
    assert all(l["answerable"] for l in body["brief"]["lines"] if l.get("rec_key"))
    assert [k for k, _ in _shown(db_path, http.rid, "home")] == sorted(l["rec"] for l in _LINES)
    http.get("/mobile/api/morning-brief?view=home", headers=_BEARER)
    assert len(_events(db_path, http.rid, "shown")) == 4, "once a day"
    assert {r["user_id"] for r in _events(db_path, http.rid, "shown")} == {7}


# ── C10 a keyed link survives Google sign-in and lands on its own location ──

class _Resp:
    def __init__(self, p):
        self._p, self.ok, self.status_code = p, True, 200

    def json(self):
        return self._p


def test_c10_google_sign_in_keeps_where_the_link_was_going(db_path, monkeypatch):
    import auth_routes, requests
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_ID", "cid")
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    rid = _rid(db_path, owner_email="owner@x.test")
    auth.create_user(rid, "owner", "owner@x.test", "ownerpass1", db_path=db_path)
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "t"
    app.register_blueprint(auth_routes.auth_bp)
    client = app.test_client()
    nxt = "/?ask=Walk me through this&rec=trim_day:Friday&src=weekly_email"
    page = client.get("/login", query_string={"next": nxt}).get_data(as_text=True)
    assert 'href="/auth/google-sso?next=/%3Fask%3DWalk%20me%20through%20this%26rec%3Dtrim_day%3AFriday' in page
    start = client.get("/auth/google-sso", query_string={"next": nxt})
    assert start.status_code == 302
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp({"access_token": "at"}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"email": "owner@x.test", "id": "g-1",
                                                                "verified_email": True}))
    client.set_cookie("g_sso_state", "st8")
    cb = client.get("/auth/google-sso/callback?state=st8&code=c0de")
    from urllib.parse import unquote
    assert cb.status_code == 302 and unquote(cb.headers["Location"]) == nxt
    # Somewhere else entirely is never a destination.
    client.get("/auth/google-sso", query_string={"next": "https://evil.example/"})
    client.set_cookie("g_sso_state", "st9")
    assert client.get("/auth/google-sso/callback?state=st9&code=c0de").headers["Location"] == "/"


def test_c10_an_open_lands_on_the_location_the_link_names(db_path):
    a = _rid(db_path, name="A", location_group="G")
    b = _rid(db_path, name="B", location_group="G")
    other = _rid(db_path, name="Z", owner_email="z@x.test")
    rec_ledger.present(a, "negative_trend", "reviews", "alert_email", db_path=db_path)
    rec_ledger.present(b, "negative_trend", "reviews", "home", db_path=db_path)
    rec_ledger.present(other, "negative_trend", "reviews", "alert_email", db_path=db_path)
    url = rec_delivery.link("https://x.test/?tab=reviews", "negative_trend", "alert_email", rid=a)
    assert url.endswith(f"&src=alert_email&rid={a}")
    owner_on_b = dict(_user(b), base_restaurant_id=a)
    assert rec_delivery.record_link_open(owner_on_b, {"rec": "negative_trend", "src": "alert_email", "rid": str(a)})
    assert [e["restaurant_id"] for e in _q(db_path, "SELECT restaurant_id FROM rec_events WHERE event='opened'")] == [a]
    # A location this login cannot see records nothing anywhere.
    assert not rec_delivery.record_link_open(owner_on_b, {"rec": "negative_trend", "src": "alert_email",
                                                          "rid": str(other)})
    assert len(_q(db_path, "SELECT * FROM rec_events WHERE event='opened'")) == 1


# ── C11 reply-edit learning measures the owner's edits, and only theirs ─────

_DRAFT = "Thank you so much for coming in! We loved having you and hope to see you again soon! Cheers!"


def _review(db_path, rid, ext, draft=_DRAFT):
    c = sqlite3.connect(db_path)
    i = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, response_status, draft_response) VALUES (?, 'yelp', ?, 'A', 5, 'Good', '2026-09-01', "
                  "'2026-09-01', 'drafted', ?)", (rid, ext, draft)).lastrowid
    c.commit(); c.close()
    return i


def _edit_row(db_path, i):
    return _q(db_path, "SELECT draft_edited, edit_category, edit_signals FROM reviews WHERE id=?", (i,))[0]


def test_c11_a_punctuation_only_edit_is_an_edit():
    import reply_edits
    out = reply_edits.compare(_DRAFT, _DRAFT.replace("!", "."))
    assert out["category"] == "light" and out["signals"] == ["removed_exclamations"]
    assert reply_edits.compare(_DRAFT, _DRAFT.replace(" ", "  "))["category"] == "unchanged"


def test_c11_whitespace_regenerate_and_asks_rewrite_are_not_the_owners_edits(db_path, monkeypatch):
    import ask_cavnar_tools, drafter
    rid = _rid(db_path)
    ws = _review(db_path, rid, "ws")
    client_api._do_save_draft(ws, rid, _DRAFT.replace(" ", "  ", 1))
    assert _edit_row(db_path, ws)["draft_edited"] == 0
    rg = _review(db_path, rid, "rg")
    client_api._do_save_draft(rg, rid, "Owner tweak of the first draft.")
    monkeypatch.setattr(drafter, "draft_response", lambda review_id, *a, **k: models.update_draft(
        review_id, "A fresh model draft for this guest, thank you for visiting us."))
    assert client_api._do_regenerate_draft(rg, rid)[0]["ok"]
    models.record_reply_edit(rg, rid)
    assert _edit_row(db_path, rg)["edit_category"] == "unchanged", "approved the new draft as written"
    ak = _review(db_path, rid, "ak")
    assert ask_cavnar_tools._edit_review_reply(rid, review_id=ak, draft="MODEL REWRITE: see you soon.")["ok"]
    models.record_reply_edit(ak, rid)
    assert _edit_row(db_path, ak)["edit_category"] == "unchanged" and _edit_row(db_path, ak)["draft_edited"] == 0


def test_c11_the_examples_block_and_a_saved_reply_are_bounded(db_path):
    import drafter
    rows = [{"rating": 5, "review": "Great", "response": "word " * 3000} for _ in range(4)]
    block = drafter._format_examples(rows)
    assert len(block) <= drafter.EXAMPLES_BLOCK_CHARS + 200
    rid = _rid(db_path)
    i = _review(db_path, rid, "big")
    assert client_api._do_save_draft(i, rid, "x " * 30000)[0]["ok"] is False


# ── C12 / C13 the periodic emails ───────────────────────────────────────────

FIX_FIRST = {"key": "cut_waste:Salmon", "what": "Cut the salmon order", "why": "salmon waste",
             "modules": ["food_cost"], "dollars_monthly": 312.0, "same_as": "money:food_cost"}
PRIORITIES = [{"key": "money:labor", "label": "Scheduling against target", "monthly": 1864.0},
              {"key": "money:food_cost", "label": "Food cost drivers", "monthly": 742.0},
              {"key": "money:rating", "label": "Rating movement", "is_range": True, "monthly_low": 600.0,
               "monthly_high": None}]


def _monthly(monkeypatch, months=1, metrics=()):
    import monthly_review
    monkeypatch.setattr(monthly_review, "build", lambda *a, **k: {
        "month": "August 2026", "months": months, "metrics": list(metrics), "results": [], "goals": [],
        "compared_with": "July", "priorities": [dict(p) for p in PRIORITIES], "fix_first": dict(FIX_FIRST)})


def test_c12_the_one_things_priority_is_not_listed_again_and_a_missing_end_does_not_crash(db_path, monkeypatch):
    rid = _rid(db_path)
    _monthly(monkeypatch)
    html = "".join(emails._monthly_review_sections(rid))
    assert "Cut the salmon order" in html and "Food cost drivers" not in html
    assert "Scheduling against target — $1,864/month" in html and "Rating movement — $600/month" in html
    _monthly(monkeypatch, months=3)
    html = "".join(emails._monthly_review_sections(rid, months=3))
    assert "Worth your time next quarter" in html and "next month" not in html


def test_c12_a_loss_is_costing_not_worth(monkeypatch):
    import monthly_review, weekly_review
    m = {"label": "Sales per day", "value": 5444, "previous": 5744, "unit": "$", "verdict": "worsened",
         "monthly_dollars": -9082.0}
    week = weekly_review.lines({"metrics": [dict(m)], "compared_with": "last week"})[0]
    month = monthly_review.lines({"metrics": [dict(m)], "compared_with": "July"})[0]
    for line in (week, month):
        assert "costing about $9,082/month" in line and "worth" not in line
    assert "worth about" in weekly_review.lines({"metrics": [dict(m, verdict="improved")],
                                                 "compared_with": "x"})[0]


def test_c12_names_are_escaped_and_a_groups_locations_told_apart(db_path, monkeypatch):
    import html as _h
    rid = _rid(db_path, name="Rosa & Sons <Trattoria>")
    _monthly(monkeypatch)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "generate_email_personalization", lambda ctx, fallback, **k: fallback)
    monkeypatch.setattr(emails, "restaurant_usage", lambda rid: {"owns": {"reviews": True}, "used": {}})
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda **k: sent.append(k) or _Sent(True))
    emails.send_monthly_summary_email("o@x.test", "Rosa & Sons <Trattoria>", "Sam", restaurant_id=rid)
    html = sent[-1]["payload"]["html"]
    assert "<Trattoria>" not in html and _h.escape("Rosa & Sons <Trattoria>") in html
    a = models.get_restaurant(_rid(db_path, name="Gia Mia", location_group="G"))
    b = models.get_restaurant(_rid(db_path, name="Gia Mia", location_group="G"))
    models.update_restaurant(b.id, {"location_name": "Lincoln Park"})
    b = models.get_restaurant(b.id)
    emails.send_monthly_group_summary_email("o@x.test", "Sam", [a, b])
    html = sent[-1]["payload"]["html"]
    assert ">Gia Mia<" in html.replace("\n", "") or "Gia Mia" in html
    assert "Lincoln Park" in html
    assert emails.location_labels([a, a.__class__(**{**a.__dict__, "id": 999})]) == {a.id: "Gia Mia (1)",
                                                                                     999: "Gia Mia (2)"}


def test_c12_the_digest_header_dates_read_mdy(db_path, monkeypatch):
    import reporter, time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: datetime(2026, 9, 14, 9))
    src = open("reporter.py").read()
    assert 'strftime("Week of %B %-d, %Y")' not in src
    assert "Week of " + time_utils.mdy(date(2026, 9, 14)) == "Week of 9/14/26"


def test_c12_the_reach_email_escapes_the_location_name(db_path, monkeypatch):
    import strategy_jobs
    rid, uid = _job_world(db_path, monkeypatch)
    models.update_restaurant(rid, {"name": "Rosa & Sons <Trattoria>"})
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda **k: sent.append(k) or _Sent(True))
    assert strategy_jobs._reach(rid, "labor_reminder", "Waiting on you", "Two requests.", {}, db_path) == 1
    assert "<Trattoria>" not in sent[0]["payload"]["html"]


def test_c13_the_one_thing_that_is_this_weeks_link_is_said_once(db_path, monkeypatch):
    import weekly_review
    rid = _rid(db_path)
    link = {"kind": "reviews_x_labor", "subject": "service:friday", "headline": "Friday complaints, lean Fridays",
            "evidence": ["4 of 6"], "modules": ["reviews", "labor"]}
    monkeypatch.setattr(weekly_review, "build", lambda *a, **k: {
        "week": "w", "compared_with": "c", "metrics": [], "results": [], "priorities": [],
        "fix_first": {"key": "link:reviews_x_labor:service:friday", "what": "Add one Friday server"}})
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [dict(link)])
    with rec_delivery.collect() as shown:
        html = "".join(emails._weekly_review_sections(rid))
    assert "Add one Friday server" in html and "What connects" not in html
    assert shown.keys("weekly_email") == ["link:reviews_x_labor:service:friday"]


# ── C14 contract fields, Ask's suggestions ──────────────────────────────────

_ANSWER = ("Labor ran hot.\n\n1. Trim one server from Tuesday dinner.\n2. Call the fish supplier today.\n"
           "3. Review the prep list for Friday.\n4. Move the delivery to Monday.")


def test_c14_answered_suggestions_do_not_hide_an_unanswered_one(db_path):
    import ask_cavnar
    rid = _rid(db_path)
    first = ask_cavnar.record_suggestions(rid, _ANSWER, {}, user_id=7)
    assert len(first) == 3
    for s in first:
        rec_ledger.record(rid, s["rec_key"], "dismissed", surface="ask", meta={"kind": "not_for_us"})
    again = ask_cavnar.record_suggestions(rid, _ANSWER, {}, user_id=7)
    assert [s["text"] for s in again] == ["Move the delivery to Monday."] and again[0]["answerable"] is True


def test_c14_module_recs_carry_the_contract_fields(http, db_path, monkeypatch):
    client_api._cache_set("mkt-insight:" + str(http.rid), {
        "insight": "Reach is steady.\n\n1. Post the brunch special Friday.\n2. Reply to every comment.",
        "figures_verified": True, "unsupported_figures": []})
    web = http.get("/api/mkt-insight").get_json()
    assert all(r["rec_key"] == r["key"] and r["answerable"] is True for r in web["recs"])
    phone = http.get("/mobile/api/marketing/insight", headers=_BEARER).get_json()
    assert [i["rec_key"] for i in phone["rec_items"]] == [r["key"] for r in web["recs"]]
    models.update_restaurant(http.rid, {"competitor_intel": json.dumps({"insight": (
        "What they do well:\n- Fast patio service [R1]\n\nRecommendations:\n"
        "1. Add a weekday lunch special. [R1]"), "competitors": []})})
    intel = client_api.intel_recs_payload(http.rid, user_id=7)
    assert all(r["rec_key"] == r["key"] and r["answerable"] is True for r in intel["recs"])


# ── C15 a reply marked posted by hand is the alert implemented ──────────────

def test_c15_mark_as_posted_records_the_review_alert_implemented(http, db_path):
    rid = http.rid
    i = _review(db_path, rid, "mp")
    rec_ledger.present(rid, f"review:{i}", "reviews", "alert_push", db_path=db_path)
    import admin_routes
    app = Flask(__name__)
    with app.test_request_context(f"/api/mark-posted/{i}", method="POST"):
        admin_routes.mark_posted.__wrapped__(i, current_user=http.user)
    ev = _events(db_path, rid, "implemented")
    assert [e["key"] for e in ev] == [f"review:{i}"]
    assert _q(db_path, "SELECT status FROM rec_instances WHERE key=?", (f"review:{i}",))[0]["status"] == "implemented"
