"""Job claims, the scheduler lease and ledger pruning — the bookkeeping every
scheduled job stands on (DATA audit: DATA-4, DATA-6, DATA-20, DATA-22,
DATA-40, DATA-45).

What this protects: `ops.claim_period` is the once-per-period gate for every
digest, brief and alert pass, and `ops.acquire_scheduler_lease` is what keeps
two processes from running the same tick. Both have to hold up under the
failures they exist for — a database that refuses writes, a pass that runs
longer than the lease, a process killed mid-job — and neither may cost a
full-table scan per call once the platform is large.

Tests without a marker pin behaviour that works today. Tests marked
xfail(strict=True) assert the CORRECT behaviour for a defect the DATA audit
confirmed; they flip to a failure the day it is fixed, so the marker goes
with the fix.
"""
import threading
from datetime import datetime

import pytest

import models
import ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """ops imports get_conn lazily from models, so patching models is enough."""
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    yield
    ops._claim_fallback.clear()


def _write_refusing(monkeypatch):
    """Connections open fine but every write fails — stands in for a full or
    read-only volume, or a lock held past the busy timeout."""
    real = models.get_conn

    def refuses(*a, **k):
        conn = real()
        conn.execute("PRAGMA query_only=ON")
        return conn
    monkeypatch.setattr(models, "get_conn", refuses)


def _plan(conn, sql):
    return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())


# ── claim_period on write failure (DATA-22) ────────────────────────────────

def test_a_healthy_claim_is_granted_once_per_job_and_period(db_path):
    assert ops.claim_period("daily_alerts", "2026-09-22") is True
    assert ops.claim_period("daily_alerts", "2026-09-22") is False
    assert ops.claim_period("daily_alerts", "2026-09-23") is True


def test_a_claim_that_cannot_be_written_is_logged_and_memoised(db_path, monkeypatch):
    errors = []
    monkeypatch.setattr(ops.log, "error", lambda msg, *a, **k: errors.append(str(msg)))
    ops.claim_period("yesterday_job", "2026-09-21")      # the table exists, as it does in production
    _write_refusing(monkeypatch)
    ticks = [ops.claim_period("backup_db", "2026-09-22") for _ in range(3)]
    assert ticks == [True, False, False], "a write outage must run the job once, not never and not every tick"
    assert any("backup_db:2026-09-22" in e for e in errors), "the refused claim was not logged"


def test_a_period_run_during_an_outage_is_not_run_again_when_the_database_returns(db_path, monkeypatch):
    real = models.get_conn
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            import sqlite3
            raise sqlite3.OperationalError("unable to open database file")
        return real(*a, **k)
    monkeypatch.setattr(models, "get_conn", flaky)
    assert ops.claim_period("weekly_digests", "2026-09-22") is True      # outage: runs once from memory
    assert ops.claim_period("weekly_digests", "2026-09-22") is False, \
        "the digest already went out from the fallback; the recovered database re-sent it"


# ── the fallback memo (DATA-45) ────────────────────────────────────────────

def test_the_fallback_evicts_the_oldest_key(monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    extra = 10
    for i in range(ops._CLAIM_FALLBACK_MAX + extra):
        ops.claim_period("job", f"p{i:04d}")
    kept = ops._claim_fallback
    oldest = {f"job:p{i:04d}" for i in range(extra)}
    newest = {f"job:p{i:04d}" for i in range(extra, ops._CLAIM_FALLBACK_MAX + extra)}
    assert not (kept & oldest), f"oldest keys survived: {sorted(kept & oldest)}"
    assert newest <= kept, "a recent period was evicted — that job's guard re-opens mid-outage"


def test_the_fallback_never_exceeds_its_ceiling(monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    for i in range(ops._CLAIM_FALLBACK_MAX + 25):
        ops.claim_period("job", f"q{i}")
    assert len(ops._claim_fallback) <= ops._CLAIM_FALLBACK_MAX


# ── claim cost at scale (DATA-6) ───────────────────────────────────────────

def test_claim_period_does_not_scan_the_claims_table(db_path, monkeypatch):
    import sqlite3
    real = models.get_conn
    statements = []

    def traced(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(models, "get_conn", traced)
    ops.claim_period("morning_brief:1", "2026-09-22")

    deletes = [s for s in statements if s.lstrip().upper().startswith("DELETE") and "job_period_claims" in s]
    conn = sqlite3.connect(db_path)
    try:
        for sql in deletes:
            plan = _plan(conn, sql)
            assert "SCAN job_period_claims" not in plan, f"{sql!r} scans the whole table: {plan}"
    finally:
        conn.close()


# ── retention deletes (DATA-40) ────────────────────────────────────────────

def _ensure_ledger_tables(db_path):
    import auth, push, webhooks, ai_utils
    for init in (auth.init_auth, push.init_push, webhooks.init_webhooks):
        try:
            init(db_path)
        except TypeError:
            init()
    ops.capture(RuntimeError("seed job_failures"), job="edge-test")  # creates job_failures
    ops._record_run_start("edge-test")                                  # creates job_runs
    conn = models.get_conn(db_path)
    ai_utils._ensure_usage_schema(conn, db_path)                        # creates ai_usage
    conn.close()


def test_every_retention_delete_uses_an_index(db_path):
    import sqlite3
    _ensure_ledger_tables(db_path)
    conn = sqlite3.connect(db_path)
    try:
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checked, scans = [], []
        for table, days in ops._RETENTION_DAYS.items():
            if table not in present:
                continue
            col = ops._RETENTION_COLUMN.get(table, "created_at")
            plan = _plan(conn, f"DELETE FROM {table} WHERE {col} < datetime('now', '-{days} days')")
            checked.append(table)
            if f"SCAN {table}" in plan:
                scans.append(f"{table}.{col}: {plan}")
    finally:
        conn.close()
    assert len(checked) >= 6, f"too few ledger tables exist to check: {checked}"
    assert not scans, "retention deletes that scan the whole table:\n" + "\n".join(scans)


# ── the lease across a long pass (DATA-4) ──────────────────────────────────

class _StopLoop(Exception):
    pass


def _one_tick(monkeypatch, fetch):
    """Drive exactly one real scheduler_loop tick in which only the review
    fetch is due; `fetch` stands in for run_daily_fetch."""
    import scheduler, marketing_publish, notify, issues, morning_brief, delayed
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 22, 9, 0))   # a Tuesday
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda job, period: job == "review_fetch")
    monkeypatch.setattr(scheduler, "run_daily_fetch", fetch)
    monkeypatch.setattr(notify, "release_due_alerts", lambda *a, **k: {})
    monkeypatch.setattr(issues, "tick", lambda *a, **k: None)
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: None)
    monkeypatch.setattr(delayed, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None, raising=False)

    def _stop(_s):
        raise _StopLoop()
    monkeypatch.setattr(scheduler.time, "sleep", _stop)
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()


def test_a_standby_process_cannot_take_a_freshly_renewed_lease(db_path, monkeypatch):
    """The part that works: the tick renews the lease before its jobs run."""
    seen = {}

    def fetch():
        seen["contender"] = ops.acquire_scheduler_lease("standby-worker")
    _one_tick(monkeypatch, fetch)
    assert seen["contender"] is False
    assert ops.scheduler_lease_holder()["owner"] == ops._LEASE_OWNER


@pytest.mark.xfail(strict=True, reason="DATA-4: the lease is renewed only at tick start, so a pass longer than "
                                       "SCHEDULER_LEASE_STALE_SECONDS loses it and a standby runs the same tick")
def test_a_long_pass_keeps_its_lease(db_path, monkeypatch):
    """The stale window is shrunk to one second and the 'fetch' runs its
    items through the real bounded_map for a little over two, standing in
    for a three-hour pass against the thirty-minute production window."""
    import scheduler
    monkeypatch.setattr(ops, "SCHEDULER_LEASE_STALE_SECONDS", 1)
    seen = {}
    idle = threading.Event()

    def long_fetch():
        scheduler.bounded_map(list(range(22)), lambda _i: idle.wait(0.1), 1, 3600)
        seen["contender"] = ops.acquire_scheduler_lease("standby-worker")
    _one_tick(monkeypatch, long_fetch)
    assert seen["contender"] is False, "a standby took the lease while the holder was still mid-pass"


# ── a claim whose run never finished (DATA-20) ─────────────────────────────

def _backdate_claim(db_path, key, expr):
    conn = models.get_conn(db_path)
    conn.execute(f"UPDATE job_period_claims SET claimed_at=datetime('now', '{expr}') WHERE job_key=?", (key,))
    conn.commit()
    conn.close()


def _run_row(db_path, job, finished):
    conn = models.get_conn(db_path)
    conn.execute(ops._RUNS_SQL)
    conn.execute("INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES (?, datetime('now','-3 hours'), ?, ?)",
                 (job, "2026-09-22 07:00:00" if finished else None, 1 if finished else None))
    conn.commit()
    conn.close()


def test_a_claim_whose_run_finished_is_not_reclaimed(db_path):
    assert ops.claim_period("daily_alerts", "2026-09-22-10") is True
    _run_row(db_path, "daily_alerts", finished=True)
    _backdate_claim(db_path, "daily_alerts:2026-09-22-10", "-3 hours")
    assert ops.claim_period("daily_alerts", "2026-09-22-10") is False


def test_a_claim_whose_run_never_finished_can_be_reclaimed_after_its_timeout(db_path):
    """A deploy SIGKILLs the process mid-run: the claim row stands, job_runs
    has a start and no finish. Hours later the job must be allowed to run."""
    assert ops.claim_period("daily_alerts", "2026-09-22-10") is True
    _run_row(db_path, "daily_alerts", finished=False)
    _backdate_claim(db_path, "daily_alerts:2026-09-22-10", "-3 hours")
    assert ops.claim_period("daily_alerts", "2026-09-22-10") is True
