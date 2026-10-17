"""Fix round for the Food Cost + Bar audit of 10/16/26, workstream C1 — the
Bar's calculations, its cache and its own page code (registers in
/Users/simp/fc_bar_audit_1016: 02-bar-calc, 03-match-ingest, 04-pos-time,
05-ai, 06-ui, 07-sec-scale). One test or more per finding ID, each asserting
the corrected behaviour; tests/test_audit_fc_bar_1016.py keeps the strict
xfails the lead flips."""
import json
import sqlite3
import threading
import time
from datetime import date, timedelta

import pytest

import bar.cache as bcache
import bar.variance as bv
import models
import tests.test_bar_variance as tv
from bar import judge as bj
from bar import pour_cost as bp
from bar.classify import KEG_RE, classify, is_keg
from bar.common import _mdy
from bar.products import parse_pack
from tests.test_bar_judge import L, cat, daily, pc_of, steady
from tests.test_bar_variance import db  # noqa: F401

TODAY = tv.TODAY


def _late(db, rid, day=32):
    """A later invoice of another product: the invoices and the POS archive
    overlap for the whole span (bought vs sold needs 28 days)."""
    tv._invoice(db, rid, "Z9", tv._iso(day), [("8888", "COORS LIGHT 24/12 CAN", "CA", 1, 48.0)])


def _counted(beer=None, liquor=None, wine=None, frm=9, to=38, cats=None):
    c = cats or {"beer": {"used": beer}, "liquor": {"used": liquor}, "wine": {"used": wine}}
    return {"periods": [{"from": tv._iso(frm), "to": tv._iso(to), "ok": True, "categories": c}]}


# ── BAR-CALC-01: priced from the product's own cost ─────────────────────────

def test_bar_calc_01_stock_bought_before_the_span_is_priced_and_matched(db):
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    tv._invoice(db, rid, "A2", tv._iso(39), [("1002", "COORS LIGHT 24/12 CAN", "CA", 1, 48.0)])
    tv._nights(db, rid, 0, 40)
    for i in range(40):
        tv._ring(db, rid, 10 + (i % 29), "M1", "Miller Lite Btl", "Beer", "Bottles & Cans")
    tv._map(db, rid, "M1", "lakeshore beverage|1001")
    v = bv.compute(rid, today=TODAY, db_path=db, counted=_counted(beer=80.0))["variance"]
    beer = v["categories"]["beer"]
    assert beer["coverage_pct"] == 100.0 and beer["theoretical_usd"] == 80.0 and beer["variance_usd"] == 0.0
    m = next(i for i in v["items"] if i["product_key"] == "lakeshore beverage|1001")
    assert m["priced_from"] == tv._iso(0) and any("priced at" in a for a in m["assumes"])


def test_bar_calc_01_a_drink_whose_product_has_no_cost_leaves_the_matched_share(db):
    """A wine whose bottle size the invoice never says is poured in ounces
    no one knows: its sales leave the matched share and the variance is
    withheld — never its usage read as $0 theoretical."""
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("5005", "HOUSE CABERNET", "BO", 30, 10.0)])
    _late(db, rid)
    tv._nights(db, rid, 0, 34)
    for i in range(30):
        tv._ring(db, rid, i, "W1", "House Red Glass", "Wine", "Wine By The Glass", sales=9.0)
    tv._map(db, rid, "W1", "lakeshore beverage|5005")
    block = _counted(cats={"beer": {"used": None}, "liquor": {"used": None}, "wine": {"used": 300.0}}, frm=-1, to=30)
    v = bv.compute(rid, today=TODAY, db_path=db, counted=block)["variance"]
    wine = v["categories"]["wine"]
    assert wine["coverage_pct"] == 0.0 and wine["variance_usd"] is None and "needs 95%" in wine["why"]
    row = v["items"][0]
    assert row["theoretical_usd"] is None and row["gap_usd"] is None and row["confidence"] == "low"
    assert v["totals"]["by_category"]["wine"]["theoretical_usd"] == 0.0     # nothing summed as $0 for it


# ── BAR-CALC-04: a line with no unit holds no units and no dollars ──────────

def test_bar_calc_04_a_uomless_line_without_a_count_adds_no_units_or_dollars(db):
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("2002", "TITOS VODKA 1L", "BO", 2, 25.0)])
    tv._invoice(db, rid, "A2", tv._iso(5), [("2002", "TITOS VODKA 1L", None, 3, 300.0)])
    _late(db, rid)
    tv._nights(db, rid, 0, 34)
    for i in range(30):
        tv._ring(db, rid, i, "T1", "Titos", "Liquor", "Vodka")
    tv._map(db, rid, "T1", "lakeshore beverage|2002")
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    t = v["items"][0]
    assert t["bought_units"] is None and t["gap_usd"] is None and t["bought_usd"] == 50.0
    assert any("no unit of measure or count" in a for a in t["assumes"])
    assert v["totals"]["gap_usd"] == 0.0
    pk = parse_pack("MILLER LITE 24/12 CAN", None, "beer")
    assert (pk["units_per_case"], pk["units_source"]) == (24, "parsed")
    assert parse_pack("TITOS VODKA 1L", None, "liquor")["units_source"] == "none"


# ── BAR-CALC-05: used − (sold + comped + waste), set-aside in the base ──────

def test_bar_calc_05_comps_are_a_named_part_and_set_aside_sales_stay_in_the_base(db):
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 3, 48.0)])
    _late(db, rid)
    tv._nights(db, rid, 0, 34)
    for i in range(40):
        tv._ring(db, rid, 1 + i % 29, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans")
    for i in range(10):
        tv._ring(db, rid, 2 + i, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans", kind="comp", loss=5.0)
    tv._map(db, rid, "M1", "lakeshore beverage|1001")
    block = _counted(beer=100.0, frm=0, to=30)
    beer = bv.compute(rid, today=TODAY, db_path=db, counted=block)["variance"]["categories"]["beer"]
    assert beer["theoretical_usd"] == 80.0 and beer["comped_usd"] == 20.0 and beer["variance_usd"] == 0.0
    # An open-price key's sales stay in the base: matched share 200 / 260.
    for i in range(3):
        tv._ring(db, rid, 5 + i, "O1", "Open Beer", "Beer", "Bottles & Cans", sales=20.0)
    v = bv.compute(rid, today=TODAY, db_path=db, counted=block)["variance"]
    assert v["set_aside"][0]["reason"] == "open_price"
    assert v["totals"]["by_category"]["beer"]["coverage_pct"] == round(200 / 260 * 100, 1)
    assert v["categories"]["beer"]["variance_usd"] is None


# ── BAR-CALC-14: each category over its own count dates ─────────────────────

def test_bar_calc_14_each_category_reads_its_own_count_span(db):
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 3, 48.0)])
    tv._invoice(db, rid, "A2", tv._iso(0), [("2002", "TITOS VODKA 1L", "BO", 30, 25.0)], dist="LAKESHORE BEVERAGE")
    _late(db, rid)
    tv._nights(db, rid, 0, 34)
    for d in range(1, 31):
        tv._ring(db, rid, d, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans")
    tv._map(db, rid, "M1", "lakeshore beverage|1001")
    cats = {"beer": {"used": 60.0, "begin_date": tv._iso(0), "end_date": tv._iso(30)},
            "liquor": {"used": None}, "wine": {"used": None}}
    block = {"periods": [{"from": tv._iso(2), "to": tv._iso(30), "ok": True, "categories": cats}]}
    v = bv.compute(rid, today=TODAY, db_path=db, counted=block)["variance"]
    beer = v["categories"]["beer"]
    # Beer's own span starts the night after ITS count (9/2), not the period's.
    assert beer["from"] == tv._iso(1) and beer["theoretical_usd"] == 60.0 and beer["variance_usd"] == 0.0
    assert v["span"]["from"] == tv._iso(1)


# ── BAR-CALC-16: pitchers need a size; a bucket only with its lines ─────────

def test_bar_calc_16_a_pitcher_with_no_size_is_never_the_draft_default(db):
    pk = parse_pack("MILLER LITE 1/2 BBL", "KE", "beer")
    assert bv.pour_target("beer", pk, None, "Miller Lite Pitcher")[0] == "unknown"
    assert bv.pour_target("beer", pk, None, "Miller Lite 60oz Pitcher")[:2] == ("oz", 60.0)
    assert bv.pour_target("beer", pk, None, "Miller Lite Half Pint")[:2] == ("oz", 8.0)
    rid = tv._rid(db)
    tv._invoice(db, rid, "K1", tv._iso(0), [("3003", "MILLER LITE 1/2 BBL", "KE", 1, 125.0)])
    _late(db, rid)
    tv._nights(db, rid, 0, 34)
    for i in range(20):
        tv._ring(db, rid, i, "P1", "Miller Pitcher", "Beer", "Draft", sales=14.0)
    tv._map(db, rid, "P1", "lakeshore beverage|3003")
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    assert "Miller Pitcher" in v["unsized"] and v["totals"]["by_category"]["beer"]["coverage_pct"] == 0.0


def test_bar_calc_16_a_bucket_is_set_aside_only_when_its_beers_ring_after_it(db):
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    _late(db, rid)
    tv._nights(db, rid, 0, 34)
    tv._ring(db, rid, 3, "BK", "Domestic Bucket", "Beer", "Buckets", sales=20.0, ticket="T-A")
    tv._ring(db, rid, 3, "BL", "Miller Lite`Bucket", "Beer", "Buckets", sales=0.0, ticket="T-A")
    tv._ring(db, rid, 4, "BK", "Domestic Bucket", "Beer", "Buckets", sales=20.0, ticket="T-B")
    v = bv.compute(rid, today=TODAY, db_path=db)["variance"]
    aside = {a["item_id"]: a for a in v["set_aside"]}
    assert aside["BK"]["sales_usd"] == 20.0                    # only ticket T-A's bucket
    assert any(u["item_id"] == "BK" and u["sales_usd"] == 20.0 for u in v["unmapped"])


# ── BAR-CALC-17 / 19: invoice cost, charges and rebates ─────────────────────

def test_bar_calc_17_theoretical_stays_on_invoice_cost_with_rpower_beside(db):
    rid = tv._rid(db)
    tv._miller(db, rid)
    c = sqlite3.connect(db)
    c.execute("UPDATE pos_ticket_lines SET cost=0.5 WHERE restaurant_id=? AND item_id='M1' AND kind='sale'", (rid,))
    c.commit()
    c.close()
    m = tv._item(bv.compute(rid, today=TODAY, db_path=db), "miller")
    assert m["theoretical_usd"] == 80.0 and m["rpower_cost_usd"] == 20.0 and "rpower_cost" not in m["theoretical_basis"]


def test_bar_calc_19_a_per_unit_charge_is_part_of_the_products_cost(db):
    rid = tv._rid(db)
    tv._miller(db, rid)
    c = sqlite3.connect(db)
    inv = c.execute("SELECT id FROM fintech_invoices WHERE restaurant_id=? AND invoice_number='A1'", (rid,)).fetchone()[0]
    c.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, uom, qty, unit_price, "
              "cost, kind, restaurant_id) VALUES (?, 9, '1001', 'MILLER LITE 24/12 CAN', 'CA', 2, 4.8, 9.6, 'charge', ?)",
              (inv, rid))
    c.commit()
    c.close()
    m = tv._item(bv.compute(rid, today=TODAY, db_path=db), "miller")
    assert m["bought_usd"] == 105.6 and m["charges_usd"] == 9.6 and m["theoretical_usd"] == round(105.6 / 48 * 40, 2)


# ── BAR-CALC-18: an invoice discount lowers its products' cost ──────────────

def test_bar_calc_18_an_invoice_discount_is_spread_over_the_invoices_products(db):
    from bar.purchases import _lines
    from bar.spend import _distributors
    rid = tv._rid(db)
    tv._invoice(db, rid, "S1", tv._iso(0), [("9031", "TITOS VDK 1L", "CA", 1, 300.0),
                                             ("9032", "JAMESON 1L", "CA", 1, 100.0)], dist="SOUTHERN GLAZERS")
    c = sqlite3.connect(db)
    inv = c.execute("SELECT id FROM fintech_invoices WHERE restaurant_id=?", (rid,)).fetchone()[0]
    c.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, uom, qty, unit_price, "
              "cost, kind, restaurant_id) VALUES (?, 3, NULL, 'INVOICE DISCOUNT', 'EA', 1, -40, -40, 'item', ?)",
              (inv, rid))
    c.commit()
    c.close()
    ls = _lines(rid, db, backoffice=False)
    nets = {l["description"]: round(l["net"], 2) for l in ls}
    assert nets["TITOS VDK 1L"] == 270.0 and nets["JAMESON 1L"] == 90.0 and nets["INVOICE DISCOUNT"] == 0.0
    d = _distributors(ls)[0]
    assert d["spend"] == 360.0 and d["fees"] == 0.0


# ── BAR-CALC-09 / ING-15 (beyond the xfail's three) ─────────────────────────

@pytest.mark.parametrize("desc,uom,cat,units,src,ml,keg", [
    ("GUINNESS 13.2 GAL", "KE", "beer", 1, "parsed", None, 1689.6),
    ("FIREBALL 50ML", "CA", "liquor", 120, "assumed", 50.0, None),
    ("FIREBALL 10/12/50ML", "CA", "liquor", 120, "parsed", 50.0, None),
    ("BAREFOOT CAB 1.5L", "CA", "wine", 6, "assumed", 1500.0, None),
    ("MILLER 24/12OZ CAN", "CA", "beer", 24, "parsed", 354.9, None),
    ("Barmen 1873 Bourbon 6/750ML", "CA", "liquor", 6, "parsed", 750.0, None),
])
def test_bar_calc_09_more_pack_shapes(desc, uom, cat, units, src, ml, keg):
    pk = parse_pack(desc, uom, cat)
    assert (pk["units_per_case"], pk["units_source"]) == (units, src), pk
    if ml is not None:
        assert pk["container_ml"] == ml, pk
    if keg is not None:
        assert pk["keg_oz"] == keg, pk


# ── BAR-CALC-10 / ING-16: one keg pattern, beer words win as a guess ────────

@pytest.mark.parametrize("desc,want", [
    ("JAMESON BLACK BARREL 750ML", ("liquor", True)), ("KNOB CREEK SINGLE BARREL", ("liquor", True)),
    ("BUFFALO TRACE SINGLE BARREL", ("liquor", True)), ("GOOSE ISLAND BOURBON COUNTY STOUT", ("beer", False)),
    ("KRONENBOURG 1664 BLANC 4/6 12Z NR", ("beer", False)), ("ALLAGASH WHITE 1/6BL", ("beer", True)),
])
def test_bar_calc_10_barrel_is_whiskey_and_beer_words_win_as_a_guess(desc, want):
    assert classify("Some House", desc, "CA") == want


def test_bar_calc_10_the_shared_keg_test():
    for d in ("ALLAGASH WHITE 1/6BL", "LITE 1/2 BL", "ULTRA 1/2 BARREL", "GUINNESS 13.2 GAL", "KROMBACH 50L",
              "HALF ACRE K 1/2", "SIXTEL IPA"):
        assert is_keg(d), d
    for d in ("JAMESON BLACK BARREL 750ML", "KNOB CREEK SINGLE BARREL", "CHATEAU 1.50L"):
        assert not is_keg(d), d
    assert is_keg("KNOB CREEK SINGLE BARREL", "KE") and KEG_RE.search("1/4 BBL")


# ── BAR-CALC-02 / 03: missing nights and unmapped sales hold the reading ────

def test_bar_calc_02_a_trading_night_with_no_report_is_not_judged():
    lines, sales = steady()
    lines = [dict(l, net=900.0) if l["category"] == "liquor" else l for l in lines]
    base = cat(pc_of(lines, sales, targets={"liquor": 18.0}), "liquor")
    assert base["judged"] is True
    missing = sorted(sales["liquor"])[-10]
    for c in sales.values():
        c.pop(missing, None)
    liq = cat(pc_of(lines, sales, targets={"liquor": 18.0}, sales_missing=(missing,)), "liquor")
    assert liq["judged"] is False and "sales_gap" in liq["reasons"]
    assert "No nightly report is on file" in liq["not_judged_text"] and liq.get("over_target_usd") is None


def test_bar_calc_02_counted_pour_cost_needs_every_trading_night(db):
    import bar.counts as bcn
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(5), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    sales = {"beer": {tv._iso(n): 100.0 for n in range(1, 31) if n != 12}}
    lines = __import__("bar.purchases", fromlist=["_lines"])._lines(rid, db)
    rows = [{"count_date": tv._iso(0), "category": "beer", "value_usd": 500.0},
            {"count_date": tv._iso(30), "category": "beer", "value_usd": 520.0}]
    for r in rows:
        bcn.save_count(rid, r["count_date"], {"beer": r["value_usd"]}, today=tv.TODAY, db_path=db)
    out = bcn.true_pour_cost(rid, tv._iso(0), tv._iso(30), db_path=db, lines=lines, sales=sales)
    beer = out["categories"]["beer"]
    assert beer["pct"] is None and any(_mdy(tv._iso(12)) in m["why"] for m in beer["missing"]), beer["missing"]


def test_bar_calc_03_unmapped_drink_sales_hold_every_reading():
    lines, sales = steady()
    um = {"pct": 9.1, "usd": 3000.0, "hold": True}
    pc = pc_of(lines, sales, targets={"liquor": 18.0}, unmapped_of=lambda a, b: um)
    for c in pc["categories"]:
        assert c["judged"] is False and "unmapped" in c["reasons"]
    assert pc["total"]["judged"] is False and pc["unmapped"] == um


# ── BAR-CALC-06: a stock-up below the range top, above a target set high ───

def test_bar_calc_06_a_target_above_the_range_judges_by_the_range():
    at = bj.stock_up([L("X", "2026-10-01", "beer", 100.0)], "beer", "2026-09-10", "2026-10-07", pct=25.0,
                     range_hi=26.0, judge_at=30.0)
    assert at is None                                   # 25% is under min(30, 26): nothing to explain


# ── BAR-CALC-12 / 13: one basis, one window ─────────────────────────────────

def test_bar_calc_13_a_counted_and_a_purchase_category_make_no_total():
    cats = {"beer": {"key": "beer", "label": "Beer", "_bought": 400.0, "_sold": 2000.0, "basis": "counted"},
            "liquor": {"key": "liquor", "label": "Liquor", "_bought": 300.0, "_sold": 1500.0, "basis": "purchases"},
            "wine": {"key": "wine", "label": "Wine", "_bought": 0.0, "_sold": 0.0, "basis": "purchases"}}
    t = bp._total(cats, mixed=50.0)
    assert t["mixed_basis"] is True and t["pct"] is None and t["purchases"] is None and t["mixed_beside"] == 50.0


def test_bar_calc_12_mixed_store_alcohol_is_inside_only_a_whole_total():
    def c(k, b, s):
        return {"key": k, "label": k, "_bought": b, "_sold": s}
    whole = bp._total({"beer": c("beer", 200, 1000), "liquor": c("liquor", 300, 1500), "wine": c("wine", 100, 300)}, 60)
    assert whole["mixed_alcohol"] == 60 and whole["purchases"] == 660 and whole["mixed_beside"] is None
    part = bp._total({"beer": c("beer", 200, 1000), "liquor": c("liquor", 300, 1500), "wine": c("wine", 0, 300)}, 60)
    assert part["mixed_alcohol"] == 0 and part["purchases"] == 500 and part["mixed_beside"] == 60


# ── BAR-CALC-15: the window is the Fintech feed's ───────────────────────────

def test_bar_calc_15_back_office_dates_never_stretch_the_window():
    lines = [L("Lakeshore", "2026-09-01", "beer", 100.0), L("Lakeshore", "2026-09-20", "beer", 100.0),
             L("Binny's", "2026-10-10", "liquor", 50.0, kind="backoffice")]
    sales = daily(date(2026, 8, 1), 80, beer=500)
    span = bp.window_span(lines, sales, date(2026, 10, 15))
    assert span["hi"] == "2026-09-20"


# ── ING-20: never across draft / can or a stated size ───────────────────────

def test_ing_20_a_draft_button_is_never_offered_the_cans(db):
    lines = [{"product_key": "lake|1001", "description": "MILLER LITE 24/12 CAN", "uom": "CA", "category": "beer",
              "kind": "item"}]
    cands = bv.pour_candidates(lines)
    assert bv.suggest_pour("Miller Lite Draft", "beer", cands) == {}
    assert bv.suggest_pour("Miller Lite 16oz Can", "beer", cands) == {}
    assert bv.suggest_pour("Miller Lite Can", "beer", cands)["product_key"] == "lake|1001"
    assert not bv.form_fits("Miller Lite Draft", "beer", False) and bv.form_fits("Miller Lite Draft", "beer", True)


# ── AI-09: no measured input, no evidence figure ────────────────────────────

def test_ai_09_confidence_with_nothing_measured_is_none():
    assert bj.confidence()["evidence_pct"] is None
    assert bj.confidence(lag_days=1) == {"evidence_pct": None, "freshness_pct": 100}
    assert bj.confidence(invoices=2)["evidence_pct"] == 50


def test_ai_09_the_concentration_card_counts_purchases_not_products():
    from bar.insights import _price_cards
    ups = [{"persistent": True, "kind": "increase", "forward_monthly_usd": v, "purchases": 1, "confidence": "low"}
           for v in (60, 50, 40, 5, 5, 5, 5)]
    card = next(c for c in _price_cards({"increases": ups}, {}) if c["key"] == "concentration")
    assert card["confidence"]["evidence_pct"] == 100          # seven purchases (≥ 4), not seven products
    ups = [dict(u, purchases=0) for u in ups]
    card = next(c for c in _price_cards({"increases": ups}, {}) if c["key"] == "concentration")
    assert card["confidence"]["evidence_pct"] is None


# ── POS-14: the comps block counts the 100% check comps ─────────────────────

def test_pos_14_comps_by_shift_totals_carry_the_check_discount_comps(db):
    import bar.comps as bcomps
    rid = tv._rid(db)
    tv._nights(db, rid, 0, 10)
    tv._ring(db, rid, 3, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans", sales=6.0, ticket="TK1")
    tv._ring(db, rid, 3, "DSC", "100% Comp", "Discounts", "", kind="discount", ticket="TK1", sales=-6.0)
    c = sqlite3.connect(db)
    c.execute("UPDATE pos_ticket_lines SET sales=-6.0 WHERE restaurant_id=? AND kind='discount'", (rid,))
    c.commit()
    c.close()
    out = bcomps.comps_block(rid, today=TODAY, db_path=db)
    t = out["totals"]
    assert t["check_comps_usd"] == 6.0 and t["comp_usd"] == t["comp_lines_usd"] + 6.0
    assert "100%" in out["comps_text"]


# ── SCALE-02: the stamp reads only the columns the build reads ──────────────

def test_scale_02_a_tab_switch_never_moves_the_bar_stamp(db):
    rid = tv._rid(db)
    tv._miller(db, rid)
    before = bcache.stamp(rid, db_path=db, today=TODAY)
    c = sqlite3.connect(db)
    c.execute("UPDATE restaurants SET last_activity='2026-10-16 12:00:01', last_fetched_at='2026-10-16 12:00:02' "
              "WHERE id=?", (rid,))
    c.commit()
    c.close()
    assert bcache.stamp(rid, db_path=db, today=TODAY) == before
    models.update_restaurant(rid, {"bar_cost_target": 19.0}, db_path=db)
    assert bcache.stamp(rid, db_path=db, today=TODAY) != before
    assert "last_activity" not in bcache.RESTAURANT_COLUMNS and "bar_cost_target" in bcache.RESTAURANT_COLUMNS


# ── SCALE-08: at most two cold builds at once ───────────────────────────────

def test_scale_08_cold_builds_share_two_slots(db, monkeypatch):
    bcache.clear()
    monkeypatch.setattr(bcache, "stamp", lambda rid, db_path=None, today=None: f"s{rid}")
    running, peak, lock = [0], [0], threading.Lock()

    def slow(rid, db_path=None, today=None):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.15)
        with lock:
            running[0] -= 1
        return {"ok": True, "rid": rid}
    ts = [threading.Thread(target=bcache.full, args=(rid,), kwargs={"db_path": db, "build": slow})
          for rid in (101, 102, 103, 104)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert peak[0] <= bcache.BUILD_CONCURRENCY == 2


def test_scale_08_and_07_a_sweep_never_keeps_what_it_builds(db, monkeypatch):
    bcache.clear()
    monkeypatch.setattr(bcache, "stamp", lambda rid, db_path=None, today=None: "s")
    calls = []

    def build(rid, db_path=None, today=None):
        calls.append(rid)
        return {"ok": True}
    bcache.full(7, db_path=db, build=build, keep=False)
    bcache.full(7, db_path=db, build=build, keep=False)
    assert calls == [7, 7]
    bcache.full(7, db_path=db, build=build)
    bcache.full(7, db_path=db, build=build, keep=False)        # a kept copy is read
    assert calls == [7, 7, 7]


# ── SCALE-09: the waste log never reads every invoice line ──────────────────

def test_scale_09_logging_waste_reads_no_invoice_lines(db, monkeypatch):
    import bar.purchases as bpur
    import bar.waste as bw
    rid = tv._rid(db)
    tv._miller(db, rid)

    def boom(*a, **k):
        raise AssertionError("every invoice line read")
    monkeypatch.setattr(bpur, "_lines", boom)
    res = bw.log_waste(rid, tv._iso(5), "lakeshore beverage|1001", 1, "each", "spill", today=TODAY, db_path=db)
    assert res["ok"] and res["product"]
    assert "lakeshore beverage|1001" in bv.product_names(rid, db_path=db)


# ── SCALE-10: one waste entry per Idempotency-Key ───────────────────────────

def test_scale_10_a_replayed_waste_post_logs_once(db, monkeypatch):
    rid = tv._rid(db)
    tv._miller(db, rid)
    tv._as(monkeypatch, rid)
    c = tv._app().test_client()
    body = {"waste_date": (date.today() - timedelta(days=1)).isoformat(), "product_key": "lakeshore beverage|1001",
            "qty": 2, "unit": "each", "reason": "spill"}
    a = c.post("/api/food-cost/bar/waste", json=body, headers={"Idempotency-Key": "spill-1"})
    b = c.post("/api/food-cost/bar/waste", json=body, headers={"Idempotency-Key": "spill-1"})
    assert a.status_code == b.status_code == 200 and a.get_json()["id"] == b.get_json()["id"]
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM bar_waste WHERE restaurant_id=?", (rid,)).fetchone()[0]
    assert n == 1
    c.post("/api/food-cost/bar/waste", json=body, headers={"Idempotency-Key": "spill-2"})
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM bar_waste WHERE restaurant_id=?", (rid,)).fetchone()[0] == 2


# ── SCALE-07: the price push fans out as queue rows ─────────────────────────

def test_scale_07_the_price_push_queues_rows_and_pushes_nothing_inline(db, monkeypatch):
    from datetime import datetime
    import bar_signals
    import job_queue
    import scheduler
    rid = tv._rid(db)
    tv._miller(db, rid)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(bar_signals, "push_increase", lambda *a, **k: (_ for _ in ()).throw(AssertionError("inline")))
    monday_10 = datetime(2026, 10, 12, 10, 0)
    out = bar_signals.run_price_push(db_path=db, now=monday_10)
    assert out["attempted"] == 1 and out["queued"] == 1
    again = bar_signals.run_price_push(db_path=db, now=monday_10)
    assert again["queued"] == 0                              # one row per restaurant and local day
    assert job_queue.HANDLERS["bar_price_push"] == "bar_signals:run_queued_price_push"
    row = sqlite3.connect(db).execute("SELECT kind, restaurant_id FROM jobs WHERE kind='bar_price_push'").fetchone()
    assert row == ("bar_price_push", rid)
    tuesday = datetime(2026, 10, 13, 10, 0)
    assert bar_signals.run_price_push(db_path=db, now=tuesday)["queued"] == 0


# ── UI-13 / UI-19 / UI-18 / AI-09: the page's own code ──────────────────────

def _src():
    import os
    return open(os.path.join(tv.ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_ui_13_19_18_the_page_code():
    src = _src()
    board = src[src.index("function sigList(d)"):src.index("function signals(d)")]
    assert "usd: +v.unmapped_sales_usd" not in board and "* 3, fig" not in board and "perMo(" in board
    assert "sparkFit(sp, allSp)" in src
    alerts = src[src.index("function alerts(d, pc)"):src.index("function sparkFit(")]
    assert "Fintech" not in alerts and "feed.label" in alerts
    assert "Evidence ' + (cf.evidence_pct != null" in src


def test_scale_07_the_queued_push_claims_its_day_once(db, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    import bar_signals
    import scheduler
    rid = tv._rid(db)
    sent = []
    monkeypatch.setattr(bar_signals, "push_increase", lambda r, today=None, **k: sent.append((r, today)) or 1)
    monkeypatch.setattr(bar_signals, "_push_due", lambda r, now=None: (True, datetime(2026, 10, 12, 10, 0)))
    claims = set()

    def local_due(r, h, until=14, claim_key=None, now_local=None, day=None):
        k = (claim_key, now_local.date())
        if k in claims:
            return False
        claims.add(k)
        return True
    monkeypatch.setattr(scheduler, "local_due", local_due)
    job = SimpleNamespace(payload={"restaurant_id": rid, "day": "2026-10-12"})
    assert bar_signals.run_queued_price_push(job) == {"pushed": 1}
    assert bar_signals.run_queued_price_push(job) == {"skipped": "already claimed"} and len(sent) == 1
