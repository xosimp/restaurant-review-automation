"""
fintech_invoices.py — the alcohol distributor invoices a restaurant pays
through Fintech, read from the files Fintech emails (email_intake, kind
'fintech') into `fintech_invoices` and `fintech_invoice_lines`.

Three files are read:

- The PRINTOUT ('LegacyRetailerReceivedDetailWithPageBreak', Fintech's
  daily custom file, a text PDF): per invoice a header block (Store Number,
  Distributor, Vendor ID, Invoice Number, Invoice Date, Invoice Total, Due
  Date, PO Number, PO Date, Ref Inv Number) and lines of item code, a
  30-character description, qty, price and cost. It carries no unit of
  measure and no UPC: a line takes both from the same distributor's latest
  export line for that item code at the nearest price, marked
  `uom_inferred` (a case and a bottle of one code never share a unit).
- The EXPORT (the portal's payment-activity CSV, 'bankactivity_detail') and
  the My Invoices "Invoice Details" CSV: one row per invoice line with
  Vendor Name, Invoice Number / Date / Total, Product Number, Product
  Description, U.P.C, Unit Of Measure (CA = case, BO or BL = bottle, KE =
  keg, DS = deposit), Item Quantity, Item Cost (each), Item Adjustments,
  Total Discount, Extended Price, PO Number / Date, Ref Invoice Number.
  Only the columns read here (and a few goods columns kept for a later
  reader: PPC, Pack UPC, GL Code) are kept with the file — an allow-list,
  so a renamed bank column is never stored.

A keg's deposit sits inside its cost on both files (the export's Item
Adjustments, the printout's cost − qty × price). It is split out as
`deposit` only on a keg line and only in whole steps of the distributor's
own per-keg deposit — learned from its DS / EMPTY lines, else $30 (Illinois)
— or, on an export, one uniform whole-dollar step per keg (BAR-CALC-11,
10/16/26); any other difference is an `adjustment` and stays in the cost.
Every EMPTY / EMPTIES line is a deposit credit (`keg_credit`) whatever its
unit.

An invoice is one invoice whatever the file calls it: matched within the
restaurant on the invoice number without its leading zeros and the
distributor's canonical key (Fintech's vendor id when the file has one,
else the name casefolded without punctuation, LLC / Inc, "dba …"), with a
same-date same-total guard for a spelling the key misses. Separators in the
number are significant ('1-2' is not '12', ING-18). A number that differs
from another of the same distributor, date and total only by a zero or
letter prefix is a near-duplicate: both are kept and reported, never one
replaced (ING-14). A credit memo carrying its invoice's number (the other
sign) is its own document, never a version (ING-11). When a second
file carries an invoice already on file, one version is current: a
balanced copy over an unbalanced one, then the export over the printout,
then the newer file — and a balanced invoice is never silently replaced by
a file whose total differs (both versions are kept in
`fintech_invoice_versions`, the conflict is reported). `purge_file` takes a
file's invoices out again, restoring another version where one exists.

Every invoice in a file belongs to the store it names: `fintech_locations`
maps a route's store numbers to restaurants, and an invoice for a store
with no mapping is skipped and reported, never written to the route's
restaurant (the first file a restaurant receives teaches its own store
number when it names only one).

`ingest_file` says `stored` only when it wrote an invoice; a file whose
invoices are all undated is `unrecognized`.

The Bar page reads these tables (since 10/14/26: bar.purchases._lines, behind
the bar_cost facade) for its purchases, price watch and bought-vs-sold
reading; nothing else owner-facing does.
"""
import csv
import hashlib
import io
import json
import math
import re
import unicodedata
from datetime import datetime, timezone

import models as _models

# Seen in EJ's export (5/4-10/13/26): CA, KE, EA, BO, DS (the keg deposit
# lines and their empty-keg credits).
UOM_WORDS = {"CA": "case", "CS": "case", "BO": "bottle", "BL": "bottle", "BT": "bottle", "EA": "each",
             "KE": "keg", "KG": "keg", "DS": "deposit"}

# The deposit on a keg in Illinois, charged inside the keg's cost and
# credited back on the empty — the default only. Each distributor's own
# deposit is learned from its DS / EMPTY lines (_learned_deposit; FC/Bar
# audit 10/16/26, BAR-CALC-11: a $50 half-barrel or $25 sixtel deposit stayed
# inside beer cost).
KEG_DEPOSIT = 30.0
# A learned per-keg deposit is believed between these.
DEPOSIT_MIN, DEPOSIT_MAX = 5.0, 200.0
# An export keg line whose Item Adjustments is exactly one whole-dollar step
# per keg in this range is a deposit even with no DS / EMPTY line to learn
# it from (a uniform per-keg step).
UNIFORM_STEP_MIN, UNIFORM_STEP_MAX = 10.0, 100.0
# A price under this beside the same code's real price is a per-unit charge
# on that item (Southern Glazer's $2.29 on a bottle), not more of it.
CHARGE_MAX = 5.0
# A printout line takes a unit from the export line of the same code whose
# latest price is within this ratio of its own (a case and a bottle of one
# code differ by 6-12x).
UNIT_PRICE_RATIO = 1.5

_FEE_RE = re.compile(r"\b(fuel|service fee|delivery|freight|surcharge)\b", re.I)

# One file is read up to these (bar fix round 10/15/26, R5): a daily file is
# tens of invoices, a year's export a few thousand lines. Past them the file
# is refused whole (`too_many_rows`, its text not kept): a 15 MB export was
# 148,545 lines in one 3.2 s write. Invoices are committed in batches of
# COMMIT_EVERY, the file staying `reading` until the last one is written.
MAX_FILE_LINES = 20000
MAX_FILE_INVOICES = 2000
COMMIT_EVERY = 500
_ADJ_RE = re.compile(r"\b(summary\s+level\s+adjust\w*|adjustments?|rounding)\b", re.I)
_KEG_CREDIT_RE = re.compile(r"\bempt(y|ies)\b", re.I)
# A keg by its description (bar.classify._KEG_RE says the same for the
# category; kept here so the reader needs nothing above it). '1/6BL' and
# '1/2 BL' are kegs too (bar.products._KEGS reads them; FC/Bar audit
# 10/16/26, ING-16) — until bar.classify exports one shared pattern.
_KEG_RE = re.compile(r"\b(1/[246]\s*BB\w*|1/[246]\s*BL\b|BBL|BARREL|KEG|50L|30L|20L|K\s+1/2|K\s+50L|SIXTEL)\b",
                     re.I)
_NULLS = {"<null>", "null"}


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# Columns added after 10/14/26 (bar fix round 10/15/26): invoices carry
# their canonical keys and the printout's references; lines carry their
# restaurant (deletion, tenant-scoped reads), whether the unit was inferred,
# the discount and a mark when a figure could not be read.
_LATE_COLUMNS = (
    ("fintech_invoices", "distributor_key", "TEXT"),
    ("fintech_invoices", "number_key", "TEXT"),
    ("fintech_invoices", "vendor_id", "TEXT"),
    ("fintech_invoices", "ref_invoice_number", "TEXT"),
    ("fintech_invoices", "po_number", "TEXT"),
    ("fintech_invoices", "po_date", "TEXT"),
    ("fintech_invoice_lines", "restaurant_id", "INTEGER"),
    ("fintech_invoice_lines", "uom_inferred", "INTEGER"),
    ("fintech_invoice_lines", "discount", "REAL"),
    ("fintech_invoice_lines", "unparsed", "INTEGER"),
)


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
            distributor_key TEXT,
            number_key TEXT,
            vendor_id TEXT,
            ref_invoice_number TEXT,
            po_number TEXT,
            po_date TEXT,
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
            kind TEXT NOT NULL,
            restaurant_id INTEGER,
            uom_inferred INTEGER,
            discount REAL,
            unparsed INTEGER)""")
        for table, col, decl in _LATE_COLUMNS:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_lines_invoice ON fintech_invoice_lines(invoice_id)")
        # The old cross-tenant item-code index: no query uses it (every read
        # is scoped through the invoice or the line's restaurant_id). Candidate
        # for future cleanup after additional verification (bar fix round
        # 10/15/26, R22) — the 10-point trace is not done.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_lines_item ON fintech_invoice_lines(item_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_lines_rid_item "
                     "ON fintech_invoice_lines(restaurant_id, item_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_invoices_number "
                     "ON fintech_invoices(restaurant_id, number_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_invoices_file ON fintech_invoices(file_id)")
        # Every version of an invoice a file carried: the current one is the
        # invoice's lines; the others can be restored from their file.
        conn.execute("""CREATE TABLE IF NOT EXISTS fintech_invoice_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            source_format TEXT,
            total REAL,
            lines_total REAL,
            balanced INTEGER,
            line_count INTEGER,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(invoice_id, file_id))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fintech_versions_file ON fintech_invoice_versions(file_id)")
        # A route's Fintech store numbers -> the restaurant each one is.
        conn.execute("""CREATE TABLE IF NOT EXISTS fintech_locations (
            restaurant_id INTEGER NOT NULL,
            location_key TEXT NOT NULL,
            target_restaurant_id INTEGER NOT NULL,
            set_by TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, location_key))""")
        # The lines written before they carried their restaurant (once; a
        # line whose invoice is gone stays NULL for delete_orphan_lines).
        conn.execute("UPDATE fintech_invoice_lines SET restaurant_id = (SELECT i.restaurant_id FROM fintech_invoices i "
                     "WHERE i.id = fintech_invoice_lines.invoice_id) WHERE restaurant_id IS NULL")
        # The invoices written before their canonical keys (once).
        for r in conn.execute("SELECT id, distributor, invoice_number FROM fintech_invoices "
                              "WHERE distributor_key IS NULL OR number_key IS NULL").fetchall():
            conn.execute("UPDATE fintech_invoices SET distributor_key=?, number_key=? WHERE id=?",
                         (distributor_key(r["distributor"]), invoice_key(r["invoice_number"]), r["id"]))
        # The number keys written before separators were significant (ING-18,
        # 10/16/26): only a number holding a separator changes ('1-2' was
        # '12'). A credit document's key ('4471|CR|2026-10-03') is its own.
        # The new key only ever splits an old one, so no two rows merge.
        for r in conn.execute("SELECT id, invoice_number, number_key FROM fintech_invoices "
                              "WHERE number_key IS NOT NULL").fetchall():
            if "|" in str(r["number_key"]):
                continue
            k = invoice_key(r["invoice_number"])
            if k != r["number_key"]:
                conn.execute("UPDATE fintech_invoices SET number_key=? WHERE id=?", (k, r["id"]))
        conn.commit()
    finally:
        conn.close()


# ── values ─────────────────────────────────────────────────────────────────

def _blank(text) -> bool:
    s = str(text if text is not None else "").strip()
    return not s or s.lower() in _NULLS


def money(text):
    """'$1,467.22' -> 1467.22; a credit in any written form is negative:
    '($240.00)', '-$240.00', '$-240.00', '240.00-', 'CR 240.00',
    '240.00 CREDIT'. '' / '<null>' / unreadable -> None."""
    if _blank(text):
        return None
    s = str(text).strip()
    u = s.upper()
    neg = (s.startswith("(") and s.endswith(")")) or "-" in s or bool(re.search(r"(?<![A-Z])CR(EDIT)?(?![A-Z])", u))
    digits = re.sub(r"[^\d.]", "", s)
    if not digits or digits.count(".") > 1 or not re.search(r"\d", digits):
        return None
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


def number(text):
    """A quantity: '1,200' -> 1200.0, '-2.0' / '2-' / '(2)' -> -2.0; None
    when it cannot be read (never 0)."""
    if _blank(text):
        return None
    s = str(text).strip()
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-") or s.endswith("-")
    body = s.strip("()").strip("-").replace(",", "").strip()
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", body):
        return None
    v = float(body)
    return -v if neg else v


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d", "%Y%m%d",
                 "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%b-%d-%Y")


def iso_date(text):
    """'10/09/2026', '9/15/26', '2026-09-15', '2026-09-15T00:00:00',
    '10/13/2026 00:00:00', '10-01-2026', 'Oct 1, 2026', '1-Oct-2026',
    '20261001' -> 'YYYY-MM-DD'; else None."""
    if _blank(text):
        return None
    s = str(text).strip()
    m = re.match(r"^(\d{4}-\d{1,2}-\d{1,2})[T ]", s)
    if m:
        s = m.group(1)
    # a time after the date ('00:00:00', '12:00 AM', 'Z')
    s = re.sub(r"[\sT]+\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*([AaPp][Mm])?\s*(Z|[+-]\d{2}:?\d{2})?$", "", s)
    s = re.sub(r"\s+", " ", s.replace(",", " ")).strip()
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if 1990 <= d.year <= 2100:
            return d.strftime("%Y-%m-%d")
    return None


def uom_word(code):
    return UOM_WORDS.get(str(code or "").strip().upper())


_STATES = {"alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
           "colorado": "co", "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga",
           "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
           "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
           "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
           "nebraska": "ne", "nevada": "nv", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
           "pennsylvania": "pa", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
           "virginia": "va", "washington": "wa", "wisconsin": "wi", "wyoming": "wy"}
_ENTITY_WORDS = {"llc", "inc", "incorporated", "co", "corp", "corporation", "company", "ltd", "lp", "llp",
                 "pc", "the", "of", "and"}


def distributor_key(name) -> str:
    """A distributor's canonical name: casefolded, accents and punctuation
    out, '&' as 'and', state names as their codes, the entity words (LLC,
    Inc, Co …) and anything after 'dba' dropped. "Breakthru Beverage
    Illinois, LLC" and "BREAKTHRU BEVERAGE ILLINOIS LLC" are one key, as are
    "Southern Glazer's Wine & Spirits of IL" and "Southern Glazers Wine and
    Spirits of Illinois"."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().casefold()
    s = re.split(r"\bd\s*/?\s*b\s*/?\s*a\b", s)[0]
    s = s.replace("&", " and ").replace("'", "").replace("’", "")
    words = [_STATES.get(w, w) for w in re.findall(r"[a-z0-9]+", s)]
    key = " ".join(w for w in words if w not in _ENTITY_WORDS)
    if not key and s.strip():
        key = " ".join(re.findall(r"[a-z0-9]+", s))
    return key


def invoice_key(number) -> str:
    """An invoice number as one key: upper case, spaces and a leading '#'
    out, leading zeros out ('0128850598' and '128850598' are one invoice).
    Separators stay significant (FC/Bar audit 10/16/26, ING-18): '1-2' and
    '12' are two invoices, so '12', '0012' and '1-2' no longer collapse into
    one. The old looser key is `loose_invoice_key` — a spelling the strict
    key misses is caught on it only with the same distributor, date and
    total (_find_existing)."""
    s = re.sub(r"\s+", "", str(number or "").upper()).lstrip("#")
    return s.lstrip("0") or s


def loose_invoice_key(number) -> str:
    """The pre-10/16/26 key: letters and digits only, leading zeros out
    ('1-2' and '12' are one key). Used where two systems spell one number
    differently (Back Office ↔ Fintech; backoffice_ap's stored keys were
    written with it) and as the same-date same-total spelling guard here —
    never as Fintech identity on its own."""
    s = re.sub(r"[^A-Z0-9]", "", str(number or "").upper())
    return s.lstrip("0") or s


def item_key(code) -> str:
    """A distributor item code without its padding ('000009031' -> '9031');
    '' for a blank or all-zero code."""
    s = str(code or "").strip()
    if s.lower() in _NULLS:
        return ""
    return s.lstrip("0")


def location_key(loc):
    """Fintech's store number as one key ('000001  St. Charles, IL' -> '1')."""
    s = str(loc or "").strip()
    if not s or s.lower() in _NULLS:
        return None
    s = s.split()[0].upper()
    return s.lstrip("0") or s


def _is_keg(ln) -> bool:
    return str(ln.get("uom") or "").upper() in ("KE", "KG") or bool(_KEG_RE.search(ln.get("description") or ""))


def _deposit_steps(amount, qty, rate=None) -> bool:
    """amount is a whole number of `rate` deposits (the distributor's learned
    deposit, else KEG_DEPOSIT), one per keg at most, in the quantity's
    direction."""
    rate = float(rate or KEG_DEPOSIT)
    if amount is None or qty in (None, 0) or abs(amount) < 0.005 or rate <= 0:
        return False
    k = amount / rate
    if abs(k - round(k)) > 0.0002:
        return False
    k = round(k)
    return 1 <= abs(k) <= abs(qty) + 1e-9 and (k > 0) == (qty > 0)


def _uniform_step(amount, qty) -> bool:
    """The export's Item Adjustments on a keg line is one whole-dollar
    deposit per keg (every keg, the same step) inside the believable range:
    a deposit at a rate no DS / EMPTY line has taught yet."""
    if amount is None or not qty or abs(qty - round(qty)) > 1e-9:
        return False
    step = amount / qty
    return (UNIFORM_STEP_MIN <= abs(step) <= UNIFORM_STEP_MAX and abs(step - round(step)) < 0.0001
            and (amount > 0) == (qty > 0))


def _learned_deposit(lines):
    """A distributor's per-keg deposit from its own DS (deposit) and EMPTY
    lines — the most common per-unit amount among them — or None when it
    has none (the caller falls back to KEG_DEPOSIT)."""
    seen = {}
    for ln in lines:
        desc = ln.get("description") or ""
        if str(ln.get("uom") or "").upper() != "DS" and not _KEG_CREDIT_RE.search(desc):
            continue
        q, p, c = ln.get("qty"), ln.get("unit_price"), ln.get("cost")
        each = abs(float(p)) if p else (abs(float(c) / float(q)) if c and q else None)
        if each is None or not (DEPOSIT_MIN <= each <= DEPOSIT_MAX):
            continue
        each = round(each, 2)
        seen[each] = seen.get(each, 0) + 1
    if not seen:
        return None
    return max(seen.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _settle(ln, rate=None):
    """Split what the line cost beyond qty × price into the keg deposit and
    any other adjustment (both inside `cost`, which stays the truth). `rate`
    is the distributor's learned deposit (KEG_DEPOSIT when none)."""
    q, p, c = ln.get("qty"), ln.get("unit_price"), ln.get("cost")
    raw = ln.get("raw_adjustment")
    if _KEG_CREDIT_RE.search(ln.get("description") or ""):
        ln["deposit"] = c if c else None
        ln["adjustment"] = None
        return ln
    diff = round(c - q * p, 2) if None not in (q, p, c) else (raw if raw is not None else None)
    deposit = None
    if _is_keg(ln):
        for cand in (raw, diff):
            if _deposit_steps(cand, q, rate):
                deposit = round(cand, 2)
                break
        if deposit is None and _uniform_step(raw, q):
            deposit = round(raw, 2)
    rest = None if diff is None else round(diff - (deposit or 0.0), 2)
    ln["deposit"] = deposit
    ln["adjustment"] = rest if rest is not None and abs(rest) >= 0.005 else None
    return ln


def _kind(desc, price, known_price):
    d = desc or ""
    if _KEG_CREDIT_RE.search(d):
        return "keg_credit"
    if _ADJ_RE.search(d):
        return "adjustment"
    if _FEE_RE.search(d):
        return "fee"
    # A line at a few dollars for an item whose real price (on this invoice
    # or any other from the distributor) is more: a per-unit charge on that
    # item (Southern Glazer's $2.29 beside a $249.50 case), not more of it.
    # What the charge is (a deposit, a tax) the file does not say. A $0 line
    # is free goods, never a charge.
    if price is not None and 0 < price < CHARGE_MAX and (known_price or 0) >= CHARGE_MAX:
        return "charge"
    return "item"


# ── the printout ───────────────────────────────────────────────────────────

_HDR_RE = re.compile(r"^\s*(Store Number|Distributor|Vendor ID|Invoice Number|Invoice Date|Invoice Total|Due Date|"
                     r"PO Number|PO Date|Ref Inv(?:oice)? Number)\s*:\s*(.*)$")
_MONEY = r"(?:\(\s*)?-?\$\s*-?[\d,]*\d\.\d{2}(?:\s*\))?-?(?:\s*CR)?"
_QTY = r"\(?-?[\d,]*\d(?:\.\d+)?-?\)?"
_LINE_RE = re.compile(rf"^\s*(\S+)\s+(.+?)\s+({_QTY})\s+({_MONEY})\s+({_MONEY})\s*$")


def _line(code, desc, qty, price, cost, upc=None, uom=None, raw_adjustment=None, discount=None, unparsed=None):
    return {"item_code": None if _blank(code) else str(code).strip(), "description": str(desc or "").strip(),
            "upc": None if _blank(upc) else str(upc).strip(), "uom": (str(uom).strip().upper() or None) if not _blank(uom) else None,
            "qty": qty, "unit_price": price, "cost": cost, "deposit": None, "adjustment": None,
            "raw_adjustment": raw_adjustment, "discount": discount, "uom_inferred": None, "unparsed": unparsed}


def parse_printout(text):
    """Invoices from the printout's text: [{distributor, invoice_number,
    invoice_date, due_date, total, location_id, vendor_id, po_number,
    po_date, ref_invoice_number, lines: [...]}]. A line in an invoice's
    item block that cannot be read is kept as kind 'unparsed' (no cost),
    so its invoice reads unbalanced instead of quietly short."""
    invoices, cur, in_lines = [], None, False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _HDR_RE.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == "Store Number" or (key == "Distributor" and (cur is None or cur.get("distributor"))):
                cur = {"location_id": None, "lines": []}
                invoices.append(cur)
                in_lines = False
            if key == "Store Number":
                cur["location_id"] = val.split()[0] if val else None
            elif key == "Distributor":
                cur["distributor"] = val
            elif key == "Vendor ID":
                cur["vendor_id"] = val or None
            elif key == "Invoice Number":
                cur["invoice_number"] = val
            elif key == "Invoice Date":
                cur["invoice_date"] = iso_date(val)
            elif key == "Due Date":
                cur["due_date"] = iso_date(val)
            elif key == "Invoice Total":
                cur["total"] = money(val)
            elif key == "PO Number":
                cur["po_number"] = val or None
            elif key == "PO Date":
                cur["po_date"] = iso_date(val)
            else:
                cur["ref_invoice_number"] = val or None
            continue
        if cur is None:
            continue
        if line.strip().startswith("----------"):
            in_lines = True
            continue
        if "Invoice Total:" in line:
            in_lines = False
            continue
        if re.match(r"^\s*Page\s*:", line):
            in_lines = False
            continue
        if not in_lines or not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m:
            cur["lines"].append(_line(None, line.strip()[:200], None, None, None, unparsed=1))
            continue
        code, desc, qty, price, cost = m.groups()
        q, p, c = number(qty), money(price), money(cost)
        cur["lines"].append(_settle(_line(code, desc, q, p, c, unparsed=1 if q is None else None)))
    # An invoice the page break split in two (its header printed again on
    # the next page) is one invoice with every line.
    out, by_key = [], {}
    for inv in invoices:
        if not (inv.get("invoice_number") and inv.get("distributor")):
            continue
        k = (distributor_key(inv["distributor"]), invoice_key(inv["invoice_number"]))
        if k in by_key:
            by_key[k]["lines"].extend(inv["lines"])
            continue
        by_key[k] = inv
        out.append(inv)
    return out


# ── the export ─────────────────────────────────────────────────────────────

def _norm(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


_EXPORT_COLS = {
    "distributor": ("vendorname", "distributor", "distributorname"),
    "vendor_id": ("retailervendorid", "vendorid"),
    "invoice_number": ("invoicenumber", "invoiceno", "invoice"),
    "invoice_date": ("invoicedate",),
    "due_date": ("invoiceduedate", "duedate"),
    "total": ("invoicetotal", "invoiceamount"),
    "location_id": ("locationid", "storenumber", "storeid"),
    "po_number": ("ponumber",),
    "po_date": ("podate",),
    "ref_invoice_number": ("refinvoicenumber", "referenceinvoicenumber", "refinvnumber"),
    "item_code": ("productnumber", "itemnumber", "item", "itemcode"),
    "description": ("productdescription", "description", "itemdescription"),
    "upc": ("upc", "upcnumber"),
    "uom": ("unitofmeasure", "uom", "unit"),
    "qty": ("itemquantity", "quantity", "qty"),
    "unit_price": ("itemcost", "unitcost", "price"),
    "adjustment": ("itemadjustments", "itemadjustment", "totaladjustments", "adjustment"),
    "discount": ("totaldiscount", "itemdiscount", "itemdiscounts", "discount"),
    "cost": ("extendedcost", "extendedamount", "extcost", "extendedprice", "linetotal", "lineamount"),
}
# Goods columns kept with the file for a later reader (bottles per case, the
# pack's UPC, the distributor's GL class); never a payment column.
_KEEP_EXTRA = ("retailername", "ppc", "packupc", "glcode")
_KEEP_COLUMNS = frozenset(n for names in _EXPORT_COLS.values() for n in names) | frozenset(_KEEP_EXTRA)


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
    """Invoices from the payment-activity / invoice-details CSV, grouped by
    distributor and invoice number in file order."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or not is_export(rows[0]):
        return []
    idx = _columns(rows[0])

    def cell(row, field):
        i = idx.get(field)
        v = row[i].strip() if i is not None and i < len(row) else ""
        return "" if v.lower() in _NULLS else v

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
                   "due_date": iso_date(cell(row, "due_date")), "total": money(cell(row, "total")),
                   "location_id": cell(row, "location_id") or None, "vendor_id": cell(row, "vendor_id") or None,
                   "po_number": cell(row, "po_number") or None, "po_date": iso_date(cell(row, "po_date")),
                   "ref_invoice_number": cell(row, "ref_invoice_number") or None, "lines": []}
            by_key[(dist, num)] = inv
            invoices.append(inv)
        q = number(cell(row, "qty"))
        p = money(cell(row, "unit_price"))
        adj = money(cell(row, "adjustment"))
        disc = money(cell(row, "discount"))
        disc = -abs(disc) if disc else None
        c = money(cell(row, "cost"))
        if c is None and p is not None and q is not None:
            c = round(q * p + (adj or 0) + (disc or 0), 2)
        unparsed = 1 if q is None or (c is None and p is None) else None
        inv["lines"].append(_settle(_line(cell(row, "item_code"), cell(row, "description"), q, p, c,
                                          upc=cell(row, "upc"), uom=cell(row, "uom"), raw_adjustment=adj,
                                          discount=disc, unparsed=unparsed)))
    return invoices


def _export_kept(text):
    """The CSV with only the columns this module reads (and _KEEP_EXTRA):
    an allow-list, so a payment column under any name is never kept."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text
    keep = [i for i, h in enumerate(rows[0]) if _norm(h) in _KEEP_COLUMNS]
    out = io.StringIO()
    w = csv.writer(out)
    for row in rows:
        w.writerow([row[i] if i < len(row) else "" for i in keep])
    return out.getvalue()


# ── reading a file ─────────────────────────────────────────────────────────

def _pdf_text(content):
    """An untrusted PDF's text, read in a child process with a page cap and a
    time budget (email_intake.pdf_text, bar fix round 10/15/26 R20); raises
    email_intake.FileRefused past either."""
    import email_intake
    return email_intake.pdf_text(content)


# ── a stored file's text (R5) ──────────────────────────────────────────────
#
# fintech_files.content is the kept text inline, or — with BLOB_STORE set —
# a string pointer to its copy in the object store ("fintech-files/<rid>/
# <sha256>"). Every reader goes through _content; every writer _keep.

def _content(value):
    """The kept text a fintech_files.content value holds (inline, or read
    from the object store)."""
    import blob_store
    return blob_store.resolve_text(value) if value else value


def _keep(restaurant_id, key, text):
    """What fintech_files.content stores for `text`: the text, or its
    object-store pointer."""
    import blob_store
    return blob_store.store_text("fintech-files", f"{int(restaurant_id)}/{key}", text)


def _replace_content(conn, restaurant_id, file_id, old_value, text):
    """A stored file's kept text rewritten (an export cut to today's column
    allow-list): the new text kept as a new file would be, the old object
    deleted."""
    import blob_store
    new = _keep(restaurant_id, f"file-{int(file_id)}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}", text)
    conn.execute("UPDATE fintech_files SET content=? WHERE id=?", (new, int(file_id)))
    if old_value != new:
        blob_store.delete_text(old_value)


def _decode(content):
    """Bytes as text: a UTF-16 file by its BOM, then UTF-8, then Windows-1252
    (Excel's "Southern Glazer’s"), then latin-1. None when the text holds a
    NUL (a UTF-16 file without its BOM, a binary file)."""
    if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = content.decode("utf-16")
        except UnicodeDecodeError:
            return None
    else:
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    if text is None or "\x00" in text:
        return None
    return text


def read(filename, content):
    """(format, kept_text, invoices) for one file's bytes. format is
    'printout', 'export' or None (unrecognized)."""
    if content[:5] == b"%PDF-":
        text = _pdf_text(content)
        if "\x00" in text:
            return None, None, []
    elif content[:2] == b"PK":
        return None, None, []          # a spreadsheet: Fintech's CSV is the one read
    else:
        text = _decode(content)
        if text is None:
            return None, None, []
    if "Invoice Number" in text and "Distributor" in text and re.search(r"Invoice Number\s*:", text):
        return "printout", text, parse_printout(text)
    first = next(csv.reader(io.StringIO(text.split("\n", 1)[0])), [])
    if is_export(first):
        return "export", _export_kept(text), parse_export(text)
    return None, None, []


# ── storing ────────────────────────────────────────────────────────────────

def _same_distributor(row, dkey, vendor_id) -> bool:
    rv = row["vendor_id"]
    if rv and vendor_id:
        return str(rv).strip() == str(vendor_id).strip()
    return (row["distributor_key"] or distributor_key(row["distributor"])) == dkey


_INV_COLS = ("id, restaurant_id, distributor, invoice_number, invoice_date, total, source_format, file_id, balanced, "
             "lines_total, distributor_key, number_key, vendor_id")


# A number that differs from another only by a leading prefix of zeros or
# letters ('INV12345' / '12345', '0012345' before the zeros were cut) — the
# one suffix pattern a spelling difference makes. '112' vs '12' is not one.
_PREFIX_RE = re.compile(r"^(0+|[A-Z]+[-/]?)$")


def _prefix_variant(a, b) -> bool:
    if not a or not b or a == b:
        return False
    long_, short = (a, b) if len(a) > len(b) else (b, a)
    return long_.endswith(short) and bool(_PREFIX_RE.match(long_[:-len(short)]))


def _similar_distributor(row, dkey, vendor_id) -> bool:
    """The same distributor, or one spelled two ways: every word of one
    canonical key is in the other, two words at least ("Southern Glazers of
    IL" / "Southern Glazer's Wine & Spirits of IL", EDG-5 S10c). Never two
    houses that only share a word (Lakeshore / Winebow, ING-14)."""
    if _same_distributor(row, dkey, vendor_id):
        return True
    rv = row["vendor_id"]
    if rv and vendor_id:
        return False
    a = set((row["distributor_key"] or distributor_key(row["distributor"])).split())
    b = set(dkey.split())
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small) >= 2 and small <= big


def _find_existing(conn, rid, inv, dkey, nkey):
    """(row, near_row, spelled) — `row` the invoice already on file: the same
    number and distributor by key, or (`spelled`) the same date and total
    with the number and distributor spelled another way (`loose_invoice_key`,
    `_similar_distributor`). `near_row` an invoice that only LOOKS like this
    one: the same distributor, date and total with a number that differs by
    a zero or letter prefix. A near row is never replaced — both are kept
    and the file reports it (FC/Bar audit 10/16/26, ING-14: Lakeshore 112
    was overwritten by Lakeshore 12, and Lakeshore 5001 by Winebow 5001)."""
    for r in conn.execute(f"SELECT {_INV_COLS} FROM fintech_invoices WHERE restaurant_id=? AND number_key=? ORDER BY id",
                          (rid, nkey)).fetchall():
        if _same_distributor(r, dkey, inv.get("vendor_id")):
            return r, None, False
    total, day = inv.get("total"), inv.get("invoice_date")
    near = None
    if day and total is not None and abs(total) >= 0.01:
        loose = loose_invoice_key(inv.get("invoice_number"))
        for r in conn.execute(f"SELECT {_INV_COLS} FROM fintech_invoices WHERE restaurant_id=? AND invoice_date=? "
                              "ORDER BY id", (rid, day)).fetchall():
            if r["total"] is None or abs(float(r["total"]) - total) >= 0.005 or "|" in str(r["number_key"] or ""):
                continue
            rk = loose_invoice_key(r["invoice_number"])
            if rk == loose and _similar_distributor(r, dkey, inv.get("vendor_id")):
                return r, None, True   # one invoice, its number or distributor spelled two ways
            if near is None and _same_distributor(r, dkey, inv.get("vendor_id")) and _prefix_variant(rk, loose):
                near = r
    return None, near, False


def _balance_rank(b):
    return {1: 2, None: 1, 0: 0}.get(None if b is None else int(b), 1)


def _decide(existing, fmt, total, balanced, file_id) -> str:
    """'replace' | 'keep' | 'conflict' for a file's copy of an invoice
    already on file."""
    if existing["file_id"] == file_id:
        return "replace"               # the same file read again with today's reader
    et = existing["total"]
    if existing["balanced"] == 1 and et is not None and total is not None and abs(float(et) - total) >= 0.02:
        return "conflict"              # never silently over a balanced invoice
    be, bi = _balance_rank(existing["balanced"]), _balance_rank(balanced)
    if be != bi:
        return "replace" if bi > be else "keep"
    fe, fi = existing["source_format"] == "export", fmt == "export"
    if fe != fi:
        return "replace" if fi else "keep"   # the export's UPC and unit of measure win
    return "replace" if int(file_id or 0) > int(existing["file_id"] or 0) else "keep"


def _version(conn, rid, invoice_id, file_id, fmt, total, lines_total, balanced, line_count, state):
    now = _now()
    conn.execute("INSERT INTO fintech_invoice_versions (restaurant_id, invoice_id, file_id, source_format, total, "
                 "lines_total, balanced, line_count, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(invoice_id, file_id) DO UPDATE SET source_format=excluded.source_format, "
                 "total=excluded.total, lines_total=excluded.lines_total, balanced=excluded.balanced, "
                 "line_count=excluded.line_count, state=excluded.state, updated_at=excluded.updated_at",
                 (rid, invoice_id, file_id, fmt, total, lines_total, balanced, line_count, state, now, now))


def _current_version(conn, existing):
    """The invoice's version as it stands (written before versions were
    kept: recorded now from the row)."""
    if existing["file_id"] is None:
        return
    have = conn.execute("SELECT id FROM fintech_invoice_versions WHERE invoice_id=? AND file_id=?",
                        (existing["id"], existing["file_id"])).fetchone()
    if not have:
        n = conn.execute("SELECT COUNT(*) FROM fintech_invoice_lines WHERE invoice_id=?", (existing["id"],)).fetchone()[0]
        _version(conn, existing["restaurant_id"], existing["id"], existing["file_id"], existing["source_format"],
                 existing["total"], existing["lines_total"], existing["balanced"], n, "current")


def _history(conn, rid, dkey, vendor_id, exclude_invoice=None) -> list:
    """Every line this restaurant bought from the distributor (by key)."""
    sql = ("SELECT l.id, l.invoice_id, l.item_code, l.uom, l.unit_price, l.upc, l.kind, l.uom_inferred, l.qty, "
           "l.cost, l.description, i.source_format, i.invoice_date FROM fintech_invoice_lines l "
           "JOIN fintech_invoices i ON i.id = l.invoice_id WHERE i.restaurant_id=? AND (i.distributor_key=?")
    args = [rid, dkey]
    if vendor_id:
        sql += " OR i.vendor_id=?"
        args.append(str(vendor_id))
    sql += ")"
    if exclude_invoice:
        sql += " AND i.id<>?"
        args.append(int(exclude_invoice))
    return [dict(r) for r in conn.execute(sql + " ORDER BY i.invoice_date, l.id", args).fetchall()]


def _unit_book(history) -> dict:
    """{item_key: {uom: (date, price, upc)}}: the latest export price of each
    code in each unit."""
    book = {}
    for h in history:
        if (h["source_format"] != "export" or not h["uom"] or h["uom_inferred"] or h["kind"] != "item"
                or not h["unit_price"] or float(h["unit_price"]) <= 0):
            continue
        k = item_key(h["item_code"])
        if not k:
            continue
        prev = book.setdefault(k, {}).get(h["uom"])
        if prev is None or str(h["invoice_date"] or "") >= str(prev[0] or ""):
            book[k][h["uom"]] = (h["invoice_date"], float(h["unit_price"]), h["upc"])
    return book


def _infer_unit(ln, book):
    """A printout line's unit and UPC from the same code's export history: the
    unit whose latest price is nearest its own, within UNIT_PRICE_RATIO;
    nothing when none is that close (a bottle bought from a case code)."""
    if ln.get("uom") or not ln.get("unit_price") or ln["unit_price"] <= 0:
        return False
    units = book.get(item_key(ln.get("item_code")))
    if not units:
        return False
    best = min(units.items(), key=lambda kv: abs(math.log(ln["unit_price"] / kv[1][1])))
    # A code only ever sold by the keg is a keg whatever this line's price (a
    # keg is never split; a credit memo re-bills one at $59).
    keg_only = all(u in ("KE", "KG") for u in units)
    if not keg_only and abs(math.log(ln["unit_price"] / best[1][1])) > math.log(UNIT_PRICE_RATIO):
        return False
    ln["uom"], ln["uom_inferred"] = best[0], 1
    if not ln.get("upc") and best[1][2]:
        ln["upc"] = best[1][2]
    return True


def _known_prices(history, lines) -> dict:
    """{item_key: the highest real price on file}: any invoice from the
    distributor, and every line of this one (whatever its order)."""
    known = {}
    for h in list(history) + list(lines):
        if h.get("kind") not in (None, "item") or not h.get("unit_price"):
            continue
        k = item_key(h.get("item_code"))
        if k and float(h["unit_price"]) >= CHARGE_MAX:
            known[k] = max(known.get(k, 0.0), float(h["unit_price"]))
    return known


def _signature(lines) -> list:
    return sorted((item_key(ln.get("item_code")), str(ln.get("description") or ""), ln.get("qty"),
                   ln.get("unit_price"), ln.get("cost"), str(ln.get("uom") or "")) for ln in lines)


def _stored_signature(conn, invoice_id) -> list:
    rows = conn.execute("SELECT item_code, description, qty, unit_price, cost, uom, uom_inferred "
                        "FROM fintech_invoice_lines WHERE invoice_id=?", (invoice_id,)).fetchall()
    return _signature([dict(r, uom=None if r["uom_inferred"] else r["uom"]) for r in rows])


def _sign(total, lines_total):
    v = total if total is not None else lines_total
    if v is None or abs(float(v)) < 0.005:
        return 0
    return 1 if float(v) > 0 else -1


def _opposite_sign(existing, total, lines_total) -> bool:
    """The file's copy and the invoice on file under the same number run in
    opposite directions - a credit memo against its invoice."""
    a = _sign(existing["total"], existing["lines_total"])
    b = _sign(total, lines_total)
    return a != 0 and b != 0 and a != b


def _document_key(nkey, inv, signed_total) -> tuple:
    """(number_key, invoice_number shown) for a same-number document running
    the other way: '4471|CR|2026-10-03' and '4471 credit 10/3/26' for a
    credit memo, '|DB|' / 'debit' for an invoice that arrived after its
    credit. The date keeps two credits on one number apart."""
    from time_utils import mdy
    day = inv.get("invoice_date") or ""
    tag, word = ("CR", "credit") if float(signed_total or 0) < 0 else ("DB", "debit")
    shown = f"{inv['invoice_number']} {word}" + (f" {mdy(day)}" if day else "")
    return f"{nkey}|{tag}|{day}", shown


def _same_document(p, inv_row) -> bool:
    """A parsed invoice `p` is the stored row's document: the same number key
    (a credit document's key carries its tag and date)."""
    nkey = inv_row["number_key"] or invoice_key(inv_row["invoice_number"])
    pk = invoice_key(p.get("invoice_number"))
    if "|" not in nkey:
        return pk == nkey
    base, tag, day = (nkey.split("|") + ["", ""])[:3]
    want = -1 if tag == "CR" else 1
    lt = round(sum((ln.get("cost") or 0) for ln in p.get("lines") or []), 2)
    return pk == base and (p.get("invoice_date") or "") == day and _sign(p.get("total"), lt) == want


def _store_invoice(conn, restaurant_id, inv, fmt, file_id, force=False) -> dict:
    """Write one parsed invoice. Returns {result: written | kept | conflict,
    invoice_id, near_duplicate, balanced, total, kept_total}."""
    rid = int(restaurant_id)
    dkey, nkey = distributor_key(inv["distributor"]), invoice_key(inv["invoice_number"])
    existing, near, spelled = _find_existing(conn, rid, inv, dkey, nkey)
    total = inv.get("total")
    real = [ln for ln in inv["lines"] if ln.get("kind") != "unparsed" and not (ln.get("unparsed") and ln.get("cost") is None)]
    lines_total = round(sum((ln["cost"] or 0) for ln in inv["lines"]), 2)
    number = inv["invoice_number"]
    if existing is not None and _opposite_sign(existing, total, lines_total):
        # A credit memo carrying its invoice's number (or the invoice after
        # its credit): its own document, never a version of the other
        # (FC/Bar audit 10/16/26, ING-11 - Southern 4471 $600 then credit
        # 4471 -$300 was a 'conflict' and the credit was never counted).
        nkey, number = _document_key(nkey, inv, total if total is not None else lines_total)
        existing, near, spelled = None, None, False
        for r in conn.execute(f"SELECT {_INV_COLS} FROM fintech_invoices WHERE restaurant_id=? AND number_key=? "
                              "ORDER BY id", (rid, nkey)).fetchall():
            if _same_distributor(r, dkey, inv.get("vendor_id")):
                existing = r
                break
    unread = any(ln.get("cost") is None and ln.get("unparsed") for ln in inv["lines"])
    balanced = None if total is None else int(abs(lines_total - total) < 0.02 and not unread)
    out = {"invoice_id": existing["id"] if existing else None, "near_duplicate": near is not None or spelled,
           "near_of": dict(near) if near is not None else None, "balanced": balanced,
           "total": total, "kept_total": existing["total"] if existing else None}
    if existing:
        _current_version(conn, existing)
        decision = "replace" if force else _decide(existing, fmt, total, balanced, file_id)
        if (decision == "replace" and not force and existing["file_id"] != file_id
                and existing["source_format"] == fmt and _signature(inv["lines"]) == _stored_signature(conn, existing["id"])):
            decision = "keep"          # the same invoice again, line for line: nothing to write
        if decision != "replace":
            _version(conn, rid, existing["id"], file_id, fmt, total, lines_total, balanced, len(inv["lines"]),
                     "conflict" if decision == "conflict" else "kept")
            out["result"] = "conflict" if decision == "conflict" else "kept"
            return out
    history = _history(conn, rid, dkey, inv.get("vendor_id"), exclude_invoice=existing["id"] if existing else None)
    book = _unit_book(history)
    rate = _learned_deposit(list(history) + list(inv["lines"]))
    for ln in inv["lines"]:
        _infer_unit(ln, book)
        _settle(ln, rate)              # an inferred keg carries its deposit, at this distributor's rate
    known = _known_prices(history, real)
    lines = []
    for n, ln in enumerate(inv["lines"], 1):
        if ln.get("unparsed") and ln.get("cost") is None and ln.get("qty") is None:
            kind = "unparsed"
        else:
            kind = _kind(ln["description"], ln["unit_price"], known.get(item_key(ln["item_code"])))
        lines.append((n, ln, kind))
    now = _now()
    ref = inv.get("ref_invoice_number")
    ref = None if not ref or invoice_key(ref) == nkey else ref
    if existing:
        inv_id = existing["id"]
        conn.execute("UPDATE fintech_invoices SET invoice_date=?, due_date=COALESCE(?, due_date), total=?, "
                     "location_id=?, source_format=?, file_id=?, lines_total=?, balanced=?, updated_at=?, "
                     "distributor_key=?, number_key=?, vendor_id=COALESCE(?, vendor_id), "
                     "ref_invoice_number=COALESCE(?, ref_invoice_number), po_number=COALESCE(?, po_number), "
                     "po_date=COALESCE(?, po_date) WHERE id=?",
                     (inv.get("invoice_date"), inv.get("due_date"), total, inv.get("location_id"), fmt, file_id,
                      lines_total, balanced, now, dkey, nkey, inv.get("vendor_id"), ref, inv.get("po_number"),
                      inv.get("po_date"), inv_id))
        # The export's own spelling of the distributor and number is the one
        # shown (unless another row already holds it: a duplicate the repair
        # merges).
        if fmt == "export" and (existing["distributor"], existing["invoice_number"]) != (inv["distributor"],
                                                                                        number):
            if not conn.execute("SELECT id FROM fintech_invoices WHERE restaurant_id=? AND distributor=? AND "
                                "invoice_number=? AND id<>?", (rid, inv["distributor"], number,
                                                               inv_id)).fetchone():
                conn.execute("UPDATE fintech_invoices SET distributor=?, invoice_number=? WHERE id=?",
                             (inv["distributor"], number, inv_id))
        conn.execute("DELETE FROM fintech_invoice_lines WHERE invoice_id=?", (inv_id,))
        conn.execute("UPDATE fintech_invoice_versions SET state='superseded', updated_at=? "
                     "WHERE invoice_id=? AND state='current' AND file_id<>?", (now, inv_id, file_id))
    else:
        cur = conn.execute("INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, "
                           "due_date, total, location_id, source_format, file_id, lines_total, balanced, updated_at, "
                           "distributor_key, number_key, vendor_id, ref_invoice_number, po_number, po_date) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (rid, inv["distributor"], number, inv.get("invoice_date"),
                            inv.get("due_date"), total, inv.get("location_id"), fmt, file_id, lines_total,
                            balanced, now, dkey, nkey, inv.get("vendor_id"), ref, inv.get("po_number"),
                            inv.get("po_date")))
        inv_id = cur.lastrowid
    for n, ln, kind in lines:
        conn.execute("INSERT INTO fintech_invoice_lines (invoice_id, restaurant_id, line_no, item_code, description, "
                     "upc, uom, qty, unit_price, cost, deposit, adjustment, kind, uom_inferred, discount, unparsed) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (inv_id, rid, n, ln["item_code"], ln["description"], ln["upc"], ln["uom"], ln["qty"],
                      ln["unit_price"], ln["cost"], ln["deposit"], ln["adjustment"], kind, ln.get("uom_inferred"),
                      ln.get("discount"), ln.get("unparsed")))
    _version(conn, rid, inv_id, file_id, fmt, total, lines_total, balanced, len(lines), "current")
    # An earlier few-dollar line of a code this invoice prices for real was a
    # charge all along (it arrived before the code's first real price).
    priced = {item_key(ln["item_code"]) for _, ln, kind in lines
              if kind == "item" and (ln["unit_price"] or 0) >= CHARGE_MAX and item_key(ln["item_code"])}
    for h in history:
        if (h["kind"] == "item" and item_key(h["item_code"]) in priced and h["unit_price"] is not None
                and 0 < float(h["unit_price"]) < CHARGE_MAX):
            conn.execute("UPDATE fintech_invoice_lines SET kind='charge' WHERE id=?", (h["id"],))
    out.update(result="written", invoice_id=inv_id)
    return out


def _backfill_units(conn, rid, dkeys) -> int:
    """Printout lines stored before their code's export arrived take its unit
    now (and a keg's deposit with it)."""
    n = 0
    for dkey in dkeys:
        history = _history(conn, rid, dkey, None)
        book = _unit_book(history)
        rate = _learned_deposit(history)
        for h in history:
            if h["source_format"] != "printout" or h["uom"] or h["kind"] != "item":
                continue
            ln = {"item_code": h["item_code"], "description": h["description"], "uom": None, "upc": h["upc"],
                  "qty": h["qty"], "unit_price": h["unit_price"], "cost": h["cost"], "raw_adjustment": None}
            if _infer_unit(ln, book):
                _settle(ln, rate)
                conn.execute("UPDATE fintech_invoice_lines SET uom=?, upc=?, uom_inferred=1, deposit=?, adjustment=? "
                             "WHERE id=?", (ln["uom"], ln["upc"], ln["deposit"], ln["adjustment"], h["id"]))
                n += 1
    return n


# ── store numbers ──────────────────────────────────────────────────────────

def location_map(restaurant_id, db_path=None) -> dict:
    """{store key: restaurant id} for a route's restaurant."""
    conn = get_conn(db_path)
    try:
        return {r["location_key"]: int(r["target_restaurant_id"]) for r in conn.execute(
            "SELECT location_key, target_restaurant_id FROM fintech_locations WHERE restaurant_id=?",
            (int(restaurant_id),)).fetchall()}
    finally:
        conn.close()


def set_location(restaurant_id, location_id, target_restaurant_id=None, set_by=None, db_path=None) -> dict:
    """Map one Fintech store number in the route restaurant's files to a
    restaurant (itself by default); target None with clear=True is not
    offered — a store is moved, never forgotten silently."""
    key = location_key(location_id)
    if not key:
        raise ValueError("A store number is needed.")
    target = int(target_restaurant_id or restaurant_id)
    conn = get_conn(db_path)
    try:
        if not conn.execute("SELECT id FROM restaurants WHERE id=?", (target,)).fetchone():
            raise ValueError("No such restaurant.")
        conn.execute("INSERT INTO fintech_locations (restaurant_id, location_key, target_restaurant_id, set_by, "
                     "created_at) VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, location_key) DO UPDATE SET "
                     "target_restaurant_id=excluded.target_restaurant_id, set_by=excluded.set_by",
                     (int(restaurant_id), key, target, set_by, _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "location_key": key, "restaurant_id": target}


def _route(conn, rid, invoices):
    """[(target_rid, inv)], [{location_id, invoices}] skipped. With no map
    yet, a file naming one store teaches it as the route's own; a file
    naming several is held until they are mapped."""
    mapping = {r["location_key"]: int(r["target_restaurant_id"]) for r in conn.execute(
        "SELECT location_key, target_restaurant_id FROM fintech_locations WHERE restaurant_id=?", (rid,)).fetchall()}
    locs = {location_key(i.get("location_id")) for i in invoices} - {None}
    if not mapping and len(locs) == 1:
        loc = next(iter(locs))
        conn.execute("INSERT INTO fintech_locations (restaurant_id, location_key, target_restaurant_id, set_by, "
                     "created_at) VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, location_key) DO NOTHING",
                     (rid, loc, rid, "first file", _now()))
        mapping = {loc: rid}
    live = {}
    routed, skipped = [], {}
    for inv in invoices:
        lk = location_key(inv.get("location_id"))
        target = rid if lk is None else mapping.get(lk)
        if target is not None and target != rid and target not in live:
            live[target] = bool(conn.execute("SELECT id FROM restaurants WHERE id=?", (target,)).fetchone())
        if target is None or (target != rid and not live.get(target)):
            skipped[inv.get("location_id") or ""] = skipped.get(inv.get("location_id") or "", 0) + 1
            continue
        routed.append((target, inv))
    return routed, [{"location_id": k, "invoices": v} for k, v in skipped.items()]


# ── reading, re-reading and taking a file out ──────────────────────────────

def _write(conn, rid, fmt, invoices, file_id, commit_every=None) -> dict:
    """Route and store a file's parsed invoices; the counts ingest reports.
    With `commit_every` (a new file's read, R5) the work is committed in
    batches of that many invoices — never one long write lock for a big
    file; a re-read leaves the commit to its caller (reread_all's dry run
    rolls back)."""
    routed, skipped = _route(conn, rid, invoices)
    res = {"invoices": 0, "lines": 0, "kept": 0, "conflicts": [], "near_duplicates": [], "unbalanced": [],
           "skipped_locations": skipped, "routed": {}}
    touched = {}
    for n, (target, inv) in enumerate(routed, 1):
        if commit_every and n % int(commit_every) == 0:
            conn.commit()
        o = _store_invoice(conn, target, inv, fmt, file_id)
        if o.get("near_duplicate"):
            # Kept as its own invoice beside the one it resembles (ING-14):
            # reported, for a person to purge if it was the same one.
            near = o.get("near_of") or {}
            res["near_duplicates"].append(f"{inv['invoice_number']} (kept beside #{near.get('invoice_number')})"
                                          if near.get("invoice_number") else inv["invoice_number"])
        if o["result"] == "written":
            res["invoices"] += 1
            res["lines"] += len(inv["lines"])
            if o["balanced"] == 0:
                res["unbalanced"].append(inv["invoice_number"])
            if target != rid:
                res["routed"][target] = res["routed"].get(target, 0) + 1
            touched.setdefault(target, set()).add(distributor_key(inv["distributor"]))
        elif o["result"] == "conflict":
            res["conflicts"].append({"distributor": inv["distributor"], "invoice_number": inv["invoice_number"],
                                     "kept_total": o["kept_total"], "file_total": o["total"]})
        else:
            res["kept"] += 1
    if fmt == "export":
        for target, dkeys in touched.items():
            _backfill_units(conn, target, dkeys)
    return res


def _status(res, parsed) -> str:
    if res["invoices"]:
        return "stored"
    if res["conflicts"]:
        return "conflict"
    if res["skipped_locations"] and sum(s["invoices"] for s in res["skipped_locations"]) == parsed:
        return "location_unmapped"
    return "unchanged"


# A vouched file (unsigned mail, its own contents its only credential) is
# held when an invoice's total passes this many times the distributor's
# largest on file, or a line's price sits this far outside the same item's
# prices on file (FC/Bar audit 10/16/26, SEC-02: a forged file naming a known
# store and distributor at inflated prices was read).
VOUCH_TOTAL_RATIO = 3.0
VOUCH_PRICE_RATIO = 3.0
# How many of the restaurant's latest files a new file's shape is checked
# against.
VOUCH_SHAPE_FILES = 10


def _shape(fmt, text):
    """A file's format and column set: an export's header columns (the ones
    kept, _KEEP_COLUMNS, so a kept copy and a fresh file compare alike), a
    printout's header labels. None when there is no text."""
    if not text:
        return None
    if fmt == "export":
        first = next(csv.reader(io.StringIO(text.split("\n", 1)[0])), [])
        return ("export", frozenset(_norm(h) for h in first if _norm(h) in _KEEP_COLUMNS))
    if fmt == "printout":
        labels = set()
        for line in text.splitlines():
            m = _HDR_RE.match(line)
            if m:
                labels.add(m.group(1))
        return ("printout", frozenset(labels))
    return None


def _shape_matches(new, earlier) -> bool:
    """An export's columns must be exactly an earlier export's (a forged CSV
    without the Location ID column is not Fintech's file); a printout's
    header labels must all be ones an earlier printout printed."""
    if new is None:
        return False
    if new[0] == "export":
        return any(e == new for e in earlier)
    seen = set()
    for e in earlier:
        if e and e[0] == "printout":
            seen |= e[1]
    return bool(seen) and new[1] <= seen


def _earlier_shapes(conn, rid) -> list:
    shapes = []
    for f in conn.execute("SELECT format, content FROM fintech_files WHERE restaurant_id=? AND format IS NOT NULL "
                          "AND content IS NOT NULL AND status NOT IN ('purged', 'reading') AND invoices > 0 "
                          "ORDER BY id DESC LIMIT ?", (rid, VOUCH_SHAPE_FILES)).fetchall():
        try:
            shapes.append(_shape(f["format"], _content(f["content"])))
        except Exception as e:          # an object-store read: the file cannot vouch, and someone hears
            import ops
            ops.capture(e, job="fintech_vouch", context=f"restaurant_id={rid}", restaurant_id=rid)
    return shapes


def _price_band(conn, rid):
    """({distributor key: largest |invoice total|}, {(distributor key, item
    key, uom): (lowest, highest) unit price}, {distributor key: highest unit
    price}) from the invoices on file."""
    totals, items, top = {}, {}, {}
    for r in conn.execute("SELECT distributor_key, MAX(ABS(total)) AS m FROM fintech_invoices WHERE restaurant_id=? "
                          "AND total IS NOT NULL GROUP BY distributor_key", (rid,)).fetchall():
        if r["distributor_key"]:
            totals[r["distributor_key"]] = float(r["m"] or 0)
    for r in conn.execute("SELECT i.distributor_key, l.item_code, l.uom, MIN(l.unit_price) AS lo, "
                          "MAX(l.unit_price) AS hi FROM fintech_invoice_lines l JOIN fintech_invoices i "
                          "ON i.id = l.invoice_id WHERE i.restaurant_id=? AND l.kind='item' AND l.unit_price > 0 "
                          "GROUP BY i.distributor_key, l.item_code, l.uom", (rid,)).fetchall():
        dk = r["distributor_key"]
        if not dk:
            continue
        for key in ((dk, item_key(r["item_code"]), str(r["uom"] or "")), (dk, item_key(r["item_code"]), "*")):
            lo, hi = items.get(key, (float(r["lo"]), float(r["hi"])))
            items[key] = (min(lo, float(r["lo"])), max(hi, float(r["hi"])))
        top[dk] = max(top.get(dk, 0.0), float(r["hi"]))
    return totals, items, top


def _out_of_band(inv, totals, items, top):
    """Why one invoice of a vouched file sits outside its distributor's
    history, or None."""
    dk = distributor_key(inv.get("distributor"))
    total = inv.get("total")
    if total is None:
        total = sum((ln.get("cost") or 0) for ln in inv.get("lines") or [])
    big = totals.get(dk)
    if big and abs(float(total)) > VOUCH_TOTAL_RATIO * big:
        return (f"invoice {inv.get('invoice_number')} is ${abs(float(total)):,.2f}, more than "
                f"{VOUCH_TOTAL_RATIO:g}x the largest on file from {inv.get('distributor')} (${big:,.2f})")
    for ln in inv.get("lines") or []:
        p = ln.get("unit_price")
        if not p or p <= 0 or _KEG_CREDIT_RE.search(ln.get("description") or ""):
            continue
        ik = item_key(ln.get("item_code"))
        band = items.get((dk, ik, str(ln.get("uom") or ""))) if ln.get("uom") else items.get((dk, ik, "*"))
        if band and not (band[0] / VOUCH_PRICE_RATIO <= p <= band[1] * VOUCH_PRICE_RATIO):
            return (f"{ln.get('description') or ik} at ${p:,.2f} is far outside its prices on file "
                    f"(${band[0]:,.2f}-${band[1]:,.2f})")
        if band is None and top.get(dk) and p > VOUCH_PRICE_RATIO * top[dk]:
            return (f"{ln.get('description') or ik} at ${p:,.2f} is more than {VOUCH_PRICE_RATIO:g}x anything "
                    f"bought from {inv.get('distributor')}")
    return None


def vouch(restaurant_id, filename, content, db_path=None) -> tuple:
    """(ok, why) — whether a file's own contents vouch for it when its email
    could not be authenticated (email_intake.process, owner 10/16/26: Fintech's
    mail went out unsigned on 10/15 and 10/16 and both files sat held). Read
    without writing. It must:
      - parse as a Fintech file with dated invoices;
      - name a store on EVERY invoice (a store-less invoice would be routed
        to the route's own restaurant — FC/Bar audit 10/16/26, ING-05), and
        only stores this restaurant's invoices or store map already carry;
      - name only distributors it already buys from;
      - have the format and column set of an earlier file (an export with
        exactly an earlier export's columns, a printout with an earlier
        printout's header labels);
      - sit inside each distributor's history: no invoice total over
        VOUCH_TOTAL_RATIO x its largest, no price VOUCH_PRICE_RATIO outside
        the item's prices on file (SEC-02);
      - change no invoice already on file (a resend of the same one is fine).
    A new store, a new distributor, a new shape or a price out of band is
    exactly what a forged file would bring, so any keeps the email held for
    an admin's one-tap release."""
    import email_intake
    try:
        fmt, kept, invoices = read(filename, content)
    except email_intake.FileRefused as e:
        return False, e.detail
    dated = [i for i in invoices if i.get("invoice_date")]
    if not fmt or not dated:
        return False, "it doesn't read as a Fintech invoice file"
    storeless = [i.get("invoice_number") for i in dated if location_key(i.get("location_id")) is None]
    if storeless:
        return False, (f"{len(storeless)} invoice{'s' if len(storeless) != 1 else ''} name"
                       f"{'' if len(storeless) != 1 else 's'} no store")
    rid = int(restaurant_id)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT location_id, distributor_key FROM fintech_invoices WHERE restaurant_id=?",
                            (rid,)).fetchall()
        known_dists = {r["distributor_key"] for r in rows if r["distributor_key"]}
        if not known_dists:
            return False, "no invoices on file yet to check it against"
        known_locs = {location_key(r["location_id"]) for r in rows} | set(location_map(rid, db_path))
        known_locs.discard(None)
        new_locs = sorted({location_key(i.get("location_id")) for i in dated} - known_locs)
        if new_locs:
            return False, f"store {', '.join(new_locs)} is new to this restaurant"
        new_dists = sorted({i.get("distributor") or "" for i in dated
                            if distributor_key(i.get("distributor")) not in known_dists})
        if new_dists:
            return False, f"{', '.join(new_dists[:3])} {'is' if len(new_dists) == 1 else 'are'} new to this restaurant"
        if not _shape_matches(_shape(fmt, kept), _earlier_shapes(conn, rid)):
            return False, f"its {fmt} layout matches none of the files this restaurant has received"
        totals, items, top = _price_band(conn, rid)
        for inv in dated:
            why = _out_of_band(inv, totals, items, top)
            if why:
                return False, why
            existing, _near, _spelled = _find_existing(conn, rid, inv, distributor_key(inv["distributor"]),
                                                       invoice_key(inv["invoice_number"]))
            if existing is not None and (existing["total"] is None or inv.get("total") is None
                                         or abs(float(existing["total"]) - float(inv["total"])) >= 0.02):
                return False, f"it would change invoice {inv['invoice_number']} already on file"
    finally:
        conn.close()
    return True, (f"{len(dated)} invoices from distributors and a store already on file, in an earlier file's "
                  f"layout, inside each distributor's prices")


def ingest_file(restaurant_id, filename, content, intake_id=None, db_path=None):
    """email_intake's reader for kind 'fintech'. Returns {"status": stored
    (an invoice written) | duplicate (the same file again) | unrecognized
    (neither format, or every invoice undated) | unchanged (every invoice
    already on file as well or better) | conflict (a balanced invoice on
    file disagrees: kept, reported) | location_unmapped, "format",
    "file_id", "invoices" (written), "parsed", "lines", "undated",
    "unbalanced", "kept", "conflicts", "near_duplicates",
    "skipped_locations", "routed"}."""
    sha = hashlib.sha256(content).hexdigest()
    rid = int(restaurant_id)
    conn = get_conn(db_path)
    try:
        seen = conn.execute("SELECT id, status FROM fintech_files WHERE restaurant_id=? AND sha256=?",
                            (rid, sha)).fetchone()
    finally:
        conn.close()
    if seen and seen["status"] != "reading":
        return {"status": "duplicate", "file_id": seen["id"]}
    # An untrusted file is refused, never half-read (R5, R20): a PDF past its
    # page cap or time budget, a file past MAX_FILE_LINES / MAX_FILE_INVOICES.
    import email_intake
    refused = None
    try:
        fmt, kept, invoices = read(filename, content)
    except email_intake.FileRefused as e:
        fmt, kept, invoices, refused = None, None, [], (e.status, e.detail)
    n_lines = sum(len(i["lines"]) for i in invoices)
    if refused is None and (len(invoices) > MAX_FILE_INVOICES or n_lines > MAX_FILE_LINES):
        refused = ("too_many_rows", f"{len(invoices)} invoices and {n_lines} lines; a file is read up to "
                                    f"{MAX_FILE_INVOICES} invoices and {MAX_FILE_LINES} lines")
        fmt, kept, invoices = None, None, []
    undated = sum(1 for i in invoices if not i.get("invoice_date"))
    readable = bool(fmt and invoices and undated < len(invoices))
    kept = _keep(rid, sha, kept)
    conn = get_conn(db_path)
    try:
        if seen:                       # a read a crash cut short: read again, whole
            file_id = seen["id"]
        else:
            cur = conn.execute("INSERT INTO fintech_files (restaurant_id, intake_id, filename, format, sha256, "
                               "content, invoices, lines, status, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (rid, intake_id, str(filename)[:200], fmt, sha, kept, 0, 0,
                                "reading" if readable else (refused[0] if refused else "unrecognized"),
                                refused[1] if refused else (None if readable or not invoices else "no invoice dates"),
                                _now()))
            file_id = cur.lastrowid
        conn.commit()
        if refused:
            return {"status": refused[0], "detail": refused[1], "format": None, "file_id": file_id, "invoices": 0,
                    "parsed": 0, "lines": 0, "undated": 0, "unbalanced": []}
        if not readable:
            return {"status": "unrecognized", "format": fmt, "file_id": file_id, "invoices": 0,
                    "parsed": len(invoices), "lines": 0, "undated": undated, "unbalanced": []}
        res = _write(conn, rid, fmt, invoices, file_id, commit_every=COMMIT_EVERY)
        status = _status(res, len(invoices))
        detail = {k: res[k] for k in ("kept", "conflicts", "near_duplicates", "skipped_locations", "routed") if res[k]}
        if undated:
            detail["undated"] = undated
        conn.execute("UPDATE fintech_files SET status=?, invoices=?, lines=?, detail=? WHERE id=?",
                     (status, res["invoices"], res["lines"], json.dumps(detail, default=str)[:4000] if detail else None,
                      file_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    res.update(status=status, format=fmt, file_id=file_id, parsed=len(invoices), undated=undated)
    res["routed"] = {str(k): v for k, v in res["routed"].items()}
    return res


def _parse_kept(fmt, content):
    content = _content(content)            # inline, or the object store's copy (R5)
    return parse_export(content) if fmt == "export" else parse_printout(content)


def reread_file(restaurant_id, file_id, db_path=None) -> dict:
    """Read a stored file again with today's reader (a column it did not know
    yet, the deposit split, the unit inference): its invoices are written as
    a fresh read would write them, never over a version that outranks them.
    An export's kept text is cut to today's column allow-list too."""
    conn = get_conn(db_path)
    try:
        f = conn.execute("SELECT format, content, status FROM fintech_files WHERE id=? AND restaurant_id=?",
                         (int(file_id), int(restaurant_id))).fetchone()
        if not f or not f["content"] or f["status"] == "purged":
            return {"ok": False, "error": "no such file"}
        content = _content(f["content"])
        if f["format"] == "export":
            trimmed = _export_kept(content)
            if trimmed != content:
                _replace_content(conn, int(restaurant_id), int(file_id), f["content"], trimmed)
                content = trimmed
        invoices = _parse_kept(f["format"], content)
        dated = [i for i in invoices if i.get("invoice_date")]
        res = _write(conn, int(restaurant_id), f["format"], dated, int(file_id)) if dated else None
        conn.commit()
    finally:
        conn.close()
    if res is None:
        return {"ok": True, "invoices": 0, "parsed": len(invoices), "undated": len(invoices)}
    return {"ok": True, "invoices": res["invoices"], "parsed": len(invoices), "undated": len(invoices) - len(dated),
            "kept": res["kept"], "conflicts": res["conflicts"], "unbalanced": res["unbalanced"]}


def purge_file(restaurant_id, file_id, db_path=None) -> dict:
    """Take a file's invoices out (a forged or wrong file; the admin action
    is admin_routes'). An invoice another file also carried goes back to the
    best remaining version; one only this file carried is deleted with its
    lines. The file row stays, marked 'purged', so the same bytes sent again
    are still a duplicate."""
    rid, fid = int(restaurant_id), int(file_id)
    conn = get_conn(db_path)
    restored, deleted = 0, 0
    try:
        f = conn.execute("SELECT id, status FROM fintech_files WHERE id=? AND restaurant_id=?", (fid, rid)).fetchone()
        if not f:
            return {"ok": False, "error": "no such file"}
        conn.execute("DELETE FROM fintech_invoice_versions WHERE file_id=? AND state<>'current'", (fid,))
        for inv in conn.execute(f"SELECT {_INV_COLS} FROM fintech_invoices WHERE file_id=?", (fid,)).fetchall():
            others = conn.execute(
                "SELECT v.file_id, v.source_format, v.balanced, f.content FROM fintech_invoice_versions v "
                "JOIN fintech_files f ON f.id = v.file_id WHERE v.invoice_id=? AND v.file_id<>? "
                "AND f.status<>'purged' AND f.content IS NOT NULL ORDER BY v.file_id DESC",
                (inv["id"], fid)).fetchall()
            others = sorted(others, key=lambda o: (_balance_rank(o["balanced"]), o["source_format"] == "export",
                                                   int(o["file_id"])), reverse=True)
            conn.execute("DELETE FROM fintech_invoice_versions WHERE invoice_id=? AND file_id=?", (inv["id"], fid))
            back = None
            dkey = inv["distributor_key"] or distributor_key(inv["distributor"])
            for o in others:
                back = next((p for p in _parse_kept(o["source_format"], o["content"])
                             if _same_document(p, inv)
                             and (distributor_key(p["distributor"]) == dkey or
                                  (p.get("total") is not None and inv["total"] is not None
                                   and abs(p["total"] - float(inv["total"])) < 0.005))), None)
                if back is not None:
                    _store_invoice(conn, inv["restaurant_id"], back, o["source_format"], int(o["file_id"]), force=True)
                    restored += 1
                    break
            if back is None:
                conn.execute("DELETE FROM fintech_invoice_lines WHERE invoice_id=?", (inv["id"],))
                conn.execute("DELETE FROM fintech_invoice_versions WHERE invoice_id=?", (inv["id"],))
                conn.execute("DELETE FROM fintech_invoices WHERE id=?", (inv["id"],))
                deleted += 1
        conn.execute("DELETE FROM fintech_invoice_versions WHERE file_id=?", (fid,))
        conn.execute("UPDATE fintech_files SET status='purged', invoices=0, lines=0 WHERE id=?", (fid,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "restored": restored, "deleted": deleted}


def delete_orphan_lines(dry_run=True, db_path=None) -> int:
    """Invoice lines whose invoice is gone (a restaurant deleted before lines
    carried restaurant_id). Counted, and deleted unless dry_run."""
    conn = get_conn(db_path)
    try:
        where = "NOT EXISTS (SELECT 1 FROM fintech_invoices i WHERE i.id = fintech_invoice_lines.invoice_id)"
        n = conn.execute(f"SELECT COUNT(*) FROM fintech_invoice_lines WHERE {where}").fetchone()[0]
        if n and not dry_run:
            conn.execute(f"DELETE FROM fintech_invoice_lines WHERE {where}")
            conn.commit()
    finally:
        conn.close()
    return int(n)


def merge_duplicates(restaurant_id, dry_run=True, db_path=None) -> list:
    """Invoices stored twice before the canonical keys (the same number and
    distributor by key): the best version stays (balanced, then export, then
    newer file), the other's lines and row go. Returns what was (or would
    be) merged."""
    conn = get_conn(db_path)
    try:
        out = _merge_duplicates(conn, int(restaurant_id), dry_run)
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return out


def _merge_duplicates(conn, rid, dry_run) -> list:
    out, groups = [], {}
    for r in conn.execute(f"SELECT {_INV_COLS} FROM fintech_invoices WHERE restaurant_id=? ORDER BY id",
                          (rid,)).fetchall():
        groups.setdefault((r["vendor_id"] or r["distributor_key"] or distributor_key(r["distributor"]),
                           r["number_key"] or invoice_key(r["invoice_number"])), []).append(r)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda r: (_balance_rank(r["balanced"]), r["source_format"] == "export",
                                           int(r["file_id"] or 0)), reverse=True)
        keep, drop = rows[0], rows[1:]
        out.append({"kept": keep["id"], "dropped": [d["id"] for d in drop],
                    "distributor": keep["distributor"], "invoice_number": keep["invoice_number"]})
        if dry_run:
            continue
        _current_version(conn, keep)
        for d in drop:
            if d["file_id"] is not None and d["file_id"] != keep["file_id"]:
                _version(conn, rid, keep["id"], int(d["file_id"]), d["source_format"], d["total"],
                         d["lines_total"], d["balanced"], None, "kept")
            conn.execute("DELETE FROM fintech_invoice_lines WHERE invoice_id=?", (d["id"],))
            conn.execute("DELETE FROM fintech_invoice_versions WHERE invoice_id=?", (d["id"],))
            conn.execute("DELETE FROM fintech_invoices WHERE id=?", (d["id"],))
    return out


def _ledger(conn, rid) -> dict:
    """What the restaurant's invoices hold, for a before/after."""
    inv = conn.execute("SELECT COUNT(*) AS n, SUM(CASE WHEN balanced=1 THEN 1 ELSE 0 END) AS balanced "
                       "FROM fintech_invoices WHERE restaurant_id=?", (rid,)).fetchone()
    kinds = {r["kind"]: {"lines": int(r["n"]), "cost": round(float(r["cost"] or 0), 2),
                         "deposit": round(float(r["deposit"] or 0), 2)}
             for r in conn.execute("SELECT l.kind, COUNT(*) AS n, SUM(l.cost) AS cost, SUM(l.deposit) AS deposit "
                                   "FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id = l.invoice_id "
                                   "WHERE i.restaurant_id=? GROUP BY l.kind", (rid,)).fetchall()}
    inferred = conn.execute("SELECT COUNT(*) FROM fintech_invoice_lines WHERE restaurant_id=? AND uom_inferred=1",
                            (rid,)).fetchone()[0]
    return {"invoices": int(inv["n"] or 0), "balanced": int(inv["balanced"] or 0), "kinds": kinds,
            "units_inferred": int(inferred or 0)}


def reread_all(restaurant_id, apply=False, db_path=None) -> dict:
    """Re-read every stored file of a restaurant with today's reader, in the
    order they arrived (bar fix round 10/15/26: the export's keg deposits
    and empties, printout units from the export history, item codes without
    padding, canonical distributors, the column allow-list on kept text).
    Also merges invoices stored twice before the canonical keys and teaches
    the store map from the invoices on file when they name one store.
    Idempotent. Without apply it runs in one transaction and rolls back:
    the result says what would change."""
    rid = int(restaurant_id)
    conn = get_conn(db_path)
    try:
        before = _ledger(conn, rid)
        merged = _merge_duplicates(conn, rid, dry_run=False)
        locs = {location_key(r["location_id"]) for r in conn.execute(
            "SELECT DISTINCT location_id FROM fintech_invoices WHERE restaurant_id=?", (rid,)).fetchall()} - {None}
        has_map = conn.execute("SELECT 1 FROM fintech_locations WHERE restaurant_id=? LIMIT 1", (rid,)).fetchone()
        seeded = None
        if not has_map and len(locs) == 1:
            seeded = next(iter(locs))
            conn.execute("INSERT INTO fintech_locations (restaurant_id, location_key, target_restaurant_id, set_by, "
                         "created_at) VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, location_key) DO NOTHING",
                         (rid, seeded, rid, "bar_reread_fintech", _now()))
        files = []
        for f in conn.execute("SELECT id, format, content, status FROM fintech_files WHERE restaurant_id=? "
                              "AND format IS NOT NULL AND content IS NOT NULL AND status<>'purged' ORDER BY id",
                              (rid,)).fetchall():
            content, trimmed = _content(f["content"]), False
            if f["format"] == "export":
                cut = _export_kept(content)
                if cut != content:
                    if apply:          # a dry run never writes to the object store
                        _replace_content(conn, rid, int(f["id"]), f["content"], cut)
                    content, trimmed = cut, True
            invoices = [i for i in _parse_kept(f["format"], content) if i.get("invoice_date")]
            res = _write(conn, rid, f["format"], invoices, int(f["id"]))
            files.append({"file_id": f["id"], "format": f["format"], "written": res["invoices"],
                          "kept": res["kept"], "conflicts": res["conflicts"],
                          "skipped_locations": res["skipped_locations"], "trimmed": trimmed})
        after = _ledger(conn, rid)
        if apply:
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"restaurant_id": rid, "applied": bool(apply), "before": before, "after": after, "merged": merged,
            "store_map_seeded": seeded, "stores_on_file": sorted(locs), "files": files}


