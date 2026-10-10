"""Workstream F (scheduler audit 10/10/26): Shift Quality as the owner's
report, the solver's objective separate (LOGIC-19).

  RM-3 / LOGIC-18  stability judged per slot against the last PUBLISHED
                   week, about a tenth of the week's weight
  LOGIC-30         demand match folded into operational strength (one rating
                   measure, one bar per demand level), its weight scaled
                   until enough people are rated
"""
import shift_quality as sq

SAT, FRI, LAST_SAT = "2026-10-17", "2026-10-16", "2026-10-10"


def row(d, name, role="Server", start="5:00pm", end="10:00pm"):
    return {"date": d, "day": sq._day_name(d), "employee": name, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": ""}


def dim(out, date, key):
    shift = next(s for s in out["shifts"] if s["date"] == date and s["daypart"] == "night")
    return next((d for d in shift.get("dimensions") or [] if d["key"] == key), None)


TYPICAL = {("Saturday", "night"): {"Server": 2}}


def test_ana_moved_off_her_published_slot_reads_as_change_even_on_a_day_she_usually_works():
    """The audit's case: a weekday and daypart she usually works scored 100
    whatever last week's slots were."""
    pattern = {"Ana": {"days": ["Saturday", "Friday"], "dayparts": ["night"]},
               "Ben": {"days": ["Saturday"], "dayparts": ["night"]}}
    last = [row(LAST_SAT, "Ana", start="5:00pm"), row(LAST_SAT, "Ben", start="5:00pm")]
    same = sq.score_rows([row(SAT, "Ana"), row(SAT, "Ben")], typical_headcount=TYPICAL, prior_pattern=pattern,
                         last_published_rows=last)
    moved = sq.score_rows([row(SAT, "Ana", start="6:30pm"), row(SAT, "Cy")], typical_headcount=TYPICAL,
                          prior_pattern=pattern, last_published_rows=last)
    assert dim(same, SAT, "stability")["score"] == 100
    st = dim(moved, SAT, "stability")
    # Ana an hour and a half later (a different slot), Cy new, Ben off it
    assert st["score"] == 0 and st["facts"]["basis"] == "last_published_week"
    assert set(st["facts"]["new"]) == {"Ana", "Cy"} and st["facts"]["off"] == ["ana", "ben"]
    # within 30 minutes is the same slot
    near = sq.score_rows([row(SAT, "Ana", start="5:30pm"), row(SAT, "Ben")], typical_headcount=TYPICAL,
                         last_published_rows=last)
    assert dim(near, SAT, "stability")["score"] == 100


def test_without_a_published_week_the_usual_pattern_is_still_the_reference():
    out = sq.score_rows([row(SAT, "Ana")], typical_headcount={("Saturday", "night"): {"Server": 1}},
                        prior_pattern={"Ana": {"days": ["Friday"], "dayparts": ["night"]}})
    st = dim(out, SAT, "stability")
    assert st["score"] == 0 and st["facts"]["basis"] == "usual_pattern"


def test_stability_is_about_a_tenth_of_the_weight_and_moves_the_week():
    w = sq.DEFAULT_WEIGHTS
    assert 0.09 <= w["stability"] / float(sum(w.values())) <= 0.12
    last = [row(LAST_SAT, "Ana"), row(LAST_SAT, "Ben")]
    same = sq.score_rows([row(SAT, "Ana"), row(SAT, "Ben")], typical_headcount=TYPICAL, last_published_rows=last)
    churned = sq.score_rows([row(SAT, "Cy"), row(SAT, "Dee")], typical_headcount=TYPICAL, last_published_rows=last)
    assert same["score"] - churned["score"] >= 8


ROSTER = {n: "Bartender" for n in "ABCD"}
PEAK = [sq.ShiftProfile(key="busy", label="Busy night", demand="peak", source="restaurant",
                        min_strength={"Bartender": 6})]


def test_demand_match_is_one_bar_inside_operational_strength():
    out = sq.score_rows([row(SAT, "A", "Bartender"), row(SAT, "B", "Bartender")], profiles=PEAK,
                        scores={"A": 3, "B": 3, "C": 5, "D": 5}, roster_roles=ROSTER)
    assert dim(out, SAT, "demand_match") is None
    st = dim(out, SAT, "operational_strength")
    # the target's bar is 6 / 2 = 3 (met); the busy-night bar from the team's
    # own ratings is 5 — the stronger of the two judges, 2 points under it
    assert st["score"] == 50
    assert st["facts"]["shortfalls"][0]["basis"] == "demand" and st["facts"]["shortfalls"][0]["target_bar"] == 3
    # the busy-night bar marks down; only the owner's target caps
    shift = next(s for s in out["shifts"] if s["date"] == SAT)
    assert shift["capped_by"] != "operational_strength"


def test_a_strong_team_is_not_counted_twice():
    """Strength 100 and demand 100 used to add 28 points of weight for one
    fact; now one dimension at one weight."""
    out = sq.score_rows([row(SAT, "C", "Bartender"), row(SAT, "D", "Bartender")], profiles=PEAK,
                        scores={"A": 3, "B": 3, "C": 5, "D": 5}, roster_roles=ROSTER)
    keys = [d["key"] for d in next(s for s in out["shifts"] if s["date"] == SAT)["dimensions"]]
    assert keys.count("operational_strength") == 1 and "demand_match" not in keys


def test_one_rating_on_a_big_roster_counts_for_little():
    roster = {f"P{i}": "Bartender" for i in range(20)}
    roster["A"] = "Bartender"
    rows = [row(SAT, "A", "Bartender"), row(SAT, "P1", "Bartender")]
    out = sq.score_rows(rows, profiles=PEAK, scores={"A": 1}, roster_roles=roster)
    st = dim(out, SAT, "operational_strength")
    assert st["facts"]["weight_share"] < 0.2
    assert st["weight"] < sq.DEFAULT_WEIGHTS["operational_strength"] * 0.2
