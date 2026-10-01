"""Event Intelligence phase 4: beyond the Bears (10/1/26).

The Blackhawks, the Bulls, the Fire and the White Sox postseason join the
catalog as season files from each league's own published schedule. A
frequent series (an NBA or NHL season) would flag most winter nights, so its
games leave a night in every baseline until this restaurant has measured
them to matter, and stay context — never the brief's alert, the report's
games ahead or the game-night line — until then. A date's events come
rarest first, so a Bears Sunday is never read as the Blackhawks game the
same day.
"""
import json
import os
import sys
from datetime import date

import pytest

import event_memory
import models
from event_intel import engine, playbook, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ST_CHARLES = (41.9142, -88.3087)


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
    monkeypatch.setattr(event_memory, "record_night", lambda *a, **k: {"recorded": 0})
    import weather
    monkeypatch.setattr(weather, "forecast_for_day", lambda *a, **k: None)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    return db_path


def _restaurant(db):
    rid = create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    r = get_restaurant(rid, db_path=db)
    engine.ensure_follows(r, db_path=db)
    return r


def _event(db, slug, iso, side=None):
    s = store.series_by_slug(slug, db_path=db)
    rows = [e for e in store.events_for([s["id"]], iso, iso, db_path=db) if side in (None, e["home_away"])]
    return rows[0]


def test_every_season_file_is_well_formed():
    folder = store.SEASONS_DIR
    slugs = set()
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".json"):
            continue
        data = json.load(open(os.path.join(folder, fn)))
        s = data["series"]
        assert s["slug"] not in slugs and data.get("sources") and data.get("fetched"), fn
        slugs.add(s["slug"])
        assert s["league"] in engine.START_WORDS, fn
        ids = [e["external_id"] for e in data["events"]]
        assert len(ids) == len(set(ids)), fn
        for e in data["events"]:
            assert e["home_away"] in ("home", "away") and e["opponent"], (fn, e)
            assert e["date"] is None or date.fromisoformat(e["date"])
            assert e["kickoff"] is None or (len(e["kickoff"]) == 5 and e["kickoff"][2] == ":"), (fn, e)
            assert e["status"] in ("scheduled", "completed", "postponed", "cancelled"), (fn, e)
            assert (e["status"] == "completed") == bool(e.get("result")) or e["status"] != "completed", (fn, e)
    assert {"nfl-chicago-bears", "nhl-chicago-blackhawks", "nba-chicago-bulls", "mls-chicago-fire",
            "mlb-chicago-white-sox"} <= slugs


def test_a_restaurant_near_chicago_follows_every_team(db):
    r = _restaurant(db)
    followed = {store.series_by_id(f["series_id"], db_path=db)["slug"] if hasattr(store, "series_by_id") else f["series_id"]
                for f in store.follows(r.id, db_path=db)}
    assert len(followed) >= 5


def test_a_dates_rarest_game_comes_first(db):
    # 10/22/26: the Bears host the Patriots; is there a frequent series game too?
    r = _restaurant(db)
    ids = [f["series_id"] for f in store.follows(r.id, db_path=db)]
    rows = store.events_for(ids, "2026-10-01", "2027-01-31", db_path=db)
    by_day = {}
    for e in rows:
        by_day.setdefault(e["event_date"], []).append(e)
    shared = [d for d, es in by_day.items() if len(es) > 1 and any(e["slug"] == "nfl-chicago-bears" for e in es)]
    assert shared, "no date with the Bears and another team to test against"
    for d in shared:
        assert by_day[d][0]["slug"] == "nfl-chicago-bears", d
        counts = [e["series_games"] for e in by_day[d]]
        assert counts == sorted(counts), d


def test_a_frequent_series_is_context_until_measured(db):
    r = _restaurant(db)
    hawks = _event(db, "nhl-chicago-blackhawks", "2027-04-10")
    assert hawks["series_games"] >= event_memory.FREQUENT_SERIES_GAMES
    assert not engine.headline(r.id, hawks, db_path=db)
    bears = _event(db, "nfl-chicago-bears", "2026-10-22")
    assert engine.headline(r.id, bears, db_path=db)
    # Three earlier Blackhawks home nights measured +18%: now it earns the alert.
    s = store.series_by_slug("nhl-chicago-blackhawks", db_path=db)
    past = [e for e in store.events_for([s["id"]], "2026-10-01", "2027-04-01", db_path=db)
            if e["home_away"] == "home" and e["season_type"] == "regular"][:3]
    c = models.get_conn(db)
    try:
        for e in past:
            c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, lift_pct, net, "
                      "baseline) VALUES (?,?,?,?,?,?,?,?)",
                      (r.id, e["event_date"], date.fromisoformat(e["event_date"]).strftime("%A"), "event",
                       "blackhawks united center", 18.0, 5900, 5000))
        c.commit()
    finally:
        c.close()
    assert engine.headline(r.id, hawks, db_path=db)


def test_an_unmeasured_frequent_game_leaves_its_night_in_the_baseline(db):
    r = _restaurant(db)
    hawks = _event(db, "nhl-chicago-blackhawks", "2027-04-10")
    bears = _event(db, "nfl-chicago-bears", "2026-10-22")
    flags = {
        "2027-04-10": [{"kind": "event", "label": "blackhawks united center", "ref": f"event:{hawks['id']}",
                        "raw": "Blackhawks home game · United Center"}],
        "2026-10-22": [{"kind": "event", "label": "bears soldier field", "ref": f"event:{bears['id']}",
                        "raw": "Bears home game · Soldier Field"}],
        "2026-10-29": [{"kind": "event", "label": "trivia night", "ref": None, "raw": "Trivia night"}],
        "2026-11-05": [],
    }
    ok = event_memory.ordinary_nights(r.id, flags, db_path=db)
    assert ok == {"2027-04-10", "2026-11-05"}


def test_the_rush_and_the_send_time_name_each_sports_start():
    assert engine.start_word({"league": "NHL"}) == "puck drop"
    assert engine.start_word({"league": "NBA"}) == "tip-off"
    assert engine.start_word({"league": "MLB"}) == "first pitch"
    assert engine.start_word({"league": "NFL"}) == "kickoff"
    assert playbook._offsets_words(-1, 0, "puck drop") == "the hour before puck drop"
    from event_intel import gameday
    plan = gameday.send_plan({"event_date": "2026-11-17", "kickoff_local": "19:00", "league": "NHL"})
    assert "before puck drop" in plan["text_words"] and "before puck drop" in plan["basis"]


def test_an_if_necessary_game_says_so(db):
    sox = _event(db, "mlb-chicago-white-sox", "2026-10-08")
    assert "if necessary" in engine.describe(sox)


# ── reviews against game nights ────────────────────────────────────────────

def _review(db, rid, day, cats, n):
    c = models.get_conn(db)
    try:
        for i in range(n):
            c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, fetched_at, "
                      "processed, categories, sentiment) VALUES (?,?,?,?,?,?,?,1,?,?)",
                      (rid, "google", f"{day}-{cats}-{i}", 2, "x", day, day, json.dumps(cats), "negative"))
        c.commit()
    finally:
        c.close()


def test_service_reviews_around_game_nights_are_a_lean_past_the_floors(db):
    from event_intel import reviews
    r = _restaurant(db)
    # 9/20 and 9/28 are Bears home games; 9/21 and 9/29 sit inside their windows.
    _review(db, r.id, "2026-09-21", ["service"], 5)
    _review(db, r.id, "2026-09-29", ["food_quality"], 5)
    _review(db, r.id, "2026-09-03", ["food_quality"], 9)
    _review(db, r.id, "2026-09-05", ["wait_time"], 1)
    out = reviews.game_night_reviews(r.id, today=date(2026, 10, 1), days=60, db_path=db)
    assert out["game_reviews"] == 10 and out["game_pct"] == 50
    assert out["other_reviews"] == 10 and out["other_pct"] == 10
    assert out["lean"] == "more" and "a lean, not proof" in out["text"]
    # Under the floor on one side: shares withheld, nothing said.
    thin = reviews.game_night_reviews(r.id, today=date(2026, 9, 25), days=30, db_path=db)
    assert thin["game_pct"] is None and thin["text"] is None


# ── what games do elsewhere, behind the privacy floor ─────────────────────

def _measured_bears_fan(db, i):
    rid = create_restaurant(Restaurant(name=f"Tap {i}", owner_email=f"o{i}@x{i}.com", timezone="America/Chicago"),
                            db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    engine.ensure_follows(get_restaurant(rid, db_path=db), db_path=db)
    c = models.get_conn(db)
    try:
        for d, lift in (("2026-09-20", 20 + i), ("2026-09-28", 30 + i), ("2026-10-04", 10 + i)):
            c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, lift_pct) "
                      "VALUES (?,?,?,?,?,?)", (rid, d, date.fromisoformat(d).strftime("%A"), "event",
                                               "bears soldier field", lift))
        c.commit()
    finally:
        c.close()
    return rid


def test_peer_game_effect_needs_five_restaurants_from_five_owners(db, monkeypatch):
    from event_intel import peers
    monkeypatch.setattr(peers, "_memo", {})
    viewer = _restaurant(db)
    game = _event(db, "nfl-chicago-bears", "2026-10-22")
    for i in range(4):
        _measured_bears_fan(db, i)
    assert peers.peer_effect(viewer.id, game, db_path=db) is None
    _measured_bears_fan(db, 4)
    monkeypatch.setattr(peers, "_memo", {})
    out = peers.peer_effect(viewer.id, game, db_path=db)
    assert out and out["n"] == 5 and out["claim_kind"] == "peers"
    assert out["median_lift_pct"] % peers.PEER_STEP_PCT == 0
    assert "not yours" in out["text"] and "Tap" not in out["text"]
