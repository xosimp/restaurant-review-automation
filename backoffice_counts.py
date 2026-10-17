"""backoffice_counts.py — waste measured count to count, from Back Office.

Back Office (restacct) keeps inventory as weekly periods (Wednesday to
Tuesday at EJ's): each submitted period is a dollar value per inventory GL
account (Produce, Dairy, Meat, … Beer, Wine, Liquor), built from count
sheets per storage location. There is no API and no count export, so the
submitted periods are read from Back Office's own pages
(scripts/backoffice_import.py takes the JSON) and stored here.

Waste between two counts, food only:

    used by the kitchen  = food on hand at the first count
                         + food bought between them (Back Office invoices)
                         − food on hand at the second count
    what the recipes say = the recipes' cost % of the dishes sold in the
                           period (backoffice_recipes.period_summaries,
                           priced as of the period's end) × the POS's food
                           sales in the period
    waste                = used − what the recipes say

So "waste" is everything the kitchen used that the recipes do not account
for: spoilage, over-portioning, staff meals, and items a recipe is missing.
It is never a figure without two counts that both carry food — one count
is a stock value only, a bar-only count is no kitchen count at all
(FC-CALC-06), and the trend never charts either as a week of $0. It is
withheld, with the reason, when the priced dishes are under 60% of the
period's food sales (their recipe % would stand in for the rest:
FC-CALC-07), when the recipes call for more than the kitchen used (a count,
an invoice or a recipe is off: FC-CALC-19), when food sales miss more than
a fifth of the nights the restaurant trades (FC-CALC-16), and when a count
lacks a food account the counts around it carry (ING-17).

The periods are materialised in `bo_waste_periods` (SCALE-01): computed
once from one read of the recipe book, the invoice lines and the POS, kept
with a stamp of every input (`inputs_stamp`: counts, invoices and their
lines, the chart of accounts, vendor categories, the recipe book, the
nightly report's food sales, the POS archive), and recomputed only when
that stamp moves. A request reads rows.
"""
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

import backoffice_ap as bo

# A period whose food sales cover fewer of the nights the restaurant trades
# than this is not measured: the sales side would be short and the waste
# would read too high. Trading nights are the weekdays with food sales
# anywhere on file (a restaurant closed Sunday and Monday is not short for
# them: FC-CALC-16, the rule cogs._covers_trading_days keeps for net sales).
MIN_NIGHTS_SHARE = 0.8
# The share of the period's food sales the fully priced dishes must cover
# before their recipe % may stand for all of it (FC-CALC-07).
MIN_COVERAGE = 0.6
# Weeks of food sales before the first count read for the trading weekdays.
REFERENCE_DAYS = 56
# Bumped when the computation changes, so stored periods are recomputed.
WASTE_VERSION = "2"


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def init_backoffice_counts(db_path=None):
    """The materialised waste periods (created at boot, models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_waste_periods (
            restaurant_id INTEGER NOT NULL,
            period_end TEXT NOT NULL,
            stamp TEXT NOT NULL,
            payload TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, period_end))""")
        conn.commit()
    finally:
        conn.close()


def _capture(e, restaurant_id, job, db_path=None):
    try:
        import ops
        ops.capture(e, job=job, restaurant_id=restaurant_id, db_path=db_path)
    except Exception:
        pass


def _iso(s):
    """'10/20/2026' or '2026-10-20' → '2026-10-20'; None when neither."""
    s = str(s or "").strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return None


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def category(gl_name) -> str:
    """An inventory account's category by its name ('MEAT INVENTORY' →
    food, 'BEER INVENTORY' → beer), the Chart of Accounts' own words."""
    return bo.gl_category("purchase", re.sub(r"\bINVENTORY\b", "", str(gl_name or ""), flags=re.I))


def _coa(restaurant_id, db_path=None) -> dict:
    try:
        return bo.gl_map(restaurant_id, db_path)
    except Exception:
        return {}


def _account(gl, scraped_name, stored_category, coa) -> tuple:
    """(category, label) of one count row: by its GL number through the
    chart of accounts first, then the name read off the page (ING-17: a
    page whose name cell was not read no longer drops the stock from food)."""
    a = coa.get(str(gl or "").strip())
    if a and a.get("name"):
        return category(a["name"]), a["name"]
    if scraped_name:
        return category(scraped_name), scraped_name
    return stored_category, str(gl or "")


def save_counts(restaurant_id, book, db_path=None) -> dict:
    """Store submitted inventories read from Back Office: book =
    {"inventory_counts": [{start, end, status, gls: [{gl, name, amount}],
    items: [{location, item, gl_name, unit, qty, total}]}]}. A period that
    is not submitted (started, late) is skipped — a half-entered count is
    not a count. A period read again replaces what was stored for it."""
    periods, skipped = 0, []
    gl_rows = item_rows = 0
    coa = _coa(restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        for p in (book or {}).get("inventory_counts") or []:
            end, start = _iso(p.get("end")), _iso(p.get("start"))
            if not end:
                skipped.append({"period": p.get("end"), "why": "no period end date"})
                continue
            if str(p.get("status") or "").strip().lower() != "submitted":
                skipped.append({"period": end, "why": f"status {p.get('status') or 'unknown'}, not submitted"})
                continue
            gls = [g for g in p.get("gls") or [] if _num(g.get("amount")) is not None and (g.get("gl") or g.get("name"))]
            if not gls:
                skipped.append({"period": end, "why": "no account values"})
                continue
            conn.execute("DELETE FROM bo_inventory_counts WHERE restaurant_id=? AND period_end=?", (int(restaurant_id), end))
            conn.execute("DELETE FROM bo_inventory_count_items WHERE restaurant_id=? AND period_end=?",
                         (int(restaurant_id), end))
            for g in gls:
                gl = str(g.get("gl") or g.get("name")).strip()
                cat, _label = _account(g.get("gl"), g.get("name"), category(g.get("name")), coa)
                conn.execute("INSERT INTO bo_inventory_counts (restaurant_id, period_end, period_start, gl, gl_name, "
                             "category, amount, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (int(restaurant_id), end, start, gl, str(g.get("name") or "").strip(), cat,
                              _num(g["amount"]), _now()))
                gl_rows += 1
            for i, it in enumerate(p.get("items") or []):
                if not str(it.get("item") or "").strip():
                    continue
                conn.execute("INSERT INTO bo_inventory_count_items (restaurant_id, period_end, location, line_no, item, "
                             "gl_name, unit, qty, total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (int(restaurant_id), end, str(it.get("location") or "").strip() or "—", i,
                              str(it["item"]).strip(), it.get("gl_name"), it.get("unit"), _num(it.get("qty")),
                              _num(it.get("total"))))
                item_rows += 1
            periods += 1
        conn.commit()
    finally:
        conn.close()
    return {"periods": periods, "accounts": gl_rows, "items": item_rows, "skipped": skipped}


def counts(restaurant_id, db_path=None, food_only=True) -> list:
    """[{end, start, food, by_category, has_food, food_gls, gl_gap}] oldest
    first — one per submitted period on file. With `food_only` (the
    default: every caller pairs kitchen counts) only the counts that carry a
    food account: a bar-only week is no kitchen count, never $0 of food on
    hand (FC-CALC-06). `gl_gap` names the food accounts this count lacks
    that the counts around it carry (ING-17), else None."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT period_end, period_start, gl, gl_name, category, amount FROM bo_inventory_counts "
                            "WHERE restaurant_id=? ORDER BY period_end", (int(restaurant_id),)).fetchall()
    except Exception as e:
        # The table is created at boot; a failure here is not "no counts"
        # to an owner without someone hearing of it (SCALE-11).
        _capture(e, restaurant_id, "backoffice_counts.counts", db_path)
        return []
    finally:
        conn.close()
    coa = _coa(restaurant_id, db_path) if rows else {}
    by = {}
    for r in rows:
        c = by.setdefault(r["period_end"], {"end": r["period_end"], "start": r["period_start"],
                                            "by_category": defaultdict(float), "_gls": {}})
        cat, label = _account(r["gl"], r["gl_name"], r["category"], coa)
        c["by_category"][cat] += float(r["amount"] or 0)
        if cat == "food":
            c["_gls"][str(r["gl"])] = label
    out = []
    for end in sorted(by):
        c = by[end]
        c["by_category"] = {k: round(v, 2) for k, v in c["by_category"].items()}
        c["has_food"] = bool(c["_gls"])
        c["food"] = round(c["by_category"].get("food", 0.0), 2) if c["has_food"] else None
        out.append(c)
    food = [c for c in out if c["has_food"]]
    for i, c in enumerate(food):
        prev = food[i - 1]["_gls"] if i > 0 else None
        nxt = food[i + 1]["_gls"] if i + 1 < len(food) else None
        if prev is not None and nxt is not None:
            want = set(prev) & set(nxt)
        elif prev is not None:
            want = set(prev) & (set(food[i - 2]["_gls"]) if i >= 2 else set(prev))
        elif nxt is not None:
            want = set(nxt) & (set(food[i + 2]["_gls"]) if i + 2 < len(food) else set(nxt))
        else:
            want = set()
        gone = sorted(want - set(c["_gls"]))
        labels = {**(prev or {}), **(nxt or {})}
        c["gl_gap"] = [labels.get(g) or g for g in gone] or None
    for c in out:
        c["food_gls"] = sorted(c.pop("_gls"))
        c.setdefault("gl_gap", None)
    return food if food_only else out


# ── the inputs stamp ──────────────────────────────────────────────────────

# (table, aggregates) per restaurant: one indexed read each. The POS lines
# are stood for by their archive day rows (a night archived or re-archived
# writes one) and the newest night on file — never a scan of the lines.
_STAMP_SOURCES = (
    ("bo_inventory_counts", "COUNT(*), MAX(updated_at), SUM(amount)"),
    ("bo_invoices", "COUNT(*), MAX(updated_at), SUM(amount)"),
    ("bo_invoice_lines", "COUNT(*), MAX(updated_at), SUM(ext_amount)"),
    ("bo_gl_accounts", "COUNT(*), MAX(updated_at)"),
    ("bo_vendor_categories", "COUNT(*), MAX(set_at)"),
    ("bo_recipe_books", "MAX(loaded_at)"),
    ("fintech_invoices", "COUNT(*), MAX(updated_at)"),
    ("pos_archive_days", "COUNT(*), MAX(archived_at), SUM(lines), SUM(restated)"),
    ("pos_ticket_lines", "MAX(business_date)"),
)
# The nightly report's sales categories, a range on its (restaurant, metric,
# date) index (never LIKE).
_DSR_SQL = ("SELECT COUNT(*), MAX(updated_at), SUM(value), MAX(business_date) FROM dsr_metrics "
            "WHERE restaurant_id=? AND metric >= 'sales.cat:' AND metric < 'sales.cat;'")


def _read(conn, sql, args):
    try:
        row = conn.execute(sql, args).fetchone()
    except Exception:
        try:
            conn.rollback()      # Postgres: a failed statement aborts the transaction
        except Exception:
            pass
        return None
    return tuple(row) if row is not None else None


def inputs_stamp(restaurant_id, db_path=None) -> str:
    """A digest of everything a waste period is computed from (module note)."""
    rid = int(restaurant_id)
    parts = [WASTE_VERSION]
    conn = get_conn(db_path)
    try:
        for table, aggs in _STAMP_SOURCES:
            parts.append(repr(_read(conn, f"SELECT {aggs} FROM {table} WHERE restaurant_id=?", (rid,))))
        parts.append(repr(_read(conn, _DSR_SQL, (rid,))))
    finally:
        conn.close()
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


def _stored(restaurant_id, stamp, ends, db_path=None):
    """The kept periods when every one is from this stamp and they are the
    periods the counts make; else None."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT period_end, stamp, payload FROM bo_waste_periods WHERE restaurant_id=? "
                            "ORDER BY period_end", (int(restaurant_id),)).fetchall()
    except Exception as e:
        _capture(e, restaurant_id, "backoffice_counts._stored", db_path)
        return None
    finally:
        conn.close()
    if [r["period_end"] for r in rows] != ends or any(r["stamp"] != stamp for r in rows):
        return None
    return [json.loads(r["payload"]) for r in rows]


def _store(restaurant_id, stamp, periods, db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM bo_waste_periods WHERE restaurant_id=?", (int(restaurant_id),))
        now = _now()
        for p in periods:
            conn.execute("INSERT INTO bo_waste_periods (restaurant_id, period_end, stamp, payload, computed_at) "
                         "VALUES (?,?,?,?,?)", (int(restaurant_id), p["end"], stamp, json.dumps(p), now))
        conn.commit()
    except Exception as e:
        # Kept periods are a cache of a pure computation: the caller still
        # gets the figures; the next read computes them again.
        try:
            conn.rollback()
        except Exception:
            pass
        _capture(e, restaurant_id, "backoffice_counts._store", db_path)
    finally:
        conn.close()


# ── waste between counts ─────────────────────────────────────────────────

def waste_periods(restaurant_id, today=None, db_path=None, since=None, until=None, limit=None) -> list:
    """Each period between two consecutive food counts: {start, end,
    opening, closing, bought, used, food_sales, nights, trading_nights,
    days, recipe_pct, recipe_sales, coverage_pct, expected, waste, rate,
    used_pct} — or the same with `why_not` and waste None when it cannot be
    measured honestly. Read from `bo_waste_periods` while its stamp holds,
    computed and kept otherwise. `since` / `until` (ISO dates) bound the
    period ends returned, `limit` keeps the newest N. `today` is accepted
    for older callers; a period is priced as of its own end, never today."""
    cs = counts(restaurant_id, db_path)
    if len(cs) < 2:
        return []
    ends = [c["end"] for c in cs[1:]]
    stamp = inputs_stamp(restaurant_id, db_path)
    out = _stored(restaurant_id, stamp, ends, db_path)
    if out is None:
        out = _compute(restaurant_id, cs, db_path)
        _store(restaurant_id, stamp, out, db_path)
    if since:
        out = [p for p in out if p["end"] >= str(since)[:10]]
    if until:
        out = [p for p in out if p["end"] <= str(until)[:10]]
    if limit:
        out = out[-int(limit):]
    return out


def _compute(restaurant_id, cs, db_path=None) -> list:
    """Every period between consecutive food counts, from one read of each
    input (module note)."""
    import backoffice_recipes as br
    from time_utils import mdy
    first, last = cs[0]["end"], cs[-1]["end"]
    food = [e for e in bo.entries(restaurant_id, start=first, end=last, db_path=db_path) if e["category"] == "food"]
    ref_from = (date.fromisoformat(first) - timedelta(days=REFERENCE_DAYS)).isoformat()
    sales = bo._food_sales(restaurant_id, ref_from, last, db_path)
    trading = {date.fromisoformat(d).weekday() for d, v in sales.items() if v > 0}
    out, need = [], []
    for a, b in zip(cs, cs[1:]):
        start = (date.fromisoformat(a["end"]) + timedelta(days=1)).isoformat()
        end = b["end"]
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
        days = (d1 - d0).days + 1
        trading_nights = sum(1 for k in range(days) if (d0 + timedelta(days=k)).weekday() in trading)
        bought = round(sum(float(e["amount"]) for e in food if start <= e["date"] <= end), 2)
        fs = round(sum(v for d, v in sales.items() if start <= d <= end), 2)
        nights = sum(1 for d, v in sales.items() if start <= d <= end and v > 0)
        used = round(a["food"] + bought - b["food"], 2)
        p = {"start": start, "end": end, "days": days, "opening": a["food"], "closing": b["food"],
             "bought": bought, "used": used, "food_sales": fs, "nights": nights, "trading_nights": trading_nights,
             "recipe_pct": None, "recipe_sales": None, "coverage_pct": None, "expected": None, "waste": None,
             "rate": None, "used_pct": round(100.0 * used / fs, 1) if fs > 0 else None}
        gap = [(c, c["gl_gap"]) for c in (a, b) if c.get("gl_gap")]
        if gap:
            c, names = gap[0]
            p["why_not"] = (f"the {mdy(c['end'])} count has no {', '.join(names)} — the counts around it do, so "
                            "a count was not read whole")
        elif fs <= 0 or not trading_nights or nights < MIN_NIGHTS_SHARE * trading_nights:
            p["why_not"] = f"food sales cover {nights} of the {trading_nights} nights the restaurant trades"
        elif used <= 0:
            p["why_not"] = "the counts and invoices leave nothing used — a count or an invoice is missing"
        else:
            need.append(len(out))
        out.append(p)
    sums = br.period_summaries(restaurant_id, [(date.fromisoformat(out[i]["start"]), date.fromisoformat(out[i]["end"]))
                                               for i in need], db_path) if need else None
    for j, i in enumerate(need):
        p = out[i]
        s = (sums or [None] * len(need))[j] or {}
        if s.get("cost_pct") is None:
            p["why_not"] = "no fully priced recipe sold in the period"
            continue
        p["recipe_pct"], p["recipe_sales"] = s["cost_pct"], s["covered_sales"]
        cov = s["covered_sales"] / p["food_sales"]
        p["coverage_pct"] = round(100.0 * cov, 1)
        if cov < MIN_COVERAGE:
            p["why_not"] = (f"the fully priced dishes are {p['coverage_pct']:g}% of the period's food sales — "
                            f"waste needs recipes for at least {MIN_COVERAGE * 100:.0f}%")
            continue
        expected = round(p["food_sales"] * s["cost_pct"] / 100.0, 2)
        p["expected"] = expected
        waste = round(p["used"] - expected, 2)
        if waste < 0:
            p["why_not"] = (f"the recipes call for ${-waste:,.0f} more than the kitchen used — a count, an invoice "
                            "or a recipe is off")
            p["shortfall"] = round(-waste, 2)
            continue
        p["waste"] = waste
        p["rate"] = round(100.0 * waste / p["bought"], 1) if p["bought"] > 0 else None
    return out


def waste_weeks(restaurant_id, today=None, db_path=None, since=None, until=None) -> list:
    """The waste trend's weeks (waste_trend.load_waste_history's shape),
    one per food count, oldest first. The first count, or a period that
    could not be measured, carries its stock value with waste None: food
    cost % brackets a window with it, the trend never charts it. A period
    longer or shorter than a week is put per week (waste and purchases ×
    7 / days: FC-CALC-13) — a count skipped in Back Office no longer
    charts two weeks of waste as one week's spike; `days` and
    `period_waste` carry the period as measured."""
    from waste_trend import _label
    cs = counts(restaurant_id, db_path)
    if not cs:
        return []
    lo, hi = (str(since)[:10] if since else None), (str(until)[:10] if until else None)
    measured = {p["end"]: p for p in waste_periods(restaurant_id, db_path=db_path, since=lo, until=hi)}
    out = []
    for c in cs:
        if (lo and c["end"] < lo) or (hi and c["end"] > hi):
            continue
        we = date.fromisoformat(c["end"])
        p = measured.get(c["end"])
        ws = date.fromisoformat(p["start"]) if p else we - timedelta(days=6)
        days = p["days"] if p else 7
        f = 7.0 / days
        waste = p["waste"] if p else None
        out.append({
            "week_end": we.isoformat(), "start": ws.isoformat(), "label": _label(we), "start_label": _label(ws),
            "waste": round(waste * f, 2) if waste is not None else None, "rate": p["rate"] if p else None,
            "inv_value": c["food"] or None,
            "purchased": round(p["bought"] * f, 2) if p and p["bought"] > 0 else None,
            "days": days, "per_week": days != 7, "period_waste": waste,
            "top_items": [], "categories": None, "has_items": False,
            "source": "back_office_count", "count": p,
        })
    return out
