"""Schedule fix round 10/11/26, workstream G1 — the learning estimators made
safe to act on (blind re-audit LEARNING-1..7, -13..17, -23..26, -28, -29,
SCALE-26): honest hold-outs, multiplicity control, no ratchet, engagement,
fleet sampling, the "nothing learned" switch."""
import datetime as dt
import math
import random

import pytest

import learning_stats as ls
import models
import schedule_learning as sl
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


# ── LEARNING-1: the decision's test is calibrated under the null ──────────

def test_williams_test_confirms_noise_about_as_often_as_it_says():
    rng = random.Random(7)
    hits = 0
    for _ in range(400):
        y = [rng.gauss(0, 1) for _ in range(40)]
        a = [rng.gauss(0, 1) for _ in range(40)]
        b = [x + rng.gauss(0, 0.3) for x in a]          # two near-identical scores, neither informative
        got = sl.better_correlation(a, b, y)
        hits += got["p"] is not None and got["p"] <= 0.10
    assert hits / 400.0 <= 0.14, hits


def test_a_real_improvement_is_found():
    rng = random.Random(3)
    y = [rng.gauss(0, 1) for _ in range(60)]
    a = [rng.gauss(0, 1) for _ in range(60)]
    b = [0.6 * yy + 0.4 * aa for yy, aa in zip(y, a)]
    assert sl.better_correlation(a, b, y)["p"] < 0.01


def _samples(weeks, informative=False, seed=1, start=0):
    rng = random.Random(seed)
    out = []
    for w in range(start, start + weeks):
        for k in range(14):
            cov = float(rng.randint(40, 100))
            issues = float(rng.random() < (0.4 - (cov - 40) / 200.0 if informative else 0.15))
            out.append(({"coverage": cov, "fairness": float(rng.randint(40, 100))},
                        {"issues": issues, "review_rating": None, "labor_vs_target": rng.gauss(0, 3)}, 1000 + w,
                        {"cell": f"{k % 7}|night", "demand": 1.0, "sales": 2000.0}))
    return out


def test_the_candidate_never_sees_the_weeks_it_is_checked_on():
    import schedule_calibration as scal
    s = _samples(12)
    train, held, hw = scal._split(s)
    assert hw == {1009, 1010, 1011} and all(x[2] not in hw for x in train)
    # Changing the held-out outcomes changes nothing the candidate is fitted on.
    flipped = [(d, dict(o, issues=1.0 - o["issues"]), h, m) if h in hw else (d, o, h, m) for d, o, h, m in s]
    t1, _h1, _w1 = scal._split(flipped)
    assert t1 == train


def test_the_cumulative_move_is_tested_against_the_weights_before_any_automatic_change():
    import schedule_calibration as scal
    runs = [{"status": "promoted", "before": {"coverage": 22.0}, "candidate": {"coverage": 24.0}},
            {"status": "promoted", "before": {"coverage": 20.0}, "candidate": {"coverage": 22.0}},
            {"status": "shadow", "before": {}, "candidate": {}}]
    assert scal._anchor({"coverage": 24.0}, runs) == {"coverage": 20.0}
    # An owner's own change breaks the chain: the anchor is what is in force.
    assert scal._anchor({"coverage": 30.0}, runs) == {"coverage": 30.0}


# ── LEARNING-23: a sample across the fleet, rotated ───────────────────────

def test_the_fleet_sample_reads_every_era_and_rotates():
    ids = list(range(1, 9001))
    a = ls.fleet_sample(ids, 300, key="2026-W41")
    b = ls.fleet_sample(ids, 300, key="2026-W42")
    assert len(a) == 300 and a != b
    # Every fifth of sign-up order is represented, not the first 300 ids.
    for lo in range(0, 9000, 1800):
        assert sum(1 for i in a if lo < i <= lo + 1800) == 60
    assert ls.fleet_sample(range(10), 300, key="x") == list(range(10))


# ── LEARNING-5 / LEARNING-17: the intervals ───────────────────────────────

def test_the_panel_interval_uses_t_on_clusters_minus_one():
    rng = random.Random(2)
    rows = [(g, x, 0.3 * x + rng.gauss(0, 1)) for g in range(7) for x in range(10)]
    got = ls.fe_slope_ci(rows)
    assert got["df"] == 6
    half = (got["hi"] - got["lo"]) / 2
    assert half == pytest.approx(ls.t_ppf(0.95, 6) * got["se"], rel=1e-3)


def test_an_owner_trend_needs_eight_weeks_and_a_serial_correlation_robust_interval():
    assert ls.trend_slope_ci([10, 9, 8, 7], min_n=8)["slope"] is None
    rng = random.Random(2)
    fewer = 0
    for _ in range(1000):
        x, vals = 0.0, []
        for _i in range(10):
            x = 0.5 * x + rng.gauss(0, 1)
            vals.append(12 + 4 * x)
        t = ls.trend_slope_ci(vals)
        fewer += t["hi"] < 0
    # A plain OLS interval from 4 weeks said "fewer edits" ~17% of the time here.
    assert fewer / 1000.0 <= 0.12, fewer


# ── LEARNING-4 / -5 / -6 / -7: one more person ───────────────────────────

def _units(effect=0.0, absent_every=6, n=120, seed=4):
    rng = random.Random(seed)
    out = []
    start = dt.date(2026, 5, 1)
    for i in range(n):
        d = start + dt.timedelta(days=i)
        wd = d.strftime("%A")
        busy = wd in ("Friday", "Saturday")
        absent = i % absent_every == 0
        typ = 3000 * (1.6 if busy else 1.0)
        out.append({"date": d.isoformat(), "weekday": wd, "people": (5 if busy else 4) - (1 if absent else 0),
                    "fc": math.log(typ), "absent": absent, "typical": typ,
                    "y": {"sales": rng.gauss(0, 0.05) - (effect if absent else 0.0), "comps": rng.gauss(0.02, 0.01),
                          "voids": None, "turn": rng.gauss(55, 8), "kitchen": rng.gauss(14, 3), "review": None}})
    return out


def test_the_decision_reads_the_natural_experiment_only():
    import staffing_elasticity as se
    us = _units()
    ne = se.natural_experiment(us, "sales")
    assert ne["method"] == "natural_experiment" and ne["level"] in (4, 5) and ne["df"] == ne["n"] - 1
    pa = se.panel_slope(us, "sales")
    assert pa["report_only"] is True


def test_a_service_gain_never_adds_a_person_whose_sales_net_is_clearly_negative():
    import staffing_elasticity as se
    d = se.decide({"sales": {"est": 0.0, "lo": -0.01, "hi": 0.01, "n": 8},
                   "turn": {"est": -9, "lo": -14, "hi": -4, "n": 8}}, 5000, 120.0, 30.0)
    assert d["move"] == 0


def test_the_runs_tests_are_one_holm_family():
    import staffing_elasticity as se
    # A service p of 0.01 passes alone but not in a family of 40 tests.
    est = {"sales": {"est": 0.0, "lo": -0.3, "hi": 0.3, "n": 8}, "turn": {"est": -3.0, "se": 1.2, "df": 7, "lo": -5,
                                                                           "hi": -1, "n": 8}}
    alone = se.decide(est, 5000, 120.0, 30.0)
    assert alone["move"] == 1
    family = {(i, "service:turn"): 0.5 for i in range(39)}
    family[("x", "service:turn")] = alone["tests"]["service:turn"]
    surv = ls.holm(family, se.FAMILY_ALPHA)
    got = se.decide(est, 5000, 120.0, 30.0, passed={n for (k, n) in surv if k == "x"})
    assert got["move"] == 0


def test_a_move_measured_on_busy_meals_applies_to_busy_meals():
    import staffing_elasticity as se
    wd_typ = {"Monday": 3000, "Tuesday": 3000, "Friday": 4800, "Saturday": 4800}
    assert se.apply_weekdays(1, {"treated_typical": 4800}, wd_typ) == ["Friday", "Saturday"]
    assert se.apply_weekdays(-1, {"treated_typical": 3000}, wd_typ) == ["Monday", "Tuesday"]


def test_the_requirement_takes_a_target_level_never_a_second_plus_one_and_only_its_weekdays():
    import schedule_requirements as req
    typical = {("Friday", "night"): {"Server": 5}, ("Monday", "night"): {"Server": 4}}
    el = {("server", "night"): {"move": 1, "target": 5, "weekdays": ["Friday"], "reason": "one more server"}}
    rows = req.shift_requirements(["2026-10-16", "2026-10-12"], typical_headcount=typical, elasticity=el)
    by = {r["date"]: {x["role"]: x for x in r["roles"]} for r in rows if r["daypart"] == "night"}
    # Friday's baseline already absorbed the measured +1 (5 = target): nothing more.
    assert by["2026-10-16"]["Server"]["required"] == 5
    # Monday is not a weekday it was measured on.
    assert by["2026-10-12"]["Server"]["required"] == 4
    el2 = {("server", "night"): {"move": 1, "target": 6, "weekdays": ["Friday"], "reason": "x"}}
    rows = req.shift_requirements(["2026-10-16"], typical_headcount=typical, elasticity=el2)
    assert next(r for r in rows if r["daypart"] == "night")["roles"][0]["required"] == 6


def test_a_move_binds_only_after_two_agreeing_weeks_and_never_stale(db_path, monkeypatch):
    import staffing_elasticity as se
    monkeypatch.setenv("STAFFING_ELASTICITY_APPLY", "1")
    today = dt.date(2026, 10, 12)
    assert se._streak(None, 1, today) == 1
    old = {"move": 1, "streak": 1, "estimated_on": "2026-10-05"}
    assert se._streak(old, 1, today) == 2
    assert se._streak(dict(old, estimated_on="2026-10-11"), 1, today) == 1     # a rerun the same week
    assert se._streak(old, -1, today) == 1
    rid = create_restaurant(Restaurant(name="El Co", owner_email="el@x.test", module_labor=1))
    conn = models.get_conn()
    import json
    for fam, e in (("server", {"move": 1, "binds": True, "target": 5, "weekdays": ["Friday"],
                               "estimated_on": today.isoformat(), "net": {}}),
                   ("host", {"move": 1, "binds": False, "target": 3, "estimated_on": today.isoformat()}),
                   ("busser", {"move": 1, "binds": True, "target": 3, "estimated_on": "2026-08-01"})):
        conn.execute("INSERT INTO staffing_elasticity (restaurant_id, family, daypart, move, estimate_json) "
                     "VALUES (?,?,?,?,?)", (rid, fam, "night", 1, json.dumps(e)))
    conn.commit()
    conn.close()
    got = se.moves(rid, db_path=db_path, today=today)
    assert set(got) == {("server", "night")} and got[("server", "night")]["target"] == 5
    monkeypatch.delenv("STAFFING_ELASTICITY_APPLY")
    assert se.moves(rid, db_path=db_path, today=today) == {}


def test_the_drafts_own_plus_one_is_not_adopted_as_the_usual_crew():
    import requirement_provenance as prov
    trend = {"day": "Friday", "part": "night", "role": "Server", "now": 6, "was": 5, "since": "2026-09-18",
             "direction": "up", "recent_dates": ["2026-09-18", "2026-09-25", "2026-10-02"]}
    ups = {("server", "night"): [["2026-09-10", None]]}
    got = prov.hold_down_trend(trend, "2026-08-01", restaurant_id=1, ups=ups)
    assert got["hold"] and got["provenance"] == "elasticity"
    # A rise outside any window is the restaurant's own.
    assert prov.hold_down_trend(trend, "2026-08-01", restaurant_id=1, ups={})["hold"] is False
    old = {("server", "night"): [["2026-05-01", "2026-06-01"]]}
    assert prov.hold_down_trend(trend, "2026-08-01", restaurant_id=1, ups=old)["hold"] is False


# ── LEARNING-16: a week sent unread is not a draft that needed no edits ───

def test_a_publish_without_a_review_is_not_counted_as_needing_no_edits(db_path):
    import schedule_scorecard as ssc
    rid = create_restaurant(Restaurant(name="View Co", owner_email="v@x.test", module_labor=1))
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                       "VALUES (?,?,?,?,?)", (rid, "2026-10-12", "2026-10-18", "x", "2026-10-11 10:00:30")).lastrowid
    conn.commit()
    conn.close()
    # Never opened, and no view ever recorded here: unknown, as before.
    assert ssc.engagement(rid, hid, "2026-10-11 10:00:30", False, True) is None
    # An admin's look is not the restaurant's.
    assert ssc.record_view(rid, hid, user={"is_admin": 1, "role": "admin"}) is False
    assert ssc.record_view(rid, hid, user={"id": 3, "role": "owner", "is_principal": 1}) is True
    conn = models.get_conn()
    conn.execute("UPDATE schedule_draft_views SET first_viewed_at='2026-10-11 10:00:00' WHERE history_id=?", (hid,))
    conn.commit()
    conn.close()
    # Opened 30 seconds before it went out: still not a review.
    assert ssc.engagement(rid, hid, "2026-10-11 10:00:30", False, True) is False
    assert ssc.engagement(rid, hid, "2026-10-11 10:05:00", False, True) is True
    # Edited: engaged whatever the views. A week never opened while views were
    # recorded: not engaged; one published before the first view: unknown.
    assert ssc.engagement(rid, hid, "2026-10-11 10:00:30", True, True) is True
    assert ssc.engagement(rid, 999999, "2026-10-12 10:00:00", False, True) is False
    assert ssc.engagement(rid, 999999, "2026-09-01 10:00:00", False, True) is None


def test_fewer_edits_is_never_said_while_an_outcome_got_worse(monkeypatch):
    import schedule_scorecard as ssc
    monkeypatch.setattr(ssc, "_settings_changed", lambda *a, **k: 0)
    weeks = [{"week_start": f"2026-{m:02d}-01", "drr": 30.0 - 3 * i, "unmanaged_minutes": 10.0 * i}
             for i, m in enumerate(range(1, 11))]
    _t, sentence, outcomes, _c = ssc.owner_trend(1, list(reversed(weeks)))
    assert outcomes["unmanaged_minutes"]["worse"] and "fewer edits is not better weeks" in sentence


# ── LEARNING-14: learning as a whole can be switched off ──────────────────

def test_the_no_learning_scope_turns_every_learner_neutral_and_ends_with_the_generation(monkeypatch):
    import labor
    import retention_risk
    import schedule_intel
    import schedule_memory
    import staff_settings
    import staffing_elasticity as se
    monkeypatch.setenv("STAFFING_ELASTICITY_APPLY", "1")
    with ls.learning_scope():
        ls.switch_learning_off()
        assert ls.learning_off()
        assert staff_settings.reliability(1) == {}
        assert schedule_intel.behaviour_preferences(1) == {}
        assert retention_risk.protect_signal(1) == {}
        assert schedule_memory.enforced_signals(1, ["2026-10-12"]) == []
        assert schedule_memory.prompt_lines(1, ["2026-10-12"]) == ""
        assert labor.apply_learned_headcount(1, {"x": 1}) == {"x": 1}
        assert sl.edit_predictor(1)["ready"] is False
        assert se.moves(1) == {} and se.marginal_values(1) == {}
    assert not ls.learning_off()


def test_the_learning_switchback_is_one_week_in_eight_and_paused_until_powered():
    import schedule_experiments as sx
    exp = sx.experiment("learning_v2")
    assert exp["control"] == "learning_on" and exp["until_powered"] == "learning_on"
    arms = [sx.switchback_arm(exp, 5, (dt.date(2026, 1, 5) + dt.timedelta(weeks=k)).isoformat()) for k in range(16)]
    assert arms.count("learning_off") == 2
    assert sx.DEFAULT_FLAGS["learning"] is True


def test_the_eval_scores_the_same_answer_with_nothing_learned():
    import schedule_eval as sev
    item = {"context": {"signals": {"learned": [{"k": 1}], "reliability": {"Ana": {}}, "roster": ["Ana"]},
                        "weights": {"coverage": 30}}}
    off = sev.without_learning(item)
    assert off["context"]["signals"]["learned"] == [] and off["context"]["signals"]["reliability"] == {}
    assert off["context"]["signals"]["roster"] == ["Ana"] and off["context"]["weights"] is None
    assert item["context"]["signals"]["learned"] == [{"k": 1}]


# ── LEARNING-24 / SCALE-26: the weekly sweeps are queue work ──────────────

def test_the_weekly_learning_sweeps_fan_out_as_learner_rows():
    import importlib
    import strategy_jobs as sj
    for name in ("staffing_learning", "schedule_scorecard", "quality_calibration"):
        mod, _s, fn = sj.LEARNERS[name].partition(":")
        assert callable(getattr(importlib.import_module(mod), fn))
    src = open("scheduler.py").read()
    assert 'learner_pass("schedule_scorecard"' in src and 'learner_pass("staffing_learning"' in src
    assert "quality_calibration_pass()" in src


# ── LEARNING-26: the interval at each date's own lead ─────────────────────

def test_each_dates_interval_is_backtested_at_its_own_lead(monkeypatch):
    import demand_model as dm
    seen = []
    real = dm.backtest

    def spy(series, today, *a, **k):
        seen.append((k.get("horizon", dm.HORIZON_DAYS), k.get("parity")))
        return real(series, today, *a, **k)
    monkeypatch.setattr(dm, "backtest", spy)
    today = dt.date(2026, 10, 10)
    rng = random.Random(1)
    series = {today - dt.timedelta(days=i): 1000 * (1 + rng.gauss(0, 0.05)) for i in range(1, 140)}
    got = dm.forecast_from_series(series, [today + dt.timedelta(days=2), today + dt.timedelta(days=12)], today)
    assert {h for h, p in seen if p is not None and h != dm.HORIZON_DAYS} >= {2, 12}
    assert got["dates"][(today + dt.timedelta(days=12)).isoformat()]["lead_days"] == 12
