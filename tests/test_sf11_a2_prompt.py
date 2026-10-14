"""Schedule fix round 10/11/26, workstream A2 — the schedule prompt and its
note (blind re-audit AI-11, AI-12, AI-14, AI-15, AI-16, AI-17). No model is
called here."""
import json
import types

import schedule_narrative as sn
import schedule_output as so
import schedule_prompt as sp
import schedule_requirements as req
import schedule_rules as sr
from test_schedfix_c1_prompt import ANALYSIS, NOTE_WORDS, WEEK, _blocks, _call


def _prompt(kw):
    return "\n".join(_blocks(kw))


# ── AI-12: a missing labor % is never 0 ───────────────────────────────────

def test_no_sales_means_labor_percent_not_measured_never_zero(monkeypatch):
    kw, _ = _call(monkeypatch, analysis=dict(ANALYSIS, overall_labor_pct=0, total_sales=0))
    text = _prompt(kw)
    assert "Labor %: not measured (no sales on file for that window)" in text
    assert "Overall labor over that window: 0%" not in text and "None%" not in text
    kw, _ = _call(monkeypatch)
    assert "Overall labor over that window: 28.0%" in _prompt(kw)


# ── AI-11: a demand level only where something measured it ────────────────

def test_a_shift_demand_level_is_said_only_when_measured():
    rows = req.shift_requirements(WEEK[:2], typical_headcount={("Monday", "night"): {"Server": 3}})
    assert rows and all(r["demand_measured"] is False for r in rows if r.get("demand"))
    block = req.requirements_block(rows)
    assert "demand not measured" in block
    assert not any(f"| {lvl} demand" in block for lvl in ("low", "normal", "high", "peak"))
    # The restaurant's own sales by weekday measure it.
    measured = req.shift_requirements(WEEK[:2], typical_headcount={("Monday", "night"): {"Server": 3}},
                                      demand_by_day={"Monday": 40.0, "Tuesday": -30.0})
    assert any(r["demand_measured"] for r in measured)
    assert " demand" in req.requirements_block(measured).replace("demand not measured", "")


# ── AI-14: one definition of "manager" ────────────────────────────────────

def test_the_roster_marks_a_manager_only_by_the_constraints_definition(monkeypatch):
    roster = [("S0", "Server"), ("S1", "Server"), ("Mo", "Manager")]
    kw, _ = _call(monkeypatch, roster=roster, manager_plan={"managers": [], "rows": [], "dates": []})
    text = _prompt(kw)
    assert "nobody on this roster counts as a manager" in text
    mo = next(ln for ln in text.splitlines() if ln.strip().startswith("Mo |"))
    assert "(manager)" not in mo
    kw, _ = _call(monkeypatch, roster=roster,
                  manager_plan={"managers": [{"name": "Mo", "role": "Manager"}], "rows": [], "dates": []})
    mo = next(ln for ln in _prompt(kw).splitlines() if ln.strip().startswith("Mo |"))
    assert "(manager)" in mo


# ── AI-15: what code holds is said once ───────────────────────────────────

def test_the_standing_block_no_longer_restates_what_code_holds():
    static = sp.static_block(True, NOTE_WORDS)
    assert static.count("a bound, never a sum to work out") == 0
    assert "repairing what falls short" not in static
    assert "every row code has to change is counted against the draft" in static
    # The rules block names the managers; the rule itself is 1a's.
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(sr.DAYS), roster_names=["Mo"],
                       active={"mo"})
    c.compliance = dict(sr.DEFAULTS)
    c.managers = {"mo": "Manager"}
    block = sr.prompt_block(c)
    assert "fill from the people below theirs first" not in block
    assert "It gives way only" not in next(ln for ln in block.splitlines() if "NON-NEGOTIABLE" in ln)


# ── AI-16: the model's reasons are data, not three discarded bullets ──────

def test_the_answer_carries_decisions_tied_to_rows_and_no_bullet_rules():
    schema = so.contract_schema("schema", employees=["Ana"], roles=["Server"], dates=WEEK, times=["5:00pm"])
    assert schema["required"] == ["days", "decisions"]
    item = schema["properties"]["decisions"]["items"]
    assert item["properties"]["date"]["enum"] == WEEK and item["properties"]["role"]["enum"] == ["Server"]
    for contract in ("compact", "shape"):
        assert "decisions" in so.contract_schema(contract, employees=["Ana"], roles=["Server"], dates=WEEK,
                                                 times=["5:00pm"])["required"]
    for contract in ("schema", "compact", "shape"):
        static = sp.static_block(True, NOTE_WORDS, contract=contract)
        assert "Each summary bullet" not in static and "`decisions`" in static
    assert "Each summary bullet" in sp.static_block(False, NOTE_WORDS)            # the CSV fallback keeps them
    raw = json.dumps({"days": [], "decisions": [{"date": WEEK[4], "role": "Server", "why": "Bears home game"},
                                                {"date": "2027-01-01", "role": "Server", "why": "not asked"}]})
    got = so.parse_answer(raw, dates=WEEK)
    assert got["decisions"] == [{"date": WEEK[4], "role": "Server", "why": "Bears home game"}]
    assert got["summary"] == ["Friday Server: Bears home game"]


def test_labor_hands_the_decisions_on(monkeypatch):
    answer = {"days": [], "decisions": [{"date": WEEK[4], "role": "Server", "why": "the event lifts dinner"}]}
    _kw, out = _call(monkeypatch, answer=answer)
    assert out["decisions"] == [{"date": WEEK[4], "role": "Server", "why": "the event lifts dinner"}]


# ── AI-17: the note is told why ───────────────────────────────────────────

def test_the_note_writer_reads_why_the_week_looks_as_it_does():
    rows = [{"date": WEEK[4], "day": "Friday", "employee": "Ana", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "5"}]
    result = {"decisions": [{"date": WEEK[4], "role": "Server", "why": "Bears game lifts dinner"},
                            {"date": WEEK[5], "role": "Bartender", "why": "moved since"}],
              "requirements": [{"date": WEEK[4], "daypart": "night", "reasons": ["+20% Bears home game"]}],
              "draft_source": {"solver": {"reasons": [{"text": "Ana, not Ben: Ben would hit overtime"}]}},
              "optimizer": {"changes": [{"reason": "moved Cy to Saturday to cover the floor"}]}}
    reasons = sn.reasons_for_note(result, rows)
    assert "decided: Friday Server: Bears game lifts dinner" in reasons
    assert not any("Bartender" in r for r in reasons)                  # no longer in the final week
    assert "needed: Friday dinner: +20% Bears home game" in reasons
    assert any(r.startswith("chosen: Ana, not Ben") for r in reasons)
    assert any(r.startswith("changed: moved Cy") for r in reasons)
    seen = {}

    def call(req_):
        seen["req"] = req_
        return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(
            type="text", text=json.dumps({"bullets": ["Friday dinner sized for the Bears game"]}))])
    sn.write(5, rows, reasons=reasons, call=call)
    body = seen["req"]["messages"][0]["content"]
    assert "WHY IT LOOKS THIS WAY" in body and "Bears game lifts dinner" in body
    assert "WHY IT LOOKS THIS WAY" in sn.SYSTEM


def test_the_job_passes_the_note_its_reasons():
    import inspect
    import schedule_job
    assert "reasons=_snar.reasons_for_note(result, preview_rows)" in inspect.getsource(schedule_job._stage_note)
