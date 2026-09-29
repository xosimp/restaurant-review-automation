"""Memory fix round, integration wave (9/29/26): the words and the records
the workstreams owed each other.

  #23  "your target" only when an account holder set it — M7's setter words
       in M2's goal-first target_label / target_phrase, cogs.dish_reference
       and value_delivered's opportunity label.
  #24  every writer of a sell price, the menu, a goal and the roster leaves
       one attributed change_log row with its subject.
"""
import inspect
import json
import sys

import pytest

import change_log
import models
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard,
# INT_NOTES #12/#28): a module first imported inside a test binds that
# test's patched connection for good.
import cogs  # noqa: E402,F401
import goals  # noqa: E402,F401
import inventory_ledger  # noqa: E402,F401
import inventory_sync  # noqa: E402,F401
import menu_intelligence  # noqa: E402,F401
import recipes  # noqa: E402,F401
import staff_settings  # noqa: E402,F401
import strategy_routes  # noqa: E402,F401
import thresholds  # noqa: E402,F401
import value_delivered  # noqa: E402,F401

OWNER = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 22, "role": "manager", "is_admin": 0, "username": "dana"}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Harbor Grill", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{abs(hash(name)) % 10**6}@x.test",
                                       module_inventory=1, module_labor=1))
    c = models.get_conn()
    try:
        for k, v in cols.items():
            c.execute(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
        c.commit()
    finally:
        c.close()
    return rid


def _rows(rid, **where):
    out = change_log.history(rid)
    return [r for r in out if all(r.get(k) == v for k, v in where.items())]


# ── #23 "your target" ───────────────────────────────────────────────────────

@pytest.mark.parametrize("who,label,phrase,food", [
    ("principal", "your target", "your 28% target", "your food-cost target, 29%"),
    ("delegate", "the target a manager set", "the 28% target a manager set",
     "the food-cost target a manager set, 29%"),
    ("admin", "the target Cavnar AI set", "the 28% target Cavnar AI set", "the food-cost target Cavnar AI set, 29%"),
])
def test_your_target_only_when_an_account_holder_set_it(who, label, phrase, food):
    import cogs
    import thresholds
    rid = _rid(labor_target_pct=28.0, labor_target_source="set", food_cost_target=29.0,
               food_cost_target_source="set",
               target_setters_json=json.dumps({"labor_target_pct": who, "food_cost_target": who}))
    r = models.get_restaurant(rid)
    assert thresholds.target_label(r, "labor") == label
    assert thresholds.target_phrase(r, "labor", 28.0) == phrase
    assert cogs.dish_reference(r)["basis"] == food
    assert thresholds.setter_named(r, "labor") == ("your target" if who == "principal" else label)


def test_the_owners_goal_still_comes_first_and_the_opportunity_label_uses_the_setter():
    import goals
    import thresholds
    import value_delivered
    rid = _rid(labor_target_pct=28.0, labor_target_source="set",
               target_setters_json=json.dumps({"labor_target_pct": "delegate"}))
    goals.set_goal(rid, "labor_pct", 26, deadline="2026-12-31", user_id=11, authority="principal")
    r = models.get_restaurant(rid)
    assert thresholds.target_label(r, "labor").startswith("your goal of 26%")
    src = inspect.getsource(value_delivered)
    assert '"Scheduling against " + _thr.setter_named(restaurant, "labor")' in src
    assert "Scheduling against your target" not in src


# ── #24 the change log ──────────────────────────────────────────────────────

def _dish(rid, name, price=None):
    c = models.get_conn()
    try:
        cur = c.execute("INSERT INTO menu_items (restaurant_id, name, sell_price, is_active) VALUES (?,?,?,1)",
                        (rid, name, price))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def test_a_sell_price_typed_on_food_cost_is_one_attributed_row():
    import inventory_ledger
    rid = _rid()
    mid = _dish(rid, "Salmon Plate", 24.0)
    with change_log.attributed(source="manager", actor_user_id=22):
        assert inventory_ledger.set_menu_item_price(rid, mid, 26) is True
        assert inventory_ledger.set_menu_item_price(rid, mid, 26) is True          # no change, no row
    rows = _rows(rid, kind="price")
    assert len(rows) == 1
    r = rows[0]
    assert (r["subject"], r["field"], r["old_value"], r["new_value"]) == \
        ("Salmon Plate", "sell_price", 24.0, 26.0)
    assert (r["source"], r["actor_user_id"]) == ("manager", 22)
    # The one-tap reprice writes through the same function.
    import menu_intelligence
    assert "inventory_ledger.set_menu_item_price(" in inspect.getsource(menu_intelligence.apply_reprice)


def test_a_dish_added_by_hand_from_the_pos_menu_or_a_pasted_menu_is_one_row(monkeypatch):
    import inventory_ledger
    import recipes
    rid = _rid()
    with change_log.attributed(source="owner", actor_user_id=11):
        inventory_ledger.create_menu_item(rid, "Brisket Plate")
    monkeypatch.setattr(recipes, "missing_recipes", lambda r, db_path=None: [])
    monkeypatch.setattr(recipes, "draft_missing", lambda *a, **k: {"drafted": 0, "skipped": 0})
    with change_log.attributed(source="owner", actor_user_id=11):
        recipes.draft_from_menu(rid, "Classic Burger, 14\nBrisket Plate, 19")
    adds = sorted((r["subject"], r["source"]) for r in _rows(rid, kind="menu_add"))
    assert adds == [("Brisket Plate", "owner"), ("Classic Burger", "owner")]
    prices = sorted((r["subject"], r["new_value"]) for r in _rows(rid, kind="price"))
    assert prices == [("Brisket Plate", 19.0), ("Classic Burger", 14.0)]


def test_a_back_office_sync_logs_its_price_moves_and_new_dishes_as_the_syncs():
    import inventory_sync
    rid = _rid()
    _dish(rid, "Salmon Plate", 24.0)
    inventory_sync.apply_inventory(rid, "backoffice", {"recipes": [
        {"dish": "Salmon Plate", "sell_price": 25.5, "lines": []},
        {"dish": "Wings", "sell_price": 12.0, "lines": []}]})
    got = sorted((r["kind"], r["subject"], r["source"]) for r in change_log.history(rid))
    assert got == [("menu_add", "Wings", "sync"), ("price", "Salmon Plate", "sync"), ("price", "Wings", "sync")]
    inventory_sync.apply_inventory(rid, "backoffice", {"recipes": [{"dish": "Salmon Plate", "sell_price": 25.5}]})
    assert len(change_log.history(rid)) == 3, "a price the sync sends unchanged is no change"


def test_a_goal_set_confirmed_and_ended_is_one_row_each_with_its_subject():
    import goals
    rid = _rid()
    g = goals.set_goal(rid, "labor_pct", 27, user_id=11, authority="principal")
    p = goals.propose_goal(rid, "labor_pct", 25, user_id=22, authority="delegate")
    assert len(_rows(rid, kind="goal")) == 1, "a proposal changes no target"
    goals.confirm_goal(rid, p["id"], user_id=11)
    active = next(x for x in goals.progress(rid) if x["metric"] == "labor_pct")
    goals.end_goal(rid, active["id"], user_id=11, authority="principal")
    rows = list(reversed(_rows(rid, kind="goal")))
    assert [(r["subject"], r["old_value"], r["new_value"])
            for r in rows] == [
        ("labor_pct", None, {"target": 27.0, "deadline": None}),
        ("labor_pct", {"target": 27.0, "deadline": None}, {"target": 25.0, "deadline": None}),
        ("labor_pct", {"target": 25.0, "deadline": None}, None)]
    assert all(r["source"] == "owner" and r["actor_user_id"] == 11 for r in rows)
    assert g["metric"] == "labor_pct"
    import strategy_routes
    assert "answer_authority(u)" in inspect.getsource(strategy_routes._do_goal_end)


def test_m3s_roster_changes_landed():
    import staff_settings
    rid = _rid()
    staff_settings.upsert(rid, "Maria G.", max_hours=30, updated_by="owner")
    staff_settings.upsert(rid, "Maria G.", active=False, updated_by="owner")
    got = [(r["kind"], r["subject"]) for r in reversed(change_log.history(rid))]
    assert got == [("roster_change", "Maria G."), ("roster_leave", "Maria G.")]
