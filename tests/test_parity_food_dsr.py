"""iOS parity round (10/7/26), Food Cost and the nightly report — the server
halves.

  #8   a count-sheet save for a restaurant whose inventory system has synced
       is refused (the phone could overwrite synced counts); the tracker
       payload says where its prices come from.
  #23  a waste line and a delivery replayed from the phone's offline queue
       (same idempotency key, in the header or the body) land once.
  #77  one waste-trend body for the web card, the phone's new route and the
       phone's older 8-week route.
"""
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import client_api
import inventory_ledger as il  # noqa: F401  (bound at collection, before any redirect)
import mobile_api
import models
import strategy_routes
from models import Restaurant, create_restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    return db_path


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Parity Co", owner_email="p@x.com", module_inventory=1),
                             db_path=db_path)


def _ingredient(db_path, rid, name="Basil", stock=10.0):
    c = models.get_conn(db_path)
    iid = c.execute("INSERT INTO ingredients (restaurant_id,name,unit,unit_cost,par_level,current_stock,"
                    "avg_daily_usage,is_active) VALUES (?,?,?,?,10,?,1,1)", (rid, name, "lb", 2.0, stock)).lastrowid
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id,ingredient_id,event_type,qty,event_date,source) "
              "VALUES (?,?,'recount',?,?,'migration')", (rid, iid, stock, (date.today() - timedelta(days=3)).isoformat()))
    c.commit()
    c.close()
    return iid


def _user(rid):
    return {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False, "username": "owner"}


def _call(fn, user, *args, body=None, headers=None):
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json=body, headers=headers or {}):
        return fn(user, *args)


def _events(db_path, rid, kind):
    c = models.get_conn(db_path)
    try:
        return c.execute("SELECT COUNT(*) AS n FROM ingredient_stock_events WHERE restaurant_id=? AND event_type=?",
                         (rid, kind)).fetchone()["n"]
    finally:
        c.close()


# ── #8: synced counts are the inventory system's ────────────────────────────

def _synced(monkeypatch, on=True):
    import inventory_sync
    monkeypatch.setattr(inventory_sync, "status", lambda rid, *a, **k: (
        {"provider": "marketman", "label": "MarketMan", "connected": True, "synced": True,
         "synced_at": "2026-10-06T05:00:00", "error": None} if on else
        {"provider": None, "label": None, "connected": False, "synced": False, "synced_at": None, "error": None}))


def test_a_count_sheet_save_is_refused_once_an_inventory_system_has_synced(db, monkeypatch):
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    _synced(monkeypatch, on=True)
    monkeypatch.setattr(strategy_routes, "_enters_food", lambda u: True)
    before = _events(db, rid, "recount")
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid),
                        body={"items": [{"ingredient_id": iid, "counted": 4}]})
    assert status == 409
    assert out["ok"] is False and out["code"] == "inventory_synced"
    assert "MarketMan" in out["error"]
    assert _events(db, rid, "recount") == before, "nothing was written over the synced count"


def test_a_count_sheet_save_is_written_when_nothing_has_synced(db, monkeypatch):
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    _synced(monkeypatch, on=False)
    monkeypatch.setattr(strategy_routes, "_enters_food", lambda u: True)
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid),
                        body={"items": [{"ingredient_id": iid, "counted": 4}]})
    assert status == 200 and out["written"] == 1


def test_an_unreadable_sync_status_never_blocks_a_hand_count(db, monkeypatch):
    import inventory_sync
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    monkeypatch.setattr(inventory_sync, "status", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(strategy_routes, "_enters_food", lambda u: True)
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid),
                        body={"items": [{"ingredient_id": iid, "counted": 4}]})
    assert status == 200


def test_the_tracker_says_where_its_prices_come_from(db, monkeypatch):
    rid = _restaurant(db)
    _synced(monkeypatch, on=True)
    out, status = client_api._do_food_cost_tracker(rid)
    assert status == 200
    assert out["source"]["synced"] is True and out["source"]["label"] == "MarketMan"


# ── #23: queued waste and receiving land once ───────────────────────────────

def test_a_waste_line_replayed_with_its_body_key_is_logged_once(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    body = {"ingredient_id": iid, "qty": 2, "reason": "spoiled", "idempotency_key": "waste-abc"}
    first, s1 = _call(client_api._do_log_waste_once, _user(rid), body, body=body)
    again, s2 = _call(client_api._do_log_waste_once, _user(rid), body, body=body)
    assert s1 == s2 == 200
    assert first["ok"] is True and again["ok"] is True
    assert _events(db, rid, "waste") == 1


def test_a_waste_line_with_a_header_key_is_logged_once_and_a_new_key_logs_again(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    body = {"ingredient_id": iid, "qty": 1, "reason": "dropped"}
    _call(client_api._do_log_waste_once, _user(rid), body, body=body, headers={"Idempotency-Key": "k1"})
    _call(client_api._do_log_waste_once, _user(rid), body, body=body, headers={"Idempotency-Key": "k1"})
    assert _events(db, rid, "waste") == 1
    _call(client_api._do_log_waste_once, _user(rid), body, body=body, headers={"Idempotency-Key": "k2"})
    assert _events(db, rid, "waste") == 2


def test_a_waste_line_without_a_key_is_not_deduplicated(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    body = {"ingredient_id": iid, "qty": 1, "reason": "dropped"}
    _call(client_api._do_log_waste_once, _user(rid), body, body=body)
    _call(client_api._do_log_waste_once, _user(rid), body, body=body)
    assert _events(db, rid, "waste") == 2


def test_a_replayed_receive_answers_with_the_first_result_not_already_received(db):
    rid = _restaurant(db)
    a = _ingredient(db, rid, "Beef", stock=0)
    models.record_purchase_order(rid, "Sysco", "s@x.com", [{"item": "Beef", "qty": 4, "ingredient_id": a}], 40,
                                 db_path=db)
    po = models.get_conn(db).execute("SELECT id FROM purchase_orders WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    body = {"idempotency_key": "recv-1"}
    first, s1 = _call(client_api._do_receive_po_once, _user(rid), po, body, body=body)
    again, s2 = _call(client_api._do_receive_po_once, _user(rid), po, body, body=body)
    assert s1 == 200 and first["ok"] is True
    assert (s2, again) == (s1, first)
    assert _events(db, rid, "receiving") == 1
    # Without the key the claim still holds: a second receive is refused.
    out, status = _call(client_api._do_receive_po_once, _user(rid), po, {}, body={})
    assert status == 404


def test_both_clients_route_waste_and_receiving_through_the_guard():
    import inspect
    assert "_do_log_waste_once(" in inspect.getsource(mobile_api.mobile_log_waste)
    assert "_do_log_waste_once(" in inspect.getsource(client_api.log_waste)
    assert "_do_receive_po_once(" in inspect.getsource(mobile_api.mobile_receive_purchase_order)
    assert "_do_receive_po_once(" in inspect.getsource(client_api.receive_purchase_order)


# ── #77: one waste-trend body ───────────────────────────────────────────────

def _waste_week(db_path, rid, week_end, waste):
    import json
    c = models.get_conn(db_path)
    import waste_trend
    c.execute(waste_trend._SCHEMA)
    c.execute("INSERT INTO inventory_history (restaurant_id, week_end, waste_json, items_json) VALUES (?,?,?,?)",
              (rid, week_end, json.dumps({"total_waste_cost": waste}), "[]"))
    c.commit()
    c.close()


def _phone(db_path, rid, monkeypatch):
    user = {"id": 1, "restaurant_id": rid, "is_admin": 0, "role": "owner", "username": "erik"}
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def test_the_phone_reads_the_web_cards_payload_with_ranges_and_observations(db, monkeypatch):
    import inventory
    rid = _restaurant(db)
    for i, w in enumerate([200.0, 180.0, 150.0]):
        _waste_week(db, rid, (date(2026, 9, 2) + timedelta(days=7 * i)).isoformat(), w)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([], False))
    phone = _phone(db, rid, monkeypatch)
    d = phone.get("/mobile/api/food-cost/waste-trend?range=8w", headers={"Authorization": "Bearer t"}).get_json()
    assert d["ok"] is True, d
    web, _ = client_api._do_waste_trend(rid, "8w")
    for key in ("range", "ranges", "weeks", "observations", "stats"):
        assert d[key] == web[key], key
    assert d["ranges"] == ["8w"]
    assert [w["waste"] for w in d["weeks"]] == [200.0, 180.0, 150.0]
    assert d["observations"], "the observations travel to the phone"
    # Whose target the line is: never "your target" when the owner set none.
    assert d["target"]["label"] != "your target"
    # The older 8-week route reads the same builder and keeps its shape.
    old = phone.get("/mobile/api/food-cost/trend", headers={"Authorization": "Bearer t"}).get_json()
    assert old["ok"] is True
    assert [w["waste"] for w in old["weeks"]] == [200.0, 180.0, 150.0]
    assert set(old["weeks"][0]) == {"label", "start", "end", "waste"}
    assert old["target"] == d["target"]


def test_an_unknown_range_resolves_to_one_the_history_fills(db, monkeypatch):
    import inventory
    rid = _restaurant(db)
    _waste_week(db, rid, "2026-09-16", 150.0)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([], False))
    out, status = client_api._do_waste_trend(rid, "26w")
    assert status == 200 and out["range"] == "8w"
