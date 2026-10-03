"""The ranked repair loop and the job around it (schedule audit 10/3/26,
workstream B1).

P-47  the passes ran once each in a fixed order and undid each other: one
      ranked loop now, every change held to the rules ranked above it.
E-18  hours added after the trim were never checked against the budget: the
      budget is reconciled after the last stage that adds hours, and an
      overrun says which rules put the hours there.
P-3 / P-17 / P-16  a pass that raised was printed and forgotten (one try
      around four passes, bare excepts in the input build, a scoring crash
      that removed the quality blockers): captured, said, and a hard-rule or
      manager stage that did not run holds the publish.
P-11 / SQ-21  the last station pass ran after the sweep and the score.
P-15  fix lines pointed at the wrong rows; unfixed came from the first pass.
P-41  the rules were built before the model call and reused.
P-43 / SQ-20  the quality gate ignored hard rules, manager gaps and leader
      caps, and kept a rewrite on its score alone.
P-36  the same inputs were read again and again in one generation.
P-24  no per-stage timing was kept; nothing alerted on slow generations.
P-26  a Studio save threw the generation's review away.
P-21 / L-11  the solver was a coin flip for an experiment that could never
      conclude.
"""
import datetime as dt
import inspect
import json
import sys

import pytest

# Imported before any fixture patches models.get_conn (see
# tests/test_edge_sched_economics.py).
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401
from flask import Flask

import auth
import client_api
import labor
import mobile_api
import models
import ops
import schedule_economics as econ
import schedule_engine as se
import schedule_experiments as sx
import schedule_optimizer
import schedule_rules as sr
import schedule_versions as sv
from models import create_restaurant, Restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON, TUE, WED, THU, FRI, SAT, SUN = WEEK
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
EXP = sx.EXPERIMENTS[0]


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
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    monkeypatch.delenv(sx.PIN_ENV, raising=False)
    return db_path


# ── helpers ────────────────────────────────────────────────────────────────

def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(d, emp, role="Server", start="4:00pm", end="10:00pm", hours=6, **extra):
    r = {"date": d, "day": DAYS[WEEK.index(d)], "employee": emp, "role": role, "shift_start": start,
         "shift_end": end, "scheduled_hours": str(hours), "notes": ""}
    r.update(extra)
    return r


def _x(c, editable=None, **result):
    res = {"week_dates": list(WEEK), "week_days": list(DAYS), "roster": list(c.roster_names or [])}
    res.update(result)
    return se.RepairContext(1, res, c, editable=editable)


def _only(monkeypatch, **fns):
    """Every stage of the loop a no-op except `fns` — the loop's own logic."""
    table = {k: (lambda rows, x: {"rows": rows}) for k in se._STAGE_FNS}
    table.update(fns)
    monkeypatch.setattr(se, "_STAGE_FNS", table)


def _restaurant(db_path, people=(), **cols):
    rid = create_restaurant(Restaurant(name="Loop Grill", owner_email="loop@x.com"), db_path=db_path)
    if cols:
        c = models.get_conn(db_path)
        c.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        c.commit()
        c.close()
    for name, role in people:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _line(d, emp, role="Server", start="4:00pm", end="10:00pm", hours=6):
    return f"{d},{DAYS[WEEK.index(d)]},{emp},{role},{start},{end},{hours},"


def _base(lines, **extra):
    out = {"ok": True, "schedule_csv": HEADER + "\n" + "\n".join(lines), "week_dates": list(WEEK),
           "week_days": list(DAYS), "summary": [], "hours_budget": 0, "daily_target_hours": {},
           "labor_target": 30, "blended_rate": 20.0}
    out.update(extra)
    return out


def _run(monkeypatch, rid, lines, builds=None, job="b1-job", **extra):
    """The job with the model and the input build stubbed: the first build
    returns `lines`; `builds` are the next builds (a redo), each a result
    dict. Returns {"status", "result"}."""
    queue = [_base(lines, **extra)] + list(builds or [])

    def build(r, week_start=None, focus=None, dates=None, prior_rows=None, instruction=None):
        return dict(queue.pop(0)) if len(queue) > 1 else dict(queue[0])
    monkeypatch.setattr(se, "_build_schedule_result", build)
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job",
                        lambda job_id, status, result: done.update(status=status, result=result))
    se._run_schedule_job(job, rid)
    return done


def _no_search(monkeypatch):
    """Keep the solver and the optimizer out of a scenario about the rules."""
    monkeypatch.setenv(sx.PIN_ENV, "off")
    monkeypatch.setattr(schedule_optimizer, "optimize", lambda rows, inputs=None, **k: {
        "rows": [dict(r) for r in rows], "changes": [], "before_score": None, "after_score": None,
        "quality": {}, "evaluations": 0, "seconds": 0.0, "stopped": "test"})


def _history(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return dict(conn.execute("SELECT * FROM schedule_history WHERE id=?", (hid,)).fetchone())
    finally:
        conn.close()


# ══ P-47: one ranked loop ═══════════════════════════════════════════════════

def test_a_stage_never_keeps_a_change_that_leaves_a_rule_ranked_above_it_worse(monkeypatch):
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ana"], active={"max", "ana"})
    rows = [_row(FRI, "Max", "Manager"), _row(FRI, "Ana")]

    def drops_the_manager(rows, x):
        return {"rows": [r for r in rows if r["employee"] != "Max"]}

    def ends_ana_at_nine(rows, x):
        return {"rows": [dict(r, shift_end="9:00pm", scheduled_hours="5") if r["employee"] == "Ana" else r
                         for r in rows]}
    _only(monkeypatch, optimizer=drops_the_manager, stagger=ends_ana_at_nine)
    out = se.repair_week(rows, _x(c))
    # The quality search would have left Ana alone with no manager: refused,
    # by the manager rule's rank; the quality change that harms nothing kept.
    assert {r["employee"] for r in out["rows"]} == {"Max", "Ana"}
    assert next(r for r in out["rows"] if r["employee"] == "Ana")["shift_end"] == "9:00pm"
    refused = [r for r in out["refused"] if r["stage"] == "optimizer"]
    assert refused and refused[0]["worse"][0]["tier"] == sr.TIER_MANAGER
    assert out["converged"]


def test_a_legality_repair_may_cost_a_lower_rank_only_when_the_week_ends_more_legal(monkeypatch):
    rows = [_row(FRI, "Kid", "Host", "5:00pm", "11:00pm")]
    x = _x(_c())
    before = {"by_id": {("minor_late", FRI, "kid"): 1.0}, "labels": {}, "manager": {}}

    def profile(by_id, manager=None):
        return lambda *a, **k: {"by_id": dict(by_id), "labels": {}, "manager": dict(manager or {})}
    # Cut to the minor's limit with nobody to take the stretch: more legal, so
    # the floor may pay for it (owner, 10/2/26: legality outranks coverage).
    monkeypatch.setattr(sr, "breach_profile", profile({("coverage_floor", FRI, "host", "night"): 1.0}))
    worse, _ = se._judge(sr.TIER_PERSON, rows, [dict(rows[0], shift_end="10:00pm")], before, x)
    assert worse == []
    # The same cost with the week no more legal: refused.
    monkeypatch.setattr(sr, "breach_profile", profile({("minor_late", FRI, "kid"): 1.0,
                                                        ("coverage_floor", FRI, "host", "night"): 1.0}))
    worse, _ = se._judge(sr.TIER_PERSON, rows, [dict(rows[0], shift_start="6:00pm")], before, x)
    assert worse and worse[0]["id"][0] == "coverage_floor"
    # Below the manager's rank, a stage never pays with unmanaged minutes.
    monkeypatch.setattr(sr, "breach_profile", profile({}, {FRI: 60}))
    worse, _ = se._judge(sr.TIER_COVERAGE, rows, [dict(rows[0], shift_end="9:00pm")],
                         {"by_id": {}, "labels": {}, "manager": {}}, x)
    assert worse and worse[0]["tier"] == sr.TIER_MANAGER


def test_a_pinned_row_and_a_day_the_redo_keeps_never_change(monkeypatch):
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ana", "Bo"], active={"max", "ana", "bo"})
    rows = [_row(THU, "Ana"), _row(FRI, "Max", "Manager", _pinned="manager_plan"), _row(FRI, "Bo")]

    def retimes_everyone(rows, x):
        return {"rows": [dict(r, shift_end="9:00pm") for r in rows]}

    def retimes_bo(rows, x):
        return {"rows": [dict(r, shift_end="9:00pm") if r["employee"] == "Bo" else r for r in rows]}
    _only(monkeypatch, stagger=retimes_everyone, role_times=retimes_bo)
    out = se.repair_week(rows, _x(c, editable={FRI}))
    by = {r["employee"]: r for r in out["rows"]}
    assert by["Ana"]["shift_end"] == "10:00pm"                          # the kept Thursday
    assert by["Max"]["shift_end"] == "10:00pm" and by["Max"]["_pinned"]   # the plan's row
    assert by["Bo"]["shift_end"] == "9:00pm"                             # a redone day may change
    assert any(r["stage"] == "stagger" and r["worse"][0]["tier"] == -1 for r in out["refused"])


def test_stages_trading_the_same_change_stop_at_the_bound_and_report_nothing(monkeypatch):
    c = _c(roster_names=["Ana", "Cy"], active={"ana", "cy"})

    def adds_cy(rows, x):
        if any(r["employee"] == "Cy" for r in rows):
            return {"rows": rows}
        return {"rows": rows + [_row(SAT, "Cy", start="5:00pm", end="9:00pm", hours=4)]}

    def takes_cy_off(rows, x):
        return {"rows": [r for r in rows if r["employee"] != "Cy"],
                "trimmed": [dict(_row(SAT, "Cy", start="5:00pm", end="9:00pm", hours=4), kind="removed", hours=4,
                                 reason="test")]}
    _only(monkeypatch, top_up=adds_cy, optimizer=takes_cy_off)
    out = se.repair_week([_row(SAT, "Ana")], _x(c))
    assert not out["converged"] and out["cycles"] <= se.REPAIR_MAX_CYCLES
    assert [r["employee"] for r in out["rows"]] == ["Ana"]
    assert out["trimmed"] == []          # the loop made Cy's row and took it away: nothing happened to the week


def test_the_loop_runs_its_stages_in_rank_order_until_a_cycle_changes_nothing(monkeypatch):
    order = [k for k, *_r in se.REPAIR_STAGES]
    tiers = [se._STAGE_TIER[k] for k in order]
    assert tiers == sorted(tiers)                                    # ranked
    assert order[:3] == ["person", "replace", "manager"]
    assert {"floors", "stations", "close_out"} <= {k for k in order if se._STAGE_TIER[k] == sr.TIER_COVERAGE}
    assert order.index("overtime") < order.index("min_hours") < order.index("budget") < order.index("solver")
    seen = []

    def once(key):
        def fn(rows, x):
            seen.append(key)
            if key == "floors" and not any(r["employee"] == "Bo" for r in rows):
                return {"rows": rows + [_row(FRI, "Bo")]}
            return {"rows": rows}
        return fn
    _only(monkeypatch, **{k: once(k) for k in order})
    out = se.repair_week([_row(FRI, "Ana")], _x(_c(roster_names=["Ana", "Bo"], active={"ana", "bo"})))
    assert out["converged"] and out["cycles"] == 2                   # a change, then a cycle with none
    assert seen[:len(order)] == order                                 # every stage, in rank order
    assert seen.count("solver") == 1 and seen.count("top_up") == 1      # once a generation
    assert seen.count("manager") == 2                                   # every cycle


def test_an_overtime_pass_that_raises_is_said_and_the_rest_still_run(db, monkeypatch):
    rid = _restaurant(db, [("Max", "General Manager"), ("Ana", "Server")])
    _no_search(monkeypatch)
    caught = []
    monkeypatch.setattr(se._ops, "capture", lambda exc, job="unknown", context="", **k: caught.append(job))
    monkeypatch.setattr(sr, "rebalance_overtime", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    out = _run(monkeypatch, rid, [_line(FRI, "Ana")], roster_roles={"Max": "General Manager", "Ana": "Server"})
    res = out["result"]
    assert out["status"] == "done"
    # P-3: one try used to cover overtime, close-out, the manager rule and the
    # person repairs; the manager rule ran anyway and the failure is said.
    assert res["manager_coverage"]["added"] + res["manager_coverage"]["extended"] >= 1
    assert "schedule_stage_overtime" in caught
    fail = [f for f in res["review"]["stage_failures"] if f["stage"] == "overtime"]
    assert fail and fail[0]["blocks_publish"] is False
    assert "The overtime rebalance didn't run on this draft — overtime a teammate could take may still be in it." \
        in res["review"]["lines"]


def test_a_manager_repair_that_raises_holds_the_publish(db, monkeypatch):
    rid = _restaurant(db, [("Max", "General Manager"), ("Ana", "Server")])
    _no_search(monkeypatch)
    monkeypatch.setattr(sr, "cover_manager_gaps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    res = _run(monkeypatch, rid, [_line(FRI, "Ana")], roster_roles={"Max": "General Manager", "Ana": "Server"})["result"]
    fail = [f for f in res["review"]["stage_failures"] if f["stage"] == "manager"]
    assert fail and fail[0]["blocks_publish"] is True
    line = ("⚠ The manager-every-minute repair didn't run on this draft — check a manager is on from open to "
            "close every day before you publish.")
    assert line in res["review"]["lines"] and res["stage_failures"][0]["stage"] == "manager"
    gate = client_api.publish_review(rid, schedule_id=res["history_id"], today=dt.date(2026, 9, 20))
    assert any(b["key"] == "stage:manager" or b["text"] == line.lstrip("⚠ ") for b in gate["blockers"])


def test_an_input_the_draft_went_without_is_named_in_the_review(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    _no_search(monkeypatch)

    def build(r, week_start=None, **k):
        # An input that failed while the inputs were gathered (the weather,
        # a holiday list — bare excepts until P-17).
        se._soft_fail("weather", RuntimeError("NWS down"), r)
        se._soft_fail("revenue", RuntimeError("budget table"), r)
        return _base([_line(FRI, "Ana")])
    monkeypatch.setattr(se, "_build_schedule_result", build)
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: done.update(status=s, result=r))
    se._run_schedule_job("inputs-job", rid)
    lines = done["result"]["review"]["lines"]
    said = [ln for ln in lines if ln.startswith("Some of what Cavnar AI reads for a draft couldn't be read")]
    assert said and "the weather forecast" in said[0] and "your sales budget for the week" in said[0]
    assert done["result"]["review"]["inputs_missing"] == ["weather", "revenue"]


def test_the_input_build_says_every_input_it_goes_without():
    src = inspect.getsource(se._build_schedule_result)
    for what in ("upcoming_events", "weather", "prior_week", "demand_forecast", "revenue", "labor_target_source",
                 "reservation_feed", "shifts_file"):
        assert f'_soft_fail("{what}"' in src, what
    # The silent handlers P-17 named are gone from the input build.
    assert "except Exception:\n        pass\n    # Who is experienced" not in src
    assert "except Exception:\n        weather_forecast = []" not in src


def test_a_scoring_crash_is_a_stage_failure_with_an_owner_cause_and_holds_the_publish(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    _no_search(monkeypatch)
    caught = []
    monkeypatch.setattr(se._ops, "capture", lambda exc, job="unknown", context="", **k: caught.append(job))
    monkeypatch.setattr(se, "_score_schedule_quality", lambda *a, **k: (_ for _ in ()).throw(KeyError("dim")))
    res = _run(monkeypatch, rid, [_line(FRI, "Ana")])["result"]
    assert res["quality"]["checked"] is False and "we've been alerted" in res["quality"]["error"]
    assert "KeyError" not in res["quality"]["error"]          # owner words, never the exception
    assert "schedule_stage_quality" in caught
    assert any(f["stage"] == "quality" for f in res["review"]["stage_failures"])
    gate = client_api.publish_review(rid, schedule_id=res["history_id"], today=dt.date(2026, 9, 20))
    q = [b for b in gate["blockers"] if b["key"] == "quality_unchecked"]
    assert q and "we've been alerted" in q[0]["text"]


def test_settings_the_rules_could_not_read_are_named_in_the_generation_review(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    _no_search(monkeypatch)
    real = sr.build_constraints

    def with_a_problem(*a, **k):
        c = real(*a, **k)
        c.input_problems.append({"source": "roster", "name": "Ana", "error": "ValueError: bad window"})
        return c
    monkeypatch.setattr(sr, "build_constraints", with_a_problem)
    res = _run(monkeypatch, rid, [_line(FRI, "Ana")])["result"]
    line = ("⚠ Settings for Ana couldn't be read — fix them in Team; this week wasn't checked against their "
            "rules")
    assert line in res["review"]["lines"] and line in res["review"]["lead_lines"]
    assert res["review"]["input_problems"] == [{"source": "roster", "name": "Ana"}]
    assert client_api._input_problem_text({"name": "Ana"}) == line[2:]      # one wording with the gate


# ══ E-18: the budget, reconciled and honest ═════════════════════════════════

def test_hours_the_manager_rule_adds_past_the_budget_are_named_with_the_rule(db, monkeypatch):
    rid = _restaurant(db, [("Max", "General Manager"), ("Ana", "Server")])
    _no_search(monkeypatch)
    res = _run(monkeypatch, rid, [_line(FRI, "Ana")], hours_budget=6, daily_target_hours={FRI: 6},
               roster_roles={"Max": "General Manager", "Ana": "Server"})["result"]
    rv = res["review"]
    # Ana is the night's only server and Max its only manager: the trim can
    # take neither, so the week is over — and says what put it there.
    assert rv["lines"][0].startswith("⚠ 12h scheduled against a 6h budget — 6h over the ceiling")
    assert "The rules that come before the budget added 6h: a manager every minute 6h." in rv["lines"]
    assert res["repair"]["budget"]["added_above_budget"] == {"manager": 6.0}
    assert res["repair"]["converged"]


def test_a_budget_on_an_assumed_wage_is_said_and_not_trimmed_to(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server")])
    _no_search(monkeypatch)
    caveat = "Budget assumes $26/hr — set pay rates. Shifts are not cut to it until then."
    res = _run(monkeypatch, rid, [_line(FRI, "Ana"), _line(FRI, "Bo")], hours_budget=6,
               daily_target_hours={FRI: 6}, budget_basis={"caveat": caveat, "trim_ok": False})["result"]
    assert res["trimmed"] == [] and f"⚠ {caveat}" in res["review"]["lines"]
    assert res["budget_basis"]["trim_ok"] is False                          # in the payload (E's hand-off)


# ══ P-15: stable row ids ═══════════════════════════════════════════════════

def test_fix_lines_point_at_their_rows_after_a_later_stage_takes_a_row_out(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Server"), ("Di", "Server")])
    _no_search(monkeypatch)

    def bo_to_cy(rows, c, **k):
        out = [dict(r) for r in rows]
        i = next((i for i, r in enumerate(out) if r["employee"] == "Bo"), None)
        if i is None:
            return {"rows": out, "fixes": [], "sweeps": 0}
        out[i]["employee"] = "Cy"
        return {"rows": out, "fixes": [{"index": i, "from": "Bo", "to": "Cy", "kind": "minor", "reason": "test"}],
                "sweeps": 1}
    monkeypatch.setattr(sr, "fix_person_breaches", bo_to_cy)
    calls = []

    def second_call_takes_the_first_row(rows, budget, targets, **k):
        calls.append(1)
        rows = list(rows)
        if len(calls) == 2 and rows and rows[0]["employee"] == "Ana":
            gone = rows.pop(0)
            return rows, [dict(gone, hours=6.0, kind="removed", reason="test")], 6.0
        return rows, [], 0.0
    monkeypatch.setattr(econ, "trim_to_budget", second_call_takes_the_first_row)
    res = _run(monkeypatch, rid, [_line(MON, "Ana"), _line(TUE, "Bo"), _line(WED, "Di")],
               hours_budget=1, daily_target_hours={MON: 1})["result"]
    rows = res["preview_rows"]
    fixes = [f for f in res["review"]["fixes"] if f["kind"] == "minor"]
    assert fixes and all(rows[f["index"]]["employee"] == "Cy" and rows[f["index"]]["date"] == TUE for f in fixes)
    assert all(rows[f["index"]]["_rid"] == f["row_id"] for f in fixes)
    assert [t["employee"] for t in res["trimmed"]] == ["Ana"]
    for u in res["review"]["unfixed"]:
        assert rows[u["index"]]["employee"] == u["employee"]


# ══ P-11 / SQ-21: nothing changes the rows after they are swept and scored ═══

def test_the_station_pass_runs_before_the_sweep_and_the_score(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Jo", "Line Cook")])
    _no_search(monkeypatch)
    real = sr.build_constraints

    def with_stations(*a, **k):
        c = real(*a, **k)
        c.stations = {"stations": [{"name": "Grill", "parts": ["night"]}]}
        return c
    monkeypatch.setattr(sr, "build_constraints", with_stations)
    order = []

    def stations(rows, *a, **k):
        order.append("stations")
        if any(r["employee"] == "Jo" for r in rows):
            return rows, 0, []
        return rows + [_row(FRI, "Jo", "Line Cook")], 1, []
    monkeypatch.setattr(se, "_ensure_station_coverage", stations)
    monkeypatch.setattr(se, "station_report", lambda *a, **k: {"gaps": []})
    real_score = se._score_schedule_quality
    scored = {}

    def score(rid_, rows, result, **k):
        order.append("score")
        scored["rows"] = [(r["employee"], r["date"]) for r in rows]
        return real_score(rid_, rows, result, **k)
    monkeypatch.setattr(se, "_score_schedule_quality", score)
    res = _run(monkeypatch, rid, [_line(FRI, "Ana")])["result"]
    assert "stations" not in order[order.index("score"):]
    assert sorted(scored["rows"]) == sorted((r["employee"], r["date"]) for r in res["preview_rows"])
    swept = {(v["employee"], v["date"]) for v in res["rule_violations"] if v.get("employee")}
    assert swept <= set(scored["rows"])


# ══ P-41: the rules after the model call ═══════════════════════════════════

def test_time_off_approved_while_the_model_writes_is_in_the_draft(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server")])
    _no_search(monkeypatch)
    before = sr.build_constraints(rid, list(WEEK), list(DAYS), models.get_restaurant(rid))

    def build(r, week_start=None, **k):
        # The inputs were gathered (the rules among them) — then, while the
        # model was writing, Ana's Friday off was approved.
        row, err = time_off.request_time_off(rid, "Ana", FRI, FRI, db_path=db, today=dt.date(2026, 9, 1))
        assert not err, err
        time_off.decide(rid, row["id"], True, db_path=db)
        return _base([_line(FRI, "Ana")], constraints=before)
    monkeypatch.setattr(se, "_build_schedule_result", build)
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: done.update(status=s, result=r))
    se._run_schedule_job("timeoff-job", rid)
    res = done["result"]
    ana = [r for r in res["preview_rows"] if r["employee"] == "Ana" and r["date"] == FRI]
    off = [v for v in res["rule_violations"] if v["kind"] == "approved_time_off"]
    assert off or not ana, "Ana's Friday is either handed to somebody or flagged — never left as legal"
    assert "Time off approved while this draft was being written is in it: Ana 10/9/26." in res["review"]["lines"]


def test_the_rules_are_read_again_and_only_take_in_who_can_work():
    c = _c(roster_names=["Ana", "Bo"], active={"ana", "bo"}, blocked_dates={"bo": {MON: "on approved time off"}},
           pending_off={"ana": {FRI}})
    fresh = _c(blocked_dates={"ana": {FRI: "on approved time off"}, "bo": {MON: "on approved time off"}},
               pending_off={}, inactive={"bo"})
    c.daypart_avail = {"ana": {"Monday": "morning"}}
    fresh.unavailable_days = {"ana": {"Sunday"}}
    fresh.daypart_avail = {"ana": {"Monday": "night", "Tuesday": "morning"}}
    newly = se._take_new_time_off(c, fresh)
    assert newly == [{"name": "Ana", "dates": [FRI]}]
    assert c.blocked_dates["ana"] == {FRI: "on approved time off"} and FRI not in c.pending_off["ana"]
    assert "bo" in c.inactive and "bo" not in c.active
    # Availability marked meanwhile is taken in at its narrower reading.
    assert c.unavailable_days["ana"] == {"Sunday"}
    assert c.daypart_avail["ana"] == {"Monday": "off", "Tuesday": "morning"}


# ══ P-43 / SQ-20: the quality gate ═════════════════════════════════════════

def test_the_gate_rewrites_a_day_a_free_manager_could_cover():
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ana"], active={"max", "ana"})
    rows = [_row(FRI, "Ana")]
    result = {"quality": {"checked": True, "shifts": []}, "rule_violations": sr.violations(rows, c),
              "rows": rows, "constraints": c}
    gate = se._quality_gate(result)
    assert gate and gate["dates"] == [FRI] and "manager" in gate["triggers"][FRI]
    assert any("no manager on" in f for f in gate["focus"])
    # Nobody who manages can work Friday: a rewrite cannot fix it.
    c.blocked_dates = {"max": {FRI: "on approved time off"}}
    assert se._quality_gate(dict(result, rule_violations=sr.violations(rows, c))) is None


def _leader_shift(misses=None, shortfalls=None, cap="leadership"):
    dim = {"key": cap, "weaknesses": ["Needs 1 bartender scoring 4 or above, found 0."],
           "facts": {"misses": misses or [], "shortfalls": shortfalls or []}}
    return {"date": SAT, "day": "Saturday", "daypart": "night", "scored": True, "score": 40, "capped_by": cap,
            "profile": {"demand": "peak"}, "dimensions": [dim]}


def test_a_leader_or_strength_cap_triggers_the_gate_only_when_a_qualified_person_is_free():
    c = _c(roster_names=["Bea", "Cy", "Ana"], active={"bea", "cy", "ana"},
           known_roles={"bea": {"bartender"}, "cy": {"server"}, "ana": {"server"}})
    rows = [_row(SAT, "Ana")]
    miss = {"role": "Bartender", "min_score": 4, "attribute": None, "why": "nobody_on"}
    result = {"quality": {"checked": True, "shifts": [_leader_shift([miss])]}, "rule_violations": [],
              "rows": rows, "constraints": c, "operational_scores": {"Bea": 4, "Cy": 5}}
    gate = se._quality_gate(result)
    assert gate and gate["dates"] == [SAT] and gate["triggers"][SAT] == ["leadership"]
    assert se._quality_gate(dict(result, operational_scores={"Bea": 3, "Cy": 5})) is None   # Cy isn't a bartender
    strength = _leader_shift(shortfalls=[{"role": "Bartender", "bar": 4.0}], cap="operational_strength")
    assert se._quality_gate(dict(result, quality={"checked": True, "shifts": [strength]}))["dates"] == [SAT]


def test_a_rewrite_with_more_unmanaged_minutes_is_never_kept(db, monkeypatch):
    rid = _restaurant(db, [("Max", "General Manager"), ("Ana", "Server")])
    _no_search(monkeypatch)
    # The backstop off, so the rewrite's missing manager stays missing.
    monkeypatch.setattr(sr, "cover_manager_gaps", lambda rows, c, **k: {"rows": [dict(r) for r in rows],
                                                                       "extended": [], "added": [], "left": []})
    gates = [{"dates": [FRI], "focus": ["Friday 2026-10-09 dinner: short"], "reason": "test"}]
    monkeypatch.setattr(se, "_quality_gate", lambda result: gates.pop(0) if gates else None)
    monkeypatch.setattr(se, "_gate_local", lambda quality, dates: 50.0 if quality.get("rewrite") else 10.0)
    real_score = se._score_schedule_quality

    def score(rid_, rows, result, **k):
        # The rewrite scores as well as the draft for the week and better on
        # its day: kept on the score alone, it used to be.
        q, w = real_score(rid_, rows, result, **k)
        q["rewrite"] = not any(r["employee"] == "Max" for r in rows)
        q["score"] = 90
        return q, w
    monkeypatch.setattr(se, "_score_schedule_quality", score)
    first = [_line(FRI, "Max", "General Manager"), _line(FRI, "Ana")]
    rewrite = _base([_line(FRI, "Ana")])
    done = _run(monkeypatch, rid, first, builds=[rewrite], roster_roles={"Max": "General Manager", "Ana": "Server"})
    gate = done["result"]["gate"]
    assert gate["kept"] == "original" and "no manager" in gate["reason"]


def test_the_gate_keeps_a_rewrite_only_when_no_rule_is_worse():
    two, one = {"hard": 2, "unmanaged": 0}, {"hard": 1, "unmanaged": 0}
    assert se._gate_keeps(10, 50, 70, 70, one, two) == (False, "rules")       # a better score, one more breach
    assert se._gate_keeps(50, 40, 60, 70, two, one) == (True, "rules")        # a breach fixed outranks the score
    assert se._gate_keeps(10, 50, 70, 70, one, {"hard": 1, "unmanaged": 30})[0] is False
    assert se._gate_keeps(10, 50, 70, 70, one, one) == (True, "score")


# ══ P-36: one frozen context ═══════════════════════════════════════════════

def _real_build(monkeypatch, db, rid, lines):
    """_build_schedule_result over the test restaurant, the model stubbed."""
    import time_utils
    import weather
    monkeypatch.setattr(labor, "load_shifts_for_restaurant",
                        lambda r: [{"date": "2026-09-28", "employee": "Ana", "role": "Server"},
                                   {"date": "2026-09-28", "employee": "Bo", "role": "Server"}])
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r, **k: {"is_live": True, "blended_rate": 20.0})
    monkeypatch.setattr(labor, "build_demand_forecast", lambda r: {"ok": False})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 1, 9, 0))
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: dt.datetime(2026, 10, 5))

    def fake(analysis, shifts, week_slice=None, prior_rows=None, **kwargs):
        return {"schedule_csv": HEADER + "\n" + "\n".join(lines), "narrative": ["ok"], "generation_seconds": 1.0,
                "truncated": False, "stop_reason": "end_turn", "week_dates": WEEK, "hours_budget": 0}
    monkeypatch.setattr(labor, "generate_optimized_schedule", fake)
    done = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: done.update(status=s, result=r))
    return done


def test_a_generation_reads_each_input_once(db, monkeypatch):
    rid = _restaurant(db, [("Max", "General Manager"), ("Ana", "Server"), ("Bo", "Server")], module_labor=1)
    monkeypatch.setenv(sx.PIN_ENV, "solver")
    done = _real_build(monkeypatch, db, rid, [_line(d, "Ana") for d in WEEK[:5]] + [_line(FRI, "Bo")])
    counts = {}

    def counted(name, fn):
        def wrap(*a, **k):
            counts[name] = counts.get(name, 0) + 1
            return fn(*a, **k)
        return wrap
    monkeypatch.setattr(models, "get_employee_tenure", counted("tenure", models.get_employee_tenure))
    monkeypatch.setattr(econ, "hourly_profile", counted("hourly_profile", econ.hourly_profile))
    monkeypatch.setattr(se, "_prior_week_assignments", counted("prior_week", se._prior_week_assignments))
    monkeypatch.setattr(staff_settings, "experienced_names", counted("experienced", staff_settings.experienced_names))
    se._run_schedule_job("frozen-job", rid)
    assert done["status"] == "done", done
    assert counts.get("tenure") == 1 and counts.get("experienced") == 1, counts
    assert counts.get("hourly_profile") == 1 and counts.get("prior_week") == 1, counts


def test_every_model_call_of_a_generation_shares_one_read(db, monkeypatch):
    import people
    rid = _restaurant(db, [("Ana", "Server")])
    calls = []
    monkeypatch.setattr(people, "held_roles", lambda r, *a, **k: calls.append(r) or [])
    with se.frozen_inputs():
        labor._held_roles_by_name(rid, ["Ana"])
        labor._held_roles_by_name(rid, ["Ana", "Bo"])          # a second call, another department's people
    assert calls == [rid]
    labor._held_roles_by_name(rid, ["Ana"])                    # outside a generation: read as before
    assert calls == [rid, rid]
    src = inspect.getsource(labor.generate_optimized_schedule)
    assert '_frozen_ns(("attendance_events", restaurant_id)' in src and '_frozen_rd(("readiness", restaurant_id' in src


# ══ P-24: per-stage timing, kept, and an alert on the p95 ═══════════════════

def test_each_stage_is_timed_and_kept_with_the_week(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server")])
    _no_search(monkeypatch)
    res = _run(monkeypatch, rid, [_line(FRI, "Ana")], generation_seconds=2.5)["result"]
    stages = res["stage_seconds"]
    for k in ("inputs", "model", "parse", "rules", "signals", "repair", "repair.manager", "sweep", "score",
              "save", "total"):
        assert k in stages, k
    assert stages["model"] == 2.5 and stages["total"] >= stages["repair"]
    row = _history(db, res["history_id"])
    assert json.loads(row["stage_seconds_json"])["total"] == stages["total"]
    assert row["total_seconds"] == pytest.approx(stages["total"])


def test_the_platform_check_warns_when_generations_run_slow(db, monkeypatch):
    rid = _restaurant(db)
    conn = models.get_conn(db)
    for secs in [120.0] * 3 + [900.0] * 4:
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, schedule_csv, total_seconds) "
                     "VALUES (?,?,?,?)", (rid, MON, HEADER, secs))
    conn.commit()
    conn.close()
    lat = ops.schedule_generation_latency(db)
    assert lat["n"] == 7 and lat["p95"] == 900.0 and lat["slow"]
    out = ops.check_platform_sla(send=False, db_path=db)
    assert out["schedule_generation"]["slow"] and any(p.startswith("Schedule generations are slow")
                                                      for p in out["problems"])
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET total_seconds=60")
    conn.commit()
    conn.close()
    assert not ops.schedule_generation_latency(db)["slow"]


# ══ P-26: a Studio save keeps the generation's review ═══════════════════════

def _mobile_app(db):
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _bearer(db, rid):
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def test_a_studio_save_keeps_the_generations_review_and_refreshes_what_the_rows_decide(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Server")], module_labor=1)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: {"is_live": True})
    gen_rows = [_row(THU, "Ana"), _row(FRI, "Cy"), _row(SAT, "Bo")]
    csv_text = HEADER + "\n" + "\n".join(",".join(r[k] for k in se._COLS_PINNED) for r in gen_rows)
    hid = models.save_schedule_history(rid, MON, SUN, 18, 12, 30, csv_text, [], db_path=db)
    sv.append(rid, hid, "generated", csv_text, saved_by="Cavnar AI")
    over = "⚠ 18h scheduled against a 12h budget — 6h over the ceiling"
    rule = "⚠ Bo — Saturday 4:00pm: on approved time off"
    unchecked = "Your rule “2 servers on Friday” isn't one Cavnar AI can check automatically — check this draft against it"
    trim_line = "Trimmed 4h — 1 shift removed"
    review = {"hard": 1, "soft": 0, "lines": [over, rule, unchecked, trim_line], "rule_lines": [rule],
              "budget_lines": [over], "lead_lines": [],
              "fixes": [{"index": 1, "from": "Bo", "to": "Cy", "kind": "overtime", "reason": "no overtime",
                         "row": {"employee": "Cy", "date": FRI, "role": "Server", "shift_start": "4:00pm",
                                 "shift_end": "10:00pm"}}],
              "trimmed": [{"date": WED, "employee": "Di", "hours": 4, "kind": "removed", "reason": "budget"}],
              "owner_rules_unchecked": ["2 servers on Friday"],
              "manager_coverage": {"left": [{"date": SAT, "from": "4:00pm", "to": "6:00pm", "minutes": 120}],
                                   "shortfall": {"text": "x"}},
              "stage_failures": [{"stage": "overtime", "blocks_publish": False}]}
    se._annotate_history(hid, rid, review=review)
    # The owner moves Cy's Friday to the top and takes Saturday out: within
    # the budget now, Bo's breach gone.
    edited = [_row(FRI, "Cy"), _row(THU, "Ana")]
    r = _mobile_app(db).test_client().post("/mobile/api/labor/schedule/score", headers=_bearer(db, rid),
                                           json={"rows": edited, "save": True, "history_id": hid, "version": 1})
    assert r.status_code == 200, r.get_json()
    stored = json.loads(_history(db, hid)["review_json"])
    assert stored["fixes"][0]["index"] == 0                     # re-pointed at Cy's Friday, where it is now
    assert stored["trimmed"] == review["trimmed"] and stored["owner_rules_unchecked"] == ["2 servers on Friday"]
    assert stored["stage_failures"] == review["stage_failures"]
    assert unchecked in stored["lines"] and trim_line in stored["lines"]
    assert over not in stored["lines"] and rule not in stored["lines"]       # what the rows decide, read again
    assert stored["edited"] and stored["hard"] == 0
    assert stored["manager_coverage"]["left"] == []                          # Saturday is no longer worked
    assert r.get_json()["review"]["fixes"][0]["index"] == 0


def test_refresh_review_says_an_over_budget_week_fresh_and_keeps_an_old_reviews_plain_lines():
    c = _c(roster_names=["Ana"], active={"ana"})
    rows = [_row(FRI, "Ana"), _row(SAT, "Ana")]
    viols = sr.violations(rows, c)
    old = {"lines": ["⚠ old breach", "A plain line the generation said"], "fixes": []}
    out = se.refresh_review(old, viols, rows, c, hours_budget=6)
    assert out["lines"][0] == "⚠ 12h scheduled against a 6h budget — 6h over the ceiling"
    assert "⚠ old breach" not in out["lines"] and "A plain line the generation said" in out["lines"]


# ══ P-21 / L-11: the solver is not a coin flip for an experiment that cannot conclude ══

def test_a_lone_restaurant_runs_the_solver_every_week_recorded_as_pinned(db):
    rid = _restaurant(db)
    weeks = [(dt.date(2026, 10, 12) + dt.timedelta(weeks=k)).isoformat() for k in range(12)]
    coin = [w for w in weeks if sx.hashed_arm(EXP, rid, w) == "model"]
    assert coin, "some of these weeks hash to the model's own assignment"
    for w in coin:
        [a] = sx.arms_for(rid, w, db_path=db)
        assert a["arm"] == "solver" and a["pinned"] and a["pin_source"] == "underpowered"
        assert sx.flag([a], "solver") is True
    hid = models.save_schedule_history(rid, coin[0], coin[0], 0, 0, 30, HEADER, [], db_path=db)
    sx.record(rid, hid, sx.arms_for(rid, coin[0], db_path=db), 70, db_path=db)
    conn = models.get_conn(db)
    row = conn.execute("SELECT arm, pinned, pin_source FROM schedule_experiment_weeks WHERE history_id=?",
                       (hid,)).fetchone()
    conn.close()
    assert (row["arm"], row["pinned"], row["pin_source"]) == ("solver", 1, "underpowered")
    exp = sx.readout(db_path=db)["experiments"][0]
    assert exp["verdict"]["state"] == "paused" and exp["power"]["restaurants"] == 1
    solver = next(a for a in exp["arms"] if a["arm"] == "solver")
    assert solver["generated"] == 0 and solver["pinned"] == 1             # never counted in the comparison


def test_with_enough_restaurants_generating_the_experiment_randomises(db):
    rids = [_restaurant(db) for _ in range(sx.MIN_RESTAURANTS_PER_ARM)]
    for r in rids:
        models.save_schedule_history(r, MON, SUN, 0, 0, 30, HEADER, [], db_path=db)
    assert sx.powered(db)["powered"]
    weeks = [(dt.date(2026, 10, 12) + dt.timedelta(weeks=k)).isoformat() for k in range(30)]
    arms = [sx.arms_for(rids[0], w, db_path=db)[0] for w in weeks]
    assert {a["arm"] for a in arms} == {"model", "solver"} and not any(a["pinned"] for a in arms)


def test_a_generation_for_a_lone_restaurant_runs_and_records_the_solver(db, monkeypatch):
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server")])
    w = next(x for x in [(dt.date(2026, 10, 5) + dt.timedelta(weeks=k)).isoformat() for k in range(20)]
             if sx.hashed_arm(EXP, rid, x) == "model")
    week = [(dt.date.fromisoformat(w) + dt.timedelta(days=i)).isoformat() for i in range(7)]
    lines = [f"{week[4]},Friday,Ana,Server,4:00pm,10:00pm,6,", f"{week[4]},Friday,Bo,Server,4:00pm,10:00pm,6,"]
    res = _run(monkeypatch, rid, lines, week_dates=week)["result"]
    assert res["optimizer"].get("solver"), "the solver ran on the model's week"
    conn = models.get_conn(db)
    row = conn.execute("SELECT arm, pinned, pin_source FROM schedule_experiment_weeks WHERE history_id=?",
                       (res["history_id"],)).fetchone()
    conn.close()
    assert (row["arm"], row["pinned"], row["pin_source"]) == ("solver", 1, "underpowered")


# ══ the payload: the hours split and what E, F1 handed over ════════════════

def test_the_week_saves_and_returns_its_hours_split_by_pay(db, monkeypatch):
    rid = _restaurant(db, [("Erik", "Owner"), ("Ana", "Server")])
    _no_search(monkeypatch)
    res = _run(monkeypatch, rid, [_line(FRI, "Erik", "Owner"), _line(FRI, "Ana")],
               roster_roles={"Erik": "Owner", "Ana": "Server"}, requirements=[{"date": FRI}],
               leader_rules_status={"rules": 0}, date_demand={FRI: {"lift_pct": 10}})["result"]
    assert res["hours_hourly"] == 6.0 and res["hours_salaried"] == 6.0
    row = _history(db, res["history_id"])
    assert (row["hours_hourly"], row["hours_salaried"]) == (6.0, 6.0)
    assert res["requirements"] == [{"date": FRI}] and res["leader_rules_status"] == {"rules": 0}
    assert res["date_demand"] == {FRI: {"lift_pct": 10}}
    for k in ("budget_basis", "daily_target_basis", "daily_target_reasons", "requirements_by_date",
              "labor_standards", "repair", "stage_failures", "stage_seconds"):
        assert k in res, k
