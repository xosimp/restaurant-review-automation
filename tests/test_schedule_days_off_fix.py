"""#17 — a missed run of days off is fixable in the fix pass.

schedule_rules flags "days_off" (fewer consecutive days off than the rule)
as a soft breach, and until now nothing repaired it: the owner was told
and the draft stayed as it was. apply_fixes now hands one of the person's
shifts to a legal teammate — the least score cost — and never creates a new
hard breach doing it.
"""
import schedule_engine as se
import schedule_rules as sr
import shift_quality as sq

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, role="Server", start="4:00pm", end="10:00pm"):
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "6", "notes": ""}


def _week():
    # Ana works every day but Wednesday: two days off would need two together.
    rows = [_row(i, "Ana") for i in range(7) if i != 2]
    rows += [_row(i, "Bob") for i in (0, 1, 2)]
    rows += [_row(i, "Cal") for i in (4, 5)]
    return rows


def _fix(rows, c, **kw):
    v = sr.violations(rows, c)
    return sq.apply_fixes(rows, sr.fixable(v), roster=["Ana", "Bob", "Cal"],
                          rules=se._rules_for_swaps(c), rule_constraints=c,
                          typical_headcount={(d, "night"): {"Server": 2} for d in DAYS}, **kw)


def test_days_off_is_handed_to_the_fix_pass():
    c = _c()
    viols = sr.violations(_week(), c)
    assert any(v["kind"] == "days_off" and v["employee"] == "Ana" for v in viols)
    assert any(v["kind"] == "days_off" for v in sr.fixable(viols))
    assert all(v["hard"] or v["kind"] == "days_off" for v in sr.fixable(viols))


def test_a_missed_day_off_is_fixed_by_handing_one_shift_to_a_teammate():
    c = _c()
    out = _fix(_week(), c)
    moved = [f for f in out["fixes"] if f["kind"] == "days_off"]
    assert len(moved) == 1 and moved[0]["from"] == "Ana"
    # the shift that goes is next to her Wednesday off, so she gets two together
    assert out["rows"][moved[0]["index"]]["date"] in (WEEK[1], WEEK[3])
    after = sr.violations(out["rows"], c)
    assert not any(v["kind"] == "days_off" for v in after)
    assert not any(v["hard"] for v in after)
    assert "days off" in out["rows"][moved[0]["index"]]["notes"]


def test_the_fix_never_creates_a_new_hard_breach():
    """Bob and Cal can only take the shift by breaking a rule (Bob is on time
    off every day, Cal would pass the hours ceiling): nothing moves."""
    c = _c(blocked_dates={"bob": {d: "on approved time off" for d in WEEK}},
           hours_limits={"cal": (None, 12)})
    rows = [_row(i, "Ana") for i in range(7) if i != 2] + [_row(i, "Cal") for i in (4, 5)]
    before = {(v["index"], v["kind"]) for v in sr.violations(rows, c) if v["hard"]}
    out = _fix(rows, c)
    assert not [f for f in out["fixes"] if f["kind"] == "days_off"]
    assert any(u["kind"] == "days_off" and u["employee"] == "Ana" for u in out["unfixed"])
    after = {(v["index"], v["kind"]) for v in sr.violations(out["rows"], c) if v["hard"]}
    assert after <= before


def test_the_teammate_taken_is_the_one_that_costs_the_least_score():
    """Two legal teammates; one is a level 5 and the busy Tuesday wants
    strength, so the score picks her."""
    c = _c()
    rows = [_row(i, "Ana") for i in range(7) if i != 2]
    rows += [_row(0, "Weak"), _row(0, "Strong")]
    profiles = [sq.ShiftProfile(key="n", min_strength={"Server": 4}, source="restaurant")]
    v = sr.violations(rows, c)
    out = sq.apply_fixes(rows, sr.fixable(v), profiles=profiles, roster=["Ana", "Weak", "Strong"],
                         rules=se._rules_for_swaps(c), rule_constraints=c,
                         scores={"Ana": 4, "Weak": 2, "Strong": 5})
    moved = [f for f in out["fixes"] if f["kind"] == "days_off"]
    assert moved and moved[0]["to"] == "Strong"


def test_a_partial_redo_only_hands_over_rows_on_the_editable_days():
    c = _c()
    out = _fix(_week(), c, only_dates={WEEK[5], WEEK[6]})
    moved = [f for f in out["fixes"] if f["kind"] == "days_off"]
    assert all(out["rows"][f["index"]]["date"] in (WEEK[5], WEEK[6]) for f in moved)


def test_without_the_rules_nothing_is_moved_and_it_says_why():
    rows = _week()
    v = sr.violations(rows, _c())
    out = sq.apply_fixes(rows, sr.fixable(v), roster=["Ana", "Bob", "Cal"])
    assert out["rows"] == rows
    assert any(u["kind"] == "days_off" for u in out["unfixed"])
