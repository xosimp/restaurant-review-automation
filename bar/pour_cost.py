"""
bar/pour_cost.py — the bar's cost against its sales by category.

Today the reading is PURCHASE-based: what the bar bought from distributors
in a window as a % of what the POS sold — "Bought vs sold" (basis
"purchases"), never called pour cost. "Pour cost (counted)" (basis
"counted") is the figure two counts bracketing a window give (bar/counts.py,
W6a); the label and every sentence follow `basis`.

The window is the four weeks ending on the last day invoices and sales are
both on file. Every tone, dollar figure over a range or target, and verdict
passes the one policy in bar.judge (Decision 10). Around it: the gap
detector (a distributor whose invoices are missing — said, never judged),
the total over the categories measured on both sides, the week-by-week
trend with its own noise band, a 13-week context reading, and month rows on
the restaurant's fiscal periods when it keeps them (dsr.fiscal).

The ranges are for COUNTED pour cost, from benchmark_registry with their
source and year; a category with no citable range (beer & seltzer as one
category) carries Cavnar AI's starting range, said as such.
"""
from bisect import bisect_left, bisect_right
from datetime import date, timedelta

import benchmark_registry as _br
from bar.common import LABELS, POUR_CATEGORIES, TREND_POINTS, WINDOW_DAYS, _mdy, _money
from bar.judge import (HISTORY_DAYS, MIN_DAYS, REASONS, freshness, houses, judge, open_days, stock_up,
                       stopped, usual_open_share)
from bar.ledger import as_lines, as_sales, ledger_of, sales_index

# ── ranges ────────────────────────────────────────────────────────────────
# Where each category's range comes from (benchmark_registry, definition
# counted_pour_cost). Beer & seltzer has no published blended range.
_REGISTRY_METRIC = {"beer": None, "liquor": "pour_cost_liquor_pct", "wine": "pour_cost_wine_pct"}
RANGE_DEFINITION = "counted_pour_cost"
# Cavnar AI's own starting range: beer & seltzer always (set between the
# published draft 15–18% and package 24–28% figures), and any category whose
# registry entry has aged out.
STARTING_RANGES = {"beer": (20.0, 26.0), "liquor": (14.0, 20.0), "wine": (30.0, 40.0)}
STARTING_RANGE_LABEL = "Cavnar AI's starting range"
_STARTING_NOTE = {"beer": "No published range covers draft, package and seltzer as one category; this one sits "
                          "between the published draft (15–18%) and bottled (24–28%) figures."}


def range_for(cat, today=None) -> dict:
    """{range: [lo, hi], label, source, short, year, definition, kind} for a
    pour category: its registry entry ("vendor", quoted with source and
    year), else Cavnar AI's starting range ("starting"). Both describe
    COUNTED pour cost."""
    metric = _REGISTRY_METRIC.get(cat)
    e = _br.lookup(metric, None, definition=RANGE_DEFINITION, today=today) if metric else None
    if e:
        return {"range": [e["low"], e["high"]], "label": f"Range for counted pour cost — {_br.cite(e)}",
                "source": e["source"], "short": e["short"], "year": e["year"], "definition": RANGE_DEFINITION,
                "kind": e.get("source_kind") or "vendor"}
    lo, hi = STARTING_RANGES[cat]
    return {"range": [lo, hi], "label": STARTING_RANGE_LABEL,
            "source": _STARTING_NOTE.get(cat) or "No current published range; Cavnar AI's starting range.",
            "short": STARTING_RANGE_LABEL, "year": None, "definition": RANGE_DEFINITION, "kind": "starting"}


# The range each pour category is held to, as (lo, hi) (the facade's name;
# read through range_for, which knows where each comes from).
RULE_OF_THUMB = {c: tuple(range_for(c)["range"]) for c in POUR_CATEGORIES}

# ── names ─────────────────────────────────────────────────────────────────
BASES = ("purchases", "counted")
BASIS_LABELS = {"purchases": "Bought vs sold", "counted": "Pour cost (counted)"}
BASIS_SUBS = {"purchases": "what you bought from distributors as a % of what the POS sold",
              "counted": "what was poured — opening count + purchases − closing count — as a % of what the POS sold"}

# Month rows kept in the table (most recent).
MONTH_ROWS = 12
# The 13-week context reading.
CONTEXT_DAYS = 91
# A trend move is said only beyond this many points, or the series' own
# noise (NOISE_MULTIPLE x its median weekly change) if that is larger.
TREND_NOISE_FLOOR = 3.0
NOISE_MULTIPLE = 2.0
# A house's spend on file below this share of what its rhythm expects, or a
# category's below this share of its trailing rate, is short.
SHORT_SHARE = 0.6
# A house silent for over this multiple of its own rhythm (at least ten
# days) is overdue: a biweekly house three weeks quiet is (V9).
OVERDUE_MULTIPLE = 1.25
# Under this share of a silent span's days open, the bar was closed: no gap.
CLOSED_SHARE = 0.5


def _d(iso):
    return date.fromisoformat(str(iso)[:10])


def _days(a, b):
    return (_d(b) - _d(a)).days + 1


def _window(lines, sales, start, end) -> dict:
    """{cat: {key, label, purchases, sales, pct, ratio, tone, range,
    days_with_sales, credits_exceed}} for [start, end]. `ratio` is the
    unrounded purchases ÷ sales (dollar lines are computed from the raw
    sums, R27); `pct` is it as a rounded %, None unless both sides are
    positive. Net credits over purchases are `credits_exceed`, never "no
    invoices" (V37)."""
    out = {}
    lg, si = ledger_of(lines), sales_index(sales)
    for cat in POUR_CATEGORIES:
        bought = sum(l["net"] for l in lg.in_cat(cat, start, end))
        sold_days = dict(si.positive(cat, start, end))
        sold = sum(sold_days.values())
        ratio = bought / sold if sold > 0 and bought > 0 else None
        out[cat] = {"key": cat, "label": LABELS[cat], "purchases": round(bought, 2), "sales": round(sold, 2),
                    "_bought": bought, "_sold": sold,
                    "pct": round(ratio * 100, 1) if ratio is not None else None, "tone": None,
                    "range": list(RULE_OF_THUMB[cat]), "days_with_sales": len(sold_days),
                    "credits_exceed": bought < 0}
    return out


def _unbalanced(lines, start, end) -> set:
    """Pour categories with a line in [start, end] from an invoice that did
    not add up to its total (`balanced` exposed per line by the ingest; a
    line without the key is taken as balanced)."""
    out = set()
    for l in ledger_of(lines).between(start, end):
        if l.get("balanced") in (0, False) and l["category"] in POUR_CATEGORIES:
            out.add(l["category"])
    return out


def _cat_short(lines, cat, start, end, n_days, open_set=None) -> bool:
    """Whether `cat`'s purchases on file in [start, end] fall short of its own
    trailing rate (the HISTORY_DAYS before `start`) over `n_days`: another
    house that took over the category covers a quiet one. With under four
    weeks of history it cannot be told, and counts as short."""
    hist_lo = (_d(start) - timedelta(days=HISTORY_DAYS)).isoformat()
    lg = ledger_of(lines)
    hist = [(str(l["invoice_date"])[:10], l["net"]) for l in lg.cat_before(cat, hist_lo, start)]
    if not hist:
        return True
    first = min(d for d, _v in hist)
    span = (_d(start) - _d(first)).days
    if span < MIN_DAYS:
        return True
    rate = sum(v for _d0, v in hist) / span
    on_file = sum(l["net"] for l in lg.in_cat(cat, start, end))
    return on_file < SHORT_SHARE * rate * n_days


def _gaps(lines, start, end, sales=None, owner_stopped=()) -> list:
    """Distributors whose invoices are missing inside [start, end]:
    [{distributor, from, to, from_mdy, to_mdy, categories, typical_days}].

    A house is overdue when it is silent for over 1.25x its own rhythm (at
    least 10 days) AND what it has on file falls short of what that rhythm
    expects over the days the bar was open. Never a gap: before a house's
    first invoice; a house that stopped (bar.judge.stopped — the owner said
    so, another house took its categories over, or silent HISTORY_DAYS);
    a silent span when the bar was closed too; a category another house
    covered. A biweekly or a three-invoice house is checked like any other
    (V9). Simple EJ's history came from Fintech's payment-activity export
    (10/14/26) — only PAID invoices — so liquor read 7%: a window with a gap
    is said, never judged."""
    lines = as_lines(lines)
    lg = lines.ledger
    hs = houses(lines, end)
    gone = {r["distributor"] for r in stopped(lines, end, owner_stopped)}
    has_sales = bool(sales) and any(sales.get(c) for c in POUR_CATEGORIES)
    window_open = open_days(sales, start, end) if has_sales else None
    out = []
    for dist, h in hs.items():
        if dist in gone or not h["cats"] or not h["typical"]:
            continue
        ds = h["dates"]
        # ds is sorted: the last date before the window (all _gaps reads of
        # `before`) and the dates inside it, by bisect.
        k = bisect_left(ds, start)
        before = ds[k - 1:k]
        inside = ds[k:bisect_right(ds, end)]
        if not before and not inside:
            continue
        span_start = start if before else inside[0]
        n_days = (len([d for d in window_open if d >= span_start]) if has_sales else _days(span_start, end))
        expected = h["amount"] * n_days / h["typical"]
        on_file = sum(l["net"] for l in lg.in_dist(dist, span_start, end) if l["category"] in POUR_CATEGORIES)
        if expected <= 0 or on_file >= SHORT_SHARE * expected:
            continue
        limit = max(10, OVERDUE_MULTIPLE * h["typical"])
        marks = ([before[-1]] if before else []) + inside + ([end] if (not inside or inside[-1] != end) else [])
        for a, b in zip(marks, marks[1:]):
            if (_d(b) - _d(a)).days <= limit:
                continue
            lo = max(start, (_d(a) + timedelta(days=1)).isoformat())
            hi = (_d(b) - timedelta(days=1)).isoformat() if b in inside else end
            if lo > hi:
                continue
            if has_sales:
                opened = len([d for d in window_open if lo <= d <= hi])
                if opened < CLOSED_SHARE * _days(lo, hi):
                    continue
            short = [c for c in h["cats"] if _cat_short(lines, c, start, end, len(window_open) if has_sales
                                                         else _days(start, end))]
            if not short:
                continue
            out.append({"distributor": dist, "from": lo, "to": hi, "from_mdy": _mdy(lo), "to_mdy": _mdy(hi),
                        "categories": short, "typical_days": h["typical"]})
    return out


def _total(cats, mixed=0.0) -> dict:
    """The total over the categories measured on BOTH sides and complete
    (V10): {purchases, sales, pct, ratio, partial, excluded:[{key, label,
    why, text}], included}. A category bought with no sales on the report,
    sold with no invoices, with invoices missing or credits over purchases
    is left out and named; the total is never a blend of one-sided money."""
    inc, exc = [], []
    for c in cats.values():
        b, s = c["_bought"], c["_sold"]
        if c.get("incomplete"):
            exc.append({"key": c["key"], "label": c["label"], "why": "incomplete",
                        "text": f"Some {c['label'].lower()} invoices aren't on file"})
        elif b < 0:
            exc.append({"key": c["key"], "label": c["label"], "why": "credits_exceed",
                        "text": f"{c['label']} credits exceed purchases in this window"})
        elif b > 0 and s <= 0:
            exc.append({"key": c["key"], "label": c["label"], "why": "no_sales",
                        "text": f"{c['label']} bought {_money(b)} with no {c['label'].lower()} sales on the report"})
        elif s > 0 and b == 0:
            exc.append({"key": c["key"], "label": c["label"], "why": "no_purchases",
                        "text": f"{c['label']} sold {_money(s)} with no {c['label'].lower()} invoices on file"})
        elif b > 0 and s > 0:
            inc.append(c)
    # Alcohol bought mixed (a store's receipt: beer, wine and liquor at once)
    # belongs to the total alone — never to one category (owner, 10/16/26).
    mixed = float(mixed or 0) if inc else 0.0
    bought = sum(c["_bought"] for c in inc) + mixed
    sold = sum(c["_sold"] for c in inc)
    ratio = bought / sold if sold > 0 and bought > 0 else None
    return {"purchases": round(bought, 2), "sales": round(sold, 2), "_bought": bought, "_sold": sold,
            "ratio": ratio, "pct": round(ratio * 100, 1) if ratio is not None else None,
            "partial": bool(exc), "excluded": exc, "included": [c["key"] for c in inc],
            "mixed_alcohol": round(mixed, 2)}


def _read(lines, sales, start, end, owner_stopped=()) -> dict:
    """One window read the same way everywhere (the four weeks, each trend
    point, each month row, the 13-week context): categories with their
    incomplete / unbalanced flags, the gaps, and the total."""
    lines, sales = as_lines(lines), as_sales(sales)
    cats = _window(lines, sales, start, end)
    gaps = _gaps(lines, start, end, sales, owner_stopped)
    missing = {c for g in gaps for c in g["categories"]}
    bad = _unbalanced(lines, start, end)
    for c in POUR_CATEGORIES:
        if c in missing:
            cats[c]["incomplete"] = True
        if c in bad:
            cats[c]["unbalanced"] = True
    mixed = sum(l["net"] for l in ledger_of(lines).in_cat("alcohol_mixed", start, end))
    return {"cats": cats, "gaps": gaps, "total": _total(cats, mixed), "unbalanced": sorted(bad)}


def _public(row) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _noise(values):
    """2x the median absolute change between consecutive weekly readings,
    at least TREND_NOISE_FLOOR points; None under three changes."""
    diffs = sorted(abs(b - a) for a, b in zip(values, values[1:]) if a is not None and b is not None)
    if len(diffs) < 3:
        return None
    return round(max(TREND_NOISE_FLOOR, NOISE_MULTIPLE * diffs[len(diffs) // 2]), 1)


def _target_info(v, kind):
    """A target given as thresholds.bar_target_for's dict, or a bare number
    (tests, older callers), as {pct, label, phrase, setter}; None unset."""
    if isinstance(v, dict):
        return v if v.get("pct") is not None else None
    if v is None:
        return None
    import thresholds as _thr
    pct = _thr.bar_target_value(v)
    if pct is None:
        return None
    return {"kind": kind, "pct": pct, "source": "set", "setter": None, "label": "your target",
            "phrase": f"your {pct:g}% target"}


def _judge(x, target):
    """The facade's name (kept): set the owner's target on a category row.
    Judging itself is bar.judge.judge plus _verdict."""
    t = _target_info(target, x.get("key"))
    x["target"] = t["pct"] if t else None
    x["target_source"] = "owner" if t else "industry"


def _verdict(x, jt, scope):
    """Tone and dollar figures for a JUDGED row x against its range and the
    target that judges it (jt; scope "category" | "overall"). Dollars come
    from the raw sums (R27). Under a target the range's own tone and its
    caveat stay (V18): a reading under the target but below the range is
    "low", never plain good."""
    lo, hi = x["range"]
    ratio = x["_bought"] / x["_sold"]
    pct = ratio * 100
    x["range_tone"] = "low" if pct < lo else ("above" if pct > hi else "inside")
    over_r = x["_bought"] - hi / 100 * x["_sold"]
    x["over_range_usd"] = round(over_r, 2) if pct > hi else None
    if jt:
        over_t = x["_bought"] - jt["pct"] / 100 * x["_sold"]
        x["target_tone"] = "over" if over_t > 0 else "under"
        x["tone"] = "warn" if over_t > 0 else ("low" if x["range_tone"] == "low" else "good")
        if scope == "category":
            x["over_target_usd"] = round(over_t, 2) if over_t > 0 else None
        else:
            x["over_overall_usd"] = round(over_t, 2) if over_t > 0 else None
    elif x.get("basis") == "counted":
        x["tone"] = {"low": "low", "inside": "good", "above": "warn"}[x["range_tone"]]
    else:
        # The ranges describe COUNTED pour cost: against a purchase ratio
        # they are context (`range_tone`), never a verdict tone (V1).
        x["tone"] = None


def _rows_spans(lo, hi, restaurant=None) -> list:
    """[(key, label, kind, a, b)] for the table: the restaurant's fiscal
    periods (dsr.fiscal, e.g. EJ's 4-4-5 from 12/31/25) when it keeps them,
    else calendar months — each clipped to [lo, hi], the latest MONTH_ROWS."""
    spans = []
    fiscal = None
    if restaurant is not None:
        try:
            from dsr import fiscal as _fiscal
            if _fiscal.period_span(restaurant, hi) is not None:
                fiscal = _fiscal
        except Exception as e:
            print(f"[bar.pour_cost] fiscal calendar unreadable: {e}")
            fiscal = None
    if fiscal is not None:
        day = _d(hi)
        while len(spans) < MONTH_ROWS:
            span = fiscal.period_span(restaurant, day)
            if span is None:
                break
            a, b = span
            if b.isoformat() < lo:
                break
            p = fiscal.position(restaurant, a)
            fy = p.get("fiscal_year")
            label = f"Period {p.get('period')}" + (f" · FY{str(fy)[2:]}" if fy else "")
            spans.append((f"FY{fy}-P{p.get('period')}", label, "period", a.isoformat(), b.isoformat()))
            day = a - timedelta(days=1)
        return list(reversed(spans))
    m = _d(hi).replace(day=1)
    while len(spans) < MONTH_ROWS and (m.replace(day=28) + timedelta(days=4)).replace(day=1).isoformat() > lo:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        spans.append((m.strftime("%Y-%m"), m.strftime("%b %Y"), "month", m.isoformat(),
                      (nxt - timedelta(days=1)).isoformat()))
        m = (m - timedelta(days=1)).replace(day=1)
    return list(reversed(spans))


def window_span(lines, sales, today) -> dict:
    """{lo, hi, from, to} — the overlap of invoices and sales on file through
    today, and the four-week window ending where both are on file — or
    {why_not} when there is none. bar.build asks bar.counts for a counted
    period over this window before the reading is built."""
    today_iso = today.isoformat()
    inv_dates = ledger_of(lines).days_through(today_iso)
    sale_dates = [d for d in sales_index(sales).all_days if d <= today_iso]
    if not inv_dates or not sale_dates:
        return {"why_not": ("No distributor invoices on file yet." if not inv_dates
                            else "No sales by category on file yet — they come from the nightly report.")}
    lo = max(inv_dates[0], sale_dates[0])
    hi = min(inv_dates[-1], sale_dates[-1])
    if lo > hi:
        return {"why_not": "The invoices and the sales on file don't overlap yet."}
    return {"lo": lo, "hi": hi, "to": hi, "from": max(lo, (_d(hi) - timedelta(days=WINDOW_DAYS - 1)).isoformat())}


def _pour_cost(lines, sales, today, targets=None, feed=None, owner_stopped=(), restaurant=None,
               counted=None, sales_gaps=()) -> dict:
    """The page's reading. `targets` maps None (overall) / beer / liquor /
    wine to thresholds.bar_target_for's dict (or a bare number); `feed` is
    the invoice feed's status (bar.feed, W2; None = unknown); `counted` is
    the counted period covering this window (bar.counts.latest_counted, W6a)
    or None; `sales_gaps` the nights whose drink sales weren't sorted
    (bar.sales, V5) — one inside the window keeps it from being judged.
    The basis is "counted" when every measured category rests on
    two counts, else "purchases" (each category says its own `basis`)."""
    head = {"basis": "purchases", "label": BASIS_LABELS["purchases"], "sub": BASIS_SUBS["purchases"]}
    # Every window below is a slice of one index (bar.ledger, V17).
    lines, sales = as_lines(lines), as_sales(sales)
    span = window_span(lines, sales, today)
    if "why_not" in span:
        return dict(head, available=False, why_not=span["why_not"])
    lo, hi, start, end = span["lo"], span["hi"], span["from"], span["to"]
    days = _days(start, end)
    fresh = freshness(lines, sales, today, feed)
    ranges = {c: range_for(c, today) for c in POUR_CATEGORIES}
    usual = usual_open_share(sales, start)
    win_open = open_days(sales, start, end)
    gap_nights = tuple(d for d in sales_gaps if start <= d <= end)
    w = _read(lines, sales, start, end, owner_stopped)
    cur, gaps = w["cats"], w["gaps"]
    trend = _trend(lines, sales, lo, hi, owner_stopped)
    noise = {k: _noise([p[k] for p in trend]) for k in ("total",) + POUR_CATEGORIES}
    earlier = trend[-5] if len(trend) >= 5 else None
    targets = targets or {}
    overall = _target_info(targets.get(None), "bar")
    for c in POUR_CATEGORIES:
        x = cur[c]
        rs = ranges[c]
        x["range"] = list(rs["range"])
        x["range_source"] = {k: rs[k] for k in ("label", "source", "short", "year", "definition", "kind")}
        x["spark"] = [p[c] for p in trend]
        x["prev_pct"] = earlier[c] if earlier else None
        x["prev_end_mdy"] = earlier["end_mdy"] if earlier else None
        own = _target_info(targets.get(c), c)
        _judge(x, own)
        jt, scope = (own, "category") if own else ((overall, "overall") if overall else (None, None))
        x["target_label"] = own["label"] if own else None
        x["judge_target"] = ({"pct": jt["pct"], "scope": scope, "label": jt["label"],
                              "phrase": (jt["phrase"] if scope == "category"
                                         else jt["phrase"].replace(" target", " overall target", 1))}
                             if jt else None)
        x.update({"range_tone": None, "target_tone": None, "over_range_usd": None, "over_target_usd": None,
                  "over_overall_usd": None})
        cc = ((counted or {}).get("categories") or {}).get(c) or {}
        if cc.get("pct") is not None and (cc.get("sales") or 0) > 0 and cc.get("used") is not None:
            # Two counts bracket this window (bar.counts): the reading IS pour
            # cost — what was used, not what was bought — and the counts'
            # own checks (no gap, sales coverage) stand behind it.
            x["basis"] = "counted"
            x["purchase_pct"] = x["pct"]
            x["counted"] = {"pct": cc["pct"], "used": cc["used"], "sales": cc["sales"], "begin": cc.get("begin"),
                            "end": cc.get("end"), "from_mdy": counted.get("from_mdy"), "to_mdy": counted.get("to_mdy")}
            x["_bought"], x["_sold"], x["pct"] = float(cc["used"]), float(cc["sales"]), cc["pct"]
            x.pop("incomplete", None)
            x["stock_up"] = None
            v = judge(x["pct"], x["_bought"], window_days=_days(counted["from"], counted["to"]), fresh=fresh,
                      unbalanced=bool(x.get("unbalanced")),
                      sales_gap=tuple(d for d in sales_gaps if counted["from"] < d <= counted["to"]))
        else:
            x["basis"] = "purchases"
            # The range rule (a reading over twice the range's top) is only
            # read on a whole window: a short one is not judged for being short.
            x["stock_up"] = (stock_up(lines, c, start, end, x["pct"] if days >= MIN_DAYS else None, rs["range"][1])
                             if x["pct"] is not None else None)
            v = judge(x["pct"], x["_bought"], window_days=days, open_n=len(win_open), have_n=x["days_with_sales"],
                      usual_share=usual, fresh=fresh, stock=x["stock_up"], incomplete=bool(x.get("incomplete")),
                      unbalanced=bool(x.get("unbalanced")), sales_gap=gap_nights)
        x.update(v)
        if v["judged"]:
            _verdict(x, jt, scope)
        else:
            x["tone"] = None
    # The total over what each category's reading rests on (used, for a
    # counted category); with no count it is the trend's own figure.
    tot = _total(cur) if counted else w["total"]
    measured = [cur[c]["basis"] for c in POUR_CATEGORIES if cur[c]["pct"] is not None]
    if measured and all(b == "counted" for b in measured):
        head = {"basis": "counted", "label": BASIS_LABELS["counted"], "sub": BASIS_SUBS["counted"]}
    inc = [cur[k] for k in tot["included"]]
    weights = {c["key"]: c["_sold"] for c in inc}
    wsum = sum(weights.values())
    mix_range = ([round(sum(ranges[k]["range"][0] * v for k, v in weights.items()) / wsum, 1),
                  round(sum(ranges[k]["range"][1] * v for k, v in weights.items()) / wsum, 1)] if wsum else None)
    tv = judge(tot["pct"], tot["_bought"], window_days=days, open_n=len(win_open), usual_share=usual, fresh=fresh,
               incomplete=bool(gaps), unbalanced=bool(w["unbalanced"]), sales_gap=gap_nights,
               stock=next((c["stock_up"] for c in inc if c.get("stock_up")), None))
    extra = sorted({r for c in inc for r in c["reasons"] if r == "sales_coverage"})
    if extra:
        tv["reasons"] = sorted(set(tv["reasons"]) | set(extra), key=REASONS.index)
        tv["judged"] = False
        if not tv["not_judged"]:
            tv["not_judged"] = extra[0]
            tv["not_judged_text"] = "Some categories' sales aren't on file for every open day yet."
    total = {"purchases": tot["purchases"], "sales": tot["sales"], "pct": tot["pct"],
             "partial": tot["partial"], "excluded": tot["excluded"], "included": tot["included"],
             # The range for this bar's own mix: each included category's
             # range weighted by its share of the sales (exact for the
             # aggregate ratio).
             "range": mix_range,
             "range_source": {"label": "Range for your mix, for counted pour cost — from each category's range",
                              "definition": RANGE_DEFINITION,
                              "parts": {k: ranges[k]["label"] for k in tot["included"]}},
             "spark": [p["total"] for p in trend],
             "target": overall["pct"] if overall else None,
             "target_source": "owner" if overall else "industry",
             "target_label": overall["label"] if overall else None,
             "over_target_usd": None, "top_over": None, "range_tone": None, "tone": None,
             "prev_pct": earlier["total"] if earlier else None,
             "prev_end_mdy": earlier["end_mdy"] if earlier else None}
    total.update(tv)
    if tv["judged"] and tot["ratio"] is not None and mix_range:
        pct = tot["ratio"] * 100
        total["range_tone"] = "low" if pct < mix_range[0] else ("above" if pct > mix_range[1] else "inside")
        if overall:
            over = tot["_bought"] - overall["pct"] / 100 * tot["_sold"]
            total["over_target_usd"] = round(over, 2) if over > 0 else None
            total["tone"] = "warn" if over > 0 else ("low" if total["range_tone"] == "low" else "good")
            # Which category carries the most dollars above the overall
            # target — a real one the owner can act on (V19).
            ups = [(c["_bought"] - overall["pct"] / 100 * c["_sold"], c) for c in inc]
            ups = [(u, c) for u, c in ups if u > 0]
            if ups:
                u, c = max(ups, key=lambda t: t[0])
                total["top_over"] = {"key": c["key"], "label": c["label"], "usd": round(u, 2), "pct": c["pct"]}
        elif head["basis"] == "counted":
            total["tone"] = {"low": "low", "inside": "good", "above": "warn"}[total["range_tone"]]
        # else: a purchase ratio against counted-pour-cost ranges is context
        # (range_tone) with no verdict tone (V1).
    context = None
    if _days(lo, end) >= CONTEXT_DAYS:
        a = (_d(end) - timedelta(days=CONTEXT_DAYS - 1)).isoformat()
        cw = _read(lines, sales, a, end, owner_stopped)
        context = {"from": a, "to": end, "from_mdy": _mdy(a), "to_mdy": _mdy(end), "days": CONTEXT_DAYS,
                   "total": cw["total"]["pct"], "partial": cw["total"]["partial"]}
        for c in POUR_CATEGORIES:
            context[c] = None if cw["cats"][c].get("incomplete") else cw["cats"][c]["pct"]
    months = []
    for key, label, kind, a0, b0 in _rows_spans(lo, hi, restaurant):
        a, b = max(lo, a0), min(hi, b0)
        mw = _read(lines, sales, a, b, owner_stopped)
        rows = [_public(mw["cats"][c]) for c in POUR_CATEGORIES]
        for r in rows:
            r["tone"] = None
        months.append({"month": key, "label": label, "kind": kind, "from": a, "to": b,
                       "partial": not (a == a0 and b == b0), "from_mdy": _mdy(a), "to_mdy": _mdy(b),
                       "incomplete": bool(mw["gaps"]) or bool(mw["unbalanced"]),
                       "total": {k: mw["total"][k] for k in ("purchases", "sales", "pct", "partial", "excluded")},
                       "categories": rows})
    owner_set = list(owner_stopped or ())
    return dict(head, available=True,
                window={"from": start, "to": end, "from_mdy": _mdy(start), "to_mdy": _mdy(end), "days": days,
                        "open_days": len(win_open),
                        "invoices": len({(l["distributor"], l["invoice_number"])
                                         for l in lines.ledger.between(start, end)}),
                        "coverage_pct": round(len(win_open) / days * 100) if days else None,
                        "min_days": MIN_DAYS},
                judged=bool(total["judged"]), not_judged=total["not_judged"],
                categories=[_public(cur[c]) for c in POUR_CATEGORIES],
                total=total, trend=trend, trend_noise=noise, context_13w=context,
                months=months, overlap={"from_mdy": _mdy(lo), "to_mdy": _mdy(hi)},
                gaps=gaps, complete=not gaps and not w["unbalanced"], unbalanced=w["unbalanced"],
                stopped=stopped(lines, end, owner_set), freshness=fresh)


def _trend(lines, sales, lo, hi, owner_stopped=()) -> list:
    """The four-week reading week by week: one point for the four weeks
    ending on the last day both are on file, then every seven days back,
    while a whole four weeks fits ([oldest .. newest]). A category whose
    invoices are missing in a window is a gap (None) there, never a low
    reading; the total is _total's — the same figure the page's total gives
    for the same window (`total_partial` when a category was left out)."""
    lines, sales = as_lines(lines), as_sales(sales)
    first = _d(lo)
    end = _d(hi)
    pts = []
    while len(pts) < TREND_POINTS:
        start = end - timedelta(days=WINDOW_DAYS - 1)
        if start < first:
            break
        a, b = start.isoformat(), end.isoformat()
        w = _read(lines, sales, a, b, owner_stopped)
        point = {"end": b, "end_mdy": _mdy(b), "from_mdy": _mdy(a)}
        for c in POUR_CATEGORIES:
            point[c] = None if w["cats"][c].get("incomplete") else w["cats"][c]["pct"]
        point["total"] = w["total"]["pct"]
        point["total_partial"] = w["total"]["partial"]
        pts.append(point)
        end -= timedelta(days=7)
    return list(reversed(pts))
