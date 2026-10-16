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
                           period (backoffice_recipes.plates, today's
                           prices) × the POS's food sales in the period
    waste                = used − what the recipes say

So "waste" is everything the kitchen used that the recipes do not account
for: spoilage, over-portioning, staff meals, and items a recipe is missing.
It is never a figure without two counts — one count is a stock value only,
and the trend never charts it as a week of $0.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

import backoffice_ap as bo

# A period whose food sales cover fewer nights than this is not measured:
# the sales side would be short and the waste would read too high.
MIN_NIGHTS_SHARE = 0.8


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


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


def save_counts(restaurant_id, book, db_path=None) -> dict:
    """Store submitted inventories read from Back Office: book =
    {"inventory_counts": [{start, end, status, gls: [{gl, name, amount}],
    items: [{location, item, gl_name, unit, qty, total}]}]}. A period that
    is not submitted (started, late) is skipped — a half-entered count is
    not a count. A period read again replaces what was stored for it."""
    periods, skipped = 0, []
    gl_rows = item_rows = 0
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
                conn.execute("INSERT INTO bo_inventory_counts (restaurant_id, period_end, period_start, gl, gl_name, "
                             "category, amount, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (int(restaurant_id), end, start, str(g.get("gl") or g.get("name")).strip(),
                              str(g.get("name") or "").strip(), category(g.get("name")), _num(g["amount"]), _now()))
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


def counts(restaurant_id, db_path=None) -> list:
    """[{end, start, food, by_category}] oldest first — one per submitted
    period on file."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT period_end, period_start, category, amount FROM bo_inventory_counts "
                            "WHERE restaurant_id=? ORDER BY period_end", (int(restaurant_id),)).fetchall()
    except Exception:
        return []                 # the table is created at boot; a bare test database may predate it
    finally:
        conn.close()
    by = {}
    for r in rows:
        c = by.setdefault(r["period_end"], {"end": r["period_end"], "start": r["period_start"],
                                            "by_category": defaultdict(float)})
        c["by_category"][r["category"]] += float(r["amount"] or 0)
    out = []
    for end in sorted(by):
        c = by[end]
        c["by_category"] = {k: round(v, 2) for k, v in c["by_category"].items()}
        c["food"] = round(c["by_category"].get("food", 0.0), 2)
        out.append(c)
    return out


def waste_periods(restaurant_id, today=None, db_path=None) -> list:
    """Each period between two consecutive counts: {start, end, opening,
    closing, bought, used, food_sales, nights, days, recipe_pct,
    recipe_sales, expected, waste, rate, used_pct} — or the same with
    `why_not` and waste None when it cannot be measured honestly."""
    import backoffice_recipes as br
    cs = counts(restaurant_id, db_path)
    if len(cs) < 2:
        return []
    food = [e for e in bo.entries(restaurant_id, start=cs[0]["end"], end=cs[-1]["end"], db_path=db_path)
            if e["category"] == "food"]
    sales = bo._food_sales(restaurant_id, cs[0]["end"], cs[-1]["end"], db_path)
    out = []
    for a, b in zip(cs, cs[1:]):
        start = (date.fromisoformat(a["end"]) + timedelta(days=1)).isoformat()
        end = b["end"]
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        bought = round(sum(float(e["amount"]) for e in food if start <= e["date"] <= end), 2)
        fs = round(sum(v for d, v in sales.items() if start <= d <= end), 2)
        nights = sum(1 for d, v in sales.items() if start <= d <= end and v > 0)
        used = round(a["food"] + bought - b["food"], 2)
        p = {"start": start, "end": end, "days": days, "opening": a["food"], "closing": b["food"],
             "bought": bought, "used": used, "food_sales": fs, "nights": nights,
             "recipe_pct": None, "recipe_sales": None, "expected": None, "waste": None, "rate": None,
             "used_pct": round(100.0 * used / fs, 1) if fs > 0 else None}
        if fs <= 0 or nights < MIN_NIGHTS_SHARE * days:
            p["why_not"] = f"food sales cover {nights} of {days} nights"
        elif used <= 0:
            p["why_not"] = "the counts and invoices leave nothing used — a count or an invoice is missing"
        else:
            pl = br.plates(restaurant_id, today=today, db_path=db_path,
                           start=date.fromisoformat(start), end=date.fromisoformat(end))
            s = (pl.get("summary") or {}) if pl.get("available") else {}
            if s.get("cost_pct") is None:
                p["why_not"] = "no fully priced recipe sold in the period"
            else:
                p["recipe_pct"], p["recipe_sales"] = s["cost_pct"], s["covered_sales"]
                p["expected"] = round(fs * s["cost_pct"] / 100.0, 2)
                p["waste"] = round(used - p["expected"], 2)
                p["rate"] = round(100.0 * p["waste"] / bought, 1) if bought > 0 else None
        out.append(p)
    return out


def waste_weeks(restaurant_id, today=None, db_path=None) -> list:
    """The waste trend's weeks (waste_trend.load_waste_history's shape),
    one per count, oldest first. The first count, or a period that could
    not be measured, carries its stock value with waste None: food cost %
    brackets a window with it, the trend never charts it."""
    from waste_trend import _label
    cs = counts(restaurant_id, db_path)
    if not cs:
        return []
    measured = {p["end"]: p for p in waste_periods(restaurant_id, today=today, db_path=db_path)}
    out = []
    for c in cs:
        we = date.fromisoformat(c["end"])
        p = measured.get(c["end"])
        ws = date.fromisoformat(p["start"]) if p else we - timedelta(days=6)
        out.append({
            "week_end": we.isoformat(), "start": ws.isoformat(), "label": _label(we), "start_label": _label(ws),
            "waste": p["waste"] if p else None, "rate": p["rate"] if p else None,
            "inv_value": c["food"] or None, "purchased": p["bought"] if p and p["bought"] > 0 else None,
            "top_items": [], "categories": None, "has_items": False,
            "source": "back_office_count", "count": p,
        })
    return out
