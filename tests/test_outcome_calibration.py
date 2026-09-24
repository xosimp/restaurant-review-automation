"""Outcome measurement & learning honesty — confidence audit, group F (9/24/26).

Each test names the fix it pins (F1–F11 of the fix list; CA2 = the
calibration & learning audit, its probes R, L, M, B, E, C replayed here)
and fails on the code before the fix. No model, network, email, SMS or push.
"""
import json
import math
import random
import statistics
import sys
from datetime import date, datetime, timedelta

import pytest

import metrics
import models
import rec_ledger
import rec_learning
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import goals  # noqa: E402
import marketing_signals  # noqa: E402
import outcomes  # noqa: E402
import owner_report  # noqa: E402
import schedule_intel  # noqa: E402
import value_delivered  # noqa: E402
from intelligence import features, memory, patterns, scoring  # noqa: E402

TODAY = date.today()


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


def _rid(name="Calib Co", **kw):
    for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing"):
        kw.setdefault(m, 1)
    return create_restaurant(Restaurant(name=name, owner_email="c@x.test", **kw))


def _x(sql, args=()):
    c = models.get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _q(sql, args=()):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _days(rid, start, n, labor, sales=1000.0):
    """labor_daily_history for n days from start; `labor(d)` or a constant."""
    c = models.get_conn()
    for i in range(n):
        d = start + timedelta(days=i)
        lab = labor(d) if callable(labor) else labor
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), lab, sales,
                                          round(lab / sales * 100, 1)))
    c.commit()
    c.close()


# ── F1: regression to the mean (CA2 probe R) ────────────────────────────────

def _series(rng, days, mu=30.0, level_sd=0.8, rho=0.85, day_sd=3.0):
    """CA2 probe R's null process: a weekly AR(1) level plus day noise, and
    no intervention at all."""
    lvl, out = 0.0, []
    for d in range(days):
        if d % 7 == 0:
            lvl = rho * lvl + rng.gauss(0, level_sd * math.sqrt(1 - rho * rho))
        out.append(mu + lvl + rng.gauss(0, day_sd))
    return out


def test_f1_a_do_nothing_change_on_triggered_windows_reads_improved_as_often_as_worsened():
    """CA2 probe R replayed through the production rules: the baseline
    window outcomes places for a triggered tracker (mirror_window), the band
    metrics builds from the restaurant's own history (band_from_sigma) and
    metrics.compare. Against the trigger window (the old baseline) a change
    that did nothing read improved 54% / worsened 11%; it must now read
    improved ≈ worsened, and both near the stated false-alarm rate."""
    rng = random.Random(11)
    L, H = 28, 364
    t0 = date(2025, 1, 1)
    new = {"improved": 0, "worsened": 0, "n": 0}
    old = {"improved": 0, "worsened": 0}
    for _rep in range(2500):
        s = _series(rng, H + 3 * L)
        # Day index 0 is t0. The card fires on the trigger window; the owner
        # takes it a few days later (a gap), and the after-window starts then.
        gap = rng.randint(0, 6)
        trig = (t0 + timedelta(days=H), t0 + timedelta(days=H + L - 1))
        today = trig[1] + timedelta(days=1 + gap)
        a0 = (today - t0).days
        trig_mean = statistics.fmean(s[H:H + L])
        if trig_mean < 30.5:                       # "labor over target" fired
            continue
        after = statistics.fmean(s[a0:a0 + L])
        b0, b1 = outcomes.mirror_window(trig, today, L)
        base = statistics.fmean(s[(b0 - t0).days:(b1 - t0).days + 1])
        hist_end = (b1 - t0).days + 1
        wins = [statistics.fmean(s[i:i + L]) for i in range(hist_end - L * (hist_end // L), hist_end, L)]
        band, _sd = metrics.band_from_sigma(statistics.stdev(wins), L, L)
        v = metrics.compare("labor_pct", base, after, band=band)["verdict"]
        new["n"] += 1
        if v in ("improved", "worsened"):
            new[v] += 1
        # The old rule: the window right before the start, the stated band.
        ov = metrics.compare("labor_pct", statistics.fmean(s[a0 - L:a0]), after)["verdict"]
        if ov in ("improved", "worsened"):
            old[ov] += 1
    n = new["n"]
    assert n > 500
    imp, wor = new["improved"] / n, new["worsened"] / n
    # The old reading, for the record: the bias the fix removes.
    assert old["improved"] / n > 0.4 and old["worsened"] / n < 0.15
    # Symmetric within noise (a binomial 3-sigma band on the difference) and
    # near the band's stated two-sided 10% rate.
    se = math.sqrt((imp + wor) / n)
    assert abs(imp - wor) < 3 * se + 0.01, (imp, wor)
    assert imp + wor < 0.2, (imp, wor)


def test_f1_mirror_window_sits_as_far_before_the_trigger_as_the_after_window_sits_after():
    trig = (date(2026, 8, 1), date(2026, 8, 28))
    start, end = outcomes.mirror_window(trig, date(2026, 9, 2), 28)        # 4 days after the trigger
    assert (end, start) == (date(2026, 7, 27), date(2026, 6, 30))
    assert (trig[0] - end).days - 1 == (date(2026, 9, 2) - trig[1]).days - 1


def _shown(rid, key, metric="labor_pct", module="labor", when=None):
    rec_id = rec_ledger.present(rid, key, module, "home", expected_metric=metric)
    if when is not None:
        _x("UPDATE rec_instances SET created_at=? WHERE rec_id=?", (f"{when.isoformat()} 09:00:00", rec_id))
    return rec_id


def test_f1_a_triggered_tracker_is_measured_clear_of_its_trigger_window_and_stores_it():
    rid = _rid()
    start = TODAY - timedelta(days=120)
    # Steady 30% for 92 days, then a bad 28 days (the trigger), then today.
    _days(rid, start, 92, labor=300.0)
    _days(rid, start + timedelta(days=92), 28, labor=340.0)
    _shown(rid, "trim_day:Monday", when=TODAY)
    o = outcomes.record(rid, "recommendation", "trim_day:Monday", "Trim Mondays", "labor_pct", today=TODAY)
    assert o["baseline_kind"] == outcomes.TRIGGER_BASELINE_KIND
    assert (o["trigger_start"], o["trigger_end"]) == ((TODAY - timedelta(days=28)).isoformat(),
                                                      (TODAY - timedelta(days=1)).isoformat())
    assert o["trigger_value"] == 34.0 and o["baseline_value"] == 30.0          # not the bad window
    assert o["baseline_end"] < o["trigger_start"] and not o["baseline_overlaps_trigger"]


def test_f1_a_recommendation_taken_long_after_it_fired_uses_the_plain_baseline():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=200), 200, labor=300.0)
    _shown(rid, "trim_day:Wednesday", when=TODAY - timedelta(days=90))
    o = outcomes.record(rid, "recommendation", "trim_day:Wednesday", "Trim Wednesdays", "labor_pct", today=TODAY)
    assert o["trigger_end"] == (TODAY - timedelta(days=91)).isoformat()
    assert o["baseline_kind"] == "matched weekdays" and not o["baseline_overlaps_trigger"]


def test_f1_an_untriggered_tracker_keeps_its_plain_baseline():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=60), 60, labor=300.0)
    o = outcomes.record(rid, "manual", "manual:mine", "My idea", "labor_pct", today=TODAY)
    assert o["baseline_kind"] == "matched weekdays" and o["trigger_start"] is None


def test_f1_a_baseline_that_overlaps_the_trigger_is_shown_never_counted():
    rid = _rid()
    t0 = TODAY - timedelta(days=28)
    # Only the trigger window and after it exist: no clean baseline.
    _days(rid, t0 - timedelta(days=28), 28, labor=340.0)
    _days(rid, t0, 28, labor=280.0)
    _shown(rid, "trim_day:Tuesday", when=t0)
    o = outcomes.record(rid, "recommendation", "trim_day:Tuesday", "Trim Tuesdays", "labor_pct", today=t0)
    assert o["baseline_overlaps_trigger"] is True
    e = outcomes.evaluate(o["id"], today=TODAY)
    assert e["verdict"] == "improved"                                     # the move is reported…
    assert e["counts"] is False and e["attribution"] == "associated"      # …never counted
    assert "prompted the recommendation" in e["attribution_label"]
    assert rec_learning.learned_verdict(e["verdict"], e) == "unknown"
    assert outcomes.total_value(rid)["monthly"] == 0
    outcomes.accrue_due(rid, today=TODAY)
    assert outcomes.cumulative(rid)["total"] is None
    assert "isn't counted" in outcomes.summarise(e)


def test_f1_trigger_columns_are_added_at_boot(db):
    cols = {r["name"] for r in _q("PRAGMA table_info(recommendation_outcomes)")}
    assert {c for c, _d in outcomes._ADDED_COLUMNS} <= cols


# ── F2: the restaurant's own noise band (CA2 probe L) ───────────────────────

def test_f2_probe_l_a_volatile_restaurant_no_longer_reads_two_thirds_of_nothing_as_a_move():
    """No real change, 28-day window means with sd 0.8: the stated 0.5-point
    band read 66% of them as moved (CA2 probe L). With the band estimated
    from the restaurant's own windows the false-alarm rate is near 10%."""
    rng = random.Random(7)
    moved_stated = moved_own = 0
    reps = 3000
    for _ in range(reps):
        hist = [30 + rng.gauss(0, 0.8) for _ in range(13)]
        before, after = 30 + rng.gauss(0, 0.8), 30 + rng.gauss(0, 0.8)
        band, _sd = metrics.band_from_sigma(statistics.stdev(hist), 28, 28)
        moved_stated += metrics.compare("labor_pct", before, after)["verdict"] != "no_clear_change"
        moved_own += metrics.compare("labor_pct", before, after, band=band)["verdict"] != "no_clear_change"
    assert moved_stated / reps > 0.5
    assert moved_own / reps < 0.15


def test_f2_the_stated_band_is_a_floor_never_narrowed():
    assert metrics.compare("labor_pct", 30.0, 29.6, band=0.1)["verdict"] == "no_clear_change"
    assert metrics.compare("labor_pct", 30.0, 29.0, band=1.5)["verdict"] == "no_clear_change"
    assert metrics.compare("labor_pct", 30.0, 28.0, band=1.5)["verdict"] == "improved"


def test_f2_noise_band_is_estimated_from_the_restaurants_own_windows_and_states_its_rate():
    rid = _rid()
    start = TODAY - timedelta(days=364)
    # Thirteen 28-day windows alternating 29% / 31%: a 1-point spread each way.
    _days(rid, start, 364, labor=lambda d: 290.0 if ((d - start).days // 28) % 2 == 0 else 310.0)
    nb = metrics.noise_band(rid, "labor_pct", window_days=28, end=start + timedelta(days=363), before=30.0)
    assert nb["method"] == "own windows" and nb["n_windows"] == 13
    assert 0.9 < nb["sigma"] < 1.1 and nb["band"] > 0.5
    assert 0.0 < nb["false_alarm_rate"] <= 0.11
    thin = metrics.noise_band(_rid(name="Thin"), "labor_pct", window_days=28, before=30.0)
    assert thin["band"] is None and thin["false_alarm_rate"] is None and thin["method"] == "stated"


def test_f2_a_tracker_stores_its_band_and_reads_every_window_against_it():
    rid = _rid()
    start = TODAY - timedelta(days=364 + 28)
    _days(rid, start, 364, labor=lambda d: 290.0 if ((d - start).days // 28) % 2 == 0 else 310.0)
    t0 = start + timedelta(days=364)
    _days(rid, t0, 28, labor=lambda d: 300.0 - 12.0)       # 1.2 points lower: past 0.5, inside the own band
    o = outcomes.record(rid, "manual", "manual:x", "Trim", "labor_pct", today=t0)
    assert o["noise_band"] and o["noise_band"] > 1.2 and o["false_alarm_rate"] is not None and o["band_basis"]
    e = outcomes.evaluate(o["id"], today=t0 + timedelta(days=28))
    assert e["verdict"] == "no_clear_change"


def test_f2_probe_m_a_post_that_did_nothing_rarely_reads_lifted_or_dropped():
    """CA2 probe M: 15% weekday variation, 4 baseline days, a 1σ band: a
    do-nothing post read lifted 24.9% and dropped 23.9%."""
    rng = random.Random(5)
    moved, reps = 0, 4000
    for _ in range(reps):
        base = [rng.gauss(1000, 150) for _ in range(4)]
        win = [rng.gauss(1000, 150) for _ in range(2)]
        lift = (statistics.fmean(win) - statistics.fmean(base)) / statistics.fmean(base) * 100
        moved += marketing_signals.lift_verdict(lift, marketing_signals.noise_band_pct(base, window_n=2)) \
            != "no_clear_change"
    assert moved / reps < 0.13


def _post(rid, posted, menu_item_id=None):
    return _x("INSERT INTO marketing_content_log (restaurant_id, topic, post_id, post_platform, posted_at, created_at, "
              "menu_item_id) VALUES (?,?,?,?,?,?,?)",
              (rid, "Brisket night", "p1", "instagram", posted, posted, menu_item_id))


def test_f2_a_post_needs_two_weeks_of_baseline(monkeypatch):
    rid = _rid()
    posted = datetime(2026, 9, 4, 8, 0)                    # a Friday morning, before trade
    pid = _post(rid, posted.strftime("%Y-%m-%d %H:%M:%S"))
    one_week = {"2026-09-04": 1500.0, "2026-09-05": 1500.0, "2026-08-28": 1000.0, "2026-08-29": 1000.0}
    monkeypatch.setattr(marketing_signals, "daily_sales", lambda r: one_week)
    monkeypatch.setattr(marketing_signals, "_local_post_time", lambda s, tz: posted)
    assert marketing_signals.attribution_for_post(rid, pid)["reason"] == "not_enough_history"
    two = dict(one_week, **{"2026-08-21": 1000.0, "2026-08-22": 1000.0})
    monkeypatch.setattr(marketing_signals, "daily_sales", lambda r: two)
    got = marketing_signals.attribution_for_post(rid, pid)
    assert got["ok"] and got["baseline_weeks"] == 2 and got["false_alarm_rate"] == 0.1


def test_f2_dish_units_carry_a_band_and_group_medians_respect_verdicts():
    posts = [{"lift_pct": 4.0, "verdict": "no_clear_change", "post_kind": "dish", "noise_band_pct": 8.0},
             {"lift_pct": 6.0, "verdict": "no_clear_change", "post_kind": "dish", "noise_band_pct": 8.0},
             {"lift_pct": 20.0, "verdict": "lifted", "post_kind": "dish", "noise_band_pct": 8.0}]
    g = marketing_signals._group_lift(posts, "post_kind")[0]
    assert g["median_lift_pct"] == 6.0 and g["verdict"] == "no_clear_change"
    assert g["verdicts"] == {"lifted": 1, "dropped": 0, "no_clear_change": 2}
    band = marketing_signals.noise_band_pct([10, 14, 9, 13], window_n=2)
    assert band > marketing_signals.MIN_NOISE_BAND_PCT


# ── F3: one success definition (CA2 probe B) ────────────────────────────────

def _evaluated(rid, key, metric, verdict, days_ago=40, checkin=None, recheck=None, source="recommendation",
               attribution="consistent", dollars=None, overlaps=0):
    s = TODAY - timedelta(days=days_ago)
    e = s + timedelta(days=28)
    return _x("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
              "baseline_start, baseline_end, started_on, evaluate_on, after_value, after_start, after_end, verdict, "
              "delta, dollars_monthly, status, module, attribution, concurrent, baseline_kind, owner_checkin, "
              "recheck_verdict, baseline_overlaps_trigger) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'evaluated','labor',?,'[]','prior window',?,?,?)",
              (rid, source, key, key, metric, 32.0, (s - timedelta(days=28)).isoformat(),
               (s - timedelta(days=1)).isoformat(), s.isoformat(), e.isoformat(), 29.0, s.isoformat(),
               (e - timedelta(days=1)).isoformat(), verdict, -3.0, dollars, attribution,
               json.dumps(checkin) if checkin else None, recheck, overlaps))


def test_f3_probe_b_one_reversed_disowned_result_is_no_benchmark():
    rid = _rid()
    _evaluated(rid, "trim_day:Mon", "labor_pct", "improved", checkin={"did_it": "no"}, recheck="reversed")
    f = features.compute(rid)
    assert f["outcomes_improved_rate_90d"] is None
    rid2 = _rid(name="Five Co")
    for i, m in enumerate(("labor_pct", "sales", "avg_rating", "weekly_waste", "overtime_hours")):
        _evaluated(rid2, f"k:{i}", m, "improved" if i < 3 else "no_clear_change")
    _evaluated(rid2, "k:disowned", "comp_rate", "improved", checkin={"did_it": "no"})
    assert features.compute(rid2)["outcomes_improved_rate_90d"] == 0.6


def test_f3_recs_features_come_from_the_ledger_not_home_dismissals():
    rid = _rid()
    for i in range(3):
        rec_ledger.present(rid, f"cut_waste:x{i}", "food", "food")
        rec_ledger.record(rid, f"cut_waste:x{i}", "completed", surface="food")
    rec_ledger.present(rid, "trim_day:Mon", "labor", "home")
    rec_ledger.record(rid, "trim_day:Mon", "dismissed", surface="home")
    f = features.compute(rid)
    assert (f["recs_answered_28d"], f["recs_done_28d"], f["recs_declined_28d"]) == (4, 3, 1)


def test_f3_a_success_rate_is_not_shown_below_the_floor():
    rows = [{"restaurant_id": r, "source_key": f"k{r}", "action": "measured", "outcome": "improved",
             "days_to_effect": 10} for r in range(1, 7)]
    s = scoring._summarise(rows[:4])
    assert s["success_rate"] == 1.0 and s["success_enough"] is False
    assert scoring.public(dict(s, success_available=True, acceptance_available=True))["success_rate"] is None
    assert scoring.public(dict(scoring._summarise(rows), success_available=True,
                               acceptance_available=True))["success_rate"] == 1.0


def test_f3_summarise_and_decisions_carry_the_grade_and_held_only_when_held():
    rid = _rid()
    held = outcomes.get_outcome(_evaluated(rid, "a:1", "labor_pct", "improved", attribution="held",
                                           recheck="held", dollars=400.0))
    once = outcomes.get_outcome(_evaluated(rid, "a:2", "sales", "improved", attribution="associated",
                                           dollars=400.0, days_ago=80))
    assert "held when re-checked" in outcomes.summarise(held)
    s = outcomes.summarise(once)
    assert "held" not in s and "not yet a clear result" in s and "not proven cause" in s
    import decisions
    line = decisions._fmt_outcome({"status": "evaluated", "verdict": "improved", "learned_verdict": "improved",
                                   "dollars_monthly": 400.0, "grade_phrase": outcomes.grade_phrase(once)})
    assert "not yet a clear result" in line and "held" not in line
    gone = decisions._fmt_outcome({"status": "evaluated", "verdict": "improved", "learned_verdict": "unknown"})
    assert "not counted" in gone


# ── F4: learning and value count the same results ───────────────────────────

def test_f4_learning_counts_a_result_exactly_when_value_does():
    """CA2 finding 7: the same result read "can't tell" in learning and "$X
    delivered" in value. Every combination of verdict, check-in, re-check,
    key kind and trigger overlap now counts in both or neither."""
    import itertools
    combos = itertools.product(
        ("improved", "worsened", "no_clear_change", "unknown"),
        (None, {"did_it": "no"}, {"did_it": "yes", "conditions_changed": True},
         {"did_it": "yes", "conditions_changed": False}, {"did_it": "partly"}),
        (None, "held", "faded", "reversed"),
        ("trim_day:Mon", "observed:alert_labor_over:2026-09", "observed:supplier_order_sent:2026-09",
         "observed:untaken:abc", "observed:schedule_published:2026-09"),
        (0, 1))
    for verdict, checkin, recheck, key, overlaps in combos:
        r = {"status": "evaluated", "verdict": verdict, "owner_checkin": json.dumps(checkin) if checkin else None,
             "recheck_verdict": recheck, "source_key": key, "baseline_overlaps_trigger": overlaps}
        learned = rec_learning.learned_verdict(verdict, r)
        counted = outcomes.counts_in_delivered(r)
        assert (learned in ("improved", "worsened")) == counted, r
        if counted:
            assert learned == verdict, r


def test_f4_the_informational_prefixes_are_held_in_step():
    assert rec_learning.INFORMATIONAL_PREFIXES == outcomes.INFORMATIONAL_PREFIXES
    assert outcomes.UNTAKEN_PREFIX in outcomes.INFORMATIONAL_PREFIXES


def test_f4_something_else_changed_stops_accrual_and_the_supplier_order_is_informational():
    rid = _rid()
    order = outcomes.observe(rid, "supplier_order_sent", user_id=1)
    assert order["informational"] and order["counts"] is False
    oid = _evaluated(rid, "trim_day:Wed", "labor_pct", "improved", dollars=300.0)
    _days(rid, TODAY - timedelta(days=80), 80, labor=300.0)
    outcomes.accrue_due(rid, today=TODAY)
    assert (outcomes.cumulative(rid)["total"] or 0) > 0
    r = outcomes.apply_checkin(rid, oid, "yes", True)
    assert r["counts"] is False
    assert outcomes.cumulative(rid)["total"] in (None, 0)
    assert outcomes.total_value(rid)["monthly"] == 0


def test_f4_associated_dollars_are_reported_apart_from_consistent_or_held():
    rid = _rid()
    _evaluated(rid, "a:1", "labor_pct", "improved", attribution="held", recheck="held", dollars=400.0)
    _evaluated(rid, "a:2", "overtime_hours", "improved", attribution="associated", dollars=150.0, days_ago=120)
    v = outcomes.total_value(rid)
    assert (v["consistent_monthly"], v["associated_monthly"]) == (400.0, 150.0)
    assert v["monthly"] == 550.0
    d = value_delivered.delivered(rid)
    assert d["associated_monthly"] == 150.0 and d["consistent_monthly"] == 400.0
    assert any("$150/month came alongside other changes" in s for s in value_delivered.value_lines(d))


def test_f4_the_owner_report_quotes_a_range_from_three_results_and_its_caveat():
    c = {"metric": "labor_pct", "label": "Labor %", "unit": "%", "results": 3, "improved": 3, "worsened": 0,
         "no_clear_change": 0, "mean_delta": -1.5, "mean_delta_pct": -5.0, "range": [-2.1, -0.9],
         "consistent_direction": True}
    s = owner_report._change_sentence(c)
    assert "ranged from 0.9 points to 2.1 points" in s and owner_report.CAVEAT in s
    assert owner_report.MIN_RESULTS_PER_METRIC >= 3
    m = owner_report._measured_sentence({"total": 900.0, "measured_days": 40, "by_module": {"labor": 900.0},
                                         "by_grade": {"consistent_or_held": 600.0, "associated": 300.0}})
    assert m.endswith(owner_report.CAVEAT) and "$600 was a clear move" in m and "$300" in m


# ── F5: advice not taken, the comparison group ──────────────────────────────

def test_f5_a_dismissed_recommendation_gets_an_informational_tracker_and_a_comparison():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=90), 90, labor=300.0)
    _shown(rid, "trim_day:Friday", when=TODAY - timedelta(days=3))
    rec_ledger.record(rid, "trim_day:Friday", "dismissed", surface="home", meta={"reason_code": "too_costly"})
    _shown(rid, "trim_day:Sunday", when=TODAY - timedelta(days=3))
    rec_ledger.record(rid, "trim_day:Sunday", "dismissed", surface="home", meta={"reason_code": "already_doing"})
    assert outcomes.observe_untaken(rid, today=TODAY) == 1
    rows = _q("SELECT * FROM recommendation_outcomes WHERE restaurant_id=?", (rid,))
    assert len(rows) == 1 and rows[0]["source_key"].startswith(outcomes.UNTAKEN_PREFIX)
    t = outcomes.get_outcome(rows[0]["id"])
    assert t["informational"] and t["trigger_start"] is not None
    assert outcomes.list_outcomes(rid) == []                               # never an owner's result to list
    assert len(outcomes.list_outcomes(rid, include_untaken=True)) == 1
    assert outcomes.observe_untaken(rid, today=TODAY) == 0                # once
    # A real change on the number is never blocked by it.
    outcomes.record(rid, "manual", "manual:x", "Mine", "labor_pct", today=TODAY)
    cmp = rec_learning.untaken_comparison(rid, "trim_day", taken_measured=6, taken_improved=4)
    assert cmp["measured"] == 0 and cmp["label"] is None


def test_f5_the_comparison_reads_compared_with_when_you_didnt():
    rid = _rid()
    for i in range(5):
        rec_id = _shown(rid, f"trim_day:D{i}")
        _evaluated(rid, f"{outcomes.UNTAKEN_PREFIX}{rec_id}", "labor_pct", "improved" if i < 2 else "worsened",
                   days_ago=40 + 30 * i, source="observed")
    cmp = rec_learning.untaken_comparison(rid, "trim_day", taken_measured=5, taken_improved=4)
    assert (cmp["measured"], cmp["improved"]) == (5, 2)
    assert cmp["label"].startswith("Compared with when you didn't") and "not proof of cause" in cmp["label"]
    assert rec_learning.kind_record(rid, "trim_day")["untaken"]["measured"] == 5


# ── F6: the ranking weight (CA2 probe E) ────────────────────────────────────

def _ep(i, kind, state, verdict=None, dollar=None, realised=None, age_days=30):
    now = datetime(2026, 9, 24)
    at = (now - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    return {"rec_id": f"r{i}", "key": f"{kind}:k{i}", "kind": kind, "shown": True, "state": state,
            "tag_list": [], "verdict": verdict, "verdict_at": at if verdict else None,
            "dollar_value": dollar, "tracker": ({"dollars_monthly": realised} if realised is not None else {})}


def _w(eps):
    m = rec_learning.Effectiveness(1, eps, cohort=None, now=datetime(2026, 9, 24))
    return m.weight("trim_day:new", kind="trim_day", tags=[])[0]


def test_f6_probe_e_three_results_no_longer_rank_like_thirty_and_acceptance_alone_lifts_nothing():
    three = _w([_ep(i, "trim_day", "accepted", "improved") for i in range(3)])
    thirty = _w([_ep(i, "trim_day", "accepted", "improved") for i in range(30)])
    assert three < 1.05 < thirty <= rec_learning.MAX_WEIGHT
    assert _w([_ep(i, "trim_day", "accepted") for i in range(10)]) <= 1.0       # liked, never measured
    assert 0.9 < _w([_ep(i, "trim_day", "dismissed") for i in range(10)]) < 1.0
    assert rec_learning.W_ACCEPT <= 0.2


def test_f6_displayed_dollars_are_adjusted_from_measured_results():
    eps = [_ep(i, "trim_day", "accepted", "improved", dollar=1000, realised=600) for i in range(4)]
    m = rec_learning.Effectiveness(1, eps, cohort=None, now=datetime(2026, 9, 24))
    got = m.adjusted_dollars("trim_day:Friday", 500.0)
    assert got["calibration_n"] == 4 and got["dollars_adjusted"] == round(500.0 * got["calibration_ratio"], 2)
    assert got["dollars_adjusted"] < 500.0 and got["note"] == "adjusted from 4 measured results"
    few = rec_learning.Effectiveness(1, eps[:2], cohort=None).adjusted_dollars("trim_day:Friday", 500.0)
    assert few["dollars_adjusted"] is None and few["calibration_n"] == 2


# ── F7: an automatic action is neither taken nor declined ───────────────────

def test_f7_auto_is_its_own_bucket():
    rows = [{"restaurant_id": 1, "source_key": "auto:x:1", "action": "auto", "outcome": None, "days_to_effect": None},
            {"restaurant_id": 1, "source_key": "trim:1", "action": "done", "outcome": None, "days_to_effect": None},
            {"restaurant_id": 1, "source_key": "trim:2", "action": "not_for_us", "outcome": None,
             "days_to_effect": None}]
    s = scoring._summarise(rows)
    assert (s["accepted"], s["declined"], s["auto"]) == (1, 1, 1) and s["acceptance_rate"] == 0.5


# ── F8: the schedule recommendation verdict and watched nights ──────────────

def test_f8_a_daypart_nobody_watched_never_reads_no_issues(monkeypatch):
    rid = _rid()
    for w in range(2):
        d = date(2026, 9, 4) + timedelta(weeks=w)
        hid = _x("INSERT INTO schedule_history (restaurant_id, week_start, week_end) VALUES (?,?,?)",
                 (rid, (d - timedelta(days=4)).isoformat(), (d + timedelta(days=2)).isoformat()))
        _x("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, issues) VALUES (?,?,?,?,?,0)",
           (rid, hid, d.isoformat(), "night", 10))
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda *a, **k: set())
    o = schedule_intel.outcomes_by_daypart(rid)["Friday"]["night"]
    assert o["issues"] is None and o["watched"] == 0 and "not watched" in o["issues_label"]
    monkeypatch.setattr(schedule_intel, "watched_dates", lambda *a, **k: {"2026-09-04", "2026-09-11"})
    o = schedule_intel.outcomes_by_daypart(rid)["Friday"]["night"]
    assert o["issues"] == 0 and o["issues_label"] == "no issues on 2 watched nights"


# ── F9: memory slopes per unit, pattern strength ───────────────────────────

def test_f9_a_slope_is_said_only_past_the_metrics_own_band():
    mem = {"slopes": {"avg_rating_30d": {"slope_per_week": 0.03, "latest": 4.3, "weeks": 12},
                      "labor_pct_28d": {"slope_per_week": 0.04, "latest": 30.0, "weeks": 12},
                      "food_cost_pct_28d": {"slope_per_week": 0.06, "latest": 30.0, "weeks": 12}}}
    lines = memory.lines(mem)
    # The flat 0.05/week rule said none of the rating (0.33★ over the weeks
    # read — a real move) and would have said the food cost wobble.
    assert any("avg_rating_30d" in l and "★" in l for l in lines)     # 0.33★ over 11 steps: past 0.1★
    assert not any("food_cost_pct_28d" in l for l in lines)            # 0.66 pts: inside the 1-point band
    assert not any("labor_pct_28d" in l for l in lines)                # 0.44 pts: inside the 0.5-point band


def test_f9_patterns_carry_strength_pct_never_a_probability():
    d = patterns.strength_fields({"confidence": 0.78, "sentence": "x"})
    assert d["strength_pct"] == 78 and d["confidence"] == 0.78 and d["strength_label"] == "pattern strength"
    assert "not the chance" in d["strength_basis"]
    import inspect
    import intelligence
    src = inspect.getsource(intelligence.context_lines)
    assert "confidence):" not in src and "not a" in src


# ── F10: a goal is met past its band ────────────────────────────────────────

def test_f10_a_reading_a_hair_past_the_target_is_at_target_not_met():
    rid = _rid()
    _days(rid, TODAY - timedelta(days=27), 28, labor=299.0)             # 29.9% against a 30% target
    _x("INSERT INTO owner_goals (restaurant_id, metric, target, baseline_value, status) VALUES (?,?,?,?,'active')",
       (rid, "labor_pct", 30.0, 31.0))
    g = goals.progress(rid, today=TODAY)[0]
    assert g["state"] == "at_target" and g["band"] >= 0.5
    assert "within its normal week-to-week movement" in goals.summarise(g)
    assert goals.mark_achieved(rid, today=TODAY + timedelta(days=60)) == []
    _x("UPDATE labor_daily_history SET labor_cost=280.0 WHERE restaurant_id=?", (rid,))   # 28%: clearly past
    assert goals.progress(rid, today=TODAY)[0]["state"] == "met"


# ── F11: kind_record keeps reading through learned_verdict ─────────────────

def test_f11_kind_record_never_counts_a_trigger_window_result():
    tr = {"id": 7, "status": "evaluated", "verdict": "improved", "baseline_overlaps_trigger": 1,
          "metric": "labor_pct", "after_start": "2026-08-01", "after_end": "2026-08-28", "source_key": "trim_day:x"}
    v = rec_learning.learned_verdict("improved", tr)
    eps = [{"rec_id": f"r{i}", "key": f"trim_day:k{i}", "kind": "trim_day", "shown": True, "state": "completed",
            "verdict": v, "tracker": dict(tr, id=i)} for i in range(6)]
    r = rec_learning.kind_record(1, "trim_day", episodes=eps)
    assert v == "unknown" and r["measured"] == 0 and r["rate"] is None
