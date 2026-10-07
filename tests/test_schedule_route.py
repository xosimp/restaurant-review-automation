"""The schedule on the orchestrator: a Sonnet-5.5-first ladder (AI
orchestration, owner decision 3, 10/7/26 — "Sonnet 5.5 first if the eval
clears 80%, hard weeks straight to Opus").

The ai_workflows "labor_schedule" ladder is (T3 Sonnet 5.5, T4 Opus 5.5),
both thinking at medium. A deterministic pre-router (schedule_engine.
hard_week_reasons) starts a hard week on T4; the quality gate's model
rewrite — after the code's own fill did not settle the gate — IS the
escalation to the next rung, with the gate's reasons as its notes. A
SCHEDULE_MODEL pin skips the ladder. The whole job is one run: every call
carries the run id as its ai_usage correlation id, and the run is written to
ai_runs with its start and final tier, the escalation, the gate's reasons
and the cost."""
import inspect
import json
import sqlite3
import types

import pytest

import ai_orchestrator as orch
import ai_utils
import ai_workflows as wf
import labor
import models
import schedule_engine as se
import schedule_output as so
from tests.test_schedule_b2_calls import (WEEK, _build_harness, _gate_harness, _gen, _line,  # noqa: F401
                                          _restaurant, db)

GATE = {"dates": [WEEK[5]], "focus": ["Saturday 2026-10-10 dinner: 2 servers short"],
        "triggers": {WEEK[5]: ["coverage"]}, "kinds": ["coverage"],
        "reason": "1 busy day still had a staffing hole after repair"}


@pytest.fixture(autouse=True)
def _no_overrides(monkeypatch):
    for k in ("SCHEDULE_MODEL", "AI_TIER_T3_MODEL", "AI_TIER_T4_MODEL", "AI_TIER_T3_EFFORT", "AI_TIER_T4_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(wf, "policy", lambda workflow, db_path=None: wf.POLICIES[workflow])


def _inside(fn, rid=None):
    """fn() inside one generation run (schedule_engine._in_schedule_run), as
    a job would run it: returns (fn's value, the run state at its end)."""
    out = {}

    @se._in_schedule_run
    def job(job_id, restaurant_id):
        out["value"] = fn()
        out["run"] = se.current_schedule_run()
    job(None, rid)
    return out["value"], out["run"]


def _route_ordinary(monkeypatch, reasons=()):
    monkeypatch.setattr(se, "hard_week_reasons", lambda *a, **k: list(reasons))


def _route(rid=None, restaurant=None):
    se._route_generation(rid, restaurant, WEEK, types.SimpleNamespace(owner_rules=[], owner_rules_unchecked=[]),
                         [("Ana", "Server"), ("Max", "Manager")])


def _runs(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ai_runs WHERE workflow='labor_schedule' "
                                              "ORDER BY created_at, rowid")]
    finally:
        conn.close()


# ── the ladder ─────────────────────────────────────────────────────────────

def test_the_ladder_starts_on_sonnet_55_and_climbs_once_to_opus_55():
    pol = wf.POLICIES["labor_schedule"]
    assert pol.ladder == ("T3", "T4") and pol.escalate_on == ("rule_breach",) and pol.max_escalations == 1
    assert pol.reviewer == "rules" and pol.delivery == "background"
    # A week's planned calls, the generation's own retries and splits, the gate's rewrite.
    assert 1 + se.MAX_EXTRA_CALLS + 1 <= pol.caps.calls <= 20
    assert pol.caps.usd == 6.0 and pol.caps.seconds == se.SCHEDULE_JOB_MAX_SECONDS
    assert wf.POLICY_VERSION != "2026-10-07.1"              # bumped with the ladder
    tiers = wf._tier_table()
    for t in pol.ladder:                                    # both are thinking tiers
        assert labor.schedule_model_thinks(tiers[t]["model"]) and tiers[t]["effort"] == "medium"
    start = se.start_route()
    assert (start.tier, start.model, start.effort) == ("T3", "claude-sonnet-5-5", "medium")
    assert se.schedule_route() == start                     # outside a job: the first rung
    assert se.start_route("T4").model == "claude-opus-5-5"
    assert se.schedule_effort_in_force() == "medium"


# ── the pre-router ─────────────────────────────────────────────────────────

THANKSGIVING_WEEK = [f"2026-11-{d}" for d in range(23, 30)]
HALLOWEEN_WEEK = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01"]


def test_an_ordinary_week_has_no_reason_to_start_high():
    assert se.hard_week_reasons(WEEK) == []
    assert se.hard_week_reasons(WEEK, roster_size=se.HARD_WEEK_ROSTER, owner_rules=se.HARD_WEEK_OWNER_RULES,
                                prior_generations=4) == []


def test_a_major_holiday_starts_the_week_on_t4_and_a_minor_one_does_not():
    (why,) = se.hard_week_reasons(THANKSGIVING_WEEK)
    assert "Thanksgiving 11/26/26" in why and "2026-" not in why        # M/D/YY, never ISO
    assert se.hard_week_reasons(HALLOWEEN_WEEK) == []                   # Halloween is not on the list


def test_a_dated_closure_in_the_week_is_a_hard_week():
    (why,) = se.hard_week_reasons(WEEK, closed_dates=["2026-10-07"])
    assert "dated closure" in why and "10/7/26" in why
    assert se.hard_week_reasons(WEEK, closed_dates=["2026-12-25"]) == []    # not this week


@pytest.mark.parametrize("over", [0, 1])
def test_a_big_roster_starts_on_t4(over):
    n = se.HARD_WEEK_ROSTER + over
    assert bool(se.hard_week_reasons(WEEK, roster_size=n)) is bool(over)


@pytest.mark.parametrize("over", [0, 1])
def test_many_owner_rules_start_on_t4(over):
    n = se.HARD_WEEK_OWNER_RULES + over
    assert bool(se.hard_week_reasons(WEEK, owner_rules=n)) is bool(over)


def test_a_first_generation_and_a_week_that_escalated_start_on_t4():
    assert se.hard_week_reasons(WEEK, prior_generations=0) == ["the restaurant's first schedule"]
    assert se.hard_week_reasons(WEEK, prior_generations=None) == []     # not known: no rule
    assert se.hard_week_reasons(WEEK, last_run_escalated=True) == ["this week's last draft escalated"]


def test_the_pre_router_reads_the_restaurant_first_draft_and_the_weeks_last_run(db):
    rid = _restaurant(db)
    # No draft yet: its first schedule starts on T4.
    route, run = _inside(lambda: (_route(rid), se.schedule_route())[1], rid)
    assert route.tier == "T4" and run["pre_route"] == ["the restaurant's first schedule"]
    assert run["subject"] == "week:2026-10-05" and run["steps"][0]["kind"] == "week"
    conn = models.get_conn(db)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start) VALUES (?, ?)", (rid, WEEK[0]))
    conn.commit()
    conn.close()
    route, run = _inside(lambda: (_route(rid), se.schedule_route())[1], rid)
    assert route.tier == "T3" and run["pre_route"] == []
    # A new draft of a week whose last run escalated starts where it ended.
    conn = models.get_conn(db)
    conn.execute("INSERT INTO ai_runs (run_id, restaurant_id, workflow, subject, escalations, created_at) "
                 "VALUES ('run:x', ?, 'labor_schedule', 'week:2026-10-05', 1, datetime('now', '+1 minute'))",
                 (rid,))
    conn.commit()
    conn.close()
    route, run = _inside(lambda: (_route(rid), se.schedule_route())[1], rid)
    assert route.tier == "T4" and run["pre_route"] == ["this week's last draft escalated"]


def test_the_route_is_decided_once_per_run(monkeypatch):
    _route_ordinary(monkeypatch)

    def body():
        _route()
        first = se.schedule_route()
        _route_ordinary(monkeypatch, ["holiday in the week: Christmas Day 12/25/26"])
        _route()                    # the gate's rewrite re-enters the build: the route stands
        return first, se.schedule_route()
    (first, again), run = _inside(body)
    assert first == again and first.tier == "T3" and len(run["steps"]) == 1


# ── the escalation: the quality gate's model rewrite ───────────────────────

def test_the_gate_rewrite_escalates_to_t4_with_the_gates_reasons_as_notes(monkeypatch):
    _route_ordinary(monkeypatch)

    def body():
        _route()
        before = se.schedule_route()
        verdict = se._escalate_for_gate(GATE)
        after = se.schedule_route()
        again = se._escalate_for_gate(GATE)          # max_escalations 1: no higher
        return before, verdict, after, again
    (before, verdict, after, again), run = _inside(body)
    assert before.tier == "T3" and verdict == "escalated" and again == "stay"
    assert (after.tier, after.model, after.effort) == ("T4", "claude-opus-5-5", "medium")
    assert run["escalations"] == 1 and run["route"] == after
    notes = [GATE["reason"]] + GATE["focus"]
    assert run["notes"] == notes
    step = run["steps"][1]
    assert step["tier"] == "T4" and step["trigger"] == "rule_breach" and step["notes"] == notes
    assert run["gate"]["reason"] == GATE["reason"]


def test_a_pre_routed_hard_week_rewrites_on_t4(monkeypatch):
    _route_ordinary(monkeypatch, ["roster of 80 (over 60)"])

    def body():
        _route()
        return se._escalate_for_gate(GATE), se.schedule_route()
    (verdict, route), run = _inside(body)
    assert verdict == "stay" and route.tier == "T4" and run["escalations"] == 0
    assert run["start"] == "T4" and run["steps"][-1]["tier"] == "T4" and "trigger" not in run["steps"][-1]


def test_a_run_at_its_caps_keeps_its_draft(monkeypatch):
    _route_ordinary(monkeypatch)
    monkeypatch.setattr(orch, "_spent", lambda run_id, db_path=None: (6.5, 0, 0, 3))

    def body():
        _route()
        return se._escalate_for_gate(GATE), se.schedule_route()
    (verdict, route), run = _inside(body)
    assert verdict == "capped" and route.tier == "T3" and run["escalations"] == 0
    assert run["outcome"] == "skipped" and run["capped"] == {"calls": 3, "usd": 6.5}


def test_the_rewrite_runs_on_the_next_rung(db, monkeypatch):
    rid = _restaurant(db)
    _route_ordinary(monkeypatch)
    seen = []
    monkeypatch.setattr(se, "_run_schedule_job", lambda *a, **k: seen.append((se.schedule_route().tier, k)))
    fallback = {"history_id": 1, "payload": {}}

    def body():
        _route(rid)
        se._gate_model_rewrite(None, rid, WEEK[0], GATE, fallback, None, None)
    _inside(body, rid)
    ((tier, kw),) = seen
    assert tier == "T4" and kw["focus"] == GATE["focus"] and kw["dates"] == GATE["dates"] and kw["gate"] is False


# ── the SCHEDULE_MODEL pin ─────────────────────────────────────────────────

def test_a_schedule_model_pin_wins_and_skips_the_ladder(monkeypatch):
    monkeypatch.setenv("SCHEDULE_MODEL", "claude-opus-5-5")
    _route_ordinary(monkeypatch, ["holiday in the week: Thanksgiving 11/26/26"])
    pinned = se.start_route()
    assert (pinned.tier, pinned.model, pinned.effort) == (wf.DEFAULT, "claude-opus-5-5", labor.SCHEDULE_EFFORT)

    def body():
        _route()
        return se._escalate_for_gate(GATE), se.schedule_route()
    (verdict, route), run = _inside(body)
    assert verdict == "stay" and route == pinned and run["pinned"] and run["escalations"] == 0
    assert run["pre_route"] == []                    # the pre-router does not run under a pin
    monkeypatch.setenv("SCHEDULE_MODEL", "claude-sonnet-5")      # a model that does not think
    assert se.start_route().effort is None and se.schedule_effort_in_force() == ""


def test_the_schedule_call_sends_the_pinned_model(monkeypatch):
    monkeypatch.setenv("SCHEDULE_MODEL", "claude-opus-5-5")
    captured = _call_once(monkeypatch)
    assert captured["model"] == "claude-opus-5-5" and captured["output_config"]["effort"] == labor.SCHEDULE_EFFORT


# ── every call of a generation is on the route ─────────────────────────────

def _call_once(monkeypatch, **kw):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text='{"shifts": [], "summary": ["ok"]}')],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}},
        [{"employee": "Alex", "role": "Server", "date": "2026-06-01", "day": "Monday",
          "scheduled_hours": 8, "actual_hours": 8}],
        restaurant_name="Test Bistro", hourly_rate=26.0, labor_target=23.0, monthly_revenue_target=365000.0, **kw)
    return captured


def test_the_call_runs_on_the_runs_tier_and_records_it(db, monkeypatch):
    rid = _restaurant(db)
    _route_ordinary(monkeypatch, ["roster of 80 (over 60)"])

    def body():
        _route(rid)
        return _call_once(monkeypatch, restaurant_id=rid, generation_id="gen-t4"), ai_utils.current_ai_context()
    (captured, ctx), run = _inside(body, rid)
    assert captured["model"] == "claude-opus-5-5"
    assert captured["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert captured["output_config"]["effort"] == "medium"
    assert captured["max_tokens"] == labor.SCHEDULE_MAX_TOKENS_THINKING
    (call,) = so.load_calls(generation_id="gen-t4")
    assert call["tier"] == "T4" and call["model"] == "claude-opus-5-5" and call["effort"] == "medium"
    # Every ledger row of the generation carries the run id.
    assert ctx["correlation_id"] == run["run_id"]


def test_the_call_planner_reads_the_routes_model_and_effort(monkeypatch):
    seen = []
    monkeypatch.setattr(so, "call_costs", lambda restaurant_id=None, model=None, effort=None, **k:
                        seen.append((model, effort)) or {"source": "estimate"})
    _route_ordinary(monkeypatch)

    def body():
        _route()
        se.call_cost_model(5)
        se._escalate_for_gate(GATE)
        se.call_cost_model(5)
        se._SCHED_RUN.get()["route"] = wf.Route("T4", "claude-opus-5-5", "low")
        return se.schedule_effort_in_force(), se.thinking_tokens_reserved()
    (effort, reserve), _run = _inside(body)
    assert seen == [("claude-sonnet-5-5", "medium"), ("claude-opus-5-5", "medium")]
    assert effort == "low" and reserve == se.THINKING_TOKENS_BY_EFFORT["low"]


def test_the_eval_replays_the_ladders_tiers():
    from scripts import schedule_model_eval as sme
    arms = sme.tier_arms(["T3", "T4"], ["stored"])
    assert [(a["tier"], a["model"], a["effort"]) for a in arms] == [
        ("T3", "claude-sonnet-5-5", "medium"), ("T4", "claude-opus-5-5", "medium")]
    req = sme.apply_arm({"model": "x", "messages": []}, arms[0])
    assert req["model"] == "claude-sonnet-5-5" and req["output_config"]["effort"] == "medium"
    assert req["thinking"]["type"] == "adaptive"
    with pytest.raises(ValueError):
        sme.tier_arms(["T9"], ["stored"])


def test_no_schedule_call_reads_its_model_or_effort_around_the_route():
    gen = inspect.getsource(labor.generate_optimized_schedule)
    assert 'model_for("schedule")' not in gen and "_route.apply(" in gen
    for fn in (se.call_cost_model, se.schedule_effort_in_force, se._generate_in_parts):
        assert 'model_for("schedule")' not in inspect.getsource(fn), fn.__name__


# ── one ai_runs row per generation ─────────────────────────────────────────

def _metered(answers, calls, tiers, rid):
    fake = _gen(answers, calls)

    def run(*a, **k):
        route = se.schedule_route()
        tiers.append(route.tier)
        ai_utils.log_ai_usage(rid, "labor_schedule", route.model, 20000, 3000)
        return fake(*a, **k)
    return run


def test_a_generation_that_escalates_is_one_run_with_its_tiers_reasons_and_cost(db, monkeypatch):
    calls, tiers = [], []
    rid, finished = _gate_harness(monkeypatch, db, [], calls)
    conn = models.get_conn(db)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start) VALUES (?, '2026-09-28')", (rid,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _metered([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls, tiers, rid))
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if "Bo" in json.dumps(quality) else 10.0)
    se._run_schedule_job("route-job", rid)
    assert tiers == ["T3", "T4"]                              # the week on T3, the gate's rewrite on T4
    assert calls[1]["kwargs"]["focus"] == ["Saturday 2026-10-10 dinner: 2 servers short"]
    assert finished["result"]["gate"]["kept"] == "regenerated"
    (row,) = _runs(db)
    assert row["start_tier"] is None                          # started on the first rung
    assert row["final_tier"] == "T4" and row["final_model"] == "claude-opus-5-5"
    assert row["escalations"] == 1 and row["attempts"] == 2 and row["status"] == "ok"
    assert row["subject"] == "week:2026-10-05" and row["policy_version"] == wf.POLICY_VERSION
    assert "1 busy day still had a staffing hole after repair" in json.loads(row["reasons"])
    steps = json.loads(row["steps_json"])
    assert [s.get("tier") for s in steps] == ["T3", "T4"] and steps[1]["trigger"] == "rule_breach"
    assert json.loads(row["context_json"])["outcome"] == "rewritten"
    conn = models.get_conn(db)
    usage = conn.execute("SELECT COUNT(*), SUM(cost_usd) FROM ai_usage WHERE correlation_id=?",
                         (row["run_id"],)).fetchone()
    conn.close()
    assert usage[0] == 2 and row["cost_usd"] == pytest.approx(usage[1]) and row["cost_usd"] > 0


def test_a_hard_week_is_recorded_as_pre_routed(db, monkeypatch):
    calls, tiers = [], []
    rid, finished = _gate_harness(monkeypatch, db, [], calls)
    monkeypatch.setattr(labor, "generate_optimized_schedule",
                        _metered([[_line(d, "Ana") for d in WEEK], [_line(WEEK[5], "Bo")]], calls, tiers, rid))
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if "Bo" in json.dumps(quality) else 10.0)
    se._run_schedule_job("route-hard", rid)                    # no draft before: its first schedule
    assert tiers == ["T4", "T4"]
    (row,) = _runs(db)
    assert row["start_tier"] == "T4" and row["final_tier"] == "T4" and row["escalations"] == 0
    assert json.loads(row["context_json"])["pre_route"] == ["the restaurant's first schedule"]


def test_the_evaluated_roster_does_not_start_on_opus():
    # Simple EJ's 67-person roster (prod, 10/7/26) is the week Sonnet 5.5
    # matched Opus on: a threshold under it sent every EJ's week to Opus.
    assert se.HARD_WEEK_ROSTER >= 67 * 1.5
    assert not any("roster" in r for r in se.hard_week_reasons(WEEK, roster_size=67, owner_rules=0,
                                                                prior_generations=2))
