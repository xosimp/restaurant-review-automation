"""Event Intelligence re-audit 2 fixes, labels and baselines (W1), 10/1/26.

  R1-02 / R4-01 / RX-02 / R3-08  a game's season class is part of its label
          (preseason, playoff, special), so no label reader — the quiet
          test, by_date, the forecast, the baselines, effect_for and the
          headline — pools one class's nights with another's; game_class
          keeps the playoffs and special games apart too
  R1-01 / RX-05   removing a past game (or a listed event) re-measures its night
  R1-03           clearing a status never marks an unresolved game completed
  R1-04           the close-out prefill and a kept note never re-flag what the
                  night already carries
  R2-04 / R1-06 / R4-05   last_like never crosses the season class
  R4-03 / R1-05   one clean-nights rule, on the floor `applies` decides by
  R4-06           rekey_record reads only what it has not read before

Every test pins the clock (store.local_today, engine._today): nothing here
depends on the real date.
"""
import inspect
import sys
from datetime import date

import pytest

import closeout
import demand_signals
import event_memory
import models
from event_intel import engine, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ST_CHARLES = (41.9142, -88.3087)
TODAY = date(2026, 10, 12)


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
    monkeypatch.setattr(store, "local_today", lambda tz=None: TODAY)
    monkeypatch.setattr(engine, "_today", lambda restaurant: TODAY)
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


def _games(db, slug, season_type=None, side=None):
    s = store.series_by_slug(slug, db_path=db)
    return [e for e in store.events_for([s["id"]], None, None, db_path=db)
            if season_type in (None, e["season_type"]) and side in (None, e["home_away"])]


def _on(db, slug, iso):
    return [e for e in _games(db, slug) if e["event_date"] == iso][0]


def _rows(db, rid, label, dated, kind="event", confounded=0):
    """event_outcomes rows: {iso: lift} under `label`."""
    c = models.get_conn(db)
    try:
        for d, lift in dated.items():
            c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, "
                      "lift_pct, net, baseline, baseline_n, confounded) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, d, date.fromisoformat(d).strftime("%A"), kind, label, label, lift, 100 + lift, 100, 8,
                       confounded))
        c.commit()
    finally:
        c.close()
    event_memory.refresh_effects(rid, {label}, db_path=db)
    event_memory._matters_memo.clear()


def _series_of(n, start="2026-0"):
    """n dates in early 2026 (Jan-Mar, the 2nd-9th: never a payday)."""
    return [f"{start}{1 + k // 8}-0{2 + k % 8}" for k in range(n)]


def _copy(db, rid, e, label=None):
    c = models.get_conn(db)
    try:
        cur = c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, source, ref) "
                        "VALUES (?,?,?,?,?,?)", (rid, e["event_date"], "event", label or engine.label_for(e),
                                                 "events", f"event:{e['id']}"))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _signal(db, rid, iso, kind, label, source="manual", covers=None):
    c = models.get_conn(db)
    try:
        cur = c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, source, covers) "
                        "VALUES (?,?,?,?,?,?)", (rid, iso, kind, label, source, covers))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _close_out(db, rid, iso, influence):
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO close_outs (restaurant_id, business_date, influence) VALUES (?,?,?)",
                  (rid, iso, influence))
        c.commit()
    finally:
        c.close()


def _outcome_labels(db, rid, iso):
    c = models.get_conn(db)
    try:
        return {r["label"] for r in c.execute("SELECT label FROM event_outcomes WHERE restaurant_id=? AND "
                                              "business_date=?", (rid, iso))}
    finally:
        c.close()


# ── R1-02 / R4-01 / RX-02: the season class is part of the label ──────────

def test_a_games_label_carries_its_season_class(db):
    pre = _games(db, "nba-chicago-bulls", "preseason", "home")[0]
    reg = _games(db, "nba-chicago-bulls", "regular", "home")[0]
    sox = _games(db, "mlb-chicago-white-sox", "postseason", "home")[0]
    road = _games(db, "nba-chicago-bulls", "preseason", "away")[0]
    assert engine.label_for(reg) == "Bulls home game · United Center"
    assert engine.label_for(pre) == "Bulls preseason home game · United Center"
    assert engine.label_for(road) == "Bulls preseason road game"
    assert engine.label_for(sox) == "White Sox playoff home game · Rate Field"
    cup = dict(reg, season_type="special", short_name="Fire", venue="SeatGeek Stadium")
    assert event_memory.normalise_label(engine.label_for(cup)) == "fire special seatgeek stadium"
    # The class word survives the four-word cap; "playoffs" is "playoff".
    assert event_memory.normalise_label("Red Star Big Club preseason home game · Park") == "red star club preseason"
    assert event_memory.normalise_label("Sox playoffs") == "sox playoff"
    # And the game class keeps every season class apart, as the label does.
    classes = {engine.game_class(dict(reg, season_type=t)) for t in ("regular", "preseason", "postseason", "special")}
    assert len(classes) == 4
    assert not engine.same_kind(reg, dict(reg, season_type="postseason"))


def test_a_regular_season_label_never_reads_another_classes_nights_nor_the_reverse(db):
    r = _restaurant(db)
    regs, pres, plays, cups = _series_of(4), _series_of(3, "2025-0"), _series_of(3, "2024-0"), _series_of(3, "2023-0")
    _rows(db, r.id, "bulls united center", {d: 2.0 for d in regs})
    _rows(db, r.id, "bulls preseason united center", {d: 20.0 for d in pres})
    _rows(db, r.id, "bulls playoff united center", {d: 40.0 for d in plays})
    _rows(db, r.id, "bulls special united center", {d: -10.0 for d in cups})
    # The regular label's words are a subset of every other class's: the
    # subset match alone pooled them all.
    reg = event_memory.measured_effect(r.id, "Bulls home game · United Center", db_path=db)
    assert reg["n"] == 4 and reg["median_lift_pct"] == 2.0 and sorted(reg["dates"]) == sorted(regs)
    pre = event_memory.measured_effect(r.id, "Bulls preseason home game · United Center", db_path=db)
    assert pre["n"] == 3 and pre["median_lift_pct"] == 20.0
    assert event_memory.measured_effect(r.id, "Bulls playoff home game · United Center", db_path=db)["n"] == 3
    assert event_memory.measured_effect(r.id, "Bulls special home game · United Center",
                                        db_path=db)["median_lift_pct"] == -10.0
    assert event_memory.label_counts("bulls", "bulls united center")
    assert not event_memory.label_counts("bulls", "bulls preseason united center")
    assert "label_counts(want" in inspect.getsource(event_memory.measured_effect)


def test_preseason_nights_first_never_decide_the_regular_season(db):
    # RX-02: the Bulls' first measured home nights are their October
    # preseason. They used to make the regular-season label n=3 at +1% —
    # quiet — and every regular-season game was said and judged on them.
    r = _restaurant(db)
    pre_games = _games(db, "nba-chicago-bulls", "preseason", "home")
    _rows(db, r.id, "bulls preseason united center",
          {e["event_date"]: lift for e, lift in zip(pre_games, (-4.0, 2.0, 1.0))})
    reg = [e for e in _games(db, "nba-chicago-bulls", "regular", "home") if e["event_date"] > "2026-11-01"][0]
    assert event_memory.measured_effect(r.id, engine.label_for(reg), db_path=db) is None
    assert engine.effect_for(r.id, reg, db_path=db) is None
    pre = pre_games[-1]
    eff = engine.effect_for(r.id, pre, db_path=db)
    assert eff["n"] == 3 and eff["median_lift_pct"] == 1.0
    assert eff["basis"].startswith("Bulls preseason home games have run +1%")


def test_a_frequent_series_preseason_game_has_one_test_everywhere(db):
    # Scenario B: preseason measured big, the regular season quiet. The
    # headline, effect_for, the quiet test, by_date and the forecast all
    # read the same class's nights.
    r = _restaurant(db)
    _rows(db, r.id, "bulls united center", {d: 2.0 for d in _series_of(12)})
    _rows(db, r.id, "bulls preseason united center", {d: lift for d, lift in
                                                      zip(_series_of(3, "2025-0"), (18.0, 20.0, 22.0))})
    pre = _games(db, "nba-chicago-bulls", "preseason", "home")[-1]
    reg = [e for e in _games(db, "nba-chicago-bulls", "regular", "home") if e["event_date"] > "2026-11-01"][0]
    assert engine.headline(r.id, pre, db_path=db) is True
    assert engine.headline(r.id, reg, db_path=db) is False
    eff = engine.effect_for(r.id, pre, db_path=db)
    assert eff["median_lift_pct"] == 20.0 and eff["n"] == 3
    assert event_memory.effect_matters(eff) is engine.headline(r.id, pre, db_path=db)
    flag = {"kind": "event", "label": event_memory.normalise_label(engine.label_for(pre)),
            "raw": engine.label_for(pre), "ref": f"event:{pre['id']}"}
    assert event_memory.quiet_flags(r.id, [flag], db_path=db) == set()                # loud, as the headline
    assert not event_memory.quiet_catalog(r.id, engine.label_for(pre), f"event:{pre['id']}", db_path=db)
    assert event_memory.quiet_catalog(r.id, engine.label_for(reg), f"event:{reg['id']}", db_path=db)
    _copy(db, r.id, pre)
    _copy(db, r.id, reg)
    got = demand_signals.by_date(r.id, [pre["event_date"], reg["event_date"]], db_path=db)
    assert got[pre["event_date"]]["lift_pct"] == 20 and got[pre["event_date"]]["lift_source"] == "measured"
    assert not got[reg["event_date"]]["labels"] and "+2% here over 12 nights" in got[reg["event_date"]]["context"][0]
    fx = event_memory.effects_for_day(r.id, pre["event_date"], db_path=db)
    assert fx["pct"] == 20.0 and fx["applied"][0]["n"] == 3
    assert event_memory.effects_for_day(r.id, reg["event_date"], db_path=db) is None


def test_playoff_nights_never_trip_the_regular_seasons_drift_rule(db):
    # Scenario C: a playoff run's last three nights all above every older
    # regular-season night switched the label to the recent median.
    r = _restaurant(db)
    _rows(db, r.id, "bulls united center", {d: lift for d, lift in zip(_series_of(6), (2, 3, 1, 2, 4, 3))})
    _rows(db, r.id, "bulls playoff united center", {"2026-05-04": 30.0, "2026-05-06": 35.0, "2026-05-08": 45.0})
    reg = event_memory.measured_effect(r.id, "Bulls home game · United Center", db_path=db)
    assert reg["drift"] is None and reg["n"] == 6 and reg["median_lift_pct"] == 2.5
    assert not event_memory.label_matters(r.id, "Bulls home game · United Center", db_path=db)


def test_a_sync_relabels_old_copies_and_remeasures_their_nights(db, monkeypatch):
    # Existing data: a preseason night copied and measured under the old,
    # class-less label. The next sync moves the copy and re-records the
    # night, so its row leaves the regular-season label.
    r = _restaurant(db)
    sid = store.series_by_slug("nba-chicago-bulls", db_path=db)["id"]
    store.set_follow(r.id, sid, True, db_path=db)
    pre = [e for e in _games(db, "nba-chicago-bulls", "preseason", "home") if e["event_date"] < TODAY.isoformat()][0]
    _copy(db, r.id, pre, label="Bulls home game · United Center")
    _rows(db, r.id, "bulls united center", {pre["event_date"]: 15.0})
    monkeypatch.setattr(event_memory, "measure_night", lambda *a, **k: {
        "net": 115.0, "baseline": 100.0, "baseline_n": 8, "lift_pct": 15.0, "basis": "net", "source": "x"})
    got = engine.sync_restaurant(r, today=TODAY, db_path=db)
    assert got["moved"] >= 1 and got["nights_recorded"] >= 1
    c = models.get_conn(db)
    try:
        label = c.execute("SELECT label FROM demand_signals WHERE restaurant_id=? AND ref=?",
                          (r.id, f"event:{pre['id']}")).fetchone()["label"]
        effects = {row["label"] for row in c.execute("SELECT label FROM event_effects WHERE restaurant_id=?",
                                                      (r.id,))}
    finally:
        c.close()
    assert label == "Bulls preseason home game · United Center"
    assert _outcome_labels(db, r.id, pre["event_date"]) == {"bulls preseason united center"}
    assert "bulls united center" not in effects
    past_pre = [e for e in _games(db, "nba-chicago-bulls", "preseason", "home") if e["event_date"] < TODAY.isoformat()]
    assert event_memory.measured_effect(r.id, engine.label_for(pre), db_path=db)["n"] == len(past_pre)


# ── R1-01 / RX-05: removing a past game re-measures its night ─────────────

def test_removing_a_past_game_remeasures_its_night(db):
    r = _restaurant(db)
    bears = [e for e in _games(db, "nfl-chicago-bears", "regular", "home") if e["event_date"] < "2026-10-01"][0]
    sig = _copy(db, r.id, bears)
    _rows(db, r.id, "bears soldier field", {bears["event_date"]: 31.0})
    assert event_memory.measured_effect(r.id, engine.label_for(bears), db_path=db)["n"] == 1
    out = demand_signals.delete(r.id, sig, db_path=db)
    assert out["event_id"] == bears["id"]
    # The night lost its only flag: its row and the label's summary go.
    assert _outcome_labels(db, r.id, bears["event_date"]) == set()
    assert event_memory.measured_effect(r.id, engine.label_for(bears), db_path=db) is None
    assert engine.past_games(r.id, _on(db, "nfl-chicago-bears", "2026-10-04"), db_path=db) == []


def test_removal_remeasures_past_event_nights_only(db, monkeypatch):
    r = _restaurant(db)
    seen = []
    monkeypatch.setattr(event_memory, "record_night", lambda rid, day, db_path=None: seen.append(day) or
                        {"recorded": 0})
    past = _signal(db, r.id, "2026-10-02", "event", "Street fair")
    ahead = _signal(db, r.id, "2026-10-20", "event", "Street fair")
    booked = _signal(db, r.id, "2026-10-03", "reservations", "Reservations", covers=60)
    for sid in (past, ahead, booked):
        demand_signals.delete(r.id, sid, db_path=db)
    assert seen == ["2026-10-02"]
    # Owed nights stay queued for the sync: a removal never drains them.
    engine._record_queue(r.id, db, value=["2026-09-01", "2026-09-02"])
    demand_signals.delete(r.id, _signal(db, r.id, "2026-10-05", "event", "Parade"), db_path=db)
    assert seen[-1] == "2026-10-05" and "2026-09-01" not in seen
    assert engine._record_queue(r.id, db) == ["2026-09-02", "2026-09-01"]


# ── R1-03: clearing a status never resolves an unresolved game ────────────

def test_clearing_a_status_leaves_an_if_necessary_game_with_no_result_unresolved(db):
    sox = store.series_by_slug("mlb-chicago-white-sox", db_path=db)
    g5 = [e for e in store.events_for([sox["id"]], None, None, db_path=db)
          if store.attributes_of(e).get("if_necessary") and e["home_away"] == "home"][0]
    assert g5["event_date"] < TODAY.isoformat() and not g5.get("result")
    store.edit_event(g5["id"], {"status": "postponed"}, db_path=db)
    store.edit_event(g5["id"], {}, clear=["status"], db_path=db)
    after = store.event_by_id(g5["id"], db_path=db)
    assert after["status"] == "scheduled" and store.unresolved(after, today=TODAY)
    assert not store.played(after, today=TODAY)
    # A plain past game the file still calls scheduled was played.
    plain = [e for e in store.events_for([sox["id"]], None, None, db_path=db)
             if not store.attributes_of(e).get("if_necessary") and e["event_date"] < TODAY.isoformat()
             and e["status"] == "scheduled"][0]
    store.edit_event(plain["id"], {"status": "postponed"}, db_path=db)
    store.edit_event(plain["id"], {}, clear=["status"], db_path=db)
    assert store.event_by_id(plain["id"], db_path=db)["status"] == "completed"
    # With a result entered, the if-necessary game was played.
    store.edit_event(g5["id"], {"result": "W 4-2", "status": "postponed"}, db_path=db)
    store.edit_event(g5["id"], {}, clear=["status"], db_path=db)
    assert store.event_by_id(g5["id"], db_path=db)["status"] == "completed"


# ── R1-04: the close-out never re-flags what the night carries ────────────

def test_the_prefill_offers_no_covers_booked_and_no_post(db):
    r = _restaurant(db)
    day = "2026-11-12"
    _signal(db, r.id, day, "reservations", "Reservations", covers=60)
    _signal(db, r.id, day, "post", "Post on Instagram: Taco Tuesday", source="post")
    _signal(db, r.id, day, "event", "Private party")
    assert closeout.suggestions(r.id, day, db_path=db)["influence"] == "Private party"


def test_a_kept_holiday_prefill_is_the_holiday_and_never_confounds_it(db):
    r = _restaurant(db)
    day = "2026-05-10"                                           # Mother's Day
    _close_out(db, r.id, day, "Mother's Day")
    fl = event_memory.flags_for(r.id, [day], db_path=db)[day]
    note = [f for f in fl if f["kind"] == "influence"][0]
    assert note["label"] == "mother's" and note["same"] == "holiday:mothers_day"
    marks = event_memory._confounding(fl)
    assert marks["holiday:mothers_day"] == (0, None) and marks["mother's"] == (0, None)
    # Something else that night still confounds it.
    c = models.get_conn(db)
    c.execute("UPDATE close_outs SET influence=? WHERE restaurant_id=? AND business_date=?",
              ("Mother's Day; private party", r.id, day))
    c.commit()
    c.close()
    fl = event_memory.flags_for(r.id, [day], db_path=db)[day]
    assert event_memory._confounding(fl)["holiday:mothers_day"][0] == 1


def test_a_kept_reservations_prefill_is_no_flag_and_the_night_stays_ordinary(db):
    r = _restaurant(db)
    day = "2026-11-12"                                           # a plain Thursday
    _signal(db, r.id, day, "reservations", "Reservations", covers=60)
    _close_out(db, r.id, day, "Reservations (60 covers booked)")
    fl = event_memory.flags_for(r.id, [day], db_path=db)
    assert fl[day] == []
    assert event_memory.ordinary_nights(r.id, fl, db_path=db) == {day}


def test_a_kept_campaign_sentence_is_the_campaign(db):
    r = _restaurant(db)
    day = "2026-11-12"
    _signal(db, r.id, day, "event", "Text to 412 guests to fill Thursday", source="campaign")
    _close_out(db, r.id, day, "Text to 412 guests to fill Thursday")
    fl = event_memory.flags_for(r.id, [day], db_path=db)[day]
    assert {f["kind"] for f in fl} == {"campaign", "influence"}
    marks = event_memory._confounding(fl)
    assert marks[event_memory.CAMPAIGN_LABEL[0]] == (0, None)


# ── R2-04 / R1-06 / R4-05: the last game like it keeps the class ──────────

def test_last_like_never_offers_a_preseason_game_for_a_regular_season_one(db):
    r = _restaurant(db)
    target = _on(db, "nfl-chicago-bears", "2026-10-04")         # Sunday noon, home
    _rows(db, r.id, "bears preseason soldier field", {"2026-08-15": 40.0})
    assert engine.last_like(r.id, target, db_path=db) is None
    pre = _on(db, "nfl-chicago-bears", "2026-08-15")
    later_pre = dict(pre, id=None, event_date="2026-08-20")
    assert engine.last_like(r.id, later_pre, db_path=db)["event"]["event_date"] == "2026-08-15"
    # The same class but another kickoff class stands in when it is all
    # there is; the same kind (the push's test) is preferred.
    _rows(db, r.id, "bears soldier field", {"2026-09-28": 25.0})
    assert engine.last_like(r.id, target, db_path=db)["event"]["event_date"] == "2026-09-28"
    _rows(db, r.id, "bears soldier field", {"2026-09-20": 10.0})
    assert engine.last_like(r.id, target, db_path=db)["event"]["event_date"] == "2026-09-20"


# ── R4-03 / R1-05: one clean-nights rule ──────────────────────────────────

def test_one_more_clean_night_never_switches_a_pattern_off(db):
    r = _restaurant(db)
    target = _on(db, "nfl-chicago-bears", "2027-01-03")         # home, not prime time
    _rows(db, r.id, "bears soldier field", {"2026-09-20": 20.0, "2026-10-04": 20.0})
    _rows(db, r.id, "bears soldier field", {"2026-11-22": 20.0, "2026-12-06": 20.0}, confounded=1)
    eff = engine.effect_for(r.id, target, db_path=db)
    # Two clean nights don't reach the floor `applies` decides on: every
    # night counts, and the sentence says some were mixed.
    assert eff["n"] == 4 and eff["applies"] and eff["confounded"]
    label = event_memory.measured_effect(r.id, engine.label_for(target), db_path=db)
    assert (label["n"], label["applies"], label["confounded"]) == (4, True, True)
    c = models.get_conn(db)
    c.execute("UPDATE event_outcomes SET confounded=0 WHERE restaurant_id=? AND business_date='2026-11-22'",
              (r.id,))
    c.commit()
    c.close()
    eff = engine.effect_for(r.id, target, db_path=db)
    assert eff["n"] == 3 and eff["applies"] and not eff["confounded"]
    assert "store.clean_first(hits, event_memory.EFFECT_MIN_N)" in inspect.getsource(engine.effect_for)
    assert "clean_first(" in inspect.getsource(event_memory._summary)


# ── R4-06: boot reads only what it has not read ───────────────────────────

def test_rekey_record_reads_only_rows_written_since_its_last_pass(db):
    r = _restaurant(db)
    _rows(db, r.id, "christmas", {"2025-12-25": -90.0}, kind="holiday")
    assert event_memory.rekey_record(db)["rekeyed"] == 1
    c = models.get_conn(db)
    try:
        top = c.execute("SELECT MAX(id) AS m FROM event_outcomes").fetchone()["m"]
        mark = c.execute("SELECT value FROM job_cursors WHERE key=?", (event_memory.REKEY_MARK_KEY,)).fetchone()
        assert int(mark["value"]) == top
        # A row it has already read is never read again ...
        c.execute("UPDATE event_outcomes SET label='christmas' WHERE id=?", (top,))
        c.commit()
    finally:
        c.close()
    assert event_memory.rekey_record(db) == {"rekeyed": 0, "marked": 0}
    # ... and a new one is.
    _rows(db, r.id, "new year's", {"2026-01-01": -30.0}, kind="holiday")
    assert event_memory.rekey_record(db)["rekeyed"] == 1
    assert event_memory.rekey_record(db) == {"rekeyed": 0, "marked": 0}
    assert "WHERE id > ?" in inspect.getsource(event_memory.rekey_record)
