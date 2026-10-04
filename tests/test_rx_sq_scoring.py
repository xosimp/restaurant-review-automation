"""Schedule re-audit 10/4/26, lens SQ: the week score, the optimiser and the
money and hours figures behind the schedule (SQ-2..7, SQ-9, SQ-10, SQ-12).
Each test is the auditor's reproduction turned into an assertion."""
from datetime import date, timedelta

import schedule_optimizer as so
import schedule_rules as R
import shift_quality as sq

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]
DAYS = [sq._day_name(d) for d in WEEK]


def _two_shift_week(gap_on=None):
    """14 shifts, a manager and two servers on each; `gap_on` dates have the
    dinner manager leave at 7pm, three hours before the 10pm close."""
    rows, names = [], set()
    for i, d in enumerate(WEEK):
        for s, e, tag in (("10:00am", "4:00pm", "a"), ("4:00pm", "10:00pm", "p")):
            m = f"Mgr{tag}{i}"
            rows.append({"employee": m, "role": "Manager", "date": d, "shift_start": s,
                         "shift_end": "7:00pm" if (tag == "p" and d in (gap_on or ())) else e})
            names.add(m)
            for k in range(2):
                n = f"Srv{tag}{i}{k}"
                rows.append({"employee": n, "role": "Server", "date": d, "shift_start": s, "shift_end": e})
                names.add(n)
    return rows, names


def _typical():
    return {(wd, p): {"Server": 2, "Manager": 1} for wd in DAYS for p in ("morning", "night")}


def _constraints(names):
    names = set(names) | {"Boss"}
    mgrs = {n.lower(): "Manager" for n in names if n.startswith("Mgr")}
    mgrs["boss"] = "Owner"
    return R.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS, roster_names=sorted(names),
                         active={n.lower() for n in names}, managers=mgrs)


# ── SQ-2: a broken hard rule holds the week, not only its shift ────────────

def test_sq2_manager_gap_holds_the_week_under_fair():
    rows, _n = _two_shift_week()
    clean = sq.score_rows(rows, typical_headcount=_typical())
    assert clean["score"] >= 90 and clean["held_by"] is None
    hb = [{"kind": "no_manager", "date": "2026-10-10", "hard": True,
           "label": "No manager on the floor 7:00 PM–10:00 PM", "gap_start": 19 * 60, "gap_end": 22 * 60}]
    q = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=hb)
    assert q["score"] <= sq.WEEK_HARD_BREACH_CEILING
    assert q["band"] == "weak"
    assert q["raw_score"] <= sq.WEEK_HARD_BREACH_CEILING
    assert q["held_by"]["key"] == sq.HARD_RULES_KEY
    assert "No manager on the floor" in q["held_by"]["text"]
    assert q["weaknesses"][0] == q["held_by"]["text"]


def test_sq2_more_breaches_score_lower_and_a_fix_is_worth_points():
    rows, _n = _two_shift_week()
    one = [{"kind": "no_manager", "date": "2026-10-10", "hard": True, "label": "No manager",
            "gap_start": 19 * 60, "gap_end": 22 * 60}]
    three = [dict(one[0], date=d) for d in ("2026-10-08", "2026-10-09", "2026-10-10")]
    q1 = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=one)
    q3 = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=three)
    # The hold scales rather than clips: fixing one breach of three still
    # moves the number a search chooses by.
    assert q3["raw_score"] < q1["raw_score"]
    rec = next(r for r in q1["recommendation_details"] if r["kind"] == "rules")
    clean = sq.score_rows(rows, typical_headcount=_typical())
    # Fixing the only breach lifts the week's hold too.
    assert rec["points"] >= clean["raw_score"] - q1["raw_score"] - 1


# ── SQ-3: the optimiser puts a hard breach right before anything else ──────

def test_sq3_optimizer_closes_a_manager_gap_and_never_stops_at_target_with_one_open():
    rows, names = _two_shift_week(gap_on=("2026-10-10",))
    c = _constraints(names)
    assert [v["kind"] for v in R.violations(rows, c) if v.get("hard")] == ["no_manager"]
    hard = [v for v in R.violations(rows, c) if v.get("hard")]
    q = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=hard)
    top = so._problems(q)[0]
    assert top[2]["key"] == sq.HARD_RULES_KEY and top[1]["date"] == "2026-10-10"
    # A target the held week already clears may not stop the search.
    out = so.optimize(rows, inputs={"constraints": c}, signals={"typical_headcount": _typical()},
                      constraints=c, max_seconds=20, target=10)
    assert not [v for v in R.violations(out["rows"], c) if v.get("hard")]
    assert out["after_score"] > out["before_score"]
    assert any("Saturday" in ch["reason"] for ch in out["changes"])


def test_sq3_a_rule_move_is_not_held_to_the_hours_budget():
    rows, names = _two_shift_week(gap_on=("2026-10-10",))
    c = _constraints(names)
    hours = R.hourly_hours(rows, c)
    out = so.optimize(rows, inputs={"constraints": c}, signals={"typical_headcount": _typical()},
                      constraints=c, max_seconds=20, hours_budget=hours)
    assert not [v for v in R.violations(out["rows"], c) if v.get("hard")]
