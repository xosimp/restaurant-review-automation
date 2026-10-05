"""
plu_map.py — Back Office's PLU numbers against the POS's own items.

Why it exists (10/5/26): Simple EJ's Back Office exports (item costs,
recipes, counts, invoices) name every item by PLU, and RPOWER's API carries
no PLU for EJ's items (18 of 924 have one, all internal codes like "CCFEE").
Jim's Consolidated Product Mix lists each PLU with Back Office's name and
what it sold, so the two are joined by name, with quantity as the check.

A PLU is not unique at EJ's — 2159 is Coors Light and also a Woodford
add-on — so a row is keyed by the PLU AND Back Office's name for it, and
`item_for` answers a bare PLU only when that PLU names one item.

How a line matches (`match`, pure):
  name        its name, normalised, is one POS item's name
  name+qty    several POS items carry that name; the one whose quantity over
              the report's dates equals the line's
  qty+sales   no name match; the one item not yet taken with the line's
              quantity and sales within 2% (a renamed item)
  None        left for review — never guessed
A row an owner set by hand (`how` = "manual") is never overwritten by a
later import.
"""
import re

from models import DB_PATH

_SPACE = re.compile(r"[^a-z0-9$]+")


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path or models.DB_PATH)


def normalize(name) -> str:
    """One spelling for a name in either system: case, spacing and
    punctuation folded, apostrophes dropped ("EJ's" == "EJs"), and RPOWER's
    backtick between a name and its variant read as a space ("Coors
    Light`Bucket" == "Coors Light Bucket")."""
    text = str(name or "").lower().replace("`", " ")
    text = re.sub(r"['’]", "", text)
    return _SPACE.sub(" ", text).strip()


def match(lines, menu, sold=None) -> list:
    """[{plu, bo_name, bo_category, item_id, pos_name, how, bo_qty, pos_qty}]
    for Back Office `lines` ([{plu, name, qty, gross, category}]) against the
    POS `menu` ({item_id: name}) and what each item `sold` over the same
    dates ({item_id: (qty, sales)})."""
    sold = sold or {}
    names = {mid: normalize(n) for mid, n in menu.items()}
    by_name = {}
    for mid, n in names.items():
        by_name.setdefault(n, []).append(mid)

    def qty(mid):
        q = (sold.get(mid) or (None,))[0]
        return None if q is None else round(float(q))

    out, taken = [], set()
    for ln in lines:
        n, q = normalize(ln.get("name")), ln.get("qty")
        cands = by_name.get(n) or []
        mid = how = None
        if len(cands) == 1:
            mid, how = cands[0], "name"
        elif cands and q is not None:
            same = [m for m in cands if qty(m) == round(float(q))]
            if len(same) == 1:
                mid, how = same[0], "name+qty"
        if mid is None and not cands and q:
            gross = float(ln.get("gross") or 0)
            same = [m for m, (sq, ss) in sold.items() if m in menu and m not in taken
                    and round(float(sq)) == round(float(q)) and abs(float(ss) - gross) <= max(1.0, 0.02 * abs(gross))]
            if len(same) == 1:
                mid, how = same[0], "qty+sales"
        if mid:
            taken.add(mid)
        out.append({"plu": str(ln.get("plu")), "bo_name": str(ln.get("name") or "").strip(),
                    "bo_category": ln.get("category"), "item_id": mid, "pos_name": menu.get(mid) if mid else None,
                    "how": how, "bo_qty": q, "pos_qty": qty(mid) if mid else None})
    return out


def save(restaurant_id, rows, source, provider="rpower", db_path=DB_PATH) -> dict:
    """Write matched rows; a manual row stays as the owner set it. Returns
    {"written", "kept_manual", "unmatched"}."""
    conn = get_conn(db_path)
    written = kept = unmatched = 0
    try:
        manual = {(r["plu"], r["bo_name"]) for r in conn.execute(
            "SELECT plu, bo_name FROM pos_item_plus WHERE restaurant_id=? AND how='manual'", (restaurant_id,))}
        for r in rows:
            key = (r["plu"], r["bo_name"])
            if key in manual:
                kept += 1
                continue
            conn.execute(
                "INSERT INTO pos_item_plus (restaurant_id, plu, bo_name, bo_category, provider, item_id, pos_name, how, "
                "bo_qty, pos_qty, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(restaurant_id, plu, bo_name) DO UPDATE SET bo_category=excluded.bo_category, "
                "provider=excluded.provider, item_id=excluded.item_id, pos_name=excluded.pos_name, how=excluded.how, "
                "bo_qty=excluded.bo_qty, pos_qty=excluded.pos_qty, source=excluded.source, updated_at=datetime('now')",
                (restaurant_id, r["plu"], r["bo_name"], r.get("bo_category"), provider, r.get("item_id"),
                 r.get("pos_name"), r.get("how"), r.get("bo_qty"), r.get("pos_qty"), source))
            written += 1
            unmatched += 0 if r.get("item_id") else 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "kept_manual": kept, "unmatched": unmatched}


def set_manual(restaurant_id, plu, bo_name, item_id, pos_name=None, db_path=DB_PATH):
    """An owner's (or support's) answer for one line: kept over any import."""
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pos_item_plus (restaurant_id, plu, bo_name, item_id, pos_name, how, source, updated_at) "
            "VALUES (?,?,?,?,?,'manual','manual',datetime('now')) ON CONFLICT(restaurant_id, plu, bo_name) DO UPDATE "
            "SET item_id=excluded.item_id, pos_name=excluded.pos_name, how='manual', source='manual', "
            "updated_at=datetime('now')", (restaurant_id, str(plu), str(bo_name).strip(), item_id, pos_name))
        conn.commit()
    finally:
        conn.close()


def item_for(restaurant_id, plu, bo_name=None, db_path=DB_PATH):
    """The POS item a Back Office line is: by PLU and name when the name is
    given, else by the PLU alone only when it names one item. None when
    unknown or ambiguous — never a guess."""
    conn = get_conn(db_path)
    try:
        if bo_name:
            row = conn.execute("SELECT item_id FROM pos_item_plus WHERE restaurant_id=? AND plu=? AND bo_name=?",
                               (restaurant_id, str(plu), str(bo_name).strip())).fetchone()
            if row:
                return row["item_id"]
            rows = conn.execute("SELECT bo_name, item_id FROM pos_item_plus WHERE restaurant_id=? AND plu=?",
                                (restaurant_id, str(plu))).fetchall()
            hit = [r["item_id"] for r in rows if normalize(r["bo_name"]) == normalize(bo_name)]
            return hit[0] if len(hit) == 1 else None
        items = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM pos_item_plus WHERE restaurant_id=? AND plu=?", (restaurant_id, str(plu)))}
        return next(iter(items)) if len(items) == 1 else None
    finally:
        conn.close()


def unmatched(restaurant_id, db_path=DB_PATH) -> list:
    """Back Office lines with no POS item yet, most sold first — the review list."""
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT plu, bo_name, bo_category, bo_qty FROM pos_item_plus WHERE restaurant_id=? AND item_id IS NULL "
            "ORDER BY COALESCE(bo_qty, 0) DESC", (restaurant_id,))]
    finally:
        conn.close()
