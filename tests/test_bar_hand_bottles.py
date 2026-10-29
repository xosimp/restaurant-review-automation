"""Bottles bought outside the distributors' invoices (bar.hand, owner
10/29/26: "build the hand-added bottle option").

EJ's pours Bombay, Malibu, Knob Creek, Dewar's 12, Frangelico, Kahlua and
Moët from liquor-store bottles — none is on an invoice, so the Pours-from
picker could never offer them. The owner adds one with its size and what a
bottle cost them:

- it joins the Pours-from picker (its own group) and a drink can be matched
  to it, in the same write;
- a drink poured from it is costed at that price through the same pack and
  pour as an invoice product (Cost);
- it is never a purchase: bought vs sold lists its drinks apart, never as a
  gap and never in Drinks to match;
- the same name and size is one bottle, changed; removing it sends its
  drinks back to Drinks to match.
"""
import pytest

import bar.hand as bh
import bar.margins as bmar
import bar.variance as bv
from bar.products import parse_pack
from tests.test_bar_variance import TODAY, _invoice, _map, _nights, _ring, _rid, db  # noqa: F401
from tests.test_bar_routes import MOBILE, _app, _as


def test_a_size_reads_as_typed_and_back_as_an_invoice_spells_it():
    assert bh.parse_size("750") == 750.0 and bh.parse_size("750 ml") == 750.0
    assert bh.parse_size("1 L") == 1000.0 and bh.parse_size("1.75L") == 1750.0
    assert bh.parse_size("12 oz") == pytest.approx(354.9, abs=0.1)
    for bad in ("", "a fifth", "10 gallons", "20", True, None):
        with pytest.raises(ValueError):
            bh.parse_size(bad)
    # The label is what the pack reader reads back, exactly.
    for ml, oz in ((750.0, 25.36), (1000.0, 33.81), (1750.0, 59.17), (bh.parse_size("12 oz"), 12.0)):
        pk = parse_pack(f"BOMBAY SAPPHIRE {bh.size_label(ml)}", "BT", "liquor")
        assert pk["container_oz"] == pytest.approx(oz, abs=0.05) and pk["units_per_case"] == 1


def test_a_bottle_is_added_changed_in_place_and_refused_out_of_bounds(db):
    rid = _rid(db)
    first = bh.save(rid, "Bombay Sapphire", "liquor", "1 L", "$32.99", source="Binny's", user_id=7, db_path=db)
    row = first["row"]
    assert first["added"] and bh.is_hand(row["product_key"])
    assert row["product"] == "Bombay Sapphire 1L" and row["price"] == 32.99 and row["source"] == "Binny's"
    # The same name and size again is that bottle, its price changed — never a second copy.
    again = bh.save(rid, "bombay  sapphire", "liquor", "1000", 34.5, db_path=db)
    assert not again["added"] and again["row"]["product_key"] == row["product_key"] and again["row"]["price"] == 34.5
    assert len(bh.hand_products(rid, db_path=db)) == 1
    for args, said in ((("B", "liquor", "1 L", 30), "Name the bottle"),
                       (("Malibu", "rum", "1 L", 20), "liquor, wine or beer"),
                       (("Malibu", "liquor", "1 L", 2), "check the price"),
                       (("Malibu", "liquor", "1 L", "free"), "one bottle cost"),
                       (("Malibu", "liquor", "9 L", 20), "50 ml and 5 L")):
        with pytest.raises(ValueError, match=said):
            bh.save(rid, *args, db_path=db)
    with pytest.raises(LookupError):
        bh.save(rid, "Malibu", "liquor", "1 L", 20, key="hand|000000000000", db_path=db)
    # Another restaurant never sees it.
    other = _rid(db, name="Other Tap")
    assert bh.hand_products(other, db_path=db) == []


def test_a_drink_poured_from_it_is_costed_at_the_owners_price_and_never_a_gap(db):
    rid = _rid(db)
    # Bought vs sold needs 28 days of invoices beside the sales.
    _invoice(db, rid, "A1", "2026-09-01", [("2002", "TITOS VODKA 1L", "BO", 4, 25.0)])
    _invoice(db, rid, "A2", "2026-10-01", [("2002", "TITOS VODKA 1L", "BO", 4, 25.0)])
    _nights(db, rid, 0, 34)
    for i in range(30):
        _ring(db, rid, i, "G1", "Bombay Sapphire", "Liquor", "Gin", sales=8.0)
    c = bmar.get_conn(db)
    try:
        c.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
                  "VALUES (?, 'rpower', 'G1', '1', 'Bombay Sapphire', 'Regular', 8.0)", (rid,))
        c.commit()
    finally:
        c.close()
    before = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    assert any(u["item"] == "Bombay Sapphire" for u in before["unmapped"])
    key = bh.save(rid, "Bombay Sapphire", "liquor", "1 L", 32.0, source="Binny's", db_path=db)["row"]["product_key"]
    _map(db, rid, "G1", key, pour_oz=1.5)
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    # Matched, so out of Drinks to match; never a product row with a gap.
    assert not any(u["item"] == "Bombay Sapphire" for u in v["unmapped"])
    assert not any(i["product_key"] == key for i in v["items"])
    gin = next(e for e in v["elsewhere"] if e["item"] == "Bombay Sapphire")
    assert gin["product_key"] == key and gin["rung_units"] == 30 and gin["sales_usd"] == 240.0
    assert v["elsewhere_total"] == 1 and v["elsewhere_sales_usd"] == 240.0
    # Costed at $32 a liter: 1.5 oz of 33.81 oz.
    m = bmar.margins(rid, {"canon": {}, "series": {}}, db_path=db)
    row = next(r for r in m["rows"] if r["item_id"] == "G1")
    assert row["hand"] and row["product"] == "Bombay Sapphire 1L"
    assert row["cost"] == pytest.approx(32.0 / 33.814 * 1.5, abs=0.01) and row["price"] == 8.0
    assert any("your price at Binny's" in a and "not on an invoice" in a for a in row["assumes"])


def test_removing_a_bottle_sends_its_drinks_back_to_match(db):
    rid = _rid(db)
    key = bh.save(rid, "Malibu", "liquor", "750", 19.99, db_path=db)["row"]["product_key"]
    _map(db, rid, "R1", key, pour_oz=1.5)
    assert bh.hand_products(rid, db_path=db)[0]["drinks"] == 1
    out = bh.remove(rid, key, db_path=db)
    assert out["cleared"] == 1 and out["name"] == "Malibu"
    assert bh.hand_products(rid, db_path=db) == [] and bmar.drink_map(rid, db_path=db) == []
    with pytest.raises(LookupError):
        bh.remove(rid, key, db_path=db)


def test_the_owner_adds_a_bottle_and_matches_a_drink_in_one_write(db, monkeypatch):
    rid = _rid(db)
    _invoice(db, rid, "A1", "2026-09-01", [("2002", "TITOS VODKA 1L", "BO", 4, 25.0)])
    c = bmar.get_conn(db)
    try:
        c.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
                  "VALUES (?, 'rpower', 'K1', '1', 'Knob Creek', 'Regular', 10.0)", (rid,))
        c.commit()
    finally:
        c.close()
    cl = _app().test_client()
    _as(monkeypatch, rid, uid=11)
    r = cl.post("/api/food-cost/bar/hand-product", json={"name": "Knob Creek", "category": "liquor", "size": "750",
                                                          "price": "36.99", "source": "Savway", "provider": "rpower",
                                                          "item_id": "K1", "pour_oz": "1.5"})
    out = r.get_json()
    assert r.status_code == 200 and out["added"] and out["matched"]["product_key"] == out["row"]["product_key"]
    key = out["row"]["product_key"]
    assert bmar.drink_map(rid, db_path=db)[0]["product_key"] == key
    # The drink-map route takes the owner's bottle too; a made-up hand key is refused.
    ok = cl.post("/mobile/api/food-cost/bar/drink-map", json={"provider": "rpower", "item_id": "K1",
                                                             "product_key": key, "pour_oz": 2}, headers=MOBILE)
    assert ok.status_code == 200
    bad = cl.post("/api/food-cost/bar/drink-map", json={"provider": "rpower", "item_id": "K1",
                                                         "product_key": "hand|ffffffffffff"})
    assert bad.status_code == 400
    # A drink not on the menu is refused before anything is saved.
    nm = cl.post("/api/food-cost/bar/hand-product", json={"name": "Frangelico", "category": "liquor", "size": "750",
                                                           "price": 30, "provider": "rpower", "item_id": "nope"})
    assert nm.status_code == 400 and len(bh.hand_products(rid, db_path=db)) == 1
    # The page carries it, and its picker row is marked as the owner's.
    page = cl.get("/api/food-cost/bar").get_json()
    assert any(p["product_key"] == key and p.get("hand") for p in page["pickable"])
    assert page["hand_products"][0]["drinks"] == 1
    from tests.test_bar_routes import _events
    import models
    assert _events(db, rid, "bar_hand_product_changed")
    assert "bar_hand_product_changed" in models.ACCOUNT_EVENT_TYPES and "bar_hand_product_changed" in models.ACCOUNT_EVENT_LABELS
    d = cl.delete("/api/food-cost/bar/hand-product/" + key.split("|")[1])
    assert d.status_code == 200 and d.get_json()["cleared"] == 1
    assert cl.delete("/api/food-cost/bar/hand-product/" + key.split("|")[1]).status_code == 404
    assert cl.delete("/api/food-cost/bar/hand-product/not-a-key").status_code == 404


def test_only_the_account_holder_adds_or_removes_a_bottle(db, monkeypatch):
    rid = _rid(db)
    cl = _app().test_client()
    body = {"name": "Kahlua", "category": "liquor", "size": "1 L", "price": 28}
    for role, grants in (("manager", ["foodcost.view"]), ("member", []), ("support", [])):
        _as(monkeypatch, rid, role=role, grants=grants)
        assert cl.post("/api/food-cost/bar/hand-product", json=body).status_code == 403, role
        assert cl.post("/mobile/api/food-cost/bar/hand-product", json=body, headers=MOBILE).status_code == 403, role
        assert cl.delete("/api/food-cost/bar/hand-product/0123456789ab").status_code == 403, role
    assert bh.hand_products(rid, db_path=db) == []
