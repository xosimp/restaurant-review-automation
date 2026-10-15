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
