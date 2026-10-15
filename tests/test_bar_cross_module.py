"""The Bar beyond its page (barfix W9, 10/15/26): Ask's read_bar, the DSR's
weekly bar line, the digest / Home card and push for a price increase that
held, the game-week order hint, the value_delivered guard, prime cost's
beverage basis (R7), alcohol out of food COGS (R8), the alcohol targets'
freshness source (R9) and the sales copy (R12)."""
import ast
import json
import os
from datetime import date, timedelta

import pytest

import bar_cost as bc
import bar_signals
import fintech_invoices as fi
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 10, 14)
HEAD = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
        "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n")


def _row(vendor, inv, day, code, desc, uom, qty, cost):
    return (f'EJs,"{vendor}",1,{inv},{day.strftime("%m/%d/%Y")},${qty * cost:.2f},{code},"{desc}",0,{uom},'
            f'{qty},${cost:.2f},$0.00\n')


def _rid(db, **kw):
    kw.setdefault("module_inventory", 1)
    kw.setdefault("name", "EJ's")
    return models.create_restaurant(models.Restaurant(owner_email="o@x.test", **kw), db_path=db)


def _bar(db, rid, titos_after=330.0, weeks=10):
    """Ten weeks of a steady bar: kegs, Tito's (300 a case, then
    `titos_after` from week 6 — a change that holds), a wine; drink sales
    every night."""
    start = TODAY - timedelta(days=70)
    csv = HEAD
    for w in range(weeks):
        d = start + timedelta(days=7 * w)
        csv += _row("Chicago Beverage", f"C{w}", d, "13682", "DAISY CUTTER K 1/2BB", "KE", 4, 229.0)
        csv += _row("Breakthru Beverage", f"B{w}", d + timedelta(days=1), "9031", "TITOS VDK 1L", "CA", 4,
                    300.0 if w < 5 else titos_after)
        csv += _row("Southern Glazer's", f"S{w}", d + timedelta(days=2), "5501", "CH SOUVERAIN P NOIR 750ML", "CA",
                    2, 180.0)
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    conn = models.get_conn(db)
    for k in range(72):
        day = (start + timedelta(days=k)).isoformat()
        for cat, v in (("Beer", 600), ("Liquor", 900), ("Wine", 200)):
            conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                         "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, "sales.cat:" + cat, v))
    conn.commit()
    conn.close()
    return bc.build(rid, db_path=db, today=TODAY)


# ── F4: Ask Cavnar AI read_bar ──────────────────────────────────────────────

def test_ask_read_bar_is_registered_and_permissioned(db_path):
    import ask_cavnar
    import ask_cavnar_tools as tools
    t = tools._BY_NAME["read_bar"]
    assert t["kind"] == "read" and t["module"] == "module_inventory"
    assert "pour cost" in t["spec"]["description"] and "not pour cost" in t["spec"]["description"]
    assert ask_cavnar._TOOL_LABELS["read_bar"] and ask_cavnar._modules_for(["read_bar"]) == ["bar"]
    import data_freshness
    assert data_freshness.sources_for(["bar"]) == ("invoices", "dsr", "sales")
    r = models.get_restaurant(_rid(db_path, module_labor=1, module_reviews=1), db_path=db_path)
    manager = {"id": 9, "role": "manager", "is_admin": 0}
    owner = {"id": 7, "role": "client", "is_admin": 0}
    assert "read_bar" not in {s["name"] for s in tools.tool_specs(tools.viewer_restaurant(r, manager))}
    assert "read_bar" in {s["name"] for s in tools.tool_specs(tools.viewer_restaurant(r, owner))}
    out = json.loads(tools.run_read_tool("read_bar", r.id, {}, restaurant=tools.viewer_restaurant(r, manager)))
    assert "not available" in out["error"]


def test_read_bar_types_bought_vs_sold_as_an_estimate_and_prices_that_held(db_path):
    import response_validation as rv
    rid = _rid(db_path)
    p = _bar(db_path, rid)
    out = bar_signals.ask_read(rid, db_path=db_path, p=p)
    assert out["available"] is True and "purchase_ratio" in out and "pour_cost_counted" not in out
    assert "Never call it pour cost" in out["how_to_say_it"]
    facts = {f.key: f for f in rv.facts_from_dict(out)}
    assert facts["purchase_ratio.est_pct"].kind == "estimate" and facts["purchase_ratio.est_pct"].unit == "%"
    assert facts["purchase_ratio.purchases_usd"].kind == "measured"
    up = out["changes_that_held"][0]
    assert up["product"].startswith("Titos") and up["kind"] == "increase" and up["price_was"] == 300.0
    assert facts["changes_that_held.0.est_forward_monthly_usd"].kind == "estimate"
    assert facts["changes_that_held.0.est_forward_monthly_usd"].period == "month"
    assert facts["changes_that_held.0.already_paid_usd"].kind == "measured"
    liq = next(c for c in out["purchase_ratio"]["categories"] if c["category"] == "Liquor")
    assert facts[f"purchase_ratio.categories.{out['purchase_ratio']['categories'].index(liq)}"
                 ".industry_range_low_pct"].kind == "benchmark"
    assert "context, never a verdict" in liq["range_is"]


def test_read_bar_says_counted_pour_cost_is_measured(db_path):
    import response_validation as rv
    rid = _rid(db_path)
    p = _bar(db_path, rid)
    p["pour_cost"] = dict(p["pour_cost"], basis="counted", label="Pour cost (counted)")
    out = bar_signals.ask_read(rid, db_path=db_path, p=p)
    assert "pour_cost_counted" in out and "purchase_ratio" not in out
    facts = {f.key: f for f in rv.facts_from_dict(out)}
    assert facts["pour_cost_counted.pct"].kind == "measured"


def test_read_bar_without_invoices_says_so(db_path):
    rid = _rid(db_path)
    out = bar_signals.ask_read(rid, db_path=db_path, today=TODAY)
    assert out["available"] is False and "Fintech" in out["why_not"]


# ── the value_delivered guard ───────────────────────────────────────────────

def test_value_delivered_never_counts_bar_price_changes():
    """A price watch "decrease" of −$2,228 must never become "saved": the
    Bar's money is forward estimates (forward_monthly_usd / impact_90d) and
    keg deposits, none of it measured before/after. value_delivered never
    imports the Bar, and never reads those keys."""
    src = open(os.path.join(ROOT, "value_delivered.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    assert not {m for m in mods if m == "bar_cost" or m == "bar_signals" or m == "bar" or m.startswith("bar.")
                or m == "fintech_invoices"}, mods
    for word in ("impact_90d", "forward_monthly_usd", "already_paid_usd", "deposits", "price_watch", "bar_price"):
        assert word not in src, word


# ── price increases that held: digest, Home, push ───────────────────────────

def _row_w5(pk, fwd, **kw):
    """A W5-shaped row: the regime's reference is the NEW usual price."""
    return dict({"product_key": pk, "canonical_key": pk, "name": "Titos VDK 1L", "distributor": "Breakthru",
                 "unit": "case", "from": 300.0, "to": 330.0, "pct": 10.0, "kind": "increase", "persistent": True,
                 "listed": True, "headline": True, "confidence": "high", "forward_monthly_usd": fwd,
                 "since": "2026-09-10", "since_mdy": "9/10/26", "not_bought_lately": False,
                 "regime": {"reference": 330.0, "since": "2026-09-10", "since_mdy": "9/10/26"}}, **kw)


def test_only_a_held_listed_headline_increase_is_news():
    pw = {"increases": [_row_w5("a", 40.0), _row_w5("b", 60.0, persistent=False),
                        _row_w5("c", 80.0, kind="deal_ended"), _row_w5("d", 90.0, headline=False),
                        _row_w5("e", 70.0, not_bought_lately=True), _row_w5("f", 30.0)]}
    rows = bar_signals.headline_increases({"price_watch": pw})
    assert [r["product_key"] for r in rows] == ["a", "f"]
    assert [r["product_key"] for r in bar_signals.headline_increases({"price_watch": pw}, floor=35)] == ["a"]
    assert bar_signals.headline_increases(None) == []


def test_digest_and_home_carry_the_held_increase_and_say_its_money_is_an_estimate(db_path):
    rid = _rid(db_path)
    p = _bar(db_path, rid)
    line = bar_signals.digest_line(rid, p=p)
    assert line and line["rec"]["key"].startswith("bar_price:") and "$330 a case since" in line["text"]
    assert "after $300" in line["text"] and "usual buying" in line["text"]
    home = bar_signals.home_rec(rid, p=p)
    assert home["key"] == line["rec"]["key"] and "(an estimate)" in home["impact"]
    assert "pour cost" not in (line["text"] + home["why"] + home["title"]).lower()
    import rec_ledger
    assert rec_ledger.kind_of(home["key"]) == "bar_price" and rec_ledger.KIND_TOPIC["bar_price"] == "purchasing"
    flat = _bar_flat(db_path)
    assert bar_signals.digest_line(flat[0], p=flat[1]) is None and bar_signals.home_rec(flat[0], p=flat[1]) is None


def _bar_flat(db_path):
    rid = _rid(db_path, name="Flat")
    return rid, _bar(db_path, rid, titos_after=300.0)


def test_the_push_goes_once_and_only_at_25_a_month(db_path, monkeypatch):
    rid = _rid(db_path)
    p = _bar(db_path, rid)
    sent = []
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "_reach",
                        lambda rid_, kind, title, body, data, db, **kw: sent.append((kind, title, data, kw)) or 1)
    assert bar_signals.push_increase(rid, db_path=db_path, p=p) == 1
    kind, title, data, kw = sent[0]
    assert kind == "bar_price" and data == {"tab": "inventory", "nav": "bar"} and kw["email"] is False
    assert kw["rec"]["key"].startswith("bar_price:")
    # Pushed once: the episode's alert_push showing stops a second.
    import rec_ledger
    rec_ledger.present(rid, kw["rec"]["key"], "food", "alert_push", title="x", db_path=db_path)
    assert bar_signals.push_increase(rid, db_path=db_path, p=p) == 0
    # Under $25 a month: Home and the digest, never a push.
    small = {"available": True, "price_watch": {"increases": [_row_w5("z", 20.0)]}}
    assert bar_signals.push_increase(rid, db_path=db_path, p=small) == 0 and len(sent) == 1


def test_the_price_push_is_a_registered_job_refused_off_the_scheduler(db_path, monkeypatch):
    import jobs_registry
    import scheduler
    spec = jobs_registry.JOBS["bar_price_push"]
    assert spec["target"] == ("bar_signals", "run_price_push") and spec["sends"] is True
    src = open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8").read()
    assert '_ops.run_job("bar_price_push", run_price_push)' in src
    rid = _rid(db_path)
    _bar(db_path, rid)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    out = bar_signals.run_price_push(db_path=db_path)
    assert out["attempted"] == 0 and out["skipped"] == 1 and out["reason"]
    import notify
    import push
    assert notify.ALERT_TAB["bar_price"] == "inventory" and "bar_price" in push.PRIORITY
    assert "bar_price" in models.NON_ALERT_TYPES


# ── the DSR's weekly bar line ───────────────────────────────────────────────

def test_dsr_week_bar_line_names_its_basis_and_is_absent_without_a_bar(db_path):
    rid = _rid(db_path)
    p = _bar(db_path, rid)
    line = bar_signals.dsr_week(rid, TODAY - timedelta(days=6), TODAY, db_path=db_path, p=p)
    assert line["available"] and line["basis"] == "purchases" and line["label"] == "Bought vs sold"
    assert "bought vs sold" in line["text"] and "pour cost" not in line["text"].lower()
    assert line["top_price_increase"]["name"].startswith("Titos")
    none_rid = _rid(db_path, name="No bar")
    assert bar_signals.dsr_week(none_rid, TODAY - timedelta(days=6), TODAY, db_path=db_path) is None


def test_the_week_grid_carries_the_bar_line_only_for_food_cost_logins(db_path):
    from dsr import access, rollup
    rid = _rid(db_path)
    _bar(db_path, rid)
    r = models.get_restaurant(rid, db_path=db_path)
    g = rollup.week(r, TODAY, db_path=db_path)
    assert g["bar"] and g["bar"]["basis"] == "purchases"
    assert access.redact_grid(g, {"role": "manager", "id": 3, "is_admin": 0})["bar"] is None
    assert access.redact_grid(g, {"role": "client", "id": 1, "is_admin": 0})["bar"] == g["bar"]
    off = models.get_restaurant(_rid(db_path, name="Off", module_inventory=0), db_path=db_path)
    assert rollup.week(off, TODAY, db_path=db_path)["bar"] is None


# ── F13: the game-week order hint ───────────────────────────────────────────

def test_event_order_hint_is_absent_without_measured_game_weeks(db_path):
    rid = _rid(db_path)
    _bar(db_path, rid)
    assert bar_signals.event_order_hint(rid, today=TODAY, db_path=db_path) is None


def test_event_order_hint_from_three_measured_game_weeks(db_path, monkeypatch):
    from event_intel import engine, store
    rid = _rid(db_path)
    start = TODAY - timedelta(days=140)
    csv = HEAD
    games = [TODAY - timedelta(days=7 * k) for k in (2, 4, 6)]
    for w in range(20):
        d = start + timedelta(days=7 * w)
        qty = 4
        if any(0 <= (g - d).days <= 6 for g in games):
            qty = 6                                  # 50% more beer in a game week
        csv += _row("Chicago Beverage", f"C{w}", d, "13682", "DAISY CUTTER K 1/2BB", "KE", qty, 229.0)
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db_path)
    nxt = {"id": 99, "series_id": 1, "event_date": (TODAY + timedelta(days=3)).isoformat()}
    monkeypatch.setattr(store, "follows", lambda *a, **k: [{"series_id": 1}])
    monkeypatch.setattr(engine, "upcoming", lambda *a, **k: [{"event": nxt, "label": "Bears home game",
                                                              "describe": "Bears vs Packers"}])
    monkeypatch.setattr(engine, "past_games", lambda *a, **k: [{"event": {"event_date": g.isoformat()}}
                                                               for g in games])
    monkeypatch.setattr(engine, "same_kind", lambda e, g: True)
    monkeypatch.setattr(store, "events_for", lambda *a, **k: [{"event_date": g.isoformat()} for g in games])
    monkeypatch.setattr(engine, "restaurant_clock", lambda *a, **k: None)
    monkeypatch.setattr(engine, "local_kickoff", lambda e, tz=None: None)
    hint = bar_signals.event_order_hint(rid, today=TODAY, db_path=db_path)
    assert hint and hint["category"] == "beer" and hint["games"] == 3 and hint["lift_pct"] == 50
    assert "Bears home game" in hint["text"] and "50% more beer & seltzer" in hint["text"]
    # Two measured game weeks are not enough: absent, never a guessed lift.
    monkeypatch.setattr(engine, "past_games", lambda *a, **k: [{"event": {"event_date": g.isoformat()}}
                                                               for g in games[:2]])
    assert bar_signals.event_order_hint(rid, today=TODAY, db_path=db_path) is None


# ── R7: prime cost names its beverage basis ─────────────────────────────────

def _blocks(net=9000.0, labor=2430.0, food=2000.0, cov=80.0, cats=None):
    sales = {"net": net}
    sales.update({f"cat:{k}": v for k, v in (cats or {}).items()})
    return {"sales": {"status": "ready", "metrics": sales}, "labor": {"status": "ready", "metrics": {"cost": labor}},
            "food": {"status": "ready", "metrics": {"est_food_cost": food, "recipe_coverage_pct": cov}}}


def test_prime_cost_names_its_beverage_basis(db_path, monkeypatch):
    """Bar audit R7 / AIX-12: labor + the recipe estimate (no drinks in it)
    over a net that holds the drink sales read ~7 points low. Without a
    counted beverage cost, drink sales leave the denominator and the KPI
    says "excl. beverage" with no goal and no peers; with the bar's counted
    pour cost it is the full prime cost over the full net."""
    from dsr import kpis
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    b = _blocks(cats={"Food": 6300.0, "Liquor": 1800.0, "Beer": 900.0})
    k = kpis.kpi("prime_pct", b, r, TODAY, db_path=db_path, with_peers=False)
    assert k["value"] == round(4430 / 6300 * 100, 1) and k["label"] == "Prime cost excl. beverage"
    assert k["beverage_basis"] == "excluded" and k["target"] is None
    assert "Not comparable with a full prime cost or its 60–65% benchmark" in k["basis"]
    monkeypatch.setattr(kpis, "counted_bev_rate",
                        lambda *a, **kw: {"pct": 20.0, "from": "2026-09-01", "to": "2026-09-30",
                                          "from_mdy": "9/1/26", "to_mdy": "9/30/26"})
    k = kpis.kpi("prime_pct", b, r, TODAY, db_path=db_path, with_peers=False)
    assert k["value"] == round((2430 + 2000 + 2700 * 0.2) / 9000 * 100, 1) and k["label"] == "Prime cost"
    assert k["beverage_basis"] == "counted" and "counted pour cost (20%, 9/1/26–9/30/26)" in k["basis"]
    # No drink categories mapped: over net, as before, saying drinks aren't in it.
    monkeypatch.setattr(kpis, "counted_bev_rate", lambda *a, **kw: None)
    k = kpis.kpi("prime_pct", _blocks(), r, TODAY, db_path=db_path, with_peers=False)
    assert k["value"] == round(4430 / 9000 * 100, 1) and k["beverage_basis"] == "none_measured"


def test_counted_bev_rate_needs_two_counts(db_path):
    from dsr import kpis
    rid = _rid(db_path)
    _bar(db_path, rid)
    assert kpis.counted_bev_rate(rid, TODAY, db_path=db_path) is None


def test_the_monthly_prime_projection_names_beverage_left_out(db_path):
    import cogs
    rid = _rid(db_path)
    _bar(db_path, rid)
    bev = cogs.beverage_sales_in_window(rid, TODAY - timedelta(days=6), TODAY, db_path=db_path)
    assert bev == 7 * (600 + 900 + 200)
    assert cogs.beverage_sales_in_window(_rid(db_path, name="None"), TODAY - timedelta(days=6), TODAY,
                                         db_path=db_path) is None
    src = open(os.path.join(ROOT, "food_cost_intelligence.py"), encoding="utf-8").read()
    assert '"beverage_basis": beverage_basis' in src and "Beverage cost is not in it" in src


# ── R8: alcohol stays out of food COGS ──────────────────────────────────────

def test_liquor_ingredients_stay_out_of_food_cogs(db_path):
    import cogs
    import inventory
    rid = _rid(db_path)
    conn = models.get_conn(db_path)
    ids = {}
    for name, cat, cost in (("Chicken Breast", "Protein", 3.0), ("Titos VDK 1L", "Liquor", 25.0),
                            ("Corona Extra 12oz", None, 1.5), ("White Wine Vinegar", None, 4.0)):
        cur = conn.execute("INSERT INTO ingredients (restaurant_id, name, category, unit, unit_cost, current_stock) "
                           "VALUES (?,?,?,?,?,?)", (rid, name, cat, "ea", cost, 10))
        ids[name] = cur.lastrowid
        conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                     "source) VALUES (?,?,?,?,?,?)", (rid, cur.lastrowid, "receiving", 10, "2026-10-10", "test"))
    conn.commit()
    conn.close()
    dollars, n = cogs.purchases_in_window(rid, date(2026, 10, 1), date(2026, 10, 14), db_path=db_path)
    assert (dollars, n) == (30.0 + 40.0, 2), "Tito's and Corona are the Bar's; the vinegar is food"
    assert cogs.is_bar_item("Liquor", "anything") and cogs.is_bar_item(None, "TITOS VDK 1L")
    assert not cogs.is_bar_item("Produce", "Titos") and not cogs.is_bar_item(None, "VODKA SAUCE")
    items = [{"name": "Chicken", "category": "Protein", "current_stock": 10, "unit_cost": 3.0},
             {"name": "Titos", "category": "Liquor", "current_stock": 10, "unit_cost": 25.0}]
    assert cogs.food_stock_value(items) == 30.0
    a = inventory.analyse_inventory([dict(it, par_level=5, avg_daily_usage=1, waste_last_week=0, last_order_qty=5,
                                          case_size=1, item=it["name"]) for it in items], today=TODAY)
    assert a["total_stock_value"] == 280.0 and a["food_stock_value"] == 30.0


def test_the_food_invoice_scan_warns_on_alcohol_lines(db_path):
    import invoices
    rid = _rid(db_path)
    out = invoices.propose(rid, {"supplier": "Breakthru Beverage", "lines": [
        {"description": "TITOS VDK 1L", "quantity": 2, "unit": "case", "unit_price": 300, "line_total": 600},
        {"description": "WHITE WINE VINEGAR", "quantity": 1, "unit": "case", "unit_price": 20, "line_total": 20}]},
        db_path=db_path)
    titos, vinegar = out["lines"]
    assert titos["alcohol"] == "liquor" and titos["selected"] is False and titos["verified"] is False
    assert "Bar page" in titos["note"] and "food cost" in titos["note"]
    assert not vinegar.get("alcohol")
    assert out["alcohol_lines"] == 1 and "1 line on this invoice" in out["alcohol_note"]


# ── R9: the alcohol targets' freshness source ───────────────────────────────

def test_alcohol_target_change_touches_bar_sources_only(db_path):
    import rec_trust
    rid = _rid(db_path)
    models.update_restaurant(rid, {"liquor_cost_target": 18.0}, db_path=db_path)
    got = [c for c in rec_trust.owner_changes(rid, db_path=db_path) if "liquor cost target" in c["what"]]
    assert got and set(got[0]["sources"]) == {"invoices"}
    for f in ("bar_cost_target", "beer_cost_target", "liquor_cost_target", "wine_cost_target"):
        assert rec_trust._TARGET_SOURCES[f][0] == "invoices", f
    import data_freshness
    assert "invoices" in data_freshness.MODULE_SOURCES["bar"]
    assert "invoices" not in data_freshness.MODULE_SOURCES["food_cost"]


# ── R12: the sales copy says what shipped ───────────────────────────────────

def test_sales_copy_says_bar_tracking_is_partly_live_and_no_more():
    import promise
    import sales_audit_engine as eng
    bar = eng.MODULES["bar"]
    assert bar["live"] is False and bar["partly_live"] is True
    assert "Not available" not in bar["does"] and "bar purchase and price tracking" in bar["does"]
    assert "Arriving: counted pour cost and variance" in bar["does"]
    assert eng._feasibility(bar).startswith("Partly live")
    src = open(os.path.join(ROOT, "sales_audit_cheatsheet.py"), encoding="utf-8").read()
    assert "Bar & Alcohol is upcoming" not in src and "Bar & Alcohol is an upcoming module" not in src
    assert "bar variance tracking is an upcoming module" not in src
    assert "bar purchase and price tracking is live" in src.lower()
    note = promise.NOT_MEASURABLE["bar"]
    assert "no pour-cost" not in note and "pour cost only from the bar's own counts" in note
    reg = open(os.path.join(ROOT, "models.py"), encoding="utf-8").read()
    assert "live INSIDE Food Cost" in reg and "partly" in reg
    # The printed one-sheet (its builder and the HTML it writes) agrees:
    # bar purchases and prices under Food Cost, bar pour cost and variance
    # arriving — never the whole Bar "coming soon".
    for name in ("build_one_sheet.py", "cavnar-one-sheet.html"):
        sheet = open(os.path.join(ROOT, "brand", "print", name), encoding="utf-8").read()
        soon = sheet.split("<b>Coming soon</b>")[1].split("</span>")[0] if "<b>Coming soon</b>" in sheet else ""
        assert "Bar" not in soon, name
        assert "<b>Arriving</b>Bar pour cost &amp; variance" in sheet, name
        assert "bar purchases and price changes" in sheet, name


# ── found on the way: the price card's "from a usual" ───────────────────────

def test_the_price_card_names_the_price_before_the_change():
    """A W5 row's regime reference is the NEW usual price once a change
    held: the card read "$330 a case since 9/10/26, from a usual $330"."""
    from bar import insights as bi
    card = bi._insights({"available": False}, {"increases": [_row_w5("a", 40.0, purchases=4)]}, 0)[0]
    assert "from a usual $300" in card["metric"] and "from a usual $330" not in card["metric"]
