"""Schedule fix round 10/10/26, workstream G1 — calibration v2 (LEARNING-8:
fixed effects, per-outcome fits combined in dollars, held-out weeks, a
corrected threshold search) and calibration where nobody presses Apply
(LEARNING-9: cohort, shadow, bounded promotion, the owner's Undo)."""
import datetime as dt
import json
import random

import pytest

import models
import schedule_calibration as scal
import schedule_intel as si
import schedule_learning as sl
import shift_quality as sq
import staff_settings
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    monkeypatch.setattr(si, "watched_dates", lambda rid, a, b, db_path=None: {
        (dt.date.fromisoformat(a) + dt.timedelta(days=i)).isoformat()
        for i in range((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1)})
    yield


def _rid(name="Cal Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test", module_labor=1))


def _week(i):
    start = dt.date(2026, 6, 1) + dt.timedelta(weeks=i)
    return [(start + dt.timedelta(days=k)).isoformat() for k in range(7)]


def _world(rid, weeks, shift_fn, start=0, seed=11):
    """shift_fn(week, k, rng) -> (date index, cov, issues): one shift.
    `start`: the first week's index (later published weeks)."""
    rng = random.Random(seed)
    conn = models.get_conn()
    for i in range(start, start + weeks):
        days = _week(i)
        shifts, outs = [], []
        for k in range(6):
            di, cov, issues = shift_fn(i, k, rng)
            d = days[di]
            shifts.append({"date": d, "daypart": "night", "scored": True, "score": cov,
                           "dimensions": [{"key": "coverage", "score": cov},
                                          {"key": "fairness", "score": rng.choice([40, 60, 80, 100])}]})
            outs.append((d, issues))
        cur = conn.execute(
            "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
            "labor_target, schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,0,0,30,?,'[]',?,"
            "'2026-10-01')", (rid, days[0], days[6], HEADER, json.dumps({"score": 80, "shifts": shifts})))
        for d, issues in outs:
            conn.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, "
                         "issues) VALUES (?,?,?,?,10,2,?)", (rid, cur.lastrowid, d, "night", issues))
    conn.commit()
    conn.close()


def _confounded(i, k, rng):
    # Three quiet nights (Mon-Wed) and three busy ones (Fri-Sun): the busy
    # ones score higher on coverage AND have more issues — demand, not
    # coverage, drives the issues; inside a night, coverage does nothing.
    busy = k >= 3
    di = (4 + k - 3) if busy else k
    cov = rng.randint(80, 100) if busy else rng.randint(50, 70)
    issues = (2 if busy else 0) + rng.choice([0, 1])
    return di, cov, issues


def test_a_busy_nights_issues_are_not_charged_to_coverage():
    rid = _rid()
    _world(rid, 10, _confounded)
    cal = sl.calibrate_weights(rid, current_weights={})
    cov = cal["dimensions"]["coverage"]
    # Read across nights, coverage "went with" more issues (the old fit
    # cut its weight); within each weekday and daypart it does not.
    assert cov["correlation"]["issues"] > 0.5
    # Within a night it is chance-level (the old joint fit read the full
    # cross-night relationship and cut coverage the whole 30%) ...
    assert cov["evidence"] is None or abs(cov["evidence"]) < 0.3
    # ... and what chance is left does not survive the held-out weeks.
    assert cov["suggested"] == cov["current"], "coverage was cut for the busy nights' demand"
    assert cal["method"] == "v2" and "weekday and daypart" in cal["controls"]
    assert cal["dollar_weights"]["issues"] == sl.CALIBRATION_ISSUE_USD


def _helps(i, k, rng):
    di = k
    cov = rng.choice([50, 60, 70, 80, 90, 100])
    return di, cov, (0 if cov >= 80 else (1 if cov >= 60 else 2))


def test_a_suggestion_must_hold_on_the_newest_held_out_weeks():
    rid = _rid()

    def flips(i, k, rng):
        di, cov, issues = _helps(i, k, rng)
        if i >= 9:                                    # the newest weeks say the opposite
            issues = 2 - issues
        return di, cov, issues
    _world(rid, 12, flips)
    cal = sl.calibrate_weights(rid, current_weights={})
    assert cal["holdout"]["confirmed"] is False
    assert cal["dimensions"]["coverage"]["suggested"] == cal["dimensions"]["coverage"]["current"]
    assert "Held back" in cal["dimensions"]["coverage"]["explanation"]
    rid2 = _rid("Steady Co")
    _world(rid2, 12, _helps)
    ok = sl.calibrate_weights(rid2, current_weights={})
    assert ok["holdout"]["confirmed"] is True and "coverage" in ok["moving"]


def test_a_floor_line_found_by_chance_does_not_move():
    rng = random.Random(3)
    noise = [(float(rng.randint(40, 100)), {"issues": float(rng.choice([0, 1, 2])), "review_rating": None,
                                            "labor_vs_target": None}) for _ in range(80)]
    assert sl._threshold_pvalue(noise, 70, 0.5, "noise") > sl.CALIBRATION_THRESHOLD_ALPHA
    step = [(float(x), {"issues": 0.0 if x >= 60 else 2.0, "review_rating": None, "labor_vs_target": None})
            for x in range(40, 100, 1)]
    fit = sl._threshold_fit(step, 70)
    assert sl._threshold_pvalue(step, 70, fit[1], "step") <= sl.CALIBRATION_THRESHOLD_ALPHA


def test_the_weights_move_on_their_own_within_the_step_and_the_owner_can_undo(monkeypatch):
    # Auto-promotion is OFF by default since the blind re-audit (10/11/26);
    # this test is about what it does when switched on. A candidate that
    # passes the held-out weeks waits (pending) for new published weeks it
    # never saw, and is promoted only when they confirm it (LEARNING-1).
    monkeypatch.setenv(scal.AUTOPROMOTE_ENV, "1")
    rid = _rid()
    _world(rid, 16, _helps)
    first = scal.shadow_run(rid)
    assert first["status"] == "pending", first
    assert not models.get_quality_weights(rid), "nothing moves on the retrospective check alone"
    # The same evidence read again says nothing new (LEARNING-3).
    assert scal.shadow_run(rid)["status"] == "no_new_data"
    _world(rid, 2, _helps, start=16, seed=12)
    out = scal.shadow_run(rid)
    assert out["status"] == "promoted", out
    w = models.get_quality_weights(rid)
    default = sq.DEFAULT_WEIGHTS["coverage"]
    assert w["coverage"] == round(default * (1 + sl.CALIBRATION_MAX_STEP), 1), "more than one bounded step"
    prom = scal.latest_promotion(rid)
    assert prom["can_undo"] and any(c["dimension"] == "coverage" for c in prom["changes"])
    assert "held-out" in prom["reason"] and "published since" in prom["reason"]
    scal.revert(rid, user={"username": "owner"})
    assert (models.get_quality_weights(rid) or {}).get("coverage") in (None, default)
    assert scal.latest_promotion(rid) is None
    # For eight weeks after the owner's undo, nothing is promoted again.
    _world(rid, 2, _helps, start=18, seed=13)
    assert scal.shadow_run(rid)["status"] == "pending"
    _world(rid, 2, _helps, start=20, seed=14)
    again = scal.shadow_run(rid)
    assert again["status"] == "held" and "undid" in again["reason"]
    with pytest.raises(ValueError):
        scal.revert(rid)


def test_a_demo_is_never_promoted_and_the_switch_keeps_runs_in_shadow(monkeypatch):
    rid = _rid()
    _world(rid, 16, _helps)
    monkeypatch.setenv(scal.AUTOPROMOTE_ENV, "0")
    assert scal.shadow_run(rid)["status"] == "pending"
    _world(rid, 2, _helps, start=16, seed=12)
    held = scal.shadow_run(rid)
    assert held["status"] == "held" and "switched off" in held["reason"]
    monkeypatch.delenv(scal.AUTOPROMOTE_ENV)
    monkeypatch.setattr(models, "learns_for_itself", lambda *a, **k: False)
    _world(rid, 2, _helps, start=18, seed=13)
    assert scal.shadow_run(rid)["status"] == "pending"
    _world(rid, 2, _helps, start=20, seed=14)
    out = scal.shadow_run(rid)
    assert out["status"] == "held" and "does not learn" in out["reason"]
    assert not models.get_quality_weights(rid)


def test_the_cohort_says_nothing_under_five_restaurants(monkeypatch):
    import intelligence.jobs as ij
    monkeypatch.setattr(ij, "excluded_learning_ids", lambda db_path=None: set())
    for n in range(3):
        _world(_rid(f"R{n} Co"), 9, _helps)
    assert scal.cohort_evidence() == {}
    for n in range(3, 6):
        _world(_rid(f"R{n} Co"), 9, _helps)
    got = scal.cohort_evidence()
    assert got["restaurants"] == 6 and got["evidence"]["coverage"] > sl.CALIBRATION_MIN_EVIDENCE
