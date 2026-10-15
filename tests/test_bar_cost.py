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
    assert cats["liquor"]["pct"] == 20.0
    # Two days is shown, never judged (Bar audit V8 — this test used to pin a
    # "good" tone on this two-day window).
    assert cats["liquor"]["tone"] is None and cats["liquor"]["judged"] is False
    assert cats["liquor"]["not_judged"] == "short_window" and out["pour_cost"]["window"]["days"] == 2
    assert out["pour_cost"]["basis"] == "purchases" and out["pour_cost"]["label"] == "Bought vs sold"
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


def test_a_distributor_gone_quiet_is_said_and_its_category_not_judged(db):
    """EJ's history was Fintech's PAID-invoice export (10/14/26): the spirits
    house's last weeks were missing and liquor read 7%."""
    rid = _rid(db)
    rows = ""
    for i, d in enumerate(("08/04", "08/11", "08/18", "08/25", "09/01", "09/08")):
        rows += _row("Breakthru Beverage", f"B{i}", d + "/2026", "300.00", "9031", "TITOS VDK 1L", "CA", 1, "300.00")
    rows += _row("Chicago Beverage", "C1", "10/05/2026", "229.00", "13682", "DAISY CUTTER K 1/2BB", "KE", 1, "229.00")
    fi.ingest_file(rid, "x.csv", (HEAD + rows).encode(), db_path=db)
    # Sales every night (three scattered nights read as a closure since the
    # gap detector learned that a silent span with no sales is no gap, V9).
    from datetime import timedelta
    for k in range(35):
        _sales(db, rid, (date(2026, 9, 1) + timedelta(days=k)).isoformat(), beer=500, liquor=900, wine=0)
    pc = bc.build(rid, db_path=db, today=date(2026, 10, 6))["pour_cost"]
    assert pc["complete"] is False and pc["gaps"][0]["distributor"] == "Breakthru Beverage"
    liq = next(c for c in pc["categories"] if c["key"] == "liquor")
    assert liq.get("incomplete") is True and liq["tone"] is None


def test_a_tax_line_is_never_a_price_change(db):
    """EJ's Elgin invoices print sales tax as an item line: it read as a
    69.9% price drop in Price watch (10/14/26)."""
    rid = _rid(db)
    csv = HEAD + _row("Elgin Beverage", "E1", "09/01/2026", "12.00", "99901", "SALES TAX", "EA", 1, "5.25") \
        + _row("Elgin Beverage", "E2", "09/15/2026", "12.00", "99901", "SALES TAX", "EA", 1, "1.58")
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    pw = bc.build(rid, db_path=db, today=date(2026, 10, 1))["price_watch"]
    assert not pw["increases"] and not pw["decreases"]


def test_the_week_by_week_trend_reads_every_point_of_a_steady_house(db):
    """Renamed (Bar audit EDG-24): it was called "…gaps a window with
    invoices missing" and asserted there were none. The gap branch is
    tests/test_bar_judge.py's
    test_a_trend_window_with_a_house_missing_is_a_gap_and_its_total_is_partial."""
    rid = _rid(db)
    rows = ""
    days = [f"{m:02d}/{d:02d}/2026" for m, d in ((8, 3), (8, 10), (8, 17), (8, 24), (8, 31), (9, 7), (9, 14),
                                                (9, 21), (9, 28), (10, 5))]
    for i, d in enumerate(days):
        rows += _row("Chicago Beverage", f"C{i}", d, "229.00", "13682", "DAISY CUTTER K 1/2BB", "KE", 1, "229.00")
    fi.ingest_file(rid, "x.csv", (HEAD + rows).encode(), db_path=db)
    from datetime import timedelta
    d0 = date(2026, 8, 3)
    for k in range(64):
        _sales(db, rid, (d0 + timedelta(days=k)).isoformat(), beer=150)
    pc = bc.build(rid, db_path=db, today=date(2026, 10, 6))["pour_cost"]
    trend = pc["trend"]
    assert len(trend) >= 5 and trend[-1]["end"] == pc["window"]["to"]
    assert all(p["beer"] is not None for p in trend)
    beer = next(c for c in pc["categories"] if c["key"] == "beer")
    assert beer["spark"] == [p["beer"] for p in trend] and beer["prev_pct"] == trend[-5]["beer"]
    assert pc["total"]["range"] == [20.0, 26.0], "an all-beer mix takes beer's range"


def test_the_insights_say_only_what_was_measured(db):
    rid = _rid(db)
    csv = ""
    for i, (code, desc, p1, p2, q) in enumerate((("1", "TITOS VDK 1L", 150, 170, 6), ("2", "KETEL ONE VDK 1L", 290, 309, 6),
                                                 ("3", "ST GERMAIN LIQUEUR 750ML", 180, 198, 7),
                                                 ("4", "CASAMIGOS TEQ BL 1L", 400, 402, 2))):
        csv += _row("Breakthru Beverage", f"A{i}", "08/10/2026", str(p1), code, desc, "CA", 1, str(p1))
        csv += _row("Breakthru Beverage", f"B{i}", "09/10/2026", str(p2 * (q - 1)), code, desc, "CA", q - 1, str(p2))
    fi.ingest_file(rid, "x.csv", (HEAD + csv).encode(), db_path=db)
    out = bc.build(rid, db_path=db, today=date(2026, 10, 1))
    keys = [i["key"] for i in out["insights"]]
    top = out["price_watch"]["increases"][0]
    assert top["name"] == "ST Germain Liqueur 750ML"  # +$18 x 7 > +$20 x 6
    # A single higher purchase is not a change that held (Bar audit V2/V24):
    # a price card needs W5's kind "increase" and `persistent`; and four
    # increases never make a concentration card (V25 — this test used to pin
    # that tautology). The price-card rules are tests/test_bar_judge.py's.
    held = [r for r in out["price_watch"]["increases"] if r.get("persistent") is True and r.get("kind") == "increase"]
    assert all(k == "price:" + r["product_key"] for k, r in zip([k for k in keys if k.startswith("price:")], held))
    assert "concentration" not in keys
    assert all(set(i) == {"key", "tone", "headline", "metric", "action", "basis", "confidence"}
               for i in out["insights"])
    assert not any(i["key"].startswith(("over:", "trend:")) for i in out["insights"]), "no sales: nothing judged"
    s = out["price_watch"]["summary"]
    assert s["up_n"] == 4 and s["up_impact_90d"] == round(sum(r["impact_90d"] for r in out["price_watch"]["increases"]), 2)
    assert bc.display_name("KETEL ONE VDK 1L") == "Ketel ONE VDK 1L"


def test_the_owners_own_targets_judge_the_bar_when_set(db):
    """Account -> Targets (10/14/26): with a target of their own a category is
    judged against it — over it is a warning with the dollars over — and the
    industry range stays as context; without one the range judges."""
    # Four whole weeks (Bar audit V8: this test used to judge a two-day
    # window): a keg and a case of liquor every week, sales every night.
    from datetime import timedelta
    rid = _rid(db)
    csv = HEAD
    for k in range(5):
        d = (date(2026, 9, 6) + timedelta(days=7 * k)).strftime("%m/%d/%Y")
        csv += _row("Chicago Beverage", f"A{k}", d, "458.00", "13682", "DAISY CUTTER K 1/2BB", "KE", 2, "229.00")
    for k in range(4):
        d = (date(2026, 9, 10) + timedelta(days=7 * k)).strftime("%m/%d/%Y")
        csv += _row("Breakthru Beverage", f"B{k}", d, "300.00", "9031", "TITOS VDK 1L", "CA", 1, "300.00")
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    for k in range(28):
        _sales(db, rid, (date(2026, 9, 7) + timedelta(days=k)).isoformat(), beer=550, liquor=6000 / 28, wine=100)
    models.update_restaurant(rid, {"liquor_cost_target": 18.0, "bar_cost_target": 25.0}, db_path=db)
    out = bc.build(rid, db_path=db, today=date(2026, 10, 5))
    pc = out["pour_cost"]
    assert pc["window"]["days"] == 28
    cats = {c["key"]: c for c in pc["categories"]}
    liq = cats["liquor"]
    assert liq["pct"] == 20.0 and liq["target"] == 18.0 and liq["target_source"] == "owner"
    assert liq["judged"] is True
    assert liq["tone"] == "warn", "20% is a reading over the owner's 18%"
    assert liq["over_target_usd"] == round(1200 - 18.0 / 100 * liq["sales"], 2)
    assert cats["beer"]["target"] is None and cats["beer"]["target_source"] == "industry"
    assert pc["total"]["target"] == 25.0
    keys = [i["key"] for i in out["insights"]]
    assert "over_target:liquor" in keys
