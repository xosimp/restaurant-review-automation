"""The schedule call's output contract (schedule audit 10/3/26: PR-11, PR-12,
PR-13, P-35, PR-15, PR-28, PR-29, PR-30).

- PR-12: the schema is built for each generation — the roster, its roles,
  the week's dates and the clock times are enums — and is the same for
  every slice of one generation.
- PR-13 / P-35: rows are grouped by date and carry no weekday or hours
  column (the code derives both); a row costs about half the output.
- PR-15: a note is one of a fixed list, the prompt says the employee reads
  it, and no engine mark reaches an employee's schedule.
- PR-28: a structured answer is never read as CSV; a truncated answer keeps
  every complete day; a refusal is said, never retried as CSV; only a 400
  naming the schema falls back.
- PR-29 / PR-30: a model whose thinking is always on is never left at its
  default effort by a call that named none; running into the context
  window is truncation.
- PR-11: what the finished week does not meet is read from its rows and
  saved with the review; the prompt stops asking a three-bullet summary to
  carry it.
"""
import json
import re
import sys
import types

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import ai_utils
import labor
import models
import schedule_engine as se
import schedule_output as so
import schedule_rules as sr
from schedule_rules import Constraints

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
_ANALYSIS = {"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
             "period_days": 0, "total_sales": 0}


class _Msg:
    def __init__(self, text, stop="end_turn", usage=None, stop_details=None):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop
        self.stop_details = stop_details
        self.usage = usage


def _capture(monkeypatch, answers):
    """labor's model call replaced: each call is recorded and answered in
    turn from `answers` (a message, an exception to raise, or a callable)."""
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


def _schema(kw):
    return kw["output_config"]["format"]["schema"]


def _shift_props(schema):
    return schema["properties"]["days"]["items"]["properties"]["shifts"]["items"]


def _answer(days, summary=("Kept Friday lean.",)):
    return json.dumps({"days": [{"date": d, "shifts": s} for d, s in days], "summary": list(summary)},
                      separators=(",", ":"))


def _gen(**kw):
    shifts = kw.pop("shifts", [])
    base = dict(roster=[("Ana", "Server"), ("Ben", "Line Cook")], week_start=WEEK[0], hourly_rate=20,
                labor_target=30)
    base.update(kw)
    return labor.generate_optimized_schedule(_ANALYSIS, shifts, **base)


# ── PR-12: the schema is built for the generation ─────────────────────────

def test_the_schema_enumerates_the_roster_its_roles_the_weeks_dates_and_clock_times(monkeypatch):
    seen = _capture(monkeypatch, [_Msg(_answer([]))])
    history = [{"employee": "Ana", "role": "Bartender", "date": "2026-09-28", "shift_start": "4:00pm",
                "shift_end": "10:00pm", "scheduled_hours": 6}]
    labor.generate_optimized_schedule(
        _ANALYSIS, history, roster=[("Ana", "Server"), ("Ben", "Line Cook")], week_start=WEEK[0],
        hourly_rate=20, labor_target=30, closed_dates=[WEEK[0]], held_roles={"ben": ["expo"]},
        close_times={"Friday": "9:50pm"}, hours_notes="Hosts arrive 4:45pm")
    schema = _schema(seen[0])
    shift = _shift_props(schema)
    props = shift["properties"]
    assert props["employee"]["enum"] == ["Ana", "Ben"]
    assert set(props["role"]["enum"]) == {"Server", "Line Cook", "Bartender", "expo"}
    date_enum = schema["properties"]["days"]["items"]["properties"]["date"]["enum"]
    assert date_enum == WEEK[1:]                                   # the closed Monday is not writable
    times = props["start"]["enum"]
    assert times == props["end"]["enum"]
    assert {"12:00am", "4:00pm", "4:15pm", "11:45pm", "9:50pm", "4:45pm"} <= set(times)
    assert not [t for t in times if not re.fullmatch(r"\d{1,2}:\d{2}(am|pm)", t)]   # no 24-hour form
    # the derived columns are gone and the note is a fixed list, optional
    assert set(props) == {"employee", "role", "start", "end", "note"}
    assert "note" not in shift["required"] and props["note"]["enum"] == list(so.NOTE_VALUES)
    assert shift["additionalProperties"] is False


def test_every_slice_of_one_generation_answers_against_the_same_schema(monkeypatch):
    seen = _capture(monkeypatch, [_Msg(_answer([]))])
    _gen(week_slice=WEEK[:3], generation_id="g1")
    _gen(week_slice=WEEK[3:], generation_id="g1",
         prior_rows=[{"date": WEEK[0], "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
                      "shift_end": "10:00pm", "scheduled_hours": "6"}])
    assert json.dumps(_schema(seen[0]), sort_keys=True) == json.dumps(_schema(seen[1]), sort_keys=True)


def test_a_department_part_enumerates_only_its_own_people(monkeypatch):
    seen = _capture(monkeypatch, [_Msg(_answer([]))])
    _gen(roster=[("Ben", "Line Cook"), ("Cy", "Dishwasher")])
    assert _shift_props(_schema(seen[0]))["properties"]["employee"]["enum"] == ["Ben", "Cy"]


# ── PR-13 / P-35: grouped rows, derived columns ───────────────────────────

def test_grouped_rows_become_the_pipelines_csv_with_the_day_and_hours_worked_out(monkeypatch):
    _capture(monkeypatch, [_Msg(_answer([
        (WEEK[0], [{"employee": "Ana", "role": "Server", "start": "11:00am", "end": "3:30pm", "note": "opener"}]),
        (WEEK[1], [{"employee": "Ben", "role": "Line Cook", "start": "5:00pm", "end": "1:00am", "note": "closer"},
                   {"employee": "Ana", "role": "Server", "start": "4:00pm", "end": "9:00pm"}]),
    ]))])
    out = _gen()
    lines = out["schedule_csv"].split("\n")
    assert lines[0] == "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
    assert lines[1:] == ["2026-10-12,Monday,Ana,Server,11:00am,3:30pm,4.5,opener",
                         "2026-10-13,Tuesday,Ben,Line Cook,5:00pm,1:00am,8,closer",
                         "2026-10-13,Tuesday,Ana,Server,4:00pm,9:00pm,5,"]
    assert out["structured"] is True and out["complete_dates"] == WEEK[:2]
    assert out["summary"] == ["Kept Friday lean."]


def test_a_slice_keeps_only_its_own_dates(monkeypatch):
    _capture(monkeypatch, [_Msg(_answer([
        (WEEK[0], [{"employee": "Ana", "role": "Server", "start": "11:00am", "end": "3:00pm"}]),
        (WEEK[4], [{"employee": "Ana", "role": "Server", "start": "11:00am", "end": "3:00pm"}]),
    ]))])
    out = _gen(week_slice=WEEK[3:])
    assert [ln.split(",")[0] for ln in out["schedule_csv"].split("\n")[1:]] == [WEEK[4]]


def test_a_row_costs_about_half_what_the_old_contract_did():
    """The measured cost of the format: the same week's rows written in the
    old contract (eight keys a row, the weekday and hours spelled out) and
    in this one. Output tokens follow the characters written; the old
    contract's own figure was ~60 tokens a row."""
    rows = [("Maria Lopez", "Server", "10:30am", "4:00pm", "opener"), ("Ben Ortiz", "Line Cook", "4:00pm", "11:00pm", ""),
            ("Ana Kim", "Bartender", "5:00pm", "1:00am", "closer"), ("Dee Fox", "Host", "4:30pm", "9:30pm", "")] * 8
    old = json.dumps({"shifts": [{"date": WEEK[i % 7], "day": "Wednesday", "employee": e, "role": r,
                                  "shift_start": s, "shift_end": t, "scheduled_hours": 6.5, "notes": n or "flex"}
                                 for i, (e, r, s, t, n) in enumerate(rows)], "summary": []}, separators=(",", ":"))
    new = json.dumps({"days": [{"date": d, "shifts": [{"employee": e, "role": r, "start": s, "end": t,
                                                        **({"note": n} if n else {})}
                                                       for i, (e, r, s, t, n) in enumerate(rows) if WEEK[i % 7] == d]}
                               for d in WEEK], "summary": []}, separators=(",", ":"))
    assert len(new) / len(old) < 0.6, (len(new), len(old))
    assert so.ANSWER_TOKENS_PER_ROW_ESTIMATE <= so.OLD_TOKENS_PER_ROW * 0.6


def test_the_call_reports_what_a_row_cost(monkeypatch):
    usage = types.SimpleNamespace(input_tokens=18000, output_tokens=9000, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)
    _capture(monkeypatch, [_Msg(_answer([(WEEK[0], [{"employee": "Ana", "role": "Server", "start": "11:00am",
                                                     "end": "3:00pm"}] * 3)]), usage=usage)])
    mc = _gen()["model_call"]
    assert mc["rows"] == 3 and mc["output_tokens"] == 9000 and mc["output_tokens_per_row"] == 3000.0
    assert mc["contract"] == "schema" and mc["stop_reason"] == "end_turn"


# ── PR-15: notes the employee reads ───────────────────────────────────────

def test_the_prompt_says_the_employee_reads_the_note_and_lists_the_only_notes(monkeypatch):
    seen = _capture(monkeypatch, [_Msg(_answer([]))])
    _gen()
    prompt = seen[0]["messages"][0]["content"]
    assert "printed on that employee's own schedule and read by them" in prompt
    assert "opener, closer, staggered start, training shift, standby" in prompt
    assert "Never put a rating or score, reliability or attendance, pay, performance" in prompt
    assert "one brief phrase per shift" not in prompt and "YoY match - high volume" not in prompt


def test_the_csv_contract_keeps_only_a_note_from_the_list(monkeypatch):
    csv_answer = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                  f"{WEEK[0]},Monday,Ana,Server,11:00am,3:00pm,4,still developing - pair with Ben\n"
                  f"{WEEK[0]},Monday,Ben,Line Cook,4:00pm,10:00pm,6,Closer\n---SUMMARY---\n- ok")
    _capture(monkeypatch, [_Msg(csv_answer)])
    out = _gen(structured=False)
    notes = [ln.split(",", 7)[7] for ln in out["schedule_csv"].split("\n")[1:]]
    assert notes == ["", "closer"]


@pytest.mark.parametrize("note,staff_sees", [
    ("closer (auto-capped to close time)", "closer"),
    ("opener (extended — PAR hours top-up)", "opener"),
    ("extended — PAR hours top-up", ""),
    ("server (trimmed — over the 7-server cap)", "server"),
    ("added — coverage top-up", ""),
    ("closer — NEEDS REVIEW: two shifts overlap", "closer"),
    ("opener — Cavnar AI: solver put Ana here (was Ben)", "opener"),
    ("Cavnar: Server strength was under target (swapped with Hana W.)", ""),
    ("training shift (was Jo — days off)", "training shift"),
    ("opener; staggered start", "opener; staggered start"),
    ("bring the keys", "bring the keys"),
])
def test_no_engine_mark_reaches_an_employees_schedule(note, staff_sees):
    assert labor.staff_facing_note(note) == staff_sees
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                f"{WEEK[0]},Monday,Ana,Server,11:00am,3:00pm,4,{note.replace(',', ';')}")
    assert labor.employee_shifts_from_csv(csv_text, "Ana")[0]["notes"] == staff_sees


# ── PR-28: never CSV, salvage, refusal, the fallback ──────────────────────

def test_a_structured_answer_is_never_read_as_csv(monkeypatch):
    marked = []
    monkeypatch.setattr(ai_utils, "mark_outcome", lambda m, o, reason=None, db_path=None: marked.append(o))
    _capture(monkeypatch, [_Msg("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                                f"{WEEK[0]},Monday,Ana,Server,11:00am,3:00pm,4,x\n---SUMMARY---\n- a")])
    out = _gen()
    assert out["schedule_csv"].split("\n")[1:] in ([], [""]) and out["summary"] == []
    assert out["structured"] is False and marked == ["unparseable"]


def test_a_truncated_answer_keeps_every_complete_day_and_drops_the_day_it_was_cut_in(monkeypatch):
    full = _answer([(WEEK[0], [{"employee": "Ana", "role": "Server", "start": "11:00am", "end": "3:00pm"},
                               {"employee": "Ben", "role": "Line Cook", "start": "4:00pm", "end": "10:00pm"}]),
                    (WEEK[1], [{"employee": "Ana", "role": "Server", "start": "11:00am", "end": "3:00pm"},
                               {"employee": "Ben", "role": "Line Cook", "start": "4:00pm", "end": "10:00pm"}])])
    cut = full[:full.rindex('"start":"4:00pm"')]                  # inside the second day's second row
    _capture(monkeypatch, [_Msg(cut, stop="max_tokens")])
    out = _gen()
    assert out["truncated"] is True and out["salvaged"] is True
    assert out["complete_dates"] == [WEEK[0]] and out["partial_dates"] == [WEEK[1]]
    assert [ln.split(",")[0] for ln in out["schedule_csv"].split("\n")[1:]] == [WEEK[0], WEEK[0]]


def test_running_into_the_context_window_is_truncation(monkeypatch):
    _capture(monkeypatch, [_Msg('{"days":[{"date":"2026-10-12","shifts":[', stop="model_context_window_exceeded")])
    out = _gen()
    assert out["truncated"] is True and out["partial_dates"] == [WEEK[0]]
    assert ai_utils.outcome_of(types.SimpleNamespace(stop_reason="model_context_window_exceeded")) == "truncated"


def test_a_refusal_is_said_and_never_retried_as_csv(monkeypatch):
    seen = _capture(monkeypatch, [_Msg("", stop="refusal", stop_details=types.SimpleNamespace(category="cyber"))])
    with pytest.raises(se.ScheduleGenerationError) as err:
        _gen()
    assert len(seen) == 1
    assert "declined" in str(err.value) and "nothing was saved" in str(err.value)
    assert err.value.stop_reason == "refusal" and err.value.category == "cyber"


def _bad_request(message):
    import anthropic
    import httpx
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(message, response=httpx.Response(400, request=req),
                                     body={"type": "error", "error": {"type": "invalid_request_error",
                                                                      "message": message}})


@pytest.mark.parametrize("error", [
    RuntimeError("the output format went wrong"),
    lambda: _bad_request("messages.0.content: invalid format for this field"),
    lambda: _bad_request("output_config.effort: high is not supported with this thinking setting"),
])
def test_an_error_that_merely_says_format_is_raised_not_retried_as_csv(monkeypatch, error):
    exc = error() if callable(error) else error
    seen = _capture(monkeypatch, [exc])
    with pytest.raises(type(exc)):
        _gen()
    assert len(seen) == 1


def test_a_refused_schema_is_asked_again_as_the_plain_shape_before_csv(monkeypatch):
    ok = _Msg(_answer([(WEEK[0], [{"employee": "Ana", "role": "Server", "start": "11:00am", "end": "3:00pm"}])]))
    seen = _capture(monkeypatch, [_bad_request("output_config.format.schema: the grammar is too large"), ok])
    out = _gen()
    assert len(seen) == 2
    first, second = _shift_props(_schema(seen[0])), _shift_props(_schema(seen[1]))
    assert "enum" in first["properties"]["employee"] and "enum" not in second["properties"]["employee"]
    assert out["model_call"]["contract"] == "plain_schema" and out["schedule_csv"].count("\n") == 1


# ── PR-29 / PR-30: every model a *_MODEL override could pick ──────────────

def _quiet(monkeypatch):
    for name in ("_log_usage_safe", "_record_trace_safe", "_breaker_record", "_log_failure_safe"):
        monkeypatch.setattr(ai_utils, name, lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "_breaker_check", lambda *a, **k: None)


@pytest.mark.parametrize("model,kwargs,thinking,effort", [
    ("claude-opus-5-5", {}, None, "low"),
    ("claude-fable-5-1", {}, None, "low"),
    ("claude-opus-5-5", {"output_config": {"effort": "high"}}, None, "high"),
    ("claude-opus-5-5", {"thinking": {"type": "adaptive"}}, {"type": "adaptive"}, None),
    ("claude-sonnet-5-5", {}, {"type": "between_tools"}, None),
    ("claude-sonnet-5", {}, {"type": "disabled"}, None),
])
def test_a_call_that_names_no_effort_never_runs_an_always_thinking_model_at_its_default(
        monkeypatch, model, kwargs, thinking, effort):
    """A call site written for 500 tokens and moved to Opus 5.5 by an
    override thought at medium effort and could answer nothing."""
    _quiet(monkeypatch)
    sent = {}
    client = types.SimpleNamespace(messages=types.SimpleNamespace(
        create=lambda **kw: sent.update(kw) or types.SimpleNamespace(content=[], stop_reason="end_turn",
                                                                    usage=None)))
    ai_utils.create_with_retry(client, model=model, max_tokens=500, messages=[{"role": "user", "content": "x"}],
                               **kwargs)
    assert sent.get("thinking") == thinking
    assert (sent.get("output_config") or {}).get("effort") == effort


def test_no_call_site_sends_a_setting_a_55_model_refuses():
    """Every model_for() call path survives a *_MODEL override to a 5.5
    model: no explicit disabled thinking or thinking budget (400 on Opus
    5.5 and Sonnet 5.5), no forced tool_choice (400 on both), no assistant
    prefill (400 since the 4.6 family)."""
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = [f for f in subprocess.check_output(["git", "ls-files", "*.py"], cwd=root, text=True).split()
             if not f.startswith(("tests/", "scripts/"))]
    bad = []
    for f in files:
        src = open(os.path.join(root, f), encoding="utf-8").read()
        if f != "ai_utils.py" and re.search(r"""["']type["']\s*:\s*["']disabled["']""", src):
            bad.append((f, "disabled thinking"))
        if re.search(r"budget_tokens\s*[=:]", src):
            bad.append((f, "budget_tokens"))
        if re.search(r"""tool_choice\s*=\s*\{\s*["']type["']\s*:\s*["'](any|tool)["']""", src):
            bad.append((f, "forced tool_choice"))
    assert not bad, bad


def test_the_schedule_on_sonnet_55_thinks_adaptively_at_high_effort_streamed(monkeypatch):
    monkeypatch.setenv("SCHEDULE_MODEL", "claude-sonnet-5-5")
    seen = _capture(monkeypatch, [_Msg(_answer([]))])
    _gen()
    kw = seen[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kw["output_config"]["effort"] == "high" and kw["stream"] is True
    assert kw["max_tokens"] == labor.SCHEDULE_MAX_TOKENS_THINKING


# ── PR-11: what the week misses, from its rows ────────────────────────────

def test_the_prompt_no_longer_asks_the_summary_to_carry_what_the_week_misses(monkeypatch):
    seen = _capture(monkeypatch, [_Msg(_answer([]))])
    shifts = []
    for d in ("2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07",
              "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05", "2026-07-27", "2026-07-20"):
        shifts.append({"employee": "Ana", "role": "Server", "date": d, "shift_start": "4:00pm",
                       "shift_end": "10:00pm", "scheduled_hours": 6, "actual_hours": 0})
    _gen(shifts=shifts, operational_scores={"Ana": 4}, strength_thresholds={"Server": 6},
         leader_rules=[{"role": "Server", "count": 1, "min_score": 4, "days": ["Friday"]}],
         projected_revenue_override=40000)
    prompt = seen[0]["messages"][0]["content"]
    for phrase in ("say so plainly in your summary", "say which in the summary", "Say in the summary which days",
                   "note in the summary that a standby", "name each one you applied"):
        assert phrase not in prompt, phrase
    assert "checked in code and shown to the owner" in prompt
    import staffing_signals
    block = staffing_signals.soft_block([{"day": "Friday", "date": WEEK[4], "daypart": "night", "role": "Server",
                                          "text": "+1 Server — complaints about waits", "source": "reviews"}])
    assert "in the summary" not in block and "shown which" in block


def _c(**kw):
    c = Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(sr.DAYS))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(name, role, start, end, date=WEEK[4], hours=None):
    return {"date": date, "day": "Friday", "employee": name, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours) if hours is not None else "", "notes": ""}


def test_unmet_items_list_every_miss_of_the_finished_week_from_its_rows():
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann", "Bob", "Cy"], active={"max", "ann", "bob", "cy"},
           role_floors={"Server": {"night": 3}}, hours_limits={"cy": (30, 40)}, employment={"cy": "full"})
    rows = [_row("Ann", "Server", "4:00pm", "11:00pm", hours=7), _row("Bob", "Server", "4:00pm", "11:00pm", hours=7),
            _row("Max", "Manager", "6:00pm", "11:00pm", hours=5)]
    viols = sr.violations(rows, c)
    quality = {"checked": True, "shifts": [
        {"scored": True, "date": WEEK[4], "day": "Friday", "daypart": "night", "dimensions": [
            {"key": "coverage", "facts": {"short": {"Server": 1, "Bartender": 1},
                                          "gaps": ["Server short 1 of 3", "Bartender short 1 of 1"]}},
            {"key": "operational_strength", "facts": {"shortfalls": [{"role": "Server", "strength": 5, "target": 8}]}},
            {"key": "leadership", "facts": {"misses": [{"rule": "1 Server scoring 4 or above", "found": 0}],
                                            "profile_leader_missing": False}}]}]}
    asks = [{"day": "Friday", "date": WEEK[4], "daypart": "night", "role": "Server", "source": "reviews",
             "applied": False, "scheduled": 2, "typical": 2}]
    items = so.unmet_items(rows, constraints=c, violations=viols, quality=quality, soft_requirements=asks,
                           hours_budget=10, station_gaps=[{"date": WEEK[4], "day": "Friday", "daypart": "night",
                                                           "station": "Grill"}],
                           owner_rules_unchecked=["Two hosts on patio nights"])
    kinds = [i["kind"] for i in items]
    assert kinds[0] == "manager"                                  # the owner's highest rule leads
    mgr = items[0]
    assert mgr["date"] == WEEK[4] and "4:00pm to 6:00pm" in mgr["why"] and mgr["minutes"] == 120
    floor = next(i for i in items if i["kind"] == "floor")
    assert floor["what"] == "Your Server floor" and floor["daypart"] == "night"
    # the Server shortage is said once (as the floor); the Bartender one stays
    coverage = [i for i in items if i["kind"] == "coverage"]
    assert [i["what"] for i in coverage] == ["Bartender on Friday dinner/night"]
    assert any(i["kind"] == "strength" and "5 against a target of 8" in i["why"] for i in items)
    assert any(i["kind"] == "leadership" and "Needs 1 Server scoring 4 or above" in i["what"] for i in items)
    ask = next(i for i in items if i["kind"] == "ask")
    assert ask["what"] == "+1 Server on Friday dinner/night" and "the reviews diagnosis" in ask["why"]
    mn = next(i for i in items if i["kind"] == "min_hours")
    assert mn["employee"] == "Cy" and mn["full_time"] is True and "0h this week" in mn["why"]   # no shift at all
    assert any(i["kind"] == "budget" and "over" in i["why"] for i in items)
    assert any(i["kind"] == "station" and i["what"] == "Grill station" for i in items)
    assert items[-1]["kind"] == "unchecked_rule" and "Two hosts" in items[-1]["what"]
    # owner-facing words carry no ISO date
    assert not [i for i in items if re.search(r"\d{4}-\d{2}-\d{2}", i["what"] + i["why"])]


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


def test_the_job_saves_what_the_week_misses_with_its_review(db, monkeypatch):
    from models import create_restaurant, Restaurant
    rid = create_restaurant(Restaurant(name="Unmet Grill", owner_email="u@x.test"), db_path=db)
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                f"{WEEK[4]},Friday,Ana,Server,4:00pm,10:00pm,6,\n{WEEK[4]},Friday,Max,Manager,6:00pm,10:00pm,4,")
    base = {"ok": True, "schedule_csv": csv_text, "week_dates": WEEK,
            "week_days": list(sr.DAYS), "summary": [], "hours_budget": 0, "daily_target_hours": {},
            "labor_target": 30, "blended_rate": 20.0, "roster": ["Ana", "Max"],
            "roster_roles": {"Ana": "Server", "Max": "Manager"},
            # Max manages: a 4-6pm stretch with Ana on and no manager.
            "constraints": _c(restaurant_id=rid, managers={"max": "Manager"}, roster_names=["Ana", "Max"],
                              active={"ana", "max"})}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    # Keep the backstops out of it: the gap stays for the review to name.
    monkeypatch.setattr(sr, "cover_manager_gaps", lambda rows, c, **k: {"rows": rows, "extended": [], "added": [],
                                                                        "left": []})
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job",
                        lambda job_id, status, result: finished.update(status=status, result=result))
    se._run_schedule_job("unmet-job", rid)
    assert finished["status"] == "done", finished
    unmet = finished["result"]["review"]["unmet"]
    assert any(u["kind"] == "manager" and u["date"] == WEEK[4] for u in unmet), unmet
    conn = models.get_conn(db)
    try:
        stored = json.loads(conn.execute("SELECT review_json FROM schedule_history WHERE restaurant_id=?",
                                         (rid,)).fetchone()["review_json"])
    finally:
        conn.close()
    assert stored["unmet"] == unmet


def test_an_edited_week_keeps_its_list_of_what_it_misses_and_its_asks(db, monkeypatch):
    """The re-score an edit runs (one body for web and phone) says what the
    edited week misses as generation does, and saves the asks with it so
    the next edit still reads them."""
    from flask import Flask
    import auth
    import mobile_api
    import strategy_routes
    from models import create_restaurant, Restaurant
    monkeypatch.setattr(auth, "DB_PATH", db)
    auth.init_auth(db_path=db)
    rid = create_restaurant(Restaurant(name="Edit Grill", owner_email="e@x.test", module_labor=1), db_path=db)
    for name, role in (("Max", "Manager"), ("Ana", "Server")):
        models.add_manual_team_member(rid, name, role=role, db_path=db)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: {"is_live": True})
    rows = [{"date": WEEK[4], "day": "Friday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    csv_text = so.CSV_HEADER + "\n" + "\n".join(so.csv_lines(rows))
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 4, 30, csv_text, [], db_path=db)
    ask = {"day": "Friday", "date": WEEK[4], "daypart": "night", "role": "Server", "source": "reviews",
           "text": "+1 Server — waits", "applied": True, "scheduled": 2, "typical": 1}
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET review_json=? WHERE id=?", (json.dumps({"soft_requirements": [ask]}), hid))
    conn.commit()
    conn.close()
    uid = auth.create_user(rid, "owner", "o@x.test", "pw", db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET role='client' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    headers = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    client = app.test_client()
    for _round in (1, 2):
        versions = schedule_versions.list_versions(rid, hid)
        r = client.post("/mobile/api/labor/schedule/score", headers=headers,
                        json={"rows": rows, "history_id": hid, "save": True,
                              "version": versions[-1]["version"] if versions else None})
        assert r.status_code == 200, r.get_json()
        review = r.get_json()["review"]
        kinds = {u["kind"] for u in review["unmet"]}
        assert {"manager", "ask", "budget"} <= kinds, review["unmet"]
        assert review["soft_requirements"][0]["applied"] is False     # re-read against the edited rows
        conn = models.get_conn(db)
        stored = json.loads(conn.execute("SELECT review_json FROM schedule_history WHERE id=?",
                                         (hid,)).fetchone()["review_json"])
        conn.close()
        assert stored["unmet"] == review["unmet"] and stored["soft_requirements"]
    # the re-check route after an edit says the same
    r = client.post("/mobile/api/labor/schedule/violations", headers=headers, json={"rows": rows, "history_id": hid})
    assert {"manager", "ask", "budget"} <= {u["kind"] for u in r.get_json()["review"]["unmet"]}
