"""Scheduler audit 10/10/26, workstream E — the prompt in its right place.

AI-12: no counting or fill-order instructions — code holds minimums,
overtime, room and the ceiling, so the prompt states them as bounds.
AI-13: rules code already enforces are one clause naming that code holds
them, not paragraphs the model must obey.
AI-14: one ranked priority table, no block claiming an absolute rank of its
own; the shape contract's PRIORITIES and defaults speak of slots, not people,
and its input is AVAILABLE BY DAY (per role counts) instead of the ROSTER.
AI-20: the shape contract reads the restaurant's usual shifts."""
import datetime as dt
import json
import re
import types

import labor
import schedule_output as so
import schedule_prompt as sp
import schedule_engine as se
import schedule_rules as sr

WEEK = [(dt.date(2026, 10, 5) + dt.timedelta(days=i)).isoformat() for i in range(7)]
NOTE_WORDS = ", ".join(v for v in so.NOTE_VALUES if v)


def _static(contract="schema"):
    return sp.static_block(True, NOTE_WORDS, contract=contract)


def test_ai12_no_fill_order_or_counting_instruction_in_the_standing_text():
    for contract in ("schema", "compact", "shape"):
        s = _static(contract)
        for gone in ("Fill them from the people still under their minimum hours first",
                     "count the people", "add up", "Count each", "sum of"):
            assert gone not in s, (contract, gone)
        assert "CODE OWNS THE ARITHMETIC" in s
        assert "never sums for you to work out" in s


def test_ai13_rules_code_holds_are_one_clause_naming_code():
    s = _static()
    assert "(code staggers the starts)" in s
    assert "code keeps each person to the roles their CAN WORK lists" in s
    assert "code trims to it after you write" in s
    assert "it never adds hours" in s
    # The old paragraphs that taught the stagger and the roles are gone.
    assert "Never schedule two employees in the same role at the exact same start time" not in s
    assert "costs nothing extra" not in s


def test_ai14_one_ranked_table_and_no_block_claims_its_own_rank():
    s = _static()
    assert s.count("PRIORITIES — the one ranked order") == 1
    assert "a block that sounds absolute still sits at its rank here" in s
    assert "outranks" not in labor.SCHEDULE_SYSTEM_RULES
    assert "it ranks where the request's PRIORITIES put it" in labor.SCHEDULE_SYSTEM_RULES
    assert "the constraint wins" not in labor.STAFF_CONSTRAINTS_RULE


def test_ai14_the_shape_contract_speaks_of_slots_never_people():
    shape, schema = _static("shape"), _static("schema")
    assert sp.SHAPE_PRIORITIES != sp.PRIORITIES
    assert "each person's own limits in the ROSTER" in sp.PRIORITIES
    assert "each person's own limits in the ROSTER" not in sp.SHAPE_PRIORITIES
    assert "how many people AVAILABLE BY DAY says can work each role" in sp.SHAPE_PRIORITIES
    assert "write no slot for them" in sp.SHAPE_PRIORITIES
    assert "HOW TO WRITE SLOTS" in shape and "HOW TO WRITE SHIFTS" not in shape
    assert "HOW TO WRITE SHIFTS" in schema
    defaults = shape[shape.index("HOW TO WRITE SLOTS"):shape.index("NOTES AND TIMES")]
    for person_line in ("ROSTER shows missing shifts", "pair them with a dependable teammate", "CAN WORK"):
        assert person_line not in defaults, person_line
    assert "Code chooses who works each slot" in shape


def test_ai14_availability_table_counts_roles_closers_and_managers():
    counts = {(WEEK[4], "night"): {"Server": {"can": 5, "closers": 2, "managers": 0},
                                   "Manager": {"can": 2, "closers": 0, "managers": 2}},
              (WEEK[4], "morning"): {"Server": {"can": 3, "closers": 0, "managers": 0}}}
    text = sp.availability_table(counts, WEEK)
    assert text.lstrip().startswith("AVAILABLE BY DAY")
    assert "Fri 2026-10-09 lunch: Server 3" in text
    assert "Fri 2026-10-09 dinner: Manager 2 (2 managers); Server 5 (2 may close)" in text
    assert sp.availability_table({}, WEEK) == ""


def test_ai14_shape_availability_reads_the_constraints():
    names = ["Ann", "Bob", "Cat"]
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(sr.DAYS), roster_names=names,
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    c.blocked_dates = {"cat": {WEEK[4]: "on approved time off"}}
    out = se.shape_availability(c, [(n, "Server") for n in names], [WEEK[4]])
    assert out[(WEEK[4], "night")]["Server"]["can"] == 2


def _model(monkeypatch):
    seen = []

    def fake(client, **kw):
        seen.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps({"days": []}))],
                                     stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "claude-opus-5-5")
    return seen


def test_ai14_the_shape_call_reads_available_by_day_instead_of_the_roster(monkeypatch):
    seen = _model(monkeypatch)
    table = sp.availability_table({(WEEK[4], "night"): {"Server": {"can": 4, "closers": 1, "managers": 0}}}, WEEK)
    labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "total_sales": 60000, "period_days": 21, "by_day": {}},
        [], restaurant_name="T", hourly_rate=20.0, labor_target=30.0, week_start=WEEK[0],
        roster=[(f"S{i}", "Server") for i in range(4)], contract="shape", availability_table=table,
        staff_notes=[{"employee_name": "S0", "notes": "no Fridays"}])
    prompt = sp.prompt_text(seen[-1]["messages"][0]["content"])
    assert "AVAILABLE BY DAY" in prompt
    assert "ROSTER — one line per person" not in prompt
    assert "- S0: no Fridays" not in prompt
    assert "STAFF CONSTRAINTS — PRIORITIES 1b. Code holds each person's notes" in prompt


def test_the_schema_call_still_reads_the_roster(monkeypatch):
    seen = _model(monkeypatch)
    labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "total_sales": 60000, "period_days": 21, "by_day": {}},
        [], restaurant_name="T", hourly_rate=20.0, labor_target=30.0, week_start=WEEK[0],
        roster=[(f"S{i}", "Server") for i in range(4)],
        availability_table="\n\nAVAILABLE BY DAY — ignored on the schema contract")
    prompt = sp.prompt_text(seen[-1]["messages"][0]["content"])
    assert "ROSTER — one line per person" in prompt and "ignored on the schema contract" not in prompt
    assert not re.search(r"AVAILABLE BY DAY —", prompt)
