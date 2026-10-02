"""Event Intelligence re-audit fixes, playbook, brief and report (C, 10/1/26).

Each test names the re-audit finding it holds: the clean-nights rule in the
staffing plan and the rush (P2-03), a "jump" only from games that jumped
(P2-01), the extra person's start (P2-02), kickoffs on the restaurant's
clock (P2-04), one game said once in the brief (P2-06), an unplayed
if-necessary game (P2-07, P4-03), the footer's names (P2-08), the day
after's "One game" (P3-04), one figure for the headline and the alert
(P4-07), two games on one date (P4-11), the per-load cost (X-2), the one
quiet test on every unasked surface (X-11), and a game's class and ground
(A1 handoffs 6 and 8).

Nothing here reads the real calendar: every date is passed, or the clock is
pinned.
"""
import inspect
import json
import re
import sys
from datetime import date, datetime, timedelta

import pytest

import event_memory
import models
from event_intel import engine, gameday, playbook, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant
from test_event_playbook import (GAMES, SAINTS, _outcome, _restaurant, _saints, _staff, _tickets,  # noqa: F401
                                 _usual_sundays, db)

ST_CHARLES = (41.9142, -88.3087)
BULLS_PAST = ("2026-10-24", "2026-10-28", "2026-11-08", "2026-11-11")   # Bulls home, regular season
BULLS_NEXT = "2026-11-13"
SUNDAY_BOTH = "2026-11-22"         # Bears home 12:00 and Bulls home 18:00
FIRE_SOLDIER = "2026-10-10"        # Sat: Fire home at Soldier Field
FIRE_SEATGEEK = "2026-11-07"       # Sat: Fire home at SeatGeek Stadium (alt_venue)


# ── a catalog with every bundled series (the phase-4 ones too) ─────────────

@pytest.fixture
def full(db_path, monkeypatch):
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
    from event_intel import peers
    peers.invalidate()
    event_memory._matters_memo.clear()
    yield db_path
    event_memory._matters_memo.clear()
    peers.invalidate()


def _place(db, tz="America/Chicago"):
    rid = create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com", timezone=tz), db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    r = get_restaurant(rid, db_path=db)
    engine.ensure_follows(r, db_path=db)
    return r


def _event(db, slug, iso, side="home"):
    s = store.series_by_slug(slug, db_path=db)
    return [e for e in store.events_for([s["id"]], iso, iso, db_path=db) if e["home_away"] == side][0]


def _row(db, rid, day, lift, label, confounded=0, net=None):
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, lift_pct, "
                  "net, baseline, baseline_n, confounded) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, day, date.fromisoformat(day).strftime("%A"), "event", label, label, lift,
                   net if net is not None else 8000.0 * (1 + lift / 100), 8000.0, 8, confounded))
        c.commit()
    finally:
        c.close()
    event_memory.refresh_effects(rid, {label}, db_path=db)
    event_memory._matters_memo.clear()


def _bulls_matter(db, rid):
    """Bulls home nights: clean +2 and +4, and +12 and +16 on nights with
    something else on too. Two clean nights are short of EFFECT_MIN_N, so the
    label's figure is all four: a median of +8%, past the floor."""
    for day, lift, conf in zip(BULLS_PAST, (2.0, 4.0, 12.0, 16.0), (0, 0, 1, 1)):
        _row(db, rid, day, lift, "bulls united center", confounded=conf)


def _punch(db, rid, day, role, who, start):
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
                  "shift_start, shift_end, actual_hours, source) VALUES (?,?,?,?,?,?,?,?,?)",
                  (rid, day, who, who.lower(), role, start, "23:00", 7.0, "rpower"))
        c.commit()
    finally:
        c.close()


def _confound(db, rid, day):
    c = models.get_conn(db)
    try:
        c.execute("UPDATE event_outcomes SET confounded=1 WHERE restaurant_id=? AND business_date=?", (rid, day))
        c.commit()
    finally:
        c.close()


def _home_noon_world(db, rid, games, lifts, game_tickets=None, usual_tickets=None, game_bartenders=4):
    """Measured home noon games, each with its ordinary Sundays: 3 PM
    bartenders, 8 PM servers, 6 in the kitchen on a usual Sunday."""
    flagged = set(games) | {"2026-09-13", "2026-09-28", "2026-10-11", "2026-10-18", "2026-11-08", "2026-11-22"}
    bars = game_bartenders if isinstance(game_bartenders, tuple) else (game_bartenders,) * len(games)
    for day, lift, bar in zip(games, lifts, bars):
        if lift is not None:
            _outcome(db, rid, day, lift, heads={"Bartender PM": bar, "Server PM": 8, "Kitchen": 6})
        _staff(db, rid, day, {"Bartender PM": bar, "Server PM": 8, "Kitchen": 6})
        _tickets(db, rid, day, game_tickets or {10: 300, 11: 1900, 12: 900, 17: 700})
    usual = sorted({d for g in games for d in _usual_sundays(g)} - flagged)
    for d in usual:
        _staff(db, rid, d, {"Bartender PM": 3, "Server PM": 8, "Kitchen": 6})
        _tickets(db, rid, d, usual_tickets or {10: 300, 11: 500, 12: 600, 17: 700})


def _jaguars(db):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    return store.events_for([s["id"]], "2026-12-06", "2026-12-06", db_path=db)[0]


# ── P2-03: the staffing plan and the rush count effect_for's nights ────────

def test_a_confounded_game_never_joins_a_staffing_plan_while_clean_ones_suffice(db):
    r = _restaurant(db)
    # 9/20 and 10/4 clean (+20, +30, one extra bartender); 11/22 a game on
    # a night with something else on: +60% and three extra bartenders.
    _home_noon_world(db, r.id, GAMES + (SAINTS,), (20.0, 30.0, 60.0), game_bartenders=(4, 4, 6))
    _confound(db, r.id, SAINTS)
    st = playbook.staffing(r.id, _jaguars(db), db_path=db)
    assert st["n"] == 2 and not st["mixed"] and [g["date"] for g in st["games"]] == ["2026-10-04", "2026-09-20"]
    assert st["text"].startswith("Staff above a usual Sunday: 1 more Bartender PM")
    assert "On your last 2 home games" in st["text"] and st["lift_pct"] == 25.0      # data, not text (R2-03)


def test_with_too_few_clean_games_the_plan_is_said_mixed_and_never_recommended(db):
    r = _restaurant(db)
    _home_noon_world(db, r.id, GAMES, (20.0, 30.0))
    _confound(db, r.id, GAMES[0])
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["mixed"] and st["n"] == 2 and st["recommend"] == []
    assert st["text"].endswith("not yet a pattern to plan on. Some of those nights had something else on too.")
    ru = playbook.rush(r.id, _saints(db), db_path=db)
    assert ru["mixed"] and ru["pattern_offset"] is None
    line = playbook.alert(r.id, date(2026, 11, 20), db_path=db)
    assert "Staff above" not in line["text"] and "Expect the jump" not in line["text"]
    assert line["claim_kind"] == "measured" and line["tone"] == "neutral"


def test_effect_for_staffing_the_rush_and_the_item_mix_share_one_clean_nights_rule():
    from event_intel import gameday
    for fn in (engine.effect_for, playbook._staffing, playbook._rush, gameday.item_mix):
        assert "store.clean_first(" in inspect.getsource(fn), fn.__name__
    assert not hasattr(engine, "clean_first")
    clean = lambda c: {"outcome": {"confounded": c}}
    assert store.clean_first([clean(0), clean(0), clean(1)], 2) == ([clean(0), clean(0)], False)
    assert store.clean_first([clean(0), clean(1)], 2) == ([clean(0), clean(1)], True)


def test_staffing_the_rush_and_the_item_mix_share_one_same_kind_rule():
    from event_intel import gameday
    for fn in (playbook._same_class, gameday._same_kind):
        assert "engine.same_kind(" in inspect.getsource(fn), fn.__name__


# ── P2-01: a "jump" only from games that ran above usual ───────────────────

def test_no_jump_is_promised_from_games_that_ran_below_usual_every_hour(db):
    r = _restaurant(db)
    # Both games ran $100 under a usual Sunday in every hour; the least-bad
    # hour (10am) agrees across them.
    _home_noon_world(db, r.id, GAMES, (20.0, 30.0), game_tickets={10: 200, 11: 400, 12: 500, 17: 600})
    ru = playbook.rush(r.id, _saints(db), db_path=db)
    assert {g["offset"] for g in ru["games"]} == {-2}
    assert ru["pattern_offset"] is None and ru["text"] is None
    line = playbook.alert(r.id, date(2026, 11, 20), db_path=db)
    assert "Expect the jump" not in line["text"]


# ── P2-02: "from about" is when the extra person came in ───────────────────

def _bar_world(db, rid, extra_at):
    """Two openers at 10:30 every Sunday; on game nights one more bartender,
    in at `extra_at[i]`."""
    for i, (day, lift) in enumerate(zip(GAMES, (20.0, 30.0))):
        _outcome(db, rid, day, lift)
        _punch(db, rid, day, "Bartender", "Ann", "10:30")
        _punch(db, rid, day, "Bartender", "Bo", "10:30")
        _punch(db, rid, day, "Bartender", "Cy", extra_at[i])
    for d in sorted({d for g in GAMES for d in _usual_sundays(g)} - set(GAMES) - {"2026-09-13"}):
        _punch(db, rid, d, "Bartender", "Ann", "10:30")
        _punch(db, rid, d, "Bartender", "Bo", "10:30")


def test_the_plan_names_the_extra_persons_start_never_the_openers(db):
    r = _restaurant(db)
    _bar_world(db, r.id, ("17:00", "17:00"))
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["text"].startswith("Staff above a usual Sunday: 1 more Bartender from about 5pm.")


def test_extra_starts_that_disagree_leave_the_time_out(db):
    r = _restaurant(db)
    _bar_world(db, r.id, ("11:00", "19:00"))
    st = playbook.staffing(r.id, _saints(db), db_path=db)
    assert st["text"].startswith("Staff above a usual Sunday: 1 more Bartender. On your last 2")


def test_the_extra_start_is_the_one_usual_nights_do_not_have():
    assert playbook._extra_starts([630, 630, 1020], [[630, 630], [630, 640]], 1) == [1020]
    assert playbook._extra_starts([950, 950, 950, 950], [[950, 950, 950]], 1) == [950]
    assert playbook._extra_starts([600, 1020], [], 1) == []          # no usual start on file: no time
    assert playbook._extra_starts([600], [[600]], 0) == []


# ── P2-04: kickoffs, the rush and the guest text on the restaurant's clock ─

def test_an_eastern_restaurant_hears_its_own_kickoff_rush_and_text_time(db, monkeypatch):
    import time_utils
    r = _restaurant(db)
    update_restaurant(r.id, {"timezone": "America/New_York"}, db_path=db)
    # The checks are on the restaurant's clock: the busy hour is noon
    # Eastern, the hour before a 12:00 Central (1pm Eastern) kickoff.
    _home_noon_world(db, r.id, GAMES, (20.0, 30.0), game_tickets={11: 300, 12: 1900, 13: 900, 18: 700},
                     usual_tickets={11: 300, 12: 500, 13: 600, 18: 700})
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda rid, naive=False: datetime(2026, 11, 20, 8, 0))
    ru = playbook.rush(r.id, _saints(db), db_path=db)
    assert ru["pattern_offset"] == -1 and ru["games"][0]["kickoff"] == "13:00"
    line = playbook.alert(r.id, date(2026, 11, 20), marketing=True, db_path=db)
    assert line["text"].startswith("Sunday 11/22/26: Bears vs New Orleans Saints · 1pm")
    assert "Expect the jump around 12–1pm (the hour before kickoff, as on your last 2)." in line["text"]
    assert "Text your guests Sunday around 10am, 3 hours before kickoff" in line["text"]
    assert line["ask"] == "How should we get ready for Bears vs New Orleans Saints · Sun 11/22/26 · 1pm · FOX?"


def test_the_day_afters_games_ahead_say_the_restaurants_clock(db):
    from dsr import tomorrow
    r = _restaurant(db)
    update_restaurant(r.id, {"timezone": "America/New_York"}, db_path=db)
    got = tomorrow._games_ahead(r.id, date(2026, 11, 20), db)
    assert got and got[0]["text"].startswith("Sunday: Bears vs New Orleans Saints · 1pm")


# ── P2-06: one game, one figure in the brief; the Labor gate on both ───────

def test_the_brief_says_a_game_days_effect_once(db):
    import morning_brief
    r = _restaurant(db)
    _home_noon_world(db, r.id, GAMES, (20.0, 30.0))
    brief = morning_brief.build(r.id, today=date(2026, 11, 22), db_path=db)
    keys = [str(l.get("key")) for l in brief["lines"]]
    game = [l for l in brief["lines"] if str(l.get("key")).startswith("event_ahead:")]
    assert game and "Bears home games have run +25%" in game[0]["text"]
    assert "memory:event" not in keys


def test_the_remembered_line_follows_the_labor_gate_and_skips_the_game_line(monkeypatch):
    import memory_context
    import morning_brief
    from types import SimpleNamespace
    today = date(2026, 11, 22)
    ev = {"date": today.isoformat(), "text": "Sunday 11/22/26: Bears home game · Soldier Field",
          "measured": "Measured here: nights like it ran a median 18% above", "ref": "event:7"}
    rain = {"date": today.isoformat(), "text": "Sunday 11/22/26: rain", "measured": "Measured here: 9% below",
            "ref": None}
    monkeypatch.setattr(memory_context, "memory_context",
                        lambda *a, **k: SimpleNamespace(sections={"events": [ev, rain]}))
    got = lambda **k: [l["text"] for l in morning_brief._memory_lines(1, today, None, [], None, **k)
                       if l["key"] == "memory:event"]
    assert got() == ["Remembered: Sunday 11/22/26: Bears home game · Soldier Field — Measured here: nights like "
                     "it ran a median 18% above"]
    # the game line speaks for game 7: the next remembered thing is said
    assert got(game={"event_id": 7}) == ["Remembered: Sunday 11/22/26: rain — Measured here: 9% below"]
    # a viewer without the Labor view gets no measured lift here either
    assert got(denied=frozenset({"labor"})) == []


def test_memory_lines_carry_the_catalog_game_they_are_about(db):
    from types import SimpleNamespace
    r = _restaurant(db)
    _home_noon_world(db, r.id, GAMES, (20.0, 30.0))
    req = SimpleNamespace(restaurant_id=r.id, db_path=db, now=datetime(2026, 11, 22, 6), surface="brief",
                          subjects=())
    lines = event_memory.memory_lines(req)
    assert lines and lines[0]["ref"] == f"event:{_saints(db)['id']}"


# ── P2-07 and A1 handoff 1: an if-necessary game with no result ────────────

def _postseason(db, day="2026-10-08", result=None):
    sid = store.upsert_series({"slug": "mlb-test-post", "name": "Test Sox", "short_name": "Sox",
                               "category": "sports", "league": "MLB", "home_venue": "Rate Field",
                               "timezone": "America/Chicago", "lat": None, "lng": None, "radius_km": None},
                              db_path=db)
    g = {"external_id": "alds4", "date": day, "kickoff": "19:05", "home_away": "home", "opponent": "Rivals",
         "venue": "Rate Field", "season": 2026, "season_type": "postseason", "attributes": {"if_necessary": True}}
    if result:
        g["result"] = result
    store.upsert_events(sid, [g], timezone="America/Chicago", db_path=db)
    return sid, store.events_for([sid], day, day, db_path=db)[0]


def test_tonights_report_never_describes_an_if_necessary_game_with_no_result(db, monkeypatch):
    r = _restaurant(db)
    sid, e = _postseason(db)
    store.set_follow(r.id, sid, True, db_path=db)
    monkeypatch.setattr(playbook, "usual_net", lambda rid, day, db_path=None: {"median": 8000.0, "n": 6})
    assert playbook.game_night(r.id, "2026-10-08", net=7500.0, db_path=db) is None
    store.edit_event(e["id"], {"result": "W 4-2"}, db_path=db)
    g = playbook.game_night(r.id, "2026-10-08", net=7500.0, db_path=db)
    assert g and g["text"].startswith("Tonight's game sold $7,500")


def test_an_unresolved_if_necessary_night_is_left_unmeasured_with_its_flag(full, monkeypatch):
    db = full
    monkeypatch.setattr(store, "local_today", lambda tz=None: date(2026, 10, 12))
    r = _place(db)
    sid, e = _postseason(db)
    label = engine.label_for(e) + engine.UNRESOLVED_SUFFIX
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, source, ref) VALUES (?,?,?,?,?,?)",
                  (r.id, e["event_date"], "event", label, "events", f"event:{e['id']}"))
        c.commit()
    finally:
        c.close()
    _row(db, r.id, e["event_date"], 40.0, "sox rate field necessary")
    monkeypatch.setattr(event_memory, "measure_night", lambda *a, **k: {
        "net": 140.0, "baseline": 100.0, "baseline_n": 8, "lift_pct": 40.0, "basis": "net", "source": "x"})
    got = event_memory.record_night(r.id, e["event_date"], db_path=db)
    assert got["recorded"] == 0 and got["reason"] == "an if-necessary game with no result yet"
    c = models.get_conn(db)
    try:
        assert c.execute("SELECT COUNT(*) AS n FROM event_outcomes WHERE restaurant_id=? AND business_date=?",
                         (r.id, e["event_date"])).fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) AS n FROM event_effects WHERE restaurant_id=?",
                         (r.id,)).fetchone()["n"] == 0
    finally:
        c.close()
    # the flag stays: the night is out of every baseline
    assert event_memory.flags_for(r.id, [e["event_date"]], db_path=db)[e["event_date"]]
    # once a result is in, the night is measured
    store.edit_event(e["id"], {"result": "W 4-2"}, db_path=db)
    assert event_memory.record_night(r.id, e["event_date"], db_path=db)["recorded"] >= 1


# ── P2-08: the footer names what the game line says ────────────────────────

def test_a_carried_plan_leaves_no_inference_on_the_game_line(db):
    r = _restaurant(db)
    _home_noon_world(db, r.id, GAMES, (20.0, 30.0))
    saints = _saints(db)
    line = playbook.alert(r.id, date(2026, 11, 21), db_path=db,
                          carried=[{"kind": "game_staffing", "event_id": saints["id"]}])
    assert "Staff above" not in line["text"] and "staffing" not in line
    assert line["claim_kind"] == "measured" and line["tone"] == "neutral"


def test_the_footer_names_other_restaurants_nights_as_theirs():
    import morning_brief
    peer = [{"key": "event_ahead:9", "claim_kind": "computed", "text": "x"}]
    said = morning_brief.footer_source(peer)
    assert "other restaurants' game nights (an estimate)" in said and "staffing plan" not in said
    plan = [{"key": "event_ahead:9", "claim_kind": "inferred", "text": "x"}]
    assert "the game's staffing plan (an inference)" in morning_brief.footer_source(plan)


# ── P3-04: the day after says "One game" only for one ──────────────────────

def test_the_day_after_says_one_game_only_when_it_was_one(monkeypatch):
    from dsr import tomorrow
    e = {"category": "sports", "id": 1}
    monkeypatch.setattr(gameday, "prep_lines", lambda *a, **k: [])
    wings = [{"item": "Wings", "every_game": False}]
    monkeypatch.setattr(gameday, "item_mix", lambda *a, **k: {"n": 3, "basis": "b", "items": wings,
                                                               "text": "Your last 3 home games sold a median 48 Wings "
                                                                       "(usual 20) — against a usual Sunday."})
    assert tomorrow._game_prep(1, e, None)["text"].endswith(
        "against a usual Sunday. Not on every game — not yet a pattern to prep on.")
    # Two games, one of them a night with something else on: not "One game".
    monkeypatch.setattr(gameday, "item_mix", lambda *a, **k: {"n": 2, "confounded": True, "basis": "b",
                                                               "items": [{"item": "Wings", "every_game": True}],
                                                               "text": "Your last 2 home games sold a median 50 Wings."})
    assert tomorrow._game_prep(1, e, None)["text"] == ("Your last 2 home games sold a median 50 Wings. Not yet a "
                                                       "pattern to prep on.")
    monkeypatch.setattr(gameday, "item_mix", lambda *a, **k: {"n": 1, "basis": "b", "items": wings,
                                                               "text": "Your last home game sold 54 Wings."})
    assert tomorrow._game_prep(1, e, None)["text"] == ("Your last home game sold 54 Wings. One game — not yet a "
                                                       "pattern to prep on.")


# ── P4-07: the headline and the alert judge from one figure ────────────────

def test_a_frequent_series_game_is_said_with_the_figure_that_headlined_it(full):
    db = full
    r = _place(db)
    _bulls_matter(db, r.id)
    nxt = _event(db, "nba-chicago-bulls", BULLS_NEXT)
    eff = engine.effect_for(r.id, nxt, db_path=db)
    m = event_memory.measured_effect(r.id, engine.label_for(nxt), db_path=db)
    assert engine.headline(r.id, nxt, db_path=db) is True
    assert eff["median_lift_pct"] == m["median_lift_pct"] == 8.0 and eff["n"] == 4
    assert event_memory.effect_matters(eff) is engine.headline(r.id, nxt, db_path=db)
    assert eff["basis"] == ("Bulls home games have run +8% against a usual same weekday here (median of 4, +2% to "
                            "+16%; some of those nights had something else on too)")
    line = playbook.alert(r.id, date(2026, 11, 12), db_path=db)
    assert line["event_id"] == nxt["id"] and "Bulls home games have run +8%" in line["text"]


def test_a_frequent_series_preseason_game_is_judged_by_its_own_nights(full):
    db = full
    r = _place(db)
    _bulls_matter(db, r.id)
    pre = _event(db, "nba-chicago-bulls", "2026-10-16")
    assert pre["season_type"] == "preseason"
    # The regular season matters here; no preseason night is measured.
    assert engine.effect_for(r.id, pre, db_path=db) is None
    assert engine.headline(r.id, pre, db_path=db) is False


# ── P4-11: two headline games on one date ──────────────────────────────────

def test_with_two_games_that_day_the_measured_one_leads_and_the_other_is_named(full, monkeypatch):
    db = full
    r = _place(db)
    _bulls_matter(db, r.id)
    line = playbook.alert(r.id, date(2026, 11, 21), db_path=db)
    bulls = _event(db, "nba-chicago-bulls", SUNDAY_BOTH)
    bears = _event(db, "nfl-chicago-bears", SUNDAY_BOTH)
    assert line["event_id"] == bulls["id"] and line["others"] == [bears["id"]]
    assert line["text"].startswith(f"Tomorrow: {engine.describe(bulls, with_date=False)}. Also that day: "
                                   f"{engine.describe(bears, with_date=False)}.")
    assert "Bulls home games have run +8%" in line["text"] and "plan a usual" not in line["text"]
    monkeypatch.setattr(playbook, "usual_net", lambda rid, day, db_path=None: {"median": 8000.0, "n": 6})
    g = playbook.game_night(r.id, SUNDAY_BOTH, net=9000.0, db_path=db)
    assert g["event_id"] == bulls["id"] and g["others"] == [bears["id"]]
    assert g["text"].endswith(f"Also tonight: {engine.describe(bears, with_date=False)}.")


# ── X-2: what a Home load reads, as seasons accumulate ─────────────────────

def _season_world(db, rid, seasons):
    """A test series: a home game every other Saturday for 12 games a
    season, `seasons` seasons ending 2026; each game measured, with punches,
    checks and items; the Saturdays between them ordinary."""
    sid = store.upsert_series({"slug": "mlb-test-load", "name": "Test Nine", "short_name": "Nine",
                               "category": "sports", "league": "MLB", "home_venue": "Park",
                               "timezone": "America/Chicago", "lat": None, "lng": None, "radius_km": None},
                              db_path=db)
    games, quiet = [], []
    for k in range(seasons):
        first = date(2026 - (seasons - 1 - k), 4, 4)            # a Saturday-ish start each season
        first += timedelta(days=(5 - first.weekday()) % 7)
        for i in range(12):
            games.append(first + timedelta(weeks=2 * i))
            quiet.append(first + timedelta(weeks=2 * i + 1))
    nxt = games[-1] + timedelta(weeks=2)
    rows = [{"external_id": f"g{d}", "date": d.isoformat(), "kickoff": "19:05", "home_away": "home",
             "opponent": "Rivals", "venue": "Park", "season": d.year} for d in games + [nxt]]
    store.upsert_events(sid, rows, timezone="America/Chicago", db_path=db)
    store.set_follow(rid, sid, True, db_path=db)
    c = models.get_conn(db)
    try:
        for d in games:
            iso = d.isoformat()
            c.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, source, ref) "
                      "SELECT ?, ?, 'event', 'Nine home game · Park', 'events', 'event:' || id FROM catalog_events "
                      "WHERE series_id=? AND event_date=?", (rid, iso, sid, iso))
            c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, net, baseline, "
                      "lift_pct) VALUES (?,?,?,?,?,?,?,?)", (rid, iso, d.strftime("%A"), "event", "nine park",
                                                             11000.0, 8000.0, 30.0))
        for d, bar in [(x, 4) for x in games] + [(x, 3) for x in quiet]:
            iso = d.isoformat()
            for i in range(bar):
                c.execute("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
                          "shift_start, shift_end, actual_hours, source) VALUES (?,?,?,?,?,?,?,?,?)",
                          (rid, iso, f"b{i}", f"b{i}", "Bartender", "16:00", "23:00", 7.0, "rpower"))
            for h, net in {17: 500, 18: 1900 if bar == 4 else 600, 20: 700}.items():
                c.execute("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, "
                          "net_sales) VALUES (?,?,?,?,?,?)", (rid, "rpower", f"{iso}-{h}", iso, f"{iso}T{h}:10:00", net))
        c.commit()
    finally:
        c.close()
    return sid, store.events_for([sid], nxt.isoformat(), nxt.isoformat(), db_path=db)[0]


def _counting(monkeypatch):
    """Count the SELECTs every connection runs, each night's punch reads and
    each usual-night read (its flags)."""
    seen, punches = [], []
    inner = models.get_conn

    def conn(*a, **k):
        c = inner(*a, **k)
        c.set_trace_callback(lambda sql: seen.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
        return c
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is inner:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    import shift_facts
    real_rows = shift_facts.rows

    def rows(rid, since=None, until=None, **k):
        punches.append(since)
        return real_rows(rid, since=since, until=until, **k)
    monkeypatch.setattr(shift_facts, "rows", rows)
    real_flags = event_memory.flags_for

    def flags_for(rid, days, *a, **k):
        punches.append(("flags", str(list(days)[0])[:10]))
        return real_flags(rid, days, *a, **k)
    monkeypatch.setattr(event_memory, "flags_for", flags_for)
    return seen, punches


@pytest.mark.parametrize("seasons", [2, 4])
def test_a_home_load_reads_each_night_once_and_no_more_as_seasons_accumulate(db, monkeypatch, seasons):
    monkeypatch.setattr(engine, "PAST_GAMES_PER_CLASS", 6)
    r = _restaurant(db)
    store.set_follow(r.id, store.series_by_slug("nfl-chicago-bears", db_path=db)["id"], False, db_path=db)
    _sid, nxt = _season_world(db, r.id, seasons)
    seen, punches = _counting(monkeypatch)
    line = playbook.alert(r.id, date.fromisoformat(nxt["event_date"]) - timedelta(days=1), db_path=db)
    assert "Staff above a usual Saturday: 1 more Bartender" in line["text"]
    # Each night's punches, and each game's usual nights, are read once in a
    # whole brief line — not once each for the staffing, the rush and the
    # item mix.
    assert punches and len(punches) == len(set(punches))
    # Bounded by engine.past_games' cap, not by how many seasons are on
    # file: 6 past games, their usual nights, and a fixed overhead.
    assert len(seen) <= 90, len(seen)
    _SEEN[seasons] = len(seen)
    if 2 in _SEEN and 4 in _SEEN:
        assert _SEEN[4] == _SEEN[2]


_SEEN = {}


# ── X-11: every unasked surface asks the one quiet test ────────────────────

def test_every_unasked_game_surface_asks_the_one_quiet_test():
    import preshift
    from dsr import tomorrow
    headline = re.compile(r"\b(?:engine|_eng)\.headline\(")
    # Surfaces that hold the catalog game: engine.headline.
    assert "_lead(" in inspect.getsource(inspect.unwrap(playbook.alert))    # the alert's game: playbook._lead
    for fn in (playbook._lead,                # the brief's game alert (morning_brief._game_line)
               playbook._game_night,          # the report's game night (dsr.block_intel._game)
               tomorrow.build,                # the report's Tomorrow event items (staffing, prep)
               tomorrow._games_ahead,         # the report's games two and three days out
               gameday.tomorrows_games,       # the afternoon-before push (push_for, run_event_push)
               gameday.week_note):            # Food Cost's game week
        assert headline.search(inspect.getsource(inspect.unwrap(fn))), fn.__qualname__
    # Surfaces that hold only a night's flags: the same test on the flag.
    assert "quiet_catalog(" in inspect.getsource(preshift._game_line)        # the pre-shift notes
    assert "quiet_flags(" in inspect.getsource(event_memory.memory_lines)     # Remembered / prompts
    # ...and the three are one test: event_memory.quiet_game.
    assert "event_memory.quiet_game(" in inspect.getsource(engine.headline)
    assert "quiet_game(" in inspect.getsource(event_memory.quiet_flags)
    assert "quiet_flags(" in inspect.getsource(event_memory.quiet_catalog)
    # and the callers reach these surfaces
    import morning_brief
    from dsr import block_intel
    assert "playbook.alert(" in inspect.getsource(morning_brief._game_line)
    assert "playbook.game_night(" in inspect.getsource(block_intel._game)
    assert "_game_night(" in inspect.getsource(playbook.game_night)
    assert "tomorrows_games(" in inspect.getsource(gameday.push_for)


# ── A1 handoffs 6 and 8: a game's class and its ground ─────────────────────

def test_the_playbook_never_pools_an_alt_venue_home_game_with_the_home_ground():
    home = {"home_away": "home", "season_type": "regular", "is_primetime": 0}
    alt = dict(home, attributes={"alt_venue": True})
    assert playbook._same_class(home, dict(home)) and playbook._same_class(alt, dict(alt))
    assert not playbook._same_class(alt, home) and not playbook._same_class(home, alt)
    assert not playbook._same_class(home, dict(home, season_type="preseason"))
    assert not playbook._same_class(home, dict(home, is_primetime=1))


def test_an_alt_venue_games_baseline_is_kin_to_its_own_ground(full):
    db = full
    r = _place(db)
    seatgeek = _event(db, "mls-chicago-fire", FIRE_SEATGEEK)
    soldier = _event(db, "mls-chicago-fire", FIRE_SOLDIER)
    assert store.alt_venue(seatgeek) and not store.alt_venue(soldier)
    flag = lambda e: {"kind": "event", "label": event_memory.normalise_label(engine.label_for(e)),
                      "raw": engine.label_for(e), "ref": f"event:{e['id']}"}
    # The quiet Fire night at Soldier Field is ordinary for the SeatGeek
    # night (another ground, another label)...
    assert event_memory.ordinary_nights(r.id, {FIRE_SOLDIER: [flag(soldier)]}, db_path=db,
                                        tonight=[flag(seatgeek)]) == {FIRE_SOLDIER}
    # ...and still kin of another Fire night at Soldier Field.
    other = _event(db, "mls-chicago-fire", "2026-10-28")
    assert event_memory.ordinary_nights(r.id, {FIRE_SOLDIER: [flag(soldier)]}, db_path=db,
                                        tonight=[flag(other)]) == set()
    info = event_memory._catalog_info({f"event:{seatgeek['id']}"}, db_path=db)
    assert info[f"event:{seatgeek['id']}"]["venue"] == "seatgeek stadium"


# ── B handoff: the headline docstring says what is measured ────────────────

def test_the_headline_docstring_says_which_nights_are_measured():
    doc = " ".join(engine.headline.__doc__.split())
    assert "measured every night it happens" not in doc
    assert "measured on every night that has event_memory.BASELINE_MIN ordinary same weekdays before it" in doc
