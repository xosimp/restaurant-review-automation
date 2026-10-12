"""Workstream K of the 10/11/26 schedule fix round: measurement honesty.

The score must condemn 12-hour doubles, see a missing role, and not rise as
it knows less; savings are scheduled against scheduled and never reward a
thin week; the forecast's record says whether it beats a naive guess; the
schedule states the forecast it was sized on beside the one Home shows; and
the budget line says which budget binds.
"""
import schedule_economics as E


# ── BUGSGEN-22: the budget line never contradicts itself ────────────────────

def test_dollars_over_with_hours_at_budget_says_the_dollar_budget_binds():
    b = E.binding_budget(14390, 14092, 988, 988, overtime_premium=0)
    assert b["binds"] == "dollars"
    assert "988h scheduled against 988h" not in b["note"]
    assert "the hours are within budget" in b["note"]
    assert "the dollar budget is the one that binds" in b["note"]
    # The mix is paid above the blended rate the hours budget assumes.
    assert b["assumed_rate"] == round(14092 / 988, 2)
    assert b["paid_rate"] == round(14390 / 988, 2)
    assert f"${b['paid_rate']:,.2f}/h" in b["note"] and f"${b['assumed_rate']:,.2f}/h" in b["note"]


def test_overtime_premium_is_named_when_it_drives_the_overrun():
    b = E.binding_budget(10300, 10000, 700, 710, overtime_premium=280)
    assert b["binds"] == "dollars"
    assert "$280 of it is overtime premium" in b["note"]


def test_both_over_keeps_the_hours_beside_the_dollars():
    b = E.binding_budget(15000, 14000, 1050, 988)
    assert b["binds"] == "both"
    assert b["note"] == "1,050h scheduled against 988h"


def test_under_budget_is_not_a_binding_case():
    assert E.binding_budget(9000, 10000, 700, 700)["binds"] is None


def test_trim_conflict_line_names_the_binding_budget():
    line = E.budget_conflict_line({"over_by_dollars": 298, "budget_dollars": 14092, "scheduled": 988,
                                   "budget": 988, "held": {"rule": 1},
                                   "examples": [{"reason": "rule", "label": "manager on every shift"}]})
    assert "988h against 988h" not in line
    assert "the dollar budget is the one that binds" in line
