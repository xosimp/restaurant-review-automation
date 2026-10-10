"""Workstream F (scheduler audit 10/10/26) — AI-8: the deterministic+critic
arm. The model reviews a week code built and proposes a few typed edits;
code keeps an edit only when its person may legally work it, nothing ranked
above quality gets worse, and Shift Quality does not fall."""
import json
import types

import schedule_critic as scr
import schedule_eval
import schedule_experiments as sx
import schedule_rules as sr
import shift_quality as sq

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


def cons(names):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(names), active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {"server"} for n in names}
    return c


def row(d, name, start="5:00pm", end="10:00pm"):
    r = {"date": d, "day": sq._day_name(d), "employee": name, "role": "Server", "shift_start": start,
         "shift_end": end, "scheduled_hours": "", "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


def test_an_edit_is_kept_only_when_code_finds_it_breaks_nothing():
    c = cons(["Ana", "Ben", "Cy"])
    c.blocked_dates["cy"] = {WEEK[4]: "on approved time off"}
    rows = [row(WEEK[4], "Ana"), row(WEEK[5], "Ben")]
    edits = [{"op": "reassign", "date": WEEK[4], "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
              "from_employee": "Ana", "to_employee": "Cy", "reason": "her usual night"},       # Cy is off: refused
             {"op": "reassign", "date": WEEK[5], "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
              "from_employee": "Ben", "to_employee": "Cy", "reason": "Cy's regular Saturday"},  # legal: kept
             {"op": "remove", "date": WEEK[1], "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
              "reason": "slow"}]                                                                # no such shift
    got = scr.validate(rows, edits, c)
    assert [e["reason"] for e in got["kept"]] == ["Cy's regular Saturday"]
    assert {r["why"] for r in got["refused"]} >= {"no such shift in the week"}
    assert any("time off" in r["why"] for r in got["refused"])
    assert [r["employee"] for r in got["rows"]] == ["Ana", "Cy"]


def test_an_edit_that_lowers_the_score_is_refused():
    c = cons(["Ana", "Ben"])
    rows = [row(WEEK[4], "Ana")]
    edits = [{"op": "reassign", "date": WEEK[4], "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
              "from_employee": "Ana", "to_employee": "Ben", "reason": "x"}]
    got = scr.validate(rows, edits, c, score=lambda rs: 90 if rs[0]["employee"] == "Ana" else 80)
    assert not got["kept"] and "Shift Quality 90 → 80" in got["refused"][0]["why"]


def test_the_review_reads_the_models_edits_and_never_lets_it_write_the_week():
    c = cons(["Ana", "Ben"])
    rows = [row(WEEK[4], "Ana", "_x")] if False else [row(WEEK[4], "Ana")]
    seen = {}

    def fake(req):
        seen.update(req)
        body = {"edits": [{"op": "add", "date": WEEK[6], "role": "Server", "shift_start": "5:00pm",
                           "shift_end": "10:00pm", "to_employee": "Ben", "reason": "Sunday is busy"}]}
        return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text",
                                                                                            text=json.dumps(body))])
    got = scr.review(1, rows, c, call=fake)
    assert got["called"] and [e["reason"] for e in got["kept"]] == ["Sunday is busy"]
    assert seen["action"] == "schedule_critic" and "THE WEEK CODE BUILT" in seen["messages"][0]["content"]
    assert [r["employee"] for r in got["rows"]] == ["Ana", "Ben"]


def test_the_arm_is_off_the_hash_and_reachable_by_the_switch(monkeypatch):
    exp = next(e for e in sx.EXPERIMENTS if e["key"] == "draft_v1")
    arm = next(a for a in exp["arms"] if a["key"] == "code_critic")
    assert arm["weight"] == 0 and arm["flags"] == {"draft_source": "solver", "critic": True}
    assert scr.enabled([arm]) and not scr.enabled([])
    monkeypatch.setenv("SCHEDULE_DRAFT_CRITIC", "1")
    assert scr.enabled([])


def test_the_eval_runs_the_solver_first_arm_the_v0_arm_and_the_critic_arm():
    assert set(schedule_eval.CODE_ARMS) == {"carry_forward", "solver", "solver_v0"}
    assert schedule_eval.CRITIC_ARM == "solver_critic"
