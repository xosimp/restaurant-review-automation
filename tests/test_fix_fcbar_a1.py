"""Fix round for the Food Cost + Bar audit of 10/16/26, workstream A1: Back
Office intake, reconciliation and food purchases / sales (backoffice_ap).

Each test names the finding it holds (registers in docs/audits and
/Users/simp/fc_bar_audit_1016). The audit's own strict xfails for
FC-CALC-03/04/05, ING-01/02/03/04/10 live in tests/test_audit_fc_bar_1016.py.
"""
import json
from datetime import date, datetime, timedelta

import backoffice_ap as bo
import fintech_invoices as fi
from tests.test_backoffice_ap import COA, _app, _as, _conn, _events, _rid, db  # noqa: F401

B, C, NB = bo._BULLET, bo._CHECK, "\xa0"
HEAD = ["10/15/26, 4:14 PMBack Office - Invoices By Vendor",
        f"{NB} Invoice # ItemizedDate Amount Sent to RASI Terms Check # Check Date"]
T = date(2026, 10, 16)


def _printout(lines):
    return "\n".join(HEAD + lines)


def _inv(number, day, amount, gl=None, itemized=True):
    tail = f" {NB} GL: {gl} {NB} , {NB}" if gl else f" {NB} 0{NB} Debit/EFT"
    return f"{NB} {number} {C if itemized else NB} {day} {NB}{amount}{tail}"


def _cm(day, amount, gl):
    return f"{NB} CM Entry {NB} {day} {NB}{amount} {NB} GL: {gl} {NB} , {NB}"


def _stored(db, rid, vendor=None):
    c = _conn(db)
    try:
        q, a = "SELECT vendor, entry_key, invoice_number, invoice_date, amount FROM bo_invoices WHERE restaurant_id=?", [rid]
        if vendor:
            q += " AND vendor=?"
            a.append(vendor)
        return [dict(r) for r in c.execute(q + " ORDER BY invoice_date, id", a)]
    finally:
        c.close()


def _fin(db, rid, dist, number, day, total):
    c = _conn(db)
    try:
        inv = c.execute(
            "INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
            "source_format, updated_at, balanced, distributor_key, number_key) "
            "VALUES (?,?,?,?,?, 'export', '2026-10-01 00:00:00', 1, ?, ?)",
            (rid, dist, number, day, total, fi.distributor_key(dist), fi.invoice_key(number))).lastrowid
        c.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, uom, qty, "
                  "unit_price, cost, kind, restaurant_id) VALUES (?,1,'9031','TITOS VDK 1L','CA',1,?,?, 'item', ?)",
                  (inv, total, total, rid))
        c.commit()
    finally:
        c.close()


def _metric(db, rid, day, metric, value):
    c = _conn(db)
    c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
              "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, metric, value))
    c.commit()
    c.close()


# ── FC-CALC-04 / ING-12: entry keys and the printout as the record ────────

def test_a_credit_memo_a_later_year_and_a_corrected_cm_each_land_right(db):
    rid = _rid(db)
    bo.read_file(rid, "a.txt", _printout([
        f"{B}GORDON FOOD SERVICE{NB} $700.00",
        _inv("9041200735", "10/01/2026", "$900.00"),
        _inv("9041200735", "10/03/2026", "-$200.00"),
        f"{B}FARM CO{NB} $310.00",
        _inv("1001", "09/05/2026", "$210.00"),
        _cm("09/20/2026", "$100.00", "4010")]).encode(), db_path=db)
    gfs = _stored(db, rid, "GORDON FOOD SERVICE")
    assert [r["amount"] for r in gfs] == [900.0, -200.0], "a credit memo under its invoice's number is its own row"
    assert gfs[0]["entry_key"] == "inv|9041200735|2026" and gfs[1]["entry_key"] == "inv|9041200735|2026|cr"
    # Last year's printout: Farm Co's 1001 of 9/5/25 is another invoice.
    bo.read_file(rid, "b.txt", _printout([f"{B}FARM CO{NB} $55.00", _inv("1001", "09/05/2025", "$55.00")]).encode(),
                 db_path=db)
    assert sorted(r["amount"] for r in _stored(db, rid, "FARM CO")) == [55.0, 100.0, 210.0]
    # A corrected printout from 9/1: the CM entry is $110 now and invoice
    # 1001 is gone.
    got = bo.read_file(rid, "c.txt", _printout([
        f"{B}SYSCO{NB} $10.00",
        _inv("31", "09/01/2026", "$10.00"),
        f"{B}FARM CO{NB} $110.00",
        _cm("09/20/2026", "$110.00", "4010"),
        f"{B}GORDON FOOD SERVICE{NB} $700.00",
        _inv("9041200735", "10/01/2026", "$900.00"),
        _inv("9041200735", "10/03/2026", "-$200.00")]).encode(), db_path=db)
    assert got["status"] == "stored" and got["removed"] == 2 and got["unbalanced"] == []
    farm = sorted(r["amount"] for r in _stored(db, rid, "FARM CO"))
    assert farm == [55.0, 110.0], "the corrected CM replaces, never doubles; 2025's 1001 is outside the printout's dates"
    c = _conn(db)
    gone = [dict(r) for r in c.execute("SELECT vendor, amount, removed_by_file, why FROM bo_invoice_removals "
                                       "WHERE restaurant_id=? ORDER BY amount", (rid,))]
    c.close()
    assert [(g["vendor"], g["amount"]) for g in gone] == [("FARM CO", 100.0), ("FARM CO", 210.0)]
    assert all(g["removed_by_file"] == got["file_id"] and g["why"] for g in gone), "a removal is on record"


def test_old_number_only_keys_are_rekeyed_at_boot(db):
    rid = _rid(db)
    c = _conn(db)
    c.execute("INSERT INTO bo_invoices (restaurant_id, vendor, vendor_key, entry_key, invoice_number, entry_kind, "
              "itemized, invoice_date, amount, gl, created_at, updated_at) VALUES (?,?,?,?,?, 'invoice', 1, ?,?, NULL, "
              "'x', 'x')", (rid, "SYSCO", "sysco", "inv|5555", "5555", "2026-09-05", -40.0))
    c.commit()
    c.close()
    bo.init_backoffice_ap(db)
    assert [r["entry_key"] for r in _stored(db, rid)] == ["inv|5555|2026|cr"]
    bo.init_backoffice_ap(db)                         # idempotent
    assert [r["entry_key"] for r in _stored(db, rid)] == ["inv|5555|2026|cr"]


# ── ING-13: the printout reader ───────────────────────────────────────────

def test_a_parenthesised_total_is_a_header_and_a_row_never_one():
    p = bo.parse_invoices_by_vendor(_printout([
        f"{B}SYSCO{NB} $100.00", _inv("5555", "09/05/2026", "$100.00"),
        f"{B}WINEBOW{NB} ($50.00)", _cm("09/06/2026", "($50.00)", "4330"),
        f"{B}", _inv("9001", "09/06/2026", "$75.00")]))
    got = [(v["vendor"], v["total"], [(r["amount"], r["gl"]) for r in v["rows"]]) for v in p["vendors"]]
    assert got == [("SYSCO", 100.0, [(100.0, None)]), ("WINEBOW", -50.0, [(-50.0, "4330")])]
    assert p["orphans"] == 1, "a row under a vendor name the reader can't read is never filed under the one before"


def test_a_vendor_whose_rows_dont_add_up_is_held_not_saved(db):
    rid = _rid(db)
    got = bo.read_file(rid, "x.txt", _printout([
        f"{B}SYSCO{NB} $100.00", _inv("5555", "09/05/2026", "$100.00"),
        f"{B}US FOODS{NB} $900.00", _inv("7777", "09/06/2026", "$450.00")]).encode(), db_path=db)
    assert got["status"] == "conflict" and got["held"] == ["US FOODS"] and "US FOODS" in got["detail"]
    assert [r["vendor"] for r in _stored(db, rid)] == ["SYSCO"]


# ── FC-CALC-25: one invoice under two spellings of its vendor ─────────────

def _book(vendor, number, day, total):
    return {"invoices": [{"vendor": vendor, "number": number, "date": day, "total": f"${total:,.2f}", "items": [
        {"sku": "1", "name": "LETTUCE", "gl": "4010 PRODUCE", "unit": "Case", "qty": "1", "amt": str(total),
         "ext": f"{total:.2f}"}]}]}


def test_a_page_after_the_printout_under_another_spelling_counts_once(db):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "p.txt", _printout([f"{B}GFS{NB} $320.00", _inv("9041", "09/05/2026", "$320.00")]).encode(),
                 db_path=db)
    bo.save_invoice_book(rid, _book("GORDON FOOD SERVICE", "9041", "2026-09-05", 320.0), db_path=db)
    rows = _stored(db, rid)
    assert [(r["vendor"], r["amount"]) for r in rows] == [("GFS", 320.0)]
    es = [e for e in bo.entries(rid, db_path=db) if e["invoice_number"] == "9041"]
    assert [(e["gl"], e["category_source"], e["amount"]) for e in es] == [("4010", "line", 320.0)]


def test_a_printout_after_the_page_under_another_spelling_counts_once(db):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.save_invoice_book(rid, _book("GORDON FOOD SERVICE", "9041", "2026-09-05", 320.0), db_path=db)
    bo.read_file(rid, "p.txt", _printout([f"{B}GFS{NB} $320.00", _inv("9041", "09/05/2026", "$320.00")]).encode(),
                 db_path=db)
    assert [(r["vendor"], r["amount"]) for r in _stored(db, rid)] == [("GFS", 320.0)]
    es = [e for e in bo.entries(rid, db_path=db) if e["invoice_number"] == "9041"]
    assert [(e["gl"], e["amount"]) for e in es] == [("4010", 320.0)], "its lines moved with it"


# ── BAR-CALC-08 / LEAD-01: never silently added, never wrongly dropped ────

POUR_COA = (
    "GLAccountSubTypeName,FinancialStatementTypeName,CorporateID,MainAccountNumber,GLAccountDescription,"
    "UseForWebIndicator,UnitID\n"
    "Purchase,Income and Expense Statement,7101,4010,4010 PRODUCE,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4099,4099 FOOD PURCHASE DISCOUNT,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4320,4320 BEER,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4330,4330 WINE,True,7101\n"
    "Purchase,Income and Expense Statement,7101,4370,4370 LIQUOR,True,7101\n"
)


def test_a_store_run_sharing_a_number_with_another_distributor_is_kept(db):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", POUR_COA.encode(), db_path=db)
    _fin(db, rid, "Southern Glazers Wine and Spirits of IL", "0010452", "2026-09-10", 640.0)
    bo.read_file(rid, "p.txt", _printout([f"{B}BINNY'S BEVERAGE DEPOT{NB} $640.00",
                                          _inv("10452", "09/10/2026", "$640.00", gl="4370", itemized=False)]).encode(),
                 db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    assert [(e["vendor"], e["amount"]) for e in out["entries"]] == [("BINNY'S BEVERAGE DEPOT", 640.0)]
    assert out["matched"] == 0 and out["to_confirm"] == []


def test_a_fintech_house_entry_matching_no_invoice_is_held_until_the_owner_says(db, monkeypatch):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", POUR_COA.encode(), db_path=db)
    _fin(db, rid, "Lakeshore Beverage LLC", "L77", "2026-09-01", 1185.33)
    bo.read_file(rid, "p.txt", _printout([f"{B}LAKESHORE BEVERAGE{NB} $1,215.33",
                                          _cm("09/20/2026", "$1,215.33", "4320")]).encode(), db_path=db)
    out = bo.outside_fintech(rid, db_path=db)
    assert out["lines"] == [] and out["entries"] == [], "a keg deposit off Fintech's total is never added on top"
    [held] = out["to_confirm"]
    assert (held["vendor"], held["amount"], held["category"]) == ("LAKESHORE BEVERAGE", 1215.33, "beer")
    assert held["fintech_distributor"] == "Lakeshore Beverage LLC" and held["date_mdy"] == "9/20/26"
    s = bo.outside_fintech_summary(rid, db_path=db)
    assert s["total"] == 0.0 and s["to_confirm_total"] == 1215.33 and s["available"]
    # The owner counts it (web route; logged), then sets it aside.
    cl = _app().test_client()
    _as(monkeypatch, rid, role="client", uid=11)
    body = {"vendor_key": held["vendor_key"], "entry_key": held["entry_key"], "decision": "count"}
    r = cl.post("/api/food-cost/purchases/entry", json=body)
    assert r.status_code == 200 and r.get_json()["decision"] == "count"
    assert [l["net"] for l in bo.outside_fintech(rid, db_path=db)["lines"]] == [1215.33]
    assert _events(db, rid, "purchase_vendor_sorted")
    r = cl.post("/api/food-cost/purchases/entry", json=dict(body, decision="not_purchase"))
    after = bo.outside_fintech(rid, db_path=db)
    assert r.status_code == 200 and after["lines"] == [] and after["to_confirm"] == []
    assert [e["amount"] for e in after["set_aside"]] == [1215.33]
    assert cl.post("/api/food-cost/purchases/entry", json=dict(body, entry_key="cm|nope")).status_code == 400
    assert cl.post("/api/food-cost/purchases/entry", json=dict(body, decision="maybe")).status_code == 400
    _as(monkeypatch, rid, role="manager", grants=["foodcost.view"], uid=12)
    assert cl.post("/api/food-cost/purchases/entry", json=body).status_code == 403


def test_a_shortened_legal_name_is_still_the_distributor_but_one_word_never_is():
    link = bo._fintech_link
    assert link("schamberger brothers", ["schamberger bros"]) == ("schamberger bros", "exact")
    assert link("winebow", ["winebow ct il nj ny"]) == ("winebow ct il nj ny", "exact")
    assert link("chicago beverage", ["chicago beverage systems"]) == ("chicago beverage systems", "prefix")
    assert link("chicago produce", ["chicago beverage systems"]) == (None, None)
    assert link("lakeshore", ["lakeshore beverage"]) == (None, None), "one word is never enough"


def test_a_vendors_own_accounts_outrank_a_fuzzy_fintech_name(db):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", POUR_COA.encode(), db_path=db)
    _fin(db, rid, "Chicago Beverage Systems", "77", "2026-09-01", 300.0)
    bo.read_file(rid, "p.txt", _printout([f"{B}CHICAGO BEVERAGE{NB} $80.00",
                                          _cm("09/05/2026", "$80.00", "4010")]).encode(), db_path=db)
    v = bo.vendor_categories(rid, db_path=db)["chicago beverage"]
    assert (v["category"], v["source"]) == ("food", "gl")


# ── FC-CALC-17: food discounts and the fees off an invoice's lines ────────

def test_a_food_discount_nets_against_food_and_fees_are_their_own_part(db):
    rid = _rid(db)
    assert bo.gl_category("Purchase", "FOOD PURCHASE DISCOUNT") == "food"
    assert bo.gl_category("Purchase", "BAR PURCHASE DISCOUNT") == "discount"
    bo.read_file(rid, "coa.csv", POUR_COA.encode(), db_path=db)
    for i in range(1, 29):
        _metric(db, rid, (T - timedelta(days=i)).isoformat(), "sales.cat:Food", 500.0)
    bo.read_file(rid, "p.txt", _printout([
        f"{B}GREENLEAF PRODUCE{NB} $1,035.00", _inv("A1", (T - timedelta(days=5)).strftime("%m/%d/%Y"), "$1,035.00"),
        f"{B}GREENLEAF REBATES{NB} -$100.00",
        _cm((T - timedelta(days=4)).strftime("%m/%d/%Y"), "-$100.00", "4099")]).encode(), db_path=db)
    bo.save_lines(rid, "GREENLEAF PRODUCE", "A1", [{"item_code": "1", "description": "LETTUCE", "gl": "4010",
                                                    "qty": 1, "unit_amount": 1000.0, "ext_amount": 1000.0}],
                  invoice_date=(T - timedelta(days=5)).isoformat(), db_path=db)
    f = bo.food_bought_vs_sold(rid, today=T, db_path=db)
    assert f["window"]["purchases"] == 935.0, "the $100 food rebate nets against food purchases"
    assert f["window"]["fees_tax"] == 35.0
    by = {x["name"]: x for x in f["by_category"]}
    assert by[bo.FEES_NAME]["amount"] == 35.0 and "fees" in by[bo.FEES_NAME]["label"].lower()


# ── FC-CALC-20 / POS-05: one definition of food sales ─────────────────────

def test_food_sales_read_the_owners_food_categories_and_net_check_discounts(db):
    from dsr import store as dstore
    rid = _rid(db)
    d1, d2, d3 = "2026-10-01", "2026-10-02", "2026-10-03"
    # An owner category beside Food (FC-CALC-20).
    _metric(db, rid, d1, "sales.cat:Food", 800.0)
    _metric(db, rid, d1, "sales.cat:Desserts", 200.0)
    _metric(db, rid, d1, "sales.cat:Beer", 300.0)
    # Departments, no archive: the Discounts department spread by sales (estimate).
    for dep, cat in (("Kitchen", "Food"), ("Draft", "Beer")):
        dstore.set_category(rid, dep, cat, db_path=db)
    for dep, v in (("Kitchen", 600.0), ("Draft", 400.0), ("Discounts", -100.0)):
        _metric(db, rid, d2, f"sales.dep:{dep}", v)
        _metric(db, rid, d2, f"sales.depcat:{dep} › All", v)
    # Unmapped sales over 2%: the night is not fully covered.
    _metric(db, rid, d3, "sales.cat:Food", 500.0)
    _metric(db, rid, d3, "sales.cat:Unmapped", 400.0)
    # RPOWER's own discount categories (EJ's): food's is food's whole, the
    # bar's none of it, "Discount All" spread by share.
    d4 = "2026-10-05"
    _metric(db, rid, d4, "sales.dep:Kitchen", 600.0)
    _metric(db, rid, d4, "sales.depcat:Kitchen › All", 600.0)
    _metric(db, rid, d4, "sales.dep:Draft", 400.0)
    _metric(db, rid, d4, "sales.depcat:Draft › All", 400.0)
    _metric(db, rid, d4, "sales.dep:Discounts", -180.0)
    for sub, v in (("Discount Food", -50.0), ("Discount Bar", -30.0), ("Discount All", -100.0)):
        _metric(db, rid, d4, f"sales.depcat:Discounts › {sub}", v)
    fs = bo.food_sales_detail(rid, d1, d4, db_path=db)
    assert fs["by_date"][d1] == 1000.0
    assert fs["by_date"][d2] == 540.0 and "estimated" in fs["label"]
    assert fs["by_date"][d4] == 600.0 - 50.0 - 100.0 * 600.0 / 1000.0
    assert fs["estimated"] == [d2, d4]
    assert fs["partial"] == [d3]
    assert bo._food_sales(rid, d1, d4, db_path=db) == fs["by_date"], "one definition for every reader"


def test_archived_nights_take_their_per_ticket_discounts(db):
    from dsr import store as dstore
    rid = _rid(db)
    dstore.set_category(rid, "Kitchen", "Food", db_path=db)
    c = _conn(db)
    for dep, sales, disc in (("Kitchen", 700.0, 70.0), ("Discounts", 0.0, 0.0)):
        c.execute("INSERT INTO bar_sales_nights (restaurant_id, business_date, department, sales_category, sales, "
                  "discounts, comps100, comp_lines, discount_lines, basis, computed_at) "
                  "VALUES (?,?,?,?,?,?,0,0,?, 'pro_rata', datetime('now'))",
                  (rid, "2026-10-04", dep, "", sales, disc, -95.0 if dep == "Discounts" else 0.0))
    c.commit()
    c.close()
    fs = bo.food_sales_detail(rid, "2026-10-04", "2026-10-04", db_path=db)
    assert fs["by_date"] == {"2026-10-04": 630.0} and fs["estimated"] == []


# ── FC-CALC-03: the headline's coverage gate; POS-18: the local date ──────

def _weekly_invoices(rid, db, weeks=4, amount=2800.0):
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": f"N{i}", "date": (T - timedelta(days=1 + 7 * i)).isoformat(),
         "total": f"${amount:,.2f}", "items": [{"sku": "1", "name": "LETTUCE", "gl": "4010 PRODUCE", "unit": "Case",
                                               "qty": "1", "amt": str(amount), "ext": f"{amount:.2f}"}]}
        for i in range(weeks)]}, db_path=db)


def test_the_headline_is_withheld_when_nights_are_missing_but_not_for_closed_days(db):
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", POUR_COA.encode(), db_path=db)
    _weekly_invoices(rid, db)
    # 12 weeks of history, closed Mondays: a closed day is no gap.
    for i in range(1, 85):
        d = T - timedelta(days=i)
        if d.weekday() != 0:
            _metric(db, rid, d.isoformat(), "sales.cat:Food", 1000.0)
    w = bo.food_bought_vs_sold(rid, today=T, db_path=db)["window"]
    assert w["pct"] is not None and w["withheld"] is None and w["trading_days"] == w["covered_nights"]
    # Six trading nights in the window lose their report: under 80% covered.
    c = _conn(db)
    gone = [(T - timedelta(days=i)).isoformat() for i in range(2, 10) if (T - timedelta(days=i)).weekday() != 0][:6]
    c.executemany("DELETE FROM dsr_metrics WHERE restaurant_id=? AND business_date=?", [(rid, d) for d in gone])
    c.commit()
    c.close()
    w = bo.food_bought_vs_sold(rid, today=T, db_path=db)["window"]
    assert w["pct"] is None and "cover" in w["withheld"] and w["covered_nights"] < 0.8 * w["trading_days"]


def test_today_defaults_to_the_restaurants_own_date(db, monkeypatch):
    import time_utils
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", POUR_COA.encode(), db_path=db)
    _weekly_invoices(rid, db)
    for i in range(0, 40):
        _metric(db, rid, (T - timedelta(days=i)).isoformat(), "sales.cat:Food", 1000.0)
    local = datetime(2026, 10, 12, 21, 0)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: local)
    w = bo.food_bought_vs_sold(rid, db_path=db)["window"]
    assert w["to"] <= local.date().isoformat()


# ── SCALE-04 / SEC-04: the purchases routes ───────────────────────────────

def test_one_purchases_request_reads_the_fintech_lines_at_most_once(db, monkeypatch):
    import bar.purchases as bp
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.read_file(rid, "p.txt", _printout([f"{B}GORDON FOOD SERVICE{NB} $900.00",
                                          _inv("9041", "10/01/2026", "$900.00")]).encode(), db_path=db)
    calls = []
    real = bp._lines

    def counted(*a, **k):
        calls.append(k.get("backoffice"))
        return real(*a, **k)
    monkeypatch.setattr(bp, "_lines", counted)
    cl = _app().test_client()
    _as(monkeypatch, rid, role="client", uid=11)
    r = cl.get("/api/food-cost/purchases")
    assert r.status_code == 200 and r.get_json()["ok"]
    assert len(calls) <= 1, calls
    assert any(s["vendor"] == "GORDON FOOD SERVICE" for s in r.get_json()["suppliers"])
    calls.clear()
    assert cl.get("/api/food-cost/purchases").status_code == 200
    assert calls == [], "the Fintech vendor mix is kept on the Bar's stamp"


def test_sorting_a_supplier_not_on_back_office_is_refused(db, monkeypatch):
    rid = _rid(db)
    bo.read_file(rid, "p.txt", _printout([f"{B}GORDON FOOD SERVICE{NB} $900.00",
                                          _inv("9041", "10/01/2026", "$900.00")]).encode(), db_path=db)
    cl = _app().test_client()
    _as(monkeypatch, rid, role="client", uid=11)
    r = cl.post("/api/food-cost/purchases/vendor", json={"vendor": "NOT A SUPPLIER", "category": "food"})
    assert r.status_code == 400 and "Back Office" in r.get_json()["error"]
    assert cl.post("/api/food-cost/purchases/vendor",
                   json={"vendor": "GORDON FOOD SERVICE", "category": "f" * 41}).status_code == 400
    assert cl.post("/api/food-cost/purchases/vendor",
                   json={"vendor": "G" * 201, "category": "food"}).status_code == 400
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM bo_vendor_categories").fetchone()[0] == 0
    c.close()


# ── SCALE-11: a degraded path says so ──────────────────────────────────────

def test_the_fintech_mix_failing_is_captured_before_it_degrades(db, monkeypatch):
    import bar.purchases as bp
    import ops
    rid = _rid(db)
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="", **k: seen.append(job))

    def boom(*a, **k):
        raise RuntimeError("no such table")
    monkeypatch.setattr(bp, "_lines", boom)
    assert bo._fintech_vendor_mix(rid, db) == {}
    assert "backoffice_fintech_mix" in seen


# ── the import script's dry run ────────────────────────────────────────────

def test_the_dry_run_names_what_a_printout_would_remove_and_hold(db, tmp_path):
    rid = _rid(db)
    bo.read_file(rid, "a.txt", _printout([f"{B}SYSCO{NB} $300.00", _inv("1", "09/05/2026", "$100.00"),
                                          _inv("2", "09/07/2026", "$200.00")]).encode(), db_path=db)
    p = bo.parse_invoices_by_vendor(_printout([f"{B}SYSCO{NB} $100.00", _inv("1", "09/05/2026", "$100.00"),
                                               _inv("3", "09/09/2026", "$0.00"),
                                               f"{B}US FOODS{NB} $9.00", _inv("4", "09/06/2026", "$1.00")]))
    pv = bo.preview_printout(rid, p, db_path=db)
    assert pv["held"] == ["US FOODS"]
    assert [(g["vendor"], g["amount"]) for g in pv["would_remove"]] == [("SYSCO", 200.0)]
    assert len(_stored(db, rid)) == 2, "a preview writes nothing"


def test_the_import_script_dry_run_reports_held_vendors_and_removals(db, tmp_path, capsys):
    import importlib.util
    import os
    rid = _rid(db)
    bo.read_file(rid, "a.txt", _printout([f"{B}SYSCO{NB} $300.00", _inv("1", "09/05/2026", "$100.00"),
                                          _inv("2", "09/07/2026", "$200.00")]).encode(), db_path=db)
    f = tmp_path / "ibv.txt"
    f.write_text(_printout([f"{B}SYSCO{NB} $100.00", _inv("1", "09/05/2026", "$100.00"),
                            _inv("3", "09/09/2026", "$0.00"),
                            f"{B}US FOODS{NB} $9.00", _inv("4", "09/06/2026", "$1.00")]), encoding="utf-8")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "backoffice_import.py")
    spec = importlib.util.spec_from_file_location("backoffice_import_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    capsys.readouterr()
    assert mod.main(["--rid", str(rid), "--file", str(f)]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["applied"] is False and got["held"] == ["US FOODS"]
    assert [(g["vendor"], g["amount"]) for g in got["would_remove"]] == [("SYSCO", 200.0)]
    assert len(_stored(db, rid)) == 2
