"""Cross-cutting performance edge cases from the MOD audit (MOD-PERF-1..7):
the single scheduler thread, restaurant hydration cost, claim bookkeeping,
the delayed-action drain rate, silent hydration failures, per-restaurant
query plans and the emailed backup's size.

The 100k-restaurant numbers in the audit are not reproduced here; each test
pins the shape that makes them true (a count, a query plan, an ordering) on
a small database, so it runs in milliseconds and still fails the same way.
Confirmed defects are strict xfails naming the finding."""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import models
import ops


@pytest.fixture
def redirect(monkeypatch, db_path):
    """ops and delayed resolve get_conn through models at call time (or bind
    it); point both at the test database."""
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import delayed
    monkeypatch.setattr(delayed, "get_conn", lambda *a, **k: real(db_path))
    return db_path


# ── MOD-PERF-1: per-tick jobs and the lease while a long fetch runs ─────────

class _StopLoop(BaseException):
    """Leaves scheduler_loop after one tick; BaseException so the loop's own
    `except Exception` cannot swallow it."""


def _one_tick_with_a_long_fetch(monkeypatch):
    import scheduler
    import notify
    import issues
    import morning_brief
    import delayed
    import marketing_publish
    events = []
    released = threading.Event()

    monkeypatch.setattr(scheduler, "_chi_now",
                        lambda: datetime(2026, 9, 22, 8, 5, tzinfo=ZoneInfo("America/Chicago")))

    def lease(*a, **k):
        events.append("lease")
        return True
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lease)
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda job, period: job == "review_fetch")
    monkeypatch.setattr(scheduler._ops, "run_job", lambda name, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(scheduler._ops, "capture", lambda *a, **k: None)

    def long_fetch():
        # A fetch that runs to its multi-hour bound. The stub waits (briefly)
        # for the per-tick work that should be able to run alongside it.
        events.append("fetch_start")
        released.wait(0.1)
        events.append("fetch_end")
        return {"processed": 0}
    monkeypatch.setattr(scheduler, "run_daily_fetch", long_fetch)

    def release_due_alerts(*a, **k):
        events.append("release_due_alerts")
        released.set()
        return {}
    monkeypatch.setattr(notify, "release_due_alerts", release_due_alerts)
    monkeypatch.setattr(marketing_publish, "run_due_posts",
                        lambda **k: events.append("run_due_posts") or {})
    monkeypatch.setattr(issues, "tick", lambda *a, **k: None)
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: None)
    monkeypatch.setattr(delayed, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat",
                        lambda *a, **k: events.append("heartbeat"))
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)
    errors = []
    monkeypatch.setattr(scheduler.log, "error", lambda msg, *a, **k: errors.append(str(msg)))

    def _stop(_s):
        raise _StopLoop()
    monkeypatch.setattr(scheduler.time, "sleep", _stop)
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()
    assert not [e for e in errors if "Scheduler loop error" in e], errors
    return events


def test_a_tick_that_runs_the_fetch_still_reaches_every_per_tick_job(monkeypatch):
    events = _one_tick_with_a_long_fetch(monkeypatch)
    for name in ("fetch_start", "fetch_end", "release_due_alerts", "run_due_posts", "heartbeat"):
        assert name in events, events


def test_held_alerts_and_scheduled_posts_are_released_while_the_fetch_is_still_running(monkeypatch):
    events = _one_tick_with_a_long_fetch(monkeypatch)
    end = events.index("fetch_end")
    assert events.index("release_due_alerts") < end
    assert events.index("run_due_posts") < end


def test_the_scheduler_lease_is_refreshed_while_the_fetch_is_running(monkeypatch):
    events = _one_tick_with_a_long_fetch(monkeypatch)
    start, end = events.index("fetch_start"), events.index("fetch_end")
    assert any(e in ("lease", "heartbeat") for e in events[start:end])


# ── MOD-PERF-2: hydration cost is O(rows), not O(rows x columns) ────────────

def _insert_restaurants(db_path, n):
    c = sqlite3.connect(db_path)
    c.executemany("INSERT INTO restaurants (name, owner_email) VALUES (?, ?)",
                  [(f"R{i}", f"r{i}@x.com") for i in range(n)])
    c.commit(); c.close()


def test_hydrating_every_restaurant_calls_row_keys_a_bounded_number_of_times_per_row(monkeypatch, db_path):
    _insert_restaurants(db_path, 200)
    counter = {"n": 0}

    class CountingRow(sqlite3.Row):
        def keys(self):
            counter["n"] += 1
            return super().keys()

    def conn_with_counting_rows(*a, **k):
        c = sqlite3.connect(db_path)
        c.row_factory = CountingRow
        return c
    monkeypatch.setattr(models, "get_conn", conn_with_counting_rows)
    out = models.get_all_restaurants(db_path)
    assert len(out) == 200
    assert counter["n"] <= 2 * 200, counter["n"]


def test_hydrating_two_thousand_restaurants_returns_every_one(db_path):
    _insert_restaurants(db_path, 2000)
    out = models.get_all_restaurants(db_path)
    assert len(out) == 2000


# ── MOD-PERF-3: claim bookkeeping off the per-call path ─────────────────────

def _traced(monkeypatch, db_path):
    statements = []
    real = sqlite3.connect

    def get_conn(*a, **k):
        c = real(db_path, timeout=30)
        c.row_factory = sqlite3.Row
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(models, "get_conn", get_conn)
    return statements


def test_a_period_is_claimed_once(monkeypatch, db_path):
    _traced(monkeypatch, db_path)
    assert ops.claim_period("edge_job", "2026-09-22") is True
    assert ops.claim_period("edge_job", "2026-09-22") is False


def test_claiming_a_period_issues_no_delete(monkeypatch, db_path):
    statements = _traced(monkeypatch, db_path)
    ops.claim_period("edge_job", "2026-09-22")
    assert not [s for s in statements if s.strip().upper().startswith("DELETE")], statements


def test_claiming_a_period_runs_no_schema_ddl(monkeypatch, db_path):
    statements = _traced(monkeypatch, db_path)
    ops.claim_period("edge_job", "2026-09-22")
    assert not [s for s in statements if "CREATE TABLE" in s.upper()], statements


def test_the_claim_prune_uses_an_index(monkeypatch, db_path):
    _traced(monkeypatch, db_path)
    ops.claim_period("edge_job", "2026-09-22")  # the table exists after a claim, whoever creates it
    c = sqlite3.connect(db_path)
    plan = [r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN DELETE FROM job_period_claims WHERE claimed_at < datetime('now','-45 days')")]
    c.close()
    assert not [p for p in plan if p.startswith("SCAN")], plan


# ── MOD-PERF-4: one run_due pass drains every due action ────────────────────

def test_one_run_due_pass_executes_every_due_action(monkeypatch, redirect):
    import delayed
    ran = []
    monkeypatch.setitem(delayed.HANDLERS, "edge_noop", lambda rid, payload, db: ran.append(payload) or {"ok": True})
    rid = models.create_restaurant(models.Restaurant(name="Queue Co", owner_email="q@x.com"), db_path=redirect)
    for i in range(50):
        delayed.schedule(rid, "edge_noop", {"i": i}, 1, db_path=redirect)
    out = delayed.run_due(db_path=redirect, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert out["ran"] == 50 and len(ran) == 50


def test_run_due_executes_due_actions_oldest_first_and_only_once(monkeypatch, redirect):
    import delayed
    ran = []
    monkeypatch.setitem(delayed.HANDLERS, "edge_noop", lambda rid, payload, db: ran.append(payload["i"]) or {"ok": True})
    rid = models.create_restaurant(models.Restaurant(name="Queue Co", owner_email="q@x.com"), db_path=redirect)
    for i in range(5):
        delayed.schedule(rid, "edge_noop", {"i": i}, 1, db_path=redirect)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert delayed.run_due(db_path=redirect, now=later)["ran"] == 5
    assert delayed.run_due(db_path=redirect, now=later)["ran"] == 0
    assert sorted(ran) == [0, 1, 2, 3, 4]


# ── MOD-PERF-5: a malformed row must not vanish silently ────────────────────

def test_a_restaurant_with_a_malformed_numeric_column_is_kept_or_reported(monkeypatch, redirect):
    good = models.create_restaurant(models.Restaurant(name="Good", owner_email="g@x.com"), db_path=redirect)
    bad = models.create_restaurant(models.Restaurant(name="Bad", owner_email="b@x.com"), db_path=redirect)
    c = sqlite3.connect(redirect)
    c.execute("UPDATE restaurants SET monthly_revenue_target='abc' WHERE id=?", (bad,))
    c.commit(); c.close()
    ids = [r.id for r in models.get_all_restaurants(redirect)]
    assert good in ids
    if bad not in ids:
        c = sqlite3.connect(redirect)
        try:
            n = c.execute("SELECT COUNT(*) FROM job_failures WHERE context LIKE ?", (f"%{bad}%",)).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        c.close()
        assert n >= 1, "the malformed restaurant disappeared with no job_failures row"


# ── MOD-PERF-6: per-restaurant hot queries use an index ─────────────────────

_HOT_QUERIES = {
    "ai_visibility_runs": "SELECT ai_score, answered, appeared, created_at FROM ai_visibility_runs "
                          "WHERE restaurant_id=? AND ai_score IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 2",
    "review_requests": "SELECT COUNT(*) FROM review_requests WHERE restaurant_id=?",
    "marketing_attribution": "SELECT * FROM marketing_attribution WHERE restaurant_id=? AND lift_pct IS NOT NULL",
    "weekly_reports": "SELECT * FROM weekly_reports WHERE restaurant_id=? ORDER BY id DESC LIMIT 1",
}


@pytest.mark.parametrize("table", sorted(_HOT_QUERIES))
def test_a_per_restaurant_hot_query_does_not_scan_the_whole_table(db_path, table):
    c = sqlite3.connect(db_path)
    plan = [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + _HOT_QUERIES[table], (1,))]
    c.close()
    assert not [p for p in plan if p.startswith("SCAN " + table)], plan


# ── MOD-PERF-7: the emailed backup has a size ceiling ───────────────────────

def test_the_emailed_backup_is_skipped_and_recorded_above_a_configured_size(monkeypatch, redirect, tmp_path):
    import scheduler
    import resend
    from cryptography.fernet import Fernet
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("BACKUP_EMAIL_MAX_BYTES", "1")
    monkeypatch.setattr(scheduler, "BACKUP_EMAIL_MAX_BYTES", 1, raising=False)
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test")
    sent, captured = [], []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: sent.append(payload)), raising=False)
    monkeypatch.setattr(scheduler._ops, "capture", lambda exc, job="", context="", **k: captured.append((job, str(exc))))
    scheduler.backup_db()
    assert sent == []
    assert any(job == "backup_db" for job, _ in captured)


def test_the_local_backup_snapshot_is_written_without_an_encryption_key(monkeypatch, redirect, tmp_path):
    import os
    import scheduler
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    scheduler.backup_db()
    files = os.listdir(tmp_path / "backups")
    assert any(f.startswith("cavnar_ai_backup_") and f.endswith(".db") for f in files), files
