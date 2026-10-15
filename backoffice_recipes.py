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
pound; Kids Burger $0.33). So each ingredient is priced from the latest
invoice line for its item number (`backoffice_ap.bo_invoice_lines`) where
there is one, read the way the supplier bills it (a catch-weight case is a
price per pound), else from Back Office's last price; a price outside what
the item could cost is refused rather than used, and named.

`plates` returns each menu recipe's cost at today's prices with every
ingredient's source, what could not be priced and why, and — where the
recipe is matched to the POS — what it sold for over four weeks. A plate
with an unpriced ingredient is a floor ("at least"), never a percentage.
Calls no model, sends nothing.
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
# poultry and seafood never cost under $1.50 a pound.
MAX_PER_POUND = 30.0
MIN_PER_POUND_PROTEIN = 1.5
MAX_PER_GALLON = 60.0
STALE_DAYS = 120
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


def _mdy_us(s):
    """Back Office's '10/12/2026' → ISO, or None."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s or ""))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


# ── the book ──────────────────────────────────────────────────────────────

def validate(book) -> dict:
    """The recipe book as read from Back Office: {recipes: [{name, group,
    ingredients: [{name, qty, unit, item | prep}], batch_yield}], items:
    [{name, gl, vendors: [...]}], pos_names?: {recipe: [POS item name]}}.
    Raises ValueError naming what is wrong."""
    if not isinstance(book, dict) or not isinstance(book.get("recipes"), list) or not isinstance(book.get("items"), list):
        raise ValueError("not a Back Office recipe book (recipes and items)")
    names = {r.get("name") for r in book["recipes"]}
    for r in book["recipes"]:
        for g in r.get("ingredients") or []:
            if "item" in g and not (isinstance(g["item"], int) and 0 <= g["item"] < len(book["items"])):
                raise ValueError(f"{r.get('name')}: {g.get('name')} points at no item")
            if "prep" in g and g["prep"] not in names:
                raise ValueError(f"{r.get('name')}: prep {g['prep']} is not in the book")
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

def _invoice_prices(restaurant_id, db_path=None) -> dict:
    """{item code: latest invoice line} from Back Office invoice pages."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT item_code, purchase_unit, qty, unit_amount, invoice_date, vendor_key, invoice_key "
                            "FROM bo_invoice_lines WHERE restaurant_id=? AND item_code IS NOT NULL "
                            "ORDER BY invoice_date", (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        out[_sku(r["item_code"])] = dict(r)
    return out


def _sane(per_base, base, gl):
    """None, or why a price per `base` unit cannot be a real price."""
    if _dim(base) == "w":
        per_lb = per_base * 16.0 / WEIGHT[base]
        if per_lb > MAX_PER_POUND:
            return f"${per_lb:,.2f} a pound looks like a case price — check the pack"
        if per_lb < MIN_PER_POUND_PROTEIN and re.search(r"MEAT|POULTRY|SEAFOOD", str(gl or ""), re.I):
            return f"${per_lb:,.2f} a pound is too cheap for meat — the case price may be a price per pound"
    if _dim(base) == "v":
        per_gal = per_base * 128.0 / VOLUME[base]
        if per_gal > MAX_PER_GALLON:
            return f"${per_gal:,.2f} a gallon looks like a case price — check the pack"
    return None


def item_price(item, invoices, today=None) -> dict:
    """One inventory item's price per its base unit (the vendor's pack unit,
    else its purchase unit): {base, per_base, source: invoice | back_office,
    date, vendor, sku, flag?} or {error}."""
    today = today or date.today()
    vendors = item.get("vendors") or []
    best = None
    for v in vendors:
        line = invoices.get(_sku(v.get("sku")))
        if line and (best is None or str(line["invoice_date"] or "") > str(best[1]["invoice_date"] or "")):
            best = (v, line)
    if best:
        v, line = best
    else:
        dated = sorted((v for v in vendors if _ok(_num(v.get("price")))),
                       key=lambda v: _mdy_us(v.get("last_purchased")) or "", reverse=True)
        if not dated:
            return {"error": "no price in Back Office and no invoice line"}
        v, line = dated[0], None
    pu = v.get("purchase_unit") or "Each"
    per_pack = _num(v.get("per_pack"))
    base = v.get("pack_unit") or pu
    pack = per_pack if _ok(per_pack) and per_pack > 0 and v.get("pack_unit") else 1.0
    flag = None
    if line:
        lu, amt, qty = line["purchase_unit"] or pu, float(line["unit_amount"] or 0), float(line["qty"] or 0)
        catch = abs(qty - round(qty)) > 0.01
        if lu == pu and catch and _dim(base) == "w":
            per_base = amt * WEIGHT[base] / 16.0            # billed by the pound
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
            return {"error": f"the invoice bills it per {lu}; Back Office sets it up as a {pu}"}
        src, when = "invoice", line["invoice_date"]
    else:
        per_base = _num(v.get("price")) / pack
        src, when = "back_office", _mdy_us(v.get("last_purchased"))
        if when and (today - date.fromisoformat(when)).days > STALE_DAYS:
            flag = "Back Office's price is from over four months ago"
    why = _sane(per_base, base, item.get("gl"))
    if why:
        return {"error": why, "source": src, "date": when}
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
    def __init__(self, book, invoices, today):
        self.recipes = {r["name"]: r for r in book["recipes"]}
        self.items = book["items"]
        self.invoices = invoices
        self.today = today
        self.memo, self.prices = {}, {}

    def price(self, i):
        if i not in self.prices:
            self.prices[i] = item_price(self.items[i], self.invoices, self.today)
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


def _pos_sales(restaurant_id, names, start, end, db_path=None) -> dict:
    """{pos name: (units, sales)} over [start, end], sold lines only."""
    if not names:
        return {}
    conn = get_conn(db_path)
    try:
        q = ("SELECT item_name, SUM(COALESCE(qty, 1)) AS units, SUM(sales) AS sales FROM pos_ticket_lines "
             "WHERE restaurant_id=? AND business_date BETWEEN ? AND ? AND kind='sale' AND item_name IN (%s) "
             "GROUP BY item_name" % ",".join("?" * len(names)))
        rows = conn.execute(q, [int(restaurant_id), start, end, *names]).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {r["item_name"]: (float(r["units"] or 0), float(r["sales"] or 0)) for r in rows}


def plates(restaurant_id, today=None, db_path=None) -> dict:
    """{available, why_not?, loaded_at, window, plates: [{name, group,
    cost, complete, missing, lines, pos_names, units, sales, price,
    cost_pct, margin}], summary, prices, note}. Menu recipes with POS sales
    first, by what they sold."""
    from time_utils import mdy
    bk, loaded = book(restaurant_id, db_path)
    if not bk:
        return {"available": False, "why_not": "No Back Office recipes are loaded for this restaurant yet."}
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    c = _Coster(bk, _invoice_prices(restaurant_id, db_path), today)
    pos_names = bk.get("pos_names") or {}
    menu = [r for r in bk["recipes"] if _is_menu(r) and (not r["group"].endswith("MODS") or r["name"] in pos_names)]
    sold = _pos_sales(restaurant_id, sorted({n for r in menu for n in pos_names.get(r["name"], [])}),
                      start.isoformat(), end.isoformat(), db_path)
    out = []
    for r in menu:
        res = c.recipe(r["name"])
        names = pos_names.get(r["name"], [])
        units = sum(sold.get(n, (0, 0))[0] for n in names)
        sales = sum(sold.get(n, (0, 0))[1] for n in names)
        price = sales / units if units > 0 else None
        complete = not res["missing"]
        p = {"name": r["name"], "group": r["group"].split(">")[-1], "cost": round(res["cost"], 2),
             "complete": complete, "missing": res["missing"], "lines": res["lines"], "pos_names": names,
             "units": round(units), "sales": round(sales, 2), "price": round(price, 2) if price else None,
             "cost_pct": round(100 * res["cost"] / price, 1) if (price and complete) else None,
             "margin": round(price - res["cost"], 2) if (price and complete) else None}
        out.append(p)
    out.sort(key=lambda p: (-p["sales"], p["name"]))
    priced = [p for p in out if p["complete"] and p["units"] > 0]
    sold_total = sum(p["sales"] for p in out)
    summary = {"dishes": len(out), "complete": sum(1 for p in out if p["complete"]), "mapped": sum(1 for p in out if p["pos_names"]),
               "covered_sales": round(sum(p["sales"] for p in priced), 2), "mapped_sales": round(sold_total, 2),
               "cost_pct": (round(100 * sum(p["cost"] * p["units"] for p in priced) / sum(p["sales"] for p in priced), 1)
                            if priced and sum(p["sales"] for p in priced) > 0 else None)}
    srcs = {"invoice": 0, "back_office": 0, "unpriced": 0}
    for p in c.prices.values():
        srcs["unpriced" if "error" in p else p["source"]] += 1
    return {"available": True, "loaded_at": loaded, "basis": "recipes",
            "window": {"from": start.isoformat(), "to": end.isoformat(), "from_mdy": mdy(start.isoformat()),
                       "to_mdy": mdy(end.isoformat()), "days": WINDOW_DAYS},
            "plates": out, "summary": summary, "prices": srcs,
            "note": ("What each dish's recipe costs at today's supplier prices: the latest invoice line for each "
                     "item, read the way the supplier bills it, else Back Office's last price. A dish with an "
                     "ingredient that can't be priced shows what is known, never a percentage. Recipes are "
                     "Back Office's; the selling price is what the POS took over four weeks.")}
