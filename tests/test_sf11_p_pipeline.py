"""Schedule fix round 10/11/26, workstream P — the solver-first draft as
production's default and the pipeline that honours it (blind re-audit
10/11/26): the solver is the default source and the model an arm (ARCH-1,
AI-1, ARCH-27, LOGIC-7); a code source never calls the drafting model on any
pass — the gate's rewrite, an owner's redo (BUGSGEN-9); the job is sized for
code, not for model calls nobody makes (ARCH-18); the model source's fallback
solves first (BUGSGEN-21).

No model is called: labor.generate_optimized_schedule is stubbed, and every
stub raises if it is asked to draft where it must not be."""
import json
import time

import pytest

import ai_utils
import labor
import ops
import schedule_engine as se
import schedule_fallback as fb
import schedule_job as sj
import schedule_rules as sr
from test_schedule_b2_calls import db, _build_harness, _gen, _line, WEEK, DAYS  # noqa: F401

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture(autouse=True)
def _no_note_model(monkeypatch):
    monkeypatch.setenv("SCHEDULE_NOTE_WRITE", "0")


def _no_draft_model(sent):
    """A labor stub that prepares the week's facts and refuses to draft."""
    def gen(analysis, shifts, prepare_only=False, week_slice=None, **kw):
        if prepare_only:
            return {"week_dates": WEEK, "week_days": DAYS, "rows": [], "schedule_csv": HEAD + "\n",
                    "narrative": [], "hours_budget": 60,
                    "requirements_by_date": {f"{d}|night": {"Server": 2} for d in WEEK},
                    "requirements": [{"date": d, "daypart": "night", "roles": [{"role": "Server", "required": 2}]}
                                     for d in WEEK]}
        sent.append(list(week_slice or WEEK))
        raise AssertionError("the drafting model was called on a code source")
    return gen


def _constraints(names=("Ann", "Bob", "Cat", "Dee")):
    return sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                          active={n.lower() for n in names})


def _fallback(source, c, names=("Ann", "Bob", "Cat", "Dee"), **kw):
    out = {"source": source, "restaurant_id": None, "constraints": c, "roster_names": list(names),
           "roster_roles": {n: "Server" for n in names}, "week_dates": WEEK, "plan_rows": [], "plan_dates": [],
           "open_times": {}, "close_times": {d: "10:00pm" for d in DAYS}}
    out.update(kw)
    return out


# ── ARCH-1: the solver is the default; the model is an arm ───────────────

def test_the_solver_is_the_default_source_and_the_model_an_arm(monkeypatch):
    monkeypatch.delenv("SCHEDULE_DRAFT_SOURCE", raising=False)
    assert fb.DEFAULT_SOURCE == "solver" and fb.draft_source(1, []) == "solver"
    assert fb.draft_source(1, [{"flags": {"draft_source": "model"}}]) == "model"
    import schedule_experiments as sx
    exp = sx.experiment("draft_v1")
    assert exp["control"] == exp["until_powered"] == "code_shaped"
    assert next(a for a in exp["arms"] if a["key"] == "model_draft")["flags"]["draft_source"] == "model"


def test_with_nothing_set_a_generation_drafts_in_code_and_never_calls_the_model(monkeypatch):
    monkeypatch.delenv("SCHEDULE_DRAFT_SOURCE", raising=False)
    sent = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _no_draft_model(sent))
    monkeypatch.setattr(se, "_quality_signals", lambda rid, res: ({}, None))
    c = _constraints()
    out = se._generate_in_parts({}, [], [(n, "Server") for n in c.roster_names],
                                {"week_start": WEEK[0], "restaurant_id": None},
                                fallback=_fallback(fb.draft_source(None, []), c))
    assert not sent and out["chunked"] == 0
    assert out["draft_source"]["chosen"] == "solver" and out["draft_source"]["source"] == "solver"
    assert {r["date"] for r in out["rows"]} == set(WEEK)


# ── BUGSGEN-9: a code source never calls the model, on any pass ─────────

@pytest.mark.parametrize("source", ["solver", "carry_forward"])
@pytest.mark.parametrize("focus", [None, ["Saturday dinner: 2 servers short"]])
def test_a_code_sources_redo_and_gate_pass_never_call_the_model(monkeypatch, source, focus):
    """The gate's rewrite (focus) and an owner's redo carry dates; with a code
    source chosen they used to go to labor.generate_optimized_schedule — a
    real Opus rewrite was made and kept on a "solver" week."""
    sent = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _no_draft_model(sent))
    monkeypatch.setattr(se, "_quality_signals", lambda rid, res: ({}, None))
    last = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
    monkeypatch.setattr(fb, "base_week", lambda rid, wd, shifts=None, db_path=None: {
        "rows": [{"date": d, "employee": "Bob", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm",
                  "scheduled_hours": "6", "notes": ""} for d in last],
        "source": "published", "week_start": last[0], "week_end": last[-1]})
    c = _constraints()
    kw = {"week_start": WEEK[0], "restaurant_id": None, "redo_dates": [WEEK[5]],
          "prior_rows": [{"date": d, "day": DAYS[i], "employee": "Ann", "role": "Server", "shift_start": "11:00am",
                          "shift_end": "3:00pm", "scheduled_hours": "4", "notes": ""}
                         for i, d in enumerate(WEEK) if d != WEEK[5]]}
    if focus:
        kw["focus"] = focus
    out = se._generate_in_parts({}, [], [(n, "Server") for n in c.roster_names], kw, fallback=_fallback(source, c))
    assert not sent, "a code source sent a pass to the drafting model"
    assert out["chunked"] == 0 and out["draft_source"]["chosen"] == source
    assert [r for r in out["rows"] if r["date"] == WEEK[5]]


def test_the_job_never_sends_a_code_drafted_weeks_gate_to_the_model(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], calls))
    real = se._generate_in_parts

    def solver_week(*a, **k):
        out = real(*a, **k)
        out["draft_source"] = dict(out.get("draft_source") or {}, chosen="solver", source="solver")
        return out
    monkeypatch.setattr(se, "_generate_in_parts", solver_week)
    monkeypatch.setattr(se, "_quality_gate", lambda result: {
        "dates": [WEEK[5]], "focus": ["Saturday dinner: 2 servers short"], "triggers": {WEEK[5]: ["coverage"]},
        "kinds": ["coverage"], "reason": "1 busy day still had a staffing hole after repair"})
    monkeypatch.setattr(se, "_gate_exact", lambda *a: None)
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    monkeypatch.setattr(sj, "_gate_rewrite", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the gate sent a code-drafted week to the model")))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("p-gate", rid)
    assert finished["status"] == "done" and len(calls) == 1
    g = finished["result"]["gate"]
    assert g["ran"] is False and g["why"] == "code_converged" and "drafted in code" in g["reason"]


def test_an_owners_redo_of_a_code_week_is_never_sent_to_the_model(db, monkeypatch):
    """A judgement code cannot read used to go to the model; under a code
    source the redo is drafted in code and, when that changes nothing, the
    owner is told so — nothing saved, no model call."""
    import models
    from models import Restaurant, create_restaurant
    monkeypatch.setenv("SCHEDULE_DRAFT_SOURCE", "solver")
    rid = create_restaurant(Restaurant(name="Code Redo", owner_email="cr@x.com"), db_path=db)
    rows = [{"date": d, "day": DAYS[i], "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""} for i, d in enumerate(WEEK)]
    base = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42.0, 0, 30,
                                        HEAD + "\n" + "\n".join(",".join(r[c] for c in se._ANSWER_COLS)
                                                                for r in rows), [], db_path=db)
    seen = []

    def build(restaurant_id, week_start=None, focus=None, dates=None, prior_rows=None, instruction=None,
              code_redo=False):
        seen.append(code_redo)
        if not code_redo:
            raise AssertionError("the redo went to the model")
        return {"schedule_csv": HEAD + "\n" + ",".join(rows[4][c] for c in se._ANSWER_COLS),
                "rows": [rows[4]], "week_dates": WEEK, "week_days": DAYS, "summary": [], "hours_budget": 0,
                "daily_target_hours": {}, "labor_target": 30, "unwritten_dates": [], "closed_dates": [],
                "generation_notes": [], "draft_source": {"chosen": "solver", "source": "solver"}}
    monkeypatch.setattr(se, "_build_schedule_result", build)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("p-redo", rid, dates=[WEEK[4]], base_history_id=base,
                         instruction=se.with_redo_reason(None, {"chip": "other",
                                                               "text": "make Friday feel less chaotic"}))
    assert seen == [True]
    assert finished["status"] == "error"
    assert "gave the same shifts" in json.dumps(finished["result"]) and "Nothing was saved" in json.dumps(
        finished["result"])


# ── ARCH-18: the job is sized for code ────────────────────────────────────

def test_a_code_draft_holds_the_job_to_codes_time_never_the_models(monkeypatch):
    monkeypatch.setattr(se._ops, "set_async_job_deadline", lambda *a, **k: None)
    clock = se.GenerationClock(job_id="p-clock", started=1000.0)
    assert clock.deadline == 1000.0 + se.SCHEDULE_JOB_MIN_SECONDS
    clock.plan_code()
    assert clock.deadline == 1000.0 + se.SCHEDULE_CODE_JOB_SECONDS < 1000.0 + se.SCHEDULE_JOB_MIN_SECONDS
    # a deadline a model call was planned against is never shortened
    other = se.GenerationClock(job_id="p-clock2", started=1000.0)
    other.plan(2, seconds=1500)
    before = other.deadline
    other.plan_code()
    assert other.deadline == before


def test_a_code_source_plans_no_model_call_on_the_clock(monkeypatch):
    sent = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _no_draft_model(sent))
    monkeypatch.setattr(se, "_quality_signals", lambda rid, res: ({}, None))
    monkeypatch.setattr(se._ops, "set_async_job_deadline", lambda *a, **k: None)
    monkeypatch.setattr(se._ops, "set_async_job_progress", lambda *a, **k: None)
    clock = se.GenerationClock(job_id="p-clock3", started=time.time())
    planned = []
    real_plan = se.GenerationClock.plan
    monkeypatch.setattr(se.GenerationClock, "plan", lambda self, *a, **k: (planned.append(a), real_plan(self, *a, **k)))
    c = _constraints()
    token = se._CLOCK.set(clock)
    try:
        se._generate_in_parts({}, [], [(n, "Server") for n in c.roster_names],
                              {"week_start": WEEK[0], "restaurant_id": None}, fallback=_fallback("solver", c))
    finally:
        se._CLOCK.reset(token)
    assert not sent and not planned
    assert clock.deadline <= clock.started + se.SCHEDULE_CODE_JOB_SECONDS + 1


# ── BUGSGEN-21: the model's fallback solves first ─────────────────────────

def test_the_models_fallback_is_the_solver_where_the_week_has_requirements(monkeypatch):
    assert fb.code_order("model", has_requirements=True) == ("solver", "carry_forward")
    assert fb.code_order("model", has_requirements=False) == ("carry_forward", "solver")
    assert fb.code_order("carry_forward") == ("carry_forward", "solver")
    assert fb.code_order("solver") == ("solver", "carry_forward")

    def gen(analysis, shifts, prepare_only=False, **kw):
        if prepare_only:
            return _no_draft_model([])(analysis, shifts, prepare_only=True)
        raise ai_utils.AIProviderDown("down")
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    monkeypatch.setattr(se, "_quality_signals", lambda rid, res: ({}, None))
    carried = []
    monkeypatch.setattr(fb, "base_week", lambda *a, **k: carried.append(1) or {})
    c = _constraints()
    out = se._generate_in_parts({}, [], [(n, "Server") for n in c.roster_names],
                                {"week_start": WEEK[0], "restaurant_id": None}, fallback=_fallback("model", c))
    assert out["draft_source"]["code_source"] == "solver" and set(out["draft_source"]["code_dates"]) == set(WEEK)
    assert not carried, "the carried week was read before the solver"
