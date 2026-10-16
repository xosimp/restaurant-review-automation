"""Food made and not sold, from the POS (owner, 10/16/26: "are these biggest
waste items actually accurate using ej's real kitchen data? wire them up if
not") — Simple EJ's has no waste log or kitchen count; its POS comps and
voids say what was cooked and thrown away."""
import os
from datetime import date, timedelta

import pos_food_waste as pfw
from models import get_conn


def test_only_a_reason_that_says_the_food_was_made_is_waste():
    for r in ("Error Made", "Unhappy Customer", "Object in Food", "Sent back - cold", "Remake"):
        assert pfw.is_waste_reason(r), r
    for r in ("Error Not Made", "Error Order Not Made", "*Manager Test*", "Wrong Check", "*Walk Out*", "Comp", None):
        assert not pfw.is_waste_reason(r), r


def _line(conn, rid, n, item, reason, kind="comp", qty=1, dept="Food", cat="04. Hand Helds", loss=15.0, parent=None):
    d = (date.today() - timedelta(days=2)).isoformat()
    conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, ticket_id, business_date, item_name, kind, qty, "
                 "loss_amount, reason, department, sales_category, parent_line_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "rpower", "L%d" % n, "T%d" % n, d, item, kind, qty, loss, reason, dept, cat, parent))


def test_remakes_are_costed_at_plate_cost_never_menu_price(db_path, monkeypatch):
    conn = get_conn(db_path)
    rid = conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('Waste Kitchen', 'w@example.com')").lastrowid
    _line(conn, rid, 1, "EJ's Smash Burger", "Error Made")
    _line(conn, rid, 2, "EJ's Smash Burger", "Unhappy Customer")
    _line(conn, rid, 3, "Wings", "Error Not Made", kind="void")             # never made
    _line(conn, rid, 4, "Miller Lite 22oz", "Error Made", dept="Beer")        # a drink, not food
    _line(conn, rid, 5, "Side Ranch", "Error Made", loss=1.0)                 # no recipe cost
    _line(conn, rid, 6, "Add Bacon", "Error Made", parent="L1")               # a modifier follows its dish
    _line(conn, rid, 7, "{=}**TO GO*{=}", "Error Made")                       # a POS marker, not a dish
    _line(conn, rid, 8, "Open Food Remote1", "Error Made")                    # an open-price button
    conn.commit()
    conn.close()
    monkeypatch.setattr(pfw, "_plate_costs", lambda r, db_path=None: {"ej's smash burger": 4.25})
    out = pfw.remakes(rid, days=7, db_path=db_path)
    assert out["available"] and out["lines"] == 5
    assert [(i["name"], i["qty"], i["cost"]) for i in out["items"]] == [("EJ's Smash Burger", 2.0, 8.5)]
    assert out["total_cost"] == 8.5 and [u["name"] for u in out["uncosted"]] == ["Side Ranch"]



def test_the_page_shows_real_waste_and_never_sample_stock_for_a_back_office_kitchen():
    tpl = open(os.path.join(os.path.dirname(pfw.__file__), "templates", "dashboard.html"), encoding="utf-8").read()
    assert "{% set _fw = food_waste if (_bo and not inv.is_live and food_waste and food_waste.available) else None %}" in tpl
    assert "dishes made and not sold, from your POS comps and voids, at plate cost" in tpl
    assert "Your order list comes with your first kitchen count" in tpl
    assert "{% if not (_bo and not inv.is_live) %}{% for item in inv.overstock %}" in tpl
