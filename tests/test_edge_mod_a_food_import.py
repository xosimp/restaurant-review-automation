"""Food Cost inputs at the edges the MOD audit found untested: the inventory
CSV upload (only the five required columns, a blank cell, "$1.80", title-case
headers, nan/negative/1e308, names differing only by case), NaN arriving
through the invoice apply and the ingredient editor, analyse_inventory on an
empty list, a NULL column and 5,000 items, the mobile overstock/waste totals,
and the CFO driver list when the analysis underneath it raises.

The invariant for an upload: refused with a message, or saved in a shape
every Food Cost surface can read — never "uploaded" followed by a module
that raises on every load. Confirmed defects are strict xfails."""
import io
import json
import os
import sys
import threading
import time

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, get_conn

# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import cogs, food_cost_intelligence, inventory, inventory_ledger, invoices, pos, webhooks  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = "edge-mod-a-food-import-csrf"
FULL_HEADER = "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week\n"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    from models import init_email_log
    init_email_log(db_path=db_path)


class _InertThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        pass

    def start(self):
        return None

    def join(self, *a, **k):
        return None


def _rid(db_path, name="Pantry Co"):
    return create_restaurant(Restaurant(name=name, owner_email="p@x.com", module_inventory=1), db_path=db_path)


@pytest.fixture
def world(db_path, monkeypatch):
    monkeypatch.setattr(threading, "Thread", _InertThread)
    app = Flask(__name__, template_folder="/Users/simp/review_automation/templates")
    app.register_blueprint(client_api.client_bp)
    rid = _rid(db_path)
    uid = create_user(rid, "owner", "owner@pantry.test", "pw123456", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    cl = app.test_client()
    cl.set_cookie("csrf_js", CSRF)
    cl.set_cookie("session_token", create_session(uid, db_path=db_path))

    def upload(text):
        return cl.post("/client/upload-data",
                       data={"data_type": "inventory", "csv_file": (io.BytesIO(text.encode("utf-8")), "inv.csv")},
                       content_type="multipart/form-data", headers={"X-CSRF": CSRF})
    return {"rid": rid, "upload": upload, "db_path": db_path}


def _refused_or_readable(world, text):
    body = world["upload"](text).get_json()
    if not body.get("ok"):
        assert body.get("error")
        return
    items, is_live, analysis = inventory.analysis_for(world["rid"])
    assert is_live is True and analysis["total_items"] >= 1


def _ingredient_names(db_path, rid):
    c = get_conn(db_path)
    rows = c.execute("SELECT name FROM ingredients WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()
    c.close()
    return [r["name"] for r in rows]


# ── A5 CSV #7-#11 / MOD-FC-5, MOD-FC-6: what the upload validator accepts ───

def test_a_complete_inventory_csv_uploads_and_analyses(world):
    _refused_or_readable(world, FULL_HEADER + "Tomato,Produce,10,5,1.8,2,10,1\n")
    assert _ingredient_names(world["db_path"], world["rid"]) == ["Tomato"]


@pytest.mark.parametrize("text", [
    pytest.param("item,current_stock,par_level,unit_cost,waste_last_week\nTomato,5,10,1.8,1\n", id="required-only-columns"),
    pytest.param(FULL_HEADER + "Tomato,Produce,10,,1.8,2,10,1\n", id="blank-numeric-cell"),
    pytest.param(FULL_HEADER + "Tomato,Produce,10,5,$1.80,2,10,1\n", id="currency-formatted"),
    pytest.param("Item,Category,Par_Level,Current_Stock,Unit_Cost,Avg_Daily_Usage,Last_Order_Qty,Waste_Last_Week\n"
                 "Tomato,Produce,10,5,1.8,2,10,1\n", id="title-case-headers"),
    pytest.param(FULL_HEADER + "Tomato,Produce,10,-5,nan,2,10,1e308\n", id="nan-negative-huge"),
])
def test_an_inventory_csv_is_refused_or_readable(world, text):
    _refused_or_readable(world, text)


def test_names_differing_only_by_case_and_whitespace_import_as_one_ingredient(world):
    world["upload"](FULL_HEADER + "Tomato,Produce,10,5,1.8,2,10,1\ntomato ,Produce,10,5,1.8,2,10,1\n")
    assert len(_ingredient_names(world["db_path"], world["rid"])) == 1


# ── A5 invoice #9-#11 / MOD-FC-6: NaN through owner-facing writes ───────────

def _ingredient(db_path, rid, name="Salmon", cost=12.5, **kw):
    d = dict(par=20, stock=15, usage=3.0, last=20, waste=1.0)
    d.update(kw)
    c = get_conn(db_path)
    iid = c.execute(
        "INSERT INTO ingredients (restaurant_id,name,category,unit,par_level,unit_cost,case_size,current_stock,"
        "avg_daily_usage,last_order_qty,waste_last_week,is_active) VALUES (?,?,?,?,?,?,1,?,?,?,?,1)",
        (rid, name, "Protein", "lb", d["par"], cost, d["stock"], d["usage"], d["last"], d["waste"])).lastrowid
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id,ingredient_id,event_type,qty,event_date,source) "
              "VALUES (?,?,'recount',?,date('now'),'migration')", (rid, iid, d["stock"]))
    c.commit(); c.close()
    return iid


def _invoice(db_path, rid):
    c = get_conn(db_path)
    imp = c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, lines_json) VALUES (?,?,?)",
                    (rid, "Sysco", json.dumps({"lines": []}))).lastrowid
    c.commit(); c.close()
    return imp


def _cost(db_path, iid):
    c = get_conn(db_path)
    v = c.execute("SELECT unit_cost FROM ingredients WHERE id=?", (iid,)).fetchone()[0]
    c.close()
    return v


@pytest.mark.parametrize("value", [
    pytest.param(float("nan"), id="nan-literal"),
    pytest.param("nan", id="nan-string"),
])
def test_an_invoice_line_with_a_nan_cost_writes_nothing(db_path, value):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid)
    invoices.apply(rid, _invoice(db_path, rid), [{"index": 0, "ingredient_id": iid, "unit_cost": value}])
    assert _cost(db_path, iid) == 12.5


@pytest.mark.parametrize("value", [0, -3, 100001])
def test_an_invoice_line_with_a_zero_negative_or_absurd_cost_is_skipped(db_path, value):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid)
    out = invoices.apply(rid, _invoice(db_path, rid), [{"index": 0, "ingredient_id": iid, "unit_cost": value}])
    assert out["ok"] is True and out["updated"] == []
    assert _cost(db_path, iid) == 12.5


def test_the_same_ingredient_on_two_invoice_lines_ends_at_the_last_cost_and_keeps_the_original(db_path):
    """Pinned as it behaves: both lines apply in order, the last cost stands,
    and the first line's record still carries the pre-invoice cost. The audit
    does not say this should be refused."""
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid)
    out = invoices.apply(rid, _invoice(db_path, rid), [{"index": 0, "ingredient_id": iid, "unit_cost": 13.0},
                                                       {"index": 1, "ingredient_id": iid, "unit_cost": 13.5}])
    assert _cost(db_path, iid) == 13.5
    assert out["updated"][0]["old_cost"] == 12.5


def test_a_nan_written_through_the_ingredient_editor_does_not_break_analysis(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Beef")
    inventory_ledger.update_ingredient(rid, iid, par_level=float("nan"))
    inventory.analysis_for(rid)


def test_analysis_tolerates_a_null_numeric_column(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid)
    c = get_conn(db_path)
    c.execute("UPDATE ingredients SET unit_cost=NULL WHERE id=?", (iid,))
    c.commit(); c.close()
    items, is_live, analysis = inventory.analysis_for(rid)
    assert analysis["total_items"] == 1


# ── A5 analyse_inventory #9, #14 ────────────────────────────────────────────

def test_an_empty_item_list_analyses_to_zeros():
    a = inventory.analyse_inventory([])
    assert a["total_items"] == 0 and a["total_waste_cost_week"] == 0
    assert a["critical_low"] == [] and a["overstock"] == []


def test_five_thousand_items_analyse_within_a_bound(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    for i in range(5000):
        c.execute("INSERT INTO ingredients (restaurant_id,name,category,unit,par_level,unit_cost,current_stock,"
                  "avg_daily_usage,last_order_qty,waste_last_week,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                  (rid, f"Ingredient {i}", "Produce", "lb", 20, 2.5, 15, 3, 20, 1))
    c.commit(); c.close()
    t = time.time()
    items, is_live, a = inventory.analysis_for(rid)
    assert a["total_items"] == 5000
    assert time.time() - t < 10.0


# ── A5 analyse_inventory #12 / MOD-FC-21: the mobile totals ─────────────────

def _overstocked(db_path, rid, n):
    for i in range(n):
        _ingredient(db_path, rid, name=f"O{i}", cost=10, par=10, stock=30, usage=1, last=10, waste=0)


def test_the_mobile_overstock_total_covers_every_overstocked_item(db_path, monkeypatch):
    rid = _rid(db_path)
    _overstocked(db_path, rid, 10)
    monkeypatch.setattr(inventory, "get_claude_insights", lambda *a, **k: "ok")
    monkeypatch.setattr(mobile_api, "_food_cost_trust_block", lambda rid_: {})
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    uid = create_user(rid, "m", "m@pantry.test", "pw123456", db_path=db_path)
    token = create_session(uid, device_type="ios", db_path=db_path)
    body = app.test_client().get("/mobile/api/food-cost/analytics",
                                 headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body["ok"] is True
    true_total = round(10 * (30 - 10 * 1.10) * 10, 2)
    assert body["overstock_total"] == true_total


def test_analyse_inventory_values_every_overstocked_item_in_stock_value():
    items = [dict(item=f"O{i}", category="Protein", par_level=10, current_stock=30, unit_cost=10,
                  avg_daily_usage=1, last_order_qty=10, waste_last_week=0, case_size=1) for i in range(10)]
    a = inventory.analyse_inventory(items)
    assert a["total_stock_value"] == 3000.0 and len(a["overstock"]) == 5


# ── A5 CFO #9: the driver list when analysis raises ─────────────────────────

def test_cost_drivers_degrade_instead_of_raising_when_the_analysis_fails(db_path):
    import food_cost_intelligence as fci
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid)
    c = get_conn(db_path)
    c.execute("UPDATE ingredients SET unit_cost=NULL WHERE id=?", (iid,))
    c.commit(); c.close()
    out = fci.cost_drivers(rid)
    assert isinstance(out, dict)
