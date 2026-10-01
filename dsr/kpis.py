"""
dsr.kpis — the night's key numbers WITH direction: every figure beside how
it moved, whether it is the best (or worst) in weeks, a short trend line,
its target and — where a fair one exists — restaurants like yours.

    Labor           27.5%    ↓ 1.4 pts vs last Saturday · Best Saturday in 5 weeks
                             Target 28% · Restaurants like yours 29.8% (28-day)

Read at render time from the night's blocks and dsr_metrics history; no
model call. Each view gets its own set (OWNER_SET / MANAGER_SET — the
Manager DSR is operations, not finance) and every KPI passes
dsr.access.line_allowed and the block's view permission, so a manager never
sees the budget, food cost or (without LOSS_VIEW) comps, voids and refunds.

Direction is read against the SAME WEEKDAY: a Saturday against last
Saturday and the Saturdays before it, never against a Tuesday.
  change   vs last <weekday>: points for a percentage, percent for money
           and counts; ↑/↓ with good/bad tone from the metric's better side
  streak   "Best <weekday> in N weeks" / "Worst …" when tonight beats (or
           trails) every one of the last N ≥ STREAK_MIN same weekdays
  spark    the last SPARK_NIGHTS measured nights, oldest first
  target   labor % and food cost % (thresholds.target_for — "Cavnar's
           starting target" said so)
  peers    the Benchmark Engine's peer band median for the matching 28-day
           metric, only when the engine has a fair comparison; otherwise
           its reason (intelligence.engine — no all-types averages for
           economics, organisation floors, etc.)

Derived KPIs (sales per labor hour, prime cost %, beverage mix %) are
computed from the night's own measured figures and say so.
"""
from datetime import date, timedelta

import dsr

STREAK_MIN = 3
STREAK_WEEKS = 12
SPARK_NIGHTS = 14
# The DSR categories that are beverage (alcohol) sales — matched whole, by
# the category the owner mapped a POS department to, never by a substring
# ("bar" matched "Barbecue", and "NA Beverage" counted as beverage, D1-18).
BEVERAGE_CATEGORIES = ("liquor", "beer", "wine", "spirits", "cocktails")

# key: (label, fact key or derived, unit, better)  unit: money | pct | count | hours | stars | ratio
KPIS = {
    "net": ("Net sales", "sales.net", "money", "higher"),
    "guests": ("Guests", "sales.guests", "count", "higher"),
    "avg_ticket": ("Average ticket", "sales.avg_ticket", "money", "higher"),
    "labor_pct": ("Labor", "labor.pct", "pct", "lower"),
    "labor_cost": ("Labor dollars", "labor.cost", "money", None),
    "splh": ("Sales per labor hour", "derived:splh", "money", "higher"),
    "overtime": ("Overtime hours", "labor.overtime_hours", "hours", "lower"),
    "food_pct": ("Food cost", "food.est_food_cost_pct", "pct", "lower"),
    "prime_pct": ("Prime cost", "derived:prime", "pct", "lower"),
    "bev_mix": ("Beverage mix", "derived:bev_mix", "pct", None),
    "voids": ("Voids", "sales.voids", "money", "lower"),
    "discounts": ("Discounts", "sales.discounts", "money", "lower"),
    "comps": ("Comps", "sales.comps", "money", "lower"),
    "refunds": ("Refunds", "sales.refunds", "money", "lower"),
    "rating": ("Guest rating", "reviews.avg_rating", "stars", "higher"),
    "per_guest": ("Spend per guest", "sales.per_guest", "money", "higher"),
    "drinks_per_guest": ("Drinks per guest", "service.drinks_per_guest", "ratio", "higher"),
}
OWNER_SET = ("net", "labor_pct", "food_pct", "prime_pct", "avg_ticket", "guests", "per_guest", "splh",
             "labor_cost", "overtime", "drinks_per_guest", "bev_mix", "rating")
# The Manager DSR's Top KPIs and its Operations are two sets that never share
# a key (D3-5): guests, average ticket and sales per labor hour are
# Operations; overtime is a row of Today's shift. build() also skips any key
# Operations already placed, so the same tile is never drawn twice.
MANAGER_SET = ("labor_pct", "rating")
OPERATIONS_SET = ("avg_ticket", "guests", "per_guest", "splh", "drinks_per_guest", "voids", "discounts", "comps")
# Operations shows at most this many tiles (ID1-21, 9/25/26): the first four
# in OPERATIONS_SET order (guest complaints third). Discounts, voids and
# comps stay in the Sales block, where the view allows them.
OPERATIONS_MAX = 4
# The KPIs the report leads with (ID1-16): at most HEADLINE_MAX, and for the
# owner never one Today's score already states — its four components are the
# net, labor %, food cost % and the rating. Everything else sits under "All
# KPIs". SCORE_KEYS maps each to its scorecard component.
HEADLINE_MAX = 4
SCORE_KEYS = {"net": "sales", "labor_pct": "labor", "food_pct": "food", "rating": "guests"}
# the Benchmark Engine's comparable metric for a nightly KPI (28/30-day)
ENGINE_METRIC = {"labor_pct": "labor_pct_28d", "food_pct": "food_cost_pct_28d", "rating": "avg_rating_30d"}
TARGET_KIND = {"labor_pct": "labor", "food_pct": "food"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt(v, unit):
    if v is None:
        return "—"
    if unit == "money":
        return f"${v:,.2f}" if abs(v) < 100 else f"${v:,.0f}"
    if unit == "pct":
        return f"{v:.1f}%"
    if unit == "hours":
        return f"{v:g}h"
    if unit == "stars":
        return f"{v:.1f}★"
    if unit == "count":
        return f"{v:,.0f}"
    return f"{v:g}"


def _series(rid, fact, start, end, db_path):
    from dsr import store
    try:
        return {d: float(v) for d, v in store.metric_series(rid, fact, start, end, db_path=db_path) if _num(v)}
    except Exception:
        return {}


def _bev_share(metrics):
    net = metrics.get("net")
    if not _num(net) or net <= 0:
        return None
    cats = {k[4:]: v for k, v in metrics.items() if k.startswith("cat:") and _num(v)}
    bev = [v for k, v in cats.items() if " ".join(k.lower().split()) in BEVERAGE_CATEGORIES]
    return round(sum(bev) / net * 100, 1) if bev else None


def _prime(labor_cost, food_cost, net, coverage):
    """Prime cost % from DOLLARS over one denominator (D1-11): labor $ plus
    the estimated recipe cost of what sold, over the night's net — never two
    percentages with different bases added. Withheld (None) unless every
    part is measured, net is positive and the recipe estimate covers at
    least block_food.ESTIMATE_MIN_COVERAGE_PCT of the units sold."""
    from dsr.block_food import ESTIMATE_MIN_COVERAGE_PCT
    if not (_num(labor_cost) and _num(food_cost) and _num(net) and net > 0 and _num(coverage)):
        return None
    if coverage < ESTIMATE_MIN_COVERAGE_PCT:
        return None
    return round((labor_cost + food_cost) / net * 100, 1)


def _value(key, blocks):
    """Tonight's value for one KPI, or None (not measured)."""
    fact = KPIS[key][1]
    def m(block):
        b = blocks.get(block) or {}
        return (b.get("metrics") or {}) if b.get("status") == dsr.READY else {}
    if fact == "derived:splh":
        net, hours = m("sales").get("net"), m("labor").get("hours")
        return round(net / hours, 2) if _num(net) and _num(hours) and hours > 0 else None
    if fact == "derived:prime":
        return _prime(m("labor").get("cost"), m("food").get("est_food_cost"), m("sales").get("net"),
                      m("food").get("recipe_coverage_pct"))
    if fact == "derived:bev_mix":
        return _bev_share(m("sales"))
    block, _, k = fact.partition(".")
    v = m(block).get(k)
    return float(v) if _num(v) else None


# The owner's labor is all-in (dsr.access._live_salaries swaps cost and pct
# for the salaries-in figures at render); the nights it is compared with are
# stored hourly. Those nights get the same salaried day share added, so
# "vs last Saturday", the streak and the sparkline compare like with like.
ALL_IN_KEYS = ("labor_pct", "labor_cost", "prime_pct")


def _history(key, rid, day, db_path, salaried_share=None):
    """{date: value} for the KPI over the streak window (same-weekday reads
    pick from it) — derived KPIs rebuilt night by night. With a
    salaried_share (the owner's all-in labor), labor %, labor dollars and
    prime cost add it to every past night's hourly labor."""
    start = day - timedelta(days=7 * STREAK_WEEKS)
    end = day - timedelta(days=1)
    fact = KPIS[key][1]
    if salaried_share and key in ALL_IN_KEYS:
        lab = {d: v + salaried_share for d, v in _series(rid, "labor.cost", start, end, db_path).items()}
        net = _series(rid, "sales.net", start, end, db_path)
        if key == "labor_cost":
            return lab
        if key == "labor_pct":
            return {d: round(lab[d] / net[d] * 100, 1) for d in lab if net.get(d, 0) > 0}
        food, cov = (_series(rid, "food.est_food_cost", start, end, db_path),
                     _series(rid, "food.recipe_coverage_pct", start, end, db_path))
        out = {d: _prime(lab.get(d), food.get(d), net.get(d), cov.get(d)) for d in lab}
        return {d: v for d, v in out.items() if v is not None}
    if fact == "derived:splh":
        net, hrs = _series(rid, "sales.net", start, end, db_path), _series(rid, "labor.hours", start, end, db_path)
        return {d: round(net[d] / hrs[d], 2) for d in net if d in hrs and hrs[d] > 0}
    if fact == "derived:prime":
        lab, food, net, cov = (_series(rid, "labor.cost", start, end, db_path),
                               _series(rid, "food.est_food_cost", start, end, db_path),
                               _series(rid, "sales.net", start, end, db_path),
                               _series(rid, "food.recipe_coverage_pct", start, end, db_path))
        out = {d: _prime(lab.get(d), food.get(d), net.get(d), cov.get(d)) for d in lab}
        return {d: v for d, v in out.items() if v is not None}
    if fact == "derived:bev_mix":
        return {}
    return _series(rid, fact, start, end, db_path)


def _change(value, prior, unit, better, wd):
    if value is None or prior is None:
        return None
    if unit in ("pct", "stars"):
        d = round(value - prior, 1)
        text = f"{'↑' if d > 0 else '↓' if d < 0 else '→'} {abs(d):.1f}{' pts' if unit == 'pct' else '★'} vs last {wd}"
    else:
        if prior == 0:
            return None
        d = round((value - prior) / abs(prior) * 100, 1)
        text = f"{'↑' if d > 0 else '↓' if d < 0 else '→'} {abs(d):.1f}% vs last {wd}"
    tone = None
    if better and d != 0:
        tone = "good" if (d > 0) == (better == "higher") else "bad"
    return {"delta": d, "text": text, "tone": tone}


def _streak(value, same_days, better, wd):
    """Best/worst of the last N same weekdays, N ≥ STREAK_MIN."""
    if value is None or not better or len(same_days) < STREAK_MIN:
        return None
    hi = better == "higher"
    best = worst = 0
    for v in same_days:                         # newest first
        if (value > v) if hi else (value < v):
            best += 1
        else:
            break
    for v in same_days:
        if (value < v) if hi else (value > v):
            worst += 1
        else:
            break
    if best >= STREAK_MIN:
        return {"text": f"Best {wd} in {best + 1} weeks", "tone": "good"}
    if worst >= STREAK_MIN:
        return {"text": f"Worst {wd} in {worst + 1} weeks", "tone": "bad"}
    return None


def _peers(key, restaurant, db_path):
    metric = ENGINE_METRIC.get(key)
    if not metric or restaurant is None:
        return None
    try:
        from intelligence import engine
        c = engine.compare(restaurant.id, metric, kinds=("peers",), restaurant=restaurant, db_path=db_path)
        p = next((x for x in c.get("comparisons") or [] if x.get("kind") == "peers"), None) or {}
    except Exception:
        return None
    if p.get("available") and _num(p.get("p50")):
        unit = KPIS[key][2]
        v = p["p50"]
        return {"available": True, "value_text": fmt(v, unit), "label": "Restaurants like yours",
                "detail": f"{p.get('n')} {p.get('cohort_label') or 'similar restaurants'} · 28-day median",
                "strength_pct": (p.get("strength") or {}).get("pct")}
    return {"available": False, "label": "Restaurants like yours",
            "why_not": (p.get("why_not") or "No fair comparison yet")}


def _prime_goal(restaurant):
    """The owner's prime-cost goal as the KPI's target (owner_memory
    .target_for "prime_cost_pct" — memory audit 9/29/26): said as their
    goal. None without one: Cavnar AI keeps no prime-cost target of its own."""
    try:
        import owner_memory
        from time_utils import mdy
        t = owner_memory.target_for(restaurant.id, "prime_cost_pct")
    except Exception:
        return None
    if not t or not _num(t.get("value")):
        return None
    until = f" by {mdy(t['until'])}" if t.get("until") else ""
    return {"value": float(t["value"]), "value_text": f"{float(t['value']):g}%", "label": f"Your goal{until}",
            "source": "goal", "goal_id": t.get("goal_id")}


def _target(key, restaurant):
    if key == "prime_pct" and restaurant is not None:
        return _prime_goal(restaurant)
    kind = TARGET_KIND.get(key)
    if not kind or restaurant is None:
        return None
    try:
        import thresholds
        t = thresholds.target_for(restaurant, kind)
    except Exception:
        return None
    if not _num(t.get("pct")):
        return None
    label = "Target" if t.get("source") == "set" else (t.get("label") or "Target").capitalize()
    return {"value": float(t["pct"]), "value_text": f"{float(t['pct']):g}%", "label": label,
            "source": t.get("source")}


def _allowed(key, user, view):
    from dsr import access
    fact = KPIS[key][1]
    if fact.startswith("derived:"):
        if fact == "derived:prime":
            return view == access.OWNER
        return True
    block, _, k = fact.partition(".")
    need = access._block_permissions().get(block)
    if need and not access._sees(user, need):
        return False
    return access.line_allowed(user, view, k)


def kpi(key, blocks, restaurant, day, db_path=None, with_peers=True) -> dict | None:
    label, fact, unit, better = KPIS[key]
    value = _value(key, blocks)
    if value is None:
        return None
    wd = day.strftime("%A")
    rid = getattr(restaurant, "id", None)
    lab = ((blocks.get("labor") or {}).get("metrics") or {})
    share = lab.get("salaried_cost") if lab.get("includes_salaries") else None
    hist = _history(key, rid, day, db_path, salaried_share=share if _num(share) else None) if rid else {}
    last_week = hist.get((day - timedelta(days=7)).isoformat())
    nights = sorted(d for d in hist if d >= (day - timedelta(days=SPARK_NIGHTS - 1)).isoformat())
    spark = [hist[d] for d in nights] + [value]
    out = {"key": key, "label": label, "value": value, "value_text": fmt(value, unit), "unit": unit,
           "better": better, "change": _change(value, last_week, unit, better, wd),
           "streak": None, "spark": spark if len(spark) >= 3 else [],
           "target": _target(key, restaurant),
           "peers": _peers(key, restaurant, db_path) if with_peers else None,
           "derived": fact.startswith("derived:"),
           "estimate": key in ("food_pct", "prime_pct")}
    if key == "prime_pct":
        food = blocks.get("food") or {}
        cov = (food.get("metrics") or {}).get("recipe_coverage_pct")
        out["basis"] = (f"Labor dollars plus the estimated recipe cost of what sold ({cov:g}% of units sold have a "
                        "costed recipe; anything without one, drinks included, isn't in it), over net sales")
    # a streak needs an unbroken run of measured same weekdays
    run = []
    for w in range(1, STREAK_WEEKS + 1):
        d = (day - timedelta(days=7 * w)).isoformat()
        if d not in hist:
            break
        run.append(hist[d])
    out["streak"] = _streak(value, run, better, wd)
    return out


def build(facts, restaurant, user, view, db_path=None) -> dict:
    """{"top": [kpi], "operations": [kpi] (manager), "shift": {...} (manager)}
    for this login's view."""
    from dsr import access
    facts = facts or {}
    blocks = facts.get("blocks") or {}
    try:
        day = date.fromisoformat(str(facts.get("business_date"))[:10])
    except Exception:
        return {"top": [], "operations": [], "shift": None}
    keys = OWNER_SET if view == access.OWNER else [k for k in MANAGER_SET if k not in OPERATIONS_SET]
    top = []
    for k in keys:
        if not _allowed(k, user, view):
            continue
        x = kpi(k, blocks, restaurant, day, db_path)
        if x:
            top.append(x)
    ops, shift = [], None
    if view != access.OWNER:
        for k in OPERATIONS_SET:
            if not _allowed(k, user, view):
                continue
            x = kpi(k, blocks, restaurant, day, db_path, with_peers=False)
            if x:
                ops.append(x)
        comp = _complaints(blocks)
        if comp is not None:
            ops.insert(2, comp)
        ops_all = list(ops)
        ops = ops[:OPERATIONS_MAX]
        shift = shift_recap(blocks, user)
        return {"top": top, "operations": ops, "operations_all": ops_all, "shift": shift}
    return {"top": top, "operations": ops, "shift": shift}


def headline(top, scorecard=None, limit=HEADLINE_MAX):
    """The keys of the KPIs a report shows up front, in `top`'s order: the
    first `limit` of them, skipping — when there is a scorecard — every KPI
    whose score component was measured (Today's score already says it). The
    others are still in `top`, for "All KPIs"."""
    stated = set()
    if isinstance(scorecard, dict):
        measured = {c.get("key") for c in scorecard.get("components") or []
                    if isinstance(c, dict) and c.get("measured")}
        stated = {k for k, comp in SCORE_KEYS.items() if comp in measured}
    out = []
    for k in top or []:
        key = k.get("key") if isinstance(k, dict) else None
        if key and key not in stated and len(out) < limit:
            out.append(key)
    return out


def _complaints(blocks):
    b = blocks.get("reviews") or {}
    if b.get("status") != dsr.READY:
        return None
    n = (b.get("metrics") or {}).get("negative")
    if not _num(n):
        return None
    return {"key": "complaints", "label": "Guest complaints", "value": n, "value_text": f"{int(n)}",
            "unit": "count", "better": "lower", "change": None, "streak": None, "spark": [], "target": None,
            "peers": None, "derived": False, "estimate": False,
            "detail": "negative reviews that arrived today"}


def shift_recap(blocks, user=None) -> dict | None:
    """The Manager DSR's "Today's shift": who was scheduled and how the shift
    ran, from the Labor block. Rows Cavnar has no source for (break
    compliance) are left off, not shown empty."""
    from dsr import access
    from permissions import LABOR_VIEW
    b = blocks.get("labor") or {}
    if b.get("status") != dsr.READY or (user is not None and not access._sees(user, LABOR_VIEW)):
        return None
    m = b.get("metrics") or {}
    rows = []
    # "No-shows", as the Labor block names them (D1-17): scheduled people
    # who never clocked in. A call-off is someone who told you — Cavnar has
    # no record of that.
    for key, label, fmt_ in (("scheduled", "Employees scheduled", "{:.0f}"), ("no_shows", "No-shows", "{:.0f}"),
                             ("late_arrivals", "Late arrivals", "{:.0f}"), ("overtime_hours", "Overtime hours", "{:g}"),
                             ("shift_quality", "Shift quality", "{:.0f}")):
        v = m.get(key)
        if _num(v):
            tone = None
            if key in ("no_shows", "late_arrivals", "overtime_hours"):
                tone = "good" if v == 0 else "warn"
            elif key == "shift_quality":
                tone = "good" if v >= 80 else ("warn" if v >= 60 else "bad")
            rows.append({"key": key, "label": label, "value": v, "value_text": fmt_.format(v), "tone": tone})
    cov = ((b.get("detail") or {}).get("coverage") or {})
    note = None if cov.get("measured", True) else cov.get("reason")
    verdict = shift_verdict(m, b.get("detail") or {})
    return {"rows": rows, "note": note, "verdict": verdict} if rows or note or verdict else None


def shift_verdict(m, detail) -> dict | None:
    """The one line over the manager's Today's shift (ID1-21): labor against
    its target, then what went wrong on the floor — "Labor 27.5%, 0.5 pts
    under target · 1 no-show". Every figure is the Labor block's own (pct,
    target_pct, vs_target_pts, no_shows, overtime_hours); nothing is
    recomputed. "On target" within the scorecard's ON_TARGET_PTS, and the
    tone follows Today's score: over a target the owner never set
    (Cavnar's starting target) is amber, never red. None without a labor
    percentage."""
    from dsr.scorecard import ON_TARGET_PTS
    pct = m.get("pct")
    if not _num(pct):
        return None
    # The owner's own target or goal (owner_goals, memory audit 9/29/26) is
    # never a starting target.
    starting = (detail or {}).get("target_source") not in ("set", "goal")
    word = "starting target" if starting else ("goal" if (detail or {}).get("target_source") == "goal" else "target")
    pts = m.get("vs_target_pts")
    text, tone = f"Labor {pct:.1f}%", None
    if _num(pts) and _num(m.get("target_pct")):
        if abs(pts) <= ON_TARGET_PTS:
            text += f", on {word}"
        else:
            text += f", {abs(pts):.1f} pts {'under' if pts < 0 else 'over'} {word}"
        tone = "good" if pts <= ON_TARGET_PTS else ("bad" if pts > 2 and not starting else "warn")
    bits = [text]
    ns = m.get("no_shows")
    if _num(ns) and ns > 0:
        bits.append(f"{int(ns)} no-show{'' if ns == 1 else 's'}")
    ot = m.get("overtime_hours")
    if _num(ot) and ot > 0:
        bits.append(f"{ot:g} overtime hour{'' if ot == 1 else 's'}")
    if len(bits) > 1 and tone in (None, "good"):
        tone = "warn"
    return {"text": " · ".join(bits), "tone": tone}


# ── the week and the period, to date ───────────────────────────────────────
#
# "Are we going to make the week?" (owner, 9/30/26). Arithmetic only — the
# nights measured so far against the budget and last year for the same
# nights, and what the budget still needs from each night left, set beside a
# typical night for each of them (the demand forecast). Never a run rate:
# the narrative's pace-word rule (dsr.narrative _PACE_RE) holds here too.
# budget* keys are the owner's (dsr.access OWNER_ONLY_PREFIXES); the
# manager's view keeps last year.

def _span_pace(restaurant, first, last, through, db_path=None, ahead=True):
    from dsr import store
    rid = restaurant.id
    days, d = [], first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    done = [x for x in days if x <= through]
    got = store.baselines_net(rid, done, db_path=db_path) if db_path else store.baselines_net(rid, done)
    measured = {x.isoformat(): v[0] for x in done for v in [got.get(x.isoformat()) or (None, None)] if _num(v[0])}
    if not measured:
        return None
    wtd = round(sum(measured.values()), 2)
    out = {"start": first.isoformat(), "end": last.isoformat(), "net": wtd,
           "nights_measured": len(measured), "nights_total": len(days),
           "nights_left": len([x for x in days if x > through])}
    budgets, label = {}, None
    for x in days:
        b = store.night_budget(rid, x, db_path=db_path) if db_path else store.night_budget(rid, x)
        if _num(b.get("net")):
            budgets[x.isoformat()] = float(b["net"])
            label = label or ("Goal" if b.get("source") == "goal" else "Budget")
    if budgets and all(k in budgets for k in measured):
        to_date = round(sum(budgets[k] for k in measured), 2)
        out.update(budget_to_date=to_date, budget_vs=round(wtd - to_date, 2),
                   budget_vs_pct=round((wtd - to_date) / to_date * 100, 1) if to_date else None,
                   budget_label=label)
        if len(budgets) == len(days):
            total = round(sum(budgets.values()), 2)
            out.update(budget_total=total, budget_left=round(total - wtd, 2))
            if ahead and out["nights_left"]:
                out["budget_per_night_needed"] = round((total - wtd) / out["nights_left"], 2)
    ly_days = {x.isoformat(): store.last_year_day(restaurant, x) for x in done if x.isoformat() in measured}
    ly = store.baselines_net(rid, [v for v in ly_days.values() if v], db_path=db_path) if db_path \
        else store.baselines_net(rid, [v for v in ly_days.values() if v])
    ly_vals = [(ly.get(v.isoformat()) or (None, None))[0] for v in ly_days.values() if v]
    if ly_vals and len(ly_vals) == len(measured) and all(_num(v) for v in ly_vals):
        ly_total = round(sum(ly_vals), 2)
        out.update(last_year=ly_total,
                   last_year_pct=round((wtd - ly_total) / ly_total * 100, 1) if ly_total else None)
    if ahead and out["nights_left"]:
        import demand
        typical = []
        for x in days:
            if x <= through:
                continue
            try:
                fc = demand.forecast_net(rid, x, db_path=db_path) if db_path else demand.forecast_net(rid, x)
            except Exception:
                fc = None
            if fc and fc.get("available") and _num(fc.get("typical_sales")):
                typical.append(float(fc["typical_sales"]))
        if len(typical) == out["nights_left"]:
            out["typical_left"] = round(sum(typical), 2)
            out["typical_per_night"] = round(sum(typical) / len(typical), 2)
    return out


def pace(facts, restaurant, view, db_path=None) -> dict | None:
    """{"week": {...}, "period": {...}|None} to the report's night, as this
    view may read it (budget* keys are the owner's)."""
    from dsr import access, fiscal
    if restaurant is None:
        return None
    try:
        day = date.fromisoformat(str((facts or {}).get("business_date"))[:10])
    except Exception:
        return None
    try:
        ws, we = fiscal.week_bounds(restaurant, day)
        week = _span_pace(restaurant, ws, we, day, db_path)
        span = fiscal.period_span(restaurant, day)
        period = _span_pace(restaurant, span[0], span[1], day, db_path, ahead=False) if span else None
        if period:
            pos = fiscal.position(restaurant, day)
            period["label"] = f"Period {pos['period']}" if pos.get("period") else "Period"
    except Exception:
        return None
    if not week:
        return None
    if view != access.OWNER:
        strip = lambda d: {k: v for k, v in d.items() if not k.startswith(access.OWNER_ONLY_PREFIXES)} if d else d
        week, period = strip(week), strip(period)
    return {"week": week, "period": period}


# ── the six KPIs a report leads with ───────────────────────────────────────
#
# Large, animated, scannable (owner, 9/30/26: "the top 6 total KPIs"). The
# owner's six never repeat what Today's score states (sales against budget,
# labor, food, the rating); the manager's lead with labor. A tile with no
# measurement is skipped and the next candidate takes its place, so six are
# shown whenever six exist.
BIG_MAX = 6
BIG_OWNER = ("pace", "prime_pct", "guests", "per_guest", "splh", "tomorrow_labor", "drinks_per_guest",
             "avg_ticket", "overtime")
BIG_MANAGER = ("labor_pct", "pace", "guests", "per_guest", "splh", "tomorrow_labor", "drinks_per_guest",
               "avg_ticket")


def _money(v):
    return f"${v:,.0f}"


def _pace_tile(p, view):
    from dsr import access
    w = (p or {}).get("week")
    if not w:
        return None
    sub, tone, bar = [], None, None
    if view == access.OWNER and _num(w.get("budget_vs")):
        v = w["budget_vs"]
        sub.append(f"{'+' if v >= 0 else '−'}{_money(abs(v))} vs {w.get('budget_label', 'budget').lower()}")
        tone = "good" if v >= 0 else "bad"
        if _num(w.get("budget_total")) and w["budget_total"] > 0:
            bar = {"value": w["net"], "max": w["budget_total"], "marker": w.get("budget_to_date"),
                   "label": f"{_money(w['net'])} of {_money(w['budget_total'])}"}
        if _num(w.get("budget_left")) and w["budget_left"] <= 0:
            sub.append(f"The week's {w.get('budget_label', 'budget').lower()} is already made")
        elif _num(w.get("budget_per_night_needed")):
            n = w["nights_left"]
            need = (f"Needs {_money(w['budget_per_night_needed'])} "
                    + ("from the night left" if n == 1 else f"a night from the {n} left"))
            if _num(w.get("typical_per_night")):
                need += f" · a typical one does {_money(w['typical_per_night'])}"
            sub.append(need)
    elif _num(w.get("last_year_pct")):
        v = w["last_year_pct"]
        sub.append(f"{'↑' if v > 0 else '↓' if v < 0 else '→'} {abs(v):.1f}% vs last year")
        tone = "good" if v >= 0 else "bad"
    if _num(w.get("last_year_pct")) and view == access.OWNER:
        v = w["last_year_pct"]
        sub.append(f"{'↑' if v > 0 else '↓' if v < 0 else '→'} {abs(v):.1f}% vs last year")
    return {"key": "pace", "label": "Week to date", "value": w["net"], "value_text": _money(w["net"]),
            "unit": "money", "sub": sub[:2], "tone": tone, "bar": bar,
            "note": f"{w['nights_measured']} of {w['nights_total']} nights"}


def _tomorrow_tile(t, view):
    from dsr import access
    lab = (t or {}).get("labor")
    if not isinstance(lab, dict):
        return None
    pct = lab.get("salaried_total_pct") if view == access.OWNER and _num(lab.get("salaried_total_pct")) \
        else lab.get("hourly_pct")
    if not _num(pct):
        return None
    tgt = lab.get("target_pct")
    tone = None
    sub = []
    soft = lab.get("target_source") == "default"
    if _num(tgt):
        gap = round(pct - tgt, 1)
        # Over a target the owner never set is amber, never red (the
        # scorecard's rule, Benchmarking re-audit #10).
        tone = "good" if gap <= 0 else ("warn" if soft else "bad")
        name = f"Cavnar AI's starting {tgt:g}%" if soft else f"the {tgt:g}% target"
        sub.append(f"{abs(gap):.1f} pts {'over' if gap > 0 else 'under'} {name}" if gap else f"On {name}")
    cost = lab.get("salaried_total_cost") if view == access.OWNER and _num(lab.get("salaried_total_cost")) \
        else lab["hourly_cost"]
    sub.append(f"{lab['hours']:g} hours · {_money(cost)} scheduled")
    return {"key": "tomorrow_labor", "label": f"{t.get('weekday') or 'Tomorrow'}'s labor", "value": pct,
            "value_text": f"{pct:.1f}%", "unit": "pct", "sub": sub, "tone": tone,
            "gauge": {"value": pct, "target": tgt, "soft": soft} if _num(tgt) else None,
            "note": "scheduled, against the forecast"}


def _kpi_tile(k):
    sub = []
    if k.get("change"):
        sub.append(k["change"]["text"])
    if k.get("target"):
        sub.append(f"{k['target'].get('label', 'Target')} {k['target'].get('value_text')}")
    elif k.get("streak"):
        sub.append(k["streak"]["text"])
    out = {"key": k["key"], "label": k["label"], "value": k["value"], "value_text": k["value_text"],
           "unit": k.get("unit"), "sub": sub[:2], "tone": (k.get("change") or {}).get("tone"),
           "spark": k.get("spark") or [], "estimate": k.get("estimate")}
    t = k.get("target") or {}
    if k.get("unit") == "pct" and _num(t.get("value")):
        out["gauge"] = {"value": k["value"], "target": t["value"], "soft": t.get("source") == "default"}
    return out


def big(top, operations, pace_, tomorrow, view) -> list:
    """The six lead tiles for this view, in order, each
    {"key", "label", "value", "value_text", "unit", "sub": [≤2 lines],
    "tone", "spark"?, "bar"?, "gauge"?, "note"?}."""
    from dsr import access
    have = {k["key"]: k for k in list(top or []) + list(operations or []) if isinstance(k, dict) and k.get("key")}
    out = []
    for key in (BIG_OWNER if view == access.OWNER else BIG_MANAGER):
        if len(out) >= BIG_MAX:
            break
        if key == "pace":
            t = _pace_tile(pace_, view)
        elif key == "tomorrow_labor":
            t = _tomorrow_tile(tomorrow, view)
        else:
            t = _kpi_tile(have[key]) if key in have else None
        if t:
            out.append(t)
    return out
