"""Rule precedence that matches the owner (schedule audit 10/10/26,
workstream B).

The restaurant's own limits (the weekly ceiling, the longest shift, days in
a row, rest — where a jurisdiction pack does not make them law) rank below a
manager on the floor every minute and the coverage rules (LOGIC-1, LOGIC-3);
overtime is the owner's teammate test, exactly; a person's own maximum and
the law stay at the top. The closing-manager and opening-prep allowances
are the owner's verdict only — no repair pass may shorten a manager into
them (BUGSGEN-4) — and the assumed stay is said and priced (TRUST-21).
"""
from datetime import date, timedelta

import schedule_engine as se
import schedule_rules as sr

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]      # Mon 10/5 - Sun 10/11
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON, SAT = WEEK[0], WEEK[5]


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, start, end, role="Server"):
    r = {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
         "shift_end": end, "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


def _manager_week(**kw):
    """Andrew, the only manager (hourly), at 40h Monday to Friday; Ana
    serves Saturday 4-10pm with nobody running the floor."""
    c = _c(managers={"andrew": "Manager"}, roster_names=["Andrew", "Ana"], active={"andrew", "ana"},
           known_roles={"andrew": {"manager"}, "ana": {"server"}}, **kw)
    rows = [_row(i, "Andrew", "2:00pm", "10:00pm", "Manager") for i in range(5)]
    rows.append(_row(5, "Ana", "4:00pm", "10:00pm"))
    return c, rows


# ── LOGIC-1: the policy defaults rank below the manager rule ──────────────

def test_the_audit_repro_an_hourly_manager_may_pass_40h_when_nobody_else_can():
    c, rows = _manager_week()
    leg = _row(5, "Andrew", "4:00pm", "10:00pm", "Manager")
    # strict: past his overtime line and the ceiling — refused (the first
    # round of every fill)
    ok, why = c.can_add(leg, rows)
    assert not ok and "40h" in why
    # relaxed past the line, inside the restaurant's policy: the 40h ceiling
    # is still the restaurant's own limit
    ok, why = c.can_add(leg, rows, overtime=False)
    assert not ok and "ceiling" in why
    # past the restaurant's own limits: allowed — the ceiling is policy,
    # ranked below a manager on the floor (it used to refuse: "46h ... over 40h")
    assert c.can_add(leg, rows, overtime=False, policy=False) == (True, "")
    out = sr.cover_manager_gaps(rows, c)
    assert out["added"] and out["added"][0]["employee"] == "Andrew"
    assert "scheduling limits" in out["added"][0]["reason"] or "overtime" in out["added"][0]["reason"]
    assert not sr.manager_gaps(out["rows"], c)
    over = [v for v in sr.violations(out["rows"], c) if v["kind"] == "over_hours_ceiling"]
    assert over and not over[0]["hard"] and over[0]["over_by"] == 6


def test_a_persons_own_maximum_stays_hard_even_for_the_manager_rule():
    c, rows = _manager_week(hours_limits={"andrew": (None, 40)})
    leg = _row(5, "Andrew", "4:00pm", "10:00pm", "Manager")
    ok, why = c.can_add(leg, rows, overtime=False, policy=False)
    assert not ok and "own weekly maximum" in why or "over 40h" in why
    out = sr.cover_manager_gaps(rows, c)
    assert not out["added"] and out["left"] and sr.manager_gaps(out["rows"], c)


def test_a_rule_the_jurisdiction_makes_law_outranks_the_manager_rule():
    # Six days in a row is the restaurant's policy — unless Illinois' One
    # Day Rest in Seven Act makes it law (compliance_packs IL).
    c, rows = _manager_week()
    rows += [_row(5, "Andrew", "10:00am", "2:00pm", "Manager")]                 # Saturday, day six
    leg = _row(6, "Andrew", "4:00pm", "10:00pm", "Manager")
    rows.append(_row(6, "Ana", "4:00pm", "10:00pm"))
    assert c.can_add(leg, rows, overtime=False, policy=False)[0]
    c.compliance["_pack"] = {"code": "IL", "law": ["max_consecutive_days"]}
    ok, why = c.can_add(leg, rows, overtime=False, policy=False)
    assert not ok and "in a row" in why
    run = [v for v in sr.violations(rows + [leg], c) if v["kind"] == "long_run"]
    assert run and sr.tier_of(run[0]) == sr.TIER_LEGAL


def test_the_illinois_pack_marks_its_law_and_a_weaker_owner_value_is_not():
    import compliance_packs
    # Illinois law is the One Day Rest in Seven Act (and meal periods); the
    # 10-hour rest is Chicago's Fair Workweek ordinance, for covered
    # employers only (J1, lead 10/10/26) — never Illinois law.
    merged = compliance_packs.apply(dict(sr.DEFAULTS), "IL")
    assert set(merged["_pack"]["law"]) == {"max_consecutive_days"} and merged["min_rest_hours"] is None
    weaker = compliance_packs.apply(dict(sr.DEFAULTS, max_consecutive_days=7), "IL",
                                    owner_set={"max_consecutive_days": 7})
    assert "max_consecutive_days" not in weaker["_pack"]["law"]
    assert "min_rest_hours" not in compliance_packs.apply(dict(sr.DEFAULTS), "CA")["_pack"]["law"]


def test_the_tiers_rank_policy_below_manager_and_coverage():
    assert sr.TIER_LEGAL < sr.TIER_MANAGER < sr.TIER_COVERAGE < sr.TIER_POLICY < sr.TIER_OVERTIME \
        < sr.TIER_MIN_HOURS < sr.TIER_REQUIREMENT < sr.TIER_BUDGET < sr.TIER_QUALITY
    for kind in ("rest_gap", "long_run", "shift_too_long", "over_hours_ceiling"):
        assert sr.BREACH_TIER[kind] == sr.TIER_POLICY
    assert sr.BREACH_TIER["over_max_hours"] == sr.TIER_LEGAL
    assert "over_hours_ceiling" not in sr.HARD and "over_max_hours" in sr.HARD
    assert [t for t, _n, _w in sr.TIER_TABLE] == sorted(t for t, _n, _w in sr.TIER_TABLE)


# ── LOGIC-3: overtime is the teammate test, exactly ───────────────────────

def _cooks(extra_room=False):
    names = ["Cal", "Dee"] + (["Eve"] if extra_room else [])
    c = _c(roster_names=names, active={n.lower() for n in names},
           known_roles={n.lower(): {"line cook"} for n in names}, roster_roles={n: "Line Cook" for n in names},
           role_floors={"Line Cook": {"morning": 0, "night": 2, "days": {}}})
    rows = [_row(i, "Cal", "9:00am", "4:30pm", "Line Cook") for i in range(5)]       # 37.5h Mon-Fri
    rows.append(_row(5, "Dee", "4:00pm", "10:00pm", "Line Cook"))                       # Dee is on Saturday
    if extra_room:
        rows.append(_row(0, "Eve", "4:00pm", "10:00pm", "Line Cook"))
    return c, rows


def test_overtime_ok_names_the_teammate_with_room():
    c, rows = _cooks(extra_room=True)
    sat = _row(5, "Cal", "4:00pm", "10:00pm", "Line Cook")
    ok, mate = c.overtime_ok(sat, rows)
    assert not ok and mate == "Eve"
    ok, why = c.can_add(sat, rows, overtime="teammate", policy=False)
    assert not ok and "Eve" in why
    c2, rows2 = _cooks()
    assert c2.overtime_ok(sat, rows2) == (True, None)
    assert c2.can_add(sat, rows2, overtime="teammate", policy=False) == (True, "")


def test_a_floor_takes_overtime_only_when_nobody_in_the_role_can_inside_their_line():
    # A Saturday night floor of 2 cooks: Dee is the only other cook; Cal at
    # 37.5h would go to 43.5h. The floor outranks overtime (LOGIC-3).
    c, rows = _cooks()
    options = [("Cal", _row(5, "Cal", "4:00pm", "10:00pm", "Line Cook"))]
    legal, refused = se._legal_fills(c, rows, options, SAT, coverage=True)
    assert [e for e, _r in legal] == ["Cal"]
    # The top-up (requirements, below overtime) never does.
    legal, refused = se._legal_fills(c, rows, options, SAT, coverage=False)
    assert not legal and "Cal" in refused
    # With a teammate who has room, the strict round takes her and stops.
    c3, rows3 = _cooks(extra_room=True)
    options = [("Cal", _row(5, "Cal", "4:00pm", "10:00pm", "Line Cook")),
               ("Eve", _row(5, "Eve", "4:00pm", "10:00pm", "Line Cook"))]
    legal, _r = se._legal_fills(c3, rows3, options, SAT, coverage=True)
    assert [e for e, _r in legal] == ["Eve"]


# ── the engine's judge: a policy limit gives way only to a real gain ─────

class _X:
    editable = None

    def __init__(self, c):
        self.c = c

    def over_budget(self, rows):
        return 0.0


def test_the_manager_stage_may_cost_a_policy_limit_only_when_it_closes_a_gap():
    c, rows = _manager_week()
    before = sr.breach_profile(rows, c)
    fixed = rows + [_row(5, "Andrew", "4:00pm", "10:00pm", "Manager")]
    worse, _after = se._judge(sr.TIER_MANAGER, rows, fixed, before, _X(c))
    assert worse == []                                  # past the ceiling, but the gap is closed
    # The same row for a stage below the policy tier is refused.
    worse, _after = se._judge(sr.TIER_OVERTIME, rows, fixed, before, _X(c))
    assert worse and worse[0]["tier"] == sr.TIER_POLICY


def test_the_repair_loop_has_a_policy_stage_below_coverage():
    order = [k for k, *_r in se.REPAIR_STAGES]
    assert se._STAGE_TIER["policy"] == sr.TIER_POLICY
    assert order.index("close_out") < order.index("policy") < order.index("overtime")
    assert se._STAGE_TIER["top_up"] == sr.TIER_REQUIREMENT
    assert order.index("min_hours") < order.index("top_up") < order.index("budget")


# ── BUGSGEN-4: the owner's allowances are the verdict's, never a pass's ───

def _close_night(manager_end="11:00pm", server_end="11:00pm", **kw):
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ana"], active={"max", "ana"},
           known_roles={"max": {"manager"}, "ana": {"server"}}, **kw)
    rows = [_row(4, "Max", "4:00pm", manager_end, "Manager"), _row(4, "Ana", "4:00pm", server_end)]
    return c, rows


def test_a_manager_cut_into_the_closing_stay_is_a_regression_for_every_pass():
    # The audit's repro: 4-11pm cut to 4-10pm with a server to 11pm read as
    # no regression — the stay allowance forgave the hour every pass made.
    c, rows = _close_night()
    cut = [dict(rows[0], shift_end="10:00pm", scheduled_hours="6"), rows[1]]
    assert not sr.manager_gaps(cut, c)                      # the owner's verdict still forgives it
    assert sr.manager_gaps(cut, c, raw=True)                # but the stretch is there
    worse = sr.regressions(sr.breach_profile(rows, c), sr.breach_profile(cut, c), upto=sr.TIER_MANAGER)
    assert worse and worse[0]["id"] == ("no_manager", WEEK[4]) and "assumes they stay" in worse[0]["label"]
    # A server staying past the manager inside the allowance is no
    # regression: the manager did not move, the stay is said and priced.
    later = [rows[0], dict(rows[1], shift_end="11:30pm", scheduled_hours="7.5")]
    assert sr.regressions(sr.breach_profile(rows, c), sr.breach_profile(later, c), upto=sr.TIER_MANAGER) == []


def test_the_overtime_trim_never_shortens_a_closing_manager_into_the_stay():
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ana"], active={"max", "ana"},
           known_roles={"max": {"manager"}, "ana": {"server"}})
    rows = [_row(i, "Max", "3:00pm", "11:00pm", "Manager") for i in range(4)]            # 32h
    rows += [_row(4, "Max", "3:00pm", "12:00am", "Manager"), _row(4, "Ana", "3:00pm", "12:00am")]   # 41h
    out = sr.rebalance_overtime(rows, c, roster_roles={"Max": "Manager", "Ana": "Server"})
    fri = next(r for r in out["rows"] if r["employee"] == "Max" and r["date"] == WEEK[4])
    assert fri["shift_end"] == "12:00am", out["trims"]


def test_the_closing_stay_is_only_the_days_last_block():
    # A lunch manager off at 2pm with a server to 3pm, no hours on file and
    # a separate dinner later: a mid-day stretch, never "the manager stays".
    c = _c(managers={"max": "Manager", "lee": "Manager"}, roster_names=["Max", "Lee", "Ana", "Bo"],
           active={"max", "lee", "ana", "bo"})
    rows = [_row(0, "Max", "10:00am", "2:00pm", "Manager"), _row(0, "Ana", "10:00am", "3:00pm"),
            _row(0, "Lee", "5:00pm", "10:00pm", "Manager"), _row(0, "Bo", "5:00pm", "10:00pm")]
    gaps = sr.manager_gaps(rows, c)
    assert [(g[0], g[1]) for g in gaps[WEEK[0]]] == [(14 * 60, 15 * 60)]
    # The day's last block keeps the owner's allowance.
    rows[3] = dict(rows[3], shift_end="10:45pm", scheduled_hours="5.75")
    assert [(g[0], g[1]) for g in sr.manager_gaps(rows, c)[WEEK[0]]] == [(14 * 60, 15 * 60)]


# ── TRUST-21: the assumed stay is said, one tap away, and priced ──────────

def test_the_assumed_stay_is_said_with_a_one_tap_and_priced():
    c, rows = _close_night(server_end="11:45pm")
    stays = sr.assumed_stays(rows, c)
    assert len(stays) == 1
    st = stays[0]
    assert st["employee"] == "Max" and st["until"] == "11:45pm" and st["minutes"] == 45 and st["hourly"]
    assert "assumed to stay until 11:45pm, 45 min past the scheduled end of 11:00pm" in st["text"]
    assert "10/9/26" in st["text"]                                         # M/D/YY, never ISO
    assert st["action"]["label"] == "Schedule Max to 11:45pm"
    assert st["action"]["payload"] == {"employee": "Max", "date": WEEK[4], "shift_start": "4:00pm",
                                       "shift_end": "11:00pm", "new_end": "11:45pm"}
    priced = sr.with_assumed_stays(rows, c)
    assert priced[0]["shift_end"] == "11:45pm" and float(priced[0]["scheduled_hours"]) == 7.75
    assert rows[0]["shift_end"] == "11:00pm"                                # the week itself unchanged
    # Salaried: said, never priced.
    c.salaried = {"max"}
    assert not sr.assumed_stays(rows, c)[0]["hourly"]
    assert sr.with_assumed_stays(rows, c)[0]["shift_end"] == "11:00pm"


def test_the_budget_judge_counts_the_assumed_stay():
    c, rows = _close_night(server_end="11:45pm")
    x = se.RepairContext(1, {"hours_budget": 14.0, "week_dates": WEEK}, c)
    # 7h + 7.75h of the server = 14.75h scheduled; the stay adds 0.75h.
    assert x.over_budget(rows) == round(7.0 + 7.75 + 0.75 - 14.0 * 1.02, 2)


def test_a_saved_edit_refreshes_the_stays():
    c, rows = _close_night(server_end="11:45pm")
    saved = {"lines": ["x"], "rule_lines": [], "budget_lines": [], "lead_lines": [],
             "stay_lines": ["old stay"], "assumed_stays": []}
    out = se.refresh_review(saved, sr.violations(rows, c), rows, c)
    assert out["assumed_stays"] and out["stay_lines"] == [out["assumed_stays"][0]["text"]]
    assert out["stay_lines"][0] in out["lines"] and "old stay" not in out["lines"]
    fixed = [dict(rows[0], shift_end="11:45pm"), rows[1]]
    out2 = se.refresh_review(out, sr.violations(fixed, c), fixed, c)
    assert out2["assumed_stays"] == [] and not any("assumed to stay" in ln for ln in out2["lines"])


# ── ARCH-13: the plan covers what the check will ask ──────────────────────

def test_the_plan_window_is_the_checks_requirement_over_the_expected_day():
    import schedule_skeleton as sk
    c = _c(open_times={"Friday": "11:00am"}, close_times={"Friday": "10:00pm"}, role_buffers={"bartender": 60},
           close_mins={"bartender": 30})
    # History says the last person usually leaves at 10:30pm: the plan stops
    # there — not at the longest allowance (11pm) nobody uses.
    spans = {"Friday": {"start": 10 * 60, "end": 22 * 60 + 30}}
    w = sk.day_window(c, WEEK[4], c.open_times, c.close_times, spans)
    assert (w[0], w[1]) == (10 * 60, 22 * 60 + 30)
    assert (w[0], w[1]) == (min(s for s, _e in sr.manager_need(c, WEEK[4], [(10 * 60, 22 * 60 + 30)])),
                            max(e for _s, e in sr.manager_need(c, WEEK[4], [(10 * 60, 22 * 60 + 30)])))
    # A 7am start the owner's rules give the cooks that day is in the window.
    c.role_times = {("cook", "Friday", "morning"): {"start": 7 * 60, "end": None, "source": {}}}
    assert sk.day_window(c, WEEK[4], c.open_times, c.close_times, spans)[0] == 7 * 60
    # History running later than any role may stay is capped at the allowance.
    late = {"Friday": {"start": 10 * 60, "end": 24 * 60 + 60}}
    c.role_times = {}
    assert sk.day_window(c, WEEK[4], c.open_times, c.close_times, late)[1] == 23 * 60


# ── LOGIC-13 / LOGIC-14: the trim in dollars; an only shift is a cost ────

def test_the_trim_counts_dollars_and_cuts_the_dearer_hour_first():
    import schedule_economics as econ
    c = _c()
    rows = [_row(0, "Cook", "4:00pm", "10:00pm", "Line Cook"), _row(0, "Kay", "4:00pm", "10:00pm", "Line Cook"),
            _row(0, "Sam", "4:00pm", "10:00pm", "Server"), _row(0, "Tia", "4:00pm", "10:00pm", "Server")]
    rows += [_row(i, n, "4:00pm", "10:00pm", "Line Cook") for i in (1, 2) for n in ("Cook", "Kay")]
    pricing = {"role_rates": {"Line Cook": 24.0, "Server": 9.0}, "blended_rate": 15.0, "person_rates": {},
               "role_typical": {}, "ceiling": 40.0, "salaried": set(), "base_hours": {}, "bucket": c.bucket}

    def cost(rs):
        return econ.price_week(rs, pricing, rounded=False)["total"]
    # 36h at $24 + 12h at $9 = $972 against $900: a 2h cook tail is $48 —
    # the server's 2h would be only $18.
    assert cost(rows) == 972.0
    out, trimmed, _h = econ.trim_to_budget([dict(r) for r in rows], 60, {}, constraints=c,
                                           cost_fn=cost, budget_value=900.0, pricing=pricing)
    # On the day furthest over, the dearer hour goes first; each cut says
    # what it saved in dollars, and the week ends inside the dollar budget.
    assert trimmed[0]["role"] == "Line Cook" and trimmed[0]["dollars"] == 48.0
    assert all(t["dollars"] == t["hours"] * (24.0 if t["role"] == "Line Cook" else 9.0) for t in trimmed)
    assert cost(out) <= 900 and cost(out) > 900 - 24        # just enough: the last cut is half an hour
    # In hours the same week is within 2% of a 60h budget: no trim at all.
    assert econ.trim_to_budget([dict(r) for r in rows], 60, {}, constraints=c)[1] == []


def test_somebodys_only_shift_is_a_cost_not_a_refusal():
    # Pat's one Tuesday lunch is the week's most discretionary row (Ana has
    # a 12h minimum the owner set): it goes, said as her only shift — the
    # old trim refused it and left the week over budget (LOGIC-14).
    import schedule_economics as econ
    c = _c(hours_limits={"ana": (12, None)})
    rows = [_row(1, "Pat", "11:00am", "3:00pm"), _row(1, "Ana", "11:00am", "3:00pm"),
            _row(2, "Ana", "11:00am", "3:00pm"), _row(3, "Ana", "11:00am", "3:00pm")]
    out, trimmed, _h = econ.trim_to_budget([dict(r) for r in rows], 12, {WEEK[1]: 4.0}, constraints=c)
    assert [(t["employee"], t["kind"]) for t in trimmed] == [("Pat", "removed")]
    assert "Pat's only shift this week" in trimmed[0]["reason"]
    # The cost ranks it after a same-day regular's shift that may go.
    c2 = _c()
    out, trimmed, _h = econ.trim_to_budget([dict(r) for r in rows], 12, {WEEK[1]: 4.0}, constraints=c2)
    assert [t["employee"] for t in trimmed] == ["Ana"]
    # A minimum the owner set for Pat still holds hers.
    c3 = _c(hours_limits={"ana": (12, None), "pat": (4, None)})
    out, trimmed, _h = econ.trim_to_budget([dict(r) for r in rows], 12, {WEEK[1]: 4.0}, constraints=c3)
    assert not any(t["employee"] == "Pat" for t in trimmed)


def test_the_repair_context_holds_the_budget_in_dollars():
    c, rows = _close_night()
    x = se.RepairContext(1, {"hours_budget": 100.0, "labor_budget_dollars": 100.0, "week_dates": WEEK}, c)
    x._pricing = {"role_rates": {"Manager": 20.0, "Server": 10.0}, "blended_rate": 15.0, "person_rates": {},
                  "role_typical": {}, "ceiling": 40.0, "salaried": set(), "base_hours": {}, "bucket": c.bucket}
    # 7h manager at $20 + 7h server at $10 = $210 against $100: over in
    # dollars though 14h is far inside 100h.
    assert x.over_budget(rows) == round(210 - 100 * 1.02, 2)


# ── LOGIC-15 / LEARNING-10: one graded meaning, scoped ────────────────────

def test_learned_cut_evidence_is_scoped_to_the_daypart_and_role_it_named():
    import rec_learning as rl
    levers = {"friday": {"worsened": 4, "measured": 4, "strength": 0.6, "leaner": 0.0,
                         "parts": {"night": {"strength": 0.6, "leaner": 0.0}}, "roles": ["server"],
                         "whole_day": False}}
    assert rl.learned_cut_penalty(levers, "Friday", "night", "server") == 0.6
    assert rl.learned_cut_penalty(levers, "Friday", "morning", "server") == 0.0      # lunch was never cut
    assert rl.learned_cut_penalty(levers, "Friday", "night", "line cook") == 0.0     # cooks were never cut
    assert rl.learned_cut_penalty(levers, "Saturday", "night", "server") == 0.0


# ── LOGIC-16 / LOGIC-17 / BUGSGEN-24: standing positions ──────────────────

def test_a_standing_position_goes_to_the_role_family_never_a_name_that_contains_it():
    import json
    import schedule_dedicated as ded

    class _R:
        dedicated_shifts_json = json.dumps([{"label": "Patio", "family": "server", "parts": {"night": ["Friday"]}}])
    c = _c(roster_names=["Sal", "Ann"], active={"sal", "ann"},
           known_roles={"sal": {"server assistant"}, "ann": {"server"}},
           role_families={"server assistant": "server assistant"})
    hist = [{"date": "2026-09-25", "employee": "Ann", "role": "Server", "shift_start": "5:00pm", "shift_end": "11:00pm"},
            {"date": "2026-09-25", "employee": "Sal", "role": "Server Assistant", "shift_start": "4:00pm",
             "shift_end": "9:00pm"}]
    p = ded.plan(c, WEEK, ded.rules_of(_R()), history=hist, roster_roles={"Sal": "Server Assistant", "Ann": "Server"})
    assert [(r["employee"], r["role"], r["shift_start"]) for r in p["rows"]] == [("Ann", "Server", "5:00pm")]


def test_the_standing_position_stays_with_last_weeks_person():
    import json
    import schedule_dedicated as ded

    class _R:
        dedicated_shifts_json = json.dumps([{"label": "Bar tables", "family": "bartender",
                                             "parts": {"night": ["Friday", "Saturday"]}}])
    names = ["Ana", "Ben", "Cy"]
    c = _c(roster_names=names, active={n.lower() for n in names}, known_roles={n.lower(): {"bartender"} for n in names})
    hist = []
    for d in ("2026-09-25", "2026-09-26", "2026-10-02", "2026-10-03"):
        hist.append({"date": d, "employee": "Cy", "role": "Bartender", "shift_start": "5:00pm", "shift_end": "1:00am",
                     "notes": "Bar tables — " + ded.NOTE_MARK})
        hist.append({"date": d, "employee": "Ana", "role": "Bartender", "shift_start": "6:00pm", "shift_end": "2:00am"})
    p = ded.plan(c, WEEK, ded.rules_of(_R()), history=hist, roster_roles={n: "Bartender" for n in names})
    # Cy took bar tables last week — not whoever had the fewest planned
    # hours (Ana, then Ben, alphabetically) — so Cy keeps it.
    assert [r["employee"] for r in p["rows"]] == ["Cy", "Cy"]


# ── LOGIC-20: the prompt's order is the code's ────────────────────────────

def test_the_prompt_priorities_are_the_codes_tier_table_in_rank_order():
    import schedule_prompt as sp
    pri = sp.PRIORITIES
    pos = {}
    for tier, _name, words in sr.TIER_TABLE:
        assert words in pri, tier
        pos[tier] = pri.index(words)
    ranked = [sr.TIER_COVERAGE, sr.TIER_POLICY, sr.TIER_OVERTIME, sr.TIER_MIN_HOURS, sr.TIER_REQUIREMENT,
              sr.TIER_BUDGET, sr.TIER_QUALITY]
    assert [pos[t] for t in ranked] == sorted(pos[t] for t in ranked)
    assert pos[sr.TIER_LEGAL] < pos[sr.TIER_COVERAGE] and pos[sr.TIER_MANAGER] < pos[sr.TIER_COVERAGE]
    assert {t for _k, t in sr.PRIORITY_LAYOUT} == {t for t, _n, _w in sr.TIER_TABLE}
    # Leadership is a quality — met by who works, never hours past the budget.
    assert pri.index("leadership") > pos[sr.TIER_BUDGET]


# ── BUGSGEN-21: next week's overtime line sees this week's days to come ──

def test_the_begun_payroll_weeks_days_to_come_count_their_usual_hours(monkeypatch):
    import shift_facts
    c = _c()
    c.week_start_day = 2                             # Wednesday payroll, a Monday schedule week
    punches = []
    for w in range(1, 5):
        for wd_off in (4, 5, 6):                     # the Fri-Sun before the week, four weeks back
            d = (date(2026, 10, 5) - timedelta(days=7 - wd_off) - timedelta(weeks=w)).isoformat()
            punches.append({"date": d, "employee": "Ana", "actual_hours": 9.0})
    monkeypatch.setattr(shift_facts, "person_rows", lambda *a, **k: punches)
    ahead = ["2026-10-02", "2026-10-03", "2026-10-04"]                 # Fri-Sun, today is Thursday 10/1
    sr._projected_tail(c, 1, ahead, "2026-10-01", sr.DB_PATH)
    b = c.bucket(WEEK[0])
    assert c.base_hours["ana"][b] == 27.0
    assert c.projected_tail == {"dates": ahead, "hours": 27.0, "people": 1}
    # Monday and Tuesday of the draft now push her past the line.
    rows = [_row(0, "Ana", "9:00am", "5:00pm"), _row(1, "Ana", "9:00am", "5:00pm")]
    assert any(v["kind"] == "over_hours_ceiling" for v in sr.violations(rows, c))
    items = __import__("schedule_setup").setup_review(c)["items"]
    assert any(i["kind"] == "projected_tail" and "10/2/26–10/4/26" in i["text"] for i in items)


def test_the_publish_gate_holds_the_budget_in_dollars(db_path, monkeypatch):
    # 16h against a 40h hours budget reads fine in hours; at $45 an hour it
    # is $720 of pay against a $600 hourly budget (LOGIC-13).
    import json
    import client_api
    import models
    import staff_settings as ss
    real = models.get_conn
    for mod in (models, client_api, sr, se, ss):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = models.create_restaurant(models.Restaurant(name="Dollar Co", owner_email="d@x.test"), db_path=db_path)
    for n in ("Ana", "Bob"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
        ss.upsert(rid, n, floor_manager=True, db_path=db_path)
    models.update_restaurant(rid, {"role_rates_json": json.dumps({"Server": 45})}, db_path=db_path)
    start = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 + 7)
    d0 = start.isoformat()
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           f"{d0},Monday,Ana,Server,9:00am,5:00pm,8,\n{d0},Monday,Bob,Server,9:00am,5:00pm,8,\n")
    conn = real(db_path)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                 "labor_target, schedule_csv, summary_json, economics_json) VALUES (?,?,?,?,?,?,?,'[]',?)",
                 (rid, d0, (start + timedelta(days=6)).isoformat(), 16, 40, 30, csv,
                  json.dumps({"labor_budget_dollars": 600, "blended_rate": 45})))
    conn.commit()
    conn.close()
    gate = client_api.publish_blockers(rid)
    over = [b for b in gate if "hourly pay against a $600 hourly budget" in str(b)]
    assert over, gate


def test_weekly_overtime_is_said_once_and_holds_only_an_unattended_publish(db_path, monkeypatch):
    import client_api
    import models
    import staff_settings as ss
    real = models.get_conn
    for mod in (models, client_api, sr, se, ss):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    rid = models.create_restaurant(models.Restaurant(name="OT Co", owner_email="ot@x.test"), db_path=db_path)
    for n in ("Ana", "Bob"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
        ss.upsert(rid, n, floor_manager=True, db_path=db_path)
    start = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 + 7)
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"]
    for i in range(6):
        d = start + timedelta(days=i)
        lines.append(f"{d.isoformat()},{d.strftime('%A')},Ana,Server,9:00am,5:00pm,8,")
    conn = real(db_path)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                 "labor_target, schedule_csv, summary_json) VALUES (?,?,?,?,?,?,?,'[]')",
                 (rid, start.isoformat(), (start + timedelta(days=6)).isoformat(), 48, 0, 30, "\n".join(lines) + "\n"))
    conn.commit()
    conn.close()
    review = client_api.publish_review(rid)
    assert not any("weekly ceiling" in b["text"] for b in review["blockers"])          # a person may send it
    soft = [s for s in review["soft"] if "weekly ceiling" in str(s)]
    assert len(soft) == 1, review["soft"]                                             # once, not six times
    auto = client_api.publish_review(rid, unattended=True)
    assert sum(1 for b in auto["blockers"] if "weekly ceiling" in b["text"]) >= 1       # never sent unread
