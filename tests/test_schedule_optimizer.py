"""The Shift Quality repair loop (schedule_optimizer).

What it must never do matters as much as what it improves: every move is
legal (availability, hours, the rule sweep), the hours budget stays a
ceiling, nothing is hidden (each change has a sentence), and a week with
nothing to score comes back untouched.
"""
import shift_quality as sq
import schedule_optimizer as so

SAT, SUN, FRI, MON = "2026-09-12", "2026-09-13", "2026-09-11", "2026-09-07"


def row(date, name, role, start="5:00pm", end="10:00pm", hours=5):
    return {"date": date, "day": sq._day_name(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": hours, "notes": ""}


def _signals(**kw):
    base = {"roster": ["Ann", "Bob", "Cat", "Dee", "Eve"],
            "typical_headcount": {("Saturday", "night"): {"Server": 3}},
            "availability": {}, "constraints": {}, "scores": {}}
    base.update(kw)
    return base


def test_a_short_role_on_a_busy_night_is_filled_by_someone_legal():
    rows = [row(SAT, "Ann", "Server"), row(FRI, "Bob", "Server"), row(FRI, "Cat", "Server")]
    res = so.optimize(rows, {}, signals=_signals())
    assert res["after_score"] > res["before_score"]
    added = [c for c in res["changes"] if c["kind"] == "add"]
    assert added and all("Saturday" in c["reason"] for c in added)
    sat = [r for r in res["rows"] if r["date"] == SAT]
    assert len(sat) == 3
    assert all(r["notes"].startswith(so.NOTE_TAG) for r in sat if r["employee"] != "Ann")


def test_it_never_puts_someone_on_a_day_they_are_unavailable():
    rows = [row(SAT, "Ann", "Server"), row(FRI, "Bob", "Server"), row(FRI, "Cat", "Server")]
    res = so.optimize(rows, {}, signals=_signals(availability={"Bob": {"Saturday"}, "Cat": {"Saturday"}}))
    assert not any(r["employee"] in ("Bob", "Cat") and r["date"] == SAT for r in res["rows"])


def test_it_never_pushes_anyone_past_their_weekly_ceiling():
    rows = [row(SAT, "Ann", "Server")] + [row(d, "Bob", "Server", start="9:00am", end="7:00pm", hours=10)
                                          for d in ("2026-09-07", "2026-09-08", "2026-09-09", FRI)]
    res = so.optimize(rows, {}, signals=_signals(roster=["Ann", "Bob"]))
    bob = sum(float(r["scheduled_hours"]) for r in res["rows"] if r["employee"] == "Bob")
    assert bob <= 40.01


def test_the_hours_budget_stays_a_ceiling():
    rows = [row(SAT, "Ann", "Server"), row(FRI, "Bob", "Server"), row(FRI, "Cat", "Server")]
    week = sum(r["scheduled_hours"] for r in rows)
    res = so.optimize(rows, {}, signals=_signals(), hours_budget=week)
    after = sum(float(r["scheduled_hours"]) for r in res["rows"])
    assert after <= week * so.BUDGET_TOLERANCE + 0.01


def test_a_week_with_nothing_configured_comes_back_untouched():
    rows = [row(SAT, "Ann", "Server")]
    res = so.optimize(rows, {}, signals={"roster": ["Ann"]})
    assert res["changes"] == [] and res["rows"] == rows
    assert res["stopped"] == "nothing to score"


def test_the_input_rows_are_not_modified():
    rows = [row(SAT, "Ann", "Server"), row(FRI, "Bob", "Server"), row(FRI, "Cat", "Server")]
    snapshot = [dict(r) for r in rows]
    so.optimize(rows, {}, signals=_signals())
    assert rows == snapshot


def test_a_missing_closer_is_fixed_by_a_swap_with_the_one_who_can_close():
    """Only Dee may close the bar. She is on Monday, where nothing needs
    her; Saturday needs her. Swapping her with Ann fixes Saturday."""
    rows = [row(SAT, "Ann", "Bartender"), row(MON, "Dee", "Bartender")]
    rule = {"role": "Bartender", "count": 1, "attribute": "can_close", "days": ["Saturday"]}
    res = so.optimize(rows, {}, signals=_signals(
        roster=["Ann", "Dee"], typical_headcount={("Saturday", "night"): {"Bartender": 1}},
        leader_rules=[rule], leader_flags={"Dee": True}, scores={"Ann": 3, "Dee": 3}))
    sat = [r for r in res["rows"] if r["date"] == SAT]
    assert [r["employee"] for r in sat] == ["Dee"]
    assert any(c["kind"] in ("swap", "replace") for c in res["changes"])


def test_what_it_cannot_fix_is_said_with_why():
    rows = [row(SAT, "Ann", "Bartender"), row(SUN, "Ann", "Bartender", start="4:00pm")]
    rule = {"role": "Bartender", "count": 1, "attribute": "can_close"}
    sig = _signals(roster=["Ann"], typical_headcount={}, leader_rules=[rule], leader_flags={},
                   scores={"Ann": 3})
    res = so.optimize(rows, {}, signals=sig)
    s = so.summary(res, sig)
    assert s["ran"] and not s["applied"]
    assert any("authorised to close" in u["text"] for u in s["unresolved"])


def test_the_summary_lists_every_change_with_its_reason():
    rows = [row(SAT, "Ann", "Server"), row(FRI, "Bob", "Server"), row(FRI, "Cat", "Server")]
    res = so.optimize(rows, {}, signals=_signals())
    s = so.summary(res)
    assert s["applied"] and s["after_score"] >= s["before_score"]
    assert len(s["changes"]) == len(res["changes"]) and all(c["reason"] for c in s["changes"])
    assert str(s["before_score"]) in s["verdict"] and str(s["after_score"]) in s["verdict"]


def test_a_swap_that_does_not_raise_an_over_ceiling_persons_hours_is_legal():
    """Somebody already past 40 may still trade a shift for one no longer."""
    rows = [row(d, "Big", "Server", start="9:00am", end="7:00pm", hours=10)
            for d in ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", FRI)]
    rows.append(row(SAT, "Small", "Server", start="9:00am", end="7:00pm", hours=10))
    ix = sq._SwapIndex(rows, {}, {}, {})
    assert ix.total_hours("big") > ix.cap("big")
    assert ix.legal(0, 5, {})


# ── the quality gate (schedule_engine._quality_gate) ───────────────────────

def _q(shifts):
    return {"quality": {"checked": True, "shifts": shifts}}


def _shift(date, day, score, capped, demand="peak", lines=("Server short 3 of 7.",)):
    return {"date": date, "day": day, "daypart": "night", "scored": True, "score": score,
            "capped_by": capped, "profile": {"demand": demand},
            "dimensions": [{"key": capped or "coverage", "weaknesses": list(lines)}]}


def test_the_gate_regenerates_a_busy_night_left_with_a_staffing_hole():
    import schedule_engine as se
    g = se._quality_gate(_q([_shift(SAT, "Saturday", 7, "coverage_curve"), _shift(FRI, "Friday", 80, None)]))
    assert g["dates"] == [SAT]
    assert any("Saturday" in f and "Server short 3 of 7" in f for f in g["focus"])


def test_the_gate_leaves_roster_limits_and_quiet_shifts_alone():
    import schedule_engine as se
    assert se._quality_gate(_q([_shift(SAT, "Saturday", 0, "leadership")])) is None
    assert se._quality_gate(_q([_shift(MON, "Monday", 20, "coverage", demand="low")])) is None
    assert se._quality_gate(_q([_shift(SAT, "Saturday", 75, "coverage")])) is None


def test_learned_headcount_moves_the_requirement_both_ways(monkeypatch):
    import labor, schedule_learning
    monkeypatch.setattr(schedule_learning, "learned_headcount_adjustments",
                        lambda rid: {("Friday", "night"): {"server": 1, "Busser": -1},
                                     ("Monday", "morning"): {"Host": 1}})
    out = labor.apply_learned_headcount(1, {("Friday", "night"): {"Server": 4, "Busser": 1}})
    assert out[("Friday", "night")] == {"Server": 5}
    assert out[("Monday", "morning")] == {"Host": 1}
