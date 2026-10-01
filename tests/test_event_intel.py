"""event_intel — the Event Intelligence Engine (owner, 10/1/26: "a reusable
Event Intelligence Engine, not a Chicago Bears feature").

One global catalog (series → events), follows by distance (an owner's
opt-out stands), each restaurant's copy in demand_signals so every module
that already reads events becomes aware, and what the games did here —
measured from event_memory, never assumed.
"""
import json
import sys
from datetime import date, timedelta

import pytest

import models
import demand_signals
import event_memory
from event_intel import engine, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

ST_CHARLES = (41.9142, -88.3087)      # Simple EJ's, about 56 km from Soldier Field
LOS_ANGELES = (34.0522, -118.2437)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _restaurant(db, at=ST_CHARLES, name="EJ Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"latitude": at[0], "longitude": at[1]}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _bears(db):
    return store.series_by_slug("nfl-chicago-bears", db_path=db)


def test_the_2026_bears_season_is_in_the_catalog(db):
    s = _bears(db)
    assert s["name"] == "Chicago Bears" and s["category"] == "sports" and s["home_venue"] == "Soldier Field"
    conn = models.get_conn(db)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM catalog_events WHERE series_id=? ORDER BY id",
                                              (s["id"],)).fetchall()]
    finally:
        conn.close()
    assert len(rows) == 20                                    # 3 preseason + 17 regular
    assert sum(1 for r in rows if r["season_type"] == "preseason") == 3
    week18 = [r for r in rows if r["external_id"] == "2026-reg-18"][0]
    assert week18["event_date"] is None and week18["opponent"] == "Minnesota Vikings"
    prime = sorted(r["event_date"] for r in rows if r["is_primetime"])
    assert prime == ["2026-09-28", "2026-10-22", "2026-11-02", "2026-11-08", "2026-12-19"]
    done = {r["event_date"]: r["result"] for r in rows if r["status"] == "completed"}
    assert done["2026-09-28"] == "W 27-7" and len(done) == 6
    jets = [r for r in rows if r["event_date"] == "2026-10-04"][0]
    assert (jets["home_away"], jets["kickoff_local"], jets["broadcast"], jets["venue"]) == \
        ("home", "12:00", "FOX", "Soldier Field")


def test_loading_the_season_twice_changes_nothing(db):
    before = store.load_bundled(db_path=db)
    again = store.load_bundled(db_path=db)
    assert again[0]["written"] == 20 and again[0]["moved"] == 0 and before[0]["series_id"] == again[0]["series_id"]


def test_restaurants_near_a_team_follow_it_and_an_opt_out_stands(db):
    near, far = _restaurant(db), _restaurant(db, at=LOS_ANGELES, name="LA Co")
    assert engine.ensure_follows(near, db_path=db) == ["nfl-chicago-bears"]
    assert engine.ensure_follows(far, db_path=db) == []
    f = store.follows(near.id, db_path=db)[0]
    assert f["source"] == "auto" and 40 < f["distance_km"] < 70
    store.set_follow(near.id, f["series_id"], False, source="owner", db_path=db)
    assert engine.ensure_follows(near, db_path=db) == [] and store.follows(near.id, db_path=db) == []


def test_each_game_becomes_the_restaurants_own_event_and_sync_is_idempotent(db, monkeypatch):
    recorded = []
    monkeypatch.setattr(event_memory, "record_night", lambda rid, d, db_path=None: recorded.append(d) or {"recorded": 1})
    r = _restaurant(db)
    engine.ensure_follows(r, db_path=db)
    got = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert got["added"] == 19                                 # every dated game; Week 18 waits for its date
    sig = {s["date"]: s for s in demand_signals.upcoming(r.id, "2026-08-01", "2027-02-01", db_path=db)}
    assert sig["2026-10-04"]["label"] == "Bears home game · Soldier Field"
    assert sig["2026-10-11"]["label"] == "Bears road game"
    assert sig["2026-10-04"]["source"] == "events" and sig["2026-10-04"]["ref"].startswith("event:")
    # the six games already played were measured at once
    assert sorted(recorded) == ["2026-08-15", "2026-08-22", "2026-08-29", "2026-09-13", "2026-09-20", "2026-09-28"]
    again = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert (again["added"], again["moved"], again["removed"]) == (0, 0, 0)


def test_a_moved_game_moves_and_an_unfollowed_team_goes(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    r = _restaurant(db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    sid = _bears(db)["id"]
    store.upsert_events(sid, [{"external_id": "2026-reg-18", "season_type": "regular", "week": "Week 18",
                               "date": "2027-01-10", "kickoff": "12:00", "home_away": "away",
                               "opponent": "Minnesota Vikings", "venue": "U.S. Bank Stadium"}], db_path=db)
    got = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert got["added"] == 1
    assert demand_signals.upcoming(r.id, "2027-01-10", "2027-01-10", db_path=db)[0]["label"] == "Bears road game"
    store.set_follow(r.id, sid, False, db_path=db)
    gone = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert gone["removed"] == 20 and demand_signals.upcoming(r.id, "2026-08-01", "2027-02-01", db_path=db) == []


def test_an_owners_own_entry_for_the_same_night_is_never_overwritten(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    r = _restaurant(db)
    demand_signals.save(r.id, [{"date": "2026-10-04", "kind": "event", "label": "Bears home game · Soldier Field",
                                "lift_pct": 40}], db_path=db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    rows = demand_signals.upcoming(r.id, "2026-10-04", "2026-10-04", db_path=db)
    assert len(rows) == 1 and rows[0]["lift_pct"] == 40 and rows[0]["source"] == "manual"


def test_home_and_road_games_are_learned_apart():
    # event_memory drops "home"/"away" as stop words; the labels carry the
    # difference so the forecast's measured effects stay apart.
    home = event_memory.normalise_label("Bears home game · Soldier Field")
    road = event_memory.normalise_label("Bears road game")
    assert home == "bears soldier field" and road == "bears road" and home != road
    assert "bears" in home.split() and "bears" in road.split()


def _outcome(db, rid, day, lift, label="bears soldier field", net=11000.0, base=8000.0):
    conn = models.get_conn(db)
    try:
        conn.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, net, baseline, "
                     "lift_pct, covers, labor_pct) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rid, day, date.fromisoformat(day).strftime("%A"), "event", label, net, base, lift, 300, 28.0))
        conn.commit()
    finally:
        conn.close()


def test_the_effect_is_measured_here_most_specific_first(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    r = _restaurant(db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    _outcome(db, r.id, "2026-08-15", 10.0)                     # home, day
    _outcome(db, r.id, "2026-09-20", 20.0)                     # home, day
    _outcome(db, r.id, "2026-09-28", 93.0)                     # home, prime time (MNF)
    _outcome(db, r.id, "2026-09-13", 5.0, label="bears road")  # road
    _outcome(db, r.id, "2026-09-20", 50.0, label="homecoming")  # another event that night: never counted
    jets = [c for c in engine.context_for(r.id, "2026-10-04", db_path=db)][0]
    assert jets["describe"] == "Bears vs New York Jets · Sun 10/4/26 · 12pm · FOX"
    eff = jets["effect"]
    assert eff["segment"] == "Bears home games" and eff["n"] == 2 and eff["median_lift_pct"] == 15.0
    pats = [c for c in engine.context_for(r.id, "2026-10-22", db_path=db)][0]
    # one prime-time home game is not a segment yet: home games, all three
    assert pats["effect"]["segment"] == "Bears home games" and pats["effect"]["n"] == 3
    last = engine.last_like(r.id, pats["event"], db_path=db)
    assert last["event"]["event_date"] == "2026-09-28" and last["net"] == 11000.0
    assert engine.effect_for(r.id, pats["event"], db_path=db)["basis"].startswith("Bears home games have run +20%")


def test_no_measurement_no_effect(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    r = _restaurant(db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert engine.context_for(r.id, "2026-10-04", db_path=db)[0]["effect"] is None


def test_the_nightly_report_names_the_game(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    r = _restaurant(db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    from dsr import tomorrow
    items = [i for i in tomorrow.build(r, date(2026, 10, 3), facts={}, db_path=db)["items"] if i["kind"] == "event"]
    assert items and items[0]["text"].startswith("Bears vs New York Jets · Sun 10/4/26 · 12pm · FOX")
    import dsr
    from dsr import block_intel
    ctx = dsr.Context(r, date(2026, 10, 4), db_path=db)
    ev = block_intel._events(ctx, [])
    assert ev["items"][0]["label"].startswith("Bears vs New York Jets")


def test_ask_reads_the_games(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    r = _restaurant(db)
    engine.ensure_follows(r, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    _outcome(db, r.id, "2026-09-28", 93.0)
    import ask_cavnar_tools as tools
    monkeypatch.setattr(tools, "_local_today_of", lambda rid: date(2026, 10, 1))
    out = tools._read_events(r.id, days=7)
    assert out["following"][0]["name"] == "Chicago Bears"
    assert out["upcoming"][0]["what"].startswith("Bears vs New York Jets")
    assert out["upcoming"][0]["last_like_it"]["net"] == 11000.0
    assert out["recent"][0]["what"].startswith("Bears vs Philadelphia Eagles") and out["recent"][0]["result"] == "W 27-7"
    assert any(t["spec"]["name"] == "read_events" for t in tools.TOOLS)


def test_the_daily_job_is_registered_before_event_memory():
    import jobs_registry
    from pathlib import Path
    assert jobs_registry.JOBS["event_sync"]["target"] == ("event_intel.engine", "run_event_sync")
    src = (Path(__file__).resolve().parent.parent / "scheduler.py").read_text()
    assert src.index('_ops.run_job("event_sync"') < src.index('_ops.run_job("event_memory"')
