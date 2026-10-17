"""backoffice_read.py — Food Cost's read for a kitchen on Back Office.

A restaurant whose food comes from Back Office (backoffice_ap.feed_state)
has no inventory CSV and no POS count, so the inventory analysis is the
sample pantry and its model read refuses (inventory.SAMPLE_DATA_NOTICE).
Its real figures are Back Office's invoices, the nightly report's food
sales, the recipe book and — from its first submitted inventory — counts.
This writes the read from those, deterministically: every sentence a
measured figure, no model call (the Bar's read is built the same way).

The read covers the restaurant's last complete fiscal week
(restaurants.fiscal_week_start_dow, else week_start_day — EJ's runs
Wednesday to Tuesday), never a trailing seven days ending today.
"""
from collections import defaultdict
from datetime import date, timedelta

# An account's week is named only when it moved this much against its
# own four-week weekly average, and by at least this many dollars.
MOVE_PCT = 20.0
MOVE_USD = 500.0


def fiscal_week(restaurant, today=None):
    """(start, end) of the last complete week in the restaurant's own week."""
    today = today or date.today()
    dow = getattr(restaurant, "fiscal_week_start_dow", None)
    if dow is None:
        dow = getattr(restaurant, "week_start_day", 0) or 0
    this_start = today - timedelta(days=(today.weekday() - int(dow)) % 7)
    start = this_start - timedelta(days=7)
    return start, start + timedelta(days=6)


def _money(v):
    return f"${v:,.0f}"


def _pct(v):
    return f"{v:.1f}%"


def food_read(restaurant_id, restaurant=None, today=None, db_path=None) -> dict | None:
    """{text, week_start, week_end, start_mdy, end_mdy, facts} for a kitchen
    on Back Office, or None when there is no Back Office food to read."""
    import backoffice_ap as bo
    import backoffice_counts as bc
    import backoffice_recipes as br
    from time_utils import mdy
    if restaurant is None:
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id)
    if today is None:
        from models import _restaurant_today
        today = _restaurant_today(restaurant_id)
    start, end = fiscal_week(restaurant, today)
    s, e = start.isoformat(), end.isoformat()
    first = (start - timedelta(days=28)).isoformat()
    food = [x for x in bo.entries(restaurant_id, start=first, end=e, db_path=db_path) if x["category"] == "food"]
    if not food:
        return None
    sales = bo._food_sales(restaurant_id, first, e, db_path)

    def window(a, b):
        p = sum(float(x["amount"]) for x in food if a <= x["date"] <= b)
        f = sum(v for d, v in sales.items() if a <= d <= b)
        return p, f

    bought, fsales = window(s, e)
    pb, pf = window((start - timedelta(days=7)).isoformat(), (start - timedelta(days=1)).isoformat())
    fb, ff = window((end - timedelta(days=27)).isoformat(), e)
    feed = bo.feed_state(restaurant_id, db_path)
    facts = {"bought": round(bought, 2), "food_sales": round(fsales, 2), "prev_bought": round(pb, 2),
             "prev_food_sales": round(pf, 2), "four_week_bought": round(fb, 2), "four_week_sales": round(ff, 2),
             "invoices_through": feed.get("invoices_through")}
    week = f"{mdy(s)} – {mdy(e)}"
    out = {"week_start": s, "week_end": e, "start_mdy": mdy(s), "end_mdy": mdy(e), "facts": facts}
    if fsales <= 0:
        out["text"] = (f"No food sales are on file for {week} yet, so there is nothing to set the "
                       f"{_money(bought)} of food bought that week against.")
        return out
    pct = 100.0 * bought / fsales
    facts["pct"] = round(pct, 1)
    # A headline and labelled points, never one paragraph (owner, 10/16/26:
    # "too big of a paragraph"): what moved, then each figure on its own row.
    lead = f"{_pct(pct)} bought vs sold"
    points = [("Bought", f"{_money(bought)} of food against {_money(fsales)} of food sales")]
    if pf > 0:
        prev = 100.0 * pb / pf
        facts["prev_pct"] = round(prev, 1)
        if abs(pct - prev) >= 0.5:
            lead += f" — {'up' if pct > prev else 'down'} from {_pct(prev)} the week before"
        else:
            lead += " — level with the week before"
    if ff > 0:
        facts["four_week_pct"] = round(100.0 * fb / ff, 1)
        # The figure and its end date; why four weeks is steadier was a
        # second clause on every read (text round 10/17/26).
        points.append(("Four weeks", f"{_pct(100.0 * fb / ff)} to {mdy(e)}"))
    pl = br.plates(restaurant_id, today=today, db_path=db_path, start=start, end=end)
    rp = ((pl.get("summary") or {}).get("cost_pct")) if pl.get("available") else None
    measured = [p for p in bc.waste_periods(restaurant_id, today=today, db_path=db_path)
                if p.get("waste") is not None and s <= p["end"] <= e]
    if measured:
        m = measured[-1]
        facts.update(counted_waste=m["waste"], counted_used=m["used"], recipe_pct=m["recipe_pct"])
        points.append(("Counted", f"{mdy(m['start'])} – {mdy(m['end'])}: {_money(m['used'])} of food used, "
                                  f"{_pct(m['used_pct'])} of food sales"))
        points.append(("Beyond recipes", f"{_money(m['waste'])} more than the recipes call for "
                                         f"({_pct(m['recipe_pct'])} by recipe)"))
    elif rp is not None:
        gap = pct - rp
        facts.update(recipe_pct=rp, gap_usd=round(fsales * gap / 100.0, 2))
        points.append(("By recipe", f"{_pct(rp)} — what the dishes sold should have used"))
        if gap > 0:
            points.append(("Not in a recipe", f"about {_money(fsales * gap / 100.0)} — waste, portions, staff meals "
                                              "or stock on the shelf"))
        if not bc.counts(restaurant_id, db_path):
            points.append(("Next", "weekly Back Office counts split shelf stock from waste"))
    # The account that moved most against its own weekly average of the four
    # weeks before — over the weeks whose invoices are split by account
    # (their lines on file), never a week whose food sits in "Not split yet".
    split = [x["date"] for x in food if x.get("category_source") == "line"]
    prior = [(start - timedelta(days=7 * k)) for k in range(1, 5)]
    prior = [w for w in prior if split and w.isoformat() >= min(split)]
    by_week, avg = defaultdict(float), defaultdict(float)
    for x in food:
        name = bo._account_label(x.get("gl_name") or "Food (by vendor)")
        if s <= x["date"] <= e:
            by_week[name] += float(x["amount"])
        elif any(w.isoformat() <= x["date"] <= (w + timedelta(days=6)).isoformat() for w in prior):
            avg[name] += float(x["amount"]) / len(prior)
    if len(prior) < 2:
        avg.clear()
    moves = sorted(((n, v, avg.get(n, 0.0)) for n, v in by_week.items()
                    if n != "Not split yet" and avg.get(n, 0.0) > 0 and abs(v - avg[n]) >= MOVE_USD
                    and abs(v - avg[n]) / avg[n] * 100 >= MOVE_PCT),
                   key=lambda m: -abs(m[1] - m[2]))
    if moves:
        n, v, a = moves[0]
        facts["mover"] = {"account": n, "week": round(v, 2), "prior_avg": round(a, 2), "prior_weeks": len(prior)}
        points.append((n, f"{_money(v)}, {abs(v - a) / a * 100:.0f}% {'above' if v > a else 'below'} its "
                           f"weekly average of the {len(prior)} weeks before"))
    if feed.get("invoices_through") and feed["invoices_through"] < e:
        points.append(("Invoices", f"on file through {mdy(feed['invoices_through'])} — the week's purchases "
                                   "may be short"))
    out["lead"], out["points"] = lead + ".", [{"label": k, "text": v} for k, v in points]
    # The phone and the cache read plain text: the headline, then a line per point.
    out["text"] = out["lead"] + "\n" + "\n".join(f"• {k}: {v}." for k, v in points)
    return out


def food_read_html(read) -> str:
    """The web's markup for a read: the headline, then one labelled row per
    point (`.ai-pts`, DESIGN_SYSTEM → AI read). Every value is escaped."""
    from markupsafe import escape
    if not read.get("points"):
        return '<p style="margin:0;line-height:1.7">' + str(escape(read.get("text") or "")) + "</p>"
    rows = "".join(f'<div class="r"><span class="k">{escape(p["label"])}</span><span class="v">{escape(p["text"])}</span></div>'
                   for p in read["points"])
    return f'<p class="ai-lead">{escape(read["lead"])}</p><div class="ai-pts">{rows}</div>'
