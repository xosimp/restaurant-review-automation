"""Blind re-audit 10/20/26, refix G1 — infrastructure, scheduler, job queue,
nightly jobs: R1-02 … R1-14, R9-13 (= R1-05) and R9-18."""
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


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
    import job_queue
    import ops
    job_queue.reset_for_tests()
    ops._claim_fallback.clear()
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    return db_path


def _q(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _x(sql, args=()):
    conn = models.get_conn()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _restaurant(name="G1 Grill", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    if cols:
        _x("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    return rid


# ── R1-05 / R9-13: a lost queue lease stops the handler ────────────────────

@pytest.fixture
def kind(db, monkeypatch):
    """A test kind in the bulk group whose handler the test supplies."""
    import job_queue
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    monkeypatch.setitem(job_queue.KINDS, "g1_test", ("bulk", "bulk", 3))
    monkeypatch.setitem(job_queue.HANDLERS, "g1_test", "g1:handler")
    handler = {}
    real_resolve = job_queue._resolve
    monkeypatch.setattr(job_queue, "_resolve", lambda t: handler["fn"] if t == "g1:handler" else real_resolve(t))
    return handler


def test_a_row_whose_lease_another_consumer_took_stops_at_its_next_boundary(kind):
    import job_queue
    qid, _ = job_queue.enqueue("g1_test", {"n": 1})
    job = job_queue.claim("me", groups=("bulk",))
    reached = []

    def handler(j):
        job_queue.checkpoint("one")
        reached.append("one")
        # Another consumer resumed the row (its lease had run out).
        _x("UPDATE jobs SET lease_owner='other' WHERE id=?", (qid,))
        job_queue.ensure_lease()
        reached.append("sent")              # never: ensure_lease raised
        return {"ok": 1}
    kind["fn"] = handler
    assert job_queue.run_one(job) is None
    assert reached == ["one"], "the copy that lost its lease went on to send"
    row = _q("SELECT state, lease_owner, attempts FROM jobs WHERE id=?", (qid,))[0]
    assert row["state"] == "running" and row["lease_owner"] == "other", "the loser must not fail or finish the row"


def test_the_lease_keeper_marks_a_row_lost_and_checkpoint_and_model_wait_stop_it(kind, monkeypatch):
    import job_queue
    monkeypatch.setattr(job_queue, "RENEW_SECONDS", 0.05)
    qid, _ = job_queue.enqueue("g1_test", {})
    job = job_queue.claim("me", groups=("bulk",))
    seen = {}

    def handler(j):
        _x("UPDATE jobs SET lease_owner='other' WHERE id=?", (qid,))
        deadline = time.time() + 5
        while not job_queue.lease_lost() and time.time() < deadline:
            time.sleep(0.02)
        seen["lost"] = job_queue.lease_lost()
        try:
            with job_queue.model_wait():
                seen["paid"] = True
        except job_queue.LeaseLost:
            seen["model_refused"] = True
        job_queue.checkpoint("after")
        seen["went_on"] = True
    kind["fn"] = handler
    job_queue.run_one(job)
    assert seen == {"lost": True, "model_refused": True}


def test_renewals_failing_for_a_whole_lease_lose_the_row(kind, monkeypatch):
    import job_queue
    monkeypatch.setattr(job_queue, "RENEW_SECONDS", 0.02)
    monkeypatch.setattr(job_queue, "LEASE_SECONDS", 0.1)
    job_queue.enqueue("g1_test", {})
    job = job_queue.claim("me", groups=("bulk",))
    monkeypatch.setattr(job_queue, "renew", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database down")))
    keeper = job_queue._LeaseKeeper(job)
    keeper.start()
    keeper.join(3)
    assert keeper.lost and job_queue.lease_lost(job)


def test_this_process_never_resumes_a_row_it_is_still_running(kind):
    import job_queue
    qid, _ = job_queue.enqueue("g1_test", {})
    job = job_queue.claim("me", groups=("bulk",))
    with job_queue._held_lock:
        job_queue._held[int(qid)] = job
    try:
        _x("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 5, qid))
        assert job_queue.claim("me", groups=("bulk",)) is None, "its own still-running row was claimed again"
    finally:
        with job_queue._held_lock:
            job_queue._held.pop(int(qid), None)
    again = job_queue.claim("me", groups=("bulk",))
    assert again is not None and again["id"] == qid, "once its handler stopped, the dead lease is resumed"


def test_shutdown_marks_each_row_lost_before_handing_it_back(kind):
    import job_queue
    qid, _ = job_queue.enqueue("g1_test", {})
    job = job_queue.claim("me", groups=("bulk",))
    with job_queue._held_lock:
        job_queue._held[int(qid)] = job
    try:
        assert job_queue.shutdown() == 1
        assert job_queue.lease_lost(job), "the handler still running would go on beside the next process"
    finally:
        with job_queue._held_lock:
            job_queue._held.pop(int(qid), None)
        job_queue.reset_for_tests()


# ── R1-08: notices off the bulk slots; a learner row never expires silently ─

def test_time_sensitive_rows_are_never_behind_the_learners():
    import job_queue
    for k in ("schedule_notices", "auto_publish", "silent_push"):
        assert job_queue.KINDS[k][0] == "notify", k
    assert "notify" in job_queue.GROUPS and job_queue.group_concurrency("notify") == 2
    assert job_queue.KINDS["email_intake"][:2] == ("bulk", "scheduled")
    assert job_queue.EXPIRE_HOOKS["learner"] == "strategy_jobs:on_learner_expired"


def test_an_expired_learner_row_is_a_missed_window(db):
    import ops
    import strategy_jobs
    rid = _restaurant()
    _x("UPDATE restaurants SET created_at='2026-01-01 00:00:00' WHERE id=?", (rid,))
    strategy_jobs.on_learner_expired(type("J", (), {"payload": {"learner": "schedule_outcomes", "restaurant_id": rid,
                                                               "day": "2026-10-20"}})())
    rows = _q("SELECT job, restaurant_id, local_date FROM missed_windows")
    assert rows == [{"job": "learner:schedule_outcomes", "restaurant_id": rid, "local_date": "2026-10-20"}]
    assert ops is not None


def test_the_food_cost_diagnoses_go_ahead_of_the_bulk_learners(db, monkeypatch):
    import job_queue
    import strategy_jobs
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    rid = _restaurant()
    strategy_jobs.fan_out("food_cost_diagnosis", [rid], "2026-10-20", priority_class="scheduled")
    strategy_jobs.fan_out("schedule_outcomes", [rid], "2026-10-20") if "schedule_outcomes" in strategy_jobs.LEARNERS \
        else None
    row = _q("SELECT priority_class FROM jobs WHERE idempotency_key LIKE 'learner:food_cost_diagnosis:%'")[0]
    assert row["priority_class"] == "scheduled"
    assert strategy_jobs.open_restaurants("food_cost_diagnosis", "2026-10-20") == {rid}


# ── R9-18: the morning batch is the calling thread's ───────────────────────

def test_a_lane_thread_never_collects_into_the_loops_batch(db, monkeypatch):
    import notify
    notify.begin_daily_batch()
    try:
        seen = {}

        def lane():
            seen["batch"] = notify._open_batch()
        t = threading.Thread(target=lane)
        t.start()
        t.join()
        assert seen["batch"] is None, "another thread saw the loop's open batch"
        assert notify._open_batch() == {}
    finally:
        notify.discard_daily_batch()
    assert notify._open_batch() is None


# ── R1-06: Sentry hears one event per job and failure type per window ──────

def test_sentry_is_sampled_per_job_and_failure_type(db, monkeypatch):
    import ops
    import sentry_setup
    sentry_setup.reset_for_tests()
    sent = []
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda e: sent.append(e))
    for rid in range(20):
        ops.capture(RuntimeError("RPOWER down"), job="pos_sync", context=f"restaurant_id={rid}")
    ops.capture(ValueError("other"), job="pos_sync")
    assert len(sent) == 2, "every per-restaurant failure of one outage went to Sentry"
    assert len(_q("SELECT id FROM job_failures WHERE job='pos_sync'")) == 21, "the rows are kept for every one"
    sentry_setup.reset_for_tests()


def test_the_hourly_ceiling_drops_events_past_it(monkeypatch):
    import sentry_setup
    sentry_setup.reset_for_tests()
    monkeypatch.setattr(sentry_setup, "MAX_EVENTS_PER_HOUR", 3)
    got = [sentry_setup.before_send({"exception": {"values": [i]}}) for i in range(5)]
    assert sum(1 for g in got if g is not None) == 3
    logged = [sentry_setup.before_send({"logger": "x", "logentry": {"message": "same"}}) for _ in range(2)]
    assert logged == [None, None], "past the ceiling, and a repeated log line within its window"
    sentry_setup.reset_for_tests()


# ── R1-09: a claim that cannot be checked on Postgres is refused ───────────

def test_a_pool_timeout_refuses_the_claim_instead_of_memoising_a_yes(db, monkeypatch):
    import sqlite3
    import ops
    assert ops.claim_period("weekly_digest", "2026-10-20-9") is True

    def exhausted(*a, **k):
        raise sqlite3.OperationalError("database connection pool exhausted (20 + 10 in use; waited 30s)")
    monkeypatch.setattr(models, "get_conn", exhausted)
    assert ops.claim_period("weekly_digest", "2026-10-20-9") is False
    assert ops.claim_period("ops_digest", "2026-10-20") is False, "an unchecked period is refused, not run"
    assert ops.claim_cooldown("order:1", 10) is False
    assert ops.period_claimed("brief", "2026-10-20") is True
    assert not ops._claim_fallback


def test_on_postgres_every_unchecked_claim_is_refused(db, monkeypatch):
    import db_dialect
    import ops
    monkeypatch.setattr(db_dialect, "backend", lambda: "postgres")
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert ops.claim_period("monthly_summary", "2026-10") is False
    assert ops.claim_marker("competitor_move_told", "x") is False


# ── R1-10: a daily job killed mid-run comes back without the two hours ─────

def _run(job, started_ago_min, pulse_ago_min=None, finished=False, error=None, owner="gone"):
    _x("INSERT INTO job_runs (job, started_at, owner, pulse_at, finished_at, ok, error) VALUES "
       "(?, datetime('now', ?), ?, " + ("datetime('now', ?)" if pulse_ago_min is not None else "?") + ", "
       + ("datetime('now')" if finished else "NULL") + ", ?, ?)",
       (job, f"-{started_ago_min} minutes", owner,
        f"-{pulse_ago_min} minutes" if pulse_ago_min is not None else None,
        0 if finished else None, error))


def test_a_run_whose_pulse_stopped_is_reclaimed_after_minutes_not_hours(db):
    import ops
    assert ops.claim_period("pos_sync", "2026-10-20") is True
    _x("UPDATE job_period_claims SET claimed_at=datetime('now','-15 minutes') WHERE job_key='pos_sync:2026-10-20'")
    _run("pos_sync", 14, pulse_ago_min=12)
    assert ops.claim_period("pos_sync", "2026-10-20") is True, "a deploy at 3:10 re-ran the sync at 5:10"


def test_a_run_closed_as_interrupted_at_boot_is_still_reclaimed(db):
    import ops
    assert ops.claim_period("pos_sync", "2026-10-21") is True
    _x("UPDATE job_period_claims SET claimed_at=datetime('now','-40 minutes') WHERE job_key='pos_sync:2026-10-21'")
    _run("pos_sync", 39, pulse_ago_min=35)
    assert ops.close_orphaned_runs() == 1                 # the new process booted 30 minutes later
    assert ops.claim_period("pos_sync", "2026-10-21") is True, "the job lost its day"


def test_a_job_that_keeps_killing_its_process_is_not_reclaimed_forever(db, monkeypatch):
    import ops
    monkeypatch.setattr(ops, "CLAIM_RECLAIMS_PER_DAY", 2)
    assert ops.claim_period("backup_db", "2026-10-22") is True
    _x("UPDATE job_period_claims SET claimed_at=datetime('now','-30 minutes') WHERE job_key='backup_db:2026-10-22'")
    _run("backup_db", 200, finished=True, error="interrupted — the process running it ended (a deploy or a crash)")
    _run("backup_db", 25, pulse_ago_min=20)
    ops.close_orphaned_runs()
    assert ops.claim_period("backup_db", "2026-10-22") is False


def test_a_pulsing_or_finished_run_is_still_left_alone(db):
    import ops
    assert ops.claim_period("daily_alerts", "2026-10-20-10") is True
    _x("UPDATE job_period_claims SET claimed_at=datetime('now','-30 minutes') "
       "WHERE job_key='daily_alerts:2026-10-20-10'")
    _run("daily_alerts", 29, pulse_ago_min=0)
    assert ops.claim_period("daily_alerts", "2026-10-20-10") is False
    _x("UPDATE job_runs SET finished_at=datetime('now'), ok=1, pulse_at=datetime('now','-20 minutes')")
    assert ops.claim_period("daily_alerts", "2026-10-20-10") is False


def test_a_failed_jobs_retry_survives_a_restart(db, monkeypatch):
    import jobs_registry
    import ops
    import scheduler
    p = scheduler._PulsedOps()
    p._after("pos_sync", "pos_sync", "2026-10-20", jobs_registry.spec("pos_sync"), {"raised": True})
    assert ops.pending_retries() and ops.pending_retries()[0][:3] == ("pos_sync", "2026-10-20", 1)
    fresh = scheduler._PulsedOps()                  # the next process, after a deploy
    fresh.became_runner()
    retry = fresh._retries.get(("pos_sync", "2026-10-20"))
    assert retry and retry["attempt"] == 1 and retry["at"] > time.monotonic()
    assert jobs_registry.RETRY_BACKOFF_MINUTES[0] * 60 - 5 < retry["at"] - time.monotonic() <= \
        jobs_registry.RETRY_BACKOFF_MINUTES[0] * 60
    fresh._claimed["pos_sync"] = "2026-10-20"
    fresh._after("pos_sync", "pos_sync", "2026-10-20", jobs_registry.spec("pos_sync"), {"result": {"ok": 1}})
    assert ops.pending_retries() == []


# ── R1-02: the loop's cadence, and reminders that never go missing ─────────

def test_the_pause_is_what_is_left_of_the_tick(monkeypatch):
    import scheduler
    slept = []
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: slept.append(s))
    scheduler._SWEEP_DONE.clear()
    scheduler._tick_pause(time.monotonic() - 100)
    assert scheduler.SCHEDULER_TICK_SECONDS - 101 <= slept[-1] <= scheduler.SCHEDULER_TICK_SECONDS - 99
    scheduler._tick_pause(time.monotonic() - 3 * scheduler.SCHEDULER_TICK_SECONDS)
    assert slept[-1] == scheduler.SWEEP_FOLLOW_SECONDS, "a long tick is followed at once"


def test_the_minute_jobs_run_on_their_lanes_not_the_loop(db, monkeypatch):
    import ops
    import scheduler
    import staff_reminders
    import dsr.deliver
    import ai_batches
    import labor_live
    import pos_schedule_push
    threads = {}

    def rec(name):
        def fn(*a, **k):
            threads[name] = threading.current_thread().name
            return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
        return fn
    monkeypatch.setattr(staff_reminders, "run_job", rec("staff_reminders"))
    monkeypatch.setattr(dsr.deliver, "release_held", rec("dsr_delivery"))
    monkeypatch.setattr(ai_batches, "run_collector", rec("ai_batch_collect"))
    monkeypatch.setattr(labor_live, "run_live_labor", rec("live_labor"))
    monkeypatch.setattr(pos_schedule_push, "run_queue", rec("pos_schedule_push"))
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    p = scheduler._PulsedOps()
    for lane in scheduler._LANES.values():
        lane.join(10)
    assert scheduler._submit_minute_jobs(p, datetime(2026, 10, 20, 15, 0)) == 5
    for lane in ("minute", "live"):
        scheduler._LANES[lane].join(10)
    assert threads == {"ai_batch_collect": "scheduler-lane-minute", "dsr_delivery": "scheduler-lane-minute",
                       "staff_reminders": "scheduler-lane-minute", "live_labor": "scheduler-lane-live",
                       "pos_schedule_push": "scheduler-lane-live"}
    assert scheduler._submit_minute_jobs(p, datetime(2026, 10, 20, 15, 1)) == 0, "claimed once per slot"
    assert len(_q("SELECT id FROM job_runs WHERE job IN ('staff_reminders','live_labor')")) == 2


def test_the_intraday_slot_runs_as_one_chain_on_its_lane(db, monkeypatch):
    import ops
    import scheduler
    import strategy_jobs
    import staff_knowledge
    import task_sheets
    from event_intel import gameday
    seen = []

    def rec(name):
        def fn(restaurants=None, **k):
            seen.append((name, threading.current_thread().name, restaurants))
            return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
        return fn
    for mod, attr in ((strategy_jobs, "run_intraday_capture"), (strategy_jobs, "run_pre_dinner_pulse"),
                      (strategy_jobs, "run_coverage_check"), (strategy_jobs, "run_preshift_nudge"),
                      (strategy_jobs, "run_closing_summary"), (strategy_jobs, "run_demand_opportunity"),
                      (staff_knowledge, "run_cert_reminders"), (task_sheets, "run_job"), (gameday, "run_event_push")):
        monkeypatch.setattr(mod, attr, rec(attr))
    monkeypatch.setattr(strategy_jobs, "slot_restaurants", lambda: ["the slot"])
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    for lane in scheduler._LANES.values():
        lane.join(10)
    p = scheduler._PulsedOps()
    assert scheduler._submit_intraday(p, datetime(2026, 10, 20, 17, 5)) is True
    scheduler._LANES["intraday"].join(10)
    assert len(seen) == 9 and {t for _n, t, _r in seen} == {"scheduler-lane-intraday"}
    assert all(r == ["the slot"] for _n, _t, r in seen), "the restaurants are read once and handed to every job"
    assert scheduler._submit_intraday(p, datetime(2026, 10, 20, 17, 10)) is False, "one chain a 20-minute slot"
    runs = _q("SELECT job, claim_job FROM job_runs WHERE claim_job='intraday'")
    assert len(runs) == 9


def test_a_late_pass_still_reminds_an_overdue_critical_line_once(db, monkeypatch):
    import json
    import task_sheets
    import staff_reminders
    rid = _restaurant("Remind Co")
    monkeypatch.setattr(task_sheets, "published_day_shifts", lambda r, day: (
        [{"employee": "Ana", "role": "Server", "start": "2026-10-01T15:10:00", "end": "2026-10-01T22:00:00"}], True))
    c = models.get_conn()
    sheet = c.execute("INSERT INTO task_sheets (restaurant_id, job_code, shift_kind, title) VALUES (?,?,?,?)",
                      (rid, "Server", "opening", "Opening")).lastrowid
    c.execute("INSERT INTO task_assignments (restaurant_id, sheet_id, task_date, job_code, shift_kind, title, "
              "assignees_json, sheet_version, lines_json, line_count) VALUES (?,?,?,?,?,?,?,1,?,1)",
              (rid, sheet, "2026-10-01", "Server", "opening", "Opening", json.dumps(["Ana"]),
               json.dumps([{"line_id": 7, "label": "Temp the walk-in", "critical": 1,
                            "due_at": "2026-10-01T14:50:00"}])))
    c.commit()
    c.close()
    # The pass that should have run at 14:40 came at 15:00: the line is ten
    # minutes overdue and the shift ten minutes away.
    due = staff_reminders._due_reminders(rid, datetime(2026, 10, 1, 15, 0), None)
    kinds = {d[0]: d for d in due}
    assert set(kinds) == {"shift_reminder", "task_reminder"}
    assert kinds["task_reminder"][3] == "Overdue: Temp the walk-in"
    assert "was due at 2:50pm" in kinds["task_reminder"][4]
    # Past the catch-up, nothing.
    assert not [d for d in staff_reminders._due_reminders(rid, datetime(2026, 10, 1, 15, 25), None)
                if d[0] == "task_reminder"]


# ── R1-03: a brief waits only on its own inputs, to its own hour ───────────

def test_a_fleet_sweep_holds_each_brief_only_to_its_own_hour_plus_grace(db, monkeypatch):
    import scheduler
    import time_utils
    east = _restaurant("East Grill", timezone="America/New_York", morning_brief_hour=7)
    chi = _restaurant("Chi Grill", timezone="America/Chicago", morning_brief_hour=7)
    hold = scheduler._BriefHold("review_diagnoses", ())
    # 8:05 ET = 7:05 CT: East is past its grace (7am + 60), Chicago is not.
    clock = {"America/New_York": datetime(2026, 10, 20, 8, 5), "America/Chicago": datetime(2026, 10, 20, 7, 5)}
    monkeypatch.setattr(time_utils, "restaurant_now", lambda tz=None, naive=False: clock[tz])
    assert hold(east) is False, "an East Coast brief waited for 8am Chicago"
    assert hold(chi) is True


def test_a_queued_input_holds_only_its_own_restaurant(db, monkeypatch):
    import scheduler
    import strategy_jobs
    import time_utils
    a = _restaurant("A Grill", morning_brief_hour=7)
    b = _restaurant("B Grill", morning_brief_hour=7)

    class _Ops:
        def settled(self, job, day):
            return True
    monkeypatch.setattr(strategy_jobs, "open_restaurants", lambda name, day, db_path=None: {a})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda tz=None, naive=False: datetime(2026, 10, 20, 7, 10))
    hold = scheduler._brief_hold(_Ops(), datetime(2026, 10, 20, 7, 10))
    assert hold is not None and hold.fleet_input is None
    assert hold(a) is True and hold(b) is False, "one restaurant's open row held the fleet's briefs"
    assert scheduler._brief_hold(_Ops(), datetime(2026, 10, 20, 8, 0)) is None


def test_run_due_leaves_a_held_restaurant_unclaimed(db, monkeypatch):
    import morning_brief
    import ops
    sent = []
    monkeypatch.setattr(morning_brief, "_brief_queue", lambda db_path=None: ([(1, "2026-10-20"), (2, "2026-10-20")], []))
    monkeypatch.setattr(morning_brief, "deliver", lambda rid, **k: sent.append(rid) or {"sent": 1})
    monkeypatch.setattr(models, "get_restaurant", lambda rid, db_path=None: None)
    out = morning_brief.run_due(hold=lambda rid: rid == 1)
    assert sent == [2] and out["held"] == 1 and "deliver_ms_avg" in out
    assert not ops.period_claimed("morning_brief:1", "2026-10-20"), "a held brief must stay unclaimed"


# ── R1-12: a stuck lane is told, then replaced; it never keeps the lease ───

def test_a_stuck_lane_is_paged_then_replaced(monkeypatch):
    import ops
    import scheduler
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, **k: pages.append(key) or {"sent": True})
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(ops, "run_job", lambda name, fn, *a, **k: fn(*a, **k))
    release = threading.Event()
    lane = scheduler._Lane("g1test")
    monkeypatch.setitem(scheduler._LANES, "g1test", lane)
    assert lane.submit("labor_prewarm", lambda: release.wait(10))
    deadline = time.time() + 5
    while lane.current()[0] is None and time.time() < deadline:
        time.sleep(0.01)
    bound = scheduler.jobs_registry.max_minutes("labor_prewarm") * 60
    started = lane._current[1]
    lane._current = ("labor_prewarm", started - 2.5 * bound)
    assert scheduler.watch_lanes().get("g1test") == "stuck" and pages == ["lane_stuck:g1test"]
    assert scheduler.watch_lanes().get("g1test") is None, "told once"
    assert not lane._within(2), "a lane past twice its bound must stop renewing the lease"
    lane._current = ("labor_prewarm", started - 3.5 * bound)
    assert scheduler.watch_lanes().get("g1test") == "replaced"
    assert scheduler._LANES["g1test"] is not lane and not scheduler._LANES["g1test"].busy()
    release.set()
    lane.join(5)


# ── R1-07: the DSR sweep is sized to its night ─────────────────────────────

def test_the_dsr_sweep_sizes_its_workers_to_the_work(monkeypatch):
    from dsr import pipeline
    monkeypatch.setattr(pipeline, "SWEEP_WORKERS", None)
    assert pipeline.sweep_workers(5) == 4, "today's fleet keeps today's four"
    assert pipeline.sweep_workers(250) == 10
    assert pipeline.sweep_workers(5000) == pipeline.SWEEP_MAX_WORKERS
    monkeypatch.setattr(pipeline, "SWEEP_WORKERS", 2)
    assert pipeline.sweep_workers(5000) == 2, "an operator's pin wins"


# ── R1-14: boot backfills past the budget run after the boot ───────────────

def test_a_boot_past_its_budget_defers_the_backfills(monkeypatch):
    ran = []
    monkeypatch.setattr(models, "_DEFERRED_BOOT_BACKFILLS", [])
    assert models._boot_backfill(time.monotonic(), "quick", lambda: ran.append("quick")) is True
    late = time.monotonic() - models.BOOT_BACKFILL_BUDGET_SECONDS - 1
    assert models._boot_backfill(late, "people", lambda: ran.append("people")) is False
    assert ran == ["quick"]
    assert models.run_deferred_boot_backfills() == 1 and ran == ["quick", "people"]
    assert models._boot_backfill(late, "broken", lambda: 1 / 0) is False
    assert models.run_deferred_boot_backfills() == 0, "a failing deferred backfill is logged, never raised"
