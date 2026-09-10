"""Audit #9 remediation: the schedule generator's own inputs.

TYPICAL HEADCOUNT is the prompt block the scheduler calls "your starting
point for who/how many per role per day". It divided a DEDUPLICATED set of
employees by the number of weeks of history, so a restaurant running the
same six servers every Friday reported two, while a restaurant with a
rotating roster of eighteen reported six. The metric rewarded churn and
punished a stable roster, and the resulting gap to the hours budget was
then answered by adding staff.

Alongside it: the daypart split never returned "morning" for any CSV a
client actually uploads, and the no-show rate read 100% on every day when
the actual_hours column was simply absent.
"""
import types

import pytest

import labor
from labor import generate_optimized_schedule


def _capture(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(
                text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                     "---SUMMARY---\n- ok")],
            stop_reason="end_turn",
        )
    monkeypatch.setattr(labor, "create_with_retry", fake)
    return captured


def _analysis():
    return {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [],
            "dow_summary": {}, "total_sales": 70000, "period_days": 21, "by_day": {}}


def _prompt(monkeypatch, shifts, **kw):
    captured = _capture(monkeypatch)
    generate_optimized_schedule(_analysis(), shifts, restaurant_name="Test",
                                hourly_rate=20.0, labor_target=30.0, **kw)
    return captured["messages"][0]["content"]


# ── Headcount reflects how many people are actually on ─────────────────────

def _fridays(names_per_week):
    out = []
    for date, names in zip(("2026-08-07", "2026-08-14", "2026-08-21"), names_per_week):
        for n in names:
            out.append({"date": date, "day": "Friday", "employee": n, "role": "Server",
                        "shift_start": "16:00", "shift_end": "23:00",
                        "scheduled_hours": 7, "actual_hours": 7, "sales": 9000})
    return out


def test_a_stable_roster_is_not_divided_by_its_own_consistency(monkeypatch):
    """Six servers every Friday for three weeks is six, not two."""
    crew = [f"Server{i}" for i in range(1, 7)]
    prompt = _prompt(monkeypatch, _fridays([crew, crew, crew]))
    assert "Friday: Server: 6 night" in prompt


def test_a_rotating_roster_gives_the_same_answer(monkeypatch):
    """Eighteen different people, six on each Friday, is still six."""
    weeks = [[f"P{i}" for i in range(w * 6, w * 6 + 6)] for w in range(3)]
    prompt = _prompt(monkeypatch, _fridays(weeks))
    assert "Friday: Server: 6 night" in prompt


def test_a_role_that_only_works_some_weeks_is_averaged_down(monkeypatch):
    """One cook on one of three Fridays should not read as a permanent
    Friday cook."""
    shifts = _fridays([[f"S{i}" for i in range(6)]] * 3)
    shifts.append({"date": "2026-08-07", "day": "Friday", "employee": "Cook1", "role": "Cook",
                   "shift_start": "15:00", "shift_end": "23:00",
                   "scheduled_hours": 8, "actual_hours": 8, "sales": 9000})
    prompt = _prompt(monkeypatch, shifts)
    assert "Server: 6 night" in prompt
    assert "Cook: 1 night" not in prompt


# ── The morning crew is visible ────────────────────────────────────────────

def test_twenty_four_hour_times_split_into_morning_and_night(monkeypatch):
    """Every client CSV uses 24-hour times — the bundled sample and the
    paste-box template both do. The parser accepted 12-hour only, so every
    shift fell through to a bare `except` and was classified night."""
    shifts = []
    for d in ("2026-08-07", "2026-08-14"):
        for i in range(3):
            shifts.append({"date": d, "day": "Friday", "employee": f"AM{i}", "role": "Server",
                           "shift_start": "10:00", "shift_end": "16:00",
                           "scheduled_hours": 6, "actual_hours": 6, "sales": 9000})
        for i in range(5):
            shifts.append({"date": d, "day": "Friday", "employee": f"PM{i}", "role": "Server",
                           "shift_start": "16:00", "shift_end": "23:00",
                           "scheduled_hours": 7, "actual_hours": 7, "sales": 9000})
    prompt = _prompt(monkeypatch, shifts)
    assert "Server: 3 morning / 5 night" in prompt


def test_twelve_hour_times_still_split_correctly(monkeypatch):
    """The module's own generated schedules are 12-hour, and get re-ingested."""
    shifts = []
    for d in ("2026-08-07", "2026-08-14"):
        shifts.append({"date": d, "day": "Friday", "employee": "AM1", "role": "Server",
                       "shift_start": "10:00am", "shift_end": "4:00pm",
                       "scheduled_hours": 6, "actual_hours": 6, "sales": 9000})
        shifts.append({"date": d, "day": "Friday", "employee": "PM1", "role": "Server",
                       "shift_start": "4:00pm", "shift_end": "11:00pm",
                       "scheduled_hours": 7, "actual_hours": 7, "sales": 9000})
    prompt = _prompt(monkeypatch, shifts)
    assert "Server: 1 morning / 1 night" in prompt


def test_an_unreadable_start_time_says_so_rather_than_guessing_night(monkeypatch):
    shifts = []
    for d in ("2026-08-07", "2026-08-14"):
        shifts.append({"date": d, "day": "Friday", "employee": "X1", "role": "Server",
                       "shift_start": "dinner", "shift_end": "close",
                       "scheduled_hours": 7, "actual_hours": 7, "sales": 9000})
    prompt = _prompt(monkeypatch, shifts)
    assert "unspecified start time" in prompt


# ── No-show risk needs evidence ────────────────────────────────────────────

def test_a_missing_clock_in_column_is_not_a_hundred_percent_no_show_rate(monkeypatch):
    """Measured before the fix: every weekday reported a 100% historical
    no-show rate, which told the scheduler to add a standby seven days a
    week off the back of an absent column."""
    shifts = [{"date": f"2026-08-{d:02d}", "day": "Friday", "employee": f"E{i}",
               "role": "Server", "shift_start": "16:00", "shift_end": "23:00",
               "scheduled_hours": 7, "sales": 9000}
              for d in (7, 14, 21) for i in range(4)]
    prompt = _prompt(monkeypatch, shifts)
    assert "NO-SHOW RISK" not in prompt


def test_a_real_no_show_is_still_detected(monkeypatch):
    shifts = []
    for d in (7, 14, 21):
        for i in range(4):
            shifts.append({"date": f"2026-08-{d:02d}", "day": "Friday", "employee": f"E{i}",
                           "role": "Server", "shift_start": "16:00", "shift_end": "23:00",
                           "scheduled_hours": 7, "actual_hours": 0 if i == 0 else 7,
                           "sales": 9000})
    prompt = _prompt(monkeypatch, shifts)
    assert "NO-SHOW RISK" in prompt


# ── The hours figure is a ceiling ──────────────────────────────────────────

def test_a_sub_week_period_produces_no_revenue_projection(monkeypatch):
    """One Saturday times seven is not a week of revenue, and it fed
    straight into the hours budget."""
    captured = _capture(monkeypatch)
    a = _analysis()
    a.update(period_days=1, total_sales=9000)
    result = generate_optimized_schedule(
        a, [{"date": "2026-08-08", "day": "Saturday", "employee": "A", "role": "Server",
             "shift_start": "16:00", "shift_end": "23:00", "scheduled_hours": 7,
             "actual_hours": 7, "sales": 9000}],
        restaurant_name="Test", hourly_rate=20.0, labor_target=30.0)
    assert result["projected_revenue"] == 0.0
    assert captured  # the call still happened


def test_partial_yoy_coverage_is_not_scaled_onto_the_days_it_has(monkeypatch):
    """Two of seven days carrying prior-year data used to absorb the whole
    week's hours budget between them."""
    yoy = [{"next_week_dow": "Friday", "next_week_date": "2026-08-28",
            "yoy_sales": 9000, "yoy_labor_pct": 28, "yoy_hours": 60},
           {"next_week_dow": "Saturday", "next_week_date": "2026-08-29",
            "yoy_sales": 11000, "yoy_labor_pct": 28, "yoy_hours": 70}]
    captured = _capture(monkeypatch)
    result = generate_optimized_schedule(
        _analysis(), _fridays([[f"S{i}" for i in range(6)]] * 3),
        restaurant_name="Test", hourly_rate=20.0, labor_target=30.0,
        yoy_context=yoy, monthly_revenue_target=365000.0)
    assert result["daily_target_hours"]["2026-08-28"] == 60.0
    assert result["daily_target_hours"]["2026-08-29"] == 70.0
    assert "NOT scaled to the weekly budget" in captured["messages"][0]["content"]


def test_full_yoy_coverage_is_scaled(monkeypatch):
    dates = [f"2026-08-{d}" for d in range(24, 31)]
    dows = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    yoy = [{"next_week_dow": w, "next_week_date": d, "yoy_sales": 9000,
            "yoy_labor_pct": 28, "yoy_hours": 50} for w, d in zip(dows, dates)]
    _capture(monkeypatch)
    result = generate_optimized_schedule(
        _analysis(), _fridays([[f"S{i}" for i in range(6)]] * 3),
        restaurant_name="Test", hourly_rate=20.0, labor_target=30.0,
        yoy_context=yoy, monthly_revenue_target=365000.0)
    assert sum(result["daily_target_hours"].values()) == pytest.approx(
        result["hours_budget"], rel=0.02)


def test_the_roster_travels_with_the_result(monkeypatch):
    """The caller checks the model's rows against it — "use real employee
    names from the staff list" was prompt text with no enforcement."""
    _capture(monkeypatch)
    result = generate_optimized_schedule(
        _analysis(), _fridays([[f"S{i}" for i in range(6)]] * 3),
        restaurant_name="Test", hourly_rate=20.0, labor_target=30.0)
    assert set(result["roster"]) == {f"S{i}" for i in range(6)}
