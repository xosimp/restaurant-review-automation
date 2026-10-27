"""A drink's cost reads the bottles a case holds from the invoice (owner,
10/27/26: Erik - "how could they sell Sneaky Fox vodka for cents on the
dollar"). Fintech's export states the case in PPC; it was kept, never read,
so every case was assumed 12 (a 6-bottle WhistlePig case cost half a pour)
and "6 BOTTLE" read as a beer's 24. Southern Glazer's $2.29-a-bottle charge,
alone on a line, stood as a bottle of whiskey; a bucket read as one can."""
from datetime import date, timedelta

import pytest

import fintech_invoices as fi
import models
from bar.products import parse_pack, unit_costs


@pytest.fixture
def db(db_path):
    return db_path


def _rid(db):
    return models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test"), db_path=db)


HEAD = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
        "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustments,Extended Price,PPC\n")
SG = "Southern Glazer's Wine & Spirits of IL"


def _row(inv, day, total, code, desc, uom, qty, cost, ppc=""):
    return f'EJs,"{SG}",1,{inv},{day},${total},{code},{desc},0,{uom},{qty},${cost},$0.00,${qty * cost:.2f},{ppc}\n'


def _lines(db, rid):
    conn = models.get_conn(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT description, uom, unit_price, kind, ppc FROM fintech_invoice_lines WHERE restaurant_id=? "
            "ORDER BY id", (rid,)).fetchall()]
    finally:
        conn.close()


def test_the_case_count_is_read_from_the_invoice_and_from_its_words():
    assert parse_pack("WHISTLEPIG BBN PIGGYBACK 6YR 100 750ML", "CA", "liquor")["units_per_case"] == 12
    pk = parse_pack("WHISTLEPIG BBN PIGGYBACK 6YR 100 750ML", "CA", "liquor", ppc=6)
    assert (pk["units_per_case"], pk["units_source"]) == (6, "invoice")
    assert parse_pack("SNEAKY FOX VODKA 6 BOTTLE 1L", "CA", "liquor")["units_per_case"] == 6     # was 24
    assert parse_pack("PALOMO ESPADIN MEZCAL 6CS 1L 92PF", "CA", "liquor")["units_per_case"] == 6  # was 12
    assert parse_pack("MILLER LITE 12NR", "CA", "beer")["units_per_case"] == 24                  # beer keeps it
    assert parse_pack("TITOS VDK HANDMADE 1L", "BO", "liquor", ppc=12)["units_per_case"] == 1    # a bottle is one
    uc = unit_costs(87.0, "CA", "SNEAKY FOX VODKA 6 BOTTLE 1L", "liquor")
    assert uc["per_each"] == 14.5 and round(uc["per_pour"], 2) == 0.64 and uc["confidence"] == "high"


def test_the_export_ppc_is_stored_and_a_lone_per_bottle_charge_is_a_charge(db):
    rid = _rid(db)
    day = (date.today() - timedelta(days=5)).strftime("%m/%d/%Y")
    csv = HEAD + "".join([
        _row("A1", day, 911.66, "000561664", "WHISTLEPIG BBN PIGGYBACK 6YR 100 750ML", "CA", 5, 126.0, 6),
        _row("A1", day, 911.66, "000028728", "BACARDI RUM SUPERIOR WHITE 80 1.0L", "BO", 6, 23.08),
        _row("A1", day, 911.66, "000028728", "BACARDI RUM SUPERIOR WHITE 80 1.0L", "BO", 1, 2.29),
        _row("A1", day, 911.66, "000524720", "BASIL HAYDENS BBN TOAST 80 750ML", "BO", 1, 2.29),
        _row("A1", day, 911.66, "000641424", "JAMESON WSKY TRIPLE TRIPLE 80 750ML", "BO", 2, 2.29)])
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    got = {(l["description"][:12], l["unit_price"]): (l["kind"], l["ppc"]) for l in _lines(db, rid)}
    assert got[("WHISTLEPIG B", 126.0)] == ("item", 6)
    assert got[("BACARDI RUM ", 2.29)][0] == "charge"
    assert got[("BASIL HAYDEN", 2.29)][0] == "charge" and got[("JAMESON WSKY", 2.29)][0] == "charge"


def test_the_backfill_reads_ppc_from_the_stored_file_and_relabels_lone_charges(db):
    rid = _rid(db)
    day = (date.today() - timedelta(days=5)).strftime("%m/%d/%Y")
    fi.ingest_file(rid, "x.csv", (HEAD + _row("B1", day, 630.0, "000561664", "WHISTLEPIG BBN PIGGYBACK 6YR 100 750ML",
                                              "CA", 5, 126.0, 6)
                                  + _row("B1", day, 630.0, "000031393", "JAGERMEISTER 70 1.0L", "BO", 4, 31.12)
                                  + _row("B1", day, 630.0, "000031393", "JAGERMEISTER 70 1.0L", "BO", 1, 2.29)
                                  + _row("B1", day, 630.0, "000412253", "JAMESON IRISH BLACK BRL 80 1.0L", "BO", 2, 2.29)
                                  ).encode(), db_path=db)
    conn = models.get_conn(db)          # rows as an older reader stored them
    conn.execute("UPDATE fintech_invoice_lines SET ppc=NULL WHERE restaurant_id=?", (rid,))
    conn.execute("UPDATE fintech_invoice_lines SET kind='item' WHERE restaurant_id=? AND description LIKE ?",
                 (rid, "%BLACK BRL%"))
    conn.commit()
    conn.close()
    assert fi.backfill_ppc_and_charges(db_path=db) >= 2
    got = {l["description"][:12]: (l["kind"], l["ppc"]) for l in _lines(db, rid) if l["unit_price"] != 2.29 or "BLACK" in l["description"]}
    assert got["WHISTLEPIG B"] == ("item", 6) and got["JAMESON IRIS"][0] == "charge"


def test_a_bucket_is_its_beers_and_a_misread_bottle_never_judges_a_price(db):
    import bar.margins as bm
    rid = _rid(db)
    conn = models.get_conn(db)
    day = (date.today() - timedelta(days=3)).isoformat()
    lines = [("b1", "Surfside Bucket", 1.0)] + [(f"s{i}", "Surfside`Bucket", 1.0) for i in range(5)] \
        + [("b2", "Domestic Bucket", 2.0)] + [(f"d{i}", "Miller Lite`Bucket", 1.0) for i in range(10)]
    for lid, name, q in lines:
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, ticket_id, business_date, "
                     "item_name, kind, qty) VALUES (?,?,?,?,?,?,?,?)", (rid, "rpower", lid, "t1", day, name, "sale", q))
    conn.commit()
    conn.close()
    assert bm.bucket_size(rid, db_path=db) == 5
    assert bm._FLOOR["liquor"] == 5.0 and bm._BUCKET_RE.search("Surfside Bucket")


def test_a_new_button_reads_the_price_list_that_morning_and_can_be_matched(db, monkeypatch):
    """Anthony, 10/27/26: a new Peach Crown button - its sales arrive the
    morning after its first night; its price list was read up to a week
    later, and Match refused it until then."""
    import pos_archive
    rid = _rid(db)
    monkeypatch.setattr(pos_archive, "PRICE_SYNC_DAYS", 7)
    conn = models.get_conn(db)
    conn.execute("INSERT INTO pos_archive_state (restaurant_id, provider, prices_read_at) "
                 "VALUES (?, 'rpower', datetime('now'))", (rid,))
    conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price, "
                 "seen_at) VALUES (?, 'rpower', 'OLD', 'L1', 'Titos', 'Level 1', 9, datetime('now'))", (rid,))
    for lid, iid, name, day in (("o1", "OLD", "Titos", "2026-10-25"), ("o2", "OLD", "Titos", "2026-10-26"),
                                ("n1", "NEW", "Peach Crown", "2026-10-26")):
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, ticket_id, business_date, "
                     "item_id, item_name, item_kind, kind, qty) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rid, "rpower", lid, "t", day, iid, name, "dish", "sale", 1))
    conn.commit()
    conn.close()
    assert pos_archive._prices_due(rid, "rpower", "2026-10-26", db) is True        # Peach Crown's first night
    assert pos_archive._prices_due(rid, "rpower", "2026-10-25", db) is False       # nothing new that night
