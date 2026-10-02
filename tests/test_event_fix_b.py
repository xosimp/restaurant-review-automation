"""Event Intelligence re-audit fixes, memory and baselines (B), 10/1/26.

One quiet-game test everywhere: a frequent series' game this restaurant has
not measured to matter is context — no planned lift, no held cut, no
unasked line (the brief's "Remembered:", the pre-shift notes) — its nights
stay in baselines, a closer's note naming it is that game, home and road
are measured apart, and a game turning loud re-records what it changed.
"""
import json
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import closeout
import demand_signals
import event_memory
import models
from event_intel import engine, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ST_CHARLES = (41.9142, -88.3087)
BULLS_HOME = "2026-11-13"        # Fri: Bulls at the United Center, nothing else
BULLS_HOME_2 = "2026-11-20"      # Fri: Bulls at the United Center
BULLS_ROAD = "2026-11-16"        # Mon: Bulls on the road, nothing else
BULLS_ROAD_2 = "2026-11-18"      # Wed: Bulls on the road
HAWKS_HOME = "2026-11-19"        # Thu: Blackhawks at the United Center, nothing else
BEARS_HOME = "2026-10-22"        # Thu: Bears at Soldier Field


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
    import weather
    monkeypatch.setattr(weather, "forecast_for_day", lambda *a, **k: None)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    event_memory._matters_memo.clear()
    yield db_path
    event_memory._matters_memo.clear()


def _restaurant(db):
    rid = create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _event(db, slug, iso, side=None):
    s = store.series_by_slug(slug, db_path=db)
    return [e for e in store.events_for([s["id"]], iso, iso, db_path=db) if side in (None, e["home_away"])][0]


def _copy(db, rid, e):
    """The sync's copy of a followed game in demand_signals."""
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, source, ref) VALUES (?,?,?,?,?,?)",
                  (rid, e["event_date"], "event", engine.label_for(e), "events", f"event:{e['id']}"))
        c.commit()
    finally:
        c.close()


def _owner_event(db, rid, iso, label):
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label) VALUES (?,?,?,?)",
                  (rid, iso, "event", label))
        c.commit()
    finally:
        c.close()


def _outcomes(db, rid, label, lifts, start="2026-01-0", kind="event"):
    c = models.get_conn(db)
    try:
        for k, lift in enumerate(lifts, 1):
            d = f"{start}{k}"
            c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, "
                      "lift_pct, net, baseline, baseline_n) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rid, d, date.fromisoformat(d).strftime("%A"), kind, label, label, lift, 100 + lift, 100, 8))
        c.commit()
    finally:
        c.close()
    event_memory.refresh_effects(rid, {label}, db_path=db)
    event_memory._matters_memo.clear()


def _close_out(db, rid, iso, influence):
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO close_outs (restaurant_id, business_date, influence) VALUES (?,?,?)",
                  (rid, iso, influence))
        c.commit()
    finally:
        c.close()


def _flag(e):
    return {"kind": "event", "label": event_memory.normalise_label(engine.label_for(e)), "raw": engine.label_for(e),
            "ref": f"event:{e['id']}"}


# ── P1-01 + P4-10: a game measured NOT to matter is context, and holds no cut ──

def test_a_game_measured_not_to_matter_is_context_and_holds_no_cut(db):
    import strategy_jobs
    r = _restaurant(db)
    bulls = _event(db, "nba-chicago-bulls", BULLS_HOME, "home")
    bears = _event(db, "nfl-chicago-bears", BEARS_HOME, "home")
    _copy(db, r.id, bulls)
    _copy(db, r.id, bears)
    # Three Bulls home nights at +1%, +2%, -1%: the record applies (3 nights)
    # and says nothing (+1%, under EFFECT_FLOOR_PCT).
    _outcomes(db, r.id, "bulls united center", [1.0, 2.0, -1.0])
    assert event_memory.measured_effect(r.id, engine.label_for(bulls), db_path=db)["applies"]
    assert not event_memory.label_matters(r.id, engine.label_for(bulls), db_path=db)
    d = demand_signals.by_date(r.id, [BULLS_HOME], db_path=db)[BULLS_HOME]
    assert d["labels"] == [] and d["lift_pct"] is None and not d.get("assumed") and not d.get("lift_source")
    assert "no measurable effect to plan on" in d["context"][0]
    assert strategy_jobs._pulse_suppressed(r, date.fromisoformat(BULLS_HOME), db_path=db) is None
    # The schedule reads it as context: "staff a usual Friday".
    block = demand_signals.prompt_block({BULLS_HOME: d}, [BULLS_HOME])
    assert "staff a usual Friday" in block and "expect about" not in block
    # An infrequent series (the Bears) measured at +2% is the same: the
    # forecast never applies it (effects_for_day), so nothing plans on it.
    _outcomes(db, r.id, "bears soldier field", [2.0, 3.0, 1.0], start="2025-10-1")
    d = demand_signals.by_date(r.id, [BEARS_HOME], db_path=db)[BEARS_HOME]
    assert d["labels"] == [] and d.get("context")
    # Measured to matter (+20%): now a planned lift, and the cut is held.
    c = models.get_conn(db)
    c.execute("UPDATE event_outcomes SET lift_pct=20 WHERE restaurant_id=? AND label='bulls united center'", (r.id,))
    c.commit()
    c.close()
    event_memory.refresh_effects(r.id, {"bulls united center"}, db_path=db)
    event_memory._matters_memo.clear()
    d = demand_signals.by_date(r.id, [BULLS_HOME], db_path=db)[BULLS_HOME]
    assert d["lift_pct"] == 20 and d["lift_source"] == "measured" and not d.get("context")
    assert strategy_jobs._pulse_suppressed(r, date.fromisoformat(BULLS_HOME), db_path=db)


def test_one_test_decides_the_forecast_the_headline_and_the_schedule():
    eff = {"applies": True, "median_lift_pct": 4.9}
    assert not event_memory.effect_matters(eff)
    assert event_memory.effect_matters(dict(eff, median_lift_pct=-5.0))
    assert not event_memory.effect_matters(dict(eff, applies=False, median_lift_pct=40.0))
    assert not event_memory.effect_matters(None)
    import inspect
    assert "effect_matters(measured_effect(" in inspect.getsource(event_memory.label_matters)
    assert 'not e["applies"] or abs(e["median_lift_pct"]) < EFFECT_FLOOR_PCT' in \
        inspect.getsource(event_memory.effects_for_day)                  # effect_matters, spelled out
    assert "event_memory.effect_matters(measured)" in inspect.getsource(demand_signals.by_date)


# ── P1-02: the close-out prefill, and a closer's note naming the game ──────

def test_the_close_out_prefill_leaves_followed_games_out(db):
    r = _restaurant(db)
    _copy(db, r.id, _event(db, "nba-chicago-bulls", BULLS_HOME, "home"))
    _owner_event(db, r.id, BULLS_HOME, "Private party")
    s = closeout.suggestions(r.id, BULLS_HOME, db_path=db)
    assert s["influence"] == "Private party"


def test_a_closers_note_naming_a_quiet_game_is_that_game(db):
    r = _restaurant(db)
    bulls = _event(db, "nba-chicago-bulls", BULLS_HOME, "home")
    _copy(db, r.id, bulls)
    # The old prefill's words, kept by the closer, and a closer's own words.
    _close_out(db, r.id, BULLS_HOME, "Bulls home game · United Center; Bulls game")
    fl = event_memory.flags_for(r.id, [BULLS_HOME], db_path=db)[BULLS_HOME]
    notes = [f for f in fl if f["kind"] == "influence"]
    assert len(notes) == 2 and all(f["ref"] == f"event:{bulls['id']}" for f in notes)
    quiet = event_memory.quiet_flags(r.id, fl, db_path=db)
    assert all(id(f) in quiet for f in fl)
    # So the night stays in the baselines (for a night that is not its kin).
    assert event_memory.ordinary_nights(r.id, {BULLS_HOME: fl}, db_path=db) == {BULLS_HOME}
    # And it confounds nothing: a rain note the same night stands alone.
    marks = event_memory._confounding(
        fl + [{"kind": "rain", "label": "rain"}], quiet=quiet, games={id(f) for f in fl})
    assert marks["rain"] == (0, None)
    # Once the Bulls matter here, the note is as loud as the game.
    _outcomes(db, r.id, "bulls united center", [18.0, 20.0, 22.0])
    fl = event_memory.flags_for(r.id, [BULLS_HOME], db_path=db)[BULLS_HOME]
    assert event_memory.quiet_flags(r.id, fl, db_path=db) == set()
    assert event_memory.ordinary_nights(r.id, {BULLS_HOME: fl}, db_path=db) == set()


def test_a_closers_note_on_a_night_with_no_game_is_its_own_thing(db):
    r = _restaurant(db)
    _close_out(db, r.id, BULLS_HOME, "Bulls game")          # not followed here: nothing flagged
    fl = event_memory.flags_for(r.id, [BULLS_HOME], db_path=db)[BULLS_HOME]
    assert [f for f in fl if f["kind"] == "influence"] and not any(f.get("ref") for f in fl)
    assert event_memory.ordinary_nights(r.id, {BULLS_HOME: fl}, db_path=db) == set()


# ── P4-01: no "Remembered:" for a quiet game ───────────────────────────────

def test_memory_lines_say_nothing_unasked_about_a_quiet_game(db):
    r = _restaurant(db)
    bulls = _event(db, "nba-chicago-bulls", BULLS_HOME, "home")
    _copy(db, r.id, bulls)
    _owner_event(db, r.id, BULLS_HOME, "Trivia night")
    _outcomes(db, r.id, "bulls united center", [1.0, 2.0, -1.0])
    _outcomes(db, r.id, "trivia night", [12.0, 10.0, 14.0], start="2026-02-0")

    def lines(surface):
        req = SimpleNamespace(restaurant_id=r.id, db_path=db, now=datetime(2026, 11, 13, 9), subjects=(),
                              surface=surface)
        return " | ".join(x["text"] for x in event_memory.memory_lines(req))
    for surface in ("brief", "dsr_narrative", "schedule", "labor_read", "weekly_plan"):
        got = lines(surface)
        assert "Bulls" not in got and "Trivia night" in got, surface
    assert "Bulls home game" in lines("ask")             # asked: still there


# ── P1-08 + P4-02: the pre-shift notes ─────────────────────────────────────

def test_the_lineup_drops_quiet_games_and_says_the_rest_as_games(db, monkeypatch):
    import labor
    import preshift
    monkeypatch.setattr(labor, "build_demand_forecast", lambda *a, **k: {})
    r = _restaurant(db)
    _copy(db, r.id, _event(db, "nba-chicago-bulls", BULLS_HOME, "home"))
    _owner_event(db, r.id, BULLS_HOME, "Private party")
    bears = _event(db, "nfl-chicago-bears", BEARS_HOME, "home")
    _copy(db, r.id, bears)

    def text(iso):
        return " ".join(i["text"] for i in preshift.build(r.id, day=date.fromisoformat(iso), db_path=db)["items"])
    t = text(BULLS_HOME)
    assert "Bulls" not in t and "On the books tonight: Private party." in t
    t = text(BEARS_HOME)
    assert f"Game tonight: Bears vs {bears['opponent']}" in t and "On the books" not in t
    # Measured to matter: the Bulls game is said, as a game.
    _outcomes(db, r.id, "bulls united center", [18.0, 20.0, 22.0])
    assert "Game tonight: Bulls vs" in text(BULLS_HOME)


# ── P4-06: home and road kept apart, not out of each other's baselines ─────

def test_a_home_games_baseline_keeps_the_series_road_nights_and_the_reverse(db):
    r = _restaurant(db)
    home, home2 = _event(db, "nba-chicago-bulls", BULLS_HOME, "home"), _event(db, "nba-chicago-bulls", BULLS_HOME_2, "home")
    road, road2 = _event(db, "nba-chicago-bulls", BULLS_ROAD, "away"), _event(db, "nba-chicago-bulls", BULLS_ROAD_2, "away")
    hawks = _event(db, "nhl-chicago-blackhawks", HAWKS_HOME, "home")
    nights = {BULLS_HOME: [_flag(home)], BULLS_HOME_2: [_flag(home2)], BULLS_ROAD: [_flag(road)],
              BULLS_ROAD_2: [_flag(road2)], HAWKS_HOME: [_flag(hawks)]}
    # A Bulls home night: its own label's nights and the United Center's are
    # kin; the Bulls' road nights are ordinary.
    ok = event_memory.ordinary_nights(r.id, {d: f for d, f in nights.items() if d != BULLS_HOME}, db_path=db,
                                      tonight=nights[BULLS_HOME])
    assert ok == {BULLS_ROAD, BULLS_ROAD_2}
    # A Bulls road night: the road nights are kin; home nights are ordinary.
    ok = event_memory.ordinary_nights(r.id, {d: f for d, f in nights.items() if d != BULLS_ROAD}, db_path=db,
                                      tonight=nights[BULLS_ROAD])
    assert ok == {BULLS_HOME, BULLS_HOME_2, HAWKS_HOME}


# ── P4-08: a state change re-records; a game's own rows never read its state ──

def _cursor(db, key):
    c = models.get_conn(db)
    try:
        row = c.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        c.close()


def test_a_label_turning_loud_queues_its_history_for_re_recording(db, monkeypatch):
    r = _restaurant(db)
    key = f"{event_memory.RERECORD_PREFIX}{r.id}"
    _outcomes(db, r.id, "bulls united center", [1.0, 2.0])
    _outcomes(db, r.id, "bulls united center", [1.0], start="2026-03-0")
    assert _cursor(db, key) is None                         # applies, but +1%: still quiet, no change
    _outcomes(db, r.id, "bulls united center", [30.0, 30.0, 30.0], start="2026-04-0")
    assert _cursor(db, key) is not None                     # quiet -> loud: queued
    # Drained through the backfill: a finished backfill restarts from the top.
    today = date(2026, 11, 20)
    floor = today - __import__("datetime").timedelta(days=event_memory.BACKFILL_DAYS)
    seen = []
    monkeypatch.setattr(event_memory, "record_weather", lambda *a, **k: {"recorded": 0})
    monkeypatch.setattr(event_memory, "record_night", lambda rid, d, db_path=None: seen.append(str(d)) or {})
    monkeypatch.setattr(event_memory, "_history_nights", lambda rid, s, e, db_path=None: [e, s])
    bf = f"{event_memory.BACKFILL_CURSOR_PREFIX}{r.id}"
    # Under way (the cursor above the floor): it finishes first.
    event_memory._backfill_cursor(r.id, db_path=db, value="2026-06-01")
    event_memory.remember_restaurant(r, today=today, db_path=db)
    assert _cursor(db, key) is not None and "2026-05-31" in seen
    # Finished (the cursor at the floor): restarted from the top this pass.
    seen.clear()
    event_memory._backfill_cursor(r.id, db_path=db, value=floor.isoformat())
    event_memory.remember_restaurant(r, today=today, db_path=db)
    assert _cursor(db, key) is None
    assert "2026-11-12" in seen                              # yesterday - RECENT_NIGHTS: the top again
    assert _cursor(db, bf) is not None


def test_a_games_own_confounding_never_reads_its_own_state():
    bulls = {"kind": "event", "label": "bulls united center"}
    hawks = {"kind": "event", "label": "blackhawks road"}
    rain = {"kind": "rain", "label": "rain"}
    flags, games = [bulls, hawks, rain], {id(bulls), id(hawks)}
    as_quiet = event_memory._confounding(flags, quiet={id(bulls), id(hawks)}, games=games)
    as_loud = event_memory._confounding(flags, quiet={id(hawks)}, games=games)
    assert as_quiet["bulls united center"] == as_loud["bulls united center"]
    assert json.loads(as_loud["bulls united center"][1]) == ["blackhawks road", "rain"]
    # Rain: confounded only by a loud game, never by a quiet one.
    assert as_quiet["rain"] == (0, None)
    assert as_loud["rain"] == (1, json.dumps(["bulls united center"]))


def test_record_night_marks_one_rule_and_boot_never_undoes_it(db, monkeypatch):
    r = _restaurant(db)
    bulls = _event(db, "nba-chicago-bulls", BULLS_HOME, "home")
    _copy(db, r.id, bulls)
    _close_out(db, r.id, BULLS_HOME, "rain")
    monkeypatch.setattr(event_memory, "measure_night", lambda *a, **k: {
        "net": 110.0, "baseline": 100.0, "baseline_n": 8, "lift_pct": 10.0, "basis": "net", "source": "x"})

    def marks():
        c = models.get_conn(db)
        try:
            return {row["label"]: (row["confounded"], row["co_labels"]) for row in c.execute(
                "SELECT label, confounded, co_labels FROM event_outcomes WHERE restaurant_id=? AND business_date=?",
                (r.id, BULLS_HOME))}
        finally:
            c.close()
    event_memory.record_night(r.id, BULLS_HOME, db_path=db)
    quiet = marks()
    assert quiet["rain"] == (0, None) and quiet["bulls united center"] == (1, json.dumps(["rain"]))
    # Boot's one-time re-mark leaves a night record_night marked alone.
    assert event_memory.rekey_record(db)["marked"] == 0 and marks() == quiet
    # The Bulls turn loud: the rain row now carries them; the Bulls' own row
    # is what it was.
    _outcomes(db, r.id, "bulls united center", [30.0, 30.0, 30.0], start="2026-04-0")
    event_memory.record_night(r.id, BULLS_HOME, db_path=db)
    loud = marks()
    assert loud["bulls united center"] == quiet["bulls united center"]
    assert loud["rain"] == (1, json.dumps(["bulls united center"]))
    # A night recorded before record_night marked its own (9/29/26) is still
    # brought to the rule at boot.
    c = models.get_conn(db)
    c.execute("UPDATE event_outcomes SET confounded=0, co_labels=NULL, recorded_at='2026-09-01 00:00:00' "
              "WHERE restaurant_id=? AND business_date=?", (r.id, BULLS_HOME))
    c.commit()
    c.close()
    assert event_memory.rekey_record(db)["marked"] == 2
