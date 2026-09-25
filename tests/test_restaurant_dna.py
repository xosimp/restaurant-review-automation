"""Restaurant DNA and learning (Benchmarking audit 9/24/26, workstream D):
the restaurant's own operational profile (intelligence/dna.py), the
similarity between two profiles, effect sizes in the learning table, staff
last-seen dates, the waste-logging gate, peer priors that are disclosed and
bounded, the borrowed-headcount Evidence cap, prospective patterns, bounded
discovery, the materialised comparisons, the peer-benchmark freshness
source and the dormant neighbour prediction (intelligence/predict.py).

Top-50 items #22, #24, #25, #26, #27, #32, #33, #45, #46 and the prediction
scaffolding."""
import json
import random
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant

import intelligence  # noqa: E402 — imported before any fixture patches get_conn
from intelligence import dna, predict, features, feedback, scoring, patterns, jobs, privacy, comparison_cache  # noqa: E402
from intelligence import engine as eng  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    for mod in (models, dna, predict, features, feedback, scoring, patterns, jobs, comparison_cache, eng, intelligence):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)


def _rid(db_path, name, **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.replace(' ', '').lower()}@x.test", **kw),
                             db_path=db_path)


def _sales(db_path, rid, days=70, base=3000.0, seed=1, weekend=1.6, hours_per_1k=10.0, end=None):
    rng = random.Random(seed)
    end = end or date.today() - timedelta(days=1)
    conn = get_conn(db_path)
    for k in range(days):
        d = end - timedelta(days=k)
        s = base * (weekend if d.weekday() >= 4 else 1.0) * (1 + rng.uniform(-0.1, 0.1))
        h = s / 1000.0 * hours_per_1k
        conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, sales, total_hours, "
                     "labor_pct) VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), round(s, 2),
                                                         round(h, 1), 28.0))
    conn.commit()
    conn.close()


def _dims(**z):
    """A DNA row from z values; `service_type` is categorical."""
    out = {}
    for k, v in z.items():
        out[k] = {"raw": v} if k == "service_type" else {"raw": v, "z": v}
    return out


# ── #24: the dimensions, never dollars, None never 0 ───────────────────────

def test_structural_weights_are_stated_and_sum_to_one():
    assert abs(sum(dna.STRUCTURAL_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(dna.STRUCTURAL_WEIGHTS) == set(dna.STRUCTURAL)
    assert abs(sum(dna.prediction_weights("labor_pct").values()) - 1.0) < 1e-9


def test_an_unmeasured_dimension_is_none_with_what_it_needs_never_zero(db_path):
    rid = _rid(db_path, "Blank Slate")
    row = dna.compute(rid, db_path=db_path, features={})
    for dim, e in row["dims"].items():
        assert e["raw"] is None and e["z"] is None, dim
        assert e["basis"].startswith("needs "), dim
    assert row["coverage"] == 0.0


def test_the_profile_is_ratios_and_bands_and_passes_the_privacy_check(db_path):
    rid = _rid(db_path, "Band Grill")
    _sales(db_path, rid, days=70, base=3500.0)
    row = dna.compute(rid, db_path=db_path, features={"labor_pct_28d": 29.5, "avg_rating_30d": 4.4,
                                                      "reviews_30d": 12})
    d = row["dims"]
    assert d["volume_band"]["raw"] == 2                    # 3,500–5,600/day sits in the $3k–$6k band
    assert 0.5 < d["weekend_share"]["raw"] < 0.6          # Fri–Sun at 1.6×
    assert d["sales_volatility"]["raw"] is not None and d["sales_volatility"]["raw"] < 0.15
    assert d["labor_flex"]["raw"] is not None and 0.8 < d["labor_flex"]["raw"] < 1.2   # hours track sales
    assert d["labor_pct"]["raw"] == 29.5 and d["labor_pct"]["z"] == round((29.5 - 30.0) / 3.0, 3)
    assert d["labor_pct"]["norm"]["kind"] == "anchor"
    # No dollar figure anywhere in the stored row — not the mean, not a day.
    blob = json.dumps(row)
    assert "3500" not in blob and "5600" not in blob
    privacy.assert_anonymous({"dims": {k: v for k, v in d.items() if k != "service_type"}})
    for k in d:
        assert not any(stem in k for stem in privacy.FORBIDDEN_KEY_STEMS), k


def test_service_type_counts_only_when_the_owner_set_it(db_path):
    guessed = _rid(db_path, "Tony's Pizzeria")
    assert dna.compute(guessed, db_path=db_path, features={})["dims"]["service_type"]["raw"] is None
    update_restaurant(guessed, {"category": "pizza"}, db_path=db_path)
    assert dna.compute(guessed, db_path=db_path, features={})["dims"]["service_type"]["raw"] == "pizza"


def test_z_uses_stated_anchors_below_thirty_and_the_robust_z_from_thirty(db_path):
    wk = features.iso_week(date.today())
    rids = [_rid(db_path, f"Norm {i}") for i in range(29)]
    conn = get_conn(db_path)
    for i, rid in enumerate(rids):
        conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage) VALUES (?,?,?,0.5)",
                     (rid, wk, json.dumps({"labor_pct": {"raw": 20.0 + i * 0.5}})))
    conn.commit()
    conn.close()
    assert dna.platform_norms(db_path=db_path)["labor_pct"]["kind"] == "anchor"
    rid = _rid(db_path, "Norm 30")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage) VALUES (?,?,?,0.5)",
                 (rid, wk, json.dumps({"labor_pct": {"raw": 35.0}})))
    conn.commit()
    conn.close()
    n = dna.platform_norms(db_path=db_path)["labor_pct"]
    assert n["kind"] == "robust" and n["n"] == 30 and 26.0 < n["centre"] < 28.0 and n["scale"] > 0
    z = dna.normalise({"labor_pct": {"raw": 40.0, "n": 20, "basis": "x"}}, {"labor_pct": n})["labor_pct"]["z"]
    assert z == dna.Z_CLIP or z < dna.Z_CLIP


# ── #24: similarity ─────────────────────────────────────────────────────────

def test_distance_is_missing_aware_and_refuses_thin_overlap():
    a = _dims(volume_band=1.0, weekend_share=0.5, daypart_mix=-0.5, service_type="pizza")
    b = _dims(volume_band=1.0, weekend_share=0.5, daypart_mix=-0.5, service_type="pizza")
    assert dna.distance(a, b) == 0.0
    c = _dims(volume_band=2.0, weekend_share=0.5, daypart_mix=-0.5, service_type="bar")
    d = dna.distance(a, c)
    # (0.30·1² + 0.15·2²) ÷ 0.80 over the four shared structural dimensions
    assert abs(d - ((0.30 * 1.0 + 0.15 * 4.0) / 0.80) ** 0.5) < 1e-3
    # One structural dimension missing: 0.65 of the weight is shared, but
    # only 3 structural dimensions — not comparable, and never "0 apart".
    thin = _dims(volume_band=1.0, weekend_share=0.5, service_type="pizza")
    det = dna.distance_detail(a, thin)
    assert det["comparable"] is False and det["d"] is None and "structural" in det["why_not"]
    assert dna.distance(a, {}) is None


# ── #24: the pass, the owner's read, the routes ─────────────────────────────

def test_the_nightly_features_pass_writes_the_dna_row_beside_the_features(db_path):
    rid = _rid(db_path, "Nightly Diner")
    _sales(db_path, rid, days=60)
    out = jobs.run_features(db_path=db_path, wall_seconds=60, workers=1)
    assert out["computed"] >= 1
    row = dna.latest(rid, db_path=db_path)
    assert row and row["week"] == features.iso_week(date.today()) and row["dims"]["volume_band"]["raw"] is not None


def test_the_profile_trends_against_four_weeks_ago_and_says_what_is_missing(db_path, monkeypatch):
    rid = _rid(db_path, "Profile Pub")
    conn = get_conn(db_path)
    now_wk, old_wk = features.iso_week(date.today()), features.iso_week(date.today() - timedelta(weeks=4))
    conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage) VALUES (?,?,?,0.1)",
                 (rid, old_wk, json.dumps({"labor_pct": {"raw": 33.0, "z": 1.0, "basis": "b"}})))
    conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage) VALUES (?,?,?,0.1)",
                 (rid, now_wk, json.dumps({"labor_pct": {"raw": 30.0, "z": 0.0, "basis": "b", "n": 20}})))
    conn.commit()
    conn.close()
    p = dna.profile(rid, db_path=db_path)
    items = {i["key"]: i for f in p["families"] for i in f["dimensions"]}
    lab = items["labor_pct"]
    assert lab["display"] == "30.0%" and lab["trend"]["previous"] == 33.0 and lab["trend"]["direction"] == "down"
    assert items["volume_band"]["measured"] is False and items["volume_band"]["needs"]
    assert all("z" not in i and "norm" not in i for i in items.values())      # figures, never a z-score
    assert isinstance(p["coverage_pct"], int)
    # No personality label: a profile is measured figures only.
    assert not any(k in p for k in ("personality", "archetype", "persona", "type_label"))
    # Projected by module permission: a login without labor view sees no labor dimensions.
    p2 = dna.profile(rid, db_path=db_path, modules={"reviews", "marketing"})
    keys = {i["key"] for f in p2["families"] for i in f["dimensions"]}
    assert "labor_pct" not in keys and "rating_level" in keys and "rec_uptake" in keys


def test_the_dna_routes_exist_on_web_and_mobile_with_one_body():
    import auth
    import client_api
    import mobile_api
    web, mob = open(client_api.__file__).read(), open(mobile_api.__file__).read()
    assert '@client_bp.route("/api/dna")' in web and '@mobile_bp.route("/dna")' in mob
    assert web.count("intelligence.dna_payload(current_user)") == 1
    assert mob.count("intelligence.dna_payload(current_user)") == 1
    assert "/api/dna" in auth._UNGATED_PREFIXES and "/mobile/api/dna" in auth._UNGATED_PREFIXES


def test_the_dna_payload_is_the_logins_own_restaurant(db_path, monkeypatch):
    import permissions
    rid = _rid(db_path, "Payload Place")
    monkeypatch.setattr(permissions, "has_permission",
                        lambda user, perm: perm != permissions.MODULE_VIEW_PERMISSIONS["labor"])
    assert dna.payload_for({"id": 1}, db_path=db_path)["ok"] is False
    out = dna.payload_for({"id": 1, "restaurant_id": rid, "role": "manager"}, db_path=db_path)
    assert out["ok"] and out["profile"]["available"] is False       # no row yet: says when it builds


# ── #26: last_seen ──────────────────────────────────────────────────────────

def test_remember_tenure_keeps_the_latest_date_and_never_moves_it_back(db_path):
    import schedule_intel
    rid = _rid(db_path, "Tenure Tap")
    schedule_intel.remember_tenure(rid, [{"employee": "Ana", "date": "2026-03-01"},
                                         {"employee": "Ana", "date": "2026-09-10"}], db_path=db_path)
    schedule_intel.remember_tenure(rid, [{"employee": "Ana", "date": "2026-05-01"}], db_path=db_path)
    conn = get_conn(db_path)
    r = conn.execute("SELECT first_seen, last_seen FROM staff_first_seen WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert r["first_seen"] == "2026-03-01" and r["last_seen"] == "2026-09-10"


def test_retention_stays_dormant_until_last_seen_fills_then_measures(db_path):
    today = date.today()
    staff = [{"first_seen": (today - timedelta(days=300)).isoformat(), "last_seen": None, "shifts_seen": 30,
              "updated_at": today.isoformat()} for _ in range(12)]
    assert dna._retention(staff, today)["raw"] is None
    for i, s in enumerate(staff):
        s["last_seen"] = (today - timedelta(days=100 if i < 3 else 2)).isoformat()
    r = dna._retention(staff, today)
    assert r["raw"] == 0.25 and r["n"] == 12


# ── #27: the waste-logging gate ─────────────────────────────────────────────

def test_waste_pct_enters_cross_restaurant_reads_only_when_waste_is_logged_regularly(db_path):
    wk = features.iso_week(date.today())
    regular, sparse, old = _rid(db_path, "Regular Logger"), _rid(db_path, "Sparse Logger"), _rid(db_path, "Old Row")
    conn = get_conn(db_path)
    for rid, reg in ((regular, 0.875), (sparse, 0.25), (old, None)):
        f = {"waste_sales_pct_28d": 0.4, "food_cost_pct_28d": 29.0}
        if reg is not None:
            f[features.WASTE_REGULARITY_KEY] = reg
        conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,0.5)",
                     (rid, wk, json.dumps(f)))
    conn.commit()
    conn.close()
    latest = features.latest_by_restaurant(db_path=db_path)
    assert latest[regular]["features"]["waste_sales_pct_28d"] == 0.4
    assert latest[sparse]["features"]["waste_sales_pct_28d"] is None           # withdrawn, never 0
    assert latest[old]["features"]["waste_sales_pct_28d"] is None              # regularity unmeasured
    assert latest[sparse]["features"]["food_cost_pct_28d"] == 29.0
    weekly = features.weekly_by_restaurant(weeks=2, db_path=db_path)[wk]
    assert weekly[sparse]["waste_sales_pct_28d"] is None and weekly[regular]["waste_sales_pct_28d"] == 0.4


def test_waste_regularity_counts_weeks_with_a_logged_waste_event(db_path):
    rid = _rid(db_path, "Waste Weeks")
    today = date.today()
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, created_at) VALUES (?,?,?)",
                 (rid, "Flour", (today - timedelta(days=90)).isoformat() + " 00:00:00"))
    ing = conn.execute("SELECT id FROM ingredients WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    for k in (0, 1, 3, 5):             # 4 of the last 8 seven-day windows
        conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                     "source) VALUES (?,?,?,?,?,?)", (rid, ing, "waste", 1, (today - timedelta(days=7 * k + 1)).isoformat(),
                                                      "manual"))
    conn.commit()
    share, hit = features.waste_log_regularity(conn, rid, today)
    conn.close()
    assert (share, hit) == (0.5, 4)
    row = dna.compute(rid, db_path=db_path, features={"waste_sales_pct_28d": 0.3,
                                                      features.WASTE_REGULARITY_KEY: 0.5})
    assert row["dims"]["waste_logging_regularity"]["raw"] == 0.5
    assert row["dims"]["waste_rate"]["raw"] is None and "regular" in row["dims"]["waste_rate"]["basis"]


# ── #25: effect sizes in learning ───────────────────────────────────────────

def _tracker(db_path, rid, key, metric, verdict, baseline, after, sigma=0.5, checkin=None, start="2026-06-01"):
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, baseline_value, "
        "started_on, evaluate_on, after_value, after_start, after_end, verdict, delta, delta_pct, status, "
        "noise_sigma, baseline_kind, owner_checkin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'evaluated',?,?,?)",
        (rid, "home", key, "t", metric, baseline, start, "2026-07-01", after, start, "2026-06-28", verdict,
         round(after - baseline, 2), round((after - baseline) / baseline * 100, 1), sigma, "prior window",
         json.dumps(checkin) if checkin else None))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def test_sync_stores_the_signed_effect_only_for_results_learning_counts(db_path):
    rid = _rid(db_path, "Effect Eatery")
    good = _tracker(db_path, rid, "trim_day:Monday", "labor_pct", "improved", 32.0, 30.0)
    disowned = _tracker(db_path, rid, "cut_waste:Flour", "labor_pct", "improved", 32.0, 29.0,
                        checkin={"did_it": "no"}, start="2026-08-01")
    feedback.sync(db_path=db_path)
    conn = get_conn(db_path)
    rows = {r["source_key"]: dict(r) for r in conn.execute(
        "SELECT source_key, outcome, metric, effect_pct, effect_z, baseline_kind, after_end, tags_json "
        "FROM intel_rec_events WHERE action='measured'").fetchall()}
    conn.close()
    g = rows[feedback.measured_key("trim_day:Monday", good)]
    # Labor % fell 2 points: better, so POSITIVE (−6.2% of 32 → +6.2; −2 ÷ 0.5 σ → +4).
    assert g["metric"] == "labor_pct" and g["effect_pct"] == 6.2 and g["effect_z"] == 4.0
    assert g["baseline_kind"] == "prior window" and g["after_end"] == "2026-06-28"
    assert "day:monday" in json.loads(g["tags_json"]) and "daytype:weekday" in json.loads(g["tags_json"])
    d = rows[feedback.measured_key("cut_waste:Flour", disowned)]
    assert d["outcome"] == "unknown" and d["effect_pct"] is None and d["metric"] is None


def test_effect_sign_follows_the_metrics_direction():
    up = feedback.effect_of({"metric": "avg_rating", "delta": 0.2, "baseline_value": 4.0, "noise_sigma": 0.1})
    assert up["effect_pct"] == 5.0 and up["effect_z"] == 2.0
    down = feedback.effect_of({"metric": "labor_pct", "delta": 1.0, "baseline_value": 30.0, "noise_sigma": 0.5})
    assert down["effect_pct"] < 0 and down["effect_z"] == -2.0
    assert feedback.effect_of({"metric": None}) is None


# ── #32: peer priors disclosed, bounded, recent ─────────────────────────────

def test_a_cohort_prior_that_lowers_the_percent_is_disclosed_in_the_basis():
    import confidence_engine as ce
    rec = {"measured": 8, "improved": 6, "source": "own", "prior_measured": 20, "prior_improved": 0,
           "prior_label": "Pizza on Cavnar", "base_rate": 0.3, "base_rate_source": "stated"}
    a = ce.accuracy(rec)
    assert a["prior"]["source"] == "cohort"
    assert "Pizza on Cavnar saw this rarely help (0 of 20), which lowers it" in a["basis"]
    # Peers who did well never lift it — and then nothing is said.
    b = ce.accuracy(dict(rec, prior_improved=20))
    assert b["prior"]["source"] == "do_nothing" and "rarely help" not in b["basis"]
    assert b["pct"] == ce.accuracy(dict(rec, prior_measured=0, prior_improved=0))["pct"]


def _event(db_path, rid, kind, key, outcome, at, cohort="pizza"):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, event_at) "
                 "VALUES (?,?,?,?, 'measured', ?, ?)", (rid, kind, key, cohort, outcome, at))
    conn.commit()
    conn.close()


def test_kind_stats_reads_a_365_day_window_with_a_half_life(db_path):
    rids = [_rid(db_path, f"Window {i}") for i in range(5)]
    now = datetime.utcnow()
    for i, r in enumerate(rids):
        _event(db_path, r, "trim_day", f"trim_day:a{i}", "improved", (now - timedelta(days=10)).strftime("%Y-%m-%d"))
        _event(db_path, r, "trim_day", f"trim_day:b{i}", "worsened", (now - timedelta(days=500)).strftime("%Y-%m-%d"))
    whole = scoring.kind_stats("trim_day", cohort="pizza", db_path=db_path)
    recent = scoring.kind_stats("trim_day", cohort="pizza", db_path=db_path, window_days=365, half_life_days=180)
    assert whole["measured"] == 10 and recent["measured"] == 5 and recent["improved"] == 5
    assert recent["success_rate_recent"] == 1.0 and 0 < recent["measured_recent"] < 5
    assert recent["window_days"] == 365


def test_a_kind_with_no_record_is_ranked_with_help_from_similar_restaurants_within_bounds(db_path):
    import rec_learning
    me = _rid(db_path, "Cold Start")
    update_restaurant(me, {"category": "pizza"}, db_path=db_path)
    peers = [_rid(db_path, f"Peer Pie {i}") for i in range(6)]
    at = (datetime.utcnow() - timedelta(days=20)).strftime("%Y-%m-%d")
    for i, r in enumerate(peers):
        _event(db_path, r, "cut_waste", f"cut_waste:x{i}", "improved", at)
        _event(db_path, r, "cut_waste", f"cut_waste:y{i}", "improved", at)
    m = rec_learning.effectiveness(me, db_path=db_path)
    w, why = m.weight("cut_waste:Flour")
    assert rec_learning.COLD_PRIOR_BOUNDS[0] <= w <= rec_learning.COLD_PRIOR_BOUNDS[1] and w > 1.0
    assert any("ranked with help from 6 similar restaurants' results" in y for y in why)
    # Peers that did badly lower it — never below the bound.
    for i, r in enumerate(peers):
        _event(db_path, r, "reprice", f"reprice:x{i}", "worsened", at)
        _event(db_path, r, "reprice", f"reprice:y{i}", "worsened", at)
    w2, _ = rec_learning.effectiveness(me, db_path=db_path).weight("reprice:Pasta")
    assert rec_learning.COLD_PRIOR_BOUNDS[0] <= w2 < 1.0
    # Below the floor (4 peers): exactly 1.0, nothing said.
    w3, why3 = rec_learning.effectiveness(me, db_path=db_path).weight("post_this_week:x")
    assert w3 == 1.0 and why3 == []
    assert rec_learning.PRIOR_WINDOW_DAYS == scoring.PRIOR_WINDOW_DAYS


# ── #33: borrowed headcount caps Evidence ───────────────────────────────────

def test_a_schedule_on_borrowed_headcount_caps_evidence_until_its_own_weeks_replace_it():
    import rec_trust
    start = {"available": True, "by_slot": [{"day": "Friday", "daypart": "night", "role": "Server", "people": 3},
                                            {"day": "Saturday", "daypart": "night", "role": "Server", "people": 4}]}
    slots = rec_trust.borrowed_slots(start, {})
    ev = rec_trust.schedule_evidence({"confidence": {"score": 90}, "borrowed_slots": slots})
    assert ev["cap"] == 49.0 and ev["cap_reason"] == "2 shifts use other restaurants' staffing, none of yours yet"
    # Half the slots now its own: the cap lifts, and is still under 100.
    own = {("Friday", "night"): {"Server": 3}, ("Monday", "night"): {"Server": 2}}
    slots2 = rec_trust.borrowed_slots(start, own)
    assert slots2 == {"borrowed": 1, "own": 2, "total": 3}
    ev2 = rec_trust.schedule_evidence({"confidence": {"score": 90}, "borrowed_slots": slots2})
    assert 49 < ev2["cap"] < 90 and "1 shift of 3" in ev2["cap_reason"]
    # Nothing borrowed: the read's own cap, unchanged.
    assert rec_trust.borrowed_slots({"available": False}, {}) is None
    assert rec_trust.schedule_evidence({"confidence": {"score": 90}})["cap"] == 90.0
    import inspect
    import schedule_engine
    assert 'quality["borrowed_slots"] = _rt_sq.borrowed_slots(' in inspect.getsource(
        schedule_engine._score_schedule_quality)


# ── #45: prospective patterns, stratified by type ───────────────────────────

def test_a_type_difference_is_not_a_prospective_effect():
    # Simpson's paradox: bars reply fast AND improve; cafes reply slowly and
    # stay flat — but within each type, replying changes nothing.
    rng = random.Random(4)
    pairs, types = {}, {}
    for i in range(24):
        bar = i < 12
        fast = (rng.random() < 0.8) if bar else (rng.random() < 0.2)
        gain = (0.3 if bar else 0.0) + rng.uniform(-0.05, 0.05)
        pairs[i] = {"t": {"response_24h_rate_30d": 0.9 if fast else 0.1, "avg_rating_30d": 4.0},
                    "t_h": {"avg_rating_30d": 4.0 + gain}}
        types[i] = "bar" if bar else "cafe"
    h = next(x for x in patterns.PROSPECTIVE_HYPOTHESES if x["key"] == "prospective_reply_fast_rating")
    pooled = patterns.test_prospective(pairs, h, lambda r: "all", shuffles=500)
    within = patterns.test_prospective(pairs, h, lambda r: types[r], shuffles=500)
    assert pooled is not None and pooled["p_value"] <= 0.05          # the pooled test is fooled
    assert within is None or within["p_value"] > 0.05                  # the stratified test is not


# A member a pattern may be read from (Benchmarking re-audit #21): live 17
# weeks, in an owner-confirmed peer group.
_LIVE = (date.today() - timedelta(days=120)).isoformat() + "T00:00:00"


def _member(db_path, name, service_model="counter", concept="pizza"):
    rid = _rid(db_path, name, created_at=_LIVE, hourly_rate=18.0)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET service_model=?, concept=?, category=?, profile_source='set' WHERE id=?",
                 (service_model, concept, concept, rid))
    conn.commit()
    conn.close()
    return rid


def test_discovery_stores_a_prospective_pattern_marked_as_such(db_path):
    rids = [_member(db_path, f"Prospect {i}") for i in range(12)]
    t = features.iso_week(date.today() - timedelta(weeks=14))
    t_h = patterns._week_plus(t, patterns.HORIZON_WEEKS)
    conn = get_conn(db_path)
    rng = random.Random(9)
    for i, r in enumerate(rids):
        fast = i % 2 == 0
        f0 = {"response_24h_rate_30d": 0.9 if fast else 0.1, "avg_rating_30d": 4.0}
        f1 = {"response_24h_rate_30d": 0.9 if fast else 0.1,
              "avg_rating_30d": round(4.0 + (0.4 if fast else 0.0) + rng.uniform(-0.05, 0.05), 3)}
        for wk, f in ((t, f0), (t_h, f1)):
            conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) "
                         "VALUES (?,?,?,0.5)", (r, wk, json.dumps(f)))
    conn.commit()
    conn.close()
    patterns.discover(db_path=db_path, cohorts={r: "pizza" for r in rids}, shuffles=400)
    got = [p for p in patterns.all_patterns(db_path=db_path) if p["hypothesis"] == "prospective_reply_fast_rating"]
    assert got, "the planted prospective effect was not found"
    by = {p["cohort"]: p for p in got}
    # Read inside the confirmed peer group (counter service), never the type.
    assert by["sm:counter"]["evidence"]["prospective"] is True
    assert by["sm:counter"]["evidence"]["pooled_types"] is False
    assert "following 13 weeks" in by["sm:counter"]["sentence"]
    if "platform" in by:
        assert by["platform"]["evidence"]["pooled_types"] is True
    for p in got:
        privacy.assert_anonymous(p)


# ── #46: bounds ─────────────────────────────────────────────────────────────

def test_discovery_is_bounded_resumable_and_retires_only_what_it_tested(db_path):
    wk = features.iso_week(date.today())
    # Two confirmed peer groups of six (Benchmarking re-audit #21: groups are
    # partitions, walked in name order after "platform").
    cohorts = {_member(db_path, f"Bound {sm} {i}", service_model=sm, concept=c): sm
               for sm, c in (("bar_led", None), ("counter", None)) for i in range(6)}
    conn = get_conn(db_path)
    for r in cohorts:
        conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) "
                     "VALUES (?,?,?,0.5)", (r, wk, json.dumps({"labor_pct_28d": 30})))
    conn.execute("INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, cohen_d, p_value, "
                 "q_value, confidence, sentence, status) VALUES ('sm:counter:h','sm:counter','h',5,5,0.2,0.5,0.01,"
                 "0.05,0.6,'Across 6 counter-service restaurants on Cavnar, x.','active')")
    conn.commit()
    conn.close()
    first = patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=50, wall_seconds=0)
    assert first["complete"] is False and first["cohorts_tested"] == ["platform"]
    # 'sm:counter' was never reached, so its pattern was not retired for failing.
    assert any(p["key"] == "sm:counter:h" for p in patterns.active(db_path=db_path, projection="admin",
                                                                   all_cohorts=True))
    second = patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=50, wall_seconds=0)
    assert second["cohorts_tested"] == ["sm:bar_led"] and second["resumed_after"] == "platform"
    third = patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=50, wall_seconds=0)
    assert third["cohorts_tested"] == ["sm:counter"]
    assert not any(p["key"] == "sm:counter:h" for p in patterns.active(db_path=db_path, projection="admin",
                                                                       all_cohorts=True))


def test_a_clearly_null_permutation_stops_early_and_a_real_one_runs_to_the_end():
    rng = random.Random(2)
    a = [rng.gauss(0, 1) for _ in range(10)]
    b = [rng.gauss(0, 1) for _ in range(10)]
    _obs, p, ran = patterns.permutation_test_sequential(a, b, shuffles=2000)
    assert ran < 2000 and p > 0.05
    _obs, p2, ran2 = patterns.permutation_test_sequential([x + 3 for x in a], b, shuffles=2000)
    from intelligence.stats import permutation_test
    assert ran2 == 2000 and p2 == permutation_test([x + 3 for x in a], b, shuffles=2000)[1]


def test_the_confidence_log_reads_only_the_last_365_days(db_path):
    rid = _rid(db_path, "Log Window")
    old = (date.today() - timedelta(days=400)).isoformat()
    for i in range(3):
        _event(db_path, rid, "trim_day", f"trim_day:o{i}", "improved", old)
    assert jobs.log_confidence(db_path=db_path, cohorts={rid: "pizza"})["written"] == 0
    import inspect
    assert "event_at >= ?" in inspect.getsource(jobs.log_confidence)


def test_comparisons_are_materialised_and_read_back_when_fresh(db_path):
    rid = _rid(db_path, "Cache Cafe")
    wk = features.iso_week(date.today())
    conn = get_conn(db_path)
    conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,0.5)",
                 (rid, wk, json.dumps({"labor_pct_28d": 29.0})))
    conn.commit()
    conn.close()
    out = comparison_cache.materialise(db_path=db_path, wall_seconds=60)
    assert out["restaurants"] >= 1 and out["complete"] is True
    hit = eng.compare(rid, "labor_pct_28d", kinds=comparison_cache.CACHED_KINDS, db_path=db_path, use_cache=True)
    assert hit.get("cached_at") and hit["metric"] == "labor_pct_28d"
    assert {c["kind"] for c in hit["comparisons"]} == set(comparison_cache.CACHED_KINDS)
    # The viewer-dependent location kind is never served from the cache.
    live = eng.compare(rid, "labor_pct_28d", db_path=db_path, use_cache=True)
    assert "cached_at" not in live
    # Stale rows are not served.
    conn = get_conn(db_path)
    conn.execute("UPDATE intel_benchmark_facts SET computed_at=datetime('now','-3 days')")
    conn.commit()
    conn.close()
    assert comparison_cache.read(rid, "labor_pct_28d", kinds=comparison_cache.CACHED_KINDS, db_path=db_path) is None


# ── #22: peer-benchmark freshness ───────────────────────────────────────────

def test_peer_benchmarks_are_a_data_freshness_source(db_path):
    import data_freshness as df
    import data_health as dh
    # One age limit with the engine: MAX_BAND_AGE_WEEKS (8) x 7 (re-audit #24;
    # it was 49 while the engine served bands to 56 days).
    assert df.SOURCES["cohort"]["horizon"] == 56 and df.SOURCES["cohort"]["label"] == "Peer benchmarks"
    assert "cohort" in df.TOOL_SOURCES["read_platform_intelligence"] and dh.OWNER_LABEL["cohort"]
    rid = _member(db_path, "Fresh Pie")
    r = models.get_restaurant(rid, db_path)
    assert df.source_state(r, "cohort", db_path=db_path)["state"] == "not_connected"
    conn = get_conn(db_path)
    ten = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    # A band stored under the TYPE is no band this restaurant is compared with.
    conn.execute("INSERT INTO intel_benchmarks (cohort, metric, week, n, computed_at) VALUES ('pizza','labor_pct_28d',"
                 "'2026-W30', 9, ?)", (ten,))
    conn.commit()
    assert df.source_state(r, "cohort", db_path=db_path)["state"] == "not_connected"
    # Its confirmed partition's band dates it (re-audit #24, R3-18).
    conn.execute("INSERT INTO intel_benchmarks (cohort, metric, week, n, computed_at) VALUES ('sm:counter',"
                 "'labor_pct_28d', '2026-W30', 9, ?)", (ten,))
    conn.commit()
    conn.close()
    s = df.source_state(r, "cohort", db_path=db_path)
    assert s["as_of_iso"] == ten[:10] and 0 < s["pct"] < 100 and "Peer bands computed" in s["basis"]


# ── prediction scaffolding (dormant) ────────────────────────────────────────

def _neighbourhood(db_path, n=7, same_org=False):
    wk = features.iso_week(date.today())
    me = _rid(db_path, "Predict Me")
    rids = [_rid(db_path, f"Predict Peer {i}") for i in range(n)]
    conn = get_conn(db_path)
    base = {"volume_band": {"raw": 2, "z": 0.0}, "weekend_share": {"raw": 0.5, "z": 0.5},
            "daypart_mix": {"raw": 0.3, "z": -0.5}, "service_type": {"raw": "pizza"},
            "waste_rate": {"raw": 2.0, "z": 0.0}}
    for i, r in enumerate([me] + rids):
        dims = json.loads(json.dumps(base))
        dims["weekend_share"]["z"] = 0.5 + 0.01 * i
        conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, coverage) VALUES (?,?,?,0.3)",
                     (r, wk, json.dumps(dims)))
        conn.execute("UPDATE restaurants SET organization_id=? WHERE id=?", (1 if (same_org and r != me) else 100 + i, r))
    conn.commit()
    conn.close()
    return me, rids


def test_prediction_is_unavailable_below_the_floors_and_names_why(db_path):
    me, _ = _neighbourhood(db_path, n=3)
    f = predict.predict_effect(me, "cut_waste", "weekly_waste", db_path=db_path)
    assert f["kind"] == "prediction" and f["available"] is False and "fewer than 5" in f["why_not"]
    assert predict.run_weekly(db_path=db_path)["written"] == 0


def test_prediction_excludes_the_viewers_organisation(db_path):
    me, rids = _neighbourhood(db_path, n=7, same_org=True)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET organization_id=1 WHERE id=?", (me,))
    conn.commit()
    conn.close()
    assert predict.neighbours(me, metric="weekly_waste", db_path=db_path) == []


def test_prediction_clears_its_floors_and_reports_only_counts_a_median_and_an_interval(db_path):
    me, rids = _neighbourhood(db_path, n=7)
    now = datetime.utcnow()
    at = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    for i, r in enumerate(rids):
        for j in range(2):
            conn.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, outcome, event_at, "
                         "metric, effect_pct, after_end) VALUES (?,?,?,?,?,?,?,?,?)",
                         (r, "cut_waste", f"cut_waste:{i}{j}#o{i}{j}", "measured", "improved", at, "weekly_waste",
                          10.0 + i + j, at))
        rec_id = f"rec{i}"
        conn.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, kind) VALUES (?,?,?,?)",
                     (rec_id, r, f"cut_waste:u{i}", "cut_waste"))
        conn.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                     "baseline_value, started_on, evaluate_on, after_value, after_start, after_end, verdict, delta, "
                     "delta_pct, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'evaluated')",
                     (r, "observed", f"observed:untaken:{rec_id}", "t", "weekly_waste", 100.0, at, at, 99.0, at, at,
                      "no_clear_change", -1.0, -1.0))
    conn.commit()
    conn.close()
    f = predict.predict_effect(me, "cut_waste", "weekly_waste", db_path=db_path)
    assert f["available"] is True and f["mixed"] is False, f
    # the nearest three quarters of the 7 candidates (NEIGHBOUR_DISTANCE_PCTL), one owner each
    assert f["n_restaurants"] == f["n_orgs"] >= predict.MIN_RESTAURANTS and f["value"] > 0
    lo, hi = f["interval"]
    assert lo <= f["value"] <= hi and lo > 0
    privacy.assert_anonymous(f)
    blob = json.dumps(f)
    assert "Predict Peer" not in blob and "restaurant_id" not in blob
