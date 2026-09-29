"""Memory fix round (9/29/26), workstream M7 — "outside" and "value_nightly".

  * outside: the async-job store ran CREATE TABLE IF NOT EXISTS on every
    job read and write (init_ops creates it at boot), and two memory readers
    dated their day by the server's clock — the value snapshots (SQLite's
    UTC date('now')) and busiest days (date.today()).
  * value_nightly: the value series was written only when someone opened
    Home, so it had holes on exactly the days nobody looked. A nightly,
    bounded, resumable pass now writes each in-service restaurant's point,
    dated on its own local day, kept forever.
"""
import inspect
import re
import sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pytest

import models
import ops
import time_utils
import value_delivered
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Nightly Co", billing="active", tz="America/Chicago"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET billing_status=?, timezone=? WHERE id=?", (billing, tz, rid))
    conn.commit()
    conn.close()
    return rid


def _rows(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── outside: no DDL on the async-job path ───────────────────────────────────

class _Spy:
    def __init__(self, conn, seen):
        self._c, self._seen = conn, seen

    def execute(self, sql, *a):
        self._seen.append(sql)
        return self._c.execute(sql, *a)

    def __getattr__(self, k):
        return getattr(self._c, k)


def test_the_async_job_store_runs_no_ddl_per_call(db_path, monkeypatch):
    seen = []
    base = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: _Spy(base(), seen))
    ops.start_async_job("job-ddl", "schedule", 7)
    ops.finish_async_job("job-ddl", "done", {"ok": True})
    assert ops.read_async_job("job-ddl")["status"] == "done"
    ops.active_job("schedule", 7)
    ops.inflight_async_jobs()
    assert seen, "the store did not run through get_conn"
    ddl = re.compile(r"\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX)", re.I)
    assert not [s for s in seen if ddl.search(s)], "schema DDL on the async-job call path"
    assert not ddl.search(inspect.getsource(ops._async_conn).split('"""')[-1])


def test_the_async_jobs_table_is_made_at_boot(db_path):
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='async_jobs'").fetchone()
    finally:
        conn.close()
    assert "_ASYNC_JOB_SQL" in inspect.getsource(ops.init_ops)


# ── outside: the restaurant's own day ───────────────────────────────────────

def _pin_local(monkeypatch, when):
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: when)


def test_a_value_snapshot_is_dated_on_the_restaurants_local_day(monkeypatch):
    rid = _rid(tz="Pacific/Honolulu")
    # 9:30pm on 9/28 in Honolulu is already 9/29 in UTC.
    local = datetime(2026, 9, 28, 21, 30, tzinfo=ZoneInfo("Pacific/Honolulu"))
    _pin_local(monkeypatch, local)
    value_delivered.record_value_snapshot(rid, 420)
    assert _rows("SELECT snapshot_date, total_value FROM value_snapshots WHERE restaurant_id=?", (rid,)) == \
        [{"snapshot_date": "2026-09-28", "total_value": 420}]
    assert "date('now')" not in inspect.getsource(value_delivered.record_value_snapshot)


def test_value_history_reads_back_to_the_local_day_window(monkeypatch):
    rid = _rid()
    conn = models.get_conn()
    for d, v in (("2026-08-01", 1), ("2026-09-01", 2), ("2026-09-20", 3)):
        conn.execute("INSERT INTO value_snapshots (restaurant_id, snapshot_date, total_value) VALUES (?,?,?)", (rid, d, v))
    conn.commit()
    conn.close()
    _pin_local(monkeypatch, datetime(2026, 9, 28, 12, 0, tzinfo=ZoneInfo("America/Chicago")))
    got = value_delivered.get_value_history(rid, days=30)
    assert [p["value"] for p in got] == [2, 3]


def test_busiest_days_counts_back_from_the_restaurants_own_day(monkeypatch):
    from intelligence import memory
    rid = _rid()
    start = date(2026, 6, 1)
    conn = models.get_conn()
    for i in range(90):
        d = start + timedelta(days=i)
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct) VALUES (?,?,?,?)",
                     (rid, d.isoformat(), 1000 + (500 if d.weekday() == 4 else 0), 25.0))
    conn.commit()
    conn.close()
    # The restaurant's day is 8/29/26: the 84 days back cover the history.
    _pin_local(monkeypatch, datetime(2026, 8, 29, 20, 0, tzinfo=ZoneInfo("America/Chicago")))
    out = memory.busiest_days(rid)
    assert out["available"] and out["busiest"][0] == "Friday"
    # A year later nothing is inside the window: the floor moved with the
    # restaurant's day, not the server's.
    _pin_local(monkeypatch, datetime(2027, 9, 1, 20, 0, tzinfo=ZoneInfo("America/Chicago")))
    assert memory.busiest_days(rid)["available"] is False
    assert "date.today()" not in inspect.getsource(memory.busiest_days).split('"""')[-1]


# ── value_nightly: a point every night, for every restaurant in service ─────

def test_the_nightly_pass_writes_a_point_for_every_restaurant_in_service(monkeypatch):
    a = _rid("Alpha Night")
    b = _rid("Bravo Night", billing="past_due")        # past due is still in service
    gone = _rid("Gone Night", billing="canceled")
    ours = _rid("Ours Night", billing="internal")
    _pin_local(monkeypatch, datetime(2026, 9, 28, 23, 0, tzinfo=ZoneInfo("America/Chicago")))
    monkeypatch.setattr(value_delivered, "compute_total_value_delivered",
                        lambda rid, db_path=None: {a: 300, b: -40}.get(rid, 0))
    out = value_delivered.run_value_snapshots()
    assert {k: out[k] for k in ("attempted", "ok", "failed", "hit_bound")} == \
        {"attempted": 2, "ok": 2, "failed": 0, "hit_bound": False}
    got = {r["restaurant_id"]: (r["snapshot_date"], r["total_value"])
           for r in _rows("SELECT * FROM value_snapshots")}
    assert got == {a: ("2026-09-28", 300), b: ("2026-09-28", -40)}
    assert gone not in got and ours not in got


def test_the_nightly_pass_is_bounded_and_resumes_from_its_cursor(monkeypatch):
    ids = [_rid(f"R{i} Night") for i in range(3)]
    _pin_local(monkeypatch, datetime(2026, 9, 28, 23, 0, tzinfo=ZoneInfo("America/Chicago")))
    import scheduler
    calls = []
    monkeypatch.setattr(value_delivered, "compute_total_value_delivered",
                        lambda rid, db_path=None: calls.append(rid) or 1)
    real = scheduler.resumable_sweep
    seen = {}

    def spy(key, ids_, fn, max_seconds, workers=1, job=None):
        seen.update(key=key, job=job, ids=list(ids_))
        return real(key, ids_, fn, max_seconds, workers=workers, job=job)
    monkeypatch.setattr(scheduler, "resumable_sweep", spy)
    value_delivered.run_value_snapshots()
    assert seen["key"] == value_delivered.VALUE_SNAPSHOT_CURSOR and seen["job"] == "value_snapshots"
    assert sorted(seen["ids"]) == sorted(ids)
    assert sorted(calls) == sorted(ids)


def test_a_failure_is_captured_and_counted_not_raised(monkeypatch):
    rid = _rid("Fails Night")
    _pin_local(monkeypatch, datetime(2026, 9, 28, 23, 0, tzinfo=ZoneInfo("America/Chicago")))

    def boom(rid, db_path=None):
        raise RuntimeError("outcomes unreadable")
    monkeypatch.setattr(value_delivered, "compute_total_value_delivered", boom)
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append(k.get("job")))
    out = value_delivered.run_value_snapshots()
    assert out["failed"] == 1 and out["ok"] == 0
    assert captured == ["value_snapshots"]


def test_the_value_history_job_is_registered_and_scheduled():
    import jobs_registry
    import scheduler
    spec = jobs_registry.JOBS["value_snapshots"]
    assert spec["target"] == ("value_delivered", "run_value_snapshots") and spec["sends"] is False
    assert '_ops.run_job("value_snapshots", run_value_snapshots)' in inspect.getsource(scheduler.scheduler_loop)
    import ops as _ops
    assert "value_snapshots" not in _ops._RETENTION_DAYS, "the value history is kept forever"
