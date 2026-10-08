"""Blind re-audit (10/8/26) of the Food Cost and Daily Sales Report fix
round — the server halves.

  #1  a count parked offline replays with its ledger_mark and counted_at:
      what was posted after the count was taken (a delivery, a waste line,
      the day's depletion) is applied after it, never erased by it or booked
      as inferred waste; a delivery posted between opening the sheet and
      taking the count is asked about, never guessed; a later count of the
      same item wins, so a replay landing twice is harmless (the F2-7 class).
  #4  a counts-only login (FOOD_COST_ENTER without FOOD_COST_VIEW) logs waste
      and is never answered with the week's waste dollars — nor from a
      stored idempotent answer.
  #5  once an inventory system has synced, a hand edit of what it owns is
      refused on every path: suppliers, recipe drafts/accept/import/scan, the
      admin recount. The close-out's 86 is kept (see test docstring).
  #7  a receive replayed after the order was received another way says so
      with a code the phone's queue reads as done.
"""
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

import admin_routes
import auth
import client_api
import inventory_ledger
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
    monkeypatch.setattr(strategy_routes, "_enters_food", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: date.today())
    _synced(monkeypatch, on=False)
    return db_path


def _synced(monkeypatch, on=True):
    import inventory_sync
    monkeypatch.setattr(inventory_sync, "status", lambda rid, *a, **k: (
        {"provider": "backoffice", "label": "Back Office", "connected": True, "synced": True,
         "synced_at": "2026-10-06T05:00:00", "error": None} if on else
        {"provider": None, "label": None, "connected": False, "synced": False, "synced_at": None, "error": None}))


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Reaudit Co", owner_email="r@x.com", module_inventory=1),
                             db_path=db_path)


TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _ingredient(db_path, rid, name="Salmon", stock=10.0):
    c = models.get_conn(db_path)
    iid = c.execute("INSERT INTO ingredients (restaurant_id,name,unit,unit_cost,par_level,current_stock,"
                    "avg_daily_usage,is_active) VALUES (?,?,?,?,10,?,1,1)", (rid, name, "lb", 2.0, stock)).lastrowid
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id,ingredient_id,event_type,qty,event_date,source,"
              "created_at) VALUES (?,?,'recount',?,?,'migration',datetime('now','-3 days'))",
              (rid, iid, stock, (date.today() - timedelta(days=3)).isoformat()))
    c.commit()
    c.close()
    inventory_ledger.recompute_rollups(rid, iid)
    return iid


def _user(rid, role="client"):
    return {"id": 1, "restaurant_id": rid, "role": role, "is_admin": False, "username": "u"}


def _call(fn, user, *args, body=None, headers=None):
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", json=body, headers=headers or {}):
        return fn(user, *args)


def _mark(db_path, rid):
    c = models.get_conn(db_path)
    try:
        return c.execute("SELECT COALESCE(MAX(id),0) AS m FROM ingredient_stock_events WHERE restaurant_id=?",
                         (rid,)).fetchone()["m"]
    finally:
        c.close()


def _stamp(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _posted_at(db_path, event_id, minutes_ago):
    """Backdates an event's created_at — when the ledger heard about it."""
    c = models.get_conn(db_path)
    c.execute("UPDATE ingredient_stock_events SET created_at=datetime('now', ?) WHERE id=?",
              (f"-{int(minutes_ago)} minutes", event_id))
    c.commit()
    c.close()


def _stock(rid, iid):
    return next(r for r in inventory_ledger.list_ingredients(rid) if r["id"] == iid)["current_stock"]


def _inferred(db_path, rid, iid):
    c = models.get_conn(db_path)
    try:
        return c.execute("SELECT COALESCE(SUM(qty),0) AS q FROM ingredient_stock_events WHERE restaurant_id=? "
                         "AND ingredient_id=? AND event_type='waste' AND source='inferred'", (rid, iid)).fetchone()["q"]
    finally:
        c.close()


def _recounts(db_path, rid, iid, source="count_sheet"):
    c = models.get_conn(db_path)
    try:
        return c.execute("SELECT COUNT(*) AS n FROM ingredient_stock_events WHERE restaurant_id=? AND ingredient_id=? "
                         "AND event_type='recount' AND source=?", (rid, iid, source)).fetchone()["n"]
    finally:
        c.close()


def _replay(rid, iid, counted, mark, taken, **extra):
    body = {"items": [{"ingredient_id": iid, "counted": counted}], "date": TODAY,
            "ledger_mark": mark, "counted_at": taken, **extra}
    return _call(strategy_routes._do_count_sheet_save, _user(rid), body=body)


# ── #1: a parked count never erases what came after it ──────────────────────

def test_a_delivery_posted_while_the_count_waited_is_kept_and_never_called_waste(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    taken = _stamp(30)            # counted 6 in the walk-in, no signal
    inventory_ledger.record_receiving(rid, iid, 40, event_date=TODAY)   # truck, logged on the web since
    out, status = _replay(rid, iid, 6, mark, taken)
    assert status == 200 and out["written"] == 1
    assert _stock(rid, iid) == 46, "the delivery after the count is on top of it"
    assert _inferred(db, rid, iid) == 4, "only the count's own gap (10 expected, 6 counted) is waste — not the 40 lb"


def test_without_counted_at_the_same_replay_would_have_erased_the_delivery(db):
    """The bug, pinned: the old replay (no mark, no time) anchors on the late
    row and reads the delivery as waste."""
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    inventory_ledger.record_receiving(rid, iid, 40, event_date=TODAY)
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid),
                        body={"items": [{"ingredient_id": iid, "counted": 6}], "date": TODAY})
    assert status == 200
    assert _stock(rid, iid) == 6 and _inferred(db, rid, iid) == 44


def test_waste_logged_after_the_count_still_comes_off_the_stock(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    taken = _stamp(30)
    assert inventory_ledger.record_waste(rid, iid, 2, event_date=TODAY, reason="dropped")["ok"]
    out, status = _replay(rid, iid, 6, mark, taken)
    assert status == 200
    assert _stock(rid, iid) == 4, "6 counted, then 2 thrown out"
    assert _inferred(db, rid, iid) == 4


def test_the_days_depletion_posted_after_the_count_comes_off_it(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    taken = _stamp(30)
    inventory_ledger.record_depletion_from_sale(rid, iid, 3, TODAY)
    inventory_ledger.recompute_rollups(rid, iid)
    out, status = _replay(rid, iid, 6, mark, taken)
    assert status == 200
    assert _stock(rid, iid) == 3
    assert _inferred(db, rid, iid) == 4


def test_an_event_dated_before_the_count_day_is_left_to_the_count(db):
    """A delivery dated yesterday, logged late: today's count already holds it."""
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    taken = _stamp(30)
    inventory_ledger.record_receiving(rid, iid, 5, event_date=YESTERDAY)
    out, status = _replay(rid, iid, 14, mark, taken)
    assert status == 200
    assert _stock(rid, iid) == 14
    assert _inferred(db, rid, iid) == 1


def test_a_delivery_posted_before_the_count_was_taken_is_asked_about_not_guessed(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    ev = inventory_ledger.record_receiving(rid, iid, 40, event_date=TODAY)
    _posted_at(db, ev, 45)        # logged while the sheet was open, before the count
    taken = _stamp(30)
    before = _recounts(db, rid, iid)
    out, status = _replay(rid, iid, 6, mark, taken)
    assert status == 409 and out["needs_confirm"] is True
    assert out["deliveries"][0]["ingredient_id"] == iid and out["deliveries"][0]["qty"] == 40
    assert "open the count sheet" in out["error"], "the queued count's question says where to answer it"
    assert _recounts(db, rid, iid) == before, "nothing written until someone answers"
    # Answered from the restored draft: it came after the count.
    out, status = _replay(rid, iid, 6, mark, taken, deliveries="after")
    assert status == 200 and _stock(rid, iid) == 46 and _inferred(db, rid, iid) == 4


def test_a_delivery_asked_about_and_one_after_the_count_are_both_kept(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    ev = inventory_ledger.record_receiving(rid, iid, 20, event_date=TODAY)
    _posted_at(db, ev, 45)
    taken = _stamp(30)
    inventory_ledger.record_receiving(rid, iid, 40, event_date=TODAY)
    out, status = _replay(rid, iid, 26, mark, taken, deliveries="counted")
    assert status == 200
    assert _stock(rid, iid) == 66, "the first truck was in the count; the second came after it"
    assert _inferred(db, rid, iid) == 4


def test_a_later_count_of_the_same_item_wins_and_a_double_landing_is_harmless(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    other = _ingredient(db, rid, name="Lemons", stock=8)
    mark = _mark(db, rid)
    taken = _stamp(30)
    # Someone else counted salmon on the web after this phone's count.
    inventory_ledger.record_recount(rid, iid, 9, event_date=TODAY, source="count_sheet")
    body = {"items": [{"ingredient_id": iid, "counted": 6}, {"ingredient_id": other, "counted": 7}],
            "date": TODAY, "ledger_mark": mark, "counted_at": taken}
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid), body=body)
    assert status == 200 and out["written"] == 1 and out["superseded"] == 1
    assert _stock(rid, iid) == 9 and _stock(rid, other) == 7
    # The same queued write lands again (its first answer was lost): nothing moves.
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid), body=body)
    assert status == 200 and out["ok"] is True and out["written"] == 0 and out["superseded"] == 2
    assert _stock(rid, iid) == 9 and _stock(rid, other) == 7
    assert _recounts(db, rid, other) == 1


def test_counted_at_from_a_clock_running_ahead_is_clamped_and_one_without_a_zone_ignored(db):
    assert strategy_routes._count_taken_at("2099-01-01T00:00:00Z") <= datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")
    assert strategy_routes._count_taken_at("2026-10-08T12:00:00") is None
    assert strategy_routes._count_taken_at("9/21/26") is None
    assert strategy_routes._count_taken_at(None) is None
    assert strategy_routes._count_taken_at("2026-09-08T12:00:00-05:00") == "2026-09-08 17:00:00"


def test_a_live_save_without_counted_at_still_asks_about_every_delivery_since_the_mark(db):
    rid = _restaurant(db)
    iid = _ingredient(db, rid, stock=10)
    mark = _mark(db, rid)
    inventory_ledger.record_receiving(rid, iid, 40, event_date=TODAY)
    out, status = _call(strategy_routes._do_count_sheet_save, _user(rid),
                        body={"items": [{"ingredient_id": iid, "counted": 6}], "ledger_mark": mark})
    assert status == 409 and out["needs_confirm"] is True
    assert "does your count include it" in out["error"]


# ── #4: counts-only logins never see waste dollars ──────────────────────────

def _stub_analysis(monkeypatch):
    import inventory
    monkeypatch.setattr(inventory, "analysis_for", lambda rid: (
        [], True, {"total_waste_cost_week": 123.45, "benchmark_label": "High",
                   "waste_items": [{"item": "Salmon", "waste_cost": 22.0, "waste_pct": 4, "waste_last_week": 2,
                                    "unit": "lb"}]}))


def test_a_counts_only_manager_logs_waste_and_gets_no_dollar_figure(db, monkeypatch):
    import permissions as p
    assert p.has_permission({"role": "manager"}, p.FOOD_COST_ENTER)
    assert not p.has_permission({"role": "manager"}, p.FOOD_COST_VIEW)
    _stub_analysis(monkeypatch)
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    out, status = _call(client_api._do_log_waste_once, _user(rid, "manager"), {"ingredient_id": iid, "qty": 1},
                        body={"ingredient_id": iid, "qty": 1})
    assert status == 200 and out["ok"] is True and out["name"] == "Salmon"
    assert "waste" not in out
    assert "123" not in str(out) and "22.0" not in str(out)


def test_the_owner_still_gets_the_weeks_waste_in_place(db, monkeypatch):
    _stub_analysis(monkeypatch)
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    out, status = _call(client_api._do_log_waste_once, _user(rid), {"ingredient_id": iid, "qty": 1},
                        body={"ingredient_id": iid, "qty": 1})
    assert status == 200 and out["waste"]["week"] == 123.45


def test_a_stored_answer_is_stripped_of_dollars_for_a_counts_only_replay(db, monkeypatch):
    """_idempotent stores the answer per restaurant, not per login."""
    _stub_analysis(monkeypatch)
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    body = {"ingredient_id": iid, "qty": 1, "idempotency_key": "waste:shared"}
    owner, _ = _call(client_api._do_log_waste_once, _user(rid), body, body=body)
    assert "waste" in owner
    mgr, status = _call(client_api._do_log_waste_once, _user(rid, "manager"), body, body=body)
    assert status == 200 and mgr["ok"] is True and "waste" not in mgr


def test_the_web_toast_reads_the_dollar_summary_only_when_present():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8") as f:
        src = f.read()
    assert "(d.waste?' \\u2014 this week\\u2019s waste is now $'" in src
    assert "function fc2ApplyWaste(w){\n  if(!w)return;" in src


# ── #5: synced from an inventory system, hand edits are refused ─────────────

def test_a_supplier_change_is_refused_once_synced(db, monkeypatch):
    rid = _restaurant(db)
    _ingredient(db, rid)
    _synced(monkeypatch, on=True)
    fn = mobile_api.mobile_set_ingredient_supplier.__wrapped__
    resp = _call(fn, _user(rid), body={"name": "Salmon", "supplier_name": "Sea", "supplier_email": "s@x.com"})
    payload, status = resp
    assert status == 409 and payload.get_json()["code"] == "inventory_synced"
    assert "Back Office" in payload.get_json()["error"]
    row = models.get_conn(db).execute("SELECT supplier_name FROM ingredients WHERE restaurant_id=?",
                                      (rid,)).fetchone()
    assert row["supplier_name"] is None


def test_a_supplier_change_is_saved_when_nothing_has_synced(db):
    rid = _restaurant(db)
    _ingredient(db, rid)
    fn = mobile_api.mobile_set_ingredient_supplier.__wrapped__
    resp = _call(fn, _user(rid), body={"name": "Salmon", "supplier_name": "Sea", "supplier_email": "s@x.com"})
    assert resp.get_json()["ok"] is True


@pytest.mark.parametrize("fn,args", [
    ("_do_recipe_draft_now", ()),
    ("_do_recipe_draft_accept", (1,)),
    ("_do_recipes_import", ()),
    ("_do_recipe_scan", ()),
])
def test_recipe_writes_are_refused_once_synced(db, monkeypatch, fn, args):
    rid = _restaurant(db)
    _synced(monkeypatch, on=True)
    monkeypatch.setattr(strategy_routes, "_sees_food", lambda u: True)
    out, status = _call(getattr(strategy_routes, fn), _user(rid), *args, body={"menu": "Burger", "csv": "a,b"})
    assert status == 409 and out["code"] == "inventory_synced"
    assert out["error"].startswith("Recipes come from Back Office")


def test_the_admin_recount_is_refused_once_synced_and_written_otherwise(db, monkeypatch):
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    _synced(monkeypatch, on=True)
    with app.test_request_context(f"/admin/inventory/recount/{rid}/{iid}", method="POST", json={"counted_qty": 3}):
        resp, status = admin_routes.record_recount_route(rid, iid)
    assert status == 409 and resp.get_json()["code"] == "inventory_synced"
    assert _stock(rid, iid) == 10
    _synced(monkeypatch, on=False)
    with app.test_request_context(f"/admin/inventory/recount/{rid}/{iid}", method="POST", json={"counted_qty": 3}):
        resp = admin_routes.record_recount_route(rid, iid)
    assert resp.get_json()["ok"] is True and _stock(rid, iid) == 3


def test_the_closeout_86_still_zeroes_a_synced_item(db, monkeypatch):
    """Kept on purpose: an 86 is not a hand count competing with the synced
    one — it is the night's fact that the item ran out in service, recorded
    without inferring waste (infer_waste=False), and it is what puts the item
    at the top of tomorrow's order before the next sync's count re-anchors
    the ledger. Refusing it would leave a sold-out item reading as stocked."""
    import closeout
    rid = _restaurant(db)
    iid = _ingredient(db, rid)
    _synced(monkeypatch, on=True)
    rest = models.get_restaurant(rid)
    closeout._act_on(rid, TODAY, {"eighty_sixed": "Salmon"}, rest, db_path=db)
    assert _stock(rid, iid) == 0
    assert _inferred(db, rid, iid) == 0


# ── #7: a late receive replay is told the order was already received ────────

def test_a_receive_after_the_order_was_received_carries_already_received(db):
    rid = _restaurant(db)
    a = _ingredient(db, rid, "Beef", stock=0)
    models.record_purchase_order(rid, "Sysco", "s@x.com", [{"item": "Beef", "qty": 4, "ingredient_id": a}], 40,
                                 db_path=db)
    po = models.get_conn(db).execute("SELECT id, po_number FROM purchase_orders WHERE restaurant_id=?",
                                     (rid,)).fetchone()
    out, status = _call(client_api._do_receive_po_once, _user(rid), po["id"], {"idempotency_key": "web-1"},
                        body={"idempotency_key": "web-1"})
    assert status == 200
    # The phone's parked receive (its own key) lands afterwards.
    out, status = _call(client_api._do_receive_po_once, _user(rid), po["id"], {"idempotency_key": "phone-1"},
                        body={"idempotency_key": "phone-1"})
    assert status == 404 and out["code"] == "already_received" and out["po_number"] == po["po_number"]
    # Someone else's order (or none) is not "already received".
    out, status = _call(client_api._do_receive_po_once, _user(rid), 999999, {}, body={})
    assert status == 404 and "code" not in out
