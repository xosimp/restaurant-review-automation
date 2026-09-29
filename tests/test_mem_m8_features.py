"""Memory fix round 9/29/26, workstream M8 — `features` (PLATFORM-5):
weekly feature rows carry the definition version they were computed under
(a column and `features_version` in the JSON), every reader takes only the
current version, compute(today=<a past day>) reads nothing after that day,
and a bounded, resumable backfill computes the past weeks a restaurant has
raw history for, marking them `backfilled`.
"""
import json
import sys
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant

import intelligence  # noqa: E402
from intelligence import features, jobs, engine  # noqa: E402


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
    jobs.invalidate_excluded()
    yield db_path
    jobs.invalidate_excluded()


def _rid(db, name="Feature Co", **kw):
    kw.setdefault("created_at", (date.today() - timedelta(days=400)).isoformat() + "T00:00:00")
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test", **kw),
                             db_path=db)


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _days(db, rid, start, n, labor_pct=30.0, sales=2000.0):
    for k in range(n):
        d = (start + timedelta(days=k)).isoformat()
        _x(db, "INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, sales, total_hours) VALUES (?,?,?,?,?)",
           (rid, d, labor_pct, sales, 40.0))


def test_every_row_carries_its_definition_version_and_readers_take_only_the_current(db):
    rid = _rid(db)
    week = features.iso_week(date.today())
    features.store(rid, {"labor_pct_28d": 31.0}, week=week, db_path=db)
    row = _q(db, "SELECT version, backfilled, features_json FROM intel_features WHERE restaurant_id=?", (rid,))[0]
    assert row["version"] == features.FEATURES_VERSION and row["backfilled"] == 0
    assert json.loads(row["features_json"])["features_version"] == features.FEATURES_VERSION
    # a row from before versioning: its definition is unknown, no reader takes it
    old = features.iso_week(date.today() - timedelta(weeks=1))
    _x(db, "INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,0.9)",
       (rid, old, json.dumps({"labor_pct_28d": 60.0})))
    _x(db, "UPDATE intel_features SET version=NULL WHERE week=?", (old,))      # as the column left it
    assert [r["week"] for r in features.series(rid, weeks=10, db_path=db)] == [week]
    assert features.latest(rid, db_path=db)["week"] == week
    assert all(old not in rows for rows in [features.weekly_by_restaurant(db_path=db)])
    # a row written without a version is stamped the current one (the boot trigger)
    older = features.iso_week(date.today() - timedelta(weeks=2))
    _x(db, "INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,0.9)",
       (rid, older, json.dumps({"labor_pct_28d": 29.0})))
    assert _q(db, "SELECT version FROM intel_features WHERE week=?", (older,))[0]["version"] == \
        features.FEATURES_VERSION


def test_a_past_day_reads_nothing_after_it(db):
    rid = _rid(db)
    asof = date.today() - timedelta(days=60)
    _days(db, rid, asof - timedelta(days=27), 28, labor_pct=30.0)          # the window
    _days(db, rid, asof + timedelta(days=1), 30, labor_pct=90.0, sales=5000)  # after it
    for k in range(6):
        d = (asof - timedelta(days=2 + k)).isoformat() + "T12:00:00"
        after = (asof + timedelta(days=5)).isoformat() + " 12:00:00"
        _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, fetched_at, "
               "response_status, posted_at) VALUES (?, 'yelp', ?, 4, 't', ?, ?, 'posted', ?)",
           (rid, f"y{k}", d, d, after))
    for k in range(3):                                                   # reviews after the day
        d = (asof + timedelta(days=3 + k)).isoformat() + "T12:00:00"
        _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, fetched_at) "
               "VALUES (?, 'yelp', ?, 1, 't', ?, ?)", (rid, f"z{k}", d, d))
    f = features.compute(rid, today=asof, db_path=db)
    assert f["labor_pct_28d"] == 30.0 and f["weekend_sales_share_28d"] is not None
    assert f["reviews_30d"] == 6 and f["avg_rating_30d"] == 4.0
    assert f["reply_rate_30d"] == 0.0                   # the replies were posted after the day: not yet made then
    assert f["features_version"] == features.FEATURES_VERSION


def test_the_backfill_fills_past_weeks_from_the_raw_tables_and_resumes(db):
    rid = _rid(db, "Arrives Co")
    start = date.today() - timedelta(weeks=14)
    _days(db, rid, start, 14 * 7)
    assert features.latest(rid, db_path=db) is None
    todo = features.weeks_to_backfill(rid, db_path=db)
    assert todo and todo[-1] < features.iso_week(date.today())
    # bounded: the first week always runs, then the clock stops it — mid-restaurant
    first = jobs.run_features_backfill(db_path=db, wall_seconds=0)
    assert first["hit_bound"] and first["ok"] == 0 and first["weeks"] <= 1
    assert jobs._backfill_mark(db, rid) == (features.FEATURES_VERSION, todo[0])     # one week, then stopped
    for k in ("attempted", "ok", "failed", "skipped", "hit_bound"):
        assert k in first
    rest = jobs.run_features_backfill(db_path=db)
    assert not rest["hit_bound"] and rest["ok"] >= 1
    rows = _q(db, "SELECT week, version, backfilled, completeness FROM intel_features WHERE restaurant_id=? "
                  "ORDER BY week", (rid,))
    assert len(rows) >= 12 and all(r["backfilled"] == 1 and r["version"] == features.FEATURES_VERSION for r in rows)
    assert features.iso_week(date.today()) not in {r["week"] for r in rows}     # this week is the nightly pass's
    # the owner's "your normal" can now be read
    cm = engine.compare(rid, "labor_pct_28d", kinds=("self",), db_path=db)
    assert len(engine._series(rid, db)) >= 12 and cm.get("comparisons")
    # the watermark: a later pass recomputes nothing
    assert jobs.run_features_backfill(db_path=db)["weeks"] == 0


def test_a_definition_change_re_derives_the_older_weeks(db, monkeypatch):
    rid = _rid(db, "Redefined Co")
    _days(db, rid, date.today() - timedelta(weeks=6), 6 * 7)
    jobs.run_features_backfill(db_path=db)
    n = len(_q(db, "SELECT 1 FROM intel_features WHERE restaurant_id=?", (rid,)))
    monkeypatch.setattr(features, "FEATURES_VERSION", features.FEATURES_VERSION + 1)
    assert features.series(rid, db_path=db) == []                   # the old definition's weeks are not read
    out = jobs.run_features_backfill(db_path=db)
    assert out["weeks"] == n
    assert {r["version"] for r in _q(db, "SELECT version FROM intel_features WHERE restaurant_id=?", (rid,))} == \
        {features.FEATURES_VERSION}


def test_a_converted_demos_seeded_weeks_are_never_its_own_past(db):
    rid = _rid(db, "Former Demo")
    cleared = date.today() - timedelta(weeks=4)
    _x(db, "UPDATE restaurants SET is_demo=0, demo_cleared_at=? WHERE id=?", (cleared.isoformat(), rid))
    _days(db, rid, date.today() - timedelta(weeks=12), 12 * 7)
    jobs.run_features_backfill(db_path=db)
    weeks = [r["week"] for r in _q(db, "SELECT week FROM intel_features WHERE restaurant_id=?", (rid,))]
    assert weeks and min(weeks) >= features.iso_week(cleared - timedelta(days=7))


def test_the_backfill_is_a_registered_job_in_the_loop():
    import inspect
    import jobs_registry
    import scheduler
    spec = jobs_registry.JOBS["intelligence_features_backfill"]
    assert spec["target"] == ("intelligence.jobs", "run_features_backfill") and not spec["sends"]
    assert 'run_job("intelligence_features_backfill"' in inspect.getsource(scheduler.scheduler_loop)
