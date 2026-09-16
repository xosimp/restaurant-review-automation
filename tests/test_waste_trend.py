"""The Waste Trend engine (waste_trend.py) — the numbers behind the Food
Cost trend card, and the rules that turn them into statements.

Written against measured problems: snapshots are stored per calendar day
so two insight views in one week drew as two weeks; the desktop route
returned six rows under a "last 8 weeks" heading; and the chart shipped
raw totals with no direction, no target gap and no financial impact.
"""
import json

import pytest
from flask import Flask

import auth
import client_api
import models
import waste_trend as wt
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)


def _restaurant(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _row(db_path, rid, week_end, waste, items=None, top_items=None):
    conn = models.get_conn(db_path)
    conn.execute(wt._SCHEMA)
    conn.execute(
        "INSERT INTO inventory_history (restaurant_id, week_end, waste_json, items_json) VALUES (?,?,?,?)",
        (rid, week_end, json.dumps({"total_waste_cost": waste, "top_items": top_items or []}),
         json.dumps(items) if items else None),
    )
    conn.commit()
    conn.close()


def _weeks(values, start="2026-07-01"):
    """A bare series for the pure functions — one week per value, seven
    days apart, no item detail."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    out = []
    for i, v in enumerate(values):
        we = d0 + timedelta(weeks=i)
        out.append({"week_end": we.isoformat(), "start": (we - timedelta(days=6)).isoformat(),
                    "label": f"{we.month}/{we.day}", "start_label": f"{(we - timedelta(days=6)).month}/{(we - timedelta(days=6)).day}",
                    "waste": float(v), "rate": None, "inv_value": None, "purchased": None,
                    "top_items": [], "categories": None, "has_items": False})
    return out


# ── The series is weeks, not view-days ─────────────────────────────────────

def test_two_snapshots_in_one_week_are_one_week_and_the_newest_wins(db_path):
    """Gia Mia's production history had 9/09 and 9/10 as adjacent bars —
    the seed anchored one and a real insight view wrote the other."""
    rid = _restaurant(db_path)
    _row(db_path, rid, "2026-09-09", 250.0)
    _row(db_path, rid, "2026-09-10", 262.5)
    _row(db_path, rid, "2026-09-16", 176.3)
    weeks, total = wt.load_waste_history(rid, db_path=db_path)
    assert total == 2
    assert [w["waste"] for w in weeks] == [262.5, 176.3]
    assert weeks[0]["week_end"] == "2026-09-10"


def test_the_limit_counts_weeks_after_bucketing_not_rows(db_path):
    rid = _restaurant(db_path)
    for d, v in (("2026-08-03", 1), ("2026-08-04", 2), ("2026-08-10", 3), ("2026-08-17", 4)):
        _row(db_path, rid, d, v)
    weeks, total = wt.load_waste_history(rid, limit=2, db_path=db_path)
    assert total == 3
    assert [w["waste"] for w in weeks] == [3.0, 4.0]


def test_a_snapshot_with_item_detail_yields_rate_top_items_and_categories(db_path):
    rid = _restaurant(db_path)
    items = [
        {"item": "Romaine", "category": "Produce", "unit_cost": 2.0, "waste_last_week": 5, "last_order_qty": 20, "current_stock": 10},
        {"item": "Dough", "category": "Bakery", "unit_cost": 1.0, "waste_last_week": 4, "last_order_qty": 60, "current_stock": 30},
        {"item": "Beer", "category": "Beverage", "unit_cost": 95.0, "waste_last_week": 0, "last_order_qty": 3, "current_stock": 4},
    ]
    _row(db_path, rid, "2026-09-16", 14.0, items=items)
    weeks, _ = wt.load_waste_history(rid, db_path=db_path)
    w = weeks[0]
    assert w["has_items"] is True
    assert [t["item"] for t in w["top_items"]] == ["Romaine", "Dough"]
    assert w["top_items"][0]["cost"] == 10.0
    assert w["categories"] == {"produce": 10.0, "bakery": 4.0}
    assert w["purchased"] == 40 + 60 + 285
    assert w["rate"] == round(14.0 / 385 * 100, 1)


def test_other_restaurants_history_never_leaks(db_path):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, "Gia Mia")
    _row(db_path, rid, "2026-09-16", 100.0)
    _row(db_path, other, "2026-09-16", 999.0)
    weeks, _ = wt.load_waste_history(rid, db_path=db_path)
    assert [w["waste"] for w in weeks] == [100.0]


# ── Derived figures ────────────────────────────────────────────────────────

def test_week_over_week_and_rolling_averages():
    s = wt.waste_trend_stats(_weeks([100, 120, 110, 130, 150, 160, 170, 190]))
    assert s["wow_delta"] == 20 and s["wow_pct"] == round(20 / 170 * 100, 1)
    assert s["rolling4"] == 167.5
    assert s["prior4"] == 115.0
    assert s["mom_delta"] == 52.5


def test_best_worst_and_largest_swings():
    s = wt.waste_trend_stats(_weeks([120, 90, 210, 150, 140]))
    assert s["best"]["waste"] == 90 and s["best"]["index"] == 1
    assert s["worst"]["waste"] == 210 and s["worst"]["index"] == 2
    assert s["largest_increase"]["delta"] == 120
    assert s["largest_decrease"]["delta"] == -60


def test_direction_reads_the_slope_not_the_last_move():
    """A single down week inside a rising series is still a rising series."""
    assert wt.waste_trend_stats(_weeks([100, 120, 115, 140, 160, 155, 180]))["direction"] == "worsening"
    assert wt.waste_trend_stats(_weeks([180, 160, 165, 140, 120, 125, 100]))["direction"] == "improving"
    assert wt.waste_trend_stats(_weeks([150, 152, 149, 151, 150, 148]))["direction"] == "flat"


def test_no_direction_is_called_on_two_weeks():
    s = wt.waste_trend_stats(_weeks([100, 150]))
    assert s["direction"] is None and s["confidence"] is None
    assert s["wow_delta"] == 50


def test_confidence_grows_with_weeks_and_consistency():
    assert wt.waste_trend_stats(_weeks([100, 120, 140]))["confidence"] == "low"
    assert wt.waste_trend_stats(_weeks([100, 120, 140, 160, 180]))["confidence"] == "medium"
    assert wt.waste_trend_stats(_weeks([100, 110, 120, 130, 140, 150, 160, 170]))["confidence"] == "high"
    # Eight weeks that zig-zag their way up are not a high-confidence trend.
    assert wt.waste_trend_stats(_weeks([100, 160, 90, 170, 95, 180, 100, 190]))["confidence"] != "high"


def test_a_spike_is_an_anomaly_and_a_quiet_series_has_none():
    s = wt.waste_trend_stats(_weeks([150, 155, 148, 152, 320, 151, 149, 153]))
    assert [a["kind"] for a in s["anomalies"]] == ["spike"]
    assert s["anomalies"][0]["index"] == 4
    assert wt.waste_trend_stats(_weeks([150, 155, 148, 152, 151, 149]))["anomalies"] == []
    # Three weeks is not enough spread to call anything unusual.
    assert wt.waste_trend_stats(_weeks([100, 100, 400]))["anomalies"] == []


def test_target_gap_and_financial_impact():
    s = wt.waste_trend_stats(_weeks([100, 120, 110, 130]), target_weekly=80.0)
    assert s["above_target"] is True
    assert s["gap_weekly"] == 50.0
    assert s["weeks_over_target"] == 4
    assert s["annualized_current"] == round(115 * 52)
    assert s["annualized_if_target"] == 80 * 52
    assert s["savings_if_at_target"] == round(35 * 52)


def test_intervention_needs_over_target_and_either_a_rising_trend_or_a_streak():
    over_and_rising = wt.waste_trend_stats(_weeks([100, 120, 140, 160, 180]), target_weekly=90.0)
    assert over_and_rising["intervention"] is True
    over_but_falling = wt.waste_trend_stats(_weeks([300, 250, 200, 150, 120]), target_weekly=90.0)
    assert over_but_falling["intervention"] is False
    under = wt.waste_trend_stats(_weeks([60, 70, 80, 85, 88]), target_weekly=90.0)
    assert under["intervention"] is False


def test_an_empty_series_yields_a_complete_but_null_stats_dict():
    s = wt.waste_trend_stats([])
    assert s["weeks"] == 0 and s["latest"] is None and s["best"] is None
    assert s["anomalies"] == []


# ── Observations only say what the numbers show ───────────────────────────

def test_observations_quote_the_stats_they_come_from():
    weeks = _weeks([100, 120, 110, 130, 150, 160, 170, 190])
    weeks[7]["top_items"] = [{"item": "Romaine", "cost": 30}, {"item": "Dough", "cost": 20}]
    stats = wt.waste_trend_stats(weeks, target_weekly=80.0)
    obs = wt.waste_trend_observations(stats, weeks)
    texts = " ".join(o["text"] for o in obs)
    assert "trending up" in texts
    assert "rose $20" in texts
    assert "worst at $190" in texts and "Romaine and Dough" in texts
    assert "over the 4.5% target" in texts
    assert f"${stats['savings_if_at_target']:,}" in texts
    assert 1 <= len(obs) <= 4
    assert all(o["tone"] in ("good", "warn", "bad", "neutral") for o in obs)


def test_two_weeks_get_a_comparison_but_no_trend_claim():
    weeks = _weeks([100, 150])
    obs = wt.waste_trend_observations(wt.waste_trend_stats(weeks), weeks)
    texts = " ".join(o["text"] for o in obs)
    assert "trending" not in texts
    assert "rose $50" in texts


def test_a_series_under_target_is_told_so():
    weeks = _weeks([60, 65, 62, 64])
    obs = wt.waste_trend_observations(wt.waste_trend_stats(weeks, target_weekly=90.0), weeks)
    assert any("under the 4.5% target" in o["text"] and o["tone"] == "good" for o in obs)


# ── The target line comes from real purchases ─────────────────────────────

def test_target_prefers_this_weeks_live_purchases():
    dollars, basis = wt.implied_target_weekly({"waste_rate_pct": 8.4, "total_waste_cost_week": 117.6}, [])
    assert basis == "live"
    assert dollars == round(117.6 / 0.084 * 0.045, 2)


def test_target_falls_back_to_the_newest_week_with_purchase_data():
    weeks = _weeks([100, 110])
    weeks[0]["purchased"] = 1000.0
    dollars, basis = wt.implied_target_weekly(None, weeks)
    assert (dollars, basis) == (45.0, "history")
    assert wt.implied_target_weekly(None, _weeks([100])) == (None, None)


# ── The whole payload ──────────────────────────────────────────────────────

def test_payload_flags_each_week_and_offers_only_ranges_the_history_can_fill(db_path):
    rid = _restaurant(db_path)
    from datetime import date, timedelta
    d0 = date(2026, 6, 3)
    vals = [150, 155, 148, 152, 320, 151, 149, 153, 160]
    for i, v in enumerate(vals):
        _row(db_path, rid, (d0 + timedelta(weeks=i)).isoformat(), v)
    p = wt.build_waste_trend(rid, "8w", analysis={"waste_rate_pct": 8.0, "total_waste_cost_week": 160.0}, db_path=db_path)
    assert p["ok"] and p["weeks_total"] == 9 and len(p["weeks"]) == 8
    assert p["ranges"] == ["8w", "13w"]
    assert p["target"]["basis"] == "live" and p["target"]["weekly"] == 90.0
    flags = [w["flags"] for w in p["weeks"]]
    assert flags[-1]["this_week"] is True
    assert sum(1 for f in flags if f["worst"]) == 1 and sum(1 for f in flags if f["best"]) == 1
    assert any(f["anomaly"] == "spike" for f in flags)
    assert all(f["over_target"] for f in flags)
    assert p["empty"] is None
    assert p["observations"]


def test_an_unknown_range_falls_back_to_eight_weeks(db_path):
    rid = _restaurant(db_path)
    assert wt.build_waste_trend(rid, "9000w", db_path=db_path)["range"] == "8w"


def test_empty_states_explain_why_and_when(db_path):
    rid = _restaurant(db_path)
    sample = wt.build_waste_trend(rid, "8w", is_live=False, db_path=db_path)["empty"]
    assert "sample" in sample["reason"].lower() and sample["weeks_needed"] == 2
    none = wt.build_waste_trend(rid, "8w", is_live=True, db_path=db_path)["empty"]
    assert none["weeks_have"] == 0 and "week" in none["needed"].lower()
    _row(db_path, rid, "2026-09-16", 120.0)
    one = wt.build_waste_trend(rid, "8w", is_live=True, db_path=db_path)
    assert one["empty"]["weeks_have"] == 1 and len(one["weeks"]) == 1
    assert one["stats"]["latest"] == 120.0


# ── Routes ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_path):
    app = Flask(__name__)
    app.register_blueprint(client_api.client_bp)
    return app.test_client()


def _login(client, db_path, rid):
    uid = auth.create_user(rid, "erik", "erik@x.test", "correct-horse", db_path=db_path)
    client.set_cookie("session_token", auth.create_session(uid, db_path=db_path))
    return uid


def test_the_route_needs_a_login(client):
    assert client.get("/api/food-cost/waste-trend").status_code in (401, 403)


def test_the_route_returns_the_card_payload_for_this_restaurant_only(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, "Gia Mia")
    _login(client, db_path, rid)
    _row(db_path, rid, "2026-09-09", 200.0)
    _row(db_path, rid, "2026-09-16", 150.0)
    _row(db_path, other, "2026-09-16", 999.0)
    import inventory
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([], True))
    monkeypatch.setattr(inventory, "analyse_inventory", lambda *a, **k: {"waste_rate_pct": 5.0, "total_waste_cost_week": 150.0})
    d = client.get("/api/food-cost/waste-trend?range=13w").get_json()
    assert d["ok"] is True
    assert [w["waste"] for w in d["weeks"]] == [200.0, 150.0]
    assert d["range"] == "8w", "13 weeks isn't offered over two weeks of history"
    assert d["stats"]["wow_delta"] == -50.0
    assert d["target"]["weekly"] == 135.0
    assert d["observations"][0]["text"].startswith("Waste fell $50")


def test_the_route_survives_the_analysis_failing(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login(client, db_path, rid)
    _row(db_path, rid, "2026-09-16", 150.0)
    import inventory
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    d = client.get("/api/food-cost/waste-trend").get_json()
    assert d["ok"] is True and d["target"]["weekly"] is None
    assert d["weeks"][0]["waste"] == 150.0


# ── The page reads the engine, once ───────────────────────────────────────

def test_the_dashboard_fetches_the_trend_from_the_engine_exactly_once():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "/api/food-cost/waste-trend" in html
    assert "/api/inv-trend" not in html, "the six-row legacy route is not the card's source"
    assert "loadInvTrend" not in html
    # The page-load path: one initial fetch, plus one forced refresh after
    # the insight call that writes this week's snapshot resolves.
    body = html.split("function loadInvInsight(){", 1)[1].split("\nfunction ", 1)[0]
    assert body.count("loadWasteTrend();") == 1, "the trend used to be fetched twice per page load"
    assert body.count("loadWasteTrend(true);") == 1, "this week's snapshot is written by the insight run"


def test_every_toggled_wt_section_has_a_hidden_override(db_path):
    """A class that sets its own `display` beats the browser's native
    [hidden]{display:none} — found live: #fc2-wt-ctl{display:flex} kept the
    range pills and Target-line button visible over the empty state, since
    nothing in this file gives [hidden] the higher specificity it needs.
    Every id this page's wtShow()/`hidden` toggles that also declares its
    own display must have a matching [id][hidden] override."""
    import os, re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()
    toggled_ids = set(re.findall(r"wtShow\('([a-z0-9-]+)'", html))
    toggled_ids |= {m for m in re.findall(r'id="(fc2-wt-[a-z-]+)"\s+hidden', html)}
    assert toggled_ids, "no toggled ids found — the selector above is stale"
    for tid in toggled_ids:
        cls_m = re.search(r'id="%s"[^>]*class="([^"]+)"' % re.escape(tid), html) \
            or re.search(r'class="([^"]+)"[^>]*id="%s"' % re.escape(tid), html)
        if not cls_m:
            continue
        for cls in cls_m.group(1).split():
            if re.search(r"\.%s\{[^}]*\bdisplay\s*:" % re.escape(cls), html):
                assert re.search(r"#%s\[hidden\]|\.%s\[hidden\]" % (re.escape(tid), re.escape(cls)), html), \
                    f"#{tid} (.{cls} sets display) has no [hidden] override — it will stay visible while hidden"


def test_the_empty_state_counts_weeks_recorded_out_of_weeks_needed():
    """The progress line ("0 of 2 weeks recorded") is built from the same
    empty-state dict the card renders from — pin the field names so the two
    never drift apart."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "e.weeks_have" in html and "e.weeks_needed" in html
    assert "weeks recorded" in html


def _dashboard_html():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_initial_tab_is_only_activated_once():
    """On a fresh load with #inventory or #competitor in the URL, switchTab()
    already calls switchFcTab()/switchIntelTab() once — a second, unguarded
    call right after it in the DOMContentLoaded handler used to fire every
    time that hash matched, doubling renderFcGauge()/renderWasteDonut()/
    renderOstockDonut() (none of them are re-entry guarded) in the same
    tick the hero numbers' count-up animation was starting. That extra
    synchronous work landing mid-animation is what read as the count-up
    lagging on load."""
    html = _dashboard_html()
    block = html.split("document.addEventListener('DOMContentLoaded', function(){", 1)[1]
    block = block.split("// 2FA digit auto-advance", 1)[0]
    assert "!btn&&document.getElementById('panel-inventory')" in block.replace(" ", "")
    assert "!btn&&document.getElementById('panel-competitor')" in block.replace(" ", "")


def test_observations_sort_good_neutral_warn_bad_for_column_fill():
    """.fc2-wt-obs fills column-first (grid-auto-flow:column), so this sort
    order is what actually groups matching-tone bullets into the same
    column — verified live: two good-tone entries mixed with a bad and a
    warn both landed at the same x, left of the other two. warn sorts
    before bad (swapped from the engine's own order) per an explicit ask:
    the red bullet and the yellow bullet had landed in each other's spot."""
    import re
    import subprocess
    html = _dashboard_html()
    assert "grid-auto-flow:column" in html.split(".fc2-wt-obs{", 1)[1].split("}", 1)[0]
    m = re.search(r"var _obTonePri=.*?;", html)
    assert m, "tone-priority map not found"
    js = m.group(0) + """
var obs = [
  {tone:'bad', text:'b'}, {tone:'good', text:'g1'},
  {tone:'warn', text:'w'}, {tone:'neutral', text:'n'}, {tone:'good', text:'g2'}
];
obs.sort(function(a,b){
  var pa=_obTonePri[a.tone]!=null?_obTonePri[a.tone]:1, pb=_obTonePri[b.tone]!=null?_obTonePri[b.tone]:1;
  return pa-pb;
});
console.log(JSON.stringify(obs.map(function(o){return o.tone;})));
"""
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    import json
    order = json.loads(out.stdout.strip())
    assert order == ["good", "good", "neutral", "warn", "bad"]


def test_the_over_target_pill_states_the_actual_target_percent():
    """'Over target' on its own doesn't say what the target is — pin the
    percent into the pill text."""
    html = _dashboard_html()
    assert "'Over '+d.target.pct+'% target'" in html


def test_the_ledger_sheen_that_never_actually_left_the_bar_is_gone():
    """.bar i::after's exit position (left:110%) was computed against the
    *unscaled* width of the same element the bar's own transform:scaleX()
    shrinks — so for any bar under 100% full (i.e. virtually all of them),
    the sheen's "off-screen" endpoint landed proportionally compressed
    back inside the visible bar and sat there permanently instead of
    exiting, showing as a stray gray/white patch near the end of the bar."""
    html = _dashboard_html()
    assert "fc2Sheen" not in html
    assert ".fc2-ledger .r .bar i::after" not in html


def test_forecast_box_has_no_emoji():
    import client_api
    src = open(client_api.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    forecast_block = src.split("forecast_html = (", 1)[1].split("unverified_html", 1)[0]
    assert "\\U0001f52e" not in forecast_block
    assert ">Forecast</div>" in forecast_block


def test_the_food_cost_tag_credits_cavnar_ai_not_just_cavnar():
    html = _dashboard_html()
    assert "Cavnar AI's read on your food cost" in html
