"""Owner screens on the Benchmark Engine (Benchmarking audit 9/24/26,
workstream O): the How you compare card and Home strip (#23, #18), the
location-to-location table and the group ranking (#19), the labor chart's
industry mark (#34), dish colours from the server (#35), waste labels
against a target (#36), the food cost label's source kind (#37), the local
market standing (#38), module helpers reading the engine (#48) and the
confidence cohort label (#40)."""
import json
import os
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn
from intelligence import benchmarks as bm
from intelligence import features as feat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _rid(db_path, **kw):
    kw.setdefault("name", f"Views Cafe {kw.get('owner_email', '')}")
    kw.setdefault("owner_email", "v@x.test")
    # Live 17 weeks on a real labor cost basis, so it may stand in a band
    # (Benchmarking audit #14, #39 — workstream P).
    kw.setdefault("created_at", (date.today() - timedelta(days=120)).isoformat() + "T00:00:00")
    kw.setdefault("hourly_rate", 18.0)
    category = kw.pop("category", None)
    rid = create_restaurant(Restaurant(**kw), db_path=db_path)
    if category:
        # create_restaurant does not write the type; the owner's CONFIRMED
        # profile does, and it is what the peer group is built from (#7, #20).
        conn = get_conn(db_path)
        conn.execute("UPDATE restaurants SET category=?, concept=?, service_model='counter', profile_source='set' "
                     "WHERE id=?", (category, category, rid))
        conn.commit()
        conn.close()
    return rid


def _week(weeks_ago=0):
    return feat.iso_week(date.today() - timedelta(weeks=weeks_ago))


def _feature(db_path, rid, week, **vals):
    conn = get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                 "VALUES (?,?,?,1.0)", (rid, week, json.dumps(vals)))
    conn.commit()
    conn.close()


def _history(db_path, rid, now, normal, metric="labor_pct_28d"):
    for back in range(1, 18):
        _feature(db_path, rid, _week(back), **{metric: normal + (back % 3) * 0.3})
    _feature(db_path, rid, _week(0), **{metric: now})


def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "is_admin": True}


# ── #23 / #18: the card, and its honest below-the-minimum state ────────────

def test_card_below_the_minimum_leads_with_the_restaurants_own_normal(db_path):
    import benchmark_views as bv
    rid = _rid(db_path, category="pizza")
    _history(db_path, rid, now=35.0, normal=30.0)
    d = bv.card(_owner(rid), "labor", db_path=db_path)
    assert d["ok"] and d["who"]["kind"] == "self"
    assert d["who"]["text"] == "vs your own previous 13 weeks"
    b = d["below_minimum"]
    assert b["text"].startswith("Not enough restaurants like yours yet — here's how you compare to your own last 13")
    assert b["why_not"]                                  # the engine's own reason, verbatim
    row = next(r for r in d["rows"] if r["metric"] == "labor_pct_28d")
    assert row["kind"] == "self" and row["standing"] == "worse than your normal" and row["behind"]
    assert row["tone"] == "warn" and row["value_text"] == "35%"
    assert row["action"]["ask"].startswith("My labor % is worse than your normal")
    assert d["strength"] is None                         # no band, no comparison strength


def test_card_with_peers_names_who_how_many_and_the_strength_as_a_percentage(db_path):
    import benchmark_views as bv
    ids = []
    for i in range(12):
        rid = _rid(db_path, name=f"Peer {i}", owner_email=f"p{i}@x.test", category="pizza")
        _feature(db_path, rid, _week(0), labor_pct_28d=28.0 + i * 0.2)
        ids.append(rid)
    bm.compute(db_path=db_path)          # each one's confirmed partition
    d = bv.card(_owner(ids[0]), "labor", db_path=db_path)
    assert d["who"]["kind"] == "peers" and d["who"]["text"].startswith(f"Compared to {d['who']['n']} other")
    assert d["below_minimum"] is None
    s = d["strength"]
    assert isinstance(s["pct"], int) and s["label"] == f"{s['pct']}% comparison strength"
    assert [r["title"] for r in s["rows"]] == ["Peer count", "Band freshness", "Your own figure", "Type match"]
    assert all(r["pct"] is None or 0 <= r["pct"] <= 100 for r in s["rows"]) and s["meaning"]
    row = next(r for r in d["rows"] if r["metric"] == "labor_pct_28d")
    assert row["kind"] == "peers" and isinstance(row["strength_pct"], int)
    # The engine's quartile word is kept as standing_key; the screen reads
    # outcome words (#17) — "bottom quarter" never reaches an owner.
    assert row["standing_key"] in ("top quarter", "above the middle", "about the middle")
    assert row["standing"] in ("in the group's best quarter — lower than 3 in 4", "better than the group's middle",
                               "about the group's middle")


def test_home_strip_puts_what_the_restaurant_is_behind_on_first(db_path):
    import benchmark_views as bv
    rid = _rid(db_path)
    for back in range(1, 18):
        _feature(db_path, rid, _week(back), labor_pct_28d=30.0 + (back % 3) * 0.3,
                 reply_rate_30d=0.5 + (back % 3) * 0.01)
    _feature(db_path, rid, _week(0), labor_pct_28d=35.0, reply_rate_30d=0.9)
    d = bv.card(_owner(rid), "home", db_path=db_path)
    assert d["scope"] == "home" and d["rows"][0]["behind"]
    assert d["rows"][0]["open_module"] == "labor"
    assert any(r["metric"] == "reply_rate_30d" and r["tone"] == "good" for r in d["rows"])


def test_card_routes_exist_on_web_and_mobile():
    web, mob = _read("client_api.py"), _read("mobile_api.py")
    assert '@client_bp.route("/api/benchmarks/card")' in web and '@mobile_bp.route("/benchmarks/card")' in mob
    assert '@client_bp.route("/api/benchmarks/locations")' in web and '@mobile_bp.route("/benchmarks/locations")' in mob
    assert web.count("benchmark_views.card(current_user") == 1 and mob.count("benchmark_views.card(current_user") == 1


def test_web_draws_the_card_on_every_module_and_home():
    dash = _read("templates", "dashboard.html")
    for m in ("labor", "food_cost", "reviews", "marketing"):
        assert f'data-bm-module="{m}"' in dash, m
    assert "window.cavBench.strip(document.getElementById('hb-bench'))" in dash
    assert "window.cavBench.locations(g.location_compare)" in dash
    assert "'/api/benchmarks/card?module='" in dash


# ── #19: location to location, and the group ranking ───────────────────────

def _org(db_path, *rids):
    conn = get_conn(db_path)
    org = conn.execute("INSERT INTO organizations (name, owner_email) VALUES ('Grp', 'g@x.test')").lastrowid
    for r in rids:
        conn.execute("UPDATE restaurants SET organization_id=? WHERE id=?", (org, r))
    conn.commit()
    conn.close()


def test_locations_are_read_against_their_own_normal_and_gaps_only_beyond_noise(db_path):
    import benchmark_views as bv
    a = _rid(db_path, name="North", owner_email="n@x.test")
    b = _rid(db_path, name="South", owner_email="s@x.test")
    c = _rid(db_path, name="East", owner_email="e@x.test")
    w = _rid(db_path, name="West", owner_email="w@x.test")
    _org(db_path, a, b, c, w)
    _history(db_path, a, now=30.2, normal=30.0)
    _history(db_path, b, now=30.5, normal=30.3)      # inside the swing: in line
    _history(db_path, w, now=30.4, normal=30.2)
    _history(db_path, c, now=38.0, normal=37.8)      # far over the others: behind
    lc = bv.location_compare(_owner(a), db_path=db_path)
    assert lc["ok"]
    labor = next(m for m in lc["metrics"] if m["metric"] == "labor_pct_28d")
    by = {l["name"]: l for l in labor["locations"]}
    assert by["East"]["called"] and by["East"]["vs_group"] == "behind your other locations"
    assert by["East"]["tone"] == "warn"
    assert not by["South"]["called"] and by["South"]["vs_group"] == "in line with your other locations"
    assert by["North"]["vs_own"] == "about your normal"


def test_a_location_without_history_is_never_called(db_path):
    import benchmark_views as bv
    a = _rid(db_path, name="Old", owner_email="o@x.test")
    b = _rid(db_path, name="New", owner_email="w@x.test")
    _org(db_path, a, b)
    _history(db_path, a, now=30.0, normal=30.0)
    _feature(db_path, b, _week(0), labor_pct_28d=40.0)
    lc = bv.location_compare(_owner(a), db_path=db_path)
    new = next(l for l in lc["metrics"][0]["locations"] if l["name"] == "New")
    assert not new["called"] and new["vs_group"].startswith("not called")


def test_group_ranking_needs_the_rating_floor_and_a_gap_beyond_noise():
    import benchmark_views as bv
    import thresholds
    assert thresholds.GROUP_RANK_MIN_REVIEWS == thresholds.RATING_MIN_REVIEWS == 5
    # Three reviews each: below the platform's rating floor, nobody is named.
    r = bv.rank_by_rating([{"id": 1, "name": "A", "rating": 4.9, "n": 3}, {"id": 2, "name": "B", "rating": 3.1, "n": 3}])
    assert r["strongest"] is None and r["weakest"] is None
    # 4.6 against 4.5 on a dozen reviews each: a tie, not a strongest and a weakest.
    r = bv.rank_by_rating([{"id": 1, "name": "A", "rating": 4.6, "n": 12}, {"id": 2, "name": "B", "rating": 4.5, "n": 12}])
    assert r["strongest"] is None and r["level"]
    r = bv.rank_by_rating([{"id": 1, "name": "A", "rating": 4.8, "n": 80}, {"id": 2, "name": "B", "rating": 3.9, "n": 60}])
    assert r["strongest"]["location"] == "A" and r["weakest"]["location"] == "B"


def test_group_brief_and_digest_rank_through_the_one_rule():
    for path in ("home_brief.py", "reporter.py"):
        assert "rank_by_rating(" in _read(path), path
    assert 'payload["location_compare"] = _bv.location_compare(current_user)' in _read("home_brief.py")


# ── #34: the labor chart's industry mark and the "$ under industry" tiles ──

def test_web_labor_industry_mark_is_neutral_with_its_source_printed():
    dash = _read("templates", "dashboard.html")
    assert ".lb2-bench .bar .tick.h{background:var(--ink2)}" in dash
    assert ".tick.h{background:var(--hb-bad)}" not in dash
    assert '% industry</span>' in dash and 'color:var(--hb-bad)">{{ _ind }}%' not in dash
    assert 'title="Industry benchmark: {{ savings_breakdown.labor_industry_basis }}"' not in dash
    assert "It includes benefits; yours is wages from your shifts" in dash
    assert "industry / mo" not in dash


def test_ios_labor_has_no_dollars_under_industry_tiles():
    oc = _read("ios", "CavnarAI", "CavnarAI", "Models", "OwnerCopy.swift")
    assert "industry / mo" not in oc and "vs the \\(industryText) industry benchmark" not in oc


# ── #35: dish colours come from the server ─────────────────────────────────

def test_dish_colours_read_the_owners_target_or_the_published_band():
    import cogs
    own = cogs.dish_reference(Restaurant(name="T", owner_email="t@x.test", food_cost_target=28.0))
    assert own["kind"] == "target" and own["pct"] == 28.0
    assert cogs.dish_tone(29.0, own) == "good" and cogs.dish_tone(33.0, own) == "warn" and cogs.dish_tone(40.0, own) == "bad"
    italian = cogs.dish_reference(Restaurant(name="I", owner_email="i@x.test", category="italian",
                                             food_cost_target=None))
    assert italian["kind"] == "published" and italian["pct"] == 32.0 and "NRA 2025" in italian["basis"]
    # A steakhouse has no food-cost entry: no reference, and every dish is neutral.
    assert cogs.dish_reference(Restaurant(name="S", owner_email="s@x.test", category="steakhouse",
                                          food_cost_target=None)) is None
    assert cogs.dish_tone(38.0, None) == "neutral"


def test_both_clients_draw_the_servers_dish_colour():
    dash = _read("templates", "dashboard.html")
    assert "item.food_cost_pct>=40" not in dash and "[item.cost_tone]" in dash
    mp = _read("ios", "CavnarAI", "CavnarAI", "Models", "MenuProfitability.swift")
    assert "pct >= 40" not in mp and "cost_tone" in mp
    assert 'item["cost_tone"] = _cogs.dish_tone(' in _read("mobile_api.py")


# ── #36: waste against a target, never "above average" ─────────────────────

def _waste_analysis(waste, target=None):
    import inventory
    rows = [{"item": "Romaine", "category": "Produce", "par_level": "20", "current_stock": "28",
             "unit_cost": "2.5", "avg_daily_usage": "3.5", "last_order_qty": "25", "waste_last_week": waste}]
    items, _ = inventory.parse_inventory_rows(rows)
    return inventory.analyse_inventory(items, waste_target_pct=target)


def test_waste_labels_say_where_the_rate_sits_against_the_target():
    # 62.50 purchased; waste in units x 2.50.
    labels = {w: _waste_analysis(w)["benchmark_label"] for w in ("1", "1.5", "2", "4")}
    assert labels == {"1": "Under target", "1.5": "Near target", "2": "Over target", "4": "Well over target"}
    for w in ("1", "1.5", "2", "4"):
        a = _waste_analysis(w)
        assert "Average" not in a["benchmark_label"] and "Excellent" not in a["benchmark_label"]
    mine = _waste_analysis("1", target=3.0)            # 4% against the owner's own 3%
    assert mine["benchmark_label"] == "Near target" and "your 3% target" in mine["benchmark_detail"]
    assert mine["waste_target"]["kind"] == "yours"


def test_ios_waste_chart_reads_your_target():
    ch = _read("ios", "CavnarAI", "CavnarAI", "Features", "FoodCost", "FoodCostTrendChart.swift")
    assert '"Industry target"' not in ch and 'RuleMark(y: .value("Your target"' in ch
    assert '"Above Average"' not in ch and '"Over target"' in ch
    t = _read("ios", "CavnarAI", "CavnarAITests", "FoodCostAnalyticsTests.swift")
    assert "industry target" not in t and '"Near target"' in t


def test_home_win_says_the_same_as_the_label():
    src = _read("home_brief.py")
    assert "Excellent waste" not in src and "'Low'} waste rate" not in src
    assert 'f"Waste is {_wl.lower()}"' in src


# ── #37: the food cost label names its source kind ─────────────────────────

def test_food_cost_label_names_the_kind_of_band():
    import benchmark_registry as br
    import cogs
    assert cogs.band_label(30.0, bench=br.lookup("food_cost_pct", "italian")) == \
        ("Within the industry band (NRA 2025)", "neutral")
    assert cogs.band_label(40.0, bench=br.lookup("food_cost_pct", "fine_dining")) == \
        ("Above the rule-of-thumb band", "bad")
    assert cogs.band_name({"source_kind": "vendor"}) == "vendor guidance"


# ── #38: the local market standing ─────────────────────────────────────────

_M = "same cuisine type and similar price level, within 3km"
_W = "widened search — no cuisine or price match, up to 6km away"


def test_market_standing_needs_three_matched_rivals():
    import competitor_intel_format as cif
    two = [{"rating": 4.2, "review_count": 300, "match_basis": _M, "distance_m": 900}] * 2
    mk = cif.market_rating(two)
    st = cif.market_standing({"own_rating": 4.8, "own_rating_basis": "google_all_time", "own_rating_count": 400}, mk)
    assert st["standing"] is None and "needs 3" in st["standing_why_not"]


def test_widened_rivals_never_enter_the_average_and_one_venue_is_capped():
    import competitor_intel_format as cif
    comps = [{"rating": 4.0, "review_count": 200, "match_basis": _M, "distance_m": 1200},
             {"rating": 4.0, "review_count": 200, "match_basis": _M, "distance_m": 2400},
             {"rating": 5.0, "review_count": 9000, "match_basis": _M, "distance_m": 500},
             {"rating": 2.0, "review_count": 800, "match_basis": _W, "distance_m": 7000}]
    mk = cif.market_rating(comps)
    assert mk["market_rating_n"] == 3 and mk["market_excluded_widened"] == 1 and mk["market_matched_n"] == 3
    # 9,000 reviews weigh as 500: (4*200 + 4*200 + 5*500) / 900 = 4.56, not the 4.9 it would dominate to.
    assert mk["market_rating"] == 4.6 and mk["market_radius_km"] == 2.4


def test_a_tie_is_neutral_and_symmetric_inside_the_standard_error():
    import competitor_intel_format as cif
    mk = cif.market_rating([{"rating": 4.3, "review_count": 300, "match_basis": _M, "distance_m": 1000}] * 3)
    own = {"own_rating": 4.4, "own_rating_basis": "google_all_time", "own_rating_count": 60}
    up = cif.market_standing(own, mk)
    down = cif.market_standing(dict(own, own_rating=4.2), mk)
    assert up["standing"] == down["standing"] == "level"
    assert up["standing_tone"] == down["standing_tone"] == "neutral"
    assert up["standing_label"] == "About level with the block"
    assert "3 restaurants matched on cuisine and price within 1 km" in up["standing_basis"]
    assert cif.market_standing(dict(own, own_rating=4.9), mk)["standing"] == "ahead"


def test_the_web_intel_header_no_longer_derives_its_own_standing():
    dash = _read("templates", "dashboard.html")
    assert "Neck and neck" not in dash and "{% elif own_vs_avg >= 0.3 %}" not in dash
    assert "intel_market.standing_basis" in dash


def test_the_reviews_competitor_benchmark_uses_the_intel_market(db_path, monkeypatch):
    import review_intelligence as ri
    rid = _rid(db_path)
    blob = {"competitors": [{"name": "A", "rating": 4.4, "match_basis": _M}, {"name": "B", "rating": 4.6, "match_basis": _M},
                            {"name": "Far", "rating": 2.0, "match_basis": _W}]}
    rest = Restaurant(name="R", owner_email="r@x.test", competitor_intel=json.dumps(blob))
    monkeypatch.setattr(models, "get_restaurant", lambda *a, **k: rest)
    out = ri.competitor_benchmark(rid, db_path=db_path)
    assert out["available"] is False                      # two matched: the widened one doesn't make three
    blob["competitors"].append({"name": "C", "rating": 4.5, "match_basis": _M})
    rest.competitor_intel = json.dumps(blob)
    out = ri.competitor_benchmark(rid, db_path=db_path)
    assert out["available"] and out["competitor_count"] == 3 and out["competitor_median"] == 4.5


# ── #48: module helpers read the engine ────────────────────────────────────

def test_labor_and_food_cost_helpers_read_the_engines_industry_comparison():
    import inspect
    import cogs
    import thresholds
    assert "engine.compare(" in inspect.getsource(thresholds.labor_industry_benchmark)
    assert "engine.compare(" in inspect.getsource(cogs._engine_industry_food_cost)
    it = thresholds.labor_industry_benchmark(Restaurant(name="N", owner_email="x@x.test", category="italian"))
    assert it["pct"] == 34.2 and it["source_kind"] == "published"


# ── #40: the cohort a confidence stand-in came from, by name ───────────────

def test_the_accuracy_dimension_names_its_cohort():
    import confidence_engine as ce
    rec = {"measured": 1, "improved": 1, "source": "own", "prior_measured": 40, "prior_improved": 20,
           "prior_label": "pizza restaurants on Cavnar"}
    acc = ce.accuracy(rec)
    assert acc["source"] == "cohort" and acc["cohort_label"] == "pizza restaurants on Cavnar"
    sw = _read("ios", "CavnarAI", "CavnarAI", "Models", "TrustConfidence.swift")
    assert '"From other restaurants on Cavnar \\u{00B7} "' not in sw and "cohortLabel" in sw


# ── the web card, run under node ───────────────────────────────────────────

_WEB_HARNESS = r'''
var document={readyState:'complete',querySelector:function(){return null;},querySelectorAll:function(){return [];},addEventListener:function(){}};
var window={cavConf:null};
function apiJson(r){return r;}
%s
var out={};
out.card=window.cavBench.card({ok:true,who:{kind:'self',text:'vs your own previous 13 weeks',n:13,as_of:'9/20/26'},strength:null,
 below_minimum:{text:'Not enough restaurants like yours yet.',why_not:'no band'},
 rows:[{metric:'labor_pct_28d',label:'Labor %%',value_text:'35%%',standing:'worse than your normal',tone:'warn',behind:true,
        against:'your own previous 13 weeks',middle_text:'30.3%%',as_of:'9/20/26',action:{label:'Ask what to change',ask:'My labor is worse.'}}]});
var s={pct:68,label:'68%% comparison strength',reason:'r',meaning:'How well supported',footer:'f',
       rows:[{key:'size',title:'Peer count',pct:55,basis:'11 others'},{key:'similarity',title:'Type match',pct:null,basis:'x'}]};
out.strength=window.cavBench.card({ok:true,who:{kind:'peers',text:'Compared to 11 other Pizza restaurants on Cavnar',as_of:'9/20/26'},strength:s,rows:[]});
out.panel=window.cavBench.panel(s);
out.locs=window.cavBench.locations({ok:true,metrics:[{metric:'labor_pct_28d',label:'Labor %%',locations:[
  {id:1,name:'East',value_text:'38%%',vs_own:'about your normal',vs_group:'behind your other locations',tone:'warn'}]}]});
console.log(JSON.stringify(out));
'''


def test_the_web_card_draws_the_servers_words():
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = re.search(r'<script id="cav-bench">(.*?)</script>', _read("templates", "dashboard.html"), re.S).group(1)
    out = subprocess.run(["node", "-e", _WEB_HARNESS % js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr[-1500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert "How you compare" in got["card"] and "hb-row important" in got["card"] and 'data-ask="' in got["card"]
    assert "Not enough restaurants like yours yet." in got["card"] and "Why: no band" in got["card"]
    assert "68" in got["strength"] and "comparison strength" in got["strength"] and "data-explain=" in got["strength"]
    assert got["panel"].count("cf-p-row") == 2 and "Peer count" in got["panel"] and "—" in got["panel"]
    assert "How your locations compare" in got["locs"] and "its normal" in got["locs"]
    assert "hb-row critical" not in got["card"] + got["locs"]      # a standing is never red
