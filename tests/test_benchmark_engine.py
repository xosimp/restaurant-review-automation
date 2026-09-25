"""The Benchmark Engine (intelligence/engine.py, metrics_registry.py):
one vocabulary of comparisons, the rule that decides WHEN NOT to compare,
the comparison-strength %, the restaurant's own normal, the owner's other
locations, validation facts and the route twins. Benchmarking audit
9/24/26 (BM4 §4, BM2-3, BM3-7, BM4-10, BM4-11)."""
import json
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant
from intelligence import benchmarks as bm
from intelligence import engine as eng
from intelligence import features as feat
from intelligence import metrics_registry as reg


def _rid(db_path, **kw):
    kw.setdefault("name", f"Engine Cafe {kw.get('owner_email', '')}")
    kw.setdefault("owner_email", "e@x.test")
    # Live long enough to stand in a band (jobs.MIN_LIVE_WEEKS, audit #39).
    kw.setdefault("created_at", (date.today() - timedelta(days=120)).isoformat() + "T00:00:00")
    kw.setdefault("hourly_rate", 18.0)          # a real labor cost basis, not the $26 default (#14)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _week(weeks_ago=0):
    return feat.iso_week(date.today() - timedelta(weeks=weeks_ago))


def _feature(db_path, rid, week, **vals):
    conn = get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                 "VALUES (?,?,?,1.0)", (rid, week, json.dumps(vals)))
    conn.commit()
    conn.close()


# ── the metric registry: when NOT to compare ───────────────────────────────

def test_economics_metrics_never_compare_against_all_types():
    for m in ("labor_pct_28d", "labor_hours_per_1k_28d", "food_cost_pct_28d", "waste_sales_pct_28d",
              "staff_per_1k.servers"):
        assert reg.comparability(m) == reg.ECONOMICS and not reg.platform_allowed(m), m
    for m in ("reply_rate_30d", "response_24h_rate_30d", "outcomes_improved_rate_90d"):
        assert reg.platform_allowed(m), m
    assert reg.comparability("avg_rating_30d") == reg.FORMAT and not reg.platform_allowed("avg_rating_30d")
    assert set(feat.BENCHMARK_KEYS) <= set(reg.METRICS), "every benchmarked feature is registered"


# ── standing and strength ──────────────────────────────────────────────────

def test_a_gap_inside_the_bands_uncertainty_reads_about_the_middle():
    band = {"p25": 29.0, "p50": 31.0, "p75": 33.0}
    assert eng.standing(31.4, band, "labor_pct_28d", 9, own_noise=0.5)[0] == "about the middle"
    assert eng.standing(27.0, band, "labor_pct_28d", 9, own_noise=0.5)[0] == "top quarter"   # lower labor is better
    assert eng.standing(34.5, band, "labor_pct_28d", 9, own_noise=0.5)[0] == "bottom quarter"
    assert eng.standing(0.95, {"p25": 0.5, "p50": 0.6, "p75": 0.7}, "reply_rate_30d", 20,
                        own_noise=0.02)[0] == "top quarter"


def test_the_restaurants_own_swing_widens_the_margin_and_gates_quartile_words():
    """Re-audit #16 (R1-08, R3-10): the margin was the band median's
    uncertainty alone, so a restaurant whose labor % swings ±2 points was
    "bottom quarter" on a gap it produces by itself."""
    band = {"p25": 27.0, "p50": 29.5, "p75": 31.0}
    # R3-10's probe: 33.4 against 27/29.5/31 at n = 8 read "bottom quarter".
    assert eng.standing(33.4, band, "labor_pct_28d", 8, own_noise=0.4)[0] == "bottom quarter"
    st, margin = eng.standing(33.4, band, "labor_pct_28d", 8, own_noise=2.5)
    assert st == "below the middle" and margin > 2.5          # past p75, but not by the whole margin
    assert eng.standing(31.3, band, "labor_pct_28d", 8, own_noise=2.5)[0] == "about the middle"
    # No own history: the swing is unknown, so no quartile word can be shown to hold.
    assert eng.standing(40.0, band, "labor_pct_28d", 8)[0] == "below the middle"


def test_comparison_strength_is_a_percentage_with_named_caps():
    wk = _week(0)
    full = eng.strength(25, wk, "set")
    guessed = eng.strength(25, wk, "inferred")
    platform = eng.strength(25, wk, None, platform=True)
    old = eng.strength(25, _week(7), "set")
    # Never 100 (re-audit #15): no comparison of other restaurants is certain.
    assert full["pct"] < 100 and full["label"] == f"{full['pct']}% comparison strength"
    # A guessed type compares with no one: the cap is 0, not the old dead 74.
    assert guessed["pct"] == eng.INFERRED_TYPE_CAP == 0 and "inferred_type" in guessed["caps_applied"]
    assert platform["pct"] <= eng.PLATFORM_CAP and "all_types" in platform["caps_applied"]
    assert old["pct"] < full["pct"] and eng.strength(8, wk, "set")["pct"] < full["pct"]


def test_comparison_strength_is_capped_by_group_size_and_reads_spread_owners_and_granularity():
    """Re-audit #15 (R1-07, R3-6, R2-12, R4-11, R3-21): the minimum group of
    8 read 80% — above the 75% bar for ranking words — and 20 read 100%;
    spread, owner count and how finely the group is split played no part."""
    wk = _week(0)
    at_floor = eng.strength(8, wk, "set", orgs=8, spread=0.1, own_quality=1.0, similarity=0.95)
    assert at_floor["pct"] <= 45 and "group_size" in at_floor["caps_applied"] and at_floor["reason"].startswith("8 other")
    assert all(eng.strength(n, wk, "set", orgs=12, spread=0.0, own_quality=1.0, similarity=0.95)["pct"] < 75
               for n in range(8, 19))
    assert eng.strength(20, wk, "set", orgs=12, spread=0.0, own_quality=1.0, similarity=0.95)["pct"] >= 75
    assert eng.strength(200, wk, "set", orgs=200, spread=0.0, own_quality=1.0, similarity=1.0)["pct"] < 100
    tight = eng.strength(30, wk, "set", orgs=10, spread=0.1, own_quality=1.0, similarity=0.95)
    wide = eng.strength(30, wk, "set", orgs=10, spread=1.0, own_quality=1.0, similarity=0.95)
    few = eng.strength(30, wk, "set", orgs=5, spread=0.1, own_quality=1.0, similarity=0.95)
    assert wide["pct"] < tight["pct"] and few["pct"] < tight["pct"]
    assert {"spread", "orgs"} <= set(tight["dimensions"])
    # Similarity is how finely the group is split, and a coarser ladder rung is marked down.
    assert eng.granularity("sm:counter", "avg_rating_30d") < eng.granularity("sm:counter", "labor_pct_28d") \
        < eng.granularity("sm:counter|pizza", "food_cost_pct_28d") < 1.0
    assert eng.granularity("sm:counter", "labor_pct_28d", level=1) < eng.granularity("sm:counter", "labor_pct_28d")
    assert eng.granularity("platform") == 0.6
    # The own dimension is THIS metric's data: a rating on 5 reviews is thin
    # even when every other feature is measured.
    rows = [{"week": _week(0), "completeness": 1.0, "features": {"avg_rating_30d": 4.2, "reviews_30d": 5}}]
    assert eng.own_quality("avg_rating_30d", rows) <= 0.5
    rows = [{"week": _week(0), "completeness": 0.1, "features": {"avg_rating_30d": 4.2, "reviews_30d": 40}}]
    assert eng.own_quality("avg_rating_30d", rows) == 1.0


# ── self: the restaurant's own normal ──────────────────────────────────────

def test_self_compares_this_restaurant_to_its_own_normal(db_path):
    rid = _rid(db_path)
    for back in range(1, 18):
        _feature(db_path, rid, _week(back), labor_pct_28d=30.0 + (back % 3) * 0.3)
    _feature(db_path, rid, _week(0), labor_pct_28d=35.0)
    out = eng.compare(rid, "labor_pct_28d", kinds=("self",), db_path=db_path)
    s = out["comparisons"][0]
    assert s["available"] and s["verdict"] == "worse than your normal" and s["delta"] > s["noise_band"]
    assert out["headline"]["kind"] == "self" and "worse than your normal" in out["headline"]["text"]
    _feature(db_path, rid, _week(0), labor_pct_28d=30.3)
    assert eng.compare(rid, "labor_pct_28d", kinds=("self",), db_path=db_path)["comparisons"][0]["verdict"] == \
        "about your normal"


def test_self_needs_enough_of_its_own_history(db_path):
    rid = _rid(db_path)
    _feature(db_path, rid, _week(0), labor_pct_28d=31.0)
    s = eng.compare(rid, "labor_pct_28d", kinds=("self",), db_path=db_path)["comparisons"][0]
    assert not s["available"] and "weeks of this restaurant's own history" in s["why_not"]


# ── peers and platform ─────────────────────────────────────────────────────

def _set_type(db_path, rid, category, service_model="counter"):
    """An owner-confirmed profile (audit #7): the peer partition is built
    from it, never from a guessed type."""
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET category=?, concept=?, service_model=?, profile_source='set', "
                 "profile_confirmed_at=datetime('now') WHERE id=?", (category, category, service_model, rid))
    conn.commit()
    conn.close()


def _cohort(db_path, n, category="pizza", **vals):
    ids = []
    for i in range(n):
        rid = _rid(db_path, name=f"Peer {i}", owner_email=f"p{i}{category}@x.test")
        _set_type(db_path, rid, category)
        _feature(db_path, rid, _week(0), **{k: v + i * (0.02 if v < 1 else 0.2) for k, v in vals.items()})
        ids.append(rid)
    bm.compute(db_path=db_path)          # each member's confirmed partition
    return ids


def test_labor_is_never_compared_to_an_all_types_band_even_when_one_exists(db_path):
    ids = _cohort(db_path, 12, "pizza", labor_pct_28d=28.0, reply_rate_30d=0.6)
    viewer = _rid(db_path, name="Corner Spot", owner_email="t@x.test")
    _set_type(db_path, viewer, "mexican", service_model="full_service")
    _feature(db_path, viewer, _week(0), labor_pct_28d=33.0, reply_rate_30d=0.9)
    conn = get_conn(db_path)
    conn.execute("DELETE FROM intel_benchmarks")     # recompute this week with the viewer in it
    conn.commit()
    conn.close()
    bm.compute(db_path=db_path)
    labor = {c["kind"]: c for c in eng.compare(viewer, "labor_pct_28d", db_path=db_path)["comparisons"]}
    assert not labor["platform"]["available"] and "all-types comparison would mislead" in labor["platform"]["why_not"]
    assert not labor["peers"]["available"]                  # one mexican restaurant: no like-for-like group
    reply = {c["kind"]: c for c in eng.compare(viewer, "reply_rate_30d", db_path=db_path)["comparisons"]}
    assert reply["platform"]["available"] and reply["platform"]["cohort_label"].endswith("all types")
    assert reply["platform"]["strength"]["pct"] <= eng.PLATFORM_CAP


def test_peers_band_carries_standing_strength_and_binds_facts(db_path):
    ids = _cohort(db_path, 12, "pizza", labor_pct_28d=28.0)
    viewer = ids[0]
    out = eng.compare(viewer, "labor_pct_28d", db_path=db_path)
    peers = next(c for c in out["comparisons"] if c["kind"] == "peers")
    assert peers["available"] and peers["n"] >= bm.MIN_QUARTILE_N and peers["type_source"] == "set"
    assert peers["standing"] in ("top quarter", "above the middle", "about the middle")
    assert out["headline"]["kind"] == "peers" and out["headline"]["text"].startswith(f"Compared to {peers['n']} other")
    fs = [f for f in out["facts"] if f["source"].get("engine_kind") == "peers"]
    assert {f["key"].rsplit(".", 1)[1] for f in fs} == {"p25", "p50", "p75"}
    assert all(f["kind"] == "benchmark" and f["source"]["n"] == peers["n"] and f["source"]["min_n"] == 8 for f in fs)
    lines = eng.prompt_lines([out])
    assert lines and f"{peers['n']} other" in lines[0] and "comparison strength" in lines[0]


def test_no_fair_comparison_is_said_and_never_a_number(db_path):
    rid = _rid(db_path)
    _feature(db_path, rid, _week(0), food_cost_pct_28d=31.0)
    out = eng.compare(rid, "food_cost_pct_28d", kinds=("self", "peers", "platform"), db_path=db_path)
    assert out["headline"]["kind"] is None and out["headline"]["text"].startswith("No fair comparison")
    assert eng.prompt_lines([out])[0].startswith("Food cost %: no fair comparison")
    assert out["facts"] == []


# ── location: the owner's other locations ──────────────────────────────────

def test_location_ranks_this_location_among_the_owners_others(db_path):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO organizations (name, owner_email) VALUES ('Group', 'g@x.test')")
    org = cur.lastrowid
    conn.commit()
    conn.close()
    a = _rid(db_path, name="North", owner_email="n@x.test")
    b = _rid(db_path, name="South", owner_email="s@x.test")
    for rid, v in ((a, 29.0), (b, 33.0)):
        conn = get_conn(db_path)
        conn.execute("UPDATE restaurants SET organization_id=? WHERE id=?", (org, rid))
        conn.commit()
        conn.close()
        _feature(db_path, rid, _week(0), labor_pct_28d=v)
        _set_type(db_path, rid, "italian", service_model="full_service")
    admin = {"id": 1, "restaurant_id": b, "is_admin": True}
    loc = eng.compare(b, "labor_pct_28d", kinds=("location",), db_path=db_path, viewer=admin)["comparisons"][0]
    assert loc["available"] and loc["rank"] == 2 and loc["of"] == 2
    denied = eng.compare(b, "labor_pct_28d", kinds=("location",), db_path=db_path,
                         viewer={"id": 9, "role": "staff", "restaurant_id": b})["comparisons"][0]
    assert not denied["available"]


# ── the route body ─────────────────────────────────────────────────────────

def test_payload_is_projected_by_module_permission(db_path, monkeypatch):
    import permissions
    rid = _rid(db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(permissions, "has_permission",
                        lambda user, perm: perm != permissions.MODULE_VIEW_PERMISSIONS["inventory"])
    user = {"id": 1, "restaurant_id": rid, "role": "manager"}
    denied = eng.payload_for(user, module="food_cost", db_path=db_path)
    assert denied["ok"] is False
    allowed = eng.payload_for(user, module="labor", db_path=db_path)
    assert allowed["ok"] and {c["metric"] for c in allowed["comparisons"]} == set(reg.metrics_for("labor"))
    assert all("facts" not in c for c in allowed["comparisons"])
    everything = eng.payload_for(user, db_path=db_path)
    assert not any(c["module"] == "inventory" for c in everything["comparisons"])


def test_the_benchmark_routes_exist_on_web_and_mobile():
    import auth
    import client_api
    import mobile_api
    web, mob = open(client_api.__file__).read(), open(mobile_api.__file__).read()
    assert '@client_bp.route("/api/benchmarks")' in web and '@mobile_bp.route("/benchmarks")' in mob
    assert web.count("engine.payload_for(current_user") == 1 and mob.count("engine.payload_for(current_user") == 1
    assert "/api/benchmarks" in auth._UNGATED_PREFIXES and "/mobile/api/benchmarks" in auth._UNGATED_PREFIXES


def test_a_module_alias_is_checked_as_the_permission_it_names(db_path, monkeypatch):
    """payload_for(module="food_cost") compared the raw alias against the
    login's permitted modules, so every non-admin login was refused its own
    food cost comparisons (found by workstream O)."""
    import permissions
    rid = _rid(db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(permissions, "has_permission", lambda user, perm: True)
    user = {"id": 1, "restaurant_id": rid, "role": "manager"}
    for alias in ("food_cost", "food", "inventory"):
        out = eng.payload_for(user, module=alias, db_path=db_path)
        assert out["ok"], alias
        assert {c["metric"] for c in out["comparisons"]} == set(reg.metrics_for("inventory"))
    monkeypatch.setattr(permissions, "has_permission",
                        lambda user, perm: perm != permissions.MODULE_VIEW_PERMISSIONS["inventory"])
    assert eng.payload_for(user, module="food", db_path=db_path)["ok"] is False


# ── re-audit round (9/24/26), workstream A ─────────────────────────────────

def _weekly_rows(values_by_back, metric="labor_pct_28d", extra=None):
    """In-memory feature rows, oldest first: {weeks back: value}."""
    out = []
    for back in sorted(values_by_back, reverse=True):
        f = {metric: values_by_back[back]}
        f.update(extra or {})
        out.append({"week": _week(back), "features": f, "completeness": 1.0})
    return out


def _steady_rows(rng, backs, shift=0.0):
    """A restaurant whose operation never changes: daily labor % ~ N(30, 4),
    each weekly row the trailing 28-day figure (R2-1's simulation)."""
    top = max(backs)
    days = [rng.gauss(30.0, 4.0) for _ in range(28 + 7 * top)]
    n = len(days)
    vals = {}
    for b in backs:
        end = n - 7 * b
        vals[b] = sum(days[end - 28:end]) / 28.0 + (shift if b == 0 else 0.0)
    return _weekly_rows(vals)


def test_your_normal_calls_an_unchanged_restaurant_better_or_worse_in_at_most_one_week_in_ten():
    """Re-audit #1 (R2-1, R3-11, R4-9): the ±1-MAD band over overlapping
    28-day rows called 45% (13 points) to 54% (6 points) of an unchanged
    restaurant's weeks "better" or "worse than your normal", each "worse"
    with an amber tone and an "Ask what to change" button."""
    import random
    rng = random.Random(20260924)
    full = [0] + list(range(4, 17))                          # 13 baseline weeks
    thin = [0] + list(range(4, 4 + eng.SELF_MIN_POINTS))     # the minimum history
    for backs in (full, thin):
        called = total = 0
        for _ in range(1200):
            s = eng._self(1, "labor_pct_28d", _steady_rows(rng, backs))
            assert s["available"], s
            total += 1
            called += s["verdict"] != "about your normal"
        assert called / total <= 0.10, (len(backs), called / total)
    # …and a real change (+4 points of labor, ~5 sigma of a 28-day figure) is still seen.
    seen = sum(eng._self(1, "labor_pct_28d", _steady_rows(rng, full, shift=4.0))["verdict"] ==
               "worse than your normal" for _ in range(300))
    assert seen / 300 >= 0.8


def test_your_normal_is_read_from_non_overlapping_windows_and_needs_enough_of_them():
    rows = _weekly_rows({b: 30.0 + (b % 3) * 0.3 for b in range(4, 4 + eng.SELF_MIN_POINTS - 1)} | {0: 30.1})
    s = eng._self(1, "labor_pct_28d", rows)
    assert not s["available"] and "weeks of this restaurant's own history" in s["why_not"]
    sigma, df = eng.window_sigma([(4, 1.0), (5, 5.0), (8, 1.0), (9, 5.0)])
    assert sigma == 0.0 and df == 2          # rows a window apart agree; neighbours never compared


def test_your_normal_allows_for_the_season_once_a_year_of_history_exists():
    """Re-audit #39 (R2-15): a July bump read "worse than your normal"
    against April–June every year."""
    def val(b):
        season = 3.0 if (b <= 3 or 52 <= b <= 55) else 0.0
        return 30.0 + season + (0.1 if b % 2 else -0.1)
    recent = _weekly_rows({b: val(b) for b in range(0, 20)})
    s = eng._self(1, "labor_pct_28d", recent)
    assert s["verdict"] == "worse than your normal" and s["basis"] == "recent"
    year = _weekly_rows({b: val(b) for b in range(0, 70)})
    s = eng._self(1, "labor_pct_28d", year)
    assert s["basis"] == "seasonal" and s["verdict"] == "about your normal"
    assert "this time last year" in s["baseline_label"] and s["last_year"] == round(val(52), 3)
    assert "this time last year" in eng.headline_text(reg.meta("labor_pct_28d"), s)
    assert eng.SERIES_WEEKS >= 52 + eng.SELF_GAP_WEEKS + eng.SELF_BASELINE_WEEKS


def test_a_labor_cost_on_the_default_wage_is_never_ranked(db_path):
    """Re-audit #11 (R2-5, R3-20, R4-8): band MEMBERS on the $26/hr default
    are left out of labor-cost bands, but the viewer's own assumed-wage
    labor % was ranked against them."""
    ids = _cohort(db_path, 12, "pizza", labor_pct_28d=28.0, labor_hours_per_1k_28d=20.0)
    viewer = ids[0]
    ok = eng.compare(viewer, "labor_pct_28d", db_path=db_path)
    assert next(c for c in ok["comparisons"] if c["kind"] == "peers")["available"]
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET hourly_rate=26.0, role_rates_json=NULL WHERE id=?", (viewer,))
    conn.commit()
    conn.close()
    out = eng.compare(viewer, "labor_pct_28d", db_path=db_path, viewer={"id": 1, "is_admin": True})
    by = {c["kind"]: c for c in out["comparisons"]}
    for k in ("peers", "platform", "location"):
        assert not by[k]["available"] and "set your pay rates" in by[k]["why_not"], k
    assert out["own"]["eligible"] is False and "set your pay rates" in out["headline"]["text"]
    assert not any(f["source"].get("engine_kind") == "peers" for f in out["facts"])
    line = eng.prompt_lines([out])[0]
    assert "set your pay rates" in line and "quarter" not in line
    # Hours per $1k does not rest on a wage: still compared.
    hrs = eng.compare(viewer, "labor_hours_per_1k_28d", db_path=db_path)
    assert next(c for c in hrs["comparisons"] if c["kind"] == "peers")["available"]
    assert eng.own_eligible({"hourly_rate": 26.0}, "labor_pct_28d")[0] is False


def test_one_priced_role_is_not_a_sourced_labor_cost(monkeypatch):
    """R2-5: labor_cost_basis said "role_rates" when ANY role had a rate, so
    a mostly-$26 labor % counted as the owner's."""
    one_role = {"id": 1, "role_rates_json": '{"Manager": 24}', "hourly_rate": None}
    monkeypatch.setattr(eng, "labor_rate_coverage", lambda r: 0.3)
    ok, why = eng.labor_cost_sourced(one_role)
    assert not ok and "30% of the hours" in why
    monkeypatch.setattr(eng, "labor_rate_coverage", lambda r: None)
    assert eng.labor_cost_sourced(one_role)[0] is False
    monkeypatch.setattr(eng, "labor_rate_coverage", lambda r: 0.85)
    assert eng.labor_cost_sourced(one_role) == (True, None)
    # An owner-set flat rate prices every unmatched role: sourced without shifts.
    monkeypatch.setattr(eng, "labor_rate_coverage", lambda r: 0.0)
    assert eng.labor_cost_sourced(dict(one_role, hourly_rate=18.0)) == (True, None)
    assert eng.labor_cost_sourced({"id": 1, "role_rates_json": '{"Server": 12, "_default": 17}'}) == (True, None)


def test_an_irregularly_logged_waste_figure_is_not_ranked_or_called_against_its_normal(db_path):
    """Re-audit #12 (R4-7): band members must log waste in 6 of 8 weeks; the
    viewer's own near-0% from two logged weeks was "top quarter" and "better
    than your normal"."""
    rid = _rid(db_path)
    for back in range(1, 18):
        _feature(db_path, rid, _week(back), waste_sales_pct_28d=3.0 + (back % 3) * 0.2, waste_log_regularity_8w=1.0)
    _feature(db_path, rid, _week(0), waste_sales_pct_28d=0.2, waste_log_regularity_8w=0.25)
    out = eng.compare(rid, "waste_sales_pct_28d", kinds=("self", "peers"), db_path=db_path)
    by = {c["kind"]: c for c in out["comparisons"]}
    assert not by["self"]["available"] and "2 of 8 weeks" in by["self"]["why_not"]
    assert out["own"]["eligible"] is False
    assert eng.own_eligible(None, "waste_sales_pct_28d", {"waste_sales_pct_28d": 1.0,
                                                           "waste_log_regularity_8w": 0.875}) == (True, None)
    _feature(db_path, rid, _week(0), waste_sales_pct_28d=3.1, waste_log_regularity_8w=0.875)
    s = eng.compare(rid, "waste_sales_pct_28d", kinds=("self",), db_path=db_path)["comparisons"][0]
    assert s["available"] and s["verdict"] == "about your normal"


def test_prompt_lines_carry_the_own_value_the_better_direction_and_outcome_words(db_path):
    """Re-audit #17 (R3-7, R3-23): the Ask context said "bottom quarter"
    with no own figure and no direction — on a lower-is-better metric that
    is the HIGHEST labor %, and a model praised it."""
    ids = _cohort(db_path, 12, "pizza", labor_pct_28d=28.0)
    viewer = _rid(db_path, name="High Labor", owner_email="hl@x.test")
    _set_type(db_path, viewer, "pizza")
    for back in range(4, 17):
        _feature(db_path, viewer, _week(back), labor_pct_28d=34.0 + (back % 3) * 0.1)
    _feature(db_path, viewer, _week(0), labor_pct_28d=34.0)
    out = eng.compare(viewer, "labor_pct_28d", kinds=("peers",), db_path=db_path)
    peers = out["comparisons"][0]
    assert peers["available"] and peers["standing"] == "bottom quarter"
    line = eng.prompt_lines([out])[0]
    assert line.startswith("Labor %: this restaurant 34% (lower is better);")
    assert "worse than 3 in 4 of the group (higher labor % than 3 in 4)" in line
    assert "bottom quarter" not in line and "top quarter" not in line
    assert eng.outcome_words("top quarter", "labor_pct_28d").endswith("(lower labor % than 3 in 4)")
    assert eng.outcome_words("top quarter", "avg_rating_30d").endswith("(higher average rating (30 days) than 3 in 4)")
    # The fact contract (engine.facts): every benchmark fact says which
    # metric, which way is better, the restaurant's own value and standing.
    fs = [f for f in out["facts"] if f["source"].get("engine_kind") == "peers"]
    assert fs
    for f in fs:
        s = f["source"]
        assert s["metric"] == "labor_pct_28d" and s["better"] == "lower" and s["own_value"] == 34.0
        assert s["comparable"] is True and s["definition_note"] is None and s["strength_pct"] is not None
        assert s["standing"] == "bottom quarter" and s["inferred"] is False
    own = next(f for f in out["facts"] if f["key"] == "bench.labor_pct_28d.own")
    assert own["kind"] == "computed" and own["value"] == 34.0


def test_industry_facts_carry_the_engines_comparability():
    entry = {"metric": "labor_pct", "category": "pizza", "label": "Pizza", "low": 25.0, "high": 35.0,
             "median": 30.0, "unit": "%", "short": "NRA 2025", "year": 2025, "source_kind": "published"}
    cm = {"metric": "labor_pct_28d", "unit": "%", "better": "lower", "own": {"value": 31.0, "eligible": True},
          "comparisons": [{"kind": "industry", "available": True, "comparable": False,
                           "definition_note": "includes benefits"}]}
    fs = [f for f in eng.facts([cm], _entries={"labor_pct_28d": entry})
          if f["source"].get("engine_kind") == "industry"]
    assert fs and all(f["source"]["comparable"] is False and f["source"]["definition_note"] == "includes benefits"
                      and f["source"]["own_value"] is None and f["source"]["metric"] == "labor_pct_28d"
                      for f in fs)
    line = eng.prompt_lines([dict(cm, label="Labor %", comparisons=[dict(cm["comparisons"][0], line="NRA")])])[0]
    assert "context only, measured differently" in line


def test_measured_and_members_count_after_the_viewers_organisation_is_taken_out(db_path):
    """Re-audit #44 (R1-15): "compared to 9 other … measured at 12 of 14" —
    the counts included the viewer's organisation while n did not."""
    conn = get_conn(db_path)
    org = conn.execute("INSERT INTO organizations (name, owner_email) VALUES ('Two', 'two@x.test')").lastrowid
    conn.commit()
    conn.close()
    ids = []
    for i in range(12):
        rid = _rid(db_path, name=f"Peer {i}", owner_email=f"q{i}@x.test")
        _set_type(db_path, rid, "pizza")
        _feature(db_path, rid, _week(0), labor_pct_28d=28.0 + i * 0.2)
        ids.append(rid)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET organization_id=? WHERE id IN (?, ?)", (org, ids[0], ids[1]))
    conn.commit()
    conn.close()
    bm.compute(db_path=db_path)
    out = eng.compare(ids[0], "labor_pct_28d", kinds=("peers",), db_path=db_path)
    peers = out["comparisons"][0]
    assert peers["available"] and peers["n"] == 10
    assert peers["measured"] == 10 and peers["members"] == 10
    assert "measured at 10 of 10 in the group" in eng.prompt_lines([out])[0]


# ── the location kind: like for like, named, closed without a viewer ───────

def _group(db_path, specs, org_name="Group"):
    """specs: [(name, location_name, service_model, labor %, hourly_rate)]."""
    conn = get_conn(db_path)
    org = conn.execute("INSERT INTO organizations (name, owner_email) VALUES (?, 'g@x.test')", (org_name,)).lastrowid
    conn.commit()
    conn.close()
    ids = []
    for name, loc, sm, v, rate in specs:
        rid = _rid(db_path, name=name, owner_email="g@x.test", hourly_rate=rate)
        _set_type(db_path, rid, "italian", service_model=sm)
        conn = get_conn(db_path)
        conn.execute("UPDATE restaurants SET organization_id=?, location_name=? WHERE id=?", (org, loc, rid))
        conn.commit()
        conn.close()
        _feature(db_path, rid, _week(0), labor_pct_28d=v, reply_rate_30d=0.5)
        ids.append(rid)
    return ids


def test_locations_compare_like_for_like_by_location_name_and_never_without_a_viewer(db_path):
    """Re-audit #27 (R2-16, R3-17, R4-25, R1-22, R4-27): a café and a
    steakhouse were ranked on labor; a default-wage location against a
    role-rated one; three locations sharing a brand name read the same name
    three times; and the kind was open to any caller with no viewer."""
    a, b, c, d = _group(db_path, [("Luigi's", "North", "full_service", 29.0, 18.0),
                                  ("Luigi's", "South", "full_service", 33.0, 18.0),
                                  ("Luigi's", "Kiosk", "counter", 20.0, 18.0),
                                  ("Luigi's", "East", "full_service", 25.0, 26.0)])
    admin = {"id": 1, "restaurant_id": b, "is_admin": True}
    loc = eng.compare(b, "labor_pct_28d", kinds=("location",), db_path=db_path, viewer=admin)["comparisons"][0]
    assert loc["available"] and {l["name"] for l in loc["locations"]} == {"North", "South"}
    assert loc["rank"] == 2 and loc["of"] == 2 and loc["cohort"] == "sm:full_service"
    kiosk = eng.compare(c, "labor_pct_28d", kinds=("location",), db_path=db_path, viewer=admin)["comparisons"][0]
    assert not kiosk["available"] and "serve differently" in kiosk["why_not"]
    east = eng.compare(d, "labor_pct_28d", kinds=("location",), db_path=db_path, viewer=admin)["comparisons"][0]
    assert not east["available"] and "set your pay rates" in east["why_not"]
    # A behaviour metric travels between service models.
    reply = eng.compare(c, "reply_rate_30d", kinds=("location",), db_path=db_path, viewer=admin)["comparisons"][0]
    assert reply["available"] and reply["of"] == 4
    # No viewer: closed. An explicit server-side call may read it.
    assert not eng.compare(b, "labor_pct_28d", kinds=("location",), db_path=db_path)["comparisons"][0]["available"]
    assert eng.compare(b, "labor_pct_28d", kinds=("location",), db_path=db_path,
                       viewer="system")["comparisons"][0]["available"]


def test_location_siblings_are_the_privacy_organisation_not_only_organization_id(db_path):
    a = _rid(db_path, name="Taco A", owner_email="own@x.test", location_group="Tacos")
    b = _rid(db_path, name="Taco B", owner_email="own@x.test", location_group="Tacos")
    other = _rid(db_path, name="Taco C", owner_email="else@x.test", location_group="Tacos")
    for rid, v in ((a, 0.4), (b, 0.6), (other, 0.9)):
        _feature(db_path, rid, _week(0), reply_rate_30d=v)
    loc = eng.compare(a, "reply_rate_30d", kinds=("location",), db_path=db_path,
                      viewer={"id": 1, "is_admin": True})["comparisons"][0]
    assert loc["available"] and {l["restaurant_id"] for l in loc["locations"]} == {a, b}


# ── serving: permission first, the nightly cache, location once ────────────

def test_payload_filters_by_permission_before_computing(db_path, monkeypatch):
    import permissions
    rid = _rid(db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(permissions, "has_permission",
                        lambda user, perm: perm != permissions.MODULE_VIEW_PERMISSIONS["labor"])
    seen = []
    real = eng.compare

    def spy(restaurant_id, metric, **kw):
        seen.append(metric)
        return real(restaurant_id, metric, **kw)
    monkeypatch.setattr(eng, "compare", spy)
    out = eng.payload_for({"id": 1, "restaurant_id": rid, "role": "manager"}, db_path=db_path)
    assert out["ok"] and seen and not any(reg.meta(m)["module"] == "labor" for m in seen)


def test_payload_is_served_from_the_nightly_cache_until_a_setting_it_depends_on_changes(db_path, monkeypatch):
    """Re-audit #28 (R2-20, R3-22, R4-14, R4-15): the cache was written
    nightly and read by nothing, while every Home load recomputed ~14 metrics
    × 6 kinds with a platform-wide scan per metric; switched on as it was,
    it would have served "profile isn't confirmed" for 36 hours after a
    confirmation."""
    from intelligence import comparison_cache
    rid = _rid(db_path)
    _set_type(db_path, rid, "pizza")
    _feature(db_path, rid, _week(0), labor_pct_28d=29.0, reply_rate_30d=0.5)
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    comparison_cache.materialise(db_path=db_path, wall_seconds=60)
    user = {"id": 1, "restaurant_id": rid, "is_admin": True}
    calls = []
    real_ctx = eng._location_context
    monkeypatch.setattr(eng, "_location_context", lambda *a, **k: calls.append(1) or real_ctx(*a, **k))
    out = eng.payload_for(user, db_path=db_path)
    assert out["ok"] and all(c.get("cached_at") for c in out["comparisons"])
    assert all(any(k["kind"] == "location" for k in c["comparisons"]) for c in out["comparisons"])
    assert len(calls) == 1, "the owner's locations are read once per request, not once per metric"
    assert all("inputs_key" not in c and "facts" not in c for c in out["comparisons"])
    # A profile change misses the cache at once.
    update_restaurant(rid, {"service_model": "full_service"}, db_path=db_path)
    again = eng.payload_for(user, db_path=db_path)
    assert again["ok"] and not any(c.get("cached_at") for c in again["comparisons"])
