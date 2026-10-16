"""Back Office's two files (backoffice_ap, 10/15/26): the Chart of Accounts
CSV and the Invoices By Vendor printout. Food purchases for Food Cost;
alcohol bought outside Fintech for the Bar, never a Fintech invoice twice.

Shapes are EJ's real ones (Simple EJ's, Back Office unit 7101, read
10/15/26): the CSV's own header, the printout's bullet and checkmark
characters, a vendor header split onto its own line, check-mode entries a
Fintech invoice reappears as weeks later when Back Office pays it.
"""
from datetime import date, timedelta

import pytest

import backoffice_ap as bo
import bar_cost
from tests.test_bar_routes import MOBILE, _app, _as, _conn, _events, _invoice, _rid, _sales, db  # noqa: F401

TODAY = date.today()

COA = (
    "GLAccountSubTypeName,FinancialStatementTypeName,CorporateID,MainAccountNumber,GLAccountDescription,"
    "UseForWebIndicator,UnitID\n"
    "Purchase,Income and Expense Statement,7101,4010,4010 PRODUCE,True,7101\n"
    "Purchase Inventory,Income and Expense Statement,7101,4011,4011 PRODUCE INVENTORY,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4030,4030 MEAT,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4080,4080 NA BEVERAGES,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4098,4098 BAR PURCHASE DISCOUNT,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4320,4320 BEER,True,7101\n"
    "Purchase Inventory,Income and Expense Statement,7101,4321,4321 BEER INVENTORY,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4330,4330 WINE,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4370,4370 LIQUOR,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4380,4380 BAR CONSUMABLES,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4934,4934 PAPER PRODUCTS,True,7101\n"
    "Other Expense,Income and Expense Statement,7101,4888,4888 RESTAURANT SUPPLIES,True,7101\n"
    "Other Expense,Income and Expense Statement,7101,5920,5920 RENT,True,7101\n"
    "Property & Equipment,Balance Sheet,7101,1420,1420 ASSET FURNITURE & FIXTURE,True,7101\n"
)

B, C, NB = "", "", "\xa0"


def _d(days_ago):
    d = TODAY - timedelta(days=days_ago)
    return d.strftime("%m/%d/%Y")


def _printout():
    """Invoices By Vendor as the PDF's text reads: EJ's shapes."""
    rows = [
        "10/15/26, 4:14 PMBack Office - Invoices By Vendor",
        "Page 1 of 2https://accounting4.restacct.com/AccountsPayable/InvoicesByVendor",
        f"{NB} Invoice # ItemizedDate Amount Sent to RASI Terms Check # Check Date",
        f"{B}GORDON FOOD SERVICE{NB} $1,400.00",
        f"{NB} 9041200735 {C} {_d(5)} {NB}$900.00 {NB} 0{NB} Debit/EFT{NB} {NB}",
        f"{NB} 9041100111 {C} {_d(12)} {NB}$500.00 {NB} 0{NB} Debit/EFT{NB} {NB}",
        f"{B}WINEBOW{NB} $1,080.00",
        f"{NB} CM Entry {NB} {_d(3)} {NB}$360.00 {NB} GL: 4330 {NB} , {NB}",
        f"{NB} CM Entry {NB} {_d(3)} {NB}$360.00 {NB} GL: 4330 {NB} , {NB}",
        f"{NB} CM Entry {NB} {_d(40)} {NB}$360.00 {NB} GL: 4330 {NB} HEFLIN, JIM{NB}",
        # A Fintech invoice Back Office records again when it pays it.
        f"{B}",
        f"BREAKTHRU BEVERAGE IL{NB} $1,330.00",
        f"{NB} CM Entry {NB} {_d(8)} {NB}$700.00 {NB} GL: 4370 {NB} HEFLIN, JIM{NB}",
        f"{NB} 0128850598 {C} {_d(20)} {NB}$630.00 {NB} 0{NB} Debit/EFT{NB} {NB}",
        f"{B}BINNY'S BEVERAGE DEPOT{NB} $120.00",
        f"{NB} CM Entry {NB} {_d(6)} {NB}$100.00 {NB} GL: 4320 {NB} HEFLIN, JIM{NB}",
        f"{NB} CM Entry {NB} {_d(6)} {NB}$20.00 {NB} GL: 4380 {NB} HEFLIN, JIM{NB}",
        "10/15/26, 4:14 PMBack Office - Invoices By Vendor",
        "Page 2 of 2https://accounting4.restacct.com/AccountsPayable/InvoicesByVendor",
        f"{B}ALSCO UNIFORMS{NB} $1,341.76",
        f"{NB} CM Entry {NB} {_d(9)} {NB}$1,341.76 {NB} GL: 4888 {NB} HEFLIN, JIM{NB}",
        f"{B}DHL DELIVERY{NB} -$677.37",
        f"{NB} CM Entry {NB} {_d(30)} {NB}-$677.37 {NB} GL: 5270 {NB} , {NB}",
    ]
    return "\n".join(rows)


def test_the_chart_of_accounts_sorts_each_account_by_its_type_and_name():
    accts = {a["gl"]: a for a in bo.parse_coa(COA.encode())}
    want = {"4010": "food", "4011": "inventory_adjustment", "4030": "food", "4080": "na_beverage",
            "4098": "discount", "4320": "beer", "4321": "inventory_adjustment", "4330": "wine", "4370": "liquor",
            "4380": "bar_consumables", "4934": "other_purchase", "4888": "not_cogs", "5920": "not_cogs",
            "1420": "not_cogs"}
    assert {g: accts[g]["category"] for g in want} == want
    assert accts["4320"]["name"] == "BEER"
    with pytest.raises(ValueError):
        bo.parse_coa(b"a,b,c\n1,2,3\n")


def test_the_printout_reads_every_vendor_including_one_split_over_two_lines():
    p = bo.parse_invoices_by_vendor(_printout())
    names = [v["vendor"] for v in p["vendors"]]
    assert names == ["GORDON FOOD SERVICE", "WINEBOW", "BREAKTHRU BEVERAGE IL", "BINNY'S BEVERAGE DEPOT",
                     "ALSCO UNIFORMS", "DHL DELIVERY"]
    assert p["mismatched"] == [], "every vendor's rows add up to the total the page printed"
    gfs = p["vendors"][0]["rows"]
    assert gfs[0]["itemized"] and gfs[0]["invoice_number"] == "9041200735" and gfs[0]["gl"] is None
    wb = p["vendors"][1]["rows"]
    assert all(r["entry_kind"] == "cm" and r["gl"] == "4330" and r["amount"] == 360.0 for r in wb)
    assert p["vendors"][-1]["rows"][0]["amount"] == -677.37


def test_a_file_is_read_once_and_an_overlapping_printout_never_doubles_an_entry(db):
    rid = _rid(db)
    first = bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    assert first["status"] == "stored" and first["kind"] == "coa"
    assert bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)["status"] == "duplicate"
    got = bo.read_file(rid, "ibv.pdf.txt", _printout().encode(), db_path=db)
    assert got["status"] == "stored" and got["kind"] == "invoices_by_vendor" and got["vendors"] == 6
    # The same entries in a second, longer printout: kept once (two alike
    # Winebow entries on one day stay two).
    again = bo.read_file(rid, "ibv2.pdf.txt", (_printout() + "\n ").encode(), db_path=db)
    assert again["status"] == "stored"
    c = _conn(db)
    n = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE restaurant_id=?", (rid,)).fetchone()[0]
    wb = c.execute("SELECT COUNT(*) FROM bo_invoices WHERE restaurant_id=? AND vendor='WINEBOW'", (rid,)).fetchone()[0]
    c.close()
    assert n == 11 and wb == 3
    assert bo.read_file(rid, "x.txt", b"hello", db_path=db)["status"] == "unrecognized"


def _fintech(db, rid):
    """Breakthru on Fintech: the $630 invoice Back Office itemizes by number,
    and a $700 one Back Office records as a check-mode entry 8 days ago
    (Fintech's is dated 30 days ago: paid weeks after delivery)."""
    _invoice(db, rid, "Breakthru Beverage Illinois, LLC", "0128850598", (TODAY - timedelta(days=20)).isoformat(),
             [("9031", "TITOS VDK 1L", "CA", 3, 210.0)])
    _invoice(db, rid, "Breakthru Beverage Illinois, LLC", "128700001", (TODAY - timedelta(days=30)).isoformat(),
             [("9032", "KETEL ONE 1L", "CA", 2, 350.0)])
    _invoice(db, rid, "Winebow Inc (CT, IL, NJ, NY)", "IL1138699", (TODAY - timedelta(days=60)).isoformat(),
             [("W1", "WHITEHAVEN SAUV BL 750ML", "CA", 1, 180.0)])


def test_alcohol_outside_fintech_reaches_the_bar_and_a_fintech_invoice_never_twice(db):
    rid = _rid(db)
    _fintech(db, rid)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "ibv.txt", _printout().encode(), db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    assert out["matched"] == 2, "the itemized Breakthru invoice by number, the check-mode one by amount"
    got = sorted((e["vendor"], e["category"], e["amount"]) for e in out["entries"])
    assert got == [("BINNY'S BEVERAGE DEPOT", "bar_consumables", 20.0), ("BINNY'S BEVERAGE DEPOT", "beer", 100.0),
                   ("WINEBOW", "wine", 360.0), ("WINEBOW", "wine", 360.0), ("WINEBOW", "wine", 360.0)]
    lines = {(l["distributor"], l["category"], l["net"]) for l in out["lines"]}
    assert ("BINNY'S BEVERAGE DEPOT", "na", 20.0) in lines, "bar consumables are mixers: not alcohol unless counted"
    assert all(l["kind"] == "backoffice" for l in out["lines"])


def test_the_bar_counts_them_in_bought_vs_sold_but_not_in_prices_products_or_gaps(db):
    rid = _rid(db)
    _fintech(db, rid)
    for d in range(1, 45):
        _sales(db, rid, (TODAY - timedelta(days=d)).isoformat(), beer=400, liquor=900, wine=300)
    before = bar_cost.build(rid, db_path=db, today=TODAY)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "ibv.txt", _printout().encode(), db_path=db)
    after = bar_cost.build(rid, db_path=db, today=TODAY)
    cat = lambda p, k: next(c for c in p["pour_cost"]["categories"] if c["key"] == k)
    win = after["pour_cost"]["window"]
    in_win = sum(360.0 for days in (3, 3, 40) if win["from"] <= (TODAY - timedelta(days=days)).isoformat() <= win["to"])
    assert round(cat(after, "wine")["purchases"] - (cat(before, "wine")["purchases"] or 0), 2) == in_win
    assert after["outside_fintech"]["available"] and after["outside_fintech"]["matched_fintech"] == 2
    assert {v["vendor"] for v in after["outside_fintech"]["vendors"]} >= {"WINEBOW"}
    keys = {p["product_key"] for p in after.get("products") or []}
    assert not any(k.startswith("backoffice|") for k in keys), "a Back Office entry is no product"
    assert after["invoices"]["count"] == before["invoices"]["count"], "the Fintech feed is what 'invoices' counts"
    assert not any(s.get("distributor") in ("WINEBOW", "BINNY'S BEVERAGE DEPOT")
                   for s in after["pour_cost"].get("stopped") or []), "no delivery rhythm read from them"


def test_food_bought_vs_sold_is_purchases_over_food_sales_and_never_called_food_cost(db):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "ibv.txt", _printout().encode(), db_path=db)
    c = _conn(db)
    for d in range(1, 30):
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                  "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, (TODAY - timedelta(days=d)).isoformat(), "sales.cat:Food",
                                                          200.0))
    c.commit()
    c.close()
    f = bo.food_bought_vs_sold(rid, today=TODAY, db_path=db)
    assert f["available"] and f["basis"] == "purchases" and f["label"] == "Food bought vs sold"
    w = f["window"]
    assert w["purchases"] == 1400.0, "GFS is food by its name; its itemized invoices carry no GL on the printout"
    assert w["pct"] == round(100 * 1400.0 / w["sales"], 1)
    assert "food cost" not in f["label"].lower() and "count" in f["note"]
    vc = bo.vendor_categories(rid, db_path=db)
    from fintech_invoices import distributor_key
    assert vc[distributor_key("GORDON FOOD SERVICE")]["source"] == "name"
    assert vc[distributor_key("ALSCO UNIFORMS")]["category"] == "not_cogs", "linen books to 4888, never food"
    assert "ALSCO UNIFORMS" not in f["unknown_vendors"]


def test_the_food_purchases_route_and_the_owner_sorting_a_supplier(db, monkeypatch):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "ibv.txt", _printout().encode(), db_path=db)
    cl = _app().test_client()
    _as(monkeypatch, rid, role="client", uid=11)
    r = cl.get("/api/food-cost/purchases")
    assert r.status_code == 200 and r.get_json()["ok"] and r.get_json()["can_change"] is True
    assert any(s["vendor"] == "GORDON FOOD SERVICE" for s in r.get_json()["suppliers"])
    r = cl.post("/api/food-cost/purchases/vendor", json={"vendor": "GORDON FOOD SERVICE", "category": "food"})
    assert r.status_code == 200 and r.get_json()["category"] == "food"
    assert _events(db, rid, "purchase_vendor_sorted") is not None
    assert cl.post("/api/food-cost/purchases/vendor", json={"vendor": "X", "category": "nonsense"}).status_code == 400
    _as(monkeypatch, rid, role="manager", grants=["foodcost.view"], uid=12)
    assert cl.post("/api/food-cost/purchases/vendor",
                   json={"vendor": "GORDON FOOD SERVICE", "category": "not_cogs"}).status_code == 403
    assert cl.post("/mobile/api/food-cost/purchases/vendor", headers=MOBILE,
                   json={"vendor": "GORDON FOOD SERVICE", "category": "not_cogs"}).status_code == 403


def test_the_food_cost_card_says_bought_vs_sold_and_loads_beside_the_cfo_read():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html"),
               encoding="utf-8").read()
    fn = src[src.index("function fc2LoadBo(){"):src.index("function fc2LoadCfo(){")]
    # The basis ("Purchase-based: ...", the server's note) sits behind the card's "i" (owner, 10/16/26: no pill).
    assert "Food bought vs sold" in fn and 'data-itip="fc2-bo-note"' in fn and "Purchase-based · not a count" not in fn
    assert "<th class=\"n\">Bought from suppliers</th><th class=\"n\">Food sales</th>" in fn          # the months have headings
    assert "'alcohol_mixed','Alcohol, mixed" in src                                                    # a store's receipt
    assert "food cost" not in fn.lower().replace("/api/food-cost", ""), "a purchase ratio is never called food cost"
    assert '<div id="fc2-bo" class="fc2-cfo" hidden></div>' in src
    assert src.count("fc2LoadBo();") >= 2, "loads where the CFO read loads"


def test_a_payment_net_of_a_credit_and_a_shortened_name_still_match_fintech(db):
    """EJ's 10/15/26: Winebow paid $1,259.34 for a $1,646.34 invoice and a
    -$387.00 credit; 'SCHAMBERGER BROTHERS INC' is Fintech's 'Schamberger
    Bros., Inc.' - neither is alcohol bought outside Fintech."""
    rid = _rid(db)
    _invoice(db, rid, "Winebow Inc (CT, IL, NJ, NY)", "IL1138699", (TODAY - timedelta(days=50)).isoformat(),
             [("W1", "WHISPERING ANGEL ROSE 750ML", "CA", 1, 1646.34)])
    _invoice(db, rid, "Winebow Inc (CT, IL, NJ, NY)", "IL1138940", (TODAY - timedelta(days=49)).isoformat(),
             [("W1", "WHISPERING ANGEL ROSE 750ML", "CA", -1, 387.0)])
    _invoice(db, rid, "Schamberger Bros., Inc.", "1000217253", (TODAY - timedelta(days=60)).isoformat(),
             [("S1", "REVOLUTION ANTI HERO 1/2BBL", "KE", 1, 201.5)])
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    txt = "\n".join([
        "Back Office - Invoices By Vendor", f"{NB} Invoice # ItemizedDate Amount",
        f"{B}WINEBOW{NB} $1,259.34", f"{NB} CM Entry {NB} {_d(20)} {NB}$1,259.34 {NB} GL: 4330 {NB} , {NB}",
        f"{B}SCHAMBERGER BROTHERS INC{NB} $201.50", f"{NB} CM Entry {NB} {_d(20)} {NB}$201.50 {NB} GL: 4320 {NB} , {NB}"])
    bo.read_file(rid, "ibv.txt", txt.encode(), db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    assert out["entries"] == [] and out["matched"] == 2


def test_a_month_compares_only_the_days_both_sides_cover():
    """EJ's August read 243.5%: a month of food invoices against six nights of
    sales. Months start where sales do and need MONTH_MIN_NIGHTS nights."""
    import inspect
    src = inspect.getsource(bo.food_bought_vs_sold)
    assert "sales_from" in src and "MONTH_MIN_NIGHTS" in src and bo.MONTH_MIN_NIGHTS >= 14


def test_an_invoice_with_its_lines_splits_by_account_and_paper_leaves_food(db):
    """Back Office's invoice page carries each line's GL account (EJ's GFS
    invoice 9041200735, 10/12/26: brisket 4030 MEAT, buttermilk 4020 DAIRY,
    chicken 4060 POULTRY). With the lines on file the invoice splits by
    account; a paper line books to 4934 and leaves food."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "ibv.txt", _printout().encode(), db_path=db)
    c = _conn(db)
    for d in range(1, 30):
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                  "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, (TODAY - timedelta(days=d)).isoformat(), "sales.cat:Food", 200.0))
    c.commit()
    c.close()
    bo.save_lines(rid, "GORDON FOOD SERVICE", "9041200735", [
        {"item_code": "197761", "description": "BEEF BRISKET", "gl": "4030", "purchase_unit": "Case", "qty": 50,
         "unit_amount": 10.0, "ext_amount": 500.0},
        {"item_code": "245570", "description": "DAIRY BUTTERMILK", "gl": "4010", "purchase_unit": "Case", "qty": 1,
         "unit_amount": 300.0, "ext_amount": 300.0},
        {"item_code": "900001", "description": "NAPKIN DINNER 2PLY", "gl": "4934", "purchase_unit": "Case", "qty": 1,
         "unit_amount": 80.0, "ext_amount": 80.0}], db_path=db)
    rows = [e for e in bo.entries(rid, db_path=db) if e["invoice_number"] == "9041200735"]
    got = sorted((e["gl"], e["category"], e["amount"], e["category_source"]) for e in rows if e["gl"])
    assert got == [("4010", "food", 300.0, "line"), ("4030", "food", 500.0, "line"), ("4934", "other_purchase", 80.0, "line")]
    rest = [e for e in rows if e["category_source"] == "invoice_rest"]
    assert len(rest) == 1 and rest[0]["amount"] == 20.0 and rest[0]["category"] == "food", "the $20 the lines don't carry"
    f = bo.food_bought_vs_sold(rid, today=TODAY, db_path=db)
    assert f["window"]["purchases"] == 1400.0 - 80.0, "the napkins leave food"
    names = {x["name"] for x in f["by_category"]}
    assert {"MEAT", "PRODUCE"} <= names
    labels = {x["label"] for x in f["by_category"]}
    assert {"Meat", "Produce"} <= labels, "the card's by-account line reads names, not GL numbers"


def _invoice_page(number, day, lines, total, page_break_after=None):
    """One invoice's Edit Invoice page as the PDF's text reads (EJ's GFS
    invoice 9041344199, 10/15/26): the header's label under each value, then
    each line as value/label pairs; the browser's page header can fall
    between two fields of one line."""
    out = ["10/15/26, 4:57 PMBack Office - Invoice",
           "Page 1 of 2https://accounting4.restacct.com/AccountsPayable/EditInvoice?invoi…",
           "Edit InvoiceHHB LLC   Entered by: JH ", "1 ", "2 ",
           "GORDON FOOD SERVICE", "Vendor*", number, "Invoice Number*", day, "Invoice Date*",
           "Debit/EFT", "Payment Method*", total, "Total Price*"]
    for i, (code, desc, gl, unit, qty, amt, ext) in enumerate(lines):
        fields = [f"({code}) {desc}", "Unit Inventory Item*", f"Use Tax Applies {gl}", "GL Account", unit,
                  "Purchase Unit*", qty, "Quantity*", amt, "Amount*", ext, "Ext Amount"]
        if page_break_after == i:
            fields[6:6] = ["10/15/26, 4:57 PMBack Office - Invoice",
                           "Page 2 of 2https://accounting4.restacct.com/AccountsPayable/EditInvoice?invoi…",
                           "11 ", "12 ", "13"]
        out += fields
    out += ["2 ", "     Add Line Item     Total" + total]
    return "\n".join(out)


GFS_LINES = [
    ("197761", "BEEF BRISKET", "4030 MEAT", "Case", "128.60", "5.94", "763.88"),
    ("516561", "POUL TRY CHICKEN BREAST B/S", "4060 POUL TRY", "Case", "4.00", "79.93", "319.72"),
    ("162080", "FOAM CUP 24 OZ", "4888 RESTAURANT SUPPLIES", "Case", "1.00", "49.37", "49.37"),
    ("Tax", "TAX", "4999 RETAIL TAX/FEES PAID", "Each", "1.00", "16.46", "16.46"),
]


def test_an_invoice_page_reads_every_line_even_across_a_page_break():
    t = _invoice_page("9041344199", "10/15/2026", GFS_LINES, "$1,149.43", page_break_after=1)
    inv = bo.parse_invoice_page(t)
    assert (inv["vendor"], inv["invoice_number"], inv["date"], inv["total"]) == \
        ("GORDON FOOD SERVICE", "9041344199", "2026-10-15", 1149.43)
    assert [(l["item_code"], l["gl"], l["ext_amount"]) for l in inv["lines"]] == [
        ("197761", "4030", 763.88), ("516561", "4060", 319.72), ("162080", "4888", 49.37), ("Tax", "4999", 16.46)]
    assert inv["lines"][0]["qty"] == 128.6 and inv["lines"][0]["unit_amount"] == 5.94
    assert inv["lines"][1]["description"] == "POULTRY CHICKEN BREAST B/S", "the PDF's split word is mended"
    assert inv["adds_up"]
    short = bo.parse_invoice_page(_invoice_page("9041344199", "10/15/2026", GFS_LINES[:3], "$1,149.43"))
    assert not short["adds_up"], "a line the reader missed is named, not hidden"


def test_an_invoice_page_stores_its_lines_and_the_invoice_counts_once(db):
    """A page read before an Invoices By Vendor printout lists the invoice
    stores it under the printout's own key: the printout later updates it in
    place, never doubles it."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    page = _invoice_page("9041200735", _d(5), GFS_LINES, "$1,149.43")
    r = bo.read_file(rid, "gfs.txt", page.encode(), db_path=db)
    assert r["status"] == "stored" and r["kind"] == "invoice_page" and r["lines"] == 4 and r["adds_up"]
    assert bo.read_file(rid, "gfs.txt", page.encode(), db_path=db)["status"] == "duplicate"
    rows = [e for e in bo.entries(rid, db_path=db) if e["invoice_number"] == "9041200735"]
    assert sorted((e["gl"], e["category_source"]) for e in rows) == [
        ("4030", "line"), ("4060", "line"), ("4888", "line"), ("4999", "line")]
    assert round(sum(e["amount"] for e in rows), 2) == 1149.43
    bo.read_file(rid, "ibv.txt", _printout().encode(), db_path=db)
    rows = [e for e in bo.entries(rid, db_path=db) if e["invoice_number"] == "9041200735"]
    assert round(sum(e["amount"] for e in rows), 2) == 900.0, "the printout's amount, once"
