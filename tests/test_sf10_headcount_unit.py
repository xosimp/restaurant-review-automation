"""One headcount unit everywhere — people on at once at a half hour
(schedule audit 10/10/26, workstream A, contract #2).

  BUGSGEN-3  the floors stage fills until the hard coverage_floor rule holds
             (family-summed, every half hour of the core window)
  LOGIC-7    the section cap holds people on at once, never a daypart's count
             of different people
  LOGIC-8    setup's suggested floors are concurrent counts the restaurant's
             own nights keep
  LOGIC-23   over_section_cap has a tier, so a lower stage that makes it
             worse is refused, and a severity that grows with the overage
  LOGIC-6    a "Leaner is fine" binds the one staffing baseline
  contract   labor.staffing_baseline carries `concurrent`; the floors set in
             the old unit are translated by crew_matrix.floors_on_at_once
"""
import json
from datetime import date, timedelta

import pytest

import crew_matrix as cm
import labor
import models
import schedule_engine as se
import schedule_rules as sr
import schedule_setup as ss
import shift_quality as sq

FRI = "2026-10-16"
SAT = "2026-10-17"


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


def _row(emp, start, end, role="Server", d=FRI):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": d, "day": date.fromisoformat(d).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": f"{((e - s) % 1440) / 60:g}", "notes": ""}


def _floor_breaches(rows, c):
    return [v for v in sr.violations(rows, c) if v["kind"] == "coverage_floor"]


# ── BUGSGEN-3: the floors stage stops only when the rule holds ────────────

def test_the_floors_stage_fills_the_half_hours_the_rule_holds_short(no_avail):
    floors = {"Server": {"morning": 0, "night": 2, "days": {}}}
    c = _c(role_floors=floors, roster_roles={"Ann": "Server", "Bo": "Server", "Cy": "Server", "Di": "Server"},
           roster_names=["Ann", "Bo", "Cy", "Di"], active={"ann", "bo", "cy", "di"})
    rows = [_row("Ann", "4:00pm", "8:00pm"), _row("Bo", "7:00pm", "10:00pm")]
    before = _floor_breaches(rows, c)
    assert before and "1 Server on at 5:30pm" in before[0]["detail"]
    # The old stage counted both as "present" for dinner and added nobody.
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], [FRI], ["Friday"], 1, {}, {},
                                               floors=floors, constraints=c)
    assert added >= 1 and dates == {FRI: added}
    assert not _floor_breaches(out, c)


def test_a_familys_floors_are_filled_to_their_sum(no_avail):
    floors = {"Server AM": {"morning": 0, "night": 1, "days": {}}, "Server PM": {"morning": 0, "night": 2, "days": {}}}
    c = _c(role_floors=floors, roster_roles={"Ann": "Server PM", "Bo": "Server PM", "Cy": "Server PM"},
           roster_names=["Ann", "Bo", "Cy"], active={"ann", "bo", "cy"})
    rows = [_row("Ann", "4:00pm", "10:00pm", "Server PM"), _row("Bo", "4:00pm", "10:00pm", "Server PM")]
    assert "your floor is 3" in _floor_breaches(rows, c)[0]["detail"]
    out, added, _dates = se._ensure_role_floors([dict(r) for r in rows], [FRI], ["Friday"], 1, {}, {},
                                                floors=floors, constraints=c)
    assert added == 1 and not _floor_breaches(out, c)
    assert sr.floor_needs(c, "Friday")[("server", "night")][0] == 3


def test_crew_add_one_is_a_legal_shift_over_the_core_window(no_avail):
    c = _c(roster_roles={"A": "Server", "B": "Server", "C": "Server", "Dee": "Server"},
           roster_names=["A", "B", "C", "Dee"], active={"a", "b", "c", "dee"})
    rows = [_row(n, "5:00pm", "11:00pm") for n in ("A", "B", "C")]
    row, hours = se.crew_add_suggestion(1, rows, FRI, "Server", "night", constraints=c)
    assert row["employee"] == "Dee" and row["role"] == "Server" and row["date"] == FRI
    sp = se._span_minutes(row)
    assert sp[0] <= 17 * 60 + 30 and sp[1] >= 20 * 60 + 30            # covers 5:30-8:30pm
    assert hours == float(row["scheduled_hours"])
    # nobody left to take it: no suggestion, never an invented person
    c.roster_roles = {"A": "Server", "B": "Server", "C": "Server"}
    assert se.crew_add_suggestion(1, rows, FRI, "Server", "night", constraints=c) is None


# ── LOGIC-23: the section cap is ranked, and how far over counts ──────────

def test_over_section_cap_is_ranked_with_the_owners_most_on_at_once():
    assert sr.BREACH_TIER["over_section_cap"] == sr.TIER_COVERAGE == sr.BREACH_TIER["over_role_max"]
    c = _c(section_cap=2)
    base = [_row("A", "5:00pm", "10:00pm"), _row("B", "5:00pm", "10:00pm")]
    short_over = base + [_row("C", "8:00pm", "9:00pm")]
    long_over = base + [_row("C", "6:00pm", "10:00pm")]
    p0, p1, p2 = (sr.breach_profile(r, c) for r in (base, short_over, long_over))
    key = ("over_section_cap", FRI, "")
    assert key not in p0["by_id"] and p2["by_id"][key] > p1["by_id"][key] > 0
    # a stage ranked below the floors sees a pass that makes it worse ...
    worse = sr.regressions(p1, p2, upto=sr.TIER_OVERTIME, hard_only=False)
    assert [w["id"] for w in worse] == [key]
    # ... and the top-up (a quality fill) is refused a server past the sections
    legal, refused = se._legal_fills(_c(section_cap=2, roster_roles={"C": "Server"}, roster_names=["C"],
                                        active={"c"}), base, [("C", _row("C", "6:00pm", "10:00pm"))], FRI,
                                     coverage=False)
    assert legal == [] and refused == {"C"}


# ── LOGIC-7: the cap's unit ───────────────────────────────────────────────

def test_the_section_cap_never_cuts_a_count_of_different_people():
    import schedule_requirements as req
    import staffing_curve as sc
    rows = req.shift_requirements([FRI], typical_headcount={("Friday", "night"): {"Server PM": 6}},
                                  section_cap=5, cap_roles=["server"])
    assert {x["role"]: x["required"] for x in rows[0]["roles"]} == {"Server PM": 6}
    # a job code carrying its daypart is still a server to the cap
    assert sc.capped_role("Server PM", ["server"]) and not sc.capped_role("Line Cook", ["server"])
    out, trimmed = sc.cap_requirement({"Server AM": 2, "Server PM": 5}, 5, ["server"])
    assert out["Server AM"] + out["Server PM"] == 5 and sum(trimmed.values()) == 2


# ── LOGIC-8: the setup's suggested floors ─────────────────────────────────

STAGGERED = [("Ann", "11:00am", "7:00pm"), ("Bo", "4:00pm", "11:00pm"), ("Cy", "4:30pm", "11:00pm"),
             ("Di", "5:00pm", "11:00pm"), ("Ed", "6:00pm", "11:00pm"), ("Flo", "6:30pm", "11:00pm")]


def _fridays(n=8):
    first = date(2026, 10, 16) - timedelta(weeks=n)
    return [(first + timedelta(weeks=i)).isoformat() for i in range(n)]


def test_suggested_floors_are_people_on_at_once_the_restaurants_own_nights_keep(monkeypatch):
    hist = []
    for k, d in enumerate(_fridays()):
        crew = STAGGERED if k % 2 else STAGGERED[:5]          # some Fridays one short of the rush
        hist += [{"date": d, "employee": e, "role": "Server", "shift_start": st, "shift_end": en}
                 for e, st, en in crew]
    monkeypatch.setattr(ss, "_history", lambda *a, **k: hist)
    c = _c(_fridays())
    out = ss.suggest_role_floors(1, c=c)
    night = out["floors"]["Server"]
    fri = night["days"].get("Friday", {}).get("night", night["night"])
    assert fri == 4 and out["unit"] == "on_at_once"          # never 6 (the different people)
    # every one of those Fridays keeps the suggested floor at every half hour
    c.role_floors = {"Server": {"morning": 0, "night": fri, "days": {}}}
    for d in _fridays():
        mine = [dict(r, day="Friday") for r in hist if r["date"] == d]
        assert not _floor_breaches(mine, c), d


# ── contract #2 and LOGIC-6 through the real baseline ────────────────────

def test_the_staffing_baseline_carries_people_on_at_once_and_binds_a_lean_answer(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"))
    shifts = [{"date": d, "employee": e, "role": "Server", "shift_start": st, "shift_end": en}
              for d in _fridays() for e, st, en in STAGGERED]
    base = labor.staffing_baseline(rid, shifts=shifts, before="2026-10-12")
    assert base["concurrent"][("Friday", "night")]["Server"]["held"] == 4
    assert base["concurrent_family"][("Friday", "night")]["server"]["peak"] == 6
    assert base["typical_headcount"][("Friday", "night")]["Server"] == 6
    models.update_restaurant(rid, {"crew_answers_json": json.dumps({"Server|Friday|night": {
        "answer": "lean", "unit": "concurrent", "usual": 4, "accepted": {"held": 3, "peak": 4, "people": 4},
        "at": "2026-10-10 12:00:00", "review": "2026-12-05"}})})
    base = labor.staffing_baseline(rid, shifts=shifts, before="2026-10-12")
    assert base["typical_headcount"][("Friday", "night")]["Server"] == 4
    assert base["crew_answers"][("Friday", "night")]["Server"]["was"] == 6


def test_floors_set_as_different_people_are_translated_once(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"))
    shifts = [{"date": d, "employee": e, "role": "Server PM", "shift_start": st, "shift_end": en}
              for d in _fridays() for e, st, en in STAGGERED]
    shifts += [{"date": d, "employee": e, "role": "Dishwasher", "shift_start": "4:00pm", "shift_end": "11:00pm"}
               for d in _fridays() for e in ("Dw1", "Dw2")]
    base = labor.historical_patterns(shifts)
    # Kept 6 (the old unit); a dishwasher floor of 2 the usual night already keeps.
    sr.save_role_floors(rid, {"Server PM": {"morning": 0, "night": 0, "days": {"Friday": {"night": 6}}},
                              "Dishwasher": {"morning": 0, "night": 2, "days": {}}})
    models.update_restaurant(rid, {"crew_answers_json": json.dumps({"Server PM|Friday|night": {
        "answer": "hold", "usual": 6, "floor_before": 0, "at": "2026-10-10 12:00:00"}})})
    plan = cm.floors_on_at_once(rid, baseline=base)
    assert [(x["role"], x["day"], x["old"], x["new"]) for x in plan["changes"]] == [("Server PM", "Friday", 6, 4)]
    assert not plan["applied"]
    assert sr.floor_for(sr.role_floors(models.get_restaurant(rid)), "Server PM", "Friday", "night") == 6
    plan = cm.floors_on_at_once(rid, baseline=base, apply=True)
    floors = sr.role_floors(models.get_restaurant(rid))
    assert plan["applied"] and sr.floor_for(floors, "Server PM", "Friday", "night") == 4
    assert sr.floor_for(floors, "Dishwasher", "Friday", "night") == 2           # never raised, kept as is
    held = json.loads(models.get_restaurant(rid).crew_answers_json)["Server PM|Friday|night"]
    assert held["unit"] == "concurrent" and held["floor_set"] == 4
    assert held["floor_before"] == {"present": False, "value": 0}
    # the Friday the restaurant runs keeps its translated floor
    c = _c(role_floors=floors)
    usual = [_row(e, st, en, "Server PM") for e, st, en in STAGGERED]
    usual += [_row(e, "4:00pm", "11:00pm", "Dishwasher") for e in ("Dw1", "Dw2")]
    assert not _floor_breaches(usual, c)
    # and running it again changes nothing
    assert cm.floors_on_at_once(rid, baseline=base)["changes"] == []
