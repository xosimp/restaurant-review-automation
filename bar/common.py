"""
bar/common.py — what every part of the Bar page shares: the categories and
their labels, the windows and limits the reads use, the call-time get_conn,
and the small formatters (M/D/YY dates, whole dollars, readable product
names). No reads of its own beyond get_conn.
"""
import functools
import re
from datetime import date

import models as _models

CATEGORIES = ("beer", "liquor", "wine", "na", "supplies", "fee", "deposit", "other")
LABELS = {"beer": "Beer & seltzer", "liquor": "Liquor", "wine": "Wine", "na": "Non-alcoholic",
          "supplies": "Bar supplies", "fee": "Fees & taxes", "deposit": "Keg deposits & credits",
          "other": "Not sorted yet"}
# The categories pour cost is read for (each one's rule-of-thumb range is
# bar.pour_cost.RULE_OF_THUMB).
POUR_CATEGORIES = ("beer", "liquor", "wine")
WINDOW_DAYS = 28
PRICE_LOOKBACK_DAYS = 120
# The four-week pour cost read week by week: at most this many weekly points.
TREND_POINTS = 12
# A four-week reading this far from the one four weeks earlier is a change
# worth saying (purchase-based pour cost moves a point or so on its own).
TREND_SAY_POINTS = 1.5
# How many insights the page leads with.
MAX_INSIGHTS = 4

_UOM_WORD = {"CA": "case", "CS": "case", "KE": "keg", "KG": "keg", "BO": "bottle", "BL": "bottle", "BT": "bottle",
             "EA": "each", "DS": "deposit"}


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


def _mdy(iso) -> str:
    """M/D/YY from an ISO date (or a datetime's string), "" when unreadable.
    Kept per day: a build formats the same few hundred days ~50k times."""
    return _mdy10(str(iso)[:10])


@functools.lru_cache(maxsize=4096)
def _mdy10(iso) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return ""
    return f"{d.month}/{d.day}/{str(d.year)[2:]}"


def display_name(description) -> str:
    """A distributor's all-caps description as an owner reads it: shorthand
    spelled out (VDK → Vodka, N WHSTL → Noon Whistle), pack, case and proof
    codes dropped (24NR, 6B, 80), the size kept (1L, 750ml, ½ barrel). Kept
    per description (the Bar asks for the same names tens of thousands of
    times a build)."""
    return _display_name(str(description or ""))


# ── names an owner reads (owner, 10/16/26: "owners should be able to look at
# a drink name and immediately tell what it is") ─────────────────────────────
# A distributor's description ("TITOS VDK HANDMADE 1L", "N WHSTL GUMMYPALOOZA
# 1/2 BBL", "CH SOUVERAIN SAUV BL CALIF 750ML") with its shorthand spelled
# out, its pack and proof codes dropped and its size kept. No accents: the
# matchers read these words as plain letters (a "Chateau" must still meet the
# POS's "Gl Chateau SB").
# Phrases a distributor shortens, read on the upper-cased description.
_PHRASES = [
    (r"\bN WHSTL\b", "NOON WHISTLE"), (r"\bANG ORCH\b", "ANGRY ORCHARD"), (r"\bSA OCTFST\b", "SAM ADAMS OKTOBERFEST"),
    (r"\bS NEV\b", "SIERRA NEVADA"), (r"^MHL\b", "MILLER HIGH LIFE"), (r"^BM\b", "BLUE MOON"), (r"^LITE\b", "MILLER LITE"),
    (r"^ULTRA\b", "MICHELOB ULTRA"), (r"^REV\b", "REVOLUTION"), (r"\bANTI HERO\b", "ANTI-HERO"),
    (r"\bCAPT MORGAN\b", "CAPTAIN MORGAN"), (r"\bJ WALKER\b", "JOHNNIE WALKER"), (r"\bDR MCGILLS\b", "DR MCGILLICUDDYS"),
    (r"^CH SOUVERAIN\b", "CHATEAU SOUVERAIN"), (r"^CH ESCLANS\b", "CHATEAU D'ESCLANS"), (r"\bWHISPER ANGEL\b", "WHISPERING ANGEL"),
    (r"\bSAUV BL\b", "SAUVIGNON BLANC"), (r"\bCAB SAUV\b", "CABERNET SAUVIGNON"), (r"\bP NOIR\b", "PINOT NOIR"),
    (r"\bP GRIGIO\b", "PINOT GRIGIO"), (r"\bTEQ BL\b", "TEQUILA BLANCO"), (r"\bTEQ REP\b", "TEQUILA REPOSADO"),
    (r"\bCOR PREMIER\b", "CORONA PREMIER"), (r"\bCORONA EX\b", "CORONA EXTRA"), (r"\bMODELO ESP\b", "MODELO ESPECIAL"),
    (r"\bSON CUTRER\b", "SONOMA-CUTRER"), (r"\bRR RNCH\b", "RUSSIAN RIVER RANCHES"), (r"^CLICQUOT\b", "VEUVE CLICQUOT"),
    (r"\bBROWN BOX\b", ""), (r"\bBAR PK\b", ""), (r"\bART BX \d+\b", ""), (r"\bNON IBC\b", ""), (r"^POS\b", ""),
    (r"\bJ HENRY AND SONS\b", "J. HENRY & SONS"), (r"\bJAMES E PEPPER\b", "JAMES E. PEPPER"),
    (r"\bK (\d+(?:\.\d+)?L)\b", r"\1 KEG"), (r"\bK (?=1/)", ""),
    (r"\b1/2 ?(?:BBL|BL|BARREL)\b", "½ BARREL"), (r"\b1/6 ?(?:BBL|BL|BARREL)\b", "⅙ BARREL"), (r"\b1/4 ?(?:BBL|BL|BARREL)\b", "¼ BARREL"),
    (r"(BARREL) \d+(?:\.\d+)?(?:GAL)?\b", r"\1"), (r"\(\d+(?:\.\d+)?G\)", ""), (r"\b\d+(?:\.\d+)?GAL\b", ""),
    (r"\b\d+[-/](\d+(?:\.\d+)?(?:ML|L|OZ|Z))\b", r"\1"), (r"\b\d+/\d+/(\d+(?:\.\d+)?)\b", r"\1OZ"),
    (r"\bKROMBACH BRAU\b", "KROMBACHER"), (r"^PABST\b", "PABST BLUE RIBBON"), (r"^OBCM ", ""), (r"\bEX DRY\b", "EXTRA DRY"),
    (r"\bCA (\d+(?:\.\d+)?ML)\b", r"CALIFORNIA \1"), (r"\bCOORS 6NR STUBBY\b", "COORS STUBBY"),
    (r"\b(\d+)OZ-\d+PKS?\b", r"\1OZ"), (r"\b(\d+)OZ-\d+P\b", r"\1OZ"), (r"\b\d+-(\d+)OZ\b", r"\1OZ"),
]
_PHRASES = [(re.compile(a), b) for a, b in _PHRASES]
# Pack and case codes: how it ships, not what it is.
_PACK = re.compile(r"^(?:\d*NR|LSE|HG|LP|\d+B|\d+P|\d*PK|C\d+|B\d+|\d+(?:/\d+)+(?:PK)?|\d+CS|CS|PET|LOOSE|BOX|NL|IBC|BX|"
                   r"\d+PF|US|RTD|BOTTLE|BOTTLES|CAN|CANS|\d*CN)$")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)(ML|M|L|Z|OZ|MG)$")
_WORDS = {"VDK": "Vodka", "VDKA": "Vodka", "BBN": "Bourbon", "BRBN": "Bourbon", "WHSKY": "Whiskey", "WSKY": "Whiskey",
          "TEQ": "Tequila", "REP": "Reposado", "LIQ": "Liqueur", "SCHNPS": "Schnapps", "VERM": "Vermouth",
          "CHARD": "Chardonnay", "CALIF": "California", "MARLB": "Marlborough", "CHAMP": "Champagne", "CRM": "Cream",
          "CINN": "Cinnamon", "APL": "Apple", "ORNG": "Orange", "CHRY": "Cherry", "BLK": "Black", "LT": "Light",
          "CKTL": "Cocktail", "WTRMLN": "Watermelon", "STRWB": "Strawberry", "RASPB": "Raspberry", "PSNFRT": "Passion Fruit",
          "GRPFRT": "Grapefruit", "SLTZR": "Seltzer", "CHOC": "Chocolate", "CUCUM": "Cucumber", "HABAN": "Habanero",
          "LNDN": "London", "SLVR": "Silver", "RSV": "Reserve", "SHRY": "Sherry", "LBL": "Label", "WT": "White",
          "LEMO": "Lemonade", "OCTFST": "Oktoberfest", "WHSTL": "Whistle", "ORG": "Organic", "BRL": "Barrel",
          "ESP": "Especial", "NA": "Non-Alcoholic", "SEL": "Selection", "CPLR": "Coupler",
          "FAUCT": "Faucet", "FVR": "Flavor", "BLD": "Blend", "DR": "Dr.", "CLASS": "Classico", "BTL": "Bottle",
          "BY": "by", "AB": "AB", "GREENS": "Green's", "EX": "Extra",
          "ST": "St.", "AND": "and", "OF": "of", "IN": "in", "THE": "The", "OCG": "Orchard", "J": "J.",
          "TITOS": "Tito's", "MAKERS": "Maker's", "ANGELS": "Angel's", "HAYDENS": "Hayden's", "BUCHANANS": "Buchanan's",
          "HENDRICKS": "Hendrick's", "DANIELS": "Daniel's", "BELLS": "Bell's", "REGANS": "Regan's", "GOSLINGS": "Gosling's",
          "MCGILLICUDDYS": "McGillicuddy's", "BARRITTS": "Barritt's", "D'ESCLANS": "d'Esclans", "DEKUYPER": "DeKuyper",
          "RUMCHATA": "RumChata", "WHISTLEPIG": "WhistlePig", "CIDERBOYS": "Ciderboys",
          "SELTZ-UP": "Seltz-Up", "IPA": "IPA", "THC": "THC", "G4": "G4"}


_SPIRITS = {"BBN", "BRBN", "BOURBON", "VDK", "VODKA", "WHSKY", "WHISKEY", "WSKY", "GIN", "RUM", "TEQ", "TEQUILA",
            "SCOTCH", "LIQ", "LIQUEUR", "APERITIVO", "BRL"}


def _size(m):
    n, u = m.group(1), m.group(2)
    n = n[:-2] if n.endswith(".0") else n
    return {"ML": n + "ml", "M": n + "ml", "L": n + "L", "Z": n + "oz", "OZ": n + "oz", "MG": n + "mg"}[u]


@functools.lru_cache(maxsize=8192)
def _display_name(description) -> str:
    s = " ".join(str(description or "").upper().split())
    for rx, rep in _PHRASES:
        s = rx.sub(rep, s)
    toks = [t for t in s.split() if not _PACK.match(t)]
    out = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        # A bare proof or vintage before the size ("VODKA 80 1.0L", "GRIGIO 25 750ML").
        if re.fullmatch(r"\d{2,3}(?:\.\d)?", t) and _SIZE.match(nxt) and 20 <= float(t) <= 200:
            continue
        if re.fullmatch(r"\d{2,3}(?:\.\d)?", t) and not nxt and 20 <= float(t) <= 200 and out:
            continue
        # A proof after the spirit word ("WHSKY 90 STRAIGHT RYE"), a case count before the size ("VODKA 6 1L").
        prev = toks[i - 1] if i else ""
        if re.fullmatch(r"\d{2,3}(?:\.\d)?", t) and prev in _SPIRITS and 40 <= float(t) <= 200:
            continue
        if re.fullmatch(r"\d", t) and _SIZE.match(nxt):
            continue
        m = _SIZE.match(t)
        if m:
            out.append(_size(m)); continue
        m = re.fullmatch(r"(\d+)YR", t)
        if m:
            out.append(m.group(1) + " Year"); continue
        if t in _WORDS:
            w = _WORDS[t]
            out.append(w[:1].upper() + w[1:] if not out and w[:1].islower() else w); continue
        if len(t) == 1 and t.isalpha() and i == len(toks) - 1:
            continue                     # a stray letter at the end ("LEMONADE C")
        if re.fullmatch(r"[A-Z][A-Z'\-]*", t):
            out.append("-".join(p[:1] + p[1:].lower() for p in t.split("-")))
        else:
            out.append(t)
    return " ".join(out)


# A POS button's name as an owner reads it (owner, 10/16/26): the POS keeps
# its own short, often misspelled names; the page shows what each drink is.
# Display only — the POS name stays the key everywhere.
_POS_FIX = [
    (r"[^\x20-\x7E]+\??$", ""),                                   # encoding debris ("AleÀÀ?")
    (r"`\s*bucket$", " (bucket)"), (r"`\s*liquor add-on$", " (liquor add-on)"), (r"`\s*game day spcl$", " (game day special)"),
    (r"^gl\s+(.+)$", r"\1 (glass)"), (r"^btl\s+(.+)$", r"\1 (bottle)"),
    (r"\bchataeu\b", "Chateau"), (r"\bwhitehave\b", "Whitehaven"), (r"\belouon\b", "Elouan"), (r"\bvueve yellow\b", "Veuve Clicquot Yellow Label"), (r"\bvueve\b", "Veuve"),
    (r"\bheniken\b", "Heineken"), (r"\bguiness\b", "Guinness"), (r"\bkrombocker\b", "Krombacher"), (r"\bspiritz\b", "Spritz"),
    (r"\blongisland\b", "Long Island"), (r"\bstrawbry\b", "Strawberry"), (r"\bmerlt\b", "Merlot"), (r"\brefreshr\b", "Refresher"),
    (r"\bwhispering ros?\b", "Whispering Angel Rose"), (r"\bmoet\b", "Moët"),
    (r"\bsb\b", "Sauvignon Blanc"), (r"\bpn\b", "Pinot Noir"), (r"\bpg\b", "Pinot Grigio"), (r"\bchard\b", "Chardonnay"),
    (r"\bcab\b", "Cabernet"), (r"\bangry orch\b", "Angry Orchard"), (r"\bold fash\b", "Old Fashioned"), (r"\brepo\b", "Reposado"),
    (r"\bcapt\b", "Captain"), (r"\bdr mcgil\b", "Dr. McGillicuddy's"), (r"^jw black\b", "Johnnie Walker Black"),
    (r"^wc\b", "White Claw"), (r"^gb\b", "Good Boy"), (r"^daly\b", "John Daly"), (r"\bwmellon\b", "Watermelon"),
    (r"^mich ultra\b", "Michelob Ultra"), (r"^mich\b", "Michelob"), (r"^jager bomb\b", "Jäger Bomb"), (r"^jager$", "Jägermeister"),
    (r"\bsam october\b", "Sam Adams Oktoberfest"), (r"\bdos xx\b", "Dos Equis"), (r"^pbr\b", "Pabst Blue Ribbon"),
    (r"\bspec rsv\b", "Special Reserve"), (r"^makers\b", "Maker's"), (r"^titos\b", "Tito's"), (r"^angels envy\b", "Angel's Envy"),
    (r"^hendricks\b", "Hendrick's"), (r"^dewars\b", "Dewar's"), (r"\b(\d+)\s*yr\b", r"\1 Year"),
    (r"^orin swift 8 year d$", "Orin Swift 8 Years in the Desert"), (r"\bacdonut\b", "Apple Cider Donut"),
    (r"^salt caramel tini$", "Salted Caramel Martini"), (r"^bo 1873 mule$", "Barmen 1873 Mule"), (r"\bgf\b", "Gluten-Free"),
    (r"^straw lem\b", "Strawberry Lemonade"), (r"^cider boys\b", "Ciderboys"), (r"^kettle one\b", "Ketel One"),
    (r"^rum chata\b", "RumChata"), (r"^basil toast$", "Basil Hayden's Toast"), (r"^basil hayden$", "Basil Hayden's"),
    (r"^old grandad$", "Old Grand-Dad"), (r"^wink$", "Wynk"), (r"^frose$", "Frosé"), (r"^whistle pig$", "WhistlePig"),
    (r"\bn/a\b", "Non-Alcoholic"), (r"\bna\b", "Non-Alcoholic"), (r"^jack$", "Jack Daniel's"), (r"^jameson black$", "Jameson Black Barrel"),
    (r"^fernet$", "Fernet-Branca"), (r"^woodford$", "Woodford Reserve"), (r"^amaro averna$", "Averna Amaro"),
    (r"^bombay$", "Bombay Sapphire"), (r"^\s*(\d+)\s*oz\s+(.+)$", r"\2 \1oz"), (r"\bmich ultra can\b", "Michelob Ultra (can)"),
]
_POS_FIX = [(re.compile(a, re.I), b) for a, b in _POS_FIX]
_KEEP = {"THC", "IPA", "N/A", "0.0"}


@functools.lru_cache(maxsize=8192)
def pos_display_name(name) -> str:
    s = " ".join(str(name or "").split())
    for rx, rep in _POS_FIX:
        s = rx.sub(rep, s)
    out = []
    for w in s.split(" "):
        if w.upper() in _KEEP:
            out.append(w.upper())
        elif re.fullmatch(r"[A-Z][A-Z'\-]{2,}", w):         # a shouted word ("MULE", "BEER"): title case
            out.append(w[:1] + w[1:].lower())
        elif w.lower() == "can)" or w.lower() == "(can)":
            out.append(w.lower())
        else:
            out.append(w)
    s = " ".join(out)
    return s[:1].upper() + s[1:] if s else s


def _money(v) -> str:
    n = round(abs(float(v or 0)))
    return ("-" if float(v or 0) < 0 else "") + f"${n:,.0f}"
