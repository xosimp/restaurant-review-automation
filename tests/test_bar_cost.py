"""The Bar page (Food Cost → Bar, 10/14/26): price watch and purchase-based
pour cost by category from the Fintech invoices and the nightly report's
sales by category."""
from datetime import date

import pytest

import bar_cost as bc
import fintech_invoices as fi
import models


@pytest.fixture
def db(db_path):
    return db_path


def _rid(db):
    return models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test"), db_path=db)


HEAD = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
        "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n")


def _row(vendor, inv, day, total, code, desc, uom, qty, cost):
    return f'EJs,"{vendor}",1,{inv},{day},${total},{code},{desc},0,{uom},{qty},${cost},$0.00\n'


def _sales(db, rid, day, beer=0, liquor=0, wine=0):
    conn = models.get_conn(db)
    for cat, v in (("Beer", beer), ("Liquor", liquor), ("Wine", wine)):
        conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                     "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, "sales.cat:" + cat, v))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("dist,desc,uom,want", [
    ("Chicago Beverage", "HALF ACRE DAISY CUTTER K 1/2BB", "KE", "beer"),
    ("Breakthru Beverage Illinois, LLC", "HIGH NOON VODKA SELTZER", "CA", "beer"),
    ("Breakthru Beverage Illinois, LLC", "CASAMIGOS TEQ BL 6B 1L", "CA", "liquor"),
    ("Southern Glazer's", "BASIL HAYDENS BBN 80 1.0L", "CA", "liquor"),
    ("Breakthru Beverage Illinois, LLC", "CH SOUVERAIN P NOIR CALIF 750ML", "CA", "wine"),
    ("Breakthru Beverage Illinois, LLC", "RED BULL 8.4Z", "CA", "na"),
    ("Euclid Beverage", "MHL 24NR HG", "CA", "beer"),
    ("Euclid Beverage", "GLASSWARE 22Z - QTY 24", "EA", "supplies"),
    ("Schamberger Bros.", "SERVICE CHARGE", "EA", "fee"),
    ("EM Spirits LLC dba Martini Coffee Roasters", "Kyoto-Style Cold Brew Coffee Concentrate", "EA", "na"),
])
def test_products_sort_from_their_names(dist, desc, uom, want):
    assert bc.classify(dist, desc, uom)[0] == want


def test_pour_cost_reads_purchases_against_sales_by_category(db):
    rid = _rid(db)
    csv = HEAD + _row("Chicago Beverage", "A1", "10/01/2026", "458.00", "13682", "DAISY CUTTER K 1/2BB", "KE", 2, "229.00") \
        + _row("Breakthru Beverage", "B1", "10/02/2026", "300.00", "9031", "TITOS VDK 1L", "CA", 1, "300.00")
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
        _sales(db, rid, d, beer=1100, liquor=750, wine=100)
    out = bc.build(rid, db_path=db, today=date(2026, 10, 4))
    # The window ends where both are on file (the last invoice, 10/2): 10/3's sales wait for its invoices.
    assert out["pour_cost"]["window"]["to"] == "2026-10-02"
    cats = {c["key"]: c for c in out["pour_cost"]["categories"]}
    assert cats["beer"]["purchases"] == 458.0 and cats["beer"]["sales"] == 2200.0
    assert cats["beer"]["pct"] == round(458 / 2200 * 100, 1)
    assert cats["liquor"]["pct"] == 20.0 and cats["liquor"]["tone"] == "good"
    assert cats["wine"]["pct"] is None, "no wine invoices is never 0%"


def test_a_keg_deposit_inside_the_printout_cost_is_not_beer_cost(db):
    rid = _rid(db)
    printout = """Store Number   : 000001
Distributor    : Chicago Beverage
Invoice Number : 101
Invoice Date   : 10/01/2026
Invoice Total  : $229.00
Due Date       : 10/01/2026

Item            Description                           Qty      Price           Cost
----------      ------------------------------       ----   --------      ---------
13682           HALF ACRE DAISY CUTTER K 1/2BB        1       $199.00       $229.00
                                                        Invoice Total:      $229.00
"""
    fi.ingest_file(rid, "p.txt", printout.encode(), db_path=db)
    _sales(db, rid, "2026-10-01", beer=1000)
    cats = {c["key"]: c for c in bc.build(rid, db_path=db, today=date(2026, 10, 2))["pour_cost"]["categories"]}
    assert cats["beer"]["purchases"] == 199.0


def test_price_watch_names_the_latest_change_and_what_it_costs(db):
    rid = _rid(db)
    csv = HEAD + _row("Breakthru Beverage", "B1", "08/01/2026", "300.00", "9031", "TITOS VDK 1L", "CA", 2, "150.00") \
        + _row("Breakthru Beverage", "B2", "09/01/2026", "320.00", "9031", "TITOS VDK 1L", "CA", 2, "160.00") \
        + _row("Breakthru Beverage", "B3", "09/20/2026", "480.00", "9031", "TITOS VDK 1L", "CA", 3, "160.00")
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    pw = bc.build(rid, db_path=db, today=date(2026, 10, 1))["price_watch"]
    assert len(pw["increases"]) == 1 and not pw["decreases"]
    up = pw["increases"][0]
    assert (up["from"], up["to"], up["change"], up["unit"]) == (150.0, 160.0, 10.0, "case")
    assert up["since"] == "2026-09-01" and up["impact_90d"] == 10.0 * 7


def test_the_owner_sorts_a_product_once_and_it_holds(db):
    rid = _rid(db)
    csv = HEAD + _row("Winebow Inc", "W1", "10/01/2026", "100.00", "777", "MYSTERY THING", "CA", 1, "100.00")
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    out = bc.build(rid, db_path=db, today=date(2026, 10, 2))
    p = next(x for x in out["products"] if x["product"] == "MYSTERY THING")
    assert p["category"] == "wine" and p["sure"] is False and out["to_check"] == 1
    bc.set_category(rid, p["product_key"], "liquor", user_id=1, db_path=db)
    p2 = next(x for x in bc.build(rid, db_path=db, today=date(2026, 10, 2))["products"] if x["product"] == "MYSTERY THING")
    assert p2["category"] == "liquor" and p2["owner_set"] is True
    with pytest.raises(ValueError):
        bc.set_category(rid, p["product_key"], "nonsense", db_path=db)


def test_no_invoices_says_so(db):
    rid = _rid(db)
    out = bc.build(rid, db_path=db)
    assert out["available"] is False and "Fintech" in out["why_not"]
