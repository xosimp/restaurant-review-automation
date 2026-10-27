"""A restaurant whose time zone nobody chose is flagged, never silently
Chicago (blind re-audit 10/26/26, lens B2).

Every per-restaurant clock reads restaurants.timezone — the Daily Sales
Report's night and deadline, quiet hours, the schedule's dates — and the
column defaults to America/Chicago whether or not anyone set it. Data Health
(and the account's own health) now say so: a zone nobody set with no
location to vouch for it, a location that says another zone, a name the time
zone database doesn't know. Any write of the zone stamps it as chosen.
"""
import sys
from datetime import date, datetime

import pytest

import account_health
import data_health
import models
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    data_health.invalidate()
    return db_path


def _sql(db, rid, **cols):
    conn = models.get_conn(db)
    conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()
    data_health.invalidate()


def test_a_restaurant_created_with_no_time_zone_is_flagged_until_somebody_sets_it(db):
    rid = create_restaurant(Restaurant(name="No Zone Diner", owner_email="nz@x.test"), db_path=db)
    r = get_restaurant(rid, db_path=db)
    assert r.timezone == "America/Chicago" and r.timezone_confirmed_at is None
    flag = data_health.timezone_problem(r)
    assert flag and flag["key"] == "timezone" and "Nobody has confirmed your time zone" in flag["text"]
    snap = data_health.snapshot(rid, restaurant=r, use_cache=False)
    assert [n["key"] for n in snap["setup_notes"]] == ["timezone"]
    # The account's own health names it too (the check could never fire).
    health = account_health.payload({"restaurant_id": rid, "role": "client", "id": 1}, restaurant=r,
                                    include_value=False)
    profile = next(i for i in health["items"] if i["key"] == "profile")
    assert "your timezone" in profile["sub"]
    # Saving the zone — even the same one — is somebody choosing it.
    update_restaurant(rid, {"timezone": "America/Chicago"}, db_path=db)
    r = get_restaurant(rid, db_path=db)
    assert r.timezone_confirmed_at
    assert data_health.timezone_problem(r) is None
    assert data_health.snapshot(rid, restaurant=r, use_cache=False)["setup_notes"] == []


def test_a_west_coast_store_left_on_chicago_is_flagged_by_its_own_location(db):
    rid = create_restaurant(Restaurant(name="Coast Bar", owner_email="cb@x.test"), db_path=db)
    _sql(db, rid, latitude=34.05, longitude=-118.24)                    # Los Angeles
    flag = data_health.timezone_problem(get_restaurant(rid, db_path=db))
    assert flag and flag["tone"] == "warn" and "looks like another zone" in flag["text"]
    # The right zone, never confirmed: the location vouches for it.
    _sql(db, rid, timezone="America/Los_Angeles")
    assert data_health.timezone_problem(get_restaurant(rid, db_path=db)) is None
    # A Chicago store on Chicago, never confirmed, with its location: fine.
    rid2 = create_restaurant(Restaurant(name="Loop Grill", owner_email="lg@x.test"), db_path=db)
    _sql(db, rid2, latitude=41.88, longitude=-87.63)
    assert data_health.timezone_problem(get_restaurant(rid2, db_path=db)) is None
    # Chicago left on a New York store: an hour off is still flagged.
    rid3 = create_restaurant(Restaurant(name="Hudson", owner_email="hu@x.test"), db_path=db)
    _sql(db, rid3, latitude=40.71, longitude=-74.0)
    assert data_health.timezone_problem(get_restaurant(rid3, db_path=db))


def test_a_zone_the_database_doesnt_know_is_flagged_even_when_confirmed(db):
    rid = create_restaurant(Restaurant(name="Typo Cafe", owner_email="tc@x.test"), db_path=db)
    _sql(db, rid, timezone="America/Chicag0", timezone_confirmed_at="2026-10-01 00:00:00")
    flag = data_health.timezone_problem(get_restaurant(rid, db_path=db))
    assert flag and "isn't one Cavnar AI knows" in flag["text"]


def test_an_la_restaurants_report_deadline_is_4am_pacific(db):
    """The deadline follows the restaurant's own zone: 4am PT is 11:00 UTC
    in October (PDT), never 4am Chicago's 09:00 UTC."""
    from dsr import pipeline
    rid = create_restaurant(Restaurant(name="Coast Bar", owner_email="cb@x.test", timezone="America/Los_Angeles"),
                            db_path=db)
    r = get_restaurant(rid, db_path=db)
    day = date(2026, 10, 20)
    assert pipeline.deadline_at(r, day) == datetime(2026, 10, 21, 4, 0)
    assert pipeline.to_utc(r, pipeline.deadline_at(r, day)) == datetime(2026, 10, 21, 11, 0)
