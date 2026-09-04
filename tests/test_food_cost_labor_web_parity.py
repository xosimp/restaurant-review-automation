"""Web parity for supplier orders, menu margins, and schedule publishing.

These three shipped iOS-only. The underlying business logic
(build_supplier_orders, menu_profitability, get_staff_contacts,
create_schedule_share, ...) is already exhaustively tested against the
mobile routes in test_mobile_api.py — these tests just pin down that the
web routes exist, are session-authed, and delegate to the same functions.
"""
import pytest
from flask import Flask

import auth
import client_api
import models
from client_api import client_bp
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Web Parity Co")
    kw.setdefault("owner_email", "w@x.com")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _login_as(monkeypatch, rid):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "webuser"})


def _ingredient(db_path, rid, name, *, supplier_name=None, supplier_email=None,
               par=10, stock=0, usage=3, cost=4.0):
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost,
                                 case_size, current_stock, avg_daily_usage, last_order_qty,
                                 waste_last_week, is_active, supplier_name, supplier_email)
        VALUES (?,?,?,?,?,?,1,?,?,0,0,1,?,?)
    """, (rid, name, "Produce", "lb", par, cost, stock, usage, supplier_name, supplier_email))
    conn.commit()
    conn.close()


def _menu_item(db_path, rid, name, price=None):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO menu_items (restaurant_id, name, sell_price, is_active) VALUES (?,?,?,1)",
                       (rid, name, price))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid


def _recipe(db_path, rid, menu_item_id, pairs):
    conn = get_conn(db_path)
    for name, cost, qty in pairs:
        cur = conn.execute("""INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active)
                              VALUES (?,?,?,?,1)""", (rid, name, "lb", cost))
        conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                     (menu_item_id, cur.lastrowid, qty))
    conn.commit()
    conn.close()


_SCHED_CSV = """date,day,employee,role,shift_start,shift_end,scheduled_hours,notes
2026-09-07,Monday,Sofia R.,Server,16:00,22:00,6.0,
2026-09-08,Tuesday,Sofia R.,Server,17:00,23:00,6.0,close
2026-09-07,Monday,Marcus T.,Cook,08:00,16:00,8.0,
"""


def _stored_schedule(db_path, rid, csv_text=_SCHED_CSV):
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled,
                                      hours_budget, labor_target, schedule_csv, summary_json)
        VALUES (?, '2026-09-07', '2026-09-13', 20, 24, 30, ?, '[]')
    """, (rid, csv_text))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


# ── Supplier orders ──────────────────────────────────────────────────────

def test_web_order_draft_groups_by_supplier(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _ingredient(db_path, rid, "Napkins")
    _login_as(monkeypatch, rid)
    d = client.get("/api/food-cost/order-draft").get_json()
    assert d["ok"] is True
    assert d["groups"][0]["supplier_email"] == "orders@fresh.test"
    assert [i["item"] for i in d["unassigned"]] == ["Napkins"]


def test_web_set_ingredient_supplier(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Salmon")
    _login_as(monkeypatch, rid)
    resp = client.post("/api/food-cost/ingredient-supplier",
                       json={"name": "Salmon", "supplier_name": "Sea Co", "supplier_email": "orders@sea.test"})
    assert resp.get_json()["ok"] is True
    row = get_conn(db_path).execute("SELECT supplier_email FROM ingredients WHERE name='Salmon'").fetchone()
    assert row["supplier_email"] == "orders@sea.test"


def test_web_send_order_emails_supplier_and_records_po(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _login_as(monkeypatch, rid)
    sent = {}
    monkeypatch.setattr("emails.send_supplier_order_email", lambda **kw: sent.update(kw))
    resp = client.post("/api/food-cost/send-order", json={})
    d = resp.get_json()
    assert d["ok"] is True
    assert d["sent"][0]["supplier_email"] == "orders@fresh.test"
    assert sent["to_email"] == "orders@fresh.test"
    orders = client.get("/api/food-cost/purchase-orders").get_json()["orders"]
    assert len(orders) == 1


def test_web_purchase_order_can_be_marked_received(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _login_as(monkeypatch, rid)
    monkeypatch.setattr("emails.send_supplier_order_email", lambda **kw: None)
    client.post("/api/food-cost/send-order", json={})
    po_id = client.get("/api/food-cost/purchase-orders").get_json()["orders"][0]["id"]
    assert client.post(f"/api/food-cost/purchase-orders/{po_id}/received").get_json()["ok"] is True
    assert client.post(f"/api/food-cost/purchase-orders/{po_id}/received").status_code == 404


# ── Menu margins ─────────────────────────────────────────────────────────

def test_web_menu_profitability_costs_the_plate(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    burger = _menu_item(db_path, rid, "Burger", price=18.0)
    _recipe(db_path, rid, burger, [("Beef", 6.0, 0.5), ("Bun", 1.0, 1.0)])
    _login_as(monkeypatch, rid)
    d = client.get("/api/food-cost/menu-profitability").get_json()
    assert d["ok"] is True
    item = d["priced"][0]
    assert item["plate_cost"] == 4.0
    assert item["margin"] == 14.0


def test_web_set_menu_item_price(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    mid = _menu_item(db_path, rid, "Dish")
    _recipe(db_path, rid, mid, [("A", 5.0, 1.0)])
    _login_as(monkeypatch, rid)
    resp = client.post("/api/food-cost/menu-item-price", json={"menu_item_id": mid, "sell_price": 15.0})
    assert resp.get_json()["ok"] is True
    d = client.get("/api/food-cost/menu-profitability").get_json()
    assert d["priced"][0]["sell_price"] == 15.0


def test_web_pricing_cannot_reach_another_restaurants_menu(client, db_path, monkeypatch):
    mine = _restaurant(db_path)
    theirs = _restaurant(db_path, name="Other Co")
    their_item = _menu_item(db_path, theirs, "Their Dish", price=10.0)
    _login_as(monkeypatch, mine)
    resp = client.post("/api/food-cost/menu-item-price", json={"menu_item_id": their_item, "sell_price": 99.0})
    assert resp.status_code == 400
    price = get_conn(db_path).execute("SELECT sell_price FROM menu_items WHERE id=?", (their_item,)).fetchone()["sell_price"]
    assert price == 10.0


# ── Schedule publishing ──────────────────────────────────────────────────

def test_web_staff_contacts_come_from_the_schedule(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _stored_schedule(db_path, rid)
    _login_as(monkeypatch, rid)
    d = client.get("/api/labor/staff-contacts").get_json()
    assert d["ok"] is True
    assert {c["employee_name"] for c in d["contacts"]} == {"Sofia R.", "Marcus T."}


def test_web_publish_schedule_sends_to_staff_with_email(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _stored_schedule(db_path, rid)
    _login_as(monkeypatch, rid)
    client.post("/api/labor/staff-contacts", json={"employee_name": "Sofia R.", "email": "sofia@x.test"})
    sent = []
    monkeypatch.setattr("emails.send_staff_schedule_email", lambda **kw: sent.append(kw))
    resp = client.post("/api/labor/publish-schedule", json={})
    d = resp.get_json()
    assert d["ok"] is True
    assert [s["employee_name"] for s in d["sent"]] == ["Sofia R."]
    assert [u["employee_name"] for u in d["unreachable"]] == ["Marcus T."]
    assert len(sent) == 1 and sent[0]["to_email"] == "sofia@x.test"


def test_web_schedule_share_status_defaults_to_latest_schedule(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    sid = _stored_schedule(db_path, rid)
    _login_as(monkeypatch, rid)
    d = client.get("/api/labor/schedule-share-status").get_json()
    assert d["ok"] is True
    assert d["schedule_id"] == sid


def test_web_publish_schedule_share_status_reflects_a_real_open(client, db_path, monkeypatch):
    """The status endpoint reads the same schedule_shares rows the public
    /s/<token> page writes to on a real view — not a separate counter."""
    from models import mark_schedule_share_viewed
    rid = _restaurant(db_path)
    _stored_schedule(db_path, rid)
    _login_as(monkeypatch, rid)
    client.post("/api/labor/staff-contacts", json={"employee_name": "Sofia R.", "email": "sofia@x.test"})
    monkeypatch.setattr("emails.send_staff_schedule_email", lambda **kw: None)
    d = client.post("/api/labor/publish-schedule", json={}).get_json()
    token = get_conn(db_path).execute(
        "SELECT token FROM schedule_shares WHERE restaurant_id=? AND employee_name='Sofia R.'", (rid,)
    ).fetchone()["token"]
    mark_schedule_share_viewed(token, db_path=db_path)
    status = client.get(f"/api/labor/schedule-share-status?schedule_id={d['schedule_id']}").get_json()["status"]
    sofia = next(s for s in status if s["employee_name"] == "Sofia R.")
    assert sofia["viewed_at"] is not None
