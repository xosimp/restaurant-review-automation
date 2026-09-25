"""Owner screens after the benchmarking re-audit (9/24/26, workstream O fix
round): a published figure measured differently is context on every
metric (#2), the server's target labels on web and iOS (#10), outcome words
instead of "bottom quarter" (#17), the own figure's date and the Data
Health gate (#25), a group and strength per row (#26), why a comparison is
missing with a Confirm-your-profile action (#29), web/iOS parity (#33) and
Home's "new comparison available" (#44)."""
import json
import os
import re
import shutil
import subprocess
from datetime import date, timedelta

import pytest

from models import Restaurant, create_restaurant, get_conn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _cm(metric="food_cost_pct_28d", label="Food cost %", unit="%", better="lower", module="inventory",
        own=31.0, week=None, head=None, comps=()):
    return {"metric": metric, "label": label, "unit": unit, "better": better, "module": module,
            "own": {"value": own, "week": week, "measured": own is not None},
            "headline": {"kind": head, "text": "x"}, "comparisons": list(comps)}


_NRA_FOOD = {"kind": "industry", "available": True, "source": "NRA 2025", "label": "full-service Italian",
             "low": 28.0, "high": 34.0, "median": 32.0, "comparable": False, "source_kind": "published",
             "definition_note": "The published figure is the median of food plus non-alcohol beverage cost; this "
                                "restaurant's food cost % is measured differently, so it is context, not a "
                                "like-for-like comparison."}


# ── #2: a figure measured differently is context, for every metric ─────────

def test_a_not_comparable_published_figure_is_context_with_the_engines_note():
    import benchmark_views as bv
    for own in (31.0, 36.0, 20.0):          # within, "on the wrong side", "better than" — all context now
        row = bv.metric_row(_cm(own=own, head="industry", comps=[_NRA_FOOD]))
        assert row["context"] and row["standing"] is None and row["tone"] == "neutral"
        assert row["behind"] is False and row["action"] is None
        assert row["note"] == _NRA_FOOD["definition_note"]            # the engine's, not a hard-coded labor line


def test_context_survives_an_engine_that_keeps_it_out_of_the_headline():
    import benchmark_views as bv
    row = bv.metric_row(_cm(head=None, comps=[_NRA_FOOD]))
    assert row and row["context"] and row["standing"] is None


def test_a_comparable_published_figure_still_reads_a_standing():
    import benchmark_views as bv
    same = dict(_NRA_FOOD, comparable=True, definition_note=None)
    row = bv.metric_row(_cm(own=36.0, head="industry", comps=[same]))
    assert not row["context"] and row["behind"] and row["tone"] == "warn" and row["action"]


def test_context_rows_stay_off_the_home_strip():
    import benchmark_views as bv
    d = bv.build([_cm(head="industry", comps=[_NRA_FOOD])], scope="home")
    assert d["rows"] == []
    d = bv.build([_cm(head="industry", comps=[_NRA_FOOD])], scope="module", module="inventory")
    assert d["rows"][0]["context"] and "as context" in d["who"]["text"]


def test_ios_labor_drops_the_points_below_industry_line_and_tolerates_absent_dollars():
    sec = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborAnalyticsSection.swift")
    assert "industryLine(" not in sec and "points below" not in sec and "point\\(pts" not in sec
    vm = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborViewModel.swift")
    assert "var laborVsIndustryMonthly: Double? = nil" in vm and "var laborVsIndustryAnnual: Double? = nil" in vm
    assert "let laborVsIndustryMonthly: Double\n" not in vm


# ── #17: outcome words ─────────────────────────────────────────────────────

def _peers(st, pct=81, cohort="p:full", n=12, label="Full-service restaurants on Cavnar AI", p50=30.0):
    return {"kind": "peers", "available": True, "standing": st, "value": 34.0, "n": n, "cohort": cohort,
            "cohort_label": label, "p50": p50, "as_of": "9/20/26", "strength": {"pct": pct}}


def test_bottom_quarter_on_a_lower_is_better_metric_says_worst_and_higher():
    import benchmark_views as bv
    assert bv.outcome_words("bottom quarter", "lower") == "in the group's worst quarter — higher than 3 in 4"
    assert bv.outcome_words("top quarter", "lower") == "in the group's best quarter — lower than 3 in 4"
    assert bv.outcome_words("bottom quarter", "higher") == "in the group's worst quarter — lower than 3 in 4"
    row = bv.metric_row(_cm("labor_pct_28d", "Labor %", module="labor", head="peers",
                            comps=[_peers("bottom quarter")]))
    assert row["standing_key"] == "bottom quarter" and "quarter" in row["standing"]
    assert "bottom" not in row["standing"] and row["tone"] == "warn" and row["behind"]
    assert "worst quarter" in row["action"]["ask"]


# ── #25: the own figure's date and the Data Health gate ────────────────────

def test_each_row_dates_the_restaurants_own_figure():
    import benchmark_views as bv
    row = bv.metric_row(_cm("labor_pct_28d", "Labor %", module="labor", head="peers", week="2026-W38",
                            comps=[_peers("about the middle")]))
    assert row["own_as_of"] == "9/20/26" and row["as_of"] == "9/20/26"


def test_out_of_date_data_withholds_the_standing_and_says_when_it_was_measured():
    import benchmark_views as bv
    cm = _cm("labor_pct_28d", "Labor %", module="labor", head="peers", week="2026-W28",
             comps=[_peers("bottom quarter")])
    d = bv.build([cm], sources={"labor": {"state": "stale", "label": "Shifts", "as_of": "7/12/26"}})
    row = d["rows"][0]
    assert row["stale"] and row["standing"] is None and row["tone"] == "neutral"
    assert not row["behind"] and row["action"] is None
    assert row["note"].startswith("Last measured 7/12/26. Shifts is out of date")
    # Current data keeps the standing.
    d = bv.build([cm], sources={"labor": {"state": "current"}})
    assert d["rows"][0]["behind"] and d["rows"][0]["standing"]


def test_the_card_reads_data_health_for_each_source():
    src = _read("benchmark_views.py")
    assert "data_freshness.source_state(" in src and "withhold_stale(" in src


# ── #26: a group and a strength per row ────────────────────────────────────

def test_rows_from_different_groups_get_a_neutral_header_and_their_own_tags():
    import benchmark_views as bv
    lab = _cm("labor_pct_28d", "Labor %", module="labor", head="peers",
              comps=[_peers("above the middle", pct=80, cohort="p:full:bar", n=9)])
    rat = _cm("avg_rating_30d", "Rating", unit="★", better="higher", module="reviews", own=4.5, head="peers",
              comps=[dict(_peers("top quarter", pct=74, cohort="p:full", n=14), value=4.5, p50=4.3)])
    selfc = _cm("reply_rate_30d", "Reply rate", unit="share", better="higher", module="reviews", own=0.9,
                head="self", comps=[{"kind": "self", "available": True, "verdict": "better than your normal",
                                     "value": 0.9, "baseline": 0.5, "baseline_label": "your own previous 13 weeks",
                                     "as_of": "9/20/26", "points": 13}])
    d = bv.build([lab, rat, selfc], scope="home")
    assert d["who"]["kind"] == "mixed" and d["who"]["groups"] == 3
    assert "2 groups of restaurants like yours" in d["who"]["text"] and "your own normal" in d["who"]["text"]
    assert d["strength"] is None                              # no one row's strength over the others
    tags = {r["metric"]: r["tag"] for r in d["rows"]}
    assert tags["labor_pct_28d"] == "vs 9 like yours · 80% comparison strength"
    assert tags["avg_rating_30d"] == "vs 14 like yours · 74% comparison strength"
    assert tags["reply_rate_30d"] == "vs your own normal"
    # One group: the header names it and carries its strength, as before.
    one = bv.build([lab], scope="home")
    assert one["who"]["kind"] == "peers" and one["who"]["text"].startswith("Compared to 9 other full-service")


# ── #29: why a comparison is missing ───────────────────────────────────────

def _no_peers(why, **extra):
    return dict({"kind": "peers", "available": False, "why_not": why}, **extra)


def _self_ok():
    return {"kind": "self", "available": True, "verdict": "about your normal", "value": 31.0, "baseline": 30.8,
            "baseline_label": "your own previous 13 weeks", "as_of": "9/20/26", "points": 10}


@pytest.mark.parametrize("why,cls,start", [
    ("this restaurant's profile isn't confirmed, so there is no like-for-like group — confirm it in Account",
     "unconfirmed", "Your restaurant profile isn't confirmed yet"),
    ("these restaurants' figures are too spread out for a middle to mean anything", "spread",
     "Restaurants like yours vary too much"),
    ("the other restaurants come from fewer than 5 separate owners", "owners", "Restaurants like yours come from too few"),
    ("fewer than 8 other restaurants have this measured", "few", "Not enough restaurants like yours yet"),
    ("set your pay rates to compare labor cost — the default rate is not your labor", "wage", "Labor cost is on"),
])
def test_the_missing_comparison_says_why(why, cls, start):
    import benchmark_views as bv
    cm = _cm("labor_pct_28d", "Labor %", module="labor", head="self",
             comps=[_self_ok(), _no_peers(why, suggestion={"text": "We think you're a pizza place — is that right?"})])
    b = bv.build([cm], can_edit=True)["below_minimum"]
    assert b["reason"] == cls and b["text"].startswith(start)
    assert b["text"].endswith("here's how you compare to your own last 13 weeks.")
    if cls == "unconfirmed":
        assert b["action"] == {"kind": "profile", "label": "Confirm your profile",
                               "suggestion": "We think you're a pizza place — is that right?", "can_edit": True}
    else:
        assert b["action"] is None


def test_the_progress_hint_names_the_missing_input_for_food_cost_and_waste(db_path):
    from intelligence import engine
    rid = create_restaurant(Restaurant(name="Counts Cafe", owner_email="c@x.test"), db_path=db_path)
    conn = get_conn(db_path)
    for i in range(20):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct) VALUES (?,?,?,?)",
                     (rid, (date.today() - timedelta(days=i + 1)).isoformat(), 2000.0, 30.0))
    conn.commit()
    conn.close()
    food = engine._own_progress(rid, "food_cost_pct_28d", db_path)
    assert "more measured day" not in food and "inventory counts" in food
    waste = engine._own_progress(rid, "waste_sales_pct_28d", db_path)
    assert "more measured day" not in waste and "waste logged" in waste
    import benchmark_views as bv
    assert bv.why_class(food) == "own_data" and bv.why_class(waste) == "own_data"


# ── #10: the server's target labels ────────────────────────────────────────

def test_web_names_the_labor_and_food_targets_as_the_server_does():
    dash = _read("templates", "dashboard.html")
    assert "{% set _tl = (savings_breakdown.labor_target_label if savings_breakdown else None) or 'your target' %}" in dash
    assert "<span>Vs {{ _tl }}</span>" in dash and "<span>Vs your target</span>" not in dash
    assert "d.food_cost_target_label" in dash and "d.food_cost_target_source!=='default'" in dash
    assert '"labor_target_label":        _labor_sb_row.get("labor_target_label")' in _read("hosted_dashboard.py")


def test_ios_names_the_targets_as_the_server_does():
    sec = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborAnalyticsSection.swift")
    assert "Your target (\\(Int(target))%)" not in sec and "targetLegend" in sec
    vm = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborViewModel.swift")
    assert 'case laborTargetLabel = "labor_target_label"' in vm
    fc = _read("ios", "CavnarAI", "CavnarAI", "Models", "FoodCostAnalytics.swift")
    assert 'case foodCostTargetLabel = "food_cost_target_label"' in fc
    ch = _read("ios", "CavnarAI", "CavnarAI", "Features", "FoodCost", "FoodCostTrendChart.swift")
    assert '"your target ~$' not in ch and "target?.label" in ch


def test_the_food_cost_payload_carries_where_its_target_came_from(db_path, monkeypatch):
    import mobile_api
    import models
    rid = create_restaurant(Restaurant(name="Tgt", owner_email="t@x.test", food_cost_target=30.0), db_path=db_path)
    monkeypatch.setattr(mobile_api, "get_restaurant", lambda r: models.get_restaurant(r, db_path=db_path))
    out = mobile_api.food_target_labelled({"ok": True, "target": 30.0}, rid)
    assert out["food_cost_target_source"] == "default" and out["food_cost_target_label"] == "Cavnar AI's starting target"
    kept = mobile_api.food_target_labelled({"food_cost_target_label": "x", "food_cost_target_source": "set"}, rid)
    assert kept["food_cost_target_label"] == "x"


# ── #33: parity ────────────────────────────────────────────────────────────

def test_ios_refreshes_the_card_after_a_profile_save_and_shows_the_local_date():
    sheet = _read("ios", "CavnarAI", "CavnarAI", "Features", "Account", "AccountRestaurantProfileSheet.swift")
    assert "BenchmarkCardStore.shared.reloadAll()" in sheet
    assert "CavnarDate.mdyLocal(" in sheet
    card = _read("ios", "CavnarAI", "CavnarAI", "Features", "Home", "HowYouCompareCard.swift")
    assert "func reloadAll()" in card and '"% STRENGTH"' not in card and "% STRENGTH\")" not in card
    assert "onOpenModule" in card
    assert "HomeBenchmarkStrip(onOpenModule:" in _read("ios", "CavnarAI", "CavnarAI", "Features", "Home", "HomeView.swift")


def test_web_shows_the_confirmed_date_on_the_owners_own_day():
    dash = _read("templates", "dashboard.html")
    assert "window.mdy(String(p.confirmed_at).slice(0, 10))" not in dash and "rpLocalDay(p.confirmed_at)" in dash


# ── #44: Home's "new comparison available" ─────────────────────────────────

def test_a_rung_up_reaches_home_for_a_module_the_login_can_see():
    import home_brief
    ev = [{"event_data": json.dumps({"family": "labor", "from": "self", "to": "peers", "week": "2026-W38"}),
           "created_at": "2026-09-22 04:00:00"},
          {"event_data": json.dumps({"family": "food", "from": "self", "to": "published", "week": "2026-W38"}),
           "created_at": "2026-09-22 04:00:00"},
          {"event_data": json.dumps({"family": "format", "from": "self", "to": "platform", "week": "2026-W38"}),
           "created_at": "2026-09-22 04:00:00"}]
    got = home_brief.comparison_changes(ev, {"labor", "reviews"})
    assert [c["module"] for c in got] == ["labor", "reviews"]
    assert got[0]["text"] == "New comparison available: your labor figures are now compared with restaurants like yours"
    assert got[0]["tone"] == "good"
    assert home_brief.comparison_changes(ev, {"inventory"}) == []   # a published figure is context, not announced
    assert "event_type='benchmark_rung_up'" in _read("home_brief.py")


# ── the web card under node ────────────────────────────────────────────────

_HARNESS = r'''
var document={readyState:'complete',querySelector:function(){return null;},querySelectorAll:function(){return [];},addEventListener:function(){}};
var window={cavConf:null};
function apiJson(r){return r;}
%s
var out={};
out.card=window.cavBench.card({ok:true,who:{kind:'self',text:'vs your own previous 13 weeks'},strength:null,
 below_minimum:{text:"Your restaurant profile isn't confirmed yet.",why_not:'unconfirmed',reason:'unconfirmed',
   action:{kind:'profile',label:'Confirm your profile',suggestion:"We think you're a pizza place",can_edit:true}},
 rows:[{metric:'food_cost_pct_28d',label:'Food cost %%',value_text:'31%%',standing:null,context:true,tone:'neutral',
        against:'full-service Italian',middle_text:'28%%–34%%',note:'Measured differently, so context.',own_as_of:'9/20/26'},
       {metric:'labor_pct_28d',label:'Labor %%',value_text:'34%%',standing:null,stale:true,tone:'neutral',note:'Last measured 7/12/26.'}]});
out.noedit=window.cavBench.card({ok:true,who:{},below_minimum:{text:'x',action:{kind:'profile',can_edit:false}},rows:[]});
console.log(JSON.stringify(out));
'''


def test_the_web_card_draws_context_stale_and_the_profile_action():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = re.search(r'<script id="cav-bench">(.*?)</script>', _read("templates", "dashboard.html"), re.S).group(1)
    out = subprocess.run(["node", "-e", _HARNESS % js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr[-1500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    card = got["card"]
    assert 'data-bm-context="1"' in card and "published: full-service Italian" in card
    text = re.sub(r"<[^>]+>", "", card)
    assert "Measured differently, so context." in card and "your figure 9/20/26" in text
    assert 'data-bm-stale="1"' in card and "Last measured 7/12/26." in card
    assert " — null" not in card and "31%</span> —" not in card and 'data-ask="' not in card
    assert 'data-bm-profile="1"' in card and "Confirm your profile" in card and "pizza place" in card
    assert 'data-bm-profile' not in got["noedit"] and "The account owner can confirm it" in got["noedit"]
