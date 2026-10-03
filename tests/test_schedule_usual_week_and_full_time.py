"""A person's usual week, and what full-time means (schedule audit 10/3/26
D-37, D-41).

D-37: the usual pattern was days and dayparts only; desired hours existed
only if staff stated them, so a 32h-a-week regular could be cut to 12h with
no signal — a retention risk the prompt, the scorer and the review never saw.
D-41: employment type was a prompt word and nothing else — at Simple EJ's
nearly everyone is "part", so the field carried no information.
"""
from datetime import date, timedelta

import pytest

import models
import schedule_requirements as req
import schedule_rules as sr
import shift_quality as sq

TODAY = date(2026, 10, 3)            # a Saturday
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


def _weeks_of(name, weeks, per_week, hours, start="4:30pm", end="12:30am"):
    rows = []
    monday = TODAY - timedelta(days=TODAY.weekday())
    for w in range(1, weeks + 1):
        base = monday - timedelta(weeks=w)
        for i in range(per_week):
            d = base + timedelta(days=i)
            rows.append({"employee": name, "date": d.isoformat(), "shift_start": start, "shift_end": end,
                         "scheduled_hours": None, "actual_hours": hours})
    return rows


# ── D-37 ────────────────────────────────────────────────────────────────────

def test_the_usual_pattern_carries_their_usual_hours_and_start():
    rows = _weeks_of("Ana", 8, 4, 8.0) + _weeks_of("Ben", 1, 3, 6.0, start="10:00am", end="4:00pm")
    # This week so far is not a week yet.
    rows.append({"employee": "Ana", "date": (TODAY - timedelta(days=1)).isoformat(), "shift_start": "4:30pm",
                 "shift_end": "12:30am", "actual_hours": 8.0})
    pat = models.usual_pattern(rows, today=TODAY)
    assert pat["Ana"]["avg_hours"] == 32.0 and pat["Ana"]["hours_weeks"] == 8
    assert pat["Ana"]["starts"] == {"night": "4:30pm"}
    assert pat["Ben"]["avg_hours"] is None                 # one week is not a usual week
    assert pat["Ben"]["starts"] == {"morning": "10:00am"}


def test_the_prompt_says_each_regulars_usual_hours():
    pattern = {"Ana": {"days": ["Friday"], "dayparts": ["night"], "avg_hours": 32.0, "starts": {"night": "4:30pm"}},
               "Ben": {"days": ["Monday"], "dayparts": ["morning"], "avg_hours": None}}
    out = req.usual_pattern_block(pattern, ["Ana", "Ben"])
    assert "USUAL HOURS" in out and "Ana ~32h (starts 4:30pm)" in out and "Ben ~" not in out
    assert "USUAL HOURS" in req.usual_pattern_block({"Cy": {"avg_hours": 20.0}}, ["Cy"])


def _ctx(hours_this_week):
    rows = [{"employee": "Ana", "role": "Server", "date": "2026-10-09", "shift_start": "4:30pm",
             "shift_end": "10:30pm", "scheduled_hours": str(hours_this_week)}]
    return sq.build_contexts(rows, prior_pattern={"Ana": {"days": ["Friday"], "dayparts": ["night"],
                                                          "avg_hours": 32.0}})[0]


def test_a_regular_cut_well_below_their_usual_week_is_a_stability_weakness():
    res = sq.dim_stability(_ctx(12))
    assert res.score == 0 and res.facts["hours_cut"] == [{"name": "Ana", "usual": 32.0, "week": 12.0}]
    assert "Ana usually works about 32h a week; 12h this week." in res.weaknesses
    fine = sq.dim_stability(_ctx(28))
    assert fine.score == 100 and not fine.facts["hours_cut"]


# ── D-41 ────────────────────────────────────────────────────────────────────

@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import staff_settings
    monkeypatch.setattr(staff_settings, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(sr, "get_conn", lambda *a, **k: real(db_path))
    r = models.create_restaurant(models.Restaurant(name="FT Co", owner_email="ft@x.test", module_labor=1))
    for n in ("Ana", "Ben", "Cal", "Erik"):
        models.add_manual_team_member(r, n, role="Cook")
    staff_settings.upsert(r, "Ana", employment_type="full")
    staff_settings.upsert(r, "Ben", employment_type="full", min_hours=36)
    staff_settings.upsert(r, "Cal", employment_type="part")
    staff_settings.upsert(r, "Erik", employment_type="full")
    import json
    models.update_restaurant(r, {"salaried_staff_json": json.dumps([{"name": "Erik", "annual": 150000}])})
    return r


def test_full_time_means_a_minimum_unless_the_owner_set_one(rid):
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert c.hours_limits["ana"] == (30.0, None) and "ana" in c.full_time_default
    assert c.hours_limits["ben"][0] == 36 and "ben" not in c.full_time_default
    assert "cal" not in c.hours_limits                      # part-time: no default
    assert "erik" not in c.full_time_default                # salaried: pay doesn't follow hours
    rows = [{"date": WEEK[0], "day": "Monday", "employee": "Ana", "role": "Cook", "shift_start": "9:00am",
             "shift_end": "5:00pm", "scheduled_hours": "8"}]
    v = next(v for v in sr.violations(rows, c, person_only=True) if v["kind"] == "under_min_hours")
    assert "full-time, at least 30h" in v["detail"] and not v["hard"]
    assert "Ana: at least 30h (full-time)" in sr.prompt_block(c)


def test_the_owner_can_set_the_full_time_line_or_turn_it_off(rid):
    sr.save_compliance(rid, {"full_time_min_hours": 35})
    assert sr.build_constraints(rid, WEEK, list(sr.DAYS)).hours_limits["ana"] == (35.0, None)
    sr.save_compliance(rid, {"full_time_min_hours": 0})
    assert "ana" not in sr.build_constraints(rid, WEEK, list(sr.DAYS)).hours_limits
