"""Schedule audit 10/3/26, workstream E — last year, holidays, the baseline.

E-8   a holiday reads last year's SAME holiday night (Christmas Eve 12/24/26
      reads 12/24/25, a Wednesday — never last Christmas Day), the row says
      which night it is, and a date can carry two holidays.
D-28  year-over-year never reads another weekday: the same weekday a week
      either side stands in, marked.
D-29  last year's sales are moved by this year's trend, and the block is
      context secondary to the projection.
L-1   published weeks feed the baseline for the people who never punch.
D-4   salaried punches never count toward the usual crew.
L-2   borrowing is per role family.
"""
import json
import types
from datetime import date, timedelta

import pytest

import labor
import models
import schedule_economics as econ
from intelligence import staffing


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _exec(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _rid(name="Holiday Co"):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                                      timezone="America/Chicago"))


def _day(rid, iso, sales, hours=50.0):
    _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, labor_pct, "
          "labor_cost) VALUES (?,?,?,?,?,?,?)",
          (rid, iso, date.fromisoformat(iso).strftime("%A"), sales, hours, 30.0, sales * 0.3))


# ── E-8: the holiday calendar and last year's holiday night ──────────────

def test_a_date_carries_every_holiday_on_it_the_fixed_one_first():
    names = econ.holiday_names(2027)
    assert names["2027-02-14"] == ["Valentine's Day", "Super Bowl Sunday"]
    assert econ._holiday_dates(2027)["2027-02-14"] == "Valentine's Day", "the Super Bowl no longer overwrites it"
    assert econ.last_years_holiday_night("Christmas Eve", date(2026, 12, 24)) == "2025-12-24"
    assert econ.last_years_holiday_night("Thanksgiving", date(2026, 11, 26)) == "2025-11-27"


def test_christmas_eve_reads_last_christmas_eve_and_says_so(db_path):
    rid = _rid()
    _day(rid, "2025-12-24", 9000)          # last Christmas Eve, a Wednesday
    _day(rid, "2025-12-25", 500)           # last Christmas Day, the 52-week match
    _day(rid, "2025-12-31", 12000)         # last New Year's Eve
    _day(rid, "2026-01-01", 800)           # last New Year's Day
    rows = models.get_yoy_schedule_context(rid, ["2026-12-24", "2026-12-31"], db_path=db_path,
                                           today=date(2026, 12, 20))
    eve, nye = rows
    assert eve["yoy_date"] == "2025-12-24" and eve["yoy_dow"] == "Wednesday" and eve["yoy_sales"] == 9000
    assert eve["holiday_matched"] and eve["yoy_match"] == "holiday" and eve["holiday_name"] == "Christmas Eve"
    assert nye["yoy_date"] == "2025-12-31" and nye["yoy_sales"] == 12000
    # Without last Christmas Eve on file, the row is an ordinary Thursday, and
    # never last Christmas Day.
    rid2 = _rid("Thin Co")
    _day(rid2, "2025-12-25", 500)
    _day(rid2, "2025-12-18", 7000)
    row = models.get_yoy_schedule_context(rid2, ["2026-12-24"], db_path=db_path, today=date(2026, 12, 20))[0]
    assert row["yoy_date"] == "2025-12-18" and row["is_holiday"] and not row["holiday_matched"]


def test_last_years_july_fourth_is_no_ordinary_friday(db_path):
    rid = _rid()
    _day(rid, "2025-07-04", 15000)          # the 52-week match of Friday 7/3/26
    _day(rid, "2025-07-11", 6000)
    row = models.get_yoy_schedule_context(rid, ["2026-07-03"], db_path=db_path, today=date(2026, 6, 28))[0]
    assert row["yoy_date"] == "2025-07-11" and row["yoy_substituted"] and not row["is_holiday"]


def test_the_prompt_names_the_night_last_year_it_reads(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    trend = {"ratio": 1.1, "nights": 40, "applied": True, "basis": "the last 8 weeks ran 10% above the same 40 nights last year"}
    yoy = [{"next_week_dow": "Thursday", "next_week_date": "2026-12-24", "yoy_date": "2025-12-24",
            "yoy_dow": "Wednesday", "yoy_match": "holiday", "holiday_matched": True, "is_holiday": True,
            "holiday_name": "Christmas Eve", "yoy_sales": 9000.0, "yoy_sales_adjusted": 9900.0, "yoy_trend": trend},
           {"next_week_dow": "Friday", "next_week_date": "2026-12-25", "yoy_date": "2025-12-19",
            "yoy_dow": "Friday", "yoy_match": "same_weekday_shift", "yoy_substituted": True, "is_holiday": True,
            "holiday_name": "Christmas Day", "holiday_matched": False, "yoy_sales": 7000.0,
            "yoy_sales_adjusted": 7700.0, "yoy_trend": trend}]
    analysis = {"overall_labor_pct": 25.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "total_sales": 0, "period_days": 0, "by_day": {}}
    labor.generate_optimized_schedule(analysis, [], roster=[("Ana", "Server")], week_start="2026-12-21",
                                      hourly_rate=20.0, labor_target=30.0, yoy_context=yoy,
                                      projected_revenue_override=60000)
    prompt = captured["messages"][0]["content"]
    block = prompt.split("Year-over-year data", 1)[1].split("\n\n", 1)[0]
    assert "last year's Christmas Eve (Wednesday 12/24/25) → $9,900 sales at this year's pace ($9,000 last year)" in block
    assert "same weekday a week off (Friday 12/19/25" in block
    assert "Christmas Day this year, but last year's Christmas Day has no figure on file" in block
    assert "USE THIS" not in prompt and "primary demand projection" not in prompt
    assert "secondary to the week's projection" in block and "10% above" in block
    assert "match staffing to last year's holiday labor hours" not in prompt


def test_each_holiday_on_a_date_reads_its_own_night(db_path):
    rid = _rid()
    # Valentine's Day 2026 (a Saturday) and Super Bowl Sunday 2026 (2/8).
    _day(rid, "2026-02-14", 12000)
    for d in ("2026-01-17", "2026-01-24", "2026-01-31", "2026-02-07", "2026-02-21", "2026-02-28"):
        _day(rid, d, 8000)
    lift = econ.holiday_lift(rid, ["2027-02-14"], db_path=db_path)["2027-02-14"]
    assert lift["names"] == ["Valentine's Day", "Super Bowl Sunday"]
    assert lift["by_name"]["Valentine's Day"]["lift_pct"] == 50 and lift["by_name"]["Valentine's Day"]["date"] == "2026-02-14"
    assert lift["by_name"]["Super Bowl Sunday"]["date"] == "2026-02-08"
    assert lift["name"] == "Valentine's Day" and lift["lift_pct"] == 50


# ── D-29: this year's trend ──────────────────────────────────────────────

def test_last_year_is_moved_by_this_years_trend(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    for k in range(1, 57):
        d = today - timedelta(days=k)
        _day(rid, d.isoformat(), 5750.0)                                    # this year
        _day(rid, (d - timedelta(days=364)).isoformat(), 5000.0)            # the same nights last year
    _day(rid, "2025-10-06", 5000.0)                                         # Monday 10/5/26's match
    trend = models.yoy_trend(rid, today=today, db_path=db_path)
    assert trend["applied"] and trend["ratio"] == 1.15 and trend["nights"] == 56
    row = models.get_yoy_schedule_context(rid, ["2026-10-05"], db_path=db_path, today=today)[0]
    assert row["yoy_sales"] == 5000.0 and row["yoy_sales_adjusted"] == 5750.0
    # The budget's last-year projection reads the adjusted figure.
    yoy = [dict(row, next_week_date=d, yoy_sales=5000.0, yoy_sales_adjusted=5750.0)
           for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11")]
    plan = labor.week_hours_plan({}, [r["next_week_date"] for r in yoy], 30.0, 20.0, yoy_context=yoy)
    assert plan["revenue_basis"] == "last_year" and plan["projected_revenue"] == 7 * 5750.0


def test_too_few_paired_nights_leave_last_year_as_it_was(db_path):
    rid = _rid()
    today = date(2026, 10, 3)
    for k in range(1, 6):
        d = today - timedelta(days=k)
        _day(rid, d.isoformat(), 9000.0)
        _day(rid, (d - timedelta(days=364)).isoformat(), 5000.0)
    trend = models.yoy_trend(rid, today=today, db_path=db_path)
    assert trend["applied"] is False and trend["ratio"] == 1.0 and "needs 20" in trend["basis"]


# ── L-1 / D-4: the baseline ──────────────────────────────────────────────

def _punch(d, emp, role, s="17:00", e="23:00"):
    return {"date": d, "employee": emp, "role": role, "shift_start": s, "shift_end": e}


def test_salaried_punches_never_count_toward_the_usual_crew():
    fridays = ("2026-09-18", "2026-09-25", "2026-10-02")
    shifts = [_punch(f, "Ana", "Server") for f in fridays] + [_punch("2026-09-25", "Erik Baylis", "Manager FOH")]
    raw = labor.historical_patterns(shifts)
    assert "Manager FOH" not in raw["typical_headcount"][("Friday", "night")]     # one punch in three: rounds away
    shifts += [_punch(f, "Erik Baylis", "Manager FOH") for f in fridays[:2]]
    noisy = labor.historical_patterns(shifts)
    assert noisy["typical_headcount"][("Friday", "night")]["Manager FOH"] == 1
    clean = labor.historical_patterns(shifts, salaried={"erik baylis"})
    assert "Manager FOH" not in clean["typical_headcount"][("Friday", "night")]
    # Who can flex is a capability, read from every punch — the salaried included.
    flex = labor.historical_patterns(shifts + [_punch("2026-10-02", "Erik Baylis", "Bartender")],
                                     salaried={"erik baylis"})
    assert flex["cross_trained"]["Erik Baylis"] == ["Bartender", "Manager FOH"]


def test_published_weeks_give_the_no_punch_people_their_usual_crew():
    fridays = ("2026-09-18", "2026-09-25", "2026-10-02")
    shifts = [_punch(f, "Ana", "Server") for f in fridays]
    published = [{"date": f, "employee": "Erik Baylis", "role": "Manager FOH", "shift_start": "4:00pm",
                  "shift_end": "11:00pm"} for f in fridays]
    # Ana's published rows never double her punches.
    published += [{"date": f, "employee": "Ana", "role": "Server", "shift_start": "4:00pm", "shift_end": "11:00pm"}
                  for f in fridays]
    pats = labor.historical_patterns(shifts, published_rows=published, salaried={"erik baylis"})
    slot = pats["typical_headcount"][("Friday", "night")]
    assert slot == {"Server": 1, "Manager FOH": 1}
    assert pats["published_headcount"][("Friday", "night")] == {"Manager FOH": 1}


def test_the_baseline_reads_the_published_weeks_from_the_record(db_path, monkeypatch):
    rid = _rid()
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erik Baylis", "annual": 150000}])},
                             db_path=db_path)
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           "2026-09-25,Friday,Erik Baylis,Manager FOH,4:00pm,11:00pm,7,\n")
    hid = models.save_schedule_history(rid, "2026-09-21", "2026-09-27", 7, 100, 30, csv, [], db_path=db_path)
    _exec("UPDATE schedule_history SET published_at='2026-09-20 10:00:00' WHERE id=?", (hid,))
    shifts = [_punch("2026-09-25", "Ana", "Server")]
    pats = labor.staffing_baseline(rid, shifts=shifts, before="2026-10-05")
    assert pats["typical_headcount"][("Friday", "night")] == {"Server": 1, "Manager FOH": 1}


# ── L-2: borrowing per role family ───────────────────────────────────────

def test_a_family_with_its_own_crew_is_never_borrowed_for_but_a_missing_one_is_looked_up(db_path):
    rid = _rid("Fam Co")
    for k in range(20):
        _day(rid, (date.today() - timedelta(days=k + 1)).isoformat(), 6000)
    shifts = [_punch((date.today() - timedelta(days=k)).isoformat(), "Ana", "Server") for k in range(1, 20)]
    own = {("Friday", "night"): {"Server": 3}}
    assert staffing.own_families(rid, own_typical=own) == {"server"}
    done = staffing.starting_headcount(rid, roster_roles={"Ana": "Server"}, shifts=shifts, db_path=db_path,
                                       own_typical=own)
    assert done["available"] is False and "every role family on its roster" in done["reason"]
    # A manager on the roster with no usual crew of their own: the lookup
    # goes on for that family (here it stops at the unconfirmed profile).
    more = staffing.starting_headcount(rid, roster_roles={"Ana": "Server", "Erik": "Manager FOH"}, shifts=shifts,
                                       db_path=db_path, own_typical=own)
    assert more["available"] is False and "every role family" not in more["reason"]
    assert "No restaurant profile is confirmed" in more["reason"]


def test_the_borrowed_note_names_the_families_it_borrows_for():
    start = {"available": True, "own_history": True, "families": ["manager"],
             "note": "Borrowed for managers (your own history has none): the median of 9+ peers — scaled to x."}
    block = staffing.prompt_block(start)
    assert "own history has no usual crew for managers" in block and "the median of 9+ peers" in block
