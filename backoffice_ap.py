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
        # An invoice's own lines (Back Office's invoice page: each line its
        # item, GL account, purchase unit, quantity and price), when on file.
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_invoice_lines (
            restaurant_id INTEGER NOT NULL,
            vendor_key TEXT NOT NULL,
            invoice_key TEXT NOT NULL,
            line_no INTEGER NOT NULL,
            item_code TEXT,
            description TEXT,
            gl TEXT,
            purchase_unit TEXT,
            qty REAL,
            unit_amount REAL,
            ext_amount REAL NOT NULL,
            invoice_date TEXT,
            file_id INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, vendor_key, invoice_key, line_no))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bo_invoice_lines_item ON bo_invoice_lines(restaurant_id, item_code)")
        # The restaurant's Back Office recipe book, whole (backoffice_recipes.load).
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_recipe_books (
            restaurant_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL,
            source TEXT,
            loaded_at TEXT NOT NULL)""")
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


# ── one invoice's page (Edit Invoice, printed to PDF) ─────────────────────

# Each line item prints as label-under-value pairs: "(197761) BEEF BRISKET" /
# "Unit Inventory Item*" / "Use Tax Applies 4030 MEAT" / "GL Account" / "Case"
# / "Purchase Unit*" / qty / "Quantity*" / price / "Amount*" / ext / "Ext Amount".
# The browser's page header and footer can fall between any two of them.
_PAGE_NOISE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d\d, .*Back Office|Page \d+ of \d+)")
_ROW_NO_RE = re.compile(r"^\d+\s*\uf057?$")
_NUM = r"\(?-?\$?[\d,]*\.?\d+\)?"
_LINE_RE = re.compile(
    r"^\(([^)\n]+)\)\s*([^\n]*)\nUnit Inventory Item\*\n(?:Use Tax Applies)?[ \t]*(?:(\d+)[ \t]*([^\n]*))?\n"
    r"GL Account\n([^\n]*)\nPurchase Unit\*\n(%s)\nQuantity\*\n(%s)\nAmount\*\n(%s)\nExt Amount" % (_NUM, _NUM, _NUM),
    re.M)


# Words the PDF's text layer splits at a kerning pair ("POUL TRY").
_PDF_SPLIT_RE = re.compile(r"\bPOUL TRY\b")


def _money(s):
    s = str(s or "").strip()
    v = float(re.sub(r"[^\d.]", "", s) or 0)
    return -v if ("(" in s or "-" in s) else v


def looks_like_invoice_page(text) -> bool:
    return "Unit Inventory Item*" in (text or "") and "Invoice Number*" in (text or "")


def parse_invoice_page(text) -> dict:
    """{vendor, invoice_number, date (ISO), total, lines: [{item_code,
    description, gl, purchase_unit, qty, unit_amount, ext_amount}], "adds_up"}
    from one invoice's Edit Invoice page printed to PDF. `adds_up` is False
    when the lines read do not sum to the page's Total Price (a line missed)."""
    t = str(text or "").replace("\xa0", " ").replace("\u202f", " ")
    kept, top = [], False
    for l in t.split("\n"):
        l = l.rstrip()
        if not l.strip():
            continue
        if _PAGE_NOISE_RE.match(l.strip()):
            top = True
            continue
        if top and _ROW_NO_RE.match(l.strip()):
            continue            # the row numbers the page's top margin repeats
        top = False
        kept.append(l)

    def field(label):
        for i, l in enumerate(kept):
            if l.strip() == label and i:
                return kept[i - 1].strip()
        return None
    vendor, number = field("Vendor*"), field("Invoice Number*")
    d, total = field("Invoice Date*"), field("Total Price*")
    if not (vendor and number and d and re.match(r"\d{1,2}/\d{1,2}/\d{4}$", d)):
        raise ValueError("no vendor, invoice number and date read from the invoice page")
    mo, dy, yr = d.split("/")
    lines = []
    for m in _LINE_RE.finditer("\n".join(kept)):
        desc = _PDF_SPLIT_RE.sub(lambda x: x.group(0).replace(" ", ""), " ".join(m.group(2).split()))
        lines.append({"item_code": m.group(1).strip(), "description": desc or None,
                      "gl": m.group(3), "purchase_unit": m.group(5).strip() or None,
                      "qty": _money(m.group(6)), "unit_amount": _money(m.group(7)),
                      "ext_amount": round(_money(m.group(8)), 2)})
    total = round(_money(total), 2) if total else None
    return {"vendor": " ".join(vendor.split()), "invoice_number": number, "date": f"{yr}-{int(mo):02d}-{int(dy):02d}",
            "total": total, "lines": lines,
            "adds_up": total is not None and abs(sum(l["ext_amount"] for l in lines) - total) < 0.02}


def save_invoice_page(restaurant_id, inv, file_id=None, db_path=None) -> dict:
    """Store a parsed invoice page: its lines (save_lines), and the invoice
    itself in bo_invoices under the same entry key an Invoices By Vendor
    printout gives it — so a page read before the printout counts once, and
    the printout's later row updates it in place."""
    save_invoices(restaurant_id, {"vendors": [{"vendor": inv["vendor"], "total": inv["total"], "rows": [
        {"entry_kind": "invoice", "invoice_number": inv["invoice_number"], "itemized": True, "date": inv["date"],
         "amount": inv["total"] if inv["total"] is not None else round(sum(l["ext_amount"] for l in inv["lines"]), 2),
         "gl": None}]}]}, file_id=file_id, db_path=db_path)
    n = save_lines(restaurant_id, inv["vendor"], inv["invoice_number"], inv["lines"], invoice_date=inv["date"],
                   file_id=file_id, db_path=db_path)
    return {"written": n}


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
        if kind == "coa":
            pass
        elif looks_like_invoice_page(text):
            inv = parse_invoice_page(text)
            if not inv["lines"]:
                return {"status": "unrecognized", "detail": "no line items read from the invoice page"}
            kind = "invoice_page"
            out = {"vendor": inv["vendor"], "invoice_number": inv["invoice_number"], "lines": len(inv["lines"]),
                   "adds_up": inv["adds_up"]}
            from time_utils import mdy
            detail = (f"{inv['vendor']} invoice {inv['invoice_number']} ({mdy(inv['date'])}): {len(inv['lines'])} lines"
                      + ("" if inv["adds_up"] else f"; lines don't add up to the ${inv['total']:,.2f} total"))
        else:
            if not looks_like_invoices_by_vendor(text):
                return {"status": "unrecognized",
                        "detail": "not a Chart of Accounts export, an Invoices By Vendor printout or an invoice page"}
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
    elif kind == "invoice_page":
        save_invoice_page(restaurant_id, inv, file_id=file_id, db_path=db_path)
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


def save_lines(restaurant_id, vendor, invoice_number, lines, invoice_date=None, file_id=None, db_path=None) -> int:
    """One invoice's lines, replacing any on file for it: [{item_code,
    description, gl, purchase_unit, qty, unit_amount, ext_amount}]."""
    vk = distributor_key(vendor) or str(vendor).lower()
    ik = invoice_key(invoice_number)
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM bo_invoice_lines WHERE restaurant_id=? AND vendor_key=? AND invoice_key=?",
                     (int(restaurant_id), vk, ik))
        for i, l in enumerate(lines, 1):
            conn.execute("INSERT INTO bo_invoice_lines (restaurant_id, vendor_key, invoice_key, line_no, item_code, "
                         "description, gl, purchase_unit, qty, unit_amount, ext_amount, invoice_date, file_id, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (int(restaurant_id), vk, ik, i, l.get("item_code"), l.get("description"),
                          str(l["gl"]) if l.get("gl") else None, l.get("purchase_unit"), l.get("qty"),
                          l.get("unit_amount"), float(l["ext_amount"]), invoice_date, file_id, _now()))
        conn.commit()
    finally:
        conn.close()
    _invalidate(restaurant_id)
    return len(lines)


def _line_groups(restaurant_id, db_path=None) -> dict:
    """{(vendor_key, invoice_key): [(gl, amount)]} — each invoice's lines on
    file summed by GL account."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT vendor_key, invoice_key, gl, SUM(ext_amount) AS amt FROM bo_invoice_lines "
                            "WHERE restaurant_id=? GROUP BY vendor_key, invoice_key, gl", (int(restaurant_id),)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = defaultdict(list)
    for r in rows:
        out[(r["vendor_key"], r["invoice_key"])].append((r["gl"], float(r["amt"] or 0)))
    return out


def entries(restaurant_id, start=None, end=None, db_path=None) -> list:
    """Every Back Office entry with its category and why: [{vendor,
    vendor_key, invoice_number, entry_kind, itemized, date, amount, gl,
    gl_name, category, category_source}]. An invoice whose lines are on file
    (bo_invoice_lines) is one row per GL account its lines book to
    (category_source 'line'), and what its lines leave of its total (tax, a
    fee) one more row on the vendor's category — so a supplier's paper and
    chemical lines leave food, and food splits into meat, dairy, produce."""
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
    groups = _line_groups(restaurant_id, db_path)
    out = []
    for r in rows:
        lg = groups.get((r["vendor_key"], invoice_key(r["invoice_number"]))) if r["entry_kind"] == "invoice" else None
        v = vc.get(r["vendor_key"]) or {"category": "unknown", "source": "unknown"}
        if lg:
            rest = float(r["amount"])
            for gl, amt in lg:
                g = gls.get(gl) if gl else None
                out.append(dict(r, amount=round(amt, 2), gl=gl, gl_name=(g or {}).get("name"),
                                category=(g or {}).get("category") or v["category"], category_source="line"))
                rest -= amt
            if abs(rest) >= 0.01:
                out.append(dict(r, amount=round(rest, 2), gl=None, gl_name=None, category=v["category"],
                                category_source="invoice_rest"))
            continue
        g = gls.get(r["gl"]) if r.get("gl") else None
        if g:
            r["category"], r["category_source"], r["gl_name"] = g["category"], "gl", g["name"]
        else:
            r["category"], r["category_source"], r["gl_name"] = v["category"], v["source"], None
        out.append(r)
    return out


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


def _account_label(name) -> str:
    """'4030 MEAT' → 'Meat'; an amount no account split reads 'Not split yet'."""
    if name == "Food (by vendor)":
        return "Not split yet"
    return re.sub(r"^\d+\s*", "", str(name or "")).strip().capitalize() or str(name)


def feed_state(restaurant_id, db_path=None) -> dict:
    """Back Office as a data source, for the pages that ask "is Food Cost
    set up?" (owner, 10/16/26: EJ's read "on but not configured" and the
    page showed sample figures while its food was read from Back Office).
    Back Office has no API — Cavnar AI reads the reports it exports — so it
    is never "connected", but it is real data: {files, last_import,
    invoices_through, recipes_loaded}; files 0 when nothing came in."""
    out = {"files": 0, "last_import": None, "invoices_through": None, "recipes_loaded": None}
    conn = get_conn(db_path)
    try:
        for sql, keys in (("SELECT COUNT(*), MAX(created_at) FROM bo_files WHERE restaurant_id=?", ("files", "last_import")),
                          ("SELECT MAX(invoice_date) FROM bo_invoices WHERE restaurant_id=?", ("invoices_through",)),
                          ("SELECT loaded_at FROM bo_recipe_books WHERE restaurant_id=?", ("recipes_loaded",))):
            try:
                row = conn.execute(sql, (int(restaurant_id),)).fetchone()
            except Exception:
                continue          # a table not created yet: that part is not on file
            if row:
                for i, k in enumerate(keys):
                    out[k] = row[i]
    finally:
        conn.close()
    out["files"] = int(out["files"] or 0)
    if not out["last_import"]:
        out["last_import"] = out["recipes_loaded"]
    out["on_file"] = bool(out["files"] or out["invoices_through"] or out["recipes_loaded"])
    return out


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
            "by_category": [{"name": k, "label": _account_label(k), "amount": round(v, 2)} for k, v in by_c.most_common()],
            "vendors": [{"vendor": k, "amount": round(v, 2)} for k, v in by_v.most_common()],
            "unknown_vendors": unknown,
            "note": ("Purchase-based: what Back Office shows you paid food suppliers, as a % of the POS's food "
                     "sales. A delivery or a stock-up moves it; a food cost % needs an opening and a closing "
                     "count. A supplier's invoice can carry paper or cleaning lines too.")}
