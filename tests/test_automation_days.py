"""The owner's days for the work Cavnar AI does on its own (owner 9/27/26):
the schedule draft (Thursday unless changed; the auto-publish follows it by
a day) and trusted supplier orders (Monday unless changed). Both were fixed
in the scheduler loop; each restaurant now runs on its own day, in its own
zone."""
from datetime import datetime

import pytest
from flask import Flask

import auth
import models
from models import Restaurant, create_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    return db_path


def _rid(db_path, name="Days Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email="d@x.test", **kw), db_path=db_path)


# ── the model ───────────────────────────────────────────────────────────────

def test_the_days_default_to_thursday_and_monday_and_save(db):
    rid = _rid(db)
    r = models.get_restaurant(rid)
    assert (r.auto_draft_weekday, r.auto_order_weekday) == (3, 0)
    assert models.auto_publish_weekday(r) == 4                       # Friday
    models.update_restaurant(rid, {"auto_draft_weekday": 1, "auto_order_weekday": 6})
    r = models.get_restaurant(rid)
    assert (models.auto_draft_weekday(r), models.auto_publish_weekday(r), models.auto_order_weekday(r)) == (1, 2, 6)


def test_a_day_out_of_range_reads_as_the_default():
    # A Sunday draft would publish on the Monday its week starts.
    class R:
        auto_draft_weekday = 6
        auto_order_weekday = 9
    assert models.auto_draft_weekday(R()) == 3 and models.auto_order_weekday(R()) == 0
    assert models.auto_publish_weekday(R()) == 4


# ── the routes (web and phone share them) ───────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client"):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 5, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.test"})


def test_the_draft_day_is_set_through_the_auto_draft_route(client, db, monkeypatch):
    rid = _rid(db, module_labor=1)
    _as(monkeypatch, rid)
    got = client.get("/api/labor/auto-draft").get_json()
    assert (got["weekday"], got["day"], got["publish_day"]) == (3, "Thursday", "Friday")
    got = client.post("/api/labor/auto-draft", json={"weekday": 1}).get_json()
    assert got["ok"], got
    assert (got["day"], got["publish_day"], got["enabled"]) == ("Tuesday", "Wednesday", False)
    pub = client.get("/api/labor/auto-publish").get_json()
    assert (pub["day"], pub["draft_day"]) == ("Wednesday", "Tuesday")
    # Sunday, a bool and a word are refused, and the day stays.
    for bad in (6, True, "Friday", None):
        r = client.post("/api/labor/auto-draft", json={"weekday": bad})
        assert r.status_code == 400, bad
    assert models.get_restaurant(rid).auto_draft_weekday == 1
    acts = [(a["type"], a["detail"]) for a in models.get_account_activity(rid)]
    assert ("auto_draft_day_changed", "Tuesday") in acts


def test_the_order_day_is_set_through_the_auto_order_route(client, db, monkeypatch):
    rid = _rid(db, module_inventory=1)
    _as(monkeypatch, rid)
    assert client.get("/api/food-cost/auto-order").get_json()["day"] == "Monday"
    got = client.post("/api/food-cost/auto-order", json={"weekday": "6"}).get_json()
    assert got["ok"] and (got["weekday"], got["day"], got["enabled"]) == (6, "Sunday", False)
    assert client.post("/api/food-cost/auto-order", json={"weekday": 7}).status_code == 400
    # Only the owner sets the standing rule, its day included.
    _as(monkeypatch, rid, "manager")
    assert client.post("/api/food-cost/auto-order", json={"weekday": 2}).status_code == 403
    assert models.get_restaurant(rid).auto_order_weekday == 6


def test_a_login_that_cannot_draft_cannot_move_the_day(client, db, monkeypatch):
    rid = _rid(db, module_labor=1)
    _as(monkeypatch, rid, "employee")
    assert client.post("/api/labor/auto-draft", json={"weekday": 0}).status_code == 403
    assert models.get_restaurant(rid).auto_draft_weekday == 3


def test_the_phone_reaches_the_same_handlers(client):
    rules = {(r.rule, m) for r in client.application.url_map.iter_rules() for m in r.methods}
    for path in ("/mobile/api/labor/auto-draft", "/mobile/api/food-cost/auto-order", "/mobile/api/labor/auto-publish"):
        assert (path, "GET") in rules and (path, "POST") in rules, path


# ── the jobs run on each restaurant's day ───────────────────────────────────

def test_the_draft_runs_on_each_restaurants_own_day(db, monkeypatch):
    import ops
    import strategy_jobs
    ran = []
    monkeypatch.setattr("schedule_engine._run_schedule_job", lambda job_id, rid: ran.append(rid))
    monkeypatch.setattr(ops, "start_async_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "error"})
    thu = _rid(db, name="Thu", module_labor=1)
    tue = _rid(db, name="Tue", module_labor=1)
    for rid in (thu, tue):
        models.update_restaurant(rid, {"auto_draft_schedule": 1})
    models.update_restaurant(tue, {"auto_draft_weekday": 1})
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 22, 9, 0))    # Tuesday
    assert ran == [tue]
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 24, 5, 0))    # Thursday, before 6am
    assert ran == [tue]
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 24, 9, 0))    # Thursday
    assert ran == [tue, thu]


def test_the_draft_day_is_the_restaurants_own_calendar(db, monkeypatch):
    """9pm Monday in Chicago is 3am Tuesday in London: a London restaurant
    with a Tuesday draft waits for ITS 6am, not Chicago's."""
    import ops
    import strategy_jobs
    ran = []
    monkeypatch.setattr("schedule_engine._run_schedule_job", lambda job_id, rid: ran.append(rid))
    monkeypatch.setattr(ops, "start_async_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "error"})
    london = _rid(db, name="London", module_labor=1, timezone="Europe/London")
    models.update_restaurant(london, {"auto_draft_schedule": 1, "auto_draft_weekday": 1})
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 21, 21, 0))   # Mon 9pm CT = Tue 3am BST
    assert ran == []
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 22, 0, 30))   # Tue 6:30am BST
    assert ran == [london]


def test_trusted_orders_and_the_auto_publish_wait_for_their_day(db, monkeypatch):
    import scheduler
    import strategy_jobs
    import time_utils
    rid = _rid(db, module_inventory=1, module_labor=1)
    models.update_restaurant(rid, {"auto_order_trusted": 1, "auto_order_weekday": 3,
                                   "auto_publish_schedule": 1, "auto_draft_weekday": 0})
    queued = []
    monkeypatch.setattr("ordering.queue_trusted_orders", lambda *a, **k: queued.append(1) or [])
    monkeypatch.setattr("data_freshness.depletion_behind", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    trust = []
    monkeypatch.setattr(models, "schedule_publish_trust", lambda *a, **k: trust.append(1) or 0)

    def at(day):
        monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, day, 9, 0))
    at(21)                                           # Monday, the draft day: neither
    strategy_jobs.run_trusted_orders(db_path=db)
    scheduler.run_auto_publish_schedules()
    assert not queued and not trust
    at(22)                                           # Tuesday: the day after a Monday draft
    scheduler.run_auto_publish_schedules()
    assert trust, "the publish check ran on the day after the draft"
    at(24)                                           # Thursday: the order day
    strategy_jobs.run_trusted_orders(db_path=db)
    assert queued == [1]


def test_the_loop_no_longer_names_the_days():
    src = open("scheduler.py", encoding="utf-8").read()
    loop = src[src.index("def scheduler_loop"):]
    assert 'now.weekday() == 3 and _ops.claim_period("auto_draft_schedule"' not in loop
    assert 'now.weekday() == 0 and _ops.claim_period("trusted_orders"' not in loop
    assert 'now.weekday() == 4 and _ops.claim_period("auto_publish_schedule"' not in loop
    assert '_ops.run_job("auto_draft_schedule", run_auto_draft_schedules, now=now)' in loop
    assert "local.weekday() != auto_publish_weekday(r)" in src
    jobs = open("strategy_jobs.py", encoding="utf-8").read()
    assert "local.weekday() != auto_order_weekday(r)" in jobs


def test_reservations_sync_the_day_before_each_draft(db, monkeypatch):
    import reservation_feeds
    thu = _rid(db, name="Thu", module_labor=1)
    mon = _rid(db, name="Mon", module_labor=1)
    for rid in (thu, mon):
        models.update_restaurant(rid, {"reservation_provider": "opentable"})
    models.update_restaurant(mon, {"auto_draft_weekday": 0})
    seen = []
    monkeypatch.setattr(reservation_feeds, "sync", lambda rid, **k: seen.append(rid) or {"error": None})
    reservation_feeds.run_reservation_sync(db_path=db, weekday=2)          # Wednesday: Thursday's draft
    assert seen == [thu]
    seen.clear()
    reservation_feeds.run_reservation_sync(db_path=db, weekday=6)          # Sunday: Monday's draft
    assert seen == [mon]


def test_the_brief_waits_for_the_owners_draft_day():
    src = open("morning_brief.py", encoding="utf-8").read()
    assert "_sched_from = auto_draft_weekday(restaurant) + 1" in src
    assert 'today.weekday() >= _sched_from' in src


# ── the page ────────────────────────────────────────────────────────────────

def test_every_surface_names_the_owners_day():
    assert "Cavnar AI drafts every Thursday" not in SRC and "Send Monday orders" not in SRC
    assert "Publish it for me on Friday</b>" not in SRC
    assert 'id="lb2-autodraft-day" class="ac-select day-pick" onchange="lb2SaveAutoDraftDay(this)"' in SRC
    assert 'id="fc2-autoorder-day" class="ac-select day-pick" onchange="fc2SaveAutoOrderDay(this)"' in SRC
    assert 'id="as-autodraft-day" class="ac-select" onchange="saveAutoDraftDay(this)"' in SRC
    assert 'id="as-autoorder-day" class="ac-select" onchange="saveAutoOrderDay(this)"' in SRC
    # The draft day offers Monday to Saturday; the order day every day.
    draft = SRC[SRC.index('id="lb2-autodraft-day"'):SRC.index("</select>", SRC.index('id="lb2-autodraft-day"'))]
    assert "Saturday" in draft and "Sunday" not in draft
    order = SRC[SRC.index('id="fc2-autoorder-day"'):SRC.index("</select>", SRC.index('id="fc2-autoorder-day"'))]
    assert "Sunday" in order
    # One writer of the names; the Studio loads its switch on a direct open.
    assert "asDayNames('as-publishday-name', d.publish_day)" in SRC
    assert "if (window.lb2LoadAutoDraftSwitch) lb2LoadAutoDraftSwitch();" in SRC


def test_the_phone_carries_both_days():
    swift = open("ios/CavnarAI/CavnarAI/Features/Account/AccountAutomationView.swift", encoding="utf-8").read()
    assert 'AccountKVRow(label: "Draft day")' in swift and 'AccountKVRow(label: "Order day")' in swift
    assert '"/mobile/api/labor/auto-draft", method: .post, body: WeekdayBody(weekday: weekday)' in swift
    assert '"/mobile/api/food-cost/auto-order", method: .post, body: WeekdayBody(weekday: weekday)' in swift
    assert 'label: "Publish the schedule \\(viewModel.autoPublish?.day ?? "Friday")"' in swift
    assert "goes Monday 8am" not in swift
