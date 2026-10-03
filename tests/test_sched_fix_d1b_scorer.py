"""Schedule audit 10/3/26, workstream D1b — the scorer's other dimensions.

Each test names the finding it pins and reproduces the audit's own case;
each one fails on the scorer as it was before the fix round.
"""
import shift_quality as sq

# 2026-10-05 is a Monday.
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def row(date, name, role, start="5:00pm", end="11:00pm", hours=6):
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def dim(shift, key):
    return next((d for d in shift["dimensions"] if d["key"] == key), None)


def week_dim(out, key):
    return next((d for d in out["week_dimensions"] if d["key"] == key), None)


def shift_on(out, date, part="night"):
    return next(s for s in out["shifts"] if s["date"] == date and s["daypart"] == part)


STD = [sq.ShiftProfile()]


# ── SQ-9: training balance at a one-person station ────────────────────────

SAT_TEAM = [row(SAT, "Ana", "Server"), row(SAT, "Bo", "Server"), row(SAT, "Cy", "Line Cook"),
            row(SAT, "Dee", "Dishwasher")]
SAT_NEED = {("Saturday", "night"): {"Server": 2, "Line Cook": 1, "Dishwasher": 1}}
SAT_SCORES = {"Ana": 4, "Bo": 4, "Cy": 4, "Dee": 2}


def test_sq9_the_manager_on_the_floor_backs_a_lone_dishwasher_rated_2():
    """The audit's case: a fully staffed shift, a lone dishwasher rated 2,
    training 0 on every shift they worked — no second dishwasher can ever
    be beside them. The manager running the floor is that station's mentor."""
    rows = SAT_TEAM + [row(SAT, "Mia", "Manager")]
    kw = dict(profiles=STD, scores=SAT_SCORES, typical_headcount=SAT_NEED)
    without = dim(sq.score_rows(rows, **kw)["shifts"][0], "training_balance")
    assert without["score"] == 0 and without["facts"]["isolated_names"] == ["Dee on dishwasher"]
    out = sq.score_rows(rows, managers={"mia": "Manager"}, **kw)
    tb = dim(out["shifts"][0], "training_balance")
    assert tb["score"] == 100
    assert tb["facts"]["mentors"] == [{"name": "Dee", "mentor": "Mia", "how": "manager", "role": "Dishwasher"}]
    assert tb["facts"]["one_person_stations"] == ["Dishwasher"]
    assert any("one-person station" in s and "Mia" in s for s in out["shifts"][0]["strengths"])
    # someone standing in as the manager that night counts the same way
    acting = sq.score_rows(SAT_TEAM, acting_managers={"cy": {SAT}}, **kw)
    assert dim(acting["shifts"][0], "training_balance")["facts"]["mentors"][0]["how"] == "manager"


def test_sq9_a_cross_trained_senior_on_the_floor_mentors_the_station():
    kw = dict(profiles=STD, scores=SAT_SCORES, typical_headcount=SAT_NEED)
    out = sq.score_rows(SAT_TEAM, cross_trained={"Cy": ["Line Cook", "Dishwasher"]}, **kw)
    tb = dim(out["shifts"][0], "training_balance")
    assert tb["score"] == 100 and tb["facts"]["mentors"][0]["how"] == "cross_role"
    assert tb["facts"]["mentors"][0]["mentor"] == "Cy"
    held = sq.score_rows(SAT_TEAM, held_roles={"cy": {"dishwasher"}}, **kw)
    assert dim(held["shifts"][0], "training_balance")["facts"]["mentors"][0]["mentor"] == "Cy"


def test_sq9_a_station_built_for_two_still_needs_a_stronger_hand_in_it():
    """The manager stands in only where the station is one person by design:
    two servers needed, the weak one beside a 3 is still on their own."""
    rows = [row(SAT, "Ana", "Server"), row(SAT, "Bo", "Server"), row(SAT, "Mia", "Manager")]
    out = sq.score_rows(rows, profiles=STD, scores={"Ana": 2, "Bo": 3},
                        typical_headcount={("Saturday", "night"): {"Server": 2}}, managers={"mia": "Manager"})
    tb = dim(out["shifts"][0], "training_balance")
    assert tb["score"] == 0 and tb["facts"]["isolated_names"] == ["Ana on server"]


def test_sq9_server_am_and_server_pm_on_one_shift_are_one_station():
    rows = [row(SAT, "Ana", "Server AM", "3:00pm", "9:00pm"), row(SAT, "Bo", "Server PM")]
    out = sq.score_rows(rows, profiles=STD, scores={"Ana": 2, "Bo": 4},
                        typical_headcount={("Saturday", "night"): {"Server AM": 1, "Server PM": 1}})
    tb = dim(out["shifts"][0], "training_balance")
    assert tb["score"] == 100 and tb["facts"]["mentors"][0]["how"] == "same_role"


# ── SQ-10: cross-training by role family ──────────────────────────────────

def test_sq10_working_server_am_and_server_pm_is_not_cross_training():
    rows = [row(SAT, "Ana", "Server PM"), row(SAT, "Bo", "Server PM"), row(SAT, "Cal", "Bartender")]
    cross = {"Ana": ["Server AM", "Server PM"], "Cal": ["Bartender", "Server PM"]}
    out = sq.score_rows(rows, profiles=STD, cross_trained=cross,
                        typical_headcount={("Saturday", "night"): {"Server PM": 2, "Bartender": 1}})
    ct = dim(out["shifts"][0], "cross_training")
    assert ct["facts"]["flexible"] == ["Cal"]


def test_sq10_a_role_held_beyond_the_roster_role_makes_somebody_flexible():
    rows = [row(SAT, "Ana", "Server"), row(SAT, "Bo", "Server")]
    out = sq.score_rows(rows, profiles=STD, cross_trained={"Zed": ["Server", "Host"]},
                        held_roles={"bo": {"bartender"}}, roster_roles={"Ana": "Server", "Bo": "Server"},
                        typical_headcount={("Saturday", "night"): {"Server": 2}})
    assert dim(out["shifts"][0], "cross_training")["facts"]["flexible"] == ["Bo"]


def test_sq10_a_role_nobody_who_works_it_can_flex_is_not_judged():
    """The audit's repro scored cross-training 0 on a shift of people who all
    hold one station: a fact about training, never about this week."""
    rows = [row(SAT, "Dee", "Dishwasher"), row(SAT, "Eve", "Dishwasher")]
    out = sq.score_rows(rows, profiles=STD, cross_trained={"Zed": ["Server", "Bartender"]},
                        typical_headcount={("Saturday", "night"): {"Dishwasher": 2}})
    shift = out["shifts"][0]
    assert "cross_training" in shift["not_applicable"]
    assert any("Cross-training was not judged for dishwashers" in b for b in shift["blind_spots"])
    # ...and once somebody who works dish can flex, the shift is judged on it
    judged = sq.score_rows(rows, profiles=STD, cross_trained={"Zed": ["Dishwasher", "Prep Cook"]},
                           typical_headcount={("Saturday", "night"): {"Dishwasher": 2}})
    assert dim(judged["shifts"][0], "cross_training")["score"] == 0


# ── SQ-11: fairness withdraws when nobody could be compared ───────────────

def test_sq11_a_three_person_week_has_no_phantom_fairness_100():
    rows = [row(d, n, r) for d in (FRI, SAT, SUN) for n, r in (("Ana", "Server"), ("Bo", "Server"), ("Cy", "Cook"))]
    out = sq.score_rows(rows, profiles=[sq.ShiftProfile(demand="peak", source="restaurant")],
                        typical_headcount={(d, "night"): {"Server": 2, "Cook": 1}
                                           for d in ("Friday", "Saturday", "Sunday")})
    for s in out["shifts"]:
        assert "fairness" in s["not_applicable"], s["dimensions"]


# ── SQ-15: the built-ins are judged with the restaurant's own sales ───────

def test_sq15_a_quiet_saturday_is_not_peak_with_an_82_bar_when_nothing_is_configured():
    out = sq.score_rows([row(SAT, "Ana", "Server")], profiles=None, demand_by_day={"Saturday": -30},
                        typical_headcount={("Saturday", "night"): {"Server": 1}})
    prof = out["shifts"][0]["profile"]
    assert prof["demand"] == "low" and prof["min_quality"] == 65 and prof["source"] == "your sales history"


def test_sq15_the_requirements_table_settles_the_same_level():
    import schedule_requirements as sr
    reqs = sr.shift_requirements([SAT], typical_headcount={("Saturday", "night"): {"Server": 2}},
                                 profiles=None, demand_by_day={"Saturday": -30})
    assert next(r for r in reqs if r["daypart"] == "night")["demand"] == "low"


def test_sq15_a_relevelled_built_in_takes_its_levels_bars_and_an_owner_profile_never_does():
    busy_thursday = sq.profile_for_shift("Thursday", "night", None, {"Thursday": 30})
    assert busy_thursday.demand == "peak" and busy_thursday.min_quality == 82 and busy_thursday.requires_leader
    owner = [sq.ShiftProfile(key="thu", days=["Thursday"], daypart="night", demand="normal", source="restaurant")]
    mine = sq.profile_for_shift("Thursday", "night", owner, {"Thursday": 30})
    assert mine.demand == "normal" and mine.min_quality == 70
    # calibration applied to a built-in stays over the level's bar (SQ-22)
    tuned = sq.profiles_from_config(None, demand_by_day={"Saturday": -30},
                                    tuning={"saturday_dinner": {"min_quality": 75, "floors": {"coverage": 60}}})
    sat = sq.profile_for_shift("Saturday", "night", tuned, {"Saturday": -30})
    assert sat.demand == "low" and sat.min_quality == 75 and sat.floors == {"coverage": 60}


# ── SQ-17: a rule fewer people can meet is capped, not set aside ──────────

RULE = {"role": "Bartender", "days": ["Saturday"], "daypart": "night", "count": 2, "min_score": 5}


def test_sq17_the_count_is_capped_at_the_people_able_and_the_rule_kept():
    kept, aside, capped = sq.roster_leader_rules([RULE], scores={"Pat": 5, "Al": 4},
                                                 people_roles={"Pat": {"Bartender PM"}, "Al": {"Bartender"}})
    assert aside == [] and kept[0]["count"] == 1 and kept[0]["asked_count"] == 2
    assert capped == [{"label": "2 bartenders scoring 5 or above", "asked": 2, "able": 1}]
    kept, aside, capped = sq.roster_leader_rules([RULE], scores={"Al": 4}, people_roles={"Al": {"Bartender"}})
    assert aside == [RULE] and kept == [] and capped == []


def test_sq17_the_one_able_bartender_is_asked_for_instead_of_the_rule_vanishing():
    rows = [row(SAT, "Al", "Bartender"), row(SAT, "Bo", "Bartender"), row(FRI, "Pat", "Bartender")]
    out = sq.score_rows(rows, profiles=STD, scores={"Pat": 5, "Al": 4, "Bo": 4}, leader_rules=[RULE],
                        unsatisfiable=1)
    lead = dim(shift_on(out, SAT), "leadership")
    assert lead is not None and lead["facts"]["misses"][0]["count"] == 1
    assert any("Only 1 on the roster can meet" in r for r in out["confidence"]["reasons"])


# ── SQ-24 / L-19: preferences once for the week, learned at half weight ───

def test_sq24_desired_hours_are_one_fact_for_the_week_not_one_per_shift():
    rows = [row(d, "Ana", "Server", hours=8) for d in (MON, TUE, WED, THU, FRI)]
    out = sq.score_rows(rows, profiles=STD, preferences={"Ana": {"preferred_dayparts": [], "desired_hours": 25}},
                        typical_headcount={(sq._day_name(d), "night"): {"Server": 1} for d in WEEK})
    assert not any(d["key"] == "preferences" for s in out["shifts"] for d in s["dimensions"])
    pref = week_dim(out, "preferences")
    assert pref["facts"]["checked"] == 1 and pref["facts"]["met"] == 0
    assert pref["facts"]["misses"] == ["Ana asked for about 25h and has 40h"]


def test_l19_a_slot_somebody_keeps_dropping_counts_against_the_week_below_a_stated_one():
    rows = [row(SUN, "Ana", "Server"), row(MON, "Ana", "Server")]
    kw = dict(profiles=STD, typical_headcount={(d, "night"): {"Server": 1} for d in ("Sunday", "Monday")})
    learned = {"Ana": {"avoid": [["Sunday", "night"]], "prefer": [], "weight": 0.5}}
    out = sq.score_rows(rows, preferences={"Ana": {"preferred_dayparts": ["night"]}},
                        learned_preferences=learned, **kw)
    pref = week_dim(out, "preferences")
    # two stated facts met (both nights), one learned miss at half weight
    assert pref["facts"]["checked"] == 2.5 and pref["facts"]["met"] == 2.0 and pref["score"] == 80
    assert "Ana keeps asking to drop Sunday night and is on it" in pref["weaknesses"][0]
    # the same slots merged into the preferences are read the same way
    merged = sq.score_rows(rows, preferences={"Ana": {"preferred_dayparts": ["night"], "learned": learned["Ana"]}}, **kw)
    assert week_dim(merged, "preferences")["score"] == 80


# ── SQ-25: overtime is scored ─────────────────────────────────────────────

OT = {"line": 40.0, "bucket_of": {d: MON for d in WEEK}, "published": {}, "rates": {"Server": 20.0},
      "default_rate": 15.0}
OT_ROWS = ([row(d, "Ana", "Server", "2:00pm", "11:00pm", 9) for d in (MON, TUE, WED, THU, FRI)]
           + [row(d, "Bo", "Server") for d in (SAT, SUN)])
OT_KW = dict(profiles=STD, roster_roles={"Ana": "Server", "Bo": "Server"},
             typical_headcount={(sq._day_name(d), "night"): {"Server": 1} for d in WEEK})


def test_sq25_overtime_the_draft_creates_is_scored_with_its_premium():
    out = sq.score_rows(OT_ROWS, overtime=OT, **OT_KW)
    ot = week_dim(out, "overtime")
    assert ot is not None and ot["score"] < 100
    who = ot["facts"]["people"][0]
    assert (who["name"], who["overtime_hours"], who["premium"], who["avoidable"], who["teammate"]) == \
        ("Ana", 5.0, 50.0, True, "Bo")
    assert ot["weaknesses"][0].startswith("Ana is 5h into overtime in the payroll week of 10/5/26 — about $50")
    assert ot["facts"]["strained"] == ["Ana"]
    fine = sq.score_rows([r for r in OT_ROWS if not (r["employee"] == "Ana" and r["date"] == FRI)], overtime=OT, **OT_KW)
    assert week_dim(fine, "overtime")["score"] == 100


def test_sq25_published_hours_count_and_a_salaried_person_owes_none():
    rows = [row(d, "Ana", "Server", hours=8) for d in (MON, TUE, WED, THU)] + \
           [row(d, "Erik", "Manager", "10:00am", "10:00pm", 12) for d in (MON, TUE, WED, THU, FRI)]
    out = sq.score_rows(rows, overtime=dict(OT, published={"ana": {MON: 10}}), salaried={"erik"}, **OT_KW)
    people = week_dim(out, "overtime")["facts"]["people"]
    assert [(p["name"], p["overtime_hours"], p["published_hours"]) for p in people] == [("Ana", 2.0, 10.0)]


def test_sq25_the_passes_scorer_sees_the_overtime():
    """The optimizer and the fill passes choose by LocalScorer: handing
    Ana's Friday to Bo, who has room, must now read as the better week."""
    scorer = sq.LocalScorer(OT_ROWS, overtime=OT, **OT_KW)
    moved = [dict(r, employee="Bo") if (r["employee"] == "Ana" and r["date"] == FRI) else r for r in OT_ROWS]
    assert scorer.score(moved) > scorer.baseline


# ── SQ-26: kitchen station coverage is scored ─────────────────────────────

def _stations():
    import kitchen_stations as ks
    return ks.normalise({"roles": ["Kitchen"], "stations": ["Grill", "Saute"],
                         "needs": [{"station": "Grill", "daypart": "night", "days": ["Friday"], "count": 1},
                                   {"station": "Saute", "daypart": "night", "days": ["Friday"], "count": 1}],
                         "skills": {"Jesus": ["Grill"], "Ana": ["Saute"], "Bo": ["Saute"]}})


def test_sq26_a_station_no_trained_cook_holds_caps_the_shift():
    kw = dict(profiles=STD, stations=_stations(), typical_headcount={("Friday", "night"): {"Kitchen": 2}})
    out = sq.score_rows([row(FRI, "Ana", "Kitchen"), row(FRI, "Bo", "Kitchen")], **kw)
    shift = out["shifts"][0]
    st = dim(shift, "stations")
    assert st["score"] == 50 and st["facts"]["gaps"] == ["Grill"] and st["floor"] == 70
    assert shift["capped_by"] == "stations" and shift["score"] == 50
    assert "Grill has no trained cook on Friday dinner — Jesus is trained on it." in st["weaknesses"]
    whole = sq.score_rows([row(FRI, "Ana", "Kitchen"), row(FRI, "Jesus", "Kitchen")], **kw)
    assert dim(whole["shifts"][0], "stations")["score"] == 100


# ── SQ-27: fatigue and fairness across weeks ──────────────────────────────

BUSY = [sq.ShiftProfile(key="nights", daypart="night", demand="peak", label="Nights", source="restaurant"),
        sq.ShiftProfile(key="days", daypart="morning", demand="low", label="Days", source="restaurant")]
# Somebody is needed on every shift, so each one is scored at all.
ANY = {(sq._day_name(d), p): {"Server": 1, "Manager": 1} for d in WEEK for p in ("morning", "night")}
NIGHTS4 = [["Thursday", "night"], ["Friday", "night"], ["Saturday", "night"], ["Sunday", "night"]]


def _ledger_week(week, hours, slots, role="Server"):
    return {"week": week, "hours": hours, "slots": slots, "role": role}


def test_sq27_four_of_the_busiest_shifts_every_week_is_fatigue():
    rows = [row(d, "Fav", "Server") for d in (THU, FRI, SAT, SUN)] + [row(d, "Al", "Server") for d in (MON, TUE)]
    ledger = {"Fav": [_ledger_week(w, 24, NIGHTS4) for w in ("2026-09-14", "2026-09-21", "2026-09-28")]}
    plain = week_dim(sq.score_rows(rows, profiles=BUSY, typical_headcount=ANY), "fatigue")
    assert "Fav" not in plain["facts"]["strained"]            # 4 is the one-week ceiling, not over it
    out = week_dim(sq.score_rows(rows, profiles=BUSY, typical_headcount=ANY, load_ledger=ledger), "fatigue")
    assert "Fav" in out["facts"]["strained"]
    assert out["facts"]["sustained_busy"] == [{"name": "Fav", "average": 4.0, "weeks": 4}]


def test_sq27_hours_past_the_ceiling_week_after_week_unless_this_is_the_lighter_week():
    heavy = [_ledger_week(w, h, [["Monday", "night"]] * 5) for w, h in
             (("2026-09-14", 46), ("2026-09-21", 45), ("2026-09-28", 44))]
    rows = [row(d, "Ana", "Server", hours=7.6) for d in (MON, TUE, WED, THU, FRI)]     # 38h this week
    out = week_dim(sq.score_rows(rows, profiles=STD, weekly_ceiling=40, typical_headcount=ANY,
                                 load_ledger={"Ana": heavy}), "fatigue")
    assert out["facts"]["sustained_hours"][0]["name"] == "Ana" and "Ana" in out["facts"]["strained"]
    relief = [row(d, "Ana", "Server", hours=6) for d in (MON, TUE, WED, THU, FRI)]     # 30h: the relief
    calm = week_dim(sq.score_rows(relief, profiles=STD, weekly_ceiling=40, typical_headcount=ANY,
                                  load_ledger={"Ana": heavy}), "fatigue")
    assert calm["facts"]["sustained_hours"] == [] and calm["facts"]["strained"] == []


def test_sq27_fairness_sees_the_busiest_shifts_carried_across_published_weeks():
    past = ("2026-08-24", "2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21")
    ledger = {"Fav": [_ledger_week(w, 24, NIGHTS4) for w in past],
              "Al": [_ledger_week(w, 24, [["Monday", "morning"], ["Tuesday", "morning"], ["Wednesday", "morning"],
                                          ["Friday", "night"]]) for w in past],
              "Bo": [_ledger_week(w, 24, [["Monday", "morning"], ["Tuesday", "morning"], ["Wednesday", "morning"],
                                          ["Saturday", "night"]]) for w in past]}
    rows = [row(SAT, "Fav", "Server"), row(SAT, "Al", "Server")]
    out = sq.score_rows(rows, profiles=BUSY, typical_headcount=ANY, load_ledger=ledger)
    fair = dim(shift_on(out, SAT), "fairness")
    assert fair is not None and [o["name"] for o in fair["facts"]["busy_ledger"]["overloaded"]] == ["Fav"]
    assert fair["score"] == 100 - sq.LEDGER_PENALTY
    assert ("Fav has had 20 of the busiest shifts over the last 5 published weeks — about 10 would be their "
            "share among servers — and is on another here.") in fair["weaknesses"]
    # one published week alone is not a pattern across weeks (and nobody here
    # could be compared this week): no fairness reading at all
    alone = sq.score_rows(rows, profiles=BUSY, typical_headcount=ANY)
    assert "fairness" in shift_on(alone, SAT)["not_applicable"]


def test_sq27_the_tail_before_the_week_is_read_with_the_restaurants_own_demand():
    ctxs = sq.build_contexts([row(MON, "Fav", "Server")], profiles=BUSY,
                             prior_week_assignments={"Fav": [{"date": "2026-10-04", "daypart": "night", "day": "Sunday"}]})
    tail = [e for e in ctxs[0].week_assignments["Fav"] if e.get("prior")]
    assert tail[0]["demand"] == "peak"


# ── D-3: salaried rows are not hourly hours, and 55h is not their fatigue ─

def test_d3_a_salaried_managers_hours_are_not_the_days_hourly_hours():
    rows = [row(SAT, "Erik Baylis", "Manager", "11:00am", "11:00pm", 12), row(SAT, "Ana", "Server")]
    kw = dict(profiles=STD, daily_target_hours={SAT: 6}, typical_headcount={("Saturday", "night"): {"Server": 1}},
              splh_targets={"Saturday": {"night": 100}}, daypart_sales={"Saturday": {"night": 600}})
    before = sq.score_rows(rows, **kw)
    assert dim(shift_on(before, SAT), "labor_efficiency")["facts"]["scheduled_hours"] == 18
    out = sq.score_rows(rows, salaried={"erik baylis"}, **kw)
    night = shift_on(out, SAT)
    assert dim(night, "labor_efficiency")["facts"]["scheduled_hours"] == 6
    assert dim(night, "labor_efficiency")["score"] == 100
    assert dim(night, "splh")["facts"]["hours"] == 6


def test_d3_a_salaried_manager_at_55h_is_held_to_their_own_ceiling():
    rows = [row(d, "Erik Baylis", "Manager", "11:00am", "10:00pm", 11) for d in (MON, TUE, WED, THU, FRI)]
    rows += [row(d, "Ana", "Server", "2:00pm", "11:00pm", 9) for d in (MON, TUE, WED, THU, FRI)]
    kw = dict(profiles=STD, weekly_ceiling=40, typical_headcount=ANY)
    plain = week_dim(sq.score_rows(rows, **kw), "fatigue")
    assert {h["name"] for h in plain["facts"]["heavy_weeks"]} == {"Erik Baylis", "Ana"}
    out = week_dim(sq.score_rows(rows, salaried={"erik baylis"}, **kw), "fatigue")
    assert [h["name"] for h in out["facts"]["heavy_weeks"]] == ["Ana"]
    capped = week_dim(sq.score_rows(rows, salaried={"erik baylis"}, hours_ceilings={"erik baylis": 50, "ana": 40},
                                    **kw), "fatigue")
    assert {(h["name"], h["ceiling"]) for h in capped["facts"]["heavy_weeks"]} == {("Erik Baylis", 50.0), ("Ana", 40.0)}


# ── D-6: managers and salaried people are experienced by default ──────────

def test_d6_an_owner_with_one_punch_is_not_still_developing():
    tenure = {"Erik": 1, "Ann": 2, "Vet": 40, "New": 2}
    rows = [row(SAT, "Erik", "Owner"), row(SAT, "Ann", "Manager"), row(SAT, "New", "Server")]
    kw = dict(profiles=STD, tenure=tenure, typical_headcount={("Saturday", "night"): {"Server": 1}})
    before = dim(sq.score_rows(rows, **kw)["shifts"][0], "experience_balance")
    assert set(before["facts"]["rookies"]) == {"Erik", "Ann", "New"}
    out = sq.score_rows(rows, managers={"ann": "Manager"}, salaried={"erik"}, **kw)
    shift = out["shifts"][0]
    exp = dim(shift, "experience_balance")
    assert exp["facts"]["veterans"] == ["Erik", "Ann"] and exp["facts"]["rookies"] == ["New"]
    assert exp["facts"]["by_default"] == ["Erik", "Ann"]
    why = {a["employee"]: a["why"] for a in shift["assignments"]}
    assert "still new" not in why["Erik"] and "still new" in why["New"]


def test_d6_the_defaults_never_turn_experience_on_by_themselves():
    rows = [row(SAT, n, "Server") for n in ("A", "B", "C")]
    out = sq.score_rows(rows, profiles=STD, tenure={"A": 2, "B": 3, "C": 5},
                        managers={"a": "Manager", "b": "Manager", "c": "Manager"},
                        typical_headcount={("Saturday", "night"): {"Server": 3}})
    assert "experience_balance" in out["shifts"][0]["not_applicable"]
    assert sq.experience_judged({"A": 2}, {"x", "y", "z"}) and not sq.experience_judged({"A": 2}, {"x"})


# ── SQ-22: what a stored score keeps for calibration ──────────────────────

def test_sq22_each_scored_dimension_keeps_the_floor_it_was_held_to():
    out = sq.score_rows([row(SAT, "Ana", "Server")], profiles=STD,
                        typical_headcount={("Saturday", "night"): {"Server": 2}})
    cov = dim(out["shifts"][0], "coverage")
    assert cov["floor"] == 70


def test_sq22_a_profiles_floors_round_trip_and_stay_bounded():
    p = sq.profile_from_dict({"key": "sat", "floors": {"coverage": 65, "leadership": 150, "nonsense": 5}})
    assert p.floors == {"coverage": 65, "leadership": 100}
    assert sq.profile_from_dict(sq.profile_to_dict(p)).floors == p.floors
