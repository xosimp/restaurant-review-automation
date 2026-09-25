"""Friction audit (9/25/26), workstream O2 - food cost, reports, settings.

Each test names the TOP50 item and the source finding it pins. No model,
network, email, SMS or push is reached: Google is stubbed, and nothing
sends.
"""
import json
import os
import sqlite3
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name="dashboard.html"):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    from dsr import store
    store.init_dsr(db_path=db_path)
    yield


def _rid(db_path, **kw):
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "O2 Bistro"), owner_email="o@x.test", **kw),
                             db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _app():
    import strategy_routes
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(strategy_routes.strategy_bp)
    return app


def _as(monkeypatch, rid, role=None):
    user = {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "u"}
    if role:
        user["role"] = role
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(user))
    return user


def _ingredient(db_path, rid, name, cost=4.0, stock=0.0):
    c = _conn(db_path)
    i = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level, current_stock, "
                  "avg_daily_usage, is_active) VALUES (?,?,?,?,10,?,3,1)", (rid, name, "lb", cost, stock)).lastrowid
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
              "VALUES (?,?,'recount',?, '2026-09-01', 'test')", (rid, i, stock))
    c.commit(); c.close()
    return i


def _import(db_path, rid, supplier, lines, applied=None, auto=0):
    c = _conn(db_path)
    i = c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, lines_json, applied_json, applied_at, "
                  "auto_applied) VALUES (?,?,?,?,?,?)",
                  (rid, supplier, json.dumps({"lines": lines}),
                   json.dumps(applied) if applied is not None else None,
                   "2026-09-20 10:00:00" if applied is not None else None, auto)).lastrowid
    c.commit(); c.close()
    return i


# ── #5 / U2-2: pending invoices are reachable ─────────────────────────────────

def test_pending_invoices_include_a_trusted_suppliers_leftovers(db_path):
    """A trusted supplier's invoice with a flagged line still needs the owner;
    list_imports only knew "never applied", so it vanished on reload."""
    import invoices
    rid = _rid(db_path)
    fresh = _import(db_path, rid, "Sysco", [{"index": 0}, {"index": 1}])
    leftover = _import(db_path, rid, "US Foods", [{"index": 0}, {"index": 1}],
                       applied=[{"index": 0, "by": "rule"}], auto=1)
    _import(db_path, rid, "Done Co", [{"index": 0}], applied=[{"index": 0, "by": "owner"}])
    pend = {p["id"]: p for p in invoices.pending_imports(rid, db_path=db_path)}
    assert set(pend) == {fresh, leftover}
    assert pend[leftover]["waiting"] == 1 and pend[fresh]["waiting"] == 2


def test_the_queue_item_opens_the_invoice_itself(db_path):
    import action_queue
    rid = _rid(db_path)
    iid = _import(db_path, rid, "Sysco", [{"index": 0}])
    items = action_queue.items(rid, db_path=db_path)["items"]
    inv = next(i for i in items if i["key"] == "invoice:pending")
    assert inv["action"]["nav"] == f"invoice/{iid}"
    _import(db_path, rid, "Other", [{"index": 0}])
    items = action_queue.items(rid, db_path=db_path)["items"]
    assert next(i for i in items if i["key"] == "invoice:pending")["action"]["nav"] == "inventory/invoices"


def test_the_web_lists_pending_invoices_and_registers_the_invoice_head():
    s = _src()
    assert '<div id="fc2-inv-pending" hidden></div>' in s
    assert "window.fc2LoadPendingInvoices=function(){" in s and "window.fc2OpenInvoice=function(id){" in s
    assert "cavNavRegister('invoice'," in s and 'data-nav="inventory/invoices"' in s
    # loaded when Food Cost opens and after an apply
    assert "loadFcMenu(); fc2LoadPendingInvoices(); }" in s
    assert "renderInvoice();fc2LoadPendingInvoices();" in s


# ── Ask tools: apply invoice lines, receive an order (Command Center ph. 3) ──

def test_apply_invoice_lines_is_a_confirmed_proposal_from_the_stored_invoice(db_path):
    import ask_cavnar_tools as tools
    rid = _rid(db_path)
    beef = _ingredient(db_path, rid, "Beef", cost=2.0)
    iid = _import(db_path, rid, "Sysco", [
        {"index": 0, "selected": True, "ingredient_id": beef, "proposed_cost": 2.4, "description": "BEEF"},
        {"index": 1, "selected": False, "ingredient_id": None, "proposed_cost": None, "description": "??"}])
    p = tools.build_proposal("apply_invoice_lines", {"lines": [{"index": 1, "unit_cost": 99}]}, restaurant_id=rid)
    assert p["route"]["web"] == f"/api/food-cost/invoices/{iid}/apply"
    assert p["route"]["mobile"] == f"/mobile/api/food-cost/invoices/{iid}/apply"
    assert p["body"] == {"use_checked": True}, "the model never names lines or costs"
    assert "Sysco" in p["summary"]
    assert any(d["label"] == "Left for you" for d in p.get("details") or [])
    # nothing waiting -> no proposal
    other = _rid(db_path, name="Empty")
    assert tools.build_proposal("apply_invoice_lines", {}, restaurant_id=other) is None


def test_use_checked_applies_only_the_stored_checked_lines(db_path, monkeypatch):
    rid = _rid(db_path)
    beef = _ingredient(db_path, rid, "Beef", cost=2.0)
    herbs = _ingredient(db_path, rid, "Herbs", cost=1.0)
    iid = _import(db_path, rid, "Sysco", [
        {"index": 0, "selected": True, "ingredient_id": beef, "proposed_cost": 2.4},
        {"index": 1, "selected": False, "ingredient_id": herbs, "proposed_cost": 9.0}])
    _as(monkeypatch, rid)
    out = _app().test_client().post(f"/api/food-cost/invoices/{iid}/apply", json={"use_checked": True}).get_json()
    assert out["ok"] is True and [u["ingredient_id"] for u in out["updated"]] == [beef]
    costs = {r["id"]: r["unit_cost"] for r in _conn(db_path).execute("SELECT id, unit_cost FROM ingredients")}
    assert costs[beef] == 2.4 and costs[herbs] == 1.0


def test_receive_purchase_order_is_a_proposal_for_the_oldest_open_order(db_path):
    import ask_cavnar_tools as tools
    rid = _rid(db_path)
    first = models.record_purchase_order(rid, "Sysco", "s@x.test", [{"item": "Beef", "qty": 4}], 40, db_path=db_path)
    models.record_purchase_order(rid, "Fresh", "f@x.test", [{"item": "Kale", "qty": 2}], 8, db_path=db_path)
    po_id = _conn(db_path).execute("SELECT id FROM purchase_orders WHERE po_number=?", (first,)).fetchone()["id"]
    p = tools.build_proposal("receive_purchase_order", {}, restaurant_id=rid)
    assert p["route"]["web"] == f"/api/food-cost/purchase-orders/{po_id}/received"
    assert first in p["summary"] and p["body"] == {}
    assert tools.build_proposal("receive_purchase_order", {"po_id": 99999}, restaurant_id=rid) is None


# ── #6 closures: see test_account_settings_web.py ─────────────────────────────

def test_account_closures_are_date_chips_not_free_text():
    s = _src()
    assert 'id="as-closure-new"' in s and "function asAddClosure()" in s
    assert "querySelectorAll('#as-closures [data-cdate]')" in s
    assert 'placeholder="12/25/2026, 1/1/2027"' not in s


# ── #40 hours from Google (U2-19) ────────────────────────────────────────────

def test_google_regular_hours_become_account_hours():
    import gmb
    got = gmb.regular_hours({"periods": [
        {"openDay": "MONDAY", "openTime": {"hours": 11}, "closeDay": "MONDAY", "closeTime": {"hours": 14}},
        {"openDay": "MONDAY", "openTime": {"hours": 17}, "closeDay": "MONDAY", "closeTime": {"hours": 22, "minutes": 30}},
        {"openDay": "FRIDAY", "openTime": {"hours": 17}, "closeDay": "SATURDAY", "closeTime": {"hours": 1}},
    ]})
    assert got == {"open": {"Monday": "11:00", "Friday": "17:00"}, "close": {"Monday": "22:30", "Friday": "01:00"}}
    assert gmb.regular_hours(None) == {"open": {}, "close": {}}


def test_the_hours_route_offers_googles_hours_without_saving(db_path, monkeypatch):
    import gmb
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    c = _app().test_client()
    monkeypatch.setattr(gmb, "is_connected", lambda r: False)
    assert c.get("/api/account-settings/hours/google").get_json()["connected"] is False
    monkeypatch.setattr(gmb, "is_connected", lambda r: True)
    monkeypatch.setattr(gmb, "get_gbp_listing", lambda r: {"ok": True, "hours": {"open": {"Monday": "09:00"},
                                                                                 "close": {"Monday": "21:00"}}})
    d = c.get("/api/account-settings/hours/google").get_json()
    assert d["ok"] and d["open"] == {"Monday": "09:00"} and d["close"] == {"Monday": "21:00"}
    assert models.get_restaurant(rid, db_path=db_path).open_times_json is None
    assert "mobile_hours_google" in dir(mobile_api), "the phone's twin"
    s = _src()
    assert "function asHoursFromGoogle(btn)" in s and "function asHoursSameEveryDay()" in s


# ── #40 covers from the POS (U2-18) ──────────────────────────────────────────

def test_pos_guest_counts_are_offered_never_written(db_path, monkeypatch):
    import covers
    rid = _rid(db_path, module_labor=1)
    y = (date.today() - timedelta(days=1)).isoformat()
    d2 = (date.today() - timedelta(days=2)).isoformat()
    c = _conn(db_path)
    for d, g in ((y, 212), (d2, 180)):
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
                  "VALUES (?,?,?,?, 'ready')", (rid, d, "sales.guests", g))
    c.commit(); c.close()
    covers.save(rid, [{"date": d2, "covers": 175}], db_path=db_path)
    assert covers.pos_offers(rid, db_path=db_path) == [{"date": y, "guests": 212}]
    assert covers.by_date(rid, db_path=db_path) == {d2: 175}, "an offer writes nothing"
    import strategy_routes
    _as(monkeypatch, rid)
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    cl = _app().test_client()
    assert cl.get("/api/labor/covers").get_json()["pos_offers"] == [{"date": y, "guests": 212}]
    assert cl.post("/api/labor/covers", json={"from_pos": True, "rows": [{"date": y, "covers": 212}]}).get_json()["ok"]
    src = _conn(db_path).execute("SELECT source FROM covers_daily WHERE date=?", (y,)).fetchone()["source"]
    assert src == "pos_confirmed"
    assert "data-cov-pos=" in _src()


# ── #36 DSR budget prefill (U2-11) ───────────────────────────────────────────

def test_budget_prefill_from_last_week_last_year_and_forecast(db_path, monkeypatch):
    from dsr import store
    rid = _rid(db_path)
    mon = date(2026, 9, 21)
    store.set_budget(rid, mon - timedelta(days=7), gross=5000, net=4500, db_path=db_path)
    c = _conn(db_path)
    c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,'ready')",
              (rid, (mon + timedelta(days=1) - timedelta(days=7)).isoformat(), "sales.net", 3000))
    c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,'ready')",
              (rid, (mon - timedelta(days=364)).isoformat(), "sales.gross", 4000))
    c.commit(); c.close()
    week = [mon + timedelta(days=i) for i in range(7)]
    lw = store.budget_prefill(rid, week, "last_week", db_path=db_path)
    assert lw["days"][0]["gross"] == 5000 and lw["days"][0]["from"] == "last week's budget"
    assert lw["days"][1]["net"] == 3000 and lw["days"][1]["gross"] is None and lw["days"][1]["from"] == "last week's sales"
    assert len(lw["missing"]) == 5, "a night with nothing to go on stays blank, never 0"
    ly = store.budget_prefill(rid, week, "last_year", pct=10, db_path=db_path)
    assert ly["days"][0]["gross"] == 4400.0 and ly["days"][0]["net"] is None
    import demand
    monkeypatch.setattr(demand, "week_projection", lambda r, dates, db_path=None: {"by_day": {mon.isoformat(): 3210.5}})
    fc = store.budget_prefill(rid, week, "forecast", db_path=db_path)
    assert fc["days"][0]["net"] == 3210.5 and fc["days"][0]["gross"] is None
    assert not [d for d in fc["days"][1:] if d["from"]]


def test_the_budget_prefill_route_is_the_owners(db_path, monkeypatch):
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    c = _app().test_client()
    assert c.get("/api/dsr/budget/prefill?start=2026-09-21&from=last_week").get_json()["ok"] is True
    assert c.get("/api/dsr/budget/prefill?start=9/21/26&from=last_week").status_code == 400
    assert c.get("/api/dsr/budget/prefill?start=2026-09-21&from=nope").status_code == 400
    assert c.get("/api/dsr/budget/prefill?start=2026-09-21&from=last_year&pct=500").status_code == 400
    _as(monkeypatch, rid, role="manager")
    assert c.get("/api/dsr/budget/prefill?start=2026-09-21&from=last_week").status_code == 403
    s = _src()
    assert 'data-dr="bud-prefill" data-from="last_week"' in s and "else if(a==='bud-prefill')" in s


# ── #45 counts and receiving for a manager (U2-27), contact 1 (U2-26) ────────

def test_a_manager_counts_and_receives_without_the_margins(db_path, monkeypatch):
    import permissions as p
    mgr = {"role": "manager"}
    assert p.has_permission(mgr, p.FOOD_COST_ENTER) and not p.has_permission(mgr, p.FOOD_COST_VIEW)
    assert p.has_permission({"role": "employee"}, p.FOOD_COST_ENTER) is False
    rid = _rid(db_path)
    ing = _ingredient(db_path, rid, "Salmon", cost=11.0, stock=5)
    models.record_purchase_order(rid, "Sea", "sea@x.test",
                                 [{"ingredient_id": ing, "item": "Salmon", "qty": 3, "unit_cost": 11.0,
                                   "line_cost": 33.0}], 33.0, db_path=db_path)
    _as(monkeypatch, rid, role="manager")
    c = _app().test_client()
    sheet = c.get("/api/food-cost/count-sheet")
    assert sheet.status_code == 200 and sheet.get_json()["items"][0]["name"] == "Salmon"
    pos = c.get("/api/food-cost/purchase-orders?status=sent").get_json()["orders"]
    assert "total_cost" not in pos[0] and "unit_cost" not in pos[0]["items"][0] and "line_cost" not in pos[0]["items"][0]
    assert c.post(f"/api/food-cost/purchase-orders/{pos[0]['id']}/received").get_json()["ok"] is True
    assert c.post("/api/food-cost/waste", json={"ingredient_id": ing, "qty": 1}).get_json()["ok"] is True
    # ...and nothing that carries a margin
    assert c.get("/api/food-cost/cfo").status_code == 403
    assert c.get("/api/food-cost/order-draft").status_code == 403
    assert c.post("/api/food-cost/ingredients", json={"name": "X"}).status_code == 403
    # the owner still sees the money
    _as(monkeypatch, rid)
    assert "total_cost" in c.get("/api/food-cost/purchase-orders").get_json()["orders"][0]


def test_the_counts_only_panel_renders_for_a_manager():
    s = _src()
    assert "{% elif fc_enter_only %}<button class=\"tab\" role=\"tab\" id=\"tab-inventory\"" in s
    assert "window.FC_ENTER_ONLY = true;" in s and "if(window.FC_ENTER_ONLY){" in s
    assert s.count("{% include '_fc_receive_js.html' %}") == 2
    part = _src("_fc_receive_js.html")
    js = part[part.index("<script>"):].replace("food-cost", "")
    assert "function receivePO(id, btn, withLines)" in js
    assert "cost" not in js and "$" not in js, "no dollar on the counts panel"
    hd = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    assert "fc_enter_only=int(" in hd


def test_alert_contact_one_starts_as_the_owner(db_path, monkeypatch):
    from models import update_restaurant
    rid = _rid(db_path)
    update_restaurant(rid, {"owner_name": "Erik", "owner_phone": "+15555550100"}, db_path=db_path)
    _as(monkeypatch, rid)
    d = _app().test_client().get("/api/alert-settings").get_json()
    assert d["owner"] == {"name": "Erik", "phone": "+15555550100"}
    assert "(!c.length && ow.name)" in _src()


# ── #37/#38 the Order card ───────────────────────────────────────────────────

def test_the_order_card_loads_itself_and_edits_quantities():
    s = _src()
    assert 'onclick="loadOrderDraft()"' not in s, "the Load button is gone (U2-16)"
    assert "whenSeen('fc2-order-send',loadOrderDraft);" in s and "whenSeen('mm-body',loadMenuMargins);" in s
    assert 'data-so-qty="' in s and "if(lines.length)body.lines=lines;" in s
    assert "window.confirm(d.error" not in s, "resend is an inline confirm, not confirm()"
    assert "cavNavRegister('order'," in s and 'data-nav="inventory/order"' in s
    assert "in the app (Food Cost" not in s, "the copy points at the web Suppliers block"
    assert "Received as ordered" in _src("_fc_receive_js.html")


def test_a_queued_edited_order_sends_the_edited_lines(db_path, monkeypatch):
    import delayed
    import inventory
    rid = _rid(db_path)
    c = _conn(db_path)
    ing = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level, current_stock, "
                    "avg_daily_usage, is_active, supplier_name, supplier_email) VALUES (?,?,?,?,10,0,3,1,?,?)",
                    (rid, "Kale", "lb", 2.0, "Fresh", "f@x.test")).lastrowid
    c.commit(); c.close()
    draft = inventory.build_supplier_orders(rid)
    sent = {}
    monkeypatch.setattr("emails.send_supplier_order_email", lambda **kw: sent.update(kw))
    out = delayed._run_order_send(rid, {"supplier_email": "f@x.test", "draft_hash": draft["groups"][0]["draft_hash"],
                                        "lines": [{"ingredient_id": ing, "qty": 9}]}, db_path)
    assert out["ok"] is True and sent["items"][0]["qty"] == 9


# ── #39 price tracker: see test_food_cost_layout.py ──────────────────────────
# ── #49 small data-entry wins ────────────────────────────────────────────────

def test_confident_recipes_accept_in_one_tap():
    s = _src()
    assert "var FC2_SURE_PCT=80;" in s and "function fc2ConfidentDrafts(ds){" in s
    fn = s[s.index("function fc2ConfidentDrafts(ds){"):s.index("document.addEventListener('click',function(e){\n  var t=e.target&&e.target.closest?e.target.closest('[data-recipe-accept-all]')")]
    assert "ln.unit_ok===false" in fn and "dr.needs_yield" in fn and "ln.confidence_pct<FC2_SURE_PCT" in fn
    assert "data-recipe-accept-all=" in s


def test_waste_can_be_logged_in_one_line_and_invoice_lines_become_ingredients():
    s = _src()
    assert "function fc2LogWaste(btn)" in s and "'/api/food-cost/waste'" in s
    assert '<option value="__new">+ New ingredient from this line</option>' in s
    assert "'/api/food-cost/ingredients'" in s


def test_the_staff_sign_in_list_can_be_searched():
    s = _src("staff_login.html")
    assert "{% if roster|length > 8 %}" in s and 'id="who-find"' in s
    assert "localStorage.getItem(LAST_KEY)" in s and "catch (e) {}" in s


# ── #30 "Do something about it" (U4-9) ───────────────────────────────────────

def test_cross_module_findings_and_cost_drivers_name_where_to_act():
    import business_intelligence as bi
    import food_cost_intelligence as fci
    assert bi.link_action({"kind": "reviews_x_labor", "day": "Friday"}) == {
        "label": "Open Friday's schedule", "nav": "labor/schedule?day=Friday"}
    assert bi.link_action({"kind": "reviews_x_menu", "dish": "Salmon"})["nav"] == "inventory/menu?dish=Salmon"
    assert bi.link_action({"kind": "reviews_x_food_cost", "day": "Monday"})["nav"] == "inventory/count?day=Monday"
    assert bi.link_action({"kind": "unknown"}) is None
    assert fci.driver_action({"kind": "price"})["nav"] == "inventory/order"
    assert fci.driver_action({"kind": "portion", "dish": "Burger"})["nav"] == "inventory/menu?dish=Burger"
    assert fci.driver_action({"kind": "waste"})["nav"] == "inventory/count"
    s = _src()
    assert "data-cav-go=\"'+esc(l.act.nav)+'\"" in s and "data-cav-go=\"'+wtEsc(x.act.nav)+'\"" in s
    assert "closest('[data-cav-go]')" in s
