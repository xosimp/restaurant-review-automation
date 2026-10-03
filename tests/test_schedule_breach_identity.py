"""Breaches are compared by what they are about, never by row position
(schedule audit 10/3/26 E-1, P-14), every pass asks one legality question
before it adds a shift (P-2), hourly hours are one number (P-6), role
families make "Server AM" and "Server PM" one role (D-13), and one
unreadable person no longer drops every rule for everyone after them (P-1)."""
import schedule_rules as sr
from schedule_rules import Constraints, breach_profile, regressions, hourly_hours
from shift_quality import role_family

WED = "2026-10-07"


def _c(**kw):
    c = Constraints(restaurant_id=1, week_dates=["2026-10-05", "2026-10-06", WED, "2026-10-08",
                                                  "2026-10-09", "2026-10-10", "2026-10-11"],
                    week_days=list(sr.DAYS))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(name, role, start, end, date=WED, hours=None):
    return {"date": date, "day": "Wednesday", "employee": name, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours) if hours is not None else "", "notes": ""}


def test_a_second_manager_gap_on_a_day_that_already_has_one_is_a_regression():
    # the audit's reproduction: a 9-10am gap already there; handing the
    # manager's evening bartender row to a non-manager opens 4-11pm too
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann", "Bob"], active={"max", "ann", "bob"})
    before_rows = [_row("Ann", "Server", "9:00am", "9:00pm", hours=12),
                   _row("Max", "Manager", "10:00am", "3:00pm", hours=5),
                   _row("Max", "Bartender", "3:00pm", "10:00pm", hours=7),
                   _row("Bob", "Server", "4:00pm", "10:00pm", hours=6)]
    after_rows = [dict(r) for r in before_rows]
    after_rows[2]["employee"] = "Bob2"
    c.active.add("bob2"); c.roster_names.append("Bob2")
    before, after = breach_profile(before_rows, c), breach_profile(after_rows, c)
    assert before["manager"] == {WED: 60}
    assert after["manager"] == {WED: 60 + 420}       # 9-10am and now 3-10pm
    worse = regressions(before, after)
    assert worse and worse[0]["id"] == ("no_manager", WED)
    # the old comparison saw nothing: both gaps pin to row 0 with one key
    old = {(v["index"], v["kind"]) for v in sr.violations(before_rows, c) if v["hard"]}
    new = {(v["index"], v["kind"]) for v in sr.violations(after_rows, c) if v["hard"]}
    assert not (new - old)


def test_a_breach_made_worse_is_a_regression_even_when_it_was_there():
    c = _c(roster_names=["Ann"], active={"ann"})
    c.compliance["weekly_hours_ceiling"] = 40.0
    rows = [_row("Ann", "Server", "9:00am", "9:00pm", date=d, hours=12)
            for d in ["2026-10-05", "2026-10-06", WED, "2026-10-08"]]
    more = rows + [_row("Ann", "Server", "9:00am", "1:00pm", date="2026-10-09", hours=4)]
    worse = regressions(breach_profile(rows, c), breach_profile(more, c))
    assert [w["id"][0] for w in worse] == ["over_max_hours"]
    assert worse[0]["after"] > worse[0]["before"]


def test_a_fixed_breach_and_a_moved_row_are_not_regressions():
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann"], active={"max", "ann"})
    gap = [_row("Ann", "Server", "9:00am", "3:00pm", hours=6), _row("Max", "Manager", "10:00am", "3:00pm", hours=5)]
    fixed = [dict(gap[0]), dict(gap[1], shift_start="9:00am", scheduled_hours="6")]
    assert regressions(breach_profile(gap, c), breach_profile(fixed, c)) == []
    # reordering rows changes indexes, not breaches
    assert regressions(breach_profile(gap, c), breach_profile(list(reversed(gap)), c)) == []


def test_can_add_refuses_a_minor_past_their_end_a_seventh_day_and_overtime():
    c = _c(roster_names=["Kid", "Cook"], active={"kid", "cook"}, minors={"kid"}, minor_bands={"kid": "16-17"})
    c.compliance["minor_latest_end"] = "10:00pm"
    ok, why = c.can_add(_row("Kid", "Host", "5:00pm", "11:00pm", hours=6), [])
    assert not ok and "minor" in why
    week = [_row("Cook", "Cook", "8:00am", "2:00pm", date=d, hours=6)
            for d in ["2026-10-05", "2026-10-06", WED, "2026-10-08", "2026-10-09", "2026-10-10"]]
    ok, why = c.can_add(_row("Cook", "Cook", "8:00am", "2:00pm", date="2026-10-11", hours=6), week)
    assert not ok and "in a row" in why
    long = [_row("Cook", "Cook", "8:00am", "6:00pm", date=d, hours=10) for d in ["2026-10-05", "2026-10-06", WED]]
    ok, why = c.can_add(_row("Cook", "Cook", "8:00am", "6:00pm", date="2026-10-08", hours=10), long)
    assert ok                      # 40h exactly
    ok, why = c.can_add(_row("Cook", "Cook", "8:00am", "1:00pm", date="2026-10-09", hours=5),
                        long + [_row("Cook", "Cook", "8:00am", "6:00pm", date="2026-10-08", hours=10)])
    assert not ok and "over 40h" in why
    # an owner-set maximum above the ceiling is still checked against the
    # overtime line by default
    c.hours_limits["cook"] = (None, 50)
    ok, why = c.can_add(_row("Cook", "Cook", "8:00am", "1:00pm", date="2026-10-09", hours=5),
                        long + [_row("Cook", "Cook", "8:00am", "6:00pm", date="2026-10-08", hours=10)],
                        overtime=False)
    assert not ok                  # the ceiling still caps a 50h maximum today (P-12 changes this)


def test_can_add_sums_a_split_minor_day():
    c = _c(roster_names=["Kid"], active={"kid"}, minors={"kid"}, minor_bands={"kid": "14-15"})
    lunch = _row("Kid", "Host", "11:00am", "1:00pm", date="2026-10-10", hours=2)    # a Saturday
    ok, _ = c.can_add(_row("Kid", "Host", "2:00pm", "6:00pm", date="2026-10-10", hours=4), [lunch])
    assert ok                      # 6h on a non-school day, cap 8h
    ok, why = c.can_add(_row("Kid", "Host", "2:00pm", "9:00pm", date="2026-10-10", hours=7), [lunch])
    assert not ok


def test_hourly_hours_leaves_salaried_people_out():
    c = _c(salaried={"erik"})
    rows = [_row("Erik", "Owner", "9:00am", "9:00pm", hours=12), _row("Ann", "Server", "5:00pm", "10:00pm", hours=5)]
    assert hourly_hours(rows, c) == 5.0
    assert hourly_hours(rows, salaried=["Erik"]) == 5.0


def test_role_family_joins_daypart_job_codes():
    assert role_family("Server AM") == role_family("server pm") == role_family("PM Server") == "server"
    assert role_family("Bartender - Lunch") == "bartender"
    assert role_family("Line Cook") == "line cook"
    assert role_family("Shift Lead") == "shift lead"
    assert role_family("AM") == "am"
    assert role_family("Server AM", {"server am": "front server"}) == "front server"
    c = _c(role_families={"barback pm": "bartender"})
    assert c.family("Barback PM") == "bartender"


def test_an_acting_manager_counts_on_their_dates_only():
    c = _c(managers={"max": "Manager"}, acting_managers={"ann": {WED}}, roster_names=["Max", "Ann"],
           active={"max", "ann"})
    rows = [_row("Ann", "Server", "9:00am", "3:00pm", hours=6)]
    assert sr.manager_gaps(rows, c) == {}
    thu = [_row("Ann", "Server", "9:00am", "3:00pm", date="2026-10-08", hours=6)]
    assert "2026-10-08" in sr.manager_gaps(thu, c)


def test_one_unreadable_person_keeps_everyone_elses_rules(db_path, monkeypatch):
    import models
    import staff_settings
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="P1", owner_email="p1@x.test"), db_path=db_path)
    people = [
        {"name": "Ann", "active": True, "role": "Server", "settings": {"time_windows": {"Monday": 5}}},
        {"name": "Max", "active": True, "role": "General Manager", "settings": {"max_hours": 45}},
        {"name": "Kid", "active": True, "role": "Host", "settings": {"is_minor": True, "minor_age_band": "16-17"}},
    ]
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: people)
    c = sr.build_constraints(rid, ["2026-10-05"], ["Monday"], db_path=db_path)
    assert "max" in c.managers and "kid" in c.minors and c.hours_limits.get("max") == (None, 45)
    assert [p.get("name") for p in c.input_problems if p["source"] == "settings"] == ["Ann"]
    assert "ann" in c.active
