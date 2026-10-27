"""Drive the real scheduler loop with a frozen clock (Audit #25 A07-13,
A07-16).

The registry-sync tests read the loop's source; a shadowed `_due` stopped
every job on 9/19/26 while every one of them stayed green. Here the loop
itself runs, tick after tick, with every job's body stubbed (ops.run_job
records the name and runs nothing), against a real database for the
claims:

  * a whole day, a tick every 10 minutes: every registry job with a daily
    SLA is started, and no job runs twice under one claimed period;
  * the 2026 DST changes, 00:30-03:30 local on 11/1 (fall back: the 1am
    hour twice) and 3/8 (spring forward: no 2am): every claimed job runs
    once, the 2am jobs run exactly once, and no hour-keyed job runs twice;
  * a Sunday, a Monday and a Tuesday morning: every registry job with a
    WEEKLY SLA is started (the Monday block is where a shadowed condition
    would stop them all — re-audit B7 P3), and the 1st of a quarter and the
    2nd of a month start the monthly and quarterly jobs;
  * a real partial run inside the loop: a sweep that fails 3 of 10
    restaurants is retried once, 30 minutes later, for those 3 only.

The clock is time_machine's (requirements-dev.txt); SQLite's own
datetime('now') is the real one, which nothing here compares against the
frozen day.
"""
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

time_machine = pytest.importorskip("time_machine")

import jobs_registry  # noqa: E402
import models  # noqa: E402
import ops  # noqa: E402
import scheduler  # noqa: E402
import status_manager  # noqa: E402

CHI = ZoneInfo("America/Chicago")
_DAILY = 26 * 60

# Daily jobs whose gate is a condition of the deployment, not the clock
# (none today: blob_offload and pg_partitions, which need object storage and
# Postgres, have no SLA). A job named here must say what gates it.
CONDITIONAL = {}


class _Stop(BaseException):
    """Raised from the stubbed sleep to leave the loop (BaseException, so the
    loop's own `except Exception` cannot swallow it)."""


@pytest.fixture
def drive(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    for v in ("HEALTHCHECK_PING_URL", "SCHEDULE_DRAFT_SOURCE", "DATABASE_URL"):
        monkeypatch.delenv(v, raising=False)
    runs, claims, errors = [], [], []
    real_run_job = ops.run_job
    stub = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    real_bodies = set()          # jobs whose own (test-stubbed) function runs, not the empty stub

    def fake_run_job(name, fn, *args, **kwargs):
        runs.append((name, kwargs.get("claim") or _claim_of(name), scheduler._chi_now()))
        if name in real_bodies:
            return real_run_job(name, fn, *args, context=kwargs.get("context", ""), claim=kwargs.get("claim"))
        return real_run_job(name, lambda: dict(stub), context=kwargs.get("context", ""), claim=kwargs.get("claim"))
    monkeypatch.setattr(ops, "run_job", fake_run_job)
    real_claim = scheduler._PulsedOps.claim_period

    def claim_period(self, job, period):
        got = real_claim(self, job, period)
        if got:
            claims.append((job, str(period)))
        return got
    monkeypatch.setattr(scheduler._PulsedOps, "claim_period", claim_period)

    real_queue = scheduler.queue_scheduled_job

    def queue_scheduled_job(name, period, context="", request_id=None):
        runs.append((name, _claim_of(name), scheduler._chi_now()))
        return real_queue(name, period, context=context, request_id=request_id)
    monkeypatch.setattr(scheduler, "queue_scheduled_job", queue_scheduled_job)
    monkeypatch.setattr(scheduler.log, "error", lambda msg, *a, **k: errors.append(str(msg)))

    def run(start_local, minutes, step=10):
        """Tick from `start_local` (an aware Chicago datetime) every `step`
        minutes for `minutes` of REAL (UTC) time."""
        start = start_local.astimezone(timezone.utc)
        ticks = {"n": 0, "of": int(minutes // step) + 1}
        with time_machine.travel(start, tick=False) as traveller:
            def sleep(_seconds):
                for lane in scheduler._LANES.values():
                    lane.join(10)
                ticks["n"] += 1
                if ticks["n"] >= ticks["of"]:
                    raise _Stop()
                traveller.shift(timedelta(minutes=step))
            monkeypatch.setattr(scheduler.time, "sleep", sleep)
            with pytest.raises(_Stop):
                scheduler.scheduler_loop()
        for lane in scheduler._LANES.values():
            lane.join(10)
        return runs, claims, errors
    run.real_bodies = real_bodies
    return run


def _no_loop_error(errors):
    bad = [e for e in errors if "Scheduler loop error" in e or "UnboundLocalError" in e]
    assert not bad, bad


def _claim_of(name):
    return (jobs_registry.spec(name) or {}).get("claim") or name


def _no_double_run(runs, claims):
    """No job started more often than the claim it ran under was claimed: a
    period claimed once is run once (a job run under another claim — the
    weekly Intel's daily retry pass, claim competitor_retry — counts there)."""
    claimed = {}
    for key, period in claims:
        claimed.setdefault(key, set()).add(period)
    counts = {}
    for name, key, _at in runs:
        counts[(name, key)] = counts.get((name, key), 0) + 1
    over = {f"{n} ({k})": (c, sorted(claimed.get(k, ()))) for (n, k), c in counts.items()
            if k in claimed and c > len(claimed[k])}
    assert not over, f"ran more often than claimed: {over}"


def _no_lost_claim(runs, claims):
    """Every claim key the loop took was run under — a period claimed and
    never run is a lost period (claim before work)."""
    started = {k for _n, k, _at in runs}
    lost = sorted({k for k, _p in claims} - started)
    assert not lost, f"claimed but never started: {lost}"


def _ran(runs):
    out = {}
    for name, _key, _at in runs:
        out[name] = out.get(name, 0) + 1
    return out


def test_a_driven_day_starts_every_daily_job_once(drive):
    # Tuesday 10/27/26, midnight to midnight, a tick every 10 minutes.
    runs, claims, errors = drive(datetime(2026, 10, 27, 0, 0, tzinfo=CHI), minutes=24 * 60 - 10)
    _no_loop_error(errors)
    _no_double_run(runs, claims)
    _no_lost_claim(runs, claims)
    ran = _ran(runs)
    daily = {n for n, s in jobs_registry.JOBS.items() if s.get("sla_minutes") == _DAILY}
    missing = sorted(daily - set(ran) - set(CONDITIONAL))
    assert not missing, f"daily registry jobs the loop never started in a day: {missing}"
    # Claimed once a day: started once.
    twice = sorted(n for n in daily if sum(1 for r in runs if r[0] == n and r[1] == _claim_of(n)) > 1
                   and len({p for k, p in claims if k == _claim_of(n)}) <= 1)
    assert not twice, twice
    assert set(CONDITIONAL) <= set(jobs_registry.JOBS), "a conditional entry names no registry job"


@pytest.mark.parametrize("day", ["2026-11-01", "2026-03-08"], ids=["fall_back", "spring_forward"])
def test_the_dst_change_runs_every_claimed_job_once(drive, day):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 0, 30, tzinfo=CHI)
    end = datetime(d.year, d.month, d.day, 3, 30, tzinfo=CHI)
    span = int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() // 60)
    assert span == (240 if day == "2026-11-01" else 120), "the window spans the change"
    runs, claims, errors = drive(start, minutes=span)
    _no_loop_error(errors)
    _no_double_run(runs, claims)
    ran = _ran(runs)
    # The 2am jobs: due from 2am, so the spring-forward gap still runs them
    # (at 3am), and the repeated fall-back hour does not run them twice.
    for name in ("backup_db", "pos_budget_check"):
        assert ran.get(name) == 1, (name, ran.get(name))
    # An hour-keyed job (claimed f"{today}-{now.hour}"): one run per local
    # hour that exists — the repeated 1am hour of 11/1 runs nothing more,
    # the missing 2am hour of 3/8 runs nothing at all.
    hours = sorted(p.rsplit("-", 1)[1] for k, p in claims if k == "issue_scan")
    assert hours == (["0", "1", "2", "3"] if day == "2026-11-01" else ["0", "1", "3"]), hours
    assert ran.get("issue_scan") == len(hours)
    # Nothing claimed was left unrun (a claim with no run is a lost period).
    _no_lost_claim(runs, claims)


_WEEKLY = 8 * 24 * 60


def test_driven_sunday_monday_and_tuesday_mornings_start_every_weekly_job(drive):
    # 11/8/26 is a Sunday, after the DST change: its early jobs, Monday's
    # block, Tuesday's recipe drafts. Ticks every 20 minutes.
    for start, minutes in ((datetime(2026, 11, 8, 3, 0, tzinfo=CHI), 3 * 60),
                           (datetime(2026, 11, 9, 4, 0, tzinfo=CHI), 8 * 60),
                           (datetime(2026, 11, 10, 4, 0, tzinfo=CHI), 2 * 60)):
        runs, claims, errors = drive(start, minutes=minutes, step=20)
    _no_loop_error(errors)
    _no_double_run(runs, claims)
    _no_lost_claim(runs, claims)
    weekly = {n for n, s in jobs_registry.JOBS.items() if s.get("sla_minutes") == _WEEKLY}
    missing = sorted(weekly - set(_ran(runs)))
    assert not missing, f"weekly registry jobs the loop never started in their week's mornings: {missing}"


def test_the_first_of_a_quarter_and_the_second_of_a_month_start_their_jobs(drive):
    # Thursday 10/1/26 (a quarter's first day) and Friday 10/2 after the 2am
    # backup (its queue row has no consumer here: settled at the followers'
    # latest hour).
    drive(datetime(2026, 10, 1, 8, 0, tzinfo=CHI), minutes=2 * 60, step=20)
    runs, claims, errors = drive(datetime(2026, 10, 2, 1, 40, tzinfo=CHI),
                                 minutes=(scheduler.BACKUP_FOLLOWERS_LATEST_HOUR - 1) * 60 + 20, step=30)
    _no_loop_error(errors)
    ran = _ran(runs)
    assert ran.get("quarterly_summaries"), "the quarter's summaries never started on its first day"
    assert ran.get("restore_drill") == 1, "the monthly restore drill was not queued on the 2nd"
    _no_double_run(runs, claims)


def test_a_partial_sweep_in_the_loop_is_retried_once_for_the_failed_restaurants(drive, monkeypatch):
    """The partial-retry path end to end, in the real loop on its lane:
    forecast_scoring fails 3 of 10 restaurants at 5am; 30 minutes later it
    runs again for those 3 — never the 7 that succeeded (re-audit B7 P3)."""
    swept = []

    def forecast_scoring():
        mine = []

        def one(rid):
            mine.append(rid)
            if rid in (2, 5, 8):
                raise RuntimeError(f"restaurant {rid}'s forecast unreadable")
        scheduler.resumable_sweep("forecast_scoring_test", list(range(1, 11)), one, 60, job="forecast_scoring")
        swept.append(sorted(mine))
        bad = sum(1 for r in mine if r in (2, 5, 8))
        return {"attempted": len(mine), "ok": len(mine) - bad, "failed": bad, "skipped": 0, "hit_bound": False}
    monkeypatch.setattr(scheduler, "run_forecast_scoring", forecast_scoring)
    drive.real_bodies.add("forecast_scoring")
    runs, claims, errors = drive(datetime(2026, 10, 27, 3, 50, tzinfo=CHI), minutes=2 * 60 + 20)
    _no_loop_error(errors)
    assert swept[0] == list(range(1, 11)) or sorted(swept[0]) == list(range(1, 11)), swept
    # The three fail again on the retry: the period stays spent — never a
    # failed-retry backoff that would run all ten once more.
    assert swept[1:] == [[2, 5, 8]], f"the retry re-ran restaurants that had succeeded: {swept}"
    at = [a for n, _k, a in runs if n == "forecast_scoring"]
    assert len(at) == 2 and at[1] - at[0] >= timedelta(minutes=jobs_registry.PARTIAL_RETRY_MINUTES), at
