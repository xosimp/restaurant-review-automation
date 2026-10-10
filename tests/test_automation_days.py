"""The owner's days for the work Cavnar AI does on its own (owner 9/27/26):
the schedule draft (the owner's day, else Monday-Thursday spread by
restaurant — AI cost audit 10/7/26 #5, it was Thursday for everyone; the
auto-publish follows it by a day) and trusted supplier orders (Monday unless
changed). Both were fixed in the scheduler loop; each restaurant now runs on
its own day, in its own zone."""
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

def test_the_days_default_and_save(db):
    # The draft day defaults to the spread (AI cost audit 10/7/26 #5 — it
    # was Thursday for every restaurant, which this test used to assert);
    # the stored column still holds its old default, unchosen.
    rid = _rid(db)
    r = models.get_restaurant(rid)
    assert (r.auto_draft_weekday, r.auto_draft_weekday_chosen, r.auto_order_weekday) == (3, 0, 0)
    spread = models.default_auto_draft_weekday(rid)
    assert spread in (0, 1, 2, 3)
    assert (models.auto_draft_weekday(r), models.auto_publish_weekday(r)) == (spread, spread + 1)
    models.update_restaurant(rid, {"auto_draft_weekday": 1, "auto_order_weekday": 6})
    r = models.get_restaurant(rid)
    assert r.auto_draft_weekday_chosen == 1                          # any write of the day is a choice
    assert (models.auto_draft_weekday(r), models.auto_publish_weekday(r), models.auto_order_weekday(r)) == (1, 2, 6)


def test_a_day_out_of_range_reads_as_the_default():
    # A Sunday draft would publish on the Monday its week starts.
    class R:
        auto_draft_weekday = 6
        auto_draft_weekday_chosen = 1
        auto_order_weekday = 9
    assert models.auto_draft_weekday(R()) == 3 and models.auto_order_weekday(R()) == 0
    assert models.auto_publish_weekday(R()) == 4


# ── AI cost audit 10/7/26 #5: an unchosen draft day is spread ──────────────

def test_an_unchosen_draft_day_is_spread_monday_to_thursday_by_restaurant(db):
    rids = [_rid(db, name=f"Spread {i}") for i in range(8)]
    days = [models.auto_draft_weekday(models.get_restaurant(rid)) for rid in rids]
    assert days == [rid % 4 for rid in rids]                          # deterministic, Monday-Thursday
    assert set(days) == {0, 1, 2, 3}
    for rid, d in zip(rids, days):
        assert models.auto_publish_weekday(models.get_restaurant(rid)) == d + 1
    # A chosen day is kept, Thursday included — never replaced by the spread.
    models.update_restaurant(rids[0], {"auto_draft_weekday": 3})
    assert models.auto_draft_weekday(models.get_restaurant(rids[0])) == 3
    # Friday and Saturday stay owner choices.
    assert 4 not in models.AUTO_DRAFT_SPREAD_WEEKDAYS and 5 not in models.AUTO_DRAFT_SPREAD_WEEKDAYS
    # One rule for the object and the raw columns.
    for rid in rids[1:]:
        r = models.get_restaurant(rid)
        assert models.effective_auto_draft_weekday(rid, r.auto_draft_weekday, r.auto_draft_weekday_chosen) \
            == models.auto_draft_weekday(r)


def test_the_draft_days_already_in_force_are_kept_once(db):
    import sqlite3
    on = _rid(db, name="Opted in")
    publish = _rid(db, name="Publishes")
    picked = _rid(db, name="Picked Wednesday")
    logged = _rid(db, name="Picked Thursday")
    never = _rid(db, name="Never opted in")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE restaurants SET auto_draft_schedule=1 WHERE id=?", (on,))
    conn.execute("UPDATE restaurants SET auto_publish_schedule=1 WHERE id=?", (publish,))
    conn.execute("UPDATE restaurants SET auto_draft_weekday=2 WHERE id=?", (picked,))
    conn.execute("INSERT INTO activity_log (restaurant_id, event_type, event_data) VALUES (?, ?, ?)",
                 (logged, "auto_draft_day_changed", "{}"))
    conn.execute("UPDATE restaurants SET auto_draft_weekday_chosen=0")
    # Restaurants that existed before auto-draft became the Labor default
    # (#73, 10/7/26): only the one this test opts in drafts.
    conn.execute("UPDATE restaurants SET auto_draft_schedule=0 WHERE id!=?", (on,))
    conn.execute("DELETE FROM data_migrations WHERE name=?", (models.AUTO_DRAFT_DAY_CHOSEN_MIGRATION,))
    conn.commit()
    conn.close()
    assert models.mark_chosen_draft_days(db_path=db) == 4
    chosen = {rid: models.get_restaurant(rid).auto_draft_weekday_chosen for rid in (on, publish, picked, logged, never)}
    assert chosen == {on: 1, publish: 1, picked: 1, logged: 1, never: 0}
    # Simple EJ's case: opted in on the old default, it keeps drafting Thursday.
    assert models.auto_draft_weekday(models.get_restaurant(on)) == 3
    assert models.auto_draft_weekday(models.get_restaurant(picked)) == 2
    assert models.auto_draft_weekday(models.get_restaurant(never)) == models.default_auto_draft_weekday(never)
    # Once: a restaurant opted in later reads the spread.
    models.update_restaurant(never, {"auto_draft_schedule": 1})
    assert models.mark_chosen_draft_days(db_path=db) == 0
    assert models.get_restaurant(never).auto_draft_weekday_chosen == 0


def test_every_restaurant_made_before_the_spread_keeps_its_day_once(db):
    # Re-audit 10/7/26 #6 (the owner's decision): the first migration kept
    # only opted-in or non-default days, so Simple EJ's - auto-draft off -
    # read a spread Tuesday/Wednesday where it had read Thursday/Friday. The
    # second marks every restaurant made before the change at its stored day.
    import sqlite3
    old_off = _rid(db, name="Old, auto-draft off", created_at="2026-09-01T09:00:00")
    old_wed = _rid(db, name="Old, picked Wednesday", created_at="2026-08-15 10:00:00")
    undated = _rid(db, name="Blank created_at")
    new = _rid(db, name="Made after the change", created_at="2026-10-07T10:00:00")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE restaurants SET auto_draft_weekday=2 WHERE id=?", (old_wed,))
    conn.execute("UPDATE restaurants SET created_at='' WHERE id=?", (undated,))
    conn.execute("UPDATE restaurants SET auto_draft_weekday_chosen=0")
    conn.execute("DELETE FROM data_migrations WHERE name=?", (models.AUTO_DRAFT_DAY_KEPT_MIGRATION,))
    conn.commit()
    conn.close()
    # Its id spreads it off Thursday, as Simple EJ's was.
    assert models.default_auto_draft_weekday(old_off) != 3 or models.default_auto_draft_weekday(new) != 3
    assert models.keep_preexisting_draft_days(db_path=db) >= 3
    days = {rid: models.auto_draft_weekday(models.get_restaurant(rid)) for rid in (old_off, old_wed, undated, new)}
    assert days[old_off] == 3 and days[old_wed] == 2 and days[undated] == 3      # the day each had
    assert days[new] == models.default_auto_draft_weekday(new)                  # the spread is for the new
    assert models.auto_publish_weekday(models.get_restaurant(old_off)) == 4     # Thursday draft, Friday publish
    # Once: a later run marks nothing, and it is a migration of its own (the
    # first already ran in production).
    assert models.keep_preexisting_draft_days(db_path=db) == 0
    assert models.AUTO_DRAFT_DAY_KEPT_MIGRATION != models.AUTO_DRAFT_DAY_CHOSEN_MIGRATION
    import inspect
    assert "keep_preexisting_draft_days(db_path=db_path)" in inspect.getsource(models.init_db)


def test_the_settings_export_says_the_day_the_draft_is_made(db):
    import json
    rid = _rid(db, name="Export Co")
    spread = models.default_auto_draft_weekday(rid)
    got = json.loads(models.build_settings_export_json(rid, db_path=db))
    assert got["auto_draft_weekday"] == spread                  # the effective day, not the stored 3
    models.update_restaurant(rid, {"auto_draft_weekday": 5})
    assert json.loads(models.build_settings_export_json(rid, db_path=db))["auto_draft_weekday"] == 5


def test_the_chosen_flag_is_whitelisted_untracked_and_migrated():
    import inspect
    import change_log
    src = inspect.getsource(models.update_restaurant)
    assert '"auto_draft_weekday_chosen"' in src
    assert "auto_draft_weekday_chosen" in change_log.RESTAURANT_UNTRACKED
    assert '("restaurants", "auto_draft_weekday_chosen", "INTEGER DEFAULT 0")' in inspect.getsource(models.ensure_columns)
    assert "mark_chosen_draft_days(db_path=db_path)" in inspect.getsource(models.init_db)


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
    # The effective day, the spread default (#5) — the same payload both
    # clients read, so the web and the phone show the day it really drafts.
    spread = models.default_auto_draft_weekday(rid)
    assert (got["weekday"], got["day"], got["publish_day"]) == (
        spread, models.WEEKDAY_NAMES[spread], models.WEEKDAY_NAMES[spread + 1])
    got = client.post("/api/labor/auto-draft", json={"weekday": 1}).get_json()
    assert got["ok"], got
    # A trial does not draft by default: only a paid Labor plan does
    # (re-audit 10/7/26 #2, which narrowed AI cost audit 10/7/26 #73).
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
    # Thursday chosen: an unchosen day is spread by restaurant now (#5).
    models.update_restaurant(thu, {"auto_draft_weekday": 3})
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
    # On the AI lane since AI cost audit 10/7/26 #6, not the loop thread.
    assert '_ops.run_in_lane("ai", "auto_draft_schedule", run_auto_draft_schedules, now=now)' in loop
    assert "local.weekday() != auto_publish_weekday(r)" in src
    jobs = open("strategy_jobs.py", encoding="utf-8").read()
    assert "local.weekday() != auto_order_weekday(r)" in jobs


def test_reservations_sync_the_day_before_each_draft(db, monkeypatch):
    import reservation_feeds
    thu = _rid(db, name="Thu", module_labor=1)
    mon = _rid(db, name="Mon", module_labor=1)
    for rid in (thu, mon):
        models.update_restaurant(rid, {"reservation_provider": "opentable"})
    # Thursday chosen: an unchosen day is spread by restaurant now (#5).
    models.update_restaurant(thu, {"auto_draft_weekday": 3})
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
    # Counted in days into the restaurant's own week (week_start_day 10/10/26).
    assert "_sched_from = (auto_draft_weekday(restaurant) - _wsd) % 7 + 1" in src
    assert "(today.weekday() - _wsd) % 7 >= _sched_from" in src


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
