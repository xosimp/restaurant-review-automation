"""Regression coverage for the Food Cost + Bar & Alcohol blind audit of
10/16/26 (registers in /Users/simp/fc_bar_audit_1016: 01-fc-calc, 02-bar-calc,
03-match-ingest, 04-pos-time, 05-ai, 07-sec-scale).

One strict xfail per confirmed defect, citing its finding ID. Each test
asserts the CORRECT behaviour, so it fails today because of the defect and
XPASSes (and so breaks the run) the day the fix lands: whoever fixes it
removes the xfail mark and the test stays as ordinary coverage.

Skipped (not covered here, and why):
- None of the requested findings was skipped. FC-CALC-12 is covered at unit
  level only (waste_trend._week_from_row on a snapshot whose waste was never
  measured); the scheduled writer (food_cost_intelligence.weekly_snapshot)
  needs inventory.analysis_for's live-data path, which is not cheap to stage.
- SCALE-01 is asserted on call counts (book / _invoice_prices once per
  waste_periods call), not on wall time, so it stays deterministic.
"""
from datetime import date, timedelta

import pytest

import backoffice_ap as bo
import backoffice_counts as bc
import backoffice_recipes as br
import fintech_invoices as fi
from tests.test_backoffice_ap import TODAY, _conn, _rid, db  # noqa: F401
from tests.test_backoffice_counts import _count, _kitchen
import tests.test_bar_variance as tv

B, C, NB = bo._BULLET, bo._CHECK, "\xa0"
HEAD = ["10/15/26, 4:14 PMBack Office - Invoices By Vendor",
        f"{NB} Invoice # ItemizedDate Amount Sent to RASI Terms Check # Check Date"]
FT_HEAD = ("Vendor Name,Invoice Number,Invoice Date,Invoice Total,Location ID,Product Number,Product Description,"
           "Unit Of Measure,Item Quantity,Item Cost,Extended Price\n")
POUR_COA = (
    "GLAccountSubTypeName,FinancialStatementTypeName,CorporateID,MainAccountNumber,GLAccountDescription,"
    "UseForWebIndicator,UnitID\n"
    "Purchase,Income and Expense Statement,7101,4010,4010 PRODUCE,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4320,4320 BEER,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4330,4330 WINE,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4370,4370 LIQUOR,True,7101\n"
)
PRODUCE_COA = (
    "GLAccountSubTypeName,FinancialStatementTypeName,CorporateID,MainAccountNumber,GLAccountDescription,"
    "UseForWebIndicator,UnitID\n"
    "Purchase,Income and Expense Statement,7101,4010,4010 PRODUCE,True,7101\n"
)


def _printout(lines):
    return "\n".join(HEAD + lines)


def _food_nights(db, rid, first, last, value=1000.0):
    """sales.cat:Food every night from `first` to `last` inclusive."""
    c = _conn(db)
    d = first
    while d <= last:
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                  "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, d.isoformat(), "sales.cat:Food", value))
        d += timedelta(days=1)
    c.commit()
    c.close()


def _fin_invoice(db, rid, dist, number, day, lines, total=None):
    """A Fintech invoice as ingest stores it (distributor_key and number_key
    set): lines [(item_code, description, uom, qty, unit_price)]."""
    c = _conn(db)
    try:
        inv = c.execute(
            "INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
            "source_format, updated_at, balanced, distributor_key, number_key) "
            "VALUES (?,?,?,?,?, 'export', '2026-10-01 00:00:00', 1, ?, ?)",
            (rid, dist, number, day, total if total is not None else sum(q * p for _c, _d, _u, q, p in lines),
             fi.distributor_key(dist), fi.invoice_key(number))).lastrowid
        for i, (code, desc, uom, qty, price) in enumerate(lines):
            c.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, uom, qty, "
                      "unit_price, cost, kind, restaurant_id) VALUES (?,?,?,?,?,?,?,?, 'item', ?)",
                      (inv, i + 1, code, desc, uom, qty, price, round(qty * price, 2), rid))
        c.commit()
    finally:
        c.close()
    return inv


def _price(item, invoices):
    return br.item_price(item, invoices, today=date(2026, 10, 16))


# ── FC-CALC: food cost calculations ───────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="FC-CALC-01: Back Office food cost returns another period's COGS for the window asked")
def test_fc_calc_01_back_office_food_cost_stays_inside_the_window_it_is_asked_for(db):
    """Monthly counts 8/31 and 9/30. October month to date (16 days to
    10/16) must not come back as September's 9/1-9/30 COGS."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", PRODUCE_COA.encode(), db_path=db)
    _food_nights(db, rid, date(2026, 8, 1), date(2026, 10, 15))
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": f"7700{i}", "date": day.isoformat(), "total": "$3,000.00",
         "items": [{"sku": "1", "name": "LETTUCE", "gl": "4010 PRODUCE", "unit": "Case", "qty": "100", "amt": "30",
                    "ext": "3000.00"}]}
        for i, day in enumerate([date(2026, 9, 5), date(2026, 9, 15), date(2026, 9, 25), date(2026, 10, 5),
                                 date(2026, 10, 12)])]}, db_path=db)

    def cnt(end, food):
        return {"start": (end - timedelta(days=29)).strftime("%m/%d/%Y"), "end": end.strftime("%m/%d/%Y"),
                "status": "Submitted", "gls": [{"gl": "4011", "name": "PRODUCE INVENTORY", "amount": f"{food:.2f}"}]}
    bc.save_counts(rid, {"inventory_counts": [cnt(date(2026, 8, 31), 4000.0), cnt(date(2026, 9, 30), 5000.0)]},
                   db_path=db)
    import cogs
    today, days = date(2026, 10, 16), 16
    out = cogs.build_food_cost_pct(rid, days=days, today=today, db_path=db)
    asked_from = today - timedelta(days=days - 1)
    got_from = out["start"] if isinstance(out["start"], date) else date.fromisoformat(str(out["start"]))
    assert out["ok"] is False or got_from >= asked_from, \
        f"asked for {asked_from}..{today}, got COGS {out['cogs']} for {out['start']}..{out['end']}"


@pytest.mark.xfail(strict=True, reason="FC-CALC-03 / POS-01: bought-vs-sold headline divides 28 days of invoices by 7 nights of sales")
def test_fc_calc_03_bought_vs_sold_headline_needs_sales_for_its_window(db):
    """The nightly report started a week ago (7 nights x $1,000); $2,800 of
    invoices weekly for four weeks. 160% is not a reading; the window must
    start at the first sales night, or withhold pct."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", PRODUCE_COA.encode(), db_path=db)
    t = date(2026, 10, 16)
    _food_nights(db, rid, t - timedelta(days=7), t - timedelta(days=1))
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": f"N{i}", "date": (t - timedelta(days=1 + 7 * i)).isoformat(),
         "total": "$2,800.00", "items": [{"sku": "1", "name": "LETTUCE", "gl": "4010 PRODUCE", "unit": "Case",
                                          "qty": "100", "amt": "28", "ext": "2800.00"}]} for i in range(4)]},
        db_path=db)
    w = bo.food_bought_vs_sold(rid, today=t, db_path=db)["window"]
    assert w["pct"] is None or (w["from"] >= (t - timedelta(days=7)).isoformat() and w["pct"] < 100), w


@pytest.mark.xfail(strict=True, reason="FC-CALC-04: a credit memo under its invoice's number overwrites the invoice")
def test_fc_calc_04_a_credit_memo_sharing_its_invoice_number_is_kept_beside_it(db):
    """GFS 9041200735 +$900 on 10/1 and the same number -$200 on 10/3; the
    vendor total prints $700 and both rows must be stored."""
    rid = _rid(db)
    p = bo.parse_invoices_by_vendor(_printout([
        f"{B}GORDON FOOD SERVICE{NB} $700.00",
        f"{NB} 9041200735 {C} 10/01/2026 {NB}$900.00 {NB} 0{NB} Debit/EFT",
        f"{NB} 9041200735 {C} 10/03/2026 {NB}-$200.00 {NB} 0{NB} Debit/EFT"]))
    assert p["mismatched"] == [], "the printout itself adds up"
    bo.save_invoices(rid, p, db_path=db)
    c = _conn(db)
    rows = c.execute("SELECT amount FROM bo_invoices WHERE restaurant_id=?", (rid,)).fetchall()
    c.close()
    assert len(rows) == 2 and round(sum(r["amount"] for r in rows), 2) == 700.0, [r["amount"] for r in rows]


def test_fc_calc_06_a_bar_only_count_is_not_zero_food_on_hand(db):
    """9/29 count: produce $6,000 + beer. 10/13: beer only (the kitchen did
    not count). Closing food is unmeasured, never $0."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", PRODUCE_COA.encode(), db_path=db)
    _food_nights(db, rid, date(2026, 9, 1), date(2026, 10, 15))
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": "1", "date": "2026-10-05", "total": "$4,000.00",
         "items": [{"sku": "1", "name": "LETTUCE", "gl": "4010 PRODUCE", "unit": "Case", "qty": "100", "amt": "40",
                    "ext": "4000.00"}]}]}, db_path=db)

    def cnt(end, gls):
        return {"start": (end - timedelta(days=6)).strftime("%m/%d/%Y"), "end": end.strftime("%m/%d/%Y"),
                "status": "Submitted", "gls": gls}
    bc.save_counts(rid, {"inventory_counts": [
        cnt(date(2026, 9, 29), [{"gl": "4011", "name": "PRODUCE INVENTORY", "amount": "6000.00"},
                                {"gl": "4321", "name": "BEER INVENTORY", "amount": "900"}]),
        cnt(date(2026, 10, 13), [{"gl": "4321", "name": "BEER INVENTORY", "amount": "950"}])]}, db_path=db)
    import cogs
    out = cogs.build_food_cost_pct(rid, days=14, today=date(2026, 10, 14), db_path=db)
    assert out["ok"] is False and out["pct"] is None, (out["closing"], out["pct"], out["missing"])


def test_fc_calc_08_whole_pound_catch_weight_is_still_a_price_per_pound():
    """A 20 lb case billed per pound: 20.4 lb at $4.50 reads $4.50/lb; 20.00
    lb at $4.50 must too (or be refused), never $0.225/lb."""
    cheddar = {"name": "CHEESE CHEDDAR", "gl": "4050 DAIRY", "vendors": [
        {"vendor": "GFS", "sku": "555", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "20",
         "price": "90.00", "last_purchased": "10/01/2026"}]}
    frac = _price(cheddar, {"555": {"item_code": "555", "purchase_unit": "Case", "qty": 20.4, "unit_amount": 4.50,
                                    "invoice_date": "2026-10-10"}})
    assert abs(frac["per_base"] - 4.50) < 0.01, "the fractional weight already reads right"
    whole = _price(cheddar, {"555": {"item_code": "555", "purchase_unit": "Case", "qty": 20.0, "unit_amount": 4.50,
                                     "invoice_date": "2026-10-10"}})
    assert "error" in whole or abs(whole["per_base"] - 4.50) < 0.01, whole.get("per_base")
    parm = {"name": "Parm", "gl": "4020 DAIRY", "vendors": [
        {"vendor": "GFS", "sku": "300", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "20",
         "price": "9.10", "last_purchased": "10/01/2026"}]}
    got = _price(parm, {"300": {"purchase_unit": "Case", "qty": 20.0, "unit_amount": 9.10, "invoice_date": "2026-10-10"}})
    assert "error" in got or got["per_base"] > 1.0, got.get("per_base")


def test_fc_calc_08_a_split_case_is_not_read_as_billed_by_weight():
    """Half a 10 lb case of mushrooms (qty 0.5) at the $25 case price is
    $2.50/lb, or refused - never $25/lb."""
    item = {"name": "MUSHROOM", "gl": "4010 PRODUCE", "vendors": [
        {"vendor": "GFS", "sku": "777", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "10",
         "price": "25.00"}]}
    got = _price(item, {"777": {"item_code": "777", "purchase_unit": "Case", "qty": 0.5, "unit_amount": 25.0,
                                "invoice_date": "2026-10-10"}})
    assert "error" in got or abs(got["per_base"] - 2.50) < 0.01, got.get("per_base")


def test_fc_calc_09_a_line_with_no_unit_amount_is_never_a_zero_price():
    item = {"name": "CHEESE CHEDDAR", "gl": "4050 DAIRY", "vendors": [
        {"vendor": "GFS", "sku": "555", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "20",
         "price": "90.00", "last_purchased": "10/01/2026"}]}
    got = _price(item, {"555": {"item_code": "555", "purchase_unit": "Case", "qty": 2.0, "unit_amount": None,
                                "invoice_date": "2026-10-10"}})
    assert "error" in got or got["per_base"] > 0, got.get("per_base")


def test_fc_calc_09_a_credit_line_never_becomes_the_ingredient_price(db):
    """Parmesan bought at $9.10/lb (a 19.6 lb case), then a -1 case credit
    for -$182: the price stays $9.10/lb."""
    rid = _rid(db)
    bo.save_lines(rid, "GORDON FOOD SERVICE", "9001", [
        {"item_code": "300", "description": "PARM", "gl": "4020", "purchase_unit": "Case", "qty": 19.6,
         "unit_amount": 9.10, "ext_amount": 178.36}], invoice_date="2026-10-05", db_path=db)
    bo.save_lines(rid, "GORDON FOOD SERVICE", "9002", [
        {"item_code": "300", "description": "PARM CREDIT", "gl": "4020", "purchase_unit": "Case", "qty": -1,
         "unit_amount": -182.0, "ext_amount": -182.0}], invoice_date="2026-10-12", db_path=db)
    parm = {"name": "Parm", "gl": "4020 DAIRY", "vendors": [
        {"vendor": "GFS", "sku": "300", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "20",
         "price": "182.00", "last_purchased": "10/01/2026"}]}
    got = _price(parm, br._invoice_prices(rid, db_path=db))
    assert "error" not in got and got["per_base"] > 0 and abs(got["per_base"] - 9.10) < 0.01, got.get("per_base")


def test_ing_06_a_sku_shared_across_vendors_prices_each_from_its_own(db):
    """GFS 591122 is a $39.26 onion sack; Sysco 591122 a $48 10 lb case of
    cheddar. The cheddar is $4.80/lb, never priced off the onions."""
    rid = _rid(db)
    bo.save_lines(rid, "GORDON FOOD SERVICE", "9041", [
        {"item_code": "591122", "description": "ONION SPANISH 50#", "gl": "4010", "purchase_unit": "Case", "qty": 1,
         "unit_amount": 39.26, "ext_amount": 39.26}], invoice_date="2026-10-10", db_path=db)
    bo.save_lines(rid, "SYSCO CHICAGO", "7001", [
        {"item_code": "591122", "description": "CHEESE CHED SHRD", "gl": "4020", "purchase_unit": "Case", "qty": 1,
         "unit_amount": 48.00, "ext_amount": 48.00}], invoice_date="2026-10-01", db_path=db)
    cheese = {"name": "Cheddar", "gl": "4020 DAIRY", "vendors": [
        {"vendor": "SYSCO", "sku": "591122", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "10",
         "price": "48.00", "last_purchased": "10/01/2026"}]}
    got = _price(cheese, br._invoice_prices(rid, db_path=db))
    assert abs(got["per_base"] - 4.80) < 0.01, got.get("per_base")


@pytest.mark.xfail(strict=True, reason="FC-CALC-12: a snapshot with no measured waste is charted as a $0 week")
def test_fc_calc_12_a_week_with_no_waste_measured_is_not_zero_waste():
    """A stored snapshot whose waste figure is null (nothing logged) reads
    waste None in the trend, never a measured $0."""
    import json
    import waste_trend
    week = waste_trend._week_from_row("2026-10-11", json.dumps({"total_waste_cost": None, "top_items": []}), None)
    assert week is None or week["waste"] is None, week["waste"]


# ── BAR-CALC: bar calculations ────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="BAR-CALC-01: stock bought before the counted span makes all usage read as variance")
def test_bar_calc_01_counted_variance_prices_stock_bought_before_the_span(db):
    """Beer counted 9/10 -> 10/10 with $80 used: exactly 40 Miller cans at $2,
    all sold and matched; the cases came 9/1, before the span. Variance is
    about $0 - or withheld - never $80."""
    import bar.variance as bv
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("1001", "MILLER LITE 24/12 CAN", "CA", 2, 48.0)])
    tv._invoice(db, rid, "A2", tv._iso(39), [("1002", "COORS LIGHT 24/12 CAN", "CA", 1, 48.0)])
    tv._nights(db, rid, 0, 40)
    for i in range(40):
        tv._ring(db, rid, 10 + (i % 29), "M1", "Miller Lite Btl", "Beer", "Bottles & Cans")
    tv._map(db, rid, "M1", "lakeshore beverage|1001")
    block = {"periods": [{"from": tv._iso(9), "to": tv._iso(38), "ok": True,
                          "categories": {"beer": {"used": 80.0}, "liquor": {"used": None}, "wine": {"used": None}}}]}
    beer = bv.compute(rid, today=tv.TODAY, db_path=db, counted=block)["variance"]["categories"]["beer"]
    assert beer.get("variance_usd") is None or abs(beer["variance_usd"]) < 1.0, beer


@pytest.mark.xfail(strict=True, reason="BAR-CALC-04: a case line with no unit of measure is read as one can")
def test_bar_calc_04_a_uom_less_case_line_is_never_one_can(db):
    """'MILLER LITE 24/12 CAN', uom NULL, 2 at $48: 48 cans, or no dollars at
    all - never $48 a can and '$1,824 more sold than bought'."""
    import bar.variance as bv
    rid = tv._rid(db)
    tv._invoice(db, rid, "A1", tv._iso(0), [("1001", "MILLER LITE 24/12 CAN", None, 2, 48.0)])
    tv._invoice(db, rid, "A2", tv._iso(30), [("2002", "TITOS VODKA 1L", "BO", 1, 25.0)])
    tv._nights(db, rid, 0, 34)
    for i in range(40):
        tv._ring(db, rid, i % 30, "M1", "Miller Lite Btl", "Beer", "Bottles & Cans")
    tv._map(db, rid, "M1", "lakeshore beverage|1001")
    v = bv.compute(rid, today=tv.TODAY, db_path=db)["variance"]
    miller = [m for m in v["items"] if "MILLER" in str(m.get("product") or "").upper()
              or m.get("product_key") == "lakeshore beverage|1001"]
    for m in miller:
        assert m.get("bought_units") in (48, None), m.get("bought_units")
        assert m.get("gap_usd") is None or m["gap_usd"] > -100, m.get("gap_usd")
    assert (v["totals"].get("gap_usd") or 0) > -1000, v["totals"].get("gap_usd")


@pytest.mark.xfail(strict=True, reason="BAR-CALC-06: the stock-up guard runs only above the range top, so a delivery reads over target")
def test_bar_calc_06_a_stock_up_below_the_range_top_is_not_judged():
    """Beer steady at 20% (target 20%), one extra $3,000 delivery in the
    window: a stock-up, held back - never '$3,000 over your target'."""
    from bar.pour_cost import _pour_cost
    today, end = date(2026, 10, 15), date(2026, 10, 13)
    lines, sales = [], {"beer": {}, "liquor": {}, "wine": {}}
    d, n = end - timedelta(days=120), 0
    while d <= end:
        sales["beer"][d.isoformat()] = 20000 / 7
        if d.weekday() == 0:
            n += 1
            lines.append({"invoice_date": d.isoformat(), "category": "beer", "net": 4000.0, "distributor": "LAKESHORE",
                          "kind": "item", "invoice_number": f"W{n}", "balanced": True})
        d += timedelta(days=1)
    lines.append({"invoice_date": (end - timedelta(days=5)).isoformat(), "category": "beer", "net": 3000.0,
                  "distributor": "LAKESHORE", "kind": "item", "invoice_number": "BIG", "balanced": True})
    lines.sort(key=lambda l: l["invoice_date"])
    beer = next(c for c in _pour_cost(lines, sales, today, targets={"beer": 20})["categories"] if c["key"] == "beer")
    assert beer["judged"] is False, (beer["pct"], beer.get("stock_up"), beer.get("over_target_usd"), beer["range"])


@pytest.mark.xfail(strict=True, reason="BAR-CALC-07: drink margins cost a bottle of wine sold as it comes as a 5-oz glass")
def test_bar_calc_07_a_bottle_of_wine_mapped_with_no_pour_costs_the_bottle(db):
    """'Btl Chateau Souverain' mapped with no pour (a bottle sold as it comes)
    to a $12 bottle, $40 on the menu: cost $12, 30% - not a 5 oz glass."""
    import models
    from bar import margins as bm
    from bar import prices as bp
    from bar.classify import product_key
    from bar.purchases import _lines
    from tests.test_bar_prices import _buys, _store
    rid = _rid(db)
    dist = "Breakthru Beverage Illinois"
    _store(db, rid, _buys(dist, "7701", "CH SOUVERAIN CHARD 750ML", "BO",
                          [("2026-09-01", 12, 12.0), ("2026-09-15", 12, 12.0), ("2026-10-01", 12, 12.0)]))
    conn = models.get_conn(db)
    conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, "rpower", "W1", "1", "Btl Chateau Souverain", "Regular", 40.0))
    conn.commit()
    conn.close()
    a = bp.analyse(_lines(rid, db), date(2026, 10, 14))
    bm.set_drink_map(rid, "rpower", "W1", product_key(dist, "7701", ""), pour_oz=None, db_path=db)
    rows = [r for r in bm.margins(rid, a, db_path=db)["rows"] if r["item_id"] == "W1"]
    assert rows, "the confirmed pair is read"
    assert rows[0]["cost"] == 12.0 and rows[0]["cost_pct"] == 30.0, (rows[0]["cost"], rows[0]["pour_label"])


@pytest.mark.xfail(strict=True, reason="BAR-CALC-09 / ING-15: the pack parser misses N/SIZE shapes like 6/1.75L and 12/750ML")
@pytest.mark.parametrize("desc,uom,cat,units,ml", [
    ("TITOS VDK 6/1.75L", "CA", "liquor", 6, 1750.0),
    ("TITOS VDK 12/750ML", "CA", "liquor", 12, 750.0),
    ("JAMESON 12/1L", "CA", "liquor", 12, 1000.0),
])
def test_bar_calc_09_the_pack_parser_reads_count_slash_size(desc, uom, cat, units, ml):
    from bar.products import parse_pack
    pk = parse_pack(desc, uom, cat)
    assert (pk["units_per_case"], pk["units_source"]) == (units, "parsed"), pk
    assert pk["container_ml"] == ml and pk["size_source"] == "parsed", pk


# ── ING: Back Office <-> Fintech matching and intake ─────────────────────

@pytest.mark.xfail(strict=True, reason="ING-01: an itemized BO invoice split across GLs is counted again against Fintech")
def test_ing_01_an_itemized_invoice_on_two_accounts_matches_fintech_whole(db):
    """Breakthru 555 ($1,010) is on Fintech; its Back Office page has liquor
    $400, wine $600 and $10 tax. None of it is alcohol outside Fintech."""
    rid = _rid(db)
    bo.save_coa(rid, bo.parse_coa(POUR_COA.encode()), db)
    _fin_invoice(db, rid, "Breakthru Beverage Illinois, LLC", "0555", "2026-09-10",
                 [("1", "TITOS VDK 1L", "CA", 1, 400.0), ("2", "WHITEHAVEN SAUV BL", "CA", 1, 600.0)], total=1010.0)
    bo.save_invoice_page(rid, {"vendor": "BREAKTHRU BEVERAGE IL", "invoice_number": "555", "date": "2026-09-10",
                               "total": 1010.0, "lines": [
                                   {"item_code": "1", "description": "TITOS", "gl": "4370", "purchase_unit": "Case",
                                    "qty": 1, "unit_amount": 400.0, "ext_amount": 400.0},
                                   {"item_code": "2", "description": "WHITEHAVEN", "gl": "4330",
                                    "purchase_unit": "Case", "qty": 1, "unit_amount": 600.0, "ext_amount": 600.0}]},
                         db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    assert out["lines"] == [], [(l["category"], l["cost"]) for l in out["lines"]]


@pytest.mark.xfail(strict=True, reason="ING-02: the invoice-number match ignores the distributor")
def test_ing_02_an_invoice_number_match_needs_the_same_distributor(db):
    """Oak Wine Cellars' invoice 1001 ($480) is not Breakthru's Fintech 1001
    ($250); Breakthru's $250 payment is that Fintech invoice."""
    rid = _rid(db)
    bo.save_coa(rid, bo.parse_coa(POUR_COA.encode()), db)
    _fin_invoice(db, rid, "Breakthru Beverage Illinois, LLC", "1001", "2026-09-01",
                 [("1", "TITOS VDK 1L", "CA", 1, 250.0)])
    bo.save_invoices(rid, bo.parse_invoices_by_vendor(_printout([
        f"{B}OAK WINE CELLARS{NB} $480.00",
        f"{NB} 1001 {NB} 09/03/2026 {NB}$480.00 {NB} GL: 4330 {NB} , {NB}",
        f"{B}BREAKTHRU BEVERAGE IL{NB} $250.00",
        f"{NB} CM Entry {NB} 09/20/2026 {NB}$250.00 {NB} GL: 4370 {NB} , {NB}"])), db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    counted = sorted((e["vendor"], e["amount"]) for e in out["entries"])
    assert counted == [("OAK WINE CELLARS", 480.0)] and out["matched"] == 1, counted


@pytest.mark.xfail(strict=True, reason="ING-03 / FC-CALC-05: a food vendor sharing a first word with a Fintech beer house is filed as beer")
def test_ing_03_a_food_vendor_is_not_matched_to_a_fintech_house_on_one_word(db):
    """Back Office 'CHICAGO PRODUCE CO' with Fintech 'Chicago Beverage
    Systems' on file: not beer, not from Fintech, and not in the Bar."""
    rid = _rid(db)
    bo.save_coa(rid, bo.parse_coa(POUR_COA.encode()), db)
    _fin_invoice(db, rid, "Chicago Beverage Systems", "77", "2026-09-01",
                 [("1", "MILLER LITE 1/2 BBL", "KE", 2, 150.0)])
    bo.save_invoices(rid, bo.parse_invoices_by_vendor(_printout([
        f"{B}CHICAGO PRODUCE CO{NB} $812.40",
        f"{NB} 88231 {C} 09/05/2026 {NB}$812.40 {NB} 0{NB} Debit/EFT{NB} {NB}"])), db_path=db)
    cat = bo.vendor_categories(rid, db_path=db)["chicago produce"]
    assert cat["source"] != "fintech" and cat["category"] not in ("beer", "wine", "liquor"), cat
    assert bo.outside_fintech(rid, db_path=db)["lines"] == []


@pytest.mark.xfail(strict=True, reason="FC-CALC-05 / ING-03: _match_fintech_vendor matches on a single shared first word")
@pytest.mark.parametrize("vk,fin", [
    ("chicago meat authority", "chicago beverage systems"),
    ("southern food group", "southern glazers wine and spirits of il"),
])
def test_fc_calc_05_one_shared_first_word_is_not_the_same_vendor(vk, fin):
    assert bo._match_fintech_vendor(vk, [fin]) is None


@pytest.mark.xfail(strict=True, reason="ING-04: one payment covering three Fintech invoices is counted again")
def test_ing_04_a_batch_payment_for_three_fintech_invoices_is_matched(db):
    """Breakthru Fintech invoices $300 + $450 + $250; one $1,000 check-mode
    entry pays them. Nothing is counted outside Fintech."""
    rid = _rid(db)
    bo.save_coa(rid, bo.parse_coa(POUR_COA.encode()), db)
    for n, amt in (("A1", 300.0), ("A2", 450.0), ("A3", 250.0)):
        _fin_invoice(db, rid, "Breakthru Beverage Illinois, LLC", n, "2026-09-01", [("1", "TITOS VDK 1L", "CA", 1, amt)])
    bo.save_invoices(rid, bo.parse_invoices_by_vendor(_printout([
        f"{B}BREAKTHRU BEVERAGE IL{NB} $1,000.00",
        f"{NB} CM Entry {NB} 09/25/2026 {NB}$1,000.00 {NB} GL: 4370 {NB} , {NB}"])), db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    assert out["lines"] == [], [(l["category"], l["cost"]) for l in out["lines"]]


@pytest.mark.xfail(strict=True, reason="ING-05: email_intake._unsigned_not_forged accepts a DMARC fail as merely unsigned")
def test_ing_05_a_dmarc_fail_is_not_unsigned_mail():
    import email_intake as ei
    assert ei._unsigned_not_forged({"spf": "none", "dkim": "none", "dmarc": "fail"}) is False


@pytest.mark.xfail(strict=True, reason="ING-05: fintech_invoices.vouch passes a forged file with no store column")
def test_ing_05_vouch_refuses_a_file_that_names_no_store(db):
    rid = _rid(db)
    real = FT_HEAD + ("Breakthru Beverage Illinois LLC,128850598,10/01/2026,420.00,000001 St. Charles,9031,"
                      "TITOS VDK 1L,CA,2,210.00,420.00\n")
    assert fi.ingest_file(rid, "real.csv", real.encode(), db_path=db)["status"] == "stored"
    forged = ("Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,"
              "Unit Of Measure,Item Quantity,Item Cost,Extended Price\n"
              "Breakthru Beverage Illinois LLC,999000111,10/14/2026,9800.00,9031,TITOS VDK 1L,CA,40,245.00,9800.00\n")
    ok, why = fi.vouch(rid, "forged.csv", forged.encode(), db_path=db)
    assert ok is False, why


@pytest.mark.xfail(strict=True, reason="ING-09: invoices.match_ingredient prices 'Cream' from SOUR CREAM and pre-selects it verified")
def test_ing_09_sour_cream_is_not_verified_as_cream(db):
    import invoices
    rid = _rid(db)
    c = _conn(db)
    c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, case_size, is_active) "
              "VALUES (?,?,?,?,?,1)", (rid, "Cream", "qt", 3.10, 1))
    c.commit()
    c.close()
    prop = invoices.propose(rid, {"supplier": "Sysco", "invoice_total": 4.05, "lines": [
        {"description": "SOUR CREAM 1QT", "quantity": 1, "unit": "qt", "unit_price": 4.05, "line_total": 4.05}]},
        db_path=db)
    line = prop["lines"][0]
    assert not line.get("verified") and not line.get("selected"), \
        {k: line.get(k) for k in ("ingredient_name", "selected", "verified", "matched_by")}


@pytest.mark.xfail(strict=True, reason="ING-10 / SCALE-03: read_file marks the file stored before its rows are written")
def test_ing_10_a_failed_write_is_read_again_on_retry(db, monkeypatch):
    rid = _rid(db)
    txt = _printout([f"{B}SYSCO{NB} $100.00", f"{NB} 5555 {C} 09/05/2026 {NB}$100.00 {NB} 0{NB}"]).encode()
    real = bo.save_invoices

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(bo, "save_invoices", boom)
    try:
        bo.read_file(rid, "ibv.txt", txt, db_path=db)
    except RuntimeError:
        pass                      # the write failed (a locked database, a deploy)
    monkeypatch.setattr(bo, "save_invoices", real)
    again = bo.read_file(rid, "ibv.txt", txt, db_path=db)
    c = _conn(db)
    n = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE restaurant_id=?", (rid,)).fetchone()[0]
    c.close()
    assert again["status"] == "stored" and n == 1, (again, n)


@pytest.mark.xfail(strict=True, reason="ING-11: a Fintech credit memo carrying its invoice's number is never counted")
def test_ing_11_a_same_number_credit_memo_is_its_own_document(db):
    """Southern 4471 for $600, then a credit memo 4471 for -$300: both on
    file, net $300 - not a 'conflict' version that is never written."""
    rid = _rid(db)
    inv = FT_HEAD + "Southern Glazers Wine and Spirits of IL,4471,10/01/2026,600.00,1,55,JAMESON 1L,CA,2,300.00,600.00\n"
    cm = FT_HEAD + "Southern Glazers Wine and Spirits of IL,4471,10/03/2026,-300.00,1,55,JAMESON 1L,CA,-1,300.00,-300.00\n"
    assert fi.ingest_file(rid, "inv.csv", inv.encode(), db_path=db)["status"] == "stored"
    fi.ingest_file(rid, "cm.csv", cm.encode(), db_path=db)
    c = _conn(db)
    rows = c.execute("SELECT total FROM fintech_invoices WHERE restaurant_id=?", (rid,)).fetchall()
    c.close()
    assert len(rows) == 2 and round(sum(r["total"] for r in rows), 2) == 300.0, [r["total"] for r in rows]


# ── POS-06: one classifier for a ticket line ─────────────────────────────

@pytest.mark.xfail(strict=True, reason="POS-06: promo/hashdisc lines are classified differently by rpower.line_kind and rpower._line_kind")
@pytest.mark.parametrize("flag", ["type_promo", "type_hashdisc"])
def test_pos_06_the_archive_and_the_nightly_report_agree_a_discount_is_a_discount(flag):
    import rpower
    types = {"X": {"mid": "X", flag: 1, "impacts_sales": 1}}
    row = {"slstype_mid": "X", "sales": -5.0, "qty": 1}
    assert rpower.line_kind(row, types) == rpower._line_kind(row, types) == "discount", \
        (rpower.line_kind(row, types), rpower._line_kind(row, types))


# ── AI: the food read's validator ────────────────────────────────────────

def _food_ctx(**kw):
    import response_validation as rv
    facts = [rv.Fact("waste.week", 120, "$", period="week"),
             rv.Fact("waste.monthly", 520, "$", kind="projection", period="month"),
             rv.Fact("item.waste", 80, "$", entity="Brisket"),
             rv.Fact("waste.rate", 9, "%")]
    return rv.ValidationContext(restaurant_id=1, surface="food_insight", facts=facts, **kw)


@pytest.mark.xfail(strict=True, reason="AI-01: a % is 'backed' by any two unrelated facts whose ratio or change happens to match")
def test_ai_01_an_invented_percentage_is_not_backed_by_unrelated_facts():
    """'Waste rose 85% this week.' - 85% is only ($80 brisket waste vs the
    $520 monthly projection), an unrelated pair: F1, as 37% already is."""
    import response_validation as rv
    ctx = _food_ctx()
    assert "F1" in rv.validate("Waste rose 37% this week.", ctx).codes, "the control already fires"
    assert "F1" in rv.validate("Waste rose 85% this week.", ctx).codes


@pytest.mark.xfail(strict=True, reason="AI-02: a 'Why:' cause sentence passes with no cause anchors")
def test_ai_02_a_why_line_needs_a_cause_anchor():
    """With no cause anchors, 'Why: line cooks are over-portioning the
    brisket.' is checked as a cause (K1, or dropped), as 'because' is."""
    import response_validation as rv
    ctx = _food_ctx()
    assert "K1" in rv.validate("Brisket waste was $80 because line cooks are over-portioning the brisket.",
                               ctx).codes, "the control already fires"
    v = rv.validate("Brisket waste was $80. Why: line cooks are over-portioning the brisket.", ctx)
    assert "K1" in v.codes or "over-portioning" not in v.text, (v.codes, v.text)


# ── SCALE-01: waste periods cost the recipes once ────────────────────────

def test_scale_01_waste_periods_cost_the_recipes_once_for_every_period(db, monkeypatch):
    rid = _kitchen(db)
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=d), f)
                                               for d, f in ((22, 1000.0), (15, 900.0), (8, 800.0), (1, 700.0))]},
                   db_path=db)
    calls = {"book": 0, "prices": 0}
    real_book, real_prices = br.book, br._invoice_prices

    def book(*a, **k):
        calls["book"] += 1
        return real_book(*a, **k)

    def prices(*a, **k):
        calls["prices"] += 1
        return real_prices(*a, **k)
    monkeypatch.setattr(br, "book", book)
    monkeypatch.setattr(br, "_invoice_prices", prices)
    periods = bc.waste_periods(rid, today=TODAY, db_path=db)
    assert len(periods) == 3 and sum(1 for p in periods if p["recipe_pct"] is not None) == 3, \
        [p.get("why_not") for p in periods]
    assert calls == {"book": 1, "prices": 1}, calls
