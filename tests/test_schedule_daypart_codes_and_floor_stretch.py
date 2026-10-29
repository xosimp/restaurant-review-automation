"""EJ's week of 11/4/26 scored 39 (Will, 10/29/26). The root causes, each
pinned:

  CODES    a job code is counted as its family's code for the half of the
           day the shift is on the floor in — the usual crew
           (labor.historical_patterns, concurrent_patterns), the draft's
           count (ShiftContext.by_role) and the generator's label
           (schedule_shapes, schedule_job._label_by_daypart) all agree. The
           draft labelled evening hosts Host AM, the punches had Host AM
           people at dinner, and 11 of 14 shifts read "No host PM" with three
           hosts on.
  STRETCH  the floors stage runs a shift on over a floor's short half hours
           when nobody more can be added (Saturday lunch: six cooks free, all
           six on, one ending at 1:00pm against a floor to 2:30pm).
  RECORD   a measured labor standard is held to the larger of the meal's
           own record and its usual crew with the date's demand (Bears
           Sunday brunch: 13 cooks asked of a meal that has never run more
           than 6); the owner's own standard is never held.
  REASON   a role left empty while somebody who holds it could take it says
           so, never "nobody who works it can take a shift".
"""
from datetime import date
from types import SimpleNamespace

import pytest

import labor
import models
import schedule_engine as se
import schedule_job
import schedule_requirements as sreq
import schedule_rules as sr
import schedule_shapes as shapes
import shift_quality as sq
from schedule_roles import code_for_daypart

WED = "2026-11-04"
FRI = "2026-10-16"


@pytest.fixture
def no_avail(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])


def _c(dates=(FRI,), **kw):
    dates = list(dates)
    c = sr.Constraints(restaurant_id=1, week_dates=dates,
                       week_days=[date.fromisoformat(d).strftime("%A") for d in dates])
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(emp, start, end, role, d=FRI):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": d, "day": date.fromisoformat(d).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": f"{((e - s) % 1440) / 60:g}", "notes": ""}


# ── CODES ─────────────────────────────────────────────────────────────────

def test_a_code_naming_the_other_half_of_the_day_counts_as_its_partner():
    codes = ["Host AM", "Host PM", "Server AM", "Server PM", "Kitchen", "Lunch Bar", "Dinner Bar"]
    assert code_for_daypart("Host AM", "night", codes) == "Host PM"
    assert code_for_daypart("Host PM", "morning", codes) == "Host AM"
    assert code_for_daypart("Host AM", "morning", codes) == "Host AM"
    assert code_for_daypart("Lunch Bar", "night", codes) == "Dinner Bar"
    assert code_for_daypart("Kitchen", "night", codes) == "Kitchen"
    # No partner for the other half: the code stays itself.
    assert code_for_daypart("Barback AM", "night", codes + ["Barback AM"]) == "Barback AM"
    # A duty is a job of its own at any hour.
    assert code_for_daypart("Server Closer", "morning", ["Server Closer", "Server Opener"]) == "Server Closer"


def test_hosts_coded_am_on_at_dinner_cover_the_dinner_host_requirement():
    rows = [_row("Ann", "3:45pm", "7:00pm", "Host AM", WED), _row("Bo", "5:00pm", "9:15pm", "Host AM", WED),
            _row("Cy", "4:00pm", "11:00pm", "Server PM", WED)]
    ctxs = sq.build_contexts(rows, typical_headcount={("Wednesday", "night"): {"Host PM": 2, "Server PM": 1},
                                                      ("Wednesday", "morning"): {"Host AM": 1}})
    night = next(x for x in ctxs if x.daypart == "night")
    assert night.by_role.get("Host PM") == ["Ann", "Bo"] and "Host AM" not in night.by_role
    cov = sq.dim_coverage(night)
    assert cov.score == 100 and not any("host" in w.lower() for w in cov.weaknesses)


def test_the_usual_crew_counts_a_dinner_punch_under_the_dinner_code():
    shifts = []
    for d in ("2026-09-02", "2026-09-09", "2026-09-16", "2026-09-23"):
        shifts += [_row("Ann", "4:00pm", "9:00pm", "Host AM", d), _row("Bo", "5:00pm", "10:00pm", "Host PM", d),
                   _row("Cy", "10:30am", "3:00pm", "Host AM", d)]
    out = labor.historical_patterns(shifts)
    night = out["typical_headcount"][("Wednesday", "night")]
    assert night.get("Host PM") == 2 and "Host AM" not in night
    assert out["typical_headcount"][("Wednesday", "morning")] == {"Host AM": 1}
    conc = out["concurrent"][("Wednesday", "night")]
    assert "Host AM" not in conc and conc["Host PM"]["most"] == 2 and conc["Host PM"]["most_people"] == 2


def test_the_generators_last_word_labels_each_shift_by_its_daypart():
    rows = [_row("Ann", "5:00pm", "9:15pm", "Host AM", WED), _row("Bo", "11:45am", "3:00pm", "Busser PM", WED),
            _row("Cy", "5:00pm", "9:00pm", "Server AM", WED), _row("Di", "10:00am", "2:00pm", "Kitchen", WED),
            dict(_row("Ed", "5:00pm", "9:00pm", "Host AM", WED), locked="1")]
    codes = {"Host AM", "Host PM", "Busser AM", "Busser PM", "Server AM", "Server PM", "Kitchen"}
    n = sq.label_by_daypart(rows, codes, keep=lambda r: r.get("locked") == "1")
    assert n == 3
    assert [r["role"] for r in rows] == ["Host PM", "Busser AM", "Server PM", "Kitchen", "Host AM"]


def test_the_pipeline_labels_only_the_days_it_may_change():
    rows = [_row("Ann", "5:00pm", "9:15pm", "Host AM", WED), _row("Bo", "5:00pm", "9:15pm", "Host AM", FRI),
            _row("Cy", "11:00am", "3:00pm", "Host PM", FRI)]
    cand = SimpleNamespace(rows=rows, pass_sig={"roster_roles": {}}, result={})
    ctx = SimpleNamespace(reqs=[], pass_editable={WED}, constraints=None)
    schedule_job._label_by_daypart(cand, ctx)
    assert [r["role"] for r in rows] == ["Host PM", "Host AM", "Host PM"]
    assert cand.result["relabelled"] == 1


# ── STRETCH ───────────────────────────────────────────────────────────────

def test_the_floors_stage_runs_a_shift_on_when_nobody_more_can_be_added(no_avail):
    floors = {"Kitchen": {"morning": 2, "night": 0, "days": {}}}
    c = _c(role_floors=floors, roster_roles={"Ann": "Kitchen", "Bo": "Kitchen"}, roster_names=["Ann", "Bo"],
           active={"ann", "bo"})
    rows = [_row("Ann", "9:00am", "3:00pm", "Kitchen"), _row("Bo", "9:00am", "1:00pm", "Kitchen")]
    assert [v for v in sr.violations(rows, c) if v["kind"] == "coverage_floor"]
    report = {}
    out, added, _dates = se._ensure_role_floors([dict(r) for r in rows], [FRI], ["Friday"], 1, {}, {},
                                                floors=floors, constraints=c, report=report)
    assert added == 0 and len(out) == 2
    bo = next(r for r in out if r["employee"] == "Bo")
    assert bo["shift_start"] == "9:00am" and sr.parse_minutes(bo["shift_end"]) >= 14 * 60 + 30
    assert not [v for v in sr.violations(out, c) if v["kind"] == "coverage_floor"]
    assert report["stretched"][0]["employee"] == "Bo" and report["stretched"][0]["from"] == "9:00am-1:00pm"


def test_a_locked_shift_is_never_stretched(no_avail):
    floors = {"Kitchen": {"morning": 2, "night": 0, "days": {}}}
    c = _c(role_floors=floors, roster_roles={"Ann": "Kitchen", "Bo": "Kitchen"}, roster_names=["Ann", "Bo"],
           active={"ann", "bo"})
    rows = [_row("Ann", "9:00am", "3:00pm", "Kitchen"), dict(_row("Bo", "9:00am", "1:00pm", "Kitchen"), locked="1")]
    out, added, _dates = se._ensure_role_floors([dict(r) for r in rows], [FRI], ["Friday"], 1, {}, {},
                                                floors=floors, constraints=c)
    assert added == 0 and next(r for r in out if r["employee"] == "Bo")["shift_end"] == "1:00pm"


# ── RECORD ────────────────────────────────────────────────────────────────

def test_a_measured_standard_is_held_to_the_meals_record_or_its_demand_scaled_usual():
    driven = {"Kitchen": {"half": {660: 10, 690: 13, 720: 8}, "peak": 13, "distinct": 13,
                          "reason": "4 tickets per cook-hour", "source": "measured"},
              "Busser AM": {"half": {690: 5}, "peak": 5, "distinct": 5, "reason": "x", "source": "measured"},
              "Server AM": {"half": {690: 12}, "peak": 12, "distinct": 12, "reason": "y", "source": "yours"}}
    conc = {"Kitchen": {"peak": 4, "most": 5, "most_people": 6}, "Busser AM": {"peak": 1, "most": 1, "most_people": 1},
            "Server AM": {"peak": 5, "most": 6, "most_people": 8}}
    # A Bears Sunday: +40% on kitchen and bussers, the record already holding
    # the meal's Bears Sundays — the larger of the two, never one on the other.
    sreq._hold_to_record(driven, conc, {"Kitchen": 1.4, "Busser AM": 1.4, "Server AM": 1.4},
                         {"Kitchen": 5, "Busser AM": 1, "Server AM": 6})
    k = driven["Kitchen"]
    assert k["distinct"] == 7 and k["peak"] == 6 and k["half"] == {660: 6, 690: 6, 720: 6}
    assert "held to the most this meal has run here (6)" in k["reason"]
    assert driven["Busser AM"]["distinct"] == 1 and driven["Busser AM"]["half"] == {690: 1}
    # The owner's own standard is their word.
    assert driven["Server AM"]["distinct"] == 12 and driven["Server AM"]["peak"] == 12


def test_the_record_never_holds_a_crew_under_its_usual():
    driven = {"Kitchen": {"half": {690: 9}, "peak": 9, "distinct": 9, "reason": "r", "source": "measured"}}
    sreq._hold_to_record(driven, {"Kitchen": {"peak": 4, "most": 4, "most_people": 4}}, {"Kitchen": 0.8},
                         {"Kitchen": 6})
    assert driven["Kitchen"]["distinct"] == 6 and driven["Kitchen"]["peak"] == 4


# ── REASON ────────────────────────────────────────────────────────────────

def test_a_role_left_empty_names_who_could_have_worked_it(no_avail):
    c = _c(roster_roles={"Josh": "Barback PM", "Ann": "Server PM"}, roster_names=["Josh", "Ann"],
           active={"josh", "ann"})
    rows = [_row("Ann", "4:00pm", "11:00pm", "Server PM")]
    reqs = [{"date": FRI, "daypart": "night", "roles": [{"role": "Barback PM", "required": 1}]}]
    got = shapes.uncovered_roles(rows, reqs, c, roster_roles=c.roster_roles)
    assert len(got) == 1 and got[0]["why"].startswith("Josh could work it")
    assert "nobody who works it" not in got[0]["why"]


# ── a game's role lifts stay on the meal it falls on ─────────────────────

def test_an_events_role_lifts_never_land_on_the_meal_it_lowers(db_path, monkeypatch):
    # A split measured from the label's own nights puts lunch at its own
    # figure (-14%), not 1.0: the day's role lifts landed on lunch too.
    import schedule_economics as econ
    rid = models.create_restaurant(models.Restaurant(name="Game Bar", owner_email="game@x.test",
                                                     timezone="America/Chicago"))
    monkeypatch.setattr(econ._DemandContext, "event_split", lambda self, d, occ: {"morning": 0.86, "night": 1.89})
    monkeypatch.setattr(econ._DemandContext, "event_labels", lambda self, d: ["bears"])
    monkeypatch.setattr(econ._DemandContext, "role_lifts",
                        lambda self, labels, pct: {"server am": 1.606, "server pm": 1.976, "kitchen": 1.55})
    sunday = "2026-11-08"
    dd = econ.date_demand(rid, [sunday], signals_by_date={sunday: {"lift_pct": 60, "labels": ["Bears home game"],
                                                                  "lift_source": "measured"}},
                          today=date(2026, 10, 29), db_path=db_path)[sunday]
    assert dd["parts"]["morning"]["roles"] == {}
    assert dd["parts"]["night"]["roles"]["server pm"] > dd["parts"]["night"]["roles"]["kitchen"] > 1.0
