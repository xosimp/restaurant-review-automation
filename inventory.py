"""
inventory.py — Food waste analysis + Claude-powered ordering recommendations
"""
import os, csv, json, math
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from ai_utils import create_with_retry, extract_text

# An explicit timeout: the SDK default let a hung call hold a request worker
# for as long as the connection stayed open, on a route a page load blocks on.
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=45.0)

# Category-specific waste tolerance — fresh produce/herbs naturally run
# higher waste (wilting, trim loss) than proteins/dairy, so a flat 20%
# threshold either buries real problems in low-waste categories or flags
# normal spoilage in high-waste ones as if it were a mistake.
_WASTE_TOLERANCE_PCT = {
    "produce": 28,
    "bakery":  25,
    "protein": 15,
    "dairy":   15,
    "beverage": 20,
    "pantry":  20,
}
_DEFAULT_WASTE_TOLERANCE_PCT = 20
# Weeks in a month, exactly — 52 / 12. Not 4.3, not 4.33.
WEEKS_PER_MONTH = 52.0 / 12.0
RECOVERABLE_BASIS = ("Only waste above each category's tolerance band counts — produce 28%, bakery 25%, "
                     "beverage and pantry 20%, protein and dairy 15% of the last order. Waste inside the band "
                     "is normal trim and spoilage and is never counted. Summed per item from the POS-synced "
                     "count and projected at 52/12 weeks a month.")

# Ingredient keywords relevant to each upcoming holiday/event — shared by
# get_claude_insights (narrative "heads-up" text) and analyse_inventory
# (actual suggested_order_qty scaling), so the two never drift apart.
_HOLIDAY_ITEM_KEYWORDS = {
    "valentine": ["salmon", "filet", "lobster", "shrimp", "chocolate", "cream", "butter"],
    "mother": ["salmon", "filet", "lobster", "shrimp", "cream", "herbs", "asparagus"],
    "father": ["prime rib", "steak", "beef", "ribs", "lobster"],
    "thanksgiving": ["turkey", "potato", "cream", "butter", "herbs", "onion"],
    "christmas": ["prime rib", "beef", "salmon", "cream", "butter", "herbs"],
    "fourth of july": ["beef", "chicken", "ribs", "corn", "potato"],
    "memorial": ["beef", "chicken", "ribs", "potato"],
    "labor day": ["beef", "chicken", "ribs", "potato"],
    "new year": ["salmon", "lobster", "shrimp", "cream", "butter", "champagne"],
    "st. patrick": ["beef", "potato", "cabbage", "onion"],
}
# Peak bump for an item tied to an event, applied in full only when the event
# is inside the window this order actually covers.
_EVENT_SCALE_FACTOR = 1.4
# An order covers roughly this many days (1.5x par plus three days of usage).
# Beyond it, scaling up for an event means buying perishables for a week that
# hasn't arrived.
_EVENT_FULL_SCALE_DAYS = 7
# Past this, the event is someone else's order to place.
_EVENT_NO_SCALE_DAYS = 21


def _event_scale(days_away):
    """How much to scale an order for an event `days_away` from now.

    The full 1.4x used to apply to any matching holiday inside a 30-day
    window, so an order covering about three days of usage was inflated 40%
    for something four weeks out — and again the next week, and the week
    after. Full strength inside the order's own coverage window, tapering to
    nothing by three weeks, and unchanged when the date can't be read.
    """
    if days_away is None:
        return _EVENT_SCALE_FACTOR
    if days_away <= _EVENT_FULL_SCALE_DAYS:
        return _EVENT_SCALE_FACTOR
    if days_away >= _EVENT_NO_SCALE_DAYS:
        return 1.0
    span = float(_EVENT_NO_SCALE_DAYS - _EVENT_FULL_SCALE_DAYS)
    remaining = (_EVENT_NO_SCALE_DAYS - days_away) / span
    return 1.0 + (_EVENT_SCALE_FACTOR - 1.0) * remaining


def _days_until_relevant_holiday(upcoming_holidays: str, today) -> int:
    """Days until the soonest holiday named in the string, or None.

    marketing.get_upcoming_holidays already formats each entry with its date —
    "Valentine's Day (Feb 14)" — so proximity is readable here without
    changing that function's contract or asking it a second question.
    """
    import re as _re_h
    if not upcoming_holidays:
        return None
    soonest = None
    for entry in upcoming_holidays.split(", "):
        m = _re_h.search(r"\(([A-Z][a-z]{2}) (\d{1,2})\)", entry)
        if not m:
            continue
        for year in (today.year, today.year + 1):
            try:
                when = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%b %d %Y").date()
            except ValueError:
                continue
            delta = (when - today).days
            if delta >= 0 and (soonest is None or delta < soonest):
                soonest = delta
            break
    return soonest

# Fri/Sat/Sun usage multiplier vs. a flat Mon-Thu baseline — most full-service
# restaurants see a real weekend demand surge that a single flat average masks.
#
# Normalised so the week's multipliers AVERAGE to 1.0. The raw shape below
# means (1+1+1+1+1.3+1.5+1.15)/7 = 1.136, so a simulation driven by a true
# 7-day average consumed ~13.6% more per week than the average it came from —
# a systematic bias toward "you'll run out sooner", and therefore toward
# ordering more than needed, in every projection the module makes.
_WEEKEND_SHAPE = {4: 1.3, 5: 1.5, 6: 1.15}  # Mon=0 ... Sun=6
_WEEKEND_MEAN = sum(_WEEKEND_SHAPE.get(d, 1.0) for d in range(7)) / 7.0
_WEEKEND_USAGE_MULTIPLIER = {d: v / _WEEKEND_MEAN for d, v in _WEEKEND_SHAPE.items()}
_WEEKDAY_BASE_MULTIPLIER = 1.0 / _WEEKEND_MEAN

_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _holiday_relevant_keywords(upcoming_holidays: str) -> set:
    """Ingredient keywords relevant to any holiday named in an
    upcoming-holidays string (see marketing.get_upcoming_holidays)."""
    relevant = set()
    if not upcoming_holidays:
        return relevant
    for holiday_str in upcoming_holidays.split(", "):
        h_lower = holiday_str.lower()
        for keyword, ingredients in _HOLIDAY_ITEM_KEYWORDS.items():
            if keyword in h_lower:
                relevant.update(ingredients)
    return relevant


def days_until_next_delivery(delivery_days: str, today=None):
    """Days from today until this restaurant's next scheduled delivery.
    delivery_days is a comma-separated list of weekday abbreviations/names
    (e.g. "Mon,Thu" or "Monday, Thursday"). Returns None when not
    configured or unparseable — callers should fall back to flat
    calendar-day thresholds in that case."""
    if not delivery_days:
        return None
    today = today or datetime.now(ZoneInfo('America/Chicago')).date()
    wanted = {d.strip()[:3].title() for d in delivery_days.split(",") if d.strip()}
    wanted_idx = {_WEEKDAY_ABBR.index(w) for w in wanted if w in _WEEKDAY_ABBR}
    if not wanted_idx:
        return None
    today_idx = today.weekday()  # Monday=0
    for offset in range(0, 8):
        if (today_idx + offset) % 7 in wanted_idx:
            return offset
    return None


def _simulate_days_remaining(current_stock: float, avg_daily_usage: float, today=None) -> float:
    """Days until current_stock is depleted, weighting Fri/Sat/Sun usage
    higher than the flat weekly average instead of a single flat division —
    a restaurant heading into a busy weekend runs out sooner than a flat
    avg_daily_usage implies. Falls back to identical output as the old flat
    division for date ranges with no weekend in them."""
    if avg_daily_usage <= 0:
        return 99.0
    today = today or datetime.now(ZoneInfo('America/Chicago')).date()
    remaining = current_stock
    for day_offset in range(0, 30):
        d = today + timedelta(days=day_offset)
        usage = avg_daily_usage * _WEEKEND_USAGE_MULTIPLIER.get(d.weekday(), _WEEKDAY_BASE_MULTIPLIER)
        if remaining <= usage:
            return round(day_offset + max(0.0, remaining / usage), 1)
        remaining -= usage
    return 30.0


def load_inventory(path: str = "sample_inventory.csv",
                   csv_string: str = None) -> list[dict]:
    """Load inventory from a CSV string (client data) or bundled sample."""
    import io
    if csv_string:
        rows = list(csv.DictReader(io.StringIO(csv_string)))
    else:
        _SAMPLE = """item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week
Romaine Lettuce,Produce,20,28,2.5,3.5,25,8.0
Chicken Breast,Protein,30,22,5.8,6.0,30,3.5
Salmon Fillet,Protein,15,18,12.5,2.5,15,2.0
Ground Beef 80/20,Protein,25,32,4.2,4.0,25,4.5
Heavy Cream,Dairy,12,9,3.8,1.8,12,1.5
Butter Unsalted,Dairy,10,14,4.5,1.5,10,0.5
Parmesan Cheese,Dairy,8,5,8.2,1.2,8,0.8
Roma Tomatoes,Produce,15,22,1.8,2.8,20,6.5
Fresh Garlic,Produce,5,7,3.2,0.8,5,0.3
Yellow Onions,Produce,10,13,1.2,1.5,10,1.2
Olive Oil Extra Virgin,Pantry,6,8,14.5,0.9,6,0.2
Pasta Rigatoni,Pantry,15,19,2.8,2.2,15,1.8
Bread Rolls,Bakery,60,45,0.45,12.0,60,15.0
Russet Potatoes,Produce,20,16,0.8,3.5,20,4.0
Baby Spinach,Produce,8,11,4.2,1.4,8,3.5
White Wine Chardonnay,Beverage,12,15,8.5,1.8,12,0.0
Lemons,Produce,10,7,0.6,1.5,10,0.8
Fresh Herbs Mix,Produce,4,6,5.5,0.7,4,2.0
Beef Stock,Pantry,8,10,4.8,1.2,8,0.5
Shrimp 16/20,Protein,10,8,14.2,1.6,10,1.2"""
        try:
            rows = list(csv.DictReader(io.StringIO(_SAMPLE)))
        except Exception:
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                return []
    for r in rows:
        r["par_level"]      = float(r["par_level"])
        r["current_stock"]  = float(r["current_stock"])
        r["unit_cost"]      = float(r["unit_cost"])
        r["avg_daily_usage"]= float(r["avg_daily_usage"])
        r["last_order_qty"] = float(r["last_order_qty"])
        r["waste_last_week"]= float(r["waste_last_week"])
        r.setdefault("unit", "")  # unit label optional (e.g. "lbs", "cases")
        # Supplier case/pack size, optional — used to round suggested_order_qty
        # up to an actionable number instead of a raw formula output.
        r["case_size"] = float(r["case_size"]) if r.get("case_size") not in (None, "") else 1.0
    return rows


def analyse_inventory(items: list[dict], delivery_days: str = None,
                      upcoming_holidays: str = None, today=None,
                      purchases_window: float = None, counted_from: str = None,
                      counted_to: str = None) -> dict:
    """Compute waste, overstock, and reorder flags.

    delivery_days: optional comma-separated weekday abbreviations (e.g.
        "Mon,Thu") — this restaurant's actual supplier delivery schedule.
        When set, critical_low/reorder_soon are judged against days until
        the next delivery instead of flat calendar-day cutoffs: 2 days of
        stock is fine the day before a delivery and dangerous the day
        after one. Falls back to the flat 2/4-day cutoffs when omitted.
    upcoming_holidays: optional comma-separated holiday/event names (see
        marketing.get_upcoming_holidays) — scales suggested_order_qty up
        for ingredients tied to a matching event.
    today: optional date override for deterministic testing; defaults to
        the real current date (America/Chicago).
    purchases_window: dollars actually received over the same 7 days the
        waste figure covers (cogs.purchases_in_window). When given it is the
        benchmark's denominator; without it the fallback is the sum of each
        item's last_order_qty, which is the LAST order whenever it happened.
        A restaurant ordering fortnightly therefore had a denominator
        covering two weeks against a numerator covering one, and its waste
        rate read roughly half of the truth — the figure that drives the
        Excellent/Concerning label, the AI's "waste rate vs industry" line
        and Home's "waste is under control" win.
    counted_from / counted_to: the real first and last count dates behind
        these figures. The window label used to be hardcoded to "today minus
        six days" regardless of when anything was actually counted, so a
        three-week-old count was presented as the last seven days.
    """
    today = today or datetime.now(ZoneInfo('America/Chicago')).date()
    delivery_offset = days_until_next_delivery(delivery_days, today)
    holiday_keywords = _holiday_relevant_keywords(upcoming_holidays)
    holiday_days_away = _days_until_relevant_holiday(upcoming_holidays, today)

    waste_items   = []
    overstock     = []
    reorder_soon     = []
    critical_low     = []
    order_reduction  = []  # items where suggested qty is meaningfully less than last order

    total_waste_cost  = 0.0
    total_stock_value = 0.0
    total_recoverable_week = 0.0

    for item in items:
        # Weekend-weighted depletion simulation instead of a flat division —
        # a restaurant heading into a busy Fri/Sat/Sun runs out sooner than
        # a single flat avg_daily_usage would suggest.
        days_remaining  = _simulate_days_remaining(item["current_stock"], item["avg_daily_usage"], today)
        waste_cost      = item["waste_last_week"] * item["unit_cost"]
        # Category-specific overstock thresholds (industry standard)
        # Proteins/dairy: flag at 110% of par (perishable, high cost)
        # Produce: flag at 120% of par
        # All others (dry, beverage): flag at 130% of par
        category = (item.get("category") or "").lower()
        if category in ("protein", "dairy"):
            overstock_multiplier = 1.10
        elif category == "produce":
            overstock_multiplier = 1.20
        else:
            overstock_multiplier = 1.30
        overstock_units = max(0, item["current_stock"] - item["par_level"] * overstock_multiplier)
        overstock_cost  = overstock_units * item["unit_cost"]
        stock_value     = item["current_stock"] * item["unit_cost"]
        waste_pct       = (item["waste_last_week"] / item["last_order_qty"] * 100
                           if item["last_order_qty"] > 0 else 0)

        total_waste_cost  += waste_cost
        total_stock_value += stock_value

        item["days_remaining"] = round(days_remaining, 1)
        item["waste_cost"]     = round(waste_cost, 2)
        item["overstock_cost"] = round(overstock_cost, 2)
        item["waste_pct"]      = round(waste_pct, 1)

        # Suggested order quantity: target 1.5x par, cover 3 days usage, adjusted for waste rate
        # If wasting a lot, pull the order quantity down proportionally
        waste_adj     = min(0.95, max(0.60, 1.0 - (waste_pct / 100) * 0.5))
        raw_qty       = (item["par_level"] * 1.5) - item["current_stock"] + (item["avg_daily_usage"] * 3)

        # Event scaling — bump quantity for items tied to a holiday/event in
        # the next 30 days, so the actual order number reflects the surge,
        # not just the AI's narrative text about it.
        # Scaled by how close the event actually is. The full 1.4x used to
        # apply to any matching holiday in the next 30 days, so an order
        # covering about three days of usage was inflated 40% for something
        # four weeks away — and then again the following week, and the week
        # after that.
        event_scaled = bool(holiday_keywords) and any(kw in item["item"].lower() for kw in holiday_keywords)
        if event_scaled:
            raw_qty *= _event_scale(holiday_days_away)

        suggested_qty = max(0.0, raw_qty * waste_adj)

        # Case-size/MOQ rounding — round up to the nearest full supplier
        # case so the number is directly actionable, not a raw formula
        # output the owner still has to do math on.
        case_size = item.get("case_size") or 1
        try:
            case_size = float(case_size)
        except (TypeError, ValueError):
            case_size = 1.0
        if case_size <= 0:
            case_size = 1.0
        if suggested_qty > 0 and case_size > 1:
            suggested_qty = math.ceil(suggested_qty / case_size) * case_size
        suggested_qty = int(round(suggested_qty))

        savings_vs_last = round((item["last_order_qty"] - suggested_qty) * item["unit_cost"], 2)
        item["suggested_order_qty"] = suggested_qty
        item["savings_vs_last"]     = savings_vs_last  # positive = save money, negative = need more
        item["event_scaled"]        = event_scaled

        # Per-category waste tolerance — see _WASTE_TOLERANCE_PCT above.
        tolerance = _WASTE_TOLERANCE_PCT.get(category, _DEFAULT_WASTE_TOLERANCE_PCT)
        item["waste_tolerance_pct"] = tolerance
        # Recoverable dollars are the slice of this item's waste that sits
        # above its category's tolerance band — the part ordering and par
        # changes can actually remove. An item wasting 40% of its order in
        # a 28% band has 12/40 of its waste dollars recoverable; an item
        # inside the band contributes nothing. This replaced a flat 65%
        # of all waste (Sep 2026), which assumed the same share of every
        # item's waste was avoidable regardless of what the count said.
        if waste_pct > tolerance and waste_cost > 0:
            recoverable_cost = waste_cost * (waste_pct - tolerance) / waste_pct
        else:
            recoverable_cost = 0.0
        item["recoverable_cost"] = round(recoverable_cost, 2)
        total_recoverable_week += recoverable_cost
        if waste_pct > tolerance:
            waste_items.append(item)
        if overstock_units > 0:
            overstock.append(item)

        if delivery_offset is not None:
            # Judge urgency against when the truck actually comes, not a
            # flat day count.
            margin = days_remaining - delivery_offset
            item["delivery_margin_days"] = round(margin, 1)
            if margin < 0 and item["current_stock"] < item["par_level"]:
                critical_low.append(item)
            elif margin <= 1.5:
                reorder_soon.append(item)
            # Flag items with meaningful savings potential even if stock isn't critically low
            elif savings_vs_last >= 5.0:
                order_reduction.append(item)
        else:
            item["delivery_margin_days"] = None
            if days_remaining <= 2 and item["current_stock"] < item["par_level"]:
                critical_low.append(item)
            elif days_remaining <= 4:
                reorder_soon.append(item)
            # Flag items with meaningful savings potential even if stock isn't critically low
            # Threshold: saves $5+ vs last order AND not already in critical/reorder lists
            elif savings_vs_last >= 5.0:
                order_reduction.append(item)

    waste_items      = sorted(waste_items,      key=lambda x: x["waste_cost"],     reverse=True)
    overstock        = sorted(overstock,        key=lambda x: x["overstock_cost"],  reverse=True)
    critical_low     = sorted(critical_low,     key=lambda x: x["days_remaining"])
    order_reduction  = sorted(order_reduction,  key=lambda x: x["savings_vs_last"], reverse=True)

    # Rounded once, then everything else derives from the rounded figure.
    # monthly was rounded to whole dollars and annual was computed from the
    # UNROUNDED monthly, so the two numbers shown side by side on the iOS
    # hero disagreed: an owner multiplying the monthly figure by twelve got a
    # different answer from the annual figure printed beside it.
    monthly_waste_projection = round(total_waste_cost * WEEKS_PER_MONTH, 2)
    annual_waste_projection  = round(monthly_waste_projection * 12, 2)
    recoverable = round(total_recoverable_week * WEEKS_PER_MONTH)
    annual_recoverable = round(recoverable * 12, 2)

    # Industry benchmark: waste cost as % of what was actually purchased over
    # the SAME period the waste covers.
    # 4-5% = industry target | 5-8% = above average | 8-15% = concerning | >15% = serious
    #
    # purchases_window comes from the receiving ledger (cogs.purchases_in_window)
    # and is the correct denominator. The fallback below is the sum of each
    # item's last order, which is a different period per item and usually a
    # longer one than the numerator — see the docstring.
    if purchases_window is not None and purchases_window > 0:
        total_purchased = float(purchases_window)
        purchases_basis = "received over the same 7 days, from the delivery ledger"
    else:
        total_purchased = sum(i["last_order_qty"] * i["unit_cost"] for i in items)
        purchases_basis = ("each item's last recorded order — no delivery ledger for this "
                           "window, so the period behind this denominator varies by item")
    # A benchmark needs a denominator, and nothing else. This used to also
    # require total_waste_cost > 0, so a restaurant that wasted nothing all
    # week — the best possible outcome — was told "Upload inventory data to
    # see benchmark", exactly as if it had no data at all.
    _has_benchmark  = total_purchased > 0
    waste_rate_pct  = round((total_waste_cost / total_purchased * 100) if _has_benchmark else 0, 1)
    # Benchmark rating. The tone is a semantic name, not a hex: these values
    # crossed a module boundary into home_brief, which tested them against
    # the string "green" and therefore never once matched.
    if not _has_benchmark:
        benchmark_label  = "—"
        benchmark_tone   = "neutral"
        benchmark_detail = "Upload inventory data to see benchmark"
    elif waste_rate_pct <= 4:
        benchmark_label  = "Excellent"
        benchmark_tone   = "good"
        benchmark_detail = ("No waste recorded this week" if total_waste_cost <= 0
                            else "At or below the 4% industry target")
    elif waste_rate_pct <= 6:
        benchmark_label  = "On Track"
        benchmark_tone   = "good"
        benchmark_detail = "Near the 4-5% industry target"
    elif waste_rate_pct <= 10:
        benchmark_label  = "Above Average"
        benchmark_tone   = "warn"
        benchmark_detail = f"Industry target is 4-5% — you're at {waste_rate_pct}%"
    elif waste_rate_pct <= 15:
        benchmark_label  = "Concerning"
        benchmark_tone   = "bad"
        benchmark_detail = f"Industry target is 4-5% — you're at {waste_rate_pct}%"
    else:
        benchmark_label  = "Needs Attention"
        benchmark_tone   = "bad"
        benchmark_detail = f"Industry target is 4-5% — you're at {waste_rate_pct}%"

    now_chi = datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None)
    # Two different windows, kept apart on purpose.
    #
    # week_start/week_end label the period the WASTE figures cover, and for a
    # ledger restaurant that genuinely is the trailing seven days —
    # recompute_rollups sums waste over exactly today-6..today. Relabelling
    # this with the count dates (the first version of this fix) was wrong:
    # it renamed a correct seven-day waste window after a single count day.
    #
    # counted_from/counted_to are a different fact: when stock was last
    # physically verified. That governs how much the CURRENT STOCK figures —
    # days remaining, reorder urgency, overstock — can be trusted, and it was
    # missing entirely, which is why a three-week-old count could drive a
    # confident order list while home_brief separately warned it was stale.
    def _parse(d):
        try:
            return datetime.strptime(str(d)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    week_end_dt   = now_chi
    week_start_dt = now_chi - timedelta(days=6)
    counted_to_dt = _parse(counted_to)
    counts_are_real = bool(counted_to_dt)
    # Days since the newest count. 0 when nothing is on file — reported
    # alongside window_from_counts=False so an absent count is never read as
    # a fresh one.
    window_age_days = max(0, (now_chi.date() - counted_to_dt.date()).days) if counted_to_dt else 0

    def fmt(dt): return dt.strftime("%-m/%-d/%y")

    return {
        "total_waste_cost_week":    round(total_waste_cost, 2),
        "monthly_waste_projection": monthly_waste_projection,
        "annual_waste_projection":  annual_waste_projection,
        "recoverable_monthly":      recoverable,
        "annual_recoverable":       annual_recoverable,
        "recoverable_weekly":       round(total_recoverable_week, 2),
        "recoverable_basis":        RECOVERABLE_BASIS,
        "projection_basis":         ("one week extrapolated at "
                                     f"{WEEKS_PER_MONTH:.2f} weeks a month — a single week, "
                                     "not a trend"),
        "waste_rate_pct":           waste_rate_pct,
        "purchases_basis":          purchases_basis,
        "total_purchased":          round(total_purchased, 2),
        # Count provenance — a different fact from the waste window above.
        # `counted_to` governs how far the CURRENT STOCK figures (days
        # remaining, reorder urgency, overstock) can be trusted; the waste
        # window is always the trailing seven days.
        "window_from_counts":       counts_are_real,
        "window_age_days":          window_age_days,
        "counted_from":             counted_from,
        "counted_to":               counted_to,
        "stock_basis":              (
            f"Stock figures rest on counts taken {window_age_days} day"
            f"{'' if window_age_days == 1 else 's'} ago."
            if counts_are_real else
            "No count dates on file — stock figures are as supplied, with no way to say how "
            "current they are."),
        "benchmark_label":          benchmark_label,
        "benchmark_tone":           benchmark_tone,
        "benchmark_detail":         benchmark_detail,
        "total_stock_value":     round(total_stock_value, 2),
        "waste_items":    waste_items[:6],
        "overstock":      overstock[:5],
        "critical_low":     critical_low[:4],
        "reorder_soon":     reorder_soon[:6],
        "order_reduction":  order_reduction[:6],
        "total_items":    len(items),
        "week_start":     fmt(week_start_dt),
        "week_end":       fmt(week_end_dt),
        "last_updated":   fmt(now_chi),
    }


def compute_item_trends(restaurant_id: int, current_items: list, db_path: str = None) -> dict:
    """
    Compare current items against up to 8 weeks of stored history.
    Returns big_8, price_alerts (>5% spike this week), trend_alerts (3+ weeks up), trend_context for Claude.
    """
    import json as _jt
    from models import get_conn as _gc_t, DB_PATH as _DB
    _db = db_path or _DB

    # Big 8: items with highest unit_cost × avg_daily_usage (biggest weekly spend impact)
    scored = sorted(
        current_items,
        key=lambda x: (x.get("unit_cost") or 0) * (x.get("avg_daily_usage") or 0),
        reverse=True,
    )
    big_8_names = {i["item"] for i in scored[:8]}
    current_prices = {i["item"]: (i.get("unit_cost") or 0) for i in current_items}

    try:
        conn = _gc_t(_db)
        # week_end < date('now','-1 day') — same guard get_claude_insights()
        # already uses for its own wow_context comparison just below.
        # get_claude_insights() auto-writes a snapshot for TODAY on every
        # insight view (not just once a week), so without this, the first
        # Analytics view of the day compares the live current price
        # against a same-day snapshot of itself — always equal, so a real
        # spike/trend from a fresh price update would silently stop being
        # detectable for the rest of that day.
        #
        # Fetch generously and bucket by ISO week below. Taking the last 8
        # ROWS meant that for an owner who opens the page daily, "8 weeks of
        # history" was 8 days — and the output says "up 12% over 3 weeks" in
        # so many words. waste_trend.load_waste_history fixed exactly this for
        # the chart; the fix never reached here.
        rows = conn.execute("""
            SELECT week_end, items_json FROM inventory_history
            WHERE restaurant_id=? AND items_json IS NOT NULL AND week_end < date('now','-1 day')
            ORDER BY week_end DESC LIMIT 80
        """, (restaurant_id,)).fetchall()
        conn.close()
    except Exception:
        rows = []

    # One entry per ISO week, newest snapshot in each week standing for it.
    from datetime import date as _date_ci
    weekly = {}
    for row in rows:                      # newest → oldest
        try:
            key = _date_ci.fromisoformat(row["week_end"]).isocalendar()[:2]
        except Exception:
            continue
        if key not in weekly:             # first seen is the newest in that week
            weekly[key] = row
    rows = [weekly[k] for k in sorted(weekly)][-8:]   # oldest → newest, 8 weeks

    # Build per-item price history: {name: [price_oldest, ..., price_newest]}
    history = {}
    for row in rows:
        try:
            for hi in _jt.loads(row["items_json"] or "[]"):
                name = hi.get("item")
                price = hi.get("unit_cost") or 0
                if name:
                    history.setdefault(name, []).append(price)
        except Exception:
            pass

    price_alerts, trend_alerts, trend_lines = [], [], []

    for name, hist in history.items():
        if name not in current_prices:
            continue
        curr = current_prices[name]

        # Week-over-week spike: >5% increase vs last stored week
        if hist:
            prev = hist[-1]
            # Drops as well as spikes. Only increases were ever detected, so
            # an ingredient getting materially cheaper — a real buying
            # opportunity, and a signal that a past spike has passed — never
            # reached the owner.
            if prev > 0 and curr != prev:
                pct = round((curr - prev) / prev * 100, 1)
                if abs(pct) >= 5:
                    price_alerts.append({
                        "item": name,
                        "old_price": prev,
                        "new_price": curr,
                        "change_pct": pct,
                        "is_big_8": name in big_8_names,
                    })

        # Consecutive upward trend: 3+ weeks including current
        all_prices = hist + [curr]
        if len(all_prices) >= 3:
            consec = 0
            for i in range(len(all_prices) - 1, 0, -1):
                if all_prices[i] > all_prices[i - 1]:
                    consec += 1
                else:
                    break
            if consec >= 3:
                base = all_prices[-consec - 1] if len(all_prices) > consec else all_prices[0]
                total_pct = round((curr - base) / base * 100, 1) if base > 0 else 0
                trend_alerts.append({
                    "item": name,
                    "weeks": consec,
                    "start_price": base,
                    "current_price": curr,
                    "total_change_pct": total_pct,
                    "is_big_8": name in big_8_names,
                })
                trend_lines.append(
                    f"{name} up {total_pct}% over {consec} weeks (${base:.2f} → ${curr:.2f})"
                )
        elif len(all_prices) >= 2 and name in big_8_names:
            base = all_prices[0]
            if base > 0:
                pct = round((curr - base) / base * 100, 1)
                direction = "up" if curr > base else "down"
                trend_lines.append(
                    f"{name} (Big 8) {direction} {abs(pct)}% over {len(all_prices)} weeks"
                )

    trend_context = ""
    if trend_lines:
        trend_context = (
            "\n\nMulti-week price trends:\n"
            + "\n".join(f"- {l}" for l in trend_lines[:6])
            + "\nIf any Big 8 item has been rising 3+ weeks, call it out and connect it to "
            "food cost trajectory or menu pricing."
        )

    return {
        "big_8": scored[:8],
        "big_8_names": list(big_8_names),
        "price_alerts": price_alerts,
        "trend_alerts": trend_alerts,
        "trend_context": trend_context,
        "weeks_of_history": len(rows),
    }


def build_price_watch(trends: dict) -> list:
    """Unifies price_alerts (single-week 5%+ spike) and trend_alerts (3+
    consecutive weeks rising) from compute_item_trends() into one
    display-ready list, deduped by item — a sustained trend is more
    informative than a same-week spike for the same ingredient, so the
    trend classification wins when an item has both. Each entry carries a
    ready-to-show suggested action so the client (web/iOS) doesn't have to
    duplicate this judgment call."""
    watch = {}
    for a in trends.get("price_alerts", []):
        rose = (a["change_pct"] or 0) > 0
        watch[a["item"]] = {
            "item": a["item"], "kind": "spike" if rose else "drop",
            "change_pct": a["change_pct"],
            "weeks": None, "old_price": a["old_price"], "new_price": a["new_price"],
            "is_big_8": a["is_big_8"],
            "action_hint": ("One-week spike — worth checking this week's invoice for an error."
                            if rose else
                            "Price dropped — a good week to buy ahead if it keeps."),
        }
    for a in trends.get("trend_alerts", []):
        hint = ("Sustained rise — consider a menu price adjustment on dishes using this, or shop suppliers."
                if a["is_big_8"] else "Sustained rise — worth keeping an eye on.")
        watch[a["item"]] = {
            "item": a["item"], "kind": "trend", "change_pct": a["total_change_pct"],
            "weeks": a["weeks"], "old_price": a["start_price"], "new_price": a["current_price"],
            "is_big_8": a["is_big_8"], "action_hint": hint,
        }
    return sorted(watch.values(), key=lambda x: abs(x["change_pct"]), reverse=True)


def _supported_savings_block(analysis: dict) -> str:
    """The dollar figures a recommendation is allowed to quote, each tied to
    the item and the mechanism that produces it.

    analyse_inventory already computes all three: recoverable_cost (waste
    above the category tolerance band), overstock_cost (capital sitting over
    par) and savings_vs_last (ordering the suggested quantity instead of
    repeating the last order). None of them used to reach the prompt, so the
    model was asked for savings figures it had no way to derive.
    """
    lines = []
    for x in (analysis.get("waste_items") or [])[:4]:
        rec = float(x.get("recoverable_cost") or 0)
        if rec > 0:
            lines.append(f"- {x['item']}: ${rec:,.2f}/week recoverable — waste above its "
                         f"{x.get('waste_tolerance_pct')}% tolerance band")
    for x in (analysis.get("order_reduction") or [])[:4]:
        sav = float(x.get("savings_vs_last") or 0)
        if sav > 0:
            lines.append(f"- {x['item']}: ${sav:,.2f} saved by ordering "
                         f"{x.get('suggested_order_qty')} instead of repeating the last order of "
                         f"{x.get('last_order_qty')}")
    for x in (analysis.get("overstock") or [])[:3]:
        ov = float(x.get("overstock_cost") or 0)
        if ov > 0:
            lines.append(f"- {x['item']}: ${ov:,.2f} of capital sitting above par "
                         f"(this is stock on hand, not a weekly saving)")
    if not lines:
        return "- None. The data does not support a specific dollar saving this week."
    return "\n".join(lines)


SAMPLE_DATA_NOTICE = (
    "Food Cost is showing example data — this restaurant has no inventory "
    "connected yet. Connect Toast or upload a count to see your own numbers. "
    "No analysis is generated for example data."
)


def get_claude_insights(analysis: dict, owner_name: str = None, restaurant_name: str = None,
                        restaurant_id: int = None, items: list = None,
                        is_live: bool = True) -> str:
    """Claude narrates inventory findings like a food cost consultant.

    is_live gates the call. load_inventory_for_restaurant falls back to a
    built-in sample pantry when a restaurant has no ingredients and no
    inventory_csv, and every caller used to discard that flag (`items,
    _is_live = ...`) — so a restaurant with nothing connected got a confident
    food-cost analysis of invented rows, phrased exactly like a real one, and
    could cut a real produce order on the strength of it. Example data does
    not get narrated.
    """
    if not is_live:
        return SAMPLE_DATA_NOTICE
    name_line = f"Owner name: {owner_name}" if owner_name else ""
    rest_line  = f"Restaurant: {restaurant_name}" if restaurant_name else ""
    wow_context = ""
    menu_context = ""
    trend_context = ""
    big_8_context = ""
    forecast_next_week = None
    forecast_monthly = None
    if restaurant_id:
        # Week over week, from the ISO-week series the trend card already
        # computes — not from "the previous snapshot row".
        #
        # week_end is TODAY's date, written on every render, so the old query
        # ("week_end < date('now','-1 day') ORDER BY week_end DESC LIMIT 1")
        # returned whenever the owner last opened the tab. An owner who looked
        # on Monday and again on Wednesday was shown a two-day delta labelled
        # "vs last week" — and the dollar forecast was computed from it. There
        # were two week-over-week implementations over one table; this is now
        # the one, and it carries a confidence the other never had.
        try:
            from waste_trend import build_waste_trend as _bwt
            _stats = (_bwt(restaurant_id) or {}).get("stats") or {}
            _d, _p = _stats.get("wow_delta"), _stats.get("wow_pct")
            if _d is not None:
                direction = "UP" if _d > 0 else "DOWN"
                wow_context = (f"\n- vs last week (ISO weeks): waste is {direction} "
                               f"${abs(_d):,.2f}"
                               + (f" ({abs(_p):g}%)" if _p is not None else "")
                               + " — mention this trend")
                # The forecast dollars are computed here rather than left to
                # the model, and only when the direction is one the series can
                # actually bear. A single week extrapolated is not a forecast;
                # waste_trend's own confidence and anomaly detection decide
                # whether there is a trend to project at all.
                _conf = _stats.get("confidence")
                _anoms = {a["index"] for a in (_stats.get("anomalies") or [])}
                _latest_is_anomaly = (_stats.get("weeks") or 0) - 1 in _anoms
                if _conf in ("high", "medium") and not _latest_is_anomaly:
                    _curr = float(_stats.get("latest") or analysis['total_waste_cost_week'])
                    forecast_next_week = round(max(0.0, _curr + _d), 2)
                    forecast_monthly = round(forecast_next_week * WEEKS_PER_MONTH, 2)
                elif _latest_is_anomaly:
                    wow_context += ("\n- NOTE: the most recent week sits outside this series' "
                                    "own spread, so it is an outlier rather than a new level. "
                                    "Do not project from it.")
        except Exception as _we:
            print(f"[inventory wow] {_we}")

        try:
            from models import get_conn as _gc_inv
            _conn_inv = _gc_inv()
            import json as _json_inv
            _prev_top = _conn_inv.execute("""
                SELECT waste_json FROM inventory_history
                WHERE restaurant_id=? AND week_end < date('now','-6 days')
                ORDER BY week_end DESC LIMIT 1
            """, (restaurant_id,)).fetchone()
            if _prev_top and _prev_top["waste_json"]:
                try:
                    prev_items = set(_json_inv.loads(_prev_top["waste_json"]).get("top_items") or [])
                except Exception:
                    prev_items = set()
                repeat = prev_items & set(x["item"] for x in analysis["waste_items"][:4])
                if repeat:
                    wow_context += (f"\n- REPEAT waste offenders (2+ weeks): {', '.join(repeat)}"
                                    " — these need stronger action, not just reordering")

            # Upsert today's snapshot. The scheduled writer
            # (food_cost_intelligence.weekly_snapshot) owns this table now;
            # this render-time write keeps the series current between runs and
            # never competes with it — same (restaurant, week_end) key, and
            # `source` is left alone so a scheduled row stays marked as one.
            #
            # The CREATE TABLE and two ALTER TABLEs that used to run here on
            # EVERY insight are gone: models.init_db declares the table, and
            # DDL on the render path is exactly the cost waste_trend removed
            # with its own _SCHEMA_ENSURED guard.
            import json as _json_inv2
            try:
                _week_end_str = datetime.strptime(analysis.get("week_end", ""), "%m/%d/%y").strftime("%Y-%m-%d")
            except Exception:
                from time_utils import restaurant_now_by_id as _rnbi
                _week_end_str = _rnbi(restaurant_id).strftime('%Y-%m-%d')
            snapshot = {
                "total_waste_cost": analysis['total_waste_cost_week'],
                "top_items": [x["item"] for x in analysis["waste_items"][:4]]
            }
            _items_json_str = _json_inv2.dumps(items) if items else None
            existing = _conn_inv.execute(
                "SELECT id FROM inventory_history WHERE restaurant_id=? AND week_end=?",
                (restaurant_id, _week_end_str)
            ).fetchone()
            if existing:
                # inv_value is deliberately NOT updated here.
                #
                # It is the anchor cogs.inventory_value_near brackets a COGS
                # window on, so rewriting it on a page render makes the food
                # cost percentage move because somebody opened a tab — and
                # worse, this write lands BEFORE the CFO evidence is assembled
                # a few lines below, so the render changed the closing
                # inventory that its own food cost % was then computed from.
                # Caught in end-to-end verification: two consecutive loads
                # produced 7.6% and 31.4% from identical underlying data.
                # The scheduled writer owns this column; once a day.
                _conn_inv.execute(
                    "UPDATE inventory_history SET waste_json=?, items_json=?, "
                    "inv_value=COALESCE(inv_value, ?), saved_at=datetime('now') WHERE id=?",
                    (_json_inv2.dumps(snapshot), _items_json_str,
                     analysis.get("total_stock_value"), existing["id"])
                )
            else:
                _conn_inv.execute(
                    "INSERT INTO inventory_history "
                    "(restaurant_id, waste_json, week_end, items_json, inv_value, source) "
                    "VALUES (?,?,?,?,?,'render')",
                    (restaurant_id, _json_inv2.dumps(snapshot), _week_end_str,
                     _items_json_str, analysis.get("total_stock_value"))
                )
            _conn_inv.commit()
            _conn_inv.close()
        except Exception as ie:
            print(f"[inventory history] {ie}")

        # Multi-week price trends via compute_item_trends
        if items:
            try:
                _trends = compute_item_trends(restaurant_id, items)
                trend_context = _trends["trend_context"]
                if _trends["big_8"]:
                    _b8_names = ", ".join(i["item"] for i in _trends["big_8"][:5])
                    big_8_context = (
                        f"\n\nBig 8 items (highest weekly spend impact): {_b8_names}"
                        + (f" (+{len(_trends['big_8']) - 5} more)" if len(_trends["big_8"]) > 5 else "")
                        + ". Prioritize recommendations for these items when relevant."
                    )
            except Exception as _te:
                print(f"[inventory trends] {_te}")

        # Menu connection — if restaurant has menu_notes, suggest menu decisions for repeat waste
        try:
            from models import get_restaurant as _gr_inv
            rest = _gr_inv(restaurant_id)
            if rest and rest.menu_notes and analysis["waste_items"]:
                top_waste_item = analysis["waste_items"][0]["item"]
                # menu_notes is owner-authored free text and reaches the model
                # verbatim; ingredient names arrive from CSV upload and Toast
                # sync. Fenced the same way the review paths fence their input.
                from ai_guard import wrap_untrusted
                menu_context = (
                    "\n- Menu context: " + wrap_untrusted(rest.menu_notes[:300])
                    + f". If {top_waste_item} appears in multiple dishes, consider whether "
                      "portion sizes or menu placement should change."
                )
        except Exception:
            pass

    from time_utils import restaurant_now_by_id as _rnbi_today
    today_inv = (_rnbi_today(restaurant_id) if restaurant_id
                 else datetime.now(ZoneInfo('America/Chicago'))).strftime("%B %d, %Y")

    # Seasonal/event awareness — pull upcoming holidays for ordering recommendations
    holiday_context = ""
    try:
        from marketing import get_upcoming_holidays as _guh
        _upcoming = _guh()
        if _upcoming:
            # Flag any inventory items that are relevant to upcoming holidays
            _all_items = (analysis.get("waste_items", []) +
                         analysis.get("critical_low", []) +
                         analysis.get("reorder_soon", []) +
                         analysis.get("order_reduction", []))
            _item_names = [x["item"].lower() for x in _all_items]
            # Holiday-to-ingredient hints — shared with analyse_inventory's
            # own event-scaling logic so narrative text and actual order
            # quantities never drift apart.
            _holiday_items = _HOLIDAY_ITEM_KEYWORDS
            relevant_flags = []
            for holiday_str in _upcoming.split(", "):
                h_lower = holiday_str.lower()
                for keyword, ingredients in _holiday_items.items():
                    if keyword in h_lower:
                        matches = [i for i in ingredients
                                  if any(i in name for name in _item_names)]
                        if matches:
                            relevant_flags.append(
                                f"{holiday_str}: consider stocking up on {', '.join(matches)}"
                            )
            _flag_lines = ("\nInventory flags for upcoming events:\n" + "\n".join("- " + f for f in relevant_flags)) if relevant_flags else ""
            _tail = "\nIf any upcoming holiday is within 2 weeks and relevant to this restaurant's inventory, include a specific ordering heads-up in your recommendations — only if it would genuinely change what they should order this week."
            holiday_context = "\n\nUpcoming holidays/events in the next 30 days: " + _upcoming + _flag_lines + _tail
    except Exception as _he:
        print(f"[inventory holiday context] {_he}")

    # Every savings figure the model is allowed to quote, computed here.
    # The prompt used to require "an estimated dollar amount" on each
    # recommendation without supplying one, so every "saves $X" in the output
    # was the model's own arithmetic on figures nothing had checked.
    savings_block = _supported_savings_block(analysis)

    # ── The CFO's evidence pack ─────────────────────────────────────────────
    #
    # The module held four financial engines and this prompt saw one of them.
    # cogs.build_food_cost_pct computed actual food cost % against the
    # restaurant's own target, menu_profitability computed plate margin and
    # contribution, recipe_coverage said how far any of it could be trusted —
    # and none reached the model, so an AI titled "food cost consultant" could
    # not say whether food cost was over target or which dish was eating the
    # margin. It narrated waste.
    #
    # Everything below is measured by code that already existed. The drivers
    # are ranked HERE, in Python, because ranking is arithmetic: the prompt
    # used to say "ranked by dollar impact" over an unordered concatenation of
    # three loops and nothing checked the order that came back.
    cfo_block = ""
    trust_block = ""
    position_block = ""
    profit_block = ""
    drivers_block = ""
    cross_block = ""
    diag_block = ""
    ranked_labels = []
    if restaurant_id:
        try:
            import food_cost_intelligence as _fci
            _ev = _fci.build_evidence(restaurant_id)
            position_block = _fci._position_block(_ev["food_cost"])
            profit_block = _fci._profit_block(_ev["profitability"])
            trust_block = _fci._trust_block(_ev["coverage"], _ev["waste_sources"])
            drivers_block = _fci._drivers_block(_ev["drivers"])
            cross_block = _fci._operational_block(_ev["operational"])
            ranked_labels = [d["label"] for d in (_ev["drivers"].get("drivers") or [])]
            _pat = _fci._pattern_block(_ev["weekday"], _ev["seasonal"])
            _acc = _ev["forecast_accuracy"]
            _acc_line = ("\n\nHow accurate past forecasts here have been: "
                         f"{_acc['scored']} scored, mean error {_acc['mean_error_pct']}% "
                         f"({_acc['reading']}). Mention this if you make a projection."
                         if _acc.get("available") else "")
            cfo_block = (
                "\n\nFOOD COST POSITION:\n" + position_block +
                "\n\nPROFITABILITY:\n" + profit_block +
                "\n\nWHERE THE MONEY IS (already ranked by dollars, then confidence, then ease "
                "— do NOT re-rank these):\n" + drivers_block +
                "\n\nHOW FAR THESE FIGURES CAN BE TRUSTED:\n" + trust_block +
                "\n\nWHERE AND WHEN THE WASTE LANDS:\n" + _pat +
                "\n\nWHAT THE OTHER MODULES RECORDED OVER THE SAME PERIOD:\n" + cross_block +
                _acc_line
            )
        except Exception as _ce:
            print(f"[inventory cfo context] {_ce}")

        # The stored root-cause read. Read, never generated here: producing
        # one is a Sonnet call over the ranked drivers and belongs on the
        # scheduler, not on the critical path of a page load.
        try:
            import food_cost_intelligence as _fci2
            _dg = _fci2.get_diagnosis(restaurant_id, include_stale=True)
            if _dg and _dg.get("cause"):
                diag_block = (
                    "\n\nROOT-CAUSE READ (stored, " + str(_dg.get("confidence")) + " confidence"
                    + (f", produced {int(_dg['age_hours'])}h ago" if _dg.get("age_hours") is not None else "")
                    + "):\n- Most likely: " + _dg["cause"]
                    + (f"\n- Alternative: {_dg['alternative_cause']}" if _dg.get("alternative_cause") else "")
                    + (f"\n- What would confirm it: {_dg['what_would_confirm']}" if _dg.get("what_would_confirm") else "")
                    + (f"\n- Recommended: {_dg['recommended_action']}" if _dg.get("recommended_action") else "")
                    + "\nUse this for the WHY sentence. Do not substitute a cause of your own.")
            else:
                diag_block = ("\n\nROOT-CAUSE READ: none has been produced yet. Do NOT state a "
                              "cause. Say what the figures show and stop.")
        except Exception:
            pass

    # Projected only when waste_trend's own confidence and anomaly checks say
    # there is a direction to project — see the wow_context block above, which
    # sets forecast_next_week to None when the latest week is an outlier or
    # the series cannot bear a direction. The dollars are computed in Python;
    # asking the model "what does that mean if it continues" guarantees a
    # model-generated number, because the consequence of a projection is by
    # definition not in the prompt.
    has_trend = bool(wow_context) and forecast_next_week is not None
    forecast_instruction = ""
    if has_trend:
        forecast_instruction = (
            '\n- Then, on a final new line, add exactly "FORECAST:" followed by one sentence '
            "predicting where waste cost is headed next week based on the week-over-week trend "
            f"above. If it continues at this rate next week lands near ${forecast_next_week:,.0f} "
            f"(${forecast_monthly:,.0f} a month) — quote those figures exactly and invent no others. "
            "Only include this if the trend is genuinely supported by the data given."
        )
        # Stored so it can be scored against what actually happens. A forecast
        # nobody checks costs nothing to get wrong, which is the opposite of
        # what a projection is for. See food_cost_intelligence.score_forecasts.
        if restaurant_id:
            try:
                import food_cost_intelligence as _fci_f
                from datetime import date as _d_f, timedelta as _td_f
                _fci_f.record_forecast(
                    restaurant_id, "waste_week",
                    (_d_f.today() + _td_f(days=7)).isoformat(),
                    forecast_next_week,
                    basis="week-over-week delta on the ISO-week waste series")
            except Exception as _fe:
                print(f"[inventory forecast log] {_fe}")

    has_why = "Most likely:" in diag_block

    prompt = f"""You are an experienced restaurant CFO reviewing this restaurant's food cost.

You are not writing a summary. The owner can already see their waste total and their inventory value on the same screen. Your value is the step after the number: what it means for their margin, what is driving it, and what to do first.
{rest_line}
{name_line}
Today's date: {today_inv}

Key findings:
- Waste this week: ${analysis['total_waste_cost_week']:,.2f}
- Projected monthly waste cost: ${analysis['monthly_waste_projection']:,.2f} ({analysis['projection_basis']})
- Recoverable with better ordering: ${analysis['recoverable_monthly']:,.2f}/month
- Total current inventory value: ${analysis['total_stock_value']:,.2f}
- Waste rate vs industry: {analysis['waste_rate_pct']}% of ${analysis['total_purchased']:,.2f} purchased (industry target is 4-5% — label: {analysis['benchmark_label']}). Denominator basis: {analysis['purchases_basis']}{wow_context}{trend_context}{big_8_context}{holiday_context}

How "recoverable" is defined: {RECOVERABLE_BASIS}
{cfo_block}{diag_block}

Top waste offenders:
{json.dumps([{"item": x["item"], "waste_units": x["waste_last_week"], "waste_cost": x["waste_cost"], "waste_pct": x["waste_pct"], "par": x["par_level"], "current_stock": x["current_stock"], "unit_cost": x["unit_cost"], "tolerance_pct": x.get("waste_tolerance_pct"), "recoverable_cost": x.get("recoverable_cost")} for x in analysis["waste_items"][:4]], indent=2)}

Overstocked items:
{json.dumps([{"item": x["item"], "current": x["current_stock"], "par": x["par_level"], "overstock_cost": x["overstock_cost"]} for x in analysis["overstock"][:3]], indent=2)}

Critical low stock:
{json.dumps([{"item": x["item"], "days_remaining": x["days_remaining"], "suggested_order_qty": x.get("suggested_order_qty"), "par": x["par_level"], "current_stock": x["current_stock"]} for x in analysis["critical_low"]], indent=2)}

Savings the data supports (these are the only savings figures that exist — use these, do not compute your own):
{savings_block}{menu_context}

Write a food cost analysis. Rules that apply to everything:
- Every dollar amount, percentage and quantity you write must appear verbatim somewhere above. Do not add, average, extrapolate or otherwise derive a number of your own — not even a rounded one.
- Never state a cause that is not in the ROOT-CAUSE READ above. If there is none, describe what the figures show and stop.
- The drivers above are ALREADY RANKED by dollars, then confidence, then ease. Follow that order. Do not promote a cheaper or easier item above a more expensive one.
- Read "HOW FAR THESE FIGURES CAN BE TRUSTED" before you commit to anything. Low recipe coverage or a high inferred-waste share means the usage figures underneath are soft, and you must say so rather than writing past it.
- Where a figure is marked as not computable, do not estimate it. "We cannot measure your food cost percentage until a second count is in" is a correct and useful sentence.
- If the data does not support a genuine, specific opportunity, say so plainly in one sentence and write no recommendations at all. An honest "nothing worth changing this week" is a correct answer.
- No markdown, no bullet points, no bold text, no asterisks whatsoever
- Do NOT label sections or write "Part 1", "Part 2", "Recommendations", or any headers
- Plain flowing prose throughout — no line that starts with a dash or number
- Friendly and direct — like a trusted advisor, not a formal report
- Always use $ signs before dollar amounts (e.g. $2,400 not 2400 or 2,400)

This is read on a phone screen — brevity is the whole point. Cut ruthlessly.

First, write one paragraph of 2 sentences max (never 3-4):
- Lead with the money: their food cost position or projected month if either is computable above, otherwise the monthly waste projection
- Name the single largest driver by item name with its dollar amount
{("- Then one sentence beginning \"Why:\" giving the cause from the ROOT-CAUSE READ, and how confident it is." if has_why else "")}

Then, on new lines after the paragraph, write 1-3 recommendations:
- Take them IN THE ORDER the drivers are ranked above. Do not reorder.
- Only include recommendations where there is a genuine, specific opportunity — do not pad to three if the data does not support it
- Maximum of three. Zero is allowed when the data supports none.
- Number each one: start with "1. ", "2. ", "3. "
- Hard cap: 30 words per recommendation. Lead with the action.
- End each one with " — " then its monthly dollar figure, its confidence and how hard it is, exactly as given above (e.g. " — $240/month, high confidence, low effort")
- Each must quote a dollar figure from the driver list or the "Savings the data supports" block — never a figure you worked out yourself
- Specific to the actual items in the data — never generic advice
- Never suggest anything that hurts guest experience, reduces quality, or cuts portions
- NEVER assume or mention ordering frequency (daily, weekly, twice a week etc.) since you don't know their ordering schedule
- Do not use the owner name anywhere in the recommendations
- On the LAST numbered recommendation only, you may add up to 8 words of warm closing after it — tied loosely to how the week looks, nothing more. Do NOT write a separate closing line after the numbered list.{forecast_instruction}"""


    msg = create_with_retry(
        client,
        model=os.getenv("INVENTORY_INSIGHT_MODEL", "claude-sonnet-5"),
        # The recommendations now carry a dollar figure, a confidence and an
        # effort level each, and the opening paragraph can carry a Why
        # sentence. 950 was sized for the old bare-action format and the
        # truncation guard below would have started firing.
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="inventory_insight",
    )
    result = extract_text(msg).strip()
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("food cost insight was truncated")
    # The return value used to be discarded. labor.py appends the marker,
    # client_api.py parses it and mobile_api.py renders it as
    # claim_kinds.insight_unverified — the whole pipeline existed and food
    # cost was the one module that computed the flag and dropped it, showing
    # figures nothing could trace back to the data as plain fact.
    from ai_guard import verify_figures
    unsupported = verify_figures(result, prompt, "inventory_insight", restaurant_id)
    # Strip any markdown that slips through
    import re as _re_inv
    result = _re_inv.sub('[*]{2}(.+?)[*]{2}', lambda m: m.group(1), result)
    result = _re_inv.sub('[*](.+?)[*]', lambda m: m.group(1), result)
    result = _re_inv.sub(r'#{1,6}\s', '', result)
    if unsupported:
        result = result.rstrip() + "\n\nUNVERIFIED: " + ", ".join(str(u) for u in unsupported[:5])
    return result


def load_inventory_for_restaurant(restaurant_id: int):
    """Load this restaurant's inventory. Resolution order: the persistent
    ingredients table (once migrated — see inventory_ledger.import_csv_to_ingredients
    and the nightly Toast depletion sync) -> the legacy inventory_csv blob
    -> sample data. Returns (items, is_live). Output shape is identical
    across all three paths, so analyse_inventory() and every caller need
    no changes regardless of which path a given restaurant is on."""
    from models import get_client_data, get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ingredients WHERE restaurant_id=? AND is_active=1",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    if rows:
        items = [{
            "item":            r["name"],
            "category":        r["category"] or "",
            "par_level":       r["par_level"],
            "current_stock":   r["current_stock"],
            "unit_cost":       r["unit_cost"],
            "avg_daily_usage": r["avg_daily_usage"],
            "last_order_qty":  r["last_order_qty"],
            "waste_last_week": r["waste_last_week"],
            "unit":            r["unit"] or "",
            "case_size":       r["case_size"] or 1.0,
            # Carried through so build_supplier_orders can group an order
            # by who it actually gets sent to. Only the ingredients-table
            # path has these; CSV/sample items simply have none, and fall
            # into the "unassigned" group.
            "supplier_name":   (r["supplier_name"] if "supplier_name" in r.keys() else None) or "",
            "supplier_email":  (r["supplier_email"] if "supplier_email" in r.keys() else None) or "",
        } for r in rows]
        return items, True
    data = get_client_data(restaurant_id)
    if data and data.get("inventory_csv"):
        return load_inventory(csv_string=data["inventory_csv"]), True
    return load_inventory(), False  # fallback to sample


def analysis_for(restaurant_id: int, items=None, is_live=None):
    """The one food-cost analysis. Every surface comes through here.

    Callers used to assemble analyse_inventory()'s arguments themselves —
    eleven call sites, four different combinations — so the Food Cost page,
    the Home brief, the weekly digest, the alert engine and the purchase
    order that actually reaches a supplier could each compute a different
    answer for the same restaurant at the same moment. delivery_days changes
    which items land in critical_low vs reorder_soon, and upcoming_holidays
    scales suggested_order_qty by 40%, so "the order shown" and "the order
    sent" were genuinely different orders.

    Returns (items, is_live, analysis). analysis carries is_live so no caller
    can drop it on the way to an email or an alert.
    """
    from models import get_restaurant
    from marketing import get_upcoming_holidays

    if items is None or is_live is None:
        items, is_live = load_inventory_for_restaurant(restaurant_id)
    restaurant = get_restaurant(restaurant_id)

    # The two measured inputs analyse_inventory could not derive for itself:
    # what was actually received over the same seven days the waste covers,
    # and when the counts behind these figures were really taken. Both come
    # from the ledger and are simply absent for a restaurant still on the CSV
    # path, in which case analyse_inventory falls back and says so.
    purchases_window, counted_from, counted_to = None, None, None
    if is_live:
        try:
            from datetime import date as _d, timedelta as _td
            import cogs as _cogs
            _end = _d.today()
            _start = _end - _td(days=6)
            _p, _n = _cogs.purchases_in_window(restaurant_id, _start, _end)
            if _n:
                purchases_window = _p
        except Exception:
            pass
        try:
            from models import get_conn as _gc
            _c = _gc()
            _row = _c.execute(
                "SELECT MIN(last_recount_at) AS lo, MAX(last_recount_at) AS hi "
                "FROM ingredients WHERE restaurant_id=? AND is_active=1 "
                "AND last_recount_at IS NOT NULL", (restaurant_id,)).fetchone()
            _c.close()
            if _row and _row["hi"]:
                counted_from, counted_to = _row["lo"], _row["hi"]
        except Exception:
            pass

    analysis = analyse_inventory(
        items,
        delivery_days=restaurant.delivery_days if restaurant else None,
        upcoming_holidays=get_upcoming_holidays(),
        purchases_window=purchases_window,
        counted_from=counted_from,
        counted_to=counted_to,
    )
    analysis["is_live"] = bool(is_live)
    return items, bool(is_live), analysis


# ── Supplier orders ────────────────────────────────────────────────────────────

def build_supplier_orders(restaurant_id: int, db_path: str = None) -> dict:
    """Turn the computed order list into orders that can actually be sent.

    The suggested-order maths already lives in analyse_inventory() — this
    only takes what it flagged (critical_low first, then reorder_soon),
    keeps anything with a real quantity, and groups it by the supplier
    each ingredient is assigned to. Items with no supplier set come back
    in their own "unassigned" group so the UI can prompt for one rather
    than silently dropping them from the order.

    Returns {"groups": [...], "unassigned": [...], "item_count": n,
             "total_cost": float} where each group is
    {"supplier_name", "supplier_email", "items": [...], "total_cost"}.
    """
    items, is_live = load_inventory_for_restaurant(restaurant_id)
    if not is_live:
        # This draft reaches /food-cost/send-order, which emails a real
        # supplier. The sample pantry carries no supplier addresses so
        # nothing would actually send today, but an order built from
        # invented stock levels should not exist at all.
        return {"groups": [], "unassigned": [], "item_count": 0, "total_cost": 0.0,
                "is_live": False, "notice": SAMPLE_DATA_NOTICE, "draft_hash": None}
    # Through analysis_for, not a bare analyse_inventory(items): this draft is
    # what gets emailed to a supplier, and it used to be computed without the
    # delivery schedule or holiday scaling the page itself applied — so the
    # order sent was not the order the owner approved.
    _, _, analysis = analysis_for(restaurant_id, items=items, is_live=is_live)

    # critical_low first — same order the UI shows them in — then
    # reorder_soon, skipping anything already picked up.
    seen, ordered = set(), []
    for bucket in ("critical_low", "reorder_soon"):
        for item in analysis.get(bucket, []):
            name = item.get("item")
            if not name or name in seen:
                continue
            if int(item.get("suggested_order_qty") or 0) <= 0:
                continue
            seen.add(name)
            ordered.append({
                "item": name,
                "unit": item.get("unit") or "",
                "qty": int(item["suggested_order_qty"]),
                "unit_cost": round(float(item.get("unit_cost") or 0), 2),
                "line_cost": round(int(item["suggested_order_qty"]) * float(item.get("unit_cost") or 0), 2),
                "urgency": "critical" if bucket == "critical_low" else "soon",
                "supplier_name": (item.get("supplier_name") or "").strip(),
                "supplier_email": (item.get("supplier_email") or "").strip(),
            })

    groups, unassigned = {}, []
    for row in ordered:
        if not row["supplier_email"]:
            unassigned.append(row)
            continue
        key = (row["supplier_name"], row["supplier_email"].lower())
        groups.setdefault(key, []).append(row)

    group_list = [{
        "supplier_name": name or email,
        "supplier_email": email,
        "items": rows,
        "total_cost": round(sum(r["line_cost"] for r in rows), 2),
    } for (name, email), rows in sorted(groups.items())]

    return {
        "groups": group_list,
        "unassigned": unassigned,
        "item_count": len(ordered),
        "total_cost": round(sum(r["line_cost"] for r in ordered), 2),
        "is_live": True,
        # Identifies exactly this draft. /food-cost/send-order rebuilds the
        # draft rather than storing it, so stock or supplier edits between
        # preview and send would silently change quantities; the client sends
        # this back and a mismatch is refused instead of mailed.
        "draft_hash": draft_hash(group_list),
    }


def draft_hash(group_list) -> str:
    """Stable fingerprint of a supplier-order draft: who it goes to, what is
    on it, and how much of each."""
    import hashlib
    shape = [[g["supplier_email"], sorted((r["item"], r["qty"]) for r in g["items"])]
             for g in group_list]
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()[:16]
