"""The Shift Quality scorer's caps, leadership, strength and coverage —
the schedule audit of 10/3/26, findings SQ-1 to SQ-32 owned by fix
workstream D1a.

Erik's week read "Monday dinner 25 / 70" and "Tuesday dinner 0 / 70".
Both numbers were leadership caps whose size depended on how many leader
rules happened to apply (SQ-1), on rules applied to dayparts the role does
not work (SQ-2), on managers not counting as leaders (SQ-13) — and an empty
daypart said nothing about why (SQ-8). Each test below names the finding it
pins and fails on the code the audit read.
"""
import sys

import pytest

import schedule_optimizer as so
import schedule_requirements as req
import shift_quality as sq

MON, TUE, WED, THU = "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10"
FRI, SAT, SUN = "2026-09-11", "2026-09-12", "2026-09-13"
STD = [sq.ShiftProfile(key="std", label="Standard shift", source="restaurant")]


def row(date, name, role, start="5:00pm", end="11:00pm", hours=6):
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": hours, "notes": ""}


def lunch(date, name, role, hours=4):
    return row(date, name, role, start="11:00am", end="3:00pm", hours=hours)


def shift_of(out, date, part="night"):
    return next(s for s in out["shifts"] if s["date"] == date and s["daypart"] == part)


def dim(shift, key):
    return next((d for d in shift.get("dimensions") or [] if d["key"] == key), None)


# ── SQ-1: partial credit per rule, a fixed cap, two kinds of miss ──────────

def _monday_dinner(rules, scores=None, rows=None):
    rows = rows or [row(MON, "Sam", "Bartender"), row(MON, "Dana", "Server"), row(MON, "Lee", "Server"),
                    row(MON, "Jo", "Cook"), row(MON, "Kim", "Host")]
    return sq.score_rows(rows, profiles=STD,
                         scores=scores or {"Sam": 4, "Dana": 4, "Lee": 4, "Jo": 4, "Kim": 4},
                         typical_headcount={("Monday", "night"): {"Bartender": 1, "Server": 2, "Cook": 1, "Host": 1}},
                         leader_rules=rules)


BAR5 = {"role": "Bartender", "days": ["Monday"], "daypart": "night", "min_score": 5}


def test_one_missing_leader_costs_the_same_whatever_the_rule_count():
    """One missed rule used to cap the shift at 0 (Erik's "Tuesday dinner
    0 / 70"), the same miss among four rules capped it at 75 or 25 (his
    "Monday dinner 25 / 70"), and among three it cost nothing. A missing
    leader now holds the shift at one fixed level."""
    alone = shift_of(_monday_dinner([BAR5]), MON)
    among_four = shift_of(_monday_dinner([BAR5,
                                          {"role": "Server", "daypart": "night", "min_score": 4},
                                          {"role": "Cook", "daypart": "night", "min_score": 4},
                                          {"role": "Host", "daypart": "night", "count": 1}]), MON)
    for shift in (alone, among_four):
        assert dim(shift, "leadership")["score"] == sq.LEADER_MISS_SCORE, dim(shift, "leadership")
        assert shift["capped_by"] == "leadership" and shift["score"] == sq.LEADER_MISS_SCORE
        assert "Needs 1 bartender scoring 5 or above, found 0" in shift["held_by"]["text"]


def test_eriks_monday_four_rules_one_met_no_longer_reads_25():
    rules = [BAR5, {"role": "Server", "daypart": "night", "min_score": 5},
             {"role": "Cook", "daypart": "night", "min_score": 5},
             {"role": "Host", "daypart": "night", "count": 1}]
    shift = shift_of(_monday_dinner(rules), MON)
    assert shift["score"] == sq.LEADER_MISS_SCORE, shift["score"]
    # Every miss is still named, not only the first.
    assert len(dim(shift, "leadership")["facts"]["misses"]) == 3


def test_a_rule_half_met_earns_half_the_credit_and_is_not_a_cap():
    rules = [{"role": "Server", "daypart": "night", "count": 2, "min_score": 5}]
    shift = shift_of(_monday_dinner(rules, scores={"Sam": 4, "Dana": 5, "Lee": 4, "Jo": 4, "Kim": 4}), MON)
    lead = dim(shift, "leadership")
    assert lead["score"] == 75, lead
    assert shift["capped_by"] != "leadership"
    miss = lead["facts"]["misses"][0]
    assert (miss["found"], miss["count"], miss["why"]) == (1, 2, "short")


def test_nobody_in_the_role_is_told_apart_from_nobody_qualified():
    empty_bar = [row(MON, "Dana", "Server"), row(MON, "Lee", "Server")]
    out = sq.score_rows(empty_bar, profiles=STD, scores={"Dana": 4, "Lee": 4},
                        typical_headcount={("Monday", "night"): {"Bartender": 1, "Server": 2}}, leader_rules=[BAR5])
    lead = dim(shift_of(out, MON), "leadership")
    assert lead["facts"]["misses"][0]["why"] == "nobody_on"
    assert lead["weaknesses"][0].startswith("No bartender on this shift"), lead["weaknesses"]

    weak_bar = empty_bar + [row(MON, "Sam", "Bartender PM")]
    out = sq.score_rows(weak_bar, profiles=STD, scores={"Dana": 4, "Lee": 4, "Sam": 3},
                        typical_headcount={("Monday", "night"): {"Bartender": 1, "Server": 2}}, leader_rules=[BAR5])
    lead = dim(shift_of(out, MON), "leadership")
    assert lead["facts"]["misses"][0]["why"] == "not_qualified"
    # The job codes the role's people work under, for a pass that swaps one.
    assert lead["facts"]["misses"][0]["roles"] == ["Bartender PM"]
    assert lead["weaknesses"][0].startswith("Needs 1 bartender scoring 5 or above, found 0. On this shift: Sam (3)")


# ── SQ-2: a rule binds only the dayparts its role works ────────────────────

SAT_TYPICAL = {("Saturday", "morning"): {"Server": 2, "Cook": 1},
               ("Saturday", "night"): {"Bartender": 2, "Server": 3, "Cook": 2}}


def _saturday_rows():
    rows = [lunch(SAT, "Ann", "Server"), lunch(SAT, "Ben", "Server"), lunch(SAT, "Cal", "Cook")]
    rows += [row(SAT, "Sam", "Bartender"), row(SAT, "Tom", "Bartender"), row(SAT, "Dee", "Server"),
             row(SAT, "Eve", "Server"), row(SAT, "Fay", "Server"), row(SAT, "Gus", "Cook"), row(SAT, "Hal", "Cook")]
    return rows


def _everyone(rows, score=4):
    return {r["employee"]: score for r in rows}


def test_a_rule_with_no_daypart_is_not_applied_to_a_lunch_the_bar_does_not_work():
    rule = {"role": "Bartender", "days": ["Saturday"], "min_score": 5}
    rows = _saturday_rows()
    out = sq.score_rows(rows, profiles=STD, scores=_everyone(rows), typical_headcount=SAT_TYPICAL,
                        leader_rules=[rule])
    sat_lunch, sat_dinner = shift_of(out, SAT, "morning"), shift_of(out, SAT, "night")
    assert "leadership" in sat_lunch["not_applicable"]
    assert sat_lunch["score"] == 100 and sat_lunch["capped_by"] is None
    assert dim(sat_dinner, "leadership")["facts"]["misses"], "dinner, where the bar works, is still judged"


def test_a_rule_the_owner_scoped_to_a_daypart_binds_it_wherever_the_role_works():
    rule = {"role": "Bartender", "days": ["Saturday"], "daypart": "morning", "min_score": 5}
    rows = _saturday_rows()
    out = sq.score_rows(rows, profiles=STD, scores=_everyone(rows), typical_headcount=SAT_TYPICAL,
                        leader_rules=[rule])
    miss = dim(shift_of(out, SAT, "morning"), "leadership")["facts"]["misses"][0]
    assert miss["why"] == "nobody_on"


def test_with_nothing_on_file_about_dayparts_a_rule_binds_every_daypart_as_before():
    rule = {"role": "Bartender", "days": ["Saturday"], "min_score": 5}
    rows = _saturday_rows()
    out = sq.score_rows(rows, profiles=STD, scores=_everyone(rows), leader_rules=[rule])
    assert dim(shift_of(out, SAT, "morning"), "leadership") is not None


def test_the_requirements_table_asks_for_the_leader_only_where_the_role_works():
    """The prompt's table told the model to add a bartender at lunch."""
    rule = {"role": "Bartender", "days": ["Saturday"], "count": 1, "min_score": 5}
    table = req.shift_requirements([SAT], typical_headcount=SAT_TYPICAL, leader_rules=[rule])
    by_part = {r["daypart"]: r for r in table}
    assert by_part["morning"]["leader"] == []
    assert by_part["night"]["leader"] == ["1 Bartender scoring 5+"]


def test_am_pm_job_codes_answer_a_rule_for_their_role():
    """"AM/PM job codes are dayparts of one role" (owner): a Bartender rule
    is answered by whoever works the bar, whichever code they punch under."""
    rows = [row(SAT, "Sam", "Bartender PM"), row(SAT, "Tom", "Bartender AM", start="11:00am", end="7:00pm", hours=8)]
    out = sq.score_rows(rows, profiles=STD, scores={"Sam": 5, "Tom": 3},
                        typical_headcount={("Saturday", "night"): {"Bartender PM": 2}},
                        leader_rules=[{"role": "Bartender", "days": ["Saturday"], "daypart": "night", "min_score": 5}])
    assert dim(shift_of(out, SAT), "leadership")["score"] == 100


def test_a_rule_or_target_on_an_am_or_pm_job_keeps_to_its_half_of_the_day():
    rows = [lunch(SAT, "Ann", "Host AM"), row(SAT, "Bea", "Host PM")]
    rule = {"role": "Host AM", "days": ["Saturday"], "min_score": 5}
    out = sq.score_rows(rows, profiles=STD, scores={"Ann": 5, "Bea": 3}, leader_rules=[rule],
                        role_minimums={"Host": 1})
    assert dim(shift_of(out, SAT, "morning"), "leadership")["score"] == 100
    assert dim(shift_of(out, SAT), "leadership") is None
    prof = [sq.ShiftProfile(key="std", min_strength={"Host PM": 4}, source="restaurant")]
    out = sq.score_rows(rows, profiles=prof, scores={"Ann": 2, "Bea": 3}, role_minimums={"Host": 1})
    assert dim(shift_of(out, SAT, "morning"), "operational_strength") is None
    assert dim(shift_of(out, SAT), "operational_strength")["facts"]["shortfalls"][0]["strength"] == 3


# ── SQ-3 / SQ-4 / SQ-5: strength per person, over the rated share ─────────

SERVER8 = [sq.ShiftProfile(key="std", min_strength={"Server": 8}, source="restaurant")]


def test_an_unrated_person_is_unknown_not_a_zero():
    """Server target 8 for two, one rated 4 and one unrated: 4/8 capped the
    shift at 50; the prompt says unrated is unknown."""
    rows = [row(SAT, "Dana", "Server"), row(SAT, "Ghost", "Server")]
    out = sq.score_rows(rows, profiles=SERVER8, scores={"Dana": 4},
                        typical_headcount={("Saturday", "night"): {"Server": 2}})
    shift = shift_of(out, SAT)
    strength = dim(shift, "operational_strength")
    assert strength["score"] == 100 and shift["capped_by"] is None, strength
    assert any("Ghost" in b and "rated people only" in b for b in shift["blind_spots"]), shift["blind_spots"]


def test_the_rated_share_is_still_judged():
    rows = [row(SAT, "Dana", "Server"), row(SAT, "Ghost", "Server")]
    out = sq.score_rows(rows, profiles=SERVER8, scores={"Dana": 2},
                        typical_headcount={("Saturday", "night"): {"Server": 2}})
    shift = shift_of(out, SAT)
    assert dim(shift, "operational_strength")["score"] == 50
    assert shift["capped_by"] == "operational_strength"


def test_a_quiet_lunch_is_held_to_the_same_per_person_bar_not_the_busiest_crews_total():
    """Bartender 8 is sized for Saturday's two bartenders. One bartender
    rated 4 at a Monday lunch that needs one read 4/8 = 50 and was capped."""
    profiles = sq.profiles_from_config(None, default_strength={"Bartender": 8})
    out = sq.score_rows([row(MON, "Sam", "Bartender", start="11:00am", end="4:00pm", hours=5)],
                        profiles=profiles, scores={"Sam": 4},
                        typical_headcount={("Monday", "morning"): {"Bartender": 1},
                                           ("Saturday", "night"): {"Bartender": 2}})
    shift = shift_of(out, MON, "morning")
    strength = dim(shift, "operational_strength")
    assert strength["score"] == 100, strength
    met = strength["facts"]["met"][0]
    assert (met["bar"], met["crew"], met["target"]) == (4.0, 2, 8.0)


def test_adding_a_body_never_buys_strength():
    """The prompt says strength is about WHO works, never adding people."""
    prof = [sq.ShiftProfile(key="std", min_strength={"Bartender": 8}, source="restaurant")]
    typical = {("Saturday", "night"): {"Bartender": 2}}
    two = [row(SAT, "Sam", "Bartender"), row(SAT, "Alex", "Bartender")]
    three = two + [row(SAT, "Pat", "Bartender")]
    scores = {"Sam": 3, "Alex": 3, "Pat": 3}
    a = dim(shift_of(sq.score_rows(two, profiles=prof, scores=scores, typical_headcount=typical), SAT),
            "operational_strength")["score"]
    b = dim(shift_of(sq.score_rows(three, profiles=prof, scores=scores, typical_headcount=typical), SAT),
            "operational_strength")["score"]
    assert a == b == 75, (a, b)


def test_the_optimizer_never_adds_a_person_to_fix_strength():
    rows = [row(SAT, "Sam", "Bartender"), row(SAT, "Alex", "Bartender"), row(FRI, "Cat", "Bartender")]
    signals = {"roster": ["Sam", "Alex", "Cat"], "scores": {"Sam": 2, "Alex": 2, "Cat": 5},
               "typical_headcount": {("Saturday", "night"): {"Bartender": 2}}, "availability": {}, "constraints": {}}
    prof = [sq.ShiftProfile(key="std", min_strength={"Bartender": 8}, source="restaurant")]
    quality = sq.score_rows(rows, profiles=prof, **signals)
    sat = shift_of(quality, SAT)
    problem = (100.0, sat, dim(sat, "operational_strength"))
    state = so._State([dict(r) for r in rows], signals, {"roster_roles": {"Sam": "Bartender", "Alex": "Bartender",
                                                                          "Cat": "Bartender"}})
    moves = so._moves_for(problem, state)
    assert moves, "replacing a weak bartender is still offered"
    assert not [m for m in moves if m[0][0] == "add"], [m[1] for m in moves]


# ── SQ-6: floors over the core service window, one definition ─────────────

def test_a_staggered_cut_holds_the_floor_through_service():
    """Floor of 2 servers at dinner; A works 4-11pm, B 5-9pm, close 11pm.
    The hard rule passed and the half-hour sweep capped the shift at 57."""
    rows = [row(SAT, "A", "Server", start="4:00pm", end="11:00pm", hours=7),
            row(SAT, "B", "Server", start="5:00pm", end="9:00pm", hours=4)]
    out = sq.score_rows(rows, profiles=STD, role_floors={"Server": {"night": 2}},
                        open_times={"Saturday": "11:00am"}, close_times={"Saturday": "11:00pm"})
    curve = dim(shift_of(out, SAT), "coverage_curve")
    assert curve["score"] == 100 and shift_of(out, SAT)["capped_by"] is None, curve
    lo, hi = sq.floor_window("night", 11 * 60, 23 * 60)
    assert (lo, hi) == sq.CORE_WINDOWS["night"]
    assert curve["facts"]["floor_window"] == [sq._fmt_minutes(lo), sq._fmt_minutes(hi)]


def test_the_shared_floor_check_is_the_one_the_hard_rule_calls():
    held = [row(SAT, "A", "Server", start="4:00pm", end="11:00pm"), row(SAT, "B", "Server", start="5:00pm", end="9:00pm")]
    assert sq.floor_shortfall(held, "Server", 2, "night", 11 * 60, 23 * 60)["held"]
    split = [row(SAT, "A", "Server", start="5:30pm", end="6:30pm"), row(SAT, "B", "Server", start="7:30pm", end="8:30pm")]
    short = sq.floor_shortfall(split, "Server", 2, "night", 11 * 60, 23 * 60)
    assert not short["held"] and short["short_minutes"] == 180 and short["on_at_worst"] == 0


def test_a_floor_window_is_clipped_to_the_hours_the_restaurant_is_open():
    assert sq.floor_window("night", 11 * 60, 20 * 60) == (17 * 60 + 30, 20 * 60)
    assert sq.floor_window("morning", 11 * 60 + 30, 22 * 60) == (11 * 60 + 30, 14 * 60 + 30)
    # A 2am close is after midnight, never "before" dinner.
    assert sq.floor_window("night", 16 * 60, 2 * 60) == sq.CORE_WINDOWS["night"]


# ── SQ-8: an empty daypart says "no shift written" ─────────────────────────

def test_an_unwritten_daypart_says_so_on_its_cap_and_below_the_bar():
    typical = {("Tuesday", "morning"): {"Server": 1}, ("Tuesday", "night"): {"Server": 2, "Cook": 1}}
    out = sq.score_rows([lunch(TUE, "Ann", "Server")], profiles=STD, typical_headcount=typical)
    night = shift_of(out, TUE)
    assert night["score"] == 0 and night["no_shift_written"] is True
    assert night["held_by"]["text"].startswith("No shift written for Tuesday dinner"), night["held_by"]
    line = next(b for b in out["below_profile"] if b["date"] == TUE and b["daypart"] == "night")
    assert line["no_shift_written"] is True and line["reason"].startswith("No shift written")


# ── SQ-12: demand match against the restaurant's own ratings ───────────────

PEAK = [sq.ShiftProfile(key="busy", label="Busy night", demand="peak", source="restaurant")]
BAR_ROSTER = {"A": "Bartender", "B": "Bartender", "C": "Bartender", "D": "Bartender"}


def _peak_match(scores):
    out = sq.score_rows([row(SAT, "A", "Bartender"), row(SAT, "B", "Bartender")], profiles=PEAK,
                        scores=scores, roster_roles=BAR_ROSTER)
    return dim(shift_of(out, SAT), "demand_match")


def test_an_average_team_can_score_full_marks_on_a_busy_night_when_it_is_the_team_there_is():
    """A peak bar of 4.0 on a scale where 3 is average: a restaurant whose
    people are all rated 3 could never score 100 on its busiest night."""
    assert _peak_match({"A": 3, "B": 3, "C": 3, "D": 3})["score"] == 100


def test_a_busy_night_staffed_below_the_restaurants_own_level_is_marked_down():
    match = _peak_match({"A": 3, "B": 3, "C": 5, "D": 5})
    assert match["score"] == 50, match
    assert "averages 4" in match["weaknesses"][0], match["weaknesses"]


def test_rating_everybody_a_point_higher_changes_nothing():
    """Fixed bars rewarded inflating ratings; a bar read from the
    restaurant's own ratings moves with them."""
    assert _peak_match({"A": 2, "B": 2, "C": 4, "D": 4})["score"] == \
        _peak_match({"A": 3, "B": 3, "C": 5, "D": 5})["score"]


# ── SQ-13: managers count as leaders ───────────────────────────────────────

def _saturday_with_owner(**kw):
    rows = [row(SAT, "Erik", "Owner"), row(SAT, "Sam", "Bartender"), row(SAT, "Dana", "Server")]
    return dim(shift_of(sq.score_rows(rows, profiles=None, **kw), SAT), "leadership")


def test_a_manager_on_the_floor_runs_the_shift_the_built_in_profile_asks_for():
    assert _saturday_with_owner(scores={"Sam": 3, "Dana": 3}, managers={"erik": "Owner"})["score"] == 100
    assert _saturday_with_owner(scores={"Sam": 3, "Dana": 3})["score"] == sq.LEADER_MISS_SCORE


def test_an_acting_manager_counts_on_their_own_dates_only():
    assert _saturday_with_owner(scores={"Sam": 3, "Dana": 3}, acting_managers={"sam": {SAT}})["score"] == 100
    assert _saturday_with_owner(scores={"Sam": 3, "Dana": 3}, acting_managers={"sam": {FRI}})["score"] == \
        sq.LEADER_MISS_SCORE


def test_with_nobody_rated_a_known_manager_still_answers_the_question():
    lead = _saturday_with_owner(managers={"erik": "Owner"})
    assert lead is not None and lead["score"] == 100


# ── SQ-14: a hard breach caps the shift ────────────────────────────────────

def _clean_saturday(**kw):
    rows = [lunch(SAT, "Lu", "Server"), row(SAT, "Ann", "Server"), row(SAT, "Bob", "Server")]
    return sq.score_rows(rows, profiles=STD, scores={"Lu": 4, "Ann": 4, "Bob": 4},
                         typical_headcount={("Saturday", "morning"): {"Server": 1},
                                            ("Saturday", "night"): {"Server": 2}}, **kw)


def test_a_manager_gap_never_reads_excellent():
    gap = {"kind": "no_manager", "date": SAT, "gap_start": 17 * 60, "gap_end": 18 * 60, "hard": True,
           "day_level": True, "detail": "no manager on Saturday from 5:00pm to 6:00pm"}
    clean, broken = _clean_saturday(), _clean_saturday(hard_breaches=[gap])
    assert clean["score"] == 100
    night = shift_of(broken, SAT)
    assert night["score"] == sq.HARD_BREACH_CAP and night["capped_by"] == "hard_rules"
    assert "no manager on Saturday from 5:00pm to 6:00pm" in night["held_by"]["text"]
    assert shift_of(broken, SAT, "morning")["score"] == 100, "the gap is at dinner"
    assert broken["band"] != "excellent"


def test_a_breach_lands_on_the_shift_it_is_about():
    floor = {"kind": "coverage_floor", "date": SAT, "daypart": "morning", "hard": True,
             "detail": "0 Host on for lunch/day Saturday, your floor is 1"}
    out = _clean_saturday(hard_breaches=[floor])
    assert shift_of(out, SAT, "morning")["capped_by"] == "hard_rules"
    assert shift_of(out, SAT)["capped_by"] is None
    rest = {"kind": "rest_gap", "date": SAT, "employee": "Ann", "shift_start": "5:00pm", "hard": True,
            "detail": "8.0h since their previous shift, the rule is 10h"}
    out = _clean_saturday(hard_breaches=[rest])
    assert shift_of(out, SAT)["capped_by"] == "hard_rules"
    assert shift_of(out, SAT, "morning")["capped_by"] is None


def test_hours_over_for_the_week_land_on_the_shift_that_tips_it_not_every_shift():
    """over_max_hours names every row of the person's payroll week; capping
    each would hold the whole week at 50 for one person's extra hours."""
    rows = [row(d, "Ann", "Server") for d in (THU, FRI, SAT)] + [row(d, "Bob", "Server") for d in (THU, FRI, SAT)]
    viols = [{"kind": "over_max_hours", "date": d, "employee": "Ann", "shift_start": "5:00pm", "hard": True,
              "bucket": THU, "detail": "48h in the payroll week — over 40h"} for d in (THU, FRI, SAT)]
    out = sq.score_rows(rows, profiles=STD, scores={"Ann": 4, "Bob": 4},
                        typical_headcount={(day, "night"): {"Server": 2} for day in ("Thursday", "Friday", "Saturday")},
                        hard_breaches=viols)
    capped = [s["day"] for s in out["shifts"] if s["capped_by"] == "hard_rules"]
    assert capped == ["Saturday"], capped


def test_a_breach_found_on_other_rows_does_not_cap_these():
    """A pass's trial moved the row the breach was about: a stale cap would
    hide what the move fixed."""
    gone = {"kind": "rest_gap", "date": SAT, "employee": "Zed", "shift_start": "5:00pm", "hard": True,
            "detail": "8.0h since their previous shift, the rule is 10h"}
    assert _clean_saturday(hard_breaches=[gone])["score"] == 100


def test_a_breach_id_tuple_lands_as_its_violation_would():
    out = _clean_saturday(hard_breaches=[("coverage_floor", SAT, "host", "morning")])
    assert shift_of(out, SAT, "morning")["capped_by"] == "hard_rules" and shift_of(out, SAT)["capped_by"] is None
    out = _clean_saturday(hard_breaches=[("no_manager", SAT)])
    assert shift_of(out, SAT, "morning")["capped_by"] == shift_of(out, SAT)["capped_by"] == "hard_rules"


def test_the_engines_breach_map_lands_and_holds_where_a_pass_changed_the_rows():
    """schedule_engine.hard_breach_map's shape: breaches by date with their
    dayparts. Scored on rows a pass changed (the what-if's swaps), a day's
    breach still holds — dropping it credited any change on a capped day
    with lifting the cap — and a person's breach goes with the person."""
    gap = {"id": ["no_manager", SAT], "kind": "no_manager", "label": "no manager on the floor",
           "detail": "no manager on Saturday from 5:00pm to 6:00pm", "employee": None, "dayparts": ["night"],
           "day_level": True, "no_show": False, "minutes": 60}
    rest = {"id": ["rest_gap", SAT, "ann"], "kind": "rest_gap", "employee": "Ann", "dayparts": ["night"],
            "detail": "8.0h since their previous shift, the rule is 10h", "day_level": False}
    swept = [lunch(SAT, "Lu", "Server"), row(SAT, "Ann", "Server"), row(SAT, "Bob", "Server")]
    m = {"by_date": {SAT: [gap]}, "week": [], "rows_sig": {SAT: sq.LocalScorer._signature(swept)}}
    night = shift_of(_clean_saturday(hard_breaches=m), SAT)
    assert night["capped_by"] == "hard_rules" and "5:00pm to 6:00pm" in night["held_by"]["text"]
    swapped = [lunch(SAT, "Lu", "Server"), row(SAT, "Cy", "Server"), row(SAT, "Bob", "Server")]
    kw = dict(profiles=STD, scores={"Lu": 4, "Cy": 4, "Bob": 4},
              typical_headcount={("Saturday", "morning"): {"Server": 1}, ("Saturday", "night"): {"Server": 2}})
    held = sq.score_rows(swapped, hard_breaches=m, **kw)
    assert shift_of(held, SAT)["capped_by"] == "hard_rules", "a swap did not bring a manager"
    gone = sq.score_rows(swapped, hard_breaches={"by_date": {SAT: [rest]}, "week": [], "rows_sig": {}}, **kw)
    assert shift_of(gone, SAT)["capped_by"] is None, "Ann's own breach left with her"
    over = {"id": ["over_max_hours", "ann", THU], "kind": "over_max_hours", "employee": "Ann",
            "dayparts": ["night"], "detail": "48h in the payroll week — over 40h"}
    weekly = {"by_date": {d: [dict(over)] for d in (THU, FRI, SAT)}, "week": [], "rows_sig": {}}
    rows = [row(d, n, "Server") for d in (THU, FRI, SAT) for n in ("Ann", "Bob")]
    out = sq.score_rows(rows, profiles=STD, scores={"Ann": 4, "Bob": 4}, hard_breaches=weekly,
                        typical_headcount={(day, "night"): {"Server": 2} for day in ("Thursday", "Friday", "Saturday")})
    assert [s["day"] for s in out["shifts"] if s["capped_by"] == "hard_rules"] == ["Saturday"]


def test_a_soft_flag_is_not_a_cap():
    soft = {"kind": "under_min_hours", "date": SAT, "employee": "Ann", "shift_start": "5:00pm", "hard": False}
    assert _clean_saturday(hard_breaches=[soft])["score"] == 100


# ── SQ-16: attribute rules need no ratings ─────────────────────────────────

CLOSER = {"closing": True, "role": "Server", "attribute": "can_close", "count": 1}


def test_a_can_close_rule_is_scored_with_nobody_rated():
    rows = [lunch(SAT, "Morning", "Server"), row(SAT, "Night", "Server")]
    ok = sq.score_rows(rows, profiles=STD, leader_rules=[CLOSER], leader_flags={"Night": True},
                       role_minimums={"Server": 1})
    assert dim(shift_of(ok, SAT), "leadership")["score"] == 100
    assert "leadership" in shift_of(ok, SAT, "morning")["not_applicable"]
    missed = sq.score_rows(rows, profiles=STD, leader_rules=[CLOSER], leader_flags={"Morning": True},
                           role_minimums={"Server": 1})
    lead = dim(shift_of(missed, SAT), "leadership")
    assert lead["score"] == sq.LEADER_MISS_SCORE and lead["facts"]["misses"][0]["why"] == "not_qualified"


# ── SQ-23: critical floors per profile ─────────────────────────────────────

def _half_kitchen(profile):
    return shift_of(sq.score_rows([row(SAT, "Cook1", "Cook")], profiles=[profile],
                                  role_minimums={"Cook": 2}, daily_target_hours={SAT: 6}), SAT)


def test_a_profile_can_set_its_own_floor():
    default = _half_kitchen(sq.ShiftProfile(key="std", source="restaurant"))
    lenient = _half_kitchen(sq.ShiftProfile(key="std", floors={"coverage": 40}, source="restaurant"))
    assert default["capped_by"] == "coverage" and default["score"] == 50
    assert lenient["capped_by"] is None and lenient["score"] > 50
    off = _half_kitchen(sq.ShiftProfile(key="std", floors={"coverage": 0}, source="restaurant"))
    assert off["capped_by"] is None
    assert dim(lenient, "coverage")["floor"] == 40 and dim(default, "coverage")["floor"] == 70


def test_profile_floors_are_bounded_and_round_trip():
    p = sq.profile_from_dict({"key": "x", "floors": {"coverage": 50, "leadership": 999, "operational_strength": -5,
                                                     "fairness": 30, "not_a_dimension": 40, "splh": "x"}})
    assert p.floors == {"coverage": 50, "leadership": 100, "operational_strength": 0, "fairness": 30}
    assert sq.profile_to_dict(sq.profile_from_dict(sq.profile_to_dict(p)))["floors"] == p.floors
    assert sq.profile_from_dict({"key": "y"}).floor("coverage") == sq.CRITICAL_FLOORS["coverage"]
    assert p.floor("operational_strength") is None, "0 is no floor"


def test_a_profile_floor_on_any_dimension_caps_at_its_score():
    """Calibration (or the owner) may set a floor on a dimension that has
    none by default; it then holds the shift like a critical one."""
    rows = [row(SAT, "A", "Cook"), row(SAT, "B", "Cook", hours=20)]
    prof = sq.ShiftProfile(key="std", floors={"labor_efficiency": 90}, source="restaurant")
    shift = shift_of(sq.score_rows(rows, profiles=[prof], role_minimums={"Cook": 2},
                                   daily_target_hours={SAT: 10}), SAT)
    assert shift["capped_by"] == "labor_efficiency" and dim(shift, "labor_efficiency")["floor"] == 90


# ── SQ-28: why a shift scored low, and what would raise it ─────────────────

def test_every_below_the_bar_line_carries_its_reason():
    out = _monday_dinner([BAR5])
    line = next(b for b in out["below_profile"] if b["date"] == MON)
    assert line["held_by"]["key"] == "leadership"
    assert line["reason"].startswith(f"Leadership is holding this shift at {sq.LEADER_MISS_SCORE}")
    # Under the bar without a cap: the costliest weakness speaks for it.
    rows = [row(SAT, "S1", "Server"), row(SAT, "S2", "Server")]
    uncapped = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="hi", min_quality=99, demand="peak",
                                                             source="restaurant")],
                             scores={"S1": 3, "S2": 3, "S3": 5},
                             roster_roles={"S1": "Server", "S2": "Server", "S3": "Server"},
                             typical_headcount={("Saturday", "night"): {"Server": 2}})
    line = next(b for b in uncapped["below_profile"] if b["date"] == SAT)
    assert line["held_by"] is None and line["reason"].startswith("Team averages 3.0"), line


def test_every_counting_dimension_is_shown():
    rows = [row(SAT, "A", "Server"), row(SAT, "B", "Server")]
    out = sq.score_rows(rows, profiles=STD, scores={"A": 4, "B": 4}, typical_headcount={("Saturday", "night"): {"Server": 2}},
                        prior_pattern={"A": {"days": ["Saturday"], "dayparts": ["night"]}},
                        tenure={"A": 40, "B": 30}, cross_trained={"A": ["Server", "Host"]})
    shift = shift_of(out, SAT)
    keys = {d["key"] for d in shift["dimensions"]}
    assert {"stability", "experience_balance", "cross_training"} <= keys
    assert all(d["customer_facing"] for d in shift["dimensions"]), shift["dimensions"]
    assert all(d["customer_facing"] for d in out["dimensions"] if not d.get("week_level"))
    assert set(sq.CUSTOMER_DIMENSIONS) == set(sq.DIMENSIONS) - set(sq.WEEK_LEVEL_DIMENSIONS)


def test_each_suggestion_says_how_many_points_it_is_worth():
    rows = [row(SAT, "Ann", "Server"), row(MON, "Bob", "Server")]
    typical = {("Saturday", "night"): {"Server": 3}, ("Monday", "night"): {"Server": 2}}
    out = sq.score_rows(rows, profiles=None, typical_headcount=typical)
    recs = out["recommendations"]
    points = out["recommendation_points"]
    assert recs and set(recs) <= set(points)
    assert all(points[r] > 0 for r in recs if r.startswith("Fill the gap"))
    gaps = [r for r in recs if r.startswith("Fill the gap")]
    # The busier, emptier Saturday is worth more and comes first.
    assert gaps[0].startswith("Fill the gap on Saturday night") and points[gaps[0]] > points[gaps[1]]
    assert [d["text"] for d in out["recommendation_details"]] == recs
    # Fixing one gap fully is what the number promises: the week's own
    # aggregation with that shift's coverage met.
    sat = shift_of(out, SAT)
    assert points[gaps[0]] == pytest.approx(
        sq.points_if_fixed(out["shifts"], sat, "coverage", out["week_dimensions"]), abs=0.01)


# ── SQ-31: under target is fine whenever the floors hold ───────────────────

def _under_target(cooks):
    rows = [row(SAT, f"C{i}", "Cook") for i in range(cooks)]
    return dim(shift_of(sq.score_rows(rows, profiles=STD, role_minimums={"Cook": 4},
                                      daily_target_hours={SAT: 40}), SAT), "labor_efficiency")


def test_one_thin_spot_does_not_turn_under_target_into_a_second_penalty():
    assert _under_target(3)["score"] == 100 and _under_target(3)["facts"]["under_with_coverage"]
    assert _under_target(2)["score"] < 100, "under the coverage floor the hours are not forgiven"


# ── SQ-32: confidence names its top reason; re-levelled built-ins are no charge ──

def test_confidence_names_its_biggest_reason():
    rows = [row(SAT, n, "Server") for n in ("A", "B", "C", "D", "E", "F")]
    conf = sq.score_rows(rows, profiles=STD, scores={"A": 4}, typical_headcount={("Saturday", "night"): {"Server": 6}})["confidence"]
    assert conf["top_reason"] == "5 of 6 scheduled staff have no Operational Score."
    pts = [b["points"] for b in conf["breakdown"]]
    assert pts == sorted(pts, reverse=True) and conf["score"] == 100 - sum(pts)


def test_built_in_profiles_relevelled_by_sales_are_not_charged_as_defaults():
    rows = [row(SAT, "A", "Server")]
    by_day = {"Saturday": 30}
    relevelled = sq.score_rows(rows, profiles=sq.profiles_from_config(None, demand_by_day=by_day),
                               demand_by_day=by_day, typical_headcount={("Saturday", "night"): {"Server": 1}})
    raw = sq.score_rows(rows, profiles=None, typical_headcount={("Saturday", "night"): {"Server": 1}})
    charge = "Using default shift profiles rather than this restaurant's own."
    assert charge not in relevelled["confidence"]["reasons"]
    assert charge in raw["confidence"]["reasons"]


# ── the engine passes the points through to every client ───────────────────

@pytest.fixture
def db(db_path, monkeypatch):
    import models
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def test_the_served_recommendation_items_carry_their_points(db, monkeypatch):
    import schedule_engine as se
    from models import create_restaurant, Restaurant
    rid = create_restaurant(Restaurant(name="Points Co", owner_email="p@x.com"), db_path=db)
    gap = "Fill the gap on Saturday night: Server short 1 of 3."
    monkeypatch.setattr(se, "_quality_signals", lambda r, result, **extra: ({}, None))
    monkeypatch.setattr(sq, "score_rows", lambda rows, **k: {"checked": False, "recommendations": [gap],
                                                             "recommendation_points": {gap: 4.2}})
    quality, _w = se._score_schedule_quality(rid, [row(SAT, "Ann", "Server")], {})
    assert quality["recommendation_items"][0]["points"] == 4.2
