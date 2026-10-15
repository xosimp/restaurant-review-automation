"""Bought vs rung, variance, the waste log, keg yield and comps by bartender
shift (barfix-1015 W6b; bar audit 10/14/26 F2 / F12 / F14 — CMP-3, CMP-11,
AIX-18 step 3, CMP §5 item 5). Units bought (cases × pack) against units
rung on owner-confirmed POS items; an unmapped item excluded and named,
never 0; poured drinks in ounces with a Double doubling its pour; comps
"poured, not paid" apart; waste beside, never netted; comps by bartender
shift owner-only, absent without a published schedule, said only past the
minimum sample and in loss_detection's words."""
import json
import os
import sqlite3
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import bar.comps as bcomps
import bar.variance as bv
import bar.waste as bw
import client_api
import mobile_api
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 10, 15)
D0 = date(2026, 9, 1)
SEP = " › "
CSV_HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _rid(db, **kw):
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "EJ's"), owner_email="o@x.test", **kw), db_path=db)


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _iso(n):
    return (D0 + timedelta(days=n)).isoformat()


def _invoice(db, rid, number, day, lines, dist="LAKESHORE BEVERAGE"):
    """lines: [(item_code, description, uom, qty, unit_price)]"""
    c = _conn(db)
    try:
        inv = c.execute("INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
                        "source_format, updated_at) VALUES (?,?,?,?,?, 'export', '2026-10-01 00:00:00')",
                        (rid, dist, number, day, sum(l[3] * l[4] for l in lines))).lastrowid
        for i, (code, desc, uom, qty, price) in enumerate(lines):
            c.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, uom, qty, "
                      "unit_price, cost, kind, restaurant_id) VALUES (?,?,?,?,?,?,?,?, 'item', ?)",
                      (inv, i + 1, code, desc, uom, qty, price, qty * price, rid))
        c.commit()
    finally:
        c.close()


def _nights(db, rid, first, last, version=4):
    c = _conn(db)
    try:
        for n in range(first, last + 1):
            c.execute("INSERT OR REPLACE INTO pos_archive_days (restaurant_id, provider, business_date, version) "
                      "VALUES (?, 'rpower', ?, ?)", (rid, _iso(n), version))
        c.commit()
    finally:
        c.close()


_LINE = [0]


def _ring(db, rid, n, item_id, name, dep, cat, kind="sale", qty=1, sales=5.0, parent=None, ticket=None,
          at=None, loss=None, approver=None, **extra):
    _LINE[0] += 1
    lid = f"L{_LINE[0]}"
    row = {"restaurant_id": rid, "provider": "rpower", "line_id": lid, "ticket_id": ticket or f"T{_LINE[0]}",
           "business_date": _iso(n), "item_id": item_id, "item_name": name, "kind": kind, "qty": qty,
           "sales": sales if kind == "sale" else 0.0, "department": dep, "sales_category": cat,
           "parent_line_id": parent, "mod_level": 1 if parent else 0,
           "item_at": at or f"{_iso(n)}T20:00:00", "loss_amount": loss, "approver_id": approver}
    row.update(extra)
    c = _conn(db)
    try:
        c.execute(f"INSERT INTO pos_ticket_lines ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                  tuple(row.values()))
        c.commit()
    finally:
        c.close()
    return lid


def _map(db, rid, item_id, key, pour_oz=None):
    from bar.margins import set_drink_map
    set_drink_map(rid, "rpower", item_id, key, pour_oz=pour_oz, db_path=db)


def _miller(db, rid):
    """2 cases × 24 bought; 40 rung on the mapped item, 1 comped, 10 of an
    unmapped beer."""
    _invoice(db, rid, "A1", _iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    _invoice(db, rid, "A2", _iso(30), [("2002", "TITOS VODKA 1L", "BO", 1, 25.0)])
    _nights(db, rid, 0, 34)
    for i in range(40):
        _ring(db, rid, i % 30, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans")
    _ring(db, rid, 3, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans", kind="comp", loss=5.0)
    for i in range(10):
        _ring(db, rid, i, "B9", "Bud Light Btl", "Beer", "Bottles & Cans")
    _map(db, rid, "M1", "lakeshore beverage|1001")


def _item(res, word):
    return next(i for i in res["variance"]["items"] if word in i["product"].lower())


def test_units_bought_against_units_rung_and_the_gap_at_unit_cost(db):
    rid = _rid(db)
    _miller(db, rid)
    res = bv.compute(rid, today=TODAY, db_path=db)
    v = res["variance"]
    assert v["available"] and v["basis"] == "bought_vs_rung" and v["label"] == "Bought vs rung"
    m = _item(res, "miller")
    assert m["unit"] == "each" and m["bought_units"] == 48 and m["rung_units"] == 40
    assert m["gap_units"] == 8 and m["gap_usd"] == 16.0          # $48 a case ÷ 24 = $2 a can
    # A comp is poured, not paid: apart, never rung.
    assert m["comped_units"] == 1 and m["comped_usd"] == 2.0


def test_an_unmapped_item_is_excluded_and_named_never_counted_as_zero(db):
    rid = _rid(db)
    _miller(db, rid)
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    assert not any("bud" in i["product"].lower() for i in v["items"])
    bud = next(u for u in v["unmapped"] if u["item"] == "Bud Light Btl")
    assert bud["rung_units"] == 10 and bud["category"] == "beer" and bud["sales_usd"] == 50.0
    # No row anywhere reads a rung of 0 for a product the POS sold.
    assert all(i["rung_units"] for i in v["items"])


def test_a_double_modifier_doubles_the_pour_and_an_add_on_follows_its_parent(db):
    rid = _rid(db)
    _miller(db, rid)
    _map(db, rid, "V1", "lakeshore beverage|2002", pour_oz=1.5)
    for i in range(9):
        _ring(db, rid, 29 - i % 2, "V1", "Titos Soda", "Liquor", "Vodka")
    parent = _ring(db, rid, 30, "V1", "Titos Soda", "Liquor", "Vodka", ticket="TD")
    _ring(db, rid, 30, "X9", "Double", "Liquor", "Vodka", sales=4.0, parent=parent, ticket="TD")
    # A priced add-on of Tito's under a comped drink: comped too, at the parent's double.
    comped = _ring(db, rid, 30, "C1", "Mule", "Liquor", "Cocktails", kind="comp", ticket="TC", loss=9.0)
    _ring(db, rid, 30, "V1", "Titos (Liquor Add-Ons)", "Liquor", "Vodka", qty=0, sales=3.0, parent=comped,
          ticket="TC")
    res = bv.compute(rid, today=TODAY, db_path=db)
    t = _item(res, "titos")
    assert t["unit"] == "oz"
    # 9 singles + one double = 11 pours of 1.5 oz; the add-on is comped (follows its parent).
    assert t["rung_units"] == 16.5 and t["comped_units"] == 1.5
    assert t["bought_units"] == round(1000 / 29.5735, 1)
    # The Double itself is a modifier, never an unmapped drink.
    assert not any(u["item"] == "Double" for u in res["variance"]["unmapped"])


def test_a_default_pour_is_said_as_an_assumption(db):
    rid = _rid(db)
    _miller(db, rid)
    _map(db, rid, "V1", "lakeshore beverage|2002")            # no pour set
    for i in range(4):
        _ring(db, rid, 29, "V1", "Titos Soda", "Liquor", "Vodka")
    t = _item(bv.compute(rid, today=TODAY, db_path=db), "titos")
    assert t["rung_units"] == 6.0 and "default_pour" in t["theoretical_basis"]
    assert any("assumes the 1.5 oz pour" in a for a in t["assumes"]) and t["confidence"] != "high"


def test_rpower_alternate_quantity_is_the_theoretical_source_when_kept(db):
    rid = _rid(db)
    _miller(db, rid)
    c = _conn(db)
    c.execute("INSERT INTO pos_sales_categories (restaurant_id, provider, category_id, name, department, alt_qty_unit) "
              "VALUES (?, 'rpower', 'cat-v', 'Vodka', 'Liquor', 'oz')", (rid,))
    c.commit()
    c.close()
    _map(db, rid, "V1", "lakeshore beverage|2002", pour_oz=1.5)
    _ring(db, rid, 29, "V1", "Titos Soda", "Liquor", "Vodka", category_id="cat-v", aqu_count=2.0)
    t = _item(bv.compute(rid, today=TODAY, db_path=db), "titos")
    assert t["rung_units"] == 2.0 and "rpower_alt_qty" in t["theoretical_basis"]


def test_withheld_when_the_pos_archive_misses_nights(db):
    rid = _rid(db)
    _invoice(db, rid, "A1", _iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    _invoice(db, rid, "A2", _iso(30), [("1001", "MILLER LITE 24/12 CAN", "CA", 1, 48.0)])
    _nights(db, rid, 0, 10)
    _nights(db, rid, 25, 34)
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    assert v["available"] is False and v["basis"] is None and "trading nights" in v["why"]


def test_withheld_when_the_two_sides_overlap_too_little(db):
    rid = _rid(db)
    _invoice(db, rid, "A1", _iso(20), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    _invoice(db, rid, "A2", _iso(30), [("1001", "MILLER LITE 24/12 CAN", "CA", 1, 48.0)])
    _nights(db, rid, 0, 34)
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    assert v["available"] is False and "needs 28" in v["why"]


def test_two_counts_make_it_variance_by_category_withheld_below_coverage(db):
    import bar.counts as bcn
    rid = _rid(db)
    _miller(db, rid)
    block = {"periods": [{"from": _iso(-1), "to": _iso(30), "ok": True,
                          "categories": {"beer": {"used": 100.0}, "liquor": {"used": None}, "wine": {"used": None}}}]}
    v = bv.compute(rid, today=TODAY, db_path=db, counted=block)["variance"]
    assert v["basis"] == "variance" and v["label"] == "Variance" and v["item_basis"] == "bought_vs_rung"
    beer = v["categories"]["beer"]
    # Bud Light (unmapped) is a fifth of beer sales: variance withheld, never computed on 80%.
    assert beer["variance_usd"] is None and "needs 90%" in beer["why"]
    assert v["categories"]["liquor"]["why"]
    assert callable(bcn.counted)  # the counts module owns `used` (bar.counts.counted builds the block)


def test_full_coverage_gives_category_variance_and_waste_never_moves_it(db):
    """F2's positive path (the withheld one is above): every beer the POS
    sold matched, so variance = what the counts say was used − the
    theoretical dollars of what was rung; F14: a waste entry is shown
    beside it and never netted out of it."""
    rid = _rid(db)
    _miller(db, rid)
    _map(db, rid, "B9", "lakeshore beverage|1001")        # the Bud Light button pours from the Miller cases
    block = {"periods": [{"from": _iso(-1), "to": _iso(30), "ok": True,
                          "categories": {"beer": {"used": 130.0}, "liquor": {"used": None}, "wine": {"used": None}}}]}
    beer = bv.compute(rid, today=TODAY, db_path=db, counted=block)["variance"]["categories"]["beer"]
    assert beer["coverage_pct"] >= 90 and beer["theoretical_usd"] == 100.0, beer   # 50 cans rung × $2
    assert beer["variance_usd"] == round(130.0 - beer["theoretical_usd"], 2) == 30.0
    bw.log_waste(rid, _iso(5), "lakeshore beverage|1001", 3, "each", "breakage", user_id=1, today=TODAY, db_path=db)
    res = bv.compute(rid, today=TODAY, db_path=db, counted=block)
    bv.attach_waste(res, rid, db_path=db)
    assert res["variance"]["categories"]["beer"]["variance_usd"] == 30.0
    assert _item(res, "miller")["waste_units"] == 3


def test_waste_is_shown_beside_the_gap_never_netted(db):
    rid = _rid(db)
    _miller(db, rid)
    bw.log_waste(rid, _iso(5), "lakeshore beverage|1001", 3, "each", "breakage", today=TODAY, db_path=db)
    blocks = bv.bar_blocks(rid, today=TODAY, db_path=db)
    m = next(i for i in blocks["variance"]["items"] if "miller" in i["product"].lower())
    assert m["gap_units"] == 8 and m["waste_units"] == 3 and m["waste_entries"] == 1
    w = blocks["bar_ops"]["waste"]
    assert w["entries"] == 1 and w["by_reason"][0]["reason"] == "breakage" and "never taken out" in w["note"]


def test_a_waste_entry_is_validated_with_a_sentence(db):
    rid = _rid(db)
    _miller(db, rid)
    with pytest.raises(ValueError, match="bought"):
        bw.log_waste(rid, _iso(5), "nope|1", 1, "each", "spill", today=TODAY, db_path=db)
    with pytest.raises(ValueError, match="already happened"):
        bw.log_waste(rid, (TODAY + timedelta(days=1)).isoformat(), "lakeshore beverage|1001", 1, "each", "spill",
                     today=TODAY, db_path=db)
    with pytest.raises(ValueError, match="why"):
        bw.log_waste(rid, _iso(5), "lakeshore beverage|1001", 1, "each", "theft", today=TODAY, db_path=db)
    with pytest.raises(ValueError, match="ounces"):
        bw.log_waste(rid, _iso(5), "lakeshore beverage|1001", 1, "case", "spill", today=TODAY, db_path=db)


def test_keg_yield_is_pints_rung_per_keg_with_its_assumption(db):
    rid = _rid(db)
    _invoice(db, rid, "K1", _iso(0), [("3003", "MILLER LITE 1/2 BBL", "KE", 1, 125.0)])
    _invoice(db, rid, "K2", _iso(30), [("3003", "MILLER LITE 1/2 BBL", "KE", 1, 125.0)])
    _nights(db, rid, 0, 34)
    _map(db, rid, "D1", "lakeshore beverage|3003", pour_oz=16)
    for i in range(200):
        _ring(db, rid, i % 30, "D1", "Miller Draft", "Beer", "Draft")
    res = bv.compute(rid, today=TODAY, db_path=db)
    k = res["keg_yield"][0]
    assert k["kegs_bought"] == 2 and k["theoretical_pints_per_keg"] == 124.0 and k["pints_per_keg"] == 100.0
    assert k["yield_pct"] == round(200 * 16 / (2 * 1984) * 100, 1) and k["expected_yield_pct"] == 90.0
    assert any("no keg counts" in a for a in k["assumes"])


def test_recipe_cards_load_as_suggestions_never_counted_until_confirmed(db, tmp_path):
    rid = _rid(db)
    _miller(db, rid)
    _invoice(db, rid, "A3", _iso(1), [("4004", "KETEL ONE VODKA 750ML", "BO", 6, 22.0)])
    cards = {"recipes": [{"name": "Espresso Martini", "alcoholic": True, "rpower_item": "Espresso Martini",
                          "ingredients": [{"item": "Ketel One Vodka", "amount": 1.5, "unit": "oz"},
                                          {"item": "Simple syrup", "amount": 0.5, "unit": "oz"},
                                          {"item": "Soda", "amount": None, "unit": "splash"}]},
                         {"name": "Spa Water", "alcoholic": False, "rpower_item": "Spa Water", "ingredients": []}]}
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(cards))
    for i in range(5):
        _ring(db, rid, 10, "E1", "Espresso Martini", "Liquor", "Cocktails", sales=13.0)
    got = bv.load_recipe_cards(rid, path=str(path), db_path=db)
    assert got["suggested"] == 1 and [r["ingredient"] for r in got["rows"]] == ["Ketel One Vodka"]
    spec = bv.drink_specs(rid, db_path=db)[0]
    assert spec["status"] == "suggested" and spec["product_key"] == "lakeshore beverage|4004" and spec["item_id"] == "E1"
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    assert not any("ketel" in i["product"].lower() for i in v["items"])
    assert next(u for u in v["unmapped"] if u["item"] == "Espresso Martini")["spec_suggested"] is True
    assert v["suggestions_total"] == 1
    bv.set_drink_spec(rid, spec["id"], confirm=True, db_path=db)
    k = _item(bv.compute(rid, today=TODAY, db_path=db), "ketel")
    assert k["rung_units"] == 7.5 and "recipe_card" in k["theoretical_basis"]
    # A reload never undoes the owner's confirmation.
    assert bv.load_recipe_cards(rid, path=str(path), db_path=db)["kept"] == 1
    assert bv.drink_specs(rid, db_path=db)[0]["status"] == "confirmed"


def test_a_card_loaded_before_its_button_attaches_by_name_the_night_it_sells(db, tmp_path):
    # Will, 10/15/26: the Midwest Wrecker went live after its card was loaded.
    rid = _rid(db)
    _miller(db, rid)
    _invoice(db, rid, "A3", _iso(1), [("4004", "KETEL ONE VODKA 750ML", "BO", 6, 22.0)])
    cards = {"recipes": [{"name": "Midwest Wrecker", "alcoholic": True, "rpower_item": "Midwest Wrecker",
                          "ingredients": [{"item": "Ketel One Vodka", "amount": 3.05, "unit": "oz"}]}]}
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(cards))
    got = bv.load_recipe_cards(rid, path=str(path), db_path=db)
    assert got["unresolved"] == ["Midwest Wrecker"]
    spec = bv.drink_specs(rid, db_path=db)[0]
    assert spec["item_id"] is None
    bv.set_drink_spec(rid, spec["id"], confirm=True, db_path=db)
    for i in range(4):
        _ring(db, rid, 10, "W1", "Midwest  Wrecker", "Liquor", "Cocktails", sales=14.0)
    k = _item(bv.compute(rid, today=TODAY, db_path=db), "ketel")
    assert k["rung_units"] == 12.2 and "recipe_card" in k["theoretical_basis"]


def test_a_retired_card_clears_its_suggestions_and_keeps_a_confirmed_spec(db, tmp_path):
    rid = _rid(db)
    _miller(db, rid)
    _invoice(db, rid, "A3", _iso(1), [("4004", "KETEL ONE VODKA 750ML", "BO", 6, 22.0)])
    card = {"name": "Yum Yum Frose", "alcoholic": True, "rpower_item": "Yum Yum Frose",
            "ingredients": [{"item": "Ketel One Vodka", "amount": 3.23, "unit": "oz"}]}
    other = {"name": "Espresso Martini", "alcoholic": True, "rpower_item": "Espresso Martini",
             "ingredients": [{"item": "Ketel One Vodka", "amount": 1.5, "unit": "oz"}]}
    path = tmp_path / "cards.json"
    path.write_text(json.dumps({"recipes": [card, other]}))
    bv.load_recipe_cards(rid, path=str(path), db_path=db)
    assert len(bv.drink_specs(rid, db_path=db)) == 2
    em = next(s for s in bv.drink_specs(rid, db_path=db) if s["item_name"] == "Espresso Martini")
    bv.set_drink_spec(rid, em["id"], confirm=True, db_path=db)
    path.write_text(json.dumps({"recipes": [dict(card, retired="10/15/26"), dict(other, retired="10/15/26")]}))
    got = bv.load_recipe_cards(rid, path=str(path), db_path=db)
    assert got["retired"] == ["Yum Yum Frose", "Espresso Martini"] and got["suggested"] == 0
    left = bv.drink_specs(rid, db_path=db)
    assert [(s["item_name"], s["status"]) for s in left] == [("Espresso Martini", "confirmed")]


def test_ejs_yum_yum_frose_is_retired_and_both_seasonal():
    data = {r["name"]: r for r in json.load(open(bv.RECIPE_CARDS, encoding="utf-8"))["recipes"]}
    assert data["Yum Yum Frose"].get("retired") and not data["Midwest Wrecker"].get("retired")
    assert data["Yum Yum Frose"]["seasonal"] is True and data["Midwest Wrecker"]["seasonal"] is True


def test_ejs_recipe_cards_parse():
    data = json.load(open(bv.RECIPE_CARDS, encoding="utf-8"))
    assert any(r.get("rpower_item") == "Perfect Game" for r in data["recipes"])


# ── comps by bartender shift (F12) ──────────────────────────────────────────

def _publish(db, rid, rows):
    """rows: [(day_n, employee, role, start, end)] — one published week each."""
    weeks = {}
    for n, e, r, a, b in rows:
        d = D0 + timedelta(days=n)
        ws = d - timedelta(days=d.weekday())
        weeks.setdefault(ws, []).append(f"{d.isoformat()},{d.strftime('%A')},{e},{r},{a},{b},8,\n")
    conn = models.get_conn(db)
    models._ensure_history_columns(conn)
    for ws, lines in weeks.items():
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                     "VALUES (?,?,?,?,datetime('now'))",
                     (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), CSV_HEAD + "".join(lines)))
    conn.commit()
    conn.close()


def _bar_night(db, rid, n, comps=0, sales=500.0):
    c = _conn(db)
    c.execute("INSERT OR REPLACE INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, is_bar) "
              "VALUES (?, 'rpower', ?, ?, 1)", (rid, f"BT{n}", _iso(n)))
    c.commit()
    c.close()
    _ring(db, rid, n, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans", sales=sales, ticket=f"BT{n}")
    for i in range(comps):
        _ring(db, rid, n, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans", kind="comp", loss=25.0,
              ticket=f"BT{n}", approver="99")


def test_comps_by_shift_are_absent_without_a_published_schedule(db):
    rid = _rid(db)
    _nights(db, rid, 0, 34)
    for n in range(0, 30):
        _bar_night(db, rid, n, comps=1 if n % 5 == 0 else 0)
    out = bcomps.comps_block(rid, today=TODAY, db_path=db)
    assert out["available"] and out["totals"]["comp_usd"] > 0
    assert out["shifts"]["available"] is False and "published schedule" in out["shifts"]["why"]
    assert "by_bartender" not in out["shifts"] and out["flagged"] == []


def test_comps_by_shift_need_the_minimum_sample_and_speak_like_loss_detection(db):
    import loss_detection as ld
    rid = _rid(db)
    _nights(db, rid, 0, 34)
    rows = []
    for n in range(0, 28):
        who = "Alex" if n % 2 == 0 else "Sam"
        rows.append((n, who, "Bartender", "4:00pm", "11:00pm"))
        _bar_night(db, rid, n, comps=3 if who == "Alex" else 0)
    for n in (28, 29, 30):
        rows.append((n, "Casey", "Bartender", "4:00pm", "11:00pm"))
        _bar_night(db, rid, n, comps=3)
    rows.append((5, "Pat", "Barback", "4:00pm", "11:00pm"))
    _publish(db, rid, rows)
    out = bcomps.comps_block(rid, today=TODAY, db_path=db)
    by = {r["bartender"]: r for r in out["shifts"]["by_bartender"]}
    assert "Pat" not in by                                 # a barback rings no tickets
    assert by["Casey"]["rate_pct"] is None and "needs 6" in by["Casey"]["too_few"]
    assert by["Alex"]["shifts"] == 14 and by["Alex"]["rate_pct"] > 0
    flag = next(f for f in out["flagged"] if f["bartender"] == "Alex")
    assert "on the 14 shifts Alex was scheduled" in flag["headline"] and flag["alternative"]
    assert "not a finding of wrongdoing" in out["note"]
    assert not any(f["bartender"] == "Casey" for f in out["flagged"])
    assert ld.SPIKE_MULTIPLE == bcomps._rules()[0]


def test_game_nights_are_shown_apart(db, monkeypatch):
    rid = _rid(db)
    _nights(db, rid, 0, 34)
    for n in range(0, 20):
        _bar_night(db, rid, n, comps=2 if n == 6 else 0)
    monkeypatch.setattr(bcomps, "_game_nights", lambda *a, **k: {_iso(6)})
    out = bcomps.comps_block(rid, today=TODAY, db_path=db)
    assert out["game_nights"]["nights"] == 1 and out["game_nights"]["comp_usd"] == 50.0
    assert out["totals"]["comp_usd"] == 0.0


# ── the routes ──────────────────────────────────────────────────────────────

def _app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _as(monkeypatch, rid, role=None):
    user = {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "u"}
    if role:
        user["role"] = role
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(user))


def test_comps_are_stripped_for_every_login_but_the_account_holder(db, monkeypatch):
    import permissions
    rid = _rid(db)
    payload = {"bar_ops": {"comps": {"owner_only": True}}}
    monkeypatch.setattr(bcomps, "comps_block", lambda *a, **k: {"available": True, "shifts": {"by_bartender": [1]}})
    monkeypatch.setattr(permissions, "is_principal", lambda u: False)
    assert bcomps.for_viewer(json.loads(json.dumps(payload)), rid, {"id": 2})["bar_ops"]["comps"] == {"owner_only": True}
    monkeypatch.setattr(permissions, "is_principal", lambda u: True)
    got = bcomps.for_viewer(json.loads(json.dumps(payload)), rid, {"id": 1})["bar_ops"]["comps"]
    assert got["shifts"]["by_bartender"] == [1] and got["owner_only"] is True


def test_the_bar_payload_carries_variance_and_bar_ops(db, monkeypatch):
    rid = _rid(db)
    _miller(db, rid)
    import bar_cost
    out = bar_cost.build(rid, today=TODAY)
    assert out["variance"]["basis"] == "bought_vs_rung"
    assert out["bar_ops"]["comps"] == {"owner_only": True} and "keg_yield" in out["bar_ops"]
    _as(monkeypatch, rid)
    body = _app().test_client().get("/api/food-cost/bar").get_json()
    assert body["bar_ops"]["comps"]["owner_only"] is True and "available" in body["bar_ops"]["comps"]


def test_a_manager_logs_waste_and_only_the_owner_removes_it(db, monkeypatch):
    import permissions as p
    assert p.enter_path_allowed({"role": "manager"}, "POST", "/mobile/api/food-cost/bar/waste")
    assert not p.enter_path_allowed({"role": "manager"}, "DELETE", "/api/food-cost/bar/waste/1")
    rid = _rid(db)
    _miller(db, rid)
    _as(monkeypatch, rid, role="manager")
    c = _app().test_client()
    r = c.post("/api/food-cost/bar/waste", json={"waste_date": (date.today() - timedelta(days=1)).isoformat(),
                                                  "product_key": "lakeshore beverage|1001", "qty": 2, "unit": "each",
                                                  "reason": "breakage"})
    assert r.status_code == 200 and r.get_json()["ok"]
    g = c.get("/api/food-cost/bar/waste").get_json()
    assert g["entries"][0]["qty"] == 2 and g["can_remove"] is False and "$" not in json.dumps(g["entries"])
    assert c.delete(f"/api/food-cost/bar/waste/{g['entries'][0]['id']}").status_code == 403
    assert c.post("/api/food-cost/bar/drink-spec", json={"id": 1, "confirm": True}).status_code == 403
    _as(monkeypatch, rid)
    assert c.delete(f"/api/food-cost/bar/waste/{g['entries'][0]['id']}").status_code == 200
    events = [r["event_type"] for r in _conn(db).execute("SELECT event_type FROM activity_log WHERE restaurant_id=?",
                                                         (rid,))]
    assert "bar_waste_logged" in events and "bar_waste_removed" in events
    assert models.ACCOUNT_EVENT_LABELS["bar_waste_logged"] == "Bar waste logged"


def test_the_routes_have_their_mobile_twins():
    rules = {r.rule for r in _app().url_map.iter_rules()}
    for path in ("/api/food-cost/bar/waste", "/api/food-cost/bar/waste/<int:entry_id>",
                 "/api/food-cost/bar/drink-specs", "/api/food-cost/bar/drink-spec"):
        assert path in rules and "/mobile" + path in rules, path
