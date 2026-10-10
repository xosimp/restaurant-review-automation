"""Scheduler audit 10/10/26, workstream E — the call planner: 128K and
measured constants (AI-17, AI-2, SCALE-23), fleet priors (AI-17), the run's
caps before every planned call (AI-16), routing by evidence (AI-3), the
cache shared only by same-schema calls (AI-24), no Fable on the route
(AI-30)."""
import pytest

import ai_workflows as wf
import labor
import schedule_engine as se
import schedule_eval
import schedule_output as so


def test_the_ceiling_is_128k_and_the_reserve_is_what_opus_measured():
    assert labor.SCHEDULE_MAX_TOKENS_THINKING == se.SCHEDULE_TOKEN_CEILING == 128000
    # Opus 5.5 medium measured ~46.7k of thinking on the eval week: the old
    # 32k "medium" reserve was under it.
    assert se.THINKING_TOKENS_BY_EFFORT["medium"] >= 46700
    assert se.THINKING_TOKENS_BY_EFFORT["high"] >= 54400
    # A 250-person week (~900-1,100 rows) is one call now, not three to five.
    assert se.CHUNK_ROWS_PER_CALL >= 1100
    assert se._plan_tasks(1100, [f"2026-10-{d:02d}" for d in range(12, 19)], [("A", "Server")] * 250) \
        and len(se._plan_tasks(1100, [f"2026-10-{d:02d}" for d in range(12, 19)], [("A", "Server")] * 250)) == 1


def test_the_assumed_speed_is_the_measured_one():
    assert se.ASSUMED_OUTPUT_TOKENS_PER_SECOND == pytest.approx(120.0)
    # A one-call Simple EJ's week (215 rows) at medium is planned at minutes,
    # not the ~27 minutes the old 26.7 tokens a second gave it.
    secs = se._assumed_call_seconds(215, effort="medium", contract="schema")
    assert 400 < secs < 600
    # The longest planned call still fits the job's deadline with room after it.
    assert se.SCHEDULE_CALL_SECONDS + se.SCHEDULE_POST_MODEL_SECONDS <= se.SCHEDULE_JOB_MAX_SECONDS


def test_a_restaurant_with_no_calls_is_planned_from_the_fleet(monkeypatch):
    calls = []

    def fake(restaurant_id=None, **kw):
        calls.append(restaurant_id)
        if restaurant_id is None:
            return {"fixed": 40000.0, "per_row": 31.0, "source": "fit", "calls": 9, "seconds_fixed": 300.0,
                    "seconds_per_row": 0.25, "seconds_source": "fit", "tokens_per_second": 118.0, "by_kind": {}}
        return {"fixed": None, "per_row": None, "source": "estimate", "calls": 0, "seconds_fixed": None,
                "seconds_per_row": None, "seconds_source": "estimate", "tokens_per_second": None, "by_kind": {}}
    monkeypatch.setattr(so, "call_costs", fake)
    costs = se.call_cost_model(77)
    assert calls == [77, None]
    assert costs["basis"] == "fleet" and costs["source"] == "fit" and costs["seconds_source"] == "fit"
    assert se.call_seconds(200, costs, headroom=False) == pytest.approx(300.0 + 0.25 * 200)


def test_with_no_calls_anywhere_the_measured_prior_plans_the_call(monkeypatch):
    empty = {"fixed": None, "per_row": None, "source": "estimate", "calls": 0, "seconds_fixed": None,
             "seconds_per_row": None, "seconds_source": "estimate", "tokens_per_second": None, "by_kind": {}}
    monkeypatch.setattr(so, "call_costs", lambda restaurant_id=None, **kw: dict(empty))
    monkeypatch.setattr(se, "schedule_route", lambda: wf.Route("T3", "claude-sonnet-5-5", "medium"))
    monkeypatch.setattr(se, "schedule_effort_in_force", lambda: "medium")
    costs = se.call_cost_model(77)
    assert costs["seconds_source"] == "prior" and costs["tokens_per_second"] == 121.0
    # Sonnet medium thinks ~25.8k, not the 48k reserve: planned on its own.
    assert se.call_seconds(215, costs, headroom=False) == pytest.approx((25800 + 1500 + 215 * 30) / 121.0, rel=0.01)
    # A prior is never read as this restaurant's measurement.
    assert not se._call_measured(costs)


def test_a_call_that_would_cross_the_runs_dollar_cap_is_not_started(monkeypatch):
    import ai_orchestrator as orch
    token = se._SCHED_RUN.set({"run_id": "r1", "steps": [], "capped": None})
    try:
        monkeypatch.setattr(se, "schedule_route", lambda: wf.Route("T4", "claude-opus-5-5", "medium"))
        monkeypatch.setattr(orch, "_spent", lambda run_id, db_path=None: (5.2, 0, 0, 4))
        hit = se._run_cap_reached(1000, {}, "schema", heads=250)
        assert hit and hit["cap_usd"] == wf.POLICIES["labor_schedule"].caps.usd and hit["next_usd"] > 0.8
        monkeypatch.setattr(orch, "_spent", lambda run_id, db_path=None: (0.4, 0, 0, 1))
        assert se._run_cap_reached(200, {}, "schema", heads=60) is None
        monkeypatch.setattr(orch, "_spent", lambda run_id, db_path=None: (0.0, 0, 0, 16))
        assert se._run_cap_reached(10, {}, "schema")["calls"] == 16
    finally:
        se._SCHED_RUN.reset(token)
    # outside a run nothing is capped
    assert se._run_cap_reached(10_000, {}, "schema", heads=500) is None


def test_the_planned_calls_stop_at_the_cap_and_keep_the_days_written(monkeypatch):
    """_generate_in_parts reads the cap before every planned call: the slice
    that would cross it is never sent, its days are unwritten "capped"."""
    monkeypatch.setenv("SCHEDULE_PARALLEL_CALLS", "1")
    sent = []
    monkeypatch.setattr(se, "_run_cap_reached",
                        lambda rows, costs=None, contract=None, heads=0, **k: ({"calls": 1, "usd": 5.9, "next_usd": 1,
                                                                                  "cap_calls": 16, "cap_usd": 6.0}
                                                                                 if sent else None))
    import labor as _l

    def gen(analysis, shifts, **kw):
        sent.append(kw.get("week_slice"))
        dates = kw.get("week_slice") or []
        lines = [f"{d},{se._date_of(d).strftime('%A')},Ana,Server,11:00am,3:00pm,4,"
                 for d in dates]
        return {"schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                + "\n".join(lines), "narrative": [], "complete_dates": list(dates), "generation_seconds": 1}
    monkeypatch.setattr(_l, "generate_optimized_schedule", gen)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": "2026-10-12", "restaurant_id": None})
    assert len(sent) == 1
    assert out["unwritten_dates"] and {u["why"] for u in out["unwritten_dates"]} == {"capped"}


def test_routing_reads_the_evals_verdict_for_the_roster_size(db_path):
    assert schedule_eval.route_evidence(250, db_path=db_path) is None
    schedule_eval.record_verdict("published_f1", "T4", "T3", (180, 10000),
                                 {"diff": 0.04, "ci90": (0.01, 0.07), "weeks": 30, "restaurants": 11}, db_path=db_path)
    why = schedule_eval.route_evidence(250, db_path=db_path)
    assert why and "T4 better for rosters of 180-10000" in why
    assert schedule_eval.route_evidence(67, db_path=db_path) is None          # another band: no evidence there
    # An interval that crosses zero names no winner, so it routes nothing.
    schedule_eval.record_verdict("published_f1", "T4", "T3", (180, 10000),
                                 {"diff": 0.01, "ci90": (-0.02, 0.04), "weeks": 30, "restaurants": 11}, db_path=db_path)
    assert schedule_eval.route_evidence(250, db_path=db_path) is None


def test_department_calls_share_no_cache_breakpoint_with_another_department():
    import schedule_prompt as sp
    # One call per department: nothing is shared, so no breakpoint is bought.
    assert sp.cache_ttls(1, 1, 400.0, 0.05) == (None, None)


def test_the_schedule_never_runs_on_fable(monkeypatch):
    monkeypatch.setenv("SCHEDULE_MODEL", "claude-fable-5-1")
    monkeypatch.delenv("SCHEDULE_ALLOW_TOP_TIER", raising=False)
    assert se.start_route().model == "claude-opus-5-5"
    monkeypatch.setenv("SCHEDULE_ALLOW_TOP_TIER", "1")
    assert se.start_route().model == "claude-fable-5-1"
