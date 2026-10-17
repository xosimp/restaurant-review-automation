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
(`bo_vendor_categories`: the owner's choice, else a suggestion — the
Fintech distributor it exactly is takes the pour category it sells most
there, a vendor whose own entries book to one category takes that, a name
that says food takes food, a vendor only NAMED like a Fintech distributor
takes its mix as a guess to confirm — and 'unknown' otherwise, left out and
named; vendor_categories).

Read by:
- Food Cost — `food_bought_vs_sold` (`purchases_view` for the route): food
  purchases against the POS's food sales (`food_sales_detail`, the one
  definition: the restaurant's DSR food categories, after check discounts)
  by month and over the last four weeks, purchase-based (no count);
- the Bar — `outside_fintech_lines`: alcohol a restaurant paid for through
  Back Office that no Fintech invoice carries (a wine house paid by check, a
  liquor-store run), as bar purchase lines (kind 'backoffice'). Back Office
  records many Fintech invoices a second time when it pays them (a
  check-mode entry weeks later, the same amount). Matching is per Back
  Office DOCUMENT (an itemized invoice split over several accounts is one),
  and only against the Fintech invoices of the distributor the vendor is
  (`_fintech_link`: its key, never one shared word): by number, else one
  invoice of its amount, else a few (≤ SUBSET_MAX) that add up to it, dated
  MATCH_DAYS_BEFORE before to MATCH_DAYS_AFTER after it was paid. An entry
  from a Fintech distributor that matches none is never silently added: it
  is held as `to_confirm` ("paid in Back Office, no Fintech invoice") until
  the owner says count it or set it aside (`bo_entry_decisions`; fix round
  10/16/26, ING-01/02/04, BAR-CALC-08, LEAD-01).

A printout is authoritative for its vendors over its dates (fix round
10/16/26, FC-CALC-04 / ING-12): an entry it no longer shows there is
removed and recorded in `bo_invoice_removals`, and after the save each
vendor's stored rows over those dates must add up to the total the page
printed beside it. A vendor whose rows don't add up on the page itself is
held, never saved (ING-13).
"""
import hashlib
import itertools
import json
import logging
import re
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from fintech_invoices import _STATES, distributor_key, invoice_key

log = logging.getLogger(__name__)

CATEGORIES = ("food", "na_beverage", "beer", "wine", "liquor", "alcohol_mixed", "bar_consumables", "discount",
              "other_purchase", "inventory_adjustment", "not_cogs", "unknown")
# A store that sells every kind of alcohol on one receipt (Binny's, a
# liquor-store run; owner, 10/16/26) is "alcohol_mixed": counted in the
# Bar's total, never in beer, wine or liquor alone (bar.pour_cost._total).
POUR = ("beer", "wine", "liquor")
# A check-mode entry Back Office records when it pays an invoice sits up to
# this many days after the Fintech invoice it pays (EJ's: a catch-up batch on
# 8/5/26 paid invoices from late May and June).
MATCH_DAYS_AFTER = 90
MATCH_DAYS_BEFORE = 10
# One Back Office payment for several Fintech invoices (a catch-up batch):
# up to SUBSET_MAX of the distributor's unused invoices, chosen from the
# SUBSET_POOL nearest the payment in time (ING-04) — a bounded search.
SUBSET_MAX = 6
SUBSET_POOL = 12
BAR_CATEGORIES = POUR + ("alcohol_mixed", "bar_consumables")
# What the owner can say about a held entry (`decide_entry`).
DECISIONS = ("count", "not_purchase")
WINDOW_DAYS = 28
MONTH_MIN_NIGHTS = 14
# The headline window's pct needs sales on this share of the days the
# restaurant trades, and at least WINDOW_MIN_NIGHTS nights (FC-CALC-03).
WINDOW_MIN_COVERAGE = 0.8
WINDOW_MIN_NIGHTS = 7
# A night whose positive sales the DSR category map leaves Unmapped above
# this share is not fully covered (FC-CALC-20; bar.sales.UNMAPPED_SAY_PCT).
UNMAPPED_NIGHT_PCT = 2.0
FEES_NAME = "Fees & tax"


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _tx(conn=None, db_path=None):
    """The caller's connection (its transaction, its commit), or one of our
    own committed on success and rolled back on failure."""
    if conn is not None:
        yield conn
        return
    c = get_conn(db_path)
    try:
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()


def _capture(e, job, restaurant_id=None, db_path=None):
    """A degraded path says so before degrading (SCALE-11)."""
    log.warning("backoffice_ap %s rid=%s: %s", job, restaurant_id, e)
    try:
        import ops
        ops.capture(e, job=job, restaurant_id=restaurant_id, db_path=db_path)
    except Exception:
        pass


def _vk(vendor) -> str:
    return distributor_key(vendor) or str(vendor or "").lower()


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
        # Inventory counts read from Back Office's submitted inventories
        # (backoffice_counts): one row per inventory GL account per period,
        # and the count sheets' item rows behind them.
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_inventory_counts (
            restaurant_id INTEGER NOT NULL,
            period_end TEXT NOT NULL,
            period_start TEXT,
            gl TEXT NOT NULL,
            gl_name TEXT,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, period_end, gl))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_inventory_count_items (
            restaurant_id INTEGER NOT NULL,
            period_end TEXT NOT NULL,
            location TEXT NOT NULL,
            line_no INTEGER NOT NULL,
            item TEXT NOT NULL,
            gl_name TEXT,
            unit TEXT,
            qty REAL,
            total REAL,
            PRIMARY KEY (restaurant_id, period_end, location, line_no))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_vendor_categories (
            restaurant_id INTEGER NOT NULL,
            vendor_key TEXT NOT NULL,
            category TEXT NOT NULL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, vendor_key))""")
        # An entry a newer printout no longer shows over its dates (voided,
        # corrected, re-keyed): removed from bo_invoices and kept here, so a
        # removal is never silent (fix round 10/16/26, ING-12).
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_invoice_removals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            vendor TEXT,
            vendor_key TEXT NOT NULL,
            entry_key TEXT NOT NULL,
            invoice_number TEXT,
            entry_kind TEXT,
            invoice_date TEXT,
            amount REAL,
            gl TEXT,
            file_id INTEGER,
            removed_by_file INTEGER,
            why TEXT,
            removed_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bo_invoice_removals ON bo_invoice_removals(restaurant_id, removed_at)")
        # The owner's word on a Back Office alcohol entry from a Fintech
        # distributor that matches no Fintech invoice: count it, or set it
        # aside (LEAD-01). No row = still to confirm.
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_entry_decisions (
            restaurant_id INTEGER NOT NULL,
            vendor_key TEXT NOT NULL,
            entry_key TEXT NOT NULL,
            decision TEXT NOT NULL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, vendor_key, entry_key))""")
        conn.commit()
        _migrate_entry_keys(conn)
        _refresh_gl_categories(conn)
    finally:
        conn.close()


def _migrate_entry_keys(conn):
    """Once per stored row, at boot: an invoice keyed by its number alone
    ('inv|<number>', before 10/16/26) takes its year and sign too
    (_entry_keys), so a credit memo or a later year's invoice under the same
    number is never written over it (FC-CALC-04). Idempotent."""
    try:
        rows = conn.execute("SELECT id, restaurant_id, vendor_key, entry_key, invoice_number, invoice_date, amount "
                            "FROM bo_invoices WHERE entry_kind='invoice'").fetchall()
        for r in rows:
            if str(r["entry_key"]).count("|") != 1:
                continue
            new = _invoice_entry_key(r["invoice_number"], r["invoice_date"], r["amount"])
            if conn.execute("SELECT 1 FROM bo_invoices WHERE restaurant_id=? AND vendor_key=? AND entry_key=?",
                            (r["restaurant_id"], r["vendor_key"], new)).fetchone():
                continue
            conn.execute("UPDATE bo_invoices SET entry_key=? WHERE id=?", (new, r["id"]))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _capture(e, "backoffice_key_migration")


def _refresh_gl_categories(conn):
    """Each stored account's category as gl_category reads it now (a food
    purchase discount books to food since 10/16/26, FC-CALC-17)."""
    try:
        for r in conn.execute("SELECT restaurant_id, gl, name, subtype, category FROM bo_gl_accounts").fetchall():
            cat = gl_category(r["subtype"], r["name"])
            if cat != r["category"]:
                conn.execute("UPDATE bo_gl_accounts SET category=? WHERE restaurant_id=? AND gl=?",
                             (cat, r["restaurant_id"], r["gl"]))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _capture(e, "backoffice_gl_refresh")


# ── the Chart of Accounts ─────────────────────────────────────────────────

_FOOD_WORDS = r"PRODUCE|DAIRY|MEAT|SEAFOOD|DRY GOOD|POULTRY|PORK|BREAD|FOOD|BAKERY|PROTEIN|GROCER"
_DISCOUNT_WORDS = r"DISCOUNT|REBATE"
_BAR_WORDS = r"\bBAR\b|BEER|BREWERY|KEG|WINE|LIQUOR|SPIRIT|MIXER"
_GL_WORDS = (
    ("inventory_adjustment", None),       # by subtype, below
    ("discount", _DISCOUNT_WORDS),
    ("bar_consumables", r"BAR CONSUMABLE|BAR PREP|MIXER"),
    ("na_beverage", r"\bNA BEV|NON[- ]?ALC|COFFEE|SODA"),
    ("beer", r"BEER|BREWERY|DRAFT BEER|KEG"),
    ("wine", r"WINE"),
    ("liquor", r"LIQUOR|SPIRIT"),
    ("food", _FOOD_WORDS),
)


def gl_category(subtype, name) -> str:
    """The Cavnar AI category one Chart of Accounts row books. A purchase
    account by its name; a 'Purchase Inventory' account is a count's
    adjustment, never a purchase; anything that is not a purchase is not
    cost of goods. A purchase account whose name says none of the above
    (paper, to-go, retail) is other_purchase. A discount or rebate account
    whose name says food ('4099 FOOD PURCHASE DISCOUNT') books to food, so
    its entries (credits, as Back Office prints them) net against food
    purchases (FC-CALC-17); a bar one, or one that names no side, stays
    'discount'."""
    st = str(subtype or "").strip().lower()
    nm = str(name or "").upper()
    if st == "purchase inventory":
        return "inventory_adjustment"
    if st != "purchase":
        return "not_cogs"
    if re.search(_DISCOUNT_WORDS, nm) and re.search(_FOOD_WORDS, nm) and not re.search(_BAR_WORDS, nm):
        return "food"
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


def save_coa(restaurant_id, accounts, db_path=None, conn=None) -> int:
    with _tx(conn, db_path) as c:
        for a in accounts:
            c.execute("INSERT INTO bo_gl_accounts (restaurant_id, gl, name, subtype, category, updated_at) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, gl) DO UPDATE SET name=excluded.name, "
                      "subtype=excluded.subtype, category=excluded.category, updated_at=excluded.updated_at",
                      (int(restaurant_id), a["gl"], a["name"], a["subtype"], a["category"], _now()))
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
# A vendor's total prints as $1,400.00, -$677.37 or ($50.00) (ING-13).
_HEAD_RE = re.compile(r"^%s?(.+?)\s+(\()?(-)?\$([\d,]+\.\d\d)\)?\s*$" % _BULLET)
_ROW_RE = re.compile(r"^\s*(CM Entry|.+?)\s+(\[I\]\s+)?(\d\d/\d\d/\d{4})\s+(\(?-?\$[\d,]+\.\d\d\)?)(.*)$")


def looks_like_invoices_by_vendor(text) -> bool:
    return "Invoices By Vendor" in (text or "") and "Invoice #" in (text or "")


def parse_invoices_by_vendor(text) -> dict:
    """{"vendors": [{vendor, total, rows: [{entry_kind, invoice_number,
    itemized, date (ISO), amount, gl}]}], "unparsed": n, "orphans": n,
    "mismatched": [vendor]} from the printout's text. A vendor whose rows do
    not add up to the total the page printed beside it is named in
    `mismatched` (a row the reader missed) — its rows are returned, and
    read_file holds that vendor rather than saving it. A row line is never
    read as a vendor header, and a vendor header the reader can't read
    starts a new vendor all the same: its rows are `orphans` (counted,
    never filed under the vendor before it — ING-13)."""
    t = str(text or "").replace("\xa0", " ").replace(_CHECK, " [I] ")
    vendors, cur, unparsed, orphans, pending = [], None, 0, 0, False
    for line in t.split("\n"):
        if not line.strip() or "Invoices By Vendor" in line or line.startswith("Page ") or "Invoice # Itemized" in line:
            continue
        if line.strip() == _BULLET:
            pending, cur = True, None
            continue
        row = _ROW_RE.match(line)
        if line.startswith(_BULLET) or pending:
            h = None if row else _HEAD_RE.match(line)
            if h:
                pending = False
                cur = {"vendor": " ".join(h.group(1).split()),
                       "total": round(float(h.group(4).replace(",", "")) * (-1 if (h.group(2) or h.group(3)) else 1), 2),
                       "rows": []}
                vendors.append(cur)
                continue
            cur = None                      # a new vendor whose header can't be read
            if not row:
                unparsed += 1
                continue
            pending = False
        m = row
        if m and cur is None:
            unparsed += 1
            orphans += 1
            continue
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
    return {"vendors": vendors, "unparsed": unparsed, "orphans": orphans, "mismatched": mismatched}


def _invoice_entry_key(number, day, amount) -> str:
    """An invoice's key: its number without padding, its year, and 'cr' when
    it is a credit — so a credit memo under its invoice's number, or the
    same number a year later, is its own row (FC-CALC-04 / ING-12). The
    amount itself is not in it: a corrected amount replaces the row."""
    try:
        neg = float(amount or 0) < 0
    except (TypeError, ValueError):
        neg = False
    return "inv|%s|%s%s" % (invoice_key(number), str(day or "")[:4], "|cr" if neg else "")


def _entry_keys(rows) -> list:
    """One key per row: an invoice by its number, year and sign
    (_invoice_entry_key), numbered from the second when a vendor shows the
    same one twice; a check-mode entry by its date and amount, numbered when
    the same vendor has two alike that day."""
    seen, out = Counter(), []
    for r in rows:
        if r["entry_kind"] == "invoice":
            base = _invoice_entry_key(r["invoice_number"], r["date"], r["amount"])
            seen[base] += 1
            out.append(base if seen[base] == 1 else f"{base}|{seen[base]}")
        else:
            base = f"cm|{r['date']}|{float(r['amount']):.2f}"
            seen[base] += 1
            out.append(f"{base}|{seen[base]}")
    return out


def _by_vendor_key(parsed) -> list:
    """[(vendor_key, vendor, printed total, rows)] — two spellings of one
    vendor on a printout are one vendor (their keys share one ordinal run)."""
    out = OrderedDict()
    for v in parsed.get("vendors") or []:
        vk = _vk(v["vendor"])
        if vk not in out:
            out[vk] = [v["vendor"], 0.0, []]
        out[vk][1] += float(v.get("total") or 0)
        out[vk][2].extend(v["rows"])
    return [(vk, name, round(total, 2), rows) for vk, (name, total, rows) in out.items()]


def _same_invoice(c, restaurant_id, number, day, amount, not_vendor_key=None):
    """The stored invoice with this number, date and amount under another
    vendor spelling ('GFS' / 'Gordon Food Service'), or None (FC-CALC-25)."""
    ik = invoice_key(number)
    for r in c.execute("SELECT id, vendor, vendor_key, entry_key, invoice_number, amount FROM bo_invoices "
                       "WHERE restaurant_id=? AND invoice_date=? AND entry_kind='invoice'",
                       (int(restaurant_id), str(day))).fetchall():
        if (r["vendor_key"] != not_vendor_key and invoice_key(r["invoice_number"]) == ik
                and abs(float(r["amount"] or 0) - float(amount or 0)) < 0.005):
            return dict(r)
    return None


def _unlisted(c, restaurant_id, vk, keep, span) -> list:
    """The stored entries of one vendor over a printout's dates that the
    printout does not show."""
    rows = c.execute("SELECT id, vendor, vendor_key, entry_key, invoice_number, entry_kind, invoice_date, amount, gl, "
                     "file_id FROM bo_invoices WHERE restaurant_id=? AND vendor_key=? AND invoice_date >= ? "
                     "AND invoice_date <= ?", (int(restaurant_id), vk, span[0], span[1])).fetchall()
    return [dict(r) for r in rows if r["entry_key"] not in keep]


def preview_printout(restaurant_id, parsed, db_path=None) -> dict:
    """What saving this printout would do, writing nothing (the import
    script's dry run): {held, would_remove: [{vendor, date, amount,
    invoice_number}]}."""
    held = set(parsed.get("mismatched") or [])
    keep = {"vendors": [v for v in parsed.get("vendors") or [] if v["vendor"] not in held]}
    dates = [r["date"] for v in keep["vendors"] for r in v["rows"]]
    gone = []
    if dates:
        span = (min(dates), max(dates))
        conn = get_conn(db_path)
        try:
            for vk, _name, _total, rows in _by_vendor_key(keep):
                gone += _unlisted(conn, restaurant_id, vk, set(_entry_keys(rows)), span)
        finally:
            conn.close()
    return {"held": sorted(held), "would_remove": [{"vendor": g["vendor"], "date": g["invoice_date"],
                                                    "amount": g["amount"], "invoice_number": g["invoice_number"]}
                                                   for g in gone]}


def save_invoices(restaurant_id, parsed, file_id=None, db_path=None, conn=None, authoritative=False) -> dict:
    """Upsert every row; returns {written, vendors, removed, unbalanced}. An
    entry seen again (a later printout overlapping this one) is updated in
    place, never doubled.

    `authoritative` (an Invoices By Vendor printout, read_file): the printout
    is the record for its vendors over its dates (its first to its last
    entry) — an entry on file there that it no longer shows (voided, a
    corrected amount, an old key) is removed into `bo_invoice_removals`; an
    invoice it shows that is stored under another spelling of its vendor
    (same number, date and amount) takes the printout's spelling instead of
    being stored twice; and each vendor's stored rows over those dates must
    then add up to the total the printout printed (else `unbalanced`)."""
    rid = int(restaurant_id)
    n = removed = 0
    unbalanced = []
    groups = _by_vendor_key(parsed)
    dates = [r["date"] for _vk_, _n, _t, rows in groups for r in rows]
    span = (min(dates), max(dates)) if dates else None
    with _tx(conn, db_path) as c:
        for vk, vendor, total, rows in groups:
            keys = _entry_keys(rows)
            for r, key in zip(rows, keys):
                if authoritative and r["entry_kind"] == "invoice" and not c.execute(
                        "SELECT 1 FROM bo_invoices WHERE restaurant_id=? AND vendor_key=? AND entry_key=?",
                        (rid, vk, key)).fetchone():
                    hit = _same_invoice(c, rid, r["invoice_number"], r["date"], r["amount"], not_vendor_key=vk)
                    if hit:
                        c.execute("UPDATE bo_invoices SET vendor=?, vendor_key=?, entry_key=? WHERE id=?",
                                  (vendor, vk, key, hit["id"]))
                        ik = invoice_key(r["invoice_number"])
                        if not c.execute("SELECT 1 FROM bo_invoice_lines WHERE restaurant_id=? AND vendor_key=? "
                                         "AND invoice_key=?", (rid, vk, ik)).fetchone():
                            c.execute("UPDATE bo_invoice_lines SET vendor_key=? WHERE restaurant_id=? AND "
                                      "vendor_key=? AND invoice_key=?", (vk, rid, hit["vendor_key"], ik))
                c.execute(
                    "INSERT INTO bo_invoices (restaurant_id, vendor, vendor_key, entry_key, invoice_number, entry_kind, "
                    "itemized, invoice_date, amount, gl, file_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(restaurant_id, vendor_key, entry_key) DO UPDATE SET vendor=excluded.vendor, "
                    "itemized=excluded.itemized, invoice_date=excluded.invoice_date, amount=excluded.amount, "
                    "gl=excluded.gl, file_id=excluded.file_id, updated_at=excluded.updated_at",
                    (rid, vendor, vk, key, r["invoice_number"], r["entry_kind"],
                     1 if r["itemized"] else 0, r["date"], r["amount"], r["gl"], file_id, _now(), _now()))
                n += 1
            if not (authoritative and span):
                continue
            for g in _unlisted(c, rid, vk, set(keys), span):
                c.execute("INSERT INTO bo_invoice_removals (restaurant_id, vendor, vendor_key, entry_key, "
                          "invoice_number, entry_kind, invoice_date, amount, gl, file_id, removed_by_file, why, "
                          "removed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rid, g["vendor"], g["vendor_key"], g["entry_key"], g["invoice_number"], g["entry_kind"],
                           g["invoice_date"], g["amount"], g["gl"], g["file_id"], file_id,
                           "not on a later printout over its dates", _now()))
                c.execute("DELETE FROM bo_invoices WHERE id=?", (g["id"],))
                removed += 1
            got = c.execute("SELECT SUM(amount) FROM bo_invoices WHERE restaurant_id=? AND vendor_key=? AND "
                            "invoice_date >= ? AND invoice_date <= ?", (rid, vk, span[0], span[1])).fetchone()[0]
            if abs(float(got or 0) - total) >= 0.02:
                unbalanced.append(vendor)
    return {"written": n, "vendors": len(groups), "removed": removed, "unbalanced": unbalanced}


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


def save_invoice_page(restaurant_id, inv, file_id=None, db_path=None, conn=None) -> dict:
    """Store a parsed invoice page: its lines (save_lines), and the invoice
    itself in bo_invoices under the same entry key an Invoices By Vendor
    printout gives it — so a page read before the printout counts once, and
    the printout's later row updates it in place. One transaction. An
    invoice already on file with the same number, date and amount under
    another spelling of the vendor ('GFS' / 'Gordon Food Service') is that
    invoice: the page is stored under its spelling, never twice
    (FC-CALC-25)."""
    amount = inv["total"] if inv["total"] is not None else round(sum(l["ext_amount"] for l in inv["lines"]), 2)
    with _tx(conn, db_path) as c:
        vendor = inv["vendor"]
        hit = _same_invoice(c, restaurant_id, inv["invoice_number"], inv["date"], amount, not_vendor_key=_vk(vendor))
        if hit:
            vendor = hit["vendor"]
        save_invoices(restaurant_id, {"vendors": [{"vendor": vendor, "total": amount, "rows": [
            {"entry_kind": "invoice", "invoice_number": inv["invoice_number"], "itemized": True, "date": inv["date"],
             "amount": amount, "gl": None}]}]}, file_id=file_id, db_path=db_path, conn=c)
        n = save_lines(restaurant_id, vendor, inv["invoice_number"], inv["lines"], invoice_date=inv["date"],
                       file_id=file_id, db_path=db_path, conn=c)
    if conn is None:
        _invalidate(restaurant_id)
    return {"written": n, "vendor": vendor}


def save_invoice_book(restaurant_id, book, vendors=None, db_path=None) -> dict:
    """Store invoices read from Back Office's own Edit Invoice pages (JSON:
    {"invoices": [{vendor, number, date (ISO), total ("$1,234.56"), items:
    [{sku, name, gl ("4030 MEAT"), unit, qty, amt, ext}]}]}) — each exactly as
    a printed invoice page would be (save_invoice_page): its lines, and the
    invoice under the printout's own key, so nothing counts twice. `vendors`
    limits it to those names (case-blind). {invoices, lines, skipped}."""
    want = {str(v).strip().upper() for v in vendors} if vendors else None
    n_inv = n_lines = 0
    skipped = []
    for inv in (book or {}).get("invoices") or []:
        vendor = " ".join(str(inv.get("vendor") or "").split())
        if not vendor or not inv.get("number") or not inv.get("date"):
            skipped.append({"why": "no vendor, number or date", "number": inv.get("number")})
            continue
        if want is not None and vendor.upper() not in want:
            continue
        lines = []
        for it in inv.get("items") or []:
            ext = _money(it.get("ext")) if it.get("ext") not in (None, "") else None
            if ext is None:
                continue
            gl = re.match(r"\s*(\d+)", str(it.get("gl") or ""))
            lines.append({"item_code": (str(it.get("sku") or "").strip() or None), "description": it.get("name"),
                          "gl": gl.group(1) if gl else None, "purchase_unit": it.get("unit") or None,
                          "qty": _money(it.get("qty")) if it.get("qty") not in (None, "") else None,
                          "unit_amount": _money(it.get("amt")) if it.get("amt") not in (None, "") else None,
                          "ext_amount": round(ext, 2)})
        if not lines:
            skipped.append({"why": "no line items", "vendor": vendor, "number": inv.get("number")})
            continue
        total = round(_money(inv["total"]), 2) if inv.get("total") else None
        page = {"vendor": vendor, "invoice_number": str(inv["number"]).strip(), "date": str(inv["date"])[:10],
                "total": total, "lines": lines,
                "adds_up": total is not None and abs(sum(l["ext_amount"] for l in lines) - total) < 0.02}
        save_invoice_page(restaurant_id, page, db_path=db_path)
        n_inv += 1
        n_lines += len(lines)
    return {"invoices": n_inv, "lines": n_lines, "skipped": skipped}


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
    {"status": stored | conflict | duplicate | unrecognized, "kind",
    "detail", ...}.

    The file's `bo_files` row and everything read from it are written in
    ONE transaction (ING-10 / SCALE-03): a write that fails leaves no trace,
    so the intake job's retry reads the file again instead of finding it
    "already read" with nothing stored. A printout vendor whose rows don't
    add up to the total printed beside it is held, not saved, and the file
    reads 'conflict' (a look needed) with that vendor named (ING-13)."""
    content = bytes(content or b"")
    sha = hashlib.sha256(content).hexdigest()
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM bo_files WHERE restaurant_id=? AND sha256=?",
                        (int(restaurant_id), sha)).fetchone():
            return {"status": "duplicate", "detail": "this file was already read"}
    finally:
        conn.close()
    kind, detail, out, status = None, None, {}, "stored"
    accounts = parsed = inv = None
    try:
        head = content[:400].decode("utf-8-sig", "replace")
        if "GLAccountSubTypeName" in head and "MainAccountNumber" in head:
            accounts = parse_coa(content)
            kind = "coa"
            cats = Counter(a["category"] for a in accounts)
            detail = f"{len(accounts)} accounts ({', '.join(f'{c} {k}' for c, k in sorted(cats.items()))})"
            out = {"accounts": len(accounts)}
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
            held = list(parsed["mismatched"])
            out = {"vendors": len(parsed["vendors"]), "unparsed": parsed["unparsed"],
                   "orphans": parsed.get("orphans", 0), "mismatched": held, "held": held}
            detail = f"{len(parsed['vendors'])} vendors, {sum(len(v['rows']) for v in parsed['vendors'])} entries"
            if held:
                status = "conflict"
                detail += (f"; held, not saved: {', '.join(held)} (its rows don't add up to the total the printout "
                           "shows — a row was missed)")
            if parsed.get("orphans"):
                status = "conflict"
                detail += f"; {parsed['orphans']} rows under a vendor name that couldn't be read, not saved"
            parsed = {"vendors": [v for v in parsed["vendors"] if v["vendor"] not in held]}
    except ValueError as e:
        return {"status": "unrecognized", "detail": str(e)}
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO bo_files (restaurant_id, sha256, filename, kind, intake_id, status, detail, "
                           "created_at) VALUES (?,?,?,?,?,?,?,?)",
                           (int(restaurant_id), sha, str(filename or "")[:200], kind, intake_id, "reading", detail,
                            _now()))
        file_id = cur.lastrowid
        if kind == "coa":
            save_coa(restaurant_id, accounts, db_path, conn=conn)
        elif kind == "invoices_by_vendor":
            res = save_invoices(restaurant_id, parsed, file_id=file_id, db_path=db_path, conn=conn,
                                authoritative=True)
            out.update(res)
            out["vendors"] = len(parsed["vendors"]) + len(out["held"])
            if res["removed"]:
                detail += f"; {res['removed']} entries no longer on Back Office over these dates removed (kept on record)"
            if res["unbalanced"]:
                status = "conflict"
                detail += f"; stored entries don't add up to the printout for {', '.join(res['unbalanced'])}"
        elif kind == "invoice_page":
            save_invoice_page(restaurant_id, inv, file_id=file_id, db_path=db_path, conn=conn)
        conn.execute("UPDATE bo_files SET status=?, detail=? WHERE id=?", (status, detail, file_id))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    _invalidate(restaurant_id)
    return dict(out, status=status, kind=kind, file_id=file_id, detail=detail)


def _invalidate(restaurant_id):
    try:
        from bar import cache
        cache.invalidate(restaurant_id)
    except Exception as e:
        _capture(e, "backoffice_invalidate", restaurant_id)


# ── categories ────────────────────────────────────────────────────────────

_FOOD_NAME = re.compile(r"FOOD|BAKERY|PRODUCE|MEATS?\b|SEAFOOD|POULTRY|DAIRY|FARMS?\b|& SONS", re.I)

# The Fintech vendor mix, kept per (database, restaurant) on the Bar's own
# stamp (bar.cache.stamp: every Fintech file, owner sort and Bar write moves
# it) and never older than _MIX_TTL — it read every Fintech line ever, four
# times per Food Cost purchases request (SCALE-04).
_MIX_TTL = 600.0
_MIX_MAX = 32
_mix_lock = threading.Lock()
_mix_kept = OrderedDict()


def _fintech_vendor_mix(restaurant_id, db_path=None) -> dict:
    """{distributor_key: (dominant pour category, {category: net})} from the
    restaurant's Fintech lines (the Bar's own sorting)."""
    try:
        from bar import cache as _bc
        key, st = _bc._key(restaurant_id, db_path), _bc.stamp(restaurant_id, db_path)
    except Exception as e:
        _capture(e, "backoffice_mix_stamp", restaurant_id, db_path)
        key = st = None
    if key is not None:
        with _mix_lock:
            got = _mix_kept.get(key)
            if got and got[0] == st and time.monotonic() - got[1] < _MIX_TTL:
                _mix_kept.move_to_end(key)
                return got[2]
    try:
        from bar.purchases import _lines
        mix = defaultdict(Counter)
        for l in _lines(restaurant_id, db_path, backoffice=False):
            if l.get("category") in POUR:
                mix[l.get("distributor_key") or distributor_key(l.get("distributor"))][l["category"]] += float(l["net"])
        out = {k: (c.most_common(1)[0][0], dict(c)) for k, c in mix.items() if c}
    except Exception as e:
        _capture(e, "backoffice_fintech_mix", restaurant_id, db_path)
        return {}
    if key is not None:
        with _mix_lock:
            _mix_kept[key] = (st, time.monotonic(), out)
            _mix_kept.move_to_end(key)
            while len(_mix_kept) > _MIX_MAX:
                _mix_kept.popitem(last=False)
    return out


# Name words that are the same word (a vendor's printout name and its
# Fintech name abbreviate differently: 'SCHAMBERGER BROTHERS INC' /
# 'Schamberger Bros., Inc.'). distributor_key has already dropped entity
# words (Inc, LLC, Co, of, and) and turned state names into their codes.
_SAME_WORD = {"bros": "brothers", "bro": "brother", "bev": "beverage", "bevs": "beverage", "beverages": "beverage",
              "dist": "distributing", "distr": "distributing", "distrib": "distributing",
              "distributor": "distributing", "distributors": "distributing", "distribution": "distributing",
              "intl": "international", "natl": "national", "mfg": "manufacturing", "svc": "service",
              "svcs": "service", "services": "service", "sys": "systems", "system": "systems", "wines": "wine",
              "spirit": "spirits", "mkt": "market", "mkts": "market", "markets": "market"}
_STATE_CODES = set(_STATES.values()) | {"nh", "nj", "nm", "ny", "nc", "nd", "ri", "sc", "sd", "wv", "dc"}


def _name_words(key) -> list:
    """A vendor key's words for matching: abbreviations spelled out and the
    trailing state codes a distributor's legal name carries dropped
    ('winebow ct il nj ny' → ['winebow'])."""
    ws = [_SAME_WORD.get(w, w) for w in str(key or "").split()]
    while len(ws) > 1 and ws[-1] in _STATE_CODES:
        ws.pop()
    return ws


def _fintech_link(vk, fin_keys) -> tuple:
    """(Fintech distributor key, how) for a Back Office vendor key — how is
    'exact' (the same key, or the same words once abbreviations and state
    codes are read alike) or 'prefix' (every word of the shorter name, at
    least two, starts the longer one: 'chicago beverage' / 'chicago beverage
    systems'; the shorter's last word may be cut short when it has three or
    more). Never one shared word ('chicago meat authority' is not 'chicago
    beverage systems'; FC-CALC-05 / ING-03), and (None, None) when two
    Fintech distributors fit equally."""
    fin_keys = list(fin_keys or [])
    if vk in fin_keys:
        return vk, "exact"
    a = _name_words(vk)
    if not a:
        return None, None
    exact = [k for k in fin_keys if _name_words(k) == a]
    if exact:
        return (exact[0], "exact") if len(exact) == 1 else (None, None)
    pref = []
    for k in fin_keys:
        b = _name_words(k)
        s, lng = (a, b) if len(a) <= len(b) else (b, a)
        if len(s) < 2:
            continue
        if lng[:len(s)] == s or (len(s) >= 3 and lng[:len(s) - 1] == s[:-1] and len(s[-1]) >= 2
                                 and lng[len(s) - 1].startswith(s[-1])):
            pref.append(k)
    return (pref[0], "prefix") if len(pref) == 1 else (None, None)


def _match_fintech_vendor(vk, fin_keys):
    """The Fintech distributor key a Back Office vendor key names, or None
    (_fintech_link)."""
    return _fintech_link(vk, fin_keys)[0]


def vendor_categories(restaurant_id, rows=None, db_path=None) -> dict:
    """{vendor_key: {category, source owner|fintech|gl|name|unknown, why}} for
    every Back Office vendor of the restaurant, read over all its entries.
    In order: the owner's choice; the Fintech distributor it exactly is
    (its pour mix there); every account its entries book to; a name that
    says food; a Fintech distributor it is only NAMED like (a 'prefix'
    match — offered as a guess, `confirm` True); else unknown. A vendor's
    own accounts and a food name outrank a fuzzy Fintech match
    (FC-CALC-05)."""
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
        fk, how = _fintech_link(vk, list(fin))
        if fk and how == "exact":
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
        if fk:
            out[vk] = {"category": fin[fk][0], "source": "fintech", "fintech_key": fk, "confirm": True,
                       "why": f"named like {fk} on Fintech, which sells mostly {fin[fk][0]} — confirm it"}
            continue
        out[vk] = {"category": "unknown", "source": "unknown", "why": "no account on its invoices"}
    return out


def known_vendor(restaurant_id, vendor, db_path=None) -> bool:
    """Whether the vendor is on the restaurant's Back Office invoices."""
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM bo_invoices WHERE restaurant_id=? AND vendor_key=? LIMIT 1",
                            (int(restaurant_id), _vk(vendor))).fetchone() is not None
    finally:
        conn.close()


def set_vendor_category(restaurant_id, vendor, category, user_id=None, db_path=None) -> dict:
    """The owner's category for one Back Office supplier. A supplier not on
    the restaurant's Back Office invoices is refused (SEC-04: a row per
    arbitrary string)."""
    if category not in CATEGORIES or category == "unknown":
        raise ValueError("Pick food, alcohol (mixed, or one kind), non-alcoholic drinks, other purchases or not food or drink.")
    if not known_vendor(restaurant_id, vendor, db_path=db_path):
        raise ValueError("That supplier isn't on your Back Office invoices.")
    vk = _vk(vendor)
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


def save_lines(restaurant_id, vendor, invoice_number, lines, invoice_date=None, file_id=None, db_path=None,
               conn=None) -> int:
    """One invoice's lines, replacing any on file for it: [{item_code,
    description, gl, purchase_unit, qty, unit_amount, ext_amount}]."""
    vk = _vk(vendor)
    ik = invoice_key(invoice_number)
    with _tx(conn, db_path) as c:
        c.execute("DELETE FROM bo_invoice_lines WHERE restaurant_id=? AND vendor_key=? AND invoice_key=?",
                  (int(restaurant_id), vk, ik))
        for i, l in enumerate(lines, 1):
            c.execute("INSERT INTO bo_invoice_lines (restaurant_id, vendor_key, invoice_key, line_no, item_code, "
                      "description, gl, purchase_unit, qty, unit_amount, ext_amount, invoice_date, file_id, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (int(restaurant_id), vk, ik, i, l.get("item_code"), l.get("description"),
                       str(l["gl"]) if l.get("gl") else None, l.get("purchase_unit"), l.get("qty"),
                       l.get("unit_amount"), float(l["ext_amount"]), invoice_date, file_id, _now()))
    if conn is None:
        _invalidate(restaurant_id)
    return len(lines)


def _line_groups(restaurant_id, db_path=None) -> dict:
    """{(vendor_key, invoice_key): {invoice_date: [(gl, amount)]}} — each
    invoice's lines on file summed by GL account, by the date the page
    gave (None for lines saved without one)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT vendor_key, invoice_key, invoice_date, gl, SUM(ext_amount) AS amt "
                            "FROM bo_invoice_lines WHERE restaurant_id=? GROUP BY vendor_key, invoice_key, "
                            "invoice_date, gl", (int(restaurant_id),)).fetchall()
    except Exception as e:
        _capture(e, "backoffice_lines", restaurant_id, db_path)
        return {}
    finally:
        conn.close()
    out = defaultdict(lambda: defaultdict(list))
    for r in rows:
        out[(r["vendor_key"], r["invoice_key"])][r["invoice_date"]].append((r["gl"], float(r["amt"] or 0)))
    return out


def _rows(restaurant_id, db_path=None) -> list:
    """Every bo_invoices row of the restaurant, oldest first."""
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, vendor, vendor_key, entry_key, invoice_number, entry_kind, itemized, invoice_date AS date, "
            "amount, gl FROM bo_invoices WHERE restaurant_id=? ORDER BY invoice_date, vendor, id",
            (int(restaurant_id),))]
    finally:
        conn.close()


def entries(restaurant_id, start=None, end=None, db_path=None, rows=None, vc=None) -> list:
    """Every Back Office entry with its category and why: [{id, vendor,
    vendor_key, entry_key, invoice_number, entry_kind, itemized, date,
    amount, doc_amount, gl, gl_name, category, category_source}]. An invoice
    whose lines are on file (bo_invoice_lines) is one row per GL account its
    lines book to (category_source 'line'), and what its lines leave of its
    total (tax, a fee, a fuel charge) one more row on the vendor's category
    (category_source 'invoice_rest', shown apart as "Fees & tax" — never
    silently folded into an account) — so a supplier's paper and chemical
    lines leave food, and food splits into meat, dairy, produce. Every row
    of one document carries its `entry_key` and the document's whole
    amount (`doc_amount`), so readers can treat it as one invoice.

    A vendor's category is read over ALL its entries, whatever window is
    asked for. `rows` / `vc` let a caller that already read them (one
    request, SCALE-04) pass them down."""
    rows = _rows(restaurant_id, db_path) if rows is None else rows
    if vc is None:
        vc = vendor_categories(restaurant_id, rows=rows, db_path=db_path)
    gls = gl_map(restaurant_id, db_path)
    groups = _line_groups(restaurant_id, db_path)
    per_inv = Counter((r["vendor_key"], invoice_key(r["invoice_number"])) for r in rows if r["entry_kind"] == "invoice")
    out = []
    for r in rows:
        if (start and r["date"] < str(start)) or (end and r["date"] > str(end)):
            continue
        r = dict(r, doc_amount=float(r["amount"]))
        lg = None
        if r["entry_kind"] == "invoice":
            ik = invoice_key(r["invoice_number"])
            g = groups.get((r["vendor_key"], ik))
            if g:
                # The lines of the invoice dated as this row; lines saved
                # with no (or another) date belong to it only when it is the
                # one entry under that number (a credit memo sharing the
                # number never takes the invoice's lines).
                lg = g.get(r["date"])
                if lg is None and per_inv[(r["vendor_key"], ik)] == 1 and len(g) == 1:
                    lg = next(iter(g.values()))
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
    except Exception as e:
        _capture(e, "backoffice_fintech_invoices", restaurant_id, db_path)
        return []
    finally:
        conn.close()


def _decisions(restaurant_id, db_path=None) -> dict:
    """{(vendor_key, entry_key): decision} — the owner's word on held entries."""
    conn = get_conn(db_path)
    try:
        return {(r["vendor_key"], r["entry_key"]): r["decision"] for r in conn.execute(
            "SELECT vendor_key, entry_key, decision FROM bo_entry_decisions WHERE restaurant_id=?",
            (int(restaurant_id),))}
    except Exception as e:
        _capture(e, "backoffice_decisions", restaurant_id, db_path)
        return {}
    finally:
        conn.close()


def _cents(v) -> int:
    return int(round(float(v or 0) * 100))


def _subset(pool, target) -> list:
    """The fewest of `pool` (2..SUBSET_MAX of them, nearest first) whose
    totals add up to `target` cents, or [] — one payment for a batch of
    invoices (a catch-up check, an invoice net of its credit)."""
    vals = [_cents(f["total"]) for f in pool]
    for size in range(2, min(SUBSET_MAX, len(pool)) + 1):
        for idx in itertools.combinations(range(len(pool)), size):
            if abs(sum(vals[i] for i in idx) - target) <= 1:
                return [pool[i] for i in idx]
    return []


def outside_fintech(restaurant_id, db_path=None, es=None) -> dict:
    """{"lines": [bar purchase lines], "matched": n, "entries": [...],
    "to_confirm": [...], "set_aside": [...]} — the Back Office entries booked
    to beer, wine, liquor, mixed alcohol or bar consumables, matched against
    Fintech one DOCUMENT at a time (every account row of an itemized invoice
    together, ING-01) and only against the invoices of the Fintech
    distributor the vendor is (_fintech_link, ING-02/03):

      1. by invoice number;
      2. else one unused invoice of the document's amount, dated from
         MATCH_DAYS_BEFORE before to MATCH_DAYS_AFTER after it was paid
         (the nearest);
      3. else 2..SUBSET_MAX unused invoices in that window adding up to it
         (a batch payment, an invoice net of its credit; ING-04).

    A matched document is that Fintech invoice and is left out whole. One
    that matches none is counted (`entries`, `lines`) only when its vendor
    is not a Fintech distributor — an entry from a Fintech distributor that
    matches nothing (a Winebow $360 with no Fintech invoice of $360, a
    payment Fintech's history starts after, a short pay) is held in
    `to_confirm` (BAR-CALC-08 / LEAD-01) until the owner counts it or sets
    it aside (decide_entry); `set_aside` lists those set aside.
    `matched` counts Back Office documents matched."""
    es = entries(restaurant_id, db_path=db_path) if es is None else es
    rows = [r for r in es if r["category"] in BAR_CATEGORIES]
    if not rows:
        return {"lines": [], "matched": 0, "entries": [], "to_confirm": [], "set_aside": []}
    from time_utils import mdy
    docs = OrderedDict()
    for r in rows:
        k = (r["vendor_key"], r.get("entry_key") or f"row|{r.get('id')}")
        d = docs.get(k)
        if d is None:
            d = docs[k] = {"key": k, "vendor": r["vendor"], "vendor_key": r["vendor_key"], "entry_key": k[1],
                           "entry_kind": r["entry_kind"], "invoice_number": r["invoice_number"], "date": r["date"],
                           "amount": float(r.get("doc_amount", r["amount"])), "rows": []}
        d["rows"].append(r)
    fins = _fintech_invoices(restaurant_id, db_path)
    fin_keys = sorted({f["distributor_key"] for f in fins if f.get("distributor_key")})
    fin_names = {}
    for f in fins:
        fin_names.setdefault(f["distributor_key"], f.get("distributor") or f["distributor_key"])
    link = {}

    def fk_of(vk):
        if vk not in link:
            link[vk] = _match_fintech_vendor(vk, fin_keys)
        return link[vk]
    by_num = defaultdict(list)
    for f in fins:
        if f.get("invoice_number"):
            by_num[(f["distributor_key"], invoice_key(f["invoice_number"]))].append(f)

    def days(f, d):
        return (date.fromisoformat(str(f["invoice_date"])[:10]) - date.fromisoformat(d["date"])).days

    def window(fk, d, used):
        return [f for f in fins if f["distributor_key"] == fk and f["id"] not in used
                and -MATCH_DAYS_AFTER <= days(f, d) <= MATCH_DAYS_BEFORE]
    order = sorted(docs.values(), key=lambda d: (d["date"], d["vendor"], d["entry_key"]))
    used, hits = set(), {}
    for d in order:                                   # 1. the invoice number, at its own distributor
        fk = fk_of(d["vendor_key"])
        if fk and d["entry_kind"] == "invoice" and d["invoice_number"]:
            same = [f for f in by_num.get((fk, invoice_key(d["invoice_number"]))) or [] if f["id"] not in used]
            if same:
                f = min(same, key=lambda f: ((float(f["total"] or 0) < 0) != (d["amount"] < 0), abs(days(f, d))))
                used.add(f["id"])
                hits[d["key"]] = [f]
    for d in order:                                   # 2. one invoice of its amount
        fk = fk_of(d["vendor_key"])
        if d["key"] in hits or not fk:
            continue
        cands = [f for f in window(fk, d, used) if abs(_cents(f["total"]) - _cents(d["amount"])) <= 1]
        if cands:
            f = min(cands, key=lambda f: abs(days(f, d)))
            used.add(f["id"])
            hits[d["key"]] = [f]
    for d in order:                                   # 3. a few that add up to it
        fk = fk_of(d["vendor_key"])
        if d["key"] in hits or not fk:
            continue
        pool = sorted(window(fk, d, used), key=lambda f: (abs(days(f, d)), f["id"]))[:SUBSET_POOL]
        combo = _subset(pool, _cents(d["amount"]))
        if combo:
            used.update(f["id"] for f in combo)
            hits[d["key"]] = combo
    decisions = _decisions(restaurant_id, db_path)
    out, to_confirm, set_aside, matched = [], [], [], 0
    for d in order:
        if d["key"] in hits:
            matched += 1
            continue
        fk = fk_of(d["vendor_key"])
        dec = decisions.get(d["key"])
        if dec == "count" or (fk is None and dec != "not_purchase"):
            out.extend(d["rows"])
            continue
        cats = Counter()
        for r in d["rows"]:
            cats[r["category"]] += float(r["amount"])
        amt = round(sum(cats.values()), 2)
        item = {"vendor": d["vendor"], "vendor_key": d["vendor_key"], "entry_key": d["entry_key"],
                "invoice_number": d["invoice_number"], "date": d["date"], "date_mdy": mdy(d["date"]),
                "amount": amt, "category": next(iter(cats)) if len(cats) == 1 else "mixed",
                "categories": {k: round(v, 2) for k, v in cats.items()},
                "fintech_distributor": fin_names.get(fk) if fk else None}
        if dec == "not_purchase":
            set_aside.append(dict(item, why="set aside by the owner"))
            continue
        item["why"] = (f"Paid in Back Office to {d['vendor']}, who supplies you through Fintech, but no Fintech "
                       f"invoice of theirs matches this ${amt:,.2f} entry — counted only once you confirm it.")
        to_confirm.append(item)
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
    return {"lines": lines, "matched": matched, "entries": out, "to_confirm": to_confirm, "set_aside": set_aside}


def outside_fintech_summary(restaurant_id, start=None, end=None, db_path=None) -> dict:
    """The Bar payload's `outside_fintech`: what the Bar counts from Back
    Office in [start, end] — {available, total, by_category, vendors:
    [{vendor, amount, entries}], matched_fintech, to_confirm: [{vendor,
    vendor_key, entry_key, invoice_number, date, date_mdy, amount,
    category, categories, fintech_distributor, why}], to_confirm_total,
    set_aside} — `to_confirm` (never in `total`) is what the owner is asked
    about: paid in Back Office to a Fintech distributor, no Fintech invoice."""
    res = outside_fintech(restaurant_id, db_path)

    def inside(e):
        return (not start or e["date"] >= start) and (not end or e["date"] <= end)
    es = [e for e in res["entries"] if inside(e)]
    tc = [e for e in res["to_confirm"] if inside(e)]
    by_v, by_c = defaultdict(lambda: [0.0, 0]), Counter()
    for e in es:
        by_v[e["vendor"]][0] += float(e["amount"])
        by_v[e["vendor"]][1] += 1
        by_c[e["category"]] += float(e["amount"])
    return {"available": bool(res["entries"]) or bool(res["matched"]) or bool(res["to_confirm"]),
            "total": round(sum(by_c.values()), 2),
            "by_category": {k: round(v, 2) for k, v in by_c.items()},
            "vendors": [{"vendor": v, "amount": round(a, 2), "entries": n}
                        for v, (a, n) in sorted(by_v.items(), key=lambda kv: -kv[1][0])],
            "matched_fintech": res["matched"],
            "to_confirm": tc, "to_confirm_total": round(sum(e["amount"] for e in tc), 2),
            "set_aside": [e for e in res["set_aside"] if inside(e)]}


def decide_entry(restaurant_id, vendor_key, entry_key, decision, user_id=None, db_path=None) -> dict:
    """The owner's word on one held Back Office entry (outside_fintech's
    `to_confirm`): 'count' (alcohol bought outside Fintech after all),
    'not_purchase' (set aside: already on Fintech, a deposit, a payment of
    an older invoice), or None to ask again. Refused for an entry that is
    not on file."""
    vk, ek = str(vendor_key or "")[:200], str(entry_key or "")[:200]
    if decision is not None and decision not in DECISIONS:
        raise ValueError("Say count it or set it aside.")
    with _tx(None, db_path) as c:
        row = c.execute("SELECT vendor FROM bo_invoices WHERE restaurant_id=? AND vendor_key=? AND entry_key=?",
                        (int(restaurant_id), vk, ek)).fetchone()
        if not row:
            raise ValueError("That Back Office entry isn't on file.")
        if decision is None:
            c.execute("DELETE FROM bo_entry_decisions WHERE restaurant_id=? AND vendor_key=? AND entry_key=?",
                      (int(restaurant_id), vk, ek))
        else:
            c.execute("INSERT INTO bo_entry_decisions (restaurant_id, vendor_key, entry_key, decision, set_by, set_at) "
                      "VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, vendor_key, entry_key) DO UPDATE SET "
                      "decision=excluded.decision, set_by=excluded.set_by, set_at=excluded.set_at",
                      (int(restaurant_id), vk, ek, decision, user_id, _now()))
    _invalidate(restaurant_id)
    return {"ok": True, "vendor": row["vendor"], "vendor_key": vk, "entry_key": ek, "decision": decision}


# ── Food Cost: food bought vs sold ────────────────────────────────────────

_FOOD_CATEGORY_RE = re.compile(r"^food|\bkitchen\b|dessert|\bkids?\b|catering|appeti[sz]er|entr[eé]e|brunch|"
                               r"breakfast|\blunch\b|\bdinner\b|\bsides?\b|pizza|bakery", re.I)
_NOT_FOOD_CATEGORY_RE = re.compile(r"beverage|drink|\bbar\b|\bna\b|retail|merch|gift|liquor|beer|wine|spirit|"
                                   r"cocktail", re.I)


def is_food_category(name) -> bool:
    """Whether a DSR category (the restaurant's own, sales.cat:<name>, mapped
    from its POS departments through dsr_category_map) is food: Erik's
    "Food", or an owner category that names kitchen sales (Desserts, Kids,
    Catering); never a drink, retail or Unmapped one (FC-CALC-20)."""
    n = " ".join(str(name or "").lower().split())
    if not n or n == "unmapped" or _NOT_FOOD_CATEGORY_RE.search(n):
        return False
    return bool(_FOOD_CATEGORY_RE.search(n))


_FOOD_DISCOUNT_RE = re.compile(r"\bfood\b|\bkitchen\b", re.I)
_BAR_DISCOUNT_RE = re.compile(r"\bbar\b|liquor|beer|wine|drink|beverage", re.I)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _food_night(mapping, met, arch):
    """One night's food sales: {net, gross, discounts, basis allocated |
    estimated | none, partial, unmapped} or None when the night has no
    sales by department, item category or category on file. Read like the
    Bar reads its drinks (bar.sales._night): the report's department and
    item-category dollars under the DSR category map AS IT STANDS (never
    the categories the report froze), else the ticket archive's cells, else
    departments, else the report's own categories."""
    from dsr import store as dstore
    unmapped_name = "Unmapped"

    def cat(dep, sub):
        if sub:
            c = dstore.category_for(dstore.sub_name(dep, sub), mapping)
            if c != unmapped_name:
                return c
        return dstore.category_for(dep, mapping)
    deps = {k[len("sales.dep:"):]: v for k, v in met.items() if k.startswith("sales.dep:")}
    subs = {}
    for k, v in met.items():
        if k.startswith("sales.depcat:"):
            dep, sub = dstore.split_sub(k[len("sales.depcat:"):])
            if sub:
                subs[(dep, sub)] = v
    cats = {k[len("sales.cat:"):]: v for k, v in met.items() if k.startswith("sales.cat:")}
    arch = arch or {}
    cells = []                                   # (category, gross, archive cell or None, POS name)
    if deps and subs:
        inside = defaultdict(float)
        for (dep, sub), v in subs.items():
            cells.append((cat(dep, sub), _f(v), arch.get((dep.lower(), sub.lower())), f"{dep} {sub}"))
            inside[dep.lower()] += _f(v)
        for dep, v in deps.items():
            rest = round(_f(v) - inside.get(dep.lower(), 0.0), 2)
            if abs(rest) >= 0.01 or dep.lower() not in inside:
                cells.append((cat(dep, None), rest, arch.get((dep.lower(), "")), dep))
    elif arch:
        for a in arch.values():
            if not a["department"] and not a["sales_category"]:
                continue                         # the night's unallocated discounts: not a sale
            cells.append((cat(a["department"], a["sales_category"] or None),
                          _f(a["sales"]) + _f(a["discount_lines"]), a,
                          f"{a['department'] or ''} {a['sales_category'] or ''}"))
    elif deps:
        for dep, v in deps.items():
            cells.append((cat(dep, None), _f(v), None, dep))
    elif cats:
        for c, v in cats.items():
            cells.append((c, _f(v), None, c))
    else:
        return None
    positive = sum(c[1] for c in cells if c[1] > 0)
    food = [(g, a) for c, g, a, _n in cells if g > 0 and is_food_category(c)]
    gross = sum(g for g, _a in food)
    unmapped = sum(g for c, g, _a, _n in cells if g > 0 and c == unmapped_name)
    if arch:
        # Department dollars hold a cell's own discount lines; the per-ticket
        # allocation takes every check discount once (bar.sales module note).
        own = sum(_f(a["discount_lines"]) for _g, a in food if a)
        disc = sum(_f(a["discounts"]) for _g, a in food if a)
        net, basis = gross - own - disc, "allocated"
    else:
        # The night's negative departments are its check discounts: one the
        # POS names as food's ("Discounts › Discount Food") is food's whole,
        # one it names as the bar's is none of it, and the rest ("Discount
        # All") is spread over the night's sales by share — an estimate, said.
        named = sum(-g for _c, g, _a, n in cells if g < 0 and _FOOD_DISCOUNT_RE.search(n or ""))
        shared = sum(-g for _c, g, _a, n in cells if g < 0 and not _FOOD_DISCOUNT_RE.search(n or "")
                     and not _BAR_DISCOUNT_RE.search(n or ""))
        if (named > 0 or shared > 0) and gross > 0:
            disc = named + (shared * gross / positive if positive > 0 else 0.0)
            net, basis = gross - disc, ("estimated" if shared > 0 else "named")
        else:
            disc, net, basis = 0.0, gross, "none"
    partial = unmapped > max(1.0, positive * UNMAPPED_NIGHT_PCT / 100.0)
    return {"net": round(net, 2), "gross": round(gross, 2), "discounts": round(gross - net, 2), "basis": basis,
            "partial": partial, "unmapped": round(unmapped, 2)}


def food_sales_detail(restaurant_id, start, end, db_path=None) -> dict:
    """The POS's food sales night by night over [start, end], on one stated
    basis — the ONE definition every Back Office reader uses (_food_sales;
    food_bought_vs_sold, backoffice_counts, backoffice_read, cogs):
    {by_date {date: food sales after check discounts}, partial [dates whose
    sales the DSR map leaves partly Unmapped], estimated [dates whose check
    discounts were spread pro rata], label}. Food is the restaurant's DSR
    food categories (is_food_category) — a department mapped elsewhere, or
    not at all, is never guessed in. Check discounts are taken off food as
    the Bar takes them off drinks (POS-05): per ticket from the archive
    (bar_sales_nights), else from the night's negative departments — one the
    POS names as food's ("Discounts › Discount Food") wholly, one it names
    as the bar's not at all, the rest ("Discount All") spread over its sales
    by share: an estimate, and the label says so. A night with
    no food sales mapped is left out (never a $0 night)."""
    from bar import sales as bs
    rid = int(restaurant_id)
    s, e = str(start)[:10], str(end)[:10]
    conn = get_conn(db_path)
    try:
        met = bs._metrics(conn, rid, s, e)
        arch = bs._archived(conn, rid, s, e)
    finally:
        conn.close()
    try:
        from dsr import store as dstore
        mapping = dstore.category_map(rid, db_path) if db_path else dstore.category_map(rid)
    except Exception as ex:
        _capture(ex, "backoffice_food_sales_map", rid, db_path)
        mapping = {}
    by_date, partial, estimated = {}, [], []
    for d in sorted(set(met) | set(arch)):
        n = _food_night(mapping, met.get(d, {}), arch.get(d))
        if not n or n["gross"] <= 0:
            continue
        by_date[d] = n["net"]
        if n["partial"]:
            partial.append(d)
        if n["basis"] == "estimated":
            estimated.append(d)
    label = "POS food sales after check discounts"
    if estimated:
        label += f" (check discounts estimated for {len(estimated)} night{'s' if len(estimated) != 1 else ''})"
    return {"by_date": by_date, "partial": partial, "estimated": estimated, "label": label}


def _food_sales(restaurant_id, start, end, db_path=None) -> dict:
    """{date: food sales} — food_sales_detail's nights (one definition)."""
    return food_sales_detail(restaurant_id, start, end, db_path)["by_date"]


def _account_label(name) -> str:
    """'4030 MEAT' → 'Meat'; an amount no account split reads 'Not split
    yet'; what an invoice's lines leave of its total reads as itself."""
    if name == "Food (by vendor)":
        return "Not split yet"
    if name == FEES_NAME:
        return "Tax, fees and charges off the lines"
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
            except Exception as e:
                try:
                    conn.rollback()   # Postgres: a failed statement aborts the transaction
                except Exception:
                    pass
                _capture(e, "backoffice_feed_state", restaurant_id, db_path)
                continue          # that part reads as not on file (SCALE-11: said, not silent)
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


def _local_today(restaurant_id):
    """The restaurant's own calendar date (POS-18): a UTC server's today is
    tomorrow after 7pm Central."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception as e:
        _capture(e, "backoffice_local_today", restaurant_id)
        return date.today()


def _trading_days(sales, start, end) -> int:
    """The days in [start, end] on a weekday the restaurant trades: a
    weekday with sales on at least half its days over the 12 weeks to `end`
    (from the first sales night) — a weekday seen fewer than twice counts
    as trading, so a short history never excuses a missing night."""
    open_days = {d for d, v in sales.items() if v > 0}
    if not open_days:
        return 0
    e = date.fromisoformat(end)
    lo = max(date.fromisoformat(min(open_days)), e - timedelta(days=83))
    seen, hit = Counter(), Counter()
    d = lo
    while d <= e:
        seen[d.weekday()] += 1
        hit[d.weekday()] += 1 if d.isoformat() in open_days else 0
        d += timedelta(days=1)
    trades = {w for w in range(7) if seen[w] < 2 or hit[w] >= 0.5 * seen[w]}
    n, d = 0, date.fromisoformat(start)
    while d <= e:
        n += 1 if d.weekday() in trades else 0
        d += timedelta(days=1)
    return n


def food_bought_vs_sold(restaurant_id, today=None, db_path=None, es=None) -> dict:
    """Food Cost's purchase-based reading from Back Office: {available,
    basis, label, sales_basis, window {from, to, from_mdy, to_mdy, days,
    purchases, fees_tax, sales, pct, withheld, nights, covered_nights,
    partial_nights, trading_days}, months: [{month, label, purchases,
    sales, pct, nights, partial}], by_category, vendors, unknown_vendors,
    note}. Purchase-based (Back Office shows no submitted count): what was
    bought as a % of what the POS sold as food — never called food cost.

    The headline window is the WINDOW_DAYS to the last day both sides cover,
    starting no earlier than the first sales night (as the months do), and
    its pct is withheld (None, `withheld` says why) unless sales cover at
    least WINDOW_MIN_COVERAGE of the days the restaurant trades in it and
    WINDOW_MIN_NIGHTS nights — a night the DSR map leaves partly Unmapped
    is not covered (FC-CALC-03 / POS-01). `today` defaults to the
    restaurant's own date (POS-18); `es` lets one request pass the entries
    it already read (SCALE-04)."""
    from time_utils import mdy
    today = today or _local_today(restaurant_id)
    es = entries(restaurant_id, db_path=db_path) if es is None else es
    food = [e for e in es if e["category"] == "food"]
    unknown = sorted({e["vendor"] for e in es if e["category"] == "unknown"})
    if not food:
        return {"available": False, "why_not": "No Back Office food invoices on file yet.", "unknown_vendors": unknown}
    last = max(e["date"] for e in food)
    first = min(e["date"] for e in food)
    fs = food_sales_detail(restaurant_id, first, today.isoformat(), db_path)
    sales = {d: v for d, v in fs["by_date"].items() if v > 0}
    if not sales:
        return {"available": False, "why_not": "No food sales from the nightly report yet.",
                "unknown_vendors": unknown}
    sales_from = min(sales)
    partial = set(fs["partial"])
    end = min(last, max(sales))
    start = max((date.fromisoformat(end) - timedelta(days=WINDOW_DAYS - 1)).isoformat(), sales_from)

    def _window(a, b):
        p = sum(float(e["amount"]) for e in food if a <= e["date"] <= b)
        s = sum(v for d, v in sales.items() if a <= d <= b)
        return round(p, 2), round(s, 2), (round(100.0 * p / s, 1) if s > 0 else None), \
            sum(1 for d in sales if a <= d <= b)
    if start > end:
        p, s, pct, n = 0.0, 0.0, None, 0
        covered = trading = 0
    else:
        p, s, pct, n = _window(start, end)
        covered = sum(1 for d in sales if start <= d <= end and d not in partial)
        trading = _trading_days(sales, start, end)
    withheld = None
    if start > end:
        withheld = "Back Office's food invoices on file end before the nightly report's food sales begin."
    elif covered < max(WINDOW_MIN_NIGHTS, WINDOW_MIN_COVERAGE * trading):
        withheld = (f"Food sales cover {covered} of the {trading} days you trade from {mdy(start)} to {mdy(end)} — "
                    f"too few to set the {'$' + format(p, ',.0f')} bought against.")
        pct = None
    fees = round(sum(float(e["amount"]) for e in food if start <= e["date"] <= end
                     and e.get("category_source") == "invoice_rest"), 2)
    # A month compares only the days both sides cover (EJ's August read 243%:
    # a month of invoices against six nights of sales), and a month with
    # fewer than MONTH_MIN_NIGHTS nights of sales is not shown.
    months = []
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
    for e in food:
        if start <= e["date"] <= end:
            name = FEES_NAME if e.get("category_source") == "invoice_rest" else (e.get("gl_name") or "Food (by vendor)")
            by_c[name] += float(e["amount"])
            by_v[e["vendor"]] += float(e["amount"])
    return {"available": True, "basis": "purchases", "label": "Food bought vs sold", "sales_basis": fs["label"],
            "window": {"from": start, "to": end, "from_mdy": mdy(start), "to_mdy": mdy(end), "days": WINDOW_DAYS,
                       "purchases": p, "fees_tax": fees, "sales": s, "pct": pct, "withheld": withheld, "nights": n,
                       "covered_nights": covered, "partial_nights": sum(1 for d in partial if start <= d <= end),
                       "trading_days": trading},
            "months": months,
            "by_category": [{"name": k, "label": _account_label(k), "amount": round(v, 2)} for k, v in by_c.most_common()],
            "vendors": [{"vendor": k, "amount": round(v, 2)} for k, v in by_v.most_common()],
            "unknown_vendors": unknown,
            "note": ("Purchase-based: what Back Office shows you paid food suppliers, as a % of the POS's food "
                     "sales after check discounts. A delivery or a stock-up moves it; a food cost % needs an "
                     "opening and a closing count. A supplier's invoice can carry paper or cleaning lines too, and "
                     "the tax and fees off its lines are shown on their own.")}


def purchases_view(restaurant_id, today=None, db_path=None) -> dict:
    """The Food Cost purchases route's whole payload (GET
    /api/food-cost/purchases and its phone twin): food_bought_vs_sold plus
    `suppliers` [{vendor, vendor_key, amount, entries, category, source,
    why, …}] — every Back Office entry and vendor category read ONCE for
    the request (SCALE-04: it read them, and every Fintech line, four
    times)."""
    rows = _rows(restaurant_id, db_path)
    vc = vendor_categories(restaurant_id, rows=rows, db_path=db_path)
    es = entries(restaurant_id, db_path=db_path, rows=rows, vc=vc)
    out = food_bought_vs_sold(restaurant_id, today=today, db_path=db_path, es=es)
    names = {e["vendor_key"]: e["vendor"] for e in es}
    # What each supplier was paid on file, so a supplier to sort says how
    # much rides on it (owner, 10/16/26).
    paid, n = Counter(), Counter()
    for e in es:
        paid[e["vendor_key"]] += float(e["amount"] or 0)
        n[e["vendor_key"]] += 1
    out["suppliers"] = sorted(({"vendor": names.get(k, k), "vendor_key": k, "amount": round(paid.get(k, 0.0), 2),
                                "entries": n.get(k, 0), **v} for k, v in vc.items()),
                              key=lambda x: (x["category"] != "unknown", -x["amount"], x["vendor"]))
    return out
