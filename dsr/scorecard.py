"""
dsr.scorecard — "Did we win today?" on the Owner DSR, in under 30 seconds.

The top of the owner's nightly report (Will, 9/25/26): Today's Score (a
verdict, sales vs budget, labor vs target, food cost, guest experience and
an overall score out of 100), then the executive summary the narrative
already writes, then Today's Wins and Today's Risks.

Everything here is DETERMINISTIC and read from the night's measured blocks
(and dsr_metrics history for "vs a usual Saturday"): no model call, no
figure a block did not measure. A component with no measurement is left out
of the score and says why — never scored as zero. The narrative's verified
went_well / needs_attention lines top up the lists when the signals below
find fewer than five.

    from dsr import scorecard
    card = scorecard.build(facts, restaurant, narrative=None, db_path=None)
    # {"verdict": {...}|None, "overall": int|None, "components": [...],
    #  "wins": [...], "risks": [...], "basis": str}

THE OVERALL SCORE (0–100) is a weighted mean of the components that were
measured, weights re-spread over what is present, and only when sales plus
at least one more component are measured:

    sales   40   vs budget, else vs Cavnar's forecast, else vs last week
    labor   25   points vs the labor target
    food    15   points vs the food-cost target (an estimate — said so)
    guests  20   the night's average rating (only at RATING_MIN_REVIEWS)

Each component maps its gap to 0–100 on a stated curve (CURVES). The
verdict reads Excellent at 85+, Good at 70+, Mixed at 55+, Tough below.
Against Cavnar's STARTING target (not one the owner set) labor and food
are still scored, but their tone never goes red (Benchmarking re-audit #10).

Owner view only: the budget and the food estimate are the owner's
(dsr.access), so the manager's report keeps its own layout.
"""
from datetime import date, timedelta

import dsr

WEIGHTS = {"sales": 40, "labor": 25, "food": 15, "guests": 20}
MIN_SCORED = 2                      # sales plus one more
VERDICTS = ((85, "Excellent day", "good"), (70, "Good day", "good"), (55, "Mixed day", "warn"),
            (0, "Tough day", "bad"))
MAX_ITEMS = 5
# Wins and risks a report shows up front (9/25/26, ID1-15): three each on the
# web, iPhone and in the email; the web keeps the rest behind "N more".
SHOWN_ITEMS = 3
HISTORY_WEEKS = 4                   # "a usual Saturday": the same weekday, the last 4 weeks
HISTORY_MIN = 2                     # …measured on at least 2 of them
CATEGORY_WIN_PCT = 15.0
CATEGORY_RISK_PCT = -20.0
CATEGORY_MIN_DOLLARS = 200.0        # a category this small moving is noise, not news
TICKET_WINDOW_DAYS = 21
TICKET_MIN_NIGHTS = 5
ON_TARGET_PTS = 0.5                 # within half a point reads "on target"
SALES_RISK_PCT = -3.0

# gap → score, piecewise linear through these points (gap in the
# component's own unit: sales %, labor/food points, rating stars).
CURVES = {
    "sales": ((-15.0, 0), (0.0, 75), (10.0, 100)),
    "labor": ((-2.0, 100), (0.0, 85), (2.0, 50), (6.0, 0)),
    "food": ((-1.5, 100), (0.0, 85), (1.5, 50), (5.0, 0)),
    "guests": ((2.0, 0), (3.0, 35), (3.5, 55), (4.0, 75), (4.5, 90), (4.8, 100)),
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def curve(kind, x):
    """The 0–100 score for gap `x` on component `kind`'s curve."""
    pts = CURVES[kind]
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])


def _money(v):
    return f"${abs(v):,.0f}"


def _smoney(v):
    return ("+" if v > 0 else "−" if v < 0 else "") + _money(v)


def _spct(v):
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):.1f}%"


def _ready(blocks, name):
    b = (blocks or {}).get(name) or {}
    return (b.get("metrics") or {}) if b.get("status") == dsr.READY else None


def _detail(blocks, name):
    return ((blocks or {}).get(name) or {}).get("detail") or {}


def _target(restaurant, kind):
    try:
        import thresholds
        t = thresholds.target_for(restaurant, kind)
        return t.get("pct"), t.get("source"), t.get("label") or "target"
    except Exception:
        return None, None, "target"


def _usual(rid, metric, day, db_path):
    """The same weekday's mean over the last HISTORY_WEEKS weeks, or None
    under HISTORY_MIN measured nights."""
    try:
        from dsr import store
        vals = []
        for w in range(1, HISTORY_WEEKS + 1):
            d = day - timedelta(days=7 * w)
            s = store.metric_series(rid, metric, d, d, db_path=db_path)
            if s and _num(s[0][1]):
                vals.append(float(s[0][1]))
        return (sum(vals) / len(vals), len(vals)) if len(vals) >= HISTORY_MIN else (None, len(vals))
    except Exception:
        return None, 0


def _series(rid, metric, start, end, db_path):
    try:
        from dsr import store
        return [(d, float(v)) for d, v in store.metric_series(rid, metric, start, end, db_path=db_path) if _num(v)]
    except Exception:
        return []


# ── the components ─────────────────────────────────────────────────────────

def _sales(sm, wd):
    if sm is None or not _num(sm.get("net")):
        return {"key": "sales", "label": "Sales", "measured": False, "value": None,
                "why": "Sales aren't in for this night"}
    for key, dkey, basis in (("vs_budget_net_pct", "vs_budget_net", "vs budget"),
                             ("vs_forecast_pct", "vs_forecast", "vs Cavnar's forecast"),
                             ("vs_last_week_pct", "vs_last_week", f"vs last {wd}")):
        pct, dollars = sm.get(key), sm.get(dkey)
        if _num(pct) and _num(dollars):
            tone = "good" if pct >= 0 else ("bad" if pct <= -5 else "warn")
            return {"key": "sales", "label": "Sales", "measured": True, "value": f"{_smoney(dollars)} {basis}",
                    "detail": f"{_money(sm['net'])} net · {_spct(pct)}", "tone": tone,
                    "score": curve("sales", float(pct)), "basis": basis,
                    "cites": ["sales.net", f"sales.{dkey}", f"sales.{key}"]}
    return {"key": "sales", "label": "Sales", "measured": True, "value": f"{_money(sm['net'])} net",
            "detail": "No budget, forecast or last-week night to compare with", "tone": None,
            "score": None, "basis": None, "cites": ["sales.net"]}


def _labor(lm, restaurant):
    if lm is None or not _num(lm.get("pct")):
        return {"key": "labor", "label": "Labor", "measured": False, "value": None,
                "why": "Labor isn't in for this night"}
    target, src, label = _target(restaurant, "labor")
    if not _num(target):
        target = lm.get("target_pct")
    if not _num(target):
        return {"key": "labor", "label": "Labor", "measured": True, "value": f"{lm['pct']:.1f}%",
                "detail": "No labor target to compare with", "tone": None, "score": None, "cites": ["labor.pct"]}
    pts = round(float(lm["pct"]) - float(target), 1)
    if abs(pts) <= ON_TARGET_PTS:
        words = f"on {label}"
    else:
        words = f"{abs(pts):.1f} pts {'below' if pts < 0 else 'above'} {label}"
    tone = "good" if pts <= ON_TARGET_PTS else ("bad" if pts > 2 else "warn")
    if src == "default" and tone == "bad":
        tone = "warn"
    return {"key": "labor", "label": "Labor", "measured": True, "value": words,
            "detail": f"{lm['pct']:.1f}% of sales · {label} {float(target):g}%", "tone": tone,
            "score": curve("labor", pts), "pts": pts, "cites": ["labor.pct", "labor.target_pct"]}


def _food(fm, restaurant):
    if fm is None or not _num(fm.get("est_food_cost_pct")):
        return {"key": "food", "label": "Food cost", "measured": False, "value": None,
                "why": "Not estimated for this night (recipes or counts missing)"}
    pct = float(fm["est_food_cost_pct"])
    target, src, label = _target(restaurant, "food")
    if not _num(target):
        return {"key": "food", "label": "Food cost", "measured": True, "value": f"{pct:.1f}% (est.)",
                "detail": "No food-cost target to compare with", "tone": None, "score": None,
                "cites": ["food.est_food_cost_pct"]}
    pts = round(pct - float(target), 1)
    words = ("On target" if abs(pts) <= ON_TARGET_PTS
             else f"{abs(pts):.1f} pts {'under' if pts < 0 else 'over'}")
    tone = "good" if pts <= ON_TARGET_PTS else ("bad" if pts > 1.5 else "warn")
    if src == "default" and tone == "bad":
        tone = "warn"
    return {"key": "food", "label": "Food cost", "measured": True, "value": words,
            "detail": f"{pct:.1f}% estimated · {label} {float(target):g}%", "tone": tone,
            "score": curve("food", pts), "pts": pts, "estimate": True,
            "cites": ["food.est_food_cost_pct"]}


def _guests(rm, rdetail):
    if rm is None:
        return {"key": "guests", "label": "Guest experience", "measured": False, "value": None,
                "why": "Reviews haven't synced for this night"}
    avg, n = rm.get("avg_rating"), rm.get("received")
    if not _num(avg):
        why = rdetail.get("rating_note") or ("No new reviews" if not n else "Too few reviews to rate the night")
        return {"key": "guests", "label": "Guest experience", "measured": False, "value": None, "why": why,
                "count": n}
    # Whole stars, rounded down unless within a quarter of the next (4.6 is
    # four stars beside "4.6", never a fifth it didn't earn).
    stars = max(0, min(5, int(float(avg) + 0.25)))
    n = int(n) if _num(n) else n
    return {"key": "guests", "label": "Guest experience", "measured": True,
            "value": "★" * stars + "☆" * (5 - stars), "stars": float(avg),
            "detail": f"{float(avg):.1f} across {n} review{'' if n == 1 else 's'}",
            "tone": "good" if avg >= 4.5 else ("warn" if avg >= 3.5 else "bad"),
            "score": curve("guests", float(avg)), "cites": ["reviews.avg_rating", "reviews.received"]}


def overall(components):
    """(score 0–100 | None, verdict dict | None) from the scored components."""
    scored = [c for c in components if c.get("measured") and _num(c.get("score"))]
    if len(scored) < MIN_SCORED or not any(c["key"] == "sales" for c in scored):
        return None, None
    w = sum(WEIGHTS[c["key"]] for c in scored)
    score = int(round(sum(WEIGHTS[c["key"]] * c["score"] for c in scored) / w))
    for floor, label, tone in VERDICTS:
        if score >= floor:
            return score, {"label": label, "tone": tone}
    return score, None


# ── wins and risks ─────────────────────────────────────────────────────────

def _signals(blocks, restaurant, day, db_path, comps):
    rid = getattr(restaurant, "id", None)
    wd = day.strftime("%A")
    wins, risks = [], []
    sm, lm = _ready(blocks, "sales"), _ready(blocks, "labor")
    fm, rm = _ready(blocks, "food"), _ready(blocks, "reviews")
    by = {c["key"]: c for c in comps}

    # Sales against budget (the headline comparison).
    if sm and _num(sm.get("vs_budget_net")) and _num(sm.get("vs_budget_net_pct")):
        d, p = sm["vs_budget_net"], sm["vs_budget_net_pct"]
        if d > 0:
            wins.append((p + 20, {"text": f"Net sales {_smoney(d)} vs budget ({_spct(p)})", "key": "sales_budget"}))
        elif p <= SALES_RISK_PCT:
            risks.append((-p + 20, {"text": f"Net sales {_smoney(d)} vs budget ({_spct(p)})", "key": "sales_budget"}))

    # Categories against a usual <weekday>.
    if sm and rid:
        for k, v in sm.items():
            if not k.startswith("cat:") or not _num(v) or k == f"cat:{dsr.UNMAPPED}":
                continue
            base, _n = _usual(rid, f"sales.{k}", day, db_path)
            if not base or base <= 0:
                continue
            pct = (float(v) - base) / base * 100.0
            name = k[4:]
            if pct >= CATEGORY_WIN_PCT and float(v) >= CATEGORY_MIN_DOLLARS:
                wins.append((min(pct, 25), {"text": f"{name} sales {_spct(pct)} vs a usual {wd}", "key": f"cat:{name}"}))
            elif pct <= CATEGORY_RISK_PCT and base >= CATEGORY_MIN_DOLLARS:
                risks.append((min(-pct, 25), {"text": f"{name} sales {_spct(pct)} vs a usual {wd}", "key": f"cat:{name}"}))

    # Average ticket: the highest in three weeks.
    if sm and rid and _num(sm.get("avg_ticket")):
        prior = _series(rid, "sales.avg_ticket", day - timedelta(days=TICKET_WINDOW_DAYS), day - timedelta(days=1),
                        db_path)
        if len(prior) >= TICKET_MIN_NIGHTS and float(sm["avg_ticket"]) > max(v for _d, v in prior):
            wins.append((12, {"text": f"Highest average ticket in 3 weeks (${float(sm['avg_ticket']):,.2f})",
                              "key": "avg_ticket"}))

    # Labor.
    lab = by.get("labor") or {}
    if _num(lab.get("pts")):
        if lab["pts"] <= ON_TARGET_PTS:
            wins.append((15 - lab["pts"] * 3, {"text": f"Labor {lm['pct']:.1f}%, {lab['value']}", "key": "labor_target"}))
        else:
            risks.append((15 + lab["pts"] * 4, {"text": f"Labor {lm['pct']:.1f}%, {lab['value']}",
                                                "key": "labor_target"}))
    if lm is not None and _num(lm.get("overtime_hours")):
        if lm["overtime_hours"] <= 0:
            wins.append((8, {"text": "No overtime", "key": "overtime"}))
        else:
            ot = float(lm["overtime_hours"])
            risks.append((10 + ot, {"text": f"{ot:g} overtime hour{'' if ot == 1 else 's'}", "key": "overtime"}))
    if lm is not None and _num(lm.get("no_shows")) and lm["no_shows"] > 0:
        n = int(lm["no_shows"])
        risks.append((9, {"text": f"{n} scheduled {'person' if n == 1 else 'people'} never clocked in",
                          "key": "no_shows"}))

    # Food.
    food = by.get("food") or {}
    if _num(food.get("pts")):
        if food["pts"] <= ON_TARGET_PTS:
            words = "on target" if abs(food["pts"]) <= ON_TARGET_PTS else food["value"] + " target"
            wins.append((10 - food["pts"] * 2, {"text": f"Food cost {fm['est_food_cost_pct']:.1f}% (est.), {words}",
                                                "key": "food_target"}))
        else:
            risks.append((12 + food["pts"] * 3, {"text": f"Food cost {fm['est_food_cost_pct']:.1f}% (est.), {food['value']} target",
                                                 "key": "food_target"}))
    stock = (_detail(blocks, "food").get("stock") or {}) if fm is not None else {}
    crit = [x for x in stock.get("critical") or [] if x.get("item")]
    crit.sort(key=lambda x: x["days_remaining"] if _num(x.get("days_remaining")) else 99)
    for x in crit[:2]:
        left = x.get("days_remaining")
        when = (f" ({left:g} day{'' if left == 1 else 's'} left)" if _num(left) else "")
        # Running out before the next delivery outranks a soft night: sooner is higher.
        urgency = 40 - 3 * float(left) if _num(left) else 20
        risks.append((max(urgency, 18), {"text": f"{x['item']} running low{when}", "key": f"stock:{x['item']}"}))
    more = int(stock.get("critical_count") or 0) - min(len(crit), 2)
    if more > 0:
        risks.append((11, {"text": f"{more} more item{'' if more == 1 else 's'} critically low", "key": "stock:more"}))

    # Guests.
    g = by.get("guests") or {}
    if g.get("measured") and _num(g.get("stars")) and g["stars"] >= 4.5:
        wins.append((10 + g["stars"], {"text": f"Guests rated the night {g['stars']:.1f}★", "key": "rating"}))
    if rm is not None:
        fives = sum(1 for r in _detail(blocks, "reviews").get("reviews") or [] if r.get("rating") == 5)
        if fives >= 2:
            wins.append((9 + fives, {"text": f"{fives} five-star reviews", "key": "five_star"}))
        if _num(rm.get("negative")) and rm["negative"] > 0:
            n = int(rm["negative"])
            risks.append((13 + n, {"text": f"{n} negative review{'' if n == 1 else 's'}", "key": "negative"}))
        if _num(rm.get("drafts_awaiting")) and rm["drafts_awaiting"] > 0:
            n = int(rm["drafts_awaiting"])
            risks.append((10 + n, {"text": f"{n} review repl{'y' if n == 1 else 'ies'} waiting to go out",
                                   "key": "replies"}))
    return wins, risks


def _top(scored, extra, limit=MAX_ITEMS):
    """The strongest signals first, then the narrative's verified lines to
    fill the list, each once."""
    out, seen = [], set()
    for _w, item in sorted(scored, key=lambda t: -t[0]):
        if item["key"] in seen:
            continue
        seen.add(item["key"])
        out.append(dict(item, source="measured"))
        if len(out) >= limit:
            return out
    for t in extra:
        if len(out) >= limit:
            break
        if t and t.lower() not in {o["text"].lower() for o in out}:
            out.append({"text": t, "key": None, "source": "narrative"})
    return out


def _texts(items):
    out = []
    for x in items or []:
        t = x if isinstance(x, str) else (x.get("text") if isinstance(x, dict) else None)
        if t:
            out.append(str(t).strip())
    return out


def build(facts, restaurant, narrative=None, db_path=None) -> dict:
    """The owner's scorecard for one night's facts (dsr.access.render's
    facts, before any manager redaction)."""
    facts = facts or {}
    blocks = facts.get("blocks") or {}
    try:
        day = date.fromisoformat(str(facts.get("business_date"))[:10])
    except Exception:
        day = date.today()
    wd = day.strftime("%a")
    comps = [_sales(_ready(blocks, "sales"), wd), _labor(_ready(blocks, "labor"), restaurant),
             _food(_ready(blocks, "food"), restaurant),
             _guests(_ready(blocks, "reviews"), _detail(blocks, "reviews"))]
    score, verdict = overall(comps)
    try:
        wins, risks = _signals(blocks, restaurant, day, db_path, comps)
    except Exception:
        wins, risks = [], []
    n = narrative or {}
    measured = sum(1 for c in comps if c.get("measured") and _num(c.get("score")))
    return {
        "overall": score,
        "verdict": verdict,
        "components": comps,
        "wins": _top(wins, _texts(n.get("went_well"))),
        "risks": _top(risks, _texts(n.get("needs_attention"))),
        "basis": ("A weighted score of what was measured tonight — sales 40, labor 25, food cost 15, guests 20"
                  if score is not None else
                  "Not enough was measured tonight to score the day (sales plus one more are needed)"),
        "scored_components": measured,
    }
