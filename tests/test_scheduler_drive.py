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
    once, the 2am jobs run exactly once, and no hour-keyed job runs twice.

The clock is time_machine's (requirements-dev.txt); SQLite's own
datetime('now') is the real one, which nothing here compares against the
frozen day.
"""
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
# A plain import, never importorskip: a missing time-machine is a broken dev
# install, and a module-level skip would turn the whole file into "1 skipped"
# (blind re-audit 10/27/26 F11).
import time_machine

import jobs_registry  # noqa: E402
import models  # noqa: E402
import ops  # noqa: E402
import scheduler  # noqa: E402
import status_manager  # noqa: E402

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

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

    def fake_run_job(name, fn, *args, **kwargs):
        runs.append((name, kwargs.get("claim") or _claim_of(name), scheduler._chi_now()))
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
