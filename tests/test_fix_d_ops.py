"""Fix round D — ops bookkeeping: failures by restaurant (#67), honest run
outcomes and orphaned runs (#150, #39), the claim under a held write lock
(#111), permanent markers (#157), one retention registry (#72), and the
lease across a dead holder and a shutdown (#134, #161)."""
import sqlite3
import threading
import time

import pytest

import models
import ops
import strategy_jobs  # noqa: F401 — binds get_conn at import: bind it here, at collection


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    yield
    ops._claim_fallback.clear()


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


# ── #67: job failures carry the restaurant and a kind ──────────────────────

@pytest.mark.parametrize("context,expected", [
    ("restaurant_id=5 Gia Mia", 5), ("rid=5", 5), ("restaurant_id=50", 50),
    ("grid=5", None), ("nothing here", None), ("restaurant_id=7 business_date=2026-09-01", 7),
])
def test_capture_reads_the_restaurant_from_the_context(db_path, context, expected):
    ops.capture(RuntimeError("x"), job="t", context=context)
    row = _q(db_path, "SELECT restaurant_id, kind FROM job_failures ORDER BY id DESC LIMIT 1")[0]
    assert row["restaurant_id"] == expected and row["kind"] == "job"


def test_capture_takes_an_explicit_restaurant_and_kind(db_path):
    ops.capture(RuntimeError("x"), job="t", context="rid=9", restaurant_id=3, kind="ai_quality")
    ops.capture(RuntimeError("y"), job="t", kind="not-a-kind")
    rows = _q(db_path, "SELECT restaurant_id, kind FROM job_failures ORDER BY id")
    assert rows[-2] == {"restaurant_id": 3, "kind": "ai_quality"} and rows[-1]["kind"] == "job"


def test_rid_5_is_not_rid_50(db_path):
    ops.capture(RuntimeError("fifty"), job="t", context="restaurant_id=50")
    ops.capture(RuntimeError("five"), job="t", context="restaurant_id=5")
    rows = _q(db_path, "SELECT error FROM job_failures WHERE restaurant_id=5")
    assert [r["error"] for r in rows] == ["five"]


def test_job_runs_carry_the_restaurant(db_path):
    ops.run_job("one_restaurant", lambda: None, context="restaurant_id=12 manual by will")
    assert _q(db_path, "SELECT restaurant_id FROM job_runs WHERE job='one_restaurant'")[0]["restaurant_id"] == 12


# ── #150 / #39: what a run records ─────────────────────────────────────────

@pytest.mark.parametrize("result,state", [
    ({"sent": 99, "failed": 1}, ops.RUN_PARTIAL),          # was FAILED
    ({"diagnosed": 0, "skipped": 3, "failed": 2}, ops.RUN_FAILED),
    ({"analysed": 2, "failed": 1}, ops.RUN_PARTIAL),
    ({"attempted": 3, "ok": 0, "failed": 3}, ops.RUN_FAILED),
    ({"drafted": 4, "complete": False}, ops.RUN_PARTIAL),   # the bound, in its own words
    ({"attempted": 0, "ok": 0, "failed": 0, "skipped": 5, "hit_bound": False}, ops.RUN_OK),
    (None, ops.RUN_OK),
])
def test_run_outcome(result, state):
    assert ops.run_outcome(result)[0] == state


def test_a_run_that_captured_a_failure_under_its_name_is_partial(db_path):
    def job():
        ops.capture(RuntimeError("one restaurant broke"), job="noisy_job", context="restaurant_id=1")
        return None
    ops.run_job("noisy_job", job)
    row = _q(db_path, "SELECT ok, error FROM job_runs WHERE job='noisy_job'")[0]
    assert row["ok"] == ops.RUN_PARTIAL and "captured" in row["error"]


def test_a_live_run_is_pulsed(db_path, monkeypatch):
    monkeypatch.setattr(ops, "RUN_PULSE_SECONDS", 0.05)
    seen = {}

    def slow():
        time.sleep(0.25)
        seen["row"] = _q(db_path, "SELECT owner, pulse_at, started_at FROM job_runs WHERE job='slow_job'")[0]
    ops.run_job("slow_job", slow)
    assert seen["row"]["owner"] == ops._LEASE_OWNER and seen["row"]["pulse_at"]


def test_orphaned_runs_are_closed_but_a_live_one_is_not(db_path):
    _x(db_path, "INSERT INTO job_runs (job, started_at, owner, pulse_at) VALUES "
                "('dead_job', datetime('now','-3 hours'), 'gone-proc', datetime('now','-2 hours'))")
    _x(db_path, "INSERT INTO job_runs (job, started_at, owner, pulse_at) VALUES "
                "('alive_job', datetime('now','-3 hours'), 'other-proc', datetime('now','-1 minutes'))")
    _x(db_path, "INSERT INTO job_runs (job, started_at) VALUES ('legacy_job', datetime('now','-3 hours'))")
    assert ops.close_orphaned_runs() == 2
    rows = {r["job"]: r for r in _q(db_path, "SELECT job, finished_at, ok, error FROM job_runs")}
    assert rows["dead_job"]["ok"] == 0 and "interrupted" in rows["dead_job"]["error"]
    assert rows["legacy_job"]["finished_at"]
    assert rows["alive_job"]["finished_at"] is None, "a run another process is still pulsing is left alone"


def test_a_reclaim_closes_the_dead_run_and_a_pulsing_run_is_not_reclaimed(db_path):
    assert ops.claim_period("daily_alerts", "2026-09-29-10") is True
    _x(db_path, "UPDATE job_period_claims SET claimed_at=datetime('now','-3 hours') WHERE job_key='daily_alerts:2026-09-29-10'")
    _x(db_path, "INSERT INTO job_runs (job, started_at, owner, pulse_at) VALUES "
                "('daily_alerts', datetime('now','-179 minutes'), 'p2', datetime('now'))")
    assert ops.claim_period("daily_alerts", "2026-09-29-10") is False, "the other process is still running it"
    _x(db_path, "UPDATE job_runs SET pulse_at=datetime('now','-2 hours') WHERE job='daily_alerts'")
    assert ops.claim_period("daily_alerts", "2026-09-29-10") is True
    assert _q(db_path, "SELECT ok FROM job_runs WHERE job='daily_alerts'")[0]["ok"] == 0


def test_stuck_is_each_jobs_own_bound():
    import jobs_registry
    assert jobs_registry.max_minutes("review_fetch") > 180 > jobs_registry.max_minutes("dsr_delivery")


def test_stuck_jobs_uses_the_bound(db_path):
    _x(db_path, "INSERT INTO job_runs (job, started_at) VALUES ('review_fetch', datetime('now','-100 minutes'))")
    _x(db_path, "INSERT INTO job_runs (job, started_at) VALUES ('dsr_delivery', datetime('now','-100 minutes'))")
    stuck = {j["job"] for j in ops.stuck_jobs()}
    assert stuck == {"dsr_delivery"}, "a 100-minute review fetch is inside its 3-hour bound"


# ── #111: a locked database never re-runs a claimed period ─────────────────

def test_a_claimed_period_is_refused_while_another_connection_holds_the_write_lock(db_path, monkeypatch):
    assert ops.claim_period("morning_brief:1", "2026-09-29") is True
    holder = sqlite3.connect(db_path, timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    try:
        real = sqlite3.connect

        def fast(*a, **k):
            c = real(db_path, timeout=0.1)
            c.row_factory = sqlite3.Row
            return c
        monkeypatch.setattr(models, "get_conn", lambda *a, **k: fast())
        # Read-first: an already-claimed period never even tries to write.
        assert ops.claim_period("morning_brief:1", "2026-09-29") is False
        # A NEW period under the lock: the write fails, the table does not
        # hold it, so the process memory runs it once — then refuses.
        assert ops.claim_period("morning_brief:1", "2026-09-30") is True
        assert ops.claim_period("morning_brief:1", "2026-09-30") is False
    finally:
        holder.rollback()
        holder.close()


def test_the_insert_race_under_a_lock_reads_before_allowing(db_path, monkeypatch):
    """The row appears between the read and the write (another process
    claimed it) and the write then fails on the lock: refused, not run."""
    assert ops.claim_period("digest", "2026-09-29") is True
    calls = {"n": 0}
    real_claimed = ops._claimed_on_read

    def racing(conn, key):
        calls["n"] += 1
        return False if calls["n"] == 1 else real_claimed(conn, key)
    monkeypatch.setattr(ops, "_claimed_on_read", racing)

    class _Locked:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a):
            if sql.lstrip().upper().startswith("INSERT INTO JOB_PERIOD_CLAIMS"):
                raise sqlite3.OperationalError("database is locked")
            return self._c.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self._c, n)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: _Locked(real(db_path)))
    assert ops.claim_period("digest", "2026-09-29") is False


# ── #157: permanent markers ─────────────────────────────────────────────────

def test_a_marker_is_once_ever_and_survives_the_claims_prune(db_path):
    assert ops.claim_marker("competitor_move_told:1:place", "drop:4.2") is True
    assert ops.claim_marker("competitor_move_told:1:place", "drop:4.2") is False
    _x(db_path, "UPDATE ops_markers SET created_at=datetime('now','-400 days')")
    ops.prune_ledgers(db_path)
    assert ops.claim_marker("competitor_move_told:1:place", "drop:4.2") is False
    assert ops.marker_created_at("competitor_move_told:1:place", "drop:4.2")
    ops.release_marker("competitor_move_told:1:place", "drop:4.2")
    assert ops.claim_marker("competitor_move_told:1:place", "drop:4.2") is True


def test_existing_marker_claims_are_carried_over_at_boot(db_path):
    _x(db_path, "INSERT INTO job_period_claims (job_key, claimed_at) VALUES "
                "('rating_unreadable:4:7/1/26', '2026-07-02 10:00:00')")
    _x(db_path, "INSERT INTO job_period_claims (job_key) VALUES ('quality_calibration:5:2026-09-21')")
    _x(db_path, "INSERT INTO job_period_claims (job_key) VALUES ('quality_calibration:2026-09-27')")
    ops.init_ops(db_path)
    keys = {r["key"]: r["created_at"] for r in _q(db_path, "SELECT key, created_at FROM ops_markers")}
    assert keys.get("rating_unreadable:4:7/1/26") == "2026-07-02 10:00:00"
    assert "quality_calibration:5:2026-09-21" in keys
    assert "quality_calibration:2026-09-27" not in keys, "the loop's daily claim is not a marker"
    assert ops.claim_marker("rating_unreadable:4", "7/1/26") is False


def test_the_marker_call_sites_use_ops_markers():
    import inspect, notify, strategy_jobs
    assert 'claim_marker(f"rating_unreadable:{rid}"' in inspect.getsource(notify._rating_unreadable)
    assert 'claim_marker(f"competitor_move_told:' in inspect.getsource(notify)
    assert "claim_marker(job, \"first_sync\")" in inspect.getsource(notify._first_seen_hours)
    assert 'claim_marker("quality_calibration"' in inspect.getsource(strategy_jobs.run_quality_calibration)


# ── #72 / #81: one retention registry ──────────────────────────────────────

def test_models_log_retention_is_a_view_of_the_one_registry():
    for table, (days, column) in models._LOG_RETENTION_DAYS.items():
        assert ops._RETENTION_DAYS[table] == days and ops._RETENTION_COLUMN[table] == column
    assert ops._RETENTION_DAYS["job_runs"] == 45 and ops._RETENTION_DAYS["push_deliveries"] == 30


def test_the_new_tables_are_registered():
    for t in ("admin_events", "data_health_daily", "stripe_events_seen", "sessions", "rec_events",
              "operator_alerts", "backup_runs", "missed_windows", "job_run_requests"):
        assert t in ops._RETENTION_DAYS and t in ops._RETENTION_COLUMN, t
    assert ops._RETENTION_DAYS["rec_events"] > 730, "the calibration window reads 730 days back"


def test_the_prune_is_chunked_and_bounded_and_returns_counts(db_path, monkeypatch):
    monkeypatch.setattr(ops, "RETENTION_CHUNK_ROWS", 3)
    for i in range(10):
        _x(db_path, "INSERT INTO job_failures (job, created_at) VALUES ('old', datetime('now','-400 days'))")
    _x(db_path, "INSERT INTO job_failures (job) VALUES ('new')")
    out = ops.prune_ledgers(db_path)
    assert out["job_failures"] == 10 and out["failed"] == 0 and out["attempted"] >= 6
    assert [r["job"] for r in _q(db_path, "SELECT job FROM job_failures")] == ["new"]
    monkeypatch.setattr(ops, "RETENTION_MAX_SECONDS", -1)
    assert ops.prune_ledgers(db_path)["hit_bound"] is True


def test_the_prune_refreshes_planner_statistics(db_path):
    ops.prune_ledgers(db_path)
    assert _q(db_path, "SELECT name FROM sqlite_master WHERE name='sqlite_stat1'")


def test_old_superseded_drafts_go_and_published_weeks_stay(db_path):
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="Sched Co", owner_email="s@x.test"), db_path=db_path)
    c = models.get_conn(db_path)
    try:
        models._ensure_history_columns(c)
        c.commit()
    finally:
        c.close()
    _x(db_path, "INSERT INTO schedule_history (id, restaurant_id, generated_at, week_start, superseded_by) "
                "VALUES (1, ?, datetime('now','-500 days'), '2025-05-01', 2)", (rid,))
    _x(db_path, "INSERT INTO schedule_history (id, restaurant_id, generated_at, week_start, published_at) "
                "VALUES (2, ?, datetime('now','-500 days'), '2025-05-01', datetime('now','-500 days'))", (rid,))
    ops.prune_ledgers(db_path)
    ids = [r["id"] for r in _q(db_path, "SELECT id FROM schedule_history")]
    assert ids == [2]


# ── #134 / #161: the lease ──────────────────────────────────────────────────

def test_a_kept_lease_is_taken_over_within_minutes_of_its_holder_going_silent(db_path):
    ops._lease_kept.set()
    try:
        assert ops.acquire_scheduler_lease() is True
    finally:
        ops._lease_kept.clear()
    assert ops.scheduler_lease_holder()["kept"] == 1
    assert ops.acquire_scheduler_lease("standby") is False
    _x(db_path, f"UPDATE scheduler_lease SET heartbeat_at=datetime('now','-{ops.LEASE_OWNER_GONE_SECONDS + 30} seconds')")
    assert ops.acquire_scheduler_lease("standby") is True, "a dead keeper's lease idled the full 30 minutes"


def test_an_unkept_lease_keeps_the_long_window(db_path):
    assert ops.acquire_scheduler_lease("old-style") is True
    _x(db_path, f"UPDATE scheduler_lease SET heartbeat_at=datetime('now','-{ops.LEASE_OWNER_GONE_SECONDS + 30} seconds')")
    assert ops.acquire_scheduler_lease("standby") is False


def test_shutdown_releases_and_blocks_re_acquisition(db_path, monkeypatch):
    monkeypatch.setattr(ops, "_shutting_down", threading.Event())
    assert ops.acquire_scheduler_lease() is True
    assert ops.shutdown_scheduler() is True
    assert ops.scheduler_lease_holder()["owner"] is None
    assert ops.acquire_scheduler_lease() is False, "a daemon thread took the lease back after release"
    assert ops.renew_scheduler_lease() is False
    # The replacement is another process, not shutting down: it takes it at once.
    monkeypatch.setattr(ops, "_shutting_down", threading.Event())
    assert ops.acquire_scheduler_lease("the-replacement") is True
