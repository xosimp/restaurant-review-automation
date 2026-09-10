"""Audit #9 remediation: the numbers the Labor module shows an owner.

Every test here was written against a measured wrong answer, not a
suspicion. The module was costing overtime at straight time while flagging
the same employee as overtime, pricing a schedule-only CSV at zero and
calling it "on track", showing a day-of-week percentage at double the
headline rate, and narrating a bundled fictional week as the owner's own.
"""
import csv
import io

import pytest

import labor
from labor import analyse_shifts

HEADER = ("date,day,employee,role,shift_start,shift_end,"
          "scheduled_hours,actual_hours,sales,notes")


def _rows(lines):
    return list(csv.DictReader(io.StringIO("\n".join([HEADER] + lines))))


# ── Overtime is priced the way it is paid ──────────────────────────────────

def test_overtime_hours_cost_one_and_a_half_times():
    """50 hours at $20 is $1,100, not $1,000. The module flagged this exact
    employee as overtime in the same result and still costed it straight."""
    rows = [f"2026-09-0{d},Day,Solo,Cook,10:00,20:00,10,10,3000," for d in range(1, 6)]
    a = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0)
    assert a["total_labor_cost"] == pytest.approx(40 * 20 + 10 * 30)
    assert a["overtime_hours"] == 10.0
    assert a["overtime_premium"] == pytest.approx(10 * 20 * 0.5)


def test_the_labor_percentage_includes_the_overtime_premium():
    rows = [f"2026-09-0{d},Day,Solo,Cook,10:00,20:00,10,10,3000," for d in range(1, 6)]
    a = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0)
    assert a["overall_labor_pct"] == pytest.approx(round(1100 / 15000 * 100, 1))


def test_no_overtime_means_no_premium():
    rows = [f"2026-09-0{d},Day,Solo,Cook,10:00,18:00,8,8,3000," for d in range(1, 6)]
    a = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0)
    assert a["overtime_premium"] == 0.0
    assert a["total_labor_cost"] == pytest.approx(40 * 20)


def test_the_premium_lands_on_the_days_that_earned_it():
    """Per-day percentages have to stay coherent with the total, or the
    day-of-week table and the headline disagree again."""
    rows = [f"2026-09-0{d},Day,Solo,Cook,10:00,20:00,10,10,3000," for d in range(1, 6)]
    a = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0)
    assert sum(d["labor_cost"] for d in a["by_day"].values()) == pytest.approx(
        a["total_labor_cost"], abs=0.05)


def test_every_overtime_week_is_reported_not_just_the_first():
    """Breaking after one flag meant three 48-hour weeks read as one
    incident, so the owner saw a third of their exposure."""
    rows = []
    for wk, start in enumerate(("2026-09-07", "2026-09-14", "2026-09-21")):
        y, m, d0 = (int(x) for x in start.split("-"))
        for i in range(6):
            rows.append(f"2026-{m:02d}-{d0 + i:02d},Day,Ana,Cook,10:00,18:00,8,8,4000,")
    a = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0)
    ot = [o for o in a["overtime_risk"] if o["employee"] == "Ana" and o["status"] == "overtime"]
    assert len(ot) == 3


def test_overtime_uses_the_restaurants_own_payroll_week():
    """Federal overtime runs on the employer's designated workweek. Sat+Sun
    plus the following Mon-Thu is 48 hours in a Saturday-start week and two
    separate under-40 weeks in a Monday-start one."""
    days = ["2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"]
    rows = [f"{d},Day,Ana,Cook,10:00,18:00,8,8,4000," for d in days]
    monday = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0, week_start_day=0)
    saturday = analyse_shifts(_rows(rows), hourly_rate=20.0, labor_target=30.0, week_start_day=5)
    assert monday["overtime_hours"] == 0.0
    assert saturday["overtime_hours"] == pytest.approx(8.0)


# ── A schedule-only CSV is not zero labor ──────────────────────────────────

def test_scheduled_hours_are_used_when_no_clock_in_exists():
    """A CSV with no actual_hours column produced $0 cost, 0% labor and an
    'on track' badge. Clients really do upload a published schedule."""
    hdr = "date,day,employee,role,shift_start,shift_end,scheduled_hours,sales,notes"
    lines = [hdr] + [f"2026-09-0{d},Day,E{i},Server,11:00,19:00,8,6000,"
                     for d in range(1, 4) for i in range(5)]
    a = analyse_shifts(list(csv.DictReader(io.StringIO("\n".join(lines)))),
                       hourly_rate=20.0, labor_target=30.0)
    assert a["total_labor_cost"] == pytest.approx(120 * 20)
    assert a["overall_labor_pct"] > 0
    assert a["hours_are_estimated"] is True


def test_a_real_timesheet_is_not_marked_estimated():
    a = analyse_shifts(_rows(["2026-09-01,Tue,A,Server,11:00,19:00,8,7.5,6000,"]),
                       hourly_rate=20.0, labor_target=30.0)
    assert a["hours_are_estimated"] is False


def test_role_hours_are_not_zero_without_a_clock_in_column():
    hdr = "date,day,employee,role,shift_start,shift_end,scheduled_hours,sales,notes"
    lines = [hdr, "2026-09-01,Tue,A,Server,11:00,19:00,8,6000,"]
    a = analyse_shifts(list(csv.DictReader(io.StringIO("\n".join(lines)))),
                       hourly_rate=20.0, labor_target=30.0)
    assert a["role_summary"]["Server"]["hours"] == 8.0


# ── One sales figure per day, used everywhere ──────────────────────────────

def test_a_blank_sales_cell_does_not_zero_the_day():
    """by_day["sales"] was ASSIGNED per row, so the last row won — including
    a blank one. Measured: the day-of-week table read 11.0% while the
    headline read 5.5% on the same data."""
    a = analyse_shifts(_rows([
        "2026-09-04,Friday,A,Server,16:00,23:00,7,7,8000,",
        "2026-09-04,Friday,B,Server,16:00,23:00,7,7,8000,",
        "2026-09-04,Friday,C,Cook,15:00,23:00,8,8,,",
        "2026-09-11,Friday,A,Server,16:00,23:00,7,7,8000,",
        "2026-09-11,Friday,B,Server,16:00,23:00,7,7,8000,",
        "2026-09-11,Friday,C,Cook,15:00,23:00,8,8,8000,",
    ]), hourly_rate=20.0, labor_target=30.0)
    assert a["by_day"]["2026-09-04"]["sales"] == 8000.0
    assert a["dow_summary"]["Friday"] == a["overall_labor_pct"]


def test_the_day_of_week_table_agrees_with_the_headline():
    a = analyse_shifts(_rows([
        "2026-09-07,Monday,A,Server,11:00,19:00,8,8,5000,",
        "2026-09-14,Monday,A,Server,11:00,19:00,8,8,5000,",
    ]), hourly_rate=25.0, labor_target=30.0)
    assert a["dow_summary"]["Monday"] == a["overall_labor_pct"]


def test_disagreeing_sales_rows_are_named_not_silently_resolved():
    a = analyse_shifts(_rows([
        "2026-09-04,Friday,A,Server,16:00,23:00,7,7,8000,",
        "2026-09-04,Friday,B,Server,16:00,23:00,7,7,9500,",
    ]), hourly_rate=20.0, labor_target=30.0)
    assert a["days_with_conflicting_sales"] == ["2026-09-04"]
    assert a["by_day"]["2026-09-04"]["sales"] == 9500.0


def test_a_day_with_no_sales_is_excluded_from_its_weekday_average():
    a = analyse_shifts(_rows([
        "2026-09-04,Friday,A,Server,16:00,23:00,7,7,8000,",
        "2026-09-11,Friday,B,Server,16:00,23:00,7,7,,",
    ]), hourly_rate=20.0, labor_target=30.0)
    assert a["days_missing_sales"] == ["2026-09-11"]
    assert a["dow_summary"]["Friday"] == a["overall_labor_pct"]


# ── Duplicate rows ─────────────────────────────────────────────────────────

def test_an_identical_row_is_a_re_upload_not_a_second_person():
    line = "2026-09-01,Tue,A,Server,11:00,19:00,8,8,6000,"
    a = analyse_shifts(_rows([line, line]), hourly_rate=20.0, labor_target=30.0)
    assert a["duplicate_rows_ignored"] == 1
    assert a["total_labor_cost"] == pytest.approx(8 * 20)


def test_two_people_on_the_same_shift_are_not_duplicates():
    a = analyse_shifts(_rows([
        "2026-09-01,Tue,A,Server,11:00,19:00,8,8,6000,",
        "2026-09-01,Tue,B,Server,11:00,19:00,8,8,6000,",
    ]), hourly_rate=20.0, labor_target=30.0)
    assert a["duplicate_rows_ignored"] == 0
    assert a["total_labor_cost"] == pytest.approx(16 * 20)


# ── Short periods are not projected ────────────────────────────────────────

def test_one_day_is_not_extrapolated_to_a_month():
    """Measured before the fix: $800 of real overage on a single Saturday
    became "$24,267/month in savings"."""
    rows = [f"2026-09-05,Saturday,E{i},Server,16:00,24:00,8,8,4000," for i in range(10)]
    a = analyse_shifts(_rows(rows), hourly_rate=25.0, labor_target=30.0)
    assert a["potential_savings"] > 0
    assert a["period_too_short_to_project"] is True
    assert a["potential_savings_monthly"] == 0.0


def test_a_full_week_is_projected():
    rows = [f"2026-09-{d:02d},Day,E{i},Server,16:00,24:00,8,8,1200,"
            for d in range(1, 9) for i in range(3)]
    a = analyse_shifts(_rows(rows), hourly_rate=25.0, labor_target=30.0)
    assert a["period_too_short_to_project"] is False
    assert a["potential_savings_monthly"] > 0


def test_the_monthly_gap_normalizes_by_the_period_it_covers():
    """calculate_monthly_gap multiplied by a hardcoded 2 with the comment
    "data covers ~2 weeks". Measured error: -53% on a one-week upload,
    +87% on a four-week one."""
    def _gap(weeks):
        from datetime import date, timedelta
        start = date(2026, 8, 3)
        rows = []
        for k in range(weeks * 7):
            d = start + timedelta(days=k)
            for i in range(6):
                rows.append(f"{d},Day,E{i},Server,16:00,24:00,8,8,5000,")
        a = analyse_shifts(_rows(rows), hourly_rate=25.0, labor_target=30.0)
        return labor.calculate_monthly_gap(a)["monthly_sales"]
    assert _gap(1) == _gap(2) == _gap(4)


def test_a_two_day_upload_gets_no_monthly_gap_at_all():
    a = analyse_shifts(_rows([
        "2026-09-01,Tue,A,Server,11:00,19:00,8,8,300,",
        "2026-09-02,Wed,A,Server,11:00,19:00,8,8,300,",
    ]), hourly_rate=26.0, labor_target=30.0)
    g = labor.calculate_monthly_gap(a)
    assert g["projectable"] is False
    assert g["monthly_gap"] == 0
    assert g["reason"]


# ── Per-role wages actually apply ──────────────────────────────────────────

def test_role_rates_match_the_lowercase_template_the_product_documents():
    """The paste box in client_data.html shows `role` as lowercase
    "server". An exact match meant every configured per-role wage was
    silently ignored in favour of the flat default."""
    a = analyse_shifts(_rows(["2026-09-01,Tue,A,server,11:00,19:00,8,8,6000,"]),
                       hourly_rate=26.0, labor_target=30.0,
                       role_rates={"Server": 18.0, "_default": 26.0})
    assert a["total_labor_cost"] == pytest.approx(8 * 18.0)


def test_an_unknown_role_still_falls_back_to_the_default():
    a = analyse_shifts(_rows(["2026-09-01,Tue,A,Sommelier,11:00,19:00,8,8,6000,"]),
                       hourly_rate=26.0, labor_target=30.0,
                       role_rates={"Server": 18.0, "_default": 26.0})
    assert a["total_labor_cost"] == pytest.approx(8 * 26.0)


# ── The flags that were computed and thrown away ───────────────────────────

def test_every_partial_data_flag_is_present_on_the_result():
    a = analyse_shifts(_rows(["2026-09-01,Tue,A,Server,11:00,19:00,8,8,6000,"]),
                       hourly_rate=20.0, labor_target=30.0)
    for key in ("sales_data_missing", "days_missing_sales", "hours_are_estimated",
                "duplicate_rows_ignored", "days_with_conflicting_sales",
                "period_too_short_to_project", "overtime_hours", "overtime_premium"):
        assert key in a, key
