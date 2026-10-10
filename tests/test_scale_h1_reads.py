"""Scheduler audit 10/10/26, workstream H1 — reading once: the read_scope
(SCALE-6, SCALE-7, LEARNING-23), the batched observation log (SCALE-26) and
the incremental as-run / outcomes reads (SCALE-15, SCALE-16, LEARNING-22)."""
import json
import sys
from datetime import date, timedelta

import pytest

import models
import schedule_intel
import schedule_memory as sm
import schedule_versions as sv
import staff_settings
from models import Restaurant, create_restaurant

HEAD = ",".join(sv.COLS) + "\n"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", conn)
    yield


def _rid(**kw):
    kw.setdefault("name", "Reads Co")
    kw.setdefault("owner_email", "r@x.test")
    kw.setdefault("module_labor", 1)
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw))


def _sql(q, *args):
    conn = models.get_conn()
    try:
        cur = conn.execute(q, args)
        conn.commit()
        return cur
    finally:
        conn.close()


def _q(q, *args):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()


SHIFTS = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
          + "".join(f"2026-09-{d:02d},Monday,Ana,Server,11:00,16:00,5,5,4000,\n" for d in range(1, 29)))


# ── SCALE-6 / SCALE-7: one read per unit of work ─────────────────────────

def test_outside_a_request_an_inputs_build_parses_the_shift_history_once(monkeypatch):
    import labor
    import schedule_engine as se
    import schedule_rules as sr
    rid = _rid()
    models.save_client_data(rid, "shifts", SHIFTS)
    parses = []
    real = labor.load_shifts
    monkeypatch.setattr(labor, "load_shifts", lambda *a, **k: (parses.append(1) if k.get("csv_string") else None,
                                                               real(*a, **k))[1])
    reads = []
    real_cd = models.get_client_data
    monkeypatch.setattr(models, "get_client_data", lambda *a, **k: (reads.append(1), real_cd(*a, **k))[1])
    week = [(date(2026, 10, 5) + timedelta(days=i)) for i in range(7)]
    rows = [{"date": "2026-10-05", "day": "Monday", "employee": "Ana", "role": "Server",
             "shift_start": "11:00am", "shift_end": "4:00pm", "scheduled_hours": "5", "notes": ""}]
    sr.build_constraints(rid, [d.isoformat() for d in week], [d.strftime("%A") for d in week])
    assert len(parses) == 1, parses               # was 7
    parses.clear()
    inputs = se.quality_inputs_from_db(rid, week_rows=rows)
    assert len(parses) == 1, parses               # was 12
    parses.clear()
    se._quality_signals(rid, inputs)
    assert len(parses) == 1, parses               # was 6
    # Across one scope, once for all three.
    parses.clear()
    reads.clear()
    with models.read_scope():
        sr.build_constraints(rid, [d.isoformat() for d in week], [d.strftime("%A") for d in week])
        inputs = se.quality_inputs_from_db(rid, week_rows=rows)
        se._quality_signals(rid, inputs)
    assert len(parses) == 1


def test_a_read_scope_reads_the_restaurant_row_once_and_sees_a_write(monkeypatch):
    rid = _rid()
    selects = []
    real = models.get_conn

    def traced(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(lambda s: selects.append(s) if "FROM restaurants WHERE id" in s else None)
        return c
    monkeypatch.setattr(models, "get_conn", traced)
    with models.read_scope():
        for _ in range(20):
            models.get_restaurant(rid)
        assert len(selects) == 1
        models.update_restaurant(rid, {"labor_target_pct": 27.5})
        assert models.get_restaurant(rid).labor_target_pct == 27.5
    # Outside the scope: every read reads.
    selects.clear()
    models.get_restaurant(rid)
    models.get_restaurant(rid)
    assert len(selects) == 2


def test_a_shifts_upload_inside_a_scope_is_read_back_new():
    import labor
    rid = _rid()
    models.save_client_data(rid, "shifts", SHIFTS)
    with models.read_scope():
        assert len(labor.load_shifts_for_restaurant(rid)) == 28
        models.save_client_data(rid, "shifts", SHIFTS.split("\n", 1)[0] + "\n" + SHIFTS.split("\n")[1] + "\n")
        assert len(labor.load_shifts_for_restaurant(rid)) == 1
        assert len(models.get_client_data(rid)["shifts_csv"].splitlines()) == 2


def test_a_generations_scope_shares_the_history_but_keeps_the_restaurant_row_live():
    import labor
    import schedule_engine as se
    rid = _rid()
    models.save_client_data(rid, "shifts", SHIFTS)
    with se.frozen_inputs():
        a = labor.load_shifts_for_restaurant(rid)
        assert labor.load_shifts_for_restaurant(rid) is a
        before = models.get_restaurant(rid).labor_target_pct
        # An owner's change in another request thread is a raw write here.
        _sql("UPDATE restaurants SET labor_target_pct=? WHERE id=?", (before or 30) + 1, rid)
        assert models.get_restaurant(rid).labor_target_pct == (before or 30) + 1      # P-41
    assert models.read_scope_store() is None


# ── SCALE-26: one transaction per restaurant's observations ──────────────

def test_an_observe_batch_writes_every_fact_in_one_transaction(monkeypatch):
    rid, other = _rid(), _rid(name="Other Co")
    opened = []
    real = sm.get_conn
    monkeypatch.setattr(sm, "get_conn", lambda db_path=None: (opened.append(1), real(db_path))[1])
    with sm.observe_batch(rid):
        for i in range(30):
            assert sm.observe(rid, "sq_as_run", date=f"2026-09-{i + 1:02d}", value={"score": i},
                              fact_key=f"sq_as_run|9|2026-09-{i + 1:02d}|night", origin="system",
                              phase="as_run", authority="system") is None
        # The same fact twice in the batch: the last value stands.
        sm.observe(rid, "sq_as_run", value={"score": 99}, fact_key="sq_as_run|9|2026-09-01|night",
                   origin="system", phase="as_run", authority="system")
        # Another restaurant inside the block is written at once.
        assert sm.observe(other, "sq_as_run", value={"score": 1}, fact_key="x", origin="system",
                          phase="as_run", authority="system")
        assert len(opened) == 1
    assert len(opened) == 2                                   # one transaction for the 31
    got = {o["fact_key"]: o["value"] for o in sm.observations(rid, kinds=("sq_as_run",))}
    assert len(got) == 30 and got["sq_as_run|9|2026-09-01|night"] == {"score": 99}


# ── SCALE-15 / SCALE-16: each ended week read once its numbers are in ────

def _monday(weeks_ago):
    t = date.today()
    return t - timedelta(days=t.weekday()) - timedelta(weeks=weeks_ago)


def _row(d, emp, start="5:00pm", end="10:00pm", role="Server", hours=5):
    return {"date": d, "day": date.fromisoformat(d).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _csv(rows):
    return HEAD + "".join(",".join(str(r[c]) for c in sv.COLS) + "\n" for r in rows)


def _published_week(rid, mon, rows):
    return _sql("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,0,100,30,?,'[]',?)",
                rid, mon.isoformat(), (mon + timedelta(days=6)).isoformat(), _csv(rows),
                (mon - timedelta(days=3)).isoformat() + " 12:00:00").lastrowid


def _punch(rid, d, emp, hours=5):
    _sql("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, shift_start, "
         "shift_end, scheduled_hours, actual_hours, pay_rate, source) VALUES (?,?,?,?,?,?,?,NULL,?,15,'rpower')",
         rid, d, emp, staff_settings.name_key(emp), "Server", "17:00", "22:00", hours)


@pytest.fixture
def scored(monkeypatch):
    calls = []

    def fake(rid_, hid_, rows):
        calls.append(hid_)
        return {"checked": True, "score": 70, "shifts": [{"date": r["date"], "daypart": "night", "scored": True,
                                                         "score": 70, "dimensions": []} for r in rows]}
    monkeypatch.setattr(schedule_intel, "score_as_run", fake)
    return calls


def test_an_ended_week_is_scored_as_run_once_and_again_only_when_its_punches_change(scored):
    rid = _rid()
    mon = _monday(3)
    fri = (mon + timedelta(days=4)).isoformat()
    hid = _published_week(rid, mon, [_row(fri, "Ana"), _row(fri, "Bo")])
    _punch(rid, fri, "Ana")
    assert schedule_intel.record_as_run_quality(rid, only_changed=True) == 1
    assert schedule_intel.record_as_run_quality(rid, only_changed=True) == 0      # was re-scored every Monday
    assert scored == [hid]
    _punch(rid, fri, "Bo")                                                           # a late punch arrives
    assert schedule_intel.record_as_run_quality(rid, only_changed=True) == 2        # both shifts now
    assert scored == [hid, hid]
    # The direct call (a re-run by hand, the tests) still reads every week.
    assert schedule_intel.record_as_run_quality(rid) == 2


def test_a_week_whose_last_pos_day_is_not_final_waits(scored):
    rid = _rid()
    mon = _monday(1)
    fri = (mon + timedelta(days=4)).isoformat()
    _published_week(rid, mon, [_row(fri, "Ana")])
    _punch(rid, fri, "Ana")
    last = (mon + timedelta(days=6)).isoformat()
    _sql("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct) VALUES (?,?,1000,30)", rid, last)
    try:
        _sql("UPDATE labor_daily_history SET final=0 WHERE restaurant_id=? AND date=?", rid, last)
    except Exception:
        pytest.skip("no final column on labor_daily_history")
    assert schedule_intel.record_as_run_quality(rid, only_changed=True) == 0 and scored == []
    _sql("UPDATE labor_daily_history SET final=1 WHERE restaurant_id=? AND date=?", rid, last)
    assert schedule_intel.record_as_run_quality(rid, only_changed=True) == 1


def test_outcomes_reread_a_settling_week_and_leave_a_settled_unchanged_one():
    rid = _rid()
    old = _monday(6)                     # ended well past WEEK_SETTLE_DAYS
    young = _monday(1)                   # ended within it
    for mon in (old, young):
        d = (mon + timedelta(days=2)).isoformat()
        _published_week(rid, mon, [_row(d, "Ana")])
    first = schedule_intel.record_outcomes(rid, only_changed=True)
    assert first["weeks"] == 2
    again = schedule_intel.record_outcomes(rid, only_changed=True)
    assert again["weeks"] == 1           # the settling week only; it used to re-read all twelve
    _punch(rid, (old + timedelta(days=2)).isoformat(), "Ana")
    assert schedule_intel.record_outcomes(rid, only_changed=True)["weeks"] == 2


def test_the_outcomes_job_runs_daily_on_a_worker_pool():
    import inspect
    import scheduler
    import strategy_jobs
    import jobs_registry
    loop = inspect.getsource(scheduler.scheduler_loop)
    i = loop.index('_ops.claim_period("schedule_outcomes"')
    line = loop[loop.rfind("\n", 0, i):i]
    assert "weekday()" not in line, line
    assert jobs_registry.JOBS["schedule_outcomes"]["cadence"] == "4am CT daily"
    assert strategy_jobs.OUTCOMES_WORKERS >= 2
    src = inspect.getsource(strategy_jobs.run_schedule_outcomes)
    # The per-restaurant body is strategy_jobs.schedule_outcomes_one since
    # SCALE-25 (the queue's learner rows run it too).
    assert "read_scope()" in src and "schedule_outcomes_one(" in src
    assert "only_changed=True" in inspect.getsource(strategy_jobs.schedule_outcomes_one)


# ── LEARNING-23: 24 weeks of versions replayed once per generation ────────

def test_the_learning_weeks_are_replayed_once_per_scope_and_handed_out_as_copies(monkeypatch):
    rid = _rid()
    mon = _monday(2)
    d = (mon + timedelta(days=1)).isoformat()
    hid = _published_week(rid, mon, [_row(d, "Ana")])
    sv.append(rid, hid, "generated", _csv([_row(d, "Ana")]), saved_by="Cavnar AI", saved_authority="system")
    sv.append(rid, hid, "edited", _csv([_row(d, "Bo")]), saved_by="erik", saved_authority="principal")
    built = []
    real = sv._learning_weeks
    monkeypatch.setattr(sv, "_learning_weeks", lambda *a, **k: (built.append(1), real(*a, **k))[1])
    with models.read_scope():
        first = sv.learning_weeks(rid)
        first[0]["items"].clear()                    # a caller's change never reaches the next one
        second = sv.learning_weeks(rid)
        third = sv.learning_weeks(rid)
    assert len(built) == 1, built                    # was one read per call
    assert second == third and second[0]["items"]
    sv.learning_weeks(rid)                           # outside a scope: read every time (G1's kept records)
    assert len(built) == 2
