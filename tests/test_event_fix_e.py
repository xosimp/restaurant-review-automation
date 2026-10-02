"""Event Intelligence re-audit fixes, round E (10/1/26): peers, reviews and
season data.

  P4-05 / X-2   peer effects are materialised nightly (event_peer_effects,
                written by the features pass) and a request reads only that
                table, once per segment for every viewer;
  P4-09         the peer line states the real planning threshold;
  P4-12         the review diagnosis names and offers only the lines on its page;
  X-7           a review stamp carrying an offset is bucketed by the
                restaurant's own day;
  X-11          the peers privacy floors each have a test;
  SD-01 / SD-02 the White Sox ALDS network and the Fire's alternate venue.
"""
import json
import os
import sys
from datetime import date

import pytest

import event_memory
import models
from event_intel import engine, peers, store
from intelligence import privacy
from intelligence.benchmarks import MIN_QUARTILE_N
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ST_CHARLES = (41.9142, -88.3087)
BEARS_NIGHTS = ("2026-09-20", "2026-09-28", "2026-10-04")


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
    # The fixture's nights (BEARS_NIGHTS) run to 10/4/26 and its game is
    # 10/22/26: the clock sits between them, so every night is past
    # (engine.past_games counts played games before today) on any real date.
    monkeypatch.setattr(store, "local_today", lambda tz=None: date(2026, 10, 21))
    import weather
    monkeypatch.setattr(weather, "forecast_for_day", lambda *a, **k: None)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    peers.invalidate()
    yield db_path
    peers.invalidate()


def _place(db, name, email):
    rid = create_restaurant(Restaurant(name=name, owner_email=email, timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"latitude": ST_CHARLES[0], "longitude": ST_CHARLES[1]}, db_path=db)
    engine.ensure_follows(get_restaurant(rid, db_path=db), db_path=db)
    return rid


def _fan(db, i, email=None, materialise=True):
    """A restaurant that measured three Bears home nights (+20, +30, +10 and i)."""
    rid = _place(db, f"Tap {i}", email or f"o{i}@x{i}.com")
    c = models.get_conn(db)
    try:
        for d, lift in zip(BEARS_NIGHTS, (20 + i, 30 + i, 10 + i)):
            c.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, lift_pct) "
                      "VALUES (?,?,?,?,?,?)", (rid, d, date.fromisoformat(d).strftime("%A"), "event",
                                               "bears soldier field", lift))
        c.commit()
    finally:
        c.close()
    if materialise:
        peers.store_member_effects(rid, db_path=db)
    return rid


def _game(db):
    s = store.series_by_slug("nfl-chicago-bears", db_path=db)
    return [e for e in store.events_for([s["id"]], "2026-10-22", "2026-10-22", db_path=db)][0]


def _viewer(db):
    return _place(db, "EJ Co", "e@x.com")


def _rows(db, rid=None):
    c = models.get_conn(db)
    try:
        sql = "SELECT * FROM event_peer_effects" + (" WHERE restaurant_id=?" if rid else "")
        return [dict(r) for r in c.execute(sql, (rid,) if rid else ()).fetchall()]
    finally:
        c.close()


# ── P4-05: materialised nightly, read once per segment ────────────────────

def test_the_peer_table_is_created_at_boot_and_never_on_a_request(db):
    c = models.get_conn(db)
    try:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_peer_effects'").fetchone()
    finally:
        c.close()
    import inspect
    assert "CREATE" not in inspect.getsource(peers.peer_effect)
    assert "CREATE" not in inspect.getsource(peers.store_member_effects)


def test_a_request_reads_only_the_materialised_medians(db, monkeypatch):
    viewer = _viewer(db)
    game = _game(db)
    for i in range(MIN_QUARTILE_N):
        _fan(db, i, materialise=False)
    # Their nights are on file, but nothing is materialised yet: no figure.
    assert peers.peer_effect(viewer, game, db_path=db) is None
    assert _rows(db) == []
    c = models.get_conn(db)
    try:
        fans = [r["restaurant_id"] for r in c.execute("SELECT DISTINCT restaurant_id FROM event_outcomes")]
    finally:
        c.close()
    for rid in fans:
        assert peers.store_member_effects(rid, db_path=db) == 1
    row = _rows(db, fans[0])[0]
    assert row["home_away"] == "home" and row["preseason"] == 0 and row["nights"] == 3
    assert row["median_lift_pct"] == 20.0 and row["first_night"] == "2026-09-20"
    # The request never reads another restaurant's nights.
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("per-night read on a request"))
    monkeypatch.setattr(engine, "effect_for", boom)
    monkeypatch.setattr(engine, "past_games", boom)
    monkeypatch.setattr(engine, "_outcomes", boom)
    peers.invalidate()
    out = peers.peer_effect(viewer, game, db_path=db)
    assert out and out["n"] == MIN_QUARTILE_N and out["median_lift_pct"] % peers.PEER_STEP_PCT == 0


def test_every_viewer_of_a_segment_shares_one_read(db, monkeypatch):
    game = _game(db)
    a, b = _viewer(db), _place(db, "Other Co", "other@y.com")
    for i in range(MIN_QUARTILE_N):
        _fan(db, i)
    calls = []
    real = peers._segment_rows
    monkeypatch.setattr(peers, "_segment_rows", lambda *x, **k: calls.append(x) or real(*x, **k))
    peers.invalidate()
    assert peers.peer_effect(a, game, db_path=db) and peers.peer_effect(b, game, db_path=db)
    assert peers.peer_effect(a, game, db_path=db)
    assert len(calls) == 1


def test_the_nightly_features_pass_writes_each_restaurants_medians(db):
    from intelligence import jobs
    fans = [_fan(db, i, materialise=False) for i in range(2)]
    out = jobs.run_features(db_path=db, today=date(2026, 10, 5), workers=1)
    assert out["ok"] >= 2
    for rid in fans:
        rows = _rows(db, rid)
        assert len(rows) == 1 and rows[0]["nights"] == 3
    # An unfollow, then the next pass: the row goes with it.
    store.set_follow(fans[0], store.series_by_slug("nfl-chicago-bears", db_path=db)["id"], False, db_path=db)
    jobs.run_features(db_path=db, today=date(2026, 10, 6), workers=1)
    assert _rows(db, fans[0]) == [] and len(_rows(db, fans[1])) == 1


def test_a_restaurant_that_may_not_teach_keeps_no_rows(db):
    rid = _fan(db, 1)
    assert _rows(db, rid)
    update_restaurant(rid, {"exclude_from_learning": 1}, db_path=db)
    from intelligence import jobs
    jobs._excluded_cache["key"] = None
    assert peers.store_member_effects(rid, db_path=db) == 0 and _rows(db, rid) == []


# ── X-11: the privacy floors, one by one ──────────────────────────────────

def test_the_count_floor_holds(db):
    viewer, game = _viewer(db), _game(db)
    for i in range(MIN_QUARTILE_N - 1):
        _fan(db, i)
    assert peers.peer_effect(viewer, game, db_path=db) is None
    _fan(db, MIN_QUARTILE_N)
    peers.invalidate()
    assert peers.peer_effect(viewer, game, db_path=db)


def test_the_viewers_own_organisation_is_left_out(db):
    viewer, game = _viewer(db), _game(db)
    for i in range(MIN_QUARTILE_N - 1):
        _fan(db, i)
    # A sister location under the viewer's own owner email would clear the
    # count; it is the viewer's organisation, so it never counts.
    sister = _fan(db, 50, email="e@x.com")
    orgs = privacy.org_map(db_path=db)
    assert orgs[sister] == orgs[viewer]
    assert peers.peer_effect(viewer, game, db_path=db) is None
    # Seen from anyone else, the sister location counts.
    other = _place(db, "Other Co", "other@y.com")
    peers.invalidate()
    assert peers.peer_effect(other, game, db_path=db)


def test_one_organisation_over_its_share_refuses_the_figure(db):
    viewer, game = _viewer(db), _game(db)
    for i in range(3):                       # 3 of 8: over a third
        _fan(db, 10 + i, email="big@chain.com")
    for i in range(MIN_QUARTILE_N - 3):      # 5 more organisations
        _fan(db, i)
    n_orgs, share = privacy.org_counts([privacy.org_map(db_path=db)[r["restaurant_id"]] for r in _rows(db)])
    assert n_orgs >= privacy.MIN_ORGS and share > privacy.MAX_ORG_SHARE
    assert peers.peer_effect(viewer, game, db_path=db) is None
    _fan(db, 30)                             # 3 of 9: a third, allowed
    peers.invalidate()
    assert peers.peer_effect(viewer, game, db_path=db)


def test_a_converted_demos_demo_era_nights_never_teach(db):
    viewer, game = _viewer(db), _game(db)
    fans = [_fan(db, i) for i in range(MIN_QUARTILE_N)]
    assert peers.peer_effect(viewer, game, db_path=db)
    # Its learning_since moves past two of its three nights. Until the
    # nightly pass re-measures it, its stored row reaches into the demo era
    # and is refused at once.
    update_restaurant(fans[0], {"learning_since": "2026-10-01 00:00:00"}, db_path=db)
    from intelligence import jobs
    jobs._excluded_cache["key"] = None
    peers.invalidate()
    assert peers.peer_effect(viewer, game, db_path=db) is None
    # Re-measured: one night is left, under the segment floor, so no row.
    assert peers.store_member_effects(fans[0], db_path=db) == 0
    assert peers.peer_effect(viewer, game, db_path=db) is None


# ── P4-09: the line states the real threshold ─────────────────────────────

def test_the_peer_line_states_when_cavnar_plans_on_your_own_nights(db):
    viewer, game = _viewer(db), _game(db)
    for i in range(MIN_QUARTILE_N):
        _fan(db, i)
    text = peers.peer_effect(viewer, game, db_path=db)["text"]
    assert f"once {event_memory.EFFECT_MIN_N} games like it are measured here" in text
    assert f"{event_memory.EFFECT_FLOOR_PCT}% or more" in text
    assert f"once {engine.SEGMENT_MIN_N} are measured" not in text


# ── P4-12: the diagnosis prompt names only the lines on its page ──────────

def _diagnose_prompt(db, monkeypatch, category):
    import ai_utils
    import data_health
    import review_intelligence as ri
    rid = _place(db, "Slice Co", "slice@x.com")
    c = models.get_conn(db)
    try:
        for i in (1, 2):
            c.execute("INSERT INTO reviews (id, restaurant_id, platform, external_id, rating, text, review_date, "
                      "fetched_at, sentiment, categories, processed) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                      (i, rid, "google", f"e{i}", 2, "Slow", "2026-09-30", "2026-09-30", "negative",
                       json.dumps([category])))
        c.commit()
    finally:
        c.close()
    cluster = {"category": category, "mentions": 7, "review_ids": [1, 2], "avg_rating": 2.0, "dish": None,
               "role": None, "daypart": None, "weekday": None, "weekday_pair": None, "complaints": [],
               "worst_severity": category, "severity_counts": {}, "unclassified": 0, "first_seen": None,
               "last_seen": None, "window_days": 90}
    monkeypatch.setattr(ri, "complaint_clusters", lambda *a, **k: [cluster])
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {"notes": [], "games": {
        "lean": "more", "game_pct": 40, "other_pct": 10, "window_days": 2, "game_reviews": 30,
        "other_reviews": 40}})
    seen = {}
    reply = {"cause": "c", "alternative_cause": "a", "what_would_confirm": "w", "evidence_review_ids": [1, 2],
             "operational_evidence": [], "confidence": "low", "recommended_action": "Add a server.",
             "expected_outcome": "If the cause is right, x."}

    def fake(client, **kw):
        import types
        seen["p"] = kw["messages"][0]["content"]
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(reply))],
                                     stop_reason="end_turn")
    monkeypatch.setattr(ai_utils, "create_with_retry", fake)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "is_held", lambda r: False)
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {})
    ri.diagnose(rid, force=True, db_path=db)
    return seen["p"]


def test_a_cluster_without_the_game_line_is_never_told_of_it(db, monkeypatch):
    p = _diagnose_prompt(db, monkeypatch, "food_quality")
    assert "Game nights" not in p and '"games"' not in p and "|games" not in p and '"module": "games' not in p


def test_a_service_cluster_with_the_game_line_may_cite_it(db, monkeypatch):
    p = _diagnose_prompt(db, monkeypatch, "service")
    assert '- Game nights: 40%' in p
    assert 'the "Game nights" line is module "games"' in p and '"module": "games"' in p


def test_the_evidence_guide_follows_the_lines_passed():
    import review_intelligence as ri
    empty = ri.evidence_guide({})
    assert empty["evidence_shape"].startswith("[]") and "games" not in empty["evidence_rule"]
    some = ri.evidence_guide({"labor": 1, "worked": 1})
    assert '"module": "labor|worked"' in some["evidence_shape"]
    assert '"Worked" (module "worked") line under' in some["evidence_rule"] and "Game nights" not in some["evidence_rule"]


# ── X-7: the restaurant's own day ─────────────────────────────────────────

def test_a_review_stamp_reads_as_the_restaurants_own_day():
    from event_intel.reviews import local_day
    tz = "America/Chicago"
    assert local_day("2026-09-23T03:00:00Z", tz) == "2026-09-22"          # 10pm CDT the evening before
    assert local_day("2026-09-23T03:00:00+00:00", tz) == "2026-09-22"
    assert local_day("2026-09-22T22:00:00", tz) == "2026-09-22"           # stored local already (MOD-REV-9)
    assert local_day("2026-09-22", tz) == "2026-09-22"
    assert local_day("2026-09-22T22:00:00-05:00", "America/Los_Angeles") == "2026-09-22"


def test_an_evening_review_with_an_offset_lands_in_the_game_window(db):
    from event_intel import reviews
    rid = _place(db, "EJ Co", "e@x.com")
    c = models.get_conn(db)
    try:
        rows = [(f"late{i}", "2026-09-23T03:00:00Z", ["service"]) for i in range(20)]     # 9/22 10pm CT
        rows += [(f"day{i}", "2026-09-04T12:00:00", ["food_quality"]) for i in range(20)]
        for ext, stamp, cats in rows:
            c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, "
                      "fetched_at, processed, categories, sentiment) VALUES (?,?,?,?,?,?,?,1,?,?)",
                      (rid, "csv", ext, 2, "x", stamp, "2026-09-30T09:00:00", json.dumps(cats), "negative"))
        c.commit()
    finally:
        c.close()
    out = reviews.game_night_reviews(rid, today=date(2026, 10, 1), days=60, db_path=db)
    # 9/20 is a Bears home game: 9/22 is the window's last day.
    assert out["game_reviews"] == 20 and out["game_service"] == 20 and out["other_reviews"] == 20


# ── SD-01 / SD-02: season data ────────────────────────────────────────────

def _season(name):
    return json.load(open(os.path.join(store.SEASONS_DIR, name)))


def test_the_white_sox_division_series_carries_its_network():
    alds = [e for e in _season("mlb-chicago-white-sox-2026-postseason.json")["events"]
            if e["week"] == "AL Division Series"]
    assert len(alds) == 5 and all(e["broadcast"] == "TBS" for e in alds)
    assert {e["external_id"] for e in alds} == {"849829", "849834", "849833", "849832", "849831"}


def test_the_fires_seatgeek_game_is_marked_an_alternate_venue(db):
    fire = {e["external_id"]: e for e in _season("mls-chicago-fire-2026.json")["events"]}
    g = fire["761935"]
    assert g["venue"] == "SeatGeek Stadium" and g["home_away"] == "home"
    assert g["attributes"] == {"alt_venue": True}
    # The one MLS game away from Soldier Field; the cup matches (season_type
    # "special") played at SeatGeek carry it too, as their own class.
    assert sum(1 for e in fire.values() if (e.get("attributes") or {}).get("alt_venue")
               and (e.get("season_type") or "regular") == "regular") == 1
    assert all((e.get("attributes") or {}).get("alt_venue") for e in fire.values()
               if e.get("venue") == "SeatGeek Stadium" and e.get("home_away") == "home")
    # Loaded into the catalog with it.
    s = store.series_by_slug("mls-chicago-fire", db_path=db)
    row = [e for e in store.events_for([s["id"]], "2026-11-07", "2026-11-07", db_path=db)
           if e["external_id"] == "761935"][0]
    assert row["attributes"].get("alt_venue") is True
