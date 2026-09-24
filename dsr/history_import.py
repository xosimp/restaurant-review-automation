"""
dsr.history_import — Last Year, from the owner's old DSR workbooks.

RPower keeps about a month of history for some stores, so the rollup's Last
Year column (dsr.rollup._last_year: that night's DSR, else an imported row,
else the POS sync) would stay empty for a year without this. The owner
uploads a .xlsx or .csv; each day becomes one dsr_history_import row
(dsr.store.import_history), and a re-import of the same day replaces it.

    parse(filename, data, today) -> {"rows", "skipped", "errors", "layout"}

LAYOUTS. Only the TEMPLATE layout is read today — one row per day under a
header row (GET /dsr/history/template.csv):

    Date, Gross, Net, Food, Liquor, Beer, Wine, Retail, NA Beverage

Headers match loosely (case, spacing and punctuation ignored; "Day" for
Date, "NA Bev" for NA Beverage, "Net Sales", "Total Gross Sales" …), columns
may come in any order, unknown columns are ignored, and every sheet of a
workbook whose header matches is read. Erik's own workbooks are NOT this
shape — one sheet per week, a weekly grid with the days across (or down) and
blocks for Last Year / Budget / Current — and his file has not arrived, so
its layout is not guessed at. When it does, add a parser to LAYOUTS: a
function (sheet_name, grid) -> rows-or-None that recognises its own sheets;
parse() tries each layout on each sheet, so the template keeps working
beside it.

Rules, whatever the layout:
  * a blank cell is NOT a zero — a day with neither gross nor net is
    skipped (counted), a blank category is left out;
  * a figure that is not a number is an error for that row, which is not
    imported — the rest of the file still is, and each error says where;
  * dates: Excel serial numbers (1900 or 1904 date system), ISO, M/D/YY and
    M/D/YYYY, with or without a weekday in front; a future date is refused;
  * the first row for a date wins; a repeat is an error, not a silent
    overwrite.

Reading .xlsx without openpyxl (not installed, and not to be): the file is a
zip of XML — xl/workbook.xml (sheets, the 1904 flag), its relationships,
xl/sharedStrings.xml and xl/worksheets/*.xml — read with zipfile and
xml.etree. Every member is size-checked before it is read and any DTD is
refused, so a crafted workbook can't expand without bound.
"""
import csv
import io
import re
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import dsr as _dsr

MAX_BYTES = 2 * 1024 * 1024           # the upload
MAX_XML_BYTES = 25 * 1024 * 1024      # one member of the workbook, uncompressed
MAX_SHEETS = 80                       # a year of weekly sheets, and some
MAX_ROWS = 5000                       # data rows across the file
MAX_ERRORS = 50
HEADER_SCAN_ROWS = 15

TEMPLATE_COLUMNS = ("Date", "Gross", "Net") + _dsr.DEFAULT_CATEGORIES

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


class HistoryImportError(ValueError):
    """The whole file can't be read — its message is owner-facing."""


# ── reading the file into grids ─────────────────────────────────────────────

def _xml(z, name):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:
        raise HistoryImportError("That workbook is too large to read.")
    raw = z.read(info)
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise HistoryImportError("That workbook couldn't be read.")
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise HistoryImportError("That workbook couldn't be read — it may be damaged.")


def _col(ref):
    """"C12" -> 2."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def read_xlsx(data):
    """([(sheet name, rows)], date1904). Each row is a list of cell values —
    float for a number, str for text, None for empty — in column order."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HistoryImportError("That isn't a readable .xlsx file.")
    with z:
        book = _xml(z, "xl/workbook.xml")
        if book is None:
            raise HistoryImportError("That isn't a readable .xlsx file.")
        pr = book.find("m:workbookPr", _NS)
        date1904 = pr is not None and pr.get("date1904") in ("1", "true")
        rels = {}
        rel_root = _xml(z, "xl/_rels/workbook.xml.rels")
        for r in (rel_root.findall("rel:Relationship", _NS) if rel_root is not None else []):
            target = r.get("Target") or ""
            target = target.lstrip("/")
            rels[r.get("Id")] = target if target.startswith("xl/") else f"xl/{target}"
        shared = []
        sst = _xml(z, "xl/sharedStrings.xml")
        for si in (sst.findall("m:si", _NS) if sst is not None else []):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        sheets = []
        for i, s in enumerate(book.findall("m:sheets/m:sheet", _NS)):
            if i >= MAX_SHEETS:
                break
            path = rels.get(s.get(f"{{{_NS['r']}}}id")) or f"xl/worksheets/sheet{i + 1}.xml"
            root = _xml(z, path)
            if root is None:
                continue
            sheets.append((s.get("name") or f"Sheet{i + 1}", _sheet_rows(root, shared)))
    return sheets, date1904


def _sheet_rows(root, shared):
    rows = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        # Excel leaves empty rows out; pad to the row's own number so an
        # error names the row the owner sees.
        try:
            number = int(row.get("r") or 0)
        except ValueError:
            number = 0
        while len(rows) < number - 1 and len(rows) <= MAX_ROWS + HEADER_SCAN_ROWS:
            rows.append([])
        cells = {}
        for c in row.findall("m:c", _NS):
            idx = _col(c.get("r") or "") if c.get("r") else len(cells)
            if idx < 0 or idx > 200:
                continue
            kind = c.get("t")
            v = c.find("m:v", _NS)
            text = v.text if v is not None else None
            if kind == "s":
                try:
                    val = shared[int(text)]
                except (TypeError, ValueError, IndexError):
                    val = None
            elif kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            elif kind in ("str", "e"):
                val = text
            elif kind == "b":
                val = None
            else:
                try:
                    val = float(text) if text not in (None, "") else None
                except ValueError:
                    val = text
            cells[idx] = val
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i) for i in range(width)])
        else:
            rows.append([])
        if len(rows) > MAX_ROWS + HEADER_SCAN_ROWS:
            break
    return rows


def read_csv(data):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text))):
        if i > MAX_ROWS + HEADER_SCAN_ROWS:
            break
        rows.append([c if c.strip() else None for c in r])
    return [("CSV", rows)], False


# ── values ──────────────────────────────────────────────────────────────────

def _norm(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


# Normalised header -> field. Categories keep Erik's labels (dsr.DEFAULT_CATEGORIES).
HEADER_ALIASES = {
    "date": "date", "day": "date", "businessdate": "date", "businessday": "date",
    "gross": "gross", "grosssales": "gross", "totalgross": "gross", "totalgrosssales": "gross",
    "net": "net", "netsales": "net", "totalnet": "net", "totalnetsales": "net",
    "food": "Food", "foodsales": "Food",
    "liquor": "Liquor", "liquorsales": "Liquor",
    "beer": "Beer", "beerbottlecan": "Beer", "beersales": "Beer",
    "wine": "Wine", "winesales": "Wine",
    "retail": "Retail", "retailrental": "Retail", "retailsales": "Retail",
    "nabeverage": "NA Beverage", "nabeverages": "NA Beverage", "nabev": "NA Beverage", "nabevs": "NA Beverage",
    "nonalcoholic": "NA Beverage", "nonalcoholicbeverage": "NA Beverage", "nonalcoholicbeverages": "NA Beverage",
}

_LABELS = {"gross": "Gross", "net": "Net"}
_MONEY_RE = re.compile(r"^\(?-?\$?-?[\d,]*\.?\d+\)?$")
_WEEKDAY_RE = re.compile(r"^(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+", re.I)


def money(v):
    """A cell as a figure: None for blank (never 0), a float, or ValueError.
    "$1,234.50", "1234.5", "(12.00)" (negative), "-" (blank)."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if s in ("", "-", "—", "–"):
        return None
    if not _MONEY_RE.match(s):
        raise ValueError(v)
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "")
    out = float(s)
    return -abs(out) if neg else out


def excel_date(serial, date1904=False):
    base = date(1904, 1, 1) if date1904 else date(1899, 12, 30)
    return base + timedelta(days=int(float(serial)))


def cell_date(v, date1904=False):
    """A cell as a date, None for blank, ValueError otherwise."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        d = excel_date(v, date1904)
    else:
        s = _WEEKDAY_RE.sub("", " ".join(str(v).split()))
        if not s:
            return None
        d = None
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = None if d else re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})$", s)
        if m:
            y = int(m.group(3))
            d = date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
        if d is None:
            if re.match(r"^\d+(\.\d+)?$", s):
                d = excel_date(s, date1904)
            else:
                raise ValueError(v)
    if not 2000 <= d.year <= 2100:
        raise ValueError(v)
    return d


# ── layouts ─────────────────────────────────────────────────────────────────

def _template_header(grid):
    """(row index, {column: field}) of the template's header, or None."""
    for i, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        cols = {}
        for j, h in enumerate(row):
            f = HEADER_ALIASES.get(_norm(h)) if isinstance(h, str) else None
            if f and f not in cols.values():
                cols[j] = f
        fields = set(cols.values())
        if "date" in fields and fields & {"gross", "net"}:
            return i, cols
    return None


def template_layout(sheet, grid, date1904, errors):
    """The template: a header row, then one row per day. None when the sheet
    isn't in this layout."""
    head = _template_header(grid)
    if head is None:
        return None
    start, cols = head
    out = []
    for i in range(start + 1, len(grid)):
        row = grid[i]
        where = f"{sheet} row {i + 1}" if sheet != "CSV" else f"Row {i + 1}"
        vals = {f: (row[j] if j < len(row) else None) for j, f in cols.items()}
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in vals.values()):
            continue
        try:
            day = cell_date(vals.get("date"), date1904)
        except (ValueError, OverflowError):
            errors.append(f"{where}: '{vals.get('date')}' isn't a date.")
            continue
        rec, bad = {"date": day, "where": where, "categories": {}}, None
        for f, v in vals.items():
            if f == "date":
                continue
            try:
                n = money(v)
            except ValueError:
                bad = f"{where}: {_LABELS.get(f, f)} '{v}' isn't a number."
                break
            if f in ("gross", "net"):
                rec[f] = n
            elif n is not None:
                rec["categories"][f] = n
        if bad:
            errors.append(bad)
            continue
        if day is None:
            errors.append(f"{where} has figures but no date.")
            continue
        out.append(rec)
    return out


# (name, parser). A parser returns the sheet's day records, or None when the
# sheet is not in its layout. Erik's weekly-grid layout goes here when his
# workbook arrives (see the module docstring).
LAYOUTS = (("template", template_layout),)


def parse(filename, data, today=None):
    """{"rows": [{"date", "gross", "net", "categories"}], "skipped", "errors",
    "layout"} — rows ready for store.import_history. Raises HistoryImportError
    when the file as a whole can't be read."""
    from time_utils import mdy
    name = str(filename or "").lower()
    if len(data or b"") > MAX_BYTES:
        raise HistoryImportError(f"That file is over {MAX_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise HistoryImportError("That file is empty.")
    if name.endswith(".xlsx") or data[:2] == b"PK":
        sheets, date1904 = read_xlsx(data)
    elif name.endswith(".csv"):
        sheets, date1904 = read_csv(data)
    else:
        raise HistoryImportError("Upload a .xlsx or .csv file.")
    today = today or date.today()
    errors, records, layouts, unread = [], [], [], []
    for sheet, grid in sheets:
        for lname, parser in LAYOUTS:
            got = parser(sheet, grid, date1904, errors)
            if got is not None:
                records += got
                layouts.append(lname)
                break
        else:
            if any(any(c is not None for c in r) for r in grid):
                unread.append(sheet)
    if not layouts:
        raise HistoryImportError("Couldn't find the columns — the first row should read "
                           + ", ".join(TEMPLATE_COLUMNS) + ". Download the template to see the layout.")
    for sheet in unread:
        errors.append(f"Sheet '{sheet}' isn't in the template layout; it was not read.")
    rows, skipped, seen = [], 0, {}
    for rec in records[:MAX_ROWS]:
        day = rec["date"]
        if day > today:
            errors.append(f"{rec['where']}: {mdy(day)} is in the future.")
            continue
        if day in seen:
            errors.append(f"{rec['where']}: {mdy(day)} is already on {seen[day]}; the first was kept.")
            continue
        seen[day] = rec["where"]
        if rec.get("gross") is None and rec.get("net") is None:
            skipped += 1                   # an empty day is not a $0 night
            continue
        rows.append({"date": day.isoformat(), "gross": rec.get("gross"), "net": rec.get("net"),
                     "categories": rec["categories"]})
    if len(records) > MAX_ROWS:
        errors.append(f"Only the first {MAX_ROWS:,} days were read.")
    if len(errors) > MAX_ERRORS:
        errors = errors[:MAX_ERRORS] + [f"…and {len(errors) - MAX_ERRORS} more."]
    return {"rows": rows, "skipped": skipped, "errors": errors, "layout": layouts[0]}


def template_csv():
    """The template's header row, and nothing else — an example row would be
    imported as a real night if it were left in."""
    buf = io.StringIO()
    csv.writer(buf).writerow(TEMPLATE_COLUMNS)
    return buf.getvalue()
