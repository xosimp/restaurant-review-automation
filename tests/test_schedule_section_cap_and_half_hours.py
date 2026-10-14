"""#35 and #40 — the requirement every stage agrees on.

#35: the section count caps how many front-of-house people work at once.
The backstop and the repair loop held the draft to it, but the requirement
the model was given and the score judged against could ask for more — so a
restaurant whose history ran eight servers against six sections was scored
short on every such shift however the week was built. The requirement is
held to the cap in the cap's own unit — people on at once, at every half
hour (staffing_curve.cap_requirement); the daypart's count of different
people is not (schedule audit 10/10/26 LOGIC-7: staggered starts fit more
people than sections). All four stages count the same front-of-house roles
(by family), and a history that runs more on at once than the cap is told
to the owner as a cap that is probably wrong.

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

def test_the_daypart_crew_is_not_cut_to_the_section_cap():
    # Schedule audit 10/10/26 LOGIC-7 (this test used to assert the cut): the
    # section count is people ON AT ONCE; the daypart crew is different
    # people across the shift. Eight staggered servers fit six sections, so
    # six servers all 5-10pm are still two short of the eight the history
    # runs — the cap judges them at the half hour, not here.
    rows = [_row(f"S{i}") for i in range(6)]
    kw = dict(profiles=[sq.ShiftProfile()], typical_headcount={("Saturday", "night"): {"Server": 8}})
    uncapped = _dim(sq.score_rows(rows, **kw), "coverage")
    assert uncapped["score"] == 75                        # 6 of the 8 the history runs
    capped = _dim(sq.score_rows(rows, section_cap=6, cap_roles=["server"], **kw), "coverage")
    assert capped["score"] == 75
    assert "held_to_section_cap" not in capped["facts"]


def test_the_cap_counts_every_front_of_house_role_together():
    out, trimmed = sc.cap_requirement({"Server": 5, "Bartender": 3, "Cook": 4}, 6, {"server", "bartender"})
    assert out["Server"] + out["Bartender"] == 6 and out["Cook"] == 4
    assert out["Bartender"] >= 1 and sum(trimmed.values()) == 2
    # servers alone when nothing says otherwise; no cap, no change
    assert sc.cap_requirement({"Server": 9, "Bartender": 3}, 6, None)[0] == {"Server": 6, "Bartender": 3}
    assert sc.cap_requirement({"Server": 9}, 0, None) == ({"Server": 9}, {})


def test_the_requirements_table_holds_only_the_half_hour_need_to_the_cap():
    # LOGIC-7: the whole-shift number (different people) is the history's;
    # every half-hour need (people on at once) stays under the cap.
    curve = {"Saturday": {17: 0.05, 18: 0.2, 19: 0.6, 20: 0.15}}
    rows = req.shift_requirements([SAT], typical_headcount={("Saturday", "night"): {"Server AM": 1, "Server PM": 12,
                                                                                       "Cook": 3}},
                                  section_cap=6, cap_roles=["server"], demand_curve=curve)
    night = next(r for r in rows if r["daypart"] == "night")
    need = {x["role"]: x["required"] for x in night["roles"]}
    assert need == {"Server PM": 12, "Server AM": 1, "Cook": 3}
    assert night["held_to_cap"]["cap"] == 6 and night["held_to_cap"]["unit"] == "on_at_once"
    # Server AM and Server PM are servers to the cap (by family): together
    # never more than six on at once at any half hour.
    at = {}
    for role, pts in night["half_hours"].items():
        if role.startswith("Server"):
            for m, n in pts:
                at[m] = at.get(m, 0) + n
    assert at and max(at.values()) <= 6


def test_a_history_over_the_cap_is_told_to_the_owner():
    # People on at once (labor.concurrent_patterns), the capped roles added
    # at each half hour.
    def fig(slots):
        return {"held": min(slots.values()), "peak": max(slots.values()), "slots": slots}
    concurrent = {("Saturday", "night"): {"Server AM": fig({1080: 2, 1110: 1}), "Server PM": fig({1080: 6, 1110: 7})},
                  ("Friday", "night"): {"Server PM": fig({1080: 7, 1110: 7})},
                  ("Monday", "night"): {"Server PM": fig({1080: 4, 1110: 4})}}
    conf = sc.cap_conflicts(concurrent, 6, ["server"])
    assert [(c["day"], c["typical"]) for c in conf] == [("Saturday", 8), ("Friday", 7)]
    line = sc.cap_conflict_line(conf)
    assert "8 servers on at once on Saturday dinner" in line and "section count is 6" in line
    assert "raise the section count" in line
    assert sc.cap_conflicts(concurrent, 8, ["server"]) == [] and sc.cap_conflict_line([]) == ""
    # Six different servers who are never more than five on at once are no
    # conflict with five sections (the old count said they were).
    staggered = {("Friday", "night"): {"Server": fig({1050: 4, 1080: 5, 1110: 5, 1140: 5})}}
    assert sc.cap_conflicts(staggered, 5, ["server"]) == []


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
    src = "".join(inspect.getsource(f) for f in se.INPUT_STEPS)
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
    # 3 x its share of the peak is 1.1 people: off the peak a fraction rounds
    # at the newsvendor fractile, not up (blind re-audit 10/11/26 LOGIC-16)
    assert needs[17 * 60 + 30] == {"Server": 1}
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
    assert out["facts"]["half_hour_needs"]["Server"][0] == ["5:30pm", 1]       # rounded, not ceiled (LOGIC-16)
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
    assert night["half_hours"]["Server"][0] == (17 * 60 + 30, 1)            # rounded, not ceiled (LOGIC-16)
    block = req.requirements_block(rows)
    assert "by the half hour: Server 1 from 5:30pm, 2 from 6:00pm, 3 from 6:30pm" in block   # LOGIC-16
    # What "by the half hour" means is said once, in the standing
    # instructions every call shares (C1, PR-26).
    import schedule_prompt
    assert "\"by the half hour\" in SHIFT REQUIREMENTS is how many of a role" in schedule_prompt.static_block()
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
