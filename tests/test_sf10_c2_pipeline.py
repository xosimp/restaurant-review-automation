"""Scheduler fix round 10/10/26, workstream C2 — the generation job as an
explicit pipeline (ARCH-16) whose quality gate holds its candidates in memory
and saves only the winner, once (ARCH-17).

The model is never called: labor.generate_optimized_schedule is stubbed as
in test_schedule_b2_calls."""
import ast
import inspect
import json

import pytest

import labor
import models
import ops
import schedule_engine as se
import schedule_job as sj
from test_schedule_b2_calls import db, _build_harness, _gen, _csv, _line, WEEK  # noqa: F401


def _history(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT id, superseded_by, schedule_csv FROM schedule_history "
                                               "WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()]
    finally:
        conn.close()


def _versions(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT reason FROM schedule_versions WHERE restaurant_id=? ORDER BY id",
                                           (rid,)).fetchall()]
    finally:
        conn.close()


def _sat_gate():
    return {"dates": [WEEK[5]], "focus": ["Saturday 2026-10-10 dinner: 2 servers short"],
            "triggers": {WEEK[5]: ["coverage"]}, "kinds": ["coverage"],
            "reason": "1 busy day still had a staffing hole after repair"}


def _finished(monkeypatch):
    out = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, st, r: out.update(status=st, result=r))
    return out


# ── ARCH-17: nothing is saved while the gate decides ──────────────────────

def test_nothing_is_saved_and_the_owners_draft_stays_in_force_while_the_gate_rewrites(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    prior = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42, 0, 30, _csv([_line(d, "Cy") for d in WEEK]),
                                         [], db_path=db)
    seen_during = []
    calls = []
    first = _gen([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls)

    def gen(*a, **k):
        if calls:
            # The gate's rewrite is out: the week's first draft is held in
            # memory, the owner's draft still the one in force.
            seen_during.append(_history(db, rid))
        return first(*a, **k)
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    gates = [_sat_gate()]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if "Bo" in json.dumps(quality) else 10.0)
    finished = _finished(monkeypatch)
    se._run_schedule_job("c2-gate", rid)
    assert len(calls) == 2
    (during,) = seen_during
    assert [(h["id"], h["superseded_by"]) for h in during] == [(prior, None)]
    hist = _history(db, rid)
    assert len(hist) == 2 and hist[-1]["id"] == finished["result"]["history_id"]
    assert ",Bo," in hist[-1]["schedule_csv"]                      # the rewrite won, saved once
    assert hist[0]["superseded_by"] == hist[-1]["id"]
    assert _versions(db, rid).count("generated") == 1                # one generated version, for the winner
    assert finished["result"]["gate"]["kept"] == "regenerated"


def test_a_rewrite_that_loses_is_never_saved(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls))
    gates = [_sat_gate()]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    monkeypatch.setattr(se, "_gate_keeps", lambda *a: (False, "score"))
    finished = _finished(monkeypatch)
    se._run_schedule_job("c2-lose", rid)
    assert len(calls) == 2
    (only,) = _history(db, rid)
    assert only["id"] == finished["result"]["history_id"] and ",Bo," not in only["schedule_csv"]
    assert finished["result"]["gate"]["kept"] == "original"
    assert _versions(db, rid) == ["generated"]


def test_every_pass_of_the_job_is_linked_to_the_one_week_it_saved(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls, linked = [], []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls))
    gates = [_sat_gate()]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    import schedule_output
    real = schedule_output.link_calls
    monkeypatch.setattr(schedule_output, "link_calls",
                        lambda r, hid, gid, conn=None: linked.append((hid, gid)) or real(r, hid, gid, conn=conn))
    finished = _finished(monkeypatch)
    se._run_schedule_job("c2-links", rid)
    hid = finished["result"]["history_id"]
    assert len(linked) == 2 and {h for h, _g in linked} == {hid} and len({g for _h, g in linked}) == 2


# ── ARCH-16: no recursion, no locals()/inspect steering ───────────────────

def _calls_in(fn_or_mod):
    tree = ast.parse(inspect.getsource(fn_or_mod))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
        if isinstance(node, ast.Name) and node.id in ("locals", "globals"):
            names.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add("import:" + (a.name or ""))
    return names


def test_the_job_never_reenters_itself_and_steers_by_nothing_but_its_state():
    for unit in (sj, se._run_schedule_job):
        names = _calls_in(unit)
        assert "_run_schedule_job" not in names, unit
        assert "locals" not in names and "globals" not in names, unit
        assert "import:inspect" not in names and "signature" not in names, unit
    # The gate's passes are a bounded loop: at most the fill and the rewrite.
    assert sj.GATE_ATTEMPTS == ("fill", "rewrite")
    # The one save: _save_durably is called from _commit alone.
    src = inspect.getsource(sj)
    assert src.count("se._save_durably(") == 1
    assert "_save_durably(" in inspect.getsource(sj._commit)
    assert not hasattr(se, "_gate_model_rewrite")


# The note is the save's pre-step, for the winner only (blind re-audit
# 10/11/26 ARCH-13): after "built", before "saved".
BOUNDARIES = ("start", "parsed", "rules_read", "signals_read", "repaired", "reviewed", "scored", "built", "note")


@pytest.mark.parametrize("stage", BOUNDARIES)
def test_a_stop_at_any_stage_boundary_saves_nothing_and_says_stopped(db, monkeypatch, stage):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], calls))
    monkeypatch.setattr(se, "_quality_gate", lambda result: None)
    ops.claim_async_job("c2-stop", "schedule", rid)
    real = sj._boundary

    def boundary(job, name):
        if name == stage:
            assert ops.stop_async_job("c2-stop", {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE}) \
                == "stopped"
        return real(job, name)
    monkeypatch.setattr(sj, "_boundary", boundary)
    with se.generation_scope("c2-stop"):
        se._run_schedule_job("c2-stop", rid)
    job = ops.read_async_job("c2-stop", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["error"] == se.STOPPED_MESSAGE
    assert _history(db, rid) == []


def test_each_stage_boundary_is_a_queue_checkpoint_and_the_save_is_the_last(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], []))
    monkeypatch.setattr(se, "_quality_gate", lambda result: None)
    import job_queue
    seen = []
    monkeypatch.setattr(job_queue, "checkpoint", lambda stage, db_path=None, **data: seen.append(stage) or True)
    _finished(monkeypatch)
    se._run_schedule_job("c2-cp", rid)
    named = [s for s in seen if s in BOUNDARIES or s == "saved"]
    # "saved" is written AT the commit and again with the timings (blind
    # re-audit 10/20/26 R5-11): one boundary, written twice.
    named = [s for i, s in enumerate(named) if not (s == "saved" and i and named[i - 1] == "saved")]
    assert named == list(BOUNDARIES) + ["saved"]
    # the timed stages are boundaries too (_StageClock)
    assert seen.index("inputs_and_model") < seen.index("parsed") < seen.index("rules") < seen.index("repair")


def test_a_poll_that_called_the_job_dead_mid_pipeline_saves_nothing_and_is_out_of_time(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], []))
    monkeypatch.setattr(se, "_quality_gate", lambda result: None)
    ops.claim_async_job("c2-dead", "schedule", rid)
    real = sj._boundary

    def boundary(job, name):
        if name == "repaired":
            ops.finish_async_job("c2-dead", "error", {"ok": False, "error": "timed out"})
        return real(job, name)
    monkeypatch.setattr(sj, "_boundary", boundary)
    raised = []
    real_fail = sj._fail
    monkeypatch.setattr(sj, "_fail", lambda job, e: raised.append(e) or real_fail(job, e))
    with se.generation_scope("c2-dead"):
        se._run_schedule_job("c2-dead", rid)
    assert _history(db, rid) == []
    assert str(raised[0]) == se._TIMED_OUT_MESSAGE and not isinstance(raised[0], se.ScheduleStopped)


# ── one bounded pool for every generation's parallel calls ───────────────

def test_parallel_calls_of_every_generation_share_one_bounded_pool(monkeypatch):
    import threading
    import time as _t
    monkeypatch.setenv("SCHEDULE_CALL_POOL_WORKERS", "2")
    monkeypatch.setattr(se, "_CALL_POOL", None)
    active, peak, lock = [0], [0], threading.Lock()

    def generate(analysis, shifts, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _t.sleep(0.05)
        with lock:
            active[0] -= 1
        return {"ok": True}
    ready = [({"dates": [WEEK[i]]}, {}, True, None) for i in range(3)]
    out = []

    def one_generation():
        out.append(se._dispatch_calls(ready, {}, [], [], [], generate))
    threads = [threading.Thread(target=one_generation) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    pool = se._call_pool()
    try:
        assert len(out) == 2 and all(len(o) == 3 and all(e is None for _r, e in o) for o in out)
        # Two generations of three calls each: never more than the pool's two
        # streams at once (each batch used to open a pool of its own).
        assert peak[0] == 2
        assert se._call_pool() is pool and pool._max_workers == 2
    finally:
        pool.shutdown(wait=True)


# ── AI-28: a model redo is sent the redone dates and the seam only ───────

def test_redo_scope_is_the_dates_and_the_days_either_side():
    import schedule_prompt as sp
    assert sp.redo_scope([WEEK[3]], WEEK) == WEEK[2:5]
    assert sp.redo_scope([WEEK[0]], WEEK) == WEEK[:2]
    assert sp.redo_scope([WEEK[5], WEEK[6]], WEEK) == WEEK[4:]
    assert sp.redo_scope([], WEEK) == []


def _redo_prompt(monkeypatch, call_kind):
    import time as _t
    from test_schedule_b2_calls import _prompt_harness, _ANALYSIS
    prompts = _prompt_harness(monkeypatch)
    weather = [{"date": d, "high_f": 60 + i, "short_forecast": f"Sky{i}"} for i, d in enumerate(WEEK)]
    kept = [{"date": d, "employee": "Ana", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm",
             "day": sq_day(d), "scheduled_hours": "6"} for d in WEEK if d != WEEK[5]]
    labor.generate_optimized_schedule(_ANALYSIS, [], roster=[("Ana", "Server")], week_start=WEEK[0],
                                      week_slice=[WEEK[5]], prior_rows=kept, weather_forecast=weather,
                                      call_kind=call_kind, deadline=_t.time() + 600)
    return prompts[-1]["prompt"]


def sq_day(d):
    return se._date_of(d).strftime("%A")


def test_a_model_redo_is_sent_its_days_and_the_seam_not_the_whole_week(monkeypatch):
    p = _redo_prompt(monkeypatch, "redo")
    assert "THIS CALL REDOES Sat 2026-10-10" in p
    # Friday, Saturday and Sunday's weather; never Monday's.
    assert "Sky4" in p and "Sky5" in p and "Sky6" in p and "Sky0" not in p and "Sky2" not in p
    # The kept days still reach it, as the seam's facts about each person.
    assert "ALREADY WRITTEN FOR THE OTHER DAYS" in p


def test_a_week_slice_and_the_switch_keep_the_whole_weeks_context(monkeypatch):
    p = _redo_prompt(monkeypatch, None)                 # a big week's slice, not a redo
    assert "Sky0" in p and "THIS CALL REDOES" not in p
    monkeypatch.setenv("SCHEDULE_SLIM_REDO", "0")
    p = _redo_prompt(monkeypatch, "gate")
    assert "Sky0" in p and "THIS CALL REDOES" not in p
