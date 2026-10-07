"""Event Intelligence re-audit 2, fix round W4 (10/1/26): the owner's event
routes, Account activity, the market history's dates, and the iOS card.

  RX-03 / R1-07  the follow switch and Put back commit the change in the
                 request and hand the re-sync (up to RECORD_MAX re-recorded
                 nights) to ops' bounded admin pool, never the request
  R2-09          a re-sync that can't be queued never answers an error for a
                 change that took effect; the activity row is written; Put
                 back's row says the kickoff on the restaurant's clock
  RX-01          event_follow_set, event_game_removed, event_game_restored,
                 demand_signals_saved and demand_signal_deleted reach the
                 owner's Account activity (models.get_account_activity)
  RX-06          the own-rating week and the market snapshot are dated on the
                 restaurant's clock, never the server's UTC date
  RX-07          the events card's default window starts on the restaurant's
                 today
  RX-09          one restaurant cannot put back or delete another's games, on
                 the web and the mobile twin
  RX-10          iOS words a catalog game as the web does and has the follow
                 switch
"""
import datetime as _dt_mod
import os
import sys
from datetime import date, datetime, timezone

import pytest
from flask import Flask

import models
import event_memory
from event_intel import engine, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ST_CHARLES = (41.9142, -88.3087)
TODAY = date(2026, 10, 1)
OWNER = {"role": "owner", "id": 1, "username": "erik"}


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
    import shutil
    keep = "nfl-chicago-bears-2026.json"
    os.makedirs(str(tmp_path / "seasons"), exist_ok=True)
    shutil.copy(os.path.join(store.SEASONS_DIR, keep), str(tmp_path / "seasons" / keep))
    monkeypatch.setattr(store, "SEASONS_DIR", str(tmp_path / "seasons"))
    c = models.get_conn(db_path)
    try:
        c.execute("DELETE FROM catalog_events WHERE series_id IN (SELECT id FROM event_series WHERE slug != 'nfl-chicago-bears')")
        c.execute("DELETE FROM event_follows WHERE series_id IN (SELECT id FROM event_series WHERE slug != 'nfl-chicago-bears')")
        c.execute("DELETE FROM event_series WHERE slug != 'nfl-chicago-bears'")
        c.commit()
    finally:
        c.close()
    return db_path


class Pool:
    """ops.run_admin_task as the routes call it: what was handed over, run
    only when the test says so (the request never runs it)."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, restaurant_id, name, fn, *args, context="", **kwargs):
        self.calls.append({"kind": kind, "restaurant_id": restaurant_id, "name": name, "fn": fn, "args": args})
        return f"job-{len(self.calls)}", False

    def drain(self):
        out = [c["fn"](*c["args"]) for c in self.calls]
        self.calls = []
        return out


@pytest.fixture
def pool(monkeypatch):
    import ops
    p = Pool()
    monkeypatch.setattr(ops, "run_admin_task", p)
    return p


def _restaurant(db, name="EJ Co", tz="America/Chicago"):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone=tz), db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    r = get_restaurant(rid, db_path=db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=TODAY, db_path=db)
    return r


def _owner(r):
    return dict(OWNER, restaurant_id=r.id)


def _call(fn, *args, path="/x", method="POST", json=None):
    app = Flask(__name__)
    with app.test_request_context(path, method=method, json=json):
        return fn(*args)


def _games(db, rid, start="2026-10-01", end="2026-12-31"):
    import demand_signals
    return [s for s in demand_signals.upcoming(rid, start, end, db_path=db) if s.get("source") == "events"]


def _sid(db):
    return store.series_by_slug("nfl-chicago-bears", db_path=db)["id"]


def _activity(rid, event_type):
    """Through the owner's own reader — the one /account/activity serves on
    the web and the phone — never the raw activity_log table."""
    return [e for e in models.get_account_activity(rid) if e["type"] == event_type]


# ── RX-03 / R1-07: the re-sync leaves the request ──────────────────────────

def test_the_follow_switch_commits_in_the_request_and_resyncs_on_the_admin_pool(db, pool, monkeypatch):
    import strategy_routes
    r = _restaurant(db)
    assert _games(db, r.id)
    real_sync = engine.sync_restaurant
    in_request = []
    monkeypatch.setattr(engine, "sync_restaurant", lambda *a, **k: in_request.append(1) or real_sync(*a, **k))
    body, status = _call(strategy_routes._do_event_follow_set, _owner(r), _sid(db), json={"active": False})
    assert status == 200 and body["ok"] and body["refreshing"] is True
    assert in_request == [], "the request never re-syncs (up to RECORD_MAX nights)"
    # The choice is committed and shown at once; the games leave with the job.
    assert store.follows(r.id, db_path=db) == []
    assert body["follows"][0]["following"] is False
    assert "a moment" in body["message"]
    (call,) = pool.calls
    assert (call["kind"], call["restaurant_id"], call["name"]) == ("event_sync_one", r.id, "event_sync_one")
    assert _games(db, r.id), "nothing left the calendar inside the request"
    (done,) = pool.drain()
    assert in_request == [1] and done["ok"] == 1 and done["removed"] > 0
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(done)
    assert _games(db, r.id) == []


def test_put_back_lifts_the_removal_in_the_request_and_resyncs_on_the_admin_pool(db, pool, monkeypatch):
    import strategy_routes
    r = _restaurant(db)
    (row,) = _games(db, r.id, "2026-10-11", "2026-10-11")
    eid = int(row["ref"].split(":")[1])
    _call(strategy_routes._do_demand_signal_delete, _owner(r), row["id"], method="DELETE")
    assert _games(db, r.id, "2026-10-11", "2026-10-11") == []
    real_sync = engine.sync_restaurant
    in_request = []
    monkeypatch.setattr(engine, "sync_restaurant", lambda *a, **k: in_request.append(1) or real_sync(*a, **k))
    body, status = _call(strategy_routes._do_event_dismissal_restore, _owner(r), eid)
    assert status == 200 and body["removed"] == [] and body["refreshing"] is True
    assert in_request == [] and store.dismissed(r.id, db_path=db) == set()
    pool.drain()
    assert len(_games(db, r.id, "2026-10-11", "2026-10-11")) == 1


def test_the_job_syncs_again_when_a_press_landed_while_it_ran(db, monkeypatch):
    """A second press joins the restaurant's pending job (claim_async_job);
    one that lands while the job is already syncing is not lost."""
    import strategy_routes
    r = _restaurant(db)
    sid = _sid(db)
    real_sync = engine.sync_restaurant
    passes = []

    def sync(rest, *a, **k):
        passes.append(1)
        if len(passes) == 1:              # the owner turns it off mid-pass
            store.set_follow(r.id, sid, False, db_path=db)
        return real_sync(rest, *a, **k)
    monkeypatch.setattr(engine, "sync_restaurant", sync)
    got = strategy_routes._event_resync_one(r.id)
    assert len(passes) == 2 and got["hit_bound"] is False
    assert _games(db, r.id) == []

    # Bounded: a state that never settles stops at EVENT_RESYNC_PASSES.
    flips = iter(range(100))
    monkeypatch.setattr(engine, "sync_restaurant",
                        lambda rest, *a, **k: store.set_follow(r.id, sid, next(flips) % 2 == 0, db_path=db) or {})
    got = strategy_routes._event_resync_one(r.id)
    assert got["hit_bound"] is True and next(flips) == strategy_routes.EVENT_RESYNC_PASSES


def test_an_unknown_calendar_is_still_a_404_and_nothing_is_queued(db, pool):
    import strategy_routes
    r = _restaurant(db)
    body, status = _call(strategy_routes._do_event_follow_set, _owner(r), 999999, json={"active": True})
    assert status == 404 and pool.calls == []
    assert _activity(r.id, "event_follow_set") == []


# ── R2-09: a committed change never answers an error ───────────────────────

def test_a_resync_that_cant_be_queued_still_answers_the_change_and_logs_it(db, monkeypatch):
    import ops
    import strategy_routes
    r = _restaurant(db)
    (row,) = _games(db, r.id, "2026-10-11", "2026-10-11")
    eid = int(row["ref"].split(":")[1])
    _call(strategy_routes._do_demand_signal_delete, _owner(r), row["id"], method="DELETE")

    def locked(*a, **k):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(ops, "run_admin_task", locked)
    monkeypatch.setattr(engine, "sync_restaurant", locked)        # the writer is busy for the re-sync too
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append((type(e).__name__, k.get("job"))))
    body, status = _call(strategy_routes._do_event_dismissal_restore, _owner(r), eid)
    assert status == 200 and body["ok"] and body["refreshing"] is False and "5am" in body["message"]
    assert store.dismissed(r.id, db_path=db) == set()
    assert _activity(r.id, "event_game_restored"), "the row is written for a change that took effect"
    # A retry is told the truth: the game isn't removed any more.
    body, status = _call(strategy_routes._do_event_dismissal_restore, _owner(r), eid)
    assert status == 404
    body, status = _call(strategy_routes._do_event_follow_set, _owner(r), _sid(db), json={"active": False})
    assert status == 200 and body["refreshing"] is False and "5am" in body["message"]
    assert store.follows(r.id, db_path=db) == [] and _activity(r.id, "event_follow_set")
    assert captured == [("OperationalError", "event_sync_one")] * 2


def test_put_backs_activity_row_says_the_kickoff_on_the_restaurants_clock(db, pool):
    import strategy_routes
    r = _restaurant(db, name="Coast Co", tz="America/Los_Angeles")
    (row,) = _games(db, r.id, "2026-10-04", "2026-10-04")       # Jets, noon Central
    eid = int(row["ref"].split(":")[1])
    _call(strategy_routes._do_demand_signal_delete, _owner(r), row["id"], method="DELETE")
    _call(strategy_routes._do_event_dismissal_restore, _owner(r), eid)
    (logged,) = _activity(r.id, "event_game_restored")
    assert "10/4/26" in logged["detail"] and "10am" in logged["detail"] and "12pm" not in logged["detail"]


# ── RX-01: the owner's Account activity shows the event changes ────────────

def test_every_event_change_reaches_account_activity_with_a_label(db, pool):
    import demand_signals
    import strategy_routes
    r = _restaurant(db)
    _call(strategy_routes._do_event_follow_set, _owner(r), _sid(db), json={"active": False})
    _call(strategy_routes._do_event_follow_set, _owner(r), _sid(db), json={"active": True})
    pool.drain()
    (row,) = _games(db, r.id, "2026-10-11", "2026-10-11")
    eid = int(row["ref"].split(":")[1])
    _call(strategy_routes._do_demand_signal_delete, _owner(r), row["id"], method="DELETE")
    _call(strategy_routes._do_event_dismissal_restore, _owner(r), eid)
    _call(strategy_routes._do_demand_signals_save, _owner(r),
          json={"rows": [{"date": "2026-10-20", "kind": "event", "label": "Rehearsal dinner", "covers": 40}]})
    (mine,) = [s for s in demand_signals.upcoming(r.id, "2026-10-20", "2026-10-20", db_path=db)
               if s.get("label") == "Rehearsal dinner"]
    _call(strategy_routes._do_demand_signal_delete, _owner(r), mine["id"], method="DELETE")
    shown = {e["type"]: e for e in models.get_account_activity(r.id)}
    for t in ("event_follow_set", "event_game_removed", "event_game_restored", "demand_signals_saved",
              "demand_signal_deleted"):
        assert t in shown, f"{t} never reaches the owner's Account activity"
        assert shown[t]["label"] == models.ACCOUNT_EVENT_LABELS[t] and shown[t]["actor"] == "erik"
    follows = [e["detail"] for e in _activity(r.id, "event_follow_set")]
    assert follows == ["Chicago Bears followed", "Chicago Bears no longer followed"]      # newest first, by name
    assert "Rehearsal dinner on 10/20/26" in shown["demand_signal_deleted"]["detail"]


def test_the_mobile_activity_route_serves_them(db, pool, monkeypatch):
    import auth
    import mobile_api
    import strategy_routes
    monkeypatch.setattr(auth, "DB_PATH", db)
    auth.init_auth(db_path=db)
    r = _restaurant(db)
    _call(strategy_routes._do_event_follow_set, _owner(r), _sid(db), json={"active": False})
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    uid = auth.create_user(r.id, "erik", "erik@x.test", "pw", db_path=db)
    headers = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    events = app.test_client().get("/mobile/api/account/activity", headers=headers).get_json()["events"]
    assert [e["detail"] for e in events if e["type"] == "event_follow_set"] == ["Chicago Bears no longer followed"]


# ── RX-07: the events card's window starts on the restaurant's today ──────

class _Evening(datetime):
    """8:30pm Central on Sunday 10/11/26 — already Monday 10/12 on a UTC server."""
    AT = datetime(2026, 10, 12, 1, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.AT.astimezone(tz) if tz else cls.AT.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.AT.replace(tzinfo=None)


class _ServerDate(date):
    @classmethod
    def today(cls):
        return date(2026, 10, 12)


def _pin_evening(monkeypatch):
    import time_utils
    monkeypatch.setattr(time_utils, "datetime", _Evening)
    monkeypatch.setattr(_dt_mod, "date", _ServerDate)           # what `date.today()` would say on the server


def test_the_events_card_keeps_tonights_game_after_7pm_central(db, monkeypatch):
    import strategy_routes
    r = _restaurant(db)
    _pin_evening(monkeypatch)
    body, status = _call(strategy_routes._do_demand_signals_get, _owner(r), path="/labor/demand-signals",
                         method="GET")
    assert status == 200
    assert "2026-10-11" in {s["date"] for s in body["signals"]}, "tonight's game is still on tonight's card"


# ── RX-06: the market history is dated on the restaurant's clock ──────────

def _pin_sunday_evening(monkeypatch):
    """8:30pm Central on Sunday 10/4/26 (ISO week 40) — Monday 10/5 (week 41) in UTC."""
    class _Sun(datetime):
        AT = datetime(2026, 10, 5, 1, 30, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.AT.astimezone(tz) if tz else cls.AT.replace(tzinfo=None)
    import time_utils
    monkeypatch.setattr(time_utils, "datetime", _Sun)
    monkeypatch.setattr(event_memory, "datetime", _Sun)          # the server's own clock is UTC


def _weeks(db, rid):
    conn = models.get_conn(db)
    try:
        return [r["week"] for r in conn.execute("SELECT week FROM own_rating_history WHERE restaurant_id=?", (rid,))]
    finally:
        conn.close()


def test_a_places_rating_on_sunday_evening_is_this_weeks(db, monkeypatch):
    import competitor
    rid = create_restaurant(Restaurant(name="Rated", owner_email="r@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"google_place_id": "pl-1"}, db_path=db)
    _pin_sunday_evening(monkeypatch)
    competitor._remember_own_listing("pl-1", ["restaurant"], 2, rating=4.4, rating_count=210)
    assert _weeks(db, rid) == ["2026-W40"]


def test_a_business_profile_rating_on_sunday_evening_is_this_weeks(db, monkeypatch):
    import gmb

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"rating": 4.6, "userRatingCount": 380}
    rid = create_restaurant(Restaurant(name="Rated", owner_email="r@x.com", timezone="America/Chicago"), db_path=db)
    monkeypatch.setattr(gmb.requests, "get", lambda *a, **k: _Resp())
    _pin_sunday_evening(monkeypatch)
    gmb.fetch_location_rating(rid, "token", "locations/1")
    assert _weeks(db, rid) == ["2026-W40"]


def test_the_competitor_check_dates_its_market_snapshot_on_the_restaurants_clock():
    import inspect
    import competitor
    # The storing moved into _store_analysis (AI cost audit 10/7/26 #59: a
    # batched read lands there too).
    src = inspect.getsource(competitor._store_analysis)
    assert "_now_ct = restaurant_now(restaurant, naive=True)" in src
    assert "record_market_snapshot(restaurant_id, competitors, closed=_closed, at=_now_ct)" in src


# ── RX-09: one restaurant can't touch another's games (web and mobile) ─────

@pytest.fixture
def twins(db, monkeypatch):
    import auth
    import client_api
    import mobile_api
    import strategy_routes
    monkeypatch.setattr(auth, "DB_PATH", db)
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app


def _bearer(db, rid, name):
    import auth
    uid = auth.create_user(rid, name, f"{name}@x.test", "pw", db_path=db)
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def _web_as(monkeypatch, rid):
    import auth
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 7, "restaurant_id": rid, "is_admin": 0, "role": "client",
                                 "username": "other", "email": "o@x.test"})


@pytest.mark.parametrize("twin", ["web", "mobile"])
def test_another_restaurant_cannot_put_back_or_delete_a_restaurants_games(db, twins, pool, monkeypatch, twin):
    import strategy_routes
    a = _restaurant(db, name="A Co")
    b = _restaurant(db, name="B Co")
    (row,) = _games(db, a.id, "2026-10-11", "2026-10-11")
    eid = int(row["ref"].split(":")[1])
    _call(strategy_routes._do_demand_signal_delete, _owner(a), row["id"], method="DELETE")
    (other,) = _games(db, a.id, "2026-10-18", "2026-10-18")
    client = twins.test_client()
    if twin == "web":
        _web_as(monkeypatch, b.id)
        prefix, headers = "/api", {}
    else:
        prefix, headers = "/mobile/api", _bearer(db, b.id, "bowner")
    got = client.post(f"{prefix}/labor/event-dismissals/{eid}/restore", headers=headers)
    assert got.status_code == 404 and got.get_json()["error"] == "That game isn't removed here."
    assert store.dismissed(a.id, db_path=db) == {eid}, "A's removal is intact"
    got = client.delete(f"{prefix}/labor/demand-signals/{other['id']}", headers=headers)
    assert got.status_code == 404 and got.get_json()["error"] == "Not found."
    assert [g["id"] for g in _games(db, a.id, "2026-10-18", "2026-10-18")] == [other["id"]]
    assert store.dismissed(a.id, db_path=db) == {eid} and store.dismissed(b.id, db_path=db) == set()
    assert pool.calls == []


# ── RX-10: iOS words a catalog game and has the follow switch ──────────────

def _swift(name):
    with open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Labor", name), encoding="utf-8") as f:
        return f.read()


def test_ios_words_a_catalog_game_as_the_web_and_has_the_follow_switch():
    section, vm = _swift("DemandSignalsSection.swift"), _swift("ScheduleSetupViewModel.swift")
    assert '"from \\(source)"' not in section
    assert '"from a calendar you follow"' in section
    web = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "from a calendar you follow" in web
    assert "followsCard" in section and '"Stop following"' in section and '"Follow"' in section
    assert '"/mobile/api/labor/event-follows/\\(seriesId)", method: .post' in vm
    assert "case ok, signals, error, follows" in vm and "struct EventFollow: Decodable" in vm
    # The follows ride the events card's GET (one source, no second read).
    assert ("/labor/demand-signals", ["GET"], __import__("strategy_routes")._do_demand_signals_get,
            "demand_signals_get") in __import__("strategy_routes")._ROUTES
