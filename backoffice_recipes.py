"""backoffice_recipes.py — plate costs from a restaurant's Back Office recipes.

Back Office (Buyers Edge) keeps every recipe a restaurant builds: menu
items, mods and the prep recipes (sauces, rubs, the smoked brisket) they
draw on, each ingredient in a kitchen unit (Tbsp, Cup, Ounce, Each), and an
inventory item behind every bought ingredient with its supplier's item
number, pack ("Case of 10 Pound") and Back Office's own unit conversions
("1 Pound = 32.4 Tbsp"). It has no API; the set is read once from its pages
and loaded here whole (`load`, `scripts/backoffice_import.py`), one row per
restaurant in `bo_recipe_books`.

Back Office's own plate costs are not used. They price an item from how it
is set up, and where the setup disagrees with how the supplier bills it the
error runs through every recipe that uses it (EJ's, 10/15/26: brisket
billed $5.94 a pound, set up as a $5.94 case of 55 pounds — 11 cents a
pound; Kids Burger $0.33). So each ingredient is priced from an invoice line
for its item number FROM ITS OWN SUPPLIER (`backoffice_ap.bo_invoice_lines`,
keyed by vendor and item number — two houses share short numbers) where
there is one, read the way the supplier bills it, else from Back Office's
last price. A credit, a $0 line or a line with no quantity is never a price
(a line with no unit amount is its extended amount over its quantity).
Whether a weight-packed case is billed by the pound is decided per item: a
weight reading on any of its lines, else the one reading of the line a
real item could cost, else the one closer to Back Office's listed price. A
price outside what the item could cost, or more than 3x from every reading
of Back Office's listed price, is refused rather than used, and named.

`plates` returns each menu recipe's cost with every ingredient's source,
what could not be priced and why, and — where the recipe is matched to the
POS — what it sold for over four weeks. A plate with an unpriced ingredient
is a floor ("at least"), never a percentage; a plate under 12% or over 70%
of its price is a setup error, flagged and kept out of the summary.
`period_summaries` prices several windows (a count period each, for
backoffice_counts) from one read of the book, the invoice lines and the
POS, each window at the prices of its own end. Calls no model, sends nothing.
"""
import json
import re
from datetime import date, timedelta

from backoffice_ap import _now, get_conn

# Ounces in each weight unit; fluid ounces in each volume unit.
WEIGHT = {"Ounce": 1.0, "Pound": 16.0, "Gram": 0.035274, "Kilogram": 35.274}
VOLUME = {"Dash": 1 / 48, "Tsp": 1 / 6, "Tbsp": 0.5, "Fluid Ounce": 1.0, "Cup": 8.0, "Pint": 16.0, "Quart": 32.0,
          "Gallon": 128.0, "Milliliter": 0.033814, "Liter": 33.814}

# A price per pound outside these is a setup or a reading error, not a
# price (EJ's Spanish onions: $39.26 for a sack, set up as a pound). Meat,
# poultry and seafood never cost under $1.50 a pound, cheese and dairy sold
# by weight never under $1, and no food under 20 cents (EJ's cheapest real
# pound, 10/16/26: flour at 41 cents, whole carrots at 44).
MAX_PER_POUND = 30.0
MIN_PER_POUND_PROTEIN = 1.5
MIN_PER_POUND_DAIRY = 1.0
MIN_PER_POUND = 0.20
MAX_PER_GALLON = 60.0
STALE_DAYS = 120
# An invoice price more than this many times from every reading of Back
# Office's listed price for the same row is a reading error, not a price.
LISTED_RATIO = 3.0
# A fully priced plate under this share of its price is a recipe missing its
# main item, not a cheap dish (EJ's Tomahawk Pork Chop, 10/15/26: sprouts,
# seasoning, oil and rosemary — no chop — 2% of $54.99; the Pretzel without
# the pretzel, 10%). Flagged, and kept out of the headline figure.
SUSPECT_PCT = 12.0
# ...and over this share, a price or a pack set up wrong (a case of 96 buns
# set up as "Each": $30 a bun, a 250% burger). Flagged and kept out too.
SUSPECT_HIGH_PCT = 70.0
# ...or over twice the food cost target, when that is higher: a kitchen
# targeting 40% runs honest 75% plates (FC-CALC-10).
SUSPECT_HIGH_TARGET_X = 2.0
WINDOW_DAYS = 28


def _dim(unit):
    return "w" if unit in WEIGHT else "v" if unit in VOLUME else "e"


def _num(s):
    try:
        return float(re.sub(r"[^\d.\-]", "", str(s or "")) or "nan")
    except ValueError:
        return float("nan")


def _ok(x):
    return x == x and x is not None


def _sku(s):
    return re.sub(r"\s+T$", "", str(s or "").strip(), flags=re.I).lstrip("0")


def _pos_key(name):
    """A POS item name as one key: case and spacing never decide a match."""
    return " ".join(str(name or "").split()).casefold()


def _mdy_us(s):
    """Back Office's '10/12/2026' → ISO, or None."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s or ""))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


def _local_today(restaurant_id):
    """The restaurant's own calendar date (FC-CALC-24): on a UTC host the
    server's date is tomorrow after 7pm Central, and "through yesterday"
    took in the restaurant's current, partial day."""
    try:
        from models import _restaurant_today
        return _restaurant_today(restaurant_id)
    except Exception:
        return date.today()


def _capture(e, restaurant_id, job, db_path=None):
    try:
        import ops
        ops.capture(e, job=job, restaurant_id=restaurant_id, db_path=db_path)
    except Exception:
        pass


# ── the book ──────────────────────────────────────────────────────────────

def validate(book) -> dict:
    """The recipe book as read from Back Office: {recipes: [{name, group,
    ingredients: [{name, qty, unit, item | prep}], batch_yield}], items:
    [{name, gl, vendors: [...]}], pos_names?: {recipe: [POS item name]}}.
    Raises ValueError naming what is wrong — including one POS item mapped
    to two recipes (each would take all its sales: ING-19)."""
    if not isinstance(book, dict) or not isinstance(book.get("recipes"), list) or not isinstance(book.get("items"), list):
        raise ValueError("not a Back Office recipe book (recipes and items)")
    names = {r.get("name") for r in book["recipes"]}
    for r in book["recipes"]:
        for g in r.get("ingredients") or []:
            if "item" in g and not (isinstance(g["item"], int) and 0 <= g["item"] < len(book["items"])):
                raise ValueError(f"{r.get('name')}: {g.get('name')} points at no item")
            if "prep" in g and g["prep"] not in names:
                raise ValueError(f"{r.get('name')}: prep {g['prep']} is not in the book")
    owner = {}
    for recipe, pos in (book.get("pos_names") or {}).items():
        for n in pos or []:
            k = _pos_key(n)
            if k in owner and owner[k] != recipe:
                raise ValueError(f"POS item {n} is mapped to both {owner[k]} and {recipe} — each would take all "
                                 "of its sales")
            owner[k] = recipe
    return book


def load(restaurant_id, book, source=None, db_path=None) -> dict:
    """Replace the restaurant's recipe book. {recipes, items, menu, mapped}."""
    validate(book)
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO bo_recipe_books (restaurant_id, payload, source, loaded_at) VALUES (?,?,?,?) "
                     "ON CONFLICT(restaurant_id) DO UPDATE SET payload=excluded.payload, source=excluded.source, "
                     "loaded_at=excluded.loaded_at",
                     (int(restaurant_id), json.dumps(book), str(source or book.get("source") or "")[:300], _now()))
        conn.commit()
    finally:
        conn.close()
    menu = [r for r in book["recipes"] if _is_menu(r)]
    return {"recipes": len(book["recipes"]), "items": len(book["items"]), "menu": len(menu),
            "mapped": len(book.get("pos_names") or {})}


def book(restaurant_id, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT payload, loaded_at FROM bo_recipe_books WHERE restaurant_id=?",
                           (int(restaurant_id),)).fetchone()
    finally:
        conn.close()
    if not row:
        return None, None
    return json.loads(row["payload"]), row["loaded_at"]


def _is_menu(r):
    return str(r.get("group") or "").startswith("MENU ITEM")


# ── prices ────────────────────────────────────────────────────────────────

def _vendor_key(name):
    from fintech_invoices import distributor_key
    return distributor_key(name) or str(name or "").strip().lower()


def _same_vendor(a, b) -> bool:
    """Whether a recipe book's vendor (key `a`) is the supplier on an invoice
    (key `b`): one key, one the other's leading words ('sysco' and 'sysco
    chicago'), or one word that is the other's initials ('gfs' and 'gordon
    food service'). Never one shared first word alone (FC-CALC-05)."""
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = a.split(), b.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if long_[:len(short)] == short:
        return True
    return len(short) == 1 and len(long_) >= 2 and short[0] == "".join(w[0] for w in long_)


def _usable(line):
    """An invoice line as a price, or None: a credit, a return, a $0 line or
    a line with no quantity is never a price; a line with no unit amount is
    its extended amount over its quantity (FC-CALC-09 / ING-08)."""
    qty = _num(line.get("qty"))
    amt = line.get("unit_amount")
    amt = _num(amt) if amt not in (None, "") else float("nan")
    if not _ok(qty) or qty <= 0:
        return None
    if not _ok(amt):
        ext = _num(line.get("ext_amount"))
        amt = ext / qty if _ok(ext) else float("nan")
    if not _ok(amt) or amt <= 0:
        return None
    return dict(line, qty=qty, unit_amount=amt)


class _Invoices:
    """Usable invoice lines by (vendor key, item number), newest first.
    `_invoice_prices` builds it from bo_invoice_lines; a plain {item number:
    line} map (a caller's own lines, tests) is read as one vendor-less line
    per number."""

    def __init__(self, lines):
        self.by_key, self.vendors = {}, {}
        for line in lines:
            u = _usable(line)
            if u is None:
                continue
            sku = _sku(u["_sku"] if "_sku" in u else u.get("item_code"))
            vk = u.get("vendor_key")
            self.by_key.setdefault((vk, sku), []).append(u)
            self.vendors.setdefault(sku, set()).add(vk)
        for v in self.by_key.values():
            v.sort(key=lambda x: str(x.get("invoice_date") or ""), reverse=True)

    @classmethod
    def of(cls, invoices):
        if isinstance(invoices, cls):
            return invoices
        return cls([dict(v, _sku=k, vendor_key=None) for k, v in (invoices or {}).items()])

    def lines_for(self, vendor, sku) -> list:
        """This vendor row's lines for its number, newest first: only its own
        supplier's when the book names one; a row with no vendor only when
        one supplier alone bills that number."""
        sku = _sku(sku)
        vks = self.vendors.get(sku) or set()
        if None in vks:                       # a vendor-less map: the caller already chose
            return self.by_key.get((None, sku), [])
        if vendor:
            want = _vendor_key(vendor)
            hits = [vk for vk in vks if _same_vendor(want, vk)]
        else:
            hits = list(vks) if len(vks) == 1 else []
        out = [l for vk in hits for l in self.by_key.get((vk, sku), [])]
        return sorted(out, key=lambda x: str(x.get("invoice_date") or ""), reverse=True)


def _invoice_prices(restaurant_id, db_path=None) -> _Invoices:
    """Every usable Back Office invoice line, by (vendor, item number)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT item_code, purchase_unit, qty, unit_amount, ext_amount, invoice_date, vendor_key, "
                            "invoice_key FROM bo_invoice_lines WHERE restaurant_id=? AND item_code IS NOT NULL "
                            "ORDER BY invoice_date", (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    return _Invoices([dict(r) for r in rows])


def _pick(lines, as_of):
    """The line that prices as of `as_of` (an ISO date): the newest on or
    before it, else the earliest after it (the nearest price there is);
    the newest of all when `as_of` is None. (line, after?)"""
    if not lines:
        return None, False
    if not as_of:
        return lines[0], False
    before = [l for l in lines if str(l.get("invoice_date") or "") <= as_of]
    if before:
        return before[0], False
    return lines[-1], True


def _sane(per_base, base, gl):
    """None, or why a price per `base` unit cannot be a real price."""
    if not _ok(per_base) or per_base <= 0:
        return "a price of $0 or less is not a price (a credit, a $0 line or a missing amount)"
    if _dim(base) == "w":
        per_lb = per_base * 16.0 / WEIGHT[base]
        if per_lb > MAX_PER_POUND:
            return f"${per_lb:,.2f} a pound looks like a case price — check the pack"
        if per_lb < MIN_PER_POUND_PROTEIN and re.search(r"MEAT|POULTRY|SEAFOOD", str(gl or ""), re.I):
            return f"${per_lb:,.2f} a pound is too cheap for meat — the case price may be a price per pound"
        if per_lb < MIN_PER_POUND_DAIRY and re.search(r"DAIRY|CHEESE", str(gl or ""), re.I):
            return f"${per_lb:,.2f} a pound is too cheap for dairy — the case price may be a price per pound"
        if per_lb < MIN_PER_POUND:
            return f"${per_lb:,.2f} a pound is less than any food costs — the case price may be a price per pound"
    if _dim(base) == "v":
        per_gal = per_base * 128.0 / VOLUME[base]
        if per_gal > MAX_PER_GALLON:
            return f"${per_gal:,.2f} a gallon looks like a case price — check the pack"
    return None


def _weight_reading(qty) -> bool:
    """A quantity that reads as a scale's weight (20.4, 128.6), not a count
    of cases or a split case (2, 0.5, 1.25)."""
    return qty >= 1 and abs(qty - round(qty * 4) / 4) > 0.01


def item_price(item, invoices, today=None, as_of=None) -> dict:
    """One inventory item's price per its base unit (the vendor's pack unit,
    else its purchase unit): {base, per_base, source: invoice | back_office,
    date, vendor, sku, flag?} or {error}. `as_of` (ISO date) prices it as of
    that day: each row's newest invoice line on or before it.

    Every way the item is bought is a candidate — vendor rows with an invoice
    line from that vendor, newest first, then Back Office's own prices,
    newest purchase first — and the first one that passes the checks prices
    it. One row set up wrong (EJ's Spanish onions, GFS 591122: a 50 lb sack
    entered as bought "by the Pound" at $39.26) no longer leaves the item
    unpriced while the same item's other rows carry a real price; the
    refusal rides along as a flag."""
    today = today or date.today()
    inv = _Invoices.of(invoices)
    vendors = item.get("vendors") or []
    billed = []
    for v in vendors:
        hist = inv.lines_for(v.get("vendor"), v.get("sku"))
        line, after = _pick(hist, as_of)
        if line:
            billed.append((v, line, hist, after))
    # Newest first; as of a day, the rows priced on or before it come first
    # (newest first), then the rows priced only after it (nearest first).
    billed.sort(key=lambda c: str(c[1]["invoice_date"] or ""), reverse=True)
    billed = [c for c in billed if not c[3]] + sorted((c for c in billed if c[3]),
                                                      key=lambda c: str(c[1]["invoice_date"] or ""))
    listed = sorted(((v, None, [], False) for v in vendors if _ok(_num(v.get("price")))),
                    key=lambda c: _mdy_us(c[0].get("last_purchased")) or "", reverse=True)
    if not billed and not listed:
        return {"error": "no price in Back Office and no invoice line"}
    first_err = None
    for v, line, hist, after in billed + listed:
        got = _candidate_price(item, v, line, hist, today)
        if "error" not in got:
            if after:
                note = "priced from a later invoice — none on file on or before this date"
                got["flag"] = (got["flag"] + "; " + note) if got.get("flag") else note
            if first_err:
                skipped = first_err
                note = (f"{skipped.get('vendor') or 'one vendor'} {skipped.get('sku') or ''}".strip()
                        + f" refused ({skipped['error']}); priced from another way it is bought")
                got["flag"] = (got["flag"] + "; " + note) if got.get("flag") else note
            return got
        first_err = first_err or got
    return first_err


def _closer(amt, a, b):
    """Whether `amt` sits closer to `a` than to `b`, by ratio."""
    import math
    return abs(math.log(amt / a)) <= abs(math.log(amt / b))


def _candidate_price(item, v, line, hist, today) -> dict:
    """One vendor row's price per base unit, from its invoice line when there
    is one, else Back Office's price for it; {error} when it is not sane."""
    pu = v.get("purchase_unit") or "Each"
    per_pack = _num(v.get("per_pack"))
    base = v.get("pack_unit") or pu
    pack = per_pack if _ok(per_pack) and per_pack > 0 and v.get("pack_unit") else 1.0
    listed = _num(v.get("price"))
    listed = listed if _ok(listed) and listed > 0 else None
    gl = item.get("gl")
    flag = None
    if line:
        lu, amt = line["purchase_unit"] or pu, float(line["unit_amount"])
        if lu == pu and _dim(base) == "w" and pack != 1.0:
            # A weight-packed case: billed by the case, or by the pound
            # under a "Case" unit (catch weight)? Decided for the item, never
            # by one line's quantity alone (FC-CALC-08 / ING-07).
            case_pb, catch_pb = amt / pack, amt * WEIGHT[base] / 16.0
            if any(_weight_reading(float(h["qty"])) for h in (hist or [line])):
                catch = True
            else:
                e_case, e_catch = _sane(case_pb, base, gl), _sane(catch_pb, base, gl)
                if e_case and e_catch:
                    return {"error": e_case, "source": "invoice", "date": line["invoice_date"],
                            "vendor": v.get("vendor"), "sku": v.get("sku")}
                if e_case or e_catch:
                    catch = bool(e_case)
                elif listed:
                    catch = not _closer(amt, listed, listed / pack)
                else:
                    catch = False
            per_base = catch_pb if catch else case_pb
            if catch:
                flag = "billed by weight"
        elif lu == pu:
            per_base = amt / pack
        elif lu == base:
            per_base = amt
            flag = f"billed per {lu}, set up in Back Office as a {pu} of {per_pack:g} {base}"
        elif _dim(lu) == _dim(base) != "e":
            t = WEIGHT if _dim(lu) == "w" else VOLUME
            per_base = amt * t[base] / t[lu]
        else:
            return {"error": f"the invoice bills it per {lu}; Back Office sets it up as a {pu}",
                    "vendor": v.get("vendor"), "sku": v.get("sku")}
        src, when = "invoice", line["invoice_date"]
    else:
        per_base = (listed or 0.0) / pack
        src, when = "back_office", _mdy_us(v.get("last_purchased"))
        if when and (today - date.fromisoformat(when)).days > STALE_DAYS:
            flag = "Back Office's price is from over four months ago"
    why = _sane(per_base, base, gl)
    if not why and src == "invoice" and listed:
        # Back Office's own listed price for the row, read either way it may
        # be set up (a case price, or a price per pack unit entered as the
        # case's): an invoice price far from both is a reading error.
        reads = [listed / pack] + ([listed * WEIGHT[base] / 16.0] if _dim(base) == "w" else [])
        if min(max(per_base / r, r / per_base) for r in reads) > LISTED_RATIO:
            why = (f"the invoice reads ${per_base:,.2f} a {base}; Back Office lists ${listed:,.2f} a {pu} — "
                   f"more than {LISTED_RATIO:g}x apart")
    if why:
        return {"error": why, "source": src, "date": when, "vendor": v.get("vendor"), "sku": v.get("sku")}
    y = _num(str(v.get("yield") or "100").rstrip("%"))
    if _ok(y) and 0 < y < 100:
        per_base = per_base * 100.0 / y
    out = {"base": base, "per_base": per_base, "source": src, "date": when, "vendor": v.get("vendor"),
           "sku": v.get("sku"), "vendor_row": v}
    if flag:
        out["flag"] = flag
    return out


def _base_per(qty, unit, base, v):
    """How many `base` units `qty` `unit` is, by unit math or the vendor's
    own conversion; None when there is no way."""
    du, db = _dim(unit), _dim(base)
    if unit == base:
        return qty
    if du == db == "w":
        return qty * WEIGHT[unit] / WEIGHT[base]
    if du == db == "v":
        return qty * VOLUME[unit] / VOLUME[base]
    if unit == (v.get("purchase_unit") or "") and v.get("pack_unit"):
        n = _num(v.get("per_pack"))
        return qty * n if _ok(n) else None
    conv = v.get("weight_per_unit") if du == "w" else v.get("volume_per_unit") if du == "v" else None
    if conv and conv[1]:
        t = WEIGHT if du == "w" else VOLUME
        if conv[1] in t and conv[0]:
            return qty * t[unit] / (float(conv[0]) * t[conv[1]])
    return None


# ── costing ───────────────────────────────────────────────────────────────

class _Coster:
    """Recipe costs at one set of prices: today's (`as_of` None, each item's
    newest line) or as of a day (a count period's end)."""

    def __init__(self, book, invoices, today, as_of=None):
        self.recipes = {r["name"]: r for r in book["recipes"]}
        self.items = book["items"]
        self.invoices = _Invoices.of(invoices)
        self.today = today
        self.as_of = as_of
        self.memo, self.prices = {}, {}

    def price(self, i):
        if i not in self.prices:
            self.prices[i] = item_price(self.items[i], self.invoices, self.today, as_of=self.as_of)
        return self.prices[i]

    def recipe(self, name, stack=()):
        """{cost, lines, missing} for one recipe as written (its whole batch)."""
        if name in self.memo:
            return self.memo[name]
        r = self.recipes[name]
        lines, missing, cost = [], [], 0.0
        for g in r.get("ingredients") or []:
            qty, unit = _num(g.get("qty")), g.get("unit") or ""
            line = {"name": g.get("name"), "qty": qty if _ok(qty) else None, "unit": unit}
            if not _ok(qty):
                line["missing"] = "no quantity"
            elif "prep" in g:
                c, why, sub = self.prep_cost(g["prep"], qty, unit, stack + (name,))
                if c is None:
                    line["missing"] = why
                else:
                    line.update(cost=round(c, 4), source="prep")
                    cost += c
                    for m in sub:
                        missing.append(dict(m, via=g["prep"] if not m.get("via") else f"{g['prep']} › {m['via']}"))
            elif "item" in g:
                p = self.price(g["item"])
                if "error" in p:
                    line["missing"] = p["error"]
                else:
                    n = _base_per(qty, unit, p["base"], p["vendor_row"])
                    if n is None:
                        line["missing"] = f"no conversion from {unit} to {p['base']}"
                    else:
                        c = n * p["per_base"]
                        line.update(cost=round(c, 4), source=p["source"], date=p["date"], sku=p["sku"],
                                    per=f"${p['per_base']:,.2f} / {p['base']}")
                        if p.get("flag"):
                            line["flag"] = p["flag"]
                        cost += c
            else:
                line["missing"] = "not linked to an item"
            if line.get("missing"):
                missing.append({"name": line["name"], "reason": line["missing"]})
            lines.append(line)
        out = {"cost": cost, "lines": lines, "missing": missing}
        self.memo[name] = out
        return out

    def prep_cost(self, name, qty, unit, stack):
        """(cost of `qty` `unit` of a prep recipe, why not, its missing)."""
        if name in stack:
            return None, "the prep recipe uses itself", []
        if name not in self.recipes:
            return None, "prep recipe not in the book", []
        r = self.recipes[name]
        whole = self.recipe(name, stack)
        y = r.get("batch_yield") or {}
        d = _dim(unit)
        per = None
        if d == "w" and y.get("weight") and y.get("weight_unit") in WEIGHT:
            per = whole["cost"] / (_num(y["weight"]) * WEIGHT[y["weight_unit"]]) * WEIGHT[unit]
        elif d == "v" and y.get("volume") and y.get("volume_unit") in VOLUME:
            per = whole["cost"] / (_num(y["volume"]) * VOLUME[y["volume_unit"]]) * VOLUME[unit]
        elif d == "e" and y.get("each") and (unit == y.get("each_unit") or unit == "Each"):
            per = whole["cost"] / _num(y["each"])
        if per is None or not _ok(per):
            have = ", ".join(f"{y[k]} {y.get(k + '_unit')}" for k in ("weight", "volume", "each") if y.get(k)) or "none"
            return None, f"the prep's yield ({have}) can't be read in {unit}", []
        return per * qty, None, whole["missing"]


def _pos_sales(restaurant_id, names, start, end, db_path=None, by_day=False) -> dict:
    """{POS key: (units, sales)} over [start, end], sold lines only, matched
    on the name's case- and space-free key (`_pos_key`) — a POS rename to
    other capitals never zeroes a dish (ING-19). With `by_day`, {(date, POS
    key): (units, sales)}: one query for several windows."""
    keys = sorted({_pos_key(n) for n in names or [] if _pos_key(n)})
    if not keys:
        return {}
    conn = get_conn(db_path)
    try:
        q = ("SELECT %s LOWER(TRIM(REPLACE(item_name, '  ', ' '))) AS k, SUM(COALESCE(qty, 1)) AS units, "
             "SUM(sales) AS sales FROM pos_ticket_lines WHERE restaurant_id=? AND business_date BETWEEN ? AND ? "
             "AND kind='sale' AND LOWER(TRIM(REPLACE(item_name, '  ', ' '))) IN (%s) GROUP BY %s"
             "LOWER(TRIM(REPLACE(item_name, '  ', ' ')))"
             % ("business_date," if by_day else "", ",".join("?" * len(keys)), "business_date, " if by_day else ""))
        rows = conn.execute(q, [int(restaurant_id), str(start), str(end), *keys]).fetchall()
    except Exception as e:
        # Degrade to "no sales" as before, but never silently (SCALE-11).
        _capture(e, restaurant_id, "backoffice_recipes._pos_sales", db_path)
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        k = _pos_key(r["k"])
        key = (r["business_date"], k) if by_day else k
        u, s = out.get(key, (0.0, 0.0))
        out[key] = (u + float(r["units"] or 0), s + float(r["sales"] or 0))
    return out


def margin_target(restaurant) -> dict:
    """The margin % at or above which a dish's margin reads green on Plate
    costs (owner, 10/16/26): the owner's plate margin target; else 100 minus
    the food cost target the page's dishes are already judged against
    (cogs.dish_reference - the owner's, or Cavnar AI's starting 30%). The
    common 28-35% food cost range is a 65-72% margin; nothing here calls it
    an industry figure. {pct, source: set | from_food_target, label}."""
    own = getattr(restaurant, "plate_margin_target_pct", None) if restaurant is not None else None
    if own is not None:
        return {"pct": float(own), "source": "set", "label": f"your plate margin target, {float(own):g}%"}
    import cogs
    ref = cogs.dish_reference(restaurant) or {"pct": 30.0, "basis": "Cavnar AI's starting food cost target, 30%"}
    pct = round(100.0 - float(ref["pct"]), 1)
    return {"pct": pct, "source": "from_food_target", "label": f"{pct:g}% — 100 minus {ref['basis']}"}


def suspect_high(restaurant_id, db_path=None) -> float:
    """The plate cost % over which a fully priced dish is flagged as a
    price or pack set up wrong: SUSPECT_HIGH_PCT, or twice the food cost
    target the dishes are judged against when that is higher (FC-CALC-10)."""
    try:
        import cogs
        from models import get_restaurant
        ref = cogs.dish_reference(get_restaurant(restaurant_id, db_path) if db_path else get_restaurant(restaurant_id))
        if ref and ref.get("pct"):
            return max(SUSPECT_HIGH_PCT, SUSPECT_HIGH_TARGET_X * float(ref["pct"]))
    except Exception as e:
        import ops
        ops.capture(e, job="backoffice_recipes", context=f"restaurant_id={restaurant_id} suspect target",
                    restaurant_id=restaurant_id)
    return SUSPECT_HIGH_PCT


def _is_addon(r) -> bool:
    """An add-on, not a dish: Back Office files it under MODS ("MOD- ADD
    GRILLED CHICKEN", "MOD- WING SAUCE CLASSIC BUFFALO") — what RPOWER rings
    under (Food Add-Ons) on a dish's ticket (owner, 10/17/26: never listed
    with the dishes)."""
    return str(r.get("group") or "").upper().endswith("MODS") or str(r.get("name") or "").upper().startswith("MOD-")


def addon_name(name) -> str:
    """'MOD- ADD GRILLED CHICKEN' → 'ADD GRILLED CHICKEN'."""
    return re.sub(r"^\s*MOD-\s*", "", str(name or ""), flags=re.I)


# Words an add-on's POS button and its Back Office recipe spell differently.
_ADDON_SKIP = {"mod", "add", "xtra", "extra", "sub", "side", "of", "the", "for", "a", "wing", "sauce", "w"}
_ADDON_SAME = {"chix": "chicken", "grill": "grilled", "bleu": "blue", "chs": "cheese", "pkle": "pickled",
               "tomat": "tomato", "tomatoes": "tomato", "onions": "onion", "strawberries": "strawberry",
               "blueberries": "blueberry", "croutons": "crouton", "cola": "cola", "bech": "bechamel"}


def _addon_words(name) -> set:
    base = str(name or "").split("`")[0]
    words = re.findall(r"[a-z0-9]+", base.lower().replace("&", " "))
    return {_ADDON_SAME.get(w, w) for w in words if w not in _ADDON_SKIP and not w.isdigit()}


def addon_links(restaurant_id, bk, start, end, db_path=None) -> dict:
    """{add-on recipe: [POS add-on names]} for the add-ons Back Office's book
    names no POS item for — matched to what RPOWER rang under its add-ons
    and modifiers in [start, end] when every word of the button is in ONE
    recipe's name ("Add Grill Chix" → MOD- ADD GRILLED CHICKEN, "Classic
    Buffalo`$1.99" → MOD- WING SAUCE CLASSIC BUFFALO). A button two recipes
    could be ("BBQ`$1.99": the Dr. Pepper BBQ wing sauce or the BBQ chip rub)
    is left unlinked."""
    pos_names = bk.get("pos_names") or {}
    owned = {_pos_key(n) for ns in pos_names.values() for n in ns or []}
    addons = [r for r in bk.get("recipes") or [] if _is_menu(r) and _is_addon(r) and not pos_names.get(r["name"])]
    if not addons:
        return {}
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT item_name FROM pos_ticket_lines WHERE restaurant_id=? AND business_date BETWEEN ? AND ? "
            "AND kind='sale' AND (item_kind='modifier' OR LOWER(COALESCE(sales_category, '')) LIKE '%add-on%' "
            "OR LOWER(COALESCE(sales_category, '')) LIKE '%add on%')",
            (int(restaurant_id), str(start), str(end))).fetchall()
    except Exception as e:
        _capture(e, restaurant_id, "backoffice_recipes.addon_links", db_path)
        return {}
    finally:
        conn.close()
    words = {r["name"]: _addon_words(addon_name(r["name"])) for r in addons}
    out = {}
    for row in rows:
        n = row["item_name"]
        if not n or _pos_key(n) in owned:
            continue
        pw = _addon_words(n)
        if not pw:
            continue
        fits = [(len(w - pw), name) for name, w in words.items() if pw <= w]
        if not fits:
            continue
        fits.sort()
        if len(fits) > 1 and fits[0][0] == fits[1][0]:
            continue
        out.setdefault(fits[0][1], []).append(n)
        owned.add(_pos_key(n))
    return out


def _menu(bk):
    pos_names = bk.get("pos_names") or {}
    return [r for r in bk["recipes"] if _is_menu(r) and (not r["group"].endswith("MODS") or r["name"] in pos_names)]


def _build(bk, coster, sold, menu, high=SUSPECT_HIGH_PCT):
    """(plates, summary) for the menu at `coster`'s prices against `sold`
    ({POS key: (units, sales)}); a plate over `high`% is suspect."""
    pos_names = bk.get("pos_names") or {}
    out = []
    for r in menu:
        res = coster.recipe(r["name"])
        names = pos_names.get(r["name"], [])
        keys = {_pos_key(n) for n in names}
        units = sum(sold.get(k, (0, 0))[0] for k in keys)
        sales = sum(sold.get(k, (0, 0))[1] for k in keys)
        price = sales / units if units > 0 else None
        complete = not res["missing"]
        p = {"name": r["name"], "group": r["group"].split(">")[-1], "cost": round(res["cost"], 2),
             # Why a dish with no POS item of its own has none (the book's
             # pos_notes: a POS line two recipes share, an upgrade line).
             "pos_note": None if names else (bk.get("pos_notes") or {}).get(r["name"]),
             "complete": complete, "missing": res["missing"], "lines": res["lines"], "pos_names": names,
             "units": round(units), "sales": round(sales, 2), "price": round(price, 2) if price else None,
             "cost_pct": round(100 * res["cost"] / price, 1) if (price and complete) else None,
             "margin_pct": round(100 - 100 * res["cost"] / price, 1) if (price and complete) else None,
             "margin": round(price - res["cost"], 2) if (price and complete) else None}
        if p["cost_pct"] is not None and p["cost_pct"] < SUSPECT_PCT:
            p["suspect"] = (f"the recipe costs {p['cost_pct']}% of the price — Back Office's recipe may be missing "
                            "its main item")
        elif p["cost_pct"] is not None and p["cost_pct"] > high:
            p["suspect"] = (f"the recipe costs {p['cost_pct']}% of the price — a price or a pack in Back Office "
                            "may be set up wrong")
        out.append(p)
    out.sort(key=lambda p: (-p["sales"], p["name"]))
    priced = [p for p in out if p["complete"] and p["units"] > 0 and not p.get("suspect")]
    sold_total = sum(p["sales"] for p in out)
    unsold = sorted(n for p in out for n in p["pos_names"] if _pos_key(n) not in sold)
    summary = {"dishes": len(out), "complete": sum(1 for p in out if p["complete"]),
               "suspect": sum(1 for p in out if p.get("suspect")), "mapped": sum(1 for p in out if p["pos_names"]),
               "covered_sales": round(sum(p["sales"] for p in priced), 2), "mapped_sales": round(sold_total, 2),
               "cost_pct": (round(100 * sum(p["cost"] * p["units"] for p in priced) / sum(p["sales"] for p in priced), 1)
                            if priced and sum(p["sales"] for p in priced) > 0 else None),
               # POS names a recipe is mapped to that sold nothing in the
               # window: a renamed POS button reads here, not as a quiet 0.
               "unsold_names": unsold}
    return out, summary


def plates(restaurant_id, today=None, db_path=None, start=None, end=None, as_of=None) -> dict:
    """{available, why_not?, loaded_at, window, plates: [{name, group,
    cost, complete, missing, lines, pos_names, units, sales, price,
    cost_pct, margin}], summary, prices, note}. Menu recipes with POS sales
    first, by what they sold. `start`/`end` (dates) price the sales of
    another window; `as_of` (a date) prices the recipes as of that day
    instead of today. The default is the four weeks to the restaurant's
    yesterday, at today's prices."""
    from time_utils import mdy
    bk, loaded = book(restaurant_id, db_path)
    if not bk:
        return {"available": False, "why_not": "No Back Office recipes are loaded for this restaurant yet."}
    today = today or _local_today(restaurant_id)
    end = end or (today - timedelta(days=1))
    start = start or (end - timedelta(days=WINDOW_DAYS - 1))
    c = _Coster(bk, _invoice_prices(restaurant_id, db_path), today,
                as_of=as_of.isoformat() if hasattr(as_of, "isoformat") else as_of)
    bk = _with_addon_links(restaurant_id, bk, start, end, db_path)
    menu = _menu(bk)
    pos_names = bk.get("pos_names") or {}
    sold = _pos_sales(restaurant_id, sorted({n for r in menu for n in pos_names.get(r["name"], [])}),
                      start.isoformat(), end.isoformat(), db_path)
    high = suspect_high(restaurant_id, db_path)
    out, summary = _build(bk, c, sold, menu, high=high)
    # Dishes and add-ons apart (owner, 10/17/26); the summary stays over
    # everything sold — an add-on's cost and price are food too.
    addons = {r["name"] for r in menu if _is_addon(r)}
    dishes = [p for p in out if p["name"] not in addons]
    add_ons = [dict(p, name=addon_name(p["name"])) for p in out if p["name"] in addons]
    srcs = {"invoice": 0, "back_office": 0, "unpriced": 0}
    for p in c.prices.values():
        srcs["unpriced" if "error" in p else p["source"]] += 1
    return {"available": True, "loaded_at": loaded, "basis": "recipes",
            "window": {"from": start.isoformat(), "to": end.isoformat(), "from_mdy": mdy(start.isoformat()),
                       "to_mdy": mdy(end.isoformat()), "days": (end - start).days + 1},
            "plates": dishes, "add_ons": add_ons, "summary": summary, "prices": srcs,
            # Short and in points (owner, 10/17/26: the tips were a paragraph).
            "note": ("Recipe cost at today's supplier prices.",
                     "Prices: each item's latest invoice, else Back Office's last price.",
                     "Selling price: what the POS took over four weeks.",
                     f"Under 12% or over {high:g}% of the price: flagged, left out of the total.")}


def _with_addon_links(restaurant_id, bk, start, end, db_path=None):
    """The book with its add-on recipes linked to the POS's add-on buttons
    (addon_links) — read time only, the stored book unchanged."""
    links = addon_links(restaurant_id, bk, start, end, db_path)
    if not links:
        return bk
    pn = dict(bk.get("pos_names") or {})
    for name, ns in links.items():
        pn[name] = list(pn.get(name) or []) + ns
    return dict(bk, pos_names=pn)


def period_summaries(restaurant_id, windows, db_path=None):
    """[summary] — `plates`' summary for each (start, end) date window, each
    priced as of its own end (the newest invoice line on or before it, not
    today's: FC-CALC-07) — from ONE read of the recipe book, the invoice
    lines and the POS (SCALE-01: a year of weekly counts re-read and
    re-costed the whole book 52 times per request). None without a book."""
    bk, _loaded = book(restaurant_id, db_path)
    if not bk or not windows:
        return None
    invoices = _invoice_prices(restaurant_id, db_path)
    lo = min(s for s, _e in windows).isoformat()
    hi = max(e for _s, e in windows).isoformat()
    bk = _with_addon_links(restaurant_id, bk, lo, hi, db_path)
    menu = _menu(bk)
    pos_names = bk.get("pos_names") or {}
    daily = _pos_sales(restaurant_id, sorted({n for r in menu for n in pos_names.get(r["name"], [])}), lo, hi,
                       db_path, by_day=True)
    today = _local_today(restaurant_id)
    high = suspect_high(restaurant_id, db_path)
    out, costers = [], {}
    for s, e in windows:
        a, b = s.isoformat(), e.isoformat()
        sold = {}
        for (d, k), (u, v) in daily.items():
            if a <= d <= b:
                pu, pv = sold.get(k, (0.0, 0.0))
                sold[k] = (pu + u, pv + v)
        if b not in costers:
            costers[b] = _Coster(bk, invoices, today, as_of=b)
        out.append(_build(bk, costers[b], sold, menu, high=high)[1])
    return out
