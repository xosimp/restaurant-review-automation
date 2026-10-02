"""Event Intelligence re-audit fixes, engine core (A1, 10/1/26).

Each test names the re-audit finding it holds: the catalog's status and
follow rules (P1-03, P1-07), what Ask's upcoming list may hold (P1-04),
who is measured (P1-05), the record tail past RECORD_MAX (P1-06, P4-13,
X-1), the label (P1-09), the one played / unresolved test (P4-03, P4-04),
preseason and another ground kept apart in every segment (P3-02, SD-02),
the bound on past games (X-2), kickoffs on the restaurant's clock (P2-04)
and dates on the game's clock, not the server's (X-6).
"""
import sys
from datetime import date, datetime, timezone

import pytest

import event_memory
import models
from event_intel import engine, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ST_CHARLES = (41.9142, -88.3087)          # about 56 km from Soldier Field
MILWAUKEE = (43.0389, -87.9065)           # about 130 km: outside the Bears' 120 km


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
    return db_path


@pytest.fixture
def recorded(monkeypatch):
    got = []
    monkeypatch.setattr(event_memory, "record_night",
                        lambda rid, d, db_path=None: got.append(str(d)[:10]) or {"recorded": 1})
    return got


def _bare(db, tz="America/Chicago", name="Bare Co"):
    """A restaurant with no location: it follows only what a test gives it."""
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.com", timezone=tz),
                            db_path=db)
    return get_restaurant(rid, db_path=db)


def _located(db, at=ST_CHARLES, name="EJ Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"latitude": at[0], "longitude": at[1]}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _series(db, events, slug="mlb-test", short="Nine", league="MLB", home_venue="Park", tz="America/Chicago"):
    sid = store.upsert_series({"slug": slug, "name": f"Test {short}", "short_name": short, "category": "sports",
                               "league": league, "home_venue": home_venue, "timezone": tz,
                               "lat": None, "lng": None, "radius_km": None}, db_path=db)
    store.upsert_events(sid, events, timezone=tz, db_path=db)
    return sid


def _game(ext, day, side="home", kickoff="19:05", **kw):
    g = {"external_id": ext, "date": day, "kickoff": kickoff, "home_away": side, "opponent": "Rivals",
         "venue": "Park" if side == "home" else "Away Park", "season": 2026}
    g.update(kw)
    return g


def _event(db, sid, day):
    return store.events_for([sid], day, day, db_path=db)[0]


def _outcome(db, rid, day, lift, label="nine park", confounded=0):
    c = models.get_conn(db)
    try:
        c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, net, baseline, "
                  "lift_pct, confounded) VALUES (?,?,?,?,?,?,?,?,?)",
                  (rid, day, date.fromisoformat(day).strftime("%A"), "event", label, 11000.0, 8000.0, lift,
                   confounded))
        c.commit()
    finally:
        c.close()


def _signals(db, rid):
    c = models.get_conn(db)
    try:
        return {r["ref"]: dict(r) for r in c.execute("SELECT date, label, ref FROM demand_signals "
                                                     "WHERE restaurant_id=? AND source='events'", (rid,))}
    finally:
        c.close()


def _status(db, sid, ext):
    c = models.get_conn(db)
    try:
        return c.execute("SELECT status FROM catalog_events WHERE series_id=? AND external_id=?",
                         (sid, ext)).fetchone()["status"]
    finally:
        c.close()


# ── P1-03: a season file can reinstate a postponed or cancelled game ───────

def test_a_postponed_or_cancelled_game_the_file_reinstates_is_scheduled_again(db):
    sid = _series(db, [_game("g1", "2026-10-20", status="postponed"), _game("g2", "2026-10-21", status="cancelled"),
                       _game("g3", "2026-09-20")])
    assert (_status(db, sid, "g1"), _status(db, sid, "g2")) == ("postponed", "cancelled")
    store.mark_past_completed("2026-10-01", db_path=db)
    # the league reschedules g1 and reinstates g2; the file says scheduled
    store.upsert_events(sid, [_game("g1", "2026-10-27"), _game("g2", "2026-10-21"), _game("g3", "2026-09-20")],
                        db_path=db)
    assert (_status(db, sid, "g1"), _status(db, sid, "g2")) == ("scheduled", "scheduled")
    # a played game still never goes back
    assert _status(db, sid, "g3") == "completed"
    # an admin's own cancellation is laid back over the file on every load
    g2 = _event(db, sid, "2026-10-21")
    store.edit_event(g2["id"], {"status": "cancelled"}, db_path=db)
    store.upsert_events(sid, [_game("g2", "2026-10-21")], db_path=db)
    assert _status(db, sid, "g2") == "cancelled"


# ── P1-04: Ask's upcoming games are games that will be played here ─────────

def test_upcoming_leaves_out_cancelled_postponed_and_removed_games_and_says_status(db):
    sid = _series(db, [_game("a", "2026-10-03"), _game("b", "2026-10-04", status="cancelled"),
                       _game("c", "2026-10-05", status="postponed"), _game("d", "2026-10-06")])
    r = _bare(db)
    store.set_follow(r.id, sid, True, db_path=db)
    store.dismiss(r.id, _event(db, sid, "2026-10-06")["id"], db_path=db)
    got = engine.upcoming(r.id, days=10, today=date(2026, 10, 1), db_path=db)
    assert [u["event"]["event_date"] for u in got] == ["2026-10-03"]
    assert got[0]["status"] == "scheduled"


# ── P1-05: an account that must not learn gets the games, not the measuring

def test_an_excluded_restaurant_gets_the_games_but_no_night_is_measured(db, recorded):
    sid = _series(db, [_game("a", "2026-09-20"), _game("b", "2026-09-27"), _game("c", "2026-10-04")])
    r = _bare(db)
    update_restaurant(r.id, {"exclude_from_learning": 1}, db_path=db)
    r = get_restaurant(r.id, db_path=db)
    store.set_follow(r.id, sid, True, db_path=db)
    got = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert got["added"] == 3 and len(_signals(db, r.id)) == 3
    assert recorded == [] and got["nights_recorded"] == 0
    # the same restaurant learning again: its past game nights are measured
    update_restaurant(r.id, {"exclude_from_learning": 0}, db_path=db)
    r = get_restaurant(r.id, db_path=db)
    store.set_follow(r.id, sid, False, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert sorted(recorded) == ["2026-09-20", "2026-09-27"]


# ── P1-06 / P4-13 / X-1: the record tail past RECORD_MAX is measured later ─

def test_the_record_tail_beyond_the_cap_is_measured_on_later_passes(db, recorded, monkeypatch):
    monkeypatch.setattr(engine, "RECORD_MAX", 2)
    days = ["2026-09-01", "2026-09-05", "2026-09-09", "2026-09-13", "2026-09-17"]
    sid = _series(db, [_game(f"g{i}", d) for i, d in enumerate(days)])
    r = _bare(db)
    store.set_follow(r.id, sid, True, db_path=db)
    first = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert first["nights_recorded"] == 2 and first["nights_queued"] == 3
    assert recorded == ["2026-09-17", "2026-09-13"]                 # newest first
    second = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert second["nights_queued"] == 1
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert sorted(recorded) == sorted(days)                          # every night, once
    quiet = engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert quiet["nights_recorded"] == 0 and len(recorded) == 5
    # the mirror case: unfollowing re-records every night the game leaves
    del recorded[:]
    store.set_follow(r.id, sid, False, db_path=db)
    for _ in range(3):
        engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert sorted(recorded) == sorted(days)
    assert engine._record_queue(r.id, db) == []


# ── P1-07: an auto follow tracks where the restaurant is ───────────────────

def test_an_auto_follow_is_withdrawn_when_the_restaurant_moves_out_of_reach(db, monkeypatch):
    r = _located(db)
    bears = store.series_by_slug("nfl-chicago-bears", db_path=db)
    engine.ensure_follows(r, db_path=db)
    row = [f for f in store.follows(r.id, db_path=db) if f["series_id"] == bears["id"]][0]
    assert row["source"] == "auto" and 40 < row["distance_km"] < 70
    # an owner's own choice on another series is theirs, wherever the restaurant is
    hawks = store.series_by_slug("nhl-chicago-blackhawks", db_path=db)
    store.set_follow(r.id, hawks["id"], True, source="owner", distance_km=55.0, db_path=db)
    update_restaurant(r.id, {"latitude": MILWAUKEE[0], "longitude": MILWAUKEE[1]}, db_path=db)
    r = get_restaurant(r.id, db_path=db)
    engine.ensure_follows(r, db_path=db)
    left = {f["series_id"]: f for f in store.follows(r.id, active_only=False, db_path=db)}
    assert bears["id"] not in left
    assert left[hawks["id"]]["source"] == "owner" and left[hawks["id"]]["distance_km"] == 55.0
    # the sync then takes the Bears games off its list
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    engine.sync_restaurant(r, today=date(2026, 10, 1), db_path=db)
    assert not [s for s in _signals(db, r.id).values() if s["label"].startswith("Bears")]
    # moving back follows again, at the new distance
    update_restaurant(r.id, {"latitude": ST_CHARLES[0] + 0.1, "longitude": ST_CHARLES[1]}, db_path=db)
    r = get_restaurant(r.id, db_path=db)
    assert "nfl-chicago-bears" in engine.ensure_follows(r, db_path=db)
    first_km = [f for f in store.follows(r.id, db_path=db) if f["series_id"] == bears["id"]][0]["distance_km"]
    update_restaurant(r.id, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    engine.ensure_follows(get_restaurant(r.id, db_path=db), db_path=db)
    now_km = [f for f in store.follows(r.id, db_path=db) if f["series_id"] == bears["id"]][0]["distance_km"]
    assert now_km != first_km and 40 < now_km < 70


# ── P1-09: home and road stay apart even with no venue ─────────────────────

def test_a_home_game_with_no_venue_is_never_merged_with_road_games(db):
    sid = _series(db, [_game("h", "2026-10-03", venue=None), _game("r", "2026-10-04", side="away")])
    home, road = _event(db, sid, "2026-10-03"), _event(db, sid, "2026-10-04")
    assert engine.label_for(home) == "Nine home game · Park"            # the series' own ground
    bare = dict(home, series_home_venue=None)
    assert engine.label_for(bare) == f"Nine home game · {engine.HOME_TOKEN}"
    for h in (home, bare):
        a = set(event_memory.normalise_label(engine.label_for(h)).split())
        b = set(event_memory.normalise_label(engine.label_for(road)).split())
        assert not (a <= b or b <= a), (a, b)


# ── P4-03 / P4-04: one test for played, one for an if-necessary game unresolved

def _playoffs(db):
    return _series(db, [_game("p1", "2026-10-03", season_type="postseason", venue="Rate Field"),
                        _game("p4", "2026-10-08", season_type="postseason", venue="Rate Field",
                              attributes={"if_necessary": True}),
                        _game("p5", "2026-10-10", season_type="postseason", venue="Rate Field",
                              attributes={"if_necessary": True}, status="cancelled")],
                   slug="mlb-test-post", short="Sox", home_venue="Rate Field")


def test_played_and_unresolved_are_one_test_each(db):
    sid = _playoffs(db)
    g1, g4, g5 = (_event(db, sid, d) for d in ("2026-10-03", "2026-10-08", "2026-10-10"))
    today = date(2026, 10, 12)
    assert store.played(g1, today) and not store.unresolved(g1, today)
    assert store.unresolved(g4, today) and not store.played(g4, today)
    assert not store.unresolved(g5, today) and not store.played(g5, today)     # cancelled: settled, not played
    assert not store.unresolved(g4, date(2026, 10, 8)) and not store.played(g4, date(2026, 10, 8))  # not yet past
    assert store.played(dict(g4, result="W 4-2"), today)
    assert store.played(dict(g4, status="completed"), today)                    # an admin marked it played
    assert not store.played(dict(g1, status="postponed"), today)
    assert store.unresolved_refs([f"event:{g4['id']}", f"event:{g1['id']}", "post:3"], today, db_path=db) == \
        {f"event:{g4['id']}"}


def test_an_unresolved_if_necessary_night_stays_flagged_unmeasured_and_asks_for_a_result(db, recorded, monkeypatch):
    sid = _playoffs(db)
    r = _bare(db)
    store.set_follow(r.id, sid, True, db_path=db)
    g4 = _event(db, sid, "2026-10-08")
    engine.sync_restaurant(r, today=date(2026, 10, 5), db_path=db)         # still ahead: an ordinary copy
    assert _signals(db, r.id)[f"event:{g4['id']}"]["label"] == "Sox home game · Rate Field"
    del recorded[:]
    engine.sync_restaurant(r, today=date(2026, 10, 12), db_path=db)
    sig = _signals(db, r.id)[f"event:{g4['id']}"]
    # flagged — not erased into an ordinary night — and not measured
    assert sig["date"] == "2026-10-08" and sig["label"] == "Sox home game · Rate Field" + engine.UNRESOLVED_SUFFIX
    assert "2026-10-08" not in recorded
    fl = event_memory.flags_for(r.id, ["2026-10-08"], db_path=db)
    assert event_memory.ordinary_nights(r.id, fl, db_path=db) == set()          # out of every baseline
    # never counted as played, even with a night on file
    _outcome(db, r.id, "2026-10-03", 30.0, label="sox rate field")
    _outcome(db, r.id, "2026-10-08", 90.0, label="sox rate field")
    later = dict(_event(db, sid, "2026-10-03"), event_date="2026-10-20", id=-1)
    assert [g["event"]["event_date"] for g in engine.past_games(r.id, later, db_path=db)] == ["2026-10-03"]
    # the admin catalog shows it needs a result (read on 10/12, Central)
    class _Oct12(_Clock):
        AT = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
    import time_utils
    monkeypatch.setattr(time_utils, "datetime", _Oct12)
    cat = [s for s in store.catalog(db_path=db) if s["id"] == sid][0]
    assert cat["needs_result"] == 1 and [e["external_id"] for e in cat["events"] if e["needs_result"]] == ["p4"]
    # the result is entered: the copy is the game's own again and the night is measured
    store.edit_event(g4["id"], {"result": "W 4-2"}, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 12), db_path=db)
    assert _signals(db, r.id)[f"event:{g4['id']}"]["label"] == "Sox home game · Rate Field"
    assert "2026-10-08" in recorded
    assert [s for s in store.catalog(db_path=db) if s["id"] == sid][0]["needs_result"] == 0


def test_a_cancelled_if_necessary_game_leaves_its_night_ordinary(db, recorded):
    sid = _playoffs(db)
    r = _bare(db)
    store.set_follow(r.id, sid, True, db_path=db)
    g4 = _event(db, sid, "2026-10-08")
    engine.sync_restaurant(r, today=date(2026, 10, 12), db_path=db)
    store.edit_event(g4["id"], {"status": "cancelled"}, db_path=db)
    engine.sync_restaurant(r, today=date(2026, 10, 12), db_path=db)
    assert f"event:{g4['id']}" not in _signals(db, r.id) and "2026-10-08" in recorded


# ── P3-02: preseason is never pooled with the regular season ───────────────

def test_a_preseason_game_is_never_told_what_regular_season_games_did(db):
    sid = _series(db, [_game("r1", "2026-09-06"), _game("r2", "2026-09-13"), _game("r3", "2026-09-20"),
                       _game("pre", "2027-08-14", season_type="preseason")])
    r = _bare(db)
    for d, lift in (("2026-09-06", 30.0), ("2026-09-13", 40.0), ("2026-09-20", 50.0)):
        _outcome(db, r.id, d, lift)
    pre = _event(db, sid, "2027-08-14")
    assert engine.effect_for(r.id, pre, db_path=db) is None
    # and the regular season is never told what preseason did
    sid2 = _series(db, [_game("p1", "2025-08-09", season_type="preseason"),
                        _game("p2", "2026-08-08", season_type="preseason"), _game("reg", "2026-10-03")],
                   slug="mlb-test-2", short="Tens")
    _outcome(db, r.id, "2025-08-09", 10.0, label="tens park")
    _outcome(db, r.id, "2026-08-08", 12.0, label="tens park")
    assert engine.effect_for(r.id, _event(db, sid2, "2026-10-03"), db_path=db) is None
    eff = engine.effect_for(r.id, dict(_event(db, sid2, "2026-08-08"), event_date="2027-08-07", id=-1),
                            db_path=db)
    assert eff and eff["n"] == 2


# ── SD-02: a home game at another ground is its own night ──────────────────

def test_a_home_game_at_another_ground_is_its_own_segment(db):
    sid = _series(db, [_game("h1", "2026-09-06"), _game("h2", "2026-09-13"),
                       _game("alt", "2026-09-20", venue="SeatGeek Stadium", attributes={"alt_venue": True}),
                       _game("alt2", "2026-09-27", venue="SeatGeek Stadium", attributes={"alt_venue": True}),
                       _game("next", "2026-10-04"),
                       _game("next_alt", "2026-10-11", venue="SeatGeek Stadium", attributes={"alt_venue": True})])
    alt = _event(db, sid, "2026-09-20")
    assert alt["attributes"] == {"alt_venue": True} and store.alt_venue(alt)     # the loader keeps it
    r = _bare(db)
    _outcome(db, r.id, "2026-09-06", 10.0)
    _outcome(db, r.id, "2026-09-13", 20.0)
    _outcome(db, r.id, "2026-09-20", -40.0, label="nine seatgeek stadium")
    _outcome(db, r.id, "2026-09-27", -30.0, label="nine seatgeek stadium")
    home = engine.effect_for(r.id, _event(db, sid, "2026-10-04"), db_path=db)
    assert home["n"] == 2 and home["median_lift_pct"] == 15.0 and sorted(home["dates"]) == ["2026-09-06",
                                                                                            "2026-09-13"]
    away_ground = engine.effect_for(r.id, _event(db, sid, "2026-10-11"), db_path=db)
    assert away_ground["n"] == 2 and away_ground["median_lift_pct"] == -35.0
    assert away_ground["segment"] == "Nine home games at SeatGeek Stadium"
    assert engine.last_like(r.id, _event(db, sid, "2026-10-04"), db_path=db)["event"]["event_date"] == "2026-09-13"
    # its label names its own ground, so event_memory's venue kin keeps it
    # apart from the home ground's nights
    assert event_memory.normalise_label(engine.label_for(alt)) == "nine seatgeek stadium"


# ── X-2: past_games is bounded ─────────────────────────────────────────────

def test_past_games_reads_a_bounded_number_of_the_newest_games_per_class(db, monkeypatch):
    monkeypatch.setattr(engine, "PAST_GAMES_PER_CLASS", 3)
    days = ["2023-09-02", "2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22", "2026-08-29"]
    sid = _series(db, [_game(f"h{i}", d) for i, d in enumerate(days)] +
                  [_game("road", "2026-08-30", side="away"), _game("next", "2026-10-03")])
    r = _bare(db)
    for i, d in enumerate(days):
        _outcome(db, r.id, d, 10.0 * (i + 1))
    _outcome(db, r.id, "2026-08-30", 5.0, label="nine road")
    nxt = _event(db, sid, "2026-10-03")
    got = engine.past_games(r.id, nxt, db_path=db)
    assert [g["event"]["event_date"] for g in got] == ["2026-08-30", "2026-08-29", "2026-08-22", "2026-08-15"]
    eff = engine.effect_for(r.id, nxt, db_path=db)
    assert eff["n"] == 3 and eff["median_lift_pct"] == 50.0                 # the newest three home nights
    monkeypatch.setattr(engine, "PAST_GAMES_PER_CLASS", 99)
    assert "2023-09-02" not in [g["event"]["event_date"] for g in engine.past_games(r.id, nxt, db_path=db)]


# ── P2-04: a kickoff on the restaurant's own clock ─────────────────────────

def test_a_kickoff_is_said_on_the_restaurants_clock(db, monkeypatch):
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    sid = _series(db, [_game("day", "2026-10-04", kickoff="12:00"), _game("late", "2026-10-05", kickoff="23:30")],
                  league="NFL", short="Bears")
    day, late = _event(db, sid, "2026-10-04"), _event(db, sid, "2026-10-05")
    assert engine.local_kickoff(day) == (date(2026, 10, 4), "12:00")
    assert engine.local_kickoff(day, "America/New_York") == (date(2026, 10, 4), "13:00")
    assert engine.local_kickoff(late, "America/New_York") == (date(2026, 10, 6), "00:30")
    assert engine.local_kickoff(dict(day, kickoff_local=None), "America/New_York") == (date(2026, 10, 4), None)
    assert engine.describe(day, tz="America/New_York") == "Bears vs Rivals · Sun 10/4/26 · 1pm"
    assert engine.describe(day) == "Bears vs Rivals · Sun 10/4/26 · 12pm"
    south_bend = _bare(db, tz="America/New_York", name="South Co")
    store.set_follow(south_bend.id, sid, True, db_path=db)
    up = engine.upcoming(south_bend.id, days=7, today=date(2026, 10, 1), db_path=db)
    assert up[0]["describe"] == "Bears vs Rivals · Sun 10/4/26 · 1pm"
    engine.sync_restaurant(south_bend, today=date(2026, 10, 1), db_path=db)
    ref = _signals(db, south_bend.id)[f"event:{day['id']}"]["ref"]
    assert engine.context_by_ref(south_bend.id, ref, db_path=db)["describe_short"] == "Bears vs Rivals · 1pm"
    assert engine.context_for(south_bend.id, "2026-10-04", db_path=db)[0]["describe"].endswith("· 1pm")


# ── X-6: dates on the game's own clock, never the server's UTC one ─────────

class _Clock(datetime):
    """8:30pm Central on 10/1/26 — already 10/2 on a UTC server."""
    AT = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.AT.astimezone(tz) if tz else cls.AT.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.AT.replace(tzinfo=None)


class _ServerDate(date):
    @classmethod
    def today(cls):
        return date(2026, 10, 2)


def _pin(monkeypatch, clock=_Clock):
    import time_utils
    monkeypatch.setattr(time_utils, "datetime", clock)
    monkeypatch.setattr(store, "datetime", clock)
    monkeypatch.setattr(engine, "date", _ServerDate)


def test_tonights_game_is_not_completed_when_the_servers_utc_date_is_tomorrow(db, monkeypatch):
    sid = _series(db, [_game("yday", "2026-09-30", kickoff="19:15"), _game("tonight", "2026-10-01", kickoff="19:15"),
                       _game("ppd", "2026-10-01", kickoff="13:05", status="postponed")])
    _pin(monkeypatch)
    assert store.local_today() == date(2026, 10, 1)
    import scheduler
    monkeypatch.setattr(scheduler, "resumable_sweep", lambda key, ids, fn, secs, workers=1, job=None: ([], False))
    engine.run_event_sync(db_path=db)                    # an admin's "Run now" at 8:30pm Central
    assert _status(db, sid, "tonight") == "scheduled" and _status(db, sid, "yday") == "completed"
    # an admin clearing tonight's postponed status gets scheduled, not completed
    ppd = [e for e in store.events_for([sid], "2026-10-01", "2026-10-01", db_path=db) if e["external_id"] == "ppd"][0]
    store.edit_event(ppd["id"], {"status": "postponed"}, db_path=db)
    monkeypatch.setattr(store, "_season_values", lambda slug, ext: {"event_date": "2026-10-01", "status": "scheduled",
                                                                   "kickoff_local": "13:05", "broadcast": None,
                                                                   "result": None})
    assert store.edit_event(ppd["id"], {}, clear=("status",), db_path=db)["after"]["status"] == "scheduled"
    # tonight's games are still upcoming for a Central restaurant
    r = _bare(db)
    store.set_follow(r.id, sid, True, db_path=db)
    assert [u["event"]["external_id"] for u in engine.upcoming(r.id, days=0, db_path=db)] == ["ppd", "tonight"]


def test_yesterdays_games_are_completed_the_next_morning(db, monkeypatch):
    class _Morning(_Clock):
        AT = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)       # 5am Central, the daily job
    sid = _series(db, [_game("yday", "2026-09-30", kickoff="19:15")])
    _pin(monkeypatch, _Morning)
    monkeypatch.setattr(engine, "date", type("_D", (date,), {"today": classmethod(lambda c: date(2026, 10, 1))}))
    import scheduler
    monkeypatch.setattr(scheduler, "resumable_sweep", lambda key, ids, fn, secs, workers=1, job=None: ([], False))
    engine.run_event_sync(db_path=db)
    assert _status(db, sid, "yday") == "completed"
