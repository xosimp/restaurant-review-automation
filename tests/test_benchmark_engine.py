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
    assert eng.standing(31.4, band, "labor_pct_28d", 9)[0] == "about the middle"
    assert eng.standing(27.0, band, "labor_pct_28d", 9)[0] == "top quarter"      # lower labor is better
    assert eng.standing(34.5, band, "labor_pct_28d", 9)[0] == "bottom quarter"
    assert eng.standing(0.95, {"p25": 0.5, "p50": 0.6, "p75": 0.7}, "reply_rate_30d", 20)[0] == "top quarter"


def test_comparison_strength_is_a_percentage_with_named_caps():
    wk = _week(0)
    full = eng.strength(25, wk, "set")
    guessed = eng.strength(25, wk, "inferred")
    platform = eng.strength(25, wk, None, platform=True)
    old = eng.strength(25, _week(7), "set")
    assert full["pct"] == 100 and full["label"] == "100% comparison strength"
    assert guessed["pct"] <= eng.INFERRED_TYPE_CAP and "inferred_type" in guessed["caps_applied"]
    assert platform["pct"] <= eng.PLATFORM_CAP and "all_types" in platform["caps_applied"]
    assert old["pct"] < full["pct"] and eng.strength(8, wk, "set")["pct"] < full["pct"]


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
    loc = eng.compare(b, "labor_pct_28d", kinds=("location",), db_path=db_path)["comparisons"][0]
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
