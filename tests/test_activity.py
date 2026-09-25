"""activity.py — the AI activity feed.

The one invariant that matters: nothing in the feed is generated to fill
space. A restaurant with no rows gets no lines; a "working" line appears
only when the process it names is armed; a viewer denied a module never
reads that module's lines. Everything else is shape.
"""
from datetime import datetime, timedelta, timezone

import pytest

import activity
import models
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    for mod in (models, activity):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    activity._CACHE.clear()


def _rid(db_path, **kw):
    kw.setdefault("module_reviews", 1)
    return create_restaurant(Restaurant(name="Feed Co", owner_email="f@x.com", **kw), db_path=db_path)


def _utc(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def test_an_empty_restaurant_gets_an_empty_feed_not_a_story(db_path):
    rid = _rid(db_path, module_labor=1, module_inventory=1, module_marketing=1)
    out = activity.build(rid, db_path=db_path)
    assert out["entries"] == [] and out["memory"] == []
    # No "watching for reviews" without reviews_live or a Google token, no
    # signal count without a measured metric, nothing at all.
    assert out["working"] == []


def test_analyzed_reviews_are_reported_with_their_time(db_path):
    rid = _rid(db_path)
    conn = get_conn(db_path)
    for i in range(3):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at, processed, draft_response) VALUES (?,?,?,?,?,?,?,?,1,?)",
                     (rid, "google", f"r{i}", "A", 5, "t", _utc(hours=3), _utc(hours=2), "Thanks!" if i < 2 else None))
    conn.commit(); conn.close()
    out = activity.build(rid, db_path=db_path)
    texts = [e["text"] for e in out["entries"]]
    assert "Finished analyzing 3 new reviews." in texts
    assert "Drafted 2 replies in your voice." in texts
    e = next(x for x in out["entries"] if x["kind"] == "analyzed")
    assert e["at"].endswith("Z") and e["module"] == "reviews"


def test_working_lines_only_name_processes_that_are_armed(db_path):
    rid = _rid(db_path)
    assert activity.build(rid, db_path=db_path)["working"] == []
    from models import update_restaurant
    update_restaurant(rid, {"reviews_live": 1}, db_path=db_path)
    # "Watching" only while the fetch keeps up (Data Freshness #27, DH4-14):
    # a connection whose fetch never ran says so instead.
    w = [x["text"] for x in activity.build(rid, db_path=db_path)["working"]]
    assert "Review checks haven't run yet" in w and not any(x.startswith("Watching") for x in w)
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET last_fetched_at=? WHERE id=?",
                 (_dt.now(_ZI("America/Chicago")).strftime("%Y-%m-%dT%H:%M:%S"), rid))
    conn.commit()
    conn.close()
    activity._CACHE.clear()
    w = [x["text"] for x in activity.build(rid, db_path=db_path)["working"]]
    assert any(x.startswith("Watching for new reviews") for x in w)
    # The 90-day comparison line needs reviews to compare.
    assert not any("Comparing" in x for x in w)


def test_competitors_are_counted_distinctly_and_only_when_recent(db_path):
    rid = _rid(db_path)
    models.init_competitor_snapshots(db_path)       # a lazy table, created by Intel's first run
    conn = get_conn(db_path)
    for pid, when in (("a", _utc(days=1)), ("a", _utc(days=2)), ("b", _utc(days=1)), ("c", _utc(days=45))):
        conn.execute("INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, captured_at) "
                     "VALUES (?,?,?,?,?)", (rid, pid, pid, 4.2, when))
    conn.commit(); conn.close()
    out = activity.build(rid, db_path=db_path)
    assert "Compared 2 competitors on rating, volume and price." in [e["text"] for e in out["entries"]]
    assert "Watching 2 competitors" in [w["text"] for w in out["working"]]


def test_a_viewer_denied_a_module_never_reads_its_lines(db_path):
    rid = _rid(db_path, module_labor=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, week_end, hours_scheduled, "
                 "hours_budget) VALUES (?,?,?,?,?,?)", (rid, _utc(hours=5), "2026-09-21", "2026-09-27", 300, 320))
    conn.commit(); conn.close()
    full = activity.build(rid, db_path=db_path)
    assert any(e["module"] == "labor" for e in full["entries"])
    denied = activity.build(rid, db_path=db_path, denied={"labor"})
    assert not any(e["module"] == "labor" for e in denied["entries"])
    assert not any(w["module"] == "labor" for w in denied["working"])


def test_a_tracker_in_flight_is_remembered_with_its_day_count(db_path):
    rid = _rid(db_path, module_labor=1)
    today = datetime.now(timezone.utc).date()
    conn = get_conn(db_path)
    conn.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                 "started_on, evaluate_on, status) VALUES (?,?,?,?,?,?,?,?)",
                 (rid, "home", "k1", "Cut Tuesday lunch by one server", "labor_pct",
                  (today - timedelta(days=8)).isoformat(), (today + timedelta(days=19)).isoformat(), "measuring"))
    conn.commit(); conn.close()
    mem = activity.build(rid, db_path=db_path)["memory"]
    assert mem and mem[0]["text"] == "Still measuring “Cut Tuesday lunch by one server” — day 9 of 27."
    assert activity.build(rid, db_path=db_path, denied={"labor"})["memory"] == []


def test_the_brief_and_flags_are_owner_facing_work(db_path):
    rid = _rid(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)", (rid, "morning_brief", _utc(hours=6)))
    conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)", (rid, "1star", _utc(hours=4)))
    conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)", (rid, "login", _utc(hours=1)))
    conn.commit(); conn.close()
    texts = [e["text"] for e in activity.build(rid, db_path=db_path)["entries"]]
    assert "Prepared this morning's brief." in texts
    assert "Flagged 1 thing worth your attention." in texts        # a login is not a flag


def test_the_feed_is_cached_briefly_per_viewer(db_path, monkeypatch):
    rid = _rid(db_path)
    calls = []
    real = activity.build
    monkeypatch.setattr(activity, "build", lambda *a, **k: calls.append(1) or real(*a, **k))
    activity.feed(rid, db_path=db_path); activity.feed(rid, db_path=db_path)
    activity.feed(rid, db_path=db_path, denied={"inventory"})
    assert len(calls) == 2


def test_the_route_exists_on_both_sides():
    import strategy_routes
    assert ("/activity", ("GET",)) in {(p, tuple(m)) for p, m, *_ in strategy_routes._ROUTES}


def test_the_web_strip_and_the_ask_trail_are_in_the_template():
    html = open("templates/dashboard.html").read()
    assert 'id="ai-strip"' in html and "/api/activity" in html
    assert "ask-steps" in html and "_askStagger" in html
