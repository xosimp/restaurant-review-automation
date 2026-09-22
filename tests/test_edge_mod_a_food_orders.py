"""Supplier orders — the one Food Cost action that puts a real purchase order
in a supplier's inbox — at the edges the MOD audit found untested: more
items due than the page shows, two same-name ingredients, one supplier
typed two ways, the send cooldown, the draft_hash guard neither client
sends, the phone ignoring the owner's undo window, a queued send that
silently voids, PO numbering after a void, what the web order screen
leaves out, and raw exception text.

Web routes run through client_bp with a session cookie and the CSRF pair;
mobile routes through mobile_bp with a Bearer token from auth.create_session.
The supplier email itself is stubbed (emails.send_supplier_order_email).
Confirmed defects are strict xfails naming the finding."""
import os
import sys

import pytest
from flask import Flask

import auth
import client_api
import inventory
import mobile_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, get_conn

CSRF = "edge-mod-a-orders-csrf"



# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import cogs, delayed, demand, food_cost_intelligence, intraday, inventory, inventory_ledger  # noqa: E401,F401
import invoices, issues, marketing_signals, notify, ops, pos, push, recipes, reporter  # noqa: E401,F401
import staff_settings, strategy_jobs, toast, square, clover, webhooks  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    # Every repo module holding a get_conn: the real one by identity, and any
    # stale redirect an earlier test's lazy import bound (CLAUDE.md "Bound imports").
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
    from push import init_push
    init_push(db_path=db_path)


@pytest.fixture
def mailed(monkeypatch):
    import emails
    out = []
    monkeypatch.setattr(emails, "send_supplier_order_email", lambda **kw: out.append(kw) or {"id": "e1"})
    return out


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Order Co"), owner_email="o@order.test",
                                        module_inventory=1, **kw), db_path=db_path)


def _ingredient(db_path, rid, name, *, par=50, stock=1, usage=5, cost=4.0, supplier_name="Sysco",
                supplier_email="orders@sysco.test", category="Protein"):
    c = get_conn(db_path)
    cur = c.execute(
        "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost, case_size, "
        "current_stock, avg_daily_usage, last_order_qty, waste_last_week, is_active, supplier_name, supplier_email) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (rid, name, category, "lb", par, cost, 1, stock, usage, par, 0, supplier_name, supplier_email))
    c.commit(); c.close()
    return cur.lastrowid


@pytest.fixture
def apps(db_path):
    app = Flask(__name__, template_folder="/Users/simp/review_automation/templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _web(apps, db_path, rid):
    uid = create_user(rid, f"web{rid}", f"web{rid}@order.test", "pw123456", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    cl = apps.test_client()
    cl.set_cookie("csrf_js", CSRF)
    cl.set_cookie("session_token", create_session(uid, db_path=db_path))
    return lambda path, body=None: cl.post(path, json=body or {}, headers={"X-CSRF": CSRF})


def _mobile(apps, db_path, rid):
    uid = create_user(rid, f"mob{rid}", f"mob{rid}@order.test", "pw123456", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    token = create_session(uid, device_type="ios", db_path=db_path)
    cl = apps.test_client()
    return lambda path, body=None: cl.post(path, json=body or {}, headers={"Authorization": f"Bearer {token}"})


def _pos(db_path, rid):
    c = get_conn(db_path)
    rows = c.execute("SELECT po_number, supplier_email, status FROM purchase_orders WHERE restaurant_id=? "
                     "ORDER BY id", (rid,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── A5 order #16 / MOD-FC-1: every item that is due reaches the order ───────

@pytest.mark.xfail(strict=True, reason="MOD-FC-1: build_supplier_orders reads the display-truncated critical_low[:4]/reorder_soon[:6]")
def test_thirty_below_par_items_for_one_supplier_make_a_thirty_line_order(db_path):
    rid = _restaurant(db_path)
    for i in range(30):
        _ingredient(db_path, rid, f"Item {i:02d}")
    d = inventory.build_supplier_orders(rid)
    assert d["item_count"] == 30
    assert [len(g["items"]) for g in d["groups"]] == [30]


def test_three_below_par_items_are_all_on_the_order(db_path):
    rid = _restaurant(db_path)
    for i in range(3):
        _ingredient(db_path, rid, f"Item {i}")
    d = inventory.build_supplier_orders(rid)
    assert d["item_count"] == 3 and len(d["groups"][0]["items"]) == 3


# ── A5 order #17 / MOD-FC-2: same name at two suppliers ─────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-2: build_supplier_orders dedupes by display name; the second supplier's line is dropped")
def test_two_same_name_ingredients_at_two_suppliers_make_two_groups(db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Chicken Breast", supplier_name="Sysco", supplier_email="a@sysco.test")
    _ingredient(db_path, rid, "Chicken Breast", par=40, usage=4, supplier_name="US Foods", supplier_email="b@usfoods.test")
    d = inventory.build_supplier_orders(rid)
    assert sorted(g["supplier_email"] for g in d["groups"]) == ["a@sysco.test", "b@usfoods.test"]


# ── A5 order #18 / MOD-FC-3: one supplier, name typed two ways ──────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-3: groups are keyed on (supplier_name, email); 'Sysco'/'SYSCO' at one address make two POs")
def test_one_supplier_address_typed_with_different_name_casing_is_one_group(db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "A", supplier_name="Sysco", supplier_email="x@sysco.test")
    _ingredient(db_path, rid, "B", supplier_name="SYSCO", supplier_email="X@sysco.test")
    d = inventory.build_supplier_orders(rid)
    assert len(d["groups"]) == 1 and len(d["groups"][0]["items"]) == 2


# ── A5 order #19-#21 / MOD-FC-11: the send cooldown ─────────────────────────

def test_a_double_click_on_send_puts_one_po_in_the_suppliers_inbox(apps, db_path, mailed):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine")
    post = _web(apps, db_path, rid)
    first = post("/api/food-cost/send-order")
    second = post("/api/food-cost/send-order")
    assert first.status_code == 200 and second.status_code == 429
    assert len(_pos(db_path, rid)) == 1 and len(mailed) == 1


@pytest.mark.xfail(strict=True, reason="MOD-FC-11: the cooldown's check-and-set is not atomic; two request threads can both be allowed")
def test_two_simultaneous_sends_are_not_both_allowed_through_the_cooldown(monkeypatch):
    """Both request threads read the cooldown before either writes it — the
    interleaving the audit's probe hit 1 time in 2,000, forced here with a
    barrier inside the read so it happens every run."""
    import threading
    barrier = threading.Barrier(2)

    class ReadThenWait(dict):
        def get(self, *a, **k):
            v = dict.get(self, *a, **k)
            try:
                barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
            return v
    monkeypatch.setattr(client_api, "_order_send_last", ReadThenWait())
    results = []
    threads = [threading.Thread(target=lambda: results.append(client_api._order_send_allowed(1))) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert sorted(results) == [False, True]


@pytest.mark.xfail(strict=True, reason="MOD-FC-11: the cooldown is per restaurant, so the second supplier's Send button is refused for 60s")
def test_sending_supplier_a_then_supplier_b_inside_a_minute_sends_both(apps, db_path, mailed):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="a@fresh.test")
    _ingredient(db_path, rid, "Salmon", supplier_name="Sea Co", supplier_email="b@sea.test")
    post = _web(apps, db_path, rid)
    assert post("/api/food-cost/send-order", {"supplier_email": "a@fresh.test"}).status_code == 200
    r = post("/api/food-cost/send-order", {"supplier_email": "b@sea.test"})
    assert r.status_code == 200, r.get_json()
    assert sorted(m["to_email"] for m in mailed) == ["a@fresh.test", "b@sea.test"]


@pytest.mark.xfail(strict=True, reason="MOD-FC-11: the cooldown is spent before validation, so a 400 'nothing to order' blocks the fixed retry")
def test_a_refused_send_does_not_start_the_cooldown(apps, db_path, mailed):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Romaine", supplier_name=None, supplier_email=None)
    post = _web(apps, db_path, rid)
    assert post("/api/food-cost/send-order").status_code == 400
    c = get_conn(db_path)
    c.execute("UPDATE ingredients SET supplier_name='Fresh Co', supplier_email='a@fresh.test' WHERE id=?", (iid,))
    c.commit(); c.close()
    r = post("/api/food-cost/send-order")
    assert r.status_code == 200, r.get_json()


# ── A5 order #22 / MOD-FC-8: the draft_hash guard ───────────────────────────

def test_a_stale_draft_hash_is_refused_with_409(apps, db_path, mailed):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine")
    r = _web(apps, db_path, rid)("/api/food-cost/send-order", {"draft_hash": "not-the-current-one"})
    assert r.status_code == 409 and r.get_json()["stale"] is True
    assert mailed == []


@pytest.mark.parametrize("surface", [
    pytest.param("web", marks=pytest.mark.xfail(strict=True, reason="MOD-FC-8: the web send route only checks draft_hash when one is supplied")),
    pytest.param("mobile", marks=pytest.mark.xfail(strict=True, reason="MOD-FC-8: the mobile send route only checks draft_hash when one is supplied")),
])
def test_a_send_without_a_draft_hash_is_refused(apps, db_path, mailed, surface):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine")
    post = (_web if surface == "web" else _mobile)(apps, db_path, rid)
    path = "/api/food-cost/send-order" if surface == "web" else "/mobile/api/food-cost/send-order"
    r = post(path, {})
    assert r.status_code in (400, 409)
    assert mailed == []


def _js_function(name):
    html = open("templates/dashboard.html", encoding="utf-8").read()
    start = html.index("function " + name + "(")
    end = html.index("\n  function ", start + 10)
    return html[start:end]


@pytest.mark.xfail(strict=True, reason="MOD-FC-8: the web client's send body is {supplier_email} only — no draft_hash")
def test_the_web_send_body_carries_the_draft_hash():
    assert "draft_hash" in _js_function("sendSupplierOrder")


@pytest.mark.xfail(strict=True, reason="MOD-FC-8: the iOS SendBody encodes only supplier_email — no draft_hash")
def test_the_ios_send_body_carries_the_draft_hash():
    src = open("ios/CavnarAI/CavnarAI/Features/FoodCost/SupplierOrderViewModel.swift", encoding="utf-8").read()
    body = src[src.index("struct SendBody"):]
    body = body[:body.index("}") + 1]
    assert "draft" in body.lower()


# ── A5 order #23 / MOD-FC-9: the phone honours the undo window ──────────────

def test_the_web_send_honours_the_send_delay(apps, db_path, mailed):
    import delayed
    rid = _restaurant(db_path)
    models.update_restaurant(rid, {"send_delay_minutes": 5}, db_path=db_path)
    _ingredient(db_path, rid, "Romaine")
    r = _web(apps, db_path, rid)("/api/food-cost/send-order")
    assert r.status_code == 200 and r.get_json()["undo_minutes"] == 5
    assert mailed == [] and delayed.pending(rid, db_path=db_path)[0]["kind"] == "order_send"


@pytest.mark.xfail(strict=True, reason="MOD-FC-9: the mobile send route is a separate copy that never reads send_delay_minutes")
def test_the_mobile_send_honours_the_send_delay(apps, db_path, mailed):
    import delayed
    rid = _restaurant(db_path)
    models.update_restaurant(rid, {"send_delay_minutes": 5}, db_path=db_path)
    _ingredient(db_path, rid, "Romaine")
    r = _mobile(apps, db_path, rid)("/mobile/api/food-cost/send-order")
    assert r.status_code == 200
    assert mailed == []
    assert delayed.pending(rid, db_path=db_path)[0]["kind"] == "order_send"


# ── A5 order #24 / MOD-FC-10: a queued send that voids tells the owner ──────

def test_a_queued_send_whose_draft_changed_sends_nothing(db_path, mailed):
    import delayed
    from datetime import datetime, timedelta, timezone
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine")
    delayed.schedule(rid, "order_send", {"draft_hash": "stale", "supplier_email": "orders@sysco.test"}, 1, db_path=db_path)
    out = delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert out["failed"] == 1 and mailed == []


@pytest.mark.xfail(strict=True, reason="MOD-FC-10: a voided queued/auto order is stored as 'failed' and nothing tells the owner")
def test_a_queued_send_whose_draft_changed_notifies_the_owner(db_path, mailed, monkeypatch):
    import delayed
    import push
    from datetime import datetime, timedelta, timezone
    pushes = []
    monkeypatch.setattr(push, "fire_push", lambda rid, at, *a, **k: pushes.append((rid, at)))
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine")
    delayed.schedule(rid, "order_send", {"draft_hash": "stale", "supplier_email": "orders@sysco.test"}, 1, db_path=db_path)
    delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    c = get_conn(db_path)
    alerts = c.execute("SELECT alert_type FROM alert_log WHERE restaurant_id=?", (rid,)).fetchall()
    c.close()
    assert pushes or alerts


# ── A5 order #25 / MOD-FC-7: PO numbering after a void ──────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-7: PO numbers are COUNT(*)+1, so voiding a non-newest PO wedges numbering permanently")
def test_a_purchase_order_allocates_after_an_older_one_is_voided(db_path):
    rid = _restaurant(db_path)
    a = models.record_purchase_order(rid, "Sysco", "a@x.test", [{"item": "x", "qty": 1}], 10)
    models.record_purchase_order(rid, "US Foods", "b@x.test", [{"item": "y", "qty": 1}], 10)
    models.void_purchase_order(rid, a)
    third = models.record_purchase_order(rid, "Sysco", "a@x.test", [{"item": "x", "qty": 1}], 5)
    assert third not in (None, "PO-0002")


def test_voiding_the_newest_purchase_order_frees_its_number(db_path):
    rid = _restaurant(db_path)
    models.record_purchase_order(rid, "Sysco", "a@x.test", [], 10)
    b = models.record_purchase_order(rid, "Sysco", "a@x.test", [], 10)
    models.void_purchase_order(rid, b)
    assert models.record_purchase_order(rid, "Sysco", "a@x.test", [], 10) == "PO-0002"


# ── A5 order #26 / MOD-FC-20: the web order screen and unassigned items ─────

def test_the_draft_returns_unassigned_items_beside_the_groups(db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine")
    _ingredient(db_path, rid, "Napkins", supplier_name=None, supplier_email=None)
    d = inventory.build_supplier_orders(rid)
    assert len(d["groups"]) == 1 and [u["item"] for u in d["unassigned"]] == ["Napkins"]


@pytest.mark.xfail(strict=True, reason="MOD-FC-20: loadOrderDraft reads d.unassigned only when there are no groups")
def test_the_web_order_screen_renders_unassigned_items_when_groups_exist():
    fn = _js_function("loadOrderDraft")
    after_empty_branch = fn[fn.index("var html = ''"):]
    assert "unassigned" in after_empty_branch


# ── MOD-FC-25: raw exception text ───────────────────────────────────────────

@pytest.mark.parametrize("surface", [
    pytest.param("web", marks=pytest.mark.xfail(strict=True, reason="MOD-FC-25: 'Couldn't build the order: {e}' returns the raw exception text")),
    pytest.param("mobile", marks=pytest.mark.xfail(strict=True, reason="MOD-FC-25: the mobile draft route returns the raw exception text")),
])
def test_a_failed_order_build_shows_a_fixed_message_not_the_exception(apps, db_path, monkeypatch, surface):
    rid = _restaurant(db_path)

    def boom(*a, **k):
        raise KeyError("avg_daily_usage")
    monkeypatch.setattr(inventory, "build_supplier_orders", boom)
    if surface == "web":
        r = _web(apps, db_path, rid)("/api/food-cost/send-order")
    else:
        uid = create_user(rid, "m", "m@order.test", "pw123456", db_path=db_path)
        token = create_session(uid, device_type="ios", db_path=db_path)
        r = apps.test_client().get("/mobile/api/food-cost/order-draft", headers={"Authorization": f"Bearer {token}"})
    body = r.get_json()
    assert body["ok"] is False
    assert "avg_daily_usage" not in body["error"]
