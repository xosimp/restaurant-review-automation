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


BOUNDARIES = ("start", "parsed", "rules_read", "signals_read", "repaired", "reviewed", "scored", "note", "built")


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
