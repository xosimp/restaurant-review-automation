"""Event re-audit 2, season data (W5, 10/1/26).

Every bundled season file loads through the real loader, and the four
findings stay fixed: the Fire's cup matches are in the catalog and told apart
from the MLS season (RS-01), the Bulls show their Chicago carrier and never a
visitor's regional feed (RS-02), the White Sox and the Cubs carry their whole
2026 regular season (RS-03), and no string carries stray whitespace (RS-04).
"""
import json
import os
from collections import Counter

import pytest

import models
from event_intel import store

TODAY = "2026-10-08"   # the files as refreshed 10/8/26 (scripts/refresh_seasons.py)
FILES = sorted(fn for fn in os.listdir(store.SEASONS_DIR) if fn.endswith(".json"))


def _data(fn):
    with open(os.path.join(store.SEASONS_DIR, fn), encoding="utf-8") as fh:
        return json.load(fh)


def _by_slug():
    return {_data(fn)["series"]["slug"]: _data(fn) for fn in FILES}


@pytest.fixture
def loaded(db_path):
    """A temp catalog emptied, then every season file loaded through
    store.load_season."""
    c = models.get_conn(db_path)
    try:
        c.execute("DELETE FROM event_follows")
        c.execute("DELETE FROM catalog_events")
        c.execute("DELETE FROM event_series")
        c.commit()
    finally:
        c.close()
    got = {fn: store.load_season(os.path.join(store.SEASONS_DIR, fn), db_path=db_path) for fn in FILES}
    return db_path, got


def _rows(db, slug):
    c = models.get_conn(db)
    try:
        return [dict(r) for r in c.execute(
            "SELECT e.* FROM catalog_events e JOIN event_series s ON s.id=e.series_id WHERE s.slug=?",
            (slug,)).fetchall()]
    finally:
        c.close()


def test_every_file_loads_through_the_real_loader(loaded):
    db, got = loaded
    for fn in FILES:
        d = _data(fn)
        assert got[fn]["written"] == len(d["events"]), fn
        rows = _rows(db, d["series"]["slug"])
        assert len(rows) == len(d["events"]), fn
        assert {r["season_type"] for r in rows} <= set(store.SEASON_TYPES), fn
    assert len({_data(fn)["series"]["slug"] for fn in FILES}) == len(FILES)


def test_ids_unique_and_every_game_well_formed():
    for fn in FILES:
        d = _data(fn)
        ids = [e["external_id"] for e in d["events"]]
        assert len(ids) == len(set(ids)), fn
        assert d.get("sources") and d.get("fetched"), fn
        assert d["series"]["timezone"] == "America/Chicago" and d["series"]["radius_km"] == 120, fn
        for e in d["events"]:
            assert e["season_type"] in store.SEASON_TYPES, (fn, e)
            assert e["home_away"] in ("home", "away"), (fn, e)
            assert e["kickoff"] is None or (len(e["kickoff"]) == 5 and e["kickoff"][2] == ":"), (fn, e)


def test_home_games_are_at_the_home_venue_unless_alt_venue():
    for fn in FILES:
        d = _data(fn)
        home = d["series"]["home_venue"]
        for e in d["events"]:
            alt = (e.get("attributes") or {}).get("alt_venue")
            if e["home_away"] == "home":
                assert (e["venue"] == home) != bool(alt), (fn, e)
            else:
                assert e["venue"] != home and not alt, (fn, e)


def test_no_past_game_is_left_scheduled_and_completed_games_have_results():
    for fn in FILES:
        for e in _data(fn)["events"]:
            if e["status"] == "completed":
                assert e.get("result"), (fn, e)
            if e["date"] and e["date"] < TODAY and e["status"] == "scheduled":
                assert (e.get("attributes") or {}).get("if_necessary"), (fn, e)
            if e["date"] and e["date"] >= TODAY:
                assert e["status"] != "completed", (fn, e)


def test_no_string_carries_stray_whitespace():
    def walk(x, where):
        if isinstance(x, str):
            assert x == x.strip() and "  " not in x, (where, x)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{where}[{i}]")
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{where}.{k}")
    for fn in FILES:
        walk(_data(fn), fn)
    hawks = {e["external_id"]: e for e in _by_slug()["nhl-chicago-blackhawks"]["events"]}
    assert [hawks[i]["broadcast"] for i in ("2026020190", "2026020916", "2026021016")] == ["CHSN, ABTV"] * 3


def test_game_counts_and_preseason_flags_per_team():
    s = _by_slug()

    def count(slug):
        return Counter((e["season_type"], e["home_away"]) for e in s[slug]["events"])
    bears, hawks, bulls = count("nfl-chicago-bears"), count("nhl-chicago-blackhawks"), count("nba-chicago-bulls")
    assert bears["preseason", "home"] + bears["preseason", "away"] == 3
    assert bears["regular", "home"] + bears["regular", "away"] == 17
    assert hawks["preseason", "home"] + hawks["preseason", "away"] == 4
    assert hawks["regular", "home"] + hawks["regular", "away"] == 84
    assert bulls["preseason", "home"] + bulls["preseason", "away"] == 5
    assert (bulls["regular", "home"], bulls["regular", "away"]) == (40, 40)
    fire = count("mls-chicago-fire")
    assert (fire["regular", "home"], fire["regular", "away"]) == (17, 17)
    assert (fire["special", "home"], fire["special", "away"]) == (5, 1)
    sox, cubs = count("mlb-chicago-white-sox"), count("mlb-chicago-cubs")
    assert (sox["regular", "home"], sox["regular", "away"]) == (81, 81)
    assert sox["postseason", "home"] + sox["postseason", "away"] == 7
    assert (cubs["regular", "home"], cubs["regular", "away"]) == (81, 81)
    assert cubs["postseason", "away"] == 2 and cubs["postseason", "home"] == 0
    # No team's preseason is mislabelled: every preseason game is before the
    # team's first regular-season game.
    for slug, d in s.items():
        reg = [e["date"] for e in d["events"] if e["season_type"] == "regular" and e["date"]]
        pre = [e["date"] for e in d["events"] if e["season_type"] == "preseason"]
        assert all(p < min(reg) for p in pre), slug
    assert not any(e["season_type"] == "preseason" for slug in ("mlb-chicago-white-sox", "mlb-chicago-cubs",
                                                                "mls-chicago-fire") for e in s[slug]["events"])


def test_rs01_the_fires_cup_matches_are_in_and_told_apart(loaded):
    db, _ = loaded
    fire = {e["external_id"]: e for e in _by_slug()["mls-chicago-fire"]["events"]}
    want = {"401869708": ("2026-04-29", "19:00", "St. Louis CITY SC", "U.S. Open Cup", "L 1-2"),
            "401863566": ("2026-08-06", "19:30", "Necaxa", "Leagues Cup", "W 2-0"),
            "401863605": ("2026-08-09", "19:00", "Santos Laguna", "Leagues Cup", "W 3-1"),
            "401863624": ("2026-08-13", "20:00", "Cruz Azul", "Leagues Cup", "W 2-1"),
            "401909652": ("2026-08-25", "19:30", "Monterrey", "Leagues Cup", "L 1-2")}
    for eid, (day, ko, opp, comp, res) in want.items():
        e = fire[eid]
        assert (e["date"], e["kickoff"], e["opponent"], e["result"]) == (day, ko, opp, res), eid
        assert e["season_type"] == "special" and e["home_away"] == "home" and e["venue"] == "SeatGeek Stadium"
        assert e["attributes"]["competition"] == comp and e["attributes"]["alt_venue"] is True
        assert e["status"] == "completed"
    detroit = fire["401867355"]
    assert detroit["home_away"] == "away" and detroit["attributes"] == {"competition": "U.S. Open Cup"}
    # The MLS season is untouched and carries no competition tag.
    assert all(not (e.get("attributes") or {}).get("competition") for e in fire.values()
               if e["season_type"] == "regular")
    # Loaded: alt_venue reads true, and the cup nights never count as MLS regular-season games.
    rows = {r["external_id"]: r for r in _rows(db, "mls-chicago-fire")}
    for eid in want:
        assert store.alt_venue(dict(rows[eid], attributes=json.loads(rows[eid]["attributes_json"])))
    assert sum(1 for r in rows.values() if r["season_type"] == "regular") == 34


def test_rs02_the_bulls_show_their_chicago_carrier():
    bulls = {e["external_id"]: e for e in _by_slug()["nba-chicago-bulls"]["events"]}
    assert not any("MNMT" in (e["broadcast"] or "") for e in bulls.values())
    assert all(e["broadcast"] for e in bulls.values())
    national = {"401910071": "NBC", "401910259": "Peacock, NBCSN", "401910376": "ESPN"}
    for eid, e in bulls.items():
        if eid in national:
            assert e["broadcast"] == national[eid]
        else:
            assert e["broadcast"].split(", ")[0] == "CHSN", e
    assert bulls["401909857"]["broadcast"] == bulls["401909309"]["broadcast"] == "CHSN"
    assert bulls["401910087"]["broadcast"] == "CHSN, NBA TV"


def test_rs03_the_sox_and_cubs_regular_seasons(loaded):
    db, _ = loaded
    s = _by_slug()
    sox, cubs = s["mlb-chicago-white-sox"], s["mlb-chicago-cubs"]
    assert (cubs["series"]["home_venue"], cubs["series"]["lat"], cubs["series"]["lng"]) == \
        ("Wrigley Field", 41.9482, -87.6555)
    assert (sox["series"]["home_venue"], sox["series"]["lat"], sox["series"]["lng"]) == \
        ("Rate Field", 41.8299, -87.6338)
    for d in (sox, cubs):
        reg = [e for e in d["events"] if e["season_type"] == "regular"]
        assert len(reg) == 162 and all(e["status"] == "completed" and e["result"] for e in reg)
        assert min(e["date"] for e in reg) == "2026-03-26" and max(e["date"] for e in reg) == "2026-09-27"
        homes = [e["date"] for e in reg if e["home_away"] == "home"]
        assert len(homes) == len(set(homes))           # no home doubleheader: one crowd a night
        wins = sum(1 for e in reg if e["result"].startswith("W "))
        assert 0 < wins < 162
    # Spot values from statsapi.mlb.com: opening day, a made-up postponement, the Cubs' last Wild Card game.
    soxe = {e["external_id"]: e for e in sox["events"]}
    assert (soxe["823812"]["date"], soxe["823812"]["kickoff"], soxe["823812"]["venue"]) == \
        ("2026-03-26", "13:10", "American Family Field")
    assert (soxe["824589"]["date"], soxe["824589"]["home_away"]) == ("2026-08-20", "home")
    cubse = {e["external_id"]: e for e in cubs["events"]}
    assert (cubse["849842"]["season_type"], cubse["849842"]["result"]) == ("postseason", "L 1-4")
    # Loaded: the regular season counts as the series' games; the ALDS is still postseason.
    rows = _rows(db, "mlb-chicago-white-sox")
    assert sum(1 for r in rows if r["season_type"] == "regular") == 162
    assert {r["external_id"] for r in rows if r["season_type"] == "postseason"} >= \
        {"849829", "849834", "849833", "849832", "849831"}
