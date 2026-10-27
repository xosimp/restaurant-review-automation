"""
scripts/make_bar_invoice_fixture.py — write tests/fixtures/bar_invoice_lines.json:
a SYNTHETIC bar purchase history for the price-watch tests (audit #25 A01-01,
R1). It replaced a production export of a customer's invoice lines, which a
public repository must never carry.

Nothing here is anybody's purchasing: the distributors, invoice numbers, item
codes and prices are invented. The products are ordinary market brands, so
the reader's name and pack rules (bar.common, bar.products) see the kind of
descriptions a distributor prints. The history is built to hold each thing
the price watch must tell apart, on purpose and with known answers:

  a deal that ended (two products) and a deal that alternates; a quantity
  ladder and a small real change across it; a mix of sizes that is a tier
  and not a price; monthly postings and a was-since; true increases and
  decreases; one purchase at a new price (unconfirmed); a credit rebill on
  one invoice and a credit memo naming its invoice; a product's own rebate;
  free goods beside a deal and free goods never bought at a price; case and
  bottle buys of one product; keg deposits charged and credited three ways;
  and around them the steady products, fees and adjustments every week has.

tests/test_bar_prices.py and tests/test_bar_page.py assert those answers as
literals. Re-running this script writes the same file (no randomness):

    python scripts/make_bar_invoice_fixture.py
"""
import json
import os
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures", "bar_invoice_lines.json")
COLUMNS = ["distributor", "invoice_number", "invoice_date", "item_code", "description", "upc", "uom", "qty",
           "unit_price", "cost", "deposit", "adjustment", "kind"]
FIRST, LAST = date(2026, 5, 4), date(2026, 10, 13)

# Invented houses. Their weekday is the delivery day; `fmt` the invoice
# number's shape; `fee` the line every invoice of theirs carries.
PRAIRIE = "Prairie Wine & Spirits of IL"
NORTH = "North Branch Beverage, LLC"
LAKESHORE = "Lakeshore Brewing Distributors, Inc."
CALUMET = "Calumet Beverage System, Inc."
FOXRIVER = "Fox River Beverage Company"
MIDWAY = "Midway Beverage Co., Inc."
LAKECOUNTY = "Lake County Beverage, LLC"
WOLFPOINT = "Wolf Point Spirits Co."

HOUSES = {
    PRAIRIE: {"day": 0, "fmt": "P0{:06d}", "start": 412001,
              "fee": ("0", "Summary Level Adjustments", None, "EA", 1.0, 0.0, 9.0, "adjustment")},
    NORTH: {"day": 1, "fmt": "NB{:07d}", "start": 3307101,
            "fee": ("NB9001", "Service Fee", None, "BO", 1.0, 12.0, 12.0, "fee")},
    LAKESHORE: {"day": 2, "fmt": "LS-{:06d}", "start": 550101, "fee": None},
    CALUMET: {"day": 3, "fmt": "7{:08d}", "start": 40012001,
              "fee": ("CB0500", "POS FUEL CHARGE MISCELLANEOUS", None, "EA", 1.0, 9.5, 9.5, "fee")},
    FOXRIVER: {"day": 3, "fmt": "FR{:06d}", "start": 230101,
               "fee": ("FR9124", "DELIVERY CHARGE", None, "EA", 1.0, 7.5, 7.5, "fee")},
    MIDWAY: {"day": 1, "fmt": "M{:06d}", "start": 610201,
             "fee": ("MW9928", "DELIVERY FEE", None, "CA", 1.0, 9.0, 9.0, "fee")},
    LAKECOUNTY: {"day": 0, "fmt": "LC-{:07d}", "start": 7700101,
                 "fee": ("0", "Summary Level Adjustments", None, "EA", 1.0, 0.0, 8.0, "adjustment")},
    WOLFPOINT: {"day": 2, "fmt": "WP{:06d}", "start": 880301, "fee": None},
}

DEPOSIT = 30.0          # a keg's deposit, charged inside the keg line (its adjustment)


def _d(s):
    return date.fromisoformat(s)


class Book:
    """Invoices by (house, date) and their lines, in order."""

    def __init__(self):
        self.invoices = {}
        self.order = []
        self.lines = []
        self.seq = {h: v["start"] for h, v in HOUSES.items()}

    def invoice(self, house, day, number=None):
        key = (house, day.isoformat(), number)
        if key not in self.invoices:
            if number is None:
                number = HOUSES[house]["fmt"].format(self.seq[house])
                self.seq[house] += 7
            self.invoices[key] = number
            self.order.append(key)
        return self.invoices[key]

    def line(self, house, day, code, desc, uom, qty, price, upc=None, kind="item", number=None,
             adjustment=None, cost=None):
        inv = self.invoice(house, day, number)
        if cost is None:
            cost = round(qty * price + (adjustment or 0.0), 2)
        self.lines.append([house, inv, day.isoformat(), code, desc, upc or "<null>", uom, float(qty),
                           float(price), float(cost), None, adjustment, kind])
        return inv

    def keg(self, house, day, code, desc, qty, price, upc=None, number=None):
        """A keg line: the deposit rides in the cost, as the export prints it."""
        return self.line(house, day, code, desc, "KE", qty, price, upc=upc, number=number,
                         adjustment=round(DEPOSIT * qty, 2) if qty else None)


def _weekdays(house):
    day = HOUSES[house]["day"]
    d = FIRST + timedelta(days=(day - FIRST.weekday()) % 7)
    while d <= LAST:
        yield d
        d += timedelta(days=7)


def build():
    b = Book()

    # ── the products with something to say (the tests' answers) ────────────

    # A deal that ended, twice (Prairie).
    for s, q, p in (("2026-06-01", 1, 205.0), ("2026-07-06", 1, 205.0), ("2026-08-03", 1, 186.5),
                    ("2026-08-31", 2, 205.0), ("2026-09-28", 2, 205.0)):
        b.line(PRAIRIE, _d(s), "PW10021", "ST GERMAIN LIQUEUR 40 750ML", "CA", q, p, upc="088352113451")
    for s, q, p in (("2026-05-18", 2, 298.0), ("2026-06-15", 2, 298.0), ("2026-07-13", 2, 276.0),
                    ("2026-08-17", 2, 298.0), ("2026-09-14", 2, 298.0)):
        b.line(PRAIRIE, _d(s), "PW10034", "JAMESON IRISH WHISKEY 80 1.0L", "CA", q, p, upc="080432400432")
    # A true decrease (Prairie).
    for s, p in (("2026-05-11", 336.0), ("2026-06-08", 336.0), ("2026-07-06", 336.0), ("2026-08-10", 318.0),
                 ("2026-09-07", 318.0), ("2026-10-05", 318.0)):
        b.line(PRAIRIE, _d(s), "PW10057", "CROWN ROYAL WHSKY 80 1.0L", "CA", 2, p, upc="087000007239")
    # Free goods beside a deal: 4 paid at $230 and 1 free on one invoice; and
    # a product only ever sent free (no price to value it at).
    for s in ("2026-06-08", "2026-07-13", "2026-08-17"):
        b.line(PRAIRIE, _d(s), "PW10088", "BACARDI RUM SUPERIOR WHITE 80 1.0L", "CA", 1, 230.0, upc="080480015305")
    inv = b.line(PRAIRIE, _d("2026-09-14"), "PW10088", "BACARDI RUM SUPERIOR WHITE 80 1.0L", "CA", 4, 230.0,
                 upc="080480015305")
    b.line(PRAIRIE, _d("2026-09-14"), "PW10088", "BACARDI RUM SUPERIOR WHITE 80 1.0L", "CA", 1, 0.0,
           upc="080480015305", kind="charge", number=inv)
    for s in ("2026-06-01", "2026-08-03", "2026-09-28"):
        b.line(PRAIRIE, _d(s), "PW10096", "GREY GOOSE VODKA 80 1.0L", "CA", 1, 0.0, upc="080480280017")
    # A credit memo naming its invoice: billed 2 at $312, a memo of -2 at
    # $347 against the 8/10 invoice: $70 back.
    b.line(PRAIRIE, _d("2026-07-06"), "PW10112", "BULLEIT BRBN 90 1.0L", "CA", 2, 312.0, upc="087000005518")
    ref = b.line(PRAIRIE, _d("2026-08-10"), "PW10112", "BULLEIT BRBN 90 1.0L", "CA", 2, 312.0, upc="087000005518")
    memo = b.line(PRAIRIE, _d("2026-08-12"), "PW10112", "BULLEIT BRBN 90 1.0L", "CA", -2, 347.0,
                  upc="087000005518", number="P0990417")
    MEMO_REFS[memo] = ref

    # An alternating deal (North Branch): $168 its price, $152 the deal.
    # The order sizes do not follow the price (1 and 2 cases at each), so it
    # is a deal that comes and goes, never a ladder.
    for s, q, p in (("2026-05-12", 1, 168.0), ("2026-05-26", 1, 152.0), ("2026-06-23", 2, 152.0),
                    ("2026-08-18", 2, 168.0), ("2026-09-29", 1, 152.0), ("2026-10-13", 1, 168.0)):
        b.line(NORTH, _d(s), "NB97846", "SON CUTRER CHARD RR RNCH 24 750ML", "CA", q, p, upc="087000141513")
    # One purchase at a new price: unconfirmed.
    for s, p in (("2026-06-09", 120.0), ("2026-07-07", 120.0), ("2026-08-04", 120.0), ("2026-09-01", 120.0),
                 ("2026-09-29", 128.0)):
        b.line(NORTH, _d(s), "NB60411", "TORRESELLA P GRIGIO 12 750ML", "CA", 1, p, upc="089744100253")
    # Case and bottle buys of one product.
    for s in ("2026-06-16", "2026-07-21", "2026-08-25"):
        b.line(NORTH, _d(s), "NB21733", "WOODFORD RSV BRBN 6B 1L", "CA", 1, 261.0, upc="081128001483")
    for s in ("2026-09-08", "2026-09-22", "2026-10-06"):
        b.line(NORTH, _d(s), "NB21733", "WOODFORD RSV BRBN 6B 1L", "BO", 1, 58.8, upc="081128001483")
    for s in ("2026-06-23", "2026-07-28", "2026-08-25"):
        b.line(NORTH, _d(s), "NB31406", "FIREBALL CINN WHSKY 1L", "CA", 1, 216.0, upc="088004144678")
    for s in ("2026-09-15", "2026-10-13"):
        b.line(NORTH, _d(s), "NB31406", "FIREBALL CINN WHSKY 1L", "BO", 3, 21.25, upc="088004144678")
    for s in ("2026-06-30", "2026-08-04", "2026-09-01"):
        b.line(NORTH, _d(s), "NB05300", "DON JULIO TEQ BL 6B 1L", "CA", 2, 270.0, upc="088076189317")
    for s in ("2026-09-22", "2026-10-06"):
        b.line(NORTH, _d(s), "NB05300", "DON JULIO TEQ BL 6B 1L", "BO", 2, 52.5, upc="088076189317")

    # A quantity ladder (Wolf Point): 2 cases at $262, 4 at $249 — the
    # order size, never a price change.
    for i, d in enumerate(_weekdays(WOLFPOINT)):
        if d < _d("2026-06-17") or i % 2:
            continue
        q = 2 if (i // 2) % 2 == 0 else 4
        b.line(WOLFPOINT, d, "WP08508", "TITOS VDK HANDMADE 1L", "CA", q, 262.0 if q == 2 else 249.0,
               upc="619947000020")

    # Kegs with the deposit in their cost, empties back (Lakeshore); a true
    # decrease on one of them.
    for i, d in enumerate(_weekdays(LAKESHORE)):
        b.keg(LAKESHORE, d, "LB01411", "STELLA ARTOIS 1/2 BBL", 1, 205.0 if d < _d("2026-08-12") else 195.0,
              upc="018200530449")
        if i % 2 == 0:
            b.keg(LAKESHORE, d, "LB04111", "DOS EQUIS LAGER 1/2 BBL", 1, 176.0, upc="00723112040022")
        if i % 3 == 1:
            b.line(LAKESHORE, d, "LB99025", "EMPTY $30 KEG", "DS", -2, 0.0, cost=-60.0)

    # A true increase, the only one (Calumet), and empties credited on their
    # own line kind.
    for d in _weekdays(CALUMET):
        if d.toordinal() % 14 < 7:
            continue
        b.line(CALUMET, d, "CB69311", "WHITE CLAW BLK CHRY 24/12 CAN", "CA", 2,
               33.75 if d < _d("2026-08-13") else 36.25, upc="635985800519")
    for s, n in (("2026-06-11", -3), ("2026-07-16", -4), ("2026-08-20", -2), ("2026-09-24", -3)):
        b.line(CALUMET, _d(s), "CB00121", "POS EMPTY KEG $30 MISCELLANEOUS", "DS", n, 0.0, kind="keg_credit",
               cost=round(30.0 * n, 2))
    for s in ("2026-06-04", "2026-07-09", "2026-08-13", "2026-09-17"):
        b.keg(CALUMET, _d(s), "CB13682", "HALF ACRE DAISY CUTTER K 1/2BBL", 3, 199.0)

    # A credit rebill on one invoice (Fox River): 4 kegs billed at $188 are
    # credited and rebilled at $168 — $80, not a price change.
    for d in _weekdays(FOXRIVER):
        if d.toordinal() % 14 >= 7:
            continue
        b.keg(FOXRIVER, d, "FR21201", "MODELO ESP 1/2 BBL", 1, 188.0, upc="080660956435")
    rebill = _d("2026-10-08")
    b.keg(FOXRIVER, rebill, "FR21201", "MODELO ESP 1/2 BBL", 4, 188.0, upc="080660956435", number="FR238811")
    b.line(FOXRIVER, rebill, "FR21201", "MODELO ESP 1/2 BBL", "KE", -4, 188.0, upc="080660956435",
           number="FR238818")
    b.line(FOXRIVER, rebill, "FR21201", "MODELO ESP 1/2 BBL", "KE", 4, 168.0, upc="080660956435",
           number="FR238818")
    # A true decrease on a mixer.
    for d in _weekdays(FOXRIVER):
        b.line(FOXRIVER, d, "FR90061", "RED BULL 8.4Z 24PK", "CA", 1, 42.5 if d < _d("2026-08-20") else 39.9,
               upc="611269991000")

    # A ladder across three sizes with a small real change on 7/21 (Midway),
    # empties back, and a product's own discount line.
    sizes = (1, 2, 3, 2)
    for i, d in enumerate(_weekdays(MIDWAY)):
        q = sizes[i % len(sizes)]
        before = d < _d("2026-07-21")
        price = {1: 130.0, 2: 120.0, 3: 114.0}[q] if before else {1: 128.0, 2: 118.0, 3: 112.0}[q]
        b.keg(MIDWAY, d, "MW50094", "ULTRA 1/2 BARREL", q, price, upc="018200003479")
        if i % 2:
            b.line(MIDWAY, d, "MW97094", "EMPTIES-AB BEER 1/2 BBL", "KE", -2, DEPOSIT)
    b.line(MIDWAY, _d("2026-09-15"), "MW88310", "MICHELOB ULTRA 2 PRODUCT DISCOUNT", "EA", 1, -80.0)

    # Monthly postings (Lake County): the price holds within a month and
    # moves between them; a keg's was-since; two small flips never listed.
    lite = {5: 124.0, 6: 126.0, 7: 124.0, 8: 124.0, 9: 126.0, 10: 122.0}
    coors = {5: 22.4, 6: 22.75, 7: 22.4, 8: 22.75, 9: 22.4, 10: 22.75}
    miller = {5: 21.6, 6: 21.95, 7: 21.6, 8: 21.95, 9: 21.6, 10: 21.95}
    for d in _weekdays(LAKECOUNTY):
        # October's new keg price is on one order so far: the month's
        # posting, read from a single purchase.
        if d < _d("2026-10-12"):
            b.keg(LAKECOUNTY, d, "LC57152", "LITE 1/2 BBL", 2, lite[d.month])
        b.line(LAKECOUNTY, d, "LC09215", "COORS LT 24NR LSE", "CA", 3, coors[d.month], upc="071990095154")
        b.line(LAKECOUNTY, d, "LC05721", "MILLER LITE 24NR LSE", "CA", 2, miller[d.month], upc="034100575717")

    # ── the steady week around them ────────────────────────────────────────
    steady = {
        PRAIRIE: [("PW20011", "KETEL ONE VDK 80 1.0L", "CA", 289.0), ("PW20027", "TANQUERAY GIN 94.6 1.0L", "CA", 254.0),
                  ("PW20035", "CAMPARI APERITIVO 48 1.0L", "CA", 318.0)],
        NORTH: [("NB40019", "CASAMIGOS TEQ BL 6B 750ML", "CA", 312.0), ("NB40025", "HENDRICKS GIN 6B 1L", "CA", 279.0),
                ("NB40033", "WHISPERING ANGEL ROSE 12 750ML", "CA", 216.0)],
        LAKESHORE: [("LB30212", "REVOLUTION ANTI HERO IPA 4/6/12 CAN", "CA", 36.0)],
        CALUMET: [("CB30144", "HIGH NOON CKTL PEACH LOOSE CAN 24", "CA", 44.0),
                  ("CB30188", "GOSLINGS GINGER BEER C24 12OZ 6P", "CA", 24.5)],
        FOXRIVER: [("FR30311", "CORONA EXTRA 24/12 NR LSE", "CA", 33.25)],
        MIDWAY: [("MW30422", "BUD LIGHT 24 BOTTLE 12OZ", "CA", 25.6), ("MW30431", "MICHELOB ULTRA 24NR LSE", "CA", 27.4)],
        LAKECOUNTY: [("LC30551", "YUENGLING LAGER 24NR LSE", "CA", 24.1)],
        WOLFPOINT: [("WP30611", "JIM BEAM BRBN 80 1.0L", "CA", 176.0)],
    }
    for house, items in steady.items():
        for i, d in enumerate(_weekdays(house)):
            for j, (code, desc, uom, price) in enumerate(items):
                if (i + j) % 3 == 2:
                    continue
                b.line(house, d, code, desc, uom, 1 + (i + j) % 3, price)
            fee = HOUSES[house]["fee"]
            if fee is not None:
                code, desc, upc, uom, qty, price, cost, kind = fee
                b.line(house, d, code, desc, uom, qty, price, upc=upc, kind=kind, cost=cost)

    rows = sorted(b.lines, key=lambda r: (r[2], r[0], r[1]))
    return rows


MEMO_REFS = {}


def main():
    MEMO_REFS.clear()
    rows = build()
    doc = {"source": "SYNTHETIC — scripts/make_bar_invoice_fixture.py. Invented distributors, invoice numbers, "
                     "item codes and prices around ordinary market brands; no customer's purchasing. 5/4-10/13/26.",
           "columns": COLUMNS, "ref_invoice_numbers": dict(sorted(MEMO_REFS.items())), "rows": rows}
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("{" + f'"source": {json.dumps(doc["source"])},\n "columns": {json.dumps(COLUMNS)},\n'
                 f' "ref_invoice_numbers": {json.dumps(doc["ref_invoice_numbers"])},\n "rows": [\n')
        fh.write(",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in rows))
        fh.write("\n ]\n}\n")
    print(f"{len(rows)} lines, {len({(r[0], r[1]) for r in rows})} invoices -> {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
