"""
bar/classify.py — sorting a bought product into a category: the description
words, the distributor houses that decide when no word does (a guess,
flagged for the owner), the product key, and the owner's correction
(`bar_product_categories`, created at boot by init_bar_cost) that holds
over any guess.
"""
import re
from datetime import datetime, timezone

from bar.common import CATEGORIES, get_conn
from fintech_invoices import distributor_key, item_key

# Description words, checked in this order (a keg is beer whatever else it
# says; a vodka seltzer is sold under the POS's Seltzer, inside Beer).
#
# THE keg pattern (BAR-CALC-10 / ING-16, 10/16/26), shared with the Fintech
# reader's deposit split (fintech_invoices._is_keg reads KEG_RE / is_keg):
# a fraction of a barrel in any spelling (1/2 BBL, 1/2BB, 1/6BL, 1/2 BL,
# 1/4 BARREL), BBL, KEG, a sixtel or pony, a litre keg (50L, 30L, 20L), K 1/2,
# or gallons. A bare BARREL is never a keg: "JAMESON BLACK BARREL" and
# "KNOB CREEK SINGLE BARREL" are whiskey — BARREL counts only with a fraction
# ("1/2 BARREL", "HALF BARREL") or with the keg unit (KE / KG), which is_keg
# reads from the line's unit.
KEG_RE = re.compile(r"(?:\b1/[246]\s*(?:BBL|BB|BL|BARREL|KEG)S?\b|\bHALF\s+BARREL\b|\bBBL\b|\bKEGS?\b|\bSIXTEL\b|"
                    r"\bPONY\s+KEG\b|(?<![\d.])\b(?:50|30|20)\s?L\b(?!\s*BOX)|\bK\s+1/[246]\b|\bK\s+50L\b|"
                    r"\b\d{1,2}(?:\.\d+)?\s*GAL(?:LONS?)?\b)", re.I)
_KEG_RE = KEG_RE


def is_keg(description, uom=None) -> bool:
    """A keg: the keg unit (KE / KG) or KEG_RE in the description. The one
    test the category (classify) and the deposit split share."""
    return str(uom or "").upper() in ("KE", "KG") or bool(KEG_RE.search(str(description or "")))


_FEE_RE = re.compile(r"\b(SERVICE\s*CHARGE|SALES\s*TAX|TAX|FUEL|DELIVERY|FREIGHT|SURCHARGE|DISCOUNT|REVERSAL|"
                     r"SUMMARY\s*LEVEL|ADJUSTMENT)\b")
_SUPPLY_RE = re.compile(r"\b(COASTERS?|GLASSWARE|GLASS|BUCKET|NAPKINS?|STRAWS?|PICKS?|SLEEVE|TAP\s*HANDLE|HANDLE|CO2|"
                        r"CUPS?|LIDS?|TOWELS?|OPENER)\b")
_SELTZER_RE = re.compile(r"\b(SELTZER|SLTZR|THC|RTD|SUN\s*CRUISER|HIGH\s*NOON|WHITE\s*CLAW|TRULY|NUTRL|SURFSIDE|CUTWATER|TWISTED\s*TEA|CIDER|LEMONADE\s+HARD)\b")
_NA_RE = re.compile(r"\b(RED\s*BULL|GINGER\s*BEER|PUREE|PSNFRT|SYRUP|OLIVE\s*BRINE|BRINE|PELLEGRINO|TOPO\s*CHICO|LA\s*CROIX|MIXER|MIX|JUICE|SODA|TONIC|WATER|COFFEE|ESPRESSO|"
                    r"GRENADINE|FEVER[\s-]*TREE|MONIN|BARRITTS|REAL\s+\w+|NA\b|N/A|NON[\s-]*ALC|ZERO\s*PROOF|COLD\s*BREW)")
_WINE_RE = re.compile(r"\b(CHARD|CHARDONNAY|CAB|CABERNET|CAB\s*SAUV|PINOT|P\s*GRIGIO|PROSECCO|CHAMP|CHAMPAGNE|BRUT|"
                      r"MERLOT|SAUV|SAUVIGNON|MOSCATO|RIESLING|MALBEC|ZIN|ZINFANDEL|RED\s*BLEND|WINE|CAVA|LAMBRUSCO|"
                      r"BURGUNDY|SHIRAZ|SYRAH|SANGIOVESE|CHIANTI|TEMPRANILLO|VINHO|NOIR|P\s*NOIR|CHENIN|BLANC|"
                      r"PETER\s*VELLA|SOUVERAIN|SON\s*CUTRER|AUSTIN|TORRESELLA|ZARDETTO|WYCLIFF|BOX\s*\d+L|\d\s*B\s+5L)\b")
_LIQUOR_RE = re.compile(r"\b(VODKA|VDK|TEQ|TEQUILA|WHSKY|WHISKY|WHISKEY|WHSK|BRBN|BOURBON|RUM|GIN|SCOTCH|LIQ|LIQUEUR|"
                        r"CORDIAL|SCHNAPPS|JAGERMEISTER|FIREBALL|TRIPLE\s*SEC|VERMOUTH|APEROL|CAMPARI|BAILEYS|KAHLUA|"
                        r"COGNAC|BRANDY|MEZCAL|AMARETTO|RYE|CROWN\s*ROYAL|TITOS|KETEL|GREY\s*GOOSE|JAMESON|CASAMIGOS|"
                        r"DON\s*JULIO|ST[\s.]*GERMAIN|ELDERFLOWER|CAZCANES|GLENFIDDICH|GLENLIVET|GLENMORANGIE|MACALLAN|"
                        r"BALVENIE|DEWARS|JOHNNIE|HENNESSY|PATRON|ESPOLON|BACARDI|MALIBU|CAPT\s*MORGAN|SKREWBALL|"
                        r"PIGGYBACK|WHISTLEPIG|BULLEIT|MAKERS|WOODFORD|ANGELS\s*ENVY|BUFFALO\s*TRACE|JACK\s*DANIEL|"
                        r"TANQUERAY|HENDRICKS|BOMBAY|BEEFEATER|SMIRNOFF|ABSOLUT|DEEP\s*EDDY|NEW\s*AMSTERDAM|"
                        r"KOVAL|RUMCHATA|HORNITOS|TEREMANA|LUNAZUL|1800|BBN|SCHNPS|CURACAO|LIMONCELLO|BITTERS?|VERM|ELDER|"
                        r"J\s*WALKER|JOHNNIE\s*WALKER|JACK\s*DANIELS?|HAYDENS?|JAMES\s*E\s*PEPPER|HENRY\s*AND\s*SONS|MCGILLS|DEKUYPER|"
                        r"PALLINI|CARPANO|BITTERMENS|KNOB\s*CREEK|SINGLE\s*BARREL|BARREL\s*PROOF|SMALL\s*BATCH|"
                        r"BLACK\s*BARREL|EAGLE\s*RARE|BLANTONS?|ELIJAH\s*CRAIG|FOUR\s*ROSES|WILD\s*TURKEY|"
                        r"JIM\s*BEAM|EVAN\s*WILLIAMS|OLD\s*FORESTER|BASIL\s*HAYDEN)\b")
_BEER_RE = re.compile(r"(\d+NR\b|\d+CN\b|\bMHL\b)|\b(NR|LN|12Z|16Z|19\.2Z|24Z|25Z|24/12|2/12|4/6|6/4|LAGER|IPA|STOUT|PILS|"
                      r"PILSNER|PORTER|LITE|ULTRA|MILLER|COORS|BUD|BUDWEISER|MICHELOB|MODELO|CORONA|STELLA|"
                      r"GUINNESS|HEINEKEN|BLUE\s*MOON|GOOSE\s*ISLAND|REVOLUTION|HALF\s*ACRE|3\s*FLOYDS|DAISY\s*CUTTER|"
                      r"KROMBACH|PACIFICO|YUENGLING|LAGUNITAS|BELLS|ANTI[\s-]*HERO|HAZY|WHEAT|HEFE|OKTOBERFEST)\b")
# Weak words (V28, 10/15/26): each says beer or wine only as a guess — a
# bottle, a can, an ale (ginger ale), light (Light rum), rosé or sparkling
# (sparkling water) — so a product sorted by one of them alone reaches the
# owner's "to check" list instead of passing as sure.
_WEAK_BEER_RE = re.compile(r"\b(BTL|BTLS|BOTTLES?|CAN|CANS|ALE|LIGHT)\b")
_WEAK_WINE_RE = re.compile(r"\b(ROSE|ROSÉ|SPARKLING)\b")
# Non-alcoholic words that win over every alcohol word (TOPO CHICO SPARKLING
# WATER was wine, CANADA DRY GINGER ALE beer, HEINEKEN 0.0 beer) unless the
# line also says it is spiked.
_NA_FIRST_RE = re.compile(r"(\b0\.0\b|0\.0%|\bNA\s+BEER\b|\bN/A\b|\bNON[\s-]*ALC\w*|\bALCOHOL[\s-]*FREE\b|"
                          r"\bZERO\s*PROOF\b|\bSPARKLING\s+WATER\b|\bMINERAL\s+WATER\b|\bGINGER\s+ALE\b|"
                          r"\bGINGER\s+BEER\b|\bCLUB\s+SODA\b|\bTONIC\s+WATER\b|\bTOPO\s*CHICO\b|\bPELLEGRINO\b|"
                          r"\bLA\s*CROIX\b|\bATHLETIC\s+BREW\w*|\bRED\s*BULL\b)")
_SPIKED_RE = re.compile(r"\b(HARD|SPIKED|SELTZER|SLTZR|VODKA|VDK|TEQUILA|TEQ|RUM|GIN|WHISKEY|WHISKY|COCKTAIL|CKTL)\b")
# A keg deposit or an empty coming back is a deposit, never beer (V28 / V4).
_DEPOSIT_RE = re.compile(r"\b(KEG\s+DEPOSIT|DEPOSIT|EMPTY|EMPTIES|EMPTYS)\b")
# Bottled in bond is a whiskey word, not a bottle of beer.
_BOND_RE = re.compile(r"\b(BTL|BOTTLED)\s+IN\s+BOND\b|\bBIB\b")
# A product's own discount or rebate (Superior's "AB 2 PRODUCT DISCOUNT",
# -$80): it lowers what that product's category cost (V33), never a fee.
_REBATE_RE = re.compile(r"\b(DISCOUNT|DISC|REBATE|ALLOWANCE|PROMO\s+CREDIT)\b")
_REBATE_NOT_RE = re.compile(r"\b(SUMMARY\s*LEVEL|INVOICE|PAYMENT|EARLY\s+PAY|CASH)\b")
# Cocktail inputs: non-alcoholic, but bought for drinks the POS sells as
# liquor (Decision 3, 10/15/26). They stay non-alcoholic; the Bar shows
# "mixers bought" beside liquor, and the owner can count them as liquor.
_MIXER_RE = re.compile(r"\b(RED\s*BULL|GINGER\s*BEER|PUREE|PSNFRT|SYRUP|OLIVE\s*BRINE|BRINE|MIXER|MIX|JUICE|"
                       r"SODA|TONIC|GRENADINE|FEVER[\s-]*TREE|MONIN|BARRITTS|REAL\s+\w+|COLD\s*BREW|ESPRESSO|"
                       r"CONCENTRATE|SOUR|LIME\s*JUICE|BLOODY|MARGARITA|CHERRIES|OLIVES)\b")


# Distributors whose book is mostly beer, mostly spirits, or mostly wine:
# the fallback when no word decides (a guess, flagged for the owner).
_BEER_HOUSES = ("CHICAGO BEVERAGE", "WINDY CITY", "EUCLID", "ELGIN", "SUPERIOR", "SCHAMBERGER", "LAKESHORE",
                "LAKE SHORE", "RELAX")
_SPIRIT_HOUSES = ("SOUTHERN GLAZER", "BREAKTHRU", "RNDC", "REPUBLIC NATIONAL")
_WINE_HOUSES = ("WINEBOW", "LIBATION", "TOUTON", "ROYAL WINE")
_NA_HOUSES = ("COFFEE", "ROASTERS")


def init_bar_cost(db_path=None):
    """The owner's corrections to how a product is sorted. Boot only."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_product_categories (
            restaurant_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            category TEXT NOT NULL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, product_key))""")
        migrate_product_keys(conn)
        conn.commit()
    finally:
        conn.close()
    # The owner's pack, link, deal-sheet, drink-map and pour settings
    # (bar.products, 10/15/26), created at boot beside this one.
    from bar.products import init_bar_products
    init_bar_products(db_path)


def product_key(distributor, item_code, description) -> str:
    """One product: the distributor (its canonical name — "Breakthru
    Beverage Illinois, LLC" and "…Illinois LLC" are one) and its own item
    code without the padding ('000009031' and '9031' are one product), else
    its description."""
    code = item_key(item_code)
    return f"{distributor_key(distributor)}|{code or str(description or '').strip().lower()}"


def _rekey(old) -> str:
    """An owner correction's key as product_key writes it today. The part
    after '|' was the raw item code (no spaces) or the lowercased
    description; only a code loses its padding."""
    dist, sep, rest = str(old).partition("|")
    if not sep:
        return old
    if rest and not re.search(r"\s", rest):
        rest = item_key(rest) or rest
    return f"{distributor_key(dist)}|{rest}"


def migrate_product_keys(conn) -> int:
    """Move the owner's corrections to today's product keys (bar fix round
    10/15/26, V14/R3: leading zeros out of item codes, canonical distributor
    names). Idempotent: a key already in today's form maps to itself. Where
    two old keys become one, the later correction holds. Runs at boot on
    init_bar_cost's connection (the table is small: one row per corrected
    product)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT restaurant_id, product_key, category, set_by, set_at FROM bar_product_categories").fetchall()]
    moved = 0
    by_new = {}
    for r in rows:
        by_new.setdefault((r["restaurant_id"], _rekey(r["product_key"])), []).append(r)
    for (rid, new), group in by_new.items():
        if len(group) == 1 and group[0]["product_key"] == new:
            continue
        keep = max(group, key=lambda r: (str(r["set_at"] or ""), r["product_key"] == new))
        for r in group:
            conn.execute("DELETE FROM bar_product_categories WHERE restaurant_id=? AND product_key=?",
                         (rid, r["product_key"]))
        conn.execute("INSERT INTO bar_product_categories (restaurant_id, product_key, category, set_by, set_at) "
                     "VALUES (?,?,?,?,?)", (rid, new, keep["category"], keep["set_by"], keep["set_at"]))
        moved += len(group)
    return moved


def is_rebate(description, kind="item") -> bool:
    """A product's own discount or rebate line (an item line saying DISCOUNT
    or REBATE about a product, not an invoice-level summary): it lowers that
    product's category cost (V33). A fee or adjustment line stays a fee."""
    if str(kind or "item") not in ("item", "charge"):
        return False
    d = " " + re.sub(r"\s+", " ", str(description or "").upper()) + " "
    return bool(_REBATE_RE.search(d)) and not _REBATE_NOT_RE.search(d)


def is_mixer(description) -> bool:
    """A cocktail input (Red Bull, purée, syrup, olive brine, cold brew
    concentrate, juice, tonic): non-alcoholic, bought for drinks sold as
    liquor (Decision 3)."""
    d = " " + re.sub(r"\s+", " ", str(description or "").upper()) + " "
    return bool(_MIXER_RE.search(d))


def _by_words(d, dist) -> tuple:
    """(category, confident) from the description's words alone, or None.

    A beer's pack and container words (24NR, 4/6, 12Z, a can count) and its
    style words (STOUT, LAGER, IPA, a brewery) beat a single spirit or wine
    word, and when both fire the sort is a guess the owner confirms
    (BAR-CALC-10 / ING-16, 10/16/26: "GOOSE ISLAND BOURBON COUNTY STOUT" was
    liquor and "KRONENBOURG 1664 BLANC 4/6 12Z NR" wine, both sure)."""
    if _SELTZER_RE.search(d):
        return "beer", True
    if _BOND_RE.search(d):
        return "liquor", True
    liquor, wine, beer = bool(_LIQUOR_RE.search(d)), bool(_WINE_RE.search(d)), bool(_BEER_RE.search(d))
    if beer and (liquor or wine):
        return "beer", False
    if liquor:
        return "liquor", True
    if wine:
        return "wine", True
    if _NA_RE.search(d):
        return "na", True
    if beer:
        return "beer", True
    # A weak word alone is a guess the owner confirms (V28).
    if _WEAK_WINE_RE.search(d):
        return "wine", False
    if _WEAK_BEER_RE.search(d):
        return "beer", False
    return None


def _by_house(dist) -> tuple:
    if any(h in dist for h in _BEER_HOUSES):
        return "beer", False
    if any(h in dist for h in _SPIRIT_HOUSES):
        return "liquor", False
    if any(h in dist for h in _WINE_HOUSES):
        return "wine", False
    return None


def classify(distributor, description, uom=None, kind="item") -> tuple:
    """(category, confident) for one invoice line's product.

    Order (V28, 10/15/26): fees and deposits first; a product's own discount
    or rebate takes that product's category (V33); supplies; non-alcoholic
    words that beat every alcohol word (sparkling water, ginger ale, 0.0, NA
    beer) unless the line is spiked; kegs; then the description's words, a
    weak word alone (BTL, CAN, ALE, LIGHT, ROSE, SPARKLING) only a guess;
    then the distributor's house, a guess."""
    k = str(kind or "item")
    u = str(uom or "").upper()
    if k in ("fee", "adjustment"):
        return "fee", True
    d = " " + re.sub(r"\s+", " ", str(description or "").upper()) + " "
    dist = str(distributor or "").upper()
    if k == "keg_credit" or u == "DS" or _DEPOSIT_RE.search(d):
        return "deposit", True
    if is_rebate(description, k):
        rest = _REBATE_RE.sub(" ", d)
        # "AB 2 PRODUCT DISCOUNT": no product word, the house decides (a guess).
        return _by_words(rest, dist) or _by_house(dist) or ("fee", False)
    if _FEE_RE.search(d):
        return "fee", True
    if _SUPPLY_RE.search(d) and not _LIQUOR_RE.search(d):
        return "supplies", True
    if any(h in dist for h in _NA_HOUSES):
        return "na", True
    if _NA_FIRST_RE.search(d) and not _SPIKED_RE.search(d):
        return "na", True
    if u in ("KE", "KG") or _KEG_RE.search(d):
        return "beer", True
    return _by_words(d, dist) or _by_house(dist) or ("other", False)


def set_category(restaurant_id, key, category, user_id=None, db_path=None) -> dict:
    """The owner's word on one product's category; '' clears it back to the guess."""
    category = str(category or "").strip().lower()
    if category and category not in CATEGORIES:
        raise ValueError("Pick a category from the list.")
    conn = get_conn(db_path)
    try:
        if not category:
            conn.execute("DELETE FROM bar_product_categories WHERE restaurant_id=? AND product_key=?",
                         (int(restaurant_id), str(key)))
        else:
            conn.execute("INSERT INTO bar_product_categories (restaurant_id, product_key, category, set_by, set_at) "
                         "VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, product_key) DO UPDATE SET "
                         "category=excluded.category, set_by=excluded.set_by, set_at=excluded.set_at",
                         (int(restaurant_id), str(key), category, user_id,
                          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "product_key": key, "category": category or None}
