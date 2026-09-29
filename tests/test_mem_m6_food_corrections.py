"""Memory audit 9/29/26, food_corrections (workstream M6): the owner's
corrections to orders, invoice matches, recipes and prices are taught back —
a per-item order correction (median sent ÷ drafted, shrunk, bounded, shown),
a supplier-scoped invoice alias written at apply and read first by propose,
the owner's confirmed recipes of the same dish type as examples, a
per-restaurant reprice acceptance ratio, and a suggested par increase after
two or more 86s in four weeks."""
import json
import sqlite3
import sys
import os
import types
from datetime import date, timedelta

import pytest
from flask import Flask

import invoices
import menu_intelligence as mi
import models
import ordering
import recipes
import rec_ledger
from models import Restaurant, create_restaurant

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import inventory, inventory_ledger, cogs, strategy_routes  # noqa: E401,F401  (imported before the redirect)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Food Co", owner_email="f@x.test", module_inventory=1, **kw),
                             db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _ingredient(db_path, rid, name, par=50, stock=1, usage=5, cost=4.0, unit="lb"):
    c = _conn(db_path)
    cur = c.execute(
        "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost, case_size, current_stock, "
        "avg_daily_usage, last_order_qty, waste_last_week, is_active, supplier_name, supplier_email) "
        "VALUES (?,?,?,?,?,?,1,?,?,?,0,1,'Sysco','orders@sysco.test')",
        (rid, name, "Protein", unit, par, cost, stock, usage, par))
    c.commit()
    iid = cur.lastrowid
    c.close()
    return iid


def _orders(db_path, rid, ing, drafted, sent, n=4, source="owner"):
    c = _conn(db_path)
    for i in range(n):
        c.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_name, supplier_email, items_json, "
                  "total_cost, status, source, draft_items_json, edited) VALUES (?,?,?,?,?,?,?,?,?,1)",
                  (rid, f"PO-{source}-{i}-{ing}", "Sysco", "orders@sysco.test",
                   json.dumps([{"ingredient_id": ing, "item": "Salmon", "qty": sent}] if sent else []),
                   100.0, "received", source, json.dumps([{"ingredient_id": ing, "item": "Salmon", "qty": drafted}])))
    c.commit()
    c.close()


# ── orders ───────────────────────────────────────────────────────────────────

def test_an_item_the_owner_keeps_cutting_carries_a_shrunk_bounded_factor(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon")
    _orders(db_path, rid, salmon, drafted=10, sent=7)
    got = ordering.order_corrections(rid)[salmon]
    # median 0.70, shrunk by 3 pseudo-orders over 4 real ones: 1 + (0.70 - 1) * 4/7.
    assert got["factor"] == 0.83 and got["orders"] == 4 and "70%" in got["basis"]


def test_too_few_orders_an_automatic_one_or_a_demo_account_teach_nothing(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon")
    _orders(db_path, rid, salmon, drafted=10, sent=7, n=2)
    _orders(db_path, rid, salmon, drafted=10, sent=2, n=4, source="automatic")
    assert ordering.order_corrections(rid) == {}
    demo = _rid(db_path, is_demo=1)
    tuna = _ingredient(db_path, demo, "Tuna")
    _orders(db_path, demo, tuna, drafted=10, sent=5)
    assert ordering.order_corrections(demo) == {}


def test_a_corrected_draft_sent_unchanged_confirms_the_factor_rather_than_undoing_it(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon")
    c = _conn(db_path)
    for i in range(4):
        c.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_email, items_json, total_cost, status, "
                  "source, draft_items_json) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, f"PO-{i}", "orders@sysco.test", json.dumps([{"ingredient_id": salmon, "qty": 7}]), 1.0, "sent",
                   "owner", json.dumps([{"ingredient_id": salmon, "qty": 7, "base_qty": 10}])))
    c.commit()
    c.close()
    assert ordering.order_corrections(rid)[salmon]["median"] == 0.7


def test_the_order_draft_is_drafted_the_way_the_owner_sends_it_and_says_so(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=50, stock=1, usage=5)
    _orders(db_path, rid, salmon, drafted=10, sent=7)
    draft = inventory.build_supplier_orders(rid)
    line = next(l for g in draft["groups"] for l in g["items"] if l["ingredient_id"] == salmon)
    assert line["base_qty"] > line["qty"] and line["qty"] == round(line["base_qty"] * 0.83)
    assert line["owner_adjusted"]["factor"] == 0.83 and "70%" in line["owner_adjusted"]["basis"]


# ── invoices ─────────────────────────────────────────────────────────────────

EXTRACTED = {"supplier": "Sysco Chicago", "invoice_date": "2026-09-20", "invoice_total": 35.0,
             "lines": [{"description": "CHKN BRST BNLS 4/10#", "quantity": 10, "unit": "lb", "unit_price": 3.5,
                        "line_total": 35.0}]}


def _import(db_path, rid, proposal):
    c = _conn(db_path)
    cur = c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, image_sha, lines_json) "
                    "VALUES (?,?,?,?,?)", (rid, proposal["supplier"], proposal["invoice_date"],
                                           f"sha{c.execute('SELECT COUNT(*) FROM invoice_imports').fetchone()[0]}",
                                           json.dumps({"lines": proposal["lines"]})))
    c.commit()
    iid = cur.lastrowid
    c.close()
    return iid


def test_the_owners_match_for_a_line_is_remembered_for_that_supplier(db_path):
    rid = _rid(db_path)
    breast = _ingredient(db_path, rid, "Chicken Breast", cost=3.2)
    _ingredient(db_path, rid, "Chicken Thigh", cost=2.1)
    first = invoices.propose(rid, EXTRACTED)
    assert first["lines"][0]["ingredient_id"] is None                  # the name matcher can't read it
    out = invoices.apply(rid, _import(db_path, rid, first), [{"index": 0, "ingredient_id": breast, "unit_cost": 3.5}],
                         user_id=1)
    assert out["ok"]
    again = invoices.propose(rid, EXTRACTED)["lines"][0]
    assert again["ingredient_id"] == breast and again["matched_by"] == "your_match"
    # Another supplier's line with the same words is not this supplier's match.
    other = invoices.propose(rid, dict(EXTRACTED, supplier="US Foods"))["lines"][0]
    assert other["ingredient_id"] is None


def test_the_rules_automatic_apply_teaches_no_match(db_path):
    rid = _rid(db_path)
    breast = _ingredient(db_path, rid, "Chicken Breast", cost=3.2)
    first = invoices.propose(rid, EXTRACTED)
    invoices.apply(rid, _import(db_path, rid, first), [{"index": 0, "ingredient_id": breast, "unit_cost": 3.5}],
                   auto=True)
    assert invoices.propose(rid, EXTRACTED)["lines"][0]["ingredient_id"] is None


# ── recipes ──────────────────────────────────────────────────────────────────

def _dish(db_path, rid, name, lines=(), source=None):
    c = _conn(db_path)
    mid = c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, name)).lastrowid
    for ing, qty in lines:
        c.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit, source) VALUES (?,?,?,?)",
                  (mid, ing, qty, source))
    c.commit()
    c.close()
    return mid


def test_the_recipe_prompt_shows_the_owners_confirmed_recipes_of_the_same_kind(db_path, monkeypatch):
    import ai_guard
    rid = _rid(db_path)
    mozz = _ingredient(db_path, rid, "Mozzarella", unit="oz")
    dough = _ingredient(db_path, rid, "Pizza Dough", unit="each")
    _ingredient(db_path, rid, "Basil", unit="oz")
    _dish(db_path, rid, "Margherita Pizza", [(mozz, 6), (dough, 1)])
    _dish(db_path, rid, "Caesar Salad", [(mozz, 1)])
    pep = _dish(db_path, rid, "Pepperoni Pizza")
    ex = recipes.confirmed_examples(rid, "pizza", exclude_item_id=pep)
    assert [e["dish"] for e in ex] == ["Margherita Pizza"]
    seen = []
    msg = types.SimpleNamespace(content=[types.SimpleNamespace(
        type="text", text=json.dumps({"ingredients": [{"name": "Mozzarella", "qty": 6, "unit": "oz",
                                                       "confidence": "medium"}], "note": None}))],
        stop_reason="end_turn")
    import ai_utils
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda client, **kw: seen.append(kw["messages"][0]["content"])
                        or msg)
    out = recipes.draft_missing(rid, client=object(), items=[{"id": pep, "name": "Pepperoni Pizza"}])
    assert out["drafted"] == 1
    assert "Margherita Pizza: Mozzarella 6 oz; Pizza Dough 1 each" in seen[-1]
    assert ai_guard.UNTRUSTED_OPEN in seen[-1] and "Caesar" not in seen[-1]


# ── repricing ────────────────────────────────────────────────────────────────

def _decisions(db_path, rid, chosen, n=3):
    c = _conn(db_path)
    for i in range(n):
        c.execute("INSERT INTO reprice_decisions (restaurant_id, menu_item_id, dish, old_price, suggested_price, "
                  "chosen_price, source) VALUES (?,?,?,?,?,?,?)", (rid, i, f"Dish {i}", 20.0, 24.0, chosen, "one_tap"))
    c.commit()
    c.close()


def test_the_owners_usual_share_of_a_suggested_rise_is_measured(db_path):
    rid = _rid(db_path)
    assert mi.reprice_acceptance(rid)["ratio"] is None
    _decisions(db_path, rid, chosen=22.0)
    acc = mi.reprice_acceptance(rid)
    # Half the rise each time, shrunk toward 1 by 3 pseudo-decisions over 3.
    assert acc["ratio"] == 0.75 and acc["decisions"] == 3 and "50%" in acc["basis"]


def test_a_suggestion_carries_what_this_owners_usual_choice_would_be_and_recover():
    acc = {"ratio": 0.5, "decisions": 4, "basis": "b"}
    [s] = mi.apply_owner_ratio([{"sell_price": 20.0, "suggested_price": 24.0, "monthly_margin_lost": 300.0}], acc)
    assert s["typical_price"] == 22.0 and s["typical_monthly"] == 150.0 and s["owner_ratio"] == acc
    assert s["suggested_price"] == 24.0 and s["monthly_margin_lost"] == 300.0     # the restore price stands
    [none] = mi.apply_owner_ratio([{"sell_price": 20.0, "suggested_price": 24.0}], {"ratio": None})
    assert none["owner_ratio"] is None and "typical_price" not in none


# ── 86s raise the par ────────────────────────────────────────────────────────

def _eighty_six(db_path, rid, ing, days_ago):
    c = _conn(db_path)
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source, "
              "note) VALUES (?,?,'recount',0,?,'closeout','86''d at close')", (rid, ing, d))
    c.commit()
    c.close()


def test_two_86s_in_four_weeks_suggest_a_higher_par(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=20, usage=3)
    _eighty_six(db_path, rid, salmon, 3)
    assert ordering.par_suggestions(rid, today=date.today()) == []
    _eighty_six(db_path, rid, salmon, 10)
    _eighty_six(db_path, rid, salmon, 40)                                      # outside the four weeks
    [s] = ordering.par_suggestions(rid, today=date.today())
    assert (s["ingredient_id"], s["par"], s["suggested_par"], s["times"]) == (salmon, 20, 25, 2)
    assert s["key"] == "raise_par:Salmon" and "86'd at close on 2 nights" in s["basis"]


def test_accepting_a_par_suggestion_sets_the_par_and_answers_it(db_path, monkeypatch):
    import change_log
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=20, usage=3)
    _eighty_six(db_path, rid, salmon, 3)
    _eighty_six(db_path, rid, salmon, 10)
    logged = []
    monkeypatch.setattr(change_log, "record", lambda *a, **k: logged.append((a, k)))
    owner = {"id": 1, "role": "client", "restaurant_id": rid}
    app = Flask(__name__)
    with app.test_request_context("/", method="GET"):
        listed, st = strategy_routes._do_par_suggestions(owner)
    assert st == 200 and [s["ingredient_id"] for s in listed["suggestions"]] == [salmon]
    with app.test_request_context("/", method="POST", json={}):
        out, st = strategy_routes._do_par_accept(owner, salmon)
    assert st == 200 and out["par"] == 25
    c = _conn(db_path)
    assert c.execute("SELECT par_level FROM ingredients WHERE id=?", (salmon,)).fetchone()[0] == 25
    c.close()
    assert logged and logged[0][0][1:5] == ("ingredient", "par_level", 20, 25)
    with app.test_request_context("/", method="GET"):
        assert strategy_routes._do_par_suggestions(owner)[0]["suggestions"] == []     # answered: gone


def test_86s_from_before_the_par_was_raised_never_raise_it_again(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=20, usage=3)
    _eighty_six(db_path, rid, salmon, 5)
    _eighty_six(db_path, rid, salmon, 10)
    key = rec_ledger.rec_key("raise_par", "Salmon")
    rec_ledger.present(rid, key, "food", "food", title="Raise salmon par", db_path=db_path)
    rec_ledger.implemented(rid, key, "food", user_id=1, meta={"module": "food"}, db_path=db_path)
    c = _conn(db_path)
    c.execute("UPDATE rec_instances SET implemented_at=date('now', '-3 days'), silenced_until=NULL WHERE key=?", (key,))
    c.commit()
    c.close()
    assert ordering.par_suggestions(rid, today=date.today()) == []
    _eighty_six(db_path, rid, salmon, 2)
    _eighty_six(db_path, rid, salmon, 1)
    [s] = ordering.par_suggestions(rid, today=date.today())
    assert s["times"] == 2 and "since the par was last set" in s["basis"]     # only the nights since it was raised


# ── the par's own change is the cutoff, however it changed (lead fix 9/29/26) ──

def _set_par_changed(db_path, iid, days_ago):
    """Pin the stamp to a clear past day: the cutoff is the restaurant's local
    day of the change, and a test must not straddle a timezone's midnight."""
    c = _conn(db_path)
    c.execute("UPDATE ingredients SET par_changed_at=datetime('now', ?) WHERE id=?", (f"-{days_ago} days", iid))
    c.commit()
    c.close()


def _par_changed_at(db_path, iid):
    c = _conn(db_path)
    v = c.execute("SELECT par_changed_at FROM ingredients WHERE id=?", (iid,)).fetchone()[0]
    c.close()
    return v


def test_a_par_raised_by_hand_ends_the_old_86s(db_path):
    """No raise_par answer exists — the owner (or an admin through view-as,
    whose implement M1 records with no implemented_at) raised the par on the
    ingredient itself. The 86s measured against the old par stop counting."""
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=20, usage=3)
    _eighty_six(db_path, rid, salmon, 10)
    _eighty_six(db_path, rid, salmon, 8)
    assert [s["ingredient_id"] for s in ordering.par_suggestions(rid, today=date.today())] == [salmon]
    assert _par_changed_at(db_path, salmon) is None
    assert inventory_ledger.update_ingredient(rid, salmon, par_level=26)
    assert _par_changed_at(db_path, salmon) is not None                        # the edit stamped it
    _set_par_changed(db_path, salmon, 6)
    assert ordering.par_suggestions(rid, today=date.today()) == []             # both 86s predate the new par
    _eighty_six(db_path, rid, salmon, 4)
    _eighty_six(db_path, rid, salmon, 2)
    [s] = ordering.par_suggestions(rid, today=date.today())                    # two new ones at the new par
    assert s["times"] == 2 and s["par"] == 26 and "since the par was last set" in s["basis"]


def test_saving_the_same_par_is_not_a_change(db_path):
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=20, usage=3)
    assert inventory_ledger.update_ingredient(rid, salmon, par_level=20, unit_cost=4.5)
    assert _par_changed_at(db_path, salmon) is None
    assert inventory_ledger.update_ingredient(rid, salmon, unit_cost=4.8)       # par not in the edit at all
    assert _par_changed_at(db_path, salmon) is None


def test_a_par_changed_by_the_inventory_sync_ends_the_old_86s(db_path, monkeypatch):
    import inventory_sync
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon", par=20, usage=3)
    _eighty_six(db_path, rid, salmon, 10)
    _eighty_six(db_path, rid, salmon, 8)
    monkeypatch.setattr(inventory_sync, "get_conn", models.get_conn, raising=False)
    inventory_sync.apply_inventory(rid, "test", {"items": [{"name": "Salmon", "par_level": 20}]})
    assert _par_changed_at(db_path, salmon) is None                             # same par: no change
    inventory_sync.apply_inventory(rid, "test", {"items": [{"name": "Salmon", "par_level": 30}]})
    assert _par_changed_at(db_path, salmon) is not None
    _set_par_changed(db_path, salmon, 6)
    assert ordering.par_suggestions(rid, today=date.today()) == []


def test_an_admins_apply_through_view_as_teaches_no_match(db_path):
    rid = _rid(db_path)
    breast = _ingredient(db_path, rid, "Chicken Breast", cost=3.2)
    first = invoices.propose(rid, EXTRACTED)
    invoices.apply(rid, _import(db_path, rid, first), [{"index": 0, "ingredient_id": breast, "unit_cost": 3.5}],
                   user_id=1, authority="admin")
    assert invoices.propose(rid, EXTRACTED)["lines"][0]["ingredient_id"] is None


# ── view-as: support at work is never the owner's habit (memory audit M1) ────

VIEW_AS = {"acting_admin_id": 99, "acting_admin": "support", "acting_admin_role": "admin"}


def test_an_admins_reprice_through_view_as_is_kept_and_never_the_owners_ratio(db_path):
    """reprice_decisions carries whose choice it was: a view-as request (g.
    view_as, permissions.acting_via) is an admin's, kept and left out of the
    owner's ratio."""
    import flask
    rid = _rid(db_path)
    app = Flask(__name__)
    with app.test_request_context("/", method="POST"):
        flask.g.view_as = dict(VIEW_AS)
        for i in range(3):
            assert mi.record_price_change(rid, 100 + i, 20.0, 24.0, user_id=5, source="one_tap",
                                          suggestion={"dish": f"Admin Dish {i}", "suggested_price": 24.0},
                                          db_path=db_path)
    c = _conn(db_path)
    assert [r[0] for r in c.execute("SELECT authority FROM reprice_decisions WHERE restaurant_id=?", (rid,))] \
        == ["admin"] * 3
    c.close()
    assert mi.reprice_acceptance(rid)["ratio"] is None
    _decisions(db_path, rid, chosen=22.0)
    acc = mi.reprice_acceptance(rid)
    assert acc["decisions"] == 3 and acc["ratio"] == 0.75


def test_an_admins_supplier_send_through_view_as_is_never_the_owners_order_record(db_path, monkeypatch):
    """purchase_orders carries whose send it was: an admin's through view-as
    teaches no order correction and earns the supplier no trust."""
    import client_api
    import emails
    import outcomes
    rid = _rid(db_path)
    salmon = _ingredient(db_path, rid, "Salmon")
    # Nothing is sent: the email is stubbed as delivered (a failed one voids
    # the PO row), and the audit line and the observed tracker are no-ops.
    monkeypatch.setattr(emails, "send_supplier_order_email", lambda **kw: types.SimpleNamespace(ok=True))
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    monkeypatch.setattr(outcomes, "observe", lambda *a, **k: None)
    viewer = {"id": 5, "role": "client", "restaurant_id": rid, "acting_admin_id": 99,
              "acting_admin_role": "admin", "device_type": "admin-view-as"}
    group = {"supplier_email": "orders@sysco.test", "supplier_name": "Sysco", "total_cost": 10.0,
             "items": [{"ingredient_id": salmon, "item": "Salmon", "qty": 7}], "draft_hash": "vh1"}
    client_api._send_supplier_orders(rid, models.get_restaurant(rid, db_path), [group], viewer)
    c = _conn(db_path)
    assert c.execute("SELECT authority FROM purchase_orders WHERE restaurant_id=?", (rid,)).fetchone()[0] == "admin"
    c.close()
    c = _conn(db_path)
    for i in range(4):
        c.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_name, supplier_email, items_json, "
                  "total_cost, status, source, draft_items_json, edited, authority) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                  (rid, f"PO-adm-{i}", "Sysco", "orders@sysco.test",
                   json.dumps([{"ingredient_id": salmon, "item": "Salmon", "qty": 7}]), 100.0, "received", "owner",
                   json.dumps([{"ingredient_id": salmon, "item": "Salmon", "qty": 10}]), "admin"))
    c.commit()
    c.close()
    assert ordering.order_corrections(rid) == {}
    assert ordering.supplier_trust(rid, "orders@sysco.test")["orders"] == 0
    _orders(db_path, rid, salmon, drafted=10, sent=7)
    assert ordering.order_corrections(rid)[salmon]["orders"] == 4
    assert ordering.supplier_trust(rid, "orders@sysco.test")["orders"] == 4
