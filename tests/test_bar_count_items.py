"""The bar count, product by product (bar.count_items, owner 10/29/26: "I
need to be able to put a count for ALL drinks, not just beer and seltzer").

- The sheet is every beer, liquor and wine product on the invoices, grouped
  and A to Z, each in the unit it is poured from — bottles, cans, kegs —
  valued at its newest invoice price through its pack.
- One product's count saves at a time (blank clears it); the date's
  category values are the sums of their counted products (bar_counts,
  method "items"), so counted pour cost reads them unchanged.
- A count keeps the unit cost it was valued at: a later price never moves
  an old count.
- A counts-only login counts without ever seeing a cost or a value.
"""
from datetime import date, timedelta

import pytest

import bar.count_items as bci
import bar.counts as bcn
from tests.test_bar_routes import MOBILE, _app, _as, _invoice, _rid, db  # noqa: F401

TODAY = date.today()


def _stock(db, rid, price_scale=1.0, day=None):
    d = (day or TODAY - timedelta(days=10)).isoformat()
    _invoice(db, rid, "Breakthru Beverage Illinois", f"S{price_scale}{d}", d, [
        ("9031", "TITOS VDK 1L", "CA", 1, 300.0 * price_scale),          # 12 bottles a case
        ("777", "MILLER LITE 1/2BB", "KE", 2, 180.0 * price_scale),       # a keg
        ("5001", "COORS LIGHT 24/12 CAN", "CA", 2, 30.0 * price_scale),   # 24 cans
        ("6001", "LA MARCA PROSECCO 750ML", "BT", 6, 12.0 * price_scale),  # by the bottle
    ])


def _row(sheet, word):
    for g in sheet["groups"]:
        for r in g["products"]:
            if word in r["name"].lower():
                return g, r
    raise AssertionError(word)


def test_the_sheet_is_every_drink_in_the_unit_it_pours_from(db):
    rid = _rid(db)
    _stock(db, rid)
    sh = bci.sheet(rid, TODAY.isoformat(), db_path=db, today=TODAY)
    assert [g["category"] for g in sh["groups"]] == ["liquor", "wine", "beer"]
    assert sh["counted"] == 0 and sh["of"] == 4
    _, titos = _row(sh, "tito")
    assert titos["unit"] == "bottle" and titos["unit_label"] == "bottles" and titos["unit_cost"] == 25.0
    _, keg = _row(sh, "miller")
    assert keg["unit"] == "keg" and keg["unit_cost"] == 180.0
    _, coors = _row(sh, "coors")
    assert coors["unit"] == "can" and coors["unit_cost"] == 1.25
    _, wine = _row(sh, "prosecco")
    assert wine["unit"] == "bottle" and wine["unit_cost"] == 12.0
    beer = next(g for g in sh["groups"] if g["category"] == "beer")
    assert [r["name"].lower()[:5] for r in beer["products"]] == ["coors", "mille"]       # A to Z


def test_each_product_saves_alone_and_the_categories_add_up(db):
    rid = _rid(db)
    _stock(db, rid)
    keys = {r["name"].lower()[:4]: r["product_key"] for g in bci.sheet(rid, TODAY.isoformat(), db_path=db,
                                                                       today=TODAY)["groups"] for r in g["products"]}
    day = TODAY.isoformat()
    a = bci.save_item(rid, day, keys["tito"], "2.5", user_id=4, db_path=db, today=TODAY)
    assert a["qty"] == 2.5 and a["value"] == 62.5 and a["category_total"] == 62.5 and a["counted"] == 1 and a["of"] == 4
    bci.save_item(rid, day, keys["mill"], "0.5", db_path=db, today=TODAY)
    bci.save_item(rid, day, keys["coor"], "30", db_path=db, today=TODAY)
    rows = {r["category"]: r for r in bcn._rows(rid, db_path=db)}
    assert rows["liquor"]["value_usd"] == 62.5 and rows["liquor"]["method"] == "items"
    assert rows["beer"]["value_usd"] == 90.0 + 37.5 and "wine" not in rows
    sh = bci.sheet(rid, day, db_path=db, today=TODAY)
    assert sh["counted"] == 3 and sh["totals"] == {"liquor": 62.5, "wine": 0.0, "beer": 127.5}
    # A blank clears that one product; the last one out takes its category off.
    out = bci.save_item(rid, day, keys["tito"], "", db_path=db, today=TODAY)
    assert out["qty"] is None and out["counted"] == 2
    assert "liquor" not in {r["category"] for r in bcn._rows(rid, db_path=db)}
    # Zero is a count, never a blank.
    z = bci.save_item(rid, day, keys["tito"], "0", db_path=db, today=TODAY)
    assert z["qty"] == 0 and {r["category"]: r["value_usd"] for r in bcn._rows(rid, db_path=db)}["liquor"] == 0


def test_a_count_keeps_the_cost_it_was_valued_at(db):
    rid = _rid(db)
    _stock(db, rid, day=TODAY - timedelta(days=20))
    day = (TODAY - timedelta(days=15)).isoformat()
    key = _row(bci.sheet(rid, day, db_path=db, today=TODAY), "tito")[1]["product_key"]
    bci.save_item(rid, day, key, 4, db_path=db, today=TODAY)
    _stock(db, rid, price_scale=1.2, day=TODAY - timedelta(days=5))          # the price went up since
    old = _row(bci.sheet(rid, day, db_path=db, today=TODAY), "tito")[1]
    assert old["qty"] == 4 and old["unit_cost"] == 25.0 and old["value"] == 100.0
    fresh = _row(bci.sheet(rid, TODAY.isoformat(), db_path=db, today=TODAY), "tito")[1]
    assert fresh["unit_cost"] == 30.0 and fresh["qty"] is None


def test_a_bad_count_is_refused_with_a_sentence(db):
    rid = _rid(db)
    _stock(db, rid)
    key = _row(bci.sheet(rid, TODAY.isoformat(), db_path=db, today=TODAY), "tito")[1]["product_key"]
    for args, said in (((TODAY + timedelta(days=1)).isoformat(), key, 2), "future"), \
                      ((TODAY.isoformat(), key, "lots"), "as a number"), \
                      ((TODAY.isoformat(), key, -1), "between 0"), \
                      ((TODAY.isoformat(), "nope|1", 2), "has bought"):
        with pytest.raises(ValueError, match=said):
            bci.save_item(rid, *args, db_path=db, today=TODAY)


def test_a_counts_only_login_counts_without_a_dollar(db, monkeypatch):
    rid = _rid(db)
    _stock(db, rid)
    # The routes date a count by the restaurant's own day (the suite's TZ is
    # a day ahead of Chicago).
    from time_utils import restaurant_now_by_id
    TODAY = restaurant_now_by_id(rid, naive=True).date()
    cl = _app().test_client()
    _as(monkeypatch, rid, role="manager", grants=["foodcost.enter"], uid=8)
    sh = cl.get(f"/api/food-cost/bar/count-sheet?date={TODAY.isoformat()}")
    body = sh.get_json()
    assert sh.status_code == 200 and "totals" not in body
    titos = _row(body, "tito")[1]
    assert "unit_cost" not in titos and "value" not in titos
    r = cl.post("/mobile/api/food-cost/bar/count-item", json={"count_date": TODAY.isoformat(),
                                                              "product_key": titos["product_key"], "qty": "3"},
                headers=MOBILE)
    out = r.get_json()
    assert r.status_code == 200 and out["qty"] == 3 and not {"unit_cost", "value", "category_total"} & set(out)
    # The account holder sees the dollars of the same count.
    _as(monkeypatch, rid, uid=1)
    full = cl.get(f"/api/food-cost/bar/count-sheet?date={TODAY.isoformat()}").get_json()
    assert full["totals"]["liquor"] == 75.0 and _row(full, "tito")[1]["value"] == 75.0
    # A manager counts by default (no dollars); a login that can neither
    # count nor see Food Cost is refused.
    _as(monkeypatch, rid, role="staff", grants=[], uid=9)
    assert cl.get("/api/food-cost/bar/count-sheet").status_code == 403
    assert cl.post("/api/food-cost/bar/count-item", json={"count_date": TODAY.isoformat(),
                                                          "product_key": titos["product_key"], "qty": 1}).status_code == 403


def test_a_bottle_bought_elsewhere_is_counted_at_the_owners_price(db):
    import bar.hand as bh
    rid = _rid(db)
    _stock(db, rid)
    key = bh.save(rid, "Bombay Sapphire", "liquor", "1 L", 32.0, source="Binny's", db_path=db)["row"]["product_key"]
    _, row = _row(bci.sheet(rid, TODAY.isoformat(), db_path=db, today=TODAY), "bombay")
    assert row["product_key"] == key and row["unit"] == "bottle" and row["unit_cost"] == 32.0
    out = bci.save_item(rid, TODAY.isoformat(), key, "1.5", db_path=db, today=TODAY)
    assert out["value"] == 48.0 and out["category_total"] == 48.0


def test_a_bottle_is_counted_as_full_bottles_and_each_opened_ones_level(db):
    # Owner, 10/29/26: ".3 for a bottle that's open — closer to 1, the fuller".
    rid = _rid(db)
    _stock(db, rid)
    day = TODAY.isoformat()
    key = _row(bci.sheet(rid, day, db_path=db, today=TODAY), "tito")[1]["product_key"]
    out = bci.save_item(rid, day, key, full="3", opens=[".3", "0.9", ""], db_path=db, today=TODAY)
    assert out["qty"] == 4.2 and out["full"] == 3 and out["open"] == [0.3, 0.9] and out["value"] == 105.0
    row = _row(bci.sheet(rid, day, db_path=db, today=TODAY), "tito")[1]
    assert row["qty"] == 4.2 and row["full"] == 3 and row["open"] == [0.3, 0.9]
    # Only open bottles, no full one.
    assert bci.save_item(rid, day, key, full="", opens=[".1"], db_path=db, today=TODAY)["qty"] == 0.1
    for full, opens, said in (("2.5", [], "whole bottles"), ("1", ["1.2"], "between .1 and .9"),
                              ("1", ["0"], "between .1 and .9"), ("1", ["half"], "part of one")):
        with pytest.raises(ValueError, match=said):
            bci.save_item(rid, day, key, full=full, opens=opens, db_path=db, today=TODAY)
    # Every box blank clears it.
    assert bci.save_item(rid, day, key, full="", opens=["", ""], db_path=db, today=TODAY)["qty"] is None
    # A count saved as one number reads back split: 2.5 is 2 full and one at .5.
    bci.save_item(rid, day, key, "2.5", db_path=db, today=TODAY)
    row = _row(bci.sheet(rid, day, db_path=db, today=TODAY), "tito")[1]
    assert row["full"] == 2 and row["open"] == [0.5]
