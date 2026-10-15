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

# Description words, checked in this order (a keg is beer whatever else it
# says; a vodka seltzer is sold under the POS's Seltzer, inside Beer).
_KEG_RE = re.compile(r"\b(1/2\s*BB|1/2BBL|1/2\s*BARREL|1/4\s*BB|1/6\s*BB|1/6BBL|BBL|BARREL|KEG|50L|30L|20L|K\s+1/2|K\s+50L|SIXTEL)\b")
_FEE_RE = re.compile(r"\b(SERVICE\s*CHARGE|SALES\s*TAX|TAX|FUEL|DELIVERY|FREIGHT|SURCHARGE|DISCOUNT|REVERSAL|"
                     r"SUMMARY\s*LEVEL|ADJUSTMENT)\b")
_SUPPLY_RE = re.compile(r"\b(COASTERS?|GLASSWARE|GLASS|BUCKET|NAPKINS?|STRAWS?|PICKS?|SLEEVE|TAP\s*HANDLE|CO2|"
                        r"CUPS?|LIDS?|TOWELS?|OPENER)\b")
_SELTZER_RE = re.compile(r"\b(SELTZER|SLTZR|THC|RTD|SUN\s*CRUISER|HIGH\s*NOON|WHITE\s*CLAW|TRULY|NUTRL|SURFSIDE|CUTWATER|TWISTED\s*TEA|CIDER|LEMONADE\s+HARD)\b")
_NA_RE = re.compile(r"\b(RED\s*BULL|GINGER\s*BEER|PUREE|PSNFRT|SYRUP|OLIVE\s*BRINE|BRINE|PELLEGRINO|TOPO\s*CHICO|LA\s*CROIX|MIXER|MIX|JUICE|SODA|TONIC|WATER|COFFEE|ESPRESSO|"
                    r"GRENADINE|FEVER[\s-]*TREE|MONIN|BARRITTS|REAL\s+\w+|NA\b|N/A|NON[\s-]*ALC|ZERO\s*PROOF|COLD\s*BREW)")
_WINE_RE = re.compile(r"\b(CHARD|CHARDONNAY|CAB|CABERNET|CAB\s*SAUV|PINOT|P\s*GRIGIO|PROSECCO|CHAMP|CHAMPAGNE|BRUT|ROSE|"
                      r"MERLOT|SAUV|SAUVIGNON|MOSCATO|RIESLING|MALBEC|ZIN|ZINFANDEL|RED\s*BLEND|WINE|CAVA|LAMBRUSCO|"
                      r"BURGUNDY|SHIRAZ|SYRAH|SANGIOVESE|CHIANTI|TEMPRANILLO|VINHO|SPARKLING|NOIR|P\s*NOIR|CHENIN|BLANC|"
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
                        r"PALLINI|CARPANO|BITTERMENS)\b")
_BEER_RE = re.compile(r"(\d+NR\b|\d+CN\b|\bMHL\b)|\b(BTL|BTLS|CAN|CANS|NR|LN|12Z|16Z|19\.2Z|24Z|25Z|24/12|2/12|4/6|6/4|LAGER|IPA|ALE|STOUT|PILS|"
                      r"PILSNER|PORTER|LITE|LIGHT|ULTRA|MILLER|COORS|BUD|BUDWEISER|MICHELOB|MODELO|CORONA|STELLA|"
                      r"GUINNESS|HEINEKEN|BLUE\s*MOON|GOOSE\s*ISLAND|REVOLUTION|HALF\s*ACRE|3\s*FLOYDS|DAISY\s*CUTTER|"
                      r"KROMBACH|PACIFICO|YUENGLING|LAGUNITAS|BELLS|ANTI[\s-]*HERO|HAZY|WHEAT|HEFE|OKTOBERFEST)\b")
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
        conn.commit()
    finally:
        conn.close()


def product_key(distributor, item_code, description) -> str:
    """One product: the distributor's own item code, else its description."""
    code = str(item_code or "").strip()
    return f"{str(distributor or '').strip().lower()}|{code or str(description or '').strip().lower()}"


def classify(distributor, description, uom=None, kind="item") -> tuple:
    """(category, confident) for one invoice line's product."""
    k = str(kind or "item")
    u = str(uom or "").upper()
    if k in ("fee", "adjustment"):
        return "fee", True
    if k == "keg_credit" or u == "DS":
        return "deposit", True
    d = " " + re.sub(r"\s+", " ", str(description or "").upper()) + " "
    dist = str(distributor or "").upper()
    if _FEE_RE.search(d):
        return "fee", True
    if _SUPPLY_RE.search(d) and not _LIQUOR_RE.search(d):
        return "supplies", True
    if any(h in dist for h in _NA_HOUSES):
        return "na", True
    if u in ("KE", "KG") or _KEG_RE.search(d):
        return "beer", True
    if _SELTZER_RE.search(d):
        return "beer", True
    if _LIQUOR_RE.search(d):
        return "liquor", True
    if _WINE_RE.search(d):
        return "wine", True
    if _NA_RE.search(d):
        return "na", True
    if _BEER_RE.search(d):
        return "beer", True
    if any(h in dist for h in _BEER_HOUSES):
        return "beer", False
    if any(h in dist for h in _SPIRIT_HOUSES):
        return "liquor", False
    if any(h in dist for h in _WINE_HOUSES):
        return "wine", False
    return "other", False


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
