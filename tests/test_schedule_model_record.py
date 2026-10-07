"""Every schedule call's full input and answer, kept and replayable
(schedule audit 10/3/26 PR-31).

The trace kept 40,000 characters of a 55-70k prompt and 12,000 of the
answer, so no real week could be replayed; scripts/schedule_eval.py
re-scored stored weeks with no model call, and the live experiment
compared the solver against the model only. Now: each call is stored in
schedule_model_calls with the exact request, the generator's own arguments
(rebuilt as they were) and the answer, keyed to its generation and to the
saved week; the schedule trace keeps the whole prompt; and
scripts/schedule_model_eval.py replays real weeks across model × effort ×
prompt variant on an injectable model call, scoring hard breaches and
manager minutes before and after the backstops, full-timers under their
minimum, rows the backstops change, Shift Quality, what the week misses,
tokens, seconds and cost per completed week.
"""
import json
import sys
import types
import zlib

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import ai_utils
import labor
import models
import ops
import schedule_engine as se
import schedule_output as so
import schedule_prompt
from models import create_restaurant, Restaurant
from scripts import schedule_model_eval as sme

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
FRI = WEEK[4]
_ANALYSIS = {"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
             "period_days": 0, "total_sales": 0}


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
    return db_path


class _Msg:
    def __init__(self, text, stop="end_turn", out_tokens=900):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop
        self.stop_details = None
        self.usage = types.SimpleNamespace(input_tokens=20000, output_tokens=out_tokens,
                                           cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _answer(days):
    return json.dumps({"days": [{"date": d, "shifts": s} for d, s in days], "summary": ["ok"]},
                      separators=(",", ":"))


def _rid(db):
    return create_restaurant(Restaurant(name="Replay Grill", owner_email="r@x.test", module_labor=1), db_path=db)


def _model(monkeypatch, msg):
    seen = []
    monkeypatch.setattr(labor, "create_with_retry", lambda client, **kw: seen.append(kw) or msg)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    return seen


# ── what is stored ─────────────────────────────────────────────────────────

def test_a_call_is_stored_with_its_whole_prompt_its_arguments_and_its_answer(db, monkeypatch):
    rid = _rid(db)
    answer = _answer([(FRI, [{"employee": "Ana", "role": "Server", "start": "4:00pm", "end": "10:00pm"}])])
    seen = _model(monkeypatch, _Msg(answer))
    big = "\n\nWHAT THE OWNER KNOWS: " + ("busy patio night. " * 4000)          # ~72k characters
    profile = shift_quality.ShiftProfile(key="fri_dinner", label="Friday dinner", days=["Friday"], daypart="night")
    labor.generate_optimized_schedule(
        _ANALYSIS, [{"employee": "Ana", "role": "Server", "date": "2026-10-02", "shift_start": "4:00pm",
                     "shift_end": "10:00pm", "scheduled_hours": 6}],
        roster=[("Ana", "Server"), ("Max", "Manager")], week_start=WEEK[0], restaurant_id=rid,
        generation_id="gen-1", extra_blocks=big, hourly_rate=20, labor_target=30,
        hourly_profile={"Friday": {17: 0.2, 18: 0.3}}, borrowed_headcount={("Friday", "night"): {"Server": 2}},
        shift_profiles=[profile])
    calls = so.load_calls(generation_id="gen-1")
    assert len(calls) == 1
    call = calls[0]
    sent_prompt = seen[0]["messages"][0]["content"]
    # Three text blocks — the standing instructions, the restaurant's week,
    # this request — the first two cached (C1, PR-26).
    assert isinstance(sent_prompt, list) and len(sent_prompt) == 3
    assert len(schedule_prompt.prompt_text(sent_prompt)) > 60000
    assert call["request"]["messages"][0]["content"] == sent_prompt         # whole, not the first 40k
    assert call["request"]["output_config"]["format"] == seen[0]["output_config"]["format"]
    assert call["answer"] == answer and call["rows"] == 1 and call["outcome"] == "ok"
    # The route's model and tier (AI orchestration 10/7/26): a direct call
    # runs on the labor_schedule ladder's first rung, T3 (Sonnet 5.5, medium).
    assert call["model"] == "claude-sonnet-5-5" and call["effort"] == "medium" and call["contract"] == "schema"
    assert call["tier"] == "T3"
    assert call["usage"]["output_tokens"] == 900 and call["dates"] == WEEK
    args = call["inputs"]
    assert args["roster"] == [("Ana", "Server"), ("Max", "Manager")]         # tuples come back as tuples
    assert args["hourly_profile"] == {"Friday": {17: 0.2, 18: 0.3}}          # int keys stay ints
    assert args["borrowed_headcount"] == {("Friday", "night"): {"Server": 2}}
    assert isinstance(args["shift_profiles"][0], shift_quality.ShiftProfile)
    assert args["shifts"][0]["employee"] == "Ana" and args["analysis"] == _ANALYSIS


def test_a_generations_shift_history_is_stored_once_and_read_back_for_every_call(db, monkeypatch):
    rid = _rid(db)
    _model(monkeypatch, _Msg(_answer([])))
    history = [{"employee": "Ana", "role": "Server", "date": "2026-10-02"}]
    for sl in (WEEK[:3], WEEK[3:]):
        labor.generate_optimized_schedule(_ANALYSIS, history, roster=[("Ana", "Server")], week_start=WEEK[0],
                                          restaurant_id=rid, generation_id="gen-2", week_slice=sl)
    conn = models.get_conn(db)
    try:
        shared = [r[0] is not None for r in conn.execute(
            "SELECT shared_z FROM schedule_model_calls WHERE generation_id='gen-2' ORDER BY id")]
    finally:
        conn.close()
    assert shared == [True, False]
    calls = so.load_calls(generation_id="gen-2")
    assert [c["dates"] for c in calls] == [WEEK[:3], WEEK[3:]]
    assert all(c["inputs"]["shifts"] == history for c in calls)


def test_a_guests_contact_details_never_reach_the_record(db, monkeypatch):
    rid = _rid(db)
    _model(monkeypatch, _Msg(_answer([])))
    labor.generate_optimized_schedule(_ANALYSIS, [], roster=[("Ana", "Server")], week_start=WEEK[0],
                                      restaurant_id=rid, generation_id="gen-p",
                                      extra_blocks="\n\nA guest (jo@example.com, 312-555-0199) asked for Ana.")
    call = so.load_calls(generation_id="gen-p")[0]
    text = json.dumps(call["request"]) + json.dumps(call["inputs"], default=str)
    assert "jo@example.com" not in text and "312-555-0199" not in text and "Ana" in text


def test_a_record_that_cannot_be_saved_is_captured_and_never_fails_the_week(db, monkeypatch):
    rid = _rid(db)
    _model(monkeypatch, _Msg(_answer([(FRI, [{"employee": "Ana", "role": "Server", "start": "4:00pm",
                                              "end": "10:00pm"}])])))
    captured = []
    monkeypatch.setattr(so, "record_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append((str(e), k.get("job"))))
    out = labor.generate_optimized_schedule(_ANALYSIS, [], roster=[("Ana", "Server")], week_start=WEEK[0],
                                            restaurant_id=rid, generation_id="gen-x")
    assert out["schedule_csv"].count("\n") == 1 and out["model_call"]["id"] is None
    assert captured == [("disk full", "schedule_model_calls")]


def test_the_saved_week_links_every_call_of_its_generation(db, monkeypatch):
    rid = _rid(db)
    so.record_call(rid, {"model": "m", "messages": []}, inputs={"roster": []}, answer="{}",
                   generation_id="gen-job", week_start=WEEK[0], dates=WEEK, contract="schema", outcome="ok")
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                f"{FRI},Friday,Ana,Server,4:00pm,10:00pm,6,")
    base = {"ok": True, "schedule_csv": csv_text, "week_dates": WEEK, "week_days": list(schedule_rules.DAYS),
            "summary": [], "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30, "blended_rate": 20.0,
            "generation_id": "gen-job"}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job",
                        lambda job_id, status, result: finished.update(status=status, result=result))
    se._run_schedule_job("link-job", rid)
    hid = finished["result"]["history_id"]
    assert hid and [c["history_id"] for c in so.load_calls(history_id=hid)] == [hid]
    assert so.generations(restaurant_ids=[rid])[0]["history_id"] == hid


def test_measured_tokens_per_row_reads_the_calls_that_finished(db):
    rid = _rid(db)
    assert so.measured_tokens_per_row(rid)["source"] == "estimate"
    for out_tokens, rows in ((9000, 100), (12000, 100), (30000, 100)):
        so.record_call(rid, {"model": "claude-opus-5-5"}, generation_id="g", week_start=WEEK[0], contract="schema",
                       stop_reason="end_turn", outcome="ok", usage={"output_tokens": out_tokens}, rows=rows,
                       answer_chars=rows * 80)
    so.record_call(rid, {"model": "claude-opus-5-5"}, generation_id="g", week_start=WEEK[0], contract="schema",
                   stop_reason="max_tokens", outcome="truncated", usage={"output_tokens": 64000}, rows=10)
    got = so.measured_tokens_per_row(rid)
    assert got == {"output_tokens_per_row": 120.0, "answer_chars_per_row": 80.0, "calls": 3, "source": "measured"}


def test_the_schedule_trace_keeps_the_whole_prompt_and_answer(db, monkeypatch):
    prompt = "Schedule this week. " + "x" * 90000
    msg = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="y" * 30000)], stop_reason="end_turn",
                                usage=types.SimpleNamespace(input_tokens=1, output_tokens=1), _request_id=None)
    kwargs = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": prompt}]}
    ai_utils._record_trace_safe("trace-sched", kwargs, msg, None, "labor_schedule", "ok", {})
    ai_utils._record_trace_safe("trace-other", kwargs, msg, None, "labor_insight", "ok", {})
    conn = models.get_conn(db)
    try:
        rows = {r["call_id"]: r for r in conn.execute("SELECT call_id, prompt_z, output_z FROM ai_calls")}
    finally:
        conn.close()
    sched = zlib.decompress(rows["trace-sched"]["prompt_z"]).decode()
    other = zlib.decompress(rows["trace-other"]["prompt_z"]).decode()
    assert len(sched) > 90000 and len(zlib.decompress(rows["trace-sched"]["output_z"]).decode()) == 30000
    assert len(other) == ai_utils.AI_TRACE_PROMPT_CHARS


def test_the_record_is_on_the_retention_registry_with_a_floor_and_its_reader():
    assert ops._RETENTION_DAYS["schedule_model_calls"] == 180
    assert ops._RETENTION_COLUMN["schedule_model_calls"] == "created_at"
    assert 0 < ops._RETENTION_FLOOR_DAYS["schedule_model_calls"] <= 180
    assert ("schedule_output.measured_tokens_per_row", 60, None) in ops._RETENTION_READERS["schedule_model_calls"]


# ── the replay harness ─────────────────────────────────────────────────────

def _stored_week(db, monkeypatch, rid):
    """One production generation: Max (manager) and Ana on Friday 4-10pm,
    the rest of the week closed; saved and linked."""
    models.add_manual_team_member(rid, "Max", role="Manager", db_path=db)
    models.add_manual_team_member(rid, "Ana", role="Server", db_path=db)
    prod = _answer([(FRI, [{"employee": "Max", "role": "Manager", "start": "4:00pm", "end": "10:00pm"},
                           {"employee": "Ana", "role": "Server", "start": "4:00pm", "end": "10:00pm"}])])
    _model(monkeypatch, _Msg(prod, out_tokens=3000))
    out = labor.generate_optimized_schedule(
        _ANALYSIS, [], roster=[("Max", "Manager"), ("Ana", "Server")], week_start=WEEK[0], restaurant_id=rid,
        generation_id="gen-e", closed_dates=[d for d in WEEK if d != FRI], hourly_rate=20, labor_target=30)
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 12, 0, 30, out["schedule_csv"], [], db_path=db)
    so.link_calls(rid, hid, "gen-e")
    return hid


def _arm_answer(req):
    """Every arm answers with Ana alone on Friday: no manager on."""
    return _Msg(_answer([(FRI, [{"employee": "Ana", "role": "Server", "start": "4:00pm", "end": "10:00pm"}])]),
                out_tokens=2000)


def test_the_eval_replays_a_real_week_on_each_arm_and_scores_it(db, monkeypatch):
    rid = _rid(db)
    _stored_week(db, monkeypatch, rid)
    weeks = sme.load_weeks(restaurant_ids=[rid])
    assert len(weeks) == 1
    received = []

    def call_model(req):
        received.append(req)
        return _arm_answer(req)
    arms = sme.arms_from(["claude-opus-5-5", "claude-sonnet-5"], ["high"], ["stored"])
    report = sme.run(weeks, arms, call_model=call_model, live=True)
    prod, opus = report["arms"]["production"], report["arms"]["claude-opus-5-5/high/stored"]
    # the stored answer had a manager on; the arm's did not, and the
    # backstop added one
    assert prod["manager_minutes_before"] == 0 and prod["hard_before"] == 0
    assert opus["manager_minutes_before"] == 360 and opus["manager_minutes_after"] == 0
    assert opus["rows_repaired"] >= 1 and opus["hard_before"] >= 1
    assert opus["completed"] == 1 and opus["cost_per_completed_week"] > 0
    assert prod["output_tokens"] == 3000 and opus["output_tokens"] == 2000
    # each arm sent the stored prompt with its own model, thinking and effort
    stored = so.load_calls(generation_id="gen-e")[0]["request"]
    opus_req, sonnet_req = received
    assert opus_req["messages"] == stored["messages"] and opus_req["model"] == "claude-opus-5-5"
    assert opus_req["thinking"]["type"] == "adaptive" and opus_req["output_config"]["effort"] == "high"
    assert opus_req["max_tokens"] == labor.SCHEDULE_MAX_TOKENS_THINKING
    assert sonnet_req["model"] == "claude-sonnet-5" and "thinking" not in sonnet_req
    assert "effort" not in sonnet_req["output_config"] and sonnet_req["max_tokens"] == 16000
    assert opus_req["output_config"]["format"] == stored["output_config"]["format"]
    for k in ("readiness", "stream", "restaurant_id", "action"):
        assert k not in opus_req


def test_without_live_nothing_is_called_and_the_cost_is_estimated(db, monkeypatch):
    rid = _rid(db)
    _stored_week(db, monkeypatch, rid)
    weeks = sme.load_weeks(restaurant_ids=[rid])
    arms = sme.arms_from(["claude-opus-5-5"], ["high", "medium"], ["stored"])
    report = sme.run(weeks, arms, call_model=lambda req: pytest.fail("a dry run called the model"), live=False)
    assert list(report["arms"]) == ["production"]
    est = sme.estimate(weeks, arms)
    assert set(est) == {"claude-opus-5-5/high/stored", "claude-opus-5-5/medium/stored"}
    assert est["claude-opus-5-5/high/stored"] == round(20000 * 4 / 1e6 + 3000 * 20 / 1e6, 2)


def test_the_rerender_arm_answers_todays_prompt_and_stores_nothing(db, monkeypatch):
    rid = _rid(db)
    _stored_week(db, monkeypatch, rid)
    weeks = sme.load_weeks(restaurant_ids=[rid])
    received = []

    def call_model(req):
        received.append(req)
        return _arm_answer(req)
    conn = models.get_conn(db)
    before = conn.execute("SELECT COUNT(*) FROM schedule_model_calls").fetchone()[0]
    conn.close()
    sme.run(weeks, sme.arms_from(["claude-opus-5-5"], ["low"], ["rerender"]), call_model=call_model, live=True)
    conn = models.get_conn(db)
    after = conn.execute("SELECT COUNT(*) FROM schedule_model_calls").fetchone()[0]
    conn.close()
    assert after == before
    prompt = schedule_prompt.prompt_text(received[0]["messages"][0]["content"])
    assert "printed on that employee's own schedule" in prompt and "Max" in prompt
    assert received[0]["output_config"]["effort"] == "low" and received[0]["model"] == "claude-opus-5-5"


def _shout(request, call):
    # The user turn is a list of text blocks (schedule_prompt.request_content).
    req = dict(request)
    blocks = list(request["messages"][0]["content"])
    req["messages"] = [{"role": "user", "content": blocks + [{"type": "text", "text": "VARIANT B"}]}]
    return req


def test_a_prompt_variant_is_a_transform_of_the_stored_request(db, monkeypatch):
    rid = _rid(db)
    _stored_week(db, monkeypatch, rid)
    received = []
    sme.run(sme.load_weeks(restaurant_ids=[rid]), sme.arms_from(["claude-opus-5-5"], ["high"], [f"{__name__}:_shout"]),
            call_model=lambda req: received.append(req) or _arm_answer(req), live=True)
    assert received[0]["messages"][0]["content"][-1] == {"type": "text", "text": "VARIANT B"}


def test_schedule_eval_shows_the_calls_behind_a_week_and_scores_the_models_own_answer(db, monkeypatch):
    rid = _rid(db)
    hid = _stored_week(db, monkeypatch, rid)
    from scripts import schedule_eval
    out = schedule_eval.evaluate(hid, calls=True)
    # The stored week was written on the route's model: a direct call runs on
    # the labor_schedule ladder's first rung, T3 Sonnet 5.5 (AI orchestration 10/7/26).
    assert out["calls"][0]["model"] == "claude-sonnet-5-5" and out["calls"][0]["rows"] == 2
    assert out["calls"][0]["tokens_per_row"] == 1500.0
    assert out["model_answer"]["hard_before"] == 0 and out["model_answer"]["manager_minutes_before"] == 0


def test_the_security_doc_names_the_record_and_its_redaction():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "docs", "ops", "SECURITY.md"), encoding="utf-8").read()
    assert "`schedule_model_calls` keeps every schedule generation call's full request" in text
    assert "180 days" in text.split("**Schedule call record**", 1)[1].split("\n", 1)[0]
    assert ops._RETENTION_DAYS["schedule_model_calls"] == 180


def test_the_default_repair_runs_the_owners_floors_and_the_manager_rule(db, monkeypatch):
    rid = _rid(db)
    for name, role in (("Max", "Manager"), ("Ana", "Server"), ("Bea", "Server")):
        models.add_manual_team_member(rid, name, role=role, db_path=db)
    c = schedule_rules.build_constraints(rid, WEEK, list(schedule_rules.DAYS), models.get_restaurant(rid))
    c.role_floors = {"Server": {"morning": 0, "night": 2}}
    c.closed_dates = {d for d in WEEK if d != FRI}
    rows = [{"date": FRI, "day": "Friday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    fixed = sme.default_repair(rows, c, {"Max": "Manager", "Ana": "Server", "Bea": "Server"})
    on = {(r["employee"], r["role"]) for r in fixed}
    assert ("Bea", "Server") in on                           # the floor of two servers
    assert any(r["employee"] == "Max" for r in fixed)       # a manager every minute
