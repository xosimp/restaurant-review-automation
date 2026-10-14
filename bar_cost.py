"""
bar_cost.py — the Bar page (Food Cost → Bar): what the bar buys and what it
earns, from the distributor invoices Fintech sends (fintech_invoices) and
the nightly report's sales by category (dsr_metrics `sales.cat:*`).

Two reads, both measured, neither a model's:

- PRICE WATCH. Every product bought more than once, its price per unit of
  measure (a case, a keg, a bottle) over time, by distributor: the latest
  change, up or down, with what it does to the money at the quantities the
  bar actually buys. A lower price is said as "lower" — often a case deal or
  a different pack — never as a saving earned.

- POUR COST BY CATEGORY. What the bar bought from its distributors in a
  window against what the POS says it sold in the same category: beer &
  seltzer, liquor, wine. Purchase-based (no bar count), so it is read over
  four weeks and by month, where a delivery landing a day early evens out;
  the targets shown are industry rules of thumb, labelled as such. A
  window with no invoices or no sales says so instead of a 0%.

Every product is sorted into a category from its description, unit and
distributor; the owner corrects a guess once (`bar_product_categories`) and
it holds. Purchases sorted nowhere ("other") are counted apart and named,
never folded into a category.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import models as _models

CATEGORIES = ("beer", "liquor", "wine", "na", "supplies", "fee", "deposit", "other")
LABELS = {"beer": "Beer & seltzer", "liquor": "Liquor", "wine": "Wine", "na": "Non-alcoholic",
          "supplies": "Bar supplies", "fee": "Fees & taxes", "deposit": "Keg deposits & credits",
          "other": "Not sorted yet"}
# The categories pour cost is read for, and the industry rule-of-thumb range
# each is commonly held to (beer blends draft ~18-22% with package ~24-28%).
POUR_CATEGORIES = ("beer", "liquor", "wine")
RULE_OF_THUMB = {"beer": (20.0, 26.0), "liquor": (16.0, 22.0), "wine": (30.0, 40.0)}
WINDOW_DAYS = 28
PRICE_LOOKBACK_DAYS = 120

_UOM_WORD = {"CA": "case", "CS": "case", "KE": "keg", "KG": "keg", "BO": "bottle", "BL": "bottle", "BT": "bottle",
             "EA": "each", "DS": "deposit"}

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


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


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


def _mdy(iso) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return ""
    return f"{d.month}/{d.day}/{str(d.year)[2:]}"


def _lines(restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.distributor, i.invoice_number, i.invoice_date, l.item_code, l.description, l.uom, l.qty, "
            "l.unit_price, l.cost, l.deposit, l.kind FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id=l.invoice_id "
            "WHERE i.restaurant_id=? AND i.invoice_date IS NOT NULL ORDER BY i.invoice_date, i.id, l.line_no",
            (int(restaurant_id),)).fetchall()
        overrides = {r["product_key"]: r["category"] for r in conn.execute(
            "SELECT product_key, category FROM bar_product_categories WHERE restaurant_id=?", (int(restaurant_id),))}
    finally:
        conn.close()
    out = []
    # A per-unit charge line ("charge") belongs to the item beside it: it
    # takes that item's category, never one of its own.
    seen_cat = {}
    for r in rows:
        r = dict(r)
        key = product_key(r["distributor"], r["item_code"], r["description"])
        cat, sure = classify(r["distributor"], r["description"], r["uom"], r["kind"])
        if r["kind"] == "charge" and key in seen_cat:
            cat, sure = seen_cat[key]
        if key in overrides and r["kind"] not in ("fee", "adjustment", "keg_credit") and r["uom"] != "DS":
            cat, sure = overrides[key], True
        if r["kind"] == "item":
            seen_cat[key] = (cat, sure)
        # A keg's $30 deposit inside its cost comes back with the empty: it is
        # never counted as what the beer cost.
        net = float(r["cost"] or 0) - (float(r["deposit"] or 0) if r["kind"] == "item" else 0.0)
        r.update(product_key=key, category=cat, sure=sure, owner_set=key in overrides, net=net)
        out.append(r)
    return out


def _sales(restaurant_id, db_path=None) -> dict:
    """{category: {date: sales}} from the nightly reports' sales by category."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
                            "AND metric LIKE 'sales.cat:%' AND value IS NOT NULL", (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    out = defaultdict(dict)
    for r in rows:
        name = str(r["metric"]).split(":", 1)[1].lower()
        if "beer" in name or "seltzer" in name:
            cat = "beer"
        elif "liquor" in name or "spirit" in name or "cocktail" in name:
            cat = "liquor"
        elif "wine" in name:
            cat = "wine"
        else:
            continue
        d = str(r["business_date"])[:10]
        out[cat][d] = out[cat].get(d, 0.0) + float(r["value"] or 0)
    return out


def _window(lines, sales, start, end) -> dict:
    """{cat: {purchases, sales, pct, days_with_sales}} for [start, end]."""
    out = {}
    for cat in POUR_CATEGORIES:
        bought = sum(l["net"] for l in lines
                     if l["category"] == cat and start <= str(l["invoice_date"])[:10] <= end)
        sold_days = {d: v for d, v in (sales.get(cat) or {}).items() if start <= d <= end}
        sold = sum(sold_days.values())
        pct = round(bought / sold * 100, 1) if sold > 0 and bought > 0 else None
        lo, hi = RULE_OF_THUMB[cat]
        tone = None if pct is None else ("good" if lo <= pct <= hi else ("warn" if pct > hi else "low"))
        out[cat] = {"key": cat, "label": LABELS[cat], "purchases": round(bought, 2), "sales": round(sold, 2),
                    "pct": pct, "tone": tone, "range": [lo, hi], "days_with_sales": len(sold_days)}
    return out


def _gaps(lines, start, end) -> list:
    """Distributors whose invoices stop inside [start, end] for much longer
    than their own rhythm: [{distributor, from, to, from_mdy, to_mdy,
    categories}]. Simple EJ's history came from Fintech's payment-activity
    export (10/14/26) — only invoices already PAID — so the spirits and wine
    houses, paid about 30 days out, were missing their last weeks and liquor
    read 7%. A window with a gap is said, never judged."""
    by = defaultdict(set)
    cats = defaultdict(lambda: defaultdict(float))
    for l in lines:
        by[l["distributor"]].add(str(l["invoice_date"])[:10])
        if l["category"] in POUR_CATEGORIES:
            cats[l["distributor"]][l["category"]] += abs(l["net"])
    out = []
    for dist, days in by.items():
        ds = sorted(days)
        if len(ds) < 4:
            continue
        steps = sorted((date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ds, ds[1:]))
        typical = steps[len(steps) // 2] or 1
        limit = max(14, int(typical * 2.5))
        inside = [d for d in ds if start <= d <= end]
        before = [d for d in ds if d < start]
        marks = ([before[-1]] if before else [start]) + inside + [end]
        for a, b in zip(marks, marks[1:]):
            gap = (date.fromisoformat(b) - date.fromisoformat(a)).days
            if gap > limit:
                lo = max(start, (date.fromisoformat(a) + timedelta(days=1)).isoformat())
                hi = (date.fromisoformat(b) - timedelta(days=1)).isoformat() if b != end else end
                mine = sorted(cats[dist], key=lambda c: -cats[dist][c])
                out.append({"distributor": dist, "from": lo, "to": hi, "from_mdy": _mdy(lo), "to_mdy": _mdy(hi),
                            "categories": [c for c in mine if cats[dist][c] > 0], "typical_days": typical})
    return out


def _pour_cost(lines, sales, today) -> dict:
    inv_dates = sorted({str(l["invoice_date"])[:10] for l in lines})
    sale_dates = sorted({d for c in sales.values() for d in c})
    if not inv_dates or not sale_dates:
        return {"available": False,
                "why_not": ("No distributor invoices on file yet." if not inv_dates
                            else "No sales by category on file yet — they come from the nightly report.")}
    lo = max(inv_dates[0], sale_dates[0])
    hi = min(inv_dates[-1], sale_dates[-1])
    if lo > hi:
        return {"available": False, "why_not": "The invoices and the sales on file don't overlap yet."}
    end = hi
    start = max(lo, (date.fromisoformat(end) - timedelta(days=WINDOW_DAYS - 1)).isoformat())
    cur = _window(lines, sales, start, end)
    gaps = _gaps(lines, start, end)
    for g in gaps:
        for c in g["categories"]:
            if c in cur:
                cur[c]["incomplete"] = True
                cur[c]["tone"] = None
    bought = sum(c["purchases"] for c in cur.values())
    sold = sum(c["sales"] for c in cur.values())
    months = []
    m = date.fromisoformat(lo).replace(day=1)
    while m.isoformat() <= hi:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        a, b = max(lo, m.isoformat()), min(hi, (nxt - timedelta(days=1)).isoformat())
        w = _window(lines, sales, a, b)
        for g in _gaps(lines, a, b):
            for c in g["categories"]:
                if c in w:
                    w[c]["incomplete"] = True
                    w[c]["tone"] = None
        full = a == m.isoformat() and b == (nxt - timedelta(days=1)).isoformat()
        months.append({"month": m.strftime("%Y-%m"), "label": m.strftime("%b %Y"), "from": a, "to": b,
                       "partial": not full, "from_mdy": _mdy(a), "to_mdy": _mdy(b),
                       "categories": [w[c] for c in POUR_CATEGORIES]})
        m = nxt
    return {"available": True, "window": {"from": start, "to": end, "from_mdy": _mdy(start), "to_mdy": _mdy(end),
                                          "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1},
            "categories": [cur[c] for c in POUR_CATEGORIES],
            "total": {"purchases": round(bought, 2), "sales": round(sold, 2),
                      "pct": round(bought / sold * 100, 1) if sold > 0 and bought > 0 else None},
            "months": months, "overlap": {"from_mdy": _mdy(lo), "to_mdy": _mdy(hi)},
            "gaps": gaps, "complete": not gaps}


def _price_watch(lines, today) -> dict:
    series = defaultdict(list)
    meta = {}
    for l in lines:
        if l["kind"] != "item" or not l["unit_price"] or float(l["unit_price"]) < 1 or float(l["qty"] or 0) <= 0:
            continue
        k = (l["product_key"], str(l["uom"] or ""))
        series[k].append((str(l["invoice_date"])[:10], float(l["unit_price"]), float(l["qty"] or 0)))
        meta[k] = l
    cutoff = (today - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
    since90 = (today - timedelta(days=90)).isoformat()
    ups, downs, repeat = [], [], 0
    for k, pts in series.items():
        if len(pts) < 2:
            continue
        repeat += 1
        # The latest price against the last different one before it.
        last_d, last_p, _q = pts[-1]
        prev = next(((d, p) for d, p, _ in reversed(pts[:-1]) if abs(p - last_p) >= 0.01), None)
        if not prev:
            continue
        # The day the current price started: the first purchase at it after the previous price.
        since = last_d
        for d, p, _q in reversed(pts):
            if abs(p - last_p) >= 0.01:
                break
            since = d
        if since < cutoff:
            continue
        l = meta[k]
        change = round(last_p - prev[1], 2)
        qty90 = sum(q for d, p, q in pts if d >= since90)
        row = {"product_key": l["product_key"], "product": l["description"], "distributor": l["distributor"],
               "uom": l["uom"], "unit": _UOM_WORD.get(str(l["uom"] or "").upper(), "unit"),
               "category": l["category"], "category_label": LABELS.get(l["category"], ""),
               "from": round(prev[1], 2), "to": round(last_p, 2), "change": change,
               "pct": round(change / prev[1] * 100, 1) if prev[1] else None,
               "since": since, "since_mdy": _mdy(since), "was_since_mdy": _mdy(prev[0]),
               "bought_90d": round(qty90, 2), "impact_90d": round(change * qty90, 2)}
        (ups if change > 0 else downs).append(row)
    ups.sort(key=lambda r: (-(r["impact_90d"] or 0), -(r["pct"] or 0)))
    downs.sort(key=lambda r: (r["impact_90d"] or 0, r["pct"] or 0))
    return {"increases": ups, "decreases": downs, "products_repeat": repeat,
            "lookback_days": PRICE_LOOKBACK_DAYS}


def _distributors(lines) -> list:
    by = defaultdict(lambda: {"spend": 0.0, "fees": 0.0, "invoices": set(), "deposits": 0.0})
    for l in lines:
        b = by[l["distributor"]]
        b["invoices"].add(l["invoice_number"])
        if l["category"] == "fee":
            b["fees"] += float(l["cost"] or 0)
        elif l["category"] == "deposit":
            b["deposits"] += float(l["cost"] or 0)
        else:
            b["spend"] += l["net"]
    total = sum(v["spend"] for v in by.values()) or 0
    out = [{"distributor": k, "spend": round(v["spend"], 2), "fees": round(v["fees"], 2),
            "deposits": round(v["deposits"], 2), "invoices": len(v["invoices"]),
            "share": round(v["spend"] / total * 100, 1) if total else None} for k, v in by.items()]
    return sorted(out, key=lambda r: -r["spend"])


def _products(lines) -> list:
    by = {}
    for l in lines:
        if l["kind"] not in ("item", "charge"):
            continue
        p = by.setdefault(l["product_key"], {"product_key": l["product_key"], "product": l["description"],
                                             "distributor": l["distributor"], "uom": l["uom"],
                                             "category": l["category"], "sure": l["sure"],
                                             "owner_set": l["owner_set"], "spend": 0.0, "last": ""})
        p["spend"] += l["net"]
        p["last"] = max(p["last"], str(l["invoice_date"])[:10])
        if l["kind"] == "item":
            p["category"], p["sure"], p["owner_set"] = l["category"], l["sure"], l["owner_set"]
    out = list(by.values())
    for p in out:
        p["spend"] = round(p["spend"], 2)
        p["category_label"] = LABELS.get(p["category"], "")
    # The ones a person should look at first: not sorted, then guessed, by spend.
    out.sort(key=lambda p: (p["category"] != "other", p["sure"] or p["owner_set"], -p["spend"]))
    return out


def build(restaurant_id, db_path=None, today=None) -> dict:
    """The Bar page's payload."""
    today = today or date.today()
    lines = _lines(restaurant_id, db_path)
    if not lines:
        return {"ok": True, "available": False,
                "why_not": "No distributor invoices yet. Once Fintech's daily file reaches Cavnar AI, "
                           "the bar's prices and pour cost fill in here."}
    sales = _sales(restaurant_id, db_path)
    inv_dates = sorted({str(l["invoice_date"])[:10] for l in lines})
    products = _products(lines)
    other_spend = round(sum(p["spend"] for p in products if p["category"] == "other"), 2)
    return {"ok": True, "available": True,
            "as_of": inv_dates[-1], "as_of_mdy": _mdy(inv_dates[-1]),
            "invoices": {"from_mdy": _mdy(inv_dates[0]), "to_mdy": _mdy(inv_dates[-1]),
                         "count": len({(l["distributor"], l["invoice_number"]) for l in lines})},
            "pour_cost": _pour_cost(lines, sales, today),
            "price_watch": _price_watch(lines, today),
            "distributors": _distributors(lines),
            "products": products,
            "to_check": sum(1 for p in products if not (p["sure"] or p["owner_set"])),
            "other_spend": other_spend,
            "categories": [{"key": c, "label": LABELS[c]} for c in ("beer", "liquor", "wine", "na", "supplies", "other")],
            "basis": "Purchases are Fintech's distributor invoices; sales are the nightly report's sales by "
                     "category. Pour cost here is purchase-based (no bar count), read over four weeks and by "
                     "month; the ranges are industry rules of thumb."}
