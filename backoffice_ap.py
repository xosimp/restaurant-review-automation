"""
backoffice_ap.py — what a restaurant pays through Back Office (Buyers Edge
Platform), read from the two files Back Office lets an owner take out: the
"Invoices By Vendor" page printed to PDF, and the Chart of Accounts CSV
(Report Center → Export Chart of Accounts). Back Office has no API for
members (docs/plans/BACK_OFFICE_INTEGRATION.md).

Two readers, one intake kind ('backoffice_ap', email_intake.READERS; also
`scripts/backoffice_import.py` for a file on hand):
- the Chart of Accounts → `bo_gl_accounts`: each GL account and the Cavnar AI
  category it books (food, na_beverage, beer, wine, liquor, bar_consumables,
  discount, other_purchase, inventory_adjustment, not_cogs) — from the
  account's own type and name, never guessed past them;
- Invoices By Vendor → `bo_invoices`: each vendor's invoices and check-mode
  entries (vendor, number or "CM Entry", itemized, date, amount, the entry's
  GL when the page shows one). A file is read once (sha256); an entry is kept
  once across overlapping printouts (`entry_key`).

An entry's category is its GL account's when the page shows one (check-mode
entries do); an itemized invoice shows none, so it takes its vendor's
(`bo_vendor_categories`: the owner's choice, else a suggestion — a
distributor already on Fintech takes the pour category it sells most there,
a vendor whose own entries book to one category takes that, a name that
says food takes food — and 'unknown' otherwise, left out and named).

Read by:
- Food Cost — `food_bought_vs_sold`: food purchases against the POS's food
  sales by month and over the last four weeks, purchase-based (no count);
- the Bar — `outside_fintech_lines`: alcohol a restaurant paid for through
  Back Office that no Fintech invoice carries (a wine house paid by check, a
  liquor-store run), as bar purchase lines (kind 'backoffice'). Back Office
  records many Fintech invoices a second time when it pays them (a
  check-mode entry weeks later, the same amount): an entry matching a
  Fintech invoice by number, or by vendor and amount within MATCH_DAYS, is
  that invoice and is never counted twice.
"""
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from fintech_invoices import distributor_key, invoice_key

log = logging.getLogger(__name__)

CATEGORIES = ("food", "na_beverage", "beer", "wine", "liquor", "bar_consumables", "discount", "other_purchase",
              "inventory_adjustment", "not_cogs", "unknown")
POUR = ("beer", "wine", "liquor")
# A check-mode entry Back Office records when it pays an invoice sits up to
# this many days after the Fintech invoice it pays (EJ's: a catch-up batch on
# 8/5/26 paid invoices from late May and June).
MATCH_DAYS_AFTER = 90
MATCH_DAYS_BEFORE = 10
WINDOW_DAYS = 28
MONTH_MIN_NIGHTS = 14


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_backoffice_ap(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            filename TEXT,
            kind TEXT NOT NULL,
            intake_id INTEGER,
            status TEXT,
            detail TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(restaurant_id, sha256))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_gl_accounts (
            restaurant_id INTEGER NOT NULL,
            gl TEXT NOT NULL,
            name TEXT,
            subtype TEXT,
            category TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, gl))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            vendor TEXT NOT NULL,
            vendor_key TEXT NOT NULL,
            entry_key TEXT NOT NULL,
            invoice_number TEXT,
            entry_kind TEXT NOT NULL,
            itemized INTEGER NOT NULL DEFAULT 0,
            invoice_date TEXT NOT NULL,
            amount REAL NOT NULL,
            gl TEXT,
            file_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(restaurant_id, vendor_key, entry_key))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bo_invoices_date ON bo_invoices(restaurant_id, invoice_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_vendor_categories (
            restaurant_id INTEGER NOT NULL,
            vendor_key TEXT NOT NULL,
            category TEXT NOT NULL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, vendor_key))""")
        conn.commit()
    finally:
        conn.close()


# ── the Chart of Accounts ─────────────────────────────────────────────────

_GL_WORDS = (
    ("inventory_adjustment", None),       # by subtype, below
    ("discount", r"DISCOUNT"),
    ("bar_consumables", r"BAR CONSUMABLE|BAR PREP|MIXER"),
    ("na_beverage", r"\bNA BEV|NON[- ]?ALC|COFFEE|SODA"),
    ("beer", r"BEER|BREWERY|DRAFT BEER|KEG"),
    ("wine", r"WINE"),
    ("liquor", r"LIQUOR|SPIRIT"),
    ("food", r"PRODUCE|DAIRY|MEAT|SEAFOOD|DRY GOOD|POULTRY|PORK|BREAD|FOOD|BAKERY|PROTEIN|GROCER"),
)


def gl_category(subtype, name) -> str:
    """The Cavnar AI category one Chart of Accounts row books. A purchase
    account by its name; a 'Purchase Inventory' account is a count's
    adjustment, never a purchase; anything that is not a purchase is not
    cost of goods. A purchase account whose name says none of the above
    (paper, to-go, retail) is other_purchase."""
    st = str(subtype or "").strip().lower()
    nm = str(name or "").upper()
    if st == "purchase inventory":
        return "inventory_adjustment"
    if st != "purchase":
        return "not_cogs"
    for cat, rx in _GL_WORDS:
        if rx and re.search(rx, nm):
            return cat
    return "other_purchase"


def parse_coa(content) -> list:
    """[{gl, name, subtype, category}] from Back Office's Chart of Accounts
    CSV (GLAccountSubTypeName, MainAccountNumber, GLAccountDescription, …).
    Raises ValueError when the header is not that file's."""
    import csv
    import io
    text = content.decode("utf-8-sig", "replace") if isinstance(content, (bytes, bytearray)) else str(content)
    rd = csv.DictReader(io.StringIO(text))
    need = {"GLAccountSubTypeName", "MainAccountNumber", "GLAccountDescription"}
    if not rd.fieldnames or not need <= set(rd.fieldnames):
        raise ValueError("not a Back Office Chart of Accounts export")
    out = []
    for r in rd:
        gl = str(r.get("MainAccountNumber") or "").strip()
        if not gl:
            continue
        desc = str(r.get("GLAccountDescription") or "").strip()
        name = desc[len(gl):].strip() if desc.startswith(gl) else desc
        sub = str(r.get("GLAccountSubTypeName") or "").strip()
        out.append({"gl": gl, "name": name, "subtype": sub, "category": gl_category(sub, name)})
    return out


def save_coa(restaurant_id, accounts, db_path=None) -> int:
    conn = get_conn(db_path)
    try:
        for a in accounts:
            conn.execute("INSERT INTO bo_gl_accounts (restaurant_id, gl, name, subtype, category, updated_at) "
                         "VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, gl) DO UPDATE SET name=excluded.name, "
                         "subtype=excluded.subtype, category=excluded.category, updated_at=excluded.updated_at",
                         (int(restaurant_id), a["gl"], a["name"], a["subtype"], a["category"], _now()))
        conn.commit()
    finally:
        conn.close()
    return len(accounts)


def gl_map(restaurant_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        return {r["gl"]: dict(r) for r in conn.execute(
            "SELECT gl, name, subtype, category FROM bo_gl_accounts WHERE restaurant_id=?", (int(restaurant_id),))}
    finally:
        conn.close()


# ── Invoices By Vendor (printed to PDF) ───────────────────────────────────

_BULLET = ""          # the vendor row's bullet in the printout's text
_CHECK = ""           # the Itemized column's checkmark
_HEAD_RE = re.compile(r"^%s?(.+?)\s+(-?)\$([\d,]+\.\d\d)\s*$" % _BULLET)
_ROW_RE = re.compile(r"^\s*(CM Entry|.+?)\s+(\[I\]\s+)?(\d\d/\d\d/\d{4})\s+(\(?-?\$[\d,]+\.\d\d\)?)(.*)$")


def looks_like_invoices_by_vendor(text) -> bool:
    return "Invoices By Vendor" in (text or "") and "Invoice #" in (text or "")


def parse_invoices_by_vendor(text) -> dict:
    """{"vendors": [{vendor, total, rows: [{entry_kind, invoice_number,
    itemized, date (ISO), amount, gl}]}], "unparsed": n, "mismatched": [vendor]}
    from the printout's text. A vendor whose rows do not add up to the total
    the page printed beside it is named in `mismatched` (a row the reader
    missed) — its rows are still returned."""
    t = str(text or "").replace("\xa0", " ").replace(_CHECK, " [I] ")
    vendors, cur, unparsed, pending = [], None, 0, False
    for line in t.split("\n"):
        if not line.strip() or "Invoices By Vendor" in line or line.startswith("Page ") or "Invoice # Itemized" in line:
            continue
        if line.strip() == _BULLET:
            pending = True
            continue
        if line.startswith(_BULLET) or pending:
            m = _HEAD_RE.match(line)
            if m:
                pending = False
                cur = {"vendor": " ".join(m.group(1).split()),
                       "total": round(float(m.group(3).replace(",", "")) * (-1 if m.group(2) else 1), 2), "rows": []}
                vendors.append(cur)
                continue
        m = _ROW_RE.match(line)
        if m and cur is not None:
            a = m.group(4)
            amt = float(re.sub(r"[^\d.]", "", a)) * (-1 if ("(" in a or "-" in a) else 1)
            gl = re.search(r"GL:\s*(\d+)", m.group(5))
            num = m.group(1).strip()
            mo, dy, yr = m.group(3).split("/")
            cur["rows"].append({"entry_kind": "cm" if num == "CM Entry" else "invoice",
                                "invoice_number": None if num == "CM Entry" else num,
                                "itemized": bool(m.group(2)), "date": f"{yr}-{mo}-{dy}", "amount": round(amt, 2),
                                "gl": gl.group(1) if gl else None})
        else:
            unparsed += 1
    mismatched = [v["vendor"] for v in vendors if abs(sum(r["amount"] for r in v["rows"]) - v["total"]) >= 0.02]
    return {"vendors": vendors, "unparsed": unparsed, "mismatched": mismatched}


def _entry_keys(rows) -> list:
    """One key per row: an invoice by its number; a check-mode entry by its
    date and amount, numbered when the same vendor has two alike that day."""
    seen, out = Counter(), []
    for r in rows:
        if r["entry_kind"] == "invoice":
            out.append("inv|" + invoice_key(r["invoice_number"]))
        else:
            base = f"cm|{r['date']}|{r['amount']:.2f}"
            seen[base] += 1
            out.append(f"{base}|{seen[base]}")
    return out


def save_invoices(restaurant_id, parsed, file_id=None, db_path=None) -> dict:
    """Upsert every row; returns {written, vendors}. An entry seen again (a
    later printout overlapping this one) is updated in place, never doubled."""
    conn = get_conn(db_path)
    n = 0
    try:
        for v in parsed["vendors"]:
            vk = distributor_key(v["vendor"]) or v["vendor"].lower()
            for r, key in zip(v["rows"], _entry_keys(v["rows"])):
                conn.execute(
                    "INSERT INTO bo_invoices (restaurant_id, vendor, vendor_key, entry_key, invoice_number, entry_kind, "
                    "itemized, invoice_date, amount, gl, file_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(restaurant_id, vendor_key, entry_key) DO UPDATE SET vendor=excluded.vendor, "
                    "itemized=excluded.itemized, invoice_date=excluded.invoice_date, amount=excluded.amount, "
                    "gl=excluded.gl, file_id=excluded.file_id, updated_at=excluded.updated_at",
                    (int(restaurant_id), v["vendor"], vk, key, r["invoice_number"], r["entry_kind"],
                     1 if r["itemized"] else 0, r["date"], r["amount"], r["gl"], file_id, _now(), _now()))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": n, "vendors": len(parsed["vendors"])}


# ── the intake reader ─────────────────────────────────────────────────────

def _text_of(filename, content):
    if bytes(content[:5]) == b"%PDF-":
        from email_intake import pdf_text
        return pdf_text(content)
    return content.decode("utf-8-sig", "replace") if isinstance(content, (bytes, bytearray)) else str(content)


def read_file(restaurant_id, filename, content, intake_id=None, db_path=None) -> dict:
    """email_intake's reader for kind 'backoffice_ap' (READERS), and the
    import script's. A Chart of Accounts CSV or an Invoices By Vendor
    printout (PDF, or its text); anything else is `unrecognized`. Returns
    {"status": stored | duplicate | unrecognized, "kind", "detail", ...}."""
    content = bytes(content or b"")
    sha = hashlib.sha256(content).hexdigest()
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM bo_files WHERE restaurant_id=? AND sha256=?",
                        (int(restaurant_id), sha)).fetchone():
            return {"status": "duplicate", "detail": "this file was already read"}
    finally:
        conn.close()
    kind, detail, out = None, None, {}
    try:
        head = content[:400].decode("utf-8-sig", "replace")
        if "GLAccountSubTypeName" in head and "MainAccountNumber" in head:
            accounts = parse_coa(content)
            kind, n = "coa", save_coa(restaurant_id, accounts, db_path)
            cats = Counter(a["category"] for a in accounts)
            detail = f"{n} accounts ({', '.join(f'{c} {k}' for c, k in sorted(cats.items()))})"
            out = {"accounts": n}
        else:
            text = _text_of(filename, content)
            if not looks_like_invoices_by_vendor(text):
                return {"status": "unrecognized",
                        "detail": "neither a Chart of Accounts export nor an Invoices By Vendor printout"}
            parsed = parse_invoices_by_vendor(text)
            if not parsed["vendors"]:
                return {"status": "unrecognized", "detail": "no vendors read from the printout"}
            kind = "invoices_by_vendor"
            out = {"vendors": len(parsed["vendors"]), "unparsed": parsed["unparsed"],
                   "mismatched": parsed["mismatched"]}
            detail = (f"{len(parsed['vendors'])} vendors, {sum(len(v['rows']) for v in parsed['vendors'])} entries"
                      + (f"; rows missed for {', '.join(parsed['mismatched'])}" if parsed["mismatched"] else ""))
    except ValueError as e:
        return {"status": "unrecognized", "detail": str(e)}
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO bo_files (restaurant_id, sha256, filename, kind, intake_id, status, detail, "
                           "created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (int(restaurant_id), sha, str(filename or "")[:200], kind, intake_id, "stored", detail,
                            _now()))
        file_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    if kind == "invoices_by_vendor":
        out.update(save_invoices(restaurant_id, parsed, file_id=file_id, db_path=db_path))
        _invalidate(restaurant_id)
    return dict(out, status="stored", kind=kind, file_id=file_id, detail=detail)


def _invalidate(restaurant_id):
    try:
        from bar import cache
        cache.invalidate(restaurant_id)
    except Exception:
        pass


# ── categories ────────────────────────────────────────────────────────────

_FOOD_NAME = re.compile(r"FOOD|BAKERY|PRODUCE|MEATS?\b|SEAFOOD|POULTRY|DAIRY|FARMS?\b|& SONS", re.I)


def _fintech_vendor_mix(restaurant_id, db_path=None) -> dict:
    """{distributor_key: (dominant pour category, {category: net})} from the
    restaurant's Fintech lines (the Bar's own sorting)."""
    try:
        from bar.purchases import _lines
        mix = defaultdict(Counter)
        for l in _lines(restaurant_id, db_path, backoffice=False):
            if l.get("category") in POUR:
                mix[l.get("distributor_key") or distributor_key(l.get("distributor"))][l["category"]] += float(l["net"])
        return {k: (c.most_common(1)[0][0], dict(c)) for k, c in mix.items() if c}
    except Exception as e:
        log.warning("backoffice_ap: fintech mix unread rid=%s: %s", restaurant_id, e)
        return {}


def _match_fintech_vendor(vk, fin_keys):
    """The Fintech distributor key a Back Office vendor key names: equal, or
    one's words the start of the other's ('winebow' / 'winebow ct il nj ny';
    'chicago beverage' / 'chicago beverage system')."""
    if vk in fin_keys:
        return vk
    for k in fin_keys:
        a, b = vk.split(), k.split()
        n = min(len(a), len(b), 2)
        if n and a[:n] == b[:n]:
            return k
    # A distinctive first word one Fintech distributor alone has
    # ('schamberger brothers' / 'schamberger bros', EJ's 10/15/26).
    first = (vk.split() or [""])[0]
    same = [k for k in fin_keys if (k.split() or [""])[0] == first]
    if len(first) >= 4 and len(same) == 1:
        return same[0]
    return None


def vendor_categories(restaurant_id, rows=None, db_path=None) -> dict:
    """{vendor_key: {category, source owner|fintech|gl|name|unknown, why}} for
    every Back Office vendor of the restaurant."""
    conn = get_conn(db_path)
    try:
        own = {r["vendor_key"]: r["category"] for r in conn.execute(
            "SELECT vendor_key, category FROM bo_vendor_categories WHERE restaurant_id=?", (int(restaurant_id),))}
        if rows is None:
            rows = [dict(r) for r in conn.execute(
                "SELECT vendor, vendor_key, gl FROM bo_invoices WHERE restaurant_id=?", (int(restaurant_id),))]
    finally:
        conn.close()
    gls = gl_map(restaurant_id, db_path)
    fin = _fintech_vendor_mix(restaurant_id, db_path)
    by_v = defaultdict(list)
    names = {}
    for r in rows:
        by_v[r["vendor_key"]].append(r)
        names[r["vendor_key"]] = r["vendor"]
    out = {}
    for vk, rs in by_v.items():
        if vk in own:
            out[vk] = {"category": own[vk], "source": "owner", "why": "set by the owner"}
            continue
        fk = _match_fintech_vendor(vk, list(fin))
        if fk:
            out[vk] = {"category": fin[fk][0], "source": "fintech", "fintech_key": fk,
                       "why": f"sells mostly {fin[fk][0]} on Fintech"}
            continue
        cats = Counter(gls.get(r["gl"], {}).get("category") for r in rs if r.get("gl"))
        cats.pop(None, None)
        if cats:
            c, n = cats.most_common(1)[0]
            # Every account the vendor's entries name agrees: that category; a
            # vendor whose every entry books outside cost of goods (linen,
            # rent) is that too, never "unknown".
            if n == sum(cats.values()) and (c not in ("not_cogs", "inventory_adjustment") or n == len(rs)):
                out[vk] = {"category": c, "source": "gl", "why": "every entry books to " + c}
                continue
        if _FOOD_NAME.search(names[vk] or ""):
            out[vk] = {"category": "food", "source": "name", "why": "its name says food"}
            continue
        out[vk] = {"category": "unknown", "source": "unknown", "why": "no account on its invoices"}
    return out


def set_vendor_category(restaurant_id, vendor, category, user_id=None, db_path=None) -> dict:
    if category not in CATEGORIES or category == "unknown":
        raise ValueError("Pick food, a beverage category, other purchase or not cost of goods.")
    vk = distributor_key(vendor) or str(vendor).lower()
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO bo_vendor_categories (restaurant_id, vendor_key, category, set_by, set_at) "
                     "VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, vendor_key) DO UPDATE SET "
                     "category=excluded.category, set_by=excluded.set_by, set_at=excluded.set_at",
                     (int(restaurant_id), vk, category, user_id, _now()))
        conn.commit()
    finally:
        conn.close()
    _invalidate(restaurant_id)
    return {"ok": True, "vendor_key": vk, "category": category}


def entries(restaurant_id, start=None, end=None, db_path=None) -> list:
    """Every Back Office entry with its category and why: [{vendor,
    vendor_key, invoice_number, entry_kind, itemized, date, amount, gl,
    gl_name, category, category_source}]."""
    conn = get_conn(db_path)
    try:
        q = ("SELECT vendor, vendor_key, invoice_number, entry_kind, itemized, invoice_date AS date, amount, gl "
             "FROM bo_invoices WHERE restaurant_id=?")
        args = [int(restaurant_id)]
        if start:
            q += " AND invoice_date >= ?"
            args.append(str(start))
        if end:
            q += " AND invoice_date <= ?"
            args.append(str(end))
        rows = [dict(r) for r in conn.execute(q + " ORDER BY invoice_date, vendor", args)]
    finally:
        conn.close()
    gls = gl_map(restaurant_id, db_path)
    vc = vendor_categories(restaurant_id, rows=rows, db_path=db_path)
    for r in rows:
        g = gls.get(r["gl"]) if r.get("gl") else None
        if g:
            r["category"], r["category_source"], r["gl_name"] = g["category"], "gl", g["name"]
        else:
            v = vc.get(r["vendor_key"]) or {"category": "unknown", "source": "unknown"}
            r["category"], r["category_source"], r["gl_name"] = v["category"], v["source"], None
    return rows


# ── the Bar: alcohol bought outside Fintech ───────────────────────────────

def _fintech_invoices(restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, distributor, distributor_key, invoice_number, invoice_date, total FROM fintech_invoices "
            "WHERE restaurant_id=? AND invoice_date IS NOT NULL", (int(restaurant_id),))]
        for r in rows:
            r["distributor_key"] = r.get("distributor_key") or distributor_key(r.get("distributor"))
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def outside_fintech(restaurant_id, db_path=None) -> dict:
    """{"lines": [bar purchase lines], "matched": n, "entries": [...]} — the
    Back Office entries booked to beer, wine, liquor or bar consumables that
    no Fintech invoice carries. An entry is a Fintech invoice (and left out)
    when its number is one, or when a Fintech invoice of the same distributor
    has its amount, dated from MATCH_DAYS_BEFORE before to MATCH_DAYS_AFTER
    after it was paid (each Fintech invoice matched once)."""
    rows = [r for r in entries(restaurant_id, db_path=db_path)
            if r["category"] in POUR + ("bar_consumables",)]
    if not rows:
        return {"lines": [], "matched": 0, "entries": []}
    fins = _fintech_invoices(restaurant_id, db_path)
    by_num = {invoice_key(f["invoice_number"]): f for f in fins if f.get("invoice_number")}
    fin_keys = sorted({f["distributor_key"] for f in fins if f.get("distributor_key")})
    used, matched, out = set(), 0, []
    for r in sorted(rows, key=lambda x: (x["date"], x["vendor"])):
        hit = None
        if r["entry_kind"] == "invoice":
            f = by_num.get(invoice_key(r["invoice_number"]))
            if f and f["id"] not in used:
                hit = f
        if hit is None:
            fk = _match_fintech_vendor(r["vendor_key"], fin_keys)
            if fk:
                d = date.fromisoformat(r["date"])
                cands = [f for f in fins if f["distributor_key"] == fk and f["id"] not in used
                         and abs(float(f["total"] or 0) - float(r["amount"])) < 0.01
                         and -MATCH_DAYS_AFTER <= (date.fromisoformat(f["invoice_date"]) - d).days <= MATCH_DAYS_BEFORE]
                if cands:
                    hit = min(cands, key=lambda f: abs((date.fromisoformat(f["invoice_date"]) - d).days))
                else:
                    # One payment for an invoice and its credit memo (Winebow
                    # $1,259.34 = $1,646.34 - $387.00, EJ's 10/15/26): two
                    # Fintech invoices of the distributor that add up to it.
                    near = [f for f in fins if f["distributor_key"] == fk and f["id"] not in used
                            and -MATCH_DAYS_AFTER <= (date.fromisoformat(f["invoice_date"]) - d).days <= MATCH_DAYS_BEFORE]
                    pair = next(((x, y) for i, x in enumerate(near) for y in near[i + 1:]
                                 if abs(float(x["total"] or 0) + float(y["total"] or 0) - float(r["amount"])) < 0.01),
                                None)
                    if pair:
                        used.add(pair[1]["id"])
                        hit = pair[0]
        if hit is not None:
            used.add(hit["id"])
            matched += 1
            continue
        out.append(r)
    lines = []
    for r in out:
        cat = r["category"]
        mixer = cat == "bar_consumables"
        lines.append({"invoice_id": None, "distributor": r["vendor"], "distributor_key": r["vendor_key"],
                      "invoice_number": r["invoice_number"] or "Back Office entry", "invoice_date": r["date"],
                      "ref_invoice_number": None, "po_number": None, "balanced": True, "item_code": None,
                      "description": f"{r['vendor']} (Back Office" + (f", {r['gl_name']}" if r.get("gl_name") else "")
                                     + ")",
                      "upc": None, "uom": None, "uom_inferred": False, "qty": None, "unit_price": None,
                      "cost": float(r["amount"]), "deposit": None, "adjustment": None, "discount": None,
                      "kind": "backoffice", "received_at": None,
                      "product_key": "backoffice|" + r["vendor_key"],
                      "category": "na" if mixer else cat, "sure": True, "owner_set": False,
                      "net": float(r["amount"]), "rebate": False, "mixer": mixer, "source": "backoffice"})
    return {"lines": lines, "matched": matched, "entries": out}


def outside_fintech_summary(restaurant_id, start=None, end=None, db_path=None) -> dict:
    """The Bar payload's `outside_fintech`: what the Bar counts from Back
    Office in [start, end] — {available, total, by_category, vendors:
    [{vendor, amount, entries}], matched_fintech}."""
    res = outside_fintech(restaurant_id, db_path)
    es = [e for e in res["entries"] if (not start or e["date"] >= start) and (not end or e["date"] <= end)]
    by_v, by_c = defaultdict(lambda: [0.0, 0]), Counter()
    for e in es:
        by_v[e["vendor"]][0] += float(e["amount"])
        by_v[e["vendor"]][1] += 1
        by_c[e["category"]] += float(e["amount"])
    return {"available": bool(res["entries"]) or bool(res["matched"]),
            "total": round(sum(by_c.values()), 2),
            "by_category": {k: round(v, 2) for k, v in by_c.items()},
            "vendors": [{"vendor": v, "amount": round(a, 2), "entries": n}
                        for v, (a, n) in sorted(by_v.items(), key=lambda kv: -kv[1][0])],
            "matched_fintech": res["matched"]}


# ── Food Cost: food bought vs sold ────────────────────────────────────────

def _food_sales(restaurant_id, start, end, db_path=None) -> dict:
    """{date: food sales} from the nightly report's categories whose name
    says food (the POS's own category), never a guess past it."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
                            "AND metric >= 'sales.cat:' AND metric < 'sales.cat;' AND business_date BETWEEN ? AND ? "
                            "AND value IS NOT NULL", (int(restaurant_id), str(start), str(end))).fetchall()
    finally:
        conn.close()
    out = defaultdict(float)
    for r in rows:
        name = r["metric"][len("sales.cat:"):].strip().lower()
        if name == "food" or name.startswith("food"):
            out[r["business_date"]] += float(r["value"] or 0)
    return dict(out)


def food_bought_vs_sold(restaurant_id, today=None, db_path=None) -> dict:
    """Food Cost's purchase-based reading from Back Office: {available,
    basis, window {from, to, days, purchases, sales, pct, nights}, months:
    [{month, label, purchases, sales, pct, nights, partial}], by_category,
    vendors, unknown_vendors, note}. Purchase-based (Back Office shows no
    submitted count): what was bought as a % of what the POS sold as food —
    never called food cost."""
    from time_utils import mdy
    today = today or date.today()
    es = [e for e in entries(restaurant_id, db_path=db_path) if e["category"] == "food"]
    unknown = sorted({e["vendor"] for e in entries(restaurant_id, db_path=db_path) if e["category"] == "unknown"})
    if not es:
        return {"available": False, "why_not": "No Back Office food invoices on file yet.", "unknown_vendors": unknown}
    last = max(e["date"] for e in es)
    first = min(e["date"] for e in es)
    sales = _food_sales(restaurant_id, first, today.isoformat(), db_path)
    if not sales:
        return {"available": False, "why_not": "No food sales from the nightly report yet.",
                "unknown_vendors": unknown}
    end = min(last, max(sales))
    start = (date.fromisoformat(end) - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    def _window(a, b):
        p = sum(float(e["amount"]) for e in es if a <= e["date"] <= b)
        s = sum(v for d, v in sales.items() if a <= d <= b)
        return round(p, 2), round(s, 2), (round(100.0 * p / s, 1) if s > 0 else None), \
            sum(1 for d in sales if a <= d <= b and sales[d] > 0)
    p, s, pct, n = _window(start, end)
    # A month compares only the days both sides cover (EJ's August read 243%:
    # a month of invoices against six nights of sales), and a month with
    # fewer than MONTH_MIN_NIGHTS nights of sales is not shown.
    months = []
    sales_from = min(d for d, v in sales.items() if v > 0)
    m0 = date.fromisoformat(max(first, sales_from)).replace(day=1)
    while m0 <= date.fromisoformat(end):
        m1 = (m0.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        a = max(m0.isoformat(), sales_from)
        b = min(m1, date.fromisoformat(end)).isoformat()
        mp, ms, mpct, mn = _window(a, b)
        if ms > 0 and mn >= MONTH_MIN_NIGHTS:
            months.append({"month": m0.strftime("%Y-%m"), "label": m0.strftime("%B %Y"), "purchases": mp,
                           "sales": ms, "pct": mpct, "nights": mn, "partial": a > m0.isoformat() or b < m1.isoformat(),
                           "from_mdy": mdy(a), "to_mdy": mdy(b)})
        m0 = m1 + timedelta(days=1)
    by_c, by_v = Counter(), Counter()
    for e in es:
        if start <= e["date"] <= end:
            by_c[e.get("gl_name") or "Food (by vendor)"] += float(e["amount"])
            by_v[e["vendor"]] += float(e["amount"])
    return {"available": True, "basis": "purchases", "label": "Food bought vs sold",
            "window": {"from": start, "to": end, "from_mdy": mdy(start), "to_mdy": mdy(end), "days": WINDOW_DAYS,
                       "purchases": p, "sales": s, "pct": pct, "nights": n},
            "months": months,
            "by_category": [{"name": k, "amount": round(v, 2)} for k, v in by_c.most_common()],
            "vendors": [{"vendor": k, "amount": round(v, 2)} for k, v in by_v.most_common()],
            "unknown_vendors": unknown,
            "note": ("Purchase-based: what Back Office shows you paid food suppliers, as a % of the POS's food "
                     "sales. A delivery or a stock-up moves it; a food cost % needs an opening and a closing "
                     "count. A supplier's invoice can carry paper or cleaning lines too.")}
