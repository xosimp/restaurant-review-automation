"""
fintech_invoices.py — the alcohol distributor invoices a restaurant pays
through Fintech, read from the files Fintech emails (email_intake, kind
'fintech') into `fintech_invoices` and `fintech_invoice_lines`.

Two files are read:

- The PRINTOUT ('LegacyRetailerReceivedDetailWithPageBreak', Fintech's
  daily custom file, a text PDF): per invoice a header block (Store Number,
  Distributor, Invoice Number, Invoice Date, Invoice Total, Due Date) and
  lines of item code, a 30-character description, qty, price and cost.
  It carries no unit of measure and no UPC. A keg line's cost is price +
  the $30 deposit (kept as `deposit`); empty kegs come back as a credit
  line at qty -n.
- The EXPORT (the portal's payment-activity CSV, 'bankactivity_detail'):
  one row per invoice line with Vendor Name, Invoice Number / Date / Total,
  Product Number, Product Description, U.P.C, Unit Of Measure (CA = case,
  BO or BL = bottle), Item Quantity, Item Cost (each), Item Adjustment.
  Its bank columns (account, trace, transaction, batch) are never stored.

An invoice is keyed by (restaurant, distributor, invoice number): a file
sent again replaces its lines, and the export — UPC and unit of measure —
is never overwritten by the printout of the same invoice. `balanced` says
whether the lines add up to the invoice total; a file that is neither
format is kept as 'unrecognized' and nothing is written from it.

Nothing here is owner-facing yet: it is the cost source the bar module
(pour cost, price changes per distributor) will read.
"""
import csv
import hashlib
import io
import re
from datetime import datetime, timezone

import models as _models

# Seen in EJ's export (5/4-10/13/26): CA, KE, EA, BO, DS (the keg deposit
# lines and their empty-keg credits).
UOM_WORDS = {"CA": "case", "CS": "case", "BO": "bottle", "BL": "bottle", "BT": "bottle", "EA": "each",
             "KE": "keg", "KG": "keg", "DS": "deposit"}

# Export columns that are about the payment, not the goods: dropped before
# the file's text is kept.
_BANK_COLUMNS = ("bankaccount", "tracenumber", "transactionid", "batchid", "withdrawdate")

_FEE_RE = re.compile(r"\b(fuel|service fee|delivery|freight|surcharge)\b", re.I)
_ADJ_RE = re.compile(r"summary level adjust", re.I)
_KEG_CREDIT_RE = re.compile(r"empty keg", re.I)


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_fintech_invoices(db_path=None):
    """Boot only (init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS fintech_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            intake_id INTEGER,
            filename TEXT,
            format TEXT,
            sha256 TEXT NOT NULL,
            content TEXT,
            invoices INTEGER NOT NULL DEFAULT 0,
            lines INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(restaurant_id, sha256))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS fintech_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            distributor TEXT NOT NULL,
            invoice_number TEXT NOT NULL,
            invoice_date TEXT,
            due_date TEXT,
            total REAL,
            location_id TEXT,
            source_format TEXT NOT NULL,
            file_id INTEGER,
            lines_total REAL,
            balanced INTEGER,
            updated_at TEXT NOT NULL,
            UNIQUE(restaurant_id, distributor, invoice_number))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_invoices_date "
                     "ON fintech_invoices(restaurant_id, invoice_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS fintech_invoice_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL,
            item_code TEXT,
            description TEXT,
            upc TEXT,
            uom TEXT,
            qty REAL,
            unit_price REAL,
            cost REAL,
            deposit REAL,
            adjustment REAL,
            kind TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_lines_invoice ON fintech_invoice_lines(invoice_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_lines_item ON fintech_invoice_lines(item_code)")
        conn.commit()
    finally:
        conn.close()


# ── values ─────────────────────────────────────────────────────────────────

def money(text):
    """'$1,467.22' -> 1467.22, '($240.00)' -> -240.0, '' -> None."""
    s = str(text or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def iso_date(text):
    """'10/09/2026' or '9/15/26' or '2026-09-15' -> '2026-09-15'; else None."""
    s = str(text or "").strip().split(" ")[0]
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def uom_word(code):
    return UOM_WORDS.get(str(code or "").strip().upper())


def _kind(code, desc, qty, price, earlier_prices):
    d = desc or ""
    if _KEG_CREDIT_RE.search(d):
        return "keg_credit"
    if _ADJ_RE.search(d) or str(code or "").strip("0") == "":
        return "adjustment"
    if _FEE_RE.search(d):
        return "fee"
    # A second line for an item already on the invoice at a few dollars
    # (Southern Glazer's $2.29 beside a $249.50 case): a per-unit charge on
    # that item, not more of it. What the charge is (a deposit, a tax) the
    # file does not say.
    if price is not None and price < 5 and any(p >= 5 for p in earlier_prices.get(code, [])):
        return "charge"
    return "item"


# ── the printout ───────────────────────────────────────────────────────────

_HDR_RE = re.compile(r"^\s*(Store Number|Distributor|Invoice Number|Invoice Date|Invoice Total|Due Date)\s*:\s*(.*)$")
_MONEY = r"\(?-?\$[\d,]+\.\d{2}\)?"
_LINE_RE = re.compile(rf"^\s*(\S+)\s+(.+?)\s+(-?\d+(?:\.\d+)?)\s+({_MONEY})\s+({_MONEY})\s*$")


def parse_printout(text):
    """Invoices from the printout's text: [{distributor, invoice_number,
    invoice_date, due_date, total, location_id, lines: [...]}]."""
    invoices, cur, in_lines = [], None, False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _HDR_RE.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == "Store Number":
                cur = {"location_id": val.split()[0] if val else None, "lines": []}
                invoices.append(cur)
                in_lines = False
            elif cur is not None:
                if key == "Distributor":
                    cur["distributor"] = val
                elif key == "Invoice Number":
                    cur["invoice_number"] = val
                elif key == "Invoice Date":
                    cur["invoice_date"] = iso_date(val)
                elif key == "Due Date":
                    cur["due_date"] = iso_date(val)
                elif key == "Invoice Total":
                    cur["total"] = money(val)
            continue
        if cur is None:
            continue
        if line.strip().startswith("----------"):
            in_lines = True
            continue
        if "Invoice Total:" in line:
            in_lines = False
            continue
        if not in_lines:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        code, desc, qty, price, cost = m.groups()
        q, p, c = float(qty), money(price), money(cost)
        deposit = None
        if q > 0 and p is not None and c is not None and abs(c - q * p) > 0.005:
            deposit = round(c - q * p, 2)
        cur["lines"].append({"item_code": code, "description": desc.strip(), "upc": None, "uom": None,
                             "qty": q, "unit_price": p, "cost": c, "deposit": deposit, "adjustment": None})
    return [i for i in invoices if i.get("invoice_number") and i.get("distributor")]


# ── the export ─────────────────────────────────────────────────────────────

def _norm(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


_EXPORT_COLS = {
    "distributor": ("vendorname", "distributor", "distributorname"),
    "invoice_number": ("invoicenumber", "invoiceno", "invoice"),
    "invoice_date": ("invoicedate",),
    "total": ("invoicetotal", "invoiceamount"),
    "location_id": ("locationid", "storenumber"),
    "item_code": ("productnumber", "itemnumber", "item", "itemcode"),
    "description": ("productdescription", "description", "itemdescription"),
    "upc": ("upc",),
    "uom": ("unitofmeasure", "uom", "unit"),
    "qty": ("itemquantity", "quantity", "qty"),
    "unit_price": ("itemcost", "unitcost", "price"),
    "adjustment": ("itemadjustment", "adjustment"),
    "cost": ("extendedcost", "extendedamount", "extcost", "extendedprice", "linetotal", "lineamount"),
}


def _columns(header):
    idx = {}
    normed = [_norm(h) for h in header]
    for field, names in _EXPORT_COLS.items():
        for n in names:
            if n in normed:
                idx[field] = normed.index(n)
                break
    return idx


def is_export(header):
    idx = _columns(header)
    return all(k in idx for k in ("distributor", "invoice_number", "description", "qty", "unit_price"))


def parse_export(text):
    """Invoices from the payment-activity CSV, grouped by distributor and
    invoice number in file order."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not is_export(rows[0]):
        return []
    idx = _columns(rows[0])

    def cell(row, field):
        i = idx.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    invoices, by_key = [], {}
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        dist, num = cell(row, "distributor"), cell(row, "invoice_number")
        if not (dist and num):
            continue
        inv = by_key.get((dist, num))
        if inv is None:
            inv = {"distributor": dist, "invoice_number": num, "invoice_date": iso_date(cell(row, "invoice_date")),
                   "due_date": None, "total": money(cell(row, "total")),
                   "location_id": cell(row, "location_id") or None, "lines": []}
            by_key[(dist, num)] = inv
            invoices.append(inv)
        try:
            q = float(cell(row, "qty") or 0)
        except ValueError:
            q = 0.0
        p = money(cell(row, "unit_price"))
        adj = money(cell(row, "adjustment"))
        c = money(cell(row, "cost"))
        if c is None and p is not None:
            c = round(q * p + (adj or 0), 2)
        inv["lines"].append({"item_code": cell(row, "item_code") or None,
                             "description": cell(row, "description"), "upc": cell(row, "upc") or None,
                             "uom": (cell(row, "uom") or "").upper() or None, "qty": q, "unit_price": p,
                             "cost": c, "deposit": None, "adjustment": adj or None})
    return invoices


def _export_without_bank(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    drop = {i for i, h in enumerate(rows[0]) if _norm(h) in _BANK_COLUMNS}
    out = io.StringIO()
    w = csv.writer(out)
    for row in rows:
        w.writerow([c for i, c in enumerate(row) if i not in drop])
    return out.getvalue()


# ── reading a file ─────────────────────────────────────────────────────────

def _pdf_text(content):
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def read(filename, content):
    """(format, kept_text, invoices) for one file's bytes. format is
    'printout', 'export' or None (unrecognized)."""
    if content[:5] == b"%PDF-":
        text = _pdf_text(content)
    elif content[:2] == b"PK":
        return None, None, []          # a spreadsheet: Fintech's CSV is the one read
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
    if "Invoice Number" in text and "Distributor" in text and re.search(r"Invoice Number\s*:", text):
        return "printout", text, parse_printout(text)
    first = next(csv.reader(io.StringIO(text.split("\n", 1)[0])), [])
    if is_export(first):
        return "export", _export_without_bank(text), parse_export(text)
    return None, None, []


def _store_invoice(conn, restaurant_id, inv, fmt, file_id):
    existing = conn.execute("SELECT id, source_format FROM fintech_invoices WHERE restaurant_id=? AND "
                            "distributor=? AND invoice_number=?",
                            (restaurant_id, inv["distributor"], inv["invoice_number"])).fetchone()
    if existing and existing["source_format"] == "export" and fmt == "printout":
        return False                   # the export's UPC and unit of measure win
    prices = {}
    lines = []
    for n, ln in enumerate(inv["lines"], 1):
        kind = _kind(ln["item_code"], ln["description"], ln["qty"], ln["unit_price"], prices)
        if ln["unit_price"] is not None:
            prices.setdefault(ln["item_code"], []).append(ln["unit_price"])
        lines.append((n, ln, kind))
    lines_total = round(sum((ln["cost"] or 0) for _, ln, _ in lines), 2)
    total = inv.get("total")
    balanced = None if total is None else int(abs(lines_total - total) < 0.02)
    now = _now()
    if existing:
        inv_id = existing["id"]
        conn.execute("UPDATE fintech_invoices SET invoice_date=?, due_date=COALESCE(?, due_date), total=?, "
                     "location_id=?, source_format=?, file_id=?, lines_total=?, balanced=?, updated_at=? "
                     "WHERE id=?", (inv.get("invoice_date"), inv.get("due_date"), total, inv.get("location_id"),
                                    fmt, file_id, lines_total, balanced, now, inv_id))
        conn.execute("DELETE FROM fintech_invoice_lines WHERE invoice_id=?", (inv_id,))
    else:
        cur = conn.execute("INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, "
                           "due_date, total, location_id, source_format, file_id, lines_total, balanced, updated_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (restaurant_id, inv["distributor"], inv["invoice_number"], inv.get("invoice_date"),
                            inv.get("due_date"), total, inv.get("location_id"), fmt, file_id, lines_total,
                            balanced, now))
        inv_id = cur.lastrowid
    for n, ln, kind in lines:
        conn.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, upc, uom, qty, "
                     "unit_price, cost, deposit, adjustment, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (inv_id, n, ln["item_code"], ln["description"], ln["upc"], ln["uom"], ln["qty"],
                      ln["unit_price"], ln["cost"], ln["deposit"], ln["adjustment"], kind))
    return True


def ingest_file(restaurant_id, filename, content, intake_id=None, db_path=None):
    """email_intake's reader for kind 'fintech'. Returns {"status":
    stored | duplicate | unrecognized, "format", "invoices", "lines",
    "unbalanced"}."""
    sha = hashlib.sha256(content).hexdigest()
    conn = get_conn(db_path)
    try:
        seen = conn.execute("SELECT id FROM fintech_files WHERE restaurant_id=? AND sha256=?",
                            (int(restaurant_id), sha)).fetchone()
    finally:
        conn.close()
    if seen:
        return {"status": "duplicate", "file_id": seen["id"]}
    fmt, kept, invoices = read(filename, content)
    status = "stored" if fmt and invoices else "unrecognized"
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO fintech_files (restaurant_id, intake_id, filename, format, sha256, content, "
                           "invoices, lines, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (int(restaurant_id), intake_id, str(filename)[:200], fmt, sha, kept,
                            len(invoices), sum(len(i["lines"]) for i in invoices), status, _now()))
        file_id = cur.lastrowid
        written, unbalanced = 0, []
        for inv in invoices:
            if _store_invoice(conn, int(restaurant_id), inv, fmt, file_id):
                written += 1
                lt = round(sum((ln["cost"] or 0) for ln in inv["lines"]), 2)
                if inv.get("total") is not None and abs(lt - inv["total"]) >= 0.02:
                    unbalanced.append(inv["invoice_number"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"status": status, "format": fmt, "file_id": file_id, "invoices": written,
            "lines": sum(len(i["lines"]) for i in invoices), "unbalanced": unbalanced}
