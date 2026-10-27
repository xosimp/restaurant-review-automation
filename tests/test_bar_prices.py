"""The Bar's price watch, products and credits (bar fix round W5, 10/15/26:
V2 V3 V15 V23 V24 V28 V33 V34 F3 F7 F8 F11 F13).

The audit (PRC, 10/14/26) read every product the old price watch flagged at
the live customer against its full purchase history: 9 of 17 increases were
false (deal endings, case tiers, monthly promo flips), and so was 75% of the
"came down" money (credit memos, tiers).

`tests/fixtures/bar_invoice_lines.json` is a SYNTHETIC six months of bar
invoices (scripts/make_bar_invoice_fixture.py — invented distributors,
invoice numbers, codes and prices; audit #25 R1: the production export it
replaced must never be in a public repository), built so each of those
traps is in it with a known answer. The verdict table below is asserted
against it, and the rules are also asserted on small histories so they hold
beyond one fixture.
"""
import json
import os
from datetime import date

import pytest

import bar_cost as bc
import fintech_invoices as fi
import models
from bar import margins as bm
from bar import prices as bp
from bar import products as bprod
from bar import spend as bs
from bar.classify import classify, is_mixer, is_rebate, product_key
from bar.purchases import _lines

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "bar_invoice_lines.json")
TODAY = date(2026, 10, 14)


def _rid(db):
    return models.create_restaurant(models.Restaurant(name="Lakeview Tap", owner_email="o@x.test"), db_path=db)


def _store(db, rid, rows):
    """Invoice lines straight into fintech_invoices / fintech_invoice_lines
    (one invoice per distributor + number), so the real _lines reads them."""
    conn = models.get_conn(db)
    inv = {}
    try:
        for n, r in enumerate(rows):
            k = (r["distributor"], r["invoice_number"])
            if k not in inv:
                cur = conn.execute(
                    "INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
                    "source_format, balanced, updated_at, ref_invoice_number) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
                    (rid, r["distributor"], r["invoice_number"], r["invoice_date"], 0, "export", 1,
                     "2026-10-14 00:00:00", r.get("ref_invoice_number")))
                inv[k] = cur.fetchone()[0]
            conn.execute(
                "INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, upc, uom, qty, "
                "unit_price, cost, deposit, adjustment, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (inv[k], n + 1, r.get("item_code"), r["description"], r.get("upc"), r.get("uom"), r["qty"],
                 r["unit_price"], r.get("cost", round(r["qty"] * r["unit_price"], 2)), r.get("deposit"),
                 r.get("adjustment"), r.get("kind", "item")))
        conn.commit()
    finally:
        conn.close()


def _buys(dist, code, desc, uom, buys, upc=None):
    """[(date, qty, price[, invoice])] → line dicts."""
    out = []
    for i, b in enumerate(buys):
        d, q, p = b[:3]
        out.append({"distributor": dist, "invoice_number": b[3] if len(b) > 3 else f"{code}-{i}",
                    "invoice_date": d, "item_code": code, "description": desc, "uom": uom, "qty": q,
                    "unit_price": p, "upc": upc})
    return out


def _watch(db, rows, today=TODAY, **kw):
    rid = _rid(db)
    _store(db, rid, rows)
    return bp.analyse(_lines(rid, db), today, **kw)["payload"], rid


def _row(pw, desc_part, uom=None):
    hits = [r for r in pw["events"] if desc_part.upper() in r["product"].upper()
            and (uom is None or r["uom"] == uom)]
    return hits[0] if hits else None


# ── the verdict table, over six synthetic months ──────────────────────────

_BOOK = {}


def load_fixture_rows():
    """The synthetic invoice lines as dicts, each credit memo carrying the
    invoice it names (the export's Ref Inv Number)."""
    data = json.load(open(FIXTURE, encoding="utf-8"))
    refs = data.get("ref_invoice_numbers") or {}
    rows = [dict(zip(data["columns"], r)) for r in data["rows"]]
    for r in rows:
        if r["invoice_number"] in refs:
            r["ref_invoice_number"] = refs[r["invoice_number"]]
    return rows


@pytest.fixture
def book(db_path):
    if "pw" not in _BOOK:
        rid = _rid(db_path)
        _store(db_path, rid, load_fixture_rows())
        lines = _lines(rid, db_path)
        a = bp.analyse(lines, TODAY)
        _BOOK.update(pw=a["payload"], lines=lines, analysis=a)
    return _BOOK


@pytest.mark.parametrize("desc,kind,persistent", [
    # Not increases: a deal that ended, twice, and one that comes and goes...
    ("ST GERMAIN", "deal_ended", False),
    ("JAMESON", "deal_ended", False),
    ("SON CUTRER", "deal_ended", False),
    # ...a quantity ladder (the order size)...
    ("TITOS", "tier", False),
    # ...a rebill, a credit memo and free goods (never a price)...
    ("MODELO", None, None),
    ("BULLEIT", None, None),
    ("BACARDI", None, None),
    # ...and the steady products around them.
    ("CASAMIGOS", None, None),
    ("WHISPERING ANGEL", None, None),
    ("KETEL ONE", None, None),
    # TRUE changes that must stay.
    ("WHITE CLAW BLK CHRY", "increase", True),
    ("CROWN ROYAL", "decrease", True),
    ("STELLA ARTOIS 1/2", "decrease", True),
    ("LITE 1/2 BBL", "decrease", True),
    ("RED BULL 8.4Z", "decrease", True),
    ("ULTRA 1/2", "decrease", True),
    # UNCERTAIN: one purchase at the new price — said, never counted.
    ("TORRESELLA", "increase", False),
])
def test_every_product_with_a_trap_reads_as_its_verdict(book, desc, kind, persistent):
    r = _row(book["pw"], desc)
    if kind is None:
        assert r is None or (r["kind"] not in ("increase", "decrease") and r["kind"] != "tier"), r and r["explain"]
        return
    assert r is not None, desc
    assert (r["kind"], r["persistent"]) == (kind, persistent), r["explain"]


def test_the_increases_are_only_the_true_one(book):
    pw = book["pw"]
    assert [r["name"] for r in pw["increases"]] == ["White Claw Black Cherry"]
    s = pw["summary"]
    assert s["up_n"] == 1 and s["up_forward_monthly_usd"] == pw["increases"][0]["forward_monthly_usd"]
    # Every trap stays out of the increases and decreases.
    names = {r["name"] for r in pw["increases"] + pw["decreases"]}
    for false in ("St. Germain", "Sonoma-Cutrer", "Jameson", "Tito's", "Modelo", "Bulleit", "Bacardi", "Coors",
                  "Michelob Ultra"):
        assert not any(n.startswith(false) for n in names), false


def test_a_deal_ending_reads_as_its_usual_price(book):
    r = _row(book["pw"], "ST GERMAIN")
    assert r["explain"] == "$205 is its usual price; $186.50 on 8/3/26 was a deal"
    assert r["regime"]["reference"] == 205.0 and r["forward_monthly_usd"] is None
    assert r not in book["pw"]["increases"]
    son = _row(book["pw"], "SON CUTRER")
    assert son["regime"] == {"reference": 168.0, "since": "2026-05-12", "since_mdy": "5/12/26"}


def test_ultra_is_a_ladder_and_its_real_change_is_two_dollars_a_keg(book):
    r = _row(book["pw"], "ULTRA 1/2")
    # The real event: every tier down $2 on 7/21 (below the 2% floor, so minor).
    assert (r["kind"], r["from"], r["to"], r["since"]) == ("decrease", 120.0, 118.0, "2026-07-21")
    assert r["persistent"] and not r["listed"] and r in book["pw"]["minor"]
    assert r["latest"]["kind"] == "tier"
    lad = next(l for l in book["pw"]["ladders"] if l["name"].startswith("Michelob Ultra"))
    assert [(t["label"], t["price"]) for t in lad["tiers"]] == [("1 keg", 128.0), ("2 kegs", 118.0),
                                                               ("3+ kegs", 112.0)]
    # "Ordering 3 kegs saves $16 a keg; you ordered 1 keg 3 times in 90 days."
    assert lad["save_per_unit"] == 16.0 and lad["small_orders_90d"] == 3 and lad["confidence"] == "high"


def test_monthly_flips_are_not_listed(book):
    for desc in ("COORS LT 24NR", "MILLER LITE 24NR"):
        r = _row(book["pw"], desc)
        assert r is None or not r["listed"], r["explain"]


def test_was_since_is_the_start_of_the_previous_price(book):
    """V34: a keg read "Was $124 since 9/30/26" when $124 began on 9/2."""
    r = _row(book["pw"], "LITE 1/2 BBL")
    assert (r["from"], r["to"], r["was_since_mdy"], r["was_until_mdy"]) == (126.0, 122.0, "9/7/26", "9/28/26")
    assert r["basis"] == "monthly posting"
    assert r["explain"] == "$122 a keg since 10/5/26, after $126 from 9/7/26 to 9/28/26"


def test_credits_are_credits_not_prices(book):
    cr = {(c["kind"], c["name"][:10]): c for c in book["pw"]["credits"]}
    m = cr[("credit", "Modelo Esp")]
    assert (m["original_price"], m["rebilled_price"], m["units"], m["credit_usd"]) == (188.0, 168.0, 4.0, 80.0)
    b = cr[("credit", "Bulleit Bo")]
    assert (b["credit_usd"], b["ref_invoice_number"]) == (70.0, "P0412085")
    assert cr[("rebate", "Michelob U")]["credit_usd"] == 80.0


def test_keg_deposit_balance_per_distributor(book):
    """F7: what each distributor holds in keg deposits — charged with each
    keg, credited three ways (an empty's own line, an empties keg line, a
    keg-credit line)."""
    dep = {d["distributor"][:8]: d for d in bs.deposits(book["lines"])}
    assert round(sum(d["charged"] for d in dep.values())) == 4680
    assert round(sum(d["credited"] for d in dep.values())) == 1560
    mid = dep["Midway B"]
    assert (mid["charged"], mid["credited"], mid["balance"]) == (1440.0, 720.0, 720.0)
    assert (dep["Lakeshor"]["kegs_in"], dep["Lakeshor"]["kegs_back"]) == (35.0, 16.0)
    assert dep["Calumet "]["balance"] == 0.0


def test_free_goods_lower_the_deal_cost(book):
    fg = [c for c in book["pw"]["credits"] if c["kind"] == "free_goods"]
    bac = next(c for c in fg if c["name"].startswith("Bacardi"))
    assert bac["credit_usd"] == 230.0 and bac["deal"]["cost_per_unit"] == 184.0
    gg = [c for c in fg if c["name"].startswith("Grey Goose")]
    assert len(gg) == 3 and all(c["credit_usd"] is None for c in gg), "never bought at a price: value unknown"


def test_case_and_bottle_buys_compare_per_bottle_and_name_the_premium(book):
    sc = {r["name"][:8]: r for r in book["pw"]["split_case"]}
    w = sc["Woodford"]
    assert (w["units_per_case"], w["per_bottle_in_case"], w["bottle_price"], w["premium_pct"]) == (6, 43.5, 58.8, 35.2)
    assert sc["Fireball"]["premium_pct"] == 18.1 and sc["Fireball"]["assumes"] == "assumes 12 a case"
    assert sc["Don Juli"]["premium_pct"] == 16.7


def test_rows_carry_the_contract(book):
    keys = {"kind", "persistent", "regime", "forward_monthly_usd", "already_paid_usd", "per_unit", "listed",
            "headline", "explain", "latest", "history", "was_since_mdy", "impact_90d", "bought_90d"}
    for r in book["pw"]["increases"] + book["pw"]["decreases"]:
        assert keys <= set(r), keys - set(r)
    for r in book["pw"]["deals"]:
        assert r["forward_monthly_usd"] is None and r["kind"] in ("deal", "deal_ended")
    tito = next(r for r in book["analysis"]["series"].values() if "TITOS" in r["line"]["description"])
    assert tito["breaks"] == [4.0]


# ── the rules on small histories ───────────────────────────────────────────

# Invented houses (scripts/make_bar_invoice_fixture.py uses the same names).
SG = "Prairie Wine & Spirits of IL"
BT = "North Branch Beverage, LLC"
MIDWAY = "Midway Beverage Co., Inc."
FOXRIVER = "Fox River Beverage Company"
CALUMET = "Calumet Beverage System, Inc."
LAKECOUNTY = "Lake County Beverage, LLC"


def test_a_deal_ending_is_not_a_price_increase(db_path):
    pw, _ = _watch(db_path, _buys(SG, "PW10021", "ST GERMAIN LIQUEUR 40 750ML", "CA",
                                   [("2026-07-06", 1, 205), ("2026-08-03", 1, 186.5), ("2026-08-31", 3, 205),
                                    ("2026-09-28", 3, 205)]))
    assert not pw["increases"] and not pw["decreases"]
    assert [r["kind"] for r in pw["deals"]] == ["deal_ended"]
    assert pw["summary"]["up_forward_monthly_usd"] == 0


def test_an_alternating_deal_never_counts(db_path):
    pw, _ = _watch(db_path, _buys(BT, "NB97846", "SON CUTRER CHARD RR RNCH 24 750ML", "CA",
                                   [("2026-05-12", 1, 168), ("2026-05-26", 1, 152), ("2026-06-23", 2, 152),
                                    ("2026-08-18", 2, 168), ("2026-09-29", 1, 152), ("2026-10-13", 1, 168)]))
    assert not pw["increases"] and not pw["decreases"] and not pw["unconfirmed"]
    r = pw["deals"][0]
    assert r["kind"] == "deal_ended" and r["regime"]["reference"] == 168.0


def test_a_quantity_tier_is_not_a_price_change(db_path):
    pw, _ = _watch(db_path, _buys(MIDWAY, "MW50094", "ULTRA 1/2 BARREL", "KE",
                                   [("2026-09-01", 3, 112), ("2026-09-08", 2, 118), ("2026-09-15", 3, 112),
                                    ("2026-09-22", 2, 118), ("2026-09-29", 3, 112), ("2026-10-06", 2, 118)]))
    assert not pw["increases"] and not pw["decreases"] and not pw["unconfirmed"]
    assert len(pw["ladders"]) == 1 and [t["price"] for t in pw["ladders"][0]["tiers"]] == [118.0, 112.0]
    assert pw["tiers"][0]["kind"] == "tier"
    assert pw["tiers"][0]["explain"] == "2 kegs at $118; 3 kegs at $112 — the order size, not a price change"


def test_a_tier_shift_is_the_change(db_path):
    pw, _ = _watch(db_path, _buys(MIDWAY, "MW50094", "ULTRA 1/2 BARREL", "KE",
                                   [("2026-07-01", 2, 124), ("2026-07-08", 3, 116), ("2026-07-15", 2, 124),
                                    ("2026-07-22", 3, 116), ("2026-09-01", 2, 119), ("2026-09-08", 3, 112),
                                    ("2026-09-15", 2, 119), ("2026-09-22", 3, 112)]))
    r = pw["decreases"][0]
    assert (r["from"], r["to"], r["persistent"]) in ((124.0, 119.0, True), (116.0, 112.0, True))
    assert r["tier"]["label"] in ("2 kegs", "3+ kegs")


def test_two_prices_on_one_invoice_are_not_a_price_change(db_path):
    rows = _buys("Halsted Coffee Supply", "HC0012", "Cold Brew Coffee Concentrate", "EA",
                 [("2026-07-21", 4, 115, "A"), ("2026-07-21", 2, 123, "A"), ("2026-08-25", 3, 115, "B"),
                  ("2026-09-24", 3, 115, "C")])
    pw, _ = _watch(db_path, rows)
    assert not pw["increases"] and not pw["decreases"] and not pw["unconfirmed"]


def test_mix_and_match_cases_share_a_tier(db_path):
    rows = []
    for inv, d, n, p in (("1", "2026-08-11", 1, 47), ("2", "2026-09-01", 1, 47), ("3", "2026-09-15", 5, 43),
                         ("4", "2026-09-29", 5, 43), ("5", "2026-10-06", 1, 47)):
        for code, flavour in (("NB37400", "PEACH"), ("NB37229", "WTRMLN"), ("NB31886", "LEMON")):
            rows += _buys(BT, code, f"HIGH NOON CKTL {flavour} LOOSE CAN 24", "CA", [(d, n, p, inv)])
    pw, _ = _watch(db_path, rows)
    assert not pw["increases"] and not pw["decreases"] and not pw["unconfirmed"]
    lad = pw["ladders"][0]
    assert lad["tiers"][-1]["label"] == "15+ cases" and lad["tiers"][-1]["mixed"], "the family's 15 cases"


def test_a_rebill_is_a_credit_not_a_price(db_path):
    rows = _buys(FOXRIVER, "FR21201", "MODELO ESP 1/2 BBL", "KE",
                 [("2026-09-24", 1, 188), ("2026-10-01", 1, 188), ("2026-10-08", 1, 188, "FR238811"),
                  ("2026-10-08", -5, 188, "FR238818"), ("2026-10-08", 5, 168, "FR238818")])
    pw, _ = _watch(db_path, rows)
    assert not pw["decreases"] and not pw["unconfirmed"]
    c = [x for x in pw["credits"] if x["kind"] == "credit"][0]
    assert (c["original_price"], c["rebilled_price"], c["units"], c["credit_usd"]) == (188.0, 168.0, 5.0, 100.0)


def test_a_credit_memo_pairs_through_its_ref_invoice_number(db_path):
    """W1 parses the printout's Ref Inv Number: a memo on its own invoice
    pairs with the line on the invoice it names, not with a same-day buy."""
    rows = _buys(SG, "PW55736", "APEROL APERITIVO 22 1.0L", "CA",
                 [("2026-07-06", 1, 186, "P0410011"), ("2026-08-10", 1, 186, "P0410018"),
                  ("2026-08-12", 1, 176, "P0410025")])
    memo = _buys(SG, "PW55736", "APEROL APERITIVO 22 1.0L", "CA", [("2026-08-12", -1, 188, "P0990011")])
    memo[0]["ref_invoice_number"] = "P0410018"
    pw, _ = _watch(db_path, rows + memo)
    c = next(x for x in pw["credits"] if x["kind"] in ("credit", "return"))
    assert (c["kind"], c["rebill_invoice_number"], c["credit_usd"]) == ("credit", "P0410018", 2.0)
    # The paired rebill (8/10) and the memo leave the series: what's left is
    # 7/6 at $186 and 8/12 at $176 — one purchase at a new price, unconfirmed.
    assert [(e["kind"], e["persistent"], e["from"], e["to"], [h["date"] for h in e["history"]])
            for e in pw["events"]] == [("decrease", False, 186.0, 176.0, ["2026-07-06", "2026-08-12"])]


def _export_then_printouts(db_path):
    """Four weekly exports (Daisy Cutter $205 a keg, Fireball $216 a case),
    then three daily printouts with no unit of measure: Daisy Cutter at
    $235, and a single Fireball bottle at $21.25 on a padded code."""
    from tests.test_fintech_intake import BANK_HEAD, BREAKTHRU, WINDY, _bank, _printout
    rid = _rid(db_path)
    csv = BANK_HEAD
    for i, d in enumerate(("8/3/2026", "8/10/2026", "8/17/2026", "8/24/2026")):
        csv += _bank(WINDY, f"E{i}", d, "$235.00", "13617", "HALF ACRE DAISY CUTTER K 1/2BBL", "KE", 1,
                     "$205.00", "$30.00", "$235.00")
        csv += _bank(BREAKTHRU, f"F{i}", d, "$216.00", "9031477", "FIREBALL CINN WHSKY 1L", "CA", 1,
                     "$216.00", "$0.00", "$216.00", upc="088004144678")
    fi.ingest_file(rid, "h.csv", csv.encode(), db_path=db_path)
    for i, d in enumerate(("09/07/2026", "09/14/2026", "09/21/2026")):
        fi.ingest_file(rid, f"p{i}.txt", _printout(WINDY, f"P{i}", d, "$265.00", [
            ("13617", "HALF ACRE DAISY CUTTER K 1/2BB", "1", "$235.00", "$265.00")]).encode(), db_path=db_path)
        fi.ingest_file(rid, f"q{i}.txt", _printout("BREAKTHRU BEVERAGE ILLINOIS LLC", f"Q{i}", d, "$21.25", [
            ("000009031477", "FIREBALL CINN WHSKY 1L", "1", "$21.25", "$21.25")]).encode(), db_path=db_path)
    return bp.analyse(_lines(rid, db_path), date(2026, 10, 1))["payload"]


def test_a_printout_price_is_compared_with_the_same_products_export_history(db_path):
    """V14: the printout carries no unit; its keg takes KE from the code's
    export history, so $205 -> $235 is seen (it read as no change)."""
    pw = _export_then_printouts(db_path)
    ups = [r for r in pw["increases"] if "DAISY" in r["product"].upper()]
    assert len(ups) == 1 and ups[0]["uom"] == "KE" and (ups[0]["from"], ups[0]["to"]) == (205.0, 235.0)


def test_printout_case_and_bottle_buys_never_compare(db_path):
    """V14: a Fireball case on the export and a bottle on a printout (same
    code, zeros padded) were one series: "-90.2%". They never compare."""
    pw = _export_then_printouts(db_path)
    fire = [r for r in pw["events"] if "FIREBALL" in r["product"].upper()]
    assert fire == [], [(r["kind"], r["from"], r["to"]) for r in fire]
    assert not any("FIREBALL" in r["product"].upper() for r in pw["decreases"] + pw["unconfirmed"])


def test_a_lone_return_comes_off_the_volume_and_is_never_a_price(db_path):
    rows = _buys(BT, "NB04842", "KETEL ONE VDK 1L", "CA",
                 [("2026-09-01", 3, 289), ("2026-09-10", 3, 289), ("2026-09-20", -1, 289, "memo")])
    pw, _ = _watch(db_path, rows)
    ret = [c for c in pw["credits"] if c["kind"] == "return"]
    assert ret and ret[0]["credit_usd"] == 289.0
    assert not pw["events"]


def test_impact_is_forward_monthly_at_the_new_regime(db_path):
    """V23: the change times the tier's usual monthly volume, and apart what
    was already paid since it started — never the change times every unit
    of 90 days as if paid."""
    pw, _ = _watch(db_path, _buys(BT, "NB08013", "CROWN ROYAL WHSKY BAR PK 1L", "CA",
                                   [("2026-07-01", 2, 410), ("2026-07-15", 2, 410), ("2026-09-01", 2, 390),
                                    ("2026-09-15", 2, 390), ("2026-10-01", 2, 390)]))
    r = pw["decreases"][0]
    # 90 days before 10/14 is 7/16: 6 cases → 2 a month.
    assert r["forward_monthly_usd"] == round(-20 * 6 / 90 * 30.44, 2)
    assert r["already_paid_usd"] == -20 * 6
    assert r["impact_90d"] == round(r["forward_monthly_usd"] * 3, 2)
    assert r["confidence"] == "high" and r["headline"]


def test_rows_not_bought_in_90_days_are_not_counted_as_went_up(db_path):
    pw, _ = _watch(db_path, _buys(CALUMET, "CB69258",
                                   "GOSLINGS GINGER BEER C24 12OZ 6P", "CA",
                                   [("2026-06-20", 3, 24.1), ("2026-06-27", 3, 24.1), ("2026-07-01", 3, 26.6),
                                    ("2026-07-08", 3, 26.6)]))
    r = pw["increases"][0]
    assert r["not_bought_lately"] and r["forward_monthly_usd"] == 0
    assert pw["summary"]["up_n"] == 0 and pw["summary"]["not_bought_lately_n"] == 1


def test_a_one_percent_flip_is_not_listed_by_default(db_path):
    pw, _ = _watch(db_path, _buys(LAKECOUNTY, "LC09215", "COORS LT 24NR LSE", "CA",
                                   [("2026-08-05", 4, 22.75), ("2026-08-12", 4, 22.75), ("2026-09-02", 4, 22.5),
                                    ("2026-09-09", 4, 22.5)]))
    assert not pw["decreases"] and pw["minor"] and not pw["minor"][0]["listed"]
    assert pw["summary"]["floor"] == {"pct": 2.0, "usd_per_unit": 0.5, "headline_monthly_usd": 10.0}


def test_a_small_monthly_change_is_listed_but_not_a_headline(db_path):
    pw, _ = _watch(db_path, _buys(BT, "NB00001", "ANGELS ENVY BRBN 750ML", "BO",
                                   [("2026-07-01", 1, 48), ("2026-07-15", 1, 48), ("2026-09-01", 1, 51),
                                    ("2026-09-15", 1, 51)]))
    r = pw["increases"][0]
    assert r["listed"] and not r["headline"], "+$3 a bottle at a bottle a month is under $10 a month"


def test_history_is_every_purchase_in_the_lookback(db_path):
    buys = [(f"2026-{m:02d}-{d:02d}", 1, 23.4) for m, d in ((7, 1), (7, 8), (7, 15), (7, 22), (7, 29), (8, 5),
                                                             (8, 12), (8, 19), (8, 26), (9, 2), (9, 9), (9, 16))]
    buys += [("2026-09-23", 1, 25.6), ("2026-09-30", 1, 25.6)]
    pw, _ = _watch(db_path, _buys(LAKECOUNTY, "LC57215", "LITE 24NR LSE", "CA", buys))
    r = pw["increases"][0]
    assert len(r["history"]) == 14 == r["purchases"], "no cap at 12 (V34)"


def test_two_codes_one_upc_are_one_product(db_path):
    rows = _buys(BT, "NB37936", "DON JULIO TEQ REP RSV NON IBC 6B 750ML", "CA",
                 [("2026-07-01", 1, 292), ("2026-07-15", 1, 292)], upc="674545000414")
    rows += _buys(BT, "NB91877", "DON JULIO TEQ REP ART BX 26 6B 750ML", "CA",
                  [("2026-09-01", 1, 308), ("2026-09-15", 1, 308)], upc="674545000414")
    pw, _ = _watch(db_path, rows)
    r = pw["increases"][0]
    assert (r["from"], r["to"]) == (292.0, 308.0)
    assert r["code_changes"] and r["code_changes"][0]["date_mdy"] == "9/1/26"
    assert r["aliases"], "the old code is the same product"


def test_placeholder_upc_never_joins_products(db_path):
    assert bprod.valid_gtin("111111111117") is None
    assert bprod.valid_gtin("<null>") is None and bprod.valid_gtin(None) is None
    assert bprod.valid_gtin("00000000001212") is None
    assert bprod.valid_gtin("674545000414") == "00674545000414"
    assert bprod.valid_gtin("674545000415") is None, "a bad check digit"
    lines = [{"product_key": "e|57152", "upc": "111111111117", "invoice_date": "2026-09-01", "kind": "item",
              "category": "beer", "distributor": "E"},
             {"product_key": "e|1993", "upc": "111111111117", "invoice_date": "2026-09-01", "kind": "item",
              "category": "beer", "distributor": "E"}]
    canon = bprod.canonical_keys(lines)
    assert canon["e|57152"] != canon["e|1993"]


def test_tier_violation_is_flagged(db_path):
    buys = [("2026-08-04", 1, 128), ("2026-08-11", 3, 112), ("2026-08-18", 1, 128), ("2026-08-25", 3, 112),
            ("2026-09-01", 1, 128), ("2026-09-08", 3, 112), ("2026-09-15", 3, 128)]
    pw, _ = _watch(db_path, _buys(MIDWAY, "MW50094", "ULTRA 1/2 BARREL", "KE", buys))
    f = pw["tier_flags"]
    assert len(f) == 1 and (f[0]["billed"], f[0]["expected"], f[0]["over_usd"]) == (128.0, 112.0, 48.0)
    assert f[0]["expected_source"] == "ladder"
    assert not pw["increases"] and not pw["unconfirmed"], "a mis-bill is not a price"


def test_the_owners_deal_sheet_flags_an_overbill(db_path):
    rows = _buys(BT, "NB08508", "TITOS VDK HANDMADE 1L", "CA",
                 [("2026-09-01", 4, 249), ("2026-09-15", 4, 249), ("2026-09-29", 4, 256)])
    rid = _rid(db_path)
    _store(db_path, rid, rows)
    key = product_key(BT, "NB08508", "")
    bprod.set_expected_price(rid, key, 4, 249, valid_from="2026-09-01", db_path=db_path)
    pw = bp.analyse(_lines(rid, db_path), TODAY, deal_sheet=bprod.deal_sheet(rid, db_path))["payload"]
    f = pw["tier_flags"]
    assert len(f) == 1 and f[0]["expected_source"] == "owner" and f[0]["over_usd"] == 28.0


# ── products: packs, per pour ──────────────────────────────────────────────

@pytest.mark.parametrize("desc,uom,cat,units,oz", [
    ("WOODFORD RSV BRBN 6B 1L", "CA", "liquor", 6, 33.81),
    ("TITOS VDK HANDMADE 1L", "CA", "liquor", 12, 33.81),
    ("LITE 24NR LSE", "CA", "beer", 24, 12.0),
    ("HIGH NOON CKTL LEMON CAN 6/4PK 355", "CA", "beer", 24, 12.0),
    ("CIDERBOYS PEACH 4/6/12 NR", "CA", "beer", 24, 12.0),
    ("GREENS DRY HOP LAGER 6/4/11.2 CANS", "CA", "beer", 24, 11.2),
    ("GOSLINGS GINGER BEER C24 12OZ 6P", "CA", "na", 24, 12.0),
    ("TORRESELLA P GRIGIO 12/750", "CA", "wine", 12, 25.36),
    ("DON JULIO TEQ 1942 6B 750ML", "BO", "liquor", 1, 25.36),
])
def test_the_pack_parser_reads_distributor_shapes(desc, uom, cat, units, oz):
    pk = bprod.parse_pack(desc, uom, cat)
    assert pk["units_per_case"] == units and round(pk["container_oz"], 2) == oz


def test_vintage_number_is_not_a_pack():
    pk = bprod.parse_pack("CH ESCLANS WHISPER ANGEL ROSE 25 750ML", "CA", "wine")
    assert pk["vintage"] == 2025 and pk["units_per_case"] == 12 and pk["units_source"] == "assumed"
    st = bprod.parse_pack("ST GERMAIN LIQUEUR 40 750ML", "CA", "liquor")
    assert st["proof"] == 40 and st["units_per_case"] == 12
    lite = bprod.parse_pack("LITE 24NR LSE", "CA", "beer")
    assert lite["units_per_case"] == 24 and lite["units_source"] == "parsed"


@pytest.mark.parametrize("desc,oz", [("ULTRA 1/2 BARREL", 1984.0), ("EAGLE PARK SLTZR 1/4 BBL", 992.0),
                                     ("REVOLUTION K 1/6 BBL", 661.0), ("KROMBACH 50L", 1690.7)])
def test_kegs_hold_their_ounces(desc, oz):
    assert bprod.parse_pack(desc, "KE", "beer")["keg_oz"] == oz


def test_per_pour_cost_uses_the_owners_pours():
    u = bprod.unit_costs(249, "CA", "TITOS VDK HANDMADE 1L", "liquor")
    assert u["per_bottle"] == 20.75 and u["per_oz"] == round(20.75 / 33.81, 3)
    assert round(u["per_pour"], 2) == round(20.75 / 33.814 * 1.5, 2) == 0.92 and u["confidence"] == "medium"
    assert u["assumes"] == ["assumes 12 a case"]
    keg = bprod.unit_costs(118, "KE", "ULTRA 1/2 BARREL", "beer")
    assert keg["per_oz"] == round(118 / 1984, 3) and keg["pour_oz"] == 16.0
    assert round(keg["per_pour"], 2) == round(118 / 1984 * 16 / 0.9, 2) and "90% of the keg poured" in keg["assumes"]
    two = bprod.unit_costs(249, "CA", "TITOS VDK HANDMADE 1L", "liquor", pour={"liquor_oz": 2.0})
    assert round(two["per_pour"], 2) == round(20.75 / 33.814 * 2.0, 2)


def test_the_owners_pours_and_mixer_switch_are_stored_and_bounded(db_path):
    rid = _rid(db_path)
    assert bprod.settings(rid, db_path)["liquor_oz"] == 1.5
    bprod.set_settings(rid, db_path=db_path, liquor_oz=1.25, count_mixers=True)
    s = bprod.settings(rid, db_path)
    assert s["liquor_oz"] == 1.25 and s["count_mixers"] is True and s["wine_oz"] == 5.0
    with pytest.raises(ValueError):
        bprod.set_settings(rid, db_path=db_path, liquor_oz=9)
    bprod.set_settings(rid, db_path=db_path, liquor_oz="")
    assert bprod.settings(rid, db_path)["liquor_oz"] == 1.5


# ── classify (V28, V33) ────────────────────────────────────────────────────

@pytest.mark.parametrize("dist,desc,uom,want", [
    ("Euclid Beverage", "TOPO CHICO SPARKLING WATER", "CA", ("na", True)),
    ("Chicago Beverage", "CANADA DRY GINGER ALE", "CA", ("na", True)),
    ("Euclid Beverage", "HEINEKEN 0.0 NA 6NR", "CA", ("na", True)),
    ("Euclid Beverage", "TOPO CHICO HARD SELTZER", "CA", ("beer", True)),
    ("Breakthru Beverage", "HEAVEN HILL BTL IN BOND", "CA", ("liquor", True)),
    ("Euclid Beverage", "KEG DEPOSIT", "EA", ("deposit", True)),
    ("Euclid Beverage", "EMPTY 1/2 BBL", "CA", ("deposit", True)),
    ("Superior Beverage", "EMPTIES-AB BEER 1/2 BBL", "KE", ("deposit", True)),
    ("Superior Beverage", "AB 2 PRODUCT DISCOUNT", "CA", ("beer", False)),
    ("Superior Beverage", "BUD LIGHT 24 BOTTLE 12OZ", "CA", ("beer", True)),
])
def test_products_sort_from_their_names_v28(dist, desc, uom, want):
    assert classify(dist, desc, uom) == want


@pytest.mark.parametrize("desc", ["MYSTERY BTL", "SOMETHING CAN", "HOUSE ALE", "MYSTERY LIGHT", "PINK ROSE",
                                  "MYSTERY SPARKLING"])
def test_a_weak_word_match_is_a_guess_for_the_owner(desc):
    cat, sure = classify("Nobody Inc", desc, "CA")
    assert cat in ("beer", "wine") and sure is False


def test_product_discount_line_reduces_category_cost(db_path):
    rid = _rid(db_path)
    # The house name decides the discount line's category ("AB 2 PRODUCT
    # DISCOUNT" names no product): one of classify's beer houses.
    _store(db_path, rid, _buys("Superior Beverage Co., Inc.", "MW10007", "BUD LIGHT 24 BOTTLE 12OZ", "CA",
                               [("2026-10-01", 40, 23.1)])
           + [{"distributor": "Superior Beverage Co., Inc.", "invoice_number": "MW10007-0", "invoice_date": "2026-10-01",
               "item_code": "MW10263", "description": "AB 2 PRODUCT DISCOUNT", "uom": "CA", "qty": -40,
               "unit_price": 2.0, "cost": -80.0}])
    lines = _lines(rid, db_path)
    disc = next(l for l in lines if "DISCOUNT" in l["description"])
    assert disc["category"] == "beer" and disc["rebate"] and disc["net"] == -80.0
    assert round(sum(l["net"] for l in lines if l["category"] == "beer"), 2) == round(40 * 23.1 - 80, 2)
    assert is_rebate("SUMMARY LEVEL DISCOUNT", "adjustment") is False
    d = bs._distributors(lines)[0]
    assert d["rebates"] == -80.0 and d["fees"] == 0


def test_mixers_stay_non_alcoholic_and_the_owner_can_count_them(db_path):
    rid = _rid(db_path)
    _store(db_path, rid, _buys(BT, "NB06996", "RED BULL 8.4Z", "CA", [("2026-10-01", 2, 39.9)])
           + _buys(BT, "NB47652", "REAL PSNFRT NA PET 6B 16.9Z", "CA", [("2026-10-01", 1, 37.75)])
           + _buys(BT, "NB00002", "TOPO CHICO SPARKLING WATER", "CA", [("2026-10-01", 1, 30)]))
    lines = _lines(rid, db_path)
    assert {l["description"]: (l["category"], l["mixer"]) for l in lines} == {
        "RED BULL 8.4Z": ("na", True), "REAL PSNFRT NA PET 6B 16.9Z": ("na", True),
        "TOPO CHICO SPARKLING WATER": ("na", False)}
    mx = bs.mixers(lines, "2026-09-04", "2026-10-01")
    assert mx["usd"] == 117.55 and mx["counted"] is False
    assert is_mixer("OLIVE BRINE") and not is_mixer("TOPO CHICO SPARKLING WATER")
    bprod.set_settings(rid, db_path=db_path, count_mixers=True)
    counted = _lines(rid, db_path)
    assert {l["category"] for l in counted if l["mixer"]} == {"liquor"}


# ── drink margins (F11) and ordering rhythm (F13) ──────────────────────────

def test_drink_margin_and_the_price_that_restores_it():
    """$240 → $340 a case of 12 × 750 ml, a 1.5 oz pour, an $11 drink."""
    before = bprod.unit_costs(240, "CA", "HOUSE VODKA 12/750", "liquor")["per_pour"]
    now = bprod.unit_costs(340, "CA", "HOUSE VODKA 12/750", "liquor")["per_pour"]
    oz = 750 / 29.5735
    assert abs(before - 240 / 12 / oz * 1.5) < 1e-3 and abs(now - 340 / 12 / oz * 1.5) < 1e-3
    m = bm.drink_margin(now, 11, before)
    assert m["cost_pct_before"] == 10.8 and m["cost_pct"] == 15.2
    assert m["restore_price"] == 15.58, "the price that brings the drink back to 10.8%"
    assert m["cost"] == 1.68


def test_margins_read_only_confirmed_pairs_and_suggest_the_rest(db_path):
    rid = _rid(db_path)
    rows = _buys(BT, "NB08508", "TITOS VDK HANDMADE 1L", "CA", [("2026-08-01", 1, 240), ("2026-08-15", 1, 240),
                                                                ("2026-09-15", 1, 280), ("2026-09-29", 1, 280)])
    _store(db_path, rid, rows)
    conn = models.get_conn(db_path)
    for item, name, price in (("11", "Titos Vodka", 9.0), ("12", "Titos Soda", 10.0)):
        conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "rpower", item, "1", name, "Regular", price))
    conn.commit()
    conn.close()
    a = bp.analyse(_lines(rid, db_path), TODAY)
    out = bm.margins(rid, a, db_path=db_path)
    assert out["rows"] == [] and {s["item_id"] for s in out["suggestions"]} == {"11", "12"}
    bm.set_drink_map(rid, "rpower", "11", product_key(BT, "NB08508", ""), pour_oz=1.5, db_path=db_path)
    out = bm.margins(rid, a, db_path=db_path)
    r = out["rows"][0]
    assert r["item"] == "Titos Vodka" and r["changed_mdy"] == "9/15/26"
    assert r["cost"] == round(280 / 12 / 33.814 * 1.5, 2) and r["restore_price"] > 9.0
    assert [s["item_id"] for s in out["suggestions"]] == ["12"]


def test_a_sku_bought_weekly_then_not_gives_one_hint():
    lines = [{"product_key": "e|1", "kind": "item", "qty": 2, "unit_price": 22.75, "category": "beer",
              "invoice_date": f"2026-06-{d:02d}", "description": "COORS LT 24NR LSE", "distributor": "E", "uom": "CA"}
             for d in (1, 8, 15, 22, 29)] + [
             {"product_key": "e|1", "kind": "item", "qty": 2, "unit_price": 22.75, "category": "beer",
              "invoice_date": "2026-07-06", "description": "COORS LT 24NR LSE", "distributor": "E", "uom": "CA"}]
    hints = bprod.reorder_hints(lines, date(2026, 9, 14))
    assert len(hints) == 1 and hints[0]["usual_every_days"] == 7 and hints[0]["days_since"] == 70
    assert bprod.reorder_hints(lines, date(2026, 7, 12)) == [], "not due yet"


def test_the_build_payload_carries_the_new_blocks(db_path):
    rid = _rid(db_path)
    _store(db_path, rid, _buys(BT, "NB08508", "TITOS VDK HANDMADE 1L", "CA",
                               [("2026-09-01", 1, 262), ("2026-09-15", 1, 262)]))
    out = bc.build(rid, db_path=db_path, today=TODAY)
    for k in ("deposits", "mixers", "margins", "reorder", "bar_settings"):
        assert k in out, k
    pw = out["price_watch"]
    for k in ("credits", "ladders", "split_case", "tier_flags", "deals", "tiers", "unconfirmed", "minor", "events"):
        assert k in pw, k


def test_a_name_match_on_a_flavour_or_a_style_alone_is_hidden(db_path):
    # Will, 10/15/26: "hide weak name matches" — "Mango Mojito" was offered
    # White Claw Mango and "Gl Moet Brut" Zardetto Prosecco Brut (40% each);
    # "Titos Vodka" (also 40%) names the brand and stays.
    rid = _rid(db_path)
    rows = (_buys(BT, "NB08508", "TITOS VDK HANDMADE 1L", "CA", [("2026-08-01", 1, 240), ("2026-09-15", 1, 240)])
            + _buys(BT, "NB54304", "WHITE CLAW MANGO 6CN", "CA", [("2026-08-01", 1, 30), ("2026-09-15", 1, 30)]))
    _store(db_path, rid, rows)
    conn = models.get_conn(db_path)
    for item, name, price in (("11", "Titos Vodka", 9.0), ("13", "Mango Mojito", 12.0)):
        conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "rpower", item, "1", name, "Regular", price))
    conn.commit()
    conn.close()
    out = bm.margins(rid, bp.analyse(_lines(rid, db_path), TODAY), db_path=db_path)
    assert {s["item"] for s in out["suggestions"]} == {"Titos Vodka"}
