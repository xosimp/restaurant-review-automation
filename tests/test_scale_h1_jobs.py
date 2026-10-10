"""Scheduler audit 10/10/26, workstream H1 — the bounded, resumable jobs:
the auto-draft's retry budget and fallback day (SCALE-3), the auto-publish
sweep (ARCH-23 / BUGSLIFE-24), and the schedule outcomes job (SCALE-15,
SCALE-16, LEARNING-22, SCALE-26)."""
import sys
from datetime import datetime

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
    return db_path


def _restaurant(db_path, **cols):
    rid = create_restaurant(Restaurant(name="Scale Grill", owner_email="scale@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


# ── SCALE-3: a failed or killed auto-draft is tried again ─────────────────

_THU_9AM = datetime(2026, 9, 24, 9, 0)      # the chosen draft day (Thursday)
_THU_10AM = datetime(2026, 9, 24, 10, 0)
_FRI_9AM = datetime(2026, 9, 25, 9, 0)      # the fallback day
_SAT_9AM = datetime(2026, 9, 26, 9, 0)


@pytest.fixture
def drafting(db, monkeypatch):
    """The auto-draft with the generation replaced: each run records itself
    and ends with the status `state["status"]`, as _run_schedule_job does
    (into the job row, never raising)."""
    import ops
    import schedule_engine as se
    state = {"status": "error", "runs": [], "pushes": []}

    def run(job_id, rid):
        state["runs"].append(rid)
        ops.finish_async_job(job_id, state["status"], {"ok": state["status"] == "done"})
    monkeypatch.setattr(se, "_run_schedule_job", run)
    monkeypatch.setattr("push.fire_push", lambda rid, kind, title, body, **k: state["pushes"].append((kind, title)))
    rid = _restaurant(db, module_labor=1, auto_draft_schedule=1, auto_draft_weekday=3, auto_draft_weekday_chosen=1,
                      billing_status="active")
    state["rid"] = rid
    return state


def test_a_failed_auto_draft_hands_its_day_back_and_the_next_pass_tries_again(drafting, db):
    import strategy_jobs
    out = strategy_jobs.run_auto_draft_schedules(db_path=db, now=_THU_9AM)
    assert drafting["runs"] == [drafting["rid"]] and out["drafted"] == 0
    # The next hourly pass the same day: tried again (it used to keep the
    # day's claim and wait a week).
    drafting["status"] = "done"
    out = strategy_jobs.run_auto_draft_schedules(db_path=db, now=_THU_10AM)
    assert drafting["runs"] == [drafting["rid"]] * 2 and out["drafted"] == 1


def test_the_week_gets_at_most_three_paid_tries_and_the_owner_is_told_after_the_last(drafting, db, monkeypatch):
    import strategy_jobs, notify, morning_brief
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 1, "is_admin": True}])
    for hour in (9, 10, 11, 12, 13):
        strategy_jobs.run_auto_draft_schedules(db_path=db, now=datetime(2026, 9, 24, hour, 0))
    assert len(drafting["runs"]) == strategy_jobs.AUTO_DRAFT_ATTEMPTS == 3
    # The fallback day spends nothing more on a week that used its budget.
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=_FRI_9AM)
    assert len(drafting["runs"]) == 3
    gave_up = [p for p in drafting["pushes"] if "couldn't be drafted" in p[1]]
    assert len(gave_up) == 1


def test_a_draft_day_missed_entirely_is_drafted_the_day_after(drafting, db):
    import strategy_jobs
    drafting["status"] = "done"
    out = strategy_jobs.run_auto_draft_schedules(db_path=db, now=_FRI_9AM)
    assert drafting["runs"] == [drafting["rid"]] and out["drafted"] == 1
    # Never on any other day.
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=_SAT_9AM)
    assert drafting["runs"] == [drafting["rid"]]


def test_the_fallback_day_never_redrafts_a_week_this_job_already_drafted(drafting, db):
    import strategy_jobs
    drafting["status"] = "done"
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=_THU_9AM)
    assert drafting["runs"] == [drafting["rid"]]
    # The owner removed the draft (no row for the week any more): the
    # fallback day does not pay for it again.
    conn = models.get_conn(db)
    conn.execute("DELETE FROM schedule_history WHERE restaurant_id=?", (drafting["rid"],))
    conn.commit()
    conn.close()
    strategy_jobs.run_auto_draft_schedules(db_path=db, now=_FRI_9AM)
    assert drafting["runs"] == [drafting["rid"]]


def test_a_draft_killed_by_a_deploy_is_tried_again_on_the_next_pass(drafting, db, monkeypatch):
    import strategy_jobs, ops
    import schedule_engine as se

    class Killed(BaseException):
        """The process going away mid-generation: no finally of ours runs
        a release, and the job row is left pending."""

    def killed(job_id, rid):
        drafting["runs"].append(rid)
        raise Killed()
    monkeypatch.setattr(se, "_run_schedule_job", killed)
    with pytest.raises(Killed):
        strategy_jobs.run_auto_draft_schedules(db_path=db, now=_THU_9AM)
    # The next boot fails every job the dead process left pending.
    ops.sweep_stale_jobs()
    drafting["status"] = "done"
    monkeypatch.setattr(se, "_run_schedule_job",
                        lambda job_id, rid: (drafting["runs"].append(rid), ops.finish_async_job(job_id, "done", {"ok": True})))
    out = strategy_jobs.run_auto_draft_schedules(db_path=db, now=_THU_10AM)
    assert drafting["runs"] == [drafting["rid"]] * 2 and out["drafted"] == 1


# ── ARCH-23 / BUGSLIFE-24: the auto-publish is bounded and resumable ──────

@pytest.fixture
def publishing(db, monkeypatch):
    """Two restaurants due to auto-publish on a Friday 9:30 (a Thursday
    draft day), with the per-restaurant work recorded instead of done."""
    import time_utils
    friday = datetime(2026, 10, 2, 9, 30)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: friday)
    rids = [_restaurant(db, auto_publish_schedule=1, module_labor=1, billing_status="active",
                        auto_draft_weekday=3, auto_draft_weekday_chosen=1) for _ in range(2)]
    seen = []
    monkeypatch.setattr(models, "schedule_publish_trust", lambda rid, *a, **k: seen.append(rid) or 0)
    return {"rids": rids, "seen": seen}


def test_a_bounded_auto_publish_pass_reports_it_and_the_next_pass_resumes_at_the_cursor(publishing, monkeypatch):
    import scheduler, time
    monkeypatch.setattr(scheduler, "AUTO_PUBLISH_WORKERS", 1)
    monkeypatch.setattr(scheduler, "AUTO_PUBLISH_MAX_SECONDS", 0.05)
    slow = models.schedule_publish_trust
    monkeypatch.setattr(models, "schedule_publish_trust", lambda rid, *a, **k: (time.sleep(0.1), slow(rid))[1])
    out = scheduler.run_auto_publish_schedules()
    # It used to report hit_bound False whatever happened.
    assert out["hit_bound"] is True and publishing["seen"] == publishing["rids"][:1]
    # The second restaurant was never claimed: the next pass reaches it first.
    monkeypatch.setattr(scheduler, "AUTO_PUBLISH_MAX_SECONDS", 60)
    out = scheduler.run_auto_publish_schedules()
    assert out["hit_bound"] is False and publishing["seen"] == publishing["rids"]


def test_a_restaurant_whose_auto_publish_raised_is_tried_again_by_a_later_pass(publishing, monkeypatch):
    import scheduler
    calls = []

    def boom(rid, *a, **k):
        calls.append(rid)
        if len(calls) == 1:
            raise RuntimeError("the database was locked")
        return 0
    monkeypatch.setattr(models, "schedule_publish_trust", boom)
    out = scheduler.run_auto_publish_schedules()
    assert out["failed"] == 1
    scheduler.run_auto_publish_schedules()
    # The one that raised had its day handed back; the other was done.
    assert sorted(calls) == sorted(publishing["rids"] + publishing["rids"][:1])


def test_a_draft_made_on_the_publish_day_itself_is_not_auto_published(db):
    import scheduler
    from datetime import date
    r = models.get_restaurant(_restaurant(db), db_path=db)
    assert scheduler._drafted_today("2026-10-02 14:10:00", date(2026, 10, 2), r)      # 9:10am Chicago
    assert not scheduler._drafted_today("2026-10-01 14:10:00", date(2026, 10, 2), r)  # the draft day
    src = open(scheduler.__file__).read()
    fn = src[src.index("def run_auto_publish_schedules"):]
    assert fn.index("_drafted_today(") < fn.index("delayed.schedule(")
