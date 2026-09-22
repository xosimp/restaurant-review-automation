"""Edge cases of the Shift Quality Engine and its fix pass (SCHED audit).

A leader rule nobody on the roster can ever meet, the fix pass's cost on a
large week, a fix that hands a shift to someone the sweep would flag, and a
manager's rescores switching the advice layer off.

xfail(strict=True) marks a confirmed defect, asserting the correct
behaviour; the marker comes off with the fix.
"""
import sys

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import models
import schedule_engine as se
import schedule_intel as si
import schedule_rules as sr
import shift_quality as sq
from models import create_restaurant, Restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = list(sr.DAYS)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        # The real one, or a redirect some earlier test left bound in a
        # module it imported for the first time mid-test.
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _row(date, emp, role, start="5:00pm", end="10:00pm", hours=5):
    return {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _bar_week(dates):
    rows = []
    for d in dates:
        for i, role in enumerate(["Bartender", "Bartender", "Server", "Server", "Server", "Cook", "Cook"]):
            rows.append(_row(d, f"{role[0]}{i}", role))
    return rows


# ── SCHED-30: a leader rule nobody can meet ───────────────────────────────

_RULE = {"role": "Bartender", "count": 1, "min_score": 5, "daypart": "night"}


@pytest.mark.xfail(strict=True, reason="SCHED-30: a rule nobody on the roster can meet still caps the shift, only lowering confidence")
def test_a_leader_rule_nobody_can_meet_does_not_cap_the_shift(db):
    rows = _bar_week(WEEK[4:6])
    scores = {r["employee"]: 4 for r in rows}                    # the best anybody is rated is 4
    q = sq.score_rows(rows, scores=scores, leader_rules=[dict(_RULE, days=["Saturday"])], unsatisfiable=1)
    sat = next(s for s in q["shifts"] if s["day"] == "Saturday")
    assert not sat.get("capped_by"), sat.get("capped_by")
    assert not any("Move somebody who clears" in r for r in q["recommendations"])


@pytest.mark.xfail(strict=True, reason="SCHED-30: an unsatisfiable rule over every night makes the week 'weak', a permanent publish blocker")
def test_a_leader_rule_nobody_can_meet_never_makes_the_week_weak_and_is_named_once(db):
    rows = _bar_week(WEEK)
    scores = {r["employee"]: 4 for r in rows}
    q = sq.score_rows(rows, scores=scores, leader_rules=[dict(_RULE)], unsatisfiable=1)
    assert q["band"] != "weak", (q["score"], q["band"])
    text = " ".join(q.get("weaknesses", []) + q.get("recommendations", []) + q["confidence"].get("reasons", []))
    assert "could not be met" in text or "nobody" in text.lower()


def test_a_leader_rule_the_roster_can_meet_still_caps_a_shift_that_misses_it(db):
    rows = _bar_week(WEEK[4:6])
    scores = {r["employee"]: 4 for r in rows}
    scores["B0"] = 5                                              # a qualified bartender exists...
    rows = [r for r in rows if not (r["day"] == "Saturday" and r["employee"] == "B0")]   # ...but is off Saturday
    q = sq.score_rows(rows, scores=scores, leader_rules=[dict(_RULE, days=["Saturday"])])
    sat = next(s for s in q["shifts"] if s["day"] == "Saturday")
    assert sat.get("capped_by")


# ── SCHED-39: the fix pass's cost ─────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-39: apply_fixes re-scores the whole week as a baseline for every hard violation (~0.18s each)")
def test_the_fix_pass_scores_the_baseline_once_not_once_per_violation(monkeypatch):
    rows = [_row(WEEK[i % 7], f"P{i}", f"Role{i}", "8:00am", "4:00pm", 8) for i in range(60)]   # nobody shares a role
    hard = [sr._v("rest_gap", i, rows[i], "x") for i in range(30)]
    calls = []
    real = sq.score_rows

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(sq, "score_rows", counted)
    out = sq.apply_fixes(rows, hard, roster=[r["employee"] for r in rows])
    assert out["fixes"] == [] and len(out["unfixed"]) == 30
    assert len(calls) <= 1 + out["evaluated"], len(calls)


def test_the_fix_pass_caps_candidate_evaluations(monkeypatch):
    rows = [_row(WEEK[0], f"P{i}", "Server", "8:00am", "4:00pm", 8) for i in range(20)]
    rows += [_row(WEEK[1], f"P{i}", "Server", "8:00am", "4:00pm", 8) for i in range(20)]
    hard = [sr._v("rest_gap", i, rows[i], "x") for i in range(20)]
    out = sq.apply_fixes(rows, hard, roster=[f"P{i}" for i in range(20)], max_evaluations=5)
    assert out["evaluated"] <= 5


# ── SCHED-6 (fix pass): a fix must not create a new hard violation ────────

@pytest.mark.xfail(strict=True, reason="SCHED-6: the swap rules carry no minors or certifications, so the fix pass hands a close to a minor")
def test_a_fix_never_hands_a_late_close_to_a_minor(db):
    rid = create_restaurant(Restaurant(name="Fix Co", owner_email="f@x.com"), db_path=db)
    for n in ("Ana", "Dev"):
        models.add_manual_team_member(rid, n, role="Bartender", db_path=db)
    staff_settings.upsert(rid, "Dev", is_minor=True)
    c = sr.build_constraints(rid, WEEK, DAYS)
    rows = [_row(WEEK[4], "Ana", "Bartender", "6:00pm", "1:00am", 7),      # the Friday close, flagged hard
            _row(WEEK[0], "Dev", "Bartender", "11:00am", "3:00pm", 4)]     # a minor bartender, free on Friday
    flagged = [sr._v("rest_gap", 0, rows[0], "not enough rest since their previous shift")]
    out = sq.apply_fixes(rows, flagged, roster=["Ana", "Dev"], rules=se._rules_for_swaps(c))
    after = sr.violations(out["rows"], c)
    assert not [v for v in after if v["kind"] == "minor_late"], out["fixes"]


# ── SCHED-26: rescores counted as showings ────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-26: every rescore records a 'shown', so ten saves in one sitting suppress a kind forever")
def test_twelve_rescores_of_one_week_suppress_no_recommendation_kind(db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Advice Co", owner_email="a@x.com"), db_path=db)
    monkeypatch.setattr(se, "_quality_signals", lambda r, result, **extra: ({}, None))
    monkeypatch.setattr(sq, "score_rows", lambda rows, **k: {"checked": False,
                                                             "recommendations": ["Fill the gap on Saturday night."]})
    rows = [_row(WEEK[5], "Ana", "Server")]
    for _ in range(12):
        quality, _w = se._score_schedule_quality(rid, rows, {})
    assert si.suppressed_kinds(rid) == set()
    assert quality["recommendations"] == ["Fill the gap on Saturday night."]


def test_a_kind_shown_across_ten_weeks_and_never_taken_is_suppressed(db):
    rid = create_restaurant(Restaurant(name="Advice Co", owner_email="a@x.com"), db_path=db)
    for week in range(10):
        si.record_recommendation(rid, "coverage", f"week {week}", "shown")
    assert si.suppressed_kinds(rid) == {"coverage"}
    si.record_recommendation(rid, "coverage", "week 10", "accepted")
    assert si.suppressed_kinds(rid) == set()
