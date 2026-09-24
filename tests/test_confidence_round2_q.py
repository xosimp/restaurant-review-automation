"""Confidence re-audit round 2, group Q (9/24/26): outcome statistics and
forecasts. Each test replays the blind auditor's probe (scratchpad blind2 p2,
p5, p6, p7, p9, p10; blind3 p10) as a seeded simulation with a tolerance, or
pins the rule the fix introduced. Every one of them failed before its fix;
the "before" figure is in each docstring."""
import json
import math
import random
import sys
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import demand  # noqa: E402
import forecast_log  # noqa: E402
import metrics  # noqa: E402
import outcomes  # noqa: E402
import rec_learning  # noqa: E402
import rec_ledger  # noqa: E402
import review_intelligence  # noqa: E402
import waste_trend  # noqa: E402
from intelligence import features, feedback, scoring  # noqa: E402


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})
    return db_path


def _rid(name="Q Co", category=None):
    kw = {m: 1 for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing")}
    rid = create_restaurant(Restaurant(name=name, owner_email="q@x.test", **kw))
    if category:
        c = models.get_conn()
        c.execute("UPDATE restaurants SET category=? WHERE id=?", (category, rid))
        c.commit()
        c.close()
    return rid


# ── the probe B2 p2 world: a labor % series, a triggered tracker, evaluated ─

L = 28
T0 = date(2025, 1, 6)


def _series(rng, days, mu=30.0, level_sd=0.8, rho=0.85, day_sd=3.0, drift_per_day=0.0, shift_at=None, shift=0.0):
    lvl, out = 0.0, []
    for d in range(days):
        if d % 7 == 0:
            lvl = rho * lvl + rng.gauss(0, level_sd * math.sqrt(1 - rho * rho))
        v = mu + lvl + rng.gauss(0, day_sd) + drift_per_day * d
        if shift_at is not None and d >= shift_at:
            v += shift
        out.append(max(5.0, v))
    return out


def _put_days(rid, start, pcts):
    rows = []
    for i, lp in enumerate(pcts):
        d = start + timedelta(days=i)
        rows.append((rid, d.isoformat(), d.strftime("%A"), round(lp * 10.0, 2), 1000.0, round(lp, 2)))
    c = models.get_conn()
    c.executemany("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def _replay(reps, hist_days, seed, fire_above=30.5, shift=None, **kw):
    """probe p2's run(): do-nothing trackers, triggered, end to end through
    outcomes.record -> evaluate -> rec_learning.learned_verdict."""
    rng = random.Random(seed)
    raw = {"improved": 0, "worsened": 0, "no_clear_change": 0, "unknown": 0}
    learned = dict(raw)
    far, flags, n, tries = [], [], 0, 0
    while n < reps and tries < reps * 20:
        tries += 1
        gap = rng.randint(0, 6)
        H = hist_days
        k = dict(kw)
        if shift is not None:
            k.update(shift_at=H, shift=shift)
        s = _series(rng, H + L + gap + 1 + L + 2, **k)
        if sum(s[H:H + L]) / L < fire_above:
            continue
        rid = _rid(f"P{tries}")
        _put_days(rid, T0, s)
        ts, te = T0 + timedelta(days=H), T0 + timedelta(days=H + L - 1)
        today = te + timedelta(days=1 + gap)
        row = outcomes.record(rid, "home", f"labor_over:{rid}", "Cut labor", "labor_pct", today=today,
                              trigger=(ts, te))
        if not outcomes.evaluate(row["id"], today=today + timedelta(days=L)):
            continue
        c = models.get_conn()
        full = dict(c.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (row["id"],)).fetchone())
        c.close()
        n += 1
        v = full["verdict"] or "unknown"
        raw[v if v in raw else "unknown"] += 1
        lv = rec_learning.learned_verdict(v, full)
        learned[lv] += 1
        flags.append((lv, [x.get("kind") for x in json.loads(full["concurrent"] or "[]")]))
        if full["false_alarm_rate"] is not None:
            far.append(full["false_alarm_rate"])
    return {"n": n, "raw": raw, "learned": learned, "far": far, "flags": flags}


# ── Q1: trend-flagged / confounded results never count as wins ──────────────

def test_q1_replay_b2_p2_s4_a_drifting_do_nothing_restaurant_no_longer_reads_as_wins():
    """B2 p2 S4 (stable, 120 days of history, spring drift −0.03 pt/day, NOTHING
    changed): 29.7% of trackers read "improved" and all 89 counted — 46 of
    them carrying "Already moving this way". Now a confounded result is
    unknown: the improved share of what counts falls toward the base rate,
    and no counted win carries any concurrent flag."""
    r = _replay(70, 120, seed=4, fire_above=0, drift_per_day=-0.03)
    lr = r["learned"]
    measured = lr["improved"] + lr["worsened"] + lr["no_clear_change"]
    assert r["raw"]["improved"] / r["n"] > 0.15                  # the drift still moves the raw reading
    assert lr["improved"] / measured <= 0.15                     # was 29.7%
    assert all(not kinds for lv, kinds in r["flags"] if lv == "improved")


def test_q1_confounded_is_one_rule_for_learning_value_and_sql(db_path):
    rid = _rid()
    base = {"status": "evaluated", "verdict": "improved", "source_key": "trim_day:Monday", "restaurant_id": rid}
    clean = dict(base, concurrent="[]")
    trend = dict(base, concurrent=json.dumps([{"kind": "trend", "label": outcomes.TREND_LABEL}]))
    other = dict(base, concurrent=[{"kind": "price_change", "label": "Soup repriced"}])
    unchecked = dict(base, concurrent=None)
    for row, conf in ((clean, False), (trend, True), (other, True), (unchecked, False)):
        assert outcomes.confounded(row) is conf
        assert rec_learning._confounded(row) is conf                  # the two stay in step
        assert outcomes.result_counts(row) is (not conf)
        assert (rec_learning.learned_verdict("improved", row) == "unknown") is conf
        # "whichever way it read": a confounded worse result is no loss either
        assert (rec_learning.learned_verdict("worsened", row) == "unknown") is conf
    assert "o.concurrent" in outcomes._COUNTS_SQL
    assert "concurrent" in rec_learning._TRACKER_CALIBRATION_COLS


# ── Q2: the weekly-scaled band states the false-alarm rate it gives ─────────

def test_q2_replay_b2_p2_s3_a_new_volatile_restaurant_moves_about_as_often_as_it_is_told():
    """B2 p2 S3 (volatile, 84 days of history, nothing changed): 28.7% of
    trackers "moved" while the stored false_alarm_rate said 10% — weeks were
    scaled as if independent. With the persistence correction the moved share
    is within a few points of the stated rate."""
    r = _replay(110, 84, seed=3, fire_above=31.5, level_sd=2.5, day_sd=6.0)
    moved = (r["raw"]["improved"] + r["raw"]["worsened"]) / r["n"]
    stated = sum(r["far"]) / len(r["far"])
    assert moved <= 0.18                                          # was 28.7%
    assert abs(moved - stated) <= 0.07


def test_q2_weekly_spread_is_corrected_for_persistence(db_path):
    rid = _rid()
    rng = random.Random(11)
    _put_days(rid, T0, _series(rng, 84, level_sd=2.5, day_sd=6.0))
    end = (T0 + timedelta(days=83)).isoformat()
    d = metrics.window_spread_detail(rid, "labor_pct", 28, end)
    assert d["method"] == "weekly, scaled" and d["rho"] >= metrics.AUTOCORR_FLOOR
    weeks = []
    for i in range(12):
        v, _ = metrics.measure(rid, "labor_pct", (T0 + timedelta(days=7 * i)).isoformat(),
                               (T0 + timedelta(days=7 * i + 6)).isoformat())
        weeks.append(v)
    m = sum(weeks) / len(weeks)
    naive = (sum((w - m) ** 2 for w in weeks) / (len(weeks) - 1)) ** 0.5 * (7 / 28) ** 0.5
    assert d["sigma"] > 1.5 * naive                               # the old sqrt(7/L) scaling
    assert metrics.window_spread(rid, "labor_pct", 28, end)[1] == "weekly, scaled"
    assert abs(metrics.t_ppf(0.95, 3) - 2.353) < 0.01 and abs(metrics.t_cdf(1.645, None) - 0.95) < 0.001


# ── Q3: a level shift at the trigger is unknown, not "worsened" ─────────────

def test_q3_replay_b2_p2_s7_a_lasting_step_at_the_trigger_does_not_read_worsened():
    """B2 p2 S7 (labor rose 1.5 pts for good at the trigger — a wage rise —
    and nothing was changed): 35.5% of trackers read "worsened" against the
    mirror baseline, feeding the rank penalties and Historical Accuracy. The
    step test files them `level_shift` and learning reads them unknown."""
    r = _replay(70, 364, seed=7, fire_above=31.0, shift=1.5)
    assert r["raw"]["worsened"] / r["n"] > 0.2                    # the raw reading still says worse
    assert r["learned"]["worsened"] / r["n"] <= 0.08              # was 35.5%
    assert any("level_shift" in kinds for _lv, kinds in r["flags"])


def test_q3_level_shift_rule_and_owner_sentence():
    r = {"id": 1, "restaurant_id": 1, "metric": "labor_pct", "baseline_kind": "before the trigger",
         "baseline_value": 30.0, "trigger_value": 31.6, "trigger_start": "2026-06-01",
         "started_on": "2026-07-06", "evaluate_on": "2026-08-03", "noise_band": None}
    assert outcomes.level_shift(r, "worsened", 31.8)["kind"] == "level_shift"
    assert outcomes.level_shift(r, "worsened", 34.5) is None      # moved on past the trigger's level
    assert outcomes.level_shift(dict(r, trigger_value=30.1), "worsened", 31.8) is None   # no step
    assert outcomes.level_shift(r, "improved", 28.0) is None      # the other way
    row = dict(r, status="evaluated", verdict="worsened", attribution="associated",
               concurrent=[{"kind": "level_shift", "label": outcomes.LEVEL_SHIFT_LABEL, "date": "2026-06-01"}])
    label = outcomes.attribution_label(row)
    assert "already at this level" in label and "isn't counted either way" in label


# ── Q4: Effectiveness — one result per window, the base rate as prior ───────

def _ep(i, kind, verdict=None, dollar=None, realised=None, age_days=30, metric=None, start=None):
    now = datetime(2026, 9, 24)
    at = (now - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    tr = {"dollars_monthly": realised} if realised is not None else {}
    if metric:
        tr.update(id=1000 + i, metric=metric, after_start=start,
                  after_end=(date.fromisoformat(start) + timedelta(days=27)).isoformat())
    return {"rec_id": f"r{kind}{i}", "key": f"{kind}:k{i}", "kind": kind, "shown": True, "state": "accepted",
            "tag_list": [], "verdict": verdict, "verdict_at": at if verdict else None,
            "dollar_value": dollar, "tracker": tr}


def test_q4_replay_b2_p5_a_measured_kind_no_longer_ranks_below_a_never_measured_one():
    """B2 p5: with a success prior of 0.5, a kind with 2 of 10 improved (a
    real effect) weighed 0.867 and one with 0 of 10 weighed 0.75, both below a
    never-measured kind at 1.0. Shrunk toward the do-nothing base rate, a kind
    that beats the base rate is not ranked below one never measured."""
    eps = ([_ep(i, "effective", "improved" if i < 2 else "no_clear_change") for i in range(10)]
           + [_ep(i, "useless", "no_clear_change") for i in range(10)])
    m = rec_learning.Effectiveness(1, eps, cohort=None, now=datetime(2026, 9, 24), base_rates={})
    never = m.weight("never_measured:x")[0]
    assert m.prior("effective")[1] == rec_learning.BASE_RATE_STATED
    assert m.weight("effective:x")[0] >= never                    # was 0.867 < 1.0
    assert m.weight("effective:x")[0] > m.weight("useless:x")[0] - 1e-9


def test_q4_overlapping_windows_on_one_number_count_once_in_effectiveness():
    """B2 #7: Effectiveness counted every episode; kind_record one per
    overlapping window. Three trackers over the same weeks on labor % are
    one change."""
    eps = [_ep(i, "trim_day", "improved", metric="labor_pct", start="2026-06-0%d" % (i + 1)) for i in range(3)]
    eps.append(_ep(9, "trim_day", "no_clear_change", metric="labor_pct", start="2026-08-01"))
    m = rec_learning.Effectiveness(1, eps, cohort=None, now=datetime(2026, 9, 24), base_rates={})
    assert m.kinds["trim_day"]["measured"] == 2 and m.kinds["trim_day"]["improved"] == 1


def test_q4_base_rate_reads_the_untaken_record_and_the_bands_false_alarm_rate():
    inputs = {"*": {"far": [0.10, 0.06]}, "trim_day": {"far": [0.08], "untaken": []}}
    assert rec_learning.base_rate_from(inputs, "trim_day")["rate"] == 0.04
    assert rec_learning.base_rate_from(inputs, "other")["rate"] == 0.04          # the restaurant's own bands
    assert rec_learning.base_rate_from({}, "x") == {"rate": 0.05, "source": "stated", "n": 0,
                                                   "basis": "half the stated 10% false-alarm rate of the noise band"}
    unt = [("improved" if i < 3 else "no_clear_change", "labor_pct", f"2026-0{i + 1}-01", f"2026-0{i + 1}-28")
           for i in range(6)]
    br = rec_learning.base_rate_from({"trim_day": {"far": [0.1], "untaken": unt}}, "trim_day")
    assert br["source"] == "untaken" and br["n"] == 6
    assert abs(br["rate"] - round((3 + 0.05 * 5) / 11, 3)) < 1e-9


# ── Q5: dollar calibration ──────────────────────────────────────────────────

def test_q5_replay_b2_p5_the_calibration_floor_no_longer_hides_the_shortfall():
    """B2 p5: 10 measured results on $1,000 estimates, 2 realised $1,000 and 8
    no clear change ($0): the card showed $800 ("adjusted from 10 measured
    results") against a measured mean of $200. With enough pairs the floor
    lifts, and the realised mean rides beside the estimate."""
    eps = ([_ep(i, "fix", "improved", dollar=1000, realised=1000) for i in range(2)]
           + [_ep(i + 2, "fix", "no_clear_change", dollar=1000) for i in range(8)])
    m = rec_learning.Effectiveness(1, eps, cohort=None, now=datetime(2026, 9, 24), base_rates={})
    item = rec_learning.attach_dollar_calibration({"key": "fix:new", "dollars_monthly": 1000}, m)
    assert item["dollars_adjusted"] <= 300                        # was 800
    assert item["realised_mean"] == 200.0 and item["calibration_n"] == 10
    few = rec_learning.Effectiveness(1, eps[:1] + eps[2:5], cohort=None, now=datetime(2026, 9, 24), base_rates={})
    assert few.calibration_ratio("fix")[0] >= rec_learning.CALIBRATION_BOUNDS[0]    # under the floor's pair count


# ── Q6: the cohort record — per episode, and no restaurant dominates ────────

def test_q6_replay_b2_p10_three_trackers_on_one_key_are_three_results():
    """B2 p10: the cohort table is unique on (restaurant, key, action) and
    sync overwrote the outcome in place — two trackers (improved, worsened)
    read measured 1 / worsened 1, and a third flipped it to improved 1."""
    rid = _rid()
    c = models.get_conn()
    for i, v in enumerate(("improved", "worsened", "improved")):
        c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, status, "
                  "verdict, started_on, evaluate_on) VALUES (?,?,?,?,?,?,?,?,?)",
                  (rid, "home", "trim_day:Monday", "t", "labor_pct", "evaluated" if i < 2 else "tracking", v,
                   f"2026-0{i + 3}-01", f"2026-0{i + 3}-29"))
    c.commit()
    c.close()
    feedback.sync()
    s = scoring.kind_stats("trim_day", restaurant_id=rid)
    assert (s["measured"], s["improved"], s["worsened"]) == (2, 1, 1)
    c = models.get_conn()
    c.execute("UPDATE recommendation_outcomes SET status='evaluated' WHERE restaurant_id=?", (rid,))
    c.commit()
    c.close()
    feedback.sync()
    s = scoring.kind_stats("trim_day", restaurant_id=rid)
    assert (s["measured"], s["improved"], s["worsened"]) == (3, 2, 1)   # was measured 1, improved 1


def test_q6_overlapping_windows_file_one_result_and_legacy_rows_are_replaced():
    rid = _rid()
    c = models.get_conn()
    for i, (start, v) in enumerate((("2026-03-01", "improved"), ("2026-03-10", "improved"))):
        end = (date.fromisoformat(start) + timedelta(days=27)).isoformat()
        c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, status, "
                  "verdict, started_on, evaluate_on, after_start, after_end) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, "home", f"trim_day:D{i}", "t", "labor_pct", "evaluated", v, start, end, start, end))
    # a row the old sync wrote under the bare key
    c.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, outcome, synced_from, "
              "event_at) VALUES (?,?,?,?,?,?,datetime('now'))", (rid, "trim_day", "trim_day:D0", "measured",
                                                                  "improved", "recommendation_outcomes"))
    c.commit()
    c.close()
    feedback.sync()
    s = scoring.kind_stats("trim_day", restaurant_id=rid)
    assert (s["measured"], s["improved"], s["unknown"]) == (1, 1, 1)


def test_q6_capped_counts_hold_any_one_restaurant_to_a_third():
    assert scoring.capped_counts({1: (8, 8), 2: (1, 1), 3: (1, 1), 4: (1, 0), 5: (1, 0)}) == (6.0, 4.0)
    assert scoring.capped_counts({i: (3, 1) for i in range(5)}) == (15.0, 5.0)      # nobody over: unchanged
    rows = ([{"restaurant_id": 1, "source_key": f"k{i}", "action": "measured", "outcome": "improved",
              "days_to_effect": 1} for i in range(8)]
            + [{"restaurant_id": r, "source_key": "k", "action": "measured", "outcome": "no_clear_change",
                "days_to_effect": 1} for r in (2, 3, 4, 5)])
    s = scoring._summarise(rows)
    assert s["measured"] == 12 and s["measured_capped"] == 6.0 and s["improved_capped"] == 2.0


def test_q6_replay_b2_p6_one_peer_no_longer_stands_as_the_cohort():
    """B2 p6: 4 own results, all worsened; a cohort record of 10 of 12
    improved with 8 of the 12 from one peer. Historical Accuracy showed 74%
    and the card 86% high. Capped, the cohort holds 6 results — under the
    floor of 10 — and does not stand in."""
    me = _rid("Me", category="pizza")
    peers = [_rid(f"Peer{i}", category="pizza") for i in range(5)]
    c = models.get_conn()
    plan = [(peers[0], 8, 8), (peers[1], 1, 1), (peers[2], 1, 1), (peers[3], 1, 0), (peers[4], 1, 0)]
    for rid, n, k in plan:
        for i in range(n):
            c.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, "
                      "event_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                      (rid, "trim_day", f"trim_day:{i}", "pizza", "measured", "improved" if i < k else "no_clear_change"))
    c.commit()
    c.close()
    rec = rec_learning.kind_record(me, "trim_day")
    assert rec["source"] == "none" and rec["rate"] is None        # was cohort, 10 of 12


# ── Q7: forecast skill against the naive forecasts ──────────────────────────

def _flog(rid, kind, horizon, predicted, actual, last=None, mean=None, created="2026-08-01"):
    err, signed = forecast_log.errors(predicted, actual)
    c = models.get_conn()
    c.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, "
              "signed_error_pct, naive_last, naive_mean, naive_n, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (rid, kind, horizon, predicted, actual, err, signed, last, mean, 8 if mean else 1, created))
    c.commit()
    c.close()


def test_q7_no_reading_before_four_scored_periods_and_negative_skill_is_withheld():
    """B1 L2 / B2 #11: two scored periods gave "close"; and a forecast that
    missed by more than repeating last week (the rating's latest+slope) read
    "close" on its 10% error. Now nothing is said below 4 periods, and a
    record with negative skill is withheld."""
    rid = _rid()
    weeks = ["2026-07-05", "2026-07-12", "2026-07-19", "2026-07-26"]
    for w in weeks[:3]:
        _flog(rid, "review_rating_week", w, 4.8, 4.4, last=4.4, mean=4.4)
    acc = forecast_log.accuracy(rid, "review_rating_week")
    assert acc["available"] is False and acc["reading"] is None and acc["withheld"] is False
    _flog(rid, "review_rating_week", weeks[3], 4.8, 4.4, last=4.4, mean=4.4)
    acc = forecast_log.accuracy(rid, "review_rating_week")
    assert acc["mean_error_pct"] <= 15                              # would have read "close"
    assert acc["withheld"] is True and acc["reading"] == forecast_log.NO_SKILL_READING
    assert acc["skill"] < 0 and acc["beats_naive"] is False and acc["skill_pct"] < 0
    good = _rid("Good")
    for w in weeks:
        _flog(good, "labor_week", w, 30.0, 30.5, last=33.0, mean=32.0)
    acc = forecast_log.accuracy(good, "labor_week")
    assert acc["withheld"] is False and acc["beats_naive"] is True and acc["skill_pct"] > 0


def test_q7_scoring_stores_the_naive_forecasts_as_they_stood_when_frozen():
    rid = _rid()
    start = date(2026, 6, 1)                                         # a Monday
    c = models.get_conn()
    for i in range(10 * 7):
        d = start + timedelta(days=i)
        pct = 30.0 + (i // 7)                                        # each ISO week one point higher
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), pct * 10, 1000.0, pct))
    c.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, created_at) VALUES (?,?,?,?,?)",
              (rid, "labor_week", "2026-08-09", 39.0, "2026-07-29 10:00:00"))    # frozen mid-week 9
    c.commit()
    c.close()
    out = forecast_log.score_due(rid, today=date(2026, 8, 20))
    assert out["scored"] == 1
    c = models.get_conn()
    row = dict(c.execute("SELECT * FROM forecast_log WHERE restaurant_id=?", (rid,)).fetchone())
    c.close()
    assert row["actual"] == 39.0 and row["naive_last"] == 37.0      # week 8: the last CLOSED when frozen
    assert row["naive_mean"] == 33.5 and row["naive_n"] == 8


def test_q7_replay_b2_p9_the_rating_forecast_beats_latest_plus_slope_and_rarely_fires_on_noise():
    """B2 p9 (a steady rating, no real trend): latest+slope was shown in 64%
    of weeks and missed by 0.455★ against 0.304★ for the 8-week mean and
    0.428★ for last week. The new trend gate rarely fires on noise, and the
    shrunk-slope forecast beats latest+slope; on a real slide it beats both
    naive forecasts."""
    def world(trend, reps, seed):
        rng = random.Random(seed)

        def week(mu):
            n = rng.randint(3, 9)
            return sum(min(5, max(1, round(rng.gauss(mu, 1.0)))) for _ in range(n)) / n
        fired, err = 0, {"new": 0.0, "product": 0.0, "mean": 0.0, "last": 0.0}
        for _ in range(reps):
            vals = [round(week(4.4 + trend * (i - 4)), 2) for i in range(8)]
            mx, my = 3.5, sum(vals) / 8
            slope = sum((i - mx) * (vals[i] - my) for i in range(8)) / 42.0
            change = vals[-1] - vals[0]
            if abs(change) < 0.15 or (slope > 0) != (change > 0):
                continue
            if waste_trend._confidence(vals, slope) not in ("high", "medium"):
                continue
            fired += 1
            a = week(4.4 + trend * 4)
            err["new"] += abs(review_intelligence.rating_next_week(vals) - a)
            err["product"] += abs(min(5, max(1, vals[-1] + slope)) - a)
            err["mean"] += abs(my - a)
            err["last"] += abs(vals[-1] - a)
        return fired / reps, {k: v / max(1, fired) for k, v in err.items()}
    shown, e = world(0.0, 6000, 3)
    assert shown <= 0.15                                             # was 64%
    assert e["new"] < e["product"] and e["new"] < e["last"]
    _shown, e = world(-0.15, 3000, 5)
    assert e["new"] < e["mean"] and e["new"] < e["last"]


def test_q7_rating_forecast_is_withheld_by_its_record():
    rid = _rid()
    trend = {"direction": "declining", "confidence": "medium", "next_week": 4.23}
    assert review_intelligence.rating_forecast(rid, trend) == 4.2
    assert review_intelligence.rating_forecast(rid, dict(trend, confidence="low")) is None
    for w in ("2026-07-05", "2026-07-12", "2026-07-19", "2026-07-26"):
        _flog(rid, "review_rating_week", w, 4.8, 4.4, last=4.4, mean=4.4)
    assert review_intelligence.rating_forecast(rid, trend) is None


def test_q7_demand_accuracy_states_its_skill_over_the_naive_forecasts():
    """B2 #11: demand_accuracy stated an error, never whether the forecast
    beat simply repeating last week — the evidence input group P reads."""
    rid = _rid()
    rng = random.Random(5)
    c = models.get_conn()
    day0 = date(2026, 8, 1)
    sales = {}
    for i in range(112):
        d = day0 - timedelta(days=112 - i)
        sales[d] = round((1000.0 + d.weekday() * 100) * math.exp(rng.gauss(0, 0.15)), 2)
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 300, sales[d], 30))
    for i in range(49):
        d = day0 - timedelta(days=49 - i)
        actual = sales[d]
        fc = 1000.0 + d.weekday() * 100                  # the weekday's true level: a good forecast
        for m, v in (("sales.net", actual), ("sales.forecast_net", fc),
                     ("sales.vs_forecast_pct", round((actual / fc - 1) * 100, 2))):
            c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
                      "VALUES (?,?,?,?,?)", (rid, d.isoformat(), m, v, "final"))
    c.commit()
    c.close()
    acc = demand.demand_accuracy(rid, today=day0)
    assert acc["available"] and acc["n_skill"] >= demand.ACCURACY_MIN_NIGHTS
    assert acc["skill_vs_last_pct"] > 0 and acc["skill_vs_mean_pct"] is not None
    assert acc["skill_pct"] == min(acc["skill_vs_last_pct"], acc["skill_vs_mean_pct"])


# ── Q8: forecast scoring gives up ───────────────────────────────────────────

def test_q8_a_period_unmeasurable_long_after_it_closed_is_marked_and_never_retried():
    """B6 #11: score_due and restaurants_due selected every unscored row
    before today, with no age limit — an unmeasurable week was retried
    forever."""
    rid = _rid()
    c = models.get_conn()
    c.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted) VALUES (?,?,?,?)",
              (rid, "labor_week", "2026-06-07", 30.0))
    c.commit()
    c.close()
    soon = forecast_log.score_due(rid, today=date(2026, 6, 15))
    assert soon == {"scored": 0, "unmeasurable": 1, "gave_up": 0}
    assert rid in forecast_log.restaurants_due(today=date(2026, 6, 15))
    late = forecast_log.score_due(rid, today=date(2026, 7, 15))
    assert late["gave_up"] == 1
    assert rid not in forecast_log.restaurants_due(today=date(2026, 7, 16))
    assert forecast_log.score_due(rid, today=date(2026, 7, 16)) == {"scored": 0, "unmeasurable": 0, "gave_up": 0}
    c = models.get_conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(forecast_log)").fetchall()}
    c.close()
    assert {"unscorable_at", "naive_last", "naive_mean", "naive_n"} <= cols


# ── Q9: the demand range covers what it says ────────────────────────────────

def test_q9_replay_b2_p7_the_demand_range_covers_about_80_percent():
    """B2 p7: the 10th-90th percentile of exactly eight nights covered 64%,
    62% and 61% of next nights, not 80%."""
    rng = random.Random(9)

    def cover(level_sd=0.0, growth=0.0, rho=0.8, reps=6000):
        inside = 0
        for _ in range(reps):
            lvl, xs = 0.0, []
            for w in range(9):
                lvl = rho * lvl + rng.gauss(0, level_sd * math.sqrt(1 - rho * rho)) if level_sd else 0.0
                xs.append(3000 * (1 + growth) ** w * math.exp(lvl + rng.gauss(0, 0.12)))
            lo, hi = demand.prediction_range(xs[:8])
            inside += lo <= xs[8] <= hi
        return inside / reps
    assert 0.75 <= cover() <= 0.85                                    # was 64%
    assert 0.73 <= cover(level_sd=0.08) <= 0.85                       # was 62%
    assert 0.70 <= cover(growth=0.015) <= 0.85                        # was 61%
    assert demand.prediction_range([1000] * 7) is None


# ── Q10: ratio features need measured days ──────────────────────────────────

def test_q10_replay_b3_p10_one_day_of_labor_is_not_a_benchmark():
    """B3 p10: one costed day at 61% published labor_pct_28d = 61.0, a
    benchmark key, at completeness 0.045."""
    rid = _rid()
    today = date(2026, 9, 24)
    c = models.get_conn()
    c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, labor_cost, sales, total_hours) "
              "VALUES (?,?,?,?,?,?)", (rid, (today - timedelta(days=1)).isoformat(), 61.0, 1830, 3000, 70))
    c.commit()
    c.close()
    f = features.compute(rid, today=today)
    assert f["labor_pct_28d"] is None and f["labor_hours_per_1k_28d"] is None       # was 61.0
    c = models.get_conn()
    for i in range(2, features.MIN_MEASURED_DAYS + 1):
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, labor_cost, sales, total_hours) "
                  "VALUES (?,?,?,?,?,?)", (rid, (today - timedelta(days=i)).isoformat(), 30.0, 900, 3000, 40))
    c.commit()
    c.close()
    f = features.compute(rid, today=today)
    assert f["labor_pct_28d"] is not None and f["labor_pct_sd_28d"] is not None


# ── Q11: trend strength, measured ───────────────────────────────────────────

def test_q11_replay_b1_p2c_a_zigzag_is_not_a_declining_trend_worth_a_band():
    """B1 p2c: [4.5, 3.9, 4.6, 3.8, 4.3] read "declining", medium — enough for
    the negative-trend alert and its email."""
    z = [4.5, 3.9, 4.6, 3.8, 4.3]
    slope = waste_trend._direction(z)["slope"]
    assert waste_trend._confidence(z, slope) == "low"                  # was medium
    ts = waste_trend.trend_strength(z)
    assert ts["trend_strength_pct"] <= 10 and ts["significant"] is False
    assert waste_trend.trend_strength([4.4, 4.3, 4.2])["trend_strength_pct"] is None   # not measurable, not 0
    clean = [4.6, 4.5, 4.45, 4.3, 4.2, 4.15, 4.0, 3.9]
    assert waste_trend._confidence(clean, waste_trend._direction(clean)["slope"]) == "high"
    rng = random.Random(1)
    noisy = sum(1 for _ in range(3000)
                if waste_trend._confidence(v := [rng.gauss(4.4, 0.3) for _ in range(8)],
                                           waste_trend._direction(v)["slope"]) in ("high", "medium"))
    assert noisy / 3000 <= 0.12                                        # every 5+ week series used to be medium


def test_q11_waste_notes_and_rating_trend_carry_the_measured_percentage():
    from tests.test_waste_trend import _weeks
    weeks = _weeks([100, 120, 140, 160, 180])
    s = waste_trend.waste_trend_stats(weeks)
    assert s["trend_strength_pct"] is not None
    obs = waste_trend.waste_trend_observations(s, weeks)
    note = next(o for o in obs if o.get("confidence"))
    assert "% trend strength" in note["confidence"] and "confidence" not in note["confidence"]
    assert note["trend_strength_pct"] == s["trend_strength_pct"]
    rid = _rid()
    t = review_intelligence.rating_trend(rid)
    assert "trend_strength_pct" in t and t["trend_strength_pct"] is None and t["next_week"] is None


# ── Q12: a recency-weighted rate beside the rate ────────────────────────────

def test_q12_kind_record_exposes_a_recency_weighted_rate():
    rid = _rid()
    now = datetime(2026, 9, 24)

    def ep(i, verdict, age):
        return {"rec_id": f"e{i}", "key": f"trim_day:{i}", "kind": "trim_day", "shown": True, "state": "accepted",
                "tag_list": [], "verdict": verdict,
                "verdict_at": (now - timedelta(days=age)).strftime("%Y-%m-%d %H:%M:%S"), "tracker": {}}
    eps = [ep(i, "no_clear_change", 300) for i in range(5)] + [ep(i + 5, "improved", 10) for i in range(5)]
    rec = rec_learning.kind_record(rid, "trim_day", now=now, episodes=eps)
    assert rec["rate"] == 0.5 and rec["source"] == "own"
    assert rec["rate_recent"] > 0.85 and rec["recent_half_life_days"] == rec_learning.RECENT_HALF_LIFE_DAYS
    assert 5.0 < rec["rate_recent_n_eff"] < 6.0
    assert rec["base_rate"] == rec_learning.BASE_RATE_STATED and rec["base_rate_source"] == "stated"
    few = rec_learning.kind_record(rid, "trim_day", now=now, episodes=eps[:3])
    assert few["rate_recent"] is None                                    # the same floor as `rate`
