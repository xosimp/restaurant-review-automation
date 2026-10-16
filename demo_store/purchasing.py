"""
demo_store.purchasing — what Halsted & Rye buys, from what it sold.

Every delivery is sized from the product the nights before it used: drinks
from the POS lines and their pours (a little over-poured, foam and the odd
broken bottle on top), food from each dish's recipe. Bar invoices arrive the
way a real store's do — a Fintech export file a month, read by the real
importer (fintech_invoices.ingest_file) — with keg deposits and returns, a
deal that ends, a few real price increases and a credit memo. Kitchen
invoices go in through Back Office's own readers (backoffice_ap), with its
Chart of Accounts and the recipe book (backoffice_recipes) built from the
same recipes the store cooks from.
"""
import csv
import io
import math
import random
from datetime import date, timedelta
from functools import lru_cache

from demo_store import day as D
from demo_store import world as w

WEIGHT = {"Ounce": 1.0, "Pound": 16.0}
VOLUME = {"Fluid Ounce": 1.0, "Cup": 8.0, "Tbsp": 0.5}

# Over-pour, foam, spillage and breakage: what a bar buys beyond what it rang.
DRINK_LOSS = {"beer": 1.06, "liquor": 1.24, "wine": 1.05, "na": 1.02}
FOOD_LOSS = 1.07

# distributor -> delivery weekdays
DELIVERY_DAYS = {"LB": (1, 4), "CB": (0, 3), "WC": (2,), "BB": (1,), "SG": (3,)}

# Price moves: product -> [(from date, change per unit)]
PRICE_MOVES = {
    "titos": [(date(2026, 6, 1), 1.50)],
    "jameson": [(date(2026, 3, 2), 1.00)],
    "casamigos": [(date(2026, 8, 17), 2.00)],
    "modelo_keg": [(date(2026, 4, 6), 8.00)],
    "antihero_keg": [(date(2026, 2, 2), 6.00)],
    "chard": [(date(2026, 7, 6), 0.75)],
    "espolon": [(date(2026, 9, 14), 1.25)],
}
# A summer deal that ends (not an increase): product -> (from, to, off per unit)
DEALS = {"miller_keg": (date(2026, 5, 18), date(2026, 7, 31), 12.00),
         "high_noon": (date(2026, 5, 4), date(2026, 8, 31), 4.80)}
KEG_DEPOSIT = 30.00
LOCATION_ID = "HR-1001"


def _rng(*k):
    return random.Random("|".join(str(x) for x in ("hr-buy",) + k))


# ── what each night used ────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _food_items():
    return w.FOOD_ITEMS


def _base_qty(item_key, qty, unit) -> float:
    """`qty unit` of an inventory item in its pack unit."""
    pack_unit = w.FOOD_ITEMS[item_key][6]
    if unit == pack_unit:
        return qty
    if unit in WEIGHT and pack_unit in WEIGHT:
        return qty * WEIGHT[unit] / WEIGHT[pack_unit]
    if unit in VOLUME and pack_unit in VOLUME:
        return qty * VOLUME[unit] / VOLUME[pack_unit]
    raise ValueError(f"{item_key}: {unit} -> {pack_unit}")


def _yield_qty(prep, qty, unit) -> float:
    """What fraction of the prep's batch `qty unit` is."""
    amount, yunit = w.PREP[prep][1]
    if unit in WEIGHT and yunit in WEIGHT:
        return qty * WEIGHT[unit] / (amount * WEIGHT[yunit])
    if unit in VOLUME and yunit in VOLUME:
        return qty * VOLUME[unit] / (amount * VOLUME[yunit])
    raise ValueError(f"{prep}: {unit} vs {yunit}")


@lru_cache(maxsize=256)
def explode(ingredients: tuple) -> dict:
    """{item key: pack units} one serving uses, preps expanded through their batch yield."""
    out = {}
    for name, qty, unit in ingredients:
        if name.startswith("@"):
            prep = name[1:]
            share = _yield_qty(prep, qty, unit)
            for k, v in explode(tuple(tuple(x) for x in w.PREP[prep][2])).items():
                out[k] = out.get(k, 0.0) + v * share
        else:
            out[name] = out.get(name, 0.0) + _base_qty(name, qty, unit)
    return out


def usage(day: date) -> dict:
    """{"drink": {product: oz}, "food": {item: pack units}} the night used (rung sales and comps)."""
    night = D.business_day(day)
    by_mid = {m["mid"]: m for m in w.menu()}
    sold = {w.ST["Sale"], w.ST["Comp"], w.ST["Non Sale"]}
    drink, food = {}, {}
    for l in night["lines"]:
        if l["slstype_mid"] not in sold:
            continue
        m = by_mid.get(l["menuitem_mid"])
        if not m:
            continue
        q = float(l["qty"] or 1)
        if m["kind"] == "drink" and m.get("pours"):
            for prod, oz in m["pours"]:
                drink[prod] = drink.get(prod, 0.0) + oz * q
        elif m["kind"] == "food" and m.get("recipe"):
            for k, v in explode(tuple(tuple(x) for x in m["recipe"])).items():
                food[k] = food.get(k, 0.0) + v * q
    if night["tickets"]:
        # Garnish, mixers and fryer top-ups the menu never rings.
        food["lime"] = food.get("lime", 0.0) + 22
        food["lemon"] = food.get("lemon", 0.0) + 10
        food["oil"] = food.get("oil", 0.0) + 40
    return {"drink": drink, "food": food}


# ── bar: Fintech export files ───────────────────────────────────────────────

def unit_price(key, day: date, units: float = 0) -> float:
    p = w.BAR_PRODUCTS[key]
    per_unit = p[6] / p[5] if p[4] == "BTL" else p[6]
    for since, up in PRICE_MOVES.get(key, []):
        if day >= since:
            per_unit += up
    deal = DEALS.get(key)
    if deal and deal[0] <= day <= deal[1]:
        per_unit -= deal[2]
    if key == "svedka" and units and units < 12:
        per_unit += 0.50                      # the case price needs a full case
    return round(per_unit, 2)


def _prev_delivery(day: date, days: tuple) -> date:
    d = day - timedelta(days=1)
    while d.weekday() not in days:
        d -= timedelta(days=1)
    return d


@lru_cache(maxsize=4096)
def _usage_cached(day: date) -> dict:
    return usage(day)


def _window_use(day: date, days: tuple, kind: str) -> dict:
    """What was used since the distributor's previous delivery, up to the night before `day`."""
    out = {}
    d = _prev_delivery(day, days)
    while d < day:
        for k, v in _usage_cached(d)[kind].items():
            out[k] = out.get(k, 0.0) + v
        d += timedelta(days=1)
    return out


def _bar_lines(dist: str, day: date) -> list:
    """One distributor's delivery on `day`: what the bar used since the last one, in whole units.
    The same day always gives the same invoice, whenever it is generated."""
    days = DELIVERY_DAYS[dist]
    used = _window_use(day, days, "drink")
    rng = _rng("inv", dist, day.isoformat())
    lines = []
    for key, p in w.BAR_PRODUCTS.items():
        if p[0] != dist:
            continue
        size, uom, per_case = p[3], p[4], p[5]
        oz = used.get(key, 0.0) * DRINK_LOSS[p[7]]
        per_unit = size * (per_case if uom == "CS" else 1)
        units = int(oz / per_unit + rng.uniform(0.15, 0.85))
        if units <= 0:
            continue
        price = unit_price(key, day, units)
        lines.append((p[1], p[2], uom, units, price, round(units * price, 2)))
        if uom == "KEG":
            lines.append(("DEP50", "KEG DEPOSIT 1/2 BBL" if size > 1000 else "KEG DEPOSIT 1/6 BBL", "EA",
                          units, KEG_DEPOSIT, round(units * KEG_DEPOSIT, 2)))
    return lines


def bar_invoices(start: date, end: date) -> list:
    """[{distributor, number, date, lines: [(code, description, uom, qty, unit, extended)]}] over [start, end]."""
    out = []
    day = start
    while day <= end:
        for dist, days in DELIVERY_DAYS.items():
            if day.weekday() not in days:
                continue
            lines = _bar_lines(dist, day)
            # The empties from the last delivery go back on this truck.
            prev = [l for l in _bar_lines(dist, _prev_delivery(day, days)) if l[2] == "KEG"]
            back = sum(l[3] for l in prev)
            if back:
                lines.append(("DEP50", "KEG DEPOSIT RETURN", "EA", -back, KEG_DEPOSIT, round(-back * KEG_DEPOSIT, 2)))
            if not lines:
                continue
            rng = _rng("credit", dist, day.isoformat())
            if rng.random() < 0.04 and any(l[2] == "BTL" for l in lines):
                b = rng.choice([l for l in lines if l[2] == "BTL"])
                lines.append(("CR" + b[0], "CREDIT BROKEN BOTTLE " + b[1], "BTL", -1, b[4], -b[4]))
            out.append({"distributor": dist, "number": f"{w.DISTRIBUTORS[dist][1]}{day.strftime('%y%m%d')}",
                        "date": day, "lines": lines})
        day += timedelta(days=1)
    return out


def fintech_csv(invoices) -> str:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["Retailer Name", "Location ID", "Vendor Name", "Retailer Vendor ID", "Invoice Number", "Invoice Date",
                 "Invoice Due Date", "Invoice Total", "Product Number", "Product Description", "UPC",
                 "Unit Of Measure", "Item Quantity", "Item Cost", "Extended Cost"])
    for inv in invoices:
        name = w.DISTRIBUTORS[inv["distributor"]][0]
        total = round(sum(l[5] for l in inv["lines"]), 2)
        for code, desc, uom, qty, unit, ext in inv["lines"]:
            wr.writerow(["HALSTED & RYE", LOCATION_ID, name, w.DISTRIBUTORS[inv["distributor"]][1], inv["number"],
                         inv["date"].strftime("%m/%d/%Y"), (inv["date"] + timedelta(days=30)).strftime("%m/%d/%Y"),
                         f"{total:.2f}", code, desc, "", uom, qty, f"{unit:.2f}", f"{ext:.2f}"])
    return buf.getvalue()


# ── kitchen: Back Office ────────────────────────────────────────────────────

GL = {"FOOD - MEAT": "5010", "FOOD - POULTRY": "5015", "FOOD - SEAFOOD": "5020", "FOOD - PRODUCE": "5030",
      "FOOD - DAIRY": "5040", "FOOD - BAKERY": "5050", "FOOD - DRY GOODS": "5060", "FOOD - FROZEN": "5070"}
COA = [("5010", "FOOD - MEAT", "Purchase"), ("5015", "FOOD - POULTRY", "Purchase"),
       ("5020", "FOOD - SEAFOOD", "Purchase"), ("5030", "FOOD - PRODUCE", "Purchase"),
       ("5040", "FOOD - DAIRY", "Purchase"), ("5050", "FOOD - BAKERY", "Purchase"),
       ("5060", "FOOD - DRY GOODS", "Purchase"), ("5070", "FOOD - FROZEN", "Purchase"),
       ("5110", "BEER PURCHASES", "Purchase"), ("5120", "LIQUOR PURCHASES", "Purchase"),
       ("5130", "WINE PURCHASES", "Purchase"), ("5140", "NA BEVERAGE - SODA & COFFEE", "Purchase"),
       ("5150", "BAR CONSUMABLES & MIXERS", "Purchase"), ("5310", "PAPER & TO-GO", "Purchase"),
       ("5320", "CLEANING & CHEMICALS", "Purchase"), ("1410", "FOOD INVENTORY", "Purchase Inventory"),
       ("6100", "REPAIRS & MAINTENANCE", "Expense"), ("6200", "LINEN SERVICE", "Expense")]
FOOD_VENDOR_DAYS = {"Sysco Chicago": (1, 4), "Testa Produce": (0, 2, 4), "Fortune Fish & Gourmet": (2, 5),
                    "Turano Baking Co": (0, 2, 4, 5)}
# Supplies the menu never rings, bought on the same trucks: vendor -> [(code, description, gl, unit, weekly $)]
SUNDRIES = {"Sysco Chicago": [("8810021", "Container Foam Hinged 9x9", "5310", "Case", 92.0),
                              ("8810412", "Cup Plastic 16oz Clear", "5310", "Case", 61.0),
                              ("8820119", "Sanitizer Quat Concentrate", "5320", "Case", 48.0),
                              ("8830044", "Cherries Maraschino Stemless", "5150", "Case", 34.0),
                              ("8830102", "Bloody Mary Mix", "5150", "Case", 38.0)]}
VENDOR_OTHER = [("Reyes Coca-Cola Bottling", "5140", 310.0, (1,)), ("Ecolab", "5320", 240.0, (2,)),
                ("Cintas", "6200", 165.0, (3,)), ("Metropolis Coffee", "5140", 95.0, (4,))]


def food_invoices(start: date, end: date) -> list:
    """[{vendor, number, date, lines: [{item_code, description, gl, purchase_unit, qty, unit_amount, ext_amount}]}].
    Each delivery is what the kitchen used since that vendor's last one — the same day always gives the same
    invoice."""
    out = []
    day = start
    while day <= end:
        for vendor, days in FOOD_VENDOR_DAYS.items():
            if day.weekday() not in days:
                continue
            used = _window_use(day, days, "food")
            rng = _rng("food", vendor, day.isoformat())
            lines = []
            for key, it in w.FOOD_ITEMS.items():
                if it[2] != vendor:
                    continue
                name, gl, _v, sku, pu, per_pack, pack_unit, price = it
                cases = int(used.get(key, 0.0) * FOOD_LOSS / per_pack + rng.uniform(0.1, 0.9))
                if cases <= 0:
                    continue
                drift = 1.0 + 0.035 * ((day - date(2025, 9, 1)).days / 365.0) + rng.gauss(0, 0.012)
                unit = round(price * drift, 2)
                if gl in ("FOOD - MEAT", "FOOD - POULTRY", "FOOD - SEAFOOD") and pack_unit == "Pound":
                    # Catch weight: billed by the pound on the case's real weight.
                    lbs = round(cases * per_pack * rng.uniform(0.97, 1.03), 2)
                    if abs(lbs - round(lbs)) < 0.02:
                        lbs += 0.13
                    per_lb = round(unit / per_pack, 2)
                    lines.append({"item_code": sku, "description": name.upper(), "gl": GL[gl], "purchase_unit": "Case",
                                  "qty": lbs, "unit_amount": per_lb, "ext_amount": round(lbs * per_lb, 2)})
                else:
                    lines.append({"item_code": sku, "description": name.upper(), "gl": GL[gl], "purchase_unit": "Case",
                                  "qty": cases, "unit_amount": unit, "ext_amount": round(cases * unit, 2)})
            for code, desc, gl, unit, weekly in SUNDRIES.get(vendor, []):
                if rng.random() < 0.5:
                    lines.append({"item_code": code, "description": desc.upper(), "gl": gl, "purchase_unit": unit,
                                  "qty": 1, "unit_amount": round(weekly / 2, 2), "ext_amount": round(weekly / 2, 2)})
            if not lines:
                continue
            prefix = {"Sysco Chicago": "SY", "Testa Produce": "TP", "Fortune Fish & Gourmet": "FF",
                      "Turano Baking Co": "TB"}[vendor]
            out.append({"vendor": vendor, "number": f"{prefix}{day.strftime('%y%m%d')}", "date": day, "lines": lines})
        for vendor, gl, weekly, days in VENDOR_OTHER:
            if day.weekday() in days:
                rng = _rng("other", vendor, day.isoformat())
                amt = round(weekly * rng.uniform(0.85, 1.15), 2)
                out.append({"vendor": vendor, "number": f"{vendor[:2].upper()}{day.strftime('%y%m%d')}",
                            "date": day, "lines": [{"item_code": None, "description": "SERVICE / DELIVERY",
                                                    "gl": gl, "purchase_unit": None, "qty": None,
                                                    "unit_amount": None, "ext_amount": amt}]})
        day += timedelta(days=1)
    return out


def recipe_book(today: date) -> dict:
    """Back Office's recipe book: every dish's recipe, the preps, every bought item."""
    keys = list(w.FOOD_ITEMS)
    idx = {k: i for i, k in enumerate(keys)}
    items = []
    for k in keys:
        name, gl, vendor, sku, pu, per_pack, pack_unit, price = w.FOOD_ITEMS[k]
        items.append({"name": name, "gl": f"{GL[gl]} {gl}", "vendors": [{
            "vendor": vendor, "sku": sku, "purchase_unit": pu, "per_pack": str(per_pack), "pack_unit": pack_unit,
            "price": f"{price:.2f}", "last_purchased": (today - timedelta(days=5)).strftime("%-m/%-d/%Y"),
            "yield": "100%"}]})

    def ing(name, qty, unit):
        if name.startswith("@"):
            return {"name": name[1:], "qty": str(qty), "unit": unit, "prep": name[1:]}
        return {"name": w.FOOD_ITEMS[name][0], "qty": str(qty), "unit": unit, "item": idx[name]}

    recipes, pos_names = [], {}
    for prep, (group, (amount, unit), ings) in w.PREP.items():
        y = {"weight": amount, "weight_unit": unit} if unit in WEIGHT else {"volume": amount, "volume_unit": unit}
        recipes.append({"name": prep, "group": group, "ingredients": [ing(*g) for g in ings], "batch_yield": y})
    # A few dishes the kitchen has not written up yet.
    unwritten = {"Kids Mac & Cheese", "Taco Bar for 20", "Skillet Cookie", "Shrimp Cocktail"}
    for cat, rows in w.FOOD_MENU.items():
        group = "MENU ITEMS > " + ("FOOD ADD-ON MODS" if cat.startswith("(") else cat.upper())
        for name, _price, recipe in rows:
            if name in unwritten:
                continue
            rname = name
            recipes.append({"name": rname, "group": group, "ingredients": [ing(*g) for g in recipe],
                            "batch_yield": {"each": 1, "each_unit": "Each"}})
            pos_names[rname] = [name]
    return {"recipes": recipes, "items": items, "pos_names": pos_names, "source": "Back Office recipe export"}
