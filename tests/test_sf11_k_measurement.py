"""Workstream K of the 10/11/26 schedule fix round: measurement honesty.

The score must condemn 12-hour doubles, see a missing role, and not rise as
it knows less; savings are scheduled against scheduled and never reward a
thin week; the forecast's record says whether it beats a naive guess; the
schedule states the forecast it was sized on beside the one Home shows; and
the budget line says which budget binds.
"""
import json
from datetime import date, timedelta

import models
import prepublish
import schedule_economics as E
from tests.test_ios_parity_labor import _rid, db  # noqa: F401


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


# ── TRUST-30: the forecast's record against a naive guess ───────────────────

def _forecast_weeks_with_naive(rid, weeks):
    """weeks: [(predicted, actual, naive_last)]"""
    conn = models.get_conn()
    for i, (p, a, n) in enumerate(weeks):
        end = (date(2026, 1, 4) + timedelta(days=7 * i)).isoformat()
        e = abs(p - a) / a * 100
        conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
                     "signed_error_pct, naive_last, scored_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
                     (rid, "revenue_week", end, p, a, e, (p - a) / a * 100, n))
    conn.commit()
    conn.close()


def _priced_week(db, rid):
    for name, role in (("Erik", "Manager"), ("Ana", "Server")):
        models.add_manual_team_member(rid, name, role=role, db_path=db)
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           "2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,\n2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,")
    hid = models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 10, 0, 30, csv, [], db_path=db)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET economics_json=? WHERE id=?",
                 (json.dumps({"projected_revenue": 2000, "labor_budget_dollars": 400, "blended_rate": 20}), hid))
    conn.commit()
    conn.close()
    return hid


def test_a_forecast_worse_than_last_week_withholds_the_range(db, monkeypatch):
    rid = _rid(db)
    # Within 5% every week — but last week's sales were within 1%.
    _forecast_weeks_with_naive(rid, [(10500, 10000, 10050)] * 6)
    rec = prepublish.forecast_record(rid)
    assert rec["beats_last_week"] == 0 and rec["vs_last_week"] == 6
    assert rec["skill"] is not None and rec["skill"] < 0
    assert not prepublish.forecast_has_skill(rec)
    hid = _priced_week(db, rid)
    monkeypatch.setattr(prepublish, "_labor_target", lambda r: 30)
    lp = prepublish.review(rid, hid)["numbers"]["labor_pct"]
    assert lp["low"] is None and lp["high"] is None
    assert lp["track_record"].startswith("Beats last week's sales as a guess on 0 of the last 6 weeks")
    assert "no range" in lp["track_record"]
    assert lp["forecast_skill"]["range_withheld"] is True


def test_a_forecast_that_beats_last_week_says_so_beside_its_range(db, monkeypatch):
    rid = _rid(db)
    _forecast_weeks_with_naive(rid, [(10100, 10000, 11000)] * 5 + [(10000, 10000, 9000)])
    rec = prepublish.forecast_record(rid)
    assert rec["beats_last_week"] == 6 and rec["skill"] > 0
    hid = _priced_week(db, rid)
    monkeypatch.setattr(prepublish, "_labor_target", lambda r: 30)
    lp = prepublish.review(rid, hid)["numbers"]["labor_pct"]
    assert lp["low"] is not None and lp["high"] is not None
    assert "beats last week's sales as a guess on 6 of the last 6 weeks" in lp["track_record"]


def test_no_skill_earns_no_confidence(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(prepublish, "forecast_record", lambda r: {
        "weeks": 8, "within_pct": 5, "weeks_within": 7, "mean_error_pct": 3.0, "skill": -0.4, "n_skill": 8,
        "beats_last_week": 1, "vs_last_week": 8, "last": None})
    out = prepublish._sure(rid, None, None, {})
    assert out["confidence"]["pct"] == 0
    assert "no better than a naive guess" in out["confidence"]["basis"][0]
