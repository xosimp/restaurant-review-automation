"""The daily competitor ratings check (owner, 10/2/26): the tracked
competitors' ratings and review counts re-read every morning — Places
details only, never a search or a model call — and the full analysis again
only when one moved enough to change the read."""
import json
import sys

import pytest

import competitor
import models
from models import Restaurant, create_restaurant, update_restaurant


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    r = create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com", timezone="America/Chicago"), db_path=db_path)
    blob = {"competitors": [{"name": "Rec Haus", "place_id": "p1", "rating": 4.5, "review_count": 300},
                            {"name": "Whiskey Bend", "place_id": "p2", "rating": 4.3, "review_count": 120}],
            "insight": "read", "generated_at": "2026-09-28"}
    c = models.get_conn(db_path)
    c.execute("UPDATE restaurants SET competitor_intel=?, google_place_id=? WHERE id=?", (json.dumps(blob), "own", r))
    c.commit(); c.close()
    return r


class _Resp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def _places(answers):
    calls = []

    def fake(endpoint, params, restaurant_id=None, action=None, timeout=10):
        calls.append((endpoint, params["place_id"], action))
        return _Resp({"status": "OK", "result": answers.get(params["place_id"], {})})
    return fake, calls


def test_a_quiet_morning_updates_the_numbers_and_reads_nothing_again(rid, monkeypatch):
    fake, calls = _places({"p1": {"rating": 4.5, "user_ratings_total": 304, "business_status": "OPERATIONAL"},
                           "p2": {"rating": 4.3, "user_ratings_total": 121, "business_status": "OPERATIONAL"},
                           "own": {"rating": 4.6, "user_ratings_total": 900}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert out["ok"] and out["checked"] == 2 and not out["reanalyse"]
    assert all(c[0] == "details" for c in calls) and len(calls) == 3
    blob = json.loads(models.get_restaurant(rid).competitor_intel)
    assert blob["competitors"][0]["review_count"] == 304 and blob["ratings_checked_at"]
    assert blob["insight"] == "read"


def test_a_review_burst_or_a_closure_asks_for_a_new_read_and_a_rating_move_is_only_reported(rid, monkeypatch):
    # A rating move alone is written into the comparison in place, never a
    # new Claude read (AI cost audit 10/7/26 #14): it is in `moved`, not in
    # `triggers`. tests/test_places_economy.py holds the rating-only case.
    fake, _calls = _places({"p1": {"rating": 4.3, "user_ratings_total": 300, "business_status": "OPERATIONAL"},
                            "p2": {"rating": 4.3, "user_ratings_total": 140, "business_status": "CLOSED_PERMANENTLY"}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert out["reanalyse"]
    assert any("4.5★ → 4.3★" in m for m in out["moved"])
    assert not any("4.5★ → 4.3★" in t for t in out["triggers"])
    assert any("20 new reviews" in m for m in out["triggers"]) and any("closed permanently" in m for m in out["triggers"])


def test_the_job_is_registered_and_scheduled():
    import inspect
    import jobs_registry
    import scheduler
    assert jobs_registry.JOBS["competitor_daily"]["target"] == ("scheduler", "run_daily_competitor_ratings")
    assert '_ops.run_in_lane("intel", "competitor_daily", run_daily_competitor_ratings)' in \
        inspect.getsource(scheduler.scheduler_loop)
