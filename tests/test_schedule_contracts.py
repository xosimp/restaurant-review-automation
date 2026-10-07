"""The schedule generator's AI cost audit items (10/7/26): #69, #70, #71, #36,
#72, #73.

- #70 the compact contract: the same rows with one-letter keys, read back to
  the same rows, recorded with its calls, sized at its own tokens a row.
- #69 the shape contract: slots without names, the people solved in code by
  schedule_solver against every rule, behind SCHEDULE_CONTRACT (default off),
  and an arm of scripts/schedule_model_eval.py.
- #71 an answer that was not cut but whose JSON broke partway keeps the days
  written before the break, and only the rest is asked for again.
- #36 the Building screen's "N of 7 days drafted", counted from the answer
  as it streams, once per finished day.
- #72 a stream that failed partway leaves a ledger row for what it was
  billed.
- #73 auto-draft on by default for a restaurant that turns Labor on from
  10/7/26, never an existing restaurant's setting.
No model is called anywhere here.
"""
import json
import os
import re
import sys
import types

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_versions  # noqa: E401,F401
import shift_requests, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import ai_utils
import labor
import models
import ops
import schedule_engine as se
import schedule_output as so
import schedule_prompt as sp
import schedule_rules as sr
import shift_quality as sq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = [sq._day_name(d) for d in WEEK]
_ANALYSIS = {"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
             "period_days": 0, "total_sales": 0}
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


def _src(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


class _Msg:
    def __init__(self, text, stop="end_turn", usage=None):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop
        self.stop_details = None
        self.usage = usage


def _capture(monkeypatch, answers):
    seen = []

    def fake(client, **kw):
        seen.append(kw)
        a = answers[min(len(seen), len(answers)) - 1]
        if isinstance(a, BaseException):
            raise a
        return a(kw) if callable(a) else a
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    return seen


def _gen(**kw):
    shifts = kw.pop("shifts", [])
    base = dict(roster=[("Ana", "Server"), ("Ben", "Line Cook")], week_start=WEEK[0], hourly_rate=20,
                labor_target=30)
    base.update(kw)
    return labor.generate_optimized_schedule(_ANALYSIS, shifts, **base)


def _days(schema):
    return schema["properties"]["days"]["items"]["properties"]


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
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


# ── the contract in force ──────────────────────────────────────────────────

def test_the_contract_is_schema_unless_schedule_contract_names_another(monkeypatch):
    monkeypatch.delenv("SCHEDULE_CONTRACT", raising=False)
    assert so.schedule_contract() == "schema" == so.DEFAULT_CONTRACT
    for v, want in (("compact", "compact"), (" Shape ", "shape"), ("tuples", "schema"), ("", "schema")):
        monkeypatch.setenv("SCHEDULE_CONTRACT", v)
        assert so.schedule_contract() == want
    assert so.schedule_contract("compact") == "compact"           # a generation's own wins over the env
    assert so.contract_family("plain_compact") == ("compact", "plain_compact")
    assert so.contract_family(None) == ("schema", "plain_schema")
    assert so.contract_base("csv") == "schema"


def test_a_generation_reads_its_contract_once_and_hands_it_to_every_call():
    src = _src("schedule_engine.py")
    assert "contract=_sched_out.schedule_contract()," in src          # _build_schedule_result, once
    assert "contract = _sched_out.schedule_contract(kwargs.get(\"contract\"))" in src


# ── #70 the compact contract ───────────────────────────────────────────────

def test_the_schema_contract_is_unchanged_and_compact_is_the_same_rows_with_one_letter_keys():
    kw = dict(employees=["Ana", "Ben"], roles=["Server", "Line Cook"], dates=WEEK, times=["4:00pm", "10:00pm"])
    assert json.dumps(so.contract_schema("schema", **kw), sort_keys=True) == \
        json.dumps(so.schedule_schema(**kw), sort_keys=True)
    full = _days(so.schedule_schema(**kw))["shifts"]["items"]
    short = _days(so.contract_schema("compact", **kw))["shifts"]["items"]
    assert set(short["properties"]) == {"e", "r", "s", "t", "n"}
    assert short["required"] == ["e", "r", "s", "t"] and short["additionalProperties"] is False
    for long_key, k in so.ROW_KEYS["compact"].items():
        assert short["properties"][k] == full["properties"][long_key]          # the same enums
    # The plain form (a roster too large to compile) keeps the keys, no enums.
    plain = _days(so.contract_schema("compact"))["shifts"]["items"]["properties"]
    assert plain["e"] == {"type": "string"} and plain["n"]["enum"] == list(so.NOTE_VALUES)


def test_a_compact_answer_reads_back_to_exactly_the_rows_a_schema_answer_does():
    rows = [("Ana", "Server", "4:00pm", "10:00pm", "closer"), ("Ben", "Line Cook", "10:00am", "4:00pm", None)]

    def answer(keys):
        shifts = []
        for e, r, s, t, n in rows:
            sh = {keys["employee"]: e, keys["role"]: r, keys["start"]: s, keys["end"]: t}
            if n:
                sh[keys["note"]] = n
            shifts.append(sh)
        return json.dumps({"days": [{"date": WEEK[0], "shifts": shifts}], "summary": ["Lean Monday."]})
    a = so.parse_answer(answer(so.ROW_KEYS["schema"]), contract="schema")
    b = so.parse_answer(answer(so.ROW_KEYS["compact"]), contract="compact")
    assert a["rows"] == b["rows"] and a["complete_dates"] == b["complete_dates"] == [WEEK[0]]
    assert b["rows"][0]["notes"] == "closer" and b["rows"][0]["scheduled_hours"] == "6"


def _row_chars(contract, count=1):
    """Characters one more row adds to an answer on `contract` (a shape
    slot covering `count` people, per person)."""
    keys = so.ROW_KEYS[contract]
    one = {"employee": "Jamie Lopez", "role": "Line Cook", "start": "10:30am", "end": "10:30pm", "count": count}

    def answer(n):
        rows = [{keys[k]: v for k, v in one.items() if k in keys} for _ in range(n)]
        return json.dumps({"days": [{"date": WEEK[0], so.DAY_ROWS_KEY[contract]: rows}], "summary": []},
                          separators=(",", ":"))
    return (len(answer(2)) - len(answer(1))) / float(count)


def test_each_contract_states_its_tokens_a_row_from_a_synthetic_answer():
    # The schema's 30 is the yardstick (checked in test_schedule_b2_calls);
    # each contract's figure is the schema's scaled by the characters a row
    # costs, and errs high rather than low (a call planned too short is cut).
    base = _row_chars("schema")
    for contract in ("compact", "shape"):
        want = so.ANSWER_TOKENS_PER_ROW_ESTIMATE * _row_chars(contract) / base
        got = so.ANSWER_TOKENS_PER_ROW[contract]
        assert want * 0.95 <= got <= want * 1.25, (contract, want, got)
        assert so.ANSWER_TOKENS_PER_ROW["plain_" + contract] == got
    assert so.ANSWER_TOKENS_PER_ROW["compact"] < so.ANSWER_TOKENS_PER_ROW["schema"]
    # A slot of two people costs less per row again.
    assert _row_chars("shape", count=2) < _row_chars("shape") / 1.5
    # The engine plans a call's seconds at the contract in force; its size
    # stays the schema's.
    assert se.output_tokens_per_row("compact") == so.ANSWER_TOKENS_PER_ROW["compact"]
    assert se.OUTPUT_TOKENS_PER_ROW == so.ANSWER_TOKENS_PER_ROW_ESTIMATE
    assert se._assumed_call_seconds(200, effort="medium", contract="compact") < \
        se._assumed_call_seconds(200, effort="medium", contract="schema")


def test_a_compact_call_sends_the_compact_schema_and_prompt_and_records_its_contract(monkeypatch):
    seen = _capture(monkeypatch, [_Msg(json.dumps({"days": [{"date": WEEK[1], "shifts": [
        {"e": "Ana", "r": "Server", "s": "4:00pm", "t": "10:00pm", "n": "closer"}]}], "summary": []}))])
    recorded = []
    monkeypatch.setattr(labor, "_record_schedule_call", lambda *a, **k: recorded.append(k) or 7)
    out = _gen(contract="compact", restaurant_id=None)
    schema = seen[0]["output_config"]["format"]["schema"]
    assert set(_days(schema)["shifts"]["items"]["properties"]) == {"e", "r", "s", "t", "n"}
    assert _days(schema)["shifts"]["items"]["properties"]["e"]["enum"] == ["Ana", "Ben"]
    static = seen[0]["messages"][0]["content"][0]["text"]
    assert "one-letter keys: `e` the employee" in static
    assert recorded[-1]["contract"] == "compact" and out["model_call"]["contract"] == "compact"
    assert out["contract"] == "compact"
    assert f"{WEEK[1]},Tuesday,Ana,Server,4:00pm,10:00pm,6,closer" in out["schedule_csv"]


def test_the_schema_contracts_standing_instructions_are_byte_identical():
    # The cached prefix of every live call is untouched by the new contracts.
    assert sp.static_block(True, "x", True) == sp.static_block(True, "x", True, contract="schema")
    assert sp.LAYOUT_JUDGEMENT in sp.LAYOUT
    shape = sp.static_block(True, "x", True, contract="shape")
    assert sp.SHAPE_JUDGEMENT in shape and sp.LAYOUT_JUDGEMENT not in shape
    assert "never names" in shape and "`c` how many" in shape
    assert sp.static_block(False, "x", True, contract="shape") == sp.static_block(False, "x", True)


def test_the_measured_cost_is_read_per_contract_and_never_from_a_salvaged_call(db):
    def rec(contract, outcome="ok", tokens=9000, rows=100):
        so.record_call(5, {"model": "m"}, model="m", effort="medium", contract=contract, stop_reason="end_turn",
                       outcome=outcome, seconds=200, usage={"output_tokens": tokens}, rows=rows,
                       call_kind="week", db_path=db)
    for _ in range(3):
        rec("schema", tokens=9000)
        rec("compact", tokens=6000)
    rec("schema", outcome="salvaged", tokens=60000, rows=10)
    schema_costs = so.call_costs(5, db_path=db)
    assert schema_costs["by_kind"]["week"]["tokens_per_row"] == 90.0
    assert schema_costs["calls"] == 3                                  # the salvaged call is left out
    assert so.call_costs(5, db_path=db, contract="compact")["by_kind"]["week"]["tokens_per_row"] == 60.0
    assert so.measured_tokens_per_row(5, db_path=db, contract="compact")["output_tokens_per_row"] == 60.0
    assert so.measured_tokens_per_row(6, db_path=db, contract="compact")["output_tokens_per_row"] == 24.0


# ── #71 a broken answer keeps the days before the break ────────────────────

def _broken_answer():
    good = {"date": WEEK[0], "shifts": [{"employee": "Ana", "role": "Server", "start": "4:00pm", "end": "10:00pm"}]}
    # The second day's row is malformed (a missing comma), and the answer is
    # not cut: it ends as a whole answer would.
    return ('{"days":[' + json.dumps(good) + ',{"date":"' + WEEK[1] + '","shifts":[{"employee":"Ben" "role":'
            '"Server","start":"4:00pm","end":"10:00pm"}]},{"date":"' + WEEK[2] + '","shifts":[]}],"summary":[]}')


def test_an_uncut_answer_whose_json_broke_keeps_the_days_before_the_break():
    p = so.parse_answer(_broken_answer())
    assert p["parsed"] and p["recovered"] and p["salvaged"]
    assert p["complete_dates"] == [WEEK[0]] and p["partial_dates"] == [WEEK[1]]
    assert [r["employee"] for r in p["rows"]] == ["Ana"]
    # Whole JSON is never "recovered"; garbage still yields nothing.
    assert not so.parse_answer('{"days":[],"summary":[]}')["recovered"]
    assert not so.parse_answer("not json at all")["parsed"]


def test_the_call_files_a_broken_answer_as_salvaged_and_says_so(monkeypatch):
    _capture(monkeypatch, [_Msg(_broken_answer())])
    recorded, events, marks = [], [], []
    monkeypatch.setattr(labor, "_record_schedule_call", lambda *a, **k: recorded.append(k) or 1)
    monkeypatch.setattr(ai_utils, "record_quality_event", lambda *a, **k: events.append((a, k)) or True)
    monkeypatch.setattr(ai_utils, "mark_outcome", lambda *a, **k: marks.append((a, k)) or True)
    out = _gen(restaurant_id=None)
    assert recorded[-1]["outcome"] == "salvaged" and out["json_recovered"] is True
    assert out["complete_dates"] == [WEEK[0]] and not out["truncated"]
    assert any(a[1] == "unparseable" and "broke partway" in k.get("detail", "") for a, k in events)
    assert marks and marks[0][0][1] == "unparseable"


def test_the_engine_retries_only_the_days_after_the_break_and_says_why(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: dt.datetime(2026, 10, 12))
    calls = []
    answers = [
        {"schedule_csv": HEADER + "\n" + f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6,",
         "complete_dates": [WEEK[0]], "partial_dates": [WEEK[1]], "json_recovered": True},
        {"schedule_csv": HEADER + "\n" + "\n".join(f"{d},{DAYS[WEEK.index(d)]},Ana,Server,4:00pm,10:00pm,6,"
                                                   for d in WEEK[1:]),
         "complete_dates": list(WEEK[1:])},
    ]

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kw):
        calls.append({"slice": week_slice, "notes": kw.get("call_notes") or ""})
        out = {"narrative": [], "generation_seconds": 1.0, "truncated": False, "stop_reason": "end_turn",
               "week_dates": WEEK, "hours_budget": 0}
        out.update(answers.pop(0))
        return out
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    shifts = [{"date": d, "employee": "Ana"} for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01",
                                                      "2026-10-02", "2026-10-03", "2026-10-04")]
    out = se._generate_in_parts({}, shifts, [("Ana", "Server")], {"tz_name": None})
    assert len(calls) == 2
    assert calls[1]["slice"] == WEEK[1:]                    # never the whole week again
    assert "JSON BROKE OFF BEFORE" in calls[1]["notes"] and "WROTE NO SHIFTS" not in calls[1]["notes"]
    assert out["generated_dates"] == WEEK


# ── #69 the shape contract ─────────────────────────────────────────────────

def test_a_shape_answer_is_slots_with_a_count_and_no_names():
    schema = so.contract_schema("shape", employees=["Ana"], roles=["Server"], dates=WEEK, times=["4:00pm"])
    slot = _days(schema)["slots"]["items"]
    assert set(slot["properties"]) == {"r", "s", "t", "c", "n"} and "e" not in slot["properties"]
    assert slot["properties"]["c"] == {"type": "integer"}
    text = json.dumps({"days": [{"date": WEEK[0], "slots": [
        {"r": "Server", "s": "4:00pm", "t": "10:00pm", "c": 3, "n": "closer"},
        {"r": "Server", "s": "11:00am", "t": "3:00pm", "c": 0},
        {"r": "Server", "s": "5:00pm", "t": "9:00pm", "c": 999}]}], "summary": []})
    p = so.parse_answer(text, contract="shape")
    assert len(p["rows"]) == 3 + 1 + so.SHAPE_MAX_COUNT              # a count is held to 1..the cap
    assert all(r["employee"] == "" and r["_slot"] for r in p["rows"])
    assert p["rows"][0]["notes"] == "closer" and p["complete_dates"] == [WEEK[0]]


def _cons(names, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _slot(date, role="Server", start="5:00pm", end="10:00pm"):
    return {"date": date, "day": sq._day_name(date), "employee": "", "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": "", "_slot": True}


def test_the_solver_staffs_each_slot_legally_and_leaves_out_one_nobody_can_work():
    names = ["Ann", "Bob", "Cat"]
    c = _cons(names, blocked_dates={"ann": {WEEK[0]: "on approved time off"}})
    roles = {n: "Server" for n in names}
    slots = [_slot(WEEK[0]) for _ in range(3)]
    got = se.assign_shape_slots(slots, [], c, signals={"roster": names, "roster_roles": roles},
                                roster_roles=roles, max_seconds=3)
    # Ann is off: two slots staffed by Bob and Cat, the third left out —
    # never filled illegally.
    assert sorted(r["employee"] for r in got["assigned"]) == ["Bob", "Cat"]
    assert len(got["unassigned"]) == 1
    assert not [v for v in sr.violations(got["assigned"], c) if v["hard"]]
    assert all("_slot" not in r and "_open" not in r and "_pinned" not in r for r in got["assigned"])


def test_rows_already_on_the_week_are_held_and_count_toward_each_persons_rules():
    names = ["Ann", "Bob"]
    c = _cons(names)
    roles = {n: "Server" for n in names}
    # Ann already works 5pm-10pm Monday: the Monday dinner slot is Bob's.
    held = [{"date": WEEK[0], "day": "Monday", "employee": "Ann", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "5", "notes": ""}]
    got = se.assign_shape_slots([_slot(WEEK[0])], held, c, signals={"roster": names, "roster_roles": roles},
                                roster_roles=roles, max_seconds=3)
    assert [r["employee"] for r in got["assigned"]] == ["Bob"]
    assert se.assign_shape_slots([_slot(WEEK[0])], [], None)["unassigned"]       # no rule set: nothing assigned


def test_a_shape_generation_puts_the_solved_rows_in_the_week_and_retries_an_unstaffed_day(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: dt.datetime(2026, 10, 12))
    names = ["Ann", "Bob"]
    c = _cons(names, blocked_dates={"ann": {WEEK[1]: "off"}, "bob": {WEEK[1]: "off"}})
    monkeypatch.setattr(se, "_quality_signals", lambda rid, result, **k: (
        {"roster": result.get("roster"), "roster_roles": result.get("roster_roles")}, {}))
    calls = []

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kw):
        calls.append({"slice": week_slice, "notes": kw.get("call_notes") or "", "contract": kw.get("contract")})
        dates = list(week_slice or WEEK)
        return {"schedule_csv": HEADER, "complete_dates": dates, "contract": "shape",
                "shape_rows": [_slot(d) for d in dates], "narrative": [], "generation_seconds": 1.0,
                "truncated": False, "stop_reason": "end_turn", "week_dates": WEEK, "hours_budget": 0}
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    shifts = [{"date": d, "employee": "Ann"} for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01",
                                                       "2026-10-02", "2026-10-03", "2026-10-04")]
    out = se._generate_in_parts({}, shifts, [("Ann", "Server"), ("Bob", "Server")],
                                {"tz_name": None, "contract": "shape", "rules_constraints": c})
    assert calls[0]["contract"] == "shape"
    rows = [r for r in out["schedule_csv"].split("\n")[1:] if r]
    assert {r.split(",")[0] for r in rows} == set(WEEK) - {WEEK[1]}
    assert all(r.split(",")[2] in names for r in rows)
    # Tuesday's slot nobody could work: asked once more, told why, then left unwritten.
    assert calls[1]["slice"] == [WEEK[1]] and "COULD LEGALLY WORK THE SLOTS" in calls[1]["notes"]
    assert [g["date"] for g in out["unwritten_dates"]] == [WEEK[1]]


def test_the_solver_counts_an_open_slot_toward_the_days_close():
    names = ["Ann"]
    c = _cons(names)
    import schedule_solver as ss
    prob = ss.Problem([dict(_slot(WEEK[0]), _open=True, employee="")], c,
                      signals={"roster": names, "roster_roles": {"Ann": "Server"}})
    assert prob.units and prob.units[0].closing


def test_the_shape_contract_is_an_arm_of_the_eval(monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import schedule_model_eval as ev
    assert ev.contract_of("rerender:shape") == "shape" and ev.contract_of("rerender") is None
    with pytest.raises(ValueError):
        ev.contract_of("rerender:tuples")
    arms = ev.tier_arms(["T3"], ["rerender", "rerender:compact", "rerender:shape"])
    assert [a["contract"] for a in arms] == [None, "compact", "shape"]
    # A stored shape call reads back to nameless rows; the eval staffs them
    # against the week's Constraints before scoring, and counts what it could not.
    call = {"request": {"output_config": {"format": {"schema": {}}}}, "dates": [WEEK[0]], "contract": "shape"}
    got = ev.rows_of_answer(json.dumps({"days": [{"date": WEEK[0], "slots": [
        {"r": "Server", "s": "5:00pm", "t": "10:00pm", "c": 3}]}], "summary": []}), call)
    assert len(got["rows"]) == 3 and not any(r["employee"] for r in got["rows"])
    names = ["Ann", "Bob"]
    ctx = {"restaurant_id": 1, "constraints": _cons(names), "roster_roles": {n: "Server" for n in names}}
    rows, unstaffed = ev.assign_open_slots(ctx, got["rows"], signals={"roster": names,
                                                                      "roster_roles": ctx["roster_roles"]})
    assert sorted(r["employee"] for r in rows) == names and unstaffed == 1
    # A rerender arm answers on its contract, else the stored call's own.
    seen = {}

    def fake_gen(**inputs):
        seen.update(inputs)
        return {"schedule_csv": HEADER, "shape_rows": [_slot(WEEK[0])]}
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake_gen)
    res = ev._rerender({"inputs": {"roster": []}, "contract": "plain_compact"},
                       {"model": "m", "contract": None}, lambda r: None, 0)
    assert seen["contract"] == "compact" and res["rows"][0]["employee"] == ""
    ev._rerender({"inputs": {"roster": []}}, {"model": "m", "contract": "shape"}, lambda r: None, 0)
    assert seen["contract"] == "shape"


# ── #36 the days drafted, as the answer streams ────────────────────────────

class _Delta:
    def __init__(self, text):
        self.type = "content_block_delta"
        self.delta = types.SimpleNamespace(type="text_delta", text=text)


class _FakeStream:
    def __init__(self, pieces, final, fail_after=None):
        self.pieces, self.final, self.fail_after = pieces, final, fail_after
        self.current_message_snapshot = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for i, p in enumerate(self.pieces):
            if self.fail_after is not None and i >= self.fail_after:
                raise RuntimeError("overloaded mid-stream")
            yield types.SimpleNamespace(type="thinking_delta")
            yield _Delta(p)

    def get_final_message(self):
        return self.final


class _FakeClient:
    def __init__(self, streams):
        self.streams = list(streams)
        self.messages = types.SimpleNamespace(stream=lambda **kw: self.streams.pop(0))
        self.timeout = 360.0


def _week_answer(dates):
    return json.dumps({"days": [{"date": d, "shifts": [{"employee": "Ana", "role": "Server", "start": "4:00pm",
                                                         "end": "10:00pm"}]} for d in dates], "summary": []})


def _pieces(text, size=7):
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_the_stream_reports_each_finished_day_once_and_the_call_is_unchanged():
    text = _week_answer(WEEK[:3])
    final = _Msg(text)
    reports = []
    client = so.DayWatch(_FakeClient([_FakeStream(_pieces(text), final)]), reports.append, dates=WEEK)
    with client.messages.stream(model="m") as s:
        assert s.get_final_message() is final                 # read through, untouched
    # Day 1 is finished when day 2 starts, day 2 when day 3 starts; day 3 is
    # the engine's to count once the answer is parsed.
    assert reports == [[WEEK[0]], [WEEK[0], WEEK[1]]]
    assert client.timeout == 360.0                             # the wrapped client's own attributes


def test_a_retried_stream_starts_its_text_again_and_a_failing_watcher_never_fails_the_call():
    text = _week_answer(WEEK[:3])
    reports = []
    client = so.DayWatch(_FakeClient([_FakeStream(_pieces(text), None, fail_after=12),
                                      _FakeStream(_pieces(text), _Msg(text))]), reports.append, dates=WEEK)
    with pytest.raises(RuntimeError):
        with client.messages.stream() as s:
            s.get_final_message()
    with client.messages.stream() as s:
        s.get_final_message()
    assert reports[-1] == [WEEK[0], WEEK[1]]

    def boom(_dates):
        raise RuntimeError("store down")
    quiet = so.DayWatch(_FakeClient([_FakeStream(_pieces(text), _Msg(text))]), boom, dates=WEEK)
    with quiet.messages.stream() as s:
        assert s.get_final_message().stop_reason == "end_turn"


def test_create_with_retry_reads_a_watched_stream_and_the_deadline_still_cuts_it(monkeypatch):
    monkeypatch.setattr(ai_utils, "_log_usage_safe", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_record_trace_safe", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    text = _week_answer(WEEK[:2])
    reports = []
    client = so.DayWatch(_FakeClient([_FakeStream(_pieces(text), _Msg(text))]), reports.append, dates=WEEK)
    msg = ai_utils.create_with_retry(client, readiness=None, stream=True, model="claude-x", max_tokens=10,
                                     messages=[], deadline=__import__("time").time() + 60)
    assert msg.stop_reason == "end_turn" and reports == [[WEEK[0]]]


def test_the_clock_writes_the_days_drafted_once_per_new_day_and_a_pending_poll_returns_them(db, monkeypatch):
    rid = models.create_restaurant(models.Restaurant(name="Progress Grill", owner_email="p@x.test"), db_path=db)
    job, _joined = ops.claim_async_job("job-progress", "schedule", rid)
    writes = []
    real = ops.set_async_job_progress
    monkeypatch.setattr(ops, "set_async_job_progress", lambda j, p: (writes.append(p), real(j, p)))
    clock = se.GenerationClock(job)
    clock.days_streamed([WEEK[0]])                             # before a plan: nothing to count against
    clock.plan_days(WEEK)
    clock.days_streamed([WEEK[0]])
    clock.days_streamed([WEEK[0]])                             # no news, no write
    clock.days_streamed([WEEK[0], WEEK[1], "2026-01-01"])      # a date outside the plan is never counted
    clock.plan_days(WEEK[:2])                                  # the gate's rewrite never re-plans the count
    assert [w["days_drafted"] for w in writes] == [0, 1, 2] and writes[-1]["days_total"] == 7
    got = ops.read_async_job(job, restaurant_id=rid)
    assert got["status"] == "pending" and got["progress"] == {"days_total": 7, "days_drafted": 2,
                                                              "dates_drafted": [WEEK[0], WEEK[1]]}
    ops.finish_async_job(job, "done", {"ok": True})
    ops.set_async_job_progress(job, {"days_total": 7, "days_drafted": 7})   # a finished job is never rewritten
    assert "progress" not in ops.read_async_job(job, restaurant_id=rid)


def test_both_status_routes_and_both_clients_carry_the_days_drafted():
    assert '"progress": job.get("progress")' in _src("client_api.py")
    assert "progress=job.get(\"progress\")" in _src("mobile_api.py")
    html = _src("templates/dashboard.html")
    assert "_schedProgress = pollData.progress || null;" in html
    assert "el.innerHTML = schedDaysLine(_schedProgress) + so + line" in html
    fn = html[html.index("function schedDaysLine(p)"):html.index("function schedDaysLine(p)") + 400]
    assert "' drafted</b>" in fn and "=>" not in fn and "let " not in fn
    swift = _src("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift")
    assert "case progress" in swift and "generationProgress = p" in swift
    assert "viewModel.generationProgress" in _src("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift")


def test_a_job_generation_watches_its_stream_and_a_direct_call_does_not(monkeypatch):
    clients = []

    def fake(client, **kw):
        clients.append(client)
        return _Msg(_week_answer([WEEK[1]]))
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: _FakeClient([]))
    monkeypatch.setattr(labor, "_record_schedule_call", lambda *a, **k: None)
    _gen(restaurant_id=None)
    assert not isinstance(clients[-1], so.DayWatch)
    token = se._CLOCK.set(se.GenerationClock(None))
    try:
        _gen(restaurant_id=None)
        assert not isinstance(clients[-1], so.DayWatch)            # no job id: nobody polls
    finally:
        se._CLOCK.reset(token)
    monkeypatch.setattr(ops, "set_async_job_deadline", lambda *a, **k: None)
    token = se._CLOCK.set(se.GenerationClock("job-x"))
    try:
        _gen(restaurant_id=None)
        assert isinstance(clients[-1], so.DayWatch)
    finally:
        se._CLOCK.reset(token)


# ── #72 a stream cut partway is billed and filed ───────────────────────────

class _Usage:
    def __init__(self, i, o, cw=0, cr=0):
        self.input_tokens, self.output_tokens = i, o
        self.cache_creation_input_tokens, self.cache_read_input_tokens = cw, cr


class _OverloadedMidStream(Exception):
    status_code = 529


def test_a_stream_cut_partway_leaves_a_ledger_row_before_the_retry(db, monkeypatch):
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai_utils, "_RETRYABLE", (_OverloadedMidStream,) + tuple(ai_utils._RETRYABLE))
    partial = types.SimpleNamespace(usage=_Usage(12000, 1, cw=3000), id="msg_cut", content=[])
    whole = types.SimpleNamespace(usage=_Usage(12000, 900, cr=3000), id="msg_ok", stop_reason="end_turn",
                                  content=[types.SimpleNamespace(type="text", text="{}")])

    class Cut(_FakeStream):
        def get_final_message(self):
            raise _OverloadedMidStream("overloaded_error mid-stream")
    client = _FakeClient([Cut([], partial), _FakeStream([], whole)])
    msg = ai_utils.create_with_retry(client, readiness=None, stream=True, model="claude-sonnet-5-5",
                                     max_tokens=10, messages=[], restaurant_id=None, action="labor_schedule")
    assert msg is whole
    conn = models.get_conn(db)
    try:
        rows = [dict(r) for r in conn.execute("SELECT outcome, reason, input_tokens, output_tokens, "
                                              "cache_write_tokens, cost_usd, call_id FROM ai_usage "
                                              "WHERE action='labor_schedule' ORDER BY id").fetchall()]
    finally:
        conn.close()
    assert [(r["outcome"], r["reason"]) for r in rows] == [("error", "stream_cut"), ("ok", None)]
    assert rows[0]["input_tokens"] == 12000 and rows[0]["cache_write_tokens"] == 3000 and rows[0]["cost_usd"] > 0
    assert rows[0]["call_id"] == rows[1]["call_id"]                # one call, its attempts
    # Re-filing the answer leaves the cut attempt's own verdict alone.
    ai_utils.mark_outcome(msg, "unparseable", db_path=db)
    conn = models.get_conn(db)
    try:
        got = [r[0] for r in conn.execute("SELECT outcome FROM ai_usage WHERE action='labor_schedule' "
                                          "ORDER BY id").fetchall()]
    finally:
        conn.close()
    assert got == ["error", "unparseable"]


def test_a_failure_before_the_stream_started_writes_no_partial_row(db):
    assert ai_utils._log_stream_cut_safe(None, "m", None, "x", 1, 1, "c", {}) is None
    empty = types.SimpleNamespace(usage=_Usage(0, 0))
    assert ai_utils._log_stream_cut_safe(empty, "m", None, "x", 1, 1, "c", {}) is None


# ── #73 auto-draft on by default for Labor ─────────────────────────────────

def _auto(db, rid):
    conn = models.get_conn(db)
    try:
        return conn.execute("SELECT auto_draft_schedule FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    finally:
        conn.close()


def test_a_new_restaurant_with_labor_starts_with_auto_draft_on_and_a_demo_never_does(db):
    on = models.create_restaurant(models.Restaurant(name="New Labor", owner_email="a@x.test"), db_path=db)
    off = models.create_restaurant(models.Restaurant(name="Reviews Only", owner_email="b@x.test", module_labor=0),
                                   db_path=db)
    demo = models.create_restaurant(models.Restaurant(name="Demo", owner_email="c@x.test", is_demo=1), db_path=db)
    assert (_auto(db, on), _auto(db, off), _auto(db, demo)) == (1, 0, 0)
    # The draft day is the spread default until the owner picks one.
    r = models.get_restaurant(on, db_path=db)
    assert models.auto_draft_weekday(r) == models.default_auto_draft_weekday(on)


def test_switching_labor_on_turns_auto_draft_on_only_for_a_restaurant_made_since_and_never_a_choice(db):
    new = models.create_restaurant(models.Restaurant(name="Upgrader", owner_email="d@x.test", module_labor=0),
                                   db_path=db)
    models.update_restaurant(new, {"module_labor": 1}, db_path=db)
    assert _auto(db, new) == 1
    # An existing restaurant (made before the default) keeps its setting.
    old = models.create_restaurant(models.Restaurant(name="Old Grill", owner_email="e@x.test", module_labor=0,
                                                     created_at="2026-09-01T09:00:00"), db_path=db)
    models.update_restaurant(old, {"module_labor": 1}, db_path=db)
    assert _auto(db, old) == 0
    # Somebody who already chose: their choice stands.
    chose = models.create_restaurant(models.Restaurant(name="Chooser", owner_email="f@x.test", module_labor=0),
                                     db_path=db)
    models.update_restaurant(chose, {"auto_draft_schedule": 1}, db_path=db)
    models.update_restaurant(chose, {"auto_draft_schedule": 0}, db_path=db)
    models.update_restaurant(chose, {"module_labor": 1}, db_path=db)
    assert _auto(db, chose) == 0
    # A write that names auto-draft itself is never overridden, and turning
    # Labor off changes nothing.
    both = models.create_restaurant(models.Restaurant(name="Both", owner_email="g@x.test", module_labor=0),
                                    db_path=db)
    models.update_restaurant(both, {"module_labor": 1, "auto_draft_schedule": 0}, db_path=db)
    assert _auto(db, both) == 0
    models.update_restaurant(new, {"module_labor": 0}, db_path=db)
    assert _auto(db, new) == 1


def test_the_default_is_said_where_the_owner_switches_it():
    assert "On by default once Labor is turned on" in _src("templates/dashboard.html")
    assert "On by default once Labor is turned on" in \
        _src("ios/CavnarAI/CavnarAI/Features/Account/AccountAutomationView.swift")
