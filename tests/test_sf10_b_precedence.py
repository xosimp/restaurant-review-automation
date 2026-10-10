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
    merged = compliance_packs.apply(dict(sr.DEFAULTS), "IL")
    assert set(merged["_pack"]["law"]) == {"min_rest_hours", "max_consecutive_days"}
    weaker = compliance_packs.apply(dict(sr.DEFAULTS, min_rest_hours=8), "IL", owner_set={"min_rest_hours": 8})
    assert "min_rest_hours" not in weaker["_pack"]["law"]
    assert compliance_packs.apply(dict(sr.DEFAULTS), "CA")["_pack"]["law"] == []


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
