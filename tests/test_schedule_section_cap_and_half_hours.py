"""#35 and #40 — the requirement every stage agrees on.

#35: the section count caps how many front-of-house people work at once.
The backstop and the repair loop held the draft to it, but the requirement
the model was given and the score judged against could ask for more — so a
restaurant whose history ran eight servers against six sections was scored
short on every such shift however the week was built. The requirement is
now held to the cap everywhere (staffing_curve.cap_requirement), all four
stages count the same front-of-house roles, and a history that runs over
the cap is told to the owner as a cap that is probably wrong.

#40: only the peak hour of each daypart carried a requirement. Each half
hour of service now does, from the measured hourly sales curve interpolated
to the half hour; the score judges it, the prompt carries it compactly, and
a restaurant with no intraday data is judged exactly as before.
"""
import inspect

import labor
import schedule_engine as se
import schedule_requirements as req
import shift_quality as sq
import staffing_curve as sc

SAT = "2026-10-10"


def _row(emp, role="Server", start="5:00pm", end="10:00pm", date=SAT):
    return {"date": date, "day": "Saturday", "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": ""}


def _dim(out, key):
    return next((d for d in out["shifts"][0]["dimensions"] if d["key"] == key), None)


# ── #35: the section cap ────────────────────────────────────────────────

def test_the_requirement_never_asks_for_more_than_the_section_cap():
    rows = [_row(f"S{i}") for i in range(6)]
    kw = dict(profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 8}})
    uncapped = _dim(sq.score_rows(rows, **kw), "coverage")
    assert uncapped["score"] == 75                        # 6 of the 8 the history runs
    capped = _dim(sq.score_rows(rows, section_cap=6, cap_roles=["server"], **kw), "coverage")
    assert capped["score"] == 100
    assert capped["facts"]["held_to_section_cap"] == {"cap": 6, "trimmed": {"Server": 2}}


def test_the_cap_counts_every_front_of_house_role_together():
    out, trimmed = sc.cap_requirement({"Server": 5, "Bartender": 3, "Cook": 4}, 6, {"server", "bartender"})
    assert out["Server"] + out["Bartender"] == 6 and out["Cook"] == 4
    assert out["Bartender"] >= 1 and sum(trimmed.values()) == 2
    # servers alone when nothing says otherwise; no cap, no change
    assert sc.cap_requirement({"Server": 9, "Bartender": 3}, 6, None)[0] == {"Server": 6, "Bartender": 3}
    assert sc.cap_requirement({"Server": 9}, 0, None) == ({"Server": 9}, {})


def test_the_requirements_table_the_model_reads_is_held_to_the_same_cap():
    rows = req.shift_requirements([SAT], typical_headcount={("Saturday", "night"): {"Server": 8, "Cook": 3}},
                                  section_cap=6, cap_roles=["server"])
    night = next(r for r in rows if r["daypart"] == "night")
    need = {x["role"]: x["required"] for x in night["roles"]}
    assert need == {"Server": 6, "Cook": 3}
    assert night["held_to_cap"]["cap"] == 6


def test_a_history_over_the_cap_is_told_to_the_owner():
    typical = {("Saturday", "night"): {"Server": 8}, ("Friday", "night"): {"Server": 7},
               ("Monday", "night"): {"Server": 4}}
    conf = sc.cap_conflicts(typical, 6, ["server"])
    assert [(c["day"], c["typical"]) for c in conf] == [("Saturday", 8), ("Friday", 7)]
    line = sc.cap_conflict_line(conf)
    assert "8 servers on Saturday dinner" in line and "section count is 6" in line
    assert "raise the section count" in line
    assert sc.cap_conflicts(typical, 8, ["server"]) == [] and sc.cap_conflict_line([]) == ""


def test_the_backstop_counts_the_same_front_of_house_roles():
    rows = [_row("S1"), _row("S2"), _row("B1", role="Bartender", start="6:00pm")]
    out, n, _dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2)
    assert n == 0                                         # servers alone: two at once, under the cap
    out, n, _dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2,
                                                 roles={"server", "bartender"})
    assert n == 1


def test_the_quality_signals_carry_the_cap_the_rules_hold(monkeypatch):
    import schedule_rules as sr
    c = sr.Constraints(restaurant_id=1, week_dates=[SAT], week_days=["Saturday"])
    c.compliance = dict(sr.DEFAULTS)
    c.section_cap, c.foh_roles = 6, {"server", "bartender"}
    sig, _w = se._quality_signals(1, {"constraints": c})
    assert sig["section_cap"] == 6 and sig["cap_roles"] == ["bartender", "server"]


def test_the_prompt_names_the_capped_roles_and_the_generator_passes_the_curve():
    params = inspect.signature(labor.generate_optimized_schedule).parameters
    assert {"hourly_profile", "section_cap_roles", "open_times", "close_times"} <= set(params)
    src = inspect.getsource(se._build_schedule_result)
    assert "hourly_profile=_safe_hourly_profile(restaurant_id)" in src
    assert "section_cap_roles=" in src


# ── #40: half-hour requirements from the sales curve ────────────────────

def test_the_hourly_curve_is_interpolated_to_the_half_hour():
    shares = sc.half_hour_shares({11: 1.0, 12: 3.0})
    assert shares == {11 * 60: 1.0, 11 * 60 + 30: 1.5, 12 * 60: 2.5, 12 * 60 + 30: 3.0}
    assert sc.half_hour_shares({}) == {}


def test_each_half_hour_of_service_needs_its_share_of_the_usual_crew():
    curve = {17: 0.1, 18: 0.3, 19: 0.4, 20: 0.2}
    needs = sc.half_hour_needs(curve, {"Server": 4}, 15 * 60, 24 * 60)
    assert 17 * 60 not in needs                           # well off the peak: the floors hold there
    assert needs[19 * 60] == {"Server": 3}                # the peak: 75% of the usual four
    assert needs[17 * 60 + 30] == {"Server": 2}
    assert max(n["Server"] for n in needs.values()) <= 3  # never more than the peak asks
    assert sc.half_hour_needs(curve, {}, 0, 1440) == {} and sc.half_hour_needs({}, {"Server": 4}, 0, 1440) == {}


def test_the_score_judges_the_climb_into_the_rush_not_only_the_peak():
    """One server on for the climb and four for the rush: the peak-hour check
    alone called that fully covered."""
    rows = [_row("A", start="5:00pm")] + [_row(n, start="6:30pm") for n in ("B", "C", "D")]
    kw = dict(profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 4}})
    curve = {"Saturday": {"17": 0.1, "18": 0.3, "19": 0.4, "20": 0.2}}
    out = _dim(sq.score_rows(rows, demand_curve=curve, **kw), "coverage_curve")
    assert out["score"] < 100
    gap = out["facts"]["gaps"]["Server"]
    assert gap["from_curve"] and gap["worst_at"] in ("5:30pm", "6:00pm")
    assert out["facts"]["half_hour_needs"]["Server"][0] == ["5:30pm", 2]
    assert "read to the half hour" in " ".join(out["weaknesses"])


def test_no_intraday_data_is_judged_exactly_as_before():
    rows = [_row("A", start="5:00pm")] + [_row(n, start="6:30pm") for n in ("B", "C", "D")]
    kw = dict(profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 4}})
    assert _dim(sq.score_rows(rows, **kw), "coverage_curve") is None
    floors = {"Server": {"night": 2}}
    out = _dim(sq.score_rows(rows, role_floors=floors, **kw), "coverage_curve")
    assert "half_hour_needs" not in out["facts"]
    assert out["facts"]["gaps"]["Server"]["from_curve"] is False


def test_the_half_hour_need_is_held_to_the_section_cap():
    rows = [_row(f"S{i}", start="5:00pm") for i in range(3)]
    kw = dict(profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 8}},
              demand_curve={"Saturday": {"17": 0.3, "18": 0.3, "19": 0.3}})
    over = _dim(sq.score_rows(rows, **kw), "coverage_curve")
    held = _dim(sq.score_rows(rows, section_cap=3, cap_roles=["server"], **kw), "coverage_curve")
    assert over["score"] < 100 and held["score"] == 100


def test_the_prompt_carries_the_half_hour_needs_compactly():
    curve = {"Saturday": {17: 0.1, 18: 0.3, 19: 0.4, 20: 0.2}}
    rows = req.shift_requirements([SAT], typical_headcount={("Saturday", "night"): {"Server": 4}},
                                  demand_curve=curve, close_times={"Saturday": "11:00pm"})
    night = next(r for r in rows if r["daypart"] == "night")
    assert night["half_hours"]["Server"][0] == (17 * 60 + 30, 2)
    block = req.requirements_block(rows)
    assert "by the half hour: Server 2 from 5:30pm, 3 from 6:30pm" in block
    assert "interpolated to the half hour" in block
    # no curve: the table and its wording are what they were
    plain = req.shift_requirements([SAT], typical_headcount={("Saturday", "night"): {"Server": 4}})
    assert "half_hours" not in plain[0]
    assert "by the half hour" not in req.requirements_block(plain)


def test_scorer_and_prompt_agree_on_every_half_hour():
    curve = {"Saturday": {17: 0.1, 18: 0.3, 19: 0.4, 20: 0.2}}
    typical = {("Saturday", "night"): {"Server": 4}}
    table = req.shift_requirements([SAT], typical_headcount=typical, demand_curve=curve)
    night = next(r for r in table if r["daypart"] == "night")
    rows = [_row(n, start="5:00pm", end="11:00pm") for n in ("A", "B", "C", "D")]
    out = _dim(sq.score_rows(rows, profiles=[sq.ShiftProfile()], typical_headcount=typical, demand_curve=curve),
               "coverage_curve")
    scored = [[sc._clock(m), n] for m, n in night["half_hours"]["Server"]]
    assert out["facts"]["half_hour_needs"]["Server"] == scored
