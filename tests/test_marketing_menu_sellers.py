"""The content calendar names the restaurant's real dishes: its POS best
sellers, food and drinks apart, add-ons and soft drinks left out (Will,
9/29/26 — Simple EJ's had no menu notes and got "a photo of your food")."""
from datetime import date, timedelta

import marketing
import models
from models import Restaurant, create_restaurant


def _item(conn, rid, name, cat, qty, kind="dish"):
    cur = conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active, pos_category, kind) VALUES (?,?,?,?,?)",
                       (rid, name, 1, cat, kind))
    conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) VALUES (?,?,?,?)",
                 (rid, cur.lastrowid, (date.today() - timedelta(days=3)).isoformat(), qty))


def test_best_sellers_split_food_and_drinks_and_skip_add_ons(db_path):
    rid = create_restaurant(Restaurant(name="Menu Co", owner_email="m@x.test"), db_path=db_path)
    conn = models.get_conn(db_path)
    _item(conn, rid, "EJ's Smash Burger", "04. Hand Helds", 1152)
    _item(conn, rid, "Wings", "01. Shareables", 766)
    _item(conn, rid, "Add Bacon", "(Food Add-Ons)", 900)
    _item(conn, rid, "Well Done", "(Modifiers)", 700, kind="modifier")
    _item(conn, rid, "Diet Coke", "20. Beverages", 279)
    _item(conn, rid, "Passion Margarita", "38. Cocktails", 145)
    _item(conn, rid, "Cash Discount", "Discount All", 461)
    conn.commit()
    conn.close()
    out = marketing.menu_sellers_block(rid, db_path=db_path)
    assert "most sold first: EJ's Smash Burger, Wings\n" in out
    assert "Best-selling drinks: Passion Margarita" in out
    for gone in ("Add Bacon", "Well Done", "Diet Coke", "Cash Discount"):
        assert gone not in out
    assert "never name a dish that is not listed" in out


def test_no_item_sales_adds_nothing(db_path):
    rid = create_restaurant(Restaurant(name="Empty Co", owner_email="e@x.test"), db_path=db_path)
    assert marketing.menu_sellers_block(rid, db_path=db_path) == ""
