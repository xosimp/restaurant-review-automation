"""Schedule audit 10/3/26, workstream E — the hours budget.

D-1  The hourly hours budget is the all-in labor target LESS the salaried
     staff's share of the week (the target judges labor with salaries since
     9/30/26): sized against the whole 35%, Simple EJ's ran 41-45% all-in
     while "under budget". The PAR block says which budget it is.
D-2  The budget's divisor is what an hour here is paid — the POS's pay on
     the punches, the owner's person and role rates, what the role's people
     make — never the role-rates-and-flat blend that put cooks at $26.
E-24 Hours priced at the assumed wage are counted; past a share the budget
     says "assumes $26/hr — set pay rates", past a larger one it is no
     ceiling to cut shifts to (trim_ok False).
"""
import schedule_prompt
import json
import types
from datetime import date, timedelta

import pytest

import labor
import models
import schedule_engine

DATES = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
SAL = json.dumps([{"name": "Erik Baylis", "annual": 150000}, {"name": "Jim", "annual": 150000}])


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _by_day(hours=10.0):
    return {(date(2026, 9, 14) + timedelta(days=i)).isoformat(): {"actual": hours} for i in range(14)}


# ── D-1: the all-in target less the salaried share ──────────────────────

def test_the_hourly_budget_is_the_target_less_the_salaried_share():
    sal = {"cost": 5769.23, "people": 2, "trading_days": 7}
    plan = labor.week_hours_plan({"by_day": _by_day()}, DATES, labor_target=35.0, hourly_rate=15.0,
                                 projected_revenue_override=100000, salaried=sal)
    assert plan["projected_revenue"] == 100000
    # $35,000 at target, $5,769 of it the salaries: $29,231 for the hourly crew.
    assert plan["labor_budget_dollars"] == 29231
    assert plan["hours_budget"] == round(29230.77 / 15.0, 1)
    b = plan["budget_basis"]
    assert b["kind"] == "all_in_less_salaries" and b["salaried_people"] == 2 and b["trading_days"] == 7
    assert b["salaried_week_cost"] == 5769 and b["target_dollars"] == 35000
    assert "counts salaries" in b["text"] and "2 salaried people" in b["text"]
    # The days still share exactly the (smaller) budget.
    assert abs(sum(plan["daily_target_hours"].values()) - plan["hours_budget"]) < 1.0


def test_a_reader_who_may_not_see_salaries_gets_the_budget_without_their_dollars():
    sal = {"cost": 5769.23, "people": 2, "trading_days": 7}
    plan = labor.week_hours_plan({"by_day": _by_day()}, DATES, labor_target=35.0, hourly_rate=15.0,
                                 projected_revenue_override=100000, salaried=sal, show_salary=False)
    b = plan["budget_basis"]
    assert "salaried_week_cost" not in b and "target_dollars" not in b
    assert "5,769" not in json.dumps(b) and "5769" not in json.dumps(b)
    assert plan["hours_budget"] == round(29230.77 / 15.0, 1)


def test_nobody_salaried_keeps_the_whole_target_and_says_so():
    plan = labor.week_hours_plan({"by_day": _by_day()}, DATES, labor_target=30.0, hourly_rate=20.0,
                                 projected_revenue_override=50000)
    assert plan["labor_budget_dollars"] == 15000 and plan["hours_budget"] == 750.0
    assert plan["budget_basis"]["kind"] == "all_in" and "whole 30% labor target" in plan["budget_basis"]["text"]


def test_salaries_that_reach_the_target_leave_no_hourly_hours_and_say_why():
    sal = {"cost": 9000.0, "people": 2, "trading_days": 7}
    plan = labor.week_hours_plan({"by_day": _by_day()}, DATES, labor_target=35.0, hourly_rate=15.0,
                                 projected_revenue_override=20000, salaried=sal)
    assert plan["hours_budget"] == 0 and plan["labor_budget_dollars"] == 0
    assert plan["budget_basis"]["salaries_exceed_target"] is True
    assert "alone reaches it" in plan["budget_basis"]["text"]
    assert plan["budget_basis"]["trim_ok"] is False


def test_the_weeks_salary_share_counts_its_trading_days():
    five = models.Restaurant(name="EJ", owner_email="e@x.test", salaried_staff_json=SAL,
                             open_times_json=json.dumps({d: "11:00am" for d in
                                                         ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                                          "Saturday")}))
    share = models.salaried_week_share(five, DATES)
    # Six trading weekdays (no Sunday): six days' share is exactly a week's salary.
    assert share["trading_days"] == 6 and abs(share["cost"] - 300000 / 52) < 0.06
    assert share["people"] == 2
    closed = models.salaried_week_share(five, DATES, closed_dates={"2026-10-09"})
    assert closed["trading_days"] == 5 and abs(closed["cost"] - 300000 / 52 / 6 * 5) < 0.06
    assert models.salaried_week_share(models.Restaurant(name="N", owner_email="n@x.test"), DATES) is None


def test_the_prompt_says_which_budget_it_is_and_never_the_salaries(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    analysis = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "total_sales": 0, "period_days": 0, "by_day": _by_day()}
    out = labor.generate_optimized_schedule(analysis, [], roster=[("Ana", "Server")], week_start="2026-10-05",
                                            projected_revenue_override=100000, hourly_rate=15.0, labor_target=35.0,
                                            salaried_week={"cost": 5769.23, "people": 2, "trading_days": 7})
    prompt = schedule_prompt.prompt_text(captured["messages"][0]["content"])
    par = prompt[prompt.index("PAR HOURS CEILING"):]
    assert "35.0% counting salaries" in par and "after the salaried staff's pay for the week, $29,231" in par
    assert "1948.7h is the MAXIMUM for the week" in par
    assert "5,769" not in prompt and "5769" not in prompt
    assert "hourly staff only" in prompt
    assert out["budget_basis"]["kind"] == "all_in_less_salaries" and "salaried_week_cost" not in out["budget_basis"]


# ── D-2: the measured wage ──────────────────────────────────────────────

def _row(d, emp, role, hours, pay=""):
    return {"date": d, "employee": emp, "role": role, "actual_hours": str(hours), "scheduled_hours": str(hours),
            "pay_rate": str(pay)}


def test_the_blended_rate_is_what_each_hour_is_paid_here():
    shifts = [_row("2026-09-28", "Cal", "Line Cook", 8, 22), _row("2026-09-28", "Sue", "Server PM", 6, 9.48),
              _row("2026-09-29", "Cal", "Line Cook", 8, ""),          # his $0 punch costs his other punches' pay
              _row("2026-09-29", "Dot", "Dishwasher", 6, "")]          # nobody's pay anywhere: the fallback
    rb = labor.labor_rate_basis(shifts, {"Server PM": 9.0}, fallback=26.0, fallback_assumed=True)
    # 8h@22 + 6h@9.48 + 8h@22 (rate book) + 6h@26 (assumed) over 28h
    assert rb["rate"] == round((16 * 22 + 6 * 9.48 + 6 * 26) / 28, 2)
    assert rb["by_source"] == {"punch": 14.0, "person": 8.0, "fallback": 6.0}
    assert rb["assumed_hours"] == 6.0 and rb["assumed_share"] == round(6 / 28, 3)
    assert rb["by_role"]["Line Cook"]["rate"] == 22.0 and rb["by_role"]["Line Cook"]["source"] == "punch"
    assert rb["by_role"]["Dishwasher"]["assumed"] and rb["assumed_roles"] == ["Dishwasher"]
    assert rb["basis"] == "partly_assumed"
    # The old divisor read the role rates and the flat rate only.
    assert models.compute_blended_rate(shifts, {"Server PM": 9.0, "_default": 26.0}) != rb["rate"]
    # An owner's own flat rate is not an assumption.
    assert labor.labor_rate_basis(shifts, {"Server PM": 9.0}, fallback=18.0, fallback_assumed=False)["assumed_share"] == 0


def test_the_restaurants_analysis_divides_by_the_measured_wage(db_path):
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test", labor_target_pct=35.0,
                                                     hourly_rate=26.0), db_path=db_path)
    # The owner's role rates cover the tipped role only, as at Simple EJ's.
    models.update_restaurant(rid, {"role_rates_json": json.dumps({"Server PM": 9.48})}, db_path=db_path)
    today = date.today()
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate"]
    for n in range(1, 8):
        d = (today - timedelta(days=n)).isoformat()
        rows.append(f"{d},X,Cal Cook,Line Cook,08:00,16:00,8,8,4000,,22")
        rows.append(f"{d},X,Sue Server,Server PM,17:00,23:00,6,6,4000,,")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="rpower", db_path=db_path)
    a = labor.analyse_shifts_for_restaurant(rid, with_salaries=False)
    want = round((56 * 22 + 42 * 9.48) / 98, 2)
    assert a["blended_rate"] == want
    assert a["rate_basis"]["assumed_share"] == 0 and a["rate_basis"]["basis"] == "measured"
    # The cost the Labor tab shows is the same wages: cost ÷ hours is the rate.
    assert abs(a["hourly_costed_labor"] / 98 - want) < 0.01


def test_every_roles_measured_rate_reaches_the_prompt(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    shifts = [_row("2026-09-28", "Cal", "Line Cook", 8, 22), _row("2026-09-28", "Sue", "Server PM", 6, 9.48),
              _row("2026-09-29", "Dot", "Dishwasher", 6, "")]
    rb = labor.labor_rate_basis(shifts, {"Server PM": 9.48}, fallback=26.0)
    analysis = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "total_sales": 0, "period_days": 0, "by_day": _by_day(), "rate_basis": rb}
    labor.generate_optimized_schedule(analysis, [], roster=[("Cal", "Line Cook")], week_start="2026-10-05",
                                      projected_revenue_override=50000, hourly_rate=rb["rate"], labor_target=30.0,
                                      role_rates={"Server PM": 9.48})
    block = schedule_prompt.prompt_text(captured["messages"][0]["content"]).split("Per-role hourly rates", 1)[1].split("\n\n", 1)[0]
    assert "Line Cook: $22.00/hr (POS pay)" in block
    assert "Server PM: $9.48/hr (POS pay)" in block
    assert "Dishwasher: $26.00/hr (assumed — no pay rate on file)" in block


# ── E-24: the assumed wage is said, and past a share it cuts nothing ──────

def test_a_budget_on_an_assumed_wage_says_so():
    some = {"rate": 15.0, "assumed_share": 0.10, "assumed_rate": 26.0, "assumed_roles": ["Dishwasher"]}
    note = labor.rate_caveat(some)
    assert note["caveat"].startswith("Budget assumes $26/hr for 10% of the hours (Dishwasher) — set pay rates")
    assert note["trim_ok"] is True
    most = {"rate": 26.0, "assumed_share": 1.0, "assumed_rate": 26.0, "assumed_roles": ["Server", "Cook"]}
    note = labor.rate_caveat(most)
    assert note["caveat"].startswith("Budget assumes $26/hr — set pay rates")
    assert note["trim_ok"] is False and "not cut to it" in note["caveat"]
    assert labor.rate_caveat({"rate": 15.0, "assumed_share": 0.01})["caveat"] is None
    plan = labor.week_hours_plan({"by_day": _by_day()}, DATES, labor_target=30.0, hourly_rate=26.0,
                                 projected_revenue_override=50000, rate_basis=most)
    assert plan["budget_basis"]["trim_ok"] is False
    assert plan["budget_basis"]["caveat"].startswith("Budget assumes $26/hr — set pay rates")


def test_the_par_block_carries_the_assumed_wage_caveat(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    rb = {"rate": 26.0, "assumed_share": 1.0, "assumed_rate": 26.0, "assumed_roles": ["Server"],
          "by_role": {"Server": {"rate": 26.0, "hours": 40, "source": "fallback", "assumed": True}}}
    analysis = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "total_sales": 0, "period_days": 0, "by_day": _by_day(), "rate_basis": rb}
    out = labor.generate_optimized_schedule(analysis, [], roster=[("Ana", "Server")], week_start="2026-10-05",
                                            projected_revenue_override=50000, hourly_rate=26.0, labor_target=30.0)
    par = schedule_prompt.prompt_text(captured["messages"][0]["content"]).split("PAR HOURS CEILING", 1)[1]
    assert "Budget assumes $26/hr — set pay rates" in par
    assert out["budget_basis"]["trim_ok"] is False


def test_the_forecast_tab_and_the_draft_get_the_same_net_budget(db_path, monkeypatch):
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test", labor_target_pct=35.0),
                                   db_path=db_path)
    models.update_restaurant(rid, {"salaried_staff_json": SAL}, db_path=db_path)
    analysis = {"is_live": True, "blended_rate": 15.0, "rate_basis": {"rate": 15.0, "assumed_share": 0},
                "by_day": _by_day(), "period_days": 14, "total_sales": 0}
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: analysis)
    import schedule_economics as econ
    monkeypatch.setattr(econ, "projected_weekly_revenue",
                        lambda r, **k: {"value": 100000, "source": "your weekly pattern"})
    out = schedule_engine.forecast_preview(rid, "2026-10-05")
    assert out["ok"] and out["budget_basis"]["kind"] == "all_in_less_salaries"
    # 7 trading days (no hours on file): a week's salary comes off the $35,000.
    assert out["labor_budget_dollars"] == round(35000 - 300000 / 52)
    assert out["hours_budget"] == round((35000 - 300000 / 52) / 15.0, 1)
