"""
bar/products.py — one bought product, whatever its distributor calls it
(F3, 10/15/26): the canonical product behind its codes, its pack and size,
and what it costs per bottle, per ounce and per pour. Also the owner's own
word on each of those, the ordering rhythm per product (F13), and the
tables that hold the owner's settings (created at boot by
init_bar_products, called from init_bar_cost).

- A UPC joins two item codes into one product only when its check digit is
  valid and it is not a placeholder (Euclid prints 111111111117 on kegs,
  empties and glassware; "<null>" and all-zero codes are no code at all).
  A code change under one UPC (a gift box, a new vintage) continues the
  product's history; the price watch marks it instead of reading a change.
- The pack parser reads the shapes EJ's distributors print — 6B, 24NR,
  6/4PK, 4/6/12, 12/750, C24, 750ML, 1.0L, 8.4Z, 12OZ, and kegs (1/2 BBL =
  1,984 oz, 1/4 = 992, 1/6 = 661, 50 L = 1,690.7). A 2-digit number after a
  wine's name is its vintage and after a spirit's its proof, never a pack.
  What the description does not say is assumed and says so (a spirits case
  is 12 unless it says 6B; a beer case 24), and the confidence falls.
- Per pour uses the owner's pour sizes (bar_settings; defaults 1.5 oz
  spirits, 5 oz wine, 16 oz draft at a stated 90% keg yield).
"""
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import median

from bar.common import _UOM_WORD, _mdy, display_name, get_conn

# ── settings (the owner's) ─────────────────────────────────────────────────

DEFAULT_POUR = {"liquor_oz": 1.5, "wine_oz": 5.0, "draft_oz": 16.0, "draft_yield_pct": 90.0}
_POUR_BOUNDS = {"liquor_oz": (0.25, 6.0), "wine_oz": (1.0, 12.0), "draft_oz": (4.0, 40.0),
                "draft_yield_pct": (50.0, 100.0)}


def init_bar_products(db_path=None):
    """The owner's word on bar products and pours. Boot only (called from
    bar.classify.init_bar_cost, which models.init_db calls)."""
    conn = get_conn(db_path)
    try:
        # One row per restaurant: whether mixers count as liquor (Decision 3)
        # and the pour sizes per-pour cost uses.
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_settings (
            restaurant_id INTEGER PRIMARY KEY,
            count_mixers INTEGER NOT NULL DEFAULT 0,
            liquor_oz REAL,
            wine_oz REAL,
            draft_oz REAL,
            draft_yield_pct REAL,
            set_by INTEGER,
            set_at TEXT NOT NULL)""")
        # The owner's pack for a product the description doesn't spell out.
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_product_packs (
            restaurant_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            units_per_case INTEGER,
            container_ml REAL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, product_key))""")
        # Two codes the owner says are one product (no shared UPC).
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_product_links (
            restaurant_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            same_as TEXT NOT NULL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, product_key))""")
        # The owner's deal sheet: the price a product should be billed at from
        # a quantity (F8). The learned ladder fills in where it is silent.
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_expected_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            product_key TEXT NOT NULL,
            min_qty REAL NOT NULL,
            price REAL NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            note TEXT,
            set_by INTEGER,
            set_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bar_expected_prices_rid "
                     "ON bar_expected_prices(restaurant_id, product_key)")
        # A POS menu item the owner confirmed is one bought product poured at
        # a size (F11): the drink margin check reads only confirmed rows.
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_drink_map (
            restaurant_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            item_id TEXT NOT NULL,
            product_key TEXT NOT NULL,
            pour_oz REAL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, provider, item_id))""")
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def settings(restaurant_id, db_path=None) -> dict:
    """{count_mixers, liquor_oz, wine_oz, draft_oz, draft_yield_pct, set}
    — the defaults where the owner set nothing."""
    out = dict(DEFAULT_POUR, count_mixers=False, set=False)
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT count_mixers, liquor_oz, wine_oz, draft_oz, draft_yield_pct FROM bar_settings "
                         "WHERE restaurant_id=?", (int(restaurant_id),)).fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    if r:
        out["set"] = True
        out["count_mixers"] = bool(r["count_mixers"])
        for k in DEFAULT_POUR:
            if r[k] is not None:
                out[k] = float(r[k])
    return out


def set_settings(restaurant_id, user_id=None, db_path=None, **fields) -> dict:
    """The owner's switch and pour sizes. Any subset; None or '' puts a pour
    size back to its default. Raises ValueError outside the bounds."""
    cur = settings(restaurant_id, db_path)
    row = {"count_mixers": int(bool(cur["count_mixers"]))}
    for k in DEFAULT_POUR:
        row[k] = cur[k] if cur[k] != DEFAULT_POUR[k] else None
    for k, v in fields.items():
        if k == "count_mixers":
            row[k] = int(bool(v))
        elif k in DEFAULT_POUR:
            if v is None or str(v).strip() == "":
                row[k] = None
                continue
            n = float(v)
            lo, hi = _POUR_BOUNDS[k]
            if not lo <= n <= hi:
                raise ValueError(f"Pick a value between {lo:g} and {hi:g}.")
            row[k] = n
        else:
            raise ValueError(f"Unknown bar setting {k}.")
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO bar_settings (restaurant_id, count_mixers, liquor_oz, wine_oz, draft_oz, "
                     "draft_yield_pct, set_by, set_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id) DO UPDATE "
                     "SET count_mixers=excluded.count_mixers, liquor_oz=excluded.liquor_oz, wine_oz=excluded.wine_oz, "
                     "draft_oz=excluded.draft_oz, draft_yield_pct=excluded.draft_yield_pct, set_by=excluded.set_by, "
                     "set_at=excluded.set_at",
                     (int(restaurant_id), row["count_mixers"], row["liquor_oz"], row["wine_oz"], row["draft_oz"],
                      row["draft_yield_pct"], user_id, _now()))
        conn.commit()
    finally:
        conn.close()
    return dict(settings(restaurant_id, db_path), ok=True)


def pack_overrides(restaurant_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT product_key, units_per_case, container_ml FROM bar_product_packs "
                            "WHERE restaurant_id=?", (int(restaurant_id),)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {r["product_key"]: {"units_per_case": r["units_per_case"], "container_ml": r["container_ml"]} for r in rows}


def set_pack(restaurant_id, key, units_per_case=None, container_ml=None, user_id=None, db_path=None) -> dict:
    """The owner's pack for one product; both empty clears it back to the parser."""
    u = int(units_per_case) if units_per_case not in (None, "") else None
    ml = float(container_ml) if container_ml not in (None, "") else None
    if u is not None and not 1 <= u <= 120:
        raise ValueError("A case holds between 1 and 120.")
    if ml is not None and not 50 <= ml <= 60000:
        raise ValueError("A container is between 50 ml and 60 L.")
    conn = get_conn(db_path)
    try:
        if u is None and ml is None:
            conn.execute("DELETE FROM bar_product_packs WHERE restaurant_id=? AND product_key=?",
                         (int(restaurant_id), str(key)))
        else:
            conn.execute("INSERT INTO bar_product_packs (restaurant_id, product_key, units_per_case, container_ml, "
                         "set_by, set_at) VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, product_key) DO UPDATE SET "
                         "units_per_case=excluded.units_per_case, container_ml=excluded.container_ml, "
                         "set_by=excluded.set_by, set_at=excluded.set_at",
                         (int(restaurant_id), str(key), u, ml, user_id, _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "product_key": key, "units_per_case": u, "container_ml": ml}


def product_links(restaurant_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT product_key, same_as FROM bar_product_links WHERE restaurant_id=?",
                            (int(restaurant_id),)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {r["product_key"]: r["same_as"] for r in rows}


def link_products(restaurant_id, key, same_as, user_id=None, db_path=None) -> dict:
    """The owner's word that two codes are one product ('' unlinks)."""
    conn = get_conn(db_path)
    try:
        if not same_as:
            conn.execute("DELETE FROM bar_product_links WHERE restaurant_id=? AND product_key=?",
                         (int(restaurant_id), str(key)))
        else:
            if str(same_as) == str(key):
                raise ValueError("A product is already itself.")
            conn.execute("INSERT INTO bar_product_links (restaurant_id, product_key, same_as, set_by, set_at) "
                         "VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, product_key) DO UPDATE SET "
                         "same_as=excluded.same_as, set_by=excluded.set_by, set_at=excluded.set_at",
                         (int(restaurant_id), str(key), str(same_as), user_id, _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "product_key": key, "same_as": same_as or None}


def deal_sheet(restaurant_id, db_path=None) -> list:
    """The owner's expected prices: [{id, product_key, min_qty, price, valid_from, valid_to, note}]."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, product_key, min_qty, price, valid_from, valid_to, note FROM bar_expected_prices "
                            "WHERE restaurant_id=? ORDER BY product_key, min_qty", (int(restaurant_id),)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _iso(d):
    if d in (None, ""):
        return None
    return date.fromisoformat(str(d)[:10]).isoformat()


def set_expected_price(restaurant_id, key, min_qty, price, valid_from=None, valid_to=None, note=None,
                       user_id=None, db_path=None) -> dict:
    """One line of the owner's deal sheet: from `min_qty` units, `price` a unit."""
    q, p = float(min_qty), float(price)
    if not 1 <= q <= 1000:
        raise ValueError("The quantity is between 1 and 1,000.")
    if not 0 < p <= 100000:
        raise ValueError("Enter the price a unit.")
    vf, vt = _iso(valid_from), _iso(valid_to)
    if vf and vt and vt < vf:
        raise ValueError("The deal ends before it starts.")
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO bar_expected_prices (restaurant_id, product_key, min_qty, price, valid_from, "
                           "valid_to, note, set_by, set_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
                           (int(restaurant_id), str(key), q, p, vf, vt, (str(note)[:200] if note else None),
                            user_id, _now()))
        new_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": new_id, "product_key": key, "min_qty": q, "price": p}


def clear_expected_price(restaurant_id, row_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM bar_expected_prices WHERE restaurant_id=? AND id=?", (int(restaurant_id), int(row_id)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": int(row_id)}


# ── identity: UPC and the canonical product ────────────────────────────────

# Codes that are no code: Euclid's placeholder on kegs, empties and glassware.
_PLACEHOLDER_GTINS = {"00111111111117"}


def valid_gtin(code):
    """A UPC/EAN/GTIN as 14 digits when its check digit is valid and it is
    not a placeholder, else None ('<null>', blanks, Euclid's 111111111117,
    a run of one digit, mostly zeros)."""
    s = re.sub(r"\D", "", str(code or ""))
    if len(s) not in (8, 12, 13, 14):
        return None
    g = s.zfill(14)
    if g in _PLACEHOLDER_GTINS or len(set(g.lstrip("0"))) <= 1 or len(g.lstrip("0")) < 7:
        return None
    digits = [int(c) for c in g]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits[:13]))
    return g if (10 - total % 10) % 10 == digits[13] else None


def canonical_keys(lines, links=None) -> dict:
    """product_key → the canonical product's key: the most recently bought
    product_key among the codes one valid UPC (or the owner's link) joins,
    within one distributor. A product with one code keeps its own key."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    last = {}
    for l in lines:
        k = l["product_key"]
        find(k)
        d = str(l.get("invoice_date") or "")[:10]
        last[k] = max(last.get(k, ""), d)
        g = valid_gtin(l.get("upc"))
        if g and l.get("kind") in ("item", "charge") and l.get("category") not in ("fee", "deposit"):
            union("upc:" + str(l.get("distributor") or "").strip().lower() + "|" + g, k)
    for k, other in (links or {}).items():
        union(other, k)
    groups = defaultdict(list)
    for k in last:
        groups[find(k)].append(k)
    out = {}
    for members in groups.values():
        rep = max(members, key=lambda k: (last.get(k, ""), k))
        for k in members:
            out[k] = rep
    return out


# ── the pack parser ────────────────────────────────────────────────────────

_ML_PER_OZ = 29.5735
_KEGS = ((re.compile(r"\b1/2\s*(?:BBL|BL|BB|BARREL)|\b1/2BB|\bK\s+1/2\b|\bHALF\s+BARREL\b"), 1984.0, "1/2 barrel"),
         (re.compile(r"\b1/4\s*(?:BBL|BL|BB|BARREL)|\b1/4BB|\bK\s+1/4\b|\bPONY\b"), 992.0, "1/4 barrel"),
         (re.compile(r"\b1/6\s*(?:BBL|BL|BB|BARREL)|\b1/6BB|\bK\s+1/6\b|\bSIXTEL\b"), 661.0, "1/6 barrel"),
         (re.compile(r"\b50\s*L\b|\bK\s+50L\b"), 1690.7, "50 L"),
         (re.compile(r"\b30\s*L\b"), 1014.4, "30 L"),
         (re.compile(r"\b20\s*L\b"), 676.3, "20 L"))
_SIZE_RE = re.compile(r"(?<![\d/.])(\d+(?:\.\d+)?)\s*(ML|M|LTR|L|OZ|Z)\b")
_TRIPLE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d+(?:\.\d+)?)\b")
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d+(?:\.\d+)?)(?:PK)?\b(?!\s*(?:BBL|BL|BB|BARREL)\b)")
_B_RE = re.compile(r"\b(\d{1,2})B\b")
_C_RE = re.compile(r"\bC(\d{1,2})\b")
_COUNT_RE = re.compile(r"\b(\d{1,2})\s*(NR|CN|CANS?|LN|BTLS?|BOTTLES?|PK|PACK|LP)\b")
_TWO_DIGIT_RE = re.compile(r"(?<![\d/.$])\b(\d{2})\b(?!\s*(?:ML|M|L|LTR|OZ|Z|PK|NR|CN|B)\b)(?![/.]\d)")


def parse_pack(description, uom=None, category=None) -> dict:
    """What one purchase unit holds: {units_per_case, units_source
    parsed|assumed|none, container_ml, container_oz, size_source
    parsed|assumed|none, keg_oz, keg_label, vintage, proof, each
    bottle|can|keg|unit}. Never a guess presented as read: what is assumed
    says so."""
    d = " " + re.sub(r"\s+", " ", str(description or "").upper()) + " "
    u = str(uom or "").upper()
    cat = str(category or "")
    out = {"units_per_case": None, "units_source": "none", "container_ml": None, "container_oz": None,
           "size_source": "none", "keg_oz": None, "keg_label": None, "vintage": None, "proof": None, "each": "unit"}
    for rx, oz, label in _KEGS:
        if rx.search(d) and (u in ("KE", "KG", "") or "BBL" in d or "BARREL" in d or u == "CA" and cat == "beer"):
            out.update(keg_oz=oz, keg_label=label, each="keg", container_oz=oz,
                       container_ml=round(oz * _ML_PER_OZ, 1), size_source="parsed", units_per_case=1,
                       units_source="parsed")
            return out
    if u in ("KE", "KG"):
        out.update(each="keg", keg_oz=1984.0, keg_label="1/2 barrel", container_oz=1984.0,
                   container_ml=round(1984.0 * _ML_PER_OZ, 1), size_source="assumed", units_per_case=1,
                   units_source="assumed")
        return out
    count = None
    m = _TRIPLE_RE.search(d)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), float(m.group(3))
        count = a * b
        if c < 50:
            out.update(container_oz=c, container_ml=round(c * _ML_PER_OZ, 1), size_source="parsed")
        else:
            out.update(container_ml=c, container_oz=round(c / _ML_PER_OZ, 2), size_source="parsed")
    if count is None:
        m = _B_RE.search(d) or _C_RE.search(d)
        if m:
            count = int(m.group(1))
    if count is None:
        for m in _SLASH_RE.finditer(d):
            a, b = int(m.group(1)), float(m.group(2))
            if a <= 1:
                continue  # 1/2, 1/4, 1/6: a keg's fraction, not a pack
            if b >= 50:  # 12/750: twelve 750 ml bottles
                count = a
                out.update(container_ml=b, container_oz=round(b / _ML_PER_OZ, 2), size_source="parsed")
            elif 12 <= a * b <= 48:  # 4/6, 6/4PK, 2/12: packs to a case
                count = int(a * b)
            elif a >= 12:  # 24/12: twenty-four 12 oz
                count = a
                out.update(container_oz=b, container_ml=round(b * _ML_PER_OZ, 1), size_source="parsed")
            if count:
                break
    if count is None:
        m = _COUNT_RE.search(d)
        if m:
            n = int(m.group(1))
            if n >= 15:
                count = n
            elif u in ("CA", "CS") and n in (4, 6, 8, 12):
                out["units_per_case"] = 24
                out["units_source"] = "assumed"  # a 6NR / 12NR case is four sixes or two twelves
    if count:
        out["units_per_case"], out["units_source"] = count, "parsed"
    if out["container_ml"] is None:
        for m in _SIZE_RE.finditer(d):
            n, unit = float(m.group(1)), m.group(2)
            if unit in ("ML",) or (unit == "M" and n >= 50):
                ml = n
            elif unit in ("L", "LTR") and n <= 20:
                ml = n * 1000
            elif unit in ("OZ", "Z") and n <= 128:
                ml = n * _ML_PER_OZ
            else:
                continue
            out.update(container_ml=round(ml, 1), container_oz=round(ml / _ML_PER_OZ, 2), size_source="parsed")
            break
    # A 2-digit number left over: a wine's vintage, a spirit's proof.
    for m in _TWO_DIGIT_RE.finditer(d):
        n = int(m.group(1))
        if cat == "wine" and out["vintage"] is None and 0 <= n <= 40:
            out["vintage"] = 2000 + n
        elif cat == "liquor" and out["proof"] is None and 30 <= n <= 160:
            out["proof"] = n
    is_case = u in ("CA", "CS")
    if cat == "beer" or "NR" in d or " CAN" in d:
        out["each"] = "can" if re.search(r"\bCN\b|\bCANS?\b|\d+CN\b", d) else "bottle"
    elif cat in ("liquor", "wine"):
        out["each"] = "bottle"
    if is_case and out["units_per_case"] is None:
        if cat == "beer":
            out["units_per_case"], out["units_source"] = 24, "assumed"
        elif cat in ("liquor", "wine"):
            ml = out["container_ml"] or 0
            out["units_per_case"] = 6 if ml >= 1700 else (24 if 0 < ml <= 400 else 12)
            out["units_source"] = "assumed"
    if not is_case:
        out["units_per_case"], out["units_source"] = 1, "parsed"
    if out["container_ml"] is None and cat == "beer" and out["each"] in ("can", "bottle"):
        out.update(container_ml=round(12 * _ML_PER_OZ, 1), container_oz=12.0, size_source="assumed")
    return out


def pour_for(category, pack, pour=None) -> tuple:
    """(ounces in one pour, label, yield) for a product: a spirit's or wine's
    pour, a keg's draft pour at its yield, a packaged beer's whole can or
    bottle (None ounces)."""
    p = dict(DEFAULT_POUR, **(pour or {}))
    if pack.get("each") == "keg":
        return p["draft_oz"], f"{p['draft_oz']:g} oz draft", p["draft_yield_pct"]
    if category == "liquor":
        return p["liquor_oz"], f"{p['liquor_oz']:g} oz pour", 100.0
    if category == "wine":
        return p["wine_oz"], f"{p['wine_oz']:g} oz glass", 100.0
    return None, "one " + (pack.get("each") or "unit"), 100.0


def unit_costs(price, uom, description, category, override=None, pour=None) -> dict:
    """What one purchase unit's price comes to: {each, per_each, per_bottle,
    per_oz, per_pour, pour_oz, pour_label, units_per_case, container_oz,
    confidence high|medium|low, assumes [..]} or None for a non-product."""
    if price in (None, "") or float(price) <= 0 or category in ("fee", "deposit", "supplies", "other"):
        return None
    pk = parse_pack(description, uom, category)
    if override:
        if override.get("units_per_case"):
            pk.update(units_per_case=int(override["units_per_case"]), units_source="owner")
        if override.get("container_ml"):
            ml = float(override["container_ml"])
            pk.update(container_ml=ml, container_oz=round(ml / _ML_PER_OZ, 2), size_source="owner")
    price = float(price)
    units = pk["units_per_case"] or 1
    per_each = price / units
    assumes = []
    if pk["units_source"] == "assumed" and str(uom or "").upper() in ("CA", "CS"):
        assumes.append(f"assumes {units} a case")
    if pk["size_source"] == "assumed":
        assumes.append(("assumes a 1/2 barrel" if pk["each"] == "keg" else f"assumes {pk['container_oz']:g} oz"))
    oz = pk["container_oz"]
    # Ounces from the exact millilitres: 750 ml is 25.36 oz, never 25.36 × a rounding.
    per_oz = per_each / (pk["container_ml"] / _ML_PER_OZ) if pk["container_ml"] else (per_each / oz if oz else None)
    pour_oz, pour_label, yld = pour_for(category, pk, pour)
    if pour_oz and per_oz is not None:
        per_pour = per_oz * pour_oz / (yld / 100.0)
        if pk["each"] == "keg":
            assumes.append(f"{yld:g}% of the keg poured")
    elif pour_oz is None:
        per_pour = per_each  # a can or bottle sold as it comes
    else:
        per_pour = None
    sources = (pk["units_source"], pk["size_source"])
    if "none" in sources or oz is None:
        conf = "low"
    elif "assumed" in sources:
        conf = "medium"
    else:
        conf = "high"
    return {"each": pk["each"], "per_each": round(per_each, 2),
            "per_bottle": round(per_each, 2) if pk["each"] in ("bottle", "can") else None,
            "per_oz": round(per_oz, 3) if per_oz is not None else None,
            "per_pour": round(per_pour, 4) if per_pour is not None else None,
            "pour_oz": pour_oz, "pour_label": pour_label, "units_per_case": units,
            "container_oz": oz, "vintage": pk["vintage"], "proof": pk["proof"],
            "confidence": conf, "assumes": assumes}


# ── ordering rhythm (F13) ──────────────────────────────────────────────────

REORDER_MIN_PURCHASES = 4


def reorder_hints(lines, today, canon=None, limit=10) -> list:
    """Products bought on a steady rhythm that are past it: [{product_key,
    name, distributor, usual_every_days, last_bought_mdy, days_since,
    overdue_days, usual_qty, purchases}] — an input for an insight ("usually
    reordered by now"), most overdue first. Needs 4 purchases on a rhythm
    (gaps within 2.5× their median); past it once the gap is 1.5× the
    median and a week."""
    canon = canon or {}
    by = defaultdict(lambda: {"dates": defaultdict(float), "line": None})
    for l in lines:
        if l.get("kind") != "item" or float(l.get("qty") or 0) <= 0 or float(l.get("unit_price") or 0) < 1:
            continue
        if l.get("category") in ("fee", "deposit", "supplies", "other"):
            continue
        k = canon.get(l["product_key"], l["product_key"])
        b = by[k]
        b["dates"][str(l["invoice_date"])[:10]] += float(l["qty"] or 0)
        b["line"] = l
    out = []
    for k, b in by.items():
        ds = sorted(b["dates"])
        if len(ds) < REORDER_MIN_PURCHASES:
            continue
        dd = [date.fromisoformat(d) for d in ds]
        gaps = [(dd[i + 1] - dd[i]).days for i in range(len(dd) - 1)]
        m = median(gaps)
        if m <= 0 or max(gaps) > 2.5 * m:
            continue
        since = (today - dd[-1]).days
        due = max(1.5 * m, m + 7)
        if since <= due:
            continue
        l = b["line"]
        out.append({"product_key": l["product_key"], "name": display_name(l["description"]),
                    "distributor": l["distributor"], "usual_every_days": round(m, 1),
                    "last_bought": ds[-1], "last_bought_mdy": _mdy(ds[-1]), "days_since": since,
                    "overdue_days": round(since - m, 1), "usual_qty": round(median(b["dates"].values()), 2),
                    "unit": _UOM_WORD.get(str(l.get("uom") or "").upper(), "unit"), "purchases": len(ds)})
    out.sort(key=lambda r: -(r["days_since"] / r["usual_every_days"]))
    return out[:limit]
