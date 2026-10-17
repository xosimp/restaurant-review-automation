"""
bar/variance.py — what the bar bought against what the POS rang, product by
product (bar fix round 10/15/26, F2 / CMP-3 / CMP-11 / AIX-18 step 3), and
keg yield (F14).

Two stages, both from data already on file:

  Stage 1 — 1:1 items (packaged beer and seltzer, bottled wine): a POS item
  the owner confirmed is one bought product sold as it comes (bar_drink_map
  with no pour, bar.margins.set_drink_map). Units bought = cases × the pack
  (bar.products.parse_pack, the owner's pack first) over every invoice line
  of the product, credits and returns netted by their negative quantity;
  units rung = the POS's sale lines of the mapped items (pos_ticket_lines).

  Stage 2 — drafts and poured drinks: rung quantity × the pour — the drink
  map's pour_oz, else the bar's default pour for the category
  (bar.products.pour_for: 1.5 oz spirits, 5 oz wine, 16 oz draft; said as
  an assumption) — against the ounces bought (bottles × size, kegs × the
  keg's ounces). A "Double" modifier linked to a drink (ticketitem's
  parent link, archive layout 4) doubles its pour; a priced "(Liquor
  Add-Ons)" line follows its parent — its quantity, its double, and its
  comp or void. Where RPOWER keeps an alternate quantity in ounces
  (pos_sales_categories.alt_qty_unit + the line's aqu_count) the rung
  ounces are RPOWER's own; where it keeps a cost on the line
  (ticketsales cost) that cost is shown beside the row as a cross-check
  (`rpower_cost_usd`) — theoretical dollars stay on invoice cost
  (BAR-CALC-17); each row says which basis it used.

Pricing (BAR-CALC-01, 10/16/26): a product's cost per each and per ounce is
what it cost in the span — its item lines with known units, plus its
per-unit charge lines and its own rebates (BAR-CALC-19) — else, when
nothing measurable was bought in the span (stock bought before it), the
latest purchase before it, said as an assumption. A drink whose product has
no cost, or whose pour is unknown (a pitcher with no size, BAR-CALC-16), is
not matched: its sales stay in the category's base and the matched share
falls. An invoice line with no unit of measure and no count in its words
adds no units and no dollars (BAR-CALC-04).

  Cocktails: the bar's recipe cards (docs/clients/simple-ejs/bar_recipes.json
  for Simple EJ's) load as SUGGESTED specs (`bar_drink_specs`: a POS button,
  a bought product, ounces) — only the account holder's confirmation makes a
  spec count. A suggestion is never silently final.

Comps (the POS's comp lines) are "poured, not paid": shown apart, never
counted as rung. Voids are shown apart too and counted nowhere. Logged waste
(bar.waste) is shown beside each row, never netted into a product's gap; a
category's counted variance subtracts comps and waste as named parts.

A POS item in a beer, liquor or wine category (the bar mapping) that is not
matched to a product is EXCLUDED and NAMED (`unmapped`) — never counted as 0
rung, which would read as a whole product's purchases gone missing.

The label: "Bought vs sold, by product" until two bar counts bracket the span; then
"Variance" — each category's used dollars from the counts (bar.counts:
begin + bought − end) against what the POS says left the shelf: the
theoretical cost of what was sold, plus what was comped and what was logged
as waste, the parts shown beside it (BAR-CALC-05) — each over the
category's OWN count dates (BAR-CALC-14) — withheld until the matched drinks
carry CATEGORY_COVERAGE_FLOOR of the category's POS drink sales (set-aside
and unmatched drinks count against it). The counts are category values, so
each product row stays bought vs rung (item counts would make each row a
variance).

Withheld with a reason when the two sides don't line up: fewer than
MIN_SPAN_DAYS where both invoices and archived POS nights are on file, or
the POS archive covering less than ARCHIVE_COVERAGE_FLOOR of the span's
trading nights. A distributor whose invoices are missing inside the span
(bar.pour_cost._gaps) marks its products incomplete.
"""
import json
import logging
import os
import re
from collections import defaultdict
from datetime import date, timedelta

from bar.common import POUR_CATEGORIES, _mdy, display_name, get_conn
from bar.products import _ML_PER_OZ, _now, canonical_keys, parse_pack, pour_for

log = logging.getLogger("bar.variance")

SPAN_DAYS = 56
MIN_SPAN_DAYS = 28
ARCHIVE_COVERAGE_FLOOR = 0.90
# Counted variance needs the matched drinks to carry this share of the
# category's POS drink sales — set-aside and unmatched sales count against
# it, since they carry no theoretical cost (BAR-CALC-05, 10/16/26: at 90%
# a tenth of what was poured read as variance).
CATEGORY_COVERAGE_FLOOR = 0.95
MAX_ITEMS = 60
MAX_UNMAPPED = 25
MAX_SUGGESTIONS = 40
PINT_OZ = 16.0
# A modifier that doubles the drink it is linked to.
DOUBLE_RE = re.compile(r"\b(double|dbl|2x)\b", re.I)
# A wine item sold as the whole bottle, read from its POS name.
WHOLE_BOTTLE_RE = re.compile(r"\b(btl|bottle)\b", re.I)
# RPOWER's alternate quantity unit when it is a volume.
_ALT_OZ_RE = re.compile(r"\b(oz|ounces?)\b", re.I)
_ALT_ML_RE = re.compile(r"\bml\b|millilit", re.I)
SPEC_STATUSES = ("suggested", "confirmed")
RECIPE_CARDS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "docs", "clients", "simple-ejs", "bar_recipes.json")
LABELS = {"bought_vs_rung": "Bought vs sold, by product", "variance": "Variance"}

# POS items that are not a drink to match, set aside and named rather than
# asked about (owner, 10/16/26, from EJ's RPOWER lines): an add-on rings on its
# own $0 or $3 line after the drink it changes; "Up" is how a martini is
# served; an open-price key has no product behind it; a bucket's own line is
# its price - its beers ring as the "<beer>`Bucket" lines after it; a 0.0 or
# NA beer has no alcohol. Each reason's sentence is shown beside the item.
SET_ASIDE = {
    "add_on": "An add-on — the drink it was added to carries the liquor",
    "style": "How a drink is served, not a drink",
    "open_price": "An open-price key — typed in by hand, no product behind it",
    "bucket": "A bucket's price — its beers ring on the lines after it and count there",
    "non_alcoholic": "Non-alcoholic — no alcohol to count",
    "not_a_drink": "You set it aside: not a drink",
    "not_on_invoices": "You set it aside: not bought on any invoice",
}
_STYLE_WORDS = {"up", "neat", "rocks", "on the rocks", "tall", "short", "dirty", "extra dirty", "dry", "wet"}


def set_aside_reason(item_name):
    """The SET_ASIDE key a POS item's own name says, or None."""
    n = " ".join(str(item_name or "").split())
    base, _, group = n.partition("`")
    if group and re.search(r"add[- ]?on", group, re.I):
        return "add_on"
    if base.lower() in _STYLE_WORDS:
        return "style"
    if re.match(r"open\s+(beer|liquor|wine|bar|drink|cocktail|shot)s?\b", base, re.I):
        return "open_price"
    if not group and re.search(r"\bbucket$", base, re.I):
        return "bucket"
    if re.search(r"(\b0\.0\b|\bn/?a$|\bnon[- ]?alc|\bzero proof\b)", base, re.I):
        return "non_alcoholic"
    return None
# Ingredient words that are alcohol (a recipe card lists juices and syrups too).
_SPIRIT_RE = re.compile(r"vodka|gin\b|rum\b|bourbon|whiske?y|scotch|rye\b|tequila|mezcal|blanco|reposado|"
                        r"liqueur|vermouth|aperol|campari|amaro|brandy|cognac|schnapps|triple\s*sec|cointreau|"
                        r"st\.?\s*germain|elderflower|kahlua|coffee\s*liqueur|prosecco|champagne|wine|beer", re.I)
_WORD = re.compile(r"[A-Za-z']{3,}")
_STOP = {"the", "and", "btl", "can", "case", "liq", "bar", "loose", "750ml"}


# ── the owner's specs (cocktail recipe cards) ───────────────────────────────

def init_bar_variance(db_path=None):
    """`bar_drink_specs`: a POS drink's ingredients as bought products and
    ounces — suggested from the bar's recipe cards, counted only once the
    account holder confirms. Boot only (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_drink_specs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            item_name TEXT NOT NULL,
            item_id TEXT,
            ingredient TEXT NOT NULL,
            product_key TEXT,
            oz REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'suggested',
            source TEXT NOT NULL DEFAULT 'recipe_card',
            recipe TEXT,
            note TEXT,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            UNIQUE (restaurant_id, provider, item_name, ingredient))""")
        conn.commit()
    finally:
        conn.close()


def drink_specs(restaurant_id, db_path=None, status=None) -> list:
    sql = ("SELECT id, provider, item_name, item_id, ingredient, product_key, oz, status, source, recipe, note "
           "FROM bar_drink_specs WHERE restaurant_id=?")
    args = [int(restaurant_id)]
    if status:
        sql += " AND status=?"
        args.append(status)
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(sql + " ORDER BY item_name, id", args).fetchall()
        except Exception:
            rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


# What kind of spirit a bottle is, not which bottle: shared alone it never
# beats a brand word (10/15/26: "WhistlePig Bourbon" and "Angel's Envy
# Bourbon" both matched Barmen 1873 Bourbon on the word "bourbon").
_GENERIC = {"bourbon", "bbn", "whiskey", "whisky", "rye", "scotch", "vodka", "vdk", "gin", "rum", "tequila", "teq",
            "mezcal", "liqueur", "liq", "brandy", "cognac", "wine", "blanco", "reposado", "anejo", "white", "dark",
            "spiced", "dry", "sweet", "red"}


def _words(s) -> set:
    """Lower-cased words, apostrophes dropped ("Angel's" reads as "angels")."""
    return {w.lower().replace("'", "") for w in _WORD.findall(str(s or "")) if w.lower() not in _STOP}


def _best_product(ingredient, products) -> tuple:
    """(product_key, name, match) — the bought liquor/wine product whose
    description shares the most words with an ingredient, or (None, None, 0)."""
    w = _words(ingredient)
    brand = w - _GENERIC
    best = (None, None, 0.0)
    for key, (name, pw) in products.items():
        shared = w & pw
        if not shared or not any(len(x) >= 4 for x in shared):
            continue
        named = shared - _GENERIC
        if brand and not named:
            continue          # the card names a brand this bottle doesn't carry
        # A brand word counts twice a generic one.
        score = (2 * len(named) + len(shared - named)) / (len(w | pw) + len(named))
        if score > best[2]:
            best = (key, name, round(score, 2))
    return best


def _pos_items(conn, restaurant_id) -> dict:
    """{name lower: (provider, item_id)} from the POS's menu catalog and the
    archive's own item names."""
    out = {}
    for sql in ("SELECT provider, item_id, name FROM pos_menu_catalog WHERE restaurant_id=?",
                "SELECT provider, item_id, MAX(item_name) FROM pos_ticket_lines WHERE restaurant_id=? "
                "AND item_id IS NOT NULL GROUP BY provider, item_id"):
        try:
            for r in conn.execute(sql, (int(restaurant_id),)).fetchall():
                if r[2]:
                    out.setdefault(" ".join(str(r[2]).lower().split()), (r[0], str(r[1])))
        except Exception:
            continue
    return out


def load_recipe_cards(restaurant_id, path=None, provider="rpower", db_path=None, lines=None, apply=True) -> dict:
    """Load a bar's recipe cards as SUGGESTED specs: each alcoholic
    ingredient measured in ounces, against the bought product its name
    matches best (or none), on the POS button the card names. Idempotent: a
    row the account holder confirmed or changed is never touched; a
    suggestion is refreshed. {ok, suggested, kept, skipped [..], unresolved
    [..], retired [..]} — `unresolved` names buttons the POS doesn't list
    yet (item_id empty; `compute` attaches the spec by name the night the
    button first sells); `retired` names cards marked off the menu, whose
    unconfirmed suggestions are cleared."""
    data = json.load(open(path or RECIPE_CARDS, encoding="utf-8"))
    if lines is None:
        from bar.purchases import _lines
        lines = _lines(restaurant_id, db_path)
    products = {}
    for l in lines:
        if l.get("kind") == "item" and l.get("category") in ("liquor", "wine"):
            products.setdefault(l["product_key"], (display_name(l["description"]), _words(l["description"])))
    out = {"ok": True, "suggested": 0, "kept": 0, "skipped": [], "unresolved": [], "retired": [], "rows": []}
    conn = get_conn(db_path)
    try:
        items = _pos_items(conn, restaurant_id)
        existing = {(r["item_name"].lower(), r["ingredient"].lower()): r for r in
                    (dict(x) for x in conn.execute(
                        "SELECT id, item_name, ingredient, status, source FROM bar_drink_specs "
                        "WHERE restaurant_id=? AND provider=?", (int(restaurant_id), provider)).fetchall())}
        for rec in data.get("recipes") or []:
            if not rec.get("alcoholic") or not rec.get("rpower_item"):
                continue
            if rec.get("retired"):
                # Off the menu (a seasonal drink replaced): its unconfirmed
                # suggestions go; a confirmed spec stays for the nights it sold.
                names = [rec["rpower_item"]] + [a["item"] for a in rec.get("rpower_also") or [] if a.get("item")]
                out["retired"].append(rec.get("name"))
                if apply:
                    for b in names:
                        conn.execute("DELETE FROM bar_drink_specs WHERE restaurant_id=? AND provider=? "
                                     "AND lower(item_name)=? AND status!='confirmed' AND source='recipe_card'",
                                     (int(restaurant_id), provider, str(b).lower()))
                continue
            buttons = [(rec["rpower_item"], 1.0, None)]
            for also in rec.get("rpower_also") or []:
                # "Walnut For 2": likely a double batch — suggested at twice
                # the card, with the card's own doubt kept as the note.
                if also.get("item"):
                    buttons.append((also["item"], 2.0, also.get("note") or "a variant of the card — confirm the spec"))
            for button, mult, note in buttons:
                hit = items.get(" ".join(str(button).lower().split()))
                if not hit:
                    out["unresolved"].append(button)
                for ing in rec.get("ingredients") or []:
                    amount, unit = ing.get("amount"), str(ing.get("unit") or "").lower()
                    if amount is None or unit != "oz":
                        continue          # a dash, a splash or a top: no volume on the card
                    pk, pname, score = _best_product(ing.get("item"), products)
                    if not pk and not _SPIRIT_RE.search(str(ing.get("item") or "")):
                        continue          # lime juice, syrups, purees: not a bought alcohol
                    row = {"button": button, "item_id": hit[1] if hit else None, "ingredient": ing["item"],
                           "product_key": pk, "product": pname, "oz": round(float(amount) * mult, 3),
                           "recipe": rec.get("name"), "note": note}
                    out["rows"].append(row)
                    prev = existing.get((str(button).lower(), str(ing["item"]).lower()))
                    if prev and (prev["status"] == "confirmed" or prev["source"] != "recipe_card"):
                        out["kept"] += 1
                        continue
                    out["suggested"] += 1
                    if not apply:
                        continue
                    conn.execute(
                        "INSERT INTO bar_drink_specs (restaurant_id, provider, item_name, item_id, ingredient, "
                        "product_key, oz, status, source, recipe, note, set_by, set_at) "
                        "VALUES (?,?,?,?,?,?,?,'suggested','recipe_card',?,?,NULL,?) "
                        "ON CONFLICT(restaurant_id, provider, item_name, ingredient) DO UPDATE SET "
                        "item_id=excluded.item_id, product_key=excluded.product_key, oz=excluded.oz, "
                        "recipe=excluded.recipe, note=excluded.note, set_at=excluded.set_at",
                        (int(restaurant_id), provider, button, row["item_id"], ing["item"], pk, row["oz"],
                         rec.get("name"), note, _now()))
        if apply:
            conn.commit()
    finally:
        conn.close()
    out["unresolved"] = sorted(set(out["unresolved"]))
    return out


def set_drink_spec(restaurant_id, spec_id, product_key=None, oz=None, confirm=None, remove=False,
                   user_id=None, db_path=None, known_keys=None) -> dict:
    """The account holder's word on one spec row: its product and ounces,
    confirmed (counted in variance) or back to a suggestion, or removed."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM bar_drink_specs WHERE restaurant_id=? AND id=?",
                           (int(restaurant_id), int(spec_id))).fetchone()
        if not row:
            raise LookupError("No such drink spec.")
        if remove:
            conn.execute("DELETE FROM bar_drink_specs WHERE restaurant_id=? AND id=?",
                         (int(restaurant_id), int(spec_id)))
            conn.commit()
            return {"ok": True, "id": int(spec_id), "removed": True, "item_name": row["item_name"],
                    "ingredient": row["ingredient"]}
        key = row["product_key"] if product_key is None else (str(product_key).strip() or None)
        if key and known_keys is not None and key not in known_keys:
            raise ValueError("Pick a product the bar has bought.")
        n = float(row["oz"]) if oz in (None, "") else float(oz)
        if not 0.05 <= n <= 40:
            raise ValueError("A pour is between 0.05 and 40 oz.")
        status = row["status"] if confirm is None else ("confirmed" if confirm else "suggested")
        if status == "confirmed" and not key:
            raise ValueError("Pick the bought product before confirming.")
        conn.execute("UPDATE bar_drink_specs SET product_key=?, oz=?, status=?, source=?, set_by=?, set_at=? "
                     "WHERE restaurant_id=? AND id=?",
                     (key, n, status, "owner", user_id, _now(), int(restaurant_id), int(spec_id)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": int(spec_id), "item_name": row["item_name"], "ingredient": row["ingredient"],
            "product_key": key, "oz": n, "status": status}


# ── reads ───────────────────────────────────────────────────────────────────

def _archive_nights(conn, restaurant_id) -> dict:
    """{date: layout version} of the archived POS nights."""
    try:
        rows = conn.execute("SELECT business_date, MAX(COALESCE(version, 1)) FROM pos_archive_days "
                            "WHERE restaurant_id=? GROUP BY business_date", (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    return {str(r[0])[:10]: int(r[1] or 1) for r in rows}


def _pos_lines(conn, restaurant_id, start, end) -> list:
    rows = conn.execute(
        "SELECT line_id, ticket_id, business_date, provider, item_id, item_name, kind, qty, sales, cost, aqu_count, "
        "department, sales_category, category_id, parent_line_id, mod_level, item_at FROM pos_ticket_lines "
        "WHERE restaurant_id=? AND business_date>=? AND business_date<=? AND kind IN ('sale','comp','void')",
        (int(restaurant_id), start, end)).fetchall()
    names = list(rows[0].keys()) if rows else []
    return [dict(zip(names, r)) for r in rows]


def _alt_units(conn, restaurant_id) -> dict:
    try:
        rows = conn.execute("SELECT category_id, alt_qty_unit FROM pos_sales_categories WHERE restaurant_id=? "
                            "AND alt_qty_unit IS NOT NULL AND alt_qty_unit<>''", (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    return {str(r[0]): str(r[1]) for r in rows}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _span(lines, nights, today, counted=None) -> dict:
    """{from, to, days, basis, period} or {why}: the span both sides cover."""
    t = today.isoformat()
    inv = sorted({d for d in (str(l["invoice_date"])[:10] for l in lines) if d <= t})
    pos = sorted(d for d in nights if d < t)
    if not inv:
        return {"why": "No distributor invoices yet."}
    if not pos:
        return {"why": "No POS item sales archived yet — bought vs sold needs the POS's ticket lines."}
    for p in reversed((counted or {}).get("periods") or []):
        if not p.get("ok"):
            continue
        # The widest of the categories' own count spans (each category's
        # theoretical is read over its own dates: BAR-CALC-14).
        a, b = _period_span(p)
        if a >= pos[0] and b <= pos[-1] and b <= inv[-1]:
            return {"from": a, "to": b, "basis": "variance", "period": p}
        break
    end = min(inv[-1], pos[-1])
    start = max((date.fromisoformat(end) - timedelta(days=SPAN_DAYS - 1)).isoformat(), inv[0], pos[0])
    if start > end:
        return {"why": "The invoices and the POS archive don't overlap yet."}
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if days < MIN_SPAN_DAYS:
        return {"why": (f"Invoices and POS item sales overlap for {days} day{'s' if days != 1 else ''} "
                        f"({_mdy(start)}–{_mdy(end)}); bought vs sold needs {MIN_SPAN_DAYS}.")}
    return {"from": start, "to": end, "basis": "bought_vs_rung", "period": None}


def _cat_span(period, c):
    """(from, to) ISO of one category's own count span — the night after
    its opening count through its closing count (bar.counts) — else the
    period's."""
    pc = ((period or {}).get("categories") or {}).get(c) or {}
    bd, ed = pc.get("begin_date") or period.get("from"), pc.get("end_date") or period.get("to")
    return (date.fromisoformat(str(bd)[:10]) + timedelta(days=1)).isoformat(), str(ed)[:10]


def _period_span(period) -> tuple:
    """(from, to): the widest of the measured categories' own spans."""
    spans = [_cat_span(period, c) for c in POUR_CATEGORIES
             if (((period.get("categories") or {}).get(c) or {}).get("used") is not None)]
    if not spans:
        spans = [_cat_span(period, None)]
    return min(s[0] for s in spans), max(s[1] for s in spans)


def _trading_days(restaurant_id, a, b, db_path=None) -> list:
    from bar.counts import _trading_days as td
    return td(restaurant_id, date.fromisoformat(a), date.fromisoformat(b), db_path=db_path)


# ── the computation ─────────────────────────────────────────────────────────

class _Product:
    def __init__(self, key):
        self.key = key
        self.name = None
        self.category = None
        self.pack = None
        self.bought_each = 0.0
        self.bought_oz = 0.0
        self.bought_usd = 0.0         # in-span item lines with known units, plus charges and rebates
        self.item_usd = 0.0           # in-span item lines with known units
        self.oz_item_usd = 0.0        # ... of those, the lines whose ounces are known
        self.extra_usd = 0.0          # per-unit charge lines and the product's own rebates (BAR-CALC-19)
        self.oz_known = True
        self.units_unknown = 0        # in-span lines with no unit and no count (BAR-CALC-04)
        self.history = []             # (date, each, oz|None, net): every bought line with known units
        self.assumes = set()
        self.distributors = set()
        self.uom_missing = False
        self.mode = "each"           # each | oz: oz once any drink pours from it
        self._last = ""
        self.rung_each = defaultdict(float)
        self.rung_oz = defaultdict(float)
        self.rung_each_day = defaultdict(lambda: defaultdict(float))
        self.rung_oz_day = defaultdict(lambda: defaultdict(float))
        self.rpower_cost = 0.0
        self.rpower_cost_lines = 0
        self.rung_lines = 0
        self.basis = set()
        self.drinks = set()
        self.per_each = None
        self.per_oz = None
        self.priced_from = None       # the purchase date a price was read from, when none fell in the span


# A serving whose size its name must state (BAR-CALC-16): a pitcher, a mug,
# a stein, a growler or a half pour holds nothing the bar's default draft
# pour says. "Half pint" is 8 oz; a tower is 100 (stated_oz).
_SERVE_RE = re.compile(r"\b(pitchers?|mugs?|steins?|growlers?|half[\s-]?pours?)\b", re.I)


def pour_target(category, pack, pour_oz, item_name, cfg=None):
    """(mode each|oz|unknown, oz, basis, assumption) for one POS drink poured
    from one bought product — THE rule, shared by the variance and the
    drink margins (BAR-CALC-07):

    - the owner's pour first; a pour at the whole container is the
      container sold as it comes;
    - a packaged beer (no pour for its category) is one can or bottle;
    - a keg pours the size the button names ("Dos Equis 22oz", a tower 100
      oz); a pitcher, mug or half pour that names no size is `unknown` —
      never the 16 oz default (BAR-CALC-16);
    - a wine named as a bottle ("Btl", "Bottle") is the whole bottle;
    - else the bar's default pour, said as an assumption."""
    pk = pack or {}
    cont = pk.get("container_oz")
    if pour_oz:
        oz = float(pour_oz)
        if pk.get("each") != "keg" and cont and oz >= 0.95 * cont:
            return "each", None, "owner_pour", None
        return "oz", oz, "owner_pour", None
    oz, label, _yield = pour_for(category, pk, cfg)
    if oz is None:
        return "each", None, "one_each", None
    if pk.get("each") == "keg":
        said = stated_oz(item_name)
        if said:
            # "Dos Equis 22oz" pours 22, not the 16 oz default (10/15/26: five
            # 22oz drafts matched without a pour were counted at 16).
            return "oz", said, "stated_pour", None
        if _SERVE_RE.search(str(item_name or "")):
            return ("unknown", None, "no_size",
                    f"{item_name} names no size — set its pour to count it")
    if category == "wine" and pk.get("each") != "keg" and WHOLE_BOTTLE_RE.search(str(item_name or "")):
        return "each", None, "one_each", f"{item_name} read as a whole bottle from its POS name"
    return "oz", oz, "default_pour", f"assumes the {label} (no pour set for {item_name or 'this drink'})"


def _pour_target(m, prod, cfg, item_name):
    """(mode, oz, basis, assumption) for one drink-map row on its product."""
    return pour_target(prod.category, prod.pack, m.get("pour_oz"), item_name, cfg)


def _line_units(l, packs, ck):
    """(pack, units, ounces) one item line bought — the owner's pack first;
    units None when the line has no unit of measure and its words state no
    count (BAR-CALC-04), ounces None when the size isn't known."""
    pk = parse_pack(l["description"], l.get("uom"), l["category"])
    ov = packs.get(l["product_key"]) or packs.get(ck) or {}
    uom = str(l.get("uom") or "").upper()
    if ov.get("units_per_case") and uom in ("CA", "CS"):
        pk.update(units_per_case=int(ov["units_per_case"]), units_source="owner")
    if ov.get("container_ml"):
        pk.update(container_oz=round(float(ov["container_ml"]) / _ML_PER_OZ, 2), size_source="owner")
    if pk["units_per_case"] is None:
        return pk, None, None
    each = _f(l["qty"]) * pk["units_per_case"]
    oz = each * float(pk["container_oz"]) if pk.get("container_oz") else None
    return pk, each, oz


def _price(p, start):
    """The product's cost per each and per ounce (BAR-CALC-01): what it
    cost in the span — its item lines with known units, plus its charges
    and rebates — else, when nothing measurable was bought in the span, the
    latest purchase before it. Never priced from nothing: None."""
    if p.bought_each > 0 and p.item_usd > 0:
        p.per_each = (p.item_usd + p.extra_usd) / p.bought_each
    if p.bought_oz > 0 and p.oz_item_usd > 0:
        share = p.oz_item_usd / p.item_usd if p.item_usd > 0 else 1.0
        p.per_oz = (p.oz_item_usd + p.extra_usd * share) / p.bought_oz
    if p.per_each is None or p.per_oz is None:
        before = [h for h in p.history if h[0] < start and h[1] > 0 and h[3] > 0]
        if before:
            if p.per_each is None:
                day = max(h[0] for h in before)
                hs = [h for h in before if h[0] == day]
                p.per_each = sum(h[3] for h in hs) / sum(h[1] for h in hs)
                p.priced_from = day
            if p.per_oz is None:
                with_oz = [h for h in before if h[2]]
                if with_oz:
                    day = max(h[0] for h in with_oz)
                    hs = [h for h in with_oz if h[0] == day]
                    p.per_oz = sum(h[3] for h in hs) / sum(h[2] for h in hs)
                    p.priced_from = p.priced_from or day


def _rebate_targets(rebate, items):
    """The in-span item lines a product's own rebate line lowers: the one
    line on its invoice, in its category, whose quantity it reverses, else
    every such line pro rata by its cost. [(line, share)]."""
    same = [l for l in items if l.get("invoice_id") == rebate.get("invoice_id")
            and l.get("category") == rebate.get("category") and _f(l["qty"]) > 0 and _f(l["net"]) > 0]
    q = -_f(rebate.get("qty"))
    exact = [l for l in same if q > 0 and abs(_f(l["qty"]) - q) < 1e-6]
    if len(exact) == 1:
        return [(exact[0], 1.0)]
    total = sum(_f(l["net"]) for l in same)
    return [(l, _f(l["net"]) / total) for l in same] if total > 0 else []


def _bucket_tickets(pos_rows) -> set:
    """Tickets whose bucket's beers ring on their own "<beer>`Bucket" lines:
    only there is a bucket's own line its price alone (BAR-CALC-16)."""
    out = set()
    for r in pos_rows:
        base, sep, group = str(r.get("item_name") or "").partition("`")
        if sep and re.search(r"bucket", group, re.I):
            out.add(r.get("ticket_id"))
    return out


def compute(restaurant_id, today=None, db_path=None, lines=None, counted=None, cfg=None, mapping=None,
            sales=None, canon=None) -> dict:
    """{variance, keg_yield, span, products} — the payload's `variance` and
    `bar_ops.keg_yield`, and the internals the waste block reads. `canon`
    is the price watch's canonical-product map when the caller has it
    (bar.build: the same lines and the owner's links), else read here."""
    today = today or date.today()
    if lines is None:
        from bar.purchases import _lines
        lines = _lines(restaurant_id, db_path)
    from bar.margins import drink_map
    from bar.products import pack_overrides, product_links, settings
    cfg = cfg or settings(restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        nights = _archive_nights(conn, restaurant_id)
    finally:
        conn.close()
    sp = _span(lines, nights, today, counted)
    if "why" in sp:
        return {"variance": {"available": False, "basis": None, "why": sp["why"], "items": [], "unmapped": []},
                "keg_yield": [], "span": None, "products": {}}
    start, end = sp["from"], sp["to"]
    trading = _trading_days(restaurant_id, start, end, db_path)
    have = [d for d in trading if d in nights]
    span_out = {"from": start, "to": end, "from_mdy": _mdy(start), "to_mdy": _mdy(end),
                "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1,
                "pos_nights": len(have), "trading_nights": len(trading)}
    if trading and len(have) < ARCHIVE_COVERAGE_FLOOR * len(trading):
        return {"variance": {"available": False, "basis": None, "span": span_out, "items": [], "unmapped": [],
                             "why": (f"The POS archive has {len(have)} of the {len(trading)} trading nights "
                                     f"{_mdy(start)}–{_mdy(end)}; bought vs sold needs "
                                     f"{int(ARCHIVE_COVERAGE_FLOOR * 100)}% so a missing night never reads as "
                                     "drinks gone missing.")},
                "keg_yield": [], "span": span_out, "products": {}}
    if canon is None:
        canon = canonical_keys(lines, product_links(restaurant_id, db_path))
    packs = pack_overrides(restaurant_id, db_path)
    # Purchases per canonical product: what was bought in the span, and every
    # bought line's unit cost for pricing what was sold from stock bought
    # before it (BAR-CALC-01).
    prods = {}
    newest = {}
    span_items, rebates, charges = [], [], []
    for l in lines:
        d = str(l["invoice_date"])[:10]
        if l.get("category") not in POUR_CATEGORIES:
            continue
        kind = l.get("kind")
        if kind == "charge":
            if start <= d <= end:
                charges.append(l)
            continue
        if kind != "item":
            continue
        if l.get("rebate"):
            if start <= d <= end:
                rebates.append(l)
            continue
        ck = canon.get(l["product_key"], l["product_key"])
        p = prods.get(ck)
        if p is None:
            p = prods[ck] = _Product(ck)
        # The newest line names the product and its pack (read once per
        # product below, not once per line: V17).
        if ck not in newest or d >= p._last:
            p._last = d
            newest[ck] = l
        if d > end:
            continue
        pk, each, oz = _line_units(l, packs, ck)
        if each is not None and _f(l["qty"]) > 0:
            p.history.append((d, each, oz, _f(l["net"])))
        if d < start:
            continue
        uom = str(l.get("uom") or "").upper()
        p.distributors.add(l["distributor"])
        if not uom:
            p.uom_missing = True
        if each is None:
            # No unit and no count: no units and no dollars in this
            # product's math, said beside it (BAR-CALC-04).
            p.units_unknown += 1
            continue
        span_items.append(l)
        p.bought_each += each
        p.item_usd += _f(l["net"])
        if oz is not None:
            p.bought_oz += oz
            p.oz_item_usd += _f(l["net"])
        else:
            p.oz_known = False
        if not uom:
            p.assumes.add(f"no unit of measure on some invoices — read as the {pk['units_per_case']} the "
                          "description states")
        if pk["units_source"] == "assumed" and uom in ("CA", "CS"):
            p.assumes.add(f"assumes {pk['units_per_case']} a case")
        if pk["size_source"] == "assumed":
            p.assumes.add("assumes a 1/2 barrel" if pk.get("each") == "keg" else f"assumes {pk['container_oz']:g} oz")
    # A per-unit charge (SGWS's $2.29 a bottle) is part of what its product
    # cost; a product's own rebate line lowers it (BAR-CALC-19).
    for l in charges:
        ck = canon.get(l["product_key"], l["product_key"])
        if ck in prods:
            prods[ck].extra_usd += _f(l["net"])
    for rb in rebates:
        for l, share in _rebate_targets(rb, span_items):
            ck = canon.get(l["product_key"], l["product_key"])
            prods[ck].extra_usd += _f(rb["net"]) * share
    for p in prods.values():
        p.bought_usd = p.item_usd + p.extra_usd
        _price(p, start)
        if p.units_unknown:
            p.assumes.add(f"{p.units_unknown} invoice line{'s' if p.units_unknown != 1 else ''} with no unit of "
                          "measure or count left out — units bought unknown")
        if p.priced_from:
            p.assumes.add(f"priced at the {_mdy(p.priced_from)} purchase — none in the span to price it from")
    for ck, l in newest.items():
        p = prods[ck]
        p.name = display_name(l["description"])
        p.category = l["category"]
        p.pack = parse_pack(l["description"], l.get("uom"), l["category"])
        ov = packs.get(l["product_key"]) or packs.get(ck)
        if ov:
            if ov.get("units_per_case") and str(l.get("uom") or "").upper() in ("CA", "CS"):
                p.pack.update(units_per_case=int(ov["units_per_case"]), units_source="owner")
            if ov.get("container_ml"):
                p.pack.update(container_ml=float(ov["container_ml"]),
                              container_oz=round(float(ov["container_ml"]) / _ML_PER_OZ, 2), size_source="owner")
    # The owner's map and confirmed specs: (provider, item_id) → [(ck, mode, oz, basis, assumption)].
    targets = defaultdict(list)
    dmap = drink_map(restaurant_id, db_path)
    from bar.margins import set_aside_map
    owner_aside = set_aside_map(restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        item_names = {}
        for r in conn.execute("SELECT provider, item_id, MAX(item_name) FROM pos_ticket_lines WHERE restaurant_id=? "
                              "AND business_date>=? AND business_date<=? AND item_id IS NOT NULL "
                              "GROUP BY provider, item_id", (int(restaurant_id), start, end)).fetchall():
            item_names[(r[0], str(r[1]))] = r[2]
        pos_rows = _pos_lines(conn, restaurant_id, start, end)
        alt = _alt_units(conn, restaurant_id)
        from bar.sales import _filer
        filed = _filer(conn, int(restaurant_id))
    finally:
        conn.close()
    for m in dmap:
        ck = canon.get(m["product_key"], m["product_key"])
        p = prods.get(ck)
        if p is None or p.pack is None:
            # A product with no purchase line on file: nothing to compare.
            continue
        k = (m["provider"], str(m["item_id"]))
        mode, oz, basis, assume = _pour_target(m, p, cfg, item_names.get(k))
        targets[k].append((ck, mode, oz, basis, assume))
    specs = drink_specs(restaurant_id, db_path)
    _names = product_names(lines=lines)
    # A card loaded before its button existed (a new seasonal drink) attaches
    # by name the first night it sells — no reload of the cards needed.
    by_name = {(k[0], " ".join(str(n or "").lower().split())): k[1] for k, n in item_names.items()}
    suggested = []
    # A drink with a confirmed recipe card pours what the card says: a
    # single-product match on the same button would count it twice (10/15/26:
    # the Salted Caramel Tini's Ketel One, once at 1.68 oz and again at 1.5).
    carded = set()
    for s in specs:
        iid = s["item_id"] or by_name.get((s["provider"], " ".join(str(s["item_name"] or "").lower().split())))
        if s["status"] == "confirmed" and s["product_key"] and iid:
            carded.add((s["provider"], str(iid)))
    for k in list(targets):
        if k in carded:
            del targets[k]
    for s in specs:
        if not s["item_id"]:
            s["item_id"] = by_name.get((s["provider"], " ".join(str(s["item_name"] or "").lower().split())))
        if s["status"] != "confirmed":
            suggested.append(s)
            continue
        if not s["item_id"] or not s["product_key"]:
            continue
        ck = canon.get(s["product_key"], s["product_key"])
        if ck not in prods or prods[ck].pack is None:
            continue
        targets[(s["provider"], str(s["item_id"]))].append((ck, "oz", float(s["oz"]), "recipe_card", None))
    # Walk the POS lines: modifiers follow their parent.
    by_id = {r["line_id"]: r for r in pos_rows}
    mult = defaultdict(lambda: 1.0)
    for r in pos_rows:
        par = r.get("parent_line_id")
        if par and par in by_id and DOUBLE_RE.search(str(r.get("item_name") or "")):
            mult[par] = 2.0
    bucket_tickets = _bucket_tickets(pos_rows)
    linked_nights = sum(1 for d in have if nights.get(d, 1) >= 4)
    if mapping is None:
        from bar.mapping import Mapping
        mapping = Mapping(restaurant_id, db_path)
    from bar.mapping import key_for
    unmapped = {}
    aside = {}
    aside_sales = defaultdict(float)
    drink_items = set()     # POS items sold as beer, liquor or wine under the mapping
    cat_sales = defaultdict(float)
    cat_sales_day = defaultdict(lambda: defaultdict(float))
    matched_lines = []      # (category, date, sales, product keys, every pour known)
    unsized = set()
    spec_pending = {(s["provider"], s["item_name"].lower()) for s in suggested}
    for r in pos_rows:
        par = by_id.get(r.get("parent_line_id")) if r.get("parent_line_id") else None
        kind = par["kind"] if par is not None else r["kind"]
        m = mult[par["line_id"]] if par is not None else mult[r["line_id"]]
        qty = _f(r["qty"])
        if par is not None and qty == 0:
            qty = _f(par["qty"])
        if kind in ("comp", "void"):
            qty = abs(qty)
        dep, sub = filed(r)
        bar_kind = mapping.resolve(*key_for(dep, sub))["kind"]
        is_drink = bar_kind in POUR_CATEGORIES
        day = str(r["business_date"])[:10]
        if kind == "sale" and is_drink:
            cat_sales[bar_kind] += _f(r["sales"])
            cat_sales_day[bar_kind][day] += _f(r["sales"])
            if r["item_id"]:
                drink_items.add((r["provider"], str(r["item_id"])))
        k = (r["provider"], str(r["item_id"] or ""))
        tg = targets.get(k)
        if not tg:
            pure_mod = par is not None and (DOUBLE_RE.search(str(r.get("item_name") or "")) or _f(r["sales"]) == 0)
            why = owner_aside.get(k) or set_aside_reason(r["item_name"])
            if why == "bucket" and r.get("ticket_id") not in bucket_tickets:
                why = None          # a bucket rung as one item: its beers are in it, ask about it
            if is_drink and kind == "sale" and r["item_id"] and not pure_mod and qty and why:
                # Set aside, and still in the matched share's base: its sales
                # carry no theoretical (BAR-CALC-05).
                a = aside.setdefault(k, {"provider": k[0], "item_id": k[1], "item": r["item_name"], "category": bar_kind,
                                         "reason": why, "label": SET_ASIDE.get(why, why), "owner": k in owner_aside,
                                         "rung_units": 0.0, "sales_usd": 0.0})
                a["rung_units"] += qty
                a["sales_usd"] += _f(r["sales"])
                aside_sales[bar_kind] += _f(r["sales"])
                continue
            if is_drink and kind == "sale" and r["item_id"] and not pure_mod and qty:
                u = unmapped.setdefault(k, {"provider": k[0], "item_id": k[1], "item": r["item_name"],
                                            "category": bar_kind, "rung_units": 0.0, "sales_usd": 0.0,
                                            "spec_suggested": (k[0], str(r["item_name"] or "").lower()) in spec_pending})
                u["rung_units"] += qty
                u["sales_usd"] += _f(r["sales"])
            continue
        known = all(mode != "unknown" for _ck, mode, _oz, _b, _a in tg)
        if kind == "sale" and is_drink:
            matched_lines.append((bar_kind, day, _f(r["sales"]), tuple(t[0] for t in tg), known))
        unit_alt = alt.get(str(r.get("category_id") or ""))
        aqu = _f(r.get("aqu_count"))
        for ck, mode, oz, basis, assume in tg:
            p = prods[ck]
            p.drinks.add(r["item_name"] or k[1])
            if assume:
                p.assumes.add(assume)
            if mode == "unknown":
                unsized.add(r["item_name"] or k[1])
                p.basis.add(basis)
                continue
            if mode == "each":
                cont_oz = float((p.pack or {}).get("container_oz") or 0)
                p.rung_each[kind] += qty * m
                p.rung_oz[kind] += qty * m * cont_oz
                p.rung_each_day[kind][day] += qty * m
                p.rung_oz_day[kind][day] += qty * m * cont_oz
                p.basis.add(basis)
            else:
                line_oz = qty * m * oz
                if len(tg) == 1 and aqu > 0 and unit_alt and (_ALT_OZ_RE.search(unit_alt) or _ALT_ML_RE.search(unit_alt)):
                    line_oz = aqu / _ML_PER_OZ if _ALT_ML_RE.search(unit_alt) else aqu
                    p.basis.add("rpower_alt_qty")
                else:
                    p.basis.add(basis)
                p.mode = "oz"
                p.rung_oz[kind] += line_oz
                p.rung_oz_day[kind][day] += line_oz
            if kind == "sale":
                p.rung_lines += 1
                if len(tg) == 1 and _f(r.get("cost")) > 0:
                    p.rpower_cost += _f(r["cost"])
                    p.rpower_cost_lines += 1
    # Distributor gaps inside the span mark their products incomplete.
    gapped = set()
    try:
        from bar.judge import owner_stopped
        from bar.pour_cost import _gaps
        for g in _gaps(lines, start, end, sales=sales, owner_stopped=owner_stopped(restaurant_id, db_path)):
            gapped.add(g["distributor"])
    except Exception:
        log.exception("[bar.variance] distributor gaps unread for %s", restaurant_id)

    def per_of(p):
        return p.per_oz if p.mode == "oz" else p.per_each

    # A drink is matched only when every product it pours from has a cost
    # and every pour is known: anything else leaves the matched share's base,
    # so coverage falls and the variance is withheld (BAR-CALC-01).
    matched_day = defaultdict(lambda: defaultdict(float))
    for c, d, s, cks, known in matched_lines:
        if known and all(per_of(prods[ck]) is not None for ck in cks):
            matched_day[c][d] += s
    mapped_sales = {c: sum(v.values()) for c, v in matched_day.items()}
    items, kegs = [], []
    for ck, p in prods.items():
        if not p.drinks:
            continue
        unit = p.mode
        complete_units = not p.units_unknown
        if unit == "oz":
            bought = p.bought_oz if (p.oz_known and complete_units) else None
            rung, comped, voided = p.rung_oz["sale"], p.rung_oz["comp"], p.rung_oz["void"]
        else:
            bought = p.bought_each if complete_units else None
            rung, comped, voided = p.rung_each["sale"], p.rung_each["comp"], p.rung_each["void"]
        per = per_of(p)
        gap = (bought - rung) if bought is not None else None
        # Theoretical dollars stay on invoice cost; RPOWER's own line cost is
        # a cross-check beside them, never the figure (BAR-CALC-17).
        theoretical_usd = rung * per if per is not None else None
        rp_all = p.rpower_cost_lines and p.rpower_cost_lines == p.rung_lines
        gap_usd = (p.bought_usd - theoretical_usd) if theoretical_usd is not None and bought is not None else None
        incomplete = bool(p.distributors & gapped)
        assumes = sorted(p.assumes)
        if not p.oz_known and unit == "oz":
            assumes.append("a bottle size isn't on the invoice — ounces bought unknown")
        if unit == "oz" and linked_nights < len(have) and any(b != "one_each" for b in p.basis):
            assumes.append(f"modifier links (a Double, an add-on) on {linked_nights} of {len(have)} nights")
        if incomplete or bought is None or p.uom_missing or per is None:
            conf = "low"
        elif assumes:
            conf = "medium"
        else:
            conf = "high"
        row = {"product_key": ck, "product": p.name, "category": p.category, "unit": unit,
               "unit_word": "oz" if unit == "oz" else ((p.pack or {}).get("each") or "unit"),
               "bought_units": _r(bought), "rung_units": _r(rung), "comped_units": _r(comped),
               "voided_units": _r(voided), "gap_units": _r(gap),
               "bought_usd": round(p.bought_usd, 2),
               "charges_usd": round(p.extra_usd, 2) if p.extra_usd else None,
               "theoretical_usd": round(theoretical_usd, 2) if theoretical_usd is not None else None,
               "rpower_cost_usd": round(p.rpower_cost, 2) if rp_all else None,
               "priced_from": p.priced_from, "priced_from_mdy": _mdy(p.priced_from) if p.priced_from else None,
               "comped_usd": round(comped * per, 2) if per is not None else None,
               "gap_usd": round(gap_usd, 2) if gap_usd is not None else None,
               "gap_pct": round(gap / bought * 100, 1) if gap is not None and bought else None,
               "theoretical_basis": sorted(p.basis), "drinks": sorted(p.drinks)[:8],
               "incomplete": incomplete, "confidence": conf, "assumes": assumes,
               "waste_units": None, "waste_entries": 0}
        items.append(row)
        if (p.pack or {}).get("each") == "keg":
            kegs.append((p, row))
    items.sort(key=lambda x: -abs(x["gap_usd"] or 0))
    unmapped_rows = sorted(unmapped.values(), key=lambda u: -u["sales_usd"])
    _cands = pour_candidates(lines)
    for u in unmapped_rows:
        u["rung_units"] = _r(u["rung_units"])
        u["sales_usd"] = round(u["sales_usd"], 2)
    for u in unmapped_rows[:MAX_UNMAPPED]:
        u["suggest"] = suggest_pour(u["item"], u["category"], _cands) or None
    aside_rows = sorted(aside.values(), key=lambda a: -a["sales_usd"])
    for a in aside_rows:
        a["rung_units"] = _r(a["rung_units"])
        a["sales_usd"] = round(a["sales_usd"], 2)
    totals = _totals(items, cat_sales, mapped_sales)
    out = {"set_aside": aside_rows, "set_aside_total": len(aside_rows),
           "set_aside_sales_usd": round(sum(a["sales_usd"] for a in aside_rows), 2),
           "available": bool(items), "basis": "bought_vs_rung", "label": LABELS["bought_vs_rung"],
           "span": span_out, "items": items[:MAX_ITEMS], "items_total": len(items),
           "unmapped": unmapped_rows[:MAX_UNMAPPED], "unmapped_total": len(unmapped_rows),
           "unmapped_sales_usd": round(sum(u["sales_usd"] for u in unmapped_rows), 2),
           "unsized": sorted(unsized)[:MAX_UNMAPPED],
           "totals": totals, "incomplete_distributors": sorted(gapped),
           "suggestions": [_spec_public(s, _names) for s in suggested[:MAX_SUGGESTIONS]],
           "suggestions_total": len(suggested),
           "note": ("Comps are poured, not paid: shown apart, never counted as sold. A POS drink not matched to a "
                    "product — or set aside — is left out of the matched share and named, never counted as zero.")}
    if not items:
        out["why"] = ("No POS drink is matched to a bought product yet. Match a drink to what you buy and its "
                      "bought vs sold appears here.")
    if sp["basis"] == "variance":
        waste = _waste_by_product_day(restaurant_id, prods, start, end, db_path)
        out.update(basis="variance", label=LABELS["variance"], item_basis="bought_vs_rung",
                   categories=_counted_variance(sp["period"], prods, per_of, cat_sales_day, matched_day, waste,
                                                (start, end)),
                   item_note="Counts are by category, so each product reads bought vs sold; a category's "
                             "variance is what the counts say it used against what was sold, comped and "
                             "logged as waste, each priced at what the product cost.")
    return {"variance": out, "keg_yield": _keg_yield(kegs, cfg), "span": span_out, "products": prods,
            "drink_items": drink_items}


def _r(v, n=1):
    return None if v is None else round(v, n)


def product_names(restaurant_id=None, db_path=None, lines=None) -> dict:
    """{product_key: the product's readable name} from the purchase lines —
    what a recipe card names as "Poured from" (10/15/26: a key the page's
    product list didn't carry read as "euclid beverage north aurora|3101756").
    Without `lines`, read from the distinct products on file
    (bar.purchases.product_catalog, SCALE-09), never every invoice line."""
    if lines is None:
        from bar.purchases import product_catalog
        return {k: v["name"] for k, v in product_catalog(restaurant_id, db_path, kinds=("item", "charge")).items()}
    out = {}
    for l in lines or []:
        if l.get("product_key") and l.get("description"):
            out.setdefault(l["product_key"], display_name(l["description"]))
    return out


_SIZE_RE = re.compile(r"(\d{1,3}(?:\.\d)?)\s*oz\b", re.I)
_NOT_ITEM_RE = re.compile(r"discount|deposit|empties|fee\b|charge|credit|handle", re.I)
# A beer's variety must agree (owner, 10/15/26: "Coors Banquet" was offered
# Coors Light 24 cans): Light, Ultra, Banquet, Original, Zero are different beers.
_VARIANTS = {"lite": "lite", "light": "lite", "lt": "lite", "ultra": "ultra", "banquet": "banquet",
             "original": "original", "zero": "na", "amber": "amber", "black": "black"}
# Non-alcoholic is a variety of its own (owner, 10/15/26: "Corona NA" was
# offered Corona Extra, "Blue Moon N/A" a blue curacao): NA, N/A, 0.0, zero,
# non-alcoholic, alcohol-free — read from the raw name, where "N/A" and "0.0"
# are not words.
_NA_RE = re.compile(r"(?<![a-z0-9])(n\s*/\s*a|na|0\.0|non[\s-]?alc\w*|no[\s-]?alc\w*|alcohol[\s-]?free)(?![a-z0-9])", re.I)


def variants(text) -> set:
    """The varieties a name states, from its raw text (a 2-letter "LT" too)."""
    raw = str(text or "").lower() if isinstance(text, str) else " ".join(text or ())
    toks = re.findall(r"[a-z]+", raw)
    out = {_VARIANTS[w] for w in toks if w in _VARIANTS}
    if _NA_RE.search(raw):
        out.add("na")
    return out


def stated_oz(name):
    """The ounces a POS button's name states: "22oz" -> 22, "60 oz pitcher"
    -> 60; a tower is 3 L, taken as 100 oz (owner, 10/15/26: "ALL towers are
    3L, 100oz"); a half pint 8. A pitcher or mug that states nothing is None
    (BAR-CALC-16: never the draft default)."""
    n = str(name or "")
    if re.search(r"\btower\b", n, re.I):
        return 100.0
    m = _SIZE_RE.search(n)
    if m:
        return float(m.group(1))
    if re.search(r"\bhalf[\s-]?pint\b", n, re.I):
        return 8.0
    return None


_POS_NOISE = re.compile(r"\b(\d{1,3}(?:\.\d)?\s*oz|btl|bottle|can|gl|glass|dft|draft|draught|n/a|na|tower|pitchers?|mugs?|half pint|tap)\b", re.I)


# How a POS button says its drink is served (ING-20): from a keg, or as a
# can or bottle. A button that says neither can pour from either.
_DRAFT_WORDS = re.compile(r"\b(draft|draught|dft|tap|pitchers?|tower|mugs?|steins?|pints?|half[\s-]?pint)\b", re.I)
_PACKAGE_WORDS = re.compile(r"\b(cans?|btl|btls|bottles?|tall\s*boy|tallboy)\b", re.I)


def pour_candidates(lines) -> dict:
    """{product_key: (name, words, category, keg, variants, container_oz)} —
    the bought beer, liquor and wine a POS drink can pour from (never a
    discount, deposit or fee); a keg by the one shared test
    (bar.classify.is_keg), and the container's ounces when the invoice says."""
    from bar.classify import is_keg
    out = {}
    for l in lines or []:
        d = l.get("description") or ""
        if l.get("kind") != "item" or l.get("category") not in POUR_CATEGORIES or _NOT_ITEM_RE.search(d):
            continue
        if l["product_key"] in out:
            continue
        keg = is_keg(d, l.get("uom"))
        pk = parse_pack(d, l.get("uom"), l["category"])
        cont = pk.get("container_oz") if pk.get("size_source") == "parsed" and not keg else None
        out[l["product_key"]] = (display_name(d), _words(d), l["category"], keg, variants(d), cont)
    return out


def form_fits(item_name, category, keg, container_oz=None) -> bool:
    """Whether a POS drink can pour from a product of this form (ING-20): a
    draft, tap, pitcher or tower button only from a keg; a can or bottle
    button never from one, and when it states its ounces, only from a
    container that size. A beer button that names a draft size (16 oz or
    more, no can or bottle word) is a draft."""
    name = str(item_name or "")
    package = bool(_PACKAGE_WORDS.search(name))
    oz = stated_oz(name)
    draft = bool(_DRAFT_WORDS.search(name)) or (category == "beer" and not package and oz is not None and oz >= 10)
    if draft and not package:
        return bool(keg)
    if package:
        if keg:
            return False
        if oz is not None and container_oz and abs(float(container_oz) - oz) > 0.6:
            return False
    return True


def suggest_pour(item_name, category, cands) -> dict:
    """What an unmatched POS drink most likely pours (owner, 10/15/26: "the
    easiest way to correlate these so I don't mess up"): the bottle or keg
    of its category whose brand words it shares, a keg for a draft size,
    and the ounces its name states ("Miller Lite 22oz" -> LITE 1/2 BBL, 22).
    {product_key, product, oz} or {} — a suggestion the owner checks, never
    a match until they press Match."""
    name = str(item_name or "")
    oz = stated_oz(name)
    draft = (category == "beer" and not _PACKAGE_WORDS.search(name)
             and (bool(_DRAFT_WORDS.search(name)) or (oz is not None and oz >= 10)))
    words = _words(_POS_NOISE.sub(" ", name))
    if not words:
        return {}
    want = variants(name)
    # Never across a draft / can boundary or a stated size (ING-20).
    pool = {k: (c[0], c[1]) for k, c in cands.items() if c[2] == category and c[4] == want
            and form_fits(name, category, c[3], c[5] if len(c) > 5 else None)}
    if category == "beer" and not draft and not _PACKAGE_WORDS.search(name):
        pool = {k: v for k, v in pool.items() if not cands[k][3]} or pool
    key, pname, score = _best_product(" ".join(sorted(words)), pool)
    if not key or score < 0.2:
        return {}
    return {"product_key": key, "product": pname, "oz": oz if (draft or category != "beer") else None}


def _spec_public(s, names=None) -> dict:
    out = {k: s[k] for k in ("id", "provider", "item_name", "item_id", "ingredient", "product_key", "oz",
                             "status", "recipe", "note")}
    out["product"] = (names or {}).get(s.get("product_key")) if s.get("product_key") else None
    return out


def _totals(items, cat_sales, mapped_sales) -> dict:
    """Per category: the rows' bought, theoretical and gap dollars (a row
    with no figure adds nothing, never a $0), and the matched share — the
    sales of drinks matched to a priced product over ALL the category's POS
    drink sales, set-aside ones included (BAR-CALC-05)."""
    by = {}
    for c in POUR_CATEGORIES:
        rows = [i for i in items if i["category"] == c]
        sales = cat_sales.get(c, 0.0)
        by[c] = {"items": len(rows),
                 "bought_usd": round(sum(i["bought_usd"] for i in rows), 2),
                 "theoretical_usd": round(sum(i["theoretical_usd"] for i in rows if i["theoretical_usd"] is not None), 2),
                 "gap_usd": round(sum(i["gap_usd"] for i in rows if i["gap_usd"] is not None), 2),
                 "comped_usd": round(sum(i["comped_usd"] for i in rows if i["comped_usd"] is not None), 2),
                 "pos_sales_usd": round(sales, 2),
                 "matched_sales_usd": round(mapped_sales.get(c, 0.0), 2),
                 "coverage_pct": round(mapped_sales.get(c, 0.0) / sales * 100, 1) if sales > 0 else None}
    return {"gap_usd": round(sum(v["gap_usd"] for v in by.values()), 2),
            "comped_usd": round(sum(v["comped_usd"] for v in by.values()), 2),
            "by_category": by}


def _waste_units(p, w):
    """A waste entry in the product's own units (ounces for a poured
    product, each otherwise), or None when it can't be converted."""
    cont = float((p.pack or {}).get("container_oz") or 0)
    keg_oz = float((p.pack or {}).get("keg_oz") or 0)
    q = float(w["qty"])
    if p.mode == "oz":
        if w["unit"] == "oz":
            return q
        if w["unit"] == "keg":
            return q * keg_oz if keg_oz else None
        return q * cont if cont else None
    if w["unit"] == "each":
        return q
    if w["unit"] == "oz" and cont:
        return q / cont
    return None


def _waste_by_product_day(restaurant_id, prods, start, end, db_path=None) -> list:
    """[(product key, date, units | None)] for every waste entry over the
    span on a product this computation knows."""
    from bar.waste import waste_rows
    links = {}
    try:
        from bar.products import product_links
        links = product_links(restaurant_id, db_path)
    except Exception:
        pass
    out = []
    for w in waste_rows(restaurant_id, start, end, db_path=db_path):
        k = links.get(w["product_key"], w["product_key"])
        p = prods.get(k) or prods.get(w["product_key"])
        if p is None:
            continue
        out.append((p.key, str(w["waste_date"])[:10], _waste_units(p, w)))
    return out


def _counted_variance(period, prods, per_of, cat_sales_day, matched_day, waste, span) -> dict:
    """Per category, from the counts: used (begin + bought − end) against
    what the POS says left the shelf — the theoretical cost of what was sold,
    plus what was comped and what was logged as waste, each priced at what
    the product cost (BAR-CALC-05) — every part over the category's OWN
    count span (BAR-CALC-14). The parts are shown beside the variance, never
    folded out of sight. Withheld below the coverage floor, the matched share
    measured over that same span against all the category's drink sales."""
    out = {}
    for c in POUR_CATEGORIES:
        pc = (period.get("categories") or {}).get(c) or {}
        used = pc.get("used")
        a, b = _cat_span(period, c) if used is not None else span
        sold = comped = waste_usd = 0.0
        uncosted = 0
        for p in prods.values():
            if p.category != c:
                continue
            per = per_of(p)
            if per is None:
                continue
            series = p.rung_oz_day if p.mode == "oz" else p.rung_each_day
            sold += per * sum(v for d, v in series["sale"].items() if a <= d <= b)
            comped += per * sum(v for d, v in series["comp"].items() if a <= d <= b)
        for key, d, units in waste:
            p = prods[key]
            if p.category != c or not (a <= d <= b):
                continue
            per = per_of(p)
            if units is None or per is None:
                uncosted += 1
                continue
            waste_usd += units * per
        sales = sum(v for d, v in cat_sales_day.get(c, {}).items() if a <= d <= b)
        matched = sum(v for d, v in matched_day.get(c, {}).items() if a <= d <= b)
        cov = round(matched / sales * 100, 1) if sales > 0 else None
        explained = sold + comped + waste_usd
        row = {"used_usd": used, "from": a, "to": b, "from_mdy": _mdy(a), "to_mdy": _mdy(b),
               "theoretical_usd": round(sold, 2), "comped_usd": round(comped, 2), "waste_usd": round(waste_usd, 2),
               "waste_uncosted": uncosted, "explained_usd": round(explained, 2),
               "pos_sales_usd": round(sales, 2), "unmatched_sales_usd": round(max(0.0, sales - matched), 2),
               "coverage_pct": cov, "variance_usd": None, "why": None}
        if used is None:
            row["why"] = "the counts didn't measure this category over the span"
        elif cov is None or cov < CATEGORY_COVERAGE_FLOOR * 100:
            row["why"] = (f"matched drinks carry {cov or 0:g}% of this category's POS drink sales; variance "
                          f"needs {int(CATEGORY_COVERAGE_FLOOR * 100)}%")
        else:
            row["variance_usd"] = round(used - explained, 2)
        out[c] = row
    return out


def _keg_yield(kegs, cfg) -> list:
    """Per keg product: pints rung per keg bought against the keg's own
    pints, with its assumption said."""
    out = []
    expected = float((cfg or {}).get("draft_yield_pct") or 90.0)
    for p, row in kegs:
        keg_oz = float((p.pack or {}).get("keg_oz") or 0)
        kegs_bought = p.bought_each
        if not keg_oz or kegs_bought <= 0:
            continue
        rung, comped = p.rung_oz["sale"], p.rung_oz["comp"]
        theo = keg_oz / PINT_OZ
        per_keg = rung / PINT_OZ / kegs_bought
        assumes = ["every keg bought in the span was poured in it — there are no keg counts yet"]
        if (p.pack or {}).get("size_source") == "assumed":
            assumes.append("assumes a 1/2 barrel")
        if "default_pour" in p.basis:
            assumes.append(f"assumes a {float((cfg or {}).get('draft_oz') or 16):g} oz draft pour")
        out.append({"product_key": p.key, "product": p.name, "keg_label": (p.pack or {}).get("keg_label"),
                    "kegs_bought": _r(kegs_bought, 2), "keg_oz": keg_oz, "pints_rung": _r(rung / PINT_OZ),
                    "pints_comped": _r(comped / PINT_OZ), "pints_per_keg": _r(per_keg),
                    "theoretical_pints_per_keg": _r(theo),
                    "yield_pct": _r((rung + comped) / (kegs_bought * keg_oz) * 100),
                    "expected_yield_pct": expected, "assumes": assumes})
    out.sort(key=lambda r: (r["yield_pct"] if r["yield_pct"] is not None else 999))
    return out


def attach_waste(result, restaurant_id, db_path=None) -> dict:
    """Waste logged over the span, beside each variance row (never netted)
    and as the `bar_ops.waste` block. Mutates and returns `result`."""
    from bar.waste import waste_block, waste_rows
    span = result.get("span")
    products = {k: {"name": p.name} for k, p in (result.get("products") or {}).items()}
    if not span:
        result["waste"] = waste_block(restaurant_id, None, None, products=products, db_path=db_path)
        result["waste"]["recent"] = waste_rows(restaurant_id, db_path=db_path, limit=20)
        return result
    rows = waste_rows(restaurant_id, span["from"], span["to"], db_path=db_path)
    prods = result.get("products") or {}
    links = {}
    try:
        from bar.products import product_links
        links = product_links(restaurant_id, db_path)
    except Exception:
        pass
    by = defaultdict(lambda: {"units": 0.0, "entries": 0})
    for w in rows:
        k = links.get(w["product_key"], w["product_key"])
        p = prods.get(k) or prods.get(w["product_key"])
        if p is None:
            continue
        n = _waste_units(p, w)
        b = by[p.key]
        b["entries"] += 1
        if n is not None and b["units"] is not None:
            b["units"] += n
        elif n is None:
            b["units"] = None
    for it in result["variance"].get("items") or []:
        b = by.get(it["product_key"])
        if b:
            it["waste_units"] = _r(b["units"]) if b["units"] is not None else None
            it["waste_entries"] = b["entries"]
    result["waste"] = waste_block(restaurant_id, span["from"], span["to"], products=products, db_path=db_path)
    return result


def bar_blocks(restaurant_id, today=None, db_path=None, lines=None, counted=None, cfg=None, mapping=None,
               sales=None, canon=None) -> dict:
    """{variance, bar_ops} for bar.build: the variance, and the bar's
    operations — waste, keg yield and (owner-only, filled at the route by
    bar.comps.for_viewer) comps by bartender shift."""
    try:
        res = compute(restaurant_id, today=today, db_path=db_path, lines=lines, counted=counted, cfg=cfg,
                      mapping=mapping, sales=sales, canon=canon)
        attach_waste(res, restaurant_id, db_path=db_path)
    except Exception:
        log.exception("[bar.variance] unread for %s", restaurant_id)
        return {"variance": {"available": False, "basis": None, "items": [], "unmapped": [],
                             "why": "Bought vs sold couldn't be read just now."},
                "bar_ops": {"waste": None, "keg_yield": [], "comps": {"owner_only": True}}}
    return {"variance": res["variance"], "drink_items": res.get("drink_items"),
            "bar_ops": {"waste": res.get("waste"), "keg_yield": res["keg_yield"],
                        # Owner-only: bar.comps.for_viewer fills it for the account holder.
                        "comps": {"owner_only": True}}}
