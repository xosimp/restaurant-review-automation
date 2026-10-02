"""Event Intelligence re-audit, fix round A2 (10/1/26): the admin catalog
correction and the owner's own event routes.

  P2-10 / X-3  an admin's catalog correction is committed and audited in the
               request; the followers' re-sync runs on the one admin job pool,
               bounded in time, and audits its own outcome
  X-4          removing a catalog game is recorded (who, and an admin in
               view-as as the admin), and Put back brings it back
  X-5          a view-as follow change is stored as the admin's, not the owner's
  P2-09/X-10   the docs and the Revert confirm say a cleared field takes the
               season file's value now and the followers re-sync
"""
import json
import os
import sys
from datetime import date

import pytest

import models
import event_memory
from event_intel import engine, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ST_CHARLES = (41.9142, -88.3087)
TODAY = date(2026, 10, 1)


@pytest.fixture
def db(db_path, monkeypatch, tmp_path):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    monkeypatch.setattr(engine, "_today", lambda r=None: TODAY)
    import weather
    monkeypatch.setattr(weather, "forecast_for_day", lambda *a, **k: None)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    _bears_only(db_path, monkeypatch, tmp_path / "seasons")
    return db_path


def _bears_only(db_path, monkeypatch, tmp_dir):
    import shutil
    keep = "nfl-chicago-bears-2026.json"
    os.makedirs(str(tmp_dir), exist_ok=True)
    shutil.copy(os.path.join(store.SEASONS_DIR, keep), os.path.join(str(tmp_dir), keep))
    monkeypatch.setattr(store, "SEASONS_DIR", str(tmp_dir))
    c = models.get_conn(db_path)
    try:
        c.execute("DELETE FROM catalog_events WHERE series_id IN (SELECT id FROM event_series WHERE slug != 'nfl-chicago-bears')")
        c.execute("DELETE FROM event_follows WHERE series_id IN (SELECT id FROM event_series WHERE slug != 'nfl-chicago-bears')")
        c.execute("DELETE FROM event_series WHERE slug != 'nfl-chicago-bears'")
        c.commit()
    finally:
        c.close()


def _restaurant(db, name="EJ Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    r = get_restaurant(rid, db_path=db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=TODAY, db_path=db)
    return r


def _event(db, ext):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    conn = models.get_conn(db)
    try:
        return dict(conn.execute("SELECT * FROM catalog_events WHERE series_id=? AND external_id=?",
                                 (s["id"], ext)).fetchone())
    finally:
        conn.close()


def _rows(db, sql, args=()):
    conn = models.get_conn(db)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _admin_events(db, action):
    return _rows(db, "SELECT * FROM admin_events WHERE event_type=? ORDER BY id", (action,))


# ── P2-10 / X-3: the catalog correction is bounded ──────────────────────────

@pytest.fixture
def admin_client(db, monkeypatch):
    import admin_routes
    import auth
    from auth import create_session, create_user, init_auth
    from flask import Flask
    for mod in (auth, admin_routes):
        monkeypatch.setattr(mod, "get_conn", models.get_conn, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db, raising=False)
    init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "event-fix-a2"
    app.register_blueprint(admin_routes.admin_bp)
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db)
    uid = create_user(hq, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db))
    c.set_cookie("csrf_js", "a2-csrf")
    return c


def _edit(c, event_id, body):
    return c.post(f"/admin/api/event-catalog/{event_id}", json=body, headers={"X-CSRF": "a2-csrf"})


def test_a_catalog_correction_never_runs_the_follower_resync_in_the_request(db, admin_client, monkeypatch):
    import admin_routes
    import demand_signals
    a, b = _restaurant(db, "EJ Co"), _restaurant(db, "EJ Two")
    calls, queued = [], []
    real_sync = engine.sync_restaurant
    monkeypatch.setattr(engine, "sync_restaurant", lambda r, **k: calls.append(r.id) or real_sync(r, **k))
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *args: queued.append((fn, args)))
    wk18 = _event(db, "2026-reg-18")
    r = _edit(admin_client, wk18["id"], {"changes": {"event_date": "2027-01-10", "kickoff_local": "15:25"}})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["queued"] and d["job_id"] and d["followers"] == 2
    assert "synced" not in d, "the request must not claim a re-sync it has not done"
    assert calls == [], "no follower is re-synced on the request thread"
    # The correction itself is committed before the job runs.
    assert _event(db, "2026-reg-18")["event_date"] == "2027-01-10"
    (edit,) = _admin_events(db, "event_catalog.edit")
    assert edit["result"] == "ok" and "queued" in edit["summary"] and "re-synced" not in edit["summary"]
    after = json.loads(edit["after_json"])
    assert after["resync_job"] == d["job_id"] and after["followers"] == 2
    assert json.loads(edit["before_json"])["event_date"] != "2027-01-10"
    assert _admin_events(db, "event_catalog.resync") == []
    # The pool runs it: both followers move, and the job audits its outcome.
    ((fn, args),) = queued
    fn(*args)
    assert sorted(calls) == sorted([a.id, b.id])
    assert [s for s in demand_signals.upcoming(a.id, "2027-01-10", "2027-01-10", db_path=db)
            if s.get("ref") == f"event:{wk18['id']}"]
    (done,) = _admin_events(db, "event_catalog.resync")
    assert done["result"] == "ok" and "2 following restaurants re-synced" in done["summary"]
    assert done["actor"] == "will"
    polled = admin_client.get(f"/admin/api/admin-jobs/{d['job_id']}").get_json()
    assert polled["status"] == "done" and polled["synced"] == 2 and polled["left"] == 0


def test_the_resync_job_stops_at_its_time_bound_and_says_what_it_left(db, monkeypatch):
    import admin_routes
    a, b, c = (_restaurant(db, n) for n in ("One", "Two", "Three"))
    done = []

    def sync(r, **k):
        done.append(r.id)
        monkeypatch.setattr(admin_routes, "EVENT_RESYNC_MAX_SECONDS", -1)   # the budget is spent
        return {}
    monkeypatch.setattr(engine, "sync_restaurant", sync)
    out = admin_routes._event_resync_job(7, 1, [a.id, b.id, c.id], {"id": 1, "username": "will"})
    assert done == [a.id] and out["synced"] == 1 and out["left"] == 2 and out["ok"]
    (row,) = _admin_events(db, "event_catalog.resync")
    assert row["result"] == "partial" and "2 left for the 5am event sync" in row["summary"]


def test_a_full_admin_pool_is_audited_as_not_started_never_as_done(db, admin_client, monkeypatch):
    import admin_routes
    _restaurant(db)
    calls = []
    monkeypatch.setattr(engine, "sync_restaurant", lambda r, **k: calls.append(r.id) or {})
    monkeypatch.setattr(admin_routes, "ADMIN_JOB_QUEUE_MAX", 0)
    d = _edit(admin_client, _event(db, "2026-reg-18")["id"], {"changes": {"broadcast": "CBS"}}).get_json()
    assert d["ok"] and d["queued"] is False and d["job_id"] is None and calls == []
    (edit,) = _admin_events(db, "event_catalog.edit")
    assert edit["result"] == "partial" and "not started" in edit["summary"] and "5am" in edit["summary"]


def test_the_edit_route_body_has_no_inline_resync():
    src = open(os.path.join(ROOT, "admin_routes.py"), encoding="utf-8").read()
    body = src[src.index("def admin_api_event_catalog_edit("):]
    body = body[:body.index("\n@admin_bp.route")]
    assert "sync_restaurant" not in body and "_start_admin_job(" in body


# ── X-5: a view-as follow change is the admin's ─────────────────────────────

VIEW_AS = {"acting_admin_id": 9, "acting_admin": "will", "acting_admin_role": "admin", "as_username": "erik"}


def _ctx(app, path, method="GET", body=None, view_as=None):
    from flask import g
    ctx = app.test_request_context(path, method=method, json=body)
    ctx.push()
    if view_as:
        g.view_as = dict(view_as)
    return ctx


def _activity(db, rid, event_type):
    return [json.loads(r["event_data"]) for r in _rows(
        db, "SELECT event_data FROM activity_log WHERE restaurant_id=? AND event_type=? ORDER BY id",
        (rid, event_type))]


def test_a_view_as_follow_change_is_stored_as_the_admins(db):
    from flask import Flask
    import strategy_routes
    r = _restaurant(db)
    sid = store.series_by_slug("nfl-chicago-bears", db_path=db)["id"]
    owner = {"restaurant_id": r.id, "role": "owner", "id": 1, "username": "erik"}
    app = Flask(__name__)
    ctx = _ctx(app, f"/labor/event-follows/{sid}", "POST", {"active": False}, view_as=VIEW_AS)
    try:
        body, status = strategy_routes._do_event_follow_set(owner, sid)
    finally:
        ctx.pop()
    assert status == 200
    (f,) = store.follows(r.id, active_only=False, db_path=db)
    assert f["source"] == "admin" and f["active"] == 0
    assert _activity(db, r.id, "event_follow_set")[-1]["acting_admin"] == "will"
    ctx = _ctx(app, f"/labor/event-follows/{sid}", "POST", {"active": True})
    try:
        strategy_routes._do_event_follow_set(owner, sid)
    finally:
        ctx.pop()
    assert store.follows(r.id, db_path=db)[0]["source"] == "owner"


# ── X-4: a removed game is recorded and can be put back ─────────────────────

def _game_row(db, rid, day="2026-10-11"):
    import demand_signals
    return [s for s in demand_signals.upcoming(rid, day, day, db_path=db) if s.get("source") == "events"]


def test_a_view_as_removal_is_the_admins_and_put_back_brings_the_game_back(db):
    from flask import Flask
    import strategy_routes
    r = _restaurant(db)
    (row,) = _game_row(db, r.id)
    eid = int(row["ref"].split(":")[1])
    owner = {"restaurant_id": r.id, "role": "owner", "id": 1, "username": "erik"}
    app = Flask(__name__)
    ctx = _ctx(app, f"/labor/demand-signals/{row['id']}", "DELETE", view_as=VIEW_AS)
    try:
        body, status = strategy_routes._do_demand_signal_delete(owner, row["id"])
    finally:
        ctx.pop()
    assert status == 200 and body["event_id"] == eid and _game_row(db, r.id) == []
    (dis,) = _rows(db, "SELECT * FROM event_dismissals WHERE restaurant_id=?", (r.id,))
    assert dis["source"] == "admin" and dis["dismissed_by"] == "will (Cavnar AI)"
    (logged,) = _activity(db, r.id, "event_game_removed")
    assert logged["acting_admin"] == "will" and "10/11/26" in logged["detail"] and logged["event_id"] == eid
    # The daily sync keeps it off.
    engine.sync_restaurant(r, today=TODAY, db_path=db)
    assert _game_row(db, r.id) == []
    # The events card lists it with who removed it, M/D/YY.
    ctx = _ctx(app, "/labor/event-dismissals")
    try:
        body, status = strategy_routes._do_event_dismissals_get(owner)
    finally:
        ctx.pop()
    (g,) = body["removed"]
    assert g["event_id"] == eid and g["by_admin"] and g["removed_by"] == "will (Cavnar AI)"
    assert g["text"].startswith("Bears") and "10/11/26" in g["text"] and "-" not in g["removed_on"]
    # A login that can't edit the schedule's inputs can't put it back.
    ctx = _ctx(app, f"/labor/event-dismissals/{eid}/restore", "POST")
    try:
        _b, st = strategy_routes._do_event_dismissal_restore({"restaurant_id": r.id, "role": "employee"}, eid)
    finally:
        ctx.pop()
    assert st == 403
    ctx = _ctx(app, f"/labor/event-dismissals/{eid}/restore", "POST")
    try:
        body, status = strategy_routes._do_event_dismissal_restore(owner, eid)
    finally:
        ctx.pop()
    assert status == 200 and body["removed"] == []
    assert len(_game_row(db, r.id)) == 1, "the game is back on the calendar at once"
    assert store.dismissed(r.id, db_path=db) == set()
    assert _activity(db, r.id, "event_game_restored")
    ctx = _ctx(app, f"/labor/event-dismissals/{eid}/restore", "POST")
    try:
        _b, st = strategy_routes._do_event_dismissal_restore(owner, eid)
    finally:
        ctx.pop()
    assert st == 404


def test_an_owners_removal_names_the_owner_and_a_manual_date_is_logged_too(db):
    from flask import Flask
    import demand_signals
    import strategy_routes
    r = _restaurant(db)
    (row,) = _game_row(db, r.id)
    owner = {"restaurant_id": r.id, "role": "owner", "id": 1, "username": "erik"}
    app = Flask(__name__)
    ctx = _ctx(app, f"/labor/demand-signals/{row['id']}", "DELETE")
    try:
        strategy_routes._do_demand_signal_delete(owner, row["id"])
    finally:
        ctx.pop()
    (dis,) = _rows(db, "SELECT * FROM event_dismissals WHERE restaurant_id=?", (r.id,))
    assert dis["source"] == "owner" and dis["dismissed_by"] == "erik"
    assert "acting_admin" not in _activity(db, r.id, "event_game_removed")[0]
    demand_signals.save(r.id, [{"date": "2026-10-20", "kind": "event", "label": "Rehearsal dinner", "covers": 40}],
                        db_path=db)
    (mine,) = [s for s in demand_signals.upcoming(r.id, "2026-10-20", "2026-10-20", db_path=db)
               if s.get("label") == "Rehearsal dinner"]
    ctx = _ctx(app, f"/labor/demand-signals/{mine['id']}", "DELETE")
    try:
        body, status = strategy_routes._do_demand_signal_delete(owner, mine["id"])
    finally:
        ctx.pop()
    assert status == 200 and body["event_id"] is None
    assert "Rehearsal dinner on 10/20/26" in _activity(db, r.id, "demand_signal_deleted")[0]["detail"]


def test_the_restore_routes_have_web_and_mobile_twins():
    import strategy_routes
    paths = {(p, tuple(m)) for p, m, _f, _e in strategy_routes._ROUTES}
    assert ("/labor/event-dismissals", ("GET",)) in paths
    assert ("/labor/event-dismissals/<int:event_id>/restore", ("POST",)) in paths


def test_an_old_dismissals_table_gains_who_removed_it_at_boot(db):
    conn = models.get_conn(db)
    try:
        conn.execute("DROP TABLE event_dismissals")
        conn.execute("CREATE TABLE event_dismissals (restaurant_id INTEGER NOT NULL, event_id INTEGER NOT NULL, "
                     "created_at TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (restaurant_id, event_id))")
        conn.commit()
    finally:
        conn.close()
    store.init_event_intel(db_path=db)
    cols = {r["name"] for r in _rows(db, "PRAGMA table_info(event_dismissals)")}
    assert {"dismissed_by", "source"} <= cols


def test_the_events_card_offers_put_back():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "/api/labor/event-dismissals" in src and "evRestore(" in src
    fn = src[src.index("function evRemovedHtml("):]
    fn = fn[:fn.index("window.evFollowSet")]
    assert 'class="cbtn cbtn-secondary cbtn-sm" onclick="evRestore(' in fn and "=>" not in fn


# ── P2-09 / X-10: the revert is immediate, and the docs say so ──────────────

def test_the_docs_and_the_revert_confirm_say_a_cleared_field_takes_the_season_value_now():
    api = open(os.path.join(ROOT, "API_REFERENCE.md"), encoding="utf-8").read()
    line = [ln for ln in api.splitlines() if ln.startswith("- `POST /admin/api/event-catalog/<event_id>`")][0]
    assert "at its next load" not in line and "season file's value now" in line
    assert "admin job pool" in line and "event_catalog.resync" in line
    admin = open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8").read()
    rev = admin[admin.index("async function evRevert("):]
    rev = rev[:rev.index("\n}")]
    assert "until the next daily load" not in rev
    assert "season file\\'s value now" in rev and "re-sync" in rev
