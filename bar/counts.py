"""
bar/counts.py — the bar's shelf counts, and the pour cost they make
measurable (audit 10/14/26 F1: FIN-13, AIX-18).

What the Bar page reads from the invoices alone is a PURCHASE ratio: what
was bought against what was sold. It cannot tell an over-pour from a
stock-up. A count can: with the shelf valued at the start and at the end of
a span,

    used (COGS) = begin value + purchases − end value
    pour cost   = used ÷ that category's sales

per category (beer & seltzer, liquor, wine). This module keeps the counts
(`bar_counts`, one value per restaurant, count date and category) and
computes that identity the way cogs.build_food_cost_pct computes food cost:
WITHHELD, with a `missing` list naming why, unless every part was measured —

  * two counts bracketing the span, each within COUNT_TOLERANCE_DAYS of its
    edge (two counts that resolve to the same date bracket nothing);
  * the distributors' invoices complete across it (bar.pour_cost._gaps: a
    distributor whose invoices stop for much longer than its own rhythm);
  * sales by category on at least SALES_COVERAGE_FLOOR of its trading days
    (the weekdays the restaurant trades, models.trading_weekdays);
  * a used figure that is possible (not negative, not above the sales).

A count dated D is the shelf at the close of D ("Count the bar tonight"), so
the span it opens is (D, next count]: purchases dated after D up to and
including the end count's date, and the sales of those same nights.
Purchases are read through bar.purchases._lines (each line's `net`: a
keg's deposit taken out; empties, deposit credits and fees are categories
of their own, never beer), sales through bar.sales._sales — this module
calls them and never copies them, so a fix to either reaches here.

Writes: `save_count` (the owner's or a FOOD_COST_ENTER login's typed values
— one value at a time, never a whole-count overwrite), `delete_count` (the
account holder), and `read_valuation_file` — email_intake's reader for kind
`backoffice_valuation`, a Back Office "inventory valuation by category"
report (UNVERIFIED: no sample yet; docs/plans/BACK_OFFICE_INTEGRATION.md).
An imported value never replaces one the owner typed.

`run_count_reminders` is the month-end job (jobs_registry
`bar_count_reminders`): on each restaurant's own last day of the month, one
"Count the bar tonight" through strategy_jobs._reach (its briefing budget,
audience and quiet hours), for restaurants with distributor invoices.
"""
import csv
import io
import re
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from bar.common import LABELS, POUR_CATEGORIES, _mdy, get_conn

COUNT_CATEGORIES = POUR_CATEGORIES               # beer, liquor, wine
METHODS = ("category_value", "items", "import")
SOURCES = ("owner", "backoffice", "import")
# A count this far from a span's edge still brackets it; further, the span
# is not the one the count measured.
COUNT_TOLERANCE_DAYS = 10
# Sales by category on at least this share of the span's trading days.
SALES_COVERAGE_FLOOR = 0.90
# A bar's whole shelf by category, typed in dollars.
MAX_COUNT_VALUE = 1_000_000
MAX_NOTE_CHARS = 500
# The counted periods the payload carries, newest last.
MAX_PERIODS = 12
# The month-end reminder: from this local hour, until REMINDER_UNTIL.
REMINDER_HOUR = 14
REMINDER_UNTIL = 20
REMINDERS_CURSOR_KEY = "bar_count_reminders_cursor"
REMINDERS_MAX_SECONDS = 5 * 60
REMINDER_ALERT_TYPE = "bar_count_due"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_bar_counts(db_path=None):
    """The bar's counts. Boot only (models.init_db, beside init_bar_cost).
    A financial record: kept, never pruned."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            count_date TEXT NOT NULL,
            category TEXT NOT NULL,
            value_usd REAL NOT NULL,
            method TEXT NOT NULL DEFAULT 'category_value',
            source TEXT NOT NULL DEFAULT 'owner',
            entered_by INTEGER,
            entered_at TEXT NOT NULL,
            note TEXT,
            UNIQUE (restaurant_id, count_date, category))""")
        conn.commit()
    finally:
        conn.close()


# ── the counts ──────────────────────────────────────────────────────────────

def _rows(restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT count_date, category, value_usd, method, source, entered_by, entered_at, note "
                            "FROM bar_counts WHERE restaurant_id=? ORDER BY count_date, category",
                            (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _by_date(rows) -> dict:
    """{count_date: {category: row}}."""
    out = {}
    for r in rows:
        out.setdefault(str(r["count_date"])[:10], {})[r["category"]] = r
    return out


def history(restaurant_id, db_path=None, dollars=True, limit=24) -> list:
    """The counts, newest first: [{count_date, count_date_mdy, categories,
    values {beer, liquor, wine}, sources, note, entered_at}]. Without
    `dollars` (a FOOD_COST_ENTER login without FOOD_COST_VIEW) only which
    categories were counted on which date — no value."""
    out = []
    for d, cats in sorted(_by_date(_rows(restaurant_id, db_path)).items(), reverse=True)[:int(limit)]:
        row = {"count_date": d, "count_date_mdy": _mdy(d),
               "categories": [c for c in COUNT_CATEGORIES if c in cats]}
        if dollars:
            row["values"] = {c: (round(float(cats[c]["value_usd"]), 2) if c in cats else None)
                             for c in COUNT_CATEGORIES}
            row["sources"] = {c: cats[c]["source"] for c in COUNT_CATEGORIES if c in cats}
            notes = sorted({str(cats[c]["note"]) for c in cats if cats[c].get("note")})
            row["note"] = " · ".join(notes) or None
            row["entered_at"] = max(str(cats[c]["entered_at"]) for c in cats)
        out.append(row)
    return out


def _parse_day(value):
    s = str(value or "").strip()
    if len(s) != 10:
        raise ValueError("Pick the count date from the calendar.")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError("Pick the count date from the calendar.")


def _parse_value(value):
    """A typed dollar value: None when blank (that category is left as it
    is), else a number from 0 to MAX_COUNT_VALUE."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Type each value in dollars.")
    try:
        v = float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError("Type each value in dollars.")
    if v != v or v < 0 or v > MAX_COUNT_VALUE:
        raise ValueError(f"A value is between $0 and ${MAX_COUNT_VALUE:,}.")
    return round(v, 2)


def save_count(restaurant_id, count_date, values, note=None, user_id=None, source="owner",
               method="category_value", db_path=None, today=None) -> dict:
    """Save the values given for one count date, ONE CATEGORY AT A TIME: a
    category not in `values`, or blank there, is left exactly as it is — a
    count is never overwritten as a whole list (owner edits never vanish).
    An import (`source` other than owner) never replaces a value the owner
    typed; it says which it kept. Raises ValueError with an owner-facing
    sentence. Returns {ok, count_date, count_date_mdy, saved, kept_owner}."""
    if source not in SOURCES:
        raise ValueError("unknown count source")
    if method not in METHODS:
        raise ValueError("unknown count method")
    day = _parse_day(count_date)
    today = today or date.today()
    if day > today:
        raise ValueError("A count can't be dated in the future.")
    if day < today - timedelta(days=3 * 366):
        raise ValueError("That count is more than three years old.")
    if not isinstance(values, dict):
        raise ValueError("Type a value for beer, liquor or wine.")
    unknown = [k for k in values if k not in COUNT_CATEGORIES]
    if unknown:
        raise ValueError("Counts are by beer, liquor and wine.")
    parsed = {c: _parse_value(values.get(c)) for c in COUNT_CATEGORIES if c in values}
    parsed = {c: v for c, v in parsed.items() if v is not None}
    if not parsed:
        raise ValueError("Type a value for beer, liquor or wine.")
    note = (str(note).strip()[:MAX_NOTE_CHARS] or None) if note else None
    iso = day.isoformat()
    saved, kept = [], []
    conn = get_conn(db_path)
    try:
        existing = {r["category"]: r["source"] for r in conn.execute(
            "SELECT category, source FROM bar_counts WHERE restaurant_id=? AND count_date=?",
            (int(restaurant_id), iso)).fetchall()}
        for c, v in parsed.items():
            if source != "owner" and existing.get(c) == "owner":
                kept.append(c)
                continue
            conn.execute(
                "INSERT INTO bar_counts (restaurant_id, count_date, category, value_usd, method, source, entered_by, "
                "entered_at, note) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, count_date, category) "
                "DO UPDATE SET value_usd=excluded.value_usd, method=excluded.method, source=excluded.source, "
                "entered_by=excluded.entered_by, entered_at=excluded.entered_at, note=excluded.note",
                (int(restaurant_id), iso, c, v, method, source, user_id, _now(), note))
            saved.append(c)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "count_date": iso, "count_date_mdy": _mdy(iso), "saved": saved, "kept_owner": kept}


def delete_count(restaurant_id, count_date, category=None, db_path=None) -> dict:
    """Remove one count date's values, or one category's. Account holder
    only (the route checks)."""
    iso = _parse_day(count_date).isoformat()
    if category is not None and category not in COUNT_CATEGORIES:
        raise ValueError("Counts are by beer, liquor and wine.")
    conn = get_conn(db_path)
    try:
        if category:
            cur = conn.execute("DELETE FROM bar_counts WHERE restaurant_id=? AND count_date=? AND category=?",
                               (int(restaurant_id), iso, category))
        else:
            cur = conn.execute("DELETE FROM bar_counts WHERE restaurant_id=? AND count_date=?",
                               (int(restaurant_id), iso))
        n = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "count_date": iso, "count_date_mdy": _mdy(iso), "deleted": int(n)}


# ── true pour cost ──────────────────────────────────────────────────────────

def _near(dates, target, exclude=None):
    """The count date nearest `target` within COUNT_TOLERANCE_DAYS, or None
    (the earlier one on a tie)."""
    best = None
    for d in dates:
        if d == exclude:
            continue
        off = abs((date.fromisoformat(d) - target).days)
        if off <= COUNT_TOLERANCE_DAYS and (best is None or off < best[0]):
            best = (off, d)
    return best[1] if best else None


def _trading_days(restaurant_id, a, b, db_path=None) -> list:
    """The ISO dates in [a, b] on a weekday the restaurant trades
    (models.trading_weekdays: all seven when no hours are set)."""
    try:
        import models as _m
        r = _m.get_restaurant(int(restaurant_id), db_path=db_path) if db_path else _m.get_restaurant(int(restaurant_id))
        trades = _m.trading_weekdays(r) if r is not None else None
    except Exception:
        trades = None
    out, d = [], a
    while d <= b:
        if not trades or d.strftime("%A") in trades:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _read_inputs(restaurant_id, db_path=None, lines=None, sales=None):
    if lines is None:
        from bar.purchases import _lines
        lines = _lines(restaurant_id, db_path)
    if sales is None:
        from bar.sales import _sales
        sales = _sales(restaurant_id, db_path)
    return lines, sales


def _missing(component, why):
    return {"component": component, "why": why}


def true_pour_cost(restaurant_id, start, end, db_path=None, lines=None, sales=None, rows=None) -> dict:
    """Counted pour cost by category over [start, end], withheld unless
    measured (cogs.build_food_cost_pct's contract):

        {ok, from, to, from_mdy, to_mdy, begin_date, end_date,
         categories {c: {begin, purchases, end, used, sales, pct, missing}},
         total {begin, purchases, end, used, sales, pct}, missing}

    Per category: used = begin + purchases − end, where begin is the count
    nearest `start` and end the count nearest `end` (each within
    COUNT_TOLERANCE_DAYS), purchases are the invoice lines dated after the
    begin count up to and including the end count, and sales are those same
    nights'. `pct` is None — and `missing` says why — when any part was not
    measured. The total is given only when all three categories are."""
    a = start if isinstance(start, date) else date.fromisoformat(str(start)[:10])
    b = end if isinstance(end, date) else date.fromisoformat(str(end)[:10])
    rows = _rows(restaurant_id, db_path) if rows is None else rows
    by_date = _by_date(rows)
    out = {"ok": False, "from": a.isoformat(), "to": b.isoformat(), "from_mdy": _mdy(a), "to_mdy": _mdy(b),
           "begin_date": None, "end_date": None, "categories": {}, "total": None, "missing": []}
    cats = {}
    begin_d = end_d = None
    for c in COUNT_CATEGORIES:
        dates = sorted(d for d, v in by_date.items() if c in v)
        bd = _near(dates, a)
        ed = _near(dates, b, exclude=bd)
        miss = []
        if bd is None:
            miss.append(_missing("opening count", f"no {LABELS[c].lower()} count within {COUNT_TOLERANCE_DAYS} "
                                                  f"days of {_mdy(a)}"))
        if ed is None:
            miss.append(_missing("closing count", f"no {LABELS[c].lower()} count within {COUNT_TOLERANCE_DAYS} "
                                                  f"days of {_mdy(b)}"))
        if bd and ed and ed <= bd:
            miss.append(_missing("a second count", "the opening and closing counts are the same count, so "
                                                   "nothing brackets the span"))
        cats[c] = {"begin": round(float(by_date[bd][c]["value_usd"]), 2) if bd else None,
                   "end": round(float(by_date[ed][c]["value_usd"]), 2) if ed else None,
                   "begin_date": bd, "end_date": ed, "purchases": None, "used": None, "sales": None,
                   "pct": None, "missing": miss}
        if not miss:
            begin_d = bd if begin_d is None else min(begin_d, bd)
            end_d = ed if end_d is None else max(end_d, ed)
    out["categories"] = cats
    measurable = [c for c in COUNT_CATEGORIES if not cats[c]["missing"]]
    if not measurable:
        out["missing"] = [_missing("two counts", f"no two bar counts bracket {_mdy(a)} to {_mdy(b)} "
                                                 f"(each within {COUNT_TOLERANCE_DAYS} days of its edge)")]
        return out
    out["begin_date"], out["end_date"] = begin_d, end_d
    lines, sales = _read_inputs(restaurant_id, db_path, lines, sales)
    # The distributors whose invoices stop inside the widest span read, and
    # the trading days with sales by category, once.
    from bar.pour_cost import _gaps
    lo = (date.fromisoformat(begin_d) + timedelta(days=1)).isoformat()
    gaps = _gaps(lines, lo, end_d) if lines and lo <= end_d else []
    for c in measurable:
        x = cats[c]
        s = (date.fromisoformat(x["begin_date"]) + timedelta(days=1)).isoformat()
        e = x["end_date"]
        if s > e:
            x["missing"].append(_missing("a span", "the counts are on consecutive days with nothing between"))
            continue
        span_lines = [l for l in lines if s <= str(l["invoice_date"])[:10] <= e]
        if not span_lines:
            x["missing"].append(_missing("purchases", f"no distributor invoices dated {_mdy(s)} to {_mdy(e)}"))
        for g in gaps:
            if c in g["categories"] and g["from"] <= e and g["to"] >= s:
                x["missing"].append(_missing("purchases", f"{g['distributor']}'s invoices are missing from "
                                                          f"{g['from_mdy']} to {g['to_mdy']}"))
        trading = _trading_days(restaurant_id, date.fromisoformat(s), date.fromisoformat(e), db_path)
        sold_days = {d for cat in COUNT_CATEGORIES for d, v in (sales.get(cat) or {}).items()
                     if s <= d <= e and v is not None}
        covered = len([d for d in trading if d in sold_days])
        if not trading or covered < SALES_COVERAGE_FLOOR * len(trading):
            x["missing"].append(_missing("sales", f"sales by category on {covered} of {len(trading)} trading days "
                                                  f"from {_mdy(s)} to {_mdy(e)}"))
        bought = round(sum(float(l["net"] or 0) for l in span_lines if l["category"] == c), 2)
        sold = round(sum(float(v or 0) for d, v in (sales.get(c) or {}).items() if s <= d <= e), 2)
        x["purchases"], x["sales"] = bought, sold
        x["used"] = round(x["begin"] + bought - x["end"], 2)
        if x["missing"]:
            continue
        if sold <= 0:
            x["missing"].append(_missing("sales", f"no {LABELS[c].lower()} sales recorded from {_mdy(s)} to {_mdy(e)}"))
            continue
        if x["used"] < 0:
            x["missing"].append(_missing("a consistent count",
                                         "the closing count is more than the opening count plus everything bought, "
                                         "so a delivery is missing or a count is wrong"))
            continue
        pct = round(x["used"] / sold * 100, 1)
        if pct > 100:
            x["missing"].append(_missing("a consistent count",
                                         f"{LABELS[c].lower()} would cost {pct:.0f}% of its sales — a count, a "
                                         "missing delivery or missing sales nights, not a measurement"))
            continue
        x["pct"] = pct
    ok = [c for c in COUNT_CATEGORIES if cats[c]["pct"] is not None]
    if len(ok) == len(COUNT_CATEGORIES):
        t = {k: round(sum(cats[c][k] for c in ok), 2) for k in ("begin", "purchases", "end", "used", "sales")}
        t["pct"] = round(t["used"] / t["sales"] * 100, 1) if t["sales"] > 0 else None
        out["total"] = t
    out["ok"] = bool(ok)
    out["missing"] = [dict(m, category=c) for c in COUNT_CATEGORIES for m in cats[c]["missing"]]
    return out


def _month_end(d):
    return d.replace(day=monthrange(d.year, d.month)[1])


def next_due(last_count, today) -> date:
    """The next month-end count: this month's last day, or next month's
    when this month's is already counted."""
    end = _month_end(today)
    if last_count and date.fromisoformat(last_count) >= end:
        end = _month_end(end + timedelta(days=1))
    return end


def counted(restaurant_id, today=None, db_path=None, lines=None, sales=None) -> dict:
    """The payload's `counted` block: {available, periods [{from, to,
    from_mdy, to_mdy, categories {c: {begin, purchases, end, used, sales,
    pct}}, total, missing}], last_count, last_count_mdy, next_due_mdy,
    missing}. One period per pair of consecutive count dates (newest last,
    at most MAX_PERIODS); `available` when any period measured a category."""
    today = today or date.today()
    rows = _rows(restaurant_id, db_path)
    dates = sorted(_by_date(rows))
    last = dates[-1] if dates else None
    out = {"available": False, "periods": [], "last_count": last, "last_count_mdy": _mdy(last) if last else None,
           "next_due_mdy": _mdy(next_due(last, today)), "missing": []}
    if len(dates) < 2:
        out["missing"] = [_missing("two counts",
                                   "no bar count yet" if not dates else
                                   f"one bar count so far ({_mdy(dates[0])}); pour cost needs the next one to "
                                   "bracket a span")]
        return out
    lines, sales = _read_inputs(restaurant_id, db_path, lines, sales)
    pairs = list(zip(dates, dates[1:]))[-MAX_PERIODS:]
    for a, b in pairs:
        p = true_pour_cost(restaurant_id, a, b, db_path=db_path, lines=lines, sales=sales, rows=rows)
        out["periods"].append({
            "from": a, "to": b, "from_mdy": _mdy(a), "to_mdy": _mdy(b), "ok": p["ok"],
            "categories": {c: {k: p["categories"][c][k] for k in ("begin", "purchases", "end", "used", "sales", "pct")}
                           for c in COUNT_CATEGORIES},
            "total": p["total"], "missing": p["missing"]})
    out["available"] = any(p["ok"] for p in out["periods"])
    if not out["available"]:
        out["missing"] = out["periods"][-1]["missing"]
    return out


def latest_counted(restaurant_id, today=None, window=None, db_path=None, lines=None, sales=None, block=None):
    """The newest counted period that measured at least one category, or
    None — for bar.build / bar.pour_cost (W4: `pour_cost.basis` becomes
    "counted" when this covers the latest window) and Ask/Home (W7/W9).
    With `window` ({from, to} ISO), only a period whose counts sit within
    COUNT_TOLERANCE_DAYS of the window's two edges covers it. `block` is an
    already built `counted()` payload, to avoid a second read."""
    block = block if block is not None else counted(restaurant_id, today=today, db_path=db_path,
                                                     lines=lines, sales=sales)
    for p in reversed(block.get("periods") or []):
        if not p.get("ok"):
            continue
        if window:
            try:
                wf = date.fromisoformat(str(window["from"])[:10])
                wt = date.fromisoformat(str(window["to"])[:10])
            except (KeyError, TypeError, ValueError):
                return None
            if (abs((date.fromisoformat(p["from"]) - wf).days) > COUNT_TOLERANCE_DAYS
                    or abs((date.fromisoformat(p["to"]) - wt).days) > COUNT_TOLERANCE_DAYS):
                continue
        return p
    return None


# ── Back Office's inventory valuation, by email ─────────────────────────────
# UNVERIFIED (10/15/26): we have no sample of Back Office's "inventory
# valuation by category" report. The reader takes a CSV or XLSX whose header
# row (within the first HEADER_SCAN_ROWS rows) names a CATEGORY column and a
# VALUE column, and a count date — a date column, or one date in the rows
# above the header (e.g. "Inventory Date: 10/31/2026", or a period's end
# "10/1/2026 - 10/31/2026"), or in the file's name. Anything else is
# `unrecognized`, never a guess: no date, two different dates, a row that
# reads as two categories, no beer/liquor/wine row, or a negative value.

HEADER_SCAN_ROWS = 15
_CAT_HEAD = re.compile(r"\b(categor(y|ies)|class|group|gl|account|type|department|dept)\b", re.I)
_VALUE_HEAD = re.compile(r"\b(value|valuation)\b", re.I)
_VALUE_HEAD_2 = re.compile(r"\b(extended|ext|total|amount|cost|\$)\b|\$", re.I)
_NOT_VALUE_HEAD = re.compile(r"\b(unit|each|per|qty|quantity|count|par|price|%|percent)\b", re.I)
_DATE_HEAD = re.compile(r"\b(date|as of|period|count(ed)? on)\b", re.I)
_ROW_SKIP = re.compile(r"\b(total|subtotal|grand)\b|non[- ]?alc|\bn/?a\b|\bna bev|soda|mixer|juice|food|coffee|"
                       r"water|paper|suppl", re.I)
_ROW_CAT = {
    "beer": re.compile(r"\b(beer|draft|draught|keg|kegs|seltzer|cider|ale|lager|ipa)\b", re.I),
    "liquor": re.compile(r"\b(liquor|liquors|spirit|spirits|vodka|whiske?y|bourbon|tequila|mezcal|rum|gin|scotch|"
                         r"cordials?|liqueurs?|brandy|cognac)\b", re.I),
    "wine": re.compile(r"\b(wine|wines|champagne|sparkling|prosecco)\b", re.I),
}
_MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RANGE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})\s*(?:-|–|to|through|thru)\s*"
                       r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", re.I)


def _dates_in(text) -> list:
    out = []
    for m in _ISO_RE.finditer(text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in _MDY_RE.finditer(text):
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            out.append(date(y, int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    return out


def _cell_date(v):
    """A date from one cell: an Excel serial number or a written date."""
    if isinstance(v, (int, float)) and 30000 <= float(v) <= 80000:
        return date(1899, 12, 30) + timedelta(days=int(float(v)))
    got = _dates_in(str(v or ""))
    return got[0] if len(got) == 1 else None


def _money_cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


def _grids(filename, content) -> list:
    """[rows] per sheet of a CSV or XLSX file; [] when it is neither."""
    name = str(filename or "").lower()
    if name.endswith(".xlsx") or content[:2] == b"PK":
        from dsr.history_import import read_xlsx, HistoryImportError
        try:
            sheets, _1904 = read_xlsx(content)
        except HistoryImportError:
            return []
        return [rows for _n, rows in sheets]
    if not (name.endswith(".csv") or name.endswith(".txt") or name.endswith(".tsv")):
        return []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    first = text.splitlines()[0] if text.strip() else ""
    delim = "\t" if first.count("\t") > first.count(",") else ","
    return [list(csv.reader(io.StringIO(text), delimiter=delim))]


def parse_valuation(filename, content) -> dict:
    """{ok, count_date, values {c: dollars}, read [{row, category, value}],
    ignored [labels], why}. `ok` False with `why` when the file is not one
    this reader can read without guessing."""
    grids = _grids(filename, content)
    if not grids:
        return {"ok": False, "why": "not a CSV or XLSX file"}
    for rows in grids:
        rows = [[("" if c is None else c) for c in (r or [])] for r in rows]
        head_i = cat_i = val_i = date_i = None
        for i, r in enumerate(rows[:HEADER_SCAN_ROWS]):
            heads = [str(c).strip() for c in r]
            ci = next((j for j, h in enumerate(heads) if h and _CAT_HEAD.search(h)), None)
            vi = next((j for j, h in enumerate(heads)
                       if h and _VALUE_HEAD.search(h) and not _NOT_VALUE_HEAD.search(h)), None)
            if vi is None:
                vi = next((j for j, h in enumerate(heads)
                           if h and _VALUE_HEAD_2.search(h) and not _NOT_VALUE_HEAD.search(h) and j != ci), None)
            if ci is not None and vi is not None and ci != vi:
                head_i, cat_i, val_i = i, ci, vi
                date_i = next((j for j, h in enumerate(heads) if h and _DATE_HEAD.search(h)
                               and j not in (ci, vi)), None)
                break
        if head_i is None:
            continue
        values, read, ignored, row_dates = {}, [], [], set()
        for r in rows[head_i + 1:]:
            label = str(r[cat_i] if cat_i < len(r) else "").strip()
            if not label:
                continue
            hits = [c for c, rx in _ROW_CAT.items() if rx.search(label)]
            if _ROW_SKIP.search(label) or not hits:
                ignored.append(label[:60])
                continue
            if len(hits) > 1:
                return {"ok": False, "why": f"the row '{label[:60]}' reads as more than one of beer, liquor and wine"}
            v = _money_cell(r[val_i] if val_i < len(r) else None)
            if v is None:
                ignored.append(label[:60])
                continue
            if v < 0:
                return {"ok": False, "why": f"the row '{label[:60]}' has a negative value"}
            if date_i is not None and date_i < len(r) and str(r[date_i]).strip():
                d = _cell_date(r[date_i])
                if d is None:
                    return {"ok": False, "why": f"the row '{label[:60]}' has a date that can't be read"}
                row_dates.add(d)
            values[hits[0]] = round(values.get(hits[0], 0.0) + v, 2)
            read.append({"row": label[:60], "category": hits[0], "value": round(v, 2)})
        if not values:
            return {"ok": False, "why": "no beer, liquor or wine row under the category column"}
        how = None
        if row_dates:
            if len(row_dates) > 1:
                return {"ok": False, "why": "more than one count date in one file"}
            count_date = row_dates.pop()
            how = "the date column"
        else:
            above = " ".join(str(c) for r in rows[:head_i] for c in r if str(c).strip())
            ranges = [m.group(2) for m in _RANGE_RE.finditer(above)]
            found = set()
            if ranges:
                found = {d for t in ranges for d in _dates_in(t)}
                how = "the end of the report's period"
            else:
                found = set(_dates_in(above)) or {_cell_date(c) for r in rows[:head_i] for c in r
                                                  if isinstance(c, (int, float))} - {None}
                how = "the report's heading"
            if not found:
                found = set(_dates_in(str(filename or "")))
                how = "the file's name"
            if len(found) != 1:
                return {"ok": False, "why": ("no count date in the file" if not found
                                             else "more than one date in the file's heading")}
            count_date = found.pop()
        return {"ok": True, "count_date": count_date.isoformat(), "values": values, "read": read,
                "ignored": ignored[:30], "date_from": how}
    return {"ok": False, "why": "no header row naming a category column and a value column"}


def read_valuation_file(restaurant_id, filename, content, intake_id=None, db_path=None) -> dict:
    """email_intake's reader for kind 'backoffice_valuation' (READERS).
    Returns {"status": stored | unrecognized, "detail", ...}: stored only
    when a count date and at least one of beer / liquor / wine were read
    and saved (source 'backoffice', method 'import'); a value the owner
    typed for that date is kept, never replaced."""
    try:
        got = parse_valuation(filename, content or b"")
    except Exception as e:                       # a damaged file is unrecognized, never a crash
        got = {"ok": False, "why": f"the file couldn't be read ({type(e).__name__})"}
    if not got.get("ok"):
        return {"status": "unrecognized", "detail": got.get("why")}
    try:
        res = save_count(restaurant_id, got["count_date"], got["values"], user_id=None, source="backoffice",
                         method="import", note=f"Back Office valuation ({str(filename or '')[:80]})",
                         db_path=db_path, today=_restaurant_today(restaurant_id, db_path))
    except ValueError as e:
        return {"status": "unrecognized", "detail": e.args[0] if e.args else "not saved"}
    return {"status": "stored" if res["saved"] else "kept_owner", "count_date": got["count_date"],
            "saved": res["saved"], "kept_owner": res["kept_owner"], "date_from": got["date_from"],
            "read": got["read"], "ignored": got["ignored"],
            "detail": f"bar count for {_mdy(got['count_date'])} from {got['date_from']}"}


def _restaurant_today(restaurant_id, db_path=None):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(int(restaurant_id), naive=True).date()
    except Exception:
        return date.today()


# ── the month-end reminder ──────────────────────────────────────────────────

def _counts(attempted=0, ok=0, failed=0, skipped=0, hit_bound=False, **extra):
    out = {"attempted": int(attempted), "ok": int(ok), "failed": int(failed), "skipped": int(skipped),
           "hit_bound": bool(hit_bound)}
    out.update(extra)
    return out


def _restaurants_with_invoices(db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT restaurant_id FROM fintech_invoices WHERE restaurant_id IS NOT NULL"
                            ).fetchall()
    finally:
        conn.close()
    return sorted(int(r["restaurant_id"]) for r in rows)


def run_count_reminders(db_path=None, now=None) -> dict:
    """Hourly: on each restaurant's own last day of the month, from
    REMINDER_HOUR local, one "Count the bar tonight" — for restaurants with
    distributor invoices, not paused or lapsed, that have not already
    counted all three categories today. Claimed once per restaurant and day
    (scheduler.local_due), delivered through strategy_jobs._reach (push to
    the app, email otherwise; its briefing budget, audience — the logins
    who can read Food Cost — and quiet hours). Refused off the production
    scheduler (scheduling_allowed). Bounded and resumable (a cursor in
    job_cursors). Returns the standard counts."""
    import models as _m
    import scheduler as _sched
    ids = _restaurants_with_invoices(db_path)
    if not _sched.scheduling_allowed():
        return _counts(skipped=len(ids), reason="not the production scheduler")
    tally = {"attempted": 0, "failed": 0, "reminded": 0, "skipped": 0}

    def _one(rid):
        r = _m.get_restaurant(rid, db_path=db_path) if db_path else _m.get_restaurant(rid)
        if r is None or (getattr(r, "billing_status", None) or "trial").lower() in _m.BLOCKED_BILLING_STATES:
            return
        from time_utils import restaurant_now
        local = now or restaurant_now(r, naive=True)
        if local.day != monthrange(local.year, local.month)[1]:
            return
        if not _sched.local_due(r, REMINDER_HOUR, until=REMINDER_UNTIL, claim_key="bar_count_reminders",
                                now_local=local):
            return
        tally["attempted"] += 1
        today = local.date().isoformat()
        done = {row["category"] for row in _rows(rid, db_path) if str(row["count_date"])[:10] == today}
        if set(COUNT_CATEGORIES) <= done:
            tally["skipped"] += 1
            return
        from strategy_jobs import _reach
        reached = _reach(rid, REMINDER_ALERT_TYPE, "Count the bar tonight",
                         "Month end: count beer, liquor and wine after close and enter the shelf value by category, "
                         "so Cavnar AI can measure pour cost for the month.",
                         {"tab": "inventory", "nav": "bar"}, db_path or _m.DB_PATH)
        if reached:
            tally["reminded"] += 1

    def _run(rid):
        try:
            _one(rid)
        except Exception:
            tally["failed"] += 1
            raise

    _done, ran_out = _sched.resumable_sweep(REMINDERS_CURSOR_KEY, ids, _run, REMINDERS_MAX_SECONDS, workers=1,
                                            job="bar_count_reminders")
    return _counts(tally["attempted"], tally["attempted"] - tally["failed"] - tally["skipped"], tally["failed"],
                   tally["skipped"],
                   hit_bound=ran_out, reminded=tally["reminded"])
