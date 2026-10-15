"""Schedule fix round 10/11/26, workstream J — the generation job pipeline
(blind re-audit 10/11/26): the gate's exact fill and its verdicts
(BUGSGEN-1, -16), a winning gate pass keeps what the first pass said about
the week (BUGSGEN-10), resume never buries a newer draft and honours Stop
(BUGSGEN-11), a saved job is never called dead (BUGSGEN-13), a re-run keeps
how its redo was drafted (BUGSGEN-15), a redo stays in its draft's week
(BUGSGEN-2), the note is written for the winner only (ARCH-13).

No model is called: labor.generate_optimized_schedule is stubbed as in
test_schedule_b2_calls, and the note's model call is stubbed or off."""
import datetime as dt
import schedule_job
import json
import types

import pytest

import labor
import models
import ops
import schedule_engine as se
import schedule_job as sj
from test_schedule_b2_calls import db, _build_harness, _gen, _csv, _line, WEEK, DAYS  # noqa: F401


@pytest.fixture(autouse=True)
def _no_note_model(monkeypatch):
    # The note's model call is off unless a test counts it.
    monkeypatch.setenv("SCHEDULE_NOTE_WRITE", "0")


def _finished(monkeypatch):
    out = {}
    real = ops.finish_async_job

    def fin(j, st, r):
        out.update(status=st, result=r)
        return real(j, st, r)
    monkeypatch.setattr(se._ops, "finish_async_job", fin)
    return out


def _history(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT id, superseded_by, week_start, schedule_csv FROM "
                                               "schedule_history WHERE restaurant_id=? ORDER BY id",
                                               (rid,)).fetchall()]
    finally:
        conn.close()


def _sat_gate():
    return {"dates": [WEEK[5]], "focus": ["Saturday dinner: 2 servers short"],
            "triggers": {WEEK[5]: ["coverage"]}, "kinds": ["coverage"],
            "reason": "1 busy day still had a staffing hole after repair"}


def _captures(monkeypatch):
    seen = []
    monkeypatch.setattr(se._ops, "capture", lambda e, job=None, context=None, **k: seen.append((job, repr(e))))
    return seen


# ── BUGSGEN-1: the gate's exact fix is filled in code, not a crash ────────

def test_the_exact_solves_fix_is_filled_in_code_and_the_gate_says_so(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], calls))
    gates = [_sat_gate()]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    fix = {"date": WEEK[5], "day": "Saturday", "employee": "Bo", "role": "Server", "shift_start": "5:00pm",
           "shift_end": "9:00pm", "scheduled_hours": "4", "notes": ""}
    monkeypatch.setattr(se, "_gate_exact", lambda view, gate, r: {"fixes": [fix], "conflicts": [], "probed": [WEEK[5]],
                                                                  "unprobed": []})
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if "Bo" in json.dumps(quality) else 10.0)
    caps = _captures(monkeypatch)
    finished = _finished(monkeypatch)
    se._run_schedule_job("j-exact", rid)
    assert len(calls) == 1, "the exact fix is filled in code — no rewrite paid for"
    assert not [c for c in caps if c[0] == "schedule_quality_gate"], caps
    g = finished["result"]["gate"]
    assert g["ran"] is True and g["kept"] == "filled" and g["path"] == "code"


# ── BUGSGEN-16: a gate owed but not run always says why ───────────────────

@pytest.mark.parametrize("why", ["error", "capped", "time"])
def test_a_gate_that_was_owed_but_did_not_run_carries_a_verdict(db, monkeypatch, why):
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], []))
    monkeypatch.setattr(se, "_quality_gate", lambda result: _sat_gate())
    monkeypatch.setattr(se, "_gate_exact", lambda *a: None)
    _captures(monkeypatch)
    if why == "error":
        def boom(result, clock):
            raise RuntimeError("gate fill check failed")
        monkeypatch.setattr(se, "_gate_fill_skip", boom)
    elif why == "capped":
        monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
        monkeypatch.setattr(se, "_escalate_for_gate", lambda gate: "capped")
    else:
        monkeypatch.setattr(se, "GATE_MIN_MODEL_SECONDS", 10 ** 9)
    finished = _finished(monkeypatch)
    with se.generation_scope("j-gate-" + why):
        ops.claim_async_job("j-gate-" + why, "schedule", rid)
        se._run_schedule_job("j-gate-" + why, rid)
    g = finished["result"]["gate"]
    assert finished["status"] == "done"
    assert g["ran"] is False and g["why"] == why and g["reason"] and g["dates"] == [WEEK[5]]


def test_the_web_shows_a_gate_that_was_skipped():
    import pathlib
    src = (pathlib.Path(sj.__file__).parent / "templates" / "dashboard.html").read_text()
    assert "if (g && (g.ran || g.why) && g.reason)" in src


# ── BUGSGEN-10: a winning gate pass keeps the first pass's week facts ─────

LINE = "Built without the AI model: Monday 10/5/26 to Friday 10/9/26 — your published week carried forward."


def _code_built_first(monkeypatch, code_dates):
    real = se._generate_in_parts
    n = []

    def wrapped(*a, **k):
        out = real(*a, **k)
        if not n:
            n.append(1)
            out["draft_source"] = {"chosen": "model", "source": "model", "code_source": "solver",
                                   "code_dates": list(code_dates), "why": {d: "provider" for d in code_dates},
                                   "line": LINE}
            out["generation_notes"] = [LINE]
            out["unwritten_dates"] = [{"date": WEEK[6], "day": "Sunday", "why": "provider"}]
        return out
    monkeypatch.setattr(se, "_generate_in_parts", wrapped)


def test_a_winning_gate_rewrite_keeps_the_first_passs_code_built_days(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls))
    _code_built_first(monkeypatch, WEEK[:5])
    gates = [_sat_gate()]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    monkeypatch.setattr(se, "_gate_exact", lambda *a: None)
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if "Bo" in json.dumps(quality) else 10.0)
    finished = _finished(monkeypatch)
    se._run_schedule_job("j-inherit", rid)
    res = finished["result"]
    assert len(calls) == 2 and res["gate"]["kept"] == "regenerated"
    assert res["draft_source"]["code_dates"] == WEEK[:5]
    assert res["review"]["lines"][0] == LINE or LINE in res["review"]["lead_lines"]
    # Sunday was empty in the first draft and the rewrite never touched it.
    assert [u["date"] for u in res["unwritten_dates"]] == [WEEK[6]] and res["partial"] is True
    assert any(str(x).startswith("⚠ Sunday") for x in res["review"]["lead_lines"])


def test_a_gate_day_the_first_pass_drafted_in_code_drops_out_of_the_line():
    facts = {"week_dates": WEEK, "draft_source": {"chosen": "model", "source": "model", "code_source": "solver",
                                                  "code_dates": WEEK[:6], "why": {d: "provider" for d in WEEK[:6]},
                                                  "line": "Built without the AI model: all six"},
             "generation_notes": ["Built without the AI model: all six", "No history line"]}
    result = {"week_dates": WEEK, "draft_source": {"chosen": "model", "source": "model", "code_dates": [], "why": {}},
              "generation_notes": ["No history line"]}
    sj._inherit_week_facts(result, facts, [WEEK[5]])
    ds = result["draft_source"]
    assert ds["code_dates"] == WEEK[:5]
    assert "Saturday" not in ds["line"] and "Friday" in ds["line"] and ds["line"].startswith("Built without")
    assert result["generation_notes"] == [ds["line"], "No history line"]


def test_a_winning_gate_pass_keeps_the_first_passs_budget_trim():
    # EJ's 10/21/26 week: the first pass trimmed 206h to the budget, the
    # gate's fill won, and the saved review said nothing was trimmed.
    trims = [{"date": WEEK[0], "employee": "Ana", "role": "Server", "hours": 3.0, "kind": "removed", "index": 4},
             {"date": WEEK[5], "employee": "Bo", "role": "Server", "hours": 2.0, "kind": "cut", "index": 9}]
    facts = {"week_dates": WEEK, "draft_source": {}, "trimmed": trims}
    result = {"week_dates": WEEK, "draft_source": {}}
    sj._inherit_week_facts(result, facts, [WEEK[5]])
    assert [t["employee"] for t in result["inherited_trimmed"]] == ["Ana"]     # the gate redid Saturday
    assert "trimmed" in sj._WEEK_FACT_KEYS
    # The repair stage folds them in beside the pass's own trims, the first
    # pass's row index dropped (it pointed into that pass's week).
    src = open(se.__file__).read()
    body = src[src.index('_inherited = [dict('):src.index('result["budget_conflict"]')]
    assert 'result.pop("inherited_trimmed"' in body and '"index"' in body and "hours_trimmed" in body


# ── BUGSGEN-11: resume never buries a newer draft; Stop is honoured ───────

def _save(db_path, rid, who):
    return models.save_schedule_history(rid, WEEK[0], WEEK[-1], 42, 0, 30, _csv([_line(d, who) for d in WEEK]), [],
                                        db_path=db_path)


def _job(hid, job_id, rid, prior=None):
    import schedule_resume as sres
    return types.SimpleNamespace(
        checkpoint={"stages": ["saved"], "payload_z": sres.pack({"ok": True, "history_id": hid,
                                                                  "review": {"lines": []}}),
                    "history_id": hid, "week_start": WEEK[0]},
        payload={"job_id": job_id, "restaurant_id": rid, "prior_draft_id": prior})


def test_a_resumed_saved_try_never_supersedes_a_newer_draft(db, monkeypatch):
    import schedule_resume as sres
    rid = _build_harness(monkeypatch, db)
    old = _save(db, rid, "Ana")
    newer = _save(db, rid, "Bo")
    ops.claim_async_job("j-res", "schedule", rid)
    assert schedule_job.finish_from_checkpoint(_job(old, "j-res", rid)) is True
    by_id = {h["id"]: h for h in _history(db, rid)}
    assert by_id[newer]["superseded_by"] is None and by_id[old]["superseded_by"] == newer
    got = ops.read_async_job("j-res", restaurant_id=rid)
    assert got["status"] == "done" and got["result"]["resumed"] is True
    assert got["result"]["review"]["lines"][-1] == sres.RESUMED_SAVED_LINE
    assert not hasattr(sres, "_keep_in_force")


def test_an_owners_stop_after_the_save_is_honoured_by_the_resumed_try(db, monkeypatch):
    import schedule_resume as sres
    rid = _build_harness(monkeypatch, db)
    prior = _save(db, rid, "Cy")
    saved = _save(db, rid, "Ana")
    ops.claim_async_job("j-stop", "schedule", rid)
    conn = models.get_conn(db)
    conn.execute("UPDATE async_jobs SET saved_ref=? WHERE job_id='j-stop'", (saved,))
    conn.commit()
    conn.close()
    assert ops.stop_async_job("j-stop", {"ok": False, "stopped": True, "error": se.STOPPED_MESSAGE}) == "stopping"
    assert schedule_job.finish_from_checkpoint(_job(saved, "j-stop", rid, prior=prior)) is True
    got = ops.read_async_job("j-stop", restaurant_id=rid)
    assert got["status"] == "error" and got["result"]["restored_history_id"] == prior
    in_force = [h["id"] for h in _history(db, rid) if h["superseded_by"] is None]
    assert in_force == [prior]


# ── BUGSGEN-13: a poll that calls a SAVED job dead yields to it ───────────

@pytest.mark.parametrize("late_seconds,poll_says", [(120, "pending"), (3600, "error")])
def test_a_poll_during_the_after_save_steps_never_overrules_the_saved_draft(db, monkeypatch, late_seconds,
                                                                             poll_says):
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK]], []))
    monkeypatch.setattr(se, "_quality_gate", lambda result: None)
    polled = []
    real_after = sj._after_save

    def after(job, winner, hid, gen_box, t_save):
        conn = models.get_conn(db)
        conn.execute("UPDATE async_jobs SET deadline_at=datetime('now', ?) WHERE job_id='j-poll'",
                     (f"-{late_seconds} seconds",))
        conn.commit()
        conn.close()
        polled.append(ops.read_async_job("j-poll", restaurant_id=rid))
        return real_after(job, winner, hid, gen_box, t_save)
    monkeypatch.setattr(sj, "_after_save", after)
    ops.claim_async_job("j-poll", "schedule", rid)
    with se.generation_scope("j-poll"):
        se._run_schedule_job("j-poll", rid)
    assert polled[0]["status"] == poll_says
    if poll_says == "error":
        assert polled[0]["result"]["saved"] is True and polled[0]["result"]["provisional"] is True
    got = ops.read_async_job("j-poll", restaurant_id=rid)
    (h,) = _history(db, rid)
    assert got["status"] == "done" and got["result"]["history_id"] == h["id"]


def test_the_boot_sweep_never_calls_a_saved_job_interrupted(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    ops.claim_async_job("j-sweep", "schedule", rid)
    conn = models.get_conn(db)
    conn.execute("UPDATE async_jobs SET saved_ref=77 WHERE job_id='j-sweep'")
    conn.commit()
    conn.close()
    ops.sweep_stale_jobs(older_than_minutes=0)
    got = ops.read_async_job("j-sweep", restaurant_id=rid)
    assert got["status"] == "error" and got["result"]["saved"] is True and "interrupted" not in got["result"]["error"]
    # The job's own finish (a resumed try's) still replaces the provisional verdict.
    ops.finish_async_job("j-sweep", "done", {"ok": True, "history_id": 77})
    assert ops.read_async_job("j-sweep", restaurant_id=rid)["status"] == "done"


# ── BUGSGEN-15: a re-run over an edited draft keeps how the redo was made ─

def test_a_rerun_after_draft_changed_keeps_a_code_redos_how(monkeypatch):
    seen = []
    base = sj.Base(rows=[], csv="", history_id=3, week_start=WEEK[0])
    monkeypatch.setattr(sj, "_read_base", lambda req: base)
    monkeypatch.setattr(se, "_redo_reads_as_rules", lambda *a: True)

    def build(job, spec):
        seen.append(spec)
        res = {"redo": {"how": "code" if spec.code_redo else (spec.redo_how or "model")}}
        return sj.Candidate(spec=spec, result=res, rows=[], payload={}, timer=None, code_redo_on=spec.code_redo,
                            answer={"schedule_csv": ""})
    monkeypatch.setattr(sj, "_build_pass", build)
    from models import DraftChanged
    commits = []

    def commit(job, cand):
        commits.append(cand)
        if len(commits) == 1:
            raise DraftChanged(3, "")
        return 1
    monkeypatch.setattr(sj, "_commit", commit)
    monkeypatch.setattr(se, "_redo_base_moved", lambda *a: "again")
    job = sj.GenerationRun(sj.Request(job_id=None, restaurant_id=1, dates=[WEEK[5]], base_history_id=3), None)
    sj._owner_redo(job)
    assert seen[0].code_redo is True and seen[1].code_redo is False and seen[1].redo_how == "code"
    cand = sj.Candidate(spec=seen[1], result={"schedule_csv": "date\n", "rows": []}, rows=[], payload={},
                        timer=None)
    sj._stage_scope(cand)
    assert cand.result["redo"]["how"] == "code"


# ── BUGSGEN-2: a redo stays in its own draft's week ───────────────────────

def test_a_redo_of_a_day_outside_its_drafts_week_is_refused():
    base = sj.Base(rows=[], csv="", history_id=3, week_start="2026-10-12")
    sj._refuse_dates_outside(base, ["2026-10-12", "2026-10-18"])
    with pytest.raises(se.ScheduleGenerationError, match="aren't in this draft's week of 10/12/26"):
        sj._refuse_dates_outside(base, ["2026-10-19"])


def test_a_redo_takes_its_week_from_the_draft_not_the_client(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    hid = _save(db, rid, "Ana")
    built = {}
    def build(job, spec):
        built["ws"] = job.req.week_start
        raise se.ScheduleGenerationError("stop here")
    monkeypatch.setattr(sj, "_build_pass", build)
    job = sj.GenerationRun(sj.Request(job_id=None, restaurant_id=rid, week_start="2026-10-07", dates=[WEEK[2]],
                                      base_history_id=hid), None)
    with pytest.raises(se.ScheduleGenerationError):
        sj._owner_redo(job)
    assert built["ws"] == WEEK[0]


def test_check_week_start_reads_a_saved_drafts_own_week(db, monkeypatch):
    import time_utils
    rid = _build_harness(monkeypatch, db)
    models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 40, 0, 30, _csv([]), [], db_path=db)
    models.update_restaurant(rid, {"week_start_day": 2}, db_path=db) \
        if "db_path" in models.update_restaurant.__code__.co_varnames else models.update_restaurant(
            rid, {"week_start_day": 2})
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: dt.datetime(2026, 10, 14, 9, 0))
    # Monday 10/12's draft: under the new Wednesday start it would read as the
    # week of 10/7-10/13 — "already happened" — but it is its own week.
    assert se.check_week_start(rid, "2026-10-12") == ("2026-10-12", None)


# ── ARCH-13: the note is written once, for the draft the job saves ────────

def test_the_note_is_written_once_for_the_winner_never_for_a_losing_rewrite(db, monkeypatch):
    rid = _build_harness(monkeypatch, db)
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls))
    gates = [_sat_gate()]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    monkeypatch.setattr(se, "_gate_fill_skip", lambda result, clock: "converged")
    monkeypatch.setattr(se, "_gate_exact", lambda *a: None)
    monkeypatch.setattr(se, "_gate_keeps", lambda *a: (False, "score"))
    import schedule_narrative
    notes = []

    def write(rid_, rows, **k):
        notes.append(sorted({r.get("employee") for r in rows}))
        return {"bullets": ["Ana holds the week"], "source": "final_week", "dropped": []}
    monkeypatch.setattr(schedule_narrative, "write", write)
    finished = _finished(monkeypatch)
    se._run_schedule_job("j-note", rid)
    assert len(calls) == 2, "the week and the gate's rewrite"
    assert notes == [["Ana"]], "one note, for the draft that was saved"
    assert finished["result"]["narrative"] == ["Ana holds the week"]
    assert finished["result"]["review"]["note"]["source"] == "final_week"


# ── BUGSGEN-14: an after-close shift is the sweep's to judge, not NEEDS REVIEW ─

def test_a_shift_starting_after_the_close_is_judged_soft_by_the_sweep_not_flagged():
    import sched_d2_week as w
    import schedule_rules as sr
    rows, c, _sig = w.big_week(breaches=False)
    d = w.WEEK[4]
    porter = w.row(d, "P50", "12:30am", "4:00am", "Dishwasher")
    se._enforce_close_time(porter, "Friday", c.close_times, {}, constraints=c)
    assert not porter.get("needs_review"), porter
    mine = [v for v in sr.violations(rows + [porter], c) if v.get("employee") == "P50" and v["date"] == d]
    assert [v["hard"] for v in mine if v["kind"] == "past_close"] == [False]       # the sweep: soft


# ── BUGSGEN-24: a week already begun is drafted from today only ──────────

def test_a_week_already_begun_is_drafted_from_today_and_keeps_the_days_worked(db, monkeypatch):
    import time_utils
    rid = _build_harness(monkeypatch, db)
    _save(db, rid, "Cy")                                        # the week's working copy
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 8, 9, 0))
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _gen([[_line(d, "Ana") for d in WEEK[3:]]], calls))
    monkeypatch.setattr(se, "_quality_gate", lambda result: None)
    finished = _finished(monkeypatch)
    se._run_schedule_job("j-begun", rid, week_start=WEEK[0])
    assert finished["status"] == "done", finished
    assert len(calls) == 1 and calls[0]["slice"] == WEEK[3:]       # nothing paid for the days gone
    res = finished["result"]
    saved = {(r["date"], r["employee"]) for r in res["preview_rows"]}
    assert {(d, "Cy") for d in WEEK[:3]} <= saved and {(d, "Ana") for d in WEEK[3:]} <= saved
    assert not any(e == "Cy" and d in WEEK[3:] for d, e in saved)
    assert not res.get("redo") and any("had already begun" in x for x in res["review"]["lines"])


def test_a_started_week_gate_never_reworks_a_day_already_worked(db, monkeypatch):
    import time_utils
    rid = _build_harness(monkeypatch, db)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 8, 9, 0))
    calls = []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen([[_line(d, "Ana") for d in WEEK[3:]]], calls))
    gate = dict(_sat_gate(), dates=[WEEK[1]], triggers={WEEK[1]: ["coverage"]})
    monkeypatch.setattr(se, "_quality_gate", lambda result: gate)
    finished = _finished(monkeypatch)
    se._run_schedule_job("j-begun-gate", rid, week_start=WEEK[0])
    assert len(calls) == 1 and finished["result"]["gate"]["why"] == "past"


# ── ARCH-25: a resumed draft whose inputs moved says so ──────────────────

def _req(content, ttl="5m"):
    return {"model": "m", "system": [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral",
                                                                                        "ttl": ttl}}],
            "messages": [{"role": "user", "content": content}]}


def test_a_resumed_answer_written_from_inputs_that_moved_is_said(db):
    import schedule_output
    import schedule_resume
    schedule_output.record_call(1, _req("ROSTER: Ana, Bo"), answer="a1", job_id="job-mv", outcome="ok")
    schedule_output.record_call(1, _req("ROSTER: Ana, Bo"), answer="a2", job_id="job-mv", outcome="ok")
    with schedule_resume.resuming("job-mv"):
        # The same prompt (only a cache TTL differs): nothing moved.
        assert schedule_resume.stored_answer(_req("ROSTER: Ana, Bo", ttl="1h")) is not None
        assert schedule_resume.resume_facts() == {"resumed": True, "answers_reused": 1, "inputs_moved": False}
        # Bo left while the job restarted: the prompt built now differs.
        assert schedule_resume.stored_answer(_req("ROSTER: Ana")) is not None
        assert schedule_resume.resume_facts()["inputs_moved"] is True
        cand = sj.Candidate(spec=sj.PassSpec(kind="week"), rows=[], payload={}, timer=None,
                            result={"review": {"lines": ["x"], "lead_lines": []}})
        sj._stage_inputs(cand)
    review = cand.result["review"]
    assert review["lines"][0] == schedule_resume.INPUTS_MOVED_LINE and review["lead_lines"][0].startswith("⚠")
    assert cand.result["resumed_inputs"]["inputs_moved"] is True
    assert schedule_resume.resume_facts() == {"resumed": False}
