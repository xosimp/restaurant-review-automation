"""Labor analysis edge cases (labor.analyse_shifts and its helpers) that the
MOD audit found untested: the shapes real timesheets arrive in — one person
spelled five ways, a correction row with negative hours, NaN and Infinity,
"8h" in an hours cell, Excel dates and headers, a totals row, a `revenue`
column, currency-formatted sales — plus overnight shifts, a DST week, a
year of a large roster, and the caveat text the Labor tab shows for days
whose sales rows disagree.

Pure functions on in-memory CSV text: no database except where a
restaurant's own settings are read. Confirmed defects are strict xfails
naming the finding; they flip to failures when the defect is fixed."""
import json
import math
import time
from datetime import date, timedelta

import pytest

import labor
import models
from labor import analyse_shifts, load_shifts


H = "date,employee,role,actual_hours,sales\n"


def _run(csv_text, **kw):
    kw.setdefault("hourly_rate", 26.0)
    kw.setdefault("labor_target", 30.0)
    return analyse_shifts(load_shifts(csv_string=csv_text), **kw)


WEEK = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]


# ── A3 #14 / MOD-LAB-15: one person, several spellings ──────────────────────

def test_one_spelling_worked_forty_five_hours_is_flagged_for_overtime():
    a = _run(H + "\n".join(f"{d},Marcus T.,Server,9,3000" for d in WEEK))
    assert [f["employee"] for f in a["overtime_risk"] if f["status"] == "overtime"] == ["Marcus T."]


def test_the_same_employee_spelled_with_case_and_whitespace_variants_is_flagged_for_overtime_once():
    names = ["Marcus T.", "marcus t.", "Marcus T. ", "MARCUS T.", "Marcus  T."]
    a = _run(H + "\n".join(f"{d},{n},Server,9,3000" for d, n in zip(WEEK, names)))
    ot = [f for f in a["overtime_risk"] if f["status"] == "overtime"]
    assert len(ot) == 1 and ot[0]["hours"] == 45.0
    assert a["overtime_premium"] > 0


# ── A3 #15, #16 / MOD-LAB-13: negative, NaN and Infinity ────────────────────

def test_a_negative_hours_row_does_not_cancel_a_real_shift():
    a = _run(H + "2026-09-14,A,Server,8,1000\n2026-09-14,B,Server,-8,1000\n")
    assert a["total_labor_cost"] == pytest.approx(8 * 26.0)


@pytest.mark.parametrize("row", [
    pytest.param("2026-09-14,A,Server,nan,1000", id="nan-hours"),
    pytest.param("2026-09-14,A,Server,inf,1000", id="inf-hours"),
    pytest.param("2026-09-14,A,Server,8,inf", id="inf-sales"),
    pytest.param("2026-09-14,A,Server,8,nan", id="nan-sales"),
])
def test_a_non_finite_cell_never_reaches_the_labor_result(row):
    a = _run(H + row + "\n")
    json.dumps(a, allow_nan=False)  # iOS JSONDecoder and web JSON.parse reject NaN/Infinity


def test_a_well_formed_file_yields_a_strict_json_result():
    a = _run(H + "2026-09-14,A,Server,8,1000\n")
    json.dumps(a, allow_nan=False)
    assert a["overall_labor_pct"] == pytest.approx(20.8)


# ── A3 #17 / MOD-LAB-14: compute_blended_rate on messy cells ────────────────

def test_the_blended_rate_tolerates_a_non_numeric_hours_cell():
    shifts = load_shifts(csv_string=H + "2026-09-14,A,Server,8h,1000\n2026-09-14,B,Server,8,1000\n"
                                        "2026-09-15,C,Server,,1000\n")
    rate = models.compute_blended_rate(shifts, {"_default": 15.0})
    assert rate == 15.0


def test_the_blended_rate_matches_roles_the_way_per_shift_rates_do():
    shifts = load_shifts(csv_string=H + "2026-09-14,A,server,8,1000\n")
    rates = {"Server": 20.0, "_default": 15.0}
    assert labor._shift_rate(shifts[0], rates, 15.0) == 20.0
    assert models.compute_blended_rate(shifts, rates) == 20.0


# ── A3 #23-#26 / MOD-LAB-10, -11, -12: spreadsheet shapes ───────────────────

def test_excel_month_day_year_dates_do_not_crash_the_analysis():
    a = _run(H + "9/14/2026,A,Server,8,100\n")
    assert a["total_labor_cost"] == pytest.approx(208.0)


def test_title_case_headers_are_analysed():
    a = _run("Date,Employee,Role,Actual_Hours,Sales\n2026-09-14,A,Server,8,1000\n")
    assert a["total_sales"] == 1000.0


def test_a_bom_prefixed_file_is_analysed():
    a = _run("﻿date,employee,role,actual_hours,sales\n2026-09-14,A,Server,8,1000\n")
    assert a["total_sales"] == 1000.0


def test_a_blank_date_totals_row_does_not_crash_the_analysis():
    a = _run(H + "2026-09-14,A,Server,8,1000\n,Total,,8,\n")
    assert a["total_sales"] == 1000.0


def test_a_revenue_column_is_read_as_sales():
    a = _run("date,employee,role,actual_hours,revenue\n2026-09-14,A,Server,8,4200\n")
    assert a["total_sales"] == 4200.0 and a["sales_data_missing"] is False


def test_currency_formatted_sales_are_read():
    a = _run('date,employee,role,actual_hours,sales\n2026-09-14,A,Server,8,"$4,200"\n')
    assert a["total_sales"] == 4200.0


# ── A3 #27, #28: overnight shift and the DST week ───────────────────────────

def test_an_overnight_shift_is_costed_by_its_hours_on_the_day_it_started():
    a = _run("date,employee,role,shift_start,shift_end,actual_hours,sales\n"
             "2026-09-14,A,Bartender,20:00,02:00,6,1000\n")
    assert a["total_labor_cost"] == pytest.approx(6 * 26.0)
    assert list(a["by_day"]) == ["2026-09-14"]
    assert a["by_day"]["2026-09-14"]["actual"] == 6.0


def test_a_week_containing_the_dst_change_counts_overtime_on_the_hours_worked():
    """US clocks fall back on Sunday 11/1/26. Hours come from the timesheet,
    so the 25-hour calendar day changes nothing: 45 hours is 5 of overtime."""
    days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-11-01"]
    a = _run(H + "\n".join(f"{d},A,Server,9,3000" for d in days))
    ot = [f for f in a["overtime_risk"] if f["status"] == "overtime"]
    assert len(ot) == 1 and ot[0]["hours"] == 45.0 and ot[0]["week_start"] == "2026-10-26"
    assert a["overtime_hours"] == 5.0


# ── A3 #19 / MOD-LAB-21: a zero-hour shift ──────────────────────────────────

def test_a_zero_hour_row_with_a_schedule_is_read_as_a_real_zero_today():
    """Pins the current reading that MOD-LAB-21 builds on: an explicit 0 in
    actual_hours is a clock reading, not a missing one, so the shift costs
    nothing. (The POS-side fix — an open Toast entry should not become that
    0 — is tested in test_edge_mod_a_labor_pos.py.)"""
    a = _run("date,employee,role,scheduled_hours,actual_hours,sales\n2026-09-14,A,Server,8,0,1000\n")
    assert a["total_labor_cost"] == 0.0
    assert labor._has_actual_hours({"actual_hours": "0"}) is True


# ── A3 #20 / MOD-LAB-20: the conflicting-sales caveat ───────────────────────

def test_days_whose_sales_rows_disagree_are_left_out_of_the_percentage():
    a = _run(H + "2026-09-14,A,Server,8,1000\n2026-09-14,B,Server,8,3000\n2026-09-15,A,Server,8,1000\n")
    assert a["days_with_conflicting_sales"] == ["2026-09-14"]
    assert a["total_sales"] == 1000.0


@pytest.mark.xfail(strict=True, reason="MOD-LAB-20: the Labor tab says 'the larger was used' for conflicting days; the code drops them")
def test_the_conflicting_sales_caveat_describes_what_the_code_does():
    html = open("templates/dashboard.html", encoding="utf-8").read()
    assert "the larger was used" not in html


# ── A3 #22: a year of a 500-person roster, scaled ───────────────────────────

def test_a_year_of_a_large_roster_is_analysed_within_a_bound():
    """365 days x 150 staff (54,750 rows — twice the client upload cap);
    the audit measured 100,000 rows at ~1.4 s. A generous ceiling so a
    quadratic regression fails here rather than on the request thread."""
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    d0 = date(2025, 9, 22)
    for i in range(365):
        d = d0 + timedelta(days=i)
        for e in range(150):
            rows.append(f"{d.isoformat()},{d.strftime('%A')},Emp {e},Server,11:00,17:00,6,6,9000,")
    t = time.time()
    a = analyse_shifts(load_shifts(csv_string="\n".join(rows)), hourly_rate=20.0, labor_target=30.0)
    elapsed = time.time() - t
    assert a["date_range"]["days"] == 365 and len(a["employee_hours"]) == 150
    assert elapsed < 15.0, elapsed


# ── A3 #47: a weekday the restaurant is closed ──────────────────────────────

@pytest.fixture
def _db(monkeypatch, db_path):
    real = models.get_conn
    import demand
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(demand, "get_conn", lambda *a, **k: real(db_path))
    return db_path


def test_a_weekday_the_restaurant_is_closed_gets_no_forecast_and_is_not_a_slow_day(_db):
    import demand
    rid = models.create_restaurant(models.Restaurant(name="Closed Mondays", owner_email="c@x.com"), db_path=_db)
    today = date(2026, 9, 22)  # a Tuesday
    history = {}
    for back in range(1, 57):
        d = today - timedelta(days=back)
        if d.strftime("%A") == "Monday":
            continue  # closed: no row at all
        history[d.isoformat()] = {"sales": 5000.0, "actual": 40.0, "labor_cost": 1000.0, "labor_pct": 20.0}
    models.save_labor_daily_history(rid, history, db_path=_db)
    monday = today - timedelta(days=1)
    fc = demand.forecast_day(rid, day=monday, db_path=_db)
    assert fc["available"] is False and "Monday" in fc["reason"]
    tue = demand.forecast_day(rid, day=today, db_path=_db)
    assert tue["available"] is True and tue["typical_sales"] == 5000.0
    slow = demand.slow_days(rid, db_path=_db)
    if slow.get("available"):
        assert "Monday" not in [d.get("day") or d.get("weekday") for d in slow["slow_days"]]
