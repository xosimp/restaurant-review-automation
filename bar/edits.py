"""
bar/edits.py — the owner's writes on the Bar page, checked before they
land (bar fix round 10/15/26, V21 / V40 / SEC-4 / SEC-14).

The routes (mobile_api's one body each, client_api's twins) are account
holder only (permissions.principal_only) and log every change; this module
holds what they check and what they need:

- `check_product_key`: a key is at most MAX_KEY_CHARS and must be a product
  this restaurant actually bought (a line of its own invoices) — a script
  could otherwise write unlimited junk rows that every build loads.
- `set_product_category`: the owner sorts a product into one of
  OWNER_CATEGORIES (beer, liquor, wine, non-alcoholic, bar supplies) or
  clears it back to Cavnar AI's guess. Fees, deposits and "not sorted" are
  never an owner's choice: moving a bottle to "fee" took it out of the
  liquor figure entirely. Returns before/after for the account log.
- `owner_sorts` / `annotate_products`: who sorted each product, by name,
  and whether it was the login looking ("set by you" only then).

Every write is stored under the ACTING login (permissions.acting_login_id):
the admin behind a view-as session, never the owner it views as.
"""
from bar.classify import product_key, set_category
from bar.common import LABELS, get_conn

MAX_KEY_CHARS = 300
OWNER_CATEGORIES = ("beer", "liquor", "wine", "na", "supplies")
SUPPORT_NAME = "Cavnar AI support"


def owner_category_choices() -> list:
    """[{key, label}] — the categories an owner may sort a product into."""
    return [{"key": c, "label": LABELS[c]} for c in OWNER_CATEGORIES]


def _clean_key(key) -> str:
    k = str(key or "").strip()
    if not k:
        raise ValueError("Which product?")
    if len(k) > MAX_KEY_CHARS:
        raise ValueError("That product isn't one of this restaurant's.")
    return k


def known_product_keys(restaurant_id, db_path=None) -> set:
    """Every product key of this restaurant's own invoice lines (the key
    bar.purchases gives each line), read from its distinct products."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT i.distributor, l.item_code, l.description FROM fintech_invoice_lines l "
            "JOIN fintech_invoices i ON i.id=l.invoice_id WHERE i.restaurant_id=?",
            (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    return {product_key(r["distributor"], r["item_code"], r["description"]) for r in rows}


def check_product_key(restaurant_id, key, db_path=None, known=None) -> str:
    """The key, trimmed — or ValueError (an owner sentence) when it is
    empty, too long, or no line of this restaurant's invoices has it."""
    k = _clean_key(key)
    keys = known if known is not None else known_product_keys(restaurant_id, db_path)
    if k not in keys:
        raise ValueError("That product isn't one of this restaurant's.")
    return k


def current_category(restaurant_id, key, db_path=None):
    """The owner's sort for `key`, or None (Cavnar AI's guess stands)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT category FROM bar_product_categories WHERE restaurant_id=? AND product_key=?",
                           (int(restaurant_id), str(key))).fetchone()
    finally:
        conn.close()
    return row["category"] if row else None


def set_product_category(restaurant_id, key, category, acting_id=None, db_path=None) -> dict:
    """The owner sorts one product (OWNER_CATEGORIES) or clears it ('' or
    None). {ok, product_key, category, before, after}. ValueError with an
    owner sentence on a bad key or category."""
    k = check_product_key(restaurant_id, key, db_path)
    cat = str(category or "").strip().lower()
    if cat and cat not in OWNER_CATEGORIES:
        raise ValueError("Pick beer & seltzer, liquor, wine, non-alcoholic or bar supplies.")
    before = current_category(restaurant_id, k, db_path)
    out = set_category(restaurant_id, k, cat, user_id=acting_id, db_path=db_path)
    out.update(before=before, after=cat or None)
    return out


def owner_sorts(restaurant_id, db_path=None) -> dict:
    """{product_key: {set_by, set_at, set_by_name}} for every product the
    owner sorted. A support login (an admin, or a view-as with no admin
    named) reads as Cavnar AI support."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT product_key, set_by, set_at FROM bar_product_categories WHERE restaurant_id=?",
                            (int(restaurant_id),)).fetchall()
        ids = sorted({int(r["set_by"]) for r in rows if r["set_by"] is not None and int(r["set_by"]) > 0})
        names = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            try:
                found = conn.execute(f"SELECT id, username, is_admin FROM users WHERE id IN ({marks})", ids).fetchall()
            except Exception as e:
                # A name we can't read is no name — the sort itself still shows.
                print(f"[bar.edits] login names unreadable for {restaurant_id}: {e}")
                found = []
            for u in found:
                names[int(u["id"])] = (SUPPORT_NAME if u["is_admin"] else _display(u["username"]))
    finally:
        conn.close()
    out = {}
    for r in rows:
        sb = r["set_by"]
        if sb is None:
            name = None
        elif int(sb) <= 0:
            name = SUPPORT_NAME
        else:
            name = names.get(int(sb))
        out[r["product_key"]] = {"set_by": sb, "set_at": r["set_at"], "set_by_name": name}
    return out


def _display(username) -> str:
    n = str(username or "").strip()
    if "@" in n:
        n = n.split("@", 1)[0]
    return n[:1].upper() + n[1:] if n else ""


def annotate_products(rows, sorts, viewer_id) -> list:
    """Copies of the payload's product rows with set_by, set_by_name and
    set_by_you (True only when this login sorted it) on the owner's sorts."""
    out = []
    for p in rows or []:
        if not isinstance(p, dict):
            out.append(p)
            continue
        q = dict(p)
        s = sorts.get(p.get("product_key")) if p.get("owner_set") else None
        q["set_by"] = s["set_by"] if s else None
        q["set_by_name"] = s["set_by_name"] if s else None
        q["set_by_you"] = bool(s and viewer_id is not None and s["set_by"] is not None
                               and int(s["set_by"]) == int(viewer_id))
        out.append(q)
    return out
