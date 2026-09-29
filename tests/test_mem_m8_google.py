"""Google user data never trains pooled learning (memory fix round 9/29/26,
workstream M8; public/privacy.html and Google's API Services User Data
Policy, Limited Use).

A restaurant whose reviews come — or ever came — through the owner's Google
Business Profile connection contributes no review-derived figure to any
pooled read: bands, patterns, trends and the cohort series (through
features.cross_restaurant_view), recommendation priors at every rung, the
confidence log, the admin totals, DNA norms and neighbour predictions. Its
own screens keep every figure. A restaurant whose reviews come from Places,
Yelp or a CSV still contributes. intelligence.provenance holds the rule and
the trace; these tests pin both.
"""
import json
import sys
from datetime import date, datetime, timedelta

import pytest

import models
import rec_ledger as rl
from models import Restaurant, create_restaurant, update_restaurant

import intelligence  # noqa: E402
from intelligence import (features, feedback, scoring, jobs, provenance, patterns, dna, predict,  # noqa: E402
                          benchmarks)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    auth.init_auth(db_path=db_path)
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()
    yield db_path
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()


THIS_WEEK = features.iso_week(date.today())


def _rid(db, name, google=False, **kw):
    kw.setdefault("created_at", (date.today() - timedelta(days=120)).isoformat() + "T00:00:00")
    kw.setdefault("hourly_rate", 18.0)
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.replace(' ', '').lower()}@x.test", **kw),
                            db_path=db)
    if google:
        _x(db, "UPDATE restaurants SET gmb_refresh_token='enc:token', gmb_location_id='locations/1' WHERE id=?",
           (rid,))
    return rid


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _seed(db, rid, week, f):
    _x(db, "INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,0.8)",
       (rid, week, json.dumps(f)))


REVIEWED = {"reviews_30d": 12, "avg_rating_30d": 4.4, "avg_rating_prior_60d": 4.2, "avg_rating_delta": 0.2,
            "reply_rate_30d": 0.9, "response_24h_rate_30d": 0.8, "labor_pct_28d": 30.0,
            "outcomes_improved_rate_90d": 0.6, "outcomes_improved_rate_90d_ex_reviews": 0.5,
            "recs_done_28d": 4, "recs_done_28d_ex_reviews": 1}


# ── the lists are the modules' own ──────────────────────────────────────────

def test_the_review_lists_are_every_review_figure_the_modules_define():
    import metrics
    from intelligence import metrics_registry as reg
    assert set(provenance.REVIEW_DNA_DIMS) == {d for d, v in dna.DIMENSIONS.items() if v.get("module") == "reviews"}
    assert set(provenance.REVIEW_METRICS) == {m for m, f in metrics.FAMILIES.items()
                                              if f in ("guest_rating", "reply_speed")}
    assert {k for k, v in reg.METRICS.items() if v["module"] == "reviews"} <= set(provenance.REVIEW_FEATURES)
    assert set(provenance.REVIEW_FREE_VARIANT) <= set(features.FEATURE_KEYS)


def test_every_feature_the_reviews_table_can_move_is_listed(db):
    """A restaurant with reviews and nothing else: every feature compute()
    measures from them must be a REVIEW_FEATURE — a new review-derived
    feature that is not listed would enter pooled learning unnoticed."""
    rid = _rid(db, "Only Reviews")
    now = datetime.utcnow()
    for k in range(12):
        d = (now - timedelta(days=1 + k * 6)).strftime("%Y-%m-%dT%H:%M:%S")
        _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, fetched_at, "
               "response_status, posted_at) VALUES (?, 'google', ?, 4, 't', ?, ?, 'posted', ?)",
           (rid, f"g{k}", d, d, d))
    f = features.compute(rid, db_path=db)
    always = {"features_version", "schedules_28d", "campaigns_28d", "posts_28d", "guest_list_size",
              "waste_log_regularity_8w"}
    moved = {k for k, v in f.items() if v not in (None, 0, 0.0) and k not in always}
    assert moved and moved <= set(provenance.REVIEW_FEATURES)


# ── who is Google-connected ─────────────────────────────────────────────────

def test_a_gbp_connection_or_any_gbp_review_marks_a_restaurant(db):
    token = _rid(db, "Token Co", google=True)
    named = _rid(db, "Named Co")
    places = _rid(db, "Places Co")
    _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_name, fetched_at) "
           "VALUES (?, 'google', 'accounts/1/locations/2/reviews/3', 5, 't', 'accounts/1/locations/2/reviews/3', "
           "datetime('now'))", (named,))
    _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at) "
           "VALUES (?, 'google', 'google_1700000000_https://x', 5, 't', datetime('now'))", (places,))
    revoked = _rid(db, "Revoked Co")
    _x(db, "UPDATE restaurants SET gmb_revoked_at=datetime('now') WHERE id=?", (revoked,))
    got = provenance.google_connected_ids(db_path=db)
    assert {token, named, revoked} <= got and places not in got


# ── features: bands, patterns, trends ───────────────────────────────────────

def test_the_pooled_view_withdraws_a_connected_restaurants_review_figures_and_keeps_its_own(db):
    conn_rid = _rid(db, "Connected Co", google=True)
    places_rid = _rid(db, "Places Only Co")
    for r in (conn_rid, places_rid):
        _seed(db, r, THIS_WEEK, REVIEWED)
    pooled = features.latest_by_restaurant(db_path=db)
    mine = pooled[conn_rid]["features"]
    assert all(mine[k] is None for k in provenance.REVIEW_FEATURES if k in REVIEWED)
    assert mine["labor_pct_28d"] == 30.0                         # not Google data: still pooled
    assert mine["outcomes_improved_rate_90d"] == 0.5 and mine["recs_done_28d"] == 1   # the review-free variants
    assert not any(k.endswith("_ex_reviews") for k in mine)
    assert pooled[places_rid]["features"]["avg_rating_30d"] == 4.4               # Places data: pooled
    for wk_rows in features.weekly_by_restaurant(db_path=db).values():
        assert wk_rows[conn_rid]["avg_rating_30d"] is None
    # the restaurant's own screens keep everything
    assert features.latest(conn_rid, db_path=db)["features"]["avg_rating_30d"] == 4.4
    # a caller that has not looked is treated as connected
    assert features.cross_restaurant_view(dict(REVIEWED))["avg_rating_30d"] is None
    # the peer ledger asks what a restaurant measured on its OWN side of a
    # comparison: the un-pooled read keeps it (the waste gate still applies)
    own = features.latest_by_restaurant(db_path=db, pooled=False)
    assert own[conn_rid]["features"]["avg_rating_30d"] == 4.4
    import inspect
    src = inspect.getsource(jobs.run_learning)
    assert "latest_by_restaurant(db_path=db_path, pooled=False)" in src and "latest=own_latest" in src


def test_a_review_pattern_is_never_found_on_connected_restaurants(db):
    rids = [_rid(db, f"Pattern Pie {i}", google=True) for i in range(16)]
    for i, r in enumerate(rids):
        fast = i % 2 == 0
        _seed(db, r, THIS_WEEK, {"response_24h_rate_30d": 0.8 if fast else 0.1,
                                 "avg_rating_delta": 0.35 if fast else -0.05, "labor_pct_28d": 30.0})
        update_restaurant(r, {"service_model": "counter", "concept": "pizza", "category": "pizza",
                              "profile_source": "set", "profile_confirmed_at": "2026-09-01T00:00:00"})
    patterns.discover(db_path=db, shuffles=300)
    assert not [p for p in patterns.all_patterns(db_path=db) if p["hypothesis"] == "reply_fast_rating"]


# ── recommendation priors, the confidence log, the admin totals ─────────────

def test_a_connected_restaurants_review_rows_never_reach_a_pooled_figure(db):
    conn_rid = _rid(db, "Linked Grill", google=True)
    rl.present(conn_rid, "top_issue:service", "reviews", "home", db_path=db)
    rl.record(conn_rid, "top_issue:service", "accepted", surface="home", db_path=db)
    rl.present(conn_rid, "trim_day:Monday", "labor", "home", db_path=db)
    rl.record(conn_rid, "trim_day:Monday", "accepted", surface="home", db_path=db)
    feedback.sync(db_path=db)
    rows = {feedback.base_key(r["source_key"]): r for r in
            _q(db, "SELECT source_key, review_derived, google_data FROM intel_rec_events WHERE restaurant_id=?",
               (conn_rid,))}
    assert rows["top_issue:service"]["google_data"] == 1 and rows["top_issue:service"]["review_derived"] == 1
    assert rows["trim_day:Monday"]["google_data"] == 0             # labor advice is not Google data
    assert scoring.kind_stats("top_issue", db_path=db)["answered"] == 0
    assert scoring.kind_stats("trim_day", db_path=db)["answered"] == 1
    assert scoring.kind_stats("top_issue", restaurant_id=conn_rid, db_path=db)["answered"] == 1   # its own record
    assert all(r["rec_kind"] != "top_issue" for r in scoring.rank_kinds(db_path=db))
    assert scoring.platform_totals(db_path=db)["answered"] == 1
    jobs.log_confidence(db_path=db)
    assert not _q(db, "SELECT 1 FROM intel_confidence_log WHERE rec_kind='top_issue'")


def test_connecting_google_later_flags_the_restaurants_existing_review_rows(db):
    rid = _rid(db, "Later Grill")
    rl.present(rid, "diag_review:service", "reviews", "home", db_path=db)
    rl.record(rid, "diag_review:service", "completed", surface="home", db_path=db)
    feedback.sync(db_path=db)
    assert _q(db, "SELECT google_data FROM intel_rec_events WHERE restaurant_id=?", (rid,))[0]["google_data"] == 0
    _x(db, "UPDATE restaurants SET gmb_refresh_token='enc:t' WHERE id=?", (rid,))
    feedback.sync(db_path=db)
    assert _q(db, "SELECT google_data FROM intel_rec_events WHERE restaurant_id=?", (rid,))[0]["google_data"] == 1


def test_a_result_on_a_review_metric_is_review_derived_whatever_its_kind():
    assert provenance.review_derived("observed:alert_neg_spike:2026-09", metric="complaints:service")
    assert provenance.review_derived("dsr_action:respond_reviews:reviews")
    assert provenance.review_derived("link:reviews_x_labor")
    assert not provenance.review_derived("trim_day:Monday#e" + "a" * 32, metric="labor_pct")
    assert not provenance.review_derived("dsr_action:control_hours:labor")


# ── DNA norms and predictions ───────────────────────────────────────────────

def test_dna_norms_leave_out_a_connected_restaurants_review_dimensions(db, monkeypatch):
    monkeypatch.setattr(dna, "MIN_ROBUST_N", 3)
    plain = [_rid(db, f"Norm Plain {i}") for i in range(3)]
    linked = [_rid(db, f"Norm Linked {i}", google=True) for i in range(3)]
    for r in plain + linked:
        val = 4.0 if r in plain else 2.0
        _x(db, "INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage, version) VALUES (?,?,?,0.5,?)",
           (r, THIS_WEEK, json.dumps({"rating_level": {"raw": val + 0.1 * (r % 3)},
                                      "labor_pct": {"raw": 30.0 + (r % 3)}}), dna.DNA_VERSION))
    norms = dna.platform_norms(db_path=db)
    assert norms["rating_level"]["kind"] == "robust" and norms["rating_level"]["n"] == 3
    assert norms["rating_level"]["centre"] >= 4.0                    # only the Places-era restaurants
    assert norms["labor_pct"]["n"] == 6                              # labor is not Google data


def test_a_review_metric_prediction_has_no_connected_neighbour(db):
    me = _rid(db, "Viewer Co")
    plain = [_rid(db, f"Near Plain {i}") for i in range(3)]
    linked = [_rid(db, f"Near Linked {i}", google=True) for i in range(3)]
    dims = {"volume_band": {"raw": 2}, "ticket_band": {"raw": 2}, "service_type": {"raw": "counter"},
            "weekend_share": {"raw": 0.4}, "open_hours": {"raw": 70}, "rating_level": {"raw": 4.3},
            "labor_pct": {"raw": 30.0}}
    dnas = {r: dims for r in [me] + plain + linked}
    review = {r for r, _d in predict.neighbours(me, metric="avg_rating", db_path=db, dnas=dnas)}
    labor = {r for r, _d in predict.neighbours(me, metric="labor_pct", db_path=db, dnas=dnas)}
    assert review and not (review & set(linked))
    assert set(linked) & labor                                     # labor results stay pooled


# ── the one-off purge ───────────────────────────────────────────────────────

def test_pooled_review_rows_from_before_the_rule_are_dropped_once(db):
    _x(db, "INSERT INTO intel_benchmarks (cohort, metric, week, n, p50) VALUES ('platform','avg_rating_30d',?,9,4.4)",
       (THIS_WEEK,))
    _x(db, "INSERT INTO intel_benchmarks (cohort, metric, week, n, p50) VALUES ('sm:counter','labor_pct_28d',?,9,30)",
       (THIS_WEEK,))
    _x(db, "INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, p_value, confidence, "
           "sentence) VALUES ('platform:reply_fast_rating','platform','reply_fast_rating',8,8,0.3,0.01,0.5,'s')")
    assert jobs.purge_google_pooled(db_path=db)["purged"] == 2
    assert _q(db, "SELECT metric FROM intel_benchmarks") == [{"metric": "labor_pct_28d"}]
    assert not _q(db, "SELECT 1 FROM intel_patterns")
    _x(db, "INSERT INTO intel_benchmarks (cohort, metric, week, n, p50) VALUES ('platform','avg_rating_30d',?,9,4.4)",
       (THIS_WEEK,))
    assert jobs.purge_google_pooled(db_path=db)["purged"] == 0           # once
