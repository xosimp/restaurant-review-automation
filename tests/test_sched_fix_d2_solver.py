"""The assignment solver after the 10/3/26 schedule fix round (workstream
D2): the manager every minute and each role's closer held as hard rules
(PR-32), pinned rows never reassigned, no new hour of overtime, code never
choosing somebody it may not, the objective the fixed scorer's (SQ-18),
labor dollars (P-32), the scheduling memory (L-3, D-35), learned
preferences (L-19, D-36), preferred pairs (L-20), lateness at the edges
(D-44), and a judge that compares breaches by identity (E-1, P-14).
"""
import schedule_rules as sr
import schedule_solver as ss
import shift_quality as sq
from sched_d2_week import WEEK, DAYS, big_week, row

MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def cons(names, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _sig(names, role="Server", **kw):
    out = {"roster": list(names), "roster_roles": {n: role for n in names}}
    out.update(kw)
    return out


def _unmanaged(rows, c):
    return {d: sum(e - s for s, e, _i in gaps) for d, gaps in sr.manager_gaps(rows, c).items()}


# ── PR-32: a manager on the floor every minute anybody is ───────────────────

def test_the_solver_never_moves_a_manager_so_a_gap_opens():
    """Max manages and is rated 1; Cat is rated 5 and free on Saturday. The
    cost model would put Cat in Max's place — and the floor without a
    manager. It used to read only the retired per-daypart switch."""
    names = ["Max", "Ann", "Cat"]
    c = cons(names, managers={"max": "Manager"})
    rows = [row(SAT, "Max", "4:00pm", "11:00pm", "Server"), row(SAT, "Ann", "4:00pm", "11:00pm", "Server"),
            row(TUE, "Cat", "4:00pm", "11:00pm", "Server")]
    sig = _sig(names, scores={"Max": 1, "Ann": 2, "Cat": 5},
               typical_headcount={("Saturday", "night"): {"Server": 2}, ("Tuesday", "night"): {"Server": 1}})
    assert not c.compliance.get("manager_on_duty")
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    before, after = _unmanaged(rows, c), _unmanaged(out["rows"], c)
    assert all(after.get(d, 0) <= m for d, m in before.items()) and not set(after) - set(before)
    assert SAT not in after
    # Without a manager on the roster the same search does swap Max out:
    # the rule, not the costs, is what held him.
    free = ss.improve(rows, {}, signals=sig, constraints=cons(names))
    assert any(r["employee"] != "Max" for r in free["rows"] if r["date"] == SAT and r["employee"] in ("Max", "Cat"))


def test_a_stretch_the_draft_left_without_a_manager_is_covered_when_one_can_take_it():
    """Tuesday's only shift has no manager on it; Max can work it."""
    names = ["Max", "Ann", "Cat"]
    c = cons(names, managers={"max": "Manager"})
    rows = [row(SAT, "Max", "4:00pm", "11:00pm", "Server"), row(SAT, "Ann", "4:00pm", "11:00pm", "Server"),
            row(TUE, "Cat", "4:00pm", "11:00pm", "Server")]
    sig = _sig(names, typical_headcount={("Saturday", "night"): {"Server": 2}, ("Tuesday", "night"): {"Server": 1}})
    assert _unmanaged(rows, c) == {TUE: 420}
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert out["applied"] and _unmanaged(out["rows"], c) == {}


def test_an_acting_manager_counts_on_their_dates_only():
    names = ["Kay", "Ann"]
    c = cons(names, managers={"boss": "Owner"}, acting_managers={"kay": {SAT}})
    prob = ss.Problem([row(SAT, "Kay", "4:00pm", "11:00pm"), row(SUN, "Kay", "4:00pm", "11:00pm")], c,
                      signals=_sig(names))
    kay = prob.pidx["kay"]
    assert prob.manages(kay, SAT) and not prob.manages(kay, SUN)


def test_a_pinned_row_is_never_reassigned():
    """The manager plan's rows (and a day the owner kept) are fixed."""
    names = ["Max", "Ann", "Cat"]
    c = cons(names)
    rows = [dict(row(SAT, "Ann", "4:00pm", "11:00pm"), _pinned="manager_plan"),
            row(TUE, "Cat", "4:00pm", "11:00pm")]
    sig = _sig(names, scores={"Ann": 1, "Cat": 5, "Max": 5},
               typical_headcount={("Saturday", "night"): {"Server": 1}, ("Tuesday", "night"): {"Server": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    assert prob.units[0].fixed and prob.units[0].why_fixed == "pinned"
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert out["rows"][0]["employee"] == "Ann"


# ── no new overtime, code chooses only whom it may ───────────────────────────

def test_the_solver_never_puts_anybody_past_their_overtime_line():
    """Bo has 36h; giving him Sunday would be 41h. Cy has room. The rule:
    overtime goes to nobody while a teammate has room — the solver never
    adds an hour past anybody's line (schedule_rules.overtime_line) to the
    draft, and where the owner allowed somebody more (a 45h maximum) the
    hour past 40 still costs what the scorer charges avoidable overtime."""
    names = ["Bo", "Cy"]
    rows = [row(d, "Bo", "9:00am", "6:00pm", "Cook") for d in (MON, TUE, WED, THU)]
    rows.append(row(SUN, "Cy", "11:00am", "4:00pm", "Cook"))
    sig = _sig(names, role="Cook", scores={"Bo": 5, "Cy": 1},
               typical_headcount={("Sunday", "morning"): {"Cook": 1}},
               overtime={"line": 40.0, "rates": {"cook": 18.0}})
    c = cons(names)
    prob = ss.Problem(rows, c, signals=sig)
    assert prob.capf(prob.pidx["bo"], c.bucket(SUN)) == 40.0
    every_shift_to_bo = [prob.pidx["bo"]] * len(prob.units)
    assert not prob.feasible(every_shift_to_bo)
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert sum(float(r["scheduled_hours"]) for r in out["rows"] if r["employee"] == "Bo") <= 40.0
    allowed = ss.Problem(rows, cons(names, hours_limits={"bo": (None, 45.0)}), signals=sig)
    sun = next(u for u in allowed.units if u.date == SUN)
    bo = allowed.pidx["bo"]
    assert bo in allowed.dom[sun.id]
    st = ss._State(allowed)
    for u in allowed.units:
        if u.date != SUN:
            st.add(u, bo)
    assert allowed.dynamic(sun, bo, st) > allowed.dynamic(sun, allowed.pidx["cy"], ss._State(allowed))


def test_code_never_chooses_somebody_dormant_or_on_a_day_their_note_covers():
    names = ["Ann", "Old", "Noted"]
    c = cons(names, dormant={"old": "2026-08-01"},
             note_caution={"noted": {"days": {"Saturday"}, "dates": set(), "text": "no Saturdays this month"}})
    rows = [row(SAT, "Ann", "4:00pm", "11:00pm")]
    prob = ss.Problem(rows, c, signals=_sig(names))
    u = prob.units[0]
    assert prob.pidx["old"] not in prob.dom[u.id] and prob.pidx["noted"] not in prob.dom[u.id]
    # a drafted person stays theirs
    rows = [row(SAT, "Old", "4:00pm", "11:00pm")]
    prob = ss.Problem(rows, c, signals=_sig(names))
    assert prob.pidx["old"] in prob.dom[prob.units[0].id]


def test_a_minor_is_never_given_a_shift_past_their_band():
    names = ["Teen", "Ann"]
    c = cons(names, minors={"teen"}, minor_bands={"teen": "14-15"})
    rows = [row(TUE, "Ann", "3:00pm", "9:00pm"), row(SAT, "Ann", "6:00am", "11:00am")]
    prob = ss.Problem(rows, c, signals=_sig(names))
    teen = prob.pidx["teen"]
    assert all(teen not in prob.dom[u.id] for u in prob.units)
    assert {"a minor working past the latest allowed end", "a minor starting before the earliest allowed start"} \
        <= set(r.get("Teen") for r in prob.reasons)


# ── SQ-18: the solver's objective is the fixed scorer's ─────────────────────

def _shift_cost(prob, assign_names, date, part, key):
    a = [prob.pidx[n.lower()] for n in assign_names]
    return prob.shift_parts(("S", date, part), a).get(key, 0.0)


def test_an_unrated_person_is_unknown_to_the_solver_as_to_the_scorer():
    """The scorer judges strength on the rated people only (SQ-3); the solver
    gave an unrated person the role's average across the roster."""
    names = ["A4", "Ghost", "B2"]
    c = cons(names)
    rows = [row(SAT, "A4", "4:00pm", "11:00pm", "Bartender"), row(SAT, "Ghost", "4:00pm", "11:00pm", "Bartender")]
    prof = [sq.ShiftProfile(key="std", min_strength={"Bartender": 8}, source="restaurant")]
    sig = _sig(names, role="Bartender", scores={"A4": 4, "B2": 2},
               typical_headcount={("Saturday", "night"): {"Bartender": 2}})
    prob = ss.Problem(rows, c, signals=sig, profiles=prof)
    # A4 + an unrated person: the rated average 4 meets the per-person bar (8 / 2)
    assert _shift_cost(prob, ["A4", "Ghost"], SAT, "night", "operational_strength") == 0
    q = sq.score_rows(rows, profiles=prof, **sig)
    assert next(d for d in q["shifts"][0]["dimensions"] if d["key"] == "operational_strength")["score"] == 100
    # B2 in Ghost's place is judged on the average of the rated: 3 of 4
    assert _shift_cost(prob, ["A4", "B2"], SAT, "night", "operational_strength") > 0


def test_a_bar_never_asks_for_more_of_the_role_than_the_shift_has():
    """'2 bartenders scoring 5' with one bartender on: the scorer caps the
    need at the role's people on the shift (D1a SQ-1); the solver charged a
    missing leader."""
    names = ["Five", "Two"]
    c = cons(names)
    rows = [row(SAT, "Five", "4:00pm", "11:00pm", "Bartender")]
    rule = {"role": "Bartender", "count": 2, "min_score": 5}
    sig = _sig(names, role="Bartender", scores={"Five": 5, "Two": 2}, leader_rules=[rule],
               typical_headcount={("Saturday", "night"): {"Bartender": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    assert _shift_cost(prob, ["Five"], SAT, "night", "leadership") == 0
    assert _shift_cost(prob, ["Two"], SAT, "night", "leadership") > 0


def test_a_leader_rule_with_no_daypart_binds_only_where_the_role_works():
    """SQ-2's one test (leader_rule_applies with where each role works): the
    solver's rule check passed no role_runs, so a bartender rule bound a
    lunch the bar never works."""
    names = ["Bar", "Lunch"]
    c = cons(names)
    rows = [row(SAT, "Lunch", "11:00am", "3:00pm", "Server"), row(SAT, "Bar", "4:00pm", "11:00pm", "Bartender")]
    rule = {"role": "Bartender", "count": 1, "min_score": 5, "days": ["Saturday"]}
    sig = {"roster": names, "roster_roles": {"Bar": "Bartender", "Lunch": "Server"}, "scores": {"Bar": 3, "Lunch": 3},
           "leader_rules": [rule], "typical_headcount": {("Saturday", "morning"): {"Server": 1},
                                                         ("Saturday", "night"): {"Bartender": 1}}}
    prob = ss.Problem(rows, c, signals=sig)
    assert prob.groups[("S", SAT, "morning")]["rules"] == []
    assert prob.groups[("S", SAT, "night")]["rules"] == [rule]


def test_a_manager_on_the_floor_runs_the_shift_the_profile_asks_for():
    """SQ-13 mirrored: the built-in Saturday wants somebody able to run it —
    an unrated manager is."""
    names = ["Max", "Ann"]
    c = cons(names, managers={"max": "Manager"})
    rows = [row(SAT, "Max", "4:00pm", "11:00pm"), row(SAT, "Ann", "4:00pm", "11:00pm")]
    sig = _sig(names, scores={"Ann": 3}, typical_headcount={("Saturday", "night"): {"Server": 2}})
    prob = ss.Problem(rows, c, signals=sig)
    assert prob.groups[("S", SAT, "night")]["prof"].requires_leader
    assert _shift_cost(prob, ["Max", "Ann"], SAT, "night", "leadership") == 0


def test_flexible_and_experienced_read_by_family_and_by_default():
    """D1b: flexible is two role FAMILIES (Server AM and PM are one); a
    manager or salaried person is experienced whatever their punch count."""
    names = ["Amy", "Bo", "Max"]
    c = cons(names, managers={"max": "Manager"})
    rows = [row(SAT, "Amy", "4:00pm", "11:00pm", "Server PM")]
    sig = {"roster": names, "roster_roles": {"Amy": "Server PM", "Bo": "Server PM", "Max": "Manager"},
           "cross_trained": {"Amy": ["Server AM", "Server PM"], "Bo": ["Server PM", "Bartender"]},
           "tenure": {"Amy": 30, "Bo": 2, "Max": 1}}
    prob = ss.Problem(rows, c, signals=sig)
    assert not prob.flexible[prob.pidx["amy"]] and prob.flexible[prob.pidx["bo"]]
    assert prob.veteran[prob.pidx["max"]] and not prob.veteran[prob.pidx["bo"]]


def test_the_judge_keeps_more_solver_answers_on_a_realistic_week():
    """The solver's answers were rejected by score_rows on the commonest
    cases because the two judged differently; on a realistic week with
    ratings for half the team, managers, closers and breaches, the solver's
    week is kept and breaks nothing the draft did not."""
    rows, c, sig = big_week()
    out = ss.improve(rows, {"roster_roles": sig["roster_roles"]}, signals=sig, constraints=c, max_seconds=6)
    assert out["applied"] and out["after_score"] > out["before_score"]
    before, after = sr.breach_profile(rows, c), sr.breach_profile(out["rows"], c)
    assert sr.regressions(before, after, upto=sr.TIER_BUDGET, hard_only=False) == []
    assert all(a["employee"] == b["employee"] for a, b in zip(rows, out["rows"]) if a.get("_pinned"))


# ── P-32: labor dollars ─────────────────────────────────────────────────────

def test_a_dearer_person_costs_the_solver_what_they_cost_the_week():
    names = ["Cheap", "Dear"]
    c = cons(names)
    rows = [row(SAT, "Cheap", "4:00pm", "11:00pm", "Cook")]
    sig = _sig(names, role="Cook", typical_headcount={("Saturday", "night"): {"Cook": 1}},
               overtime={"line": 40.0, "rates": {"cook": 15.0}, "person_rates": {"cheap": 14.0, "dear": 25.0}})
    prob = ss.Problem(rows, c, signals=sig)
    u = prob.units[0]
    assert prob.components_of(u, prob.pidx["dear"]).get("dollars", 0) > 0
    assert "dollars" not in prob.components_of(u, prob.pidx["cheap"])
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert out["rows"][0]["employee"] == "Cheap"


# ── L-3 / D-35: the scheduling memory; L-19 / D-36; L-20; D-44 ───────────────

def test_a_learned_slot_the_manager_keeps_taking_somebody_off_is_held():
    """'Bob off Tuesday dinner': the solver could put him back, and the
    manager corrected it again every week."""
    names = ["Bob", "Cy"]
    c = cons(names)
    rows = [row(TUE, "Cy", "4:00pm", "11:00pm")]
    learned = [{"kind": "moved_off", "key": "slot|bob|Tue|night|off", "person": "Bob", "day": "Tuesday",
                "daypart": "night", "role": "Server", "value": "off", "confidence": 0.9, "enforcement": "soft",
                "source": "taken off Tuesday dinner 4 of the last 5 weeks"}]
    sig = _sig(names, scores={"Bob": 5, "Cy": 2}, learned=learned,
               typical_headcount={("Tuesday", "night"): {"Server": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    u = prob.units[0]
    assert prob.components_of(u, prob.pidx["bob"]).get("learned", 0) > 0
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert out["rows"][0]["employee"] == "Cy"
    # a memory held in the prompt only is the prompt's
    prompt_only = [dict(learned[0], enforcement="prompt")]
    assert ss.Problem(rows, c, signals=dict(sig, learned=prompt_only)).learned == []


def test_a_slot_somebody_keeps_dropping_costs_the_solver_at_half_weight():
    names = ["Dee", "Eve"]
    c = cons(names)
    rows = [row(SUN, "Dee", "4:00pm", "11:00pm")]
    lp = {"Dee": {"avoid": [["Sunday", "night"]], "prefer": [], "weight": 0.5}}
    sig = _sig(names, learned_preferences=lp, typical_headcount={("Sunday", "night"): {"Server": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    u = prob.units[0]
    dee = prob.components_of(u, prob.pidx["dee"]).get("learned_preference", 0)
    stated = ss.Problem(rows, c, signals=_sig(names, preferences={"Dee": {"preferred_dayparts": ["morning"]}}))
    assert 0 < dee < stated.components_of(stated.units[0], stated.pidx["dee"]).get("preference", 0) * 0.6
    assert ss.improve(rows, {}, signals=sig, constraints=c)["rows"][0]["employee"] == "Eve"


def test_splitting_a_preferred_pair_costs_the_solver():
    """L-20: the solver built only the keep-apart pairs."""
    names = ["Ana", "Ben", "Cal"]
    c = cons(names)
    rows = [row(SAT, "Ana", "4:00pm", "11:00pm"), row(SAT, "Cal", "4:00pm", "11:00pm"),
            row(FRI, "Ben", "4:00pm", "11:00pm")]
    sig = _sig(names, pairs={"prefer": {frozenset({"ana", "ben"})}, "avoid": set()},
               typical_headcount={("Saturday", "night"): {"Server": 2}, ("Friday", "night"): {"Server": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    assert _shift_cost(prob, ["Ana", "Cal"], SAT, "night", "pairings") > 0
    assert _shift_cost(prob, ["Ana", "Ben"], SAT, "night", "pairings") == 0
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert sorted(r["employee"] for r in out["rows"] if r["date"] == SAT) == ["Ana", "Ben"]


def test_somebody_often_late_is_not_the_only_one_opening():
    """D-44's solver side: a lateness risk alone at the role's first start."""
    names = ["Late", "Prompt"]
    c = cons(names)
    rows = [row(SAT, "Late", "9:00am", "3:00pm", "Cook")]
    rel = {"Late": {"no_show_rate": 0.0, "late_risk": True, "late_rate": 0.4},
           "Prompt": {"no_show_rate": 0.0, "late_risk": False, "late_rate": 0.0}}
    sig = _sig(names, role="Cook", reliability=rel, typical_headcount={("Saturday", "morning"): {"Cook": 1}})
    prob = ss.Problem(rows, c, signals=sig)
    assert "opens" in prob.units[0].edges
    assert prob.components_of(prob.units[0], prob.pidx["late"]).get("reliability", 0) > 0
    assert ss.improve(rows, {}, signals=sig, constraints=c)["rows"][0]["employee"] == "Prompt"


# ── E-1 / P-14: the judge compares by identity ──────────────────────────────

def test_a_candidate_that_trades_one_breach_for_another_is_refused(monkeypatch):
    """The same count of hard breaches, a different breach: refused. The
    judge compared (row index, kind) sets."""
    names = ["Ann", "Bob", "Max"]
    c = cons(names, managers={"max": "Manager"}, blocked_dates={"ann": {SAT: "on approved time off"}})
    rows = [row(SAT, "Ann", "4:00pm", "11:00pm"), row(SAT, "Max", "4:00pm", "11:00pm"),
            row(TUE, "Bob", "4:00pm", "11:00pm")]
    sig = _sig(names, typical_headcount={("Saturday", "night"): {"Server": 2}})
    # Bob in for Ann (her time off fixed) and Bob's Tuesday to Max... and Max
    # off Saturday: the time-off breach gone, a manager gap made.
    traded = [dict(rows[0], employee="Bob"), dict(rows[1], employee="Bob"), dict(rows[2], employee="Max")]
    real = ss.solve

    def rigged(*a, **k):
        out = real(*a, **k)
        out["candidates"] = [traded]
        return out
    monkeypatch.setattr(ss, "solve", rigged)
    before = sum(1 for v in sr.violations(rows, c) if v["hard"])
    after = sum(1 for v in sr.violations(traded, c) if v["hard"])
    assert after <= before + 1
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert not out["applied"] and out["stats"]["refused"] == 1
