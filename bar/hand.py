"""
bar/hand.py — bottles the owner buys outside the distributors' invoices
(owner, 10/29/26: "build the hand-added bottle option").

EJ's pours Bombay Sapphire, Malibu, Knob Creek, Dewar's 12, Frangelico,
Kahlua and Moët, and none of them is on a Fintech line since 5/4/26: they
come from Binny's or Savway, paid through Back Office as one total with no
bottle named. The drink picker lists only invoiced products, so those drinks
could never be matched or costed.

A hand-added bottle is the owner's word: its name, category, size, what a
bottle cost them and where they buy it (`bar_hand_products`, created at boot
by bar.products.init_bar_products). It joins the "Pours from" picker under
the key `hand|<id>`, and a drink matched to it is costed from that price
through the same pack and pour as an invoice product (bar.margins, `series`).

It is never a purchase. No unit is bought and nothing is added to spend or
to bought vs sold: its money is already in Back Office's totals (the page's
`outside_fintech`), and bought vs sold lists its drinks apart — poured from
a bottle bought elsewhere — never as a shortfall (bar.variance).
"""
import re
import uuid

from bar.common import get_conn

PREFIX = "hand|"
CATEGORIES = ("liquor", "wine", "beer")
SIZE_ML = (50.0, 5000.0)
PRICE = (0.5, 1000.0)
_ML_PER_OZ = 29.5735
# Container sizes a can or a bottle is named in ounces ("12OZ"), never in
# millilitres: the label parse_pack reads back.
_OZ_SIZES = (8.0, 12.0, 16.0, 19.2, 24.0)
# The least a bottle of each costs (bar.margins._FLOOR): under it the price
# was mistyped, and the margin check would refuse to judge a drink on it.
_FLOOR = {"liquor": 5.0, "wine": 2.5}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ml|l|lt|liter|litre|oz|fl oz)?\s*$", re.I)


def is_hand(key) -> bool:
    return str(key or "").startswith(PREFIX)


def parse_size(value) -> float:
    """Millilitres from what the owner typed — 750, "750 ml", "1 L",
    "1.75L", "12 oz" — or ValueError (an owner sentence). A bare number is
    millilitres."""
    if isinstance(value, bool):
        raise ValueError("Give the bottle's size, like 750 ml or 1 L.")
    m = _SIZE_RE.match(str(value if value is not None else ""))
    if not m:
        raise ValueError("Give the bottle's size, like 750 ml or 1 L.")
    n, unit = float(m.group(1)), (m.group(2) or "ml").lower()
    ml = n * 1000 if unit in ("l", "lt", "liter", "litre") else (n * _ML_PER_OZ if "oz" in unit else n)
    if not SIZE_ML[0] <= ml <= SIZE_ML[1]:
        raise ValueError("A bottle is between 50 ml and 5 L.")
    return round(ml, 1)


def size_label(ml) -> str:
    """The size as an invoice spells it — "750ML", "1L", "1.75L", "12OZ" —
    so the pack reads back from the name exactly (bar.products.parse_pack)."""
    ml = float(ml)
    for oz in _OZ_SIZES:
        if abs(ml - oz * _ML_PER_OZ) < 1.0:
            return f"{oz:g}OZ"
    return f"{ml / 1000:g}L" if ml >= 1000 else f"{ml:g}ML"


def description(row) -> str:
    """The bottle as an invoice line would name it: its name and size."""
    return f"{row['name']} {size_label(row['size_ml'])}"


def _row(r, drinks=None) -> dict:
    d = dict(r)
    d["size_ml"] = float(d["size_ml"])
    d["price"] = float(d["price"])
    d["size_label"] = size_label(d["size_ml"])
    d["product"] = description(d)
    if drinks is not None:
        d["drinks"] = int(drinks.get(d["product_key"], 0))
    return d


def hand_products(restaurant_id, db_path=None) -> list:
    """[{product_key, name, category, size_ml, size_label, product, price,
    source, set_at, drinks}] — the owner's bottles, A to Z; `drinks` how many
    POS drinks pour each."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT product_key, name, category, size_ml, price, source, set_by, set_at "
                            "FROM bar_hand_products WHERE restaurant_id=? ORDER BY name, size_ml",
                            (int(restaurant_id),)).fetchall()
        counts = {}
        for r in conn.execute("SELECT product_key, COUNT(*) FROM bar_drink_map WHERE restaurant_id=? "
                              "AND product_key LIKE 'hand|%' GROUP BY product_key", (int(restaurant_id),)).fetchall():
            counts[r[0]] = int(r[1])
    finally:
        conn.close()
    return [_row(r, counts) for r in rows]


def keys(restaurant_id, db_path=None) -> set:
    return {r["product_key"] for r in hand_products(restaurant_id, db_path)}


def save(restaurant_id, name, category, size, price, source=None, key=None, user_id=None, db_path=None) -> dict:
    """Add a bottle, or change one (`key`): {ok, row, added}. The same name
    and size as one already kept is that bottle, changed — never a second
    copy. ValueError (an owner sentence) for anything out of bounds; a `key`
    not this restaurant's is a LookupError."""
    from bar.products import _now
    name = " ".join(str(name or "").split())
    if not 2 <= len(name) <= 60 or not re.search(r"[A-Za-z]", name):
        raise ValueError("Name the bottle — 2 to 60 characters, like Bombay Sapphire.")
    if category not in CATEGORIES:
        raise ValueError("Pick liquor, wine or beer.")
    ml = parse_size(size)
    try:
        p = float(str(price).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        raise ValueError("Give what one bottle cost you, like 32.99.")
    if p != p or not PRICE[0] <= p <= PRICE[1]:
        raise ValueError("A bottle's price is between $0.50 and $1,000.")
    if category in _FLOOR and ml >= 100 and p < _FLOOR[category]:
        raise ValueError(f"A bottle of {category} costs more than ${_FLOOR[category]:g} — check the price.")
    source = " ".join(str(source or "").split())[:60] or None
    conn = get_conn(db_path)
    try:
        if key:
            hit = conn.execute("SELECT product_key FROM bar_hand_products WHERE restaurant_id=? AND product_key=?",
                               (int(restaurant_id), str(key))).fetchone()
            if not hit:
                raise LookupError("That bottle isn't one of this restaurant's.")
        else:
            for r in conn.execute("SELECT product_key, name, size_ml FROM bar_hand_products WHERE restaurant_id=?",
                                  (int(restaurant_id),)).fetchall():
                if r["name"].casefold() == name.casefold() and abs(float(r["size_ml"]) - ml) < 1.0:
                    key = r["product_key"]
                    break
        added = not key
        if added:
            key = PREFIX + uuid.uuid4().hex[:12]
            conn.execute("INSERT INTO bar_hand_products (restaurant_id, product_key, name, category, size_ml, price, "
                         "source, set_by, set_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (int(restaurant_id), key, name, category, ml, round(p, 2), source, user_id, _now()))
        else:
            conn.execute("UPDATE bar_hand_products SET name=?, category=?, size_ml=?, price=?, source=?, set_by=?, "
                         "set_at=? WHERE restaurant_id=? AND product_key=?",
                         (name, category, ml, round(p, 2), source, user_id, _now(), int(restaurant_id), key))
        conn.commit()
    finally:
        conn.close()
    row = next(r for r in hand_products(restaurant_id, db_path) if r["product_key"] == key)
    return {"ok": True, "row": row, "added": added}


def remove(restaurant_id, key, db_path=None) -> dict:
    """Take a bottle off: {ok, product_key, cleared} — every drink that
    poured it goes back to Drinks to match (`cleared` of them). LookupError
    when it isn't this restaurant's."""
    conn = get_conn(db_path)
    try:
        hit = conn.execute("SELECT name FROM bar_hand_products WHERE restaurant_id=? AND product_key=?",
                           (int(restaurant_id), str(key))).fetchone()
        if not hit:
            raise LookupError("That bottle isn't one of this restaurant's.")
        cleared = conn.execute("SELECT COUNT(*) FROM bar_drink_map WHERE restaurant_id=? AND product_key=?",
                               (int(restaurant_id), str(key))).fetchone()[0]
        conn.execute("DELETE FROM bar_drink_map WHERE restaurant_id=? AND product_key=?", (int(restaurant_id), str(key)))
        conn.execute("DELETE FROM bar_hand_products WHERE restaurant_id=? AND product_key=?",
                     (int(restaurant_id), str(key)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "product_key": key, "name": hit["name"], "cleared": int(cleared or 0)}


def series(row) -> dict:
    """The price watch's shape for one bottle (bar.margins._series_for): its
    price the frontline tier, its line named with its size, sold by the
    bottle — so the pack and the pour are read as an invoice line's are."""
    return {"tiers": {"BT": {"tier": 1, "reference": float(row["price"]), "change": None}},
            "line": {"product_key": row["product_key"], "description": description(row), "uom": "BT",
                     "category": row["category"], "ppc": None}}


def picker_rows(rows) -> list:
    """The pickers' compact rows (bar.cache.trim's `pickable` shape)."""
    return [{"product_key": r["product_key"], "product": r["product"], "category": r["category"],
             "distributor": "Bought elsewhere" + (f" · {r['source']}" if r.get("source") else ""), "hand": True}
            for r in rows]
