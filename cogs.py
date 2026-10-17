"""cogs.py — actual food cost %, the number the module is named after.

Until this existed the Food Cost module never computed food cost %. It
computed waste dollars, a waste rate against purchases, stock value and
suggested orders — all useful, none of them the figure an owner reads off
their P&L and compares to a target. `restaurants.food_cost_target` was
settable in client settings, shown in the admin console, and read by
nothing.

The identity is the ordinary one:

    COGS = opening inventory + purchases - closing inventory
    food cost % = COGS / net sales * 100

Every input is measured, never assumed. When a component is missing this
returns what is missing and why, and no percentage at all — a food cost %
built on a guessed opening inventory is worse than no food cost %, because
it looks exactly like a real one. That rule is the whole design: see
`missing` in the returned payload, and note that nothing here ever
substitutes 0 for an absent measurement.
"""
import re
from datetime import date, timedelta

# A four-week window by default: long enough that one heavy delivery week
# doesn't dominate, short enough to still describe the current menu and
# current prices.
DEFAULT_WINDOW_DAYS = 28

# The industry band a computed food cost % is labelled against comes from
# benchmark_registry by the restaurant's type (NS4 H3). The unsourced
# (28, 35) "full-service" band used to be applied to every restaurant; with
# no entry for the type there is no industry label at all, only the owner's
# own target.

# An inventory snapshot this far either side of the window edge is still a
# fair opening/closing measure. Beyond it the count is too stale to anchor a
# COGS figure and the whole calculation is withheld instead.
SNAPSHOT_TOLERANCE_DAYS = 10


def local_today(restaurant_id):
    """The restaurant's own calendar date — the day boundary of every COGS
    window and of "through yesterday" (DH1-14). On a UTC host the server's
    date.today() is tomorrow after 7pm Central, so "yesterday" was local
    today, the archive never spanned it, and every evening load fell
    through to a live POS call — the traffic RPOWER asked us not to send."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _degraded(exc, where, restaurant_id=None):
    """A read that failed and was degraded to "unknown" — reported to the
    failure digest, never silent (SCALE-11): a Postgres translation miss or
    a lock read as "no sales" or "no counts", and nobody was paged."""
    try:
        import ops
        ops.capture(exc, job=f"cogs.{where}", context=f"restaurant_id={restaurant_id}")
    except Exception:
        pass


def last_delivery_date(restaurant_id, db_path=None):
    """ISO date of the newest logged delivery (receiving event), or None."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        row = conn.execute("SELECT MAX(event_date) AS d FROM ingredient_stock_events "
                           "WHERE restaurant_id=? AND event_type='receiving'", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    return str(row["d"])[:10] if row and row["d"] else None


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Alcohol is the Bar's (Food Cost → Bar, from the distributor invoices and
# the bar's own counts), never food cost: an ingredient filed under one of
# these categories, or whose name the Bar's sorter reads as beer, liquor or
# wine for sure, is partitioned out of food purchases and food stock value
# (Bar audit R8 / AIX-14). Left in, the day an owner put liquor on the count
# sheet food cost % would carry alcohol COGS over total sales while the Bar
# counted the same purchases — the two modules summing to more than was spent.
BAR_INGREDIENT_CATEGORIES = frozenset({
    "alcohol", "bar", "beer", "beer & seltzer", "liquor", "spirits", "wine", "seltzer", "hard seltzer",
    "cider", "beverage - alcohol", "alcoholic beverage", "alcoholic beverages", "beer/wine", "beer & wine",
    "beer and wine", "liquor & spirits", "keg", "kegs"})


# The Bar's sorter reads distributor lines, where every product is a drink;
# on a kitchen's list "WHITE WINE VINEGAR", "VODKA SAUCE", "RUM CAKE" and
# "COOKING WINE" are food. A kitchen word keeps a name on the food side.
_KITCHEN_RE = re.compile(r"\b(VINEGAR|SAUCE|CAKE|VINAIGRETTE|BATTER|SOUP|GLAZE|MARINADE|COOKING|DRESSING|"
                         r"BBQ|BARBECUE|CHEESE|SEASONING|EXTRACT|POWDER|FLAVOU?R(ING)?|REDUCTION|BRAISE|"
                         r"MUSTARD|SALSA|AIOLI|MARSALA|MIRIN|DIP|BREAD|BRAT|BRATWURST|SAUSAGE|CHICKEN|BEEF|PORK|FISH)\b")


def alcohol_category(name, supplier=None, unit=None):
    """'beer' / 'liquor' / 'wine' when a product name reads as alcohol for
    sure (the Bar's sorter, bar.classify), else None. A guess, or a name
    with a kitchen word in it, is never alcohol."""
    text = " ".join(str(name or "").upper().split())
    if not text or _KITCHEN_RE.search(text):
        return None
    try:
        from bar.classify import classify
        got, sure = classify(supplier or "", text, unit)
    except Exception:
        return None
    return got if sure and got in ("beer", "liquor", "wine") else None


def is_bar_item(category=None, name=None) -> bool:
    """True when an ingredient is alcohol: its category says so, or (no
    category, or a generic beverage one) its name reads as alcohol for sure
    (alcohol_category). A guess never moves an item."""
    cat = " ".join(str(category or "").lower().split())
    if cat in BAR_INGREDIENT_CATEGORIES:
        return True
    if cat and cat not in ("beverage", "beverages", "drinks", "other", "misc", "miscellaneous", "dry goods"):
        return False
    return alcohol_category(name) is not None


def food_stock_value(items) -> float:
    """Stock value of the food side only (bar items out, never below zero
    stock): the opening/closing anchor a food COGS window reads."""
    total = 0.0
    for it in items or ():
        if is_bar_item(it.get("category"), it.get("name") or it.get("item")):
            continue
        total += max(0.0, _f(it.get("current_stock"))) * _f(it.get("unit_cost"))
    return round(total, 2)


def beverage_sales_in_window(restaurant_id, start, end, db_path=None):
    """Drink (alcohol) sales over [start, end] from the nightly report's
    mapped categories (dsr.kpis.BEVERAGE_CATEGORIES, matched whole), or None
    when no beverage category is on file — unknown, never 0 (R7)."""
    from models import get_conn, DB_PATH
    from dsr.kpis import BEVERAGE_CATEGORIES
    try:
        conn = get_conn(db_path or DB_PATH)
        try:
            rows = conn.execute(
                "SELECT metric, SUM(value) AS v FROM dsr_metrics WHERE restaurant_id=? AND metric LIKE 'sales.cat:%' "
                "AND business_date >= ? AND business_date <= ? GROUP BY metric",
                (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _degraded(e, "beverage_sales_in_window", restaurant_id)
        return None
    bev = [_f(r["v"]) for r in rows if " ".join(str(r["metric"])[10:].lower().split()) in BEVERAGE_CATEGORIES]
    return round(sum(bev), 2) if bev and sum(bev) > 0 else None


def purchases_in_window(restaurant_id, start, end, db_path=None):
    """Dollar value of everything received between two dates, inclusive.

    Priced at what each delivery cost when it arrived (the unit_cost the
    receiving event recorded). Priced at today's unit_cost, an invoice that
    raised a price re-priced every delivery already in the window (MOD-FC-23).
    Rows from before the event carried a price fall back to the current one.
    Returns (dollars, n_events) so a caller can tell "nothing was bought"
    from "nothing was recorded". Alcohol (is_bar_item) is the Bar's and is
    left out of both (R8).
    """
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT i.category, i.name, COALESCE(SUM(e.qty * COALESCE(e.unit_cost, i.unit_cost, 0)), 0) AS total, "
            "COUNT(*) AS n "
            "FROM ingredient_stock_events e "
            "JOIN ingredients i ON i.id = e.ingredient_id AND i.restaurant_id = e.restaurant_id "
            "WHERE e.restaurant_id=? AND e.event_type='receiving' "
            "AND e.event_date >= ? AND e.event_date <= ? GROUP BY i.id, i.category, i.name",
            (restaurant_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    food = [r for r in rows if not is_bar_item(r["category"], r["name"])]
    return round(sum(_f(r["total"]) for r in food), 2), int(sum(int(r["n"] or 0) for r in food))


def inventory_value_near(weeks, target_day, tolerance_days=SNAPSHOT_TOLERANCE_DAYS):
    """The recorded inventory value closest to `target_day`, or None.

    `weeks` is waste_trend.load_waste_history's series; entries carry
    inv_value only when their snapshot included per-item detail. Returns
    (value, snapshot_date) or (None, None). build_food_cost_pct hands it
    only COUNTED weeks (is_counted), each dated by its count day
    (FC-CALC-02)."""
    best, best_gap, best_day = None, None, None
    for w in weeks or []:
        if w.get("inv_value") in (None, 0):
            continue
        try:
            day = date.fromisoformat(w["week_end"])
        except Exception:
            continue
        gap = abs((day - target_day).days)
        if gap <= tolerance_days and (best_gap is None or gap < best_gap):
            best, best_gap, best_day = _f(w["inv_value"]), gap, day
    return best, (best_day.isoformat() if best_day else None)


# A snapshot is a COUNT when the items recounted on its count day carry at
# least this share of its food stock value (FC-CALC-02). Between counts the
# ledger's stock is the last count + receipts - recipe depletion - logged
# waste, so a COGS bracketed by two such figures is recipe depletion plus
# logged waste: the recipes' theoretical cost, never a measurement.
COUNTED_SHARE = 0.8


def is_counted(week) -> bool:
    """True when a waste-history week's stock value is a real count: a
    snapshot whose items were (nearly all) recounted on its count day
    (waste_trend._week_from_row sets `counted`), or a Back Office count."""
    if not week:
        return False
    return bool(week.get("counted")) or week.get("source") == "back_office_count"


def edge_tolerance(days) -> int:
    """How far a count may sit from a window's edge and still bracket it:
    SNAPSHOT_TOLERANCE_DAYS for the 28-day window, proportionally less for
    a shorter one (2 days for a week), so a 7-day window is never measured
    by counts most of a week away (FC-CALC-01)."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = DEFAULT_WINDOW_DAYS
    return max(2, min(SNAPSHOT_TOLERANCE_DAYS, days * SNAPSHOT_TOLERANCE_DAYS // DEFAULT_WINDOW_DAYS))


def last_complete_day(restaurant_id, today=None):
    """The last business date a food cost window may end on: the
    restaurant's last COMPLETE business day (pos.complete_through - after
    its close, before the next day's open), so purchases, the closing count
    and sales all end on one day (FC-CALC-15, POS-02, POS-17). Purchases
    and the closing count used to run through today while sales stopped at
    yesterday, and between midnight and 5am "yesterday" was a night still
    trading.

    `today` is an as-of date: a past day (a closed week, last month) is
    complete and is its own end; the current day ends at the last complete
    day; a future as-of date (a test, a plan) ends on its eve."""
    real = local_today(restaurant_id)
    if today is not None and today > real:
        return today - timedelta(days=1)
    ct = None
    try:
        import pos
        ct = pos._complete_through_for(restaurant_id)
    except Exception:
        ct = None
    if ct is None or ct >= real:
        ct = real - timedelta(days=1)
    if today is None or today >= real:
        return ct
    return min(today, ct)


def covers_window(fc, start, end, tolerance=None) -> bool:
    """Whether a build_food_cost_pct payload measured (about) the window a
    caller asked for: ok, and its own start and end within the edge
    tolerance of [start, end]. Every caller that sets COGS beside another
    figure for that window checks this (FC-CALC-01): a Back Office kitchen
    measures count to count, and a payload for September is not October's
    month to date."""
    if not fc or not fc.get("ok"):
        return False
    try:
        s = date.fromisoformat(str(fc["start"])[:10])
        e = date.fromisoformat(str(fc["end"])[:10])
        a0 = date.fromisoformat(str(start)[:10])
        a1 = date.fromisoformat(str(end)[:10])
    except (KeyError, TypeError, ValueError):
        return False
    tol = edge_tolerance((a1 - a0).days + 1) if tolerance is None else tolerance
    return abs((s - a0).days) <= tol and abs((e - a1).days) <= tol


def window_cogs(fc, start, end):
    """(COGS dollars for [start, end], why-missing): the payload's COGS when
    it covers the window (covers_window), scaled by days when its counted
    period is a few days longer or shorter than the one asked; (None, why)
    when it measured another period or nothing at all."""
    if not fc or not fc.get("ok"):
        return None, "; ".join(m.get("why", "") for m in (fc or {}).get("missing") or []) or "not computable"
    if not covers_window(fc, start, end):
        from time_utils import mdy as _m
        return None, (f"food cost was measured {_m(str(fc.get('start'))[:10])} to {_m(str(fc.get('end'))[:10])} — "
                      f"the counts don't bracket {_m(str(start)[:10])} to {_m(str(end)[:10])}")
    asked = (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days + 1
    had = int(fc.get("window_days") or asked) or asked
    return round(_f(fc.get("cogs")) * asked / had, 2), None


def _sales_note(have, expected):
    """The owner-facing note on a sales total scaled for missing days, or
    None when every trading day was there (POS-04)."""
    if expected and 0 < have < expected:
        return (f"sales on file for {have} of {expected} trading days, scaled to the whole window "
                "(an estimate)")
    return None


def _scaled_total(in_window, reference, d0, d1):
    """(total, have, expected) over [d0, d1] under the one coverage rule
    (_covers_trading_days), the total scaled up by expected / have when a
    trading day is missing (POS-04): a missing day used to be summed as $0,
    reading food cost % up to ~11% high as a measurement. (None, have,
    expected) when coverage fails or nothing sold."""
    ok, have, expected = _covers_trading_days(in_window, reference, d0, d1)
    total = sum(in_window.values())
    if not ok or not in_window or total <= 0:
        return None, have, expected
    if 0 < have < expected:
        total = total * expected / have
    return round(total, 2), have, expected


def _archive_detail(restaurant_id, start, end, today=None):
    """(total, have, expected) from the LOCAL archive, or None - see
    _archived_net_sales."""
    from models import get_conn
    from canonical_facts import FINAL_SQL
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    yesterday = (today or local_today(restaurant_id)) - timedelta(days=1)
    if d1 > yesterday:
        d1 = yesterday
    if d1 < d0:
        return None
    s, e = d0.isoformat(), d1.isoformat()
    lookback = (d0 - timedelta(days=56)).isoformat()
    try:
        conn = get_conn()
        try:
            # Final days only (POS-04): a day read while it was trading
            # (final=0) is a partial night, and stays one whenever the next
            # sync fails - summed, it was a whole day's sales.
            rows = conn.execute(
                "SELECT date, sales FROM labor_daily_history "
                "WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 "
                f"  AND date >= ? AND date <= ? AND {FINAL_SQL}", (restaurant_id, lookback, e)).fetchall()
            edges = conn.execute(
                "SELECT MIN(date) AS first, MAX(date) AS last FROM labor_daily_history "
                f"WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 AND {FINAL_SQL}",
                (restaurant_id,)).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        _degraded(exc, "archived_net_sales", restaurant_id)
        return None
    if not edges or not edges["first"]:
        return None
    if str(edges["first"])[:10] > s or str(edges["last"])[:10] < e:
        return None          # the archive does not span this window
    by_day = {}
    for r in rows:
        try:
            by_day[date.fromisoformat(str(r["date"])[:10])] = by_day.get(
                date.fromisoformat(str(r["date"])[:10]), 0.0) + _f(r["sales"])
        except ValueError:
            continue
    in_window = {d: v for d, v in by_day.items() if d0 <= d <= d1}
    if not in_window:
        return None
    total, have, expected = _scaled_total(in_window, by_day, d0, d1)
    if total is None:
        return None          # a hole inside the span: not what the restaurant sold
    return total, have, expected


def _archived_net_sales(restaurant_id, start, end, today=None):
    """Net sales for the window from the LOCAL archive, or None.

    labor_daily_history carries one row per business date with that day's
    sales, written by each provider's nightly sync_to_db. Reading it is free
    and works when the POS is unreachable.

    Coverage takes two things. The archive must span the window - its
    earliest row at or before `start`, its latest at or after `end` - and it
    must have a row for (nearly) every day the restaurant trades inside it.
    Spanning alone was the whole test, so an archive with a three-week hole
    in the middle was summed as three weeks of zero sales and read food cost
    about eleven times too high (MOD-FC-16). "Every day it trades" is every
    date whose weekday has sales somewhere in the window or the eight weeks
    before it: a restaurant closed on Mondays has no Monday row and is not
    counted short for it. One missing day in ten (a holiday, a snow day) is
    allowed, and the total is scaled up for it (POS-04) rather than summed
    as a $0 day; more falls through to the live POS rather than quietly
    returning a smaller number - an under-reported sales figure inflates
    food cost %. Only final days count: a provisional night (final=0) is
    missing, not whole.

    A window ending today is judged through yesterday. The nightly sync
    archives each business date after it closes, so an archive complete
    through yesterday never reached `end`, and every load of a window
    ending today called the live POS (MOD-FC-17) - the traffic the RPOWER
    integrator asked us not to send. Today's sales are not in the figure
    until tonight's sync writes them.
    """
    got = _archive_detail(restaurant_id, start, end, today=today)
    return got[0] if got else None


def _covers_trading_days(in_window, reference, d0, d1):
    """(ok, have, expected): whether `in_window` ({date: sales > 0}) has a
    figure for (nearly) every day the restaurant trades between d0 and d1.
    Trading days are the weekdays with sales anywhere in `reference` (a
    closed-Monday restaurant is not counted short for Mondays). One missing
    day in ten is allowed. The one coverage rule for net sales - the archive
    and the live POS both answer to it (CA3 F14) - and for a Back Office
    kitchen's food sales (FC-CALC-16)."""
    trading_weekdays = {d.weekday() for d in reference}
    expected = sum(1 for k in range((d1 - d0).days + 1)
                   if (d0 + timedelta(days=k)).weekday() in trading_weekdays)
    have = len(in_window)
    return have >= expected - max(1, expected // 10), have, expected


def net_sales_in_window(restaurant_id, start, end):
    """Net sales for the window, or None when it cannot be known. None means
    unknown - never 0. Returns (total, why): `why` says what is missing when
    the total is None, and is the estimate note when a missing trading day
    was scaled for (POS-04) - None for a whole window.

    Reads the LOCAL ARCHIVE first and only calls the POS when the archive
    does not span the window.

    Two reasons, and the second is a vendor requirement rather than an
    optimisation. RPOWER's integrator asked explicitly that we "download and
    archive the data" rather than query it repeatedly, and told us there is
    no sandbox - every call is against a live store. Meanwhile this function
    is reached twice per profitability_projection, which is reached by
    business_intelligence.gather, which runs on every Ask Cavnar context
    build. That is a lot of traffic against an API whose rate limits we have
    not been given.

    Goes through pos.py rather than importing toast directly. That import was
    the reason food cost % was a Toast-only feature: a Square, Clover or
    RPOWER restaurant was told "no POS connected" by this function while
    pos.connected_provider knew perfectly well which POS they were on, so the
    one number this module is named after could never be computed for them.
    """
    archived = _archive_detail(restaurant_id, start, end)
    if archived is not None:
        return archived[0], _sales_note(archived[1], archived[2])
    try:
        import pos
        by_date, _provider = pos.fetch_business_days(restaurant_id, start, end)
    except Exception as e:
        # POSCapabilityError carries the honest reason ("no POS connected",
        # or connected-but-cannot-report); anything else is a transport
        # failure. Both are "unknown", and both say which.
        import pos as _pos
        if isinstance(e, getattr(_pos, "POSCapabilityError", ())):
            return None, str(e)
        return None, f"POS request failed: {e}"
    if not by_date:
        return None, "POS returned no business days for this window"
    # The same 90% coverage rule as the archive (CA3 F14). The live path
    # summed whatever days came back, so a POS that answered for 9 of 28
    # days gave a sales figure a third of the truth and a food cost % three
    # times too high. Judged through yesterday, like the archive: today's
    # sales are not in until tonight's close - and only the window's
    # complete days are summed (POS-02): the live total used to carry
    # today's half day, so the live and archive paths disagreed.
    live = {}
    for k, v in by_date.items():
        try:
            d = date.fromisoformat(str(k)[:10])
        except ValueError:
            continue
        if _f(v) > 0:
            live[d] = live.get(d, 0.0) + _f(v)
    if sum(live.values()) <= 0:
        return None, "POS reported no sales for this window"
    try:
        d0 = date.fromisoformat(str(start)[:10])
        d1 = min(date.fromisoformat(str(end)[:10]), local_today(restaurant_id) - timedelta(days=1))
    except ValueError:
        return None, "window dates unreadable"
    if d1 < d0:
        return None, "no complete business day in this window yet"
    closed = {d: v for d, v in live.items() if d0 <= d <= d1}
    total, have, expected = _scaled_total(closed, live, d0, d1)
    if total is None:
        return None, (f"POS returned sales for {have} of {expected} trading days in this window - "
                      "too few to state a food cost %")
    return total, _sales_note(have, expected)


# Above this, COGS / net sales is not a food cost % but a data error.
MAX_MEASURABLE_PCT = 100.0


def band_name(bench) -> str | None:
    """What kind of band a registry entry is, in the owner's words, so a
    rule of thumb never reads as an industry fact (BM1-12, Benchmarking
    #37): "industry band (NRA 2025)" for a published source, "rule-of-thumb
    band" for an operator rule of thumb, "vendor guidance" for a vendor
    figure. Reads `source_kind` (and `short`/`year` for the citation)."""
    if not bench:
        return None
    kind = bench.get("source_kind")
    if kind == "published":
        short = str(bench.get("short") or bench.get("source") or "")
        # "NRA 2025 Restaurant Operations Data Abstract" -> "NRA 2025".
        words = short.split()
        cite = " ".join(words[:2]) if len(words) >= 2 and words[1].isdigit() else \
            (f"{words[0]} {bench.get('year')}" if words and bench.get("year") else short)
        return f"industry band ({cite})" if cite else "industry band"
    if kind == "vendor":
        return "vendor guidance"
    return "rule-of-thumb band"


def band_label(pct, target=None, bench=None, starting=False):
    """How a computed food cost % reads. `target` is the restaurant's own
    when they've set one; `bench` (the Benchmark Engine's `industry`
    comparison for the restaurant's type) is the fallback, named by its
    source kind (band_name) — and only when the engine calls it
    `comparable`: a figure measured differently (the NRA median counts
    non-alcohol beverages; this is inventory COGS) or one that does not say
    what it counts is context, never a verdict (re-audit #2, R1-05/R4-3).
    With `starting`, `target` is Cavnar's starting target, not the owner's:
    the label says so and tops out at a watch (re-audit #10, R2-3). With
    none of these there is no label — never a band for a different kind of
    restaurant."""
    if pct is None:
        return None, None
    if target and starting:
        if pct <= target:
            return "Under Cavnar AI's starting target", "good"
        return "Above Cavnar AI's starting target", "warn"
    if target:
        if pct <= target:
            return "On target", "good"
        if pct <= target + 2:
            return "Slightly over", "warn"
        return "Over target", "bad"
    if not bench or bench.get("comparable") is not True:
        return None, None
    low, high = bench["low"], bench["high"]
    name = band_name(bench)
    if pct <= low:
        return f"Below the {name}", "good"
    if pct <= high:
        return f"Within the {name}", "neutral"
    return f"Above the {name}", "bad"


# A dish's food cost against the restaurant's reference (Benchmarking #35):
# dishes spread around the restaurant's average, so a plate a couple of
# points over is ordinary and one ten points over is the one to look at —
# the same spread the client-side 32/40 bands encoded against a 30% figure,
# now measured from the owner's own target or the published figure for the
# type, never a band for a different kind of restaurant.
DISH_WATCH_PTS = 2.0
DISH_HIGH_PTS = 10.0


def dish_reference(restaurant) -> dict | None:
    """What each dish's food cost % is coloured against (thresholds.
    target_for, re-audit #10): the owner's own food-cost target; else the
    top of a published band for the restaurant's type that the Benchmark
    Engine calls `comparable` (re-audit #2 — none today: the NRA median
    counts non-alcohol beverages); else Cavnar's starting target, labelled
    so, with colours capped at a watch (`dish_tone`).
    {pct, kind: "target"|"published"|"rule_of_thumb"|"vendor"|
     "starting_target", basis, target_label, target_source}."""
    if restaurant is None:
        return None
    import thresholds as _thr
    tgt = _thr.target_for(restaurant, "food")
    common = {"target_label": tgt["label"], "target_source": tgt["source"]}
    if tgt["alerts_allowed"]:
        return dict(common, pct=tgt["pct"], kind="target",
                    # "your" only when an account holder set it (INT #23).
                    basis=(f"{_thr.setter_named(restaurant, 'food', 'food-cost target')}, {tgt['pct']:g}%"
                           if tgt["source"] == "set"
                           # The owner's goal on food cost % (memory audit
                           # 9/29/26, owner_goals) is its own label.
                           else tgt["label"] if tgt["source"] == "goal"
                           else f"{_thr.STARTING_TARGET_LABEL}, {tgt['pct']:g}%"))
    ind = _engine_industry_food_cost(restaurant)
    if ind and ind.get("high") is not None and ind.get("comparable") is True:
        return dict(common, pct=float(ind["high"]), kind=ind.get("source_kind") or "rule_of_thumb",
                    basis=f"the top of the {band_name(ind)} for {ind.get('label') or 'your type'}, "
                          f"{float(ind['high']):g}% ({ind.get('source')})")
    return dict(common, pct=tgt["pct"], kind="starting_target",
                basis=f"{_thr.STARTING_TARGET_LABEL}, {tgt['pct']:g}% — not one you set")


def dish_tone(pct, reference) -> str:
    """good / warn / bad for one dish's food cost % against dish_reference(),
    or "neutral" with no reference or no figure. Against Cavnar's starting
    target a dish is at most "warn": red means over a target someone chose."""
    if pct is None or not reference or reference.get("pct") is None:
        return "neutral"
    ref = float(reference["pct"])
    if pct <= ref + DISH_WATCH_PTS:
        return "good"
    if pct <= ref + DISH_HIGH_PTS or reference.get("kind") == "starting_target":
        return "warn"
    return "bad"


def _engine_industry_food_cost(restaurant):
    """The Benchmark Engine's `industry` comparison for food cost % —
    {available, source_kind, low, high, label, source, year, inferred,
    line, …} — or None. The engine is the one place a published figure is
    chosen for a restaurant (Benchmarking #48)."""
    try:
        from intelligence import engine as _engine
        cm = _engine.compare(getattr(restaurant, "id", None), "food_cost_pct_28d", kinds=("industry",),
                             restaurant=restaurant, rows=[])
    except Exception as e:
        _degraded(e, "industry_comparison", getattr(restaurant, "id", None))
        return None
    ind = next((c for c in cm.get("comparisons") or () if c.get("kind") == "industry"), None)
    if not ind or not ind.get("available") or ind.get("low") is None or ind.get("high") is None:
        return None
    return ind


def _within(day, target, tol) -> bool:
    try:
        return abs((date.fromisoformat(str(day)[:10]) - target).days) <= tol
    except (TypeError, ValueError):
        return False


def _has_food(count) -> bool:
    """A Back Office count that carries a food account (FC-CALC-06): a
    bar-only week is not $0 of food on hand. backoffice_counts marks it
    `has_food` when it can; else any food GL value on the count."""
    if "has_food" in (count or {}):
        return bool(count["has_food"])
    return _f((count.get("by_category") or {}).get("food")) > 0


def _food_sales_window(restaurant_id, start, end, db_path=None):
    """(total, have, expected) of the nightly report's food sales over
    [start, end] under the one coverage rule (_scaled_total), trading days
    read from the eight weeks before (FC-CALC-16: a restaurant closed Sunday
    and Monday is not 2 nights short every week), or (None, have, expected)."""
    import backoffice_ap as bo
    raw = bo._food_sales(restaurant_id, (start - timedelta(days=56)).isoformat(), end.isoformat(), db_path)
    by_day = {}
    for k, v in (raw or {}).items():
        try:
            d = date.fromisoformat(str(k)[:10])
        except ValueError:
            continue
        if _f(v) > 0:
            by_day[d] = by_day.get(d, 0.0) + _f(v)
    in_window = {d: v for d, v in by_day.items() if start <= d <= end}
    return _scaled_total(in_window, by_day, start, end)


def _backoffice_components(restaurant_id, today, days, db_path=None):
    """Food cost's components for a kitchen on Back Office, or None when it
    is not one (Back Office food on file and no delivery ever logged here).

    `today` is the window's last day (build_food_cost_pct passes
    last_complete_day). The figure runs count to count: closing is the
    latest submitted food count on or before it, opening the earlier count
    nearest the day before the window starts. Purchases are Back Office's
    food invoices in between; sales the nightly report's food categories
    (backoffice_ap._food_sales), not net sales.

    The counts must bracket the window asked for (FC-CALC-01): the closing
    count within edge_tolerance(days) of its end, the opening no further
    than that before its start. A kitchen counting monthly asked for
    October's first 16 days used to get September's whole COGS, which prime
    cost added to October's labor and the outcomes engine read as "after".
    Refused instead — "counts don't bracket this window". An opening count
    INSIDE the window gives a shorter period that is still all inside it
    (two counts a fortnight apart); `covers_asked` says whether it is the
    window asked for, and every caller that needs exactly that window
    checks covers_window()."""
    from models import get_conn, DB_PATH
    import backoffice_ap as bo
    import backoffice_counts as bc
    from time_utils import mdy
    conn = get_conn(db_path or DB_PATH)
    try:
        logged = conn.execute("SELECT 1 FROM ingredient_stock_events WHERE restaurant_id=? AND event_type='receiving' "
                              "LIMIT 1", (restaurant_id,)).fetchone()
    except Exception as e:
        _degraded(e, "backoffice_components", restaurant_id)
        logged = None
    finally:
        conn.close()
    if logged or not bo.feed_state(restaurant_id, db_path).get("invoices_through"):
        return None
    w_end = today
    w_start = today - timedelta(days=days - 1)
    tol = edge_tolerance(days)
    cs = [c for c in bc.counts(restaurant_id, db_path) if c["end"] <= w_end.isoformat() and _has_food(c)]
    base = {"start": w_start, "end": w_end, "days": days, "opening": None,
            "opening_day": None, "closing": None, "closing_day": None, "purchases": None, "n_receipts": 0,
            "food_sales": None, "sales_note": None, "missing": [], "unmeasured_line": None, "covers_asked": False,
            # Short sentences, read as points in the "i" (owner, 10/17/26).
            "basis": ("Food on hand at one Back Office count, plus food bought, minus the next count. "
                      "Divided by the POS's food sales for the same days.")}
    if len(cs) < 2:
        base["missing"].append({"component": "two inventory counts",
                                "why": (f"one count submitted in Back Office ({mdy(cs[0]['end'])}); food cost "
                                        "needs the next one" if cs else "no inventory submitted in Back Office yet")})
        base["unmeasured_line"] = ("Comes with your next Back Office count" if cs
                                   else "Comes with your second Back Office count")
        return base
    close = cs[-1]
    if close.get("gl_gap"):
        # A count missing a food account the counts around it carry was not
        # read whole: never a closing figure (ING-17, the waste periods' rule).
        base["missing"].append({"component": "a whole count at the window's end",
                                "why": (f"the {mdy(close['end'])} count has no {', '.join(close['gl_gap'])} — the "
                                        "counts around it do, so a count was not read whole")})
        base["unmeasured_line"] = "Comes with your next Back Office count"
        return base
    close_gap = (w_end - date.fromisoformat(close["end"])).days
    if close_gap > tol:
        base["missing"].append({"component": "a count at the window's end",
                                "why": (f"the counts don't bracket {mdy(w_start)} to {mdy(w_end)}: the last Back "
                                        f"Office count is {mdy(close['end'])}, {close_gap} days before its end")})
        base["unmeasured_line"] = "Comes with your next Back Office count"
        return base
    open_target = w_start - timedelta(days=1)
    earlier = [c for c in cs[:-1] if (open_target - date.fromisoformat(c["end"])).days <= tol]
    whole = [c for c in earlier if not c.get("gl_gap")]
    if earlier and not whole:
        c = earlier[-1]
        base["missing"].append({"component": "a whole count at the window's start",
                                "why": (f"the {mdy(c['end'])} count has no {', '.join(c['gl_gap'])} — the counts "
                                        "around it do, so a count was not read whole")})
        return base
    earlier = whole
    if not earlier:
        base["missing"].append({"component": "a count at the window's start",
                                "why": (f"the counts don't bracket {mdy(w_start)} to {mdy(w_end)}: no Back Office "
                                        f"count within {tol} days before it starts")})
        return base
    open_ = min(earlier, key=lambda c: abs((date.fromisoformat(c["end"]) - open_target).days))
    start = date.fromisoformat(open_["end"]) + timedelta(days=1)
    end = date.fromisoformat(close["end"])
    n_days = (end - start).days + 1
    food = [e for e in bo.entries(restaurant_id, start=start.isoformat(), end=end.isoformat(), db_path=db_path)
            if e["category"] == "food"]
    sales_total, have, expected = _food_sales_window(restaurant_id, start, end, db_path)
    base.update(start=start, end=end, days=n_days, opening=open_["food"], opening_day=open_["end"],
                closing=close["food"], closing_day=close["end"],
                purchases=round(sum(float(e["amount"]) for e in food), 2), n_receipts=len(food),
                food_sales=sales_total, sales_note=_sales_note(have, expected),
                covers_asked=(abs((start - w_start).days) <= tol and abs((end - w_end).days) <= tol),
                basis=(f"Back Office counts {mdy(open_['end'])} → {mdy(close['end'])}, plus food invoices between. "
                       f"Divided by POS food sales, {mdy(start.isoformat())} – {mdy(end.isoformat())}."))
    if not food:
        base["missing"].append({"component": "purchases", "why": "no Back Office food invoices between the counts"})
        base["purchases"] = None
    if sales_total is None:
        base["missing"].append({"component": "food sales",
                                "why": f"the nightly report has food sales for {have} of {expected} trading days"})
        base["food_sales"] = None
    return base


def _beverage_window(restaurant_id, start, end, db_path=None):
    """(drink sales over [start, end] or None, why) from the nightly
    report's mapped beverage categories (dsr.kpis.BEVERAGE_CATEGORIES), per
    night under the one coverage rule and scaled like sales. (None, None)
    when no beverage category is on file at all — drinks are then not
    separable from net sales, said by the caller; (None, why) when the
    report's nights do not cover the window."""
    from models import get_conn, DB_PATH
    from dsr.kpis import BEVERAGE_CATEGORIES
    try:
        conn = get_conn(db_path or DB_PATH)
        try:
            rows = conn.execute(
                "SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
                "AND metric >= 'sales.cat:' AND metric < 'sales.cat;' AND business_date >= ? AND business_date <= ? "
                "AND value IS NOT NULL",
                (restaurant_id, (start - timedelta(days=56)).isoformat(), end.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _degraded(e, "beverage_window", restaurant_id)
        return None, None
    nights, bev, any_bev = {}, {}, False
    for r in rows:
        try:
            d = date.fromisoformat(str(r["business_date"])[:10])
        except ValueError:
            continue
        v = _f(r["value"])
        nights[d] = nights.get(d, 0.0) + v
        if " ".join(str(r["metric"])[10:].lower().split()) in BEVERAGE_CATEGORIES:
            any_bev = True
            bev[d] = bev.get(d, 0.0) + v
    if not any_bev:
        return None, None
    in_window = {d: v for d, v in nights.items() if start <= d <= end and v > 0}
    ok, have, expected = _covers_trading_days(in_window, {d for d, v in nights.items() if v > 0}, start, end)
    if not ok or not in_window:
        return None, (f"the nightly report's drink sales cover {have} of {expected} trading days, so food "
                      "sales can't be separated from net sales")
    total = sum(bev.get(d, 0.0) for d in in_window)
    if 0 < have < expected:
        total = total * expected / have
    return round(total, 2), None


def build_food_cost_pct(restaurant_id, days=DEFAULT_WINDOW_DAYS, db_path=None, today=None):
    """Actual food cost % for one restaurant over the `days` ending on its
    last complete business day (last_complete_day).

    Returns a payload that always says what it knows and what it doesn't:

        ok            — whether a percentage could be computed at all
        pct           — COGS / food sales * 100, or None
        cogs, opening, closing, purchases — the components
        food_sales    — the denominator: food sales (sales_basis says how
                        they were measured: "food_sales" from the nightly
                        report's food categories, "net_less_beverage" net
                        sales less its drink sales, or "net_sales" when
                        drinks are not separable on file — said in `basis`)
        net_sales     — POS net sales over the window (inventory path)
        start, end    — the period ACTUALLY measured, count to count;
                        asked_start / asked_end the window asked for, and
                        covers_asked whether they match (covers_window)
        target        — restaurants.food_cost_target
        missing       — list of {component, why} for anything absent
        basis         — one sentence naming the identity and the window
        theoretical   — on a ledger kitchen with no full count at an edge,
                        the recipes' theoretical figure, labelled so and
                        never `ok` (FC-CALC-02)

    A missing component yields ok=False and pct=None. It never yields a
    number computed from a substituted zero.
    """
    from models import get_restaurant
    from waste_trend import load_waste_history
    from time_utils import mdy as _mdy

    end = last_complete_day(restaurant_id, today)
    start = end - timedelta(days=days - 1)
    asked_start, asked_end = start, end
    tol = edge_tolerance(days)

    restaurant = get_restaurant(restaurant_id)
    # The one target read (thresholds.target_for, re-audit #10): the
    # restaurants column defaults to 30, so "a target exists" was always
    # true and Cavnar's 30% read as the owner's "Over target" in red.
    import thresholds as _thr
    _tgt = _thr.target_for(restaurant, "food") if restaurant is not None else None
    target = _tgt["pct"] if _tgt else None
    owners_target = bool(_tgt and _tgt["alerts_allowed"])

    bo = _backoffice_components(restaurant_id, end, days, db_path)
    unmeasured_line = None
    theoretical = None
    counted = False
    net_sales = None
    if bo is not None:
        # A kitchen on Back Office (no deliveries logged here, Back Office
        # food on file): count to count from its submitted inventories, its
        # Back Office food invoices between them, over the POS's FOOD sales
        # (10/16/26) - net sales carried the bar, which a food cost % never does.
        start, end, days = bo["start"], bo["end"], bo["days"]
        opening, opening_day = bo["opening"], bo["opening_day"]
        closing, closing_day = bo["closing"], bo["closing_day"]
        purchases, n_receipts = bo["purchases"], bo["n_receipts"]
        food_sales, last_delivery = bo["food_sales"], None
        sales_basis, sales_note = "food_sales", bo.get("sales_note")
        missing, basis, unmeasured_line = bo["missing"], bo["basis"], bo["unmeasured_line"]
        covers_asked = bool(bo.get("covers_asked"))
        counted = True
    else:
        missing = []
        # Only the snapshots that can be the opening or closing count are read:
        # within the edge tolerance of the window (MOD-FC-18). A snapshot
        # written the morning after the closing day still dates its count
        # to that day, so one day past the end is read too.
        weeks, _total = load_waste_history(
            restaurant_id, None, db_path=db_path,
            since=start - timedelta(days=tol + 1),
            until=end + timedelta(days=1), include_unmeasured=True, snapshots=True)
        # Each snapshot is dated by its count day (`counted_on`) when it is a
        # count, and none dated after the window's last complete day can
        # close it — a closing count today would carry deliveries and usage
        # of a day whose sales are not in yet (FC-CALC-15).
        anchored = []
        for w in weeks or []:
            day = str(w.get("counted_on") or w.get("week_end") or "")[:10]
            if day and day <= end.isoformat():
                anchored.append(dict(w, week_end=day))
        full = [w for w in anchored if is_counted(w)]

        def _near(ws, day):
            return inventory_value_near([w for w in ws if _within(w.get("week_end"), day, tol)], day)

        # Opening is the stock at the close of the day before the window,
        # closing at the close of its last day (FC-CALC-22). Only a full
        # count anchors the measured figure (FC-CALC-02); the ledger's own
        # figure between counts is the recipes' theoretical stock.
        opening, opening_day = _near(full, start - timedelta(days=1))
        closing, closing_day = _near(full, end)
        counted = opening is not None and closing is not None and opening_day != closing_day
        if not counted:
            o2, od2 = (opening, opening_day) if opening is not None else _near(anchored, start - timedelta(days=1))
            c2, cd2 = (closing, closing_day) if closing is not None else _near(anchored, end)
            open_counted, close_counted = opening is not None, closing is not None
            opening, opening_day, closing, closing_day = o2, od2, c2, cd2
        else:
            open_counted = close_counted = True

        # Owner-facing reasons (they reach Track warnings and the Food Cost
        # tab): dates M/D/YY (re-audit A34).
        if opening is None:
            missing.append({"component": "opening inventory",
                            "why": f"no counted inventory value within {tol} days of {_mdy(start)}"})
        if closing is None:
            missing.append({"component": "closing inventory",
                            "why": f"no counted inventory value within {tol} days of {_mdy(end)}"})
        # Two snapshots that are actually the same count can't bracket a period.
        if opening_day and closing_day and opening_day == closing_day:
            missing.append({
                "component": "a second inventory count",
                "why": "opening and closing resolve to the same count, so nothing brackets the window",
            })
            closing = None
        uncounted_edges = [w for w, ok_ in (("opening", open_counted), ("closing", close_counted))
                           if not ok_ and (opening if w == "opening" else closing) is not None]
        for edge in uncounted_edges:
            day = opening_day if edge == "opening" else closing_day
            missing.append({
                "component": f"{edge} inventory", "theoretical": True,
                "why": (f"no full count within {tol} days of {_mdy(start if edge == 'opening' else end)} — the "
                        f"stock on {_mdy(day)} is the last count plus deliveries minus what the recipes say was "
                        "used, the recipes' theoretical figure, not a count")})

        # Purchases and sales run over the counted period itself, the day
        # after the opening count through the closing count (FC-CALC-22,
        # POS-15): purchases in the days between the window's edge and a
        # count nine days inside it were on the opening shelf AND in
        # purchases.
        if opening_day and closing_day and opening_day < closing_day:
            start = date.fromisoformat(opening_day) + timedelta(days=1)
            end = date.fromisoformat(closing_day)
            days = (end - start).days + 1
        covers_asked = abs((start - asked_start).days) <= tol and abs((end - asked_end).days) <= tol

        # COGS rests on the deliveries logged in the window: with none, food
        # cost % would read opening − closing alone and look excellent exactly
        # when the owner stopped logging deliveries (DH3-4). Withheld, naming
        # the last delivery that was logged.
        purchases, n_receipts = purchases_in_window(restaurant_id, start, end, db_path=db_path)
        last_delivery = None
        if n_receipts == 0:
            try:
                last_delivery = last_delivery_date(restaurant_id, db_path=db_path)
            except Exception:
                last_delivery = None
            missing.append({
                "component": "purchases",
                "why": (f"deliveries not logged since {_mdy(last_delivery)}" if last_delivery
                        else "no deliveries have been logged"),
            })

        net_sales, sales_why = net_sales_in_window(restaurant_id, start, end)
        sales_note = sales_why if net_sales is not None else None
        food_sales, sales_basis = None, None
        if net_sales is None:
            missing.append({"component": "net sales", "why": sales_why or "unavailable"})
        else:
            # Food cost is food COGS over FOOD sales on every path
            # (FC-CALC-11): the ledger's COGS holds no alcohol (is_bar_item),
            # so dividing by net sales read a 40%-bar restaurant's true 32%
            # as 19.2% "On target" while the same kitchen on Back Office
            # read 32%.
            try:
                bev, bev_why = _beverage_window(restaurant_id, start, end, db_path=db_path)
            except Exception as e:
                _degraded(e, "beverage_window", restaurant_id)
                bev, bev_why = None, None
            if bev is not None and bev < net_sales:
                food_sales, sales_basis = round(net_sales - bev, 2), "net_less_beverage"
            elif bev_why:
                missing.append({"component": "food sales", "why": bev_why})
            else:
                food_sales, sales_basis = net_sales, "net_sales"

        what = ("food sales (POS net sales less the nightly report's drink sales)"
                if sales_basis == "net_less_beverage" else
                "POS net sales — drink sales aren't separated on file, so a restaurant that sells drinks reads low")
        basis = ((f"Counted inventory {_mdy(opening_day)} → {_mdy(closing_day)}, plus purchases between. "
                  f"Divided by {what}. {_mdy(start)} – {_mdy(end)}.") if counted and opening_day and closing_day else
                 (f"Opening inventory, plus purchases, minus closing inventory. Divided by {what}. "
                  f"{_mdy(start)} – {_mdy(end)}."))

    if sales_note:
        basis += f" Sales: {sales_note}."
    payload = {
        "ok": False,
        "pct": None,
        "cogs": None,
        "opening": opening, "opening_date": opening_day,
        "closing": closing, "closing_date": closing_day,
        "purchases": purchases if n_receipts else None,
        "purchase_events": n_receipts,
        "last_delivery": last_delivery,
        # The one denominator (FC-CALC-11): food sales, and how they were
        # measured. net_sales is the POS's net over the same days, kept
        # beside it on the inventory path; a Back Office kitchen has none.
        "food_sales": food_sales,
        "sales_basis": sales_basis,
        "sales_estimate": sales_note,
        "net_sales": net_sales,
        "target": target,
        # Whose target it is — the label both apps render (re-audit #10).
        "food_cost_target_label": _tgt["label"] if _tgt else None,
        "food_cost_target_source": _tgt["source"] if _tgt else None,
        "window_days": days,
        "start": start.isoformat(), "end": end.isoformat(),
        "asked_start": asked_start.isoformat(), "asked_end": asked_end.isoformat(),
        "covers_asked": covers_asked,
        "counted": counted,
        "missing": missing,
        "basis": basis,
        "label": None, "tone": None, "variance_pts": None,
        # What the owner reads in place of the figure, when a source can say
        # exactly what it waits on ("Comes with your second count…").
        "unmeasured_line": unmeasured_line,
        "theoretical": None,
        "source": "back_office" if bo is not None else "inventory",
    }
    only_uncounted = bool(missing) and all(m.get("theoretical") for m in missing)
    if missing and not only_uncounted:
        return payload

    cogs = round(opening + purchases - closing, 2)
    if only_uncounted:
        # The recipes' theoretical cost, labelled so and never `ok`: no
        # caller adds it to labor, and no screen calls it COGS (FC-CALC-02).
        if cogs >= 0 and food_sales:
            theoretical = {"cogs": cogs, "pct": round(cogs / food_sales * 100, 1),
                           "label": "by recipe (theoretical)",
                           "basis": ("By recipe (theoretical): the ledger's stock between counts is the last count "
                                     "plus deliveries minus what the recipes say was used, so this is recipe "
                                     "depletion plus logged waste — over-portioning, theft and dishes with no "
                                     "recipe don't show until a full count lands inside the window.")}
        payload["theoretical"] = theoretical
        return payload
    payload["cogs"] = cogs
    if cogs < 0:
        # Physically possible only if a count is wrong or deliveries went
        # unrecorded. Reporting a negative food cost % would be nonsense.
        payload["missing"] = [{
            "component": "a consistent count",
            "why": "closing inventory exceeds opening plus everything recorded as received, "
                   "which means a delivery went unrecorded or a count is wrong",
        }]
        return payload

    pct = round(cogs / food_sales * 100, 1)
    if pct > MAX_MEASURABLE_PCT:
        # More food cost than sales is not a kitchen, it is a count, a
        # delivery or a sales window that is wrong. Refused as not
        # measurable rather than shown (CA3 F14).
        payload["cogs"] = cogs
        payload["missing"] = [{
            "component": "a consistent count",
            "why": f"food cost would be {pct:.0f}% of sales — a count, an unrecorded delivery "
                   "or missing sales days, not a measurement",
        }]
        return payload
    # The owner's target decides the label. Without one, the Benchmark
    # Engine's `industry` band (the one place a published figure is chosen;
    # Benchmarking #48) decides it only when it is measured the same way —
    # otherwise it rides along as context — and the label falls to Cavnar's
    # starting target, named so and capped at a watch (re-audit #2, #10).
    bench = None if owners_target else _engine_industry_food_cost(restaurant)
    if owners_target:
        label, tone = band_label(pct, target)
    else:
        label, tone = band_label(pct, bench=bench)
        if label is None and target:
            label, tone = band_label(pct, target, starting=True)
    payload.update({
        "ok": True,
        "pct": pct,
        "label": label,
        "tone": tone,
        "variance_pts": round(pct - target, 1) if target else None,
        # Which band the label was read against, or sits beside as context,
        # with its source, year, its kind, whether the restaurant's type was
        # inferred (NS4 H3, M5), and whether it is a like-for-like figure
        # (`comparable`, with the engine's `definition_note` when not).
        "benchmark": ({"low": bench["low"], "high": bench["high"], "label": bench.get("label"),
                       "source": bench.get("source"), "year": bench.get("year"),
                       "source_kind": bench.get("source_kind"), "band_name": band_name(bench),
                       "inferred": bool(bench.get("inferred")), "line": bench.get("line"),
                       "comparable": bench.get("comparable") is True,
                       "definition_note": bench.get("definition_note")}
                      if bench else None),
    })
    return payload
