"""The Shift Quality Engine.

The scheduler used to answer three small questions well — who is free,
what does it cost, how many bodies — and nothing judged the result as a
whole. Erik's Saturday was the proof: every individual rule passed and the
shift was still wrong.

These tests are written as scenarios an operator would recognise rather
than as unit tests of each function, because the failure this engine exists
to catch is emergent. No single dimension going wrong produces it.
"""
import pytest

import shift_quality as sq


# ── Fixtures shaped like real weeks ────────────────────────────────────────
#
# 2026-09-07 is a Monday, so the dates below run Monday through Sunday and
# every weekday name in a test is the real one for that date.
MON, TUE, WED = "2026-09-07", "2026-09-08", "2026-09-09"
THU, FRI, SAT, SUN = "2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13"


def row(date, name, role, start="5:00pm", end="11:00pm", hours=8):
    return {"date": date, "day": "", "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": hours,
            "notes": ""}


def lunch(date, name, role, hours=6):
    return row(date, name, role, start="11:00am", end="5:00pm", hours=hours)


def profiles(**overrides):
    """A small, explicit profile set — one peak night, one quiet lunch."""
    peak = sq.ShiftProfile(
        key="sat_dinner", label="Saturday dinner", days=["Saturday"], daypart="night",
        demand="peak", min_quality=82, requires_leader=True, leader_min_score=5,
        min_strength={"Bartender": 8, "Cook": 7},
        critical_positions={"Bartender": 2, "Cook": 2, "Server": 2},
        experience_mix=0.6, priority=2, source="restaurant")
    quiet = sq.ShiftProfile(
        key="mon_lunch", label="Monday lunch", days=["Monday"], daypart="morning",
        demand="low", min_quality=60, training_allowed=True, experience_mix=0.25,
        min_strength={"Server": 4}, critical_positions={"Server": 2, "Cook": 1},
        priority=2, source="restaurant")
    for key, value in overrides.items():
        setattr(peak if key.startswith("peak_") else quiet,
                key.split("_", 1)[1], value)
    return [sq.ShiftProfile(key="default", label="Standard shift"), peak, quiet]


def saturday(names_by_role):
    return [row(SAT, n, role) for role, names in names_by_role.items() for n in names]


# ── The failure this engine exists to catch ────────────────────────────────

def test_erik_saturday_two_weakest_bartenders_is_not_a_good_shift():
    """Availability said both were free. Labor maths worked. Coverage was
    satisfied. Nothing said they were the two weakest people he had."""
    rows = saturday({"Bartender": ["Sam", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Sam": 2, "Alex": 2, "Jo": 4, "Kim": 3,
                                "Dana": 4, "Lee": 3, "Pat": 5})
    shift = out["shifts"][0]
    assert shift["score"] < 70, shift["score"]
    assert not shift["meets_profile"]
    assert any("Bartender strength" in w for w in shift["weaknesses"])


def test_the_weakest_role_sets_the_strength_figure_not_the_mean():
    """A kitchen at half strength is not rescued by an over-strong bar, and
    an operator reading an averaged "75%" would never guess one station was
    in trouble."""
    rows = saturday({"Bartender": ["Pat", "Casey"], "Cook": ["Weak1", "Weak2"]})
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="std", min_strength={"Bartender": 8, "Cook": 8}, source="restaurant")],
        scores={"Pat": 5, "Casey": 5, "Weak1": 1, "Weak2": 1})
    strength = next(d for d in out["shifts"][0]["dimensions"]
                    if d["key"] == "operational_strength")
    # Bartender clears its 8; Cook manages 2 of 8. The mean would say 62.
    assert strength["score"] == 25, strength["facts"]


def test_the_same_shift_with_the_strong_bartender_scores_well():
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Pat": 5, "Alex": 4, "Jo": 4, "Kim": 4,
                                "Dana": 4, "Lee": 4})
    shift = out["shifts"][0]
    assert shift["meets_profile"], shift
    assert shift["score"] >= 82


# ── A dimension with nothing to judge withdraws; it never scores zero ──────

def test_a_dimension_with_no_data_is_removed_not_zeroed():
    """The single most important property in the file. A restaurant that
    has not uploaded tenure must not score worse than one that has — it
    must simply not be judged on experience."""
    rows = saturday({"Bartender": ["Pat", "Alex"]})
    scores = {"Pat": 5, "Alex": 5}
    without = sq.score_rows(rows, profiles=profiles(), scores=scores)
    with_tenure = sq.score_rows(rows, profiles=profiles(), scores=scores,
                                tenure={"Pat": 60, "Alex": 60})
    assert "experience_balance" in without["shifts"][0]["not_applicable"]
    assert without["shifts"][0]["score"] == with_tenure["shifts"][0]["score"]


def test_weights_renormalise_over_what_applied():
    """Two dimensions at 100 and nine withdrawn is 100, not 100 times two
    over the full weight table."""
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Pat": 5, "Alex": 5, "Jo": 4, "Kim": 4, "Dana": 5, "Lee": 5})
    shift = out["shifts"][0]
    applied = {d["key"] for d in shift["dimensions"]}
    assert applied and len(applied) < len(sq.DIMENSIONS)
    assert shift["score"] == 100


def test_an_unrated_employee_contributes_nothing_and_is_named():
    rows = saturday({"Bartender": ["Pat", "Ghost"]})
    out = sq.score_rows(rows, profiles=profiles(), scores={"Pat": 5})
    shift = out["shifts"][0]
    strength = next(d for d in shift["dimensions"] if d["key"] == "operational_strength")
    assert strength["facts"]["shortfalls"][0]["strength"] == 5
    assert any("Ghost" in b for b in shift["blind_spots"])


# ── A critical dimension caps the shift rather than being averaged away ────

def test_an_unstaffed_kitchen_caps_the_whole_shift():
    """Coverage at 33% is not a B+ that eight 95s can outvote."""
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"], "Server": ["Dana", "Lee"]})
    full = sq.score_rows(rows, profiles=profiles(),
                         scores={"Pat": 5, "Alex": 5, "Jo": 5, "Kim": 5, "Dana": 5, "Lee": 5})
    gutted = [r for r in rows if r["role"] != "Cook"]
    short = sq.score_rows(gutted, profiles=profiles(),
                          scores={"Pat": 5, "Alex": 5, "Dana": 5, "Lee": 5})
    assert full["shifts"][0]["score"] == 100
    assert short["shifts"][0]["capped_by"] == "coverage"
    coverage = next(d for d in short["shifts"][0]["dimensions"] if d["key"] == "coverage")
    # Capped at the failing dimension's own score, with no allowance on top
    # — any allowance re-opens the same hole in miniature.
    assert short["shifts"][0]["score"] == coverage["score"]
    assert short["shifts"][0]["score"] <= 70


def test_the_cap_names_which_dimension_did_it():
    """Half a bar and no kitchen at all, with five-rated people on the floor.
    Without a cap the survivors' perfect scores would carry it."""
    rows = saturday({"Bartender": ["Sam"], "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Sam": 5, "Dana": 5, "Lee": 5})
    shift = out["shifts"][0]
    assert shift["capped_by"] == "coverage"
    assert shift["score"] == 50, shift["score"]


# ── Team composition, not individual ratings ───────────────────────────────

def test_three_average_cooks_and_one_lead_beats_four_average_cooks():
    """The difference is invisible to any per-person rule. Both teams are
    the same size, cost the same and fill the same positions."""
    flat = saturday({"Cook": ["A", "B", "C", "D"], "Bartender": ["Pat", "Alex"],
                     "Server": ["Dana", "Lee"]})
    led = saturday({"Cook": ["Lead", "B", "C", "D"], "Bartender": ["Pat", "Alex"],
                    "Server": ["Dana", "Lee"]})
    base = {"A": 3, "B": 3, "C": 3, "D": 3, "Lead": 5,
            "Pat": 5, "Alex": 4, "Dana": 4, "Lee": 4}
    flat_out = sq.score_rows(flat, profiles=profiles(), scores=base)
    led_out = sq.score_rows(led, profiles=profiles(), scores=base)
    assert led_out["shifts"][0]["score"] > flat_out["shifts"][0]["score"]


def test_a_developing_employee_alone_in_their_role_is_flagged():
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Rookie", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Pat": 5, "Alex": 4, "Rookie": 1, "Kim": 2,
                                "Dana": 4, "Lee": 4})
    training = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "training_balance")
    assert training["facts"]["isolated"] >= 1
    assert any("Rookie" in w for w in out["shifts"][0]["weaknesses"])


def test_the_same_developing_employee_beside_a_mentor_is_not_flagged():
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Rookie", "Chef"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Pat": 5, "Alex": 4, "Rookie": 1, "Chef": 5,
                                "Dana": 4, "Lee": 4})
    training = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "training_balance")
    assert training["score"] == 100
    assert training["facts"]["isolated"] == 0


# ── Leadership ─────────────────────────────────────────────────────────────

def test_a_peak_shift_with_no_qualified_leader_is_caught():
    rows = saturday({"Bartender": ["Alex", "Jamie"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(
        rows, profiles=profiles(),
        scores={"Alex": 4, "Jamie": 4, "Jo": 4, "Kim": 4, "Dana": 3, "Lee": 3},
        leader_rules=[{"role": "Bartender", "days": ["Saturday"], "daypart": "night",
                       "min_score": 5}])
    leadership = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "leadership")
    assert leadership["score"] < 100
    assert leadership["facts"]["misses"]


def test_someone_authorised_to_close_satisfies_the_profile_requirement():
    """The can_close capability was registered in version one and surfaced
    by nothing. This is the architecture claim actually paying off."""
    rows = saturday({"Bartender": ["Alex"], "Server": ["Dana"]})
    without = sq.score_rows(rows, profiles=profiles(), scores={"Alex": 3, "Dana": 3})
    with_closer = sq.score_rows(rows, profiles=profiles(), scores={"Alex": 3, "Dana": 3},
                                leader_flags={"Dana": True})
    a = next(d for d in without["shifts"][0]["dimensions"] if d["key"] == "leadership")
    b = next(d for d in with_closer["shifts"][0]["dimensions"] if d["key"] == "leadership")
    assert a["score"] == 0 and b["score"] == 100


def test_leadership_is_not_judged_when_nothing_asks_for_it():
    """No rules and no profile requirement is a question this restaurant is
    not asking, which is different from failing it."""
    rows = [row(TUE, "Alex", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()], scores={"Alex": 3})
    assert "leadership" in out["shifts"][0]["not_applicable"]


def test_a_leader_rule_only_binds_the_shift_it_names():
    rules = [{"role": "Bartender", "days": ["Saturday"], "daypart": "night", "min_score": 5}]
    friday = [row(FRI, "Alex", "Bartender"), row(FRI, "Jamie", "Bartender")]
    out = sq.score_rows(friday, profiles=[sq.ShiftProfile()],
                        scores={"Alex": 3, "Jamie": 3}, leader_rules=rules)
    assert "leadership" in out["shifts"][0]["not_applicable"]


# ── Demand ─────────────────────────────────────────────────────────────────

def test_a_team_that_is_fine_on_a_tuesday_is_not_fine_on_a_saturday():
    """Same six people, same roles, same hours. The only difference is which
    night it is, and that has to be enough to change the verdict."""
    team = {"Bartender": ["A", "B"], "Cook": ["C", "D"], "Server": ["E", "F"]}
    scores = {k: 3 for k in "ABCDEF"}
    sat = sq.score_rows([row(SAT, n, r) for r, ns in team.items() for n in ns],
                        profiles=profiles(), scores=scores)
    tue = sq.score_rows([row(TUE, n, r) for r, ns in team.items() for n in ns],
                        profiles=profiles(), scores=scores)
    assert sat["shifts"][0]["score"] < tue["shifts"][0]["score"]


def test_demand_levels_come_from_real_sales_not_an_assumption():
    """The built-ins assume a busy Friday because most restaurants have one.
    A lunch counter does not, and asserting it anyway is exactly the kind of
    invented fact this codebase keeps having to remove."""
    quiet_friday = sq.profiles_from_config(demand_by_day={"Friday": -40, "Saturday": -35})
    friday = sq.resolve_profile("Friday", "night", quiet_friday)
    assert friday.demand == "low"
    assert friday.source == "your sales history"


def test_a_configured_profile_is_never_overwritten_by_sales_history():
    """An owner who set a demand level meant it."""
    mine = [sq.ShiftProfile(key="fri", days=["Friday"], daypart="night", demand="peak",
                            source="restaurant")]
    resolved = sq.profiles_from_config(mine, demand_by_day={"Friday": -50})
    assert resolved[0].demand == "peak"


def test_a_game_day_profile_raises_the_bar_for_one_night_only():
    game_day = sq.ShiftProfile(key="game_day", label="Game day", days=["Thursday"],
                               daypart="night", demand="peak", min_quality=88,
                               requires_leader=True, min_strength={"Bartender": 9},
                               priority=5, source="restaurant")
    rows = [row(THU, "A", "Bartender"), row(THU, "B", "Bartender"),
            row(WED, "A", "Bartender"), row(WED, "B", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(), game_day],
                        scores={"A": 4, "B": 4})
    by_day = {s["day"]: s for s in out["shifts"]}
    assert by_day["Thursday"]["profile"]["label"] == "Game day"
    assert by_day["Wednesday"]["profile"]["label"] == "Standard shift"
    assert by_day["Thursday"]["score"] < by_day["Wednesday"]["score"]


# ── Training mode ──────────────────────────────────────────────────────────

def test_a_quiet_lunch_tolerates_a_weaker_team():
    """Training has to be allowed somewhere or nobody ever improves. A low
    demand profile is where, and its own quality bar is set lower to say so."""
    rows = [lunch(MON, "Rookie", "Server"), lunch(MON, "Mentor", "Server"),
            lunch(MON, "Cook1", "Cook")]
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Rookie": 2, "Mentor": 4, "Cook1": 3})
    shift = out["shifts"][0]
    assert shift["profile"]["training_allowed"]
    assert shift["meets_profile"], shift["score"]


def test_training_never_excuses_leaving_a_rookie_alone():
    rows = [lunch(MON, "Rookie", "Server"), lunch(MON, "Other", "Server"),
            lunch(MON, "Cook1", "Cook")]
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Rookie": 1, "Other": 2, "Cook1": 3})
    weaknesses = " ".join(out["shifts"][0]["weaknesses"])
    assert "nobody stronger" in weaknesses
    assert "a mentor still has to be on it" in weaknesses


def test_a_peak_shift_is_never_a_training_shift():
    for profile in sq.BUILTIN_PROFILES:
        if profile.demand in ("high", "peak"):
            assert not profile.training_allowed, profile.key


# ── Fatigue and fairness: the dimensions that push the other way ───────────
#
# Every other rule in this engine tightens constraints, and a scheduler
# under tightening constraints puts the same strong people on everything.
# These two are the counterweight, and without them the engine would
# reliably produce a schedule that burns out half a restaurant.

def test_carrying_every_busy_shift_is_scored_against_you():
    heavy = [row(d, "Workhorse", "Bartender") for d in (THU, FRI, SAT, SUN)]
    heavy += [row(d, "Spare", "Bartender") for d in (THU, FRI, SAT, SUN)]
    heavy += [row(MON, "Workhorse", "Bartender"), row(TUE, "Workhorse", "Bartender")]
    busy = [sq.ShiftProfile(key="all_peak", demand="peak", label="Busy night",
                            daypart="night", source="restaurant")]
    out = sq.score_rows(heavy, profiles=busy, scores={"Workhorse": 5, "Spare": 4})
    fatigue = [d for s in out["shifts"] for d in s["dimensions"] if d["key"] == "fatigue"]
    assert any(f["score"] < 100 for f in fatigue)
    # Said once, at week level, rather than on every shift he works.
    assert any("Workhorse" in w for w in out["weaknesses"])


def test_a_week_spread_across_the_roster_is_not_penalised():
    rows = [row(FRI, "A", "Bartender"), row(SAT, "B", "Bartender"),
            row(SUN, "C", "Bartender"), row(THU, "D", "Bartender")]
    busy = [sq.ShiftProfile(key="all_peak", demand="peak", daypart="night",
                            label="Busy night", source="restaurant")]
    out = sq.score_rows(rows, profiles=busy, scores={"A": 4, "B": 4, "C": 4, "D": 4})
    fatigue = [d for s in out["shifts"] for d in s["dimensions"] if d["key"] == "fatigue"]
    assert all(f["score"] == 100 for f in fatigue)


def test_seven_days_in_a_row_is_flagged_even_at_low_demand():
    rows = [row(d, "Nonstop", "Cook") for d in (MON, TUE, WED, THU, FRI, SAT, SUN)]
    rows += [row(d, "Other", "Cook") for d in (MON, TUE)]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(min_strength={"Cook": 4})],
                        scores={"Nonstop": 4, "Other": 4})
    text = " ".join(out["weaknesses"]) + " ".join(
        w for s in out["shifts"] for w in s["weaknesses"])
    assert "7 days in a row" in text


def test_hoarding_the_premium_shifts_shows_up_as_unfairness():
    rows = [row(d, "Favourite", "Server") for d in (THU, FRI, SAT, SUN)]
    rows += [row(MON, "Ignored", "Server"), row(TUE, "Ignored", "Server"),
             row(MON, "Third", "Server")]
    busy_nights = [sq.ShiftProfile(key="nights", demand="peak", daypart="night",
                                   label="Nights", source="restaurant"),
                   sq.ShiftProfile(key="days", demand="low", daypart="morning",
                                   label="Days", source="restaurant")]
    out = sq.score_rows(rows, profiles=busy_nights,
                        scores={"Favourite": 4, "Ignored": 4, "Third": 4})
    fairness = [d for s in out["shifts"] for d in s["dimensions"] if d["key"] == "fairness"]
    assert fairness and min(f["score"] for f in fairness) < 100


# ── Experience, separate from rating ───────────────────────────────────────

def test_a_strong_but_brand_new_team_still_reads_as_inexperienced():
    """A strong new hire and a steady veteran are different kinds of useful.
    A shift made entirely of the first kind goes wrong in ways no rating
    predicts, which is why this is its own dimension."""
    rows = saturday({"Bartender": ["New1", "New2"], "Cook": ["New3", "New4"],
                     "Server": ["New5", "New6"]})
    scores = {f"New{i}": 5 for i in range(1, 7)}
    out = sq.score_rows(rows, profiles=profiles(), scores=scores,
                        tenure={f"New{i}": 2 for i in range(1, 7)})
    experience = next(d for d in out["shifts"][0]["dimensions"]
                      if d["key"] == "experience_balance")
    assert experience["score"] < 50
    assert len(experience["facts"]["rookies"]) == 6


def test_somebody_with_no_history_is_reported_not_assumed():
    rows = saturday({"Bartender": ["Known", "Unknown"]})
    out = sq.score_rows(rows, profiles=profiles(), scores={"Known": 4, "Unknown": 4},
                        tenure={"Known": 50})
    assert any("Unknown" in b for b in out["shifts"][0]["blind_spots"])


# ── Labor efficiency ───────────────────────────────────────────────────────

def test_being_over_the_day_target_costs_more_than_being_under():
    """The labor target is a ceiling everywhere else in this module, and
    this dimension has to agree with that or the engine argues with itself."""
    over = sq.score_rows([row(SAT, "A", "Cook", hours=20), row(SAT, "B", "Cook", hours=20)],
                         profiles=[sq.ShiftProfile()], scores={"A": 4, "B": 4},
                         daily_target_hours={SAT: 30})
    under = sq.score_rows([row(SAT, "A", "Cook", hours=10), row(SAT, "B", "Cook", hours=10)],
                          profiles=[sq.ShiftProfile()], scores={"A": 4, "B": 4},
                          daily_target_hours={SAT: 30})
    over_dim = next(d for d in over["shifts"][0]["dimensions"] if d["key"] == "labor_efficiency")
    under_dim = next(d for d in under["shifts"][0]["dimensions"] if d["key"] == "labor_efficiency")
    assert over_dim["score"] < under_dim["score"]


def test_no_target_means_labor_efficiency_is_not_judged():
    out = sq.score_rows([row(SAT, "A", "Cook")], profiles=[sq.ShiftProfile()],
                        scores={"A": 4})
    assert "labor_efficiency" in out["shifts"][0]["not_applicable"]


# ── Confidence is a separate claim from quality ────────────────────────────

def test_a_94_on_three_ratings_is_not_the_same_claim_as_a_94_on_thirty():
    """Collapsing how good the schedule is and how much the engine knew
    into one number is the single most misleading thing this could do."""
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    full = sq.score_rows(rows, profiles=profiles(),
                         scores={"Pat": 5, "Alex": 5, "Jo": 4, "Kim": 4, "Dana": 4, "Lee": 4},
                         tenure={n: 40 for n in ("Pat", "Alex", "Jo", "Kim", "Dana", "Lee")},
                         demand_by_day={"Saturday": 40}, availability={"Pat": {"Monday"}})
    thin = sq.score_rows(rows, profiles=profiles(), scores={"Pat": 5, "Alex": 5})
    assert full["confidence"]["level"] == "high"
    assert thin["confidence"]["level"] in ("moderate", "low")
    assert any("Operational Score" in r for r in thin["confidence"]["reasons"])
    # Isolated: the ONLY difference between these two is how much of the
    # roster is rated, so nothing else can be what moved the number.
    everyone = {n: 4 for n in ("Pat", "Alex", "Jo", "Kim", "Dana", "Lee")}
    rated = sq.score_rows(rows, profiles=profiles(), scores=everyone)
    half = sq.score_rows(rows, profiles=profiles(),
                         scores={"Pat": 4, "Alex": 4, "Jo": 4})
    assert rated["confidence"]["score"] - half["confidence"]["score"] >= 15


def test_confidence_falls_when_rows_needed_a_human_check():
    rows = saturday({"Bartender": ["Pat", "Alex"]})
    clean = sq.score_rows(rows, profiles=profiles(), scores={"Pat": 5, "Alex": 5})
    messy = sq.score_rows(rows, profiles=profiles(), scores={"Pat": 5, "Alex": 5},
                          rows_needing_review=4, dropped_rows=2)
    assert messy["confidence"]["score"] < clean["confidence"]["score"]
    assert any("could not be read" in r for r in messy["confidence"]["reasons"])


def test_confidence_never_moves_the_quality_score():
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    scores = {"Pat": 5, "Alex": 5, "Jo": 4, "Kim": 4, "Dana": 4, "Lee": 4}
    a = sq.score_rows(rows, profiles=profiles(), scores=scores)
    b = sq.score_rows(rows, profiles=profiles(), scores=scores,
                      rows_needing_review=9, dropped_rows=5)
    assert a["score"] == b["score"]
    assert a["confidence"]["score"] > b["confidence"]["score"]


def test_every_confidence_penalty_states_its_reason():
    """A low confidence with no reason is an accusation the owner cannot act
    on. Every point deducted has to be attributable."""
    out = sq.score_rows(saturday({"Bartender": ["A", "B"]}), profiles=profiles(),
                        scores={"A": 4}, rows_needing_review=2, dropped_rows=1)
    conf = out["confidence"]
    assert conf["score"] < 100
    assert conf["reasons"]


# ── What-if: alternatives from the same people ─────────────────────────────

def test_the_engine_finds_the_swap_that_fixes_eriks_saturday():
    rows = [row(SAT, "Sam", "Bartender"), row(SAT, "Alex", "Bartender"),
            row(FRI, "Pat", "Bartender"), row(FRI, "Casey", "Bartender")]
    out = sq.compare_candidates(
        rows, profiles=profiles(),
        scores={"Pat": 5, "Casey": 4, "Sam": 2, "Alex": 2},
        leader_rules=[{"role": "Bartender", "days": ["Saturday"], "daypart": "night",
                       "min_score": 5}])
    assert out["ran"] and out["improvement"] > 0
    assert out["swaps"]
    moved = {out["swaps"][0]["from"]["employee"], out["swaps"][0]["to"]["employee"]}
    assert "Pat" in moved


def test_a_swap_is_never_made_onto_a_day_somebody_cannot_work():
    """A schedule that breaks availability with a better score is not an
    improvement, it is a broken schedule."""
    rows = [row(SAT, "Sam", "Bartender"), row(FRI, "Pat", "Bartender")]
    out = sq.compare_candidates(
        rows, profiles=profiles(), scores={"Pat": 5, "Sam": 2},
        availability={"Pat": {"Saturday"}},
        leader_rules=[{"role": "Bartender", "days": ["Saturday"], "daypart": "night",
                       "min_score": 5}])
    assert out["swaps"] == []
    # The only candidate swap was illegal, so nothing was evaluated — and the
    # verdict says exactly that rather than claiming the arrangement won.
    assert out["evaluated"] == 0
    assert "availability" in out["verdict"]


def test_a_swap_never_pushes_anybody_over_forty_hours():
    """Pat's kitchen hours are in a different role, so the ONE bartender swap
    available is the one that would take him from 38h to 44h. It is the
    right answer for quality and the wrong answer for a schedule."""
    rows = ([row(d, "Pat", "Cook", hours=12) for d in (MON, TUE, WED)]
            + [row(FRI, "Pat", "Bartender", hours=2)]
            + [row(SAT, "Sam", "Bartender", hours=8)])
    scores = {"Pat": 5, "Sam": 2}
    rules = [{"role": "Bartender", "days": ["Saturday"], "daypart": "night",
              "min_score": 5}]
    # Without the ceiling this swap is a clear win, which is what makes it
    # a real test of the ceiling rather than of anything else.
    assert sq._swap_is_legal(rows, 3, 4, {}, scores) is False
    out = sq.compare_candidates(rows, profiles=profiles(), scores=scores,
                                leader_rules=rules)
    final = {}
    for r in out["rows"]:
        final[r["employee"]] = final.get(r["employee"], 0) + r["scheduled_hours"]
    assert max(final.values()) <= sq.WEEKLY_HOURS_CEILING, final
    assert out["swaps"] == []


def test_a_swap_never_double_books_somebody():
    """A already works Friday. Trading A's Saturday row for B's Friday one
    would put A on Friday twice — and on strength alone it reads as a gain,
    because a duplicate name is counted once and B's weak score disappears."""
    scores = {"A": 5, "B": 1}
    # Both directions of the collision, because they are separate checks:
    # the person moving IN already works that day, or the person moving OUT
    # already works the day they would move to.
    rows = [row(SAT, "A", "Cook"), row(FRI, "B", "Cook"), row(FRI, "A", "Cook")]
    assert sq._swap_is_legal(rows, 0, 1, {}, scores) is False
    mirrored = [row(SAT, "A", "Cook"), row(FRI, "B", "Cook"), row(SAT, "B", "Cook")]
    assert sq._swap_is_legal(mirrored, 0, 1, {}, scores) is False
    out = sq.compare_candidates(
        rows, profiles=[sq.ShiftProfile(key="std", min_strength={"Cook": 8},
                                        source="restaurant")], scores=scores)
    seen = set()
    for r in out["rows"]:
        key = (r["employee"], r["date"], r["shift_start"])
        assert key not in seen, key
        seen.add(key)


def test_the_comparison_never_rewrites_the_schedule_the_owner_reads():
    """The engine reports what a better arrangement WOULD have been. A swap
    the manager did not ask for, applied without being told, is how trust in
    a generated schedule dies."""
    rows = [row(SAT, "Sam", "Bartender"), row(FRI, "Pat", "Bartender")]
    before = [dict(r) for r in rows]
    sq.compare_candidates(rows, profiles=profiles(), scores={"Pat": 5, "Sam": 2},
                          leader_rules=[{"role": "Bartender", "days": ["Saturday"],
                                         "daypart": "night", "min_score": 5}])
    assert rows == before


def test_finding_nothing_better_is_a_real_answer():
    rows = [row(SAT, "Pat", "Bartender"), row(FRI, "Casey", "Bartender")]
    out = sq.compare_candidates(rows, profiles=profiles(), scores={"Pat": 5, "Casey": 5})
    assert out["ran"]
    assert out["improvement"] == 0
    assert "strongest team available" in out["verdict"]


def test_every_accepted_swap_says_which_dimensions_it_moved():
    rows = [row(SAT, "Sam", "Bartender"), row(SAT, "Alex", "Bartender"),
            row(FRI, "Pat", "Bartender"), row(FRI, "Casey", "Bartender")]
    out = sq.compare_candidates(
        rows, profiles=profiles(), scores={"Pat": 5, "Casey": 4, "Sam": 2, "Alex": 2},
        leader_rules=[{"role": "Bartender", "days": ["Saturday"], "daypart": "night",
                       "min_score": 5}])
    assert out["swaps"]
    for swap in out["swaps"]:
        assert swap["moved"], swap
        assert swap["reason"].startswith("Swapped ")


def test_the_comparison_is_bounded_so_a_big_restaurant_stays_fast():
    rows = [row(d, f"E{i}", "Server")
            for d in (MON, TUE, WED, THU, FRI, SAT, SUN) for i in range(12)]
    out = sq.compare_candidates(rows, profiles=[sq.ShiftProfile()],
                                scores={f"E{i}": (i % 5) + 1 for i in range(12)},
                                max_evaluations=15)
    assert out["evaluated"] <= 15


# ── Roster shapes ──────────────────────────────────────────────────────────

def test_a_restaurant_of_only_weak_staff_still_gets_its_best_schedule():
    """An owner who cannot staff a Saturday to target needs the best
    schedule available AND to be told. Not an error, and not a refusal."""
    rows = saturday({"Bartender": ["A", "B"], "Cook": ["C", "D"], "Server": ["E", "F"]})
    out = sq.score_rows(rows, profiles=profiles(), scores={k: 1 for k in "ABCDEF"})
    assert out["checked"]
    assert out["score"] < 60
    assert out["recommendations"]
    assert out["shifts"][0]["scored"]


def test_a_restaurant_of_only_strong_staff_scores_at_the_top():
    rows = saturday({"Bartender": ["A", "B"], "Cook": ["C", "D"], "Server": ["E", "F"]})
    out = sq.score_rows(rows, profiles=profiles(), scores={k: 5 for k in "ABCDEF"},
                        tenure={k: 60 for k in "ABCDEF"})
    assert out["score"] >= 95
    assert out["band"] == "excellent"


def test_a_two_person_restaurant_is_evaluated_not_skipped():
    rows = [row(SAT, "Owner", "Cook"), row(SAT, "Helper", "Server")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="tiny", critical_positions={"Cook": 1, "Server": 1}, source="restaurant")],
        scores={"Owner": 5, "Helper": 3})
    assert out["checked"] and out["shifts"][0]["score"] == 100


def test_a_sixty_person_roster_evaluates_without_falling_over():
    rows = []
    for i in range(60):
        rows.append(row((MON, TUE, WED, THU, FRI, SAT, SUN)[i % 7], f"E{i}",
                        ("Server", "Cook", "Bartender", "Host")[i % 4]))
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={f"E{i}": (i % 5) + 1 for i in range(60)},
                        tenure={f"E{i}": i for i in range(60)})
    assert out["checked"]
    assert len(out["shifts"]) >= 7


def test_a_brand_new_restaurant_with_nothing_configured_is_told_so():
    """No ratings, no profiles, no targets. The engine must say it has
    nothing to judge rather than inventing a number."""
    rows = [row(SAT, "A", "Cook")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()])
    assert out["checked"] is False
    assert "judge" in out["reason"]


# ── Profile resolution ─────────────────────────────────────────────────────

def test_the_most_specific_profile_wins():
    generic = sq.ShiftProfile(key="nights", daypart="night", label="Any night")
    specific = sq.ShiftProfile(key="sat", days=["Saturday"], daypart="night",
                               label="Saturday night")
    assert sq.resolve_profile("Saturday", "night", [generic, specific]).key == "sat"
    assert sq.resolve_profile("Tuesday", "night", [generic, specific]).key == "nights"


def test_priority_beats_specificity_when_an_admin_sets_it():
    generic = sq.ShiftProfile(key="holiday", label="Holiday week", priority=9)
    specific = sq.ShiftProfile(key="sat", days=["Saturday"], daypart="night", priority=2)
    assert sq.resolve_profile("Saturday", "night", [generic, specific]).key == "holiday"


def test_a_shift_matching_nothing_falls_back_to_the_standard_bar():
    only_saturday = [sq.ShiftProfile(key="sat", days=["Saturday"], daypart="night")]
    assert sq.resolve_profile("Tuesday", "morning", only_saturday).key == "default"


def test_the_flat_targets_apply_everywhere_a_profile_does_not_override():
    """The per-role targets an owner already set stay meaningful. Only the
    shifts they deliberately profile differ from them."""
    mine = [sq.ShiftProfile(key="sat", days=["Saturday"], daypart="night",
                            min_strength={"Bartender": 12}),
            sq.ShiftProfile(key="tue", days=["Tuesday"], daypart="night")]
    resolved = sq.profiles_from_config(mine, default_strength={"Bartender": 8, "Cook": 6})
    by_key = {p.key: p for p in resolved}
    assert by_key["sat"].min_strength == {"Bartender": 12, "Cook": 6}
    assert by_key["tue"].min_strength == {"Bartender": 8, "Cook": 6}


def test_a_profile_round_trips_through_storage_unchanged():
    original = sq.ShiftProfile(
        key="game_day", label="Game day", days=["Sunday"], daypart="night",
        demand="peak", min_quality=90, min_strength={"Bartender": 10},
        critical_positions={"Bartender": 3}, requires_leader=True,
        leader_roles=["Bartender"], leader_min_score=5, experience_mix=0.7,
        training_allowed=False, weights={"leadership": 30}, priority=5)
    again = sq.profile_from_dict(sq.profile_to_dict(original))
    assert sq.profile_to_dict(again) == sq.profile_to_dict(original)


def test_a_corrupt_stored_profile_does_not_break_the_rest():
    assert sq.profile_from_dict({}).key == "custom"
    assert sq.profile_from_dict({"key": "x", "demand": None}).demand == "normal"


# ── Weighting ──────────────────────────────────────────────────────────────

def test_an_admin_can_make_a_dimension_count_for_more():
    """Demand match rather than a critical dimension, because a critical
    one under its floor caps the shift and no weighting can move a cap."""
    busy = [sq.ShiftProfile(key="busy", label="Busy night", demand="peak",
                            min_strength={"Bartender": 6}, source="restaurant")]
    rows = saturday({"Bartender": ["A", "B"]})
    scores = {"A": 3, "B": 3}
    normal = sq.score_rows(rows, profiles=busy, scores=scores)
    heavy = sq.score_rows(rows, profiles=busy, scores=scores,
                          weights={"demand_match": 80})
    assert normal["shifts"][0]["capped_by"] is None
    assert heavy["score"] < normal["score"]


def test_a_zero_weight_removes_a_dimension_entirely_including_its_cap():
    """Zero means it does not count here. A dimension that still capped the
    shift would leave the setting half working, in the surprising direction."""
    rows = saturday({"Bartender": ["A", "B"], "Cook": ["C", "D"], "Server": ["E", "F"]})
    scores = {"A": 1, "B": 1, "C": 5, "D": 5, "E": 5, "F": 5}
    with_strength = sq.score_rows(rows, profiles=profiles(), scores=scores)
    without = sq.score_rows(rows, profiles=profiles(), scores=scores,
                            weights={"operational_strength": 0})
    assert with_strength["shifts"][0]["capped_by"] == "operational_strength"
    assert without["shifts"][0]["capped_by"] != "operational_strength"
    assert without["score"] > with_strength["score"]


def test_fatigue_alone_never_produces_a_shift_quality_score():
    """A restaurant with nothing configured used to score 100 off the back
    of "nobody is overworked", which is true and says nothing."""
    out = sq.score_rows([row(SAT, "A", "Cook"), row(SUN, "A", "Cook")],
                        profiles=[sq.ShiftProfile()])
    assert out["checked"] is False


def test_the_default_weights_put_the_operational_dimensions_first():
    """A weighting where fairness outranks coverage would be a different
    product. Pin the ordering the design actually argues for."""
    w = sq.DEFAULT_WEIGHTS
    assert w["coverage"] > w["operational_strength"] > w["leadership"]
    assert w["leadership"] > w["demand_match"] >= w["labor_efficiency"]
    assert w["labor_efficiency"] > w["fatigue"] > w["fairness"] >= w["stability"]


# ── Explanations a manager can act on ──────────────────────────────────────

def test_every_shift_explains_itself_in_the_owners_terms():
    rows = saturday({"Bartender": ["Sam", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Sam": 2, "Alex": 2, "Jo": 4, "Kim": 4, "Dana": 4, "Lee": 4})
    shift = out["shifts"][0]
    assert shift["headline"].startswith("Saturday dinner scored ")
    assert shift["weaknesses"]
    # Real names and real numbers, never a rule id or a dimension key.
    assert any("Sam" in w for w in shift["weaknesses"])
    assert not any("_" in w for w in shift["weaknesses"])


def test_a_recommendation_never_names_somebody_not_on_the_schedule():
    """Explanations are assembled from the dimensions' own facts rather
    than written by a model, so this is structural, not a hope."""
    rows = saturday({"Bartender": ["Sam"], "Cook": ["Jo"], "Server": ["Dana"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Sam": 1, "Jo": 1, "Dana": 1, "Ghost": 5, "Phantom": 5})
    text = " ".join(out["recommendations"]) + " ".join(
        w for s in out["shifts"] for w in s["weaknesses"])
    assert "Ghost" not in text and "Phantom" not in text


def test_the_same_problem_on_five_shifts_is_stated_once():
    rows = [row(d, "Weak", "Bartender") for d in (MON, TUE, WED, THU, FRI)]
    rows += [row(d, "Other", "Bartender") for d in (MON, TUE, WED, THU, FRI)]
    every_night = [sq.ShiftProfile(key="n", label="Night", daypart="night",
                                   min_strength={"Bartender": 9}, source="restaurant")]
    out = sq.score_rows(rows, profiles=every_night, scores={"Weak": 1, "Other": 1})
    strength_lines = [w for w in out["weaknesses"] if "Bartender strength" in w]
    assert len(strength_lines) == 1
    # Hoisted from every shift, so it carries no count: it sits under a
    # week-level heading that already says what it is.
    assert strength_lines[0].endswith(".")
    # And removed from the shifts themselves, so it is read once not five times.
    assert not any("Bartender strength" in w
                   for s in out["shifts"] for w in s["weaknesses"])


def test_the_week_names_its_best_and_worst_shift():
    rows = saturday({"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    rows += [lunch(MON, "Rookie", "Server"), lunch(MON, "Mentor", "Server"),
             lunch(MON, "Jo", "Cook")]
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Pat": 5, "Alex": 5, "Jo": 4, "Kim": 4, "Dana": 4,
                                "Lee": 4, "Rookie": 1, "Mentor": 2})
    assert out["best"] and out["worst"] and out["best"] != out["worst"]


def test_a_shift_under_its_own_profile_bar_is_listed_separately():
    rows = saturday({"Bartender": ["Sam", "Alex"], "Cook": ["Jo", "Kim"],
                     "Server": ["Dana", "Lee"]})
    out = sq.score_rows(rows, profiles=profiles(),
                        scores={"Sam": 1, "Alex": 1, "Jo": 4, "Kim": 4, "Dana": 4, "Lee": 4})
    assert out["below_profile"]
    assert out["below_profile"][0]["min_quality"] == 82


# ── The week rolls up by demand, not flat ──────────────────────────────────

def test_the_weekly_score_weights_a_peak_shift_above_a_quiet_one():
    """Isolated from profile bars entirely: identical per-shift scores, and
    the only difference is WHICH of the two shifts is the weak one."""
    peak = sq.ShiftProfile(key="peak", label="Saturday night", days=["Saturday"],
                           daypart="night", demand="peak", source="restaurant")
    quiet = sq.ShiftProfile(key="quiet", label="Monday lunch", days=["Monday"],
                            daypart="morning", demand="low", source="restaurant")
    catch_all = sq.ShiftProfile(key="std", min_strength={"Cook": 10},
                                source="restaurant")
    peak.min_strength = quiet.min_strength = {"Cook": 10}
    setup = [peak, quiet, catch_all]

    def week(sat_pair, mon_pair):
        return ([row(SAT, n, "Cook") for n in sat_pair]
                + [lunch(MON, n, "Cook") for n in mon_pair])

    scores = {"S1": 5, "S2": 5, "W1": 1, "W2": 1}
    weak_saturday = sq.score_rows(week(("W1", "W2"), ("S1", "S2")),
                                  profiles=setup, scores=scores)
    weak_monday = sq.score_rows(week(("S1", "S2"), ("W1", "W2")),
                                profiles=setup, scores=scores)
    # Both weeks contain exactly one 100 and one 20; only the demand differs.
    assert sorted(s["score"] for s in weak_saturday["shifts"]) == \
           sorted(s["score"] for s in weak_monday["shifts"])
    assert weak_saturday["score"] < weak_monday["score"]


def test_a_weak_saturday_costs_the_week_more_than_a_weak_monday():
    """A flat mean says a bad Saturday dinner and a bad Monday lunch are the
    same week. No operator believes that."""
    strong = {"Bartender": ["Pat", "Alex"], "Cook": ["Jo", "Kim"], "Server": ["Dana", "Lee"]}
    scores = {"Pat": 5, "Alex": 5, "Jo": 5, "Kim": 5, "Dana": 5, "Lee": 5,
              "W1": 1, "W2": 1}
    weak_sat = [row(SAT, "W1", "Bartender"), row(SAT, "W2", "Bartender"),
                row(SAT, "Jo", "Cook"), row(SAT, "Kim", "Cook"),
                row(SAT, "Dana", "Server"), row(SAT, "Lee", "Server")]
    weak_sat += [lunch(MON, n, r) for r, ns in strong.items() for n in ns]
    weak_mon = [row(SAT, n, r) for r, ns in strong.items() for n in ns]
    weak_mon += [lunch(MON, "W1", "Server"), lunch(MON, "W2", "Server"),
                 lunch(MON, "Jo", "Cook")]
    a = sq.score_rows(weak_sat, profiles=profiles(), scores=scores)
    b = sq.score_rows(weak_mon, profiles=profiles(), scores=scores)
    assert a["score"] < b["score"]


def test_morning_and_night_are_scored_as_separate_shifts():
    rows = [lunch(SAT, "A", "Server"), row(SAT, "B", "Server")]
    out = sq.score_rows(rows, profiles=profiles(), scores={"A": 4, "B": 4})
    assert {s["daypart"] for s in out["shifts"]} == {"morning", "night"}


def test_an_unreadable_start_time_is_its_own_bucket_not_guessed_into_night():
    """A wrong daypart silently reassigns people between shifts judged
    against different profiles."""
    assert sq.daypart_of("") == "unknown"
    assert sq.daypart_of("half past nine") == "unknown"
    assert sq.daypart_of("16:00") == "night"
    assert sq.daypart_of("4:00pm") == "night"
    assert sq.daypart_of("11:00am") == "morning"
    assert sq.daypart_of("11:00") == "morning"


# ── Defects the end-to-end audit found ─────────────────────────────────────

def test_a_role_minimum_is_a_service_day_not_a_daypart():
    """"Minimum 2 bartenders" is a statement about a service day. Applied to
    both halves of it, a restaurant whose bar opens at five read as two
    bartenders short every single lunch."""
    rows = [lunch(MON, "Cook1", "Cook"), lunch(MON, "Serv1", "Server")]
    typical = {("Monday", "morning"): {"Cook": 1, "Server": 1},
               ("Monday", "night"): {"Cook": 1, "Server": 1, "Bartender": 2}}
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()],
                        role_minimums={"Bartender": 2, "Cook": 1, "Server": 1},
                        typical_headcount=typical, scores={"Cook1": 4, "Serv1": 4})
    coverage = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert coverage["score"] == 100, coverage["facts"]
    assert coverage["facts"]["gaps"] == []


def test_a_role_minimum_still_binds_the_daypart_that_runs_it():
    rows = [row(MON, "Cook1", "Cook"), row(MON, "Serv1", "Server")]
    typical = {("Monday", "night"): {"Cook": 1, "Server": 1, "Bartender": 2}}
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()],
                        role_minimums={"Bartender": 2, "Cook": 1, "Server": 1},
                        typical_headcount=typical, scores={"Cook1": 4, "Serv1": 4})
    coverage = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert any("Bartender" in g for g in coverage["facts"]["gaps"])


def test_role_minimums_apply_everywhere_when_there_is_no_history_to_narrow_them():
    rows = [row(MON, "Cook1", "Cook")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile()],
                        role_minimums={"Bartender": 1, "Cook": 1},
                        scores={"Cook1": 4})
    coverage = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert coverage["score"] == 50


def test_a_seven_day_run_produces_advice_not_just_a_complaint():
    """A weakness with no recommendation is something the manager can read
    and not act on."""
    rows = [row(d, "Nonstop", "Cook") for d in (MON, TUE, WED, THU, FRI, SAT, SUN)]
    rows += [row(d, "Other", "Cook") for d in (MON, TUE)]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(min_strength={"Cook": 4})],
                        scores={"Nonstop": 4, "Other": 4})
    assert any("day off" in r and "Nonstop" in r for r in out["recommendations"]), \
        out["recommendations"]


def test_the_comparison_never_recommends_benching_an_unrated_employee():
    """An unrated person counts as nothing toward strength, so moving them
    off a busy night always raises the score. Left unchecked, the engine
    ends up advising an owner not to schedule the people they have not got
    round to rating — the exact opposite of what the rating design says."""
    rows = [row(SAT, "Unrated", "Bartender"), row(SAT, "Alex", "Bartender"),
            row(FRI, "Pat", "Bartender"), row(FRI, "Casey", "Bartender")]
    out = sq.compare_candidates(
        rows, profiles=profiles(),
        scores={"Pat": 5, "Casey": 5, "Alex": 2})
    for swap in out["swaps"]:
        moved = {swap["from"]["employee"], swap["to"]["employee"]}
        assert "Unrated" not in moved, swap["reason"]


def test_two_unrated_people_may_still_trade_with_each_other():
    """The rule is about comparing rated against unrated, not about freezing
    unrated staff in place."""
    rows = [row(SAT, "GhostA", "Bartender"), row(FRI, "GhostB", "Bartender"),
            row(SAT, "Pat", "Bartender"), row(FRI, "Casey", "Bartender")]
    assert sq._swap_is_legal(rows, 0, 1, {}, {"Pat": 5, "Casey": 4}) is True
    assert sq._swap_is_legal(rows, 0, 2, {}, {"Pat": 5, "Casey": 4}) is False


# ── Dormancy: the scheduler itself is untouched until somebody is rated ────

def test_the_generated_prompt_is_unchanged_when_nobody_is_rated():
    """The whole feature was promised as dormant. A restaurant that has
    rated nobody and configured no profiles must produce the same prompt it
    produced before any of this existed — scoring the result afterwards is
    a separate matter and costs the schedule nothing."""
    import labor
    assert labor.format_profile_block([]) == ""
    assert labor.format_profile_block(None) == ""


def test_a_profile_block_says_what_each_shift_is_judged_on():
    import labor
    block = labor.format_profile_block([
        sq.ShiftProfile(key="sat", label="Saturday dinner", days=["Saturday"],
                        daypart="night", demand="peak", min_quality=82,
                        requires_leader=True, min_strength={"Bartender": 10},
                        critical_positions={"Cook": 2}, training_allowed=False),
        sq.ShiftProfile(key="mon", label="Monday lunch", days=["Monday"],
                        daypart="morning", demand="low", min_quality=60,
                        training_allowed=True)])
    assert "Saturday dinner" in block and "peak demand" in block
    assert "Bartender 10+" in block and "2 Cook" in block
    assert "training shift" in block
    # The model is told to optimise the whole shift, not one rule.
    assert "OVERALL quality" in block


def test_the_engine_scores_coverage_and_labor_without_any_ratings():
    """Not every dimension needs a rating. A restaurant that has rated
    nobody still gets a real answer about whether its positions are filled
    and whether it is over its hours — and low confidence saying why."""
    rows = [row(SAT, "A", "Cook"), row(SAT, "B", "Server")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="std", critical_positions={"Cook": 2, "Server": 1}, source="restaurant")],
        daily_target_hours={SAT: 16})
    assert out["checked"]
    keys = {d["key"] for d in out["shifts"][0]["dimensions"]}
    assert "coverage" in keys and "operational_strength" not in keys
    assert out["confidence"]["level"] in ("moderate", "low")


def test_adding_one_profile_does_not_drop_the_targets_everywhere_else():
    """The contract is: the flat targets are the baseline and a profile
    overrides it. That only holds if the baseline is reachable from every
    shift. Without a catch-all, an owner who added a single Monday-lunch
    profile silently lost their per-role targets for the rest of the week."""
    only_monday = [sq.ShiftProfile(key="mon", label="Monday lunch", days=["Monday"],
                                   daypart="morning", source="restaurant")]
    resolved = sq.profiles_from_config(only_monday, default_strength={"Bartender": 8})
    saturday = sq.resolve_profile("Saturday", "night", resolved)
    assert saturday.min_strength == {"Bartender": 8}

    rows = [row(SAT, "Sam", "Bartender"), row(SAT, "Alex", "Bartender")]
    out = sq.score_rows(rows, profiles=resolved, scores={"Sam": 2, "Alex": 2})
    strength = next(d for d in out["shifts"][0]["dimensions"]
                    if d["key"] == "operational_strength")
    assert strength["score"] == 50


def test_a_restaurants_own_catch_all_is_not_duplicated():
    mine = [sq.ShiftProfile(key="house", label="House standard", source="restaurant")]
    resolved = sq.profiles_from_config(mine, default_strength={"Cook": 6})
    catch_alls = [p for p in resolved if not p.days and not p.daypart]
    assert len(catch_alls) == 1
    assert catch_alls[0].key == "house"
    assert catch_alls[0].min_strength == {"Cook": 6}


# ── Both surfaces render it ────────────────────────────────────────────────
#
# The recurring failure across every audit in this codebase is a figure
# computed carefully, returned in the payload, and decoded by nothing.
# Comments are stripped before asserting, so a name that appears only in a
# comment explaining the feature cannot satisfy the check.

def _source(*parts):
    import pathlib
    return pathlib.Path(__file__).resolve().parent.parent.joinpath(*parts).read_text()


def _no_comments(text):
    body = "\n".join(line for line in text.split("\n")
                     if not line.strip().startswith("//"))
    while "<!--" in body and "-->" in body[body.index("<!--"):]:
        start = body.index("<!--")
        body = body[:start] + body[body.index("-->", start) + 3:]
    return body


def test_ios_decodes_and_renders_shift_quality():
    model = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift"))
    for field in ("struct ScheduleQuality", "struct QualityConfidence",
                  "struct ScheduleWhatIf", "var quality: ScheduleQuality?",
                  "below_profile", "what_if"):
        assert field in model, f"{field} is returned by the backend and decoded by nothing"
    view = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift"))
    # The call site, not the declaration — a panel nothing mounts reads
    # exactly like one that is wired in.
    assert "ShiftQualityPanel(quality: quality, whatIf: result.whatIf," in view
    panel = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/ShiftQualityPanel.swift"))
    for piece in ("confidencePill", "recommendations", "Why this schedule?",
                  "blindSpots", "cappedBy", "whatIf"):
        assert piece in panel, piece


def test_ios_shows_what_the_generator_is_doing_rather_than_a_spinner():
    view = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift"))
    assert "ScheduleProgressSteps()" in view
    steps = _source("ios/CavnarAI/CavnarAI/Features/Labor/ScheduleProgressSteps.swift")
    for stage in ("available", "labor target", "operational scores",
                  "leadership", "strongest team", "quality"):
        assert stage in steps, stage


def test_ios_lets_a_manager_move_somebody_and_see_the_score_change():
    model = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift"))
    assert "labor/schedule/score" in model
    assert "func overrideEmployee" in model
    # Eligibility is the server's answer, not a third hand-rolled copy of
    # the rule that had already drifted apart across three surfaces.
    assert "labor/schedule/replacements" in model and "func loadReplacements" in model
    view = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift"))
    assert "shiftRowWithOverride" in view
    assert "viewModel.overrideEmployee(rowId: row.id, to: member.name)" in view


def test_the_web_renders_shift_quality():
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "renderShiftQuality(data.quality, data.what_if);" in html
    for piece in ('id="sched-quality"', "renderQualityConfidence", "renderQualityWarnings",
                  "renderQualityShifts", "renderQualityReasoning", "_qualityDial"):
        assert piece in html, piece


def test_the_web_saves_a_manager_edit_rather_than_only_scoring_it():
    """Without the save the edit lived in the page, the score moved, and
    publishing read the CSV written at generation time — so staff received
    the week the manager had just fixed, unfixed."""
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "'/api/labor/schedule/score'" in html
    assert "save: true" in html
    assert "function applyShiftSwap" in html and "function rescoreSchedule" in html
    # Eligibility comes from the one endpoint both surfaces call.
    assert "'/api/labor/schedule/replacements'" in html
    assert "function _replacementsFor" not in html
    # And a failed save must not leave the old number looking current.
    assert "function _setQualityState" in html and "out of date" in html


def test_the_web_shows_the_generation_stages():
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "startScheduleSteps(_schedStart);" in html
    assert "stopScheduleSteps" in html


def test_the_web_can_edit_profiles_and_weighting():
    html = _no_comments(_source("templates", "dashboard.html"))
    for piece in ("loadShiftProfiles", "'/api/labor/profiles'",
                  "'/api/labor/quality-weights'", "function saveShiftProfile",
                  "function renderWeightList", 'id="sp-list"', 'id="sp-weights"'):
        assert piece in html, piece


def test_the_week_summary_names_the_pattern_not_every_shifts_own_lines():
    """Each shift already carries its own reasons. Repeating them at week
    level put the same sentence on screen twice for a manager to read
    twice, which reads as noise rather than as intelligence."""
    common = [row(d, "Weak", "Bartender") for d in (MON, TUE, WED, THU, FRI)]
    common += [row(d, "Other", "Bartender") for d in (MON, TUE, WED, THU, FRI)]
    out = sq.score_rows(common, profiles=[sq.ShiftProfile(
        key="n", label="Night", daypart="night", min_strength={"Bartender": 9},
        source="restaurant")], scores={"Weak": 1, "Other": 1})
    assert out["weaknesses"]
    assert not any(line.startswith("Monday") for line in out["weaknesses"]), out["weaknesses"]


def test_a_week_with_no_repeated_problem_still_says_something_specific():
    rows = [row(SAT, "Strong", "Bartender"), row(SAT, "Weak", "Bartender"),
            lunch(MON, "Strong", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="n", label="Any", min_strength={"Bartender": 9}, source="restaurant")],
        scores={"Strong": 5, "Weak": 1})
    assert out["weaknesses"]
    # Named to its shift, so the manager knows where to look.
    assert any(":" in line for line in out["weaknesses"])


def test_both_surfaces_group_the_reasons_rather_than_listing_them_flat():
    """Eight identically-marked lines at one indent is the wall of text the
    design brief rules out. Both surfaces group them and nest them behind a
    rail in the shift's own tone."""
    panel = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/ShiftQualityPanel.swift"))
    assert 'group("Working well"' in panel and 'group("Holding it back"' in panel
    assert "QualityBar(score: shift.score ?? 0" in panel
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "_qualityGroup('Working well'" in html and "_qualityGroup('Holding it back'" in html


def test_the_web_tone_helper_returns_only_hex():
    """Every caller builds a gradient by appending an alpha suffix. An
    rgba() value there produces invalid CSS and a bar with no fill, which
    is how the best shift of the week rendered blank."""
    html = _source("templates", "dashboard.html")
    body = html[html.index("function _qTone("):]
    body = body[:body.index("\n}")]
    assert "rgba" not in body, body
    import re as _re
    assert len(_re.findall(r"#[0-9a-f]{6}", body)) == 4


def test_leadership_withdraws_when_nobody_is_rated_rather_than_scoring_zero():
    """A requirement phrased in scores cannot be judged by a restaurant that
    has rated nobody. Answering it "not met" is a zero, and leadership
    carries a floor — so one unconfigured built-in profile capped a
    perfectly good Saturday at nothing, on a fact the owner never supplied."""
    rows = saturday({"Bartender": ["A", "B"], "Cook": ["C", "D"], "Server": ["E", "F"]})
    out = sq.score_rows(rows, profiles=sq.BUILTIN_PROFILES,
                        role_minimums={"Bartender": 2, "Cook": 2, "Server": 2},
                        leader_rules=[{"role": "Bartender", "days": ["Saturday"],
                                       "daypart": "night", "min_score": 5}])
    shift = out["shifts"][0]
    assert "leadership" in shift["not_applicable"]
    assert shift["capped_by"] != "leadership"
    assert shift["score"] == 100, shift["score"]
    # Withdrawing silently would leave the owner never learning that rating
    # somebody unlocks the check, so the reason survives the withdrawal.
    assert any("Leadership was not checked" in b for b in shift["blind_spots"])


def test_leadership_is_judged_normally_once_anybody_is_rated():
    """One rating is enough to make the question answerable. An unrated
    person genuinely does not clear a 5, and that is a real finding."""
    rows = saturday({"Bartender": ["A", "B"]})
    out = sq.score_rows(rows, profiles=profiles(), scores={"A": 3},
                        leader_rules=[{"role": "Bartender", "days": ["Saturday"],
                                       "daypart": "night", "min_score": 5}])
    leadership = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "leadership")
    assert leadership["score"] == 0
    assert leadership["facts"]["misses"]


def test_a_headcount_only_leader_rule_is_answerable_without_ratings():
    """"At least one bartender on" needs no score at all, so it must still
    be checked by a restaurant that has rated nobody."""
    rows = saturday({"Cook": ["C"]})
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="std", source="restaurant")],
                        role_minimums={"Cook": 1},
                        leader_rules=[{"role": "Bartender", "count": 1}])
    leadership = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "leadership")
    assert leadership["score"] == 0
    assert leadership["facts"]["misses"][0]["rule"] == "1 bartender"


def test_a_closer_flag_alone_makes_leadership_answerable():
    """can_close is a fact about a person that owes nothing to ratings."""
    rows = saturday({"Bartender": ["A", "B"]})
    out = sq.score_rows(rows, profiles=sq.BUILTIN_PROFILES,
                        role_minimums={"Bartender": 2}, leader_flags={"A": True})
    leadership = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "leadership")
    assert leadership["score"] == 100


def test_the_fairness_wording_names_the_shifts_rather_than_calling_them_premium():
    """"Premium shift" is scheduling jargon. An owner reading this should
    not have to ask which shifts it means."""
    rows = [row(d, "Favourite", "Server") for d in (THU, FRI, SAT, SUN)]
    rows += [row(MON, "Ignored", "Server"), row(TUE, "Ignored", "Server"),
             row(MON, "Third", "Server")]
    setup = [sq.ShiftProfile(key="nights", demand="peak", daypart="night",
                             label="Nights", source="restaurant"),
             sq.ShiftProfile(key="days", demand="low", daypart="morning",
                             label="Days", source="restaurant")]
    out = sq.score_rows(rows, profiles=setup,
                        scores={"Favourite": 4, "Ignored": 4, "Third": 4},
                        role_minimums={"Server": 1})
    text = " ".join(out["weaknesses"] + out["strengths"])
    text += " ".join(w for s in out["shifts"] for w in s["weaknesses"] + s["strengths"])
    assert "premium" not in text.lower(), text
    assert "busiest shifts" in text


def test_the_ios_quality_panel_reads_at_the_labor_tabs_own_type_scale():
    """The panel shipped a full step under the rest of the Labor tab, whose
    body text sits at 14-15, and the expanded shift detail was the worst of
    it. Only tracked uppercase micro-labels and the chip numerals under them
    are allowed below 12."""
    import re
    src = _source("ios/CavnarAI/CavnarAI/Features/Labor/ShiftQualityPanel.swift")
    small = []
    for line_no, line in enumerate(src.split("\n"), 1):
        for match in re.finditer(r"\.cavnar(?:Body|Number)\(([0-9.]+)", line):
            size = float(match.group(1))
            if size < 12 and "tracking" not in src.split("\n")[line_no]:
                small.append((line_no, size, line.strip()))
    # The two allowed: the "/100" under the dial's own numeral, and the
    # five-across dimension chip labels that sit directly beneath theirs.
    # Both are sub-labels of a figure the reader has already read.
    assert len(small) <= 2, small
    assert all(size >= 11 for _line, size, _text in small), small
    assert ".cavnarBody(14))" in src, "detail lines should sit at the module's body size"


# ── Saying the same thing seven times ─────────────────────────────────────

def test_a_line_true_of_the_whole_week_appears_once_not_on_every_shift():
    """Seven shifts reading like the same paragraph seven times. The three
    worst offenders in practice — a fully staffed roster, a role sitting the
    same distance under target every night, and a fatigue warning about
    somebody who works every day — are all week-level facts."""
    rows = []
    for d in (MON, TUE, WED, THU, FRI, SAT, SUN):
        rows += [row(d, "Weak", "Bartender"), row(d, "Other", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="n", label="Night", min_strength={"Bartender": 9}, source="restaurant")],
        scores={"Weak": 1, "Other": 1})
    for shift in out["shifts"]:
        assert not any("Bartender strength" in w for w in shift["weaknesses"]), shift
        assert shift.get("nothing_specific")
    assert any("Bartender strength" in w for w in out["weaknesses"])


def test_a_line_specific_to_one_shift_stays_on_that_shift():
    """The point is to leave each shift saying what is DIFFERENT about it,
    not to empty the sections out."""
    rows = []
    for d in (MON, TUE, WED, THU, FRI, SAT):
        rows += [row(d, "Pat", "Bartender"), row(d, "Casey", "Bartender")]
    # One night only, the weak pair works instead.
    rows += [row(SUN, "Weak1", "Bartender"), row(SUN, "Weak2", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="n", label="Night", min_strength={"Bartender": 8}, source="restaurant")],
        scores={"Pat": 5, "Casey": 4, "Weak1": 1, "Weak2": 1})
    by_day = {s["day"]: s for s in out["shifts"]}
    assert any("Bartender strength" in w for w in by_day["Sunday"]["weaknesses"])
    assert not by_day["Monday"]["weaknesses"]


def test_a_two_shift_coincidence_is_not_treated_as_a_weekly_pattern():
    rows = [row(SAT, "Weak1", "Cook"), row(SUN, "Weak1", "Cook"),
            row(MON, "Chef", "Cook"), row(TUE, "Chef", "Cook"),
            row(WED, "Chef", "Cook"), row(THU, "Chef", "Cook"),
            row(FRI, "Chef", "Cook")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="n", label="Night", min_strength={"Cook": 4}, source="restaurant")],
        scores={"Weak1": 1, "Chef": 5})
    weak_days = [s for s in out["shifts"] if s["day"] in ("Saturday", "Sunday")]
    assert all(s["weaknesses"] for s in weak_days), weak_days


def test_a_short_week_is_never_hoisted_at_all():
    """Below three shifts there is no such thing as a weekly pattern."""
    rows = [row(SAT, "Weak", "Cook"), row(SUN, "Weak", "Cook")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="n", min_strength={"Cook": 8}, source="restaurant")], scores={"Weak": 1})
    assert all(s["weaknesses"] for s in out["shifts"])


def test_a_partial_pattern_keeps_its_count_and_a_total_one_does_not():
    """"5 of 7 shifts" is the whole point of that line. "Pat works 7 days in
    a row this week — every shift this week" is a sentence arguing with
    itself."""
    rows = []
    for d in (MON, TUE, WED, THU, FRI, SAT, SUN):
        rows += [row(d, "Weak1", "Cook"), row(d, "Weak2", "Cook")]
    # A bartender short on five of the seven, present on the other two.
    for d in (MON, TUE, WED, THU, FRI):
        rows += [row(d, "Solo", "Bartender")]
    for d in (SAT, SUN):
        rows += [row(d, "Solo", "Bartender"), row(d, "Second", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(
        key="n", label="Night", source="restaurant")],
        role_minimums={"Cook": 2, "Bartender": 2},
        scores={"Weak1": 1, "Weak2": 1, "Solo": 3, "Second": 3})
    partial = [w for w in out["weaknesses"] if "Bartender" in w]
    assert partial and "5 of 7 shifts" in partial[0], out["weaknesses"]
    # The cook problem is true of all seven, so it carries no count.
    assert any("cook" in w.lower() and not w.endswith("shifts")
               for w in out["weaknesses"]), out["weaknesses"]


def test_the_week_summary_is_the_same_on_every_run():
    """Ties were broken by a set's iteration order, and Python randomises
    string hashing per process — so the findings that survived truncation
    differed on every page load and a manager refreshing watched them
    reshuffle. Run in separate interpreters, which is where it showed."""
    import json as _json
    import subprocess
    import sys
    import textwrap
    script = textwrap.dedent('''
        import json, sys
        sys.path.insert(0, %r)
        import shift_quality as sq
        rows = []
        for d in ("2026-09-07","2026-09-08","2026-09-09","2026-09-10",
                  "2026-09-11","2026-09-12","2026-09-13"):
            for n in ("Weak1", "Weak2"):
                rows.append({"date": d, "day": "", "employee": n, "role": "Cook",
                             "shift_start": "5:00pm", "shift_end": "11:00pm",
                             "scheduled_hours": 8})
        out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                            role_minimums={"Cook": 3}, scores={"Weak1": 1, "Weak2": 1})
        print(json.dumps(out["weaknesses"]))
    ''') % str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    runs = {tuple(_json.loads(subprocess.run([sys.executable, "-c", script],
                                             capture_output=True, text=True).stdout))
            for _ in range(4)}
    assert len(runs) == 1, runs


def test_findings_are_ordered_by_what_they_actually_cost_the_shift():
    """Two thirds of the shift unstaffed outranks one isolated employee, and
    score-first ordering had it the other way round — which pushed the
    coverage gap off the end of a truncated summary entirely. The cost is
    how far under a dimension is, times how much it counts."""
    rows = saturday({"Cook": ["Rookie"], "Bartender": ["Pat"]})
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                        role_minimums={"Cook": 2, "Bartender": 2, "Server": 2},
                        scores={"Rookie": 1, "Pat": 5, "Other": 5})
    weaknesses = out["shifts"][0]["weaknesses"]
    assert "unfilled" in weaknesses[0], weaknesses
    # And the lighter finding is still there, just lower down.
    assert any("nobody stronger" in w for w in weaknesses), weaknesses


# ── Audit fixes ────────────────────────────────────────────────────────────
#
# One test per finding from the production audit, named for the failure it
# prevents rather than for the function it calls.

def test_a_person_in_two_roles_at_once_is_counted_once(db_path=None):
    """P1-9. Somebody listed as Cook and Bartender at five o'clock is one
    person who cannot be in two places, and counting them twice scored a
    physically impossible shift as fully covered."""
    rows = [row(SAT, "Solo", "Cook"), row(SAT, "Solo", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                        role_minimums={"Cook": 1, "Bartender": 1}, scores={"Solo": 5})
    coverage = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert coverage["score"] == 50, coverage["facts"]
    assert coverage["facts"]["role_conflicts"]
    assert any("at the same time" in w for w in out["shifts"][0]["weaknesses"])


def test_a_row_the_repair_pass_flagged_is_not_counted_as_coverage():
    """P1-9. needs_review never reached the engine, so a row the pipeline
    had already refused to vouch for still filled a position."""
    rows = [row(SAT, "A", "Cook"), row(SAT, "Ghost", "Cook")]
    flagged = {("ghost", SAT, "5:00pm")}
    clean = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                          role_minimums={"Cook": 2}, scores={"A": 4, "Ghost": 4})
    marked = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                           role_minimums={"Cook": 2}, scores={"A": 4, "Ghost": 4},
                           flagged=flagged)
    assert clean["shifts"][0]["score"] == 100
    assert marked["shifts"][0]["capped_by"] == "coverage"


def test_a_dimension_that_crashes_is_reported_not_silently_dropped():
    """P1-4. A crash used to be indistinguishable from an unconfigured
    dimension, so coverage failing turned a capped 50 into a clean 100."""
    original = sq.DIMENSIONS["coverage"]
    def boom(_ctx):
        raise RuntimeError("boom")
    sq.DIMENSIONS["coverage"] = boom
    try:
        out = sq.score_rows([row(SAT, "A", "Cook")],
                            profiles=[sq.ShiftProfile(key="n", min_strength={"Cook": 4},
                                                      source="restaurant")],
                            scores={"A": 5}, role_minimums={"Cook": 3})
    finally:
        sq.DIMENSIONS["coverage"] = original
    shift = out["shifts"][0]
    assert [f["key"] for f in shift["failed"]] == ["coverage"]
    assert "coverage" not in shift["not_applicable"]
    assert any("could not be worked out" in b for b in shift["blind_spots"])
    assert any("could not be" in r for r in out["confidence"]["reasons"])


def test_the_verdict_only_claims_the_roster_is_best_when_it_checked_all_of_it():
    """P1-5. Sixty evaluations out of sixteen thousand legal swaps was the
    most confident sentence on the panel and the least supported."""
    big = []
    for i in range(40):
        for k in range(2):
            big.append(row((MON, TUE, WED, THU, FRI, SAT, SUN)[(i + k * 3) % 7],
                           f"E{i}", "Server"))
    out = sq.compare_candidates(big, profiles=[sq.ShiftProfile(
        key="n", min_strength={"Server": 30}, source="restaurant")],
        scores={f"E{i}": (i % 5) + 1 for i in range(40)}, max_evaluations=5)
    assert out["legal_swaps"] > out["evaluated"]
    assert "strongest team available" not in out["verdict"]
    assert "A wider search might still find something" in out["verdict"]

    small = [row(SAT, "Pat", "Bartender"), row(FRI, "Casey", "Bartender")]
    tiny = sq.compare_candidates(small, profiles=profiles(),
                                 scores={"Pat": 5, "Casey": 5})
    assert "strongest team available" in tiny["verdict"]


def test_a_swap_never_moves_somebody_carrying_a_staff_constraint():
    """P1-6. The prompt calls staff constraints the highest-priority rule of
    all, and the what-if pass could not see them — so it recommended, with a
    score improvement attached, swaps that broke one."""
    setup = [sq.ShiftProfile(key="sat", label="Saturday dinner", days=["Saturday"],
                             daypart="night", demand="peak",
                             min_strength={"Bartender": 8}, priority=2,
                             source="restaurant"),
             sq.ShiftProfile(key="std", source="restaurant")]
    rows = [row(SAT, "Sam", "Bartender"), row(FRI, "Pat", "Bartender")]
    kw = dict(profiles=setup, scores={"Pat": 5, "Sam": 2},
              leader_rules=[{"role": "Bartender", "days": ["Saturday"],
                             "daypart": "night", "min_score": 5}])
    free = sq.compare_candidates(rows, **kw)
    bound = sq.compare_candidates(rows, constraints={"Pat": "no weekends"}, **kw)
    assert free["swaps"], free["verdict"]
    assert not bound["swaps"]
    assert bound["evaluated"] == 0


def test_a_closing_requirement_binds_the_closing_shift_and_no_other():
    """P1-2. Documented in the code and implemented nowhere; the engine read
    it as a plain headcount rule and demanded a closer at breakfast."""
    rows = [lunch(SAT, "Morning", "Server"), row(SAT, "Night", "Server")]
    rule = [{"closing": True, "role": "Server", "attribute": "can_close", "count": 1}]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                        scores={"Morning": 4, "Night": 4}, leader_rules=rule)
    by_part = {s["daypart"]: s for s in out["shifts"]}
    assert "leadership" in by_part["morning"]["not_applicable"]
    night = next(d for d in by_part["night"]["dimensions"] if d["key"] == "leadership")
    assert night["score"] == 0
    assert "authorised to close" in night["facts"]["misses"][0]["rule"]


def test_a_closer_flag_satisfies_a_closing_requirement():
    """P1-2 and P1-3. The capability the flag represents is the whole point:
    an experienced closer rated 3 could never qualify on score alone."""
    rows = [row(SAT, "Night", "Server")]
    rule = [{"closing": True, "role": "Server", "attribute": "can_close", "count": 1}]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                        scores={"Night": 3}, leader_flags={"Night": True},
                        leader_rules=rule)
    leadership = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "leadership")
    assert leadership["score"] == 100


def test_fatigue_counts_the_days_worked_before_this_week_began():
    """P1-15. A run of nine days read as five, because the engine could only
    see inside its own seven-day box — and the week boundary is exactly
    where that matters."""
    rows = [row(d, "Nonstop", "Cook") for d in (MON, TUE, WED, THU, FRI)]
    rows += [row(d, "Other", "Cook") for d in (MON, TUE)]
    prior = {"Nonstop": [{"date": "2026-09-05", "daypart": "night",
                          "day": "Saturday", "demand": "normal"},
                         {"date": "2026-09-06", "daypart": "night",
                          "day": "Sunday", "demand": "normal"}]}
    setup = dict(profiles=[sq.ShiftProfile(key="n", min_strength={"Cook": 4},
                                           source="restaurant")],
                 scores={"Nonstop": 4, "Other": 4})
    narrow = sq.score_rows(rows, **setup)
    wide = sq.score_rows(rows, prior_week_assignments=prior, **setup)
    assert not any("in a row" in w for w in narrow["weaknesses"])
    assert any("7 days in a row" in w for w in wide["weaknesses"])


def test_somebody_on_another_sites_schedule_is_not_counted_as_coverage_here():
    """P2-7. Employees are name-keyed per restaurant, which is the right
    isolation — and meant nothing anywhere noticed when two sites of the
    same group put the same person on the same night."""
    rows = [row(SAT, "Shared", "Bartender"), row(SAT, "Local", "Bartender")]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", source="restaurant")],
                        role_minimums={"Bartender": 2}, scores={"Shared": 4, "Local": 4},
                        elsewhere={"Shared": [{"date": SAT, "location": "Lincoln Park"}]})
    coverage = next(d for d in out["shifts"][0]["dimensions"] if d["key"] == "coverage")
    assert coverage["score"] <= 60
    assert any("Lincoln Park" in w for w in out["shifts"][0]["weaknesses"])


def test_every_profile_field_is_bounded():
    """P1-13. A negative strength target saved happily and was then always
    met, so an owner believed a bar was enforced when it was inert."""
    p = sq.profile_from_dict({
        "key": "x", "min_strength": {"Cook": -50, "Server": 8},
        "experience_mix": 9.0, "leader_min_score": 99, "min_quality": 900,
        "critical_positions": {"Cook": -3, "Server": 2},
        "weights": {"coverage": 10 ** 9, "not_a_dimension": 5},
        "demand": "apocalyptic"})
    assert p.min_strength == {"Server": 8.0}
    assert p.experience_mix == 1.0
    assert p.leader_min_score == sq.SCORE_SCALE_MAX
    assert p.min_quality == 100
    assert p.critical_positions == {"Server": 2}
    assert p.weights == {"coverage": float(sq.MAX_WEIGHT)}
    assert p.demand == "normal"


def test_a_finding_that_carries_a_number_still_deduplicates():
    """P3-1. String matching left "81h under target" on two shifts while the
    summary reported "120h under target" for five — the same problem,
    presented as two."""
    rows = []
    for i, d in enumerate((MON, TUE, WED, THU, FRI, SAT, SUN)):
        rows.append(row(d, "A", "Cook", hours=6 + i))
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="n", min_strength={"Cook": 4},
                                                        source="restaurant")],
                        scores={"A": 5},
                        daily_target_hours={d: 40 for d in (MON, TUE, WED, THU, FRI, SAT, SUN)})
    per_shift = [w for s in out["shifts"] for w in s["weaknesses"] if "under target" in w]
    assert per_shift == [], per_shift
    assert any("under target" in w for w in out["weaknesses"])


def test_the_download_serves_the_stored_schedule_rather_than_generating_a_new_one():
    """P0-2. It called _build_schedule_result, which runs the generator
    again — so the CSV an owner printed was a different week from the one on
    screen, carried none of the review they had just worked through, and
    billed a second model call every press."""
    src = _source("client_api.py")
    body = src[src.index("def download_schedule(current_user):"):]
    body = body[:body.index("@client_bp.route")]
    assert "_build_schedule_result" not in body.split('"""')[2], body[:400]
    assert "get_schedule_history_detail" in body


def test_the_stale_job_sweep_exists_and_runs_at_boot():
    """P1-14. Generation runs on a daemon thread, which is killed at
    interpreter exit without running its finally blocks — so a deploy
    mid-generation left the row pending forever and lost a paid call."""
    assert "def sweep_stale_jobs(" in _source("ops.py")
    boot = _source("hosted_dashboard.py")
    assert "sweep_stale_jobs()" in boot


def test_both_surfaces_can_mark_somebody_authorised_to_close():
    """P1-3. The capability was registered from day one and reachable from
    no interface, so a leadership rule could only ever be answered by a
    score and an experienced closer rated 3 never qualified."""
    panel = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/TeamStrengthSection.swift"))
    assert "closerToggle(member)" in panel
    model = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift"))
    assert "func setCloser" in model and 'attribute = "can_close"' in model
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "function setTeamCloser" in html and "data-team-close" in html


def test_both_surfaces_say_what_the_explanation_is_not():
    """P2-6. The engine can say why a shift scored what it did. It cannot
    say why the AI chose one person over another, and should not imply it."""
    for text in (_source("ios/CavnarAI/CavnarAI/Features/Labor/ShiftQualityPanel.swift"),
                 _source("templates", "dashboard.html")):
        assert "does not record why the AI" in text
