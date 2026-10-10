"""Scheduler audit 10/10/26, workstream E — AI-11, AI-26, contract #7: a
draft is never left empty because the model is down, over budget, capped or
refuses; the draft's source is a switch (SCHEDULE_DRAFT_SOURCE, the draft_v1
arm); the days built without the model are said."""
import pytest

import ai_utils
import labor
import models
import schedule_engine as se
import schedule_experiments as sx
import schedule_fallback as fb
from models import Restaurant, create_restaurant
from schedule_rules import Constraints

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
LAST = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _days(ds):
    return [se._date_of(d).strftime("%A") for d in ds]


def _published(db_path, rid, rows):
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    csv = HEAD + "\n".join(",".join(r) for r in rows)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                 "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?)",
                 (rid, LAST[0], LAST[-1], 20, 40, 30, csv, "2026-10-04 10:00:00"))
    conn.commit()
    conn.close()


def _c(**kw):
    c = Constraints(restaurant_id=1, week_dates=WEEK, week_days=_days(WEEK))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── the switch ─────────────────────────────────────────────────────────────

def test_the_draft_source_is_the_env_then_the_arm_then_the_model(monkeypatch):
    monkeypatch.delenv("SCHEDULE_DRAFT_SOURCE", raising=False)
    assert fb.draft_source(5, []) == "model"
    arm = [{"experiment": "draft_v1", "arm": "carry_forward", "flags": {"draft_source": "carry_forward"}}]
    assert fb.draft_source(5, arm) == "carry_forward"
    monkeypatch.setenv("SCHEDULE_DRAFT_SOURCE", "solver")
    assert fb.draft_source(5, arm) == "solver"
    monkeypatch.setenv("SCHEDULE_DRAFT_SOURCE", "nonsense")
    assert fb.draft_source(5, []) == "model"


def test_draft_v1_is_an_experiment_that_keeps_the_model_until_it_can_conclude(db_path, monkeypatch):
    exp = sx.experiment("draft_v1")
    assert exp and exp["active"] and exp["until_powered"] == "model_draft" and exp["control"] == "model_draft"
    # F (AI-8) added the deterministic+critic arm, off the hash like code_shaped
    assert {a["key"] for a in exp["arms"]} == {"model_draft", "carried", "code_shaped", "code_critic"}
    # the code shape stays off the hash until F's solver-first draft and the eval say so
    assert next(a for a in exp["arms"] if a["key"] == "code_shaped")["weight"] == 0
    monkeypatch.setattr(sx, "_power_or_pin", lambda db: {"powered": False})
    rid = create_restaurant(Restaurant(name="Pin", owner_email="pin@x.com"), db_path=db_path)
    arms = sx.arms_for(rid, WEEK[0], db_path=db_path)
    got = next(a for a in arms if a["experiment"] == "draft_v1")
    assert got["arm"] == "model_draft" and got["pin_source"] == "underpowered"
    assert sx.value(arms, "draft_source") == "model"
    # a restaurant's own pin is its override
    sx.set_pin(rid, "draft_v1", "carried", db_path=db_path)
    assert fb.draft_source(rid, sx.arms_for(rid, WEEK[0], db_path=db_path)) == "carry_forward"


# ── the carry-forward ─────────────────────────────────────────────────────

def test_the_base_week_is_the_last_published_one(db_path):
    rid = create_restaurant(Restaurant(name="Cf", owner_email="cf@x.com"), db_path=db_path)
    _published(db_path, rid, [(LAST[4], "Friday", "Ana", "Server", "4:00pm", "10:00pm", "6", "closer")])
    base = fb.base_week(rid, WEEK, db_path=db_path)
    assert base["source"] == "published" and base["week_start"] == LAST[0] and base["rows"][0]["employee"] == "Ana"


def test_without_a_published_week_the_last_week_worked_is_carried(db_path):
    rid = create_restaurant(Restaurant(name="Cf2", owner_email="cf2@x.com"), db_path=db_path)
    shifts = [{"date": LAST[5], "employee": "Bo", "role": "Line Cook", "shift_start": "10:00am", "shift_end": "4:00pm"}]
    base = fb.base_week(rid, WEEK, shifts=shifts, db_path=db_path)
    assert base["source"] == "punches" and base["rows"][0]["employee"] == "Bo"


def test_carry_forward_maps_weekdays_and_drops_who_left_or_is_off():
    base = {"source": "published", "week_start": LAST[0], "rows": [
        {"date": LAST[4], "employee": "Ana", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm",
         "scheduled_hours": "6", "notes": "closer — NEEDS REVIEW: x"},
        {"date": LAST[4], "employee": "Gone", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm",
         "notes": ""},
        {"date": LAST[5], "employee": "Cy", "role": "Line Cook", "shift_start": "10:00am", "shift_end": "4:00pm",
         "notes": "Cavnar AI: something internal"}]}
    c = _c(roster_names=["Ana", "Cy"], active={"ana", "cy"}, blocked_dates={"cy": {WEEK[5]: "approved time off"}})
    got = fb.carry_forward_rows(base, [WEEK[4], WEEK[5]], c, roster_names=["Ana", "Cy"])
    assert [(r["employee"], r["date"]) for r in got["rows"]] == [("Ana", WEEK[4])]
    assert got["rows"][0]["notes"] == "closer"                              # the fixed note list, no engine mark
    whys = {d["employee"]: d["why"] for d in got["dropped"]}
    assert whys["Gone"] == "not on the roster now" and "time off" in whys["Cy"]


# ── the fallback inside the generation ────────────────────────────────────

def _fallback(source="model", roster=("Ana",)):
    return {"source": source, "restaurant_id": None, "constraints": None, "roster_names": list(roster),
            "roster_roles": {n: "Server" for n in roster}, "week_dates": WEEK, "plan_rows": [], "plan_dates": [],
            "open_times": {}, "close_times": {}}


def _base(monkeypatch):
    rows = [{"date": d, "employee": "Ana", "role": "Server", "shift_start": "11:00am", "shift_end": "3:00pm",
             "scheduled_hours": "4", "notes": ""} for d in LAST]
    monkeypatch.setattr(fb, "base_week", lambda rid, wd, shifts=None, db_path=None:
                        {"rows": rows, "source": "published", "week_start": LAST[0], "week_end": LAST[-1]})


@pytest.mark.parametrize("exc", [ai_utils.AIProviderDown("down"), ai_utils.AIBudgetExceeded("paused")])
def test_a_model_that_is_down_or_over_budget_still_leaves_a_week(monkeypatch, exc):
    _base(monkeypatch)

    def gen(analysis, shifts, prepare_only=False, **kw):
        if prepare_only:
            return {"week_dates": WEEK, "week_days": _days(WEEK), "hours_budget": 100, "rows": [],
                    "schedule_csv": HEAD, "narrative": [], "requirements_by_date": {}, "generation_seconds": 0}
        raise exc
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None},
                                fallback=_fallback())
    assert out["generated_dates"] == WEEK and not out["unwritten_dates"]
    assert out["draft_source"]["code_source"] == "carry_forward" and out["draft_source"]["code_dates"] == WEEK
    line = out["generation_notes"][0]
    assert line.startswith("Built without the AI model:") and "published week of 10/5/26" in line
    assert not line.startswith("⚠")


def test_a_refusal_is_drafted_in_code_never_a_try_again(monkeypatch):
    _base(monkeypatch)
    err = se.ScheduleGenerationError("declined")
    err.stop_reason = "refusal"

    def gen(analysis, shifts, prepare_only=False, **kw):
        if prepare_only:
            return {"week_dates": WEEK, "week_days": _days(WEEK), "rows": [], "schedule_csv": HEAD, "narrative": []}
        raise err
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None},
                                fallback=_fallback())
    assert out["generated_dates"] == WEEK
    assert "declined" in out["generation_notes"][0]


def test_only_the_days_the_model_missed_are_drafted_in_code(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("SCHEDULE_PARALLEL_CALLS", "1")
    calls = []

    def gen(analysis, shifts, week_slice=None, prepare_only=False, **kw):
        calls.append(week_slice)
        dates = list(week_slice or WEEK)
        if len(calls) > 1:
            raise ai_utils.AIProviderDown("down")
        rows = [{"date": d, "day": se._date_of(d).strftime("%A"), "employee": "Ana", "role": "Server",
                 "shift_start": "5:00pm", "shift_end": "10:00pm", "scheduled_hours": "5", "notes": ""} for d in dates]
        return {"schedule_csv": HEAD, "rows": rows, "narrative": [], "complete_dates": dates, "generation_seconds": 1}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None},
                                fallback=_fallback())
    model_days, code_days = calls[0], out["draft_source"]["code_dates"]
    assert set(model_days) | set(code_days) == set(WEEK) and not set(model_days) & set(code_days)
    by = {r["date"]: r["shift_start"] for r in out["rows"]}
    assert all(by[d] == "5:00pm" for d in model_days) and all(by[d] == "11:00am" for d in code_days)


def test_a_chosen_carry_forward_makes_no_model_call(monkeypatch):
    _base(monkeypatch)
    sent = []

    def gen(analysis, shifts, prepare_only=False, **kw):
        if not prepare_only:
            sent.append(1)
        return {"week_dates": WEEK, "week_days": _days(WEEK), "rows": [], "schedule_csv": HEAD, "narrative": [],
                "hours_budget": 80}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None},
                                fallback=_fallback("carry_forward"))
    assert not sent and out["chunked"] == 0
    assert out["draft_source"]["chosen"] == "carry_forward" and out["draft_source"]["source"] == "carry_forward"
    assert out["hours_budget"] == 80                     # the week's facts, from labor with no call
    assert "this week's drafts are built in code" in out["generation_notes"][0]


def test_data_that_is_not_current_is_never_papered_over(monkeypatch):
    _base(monkeypatch)

    def gen(analysis, shifts, prepare_only=False, **kw):
        if prepare_only:
            return {"week_dates": WEEK, "rows": [], "schedule_csv": HEAD, "narrative": []}
        raise ai_utils.DataNotReady({"reason": "sales stopped", "decision": "refuse"})
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    with pytest.raises(ai_utils.DataNotReady):
        se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None},
                              fallback=_fallback())


@pytest.fixture
def db(db_path, monkeypatch):
    import sys
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def test_a_week_drafted_in_code_is_saved_swept_and_said(db, monkeypatch):
    """Through the job: the code-drafted week is parsed, repaired, swept,
    saved and handed back with where it came from — the owner's line at the
    top of the review, never a "⚠" that holds the publish."""
    rid = create_restaurant(Restaurant(name="Fallback Grill", owner_email="fb@x.com"), db_path=db)
    rows = [{"date": d, "day": se._date_of(d).strftime("%A"), "employee": "Ana", "role": "Server",
             "shift_start": "11:00am", "shift_end": "3:00pm", "scheduled_hours": "4", "notes": ""} for d in WEEK]
    line = "Built without the AI model: Monday 10/12/26 and 6 more — test."
    base = {"ok": True, "schedule_csv": HEAD + "\n".join(",".join(r[c] for c in se._ANSWER_COLS) for r in rows),
            "rows": rows, "week_dates": WEEK, "week_days": _days(WEEK), "summary": [], "hours_budget": 0,
            "daily_target_hours": {}, "labor_target": 30, "unwritten_dates": [], "closed_dates": [],
            "generation_notes": [line],
            "draft_source": {"chosen": "model", "source": "model", "code_source": "carry_forward",
                             "code_dates": WEEK, "why": {d: "provider_down" for d in WEEK}}}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("code-job", rid)
    res = finished["result"]
    assert finished["status"] == "done" and res["history_id"]
    assert res["draft_source"]["code_dates"] == WEEK and res["draft_source"]["code_source"] == "carry_forward"
    assert res["review"]["lines"][0] == line and res["review"]["draft_source"]["code_dates"] == WEEK
    assert len(res["preview_rows"]) >= 7


def test_labor_prepares_the_week_without_a_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no call may be made")
    monkeypatch.setattr(labor, "create_with_retry", boom)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    out = labor.generate_optimized_schedule({"overall_labor_pct": 25, "overstaffed_days": [], "understaffed_days": [],
                                             "dow_summary": {}, "period_days": 0, "total_sales": 0}, [],
                                            roster=[("Ana", "Server")], week_start=WEEK[0], hourly_rate=20,
                                            labor_target=30, prepare_only=True)
    assert out["prepared"] and out["rows"] == [] and out["week_dates"] == WEEK
    assert out["request"]["messages"] and out["request"]["output_config"]["format"]
