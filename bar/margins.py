"""
bar/margins.py — the drink margin check (F11, 10/15/26): what a drink costs
to pour, as a share of its menu price, and the price that restores the
share it had before its product's price changed.

Read only for drinks the owner confirmed are one bought product (a bottle or
can sold as it comes, or a single-spirit pour at a size): `bar_drink_map`
(created at boot by bar.products.init_bar_products). Cavnar AI suggests the
pairs by name, never uses an unconfirmed one. Menu prices are the POS's own
(`pos_menu_prices`, synced nightly); unit costs are the price watch's usual
price for the product (bar.prices) through its pack (bar.products).
"""
import re

from bar.common import _mdy, display_name, get_conn
from bar.products import _now, parse_pack, unit_costs

_WORD = re.compile(r"[A-Z]{3,}")
_STOP = {"THE", "AND", "BTL", "CAN", "CASE", "LIQ", "VDK", "WHSKY", "BRBN", "TEQ", "750ML", "BAR", "LOOSE"}


def drink_margin(cost_per_drink, menu_price, cost_before=None) -> dict:
    """{cost, price, cost_pct, cost_pct_before, restore_price}: the drink's
    cost as a share of its price now and before the change, and the menu
    price that brings the share back to what it was."""
    c, p = float(cost_per_drink), float(menu_price)
    out = {"cost": round(c, 2), "price": round(p, 2), "cost_pct": round(c / p * 100, 1) if p else None,
           "cost_pct_before": None, "restore_price": None}
    if cost_before and p:
        before = float(cost_before) / p
        out["cost_pct_before"] = round(before * 100, 1)
        if before > 0 and c > float(cost_before):
            out["restore_price"] = round(c / before, 2)
    return out


# The owner's word that a POS item is not to be matched at all, kept in the
# same table under a "skip:<reason>" key (owner, 10/16/26). drink_map never
# returns those rows; set_aside_map does.
SKIP_REASONS = {"not_a_drink": "Not a drink", "not_on_invoices": "Not bought on any invoice"}


def drink_map(restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT provider, item_id, product_key, pour_oz FROM bar_drink_map WHERE restaurant_id=? "
                            "AND product_key NOT LIKE 'skip:%'", (int(restaurant_id),)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def set_aside_map(restaurant_id, db_path=None) -> dict:
    """{(provider, item_id): reason} - the POS items the owner set aside."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT provider, item_id, product_key FROM bar_drink_map WHERE restaurant_id=? "
                            "AND product_key LIKE 'skip:%'", (int(restaurant_id),)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {(r["provider"], str(r["item_id"])): r["product_key"][5:] for r in rows}


def set_drink_map(restaurant_id, provider, item_id, key, pour_oz=None, user_id=None, db_path=None) -> dict:
    """The owner's word that a POS item is one bought product at a pour
    (no pour: a bottle or can sold as it comes). An empty key clears it."""
    if key and str(key).startswith("skip:") and str(key)[5:] not in SKIP_REASONS:
        raise ValueError("Set it aside as not a drink, or as not bought on any invoice.")
    oz = float(pour_oz) if pour_oz not in (None, "") and not str(key or "").startswith("skip:") else None
    # 128 oz: a tower pours 100 (3 L, Will 10/15/26), a gallon pitcher 128.
    if oz is not None and not 0.25 <= oz <= 128:
        raise ValueError("A pour is between 0.25 and 128 oz.")
    conn = get_conn(db_path)
    try:
        if not key:
            conn.execute("DELETE FROM bar_drink_map WHERE restaurant_id=? AND provider=? AND item_id=?",
                         (int(restaurant_id), str(provider), str(item_id)))
        else:
            conn.execute("INSERT INTO bar_drink_map (restaurant_id, provider, item_id, product_key, pour_oz, set_by, "
                         "set_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, provider, item_id) DO UPDATE SET "
                         "product_key=excluded.product_key, pour_oz=excluded.pour_oz, set_by=excluded.set_by, "
                         "set_at=excluded.set_at",
                         (int(restaurant_id), str(provider), str(item_id), str(key), oz, user_id, _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "provider": provider, "item_id": item_id, "product_key": key or None, "pour_oz": oz}


# The window the price level a drink is mostly rung at is read over — the
# same window pos_archive.sync_prices tracks levels over.
MAIN_LEVEL_DAYS = 28


def _rung_levels(conn, restaurant_id, since):
    """({(provider, item_id): level_id rung most}, {provider: level_id rung
    most store-wide}) from the archive's sale lines since `since`."""
    per_item, store = {}, {}
    try:
        rows = conn.execute(
            "SELECT provider, item_id, price_level_id, COUNT(*) AS n FROM pos_ticket_lines WHERE restaurant_id=? "
            "AND kind='sale' AND business_date>=? AND price_level_id IS NOT NULL AND item_id IS NOT NULL "
            "GROUP BY provider, item_id, price_level_id", (int(restaurant_id), since)).fetchall()
    except Exception:
        return {}, {}
    best, totals = {}, {}
    for r in rows:
        k = (r["provider"], str(r["item_id"]))
        n = int(r["n"] or 0)
        if n > best.get(k, (None, -1))[1]:
            best[k] = (str(r["price_level_id"]), n)
        t = totals.setdefault(r["provider"], {})
        t[str(r["price_level_id"])] = t.get(str(r["price_level_id"]), 0) + n
    per_item = {k: v[0] for k, v in best.items()}
    store = {p: max(t.items(), key=lambda kv: kv[1])[0] for p, t in totals.items() if t}
    return per_item, store


def _menu(restaurant_id, db_path=None, today=None) -> dict:
    """(provider, item_id) -> {name, price, level, level_basis}: the price
    of the level the drink is MOSTLY RUNG AT (Audit #23 A3-13) — its own most
    rung level over MAIN_LEVEL_DAYS of archived sales, else the store's main
    level (the one sync_prices names), else a level named regular/default,
    else the highest. EJ's levels are "Level 1..9", so the highest won: a $9
    bottle with a $12 event level was costed at $12, its cost % a quarter
    low, and "restore price" wrong."""
    from datetime import date as _date, timedelta as _td
    if today is None:
        try:
            import pos
            today = pos.local_complete_through(restaurant_id) + _td(days=1)
        except Exception:
            today = _date.today()
    since = (today - _td(days=MAIN_LEVEL_DAYS)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT provider, item_id, item_name, level_id, level, price FROM pos_menu_prices "
                            "WHERE restaurant_id=?", (int(restaurant_id),)).fetchall()
        per_item, store = _rung_levels(conn, restaurant_id, since)
    except Exception:
        rows, per_item, store = [], {}, {}
    finally:
        conn.close()
    by = {}
    for r in rows:
        k = (r["provider"], str(r["item_id"]))
        lvl = str(r["level_id"]) if r["level_id"] is not None else None
        if lvl is not None and lvl == per_item.get(k):
            rank, basis = 3, "the level it was rung at most"
        elif lvl is not None and k not in per_item and lvl == store.get(r["provider"]):
            rank, basis = 2, "the store's main price level"
        elif re.search(r"reg|default|base|standard", str(r["level"] or ""), re.I):
            rank, basis = 1, "the regular price level"
        else:
            rank, basis = 0, "the highest price level"
        cur = by.get(k)
        if cur is None or rank > cur["rank"] or (rank == cur["rank"] and float(r["price"]) > cur["price"]):
            by[k] = {"name": r["item_name"], "price": float(r["price"]), "rank": rank, "reg": rank == 1,
                     "level": r["level"], "level_basis": basis}
    return by


def _words(s) -> set:
    return {w for w in _WORD.findall(str(s or "").upper()) if w not in _STOP}


def _series_for(analysis, key):
    canon = analysis["canon"]
    ck = canon.get(key, key)
    found = [(sk, v) for sk, v in analysis["series"].items() if sk[0] == ck]
    order = {"BO": 0, "BT": 0, "CA": 1, "CS": 1, "KE": 2, "KG": 2}
    found.sort(key=lambda x: order.get(str(x[0][1]).upper(), 3))
    return found[0][1] if found else None


# The least a bottle of each costs: below it the line was misread.
_FLOOR = {"liquor": 5.0, "wine": 2.5}
_BUCKET_RE = re.compile(r"\bbucket\b", re.I)


def bucket_size(restaurant_id, db_path=None, days=90):
    """How many beers a bucket is at this restaurant: the beers rung as
    "<beer>`Bucket" lines over the bucket price lines' quantity, rounded, in
    the last `days` of the POS archive; None when it can't be read (no
    buckets rung, or a count outside 2-12)."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(CASE WHEN item_name LIKE ? THEN qty ELSE 0 END), "
            "SUM(CASE WHEN item_name NOT LIKE ? AND LOWER(item_name) LIKE ? THEN qty ELSE 0 END) "
            "FROM pos_ticket_lines WHERE restaurant_id=? AND business_date>=? AND kind='sale'",
            ("%`Bucket", "%`%", "%bucket%", int(restaurant_id), since)).fetchone()
    finally:
        conn.close()
    beers, buckets = (float(row[0] or 0), float(row[1] or 0)) if row else (0.0, 0.0)
    if buckets <= 0:
        return None
    n = round(beers / buckets)
    return n if 2 <= n <= 12 else None


def _brand_words(name) -> set:
    """The first two words of a product's name: on a distributor's invoice,
    its brand."""
    return {t.replace("'", "") for t in re.findall(r"[a-z0-9']+", str(name or "").lower())[:2]}


def margins(restaurant_id, analysis, pour=None, db_path=None, limit_suggestions=20, drink_items=None) -> dict:
    """{rows: [{provider, item_id, item, price, product_key, product, pour_label,
    cost, cost_pct, cost_pct_before, restore_price, changed_mdy}], suggestions:
    [{provider, item_id, item, product_key, product}], mapped, menu_items}."""
    menu = _menu(restaurant_id, db_path)
    mapped = {(m["provider"], str(m["item_id"])): m for m in drink_map(restaurant_id, db_path)}
    rows = []
    bucket_n = None
    for k, m in mapped.items():
        item = menu.get(k)
        s = _series_for(analysis, m["product_key"])
        if not item or not s:
            continue
        tiers = s["tiers"]
        st = min(tiers.values(), key=lambda x: x["tier"])  # the smallest tier: the frontline price
        l = s["line"]
        # The pour the variance counts (BAR-CALC-07, 10/16/26): a bottle-named
        # or no-pour 1:1 item is the whole bottle or can, a draft its stated
        # size — never a 5 oz glass of a bottle sold whole.
        from bar.variance import pour_target
        mode, oz, _basis, assume = pour_target(l["category"], parse_pack(l["description"], l.get("uom"), l["category"], ppc=l.get("ppc")),
                                               m.get("pour_oz"), item["name"], pour)
        if mode == "unknown":
            continue
        p = dict(pour or {})
        if mode == "oz":
            p.update(liquor_oz=oz, wine_oz=oz, draft_oz=oz)
        now = unit_costs(st["reference"], l.get("uom"), l["description"], l["category"], pour=p, ppc=l.get("ppc"))
        if not now:
            continue
        cost_now = now["per_each"] if mode == "each" else now.get("per_pour")
        if cost_now is None:
            continue
        # A bottle no spirit or wine is sold at is a misread line, never a
        # cost to judge a price on (owner, 10/27/26: a $2.29 per-bottle
        # charge stood as a bottle of Jameson Black Barrel).
        if (l["category"] in _FLOOR and (now.get("container_oz") or 0) > 3.4
                and (now.get("per_bottle") or now["per_each"]) < _FLOOR[l["category"]]):
            continue
        label = f"one {now['each']}" if mode == "each" else now["pour_label"]
        # A bucket is several of the product (EJ's: five beers a bucket, rung
        # on their own lines after the bucket's price), never one (owner,
        # 10/27/26: the $32.99 Surfside Bucket read as one can, 5.7%).
        if mode == "each" and _BUCKET_RE.search(item["name"] or ""):
            if bucket_n is None:
                bucket_n = bucket_size(restaurant_id, db_path)
            if not bucket_n:
                continue
            cost_now = cost_now * bucket_n
            label = f"{bucket_n:g} {now['each']}s (a bucket)"
        before, changed = None, None
        ch = st.get("change")
        if ch:
            b = unit_costs(ch["from"], l.get("uom"), l["description"], l["category"], pour=p, ppc=l.get("ppc")) or {}
            before, changed = (b.get("per_each") if mode == "each" else b.get("per_pour")), ch["since"]
        mm = drink_margin(cost_now, item["price"], before)
        rows.append(dict(mm, provider=k[0], item_id=k[1], item=item["name"], level=item["level"],
                         level_basis=item.get("level_basis"),
                         product_key=m["product_key"], product=display_name(l["description"]),
                         pour_label=label, confidence=now["confidence"],
                         assumes=now["assumes"] + ([assume] if assume else []),
                         changed_mdy=_mdy(changed) if changed else None))
    rows.sort(key=lambda r: -(r["cost_pct"] or 0))
    # Suggestions: menu items not mapped whose name shares words with a
    # bought liquor, wine or beer product. Never used until confirmed.
    products = {}
    from bar.classify import is_keg
    for sk, v in analysis["series"].items():
        l = v["line"]
        if l["category"] in ("liquor", "wine", "beer"):
            keg = is_keg(l["description"], l.get("uom"))
            pk = parse_pack(l["description"], l.get("uom"), l["category"], ppc=l.get("ppc"))
            cont = pk.get("container_oz") if pk.get("size_source") == "parsed" and not keg else None
            products.setdefault(l["product_key"], (display_name(l["description"]), _words(l["description"]), keg, cont,
                                                      l["category"]))
    # Only what the POS sells as a drink (owner, 10/15/26: the wing sauce
    # "Blueberry Scotch Bonnet" was offered Glenmorangie Scotch), on the
    # recipe cards' matcher: a brand word, not a spirit word, and the same
    # beer variety ("Coors Banquet" is not Coors Light).
    from bar.variance import _best_product, _words as _vwords, form_fits, variants, stated_oz, _POS_NOISE, _NOT_ITEM_RE
    vprod = {pk: (name, _vwords(name)) for pk, (name, _pw, _k, _c, _cat) in products.items() if not _NOT_ITEM_RE.search(name)}
    sugg = []
    for k, item in menu.items():
        if k in mapped or (drink_items is not None and k not in drink_items):
            continue
        w = _vwords(_POS_NOISE.sub(" ", item["name"]))
        if not w:
            continue
        want = variants(item["name"])
        # The same variety, and never across a draft / can boundary or a
        # stated container size (ING-20: "Miller Lite Draft" is not the cans).
        pool = {pk: v for pk, v in vprod.items() if variants(v[0]) == want
                and form_fits(item["name"], products[pk][4], products[pk][2], products[pk][3])}
        pk, name, score = _best_product(" ".join(sorted(w)), pool)
        # A weak name match is not shown at all (owner, 10/15/26: "hide weak
        # name matches"): it needs half its words shared, or the product's
        # brand — the first two words of its invoice name ("TITOS", "WILLIAM
        # WYCLIFF") — never only a flavour or a style ("Mango Mojito" is not
        # White Claw Mango; "Moet Brut" is not Zardetto Prosecco Brut).
        if pk and (score >= 0.5 or (_brand_words(name) & w)):
            sugg.append({"provider": k[0], "item_id": k[1], "item": item["name"], "price": item["price"],
                         "product_key": pk, "product": name, "match": round(score, 2),
                         "pour_oz": stated_oz(item["name"])})
    sugg.sort(key=lambda r: -r["match"])
    return {"rows": rows, "suggestions": sugg[:limit_suggestions], "mapped": len(mapped), "menu_items": len(menu)}
