"""The Restaurant Intelligence Engine (INTELLIGENCE_ENGINE.md).

Three things these tests exist to prove, in order of importance:
1. Nothing cross-restaurant leaves the engine below the cohort floor or
   carrying an identity — asserted on every payload, not on one fixture.
2. A pattern needs real evidence: both sides big enough, an effect, a
   permutation p, and a false-discovery q. Noise discovers nothing.
3. Each module is testable alone, and the score is deterministic.
"""
import json
import random
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant

# Imported before any fixture patches get_conn (CLAUDE.md's bound-import hazard).
import intelligence  # noqa: E402
from intelligence import (privacy, stats, categories, features, feedback, scoring, memory, patterns,  # noqa: E402
                          benchmarks, trends, confidence, jobs, dashboard)
import home_brief, outcomes, issues  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    for mod in (models, features, feedback, scoring, memory, patterns, benchmarks, trends, confidence, jobs, dashboard,
                intelligence, home_brief, outcomes, issues):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import auth
    auth.init_auth(db_path=db_path)


def _rid(db_path, name="Moat Co", **kw):
    kw.setdefault("module_reviews", 1)
    # Live long enough, on a real labor cost basis, to stand in a band
    # (Benchmarking audit #14, #39).
    kw.setdefault("created_at", (date.today() - timedelta(days=120)).isoformat() + "T00:00:00")
    kw.setdefault("hourly_rate", 18.0)
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.replace(' ', '').lower()}@x.com", **kw), db_path=db_path)


def _confirm(db_path, rid, service_model, concept):
    """An owner-confirmed profile: the only thing a peer partition is built
    from (Benchmarking audit #7, #20)."""
    update_restaurant(rid, {"service_model": service_model, "concept": concept, "category": concept,
                            "profile_source": "set", "profile_confirmed_at": "2026-09-01T00:00:00"}, db_path=db_path)


def _seed_features(db_path, rid, week, f, completeness=0.8):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,?) "
                 "ON CONFLICT(restaurant_id, week) DO UPDATE SET features_json=excluded.features_json",
                 (rid, week, json.dumps(f), completeness))
    conn.commit(); conn.close()


THIS_WEEK = features.iso_week(date.today())


# ── privacy ──────────────────────────────────────────────────────────────────

def test_the_anonymity_check_catches_identity_and_dollars_anywhere_in_a_payload():
    ok = {"cohort": "pizza", "n": 7, "patterns": [{"sentence": "x", "n_with": 5, "effect": 0.2}], "restaurants": 12}
    privacy.assert_anonymous(ok)
    for bad in ({"restaurant_id": 1}, {"rows": [{"owner_email": "a@b"}]}, {"x": {"employee_name": "Ana"}},
                {"net_sales": 1000}, {"a": [[{"place_id": "x"}]]}, {"labor_cost": 1}):
        with pytest.raises(privacy.PrivacyError):
            privacy.assert_anonymous(bad)
    assert privacy.cohort_ok(5) and not privacy.cohort_ok(4) and not privacy.cohort_ok(None)


def test_every_cross_restaurant_surface_refuses_below_the_floor(db_path):
    rids = [_rid(db_path, f"R{i}") for i in range(4)]        # one short of the floor
    for r in rids:
        update_restaurant(r, {"category": "pizza"}, db_path=db_path)   # a type the owner set
    for r in rids:
        _seed_features(db_path, r, THIS_WEEK, {"avg_rating_30d": 4.2, "response_24h_rate_30d": 0.9, "avg_rating_delta": 0.3,
                                               "labor_pct_28d": 30})
    cohorts = {r: "pizza" for r in rids}
    assert patterns.discover(db_path=db_path, cohorts=cohorts)["active"] == 0
    assert benchmarks.compute(db_path=db_path, cohorts=cohorts)["written"] == 0
    b = benchmarks.benchmark(rids[0], "avg_rating_30d", cohort="pizza", db_path=db_path)
    assert b["available"] is False and f"at least {benchmarks.MIN_QUARTILE_N}" in b["reason"]
    assert trends.platform_trends(cohorts=cohorts, db_path=db_path) == []
    s = scoring.kind_stats("trim_day", cohort="pizza", db_path=db_path)
    assert s["available"] is False


# ── statistics ───────────────────────────────────────────────────────────────

def test_the_permutation_test_and_fdr_behave():
    a = [4.5, 4.6, 4.4, 4.7, 4.5, 4.6, 4.8, 4.5]
    b = [3.9, 4.0, 4.1, 3.8, 4.0, 3.9, 4.1, 4.0]
    diff, p = stats.permutation_test(a, b)
    assert 0.5 < diff < 0.7 and p < 0.01
    same = [4.0, 4.2, 3.9, 4.1, 4.0, 4.3, 3.8, 4.1]
    _, p2 = stats.permutation_test(same, list(reversed(same)))
    assert p2 > 0.5
    assert stats.permutation_test(a, b)[1] == stats.permutation_test(a, b)[1]        # seeded
    q = stats.benjamini_hochberg([0.01, 0.04, 0.03, None, 0.5])
    assert q[3] is None and q[0] <= q[2] <= q[1] <= q[4] and q[4] == 0.5
    assert stats.percentile([1, 2, 3, 4], 50) == 2.5 and stats.percentile([], 50) is None
    assert round(stats.slope([1, 2, 3, 4]), 6) == 1.0 and stats.slope([1]) is None
    assert stats.shrink(1.0, 2) < 1.0 and stats.shrink(1.0, 200) > 0.97
    assert stats.cohen_d([1, 1, 1], [2, 2, 2]) is None            # no spread → no d, not infinity


# ── categories ───────────────────────────────────────────────────────────────

def test_a_set_category_wins_and_inference_is_labelled(db_path):
    rid = _rid(db_path, "Gia Mia Pizzeria")
    r = models.get_restaurant(rid, db_path=db_path)
    assert categories.category_for(r) == ("pizza", "inferred")
    update_restaurant(rid, {"category": "italian"}, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.category == "italian" and categories.category_for(r) == ("italian", "set")
    blank = _rid(db_path, "XQZ")
    assert categories.category_for(models.get_restaurant(blank, db_path=db_path)) == (None, None)
    assert categories.valid("sports_bar") and not categories.valid("spaceship")


# ── level 1: features ────────────────────────────────────────────────────────

def test_features_are_ratios_from_own_rows_and_none_when_unmeasured(db_path):
    rid = _rid(db_path, module_labor=1)
    other = _rid(db_path, "Other")
    conn = get_conn(db_path)
    now = date.today()
    for i in range(6):
        d = (now - timedelta(days=i + 1)).isoformat()
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, "
                     "response_status, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rid, "google", f"r{i}", "A", 5 if i < 4 else 2, "t", d + "T10:00:00", d + "T11:00:00",
                      "approved" if i < 3 else "pending", (d + "T12:00:00") if i < 3 else None))
    for i in range(28):
        d = (now - timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, d, "Mon", 30 + (i % 3), 900, 3000, 40))
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, response_status) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (other, "google", "o1", "B", 1, "t", now.isoformat(), now.isoformat(), "pending"))
    conn.commit(); conn.close()
    f = features.compute(rid, today=now, db_path=db_path)
    assert f["reviews_30d"] == 6 and f["avg_rating_30d"] == 4.0
    # Three timed replies are under MIN_REVIEWS_FOR_RATIO (re-audit B3 #11):
    # a 24-hour rate from three replies is not a measured ratio.
    assert f["reply_rate_30d"] == 0.5 and f["response_24h_rate_30d"] is None
    assert f["labor_pct_28d"] == round(sum(30 + (i % 3) for i in range(28)) / 28, 2) and f["labor_pct_sd_28d"] is not None
    assert f["food_cost_pct_28d"] is None and f["campaign_tap_rate_28d"] is None        # unmeasured, never 0
    assert "sales" not in f and not any(k.endswith("_cost") for k in f)               # ratios only
    week = features.store(rid, f, today=now, db_path=db_path)
    latest = features.latest(rid, db_path=db_path)
    assert latest["week"] == week and 0 < latest["completeness"] < 1
    # the other restaurant's row never touched this one — its one review is
    # counted, and one review is under the ratio floor (re-audit B3 #11)
    other_f = features.compute(other, today=now, db_path=db_path)
    assert other_f["reviews_30d"] == 1 and other_f["avg_rating_30d"] is None


# ── level 1: feedback derived from the tables that already record answers ────

def test_feedback_sync_derives_events_idempotently(db_path):
    rid = _rid(db_path, module_labor=1)
    home_brief.dismiss(rid, "trim_day:Monday", kind="not_for_us", reason="delivery day")
    home_brief.dismiss(rid, "cut_waste:Salmon", kind="done")
    outcomes.record(rid, "home", "reprice:2026-09", "Prices changed", "food_cost_pct", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE recommendation_outcomes SET status='evaluated', verdict='improved', started_on='2026-08-01', evaluate_on='2026-08-29'")
    conn.execute("INSERT INTO ask_cavnar_actions (restaurant_id, action, summary, outcome) VALUES (?,?,?,?)",
                 (rid, "draft_campaign", "Patio text", "confirmed"))
    conn.commit(); conn.close()
    out = feedback.sync(db_path=db_path, cohorts={rid: "pizza"})
    assert out["events"] == 5
    assert feedback.sync(db_path=db_path)["events"] == 0                # idempotent
    hist = feedback.history(rid, db_path=db_path)
    kinds = {(h["rec_kind"], h["action"]) for h in hist}
    assert ("trim_day", "not_for_us") in kinds and ("cut_waste", "done") in kinds
    assert ("reprice", "measured") in kinds and ("ask:draft_campaign", "confirmed") in kinds
    measured = next(h for h in hist if h["action"] == "measured")
    assert measured["outcome"] == "improved" and measured["days_to_effect"] == 28
    with pytest.raises(ValueError):
        feedback.record(rid, "x", "x:1", "teleported", db_path=db_path)


def test_a_restaurants_own_record_names_what_worked_only_over_the_floor(db_path):
    # Updated (CA2 finding 9, CA1 A10/E4): "worked" used to need one
    # improvement and no floor; it now follows rec_learning.most_effective —
    # MIN_MEASURED_FOR_RATE clear results, success at least even — and the
    # line says "k of n".
    rid = _rid(db_path)
    for i in range(3):
        feedback.record(rid, "cut_waste", f"cut_waste:{i}", "measured", outcome="improved", days_to_effect=20, db_path=db_path)
    feedback.record(rid, "trim_day", "trim_day:Mon", "not_for_us", db_path=db_path)
    feedback.record(rid, "trim_day", "trim_day:Tue", "hidden", db_path=db_path)
    rec = memory.own_record(rid, db_path=db_path)
    assert rec["worked"] == [] and rec["ignored"] == ["trim_day"]
    assert rec["by_kind"]["cut_waste"]["success_rate"] is None           # 3 measured: below the floor
    assert rec["by_kind"]["cut_waste"]["improved"] == 3
    for i in range(3, 5):
        feedback.record(rid, "cut_waste", f"cut_waste:{i}", "measured", outcome="improved", days_to_effect=20, db_path=db_path)
    rec = memory.own_record(rid, db_path=db_path)
    assert rec["worked"] == ["cut_waste"]
    assert rec["by_kind"]["cut_waste"]["success_rate"] == 1.0 and rec["by_kind"]["cut_waste"]["median_days_to_improvement"] == 20
    lines = memory.lines(memory.restaurant_memory(rid, db_path=db_path))
    assert any("5 of 5 measured results improved" in l and "cut_waste" in l for l in lines)
    assert any("declined or hidden" in l and "trim_day" in l for l in lines)


def test_one_improvement_beside_four_worse_is_never_what_worked(db_path):
    """CA2 probe C: 1 improved and 4 worsened read "measurably improved
    things here: cut_waste" in Ask's prompt."""
    rid = _rid(db_path)
    feedback.record(rid, "cut_waste", "cut_waste:a", "measured", outcome="improved", db_path=db_path)
    for i in range(4):
        feedback.record(rid, "cut_waste", f"cut_waste:w{i}", "measured", outcome="worsened", db_path=db_path)
    rec = memory.own_record(rid, db_path=db_path)
    assert rec["worked"] == [] and rec["by_kind"]["cut_waste"]["success_rate"] == 0.2
    assert not any("cut_waste" in l for l in memory.lines({"record": rec}))


def test_busiest_days_and_seasonality_refuse_without_enough_history(db_path):
    rid = _rid(db_path, module_labor=1)
    assert memory.busiest_days(rid, db_path=db_path)["available"] is False
    assert memory.seasonality(rid, db_path=db_path)["available"] is False
    conn = get_conn(db_path)
    for i in range(56):
        d = date.today() - timedelta(days=i)
        sales = 6000 if d.weekday() in (4, 5) else 2500
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, d.isoformat(), "x", 30, 900, sales, 40))
    conn.commit(); conn.close()
    bd = memory.busiest_days(rid, db_path=db_path)
    assert bd["available"] and set(bd["busiest"]) == {"Friday", "Saturday"}


# ── levels 2/3: discovery needs evidence ─────────────────────────────────────

def _cohort(db_path, n, cat, seed):
    rng = random.Random(seed)
    rids = [_rid(db_path, f"{cat} {seed} {i}") for i in range(n)]
    for i, r in enumerate(rids):
        fast = i % 2 == 0
        f = {"response_24h_rate_30d": 0.8 if fast else 0.1,
             "avg_rating_delta": round((0.35 if fast else -0.05) + rng.uniform(-0.05, 0.05), 3),
             "avg_rating_30d": round(4.4 + rng.uniform(-0.2, 0.2), 2), "labor_pct_28d": round(30 + rng.uniform(-3, 3), 1),
             # Within the tightened 0.3 spread gate for shares (fix round #23);
             # 0.9 / 0.3 split the band 60 points wide.
             "reply_rate_30d": 0.75 if fast else 0.6, "schedule_adjust_rate": rng.choice([0.2, 0.7]),
             "labor_pct_sd_28d": round(rng.uniform(1, 4), 2)}
        _seed_features(db_path, r, THIS_WEEK, f)
    return rids


def test_discovery_writes_a_pattern_only_with_evidence_and_never_a_name(db_path):
    # Eight a side: an owner is shown a pattern only with at least
    # MIN_ORGS_PER_SIDE organisations on each side (Benchmarking audit #11).
    rids = _cohort(db_path, 16, "pizza", 1)
    # Read inside the owner-confirmed peer group, never the type
    # (Benchmarking re-audit #21): counter-service pizzerias.
    for r in rids:
        _confirm(db_path, r, "counter", "pizza")
    cohorts = {r: "pizza" for r in rids}
    out = patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=500)
    assert "sm:counter" in out["cohorts_tested"] and out["active"] >= 1
    mine = patterns.viewer_cohorts(models.get_restaurant(rids[0], db_path=db_path))
    assert mine["format"] == "sm:counter"
    act = [x for x in patterns.active(mine, db_path=db_path) if x["cohort"] != "platform"]
    keys = {p["hypothesis"] for p in act}
    assert "reply_fast_rating" in keys                              # the planted effect
    p = next(x for x in act if x["hypothesis"] == "reply_fast_rating")
    assert p["n_with"] == 8 and p["n_without"] == 8 and p["p_value"] <= 0.05 and p["q_value"] <= 0.10
    assert "Across 16 counter-service restaurants on Cavnar AI" in p["sentence"] and "higher" in p["sentence"]
    for x in act:
        privacy.assert_anonymous(x)
        assert not any(name in x["sentence"] for name in ("pizza 1 0", "Moat"))
    # the same kind is supported for a confidence score
    assert patterns.support_for("reply", cohort=mine, db_path=db_path)["hypothesis"] == "reply_fast_rating"
    # noise discovers nothing: shuffle the outcome and rerun
    conn = get_conn(db_path)
    rows = conn.execute("SELECT id, features_json FROM intel_features").fetchall()
    rng = random.Random(3)
    for r in rows:
        f = json.loads(r["features_json"]); f["avg_rating_delta"] = round(rng.uniform(-0.3, 0.3), 3)
        conn.execute("UPDATE intel_features SET features_json=? WHERE id=?", (json.dumps(f), r["id"]))
    conn.commit(); conn.close()
    out2 = patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=500)
    assert not any(x["hypothesis"] == "reply_fast_rating" and x["cohort"] == "sm:counter"
                   for x in patterns.active(mine, db_path=db_path))
    retired = [x for x in patterns.all_patterns(db_path=db_path) if x["hypothesis"] == "reply_fast_rating"]
    assert retired and retired[0]["status"] == "retired" and out2["retired"] >= 1


def test_benchmarks_place_a_restaurant_in_its_cohort_and_fall_back_to_platform(db_path):
    # Nine bars: the band a member sees leaves its own row out, and needs 8 others (NS4 M6).
    rids = _cohort(db_path, 9, "bar", 2)
    for r in rids:
        update_restaurant(r, {"category": "bar"}, db_path=db_path)     # a type the owner set (#8)
    cohorts = {r: "bar" for r in rids}
    lone = _rid(db_path, "Solo Steak")
    _seed_features(db_path, lone, THIS_WEEK, {"avg_rating_30d": 4.9, "labor_pct_28d": 22.0, "reply_rate_30d": 0.99})
    out = benchmarks.compute(db_path=db_path, cohorts={**cohorts, lone: "steakhouse"})
    assert out["written"] > 0
    b = benchmarks.benchmark(rids[0], "labor_pct_28d", cohort="bar", db_path=db_path)
    assert b["available"] and b["cohort"] == "bar" and b["n"] == 8 and b["p25"] <= b["p50"] <= b["p75"]
    assert b["cohort_label"] == "Bars on Cavnar AI" and b["as_of"]
    assert b["standing"] in ("top quarter", "above the middle", "below the middle", "bottom quarter")
    # a steakhouse alone compares platform-wide, and is told so — on a
    # behaviour metric only (Benchmarking audit #6): its rating, a format
    # metric, has no like-for-like peers and no all-types stand-in.
    s = benchmarks.benchmark(lone, "reply_rate_30d", cohort="steakhouse", db_path=db_path)
    assert s["available"] and s["cohort"] == "platform" and s["standing"] == "top quarter"
    r = benchmarks.benchmark(lone, "avg_rating_30d", cohort="steakhouse", db_path=db_path)
    assert r["available"] is False and "no like-for-like peers" in r["reason"]
    for row in benchmarks.cohort_table(db_path=db_path):
        privacy.assert_anonymous(row)
        assert row["n"] >= privacy.MIN_COHORT


def test_trends_need_six_weekly_points_over_a_cohort_at_the_floor(db_path):
    # Nine: a trend point needs the bands' floor — 8 restaurants from 5
    # owners (fix round #41; this pinned the old five).
    rids = [_rid(db_path, f"T{i}") for i in range(9)]
    cohorts = {r: "cafe" for r in rids}
    for w in range(7):
        week = features.iso_week(date.today() - timedelta(weeks=6 - w))
        for r in rids:
            _seed_features(db_path, r, week, {"avg_rating_30d": 4.0 + 0.05 * w, "labor_pct_28d": 30})
    ts = trends.platform_trends(cohorts=cohorts, db_path=db_path)
    cafe = next(t for t in ts if t["cohort"] == "cafe" and t["metric"] == "avg_rating_30d")
    assert cafe["weeks"] == 7 and cafe["slope_per_week"] > 0 and cafe["n_latest"] == 9
    flat = next(t for t in ts if t["cohort"] == "cafe" and t["metric"] == "labor_pct_28d")
    assert flat["slope_per_week"] == 0
    assert trends.emerging(cohorts=cohorts, db_path=db_path)[0]["metric"] == "avg_rating_30d"


# ── the confidence model ─────────────────────────────────────────────────────

def test_confidence_counts_only_measured_factors_and_is_deterministic(db_path):
    rid = _rid(db_path, "Quiet Cafe", module_labor=1)
    r = models.get_restaurant(rid, db_path=db_path)
    c0 = confidence.score(rid, "trim_day", restaurant=r, db_path=db_path)
    names = {f["name"] for f in c0["factors"]}
    assert "restaurant_history" not in names and "data_completeness" not in names       # unmeasured does not vote
    assert "recent_changes" in names and c0["band"] in ("low", "medium", "high")
    assert abs(sum(f["weight"] for f in c0["factors"]) - 1.0) < 1e-6
    # own record of this kind: three measured improvements lift it
    for i in range(3):
        feedback.record(rid, "trim_day", f"trim_day:{i}", "measured", outcome="improved", days_to_effect=14, db_path=db_path)
    _seed_features(db_path, rid, THIS_WEEK, {"labor_pct_28d": 31}, completeness=0.9)
    c1 = confidence.score(rid, "trim_day", restaurant=r, db_path=db_path)
    assert c1["score"] > c0["score"]
    assert next(f for f in c1["factors"] if f["name"] == "restaurant_history")["value"] > 0.6
    assert next(f for f in c1["factors"] if f["name"] == "data_completeness")["value"] == 0.9
    assert confidence.score(rid, "trim_day", restaurant=r, db_path=db_path) == c1                  # deterministic
    # recent operational change is a penalty, said in words
    outcomes.record(rid, "reprice", "reprice:now", "Prices changed", "food_cost_pct", db_path=db_path)
    c2 = confidence.score(rid, "trim_day", restaurant=r, db_path=db_path)
    rc = next(f for f in c2["factors"] if f["name"] == "recent_changes")
    assert rc["value"] < 1.0 and "prices changed" in rc["note"] and c2["score"] < c1["score"]
    # a low band carries a caution sentence
    low = [c for c in (c0, c1, c2) if c["band"] == "low"]
    for c in low:
        assert c["caution"].startswith("Low confidence")


def test_home_recommendations_carry_confidence_without_changing_their_shape(db_path, monkeypatch):
    import rec_trust
    import rec_learning
    rid = _rid(db_path, module_labor=1)
    ctx = rec_trust.Context(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    # One measured confidence per card (K1): the card's own evidence, this
    # restaurant's record of the kind, the freshness of its sources.
    c = home_brief.card_confidence(ctx, "trim_day:Monday", {"n": 4, "kind": "weekdays",
                                                            "basis": "4 Mondays in your shift data"})
    assert c["dimensions"]["evidence"]["pct"] == 100 and c["dimensions"]["accuracy"]["pct"] is None
    # No source dates it (group P: an unmeasured freshness holds it at 49,
    # below the no-record 70 — it never raises the figure).
    assert c["pct"] == 49 and c["band"] == "low" and c["label"] == "49% confidence"
    assert c["caps_applied"] == ["no_track_record", "freshness_unmeasured"]
    assert isinstance(c["score"], float) and c["caution"]
    monkeypatch.setattr(rec_learning, "kind_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    c = home_brief.card_confidence(rec_trust.Context(rid, db_path=db_path), "x:y", {"n": 9, "kind": "reviews"})
    # never fails the brief — and an unreadable record is low, never medium
    assert c["band"] == "low" and c["pct"] is None and c["score"] == 0.0


# ── jobs: bounded, resumable, and the learning pass end to end ───────────────

def test_the_feature_pass_resumes_from_its_cursor(db_path, monkeypatch):
    rids = [_rid(db_path, f"J{i}") for i in range(6)]
    calls = []

    def slow(rid, today=None, db_path=None):
        calls.append(rid)
        _seed_features(db_path, rid, THIS_WEEK, {"labor_pct_28d": 30})
        return {}
    monkeypatch.setattr(features, "compute_and_store", slow)
    # a wall clock of zero: the first batch runs, then the pass stops and remembers where
    out = jobs.run_features(db_path=db_path, wall_seconds=0, workers=2)
    assert out["complete"] is False and out["computed"] == 2 and jobs._cursor_get(db_path) == rids[1]
    calls.clear()
    out = jobs.run_features(db_path=db_path, wall_seconds=60, workers=2)
    assert out["complete"] is True and calls[0] == rids[2] and set(calls) == set(rids)    # resumed, then wrapped
    assert jobs._cursor_get(db_path) == 0


def test_the_learning_pass_runs_end_to_end_and_the_dashboard_is_anonymous(db_path):
    rids = _cohort(db_path, 10, "bar", 5)
    for r in rids:
        home_brief.dismiss(r, "trim_day:Monday", kind="done")
        _confirm(db_path, r, "bar_led", "bar")
    out = jobs.run_learning(db_path=db_path)
    assert out["restaurants"] == 10 and out["feedback"]["events"] == 10
    assert out["benchmarks"]["written"] > 0 and out["confidence_log"]["written"] >= 1
    d = dashboard.build(db_path=db_path)
    privacy.assert_anonymous(d)
    assert d["learning"]["restaurants"] == 10 and d["learning"]["cohorts"] == {"bar": 10}
    assert "bar" in d["learning"]["cohorts_at_floor"] and d["recommendations"]["totals"]["accepted"] == 10
    assert d["savings"]["note"].startswith("Four separate")
    # The bands are stored under the confirmed peer partition (#20).
    outsider = privacy.org_hash("r0")
    assert any((benchmarks.published("sm:bar_led", m, exclude_org=outsider, db_path=db_path) or {}).get("p50")
               is not None for m in features.BENCHMARK_KEYS)
    # The facade has no viewer to leave out, so it publishes no band (fix
    # round #41, R1-11 — this used to assert it did, with the viewer inside).
    facade = intelligence.industry_intelligence("sm:bar_led", db_path=db_path)
    assert not any(b for b in facade["benchmarks"] if b)
    assert out["peer_ledger"]["written"] == 10 * 3
    assert intelligence.recommendation_success("trim_day", cohort="bar", db_path=db_path)["available"] is True


def test_ask_has_the_two_read_tools_and_a_context_section(db_path):
    import ask_cavnar_tools, ask_cavnar
    names = {t["spec"]["name"] for t in ask_cavnar_tools.TOOLS}
    assert {"read_restaurant_memory", "read_platform_intelligence"} <= names
    rid = _rid(db_path, "Solo Bar")
    mem = next(t for t in ask_cavnar_tools.TOOLS if t["spec"]["name"] == "read_restaurant_memory")["fn"](rid)
    assert mem["busiest_days"]["available"] is False and mem["note"] == "This restaurant's own history only."
    plat = next(t for t in ask_cavnar_tools.TOOLS if t["spec"]["name"] == "read_platform_intelligence")["fn"](rid)
    # "Solo Bar" is only GUESSED to be a bar: no type is named and no group
    # is read (Benchmarking re-audit #20).
    assert plat["cohort"] is None and plat["inferred"] is True and plat["cohort_label"] == "All restaurants on Cavnar AI"
    assert all(b["available"] is False for b in plat["benchmarks"]) and plat["patterns"] == []
    privacy.assert_anonymous({"benchmarks": plat["benchmarks"], "patterns": plat["patterns"]})
    # With nothing to compare, the section says WHY per module (re-audit
    # R3-13); a guessed bar's labor figure is not quoted at all (#5, R3-8) —
    # only the all-restaurant prime cost target, said as one (NS4 H3/M7).
    ctx = ask_cavnar._intelligence_context(rid)
    assert "No fair comparison with other restaurants for labor yet" in ctx
    assert "bar-led concepts" not in ctx and "Prime cost % for restaurants in general" in ctx
