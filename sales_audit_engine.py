"""sales_audit_engine.py — turns audit answers into scores, findings and a
conservative opportunity range. Pure and deterministic: no AI, no I/O.

Rules this file enforces, because Will has to defend every number across a
table from the owner:

* Ranges, never one falsely precise figure. Every category returns low /
  likely / high.
* Nothing is counted without enough data — a category with missing inputs
  returns status "insufficient" and a plain-English prompt for what to ask.
* No double counting. Overtime is inside labor dollars, so the labor
  opportunity is the LARGER of the gap-to-target and the overtime estimate,
  never the sum. Waste is inside food cost (same rule). Bar variance is inside
  pour cost (same rule). Comps, voids and discounts live only in Operations.
  Agency fees live only in Marketing. Software savings count only tools the
  owner marked replaceable, and never the POS, payroll or accounting.
* Recovery shares are explicit. A gap to benchmark is never assumed 100%
  recoverable.
* Missing data never lowers a score. A category with too few answers is
  "not assessed" rather than punished.
"""
from sales_audit_schema import QUESTIONS, SECTIONS, completion

# ── Benchmarks ───────────────────────────────────────────────────────────────
# Each has a source and an applicability note. Bands are (low, high) in
# percentage points of the relevant sales base. The engine targets the TOP of
# the band unless the owner gave their own target — that is the conservative
# choice.
BENCHMARKS = {
    "labor": {
        "full_service": {"band": (30, 34), "source": "National Restaurant Association, 2025 Restaurant Operations Data Abstract: full-service median labor (incl. benefits) 36.5% of sales in 2024; profitable full-service operators median 34.2%.",
                          "note": "Band top set at the profitable-operator median. Restaurants above it are not necessarily mismanaged — wage markets vary."},
        "sports_bar":   {"band": (27, 32), "source": "Operator rule of thumb. Bar-forward concepts run lower labor % because beverage sales carry little labor.",
                          "note": "Less authoritative than the NRA figure. Prefer the owner's own target."},
        "bar":          {"band": (25, 30), "source": "Operator rule of thumb for bar-dominant concepts.", "note": "Prefer the owner's own target."},
        "fine_dining":  {"band": (33, 38), "source": "Operator rule of thumb; service-heavy formats carry more labor.", "note": "Prefer the owner's own target."},
        "fast_casual":  {"band": (25, 30), "source": "Operator rule of thumb for counter-service formats.", "note": "Prefer the owner's own target."},
    },
    "food": {
        "full_service": {"band": (28, 32), "source": "NRA 2025 Operations Data Abstract: full-service food and non-alcohol beverage cost median 32.0% of sales in 2024.",
                          "note": "Menu type moves this a lot — steak-heavy menus run higher, pizza far lower."},
        "sports_bar":   {"band": (28, 33), "source": "NRA full-service median, widened one point for shareables/wings-heavy menus.", "note": "Prefer the owner's own target."},
        "bar":          {"band": (28, 33), "source": "As sports bar.", "note": "Prefer the owner's own target."},
        "fine_dining":  {"band": (30, 35), "source": "Operator rule of thumb.", "note": "Prefer the owner's own target."},
        "fast_casual":  {"band": (26, 31), "source": "Operator rule of thumb.", "note": "Prefer the owner's own target."},
        "combined":     {"band": (28, 32), "source": "Blended food + beverage COGS for full-service with a meaningful bar.", "note": "Used when the owner's number combines food and beverage."},
    },
    "bar": {
        "blended": {"band": (18, 24), "source": "Bar-industry consensus (Backbar, Sculpture Hospitality, DoorDash for Merchants guides): blended pour cost 18–24%; spirits 18–22%, draft beer ~20–24%, bottled 24–28%, wine 30–40%.",
                    "note": "Mix matters — a wine-heavy list legitimately runs higher. Prefer the owner's own target."},
        "liquor": (18, 22), "beer": (20, 26), "wine": (30, 40),
        "variance_target": 3.0,
        "variance_source": "Well-run bars target inventory variance under 3–5% of usage; above 5% warrants investigation; above 10% signals systemic loss (bar inventory vendors' published guidance — vendor-sourced, treat as directional).",
    },
    "prime": {"band": (60, 65), "source": "Widely used operator target: prime cost (labor + COGS) under 60–65% of sales."},
    "reviews": {"source": "Luca, M. (Harvard Business School, 2016) 'Reviews, Reputation, and Revenue: The Case of Yelp.com': a one-star increase in rating associated with 5–9% revenue increase for independent restaurants.",
                "note": "The audit assumes only a 0.05–0.2 star improvement from consistent, personal responses and acting on complaint patterns — not a full star — and takes the low end of the revenue effect."},
    "waste": {"source": "Waste reduction share is an operator assumption (25–50% of logged waste is avoidable with tracking and ordering discipline)."},
}

# Recovery shares — what share of a benchmark gap is realistically recoverable.
RECOVERY = {"low": 0.30, "likely": 0.50, "high": 0.70}

COST_CATEGORIES = ("labor", "food", "bar", "marketing", "operations", "technology")
REVENUE_CATEGORIES = ("reviews", "waitlist")

CATEGORY_LABELS = [
    ("labor", "Labor"), ("food", "Food Cost"), ("bar", "Bar & Alcohol"), ("reviews", "Reviews"),
    ("marketing", "Marketing"), ("waitlist", "Waitlist & Guest Flow"), ("operations", "Operations"),
    ("technology", "Technology"),
]

MODULES = {
    "labor":      {"key": "labor", "label": "Labor intelligence", "live": True,
                   "does": "Daily labor % against your own target, AI-generated schedules built from your sales patterns, overtime and overstaffing alerts before payroll."},
    "food":       {"key": "inventory", "label": "Food Cost intelligence", "live": True,
                   "does": "Ingredient cost history, supplier price-creep alerts, food cost % vs. target, AI-suggested order quantities and menu margin reads."},
    "reviews":    {"key": "reviews", "label": "Reviews intelligence", "live": True,
                   "does": "Every Google review drafted in your voice for one-click approval, sentiment and topic trends, urgent-review alerts, competitor monitoring."},
    "marketing":  {"key": "marketing", "label": "Marketing intelligence", "live": True,
                   "does": "Social, SMS and email content written in your voice and scheduled from one place, a guest text club, and performance tracking."},
    "bar":        {"key": "bar", "label": "Bar & Alcohol intelligence", "live": False,
                   "does": "Upcoming: pour cost vs. target, variance tracking and drink-level margins. Not available today."},
    "waitlist":   {"key": "waitlist", "label": "Waitlist & guest flow", "live": False,
                   "does": "Upcoming: waitlist and turn-time intelligence. Not available today."},
    "operations": {"key": "platform", "label": "Cavnar AI platform (Home brief, Ask Cavnar, weekly digest)", "live": True,
                   "does": "One daily brief across every connected module, a weekly AI digest by email, push alerts, and Ask Cavnar to question your own numbers in plain English."},
    "technology": {"key": "platform", "label": "Cavnar AI platform (consolidation)", "live": True,
                   "does": "Pulls POS, review and marketing data into one dashboard so fewer tools need checking. Does not replace your POS, payroll or accounting."},
}

# Live pricing — pricing.py is the single source (it mirrors pricing.html).
from pricing import STARTER as _STARTER, FULL as _FULL
PRICING = {
    "starter": {"label": "Starter Module", "setup": _STARTER["setup"], "annual": _STARTER["annual"], "monthly_equiv": _STARTER["monthly"],
                "note": "One module. $%s one-time setup, $%s/yr billed annually ($%s/mo equivalent)." % ("{:,}".format(_STARTER["setup"]), "{:,}".format(_STARTER["annual"]), _STARTER["monthly"])},
    "full":    {"label": "Full System", "setup": _FULL["setup"], "annual": _FULL["annual"], "monthly_equiv": _FULL["monthly"],
                "note": "All four live modules. $%s one-time setup, $%s/yr billed annually ($%s/mo equivalent)." % ("{:,}".format(_FULL["setup"]), "{:,}".format(_FULL["annual"]), "{:,}".format(_FULL["monthly"]))},
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _num(answers, qid, allow_negative=False, max_value=None):
    """A number the engine can trust, or None. Invalid entries (negative
    where not allowed, percentages over 100 unless the question allows it,
    non-numeric) are treated as not provided — never silently used."""
    v = (answers or {}).get(qid)
    if v in (None, "", [], {}):
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    q = QUESTIONS.get(qid, {})
    if f < 0 and not (allow_negative or q.get("allow_negative")):
        return None
    if q.get("type") == "percent":
        cap = q.get("max", 100)
        if f > cap:
            return None
    if q.get("min") is not None and f < q["min"]:
        return None
    if q.get("max") is not None and f > q["max"]:
        return None
    if max_value is not None and f > max_value:
        return None
    return f


def _pos(v):
    """A revenue or cost-percentage of zero is not a real value — treat it
    as not provided rather than as a spectacular result."""
    return v if (v is not None and v > 0) else None


def _yes(answers, qid):
    v = (answers or {}).get(qid)
    if v in ("yes", True, "Yes"):
        return True
    if v in ("no", False, "No"):
        return False
    return None


def _txt(answers, qid):
    v = (answers or {}).get(qid)
    return v if isinstance(v, str) and v.strip() else None


def _rating(answers, qid):
    v = _num(answers, qid)
    return int(v) if v is not None and 1 <= v <= 5 else None


def _rng(base, low, likely, high):
    return {"low": max(0, int(round(base * low, -2))), "likely": max(0, int(round(base * likely, -2))),
            "high": max(0, int(round(base * high, -2)))}


def _round100(x):
    return int(round(x, -2)) if x else 0


def concept_class(answers):
    t = (_txt(answers, "restaurant_type") or "").lower()
    s = (_txt(answers, "service_model") or "").lower()
    if "sports" in t:
        return "sports_bar"
    if "bar" in t or "pub" in t or "bar-forward" in s:
        return "bar"
    if "fine" in t:
        return "fine_dining"
    if "fast" in t or "quick" in t or "counter" in s or "café" in t or "cafe" in t or "bakery" in t:
        return "fast_casual"
    return "full_service"


def _bench(cat, cls):
    table = BENCHMARKS[cat]
    return table.get(cls) or table["full_service"]


def _insufficient(key, label, missing, note=None, module=None):
    return {"key": key, "label": label, "status": "insufficient", "low": 0, "likely": 0, "high": 0,
            "confidence": None, "current_state": note or "Not enough data to calculate reliably.",
            "opportunity": "Insufficient data to calculate reliably", "calc": None, "missing": missing,
            "module": module or MODULES[key]}


# ── Financial derivations (with provenance) ─────────────────────────────────

def derive_financials(a):
    """Every value carries where it came from: owner, calculated, estimated."""
    f = {}

    def put(k, value, source, note=""):
        if value is not None:
            f[k] = {"value": value, "source": source, "note": note}

    years = _num(a, "years_in_business")
    months_open = int(round(years * 12)) if (years is not None and years < 1) else None
    if months_open is not None:
        f["months_open"] = {"value": months_open, "source": "owner", "note": "opened %d month%s ago" % (months_open, "" if months_open == 1 else "s")}
    annual = _pos(_num(a, "fin_annual_revenue"))
    if annual is not None:
        put("annual_revenue", annual, "owner")
    else:
        m = _pos(_num(a, "fin_monthly_revenue"))
        w = _pos(_num(a, "fin_weekly_revenue"))
        d = _pos(_num(a, "fin_daily_sales"))
        days = _num(a, "days_open")
        if m is not None:
            put("annual_revenue", m * 12, "estimated" if months_open else "calculated",
                "monthly revenue × 12" + (" — annualized from an opening period of %d months, which is rarely representative" % months_open if months_open else ""))
        elif w is not None:
            put("annual_revenue", w * 52, "calculated", "weekly revenue × 52")
        elif d is not None:
            dd = days if days else 7
            put("annual_revenue", d * dd * 52, "calculated" if days else "estimated",
                "average daily sales × %s days × 52%s" % (int(dd), "" if days else " (days open assumed 7)"))
    R = f.get("annual_revenue", {}).get("value")

    m = _num(a, "fin_monthly_revenue")
    if m is not None:
        put("monthly_revenue", m, "owner")
    elif R:
        put("monthly_revenue", R / 12.0, "calculated", "annual revenue ÷ 12")
    w = _num(a, "fin_weekly_revenue")
    if w is not None:
        put("weekly_revenue", w, "owner")
    elif R:
        put("weekly_revenue", R / 52.0, "calculated", "annual revenue ÷ 52")

    # Alcohol
    alc = _num(a, "bar_alcohol_sales")
    alc_pct = _num(a, "bar_alcohol_pct")
    parts = [x for x in (_num(a, "bar_beer_sales"), _num(a, "bar_wine_sales"), _num(a, "bar_liquor_sales")) if x is not None]
    if alc is not None:
        put("alcohol_sales", alc, "owner")
    elif alc_pct is not None and R:
        put("alcohol_sales", R * alc_pct / 100.0, "calculated", "annual revenue × alcohol %")
    elif parts:
        put("alcohol_sales", sum(parts), "calculated", "beer + wine + liquor sales")
    A = f.get("alcohol_sales", {}).get("value")
    if alc_pct is not None:
        put("alcohol_pct", alc_pct, "owner")
    elif A is not None and R:
        put("alcohol_pct", 100.0 * A / R, "calculated", "alcohol sales ÷ revenue")

    # Food sales
    fs = _num(a, "food_sales")
    if fs is not None:
        put("food_sales", fs, "owner")
    elif R and A is not None:
        put("food_sales", max(0.0, R - A), "calculated", "annual revenue − alcohol sales")
    elif R and concept_class(a) in ("full_service", "fine_dining", "fast_casual") and A is None and alc_pct is None:
        # Non-bar concepts: no bar figure given. Leave unset; the food
        # calculation will ask for it rather than assume a mix.
        pass
    FS = f.get("food_sales", {}).get("value")

    # Labor %
    lp = _pos(_num(a, "lab_labor_pct"))
    if lp is not None:
        put("labor_pct", lp, "owner", _txt(a, "lab_labor_pct_basis") or "")
    else:
        ld = _num(a, "lab_labor_dollars")
        per = _txt(a, "lab_labor_period") or "Year"
        mult = {"Week": 52, "Month": 12, "Year": 1}.get(per, 1)
        if ld is not None and R:
            put("labor_dollars", ld * mult, "calculated" if mult != 1 else "owner", "labor per %s × %d" % (per.lower(), mult))
            put("labor_pct", 100.0 * ld * mult / R, "calculated", "labor dollars ÷ revenue")
        elif _num(a, "fin_labor_cost") is not None and R:
            put("labor_dollars", _num(a, "fin_labor_cost"), "owner")
            put("labor_pct", 100.0 * _num(a, "fin_labor_cost") / R, "calculated", "annual labor cost ÷ revenue")
    if "labor_dollars" not in f and f.get("labor_pct") and R:
        put("labor_dollars", R * f["labor_pct"]["value"] / 100.0, "calculated", "revenue × labor %")

    # Food %
    fp = _pos(_num(a, "food_cost_pct"))
    combined = (_txt(a, "food_includes_bev") == "Combined")
    if fp is not None:
        put("food_pct", fp, "owner", "combined food + beverage" if combined else (_txt(a, "food_cost_basis") or ""))
        f["food_pct_combined"] = {"value": combined, "source": "owner", "note": ""}
    else:
        fc = _num(a, "fin_food_cost")
        purchases = _num(a, "food_purchases")
        if fc is not None and FS:
            put("food_pct", 100.0 * fc / FS, "calculated", "annual food cost ÷ food sales")
        elif purchases is not None and FS:
            bi, ei = _num(a, "food_begin_inv"), _num(a, "food_end_inv")
            usage = purchases + ((bi - ei) if (bi is not None and ei is not None) else 0)
            put("food_pct", 100.0 * usage / FS, "calculated",
                "(purchases + beginning − ending inventory) ÷ food sales" if bi is not None else "purchases ÷ food sales")
        f["food_pct_combined"] = {"value": False, "source": "calculated", "note": ""}

    # Beverage %
    bp = _pos(_num(a, "bar_bev_cost_pct"))
    if bp is not None:
        put("bev_pct", bp, "owner")
    else:
        bc = _num(a, "fin_bev_cost")
        if bc is not None and A:
            put("bev_pct", 100.0 * bc / A, "calculated", "annual beverage cost ÷ alcohol sales")
        else:
            cats = [(_num(a, "bar_liquor_cost_pct"), _num(a, "bar_liquor_sales")),
                    (_num(a, "bar_beer_cost_pct"), _num(a, "bar_beer_sales")),
                    (_num(a, "bar_wine_cost_pct"), _num(a, "bar_wine_sales"))]
            known = [(c, s) for c, s in cats if c is not None]
            if known:
                if all(s for _, s in known):
                    tot = sum(s for _, s in known)
                    put("bev_pct", sum(c * s for c, s in known) / tot, "calculated", "sales-weighted category pour costs")
                else:
                    put("bev_pct", sum(c for c, _ in known) / len(known), "estimated", "simple average of category pour costs (no sales weights)")

    # Prime cost
    pc = _num(a, "fin_prime_cost_pct")
    if pc is not None:
        put("prime_cost_pct", pc, "owner")
    elif f.get("labor_pct") and f.get("food_pct") and R:
        L = f["labor_pct"]["value"]
        if f.get("food_pct_combined", {}).get("value"):
            put("prime_cost_pct", L + f["food_pct"]["value"], "calculated", "labor % + combined COGS %")
        elif FS is not None and A is not None and f.get("bev_pct"):
            cogs = FS * f["food_pct"]["value"] / 100.0 + A * f["bev_pct"]["value"] / 100.0
            put("prime_cost_pct", L + 100.0 * cogs / R, "calculated", "labor % + (food cost $ + beverage cost $) ÷ revenue")
        else:
            put("prime_cost_pct", L + f["food_pct"]["value"], "estimated", "labor % + food % (beverage cost not separated)")

    # Net margin
    nm = _num(a, "fin_net_margin", allow_negative=True)
    npf = _num(a, "fin_net_profit", allow_negative=True)
    if nm is not None:
        put("net_margin", nm, "owner")
    elif npf is not None and R:
        put("net_margin", 100.0 * npf / R, "calculated", "net profit ÷ revenue")

    ac = _num(a, "fin_avg_check")
    if ac is not None:
        put("avg_check", ac, "owner")
    cov = _num(a, "fin_covers_week")
    if cov is not None:
        put("covers_week", cov, "owner")
    elif ac and f.get("weekly_revenue"):
        put("covers_week", f["weekly_revenue"]["value"] / ac, "calculated", "weekly revenue ÷ average check")

    # Software spend (from the tools table, else the financial field)
    tools = (a or {}).get("tech_tools") or []
    sw = 0.0
    any_tool_cost = False
    for row in tools if isinstance(tools, list) else []:
        try:
            c = float(str(row.get("cost") or "").replace(",", "").replace("$", "") or 0)
        except (TypeError, ValueError, AttributeError):
            c = 0
        if c > 0:
            any_tool_cost = True
            sw += c
    if any_tool_cost:
        put("software_month", sw, "calculated", "sum of the software stack")
    elif _num(a, "fin_software_spend") is not None:
        put("software_month", _num(a, "fin_software_spend"), "owner")
    return f


# ── Category opportunity calculations ───────────────────────────────────────

# Recovered improvement is capped in percentage points of the base, whatever
# the gap. Scheduling and cost tracking realistically move a cost line by one
# to three points; a restaurant ten points over benchmark has a structural
# problem (wage market, menu, concept) that monitoring does not fix, and
# quoting 70% of that gap would be the kind of number that loses the room.
POINT_CAPS = {"labor": (1.0, 2.0, 3.0), "food": (1.0, 2.0, 3.0), "bar": (1.0, 2.0, 4.0)}


def _gap_calc(base, current, target, band, source, base_label, metric_label, caps=(1.0, 2.0, 3.0)):
    gap_pts = current - target
    g = max(0.0, gap_pts)
    pts = {"low": min(g * RECOVERY["low"], caps[0]), "likely": min(g * RECOVERY["likely"], caps[1]), "high": min(g * RECOVERY["high"], caps[2])}
    r = {k: max(0, int(round(base * v / 100.0, -2))) for k, v in pts.items()}
    gap_dollars = base * g / 100.0
    capped = g > 0 and (g * RECOVERY["high"] > caps[2] or g * RECOVERY["likely"] > caps[1] or g * RECOVERY["low"] > caps[0])
    calc = {
        "current_metric": "%s %.1f%%" % (metric_label, current),
        "benchmark": "target %.1f%% (band %d–%d%%)" % (target, band[0], band[1]),
        "benchmark_source": source,
        "base": "%s $%s" % (base_label, "{:,.0f}".format(base)),
        "formula": "gap %.1f pts × $%s = $%s; recover %d%% / %d%% / %d%% of the gap = %.1f / %.1f / %.1f pts%s" % (
            g, "{:,.0f}".format(base), "{:,.0f}".format(gap_dollars),
            RECOVERY["low"] * 100, RECOVERY["likely"] * 100, RECOVERY["high"] * 100,
            pts["low"], pts["likely"], pts["high"], " (capped at %.0f / %.0f / %.0f pts)" % caps if capped else ""),
        "assumptions": ["Only the gap above the target is counted, never the whole cost line.",
                        "A benchmark gap is never treated as fully recoverable; the range brackets 30–70% of it.",
                        "Recovered improvement is capped at %.0f / %.0f / %.0f points of the base even when the gap is larger — beyond that the cause is usually structural, not something daily visibility fixes." % caps],
        "gap_pts": round(gap_pts, 1), "gap_dollars": _round100(gap_dollars), "recovered_pts": {k: round(v, 2) for k, v in pts.items()},
    }
    return r, calc


def calc_labor(a, fin, cls, owner):
    R = fin.get("annual_revenue", {}).get("value")
    L = fin.get("labor_pct", {}).get("value")
    bench = _bench("labor", cls)
    band = bench["band"]
    owner_target = _num(a, "lab_target_pct")
    target = owner_target if owner_target is not None else band[1]
    missing = []
    if R is None:
        missing.append("Ask %s for approximate annual (or monthly) revenue — nothing in the labor estimate works without it." % owner)
    if L is None:
        missing.append("Ask %s for labor as a %% of sales, or weekly payroll dollars, to size the labor opportunity." % owner)
    if owner_target is None and L is not None:
        missing.append("Ask what labor target %s actually aims for — it replaces the generic benchmark." % owner)

    ot_yes = _yes(a, "lab_overtime")
    ot_week = _num(a, "lab_ot_dollars_week")
    ot_hours = _num(a, "lab_ot_hours_week")
    wage = _num(a, "lab_avg_wage")
    ot_calc = None
    ot_rng = None
    if ot_yes:
        ot_annual = None
        how = ""
        if ot_week is not None:
            ot_annual = ot_week * 52
            how = "$%s/week overtime × 52" % "{:,.0f}".format(ot_week)
        elif ot_hours is not None and wage is not None:
            ot_annual = ot_hours * wage * 1.5 * 52
            how = "%.0f OT hours/week × $%.2f × 1.5 × 52" % (ot_hours, wage)
        elif ot_hours is not None:
            missing.append("Ask %s the average hourly wage (or weekly overtime dollars) to price the overtime." % owner)
        else:
            missing.append("Ask %s roughly how many overtime hours a week, or what overtime costs per pay period." % owner)
        if ot_annual:
            premium = ot_annual / 3.0  # the 0.5× premium share of time-and-a-half pay
            ot_rng = _rng(premium, 0.4, 0.7, 1.0)
            ot_calc = {"current_metric": "overtime ≈ $%s/yr" % "{:,.0f}".format(ot_annual),
                       "benchmark": "overtime premium avoidable through scheduling",
                       "base": how,
                       "formula": "overtime $ ÷ 3 = premium portion ($%s); × 40%% / 70%% / 100%% avoidable" % "{:,.0f}".format(premium),
                       "assumptions": ["Hours still get worked by someone at straight time, so only the 0.5× premium counts as savings.",
                                       "Assumes overtime is mostly a scheduling problem rather than a structural staffing shortage."]}

    module = MODULES["labor"]
    if L is None or R is None:
        if ot_rng:
            out = {"key": "labor", "label": "Labor", "status": "ok", "confidence": "low",
                   "current_state": "Overtime is regular; labor % not provided.",
                   "opportunity": "Overtime premium avoidable through forecast-based scheduling",
                   "calc": dict(ot_calc, method="overtime only — labor % unknown",
                                overlap="Overtime is part of labor dollars. When labor % arrives, the audit takes the larger of the two estimates, not the sum."),
                   "missing": missing, "module": module}
            out.update(ot_rng)
            return out
        return _insufficient("labor", "Labor", missing)

    gap_rng, gap_calc = _gap_calc(R, L, target, band, bench["source"], "annual revenue", "labor", POINT_CAPS["labor"])
    basis = _txt(a, "lab_labor_pct_basis")
    if basis == "Wages only":
        gap_calc["assumptions"].append("Owner's labor % is wages only; the benchmark includes benefits, so the real gap is likely wider, not narrower.")
    if owner_target is None:
        gap_calc["assumptions"].append(bench["note"])
    else:
        gap_calc["assumptions"].append("Target is the owner's own, not a generic benchmark.")

    conf = "high" if (fin["labor_pct"]["source"] == "owner" and fin["annual_revenue"]["source"] == "owner") else "moderate"
    if L <= target:
        state = "Labor at %.1f%% is at or below the %s target of %.1f%%." % (L, "owner's" if owner_target is not None else "benchmark", target)
        if ot_rng:
            out = {"key": "labor", "label": "Labor", "status": "ok", "confidence": "moderate",
                   "current_state": state + " Overtime is the remaining leak.",
                   "opportunity": "Overtime premium avoidable through forecast-based scheduling",
                   "calc": dict(ot_calc, method="overtime (labor % already on target)",
                                overlap="Labor % is on target, so only the overtime premium is counted."),
                   "missing": missing, "module": module}
            out.update(ot_rng)
            return out
        return {"key": "labor", "label": "Labor", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": conf,
                "current_state": state, "opportunity": "Performing well — no gap-based opportunity counted",
                "calc": dict(gap_calc, method="gap to target", overlap="none"), "missing": missing, "module": module}

    # Overlap rule: the larger of gap-based and overtime-based, never the sum.
    use_ot = bool(ot_rng and ot_rng["likely"] > gap_rng["likely"])
    chosen = ot_rng if use_ot else gap_rng
    calc = dict(ot_calc if use_ot else gap_calc)
    calc["method"] = "overtime premium (larger than the gap estimate)" if use_ot else "gap to target"
    calc["overlap"] = ("Overtime is inside labor dollars. The audit counts the LARGER of the gap-to-target estimate ($%s likely) and the overtime estimate ($%s likely) — never both."
                       % ("{:,.0f}".format(gap_rng["likely"]), "{:,.0f}".format(ot_rng["likely"]))) if ot_rng else "No overtime figure given, so nothing to overlap."
    out = {"key": "labor", "label": "Labor", "status": "ok", "confidence": conf,
           "current_state": "Labor at %.1f%% vs. a target of %.1f%% (%.1f points over)." % (L, target, L - target),
           "opportunity": "Bring labor toward target through forecast-based scheduling and same-shift visibility",
           "calc": calc, "missing": missing, "module": module}
    out.update(chosen)
    return out


def calc_food(a, fin, cls, owner):
    R = fin.get("annual_revenue", {}).get("value")
    F = fin.get("food_pct", {}).get("value")
    combined = bool(fin.get("food_pct_combined", {}).get("value"))
    FS = R if combined else fin.get("food_sales", {}).get("value")
    missing = []
    if F is None:
        missing.append("Ask %s roughly where food cost has been running (or annual food purchases and food sales)." % owner)
    if FS is None and F is not None:
        missing.append("Ask %s for annual food sales, or the bar's share of sales, so the food-cost gap has a base." % owner)
    if _num(a, "food_target_pct") is None and F is not None:
        missing.append("Ask what food cost target %s works to." % owner)

    waste_week = _num(a, "food_waste_week")
    waste_rng = waste_calc = None
    if waste_week:
        waste_annual = waste_week * 52
        waste_rng = _rng(waste_annual, 0.25, 0.40, 0.50)
        waste_calc = {"current_metric": "waste ≈ $%s/yr" % "{:,.0f}".format(waste_annual),
                      "benchmark": "25–50% of logged waste avoidable", "benchmark_source": BENCHMARKS["waste"]["source"],
                      "base": "$%s/week waste × 52" % "{:,.0f}".format(waste_week),
                      "formula": "waste $ × 25% / 40% / 50%",
                      "assumptions": ["Owner's own waste estimate; usually an under-count.", "Waste is a component of food cost, so it is never added on top of a food-cost gap."]}

    module = MODULES["food"]
    if F is None or FS is None:
        if waste_rng:
            out = {"key": "food", "label": "Food Cost", "status": "ok", "confidence": "low",
                   "current_state": "Food cost % not sized; waste estimate provided.",
                   "opportunity": "Reduce logged waste through ordering and prep discipline",
                   "calc": dict(waste_calc, method="waste only", overlap="When food cost % arrives, the larger of gap and waste is used."),
                   "missing": missing, "module": module}
            out.update(waste_rng)
            return out
        return _insufficient("food", "Food Cost", missing)

    bench = BENCHMARKS["food"]["combined"] if combined else _bench("food", cls)
    band = bench["band"]
    owner_target = _num(a, "food_target_pct")
    target = owner_target if owner_target is not None else band[1]
    gap_rng, gap_calc = _gap_calc(FS, F, target, band, bench["source"], "annual revenue (combined COGS)" if combined else "annual food sales",
                                  "combined food + beverage cost" if combined else "food cost", POINT_CAPS["food"])
    basis = _txt(a, "food_cost_basis")
    if basis == "Theoretical (from recipes)":
        gap_calc["assumptions"].append("Owner's figure is theoretical; actual is typically higher, so the gap is likely understated.")
    if combined:
        gap_calc["assumptions"].append("Owner's figure combines food and beverage, so it is compared with a blended COGS band and the bar category is not sized separately.")
    if owner_target is None:
        gap_calc["assumptions"].append(bench["note"])
    conf = "high" if (fin["food_pct"]["source"] == "owner" and basis == "Actual (from inventory)" and not combined) else ("moderate" if fin["food_pct"]["source"] == "owner" else "low")

    if F <= target:
        state = "Food cost at %.1f%% is at or below the target of %.1f%%." % (F, target)
        if waste_rng:
            out = {"key": "food", "label": "Food Cost", "status": "ok", "confidence": "moderate", "current_state": state + " Waste is the remaining leak.",
                   "opportunity": "Reduce logged waste through ordering and prep discipline",
                   "calc": dict(waste_calc, method="waste (food cost already on target)", overlap="Food cost is on target, so only waste is counted."),
                   "missing": missing, "module": module}
            out.update(waste_rng)
            return out
        return {"key": "food", "label": "Food Cost", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": conf,
                "current_state": state, "opportunity": "Performing well — no gap-based opportunity counted",
                "calc": dict(gap_calc, method="gap to target", overlap="none"), "missing": missing, "module": module}

    use_waste = bool(waste_rng and waste_rng["likely"] > gap_rng["likely"])
    chosen = waste_rng if use_waste else gap_rng
    calc = dict(waste_calc if use_waste else gap_calc)
    calc["method"] = "waste (larger than the gap estimate)" if use_waste else "gap to target"
    calc["overlap"] = ("Waste is inside food cost. The audit counts the LARGER of the gap estimate ($%s likely) and the waste estimate ($%s likely) — never both. Comps and discounts are counted under Operations only."
                       % ("{:,.0f}".format(gap_rng["likely"]), "{:,.0f}".format(waste_rng["likely"]))) if waste_rng else "No waste figure given. Comps and discounts are counted under Operations only."
    out = {"key": "food", "label": "Food Cost", "status": "ok", "confidence": conf,
           "current_state": "%s at %.1f%% vs. a target of %.1f%% (%.1f points over)." % ("Combined COGS" if combined else "Food cost", F, target, F - target),
           "opportunity": "Close the gap to target with price-creep alerts, theoretical-vs-actual tracking and tighter ordering",
           "calc": calc, "missing": missing, "module": module}
    out.update(chosen)
    return out


def calc_bar(a, fin, cls, owner):
    A = fin.get("alcohol_sales", {}).get("value")
    P = fin.get("bev_pct", {}).get("value")
    combined = bool(fin.get("food_pct_combined", {}).get("value"))
    module = MODULES["bar"]
    missing = []
    if A is None:
        missing.append("Ask %s for the bar's share of sales (or annual alcohol sales) — the bar can't be sized without it." % owner)
    if P is None:
        missing.append("Ask %s if they know their true pour cost (beverage cost %% of alcohol sales)." % owner)
    if combined:
        return {"key": "bar", "label": "Bar & Alcohol", "status": "insufficient", "low": 0, "likely": 0, "high": 0, "confidence": None,
                "current_state": "Beverage cost is folded into the owner's combined COGS figure, so it is sized inside Food Cost to avoid double counting.",
                "opportunity": "Counted inside Food Cost (combined COGS)", "calc": None,
                "missing": ["Ask %s to separate food and beverage cost so the bar can be sized on its own." % owner], "module": module}

    bench = BENCHMARKS["bar"]["blended"]
    band = bench["band"]
    owner_target = _num(a, "bar_target_pct")
    target = owner_target if owner_target is not None else band[1]
    var_pct = _num(a, "bar_variance_pct")
    var_rng = var_calc = None
    var_calc = None
    if A and var_pct is not None:
        pour = P if P is not None else 22.0
        cogs = A * pour / 100.0
        excess = max(0.0, var_pct - BENCHMARKS["bar"]["variance_target"])
        lost = cogs * excess / 100.0
        var_rng = _rng(lost, 0.4, 0.6, 0.8) if lost > 0 else None
        var_calc = {"current_metric": "inventory variance %.1f%% of usage" % var_pct,
                    "benchmark": "variance target ≤ %.0f%%" % BENCHMARKS["bar"]["variance_target"],
                    "benchmark_source": BENCHMARKS["bar"]["variance_source"],
                    "base": "beverage COGS ≈ $%s (alcohol sales × %.0f%% pour cost%s)" % ("{:,.0f}".format(cogs), pour, "" if P is not None else ", pour cost assumed"),
                    "formula": "COGS × (variance − 3%%) = $%s excess loss; × 40%% / 60%% / 80%% recoverable" % "{:,.0f}".format(lost),
                    "assumptions": ["Only variance above a 3% allowance is counted.", "Variance is a component of actual pour cost, so it is never added on top of a pour-cost gap."]}
        if P is None:
            var_calc["assumptions"].append("Pour cost was not given, so 22% was assumed for the COGS base — an estimate.")

    if A is None or P is None:
        if var_rng:
            out = {"key": "bar", "label": "Bar & Alcohol", "status": "ok", "confidence": "low",
                   "current_state": "Pour cost not sized; inventory variance of %.1f%% provided." % var_pct,
                   "opportunity": "Bring variance under 3% with counts against POS and pour standards",
                   "calc": dict(var_calc, method="variance only", overlap="When pour cost arrives, the larger of gap and variance is used."),
                   "missing": missing, "module": module}
            out.update(var_rng)
            return out
        return _insufficient("bar", "Bar & Alcohol", missing)

    gap_rng, gap_calc = _gap_calc(A, P, target, band, bench["source"], "annual alcohol sales", "pour cost", POINT_CAPS["bar"])
    if owner_target is None:
        gap_calc["assumptions"].append(bench["note"])
    conf = "high" if (fin["bev_pct"]["source"] == "owner" and fin["alcohol_sales"]["source"] == "owner" and _yes(a, "bar_theoretical")) else ("moderate" if fin["bev_pct"]["source"] != "estimated" else "low")

    if P <= target:
        state = "Pour cost at %.1f%% is at or below the target of %.1f%%." % (P, target)
        if var_rng:
            out = {"key": "bar", "label": "Bar & Alcohol", "status": "ok", "confidence": "moderate", "current_state": state + " Variance is the remaining leak.",
                   "opportunity": "Bring variance under 3% with counts against POS and pour standards",
                   "calc": dict(var_calc, method="variance (pour cost already on target)", overlap="Pour cost is on target, so only excess variance is counted."),
                   "missing": missing, "module": module}
            out.update(var_rng)
            return out
        return {"key": "bar", "label": "Bar & Alcohol", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": conf,
                "current_state": state, "opportunity": "Performing well — no gap-based opportunity counted",
                "calc": dict(gap_calc, method="gap to target", overlap="none"), "missing": missing, "module": module}

    use_var = bool(var_rng and var_rng["likely"] > gap_rng["likely"])
    chosen = var_rng if use_var else gap_rng
    calc = dict(var_calc if use_var else gap_calc)
    calc["method"] = "variance (larger than the gap estimate)" if use_var else "gap to target"
    calc["overlap"] = ("Variance is inside pour cost. The audit counts the LARGER of the gap estimate ($%s likely) and the variance estimate ($%s likely) — never both. Bar comps are counted under Operations only."
                       % ("{:,.0f}".format(gap_rng["likely"]), "{:,.0f}".format(var_rng["likely"]))) if var_rng else "No variance figure given. Bar comps are counted under Operations only."
    out = {"key": "bar", "label": "Bar & Alcohol", "status": "ok", "confidence": conf,
           "current_state": "Pour cost at %.1f%% vs. a target of %.1f%% (%.1f points over)." % (P, target, P - target),
           "opportunity": "Close the pour-cost gap through variance tracking, pour standards and drink-level margins",
           "calc": calc, "missing": missing, "module": module}
    out.update(chosen)
    return out


def calc_reviews(a, fin, cls, owner):
    R = fin.get("annual_revenue", {}).get("value")
    rating = _num(a, "rev_google_rating")
    missing = []
    if rating is None:
        missing.append("Look up %s's Google rating and review count — it takes ten seconds and anchors the whole reviews conversation." % owner)
    if R is None:
        missing.append("Revenue is needed to size the reputation opportunity.")
    module = MODULES["reviews"]
    if rating is None or R is None:
        return _insufficient("reviews", "Reviews", missing)
    rr = _num(a, "rev_response_rate")
    rt = _txt(a, "rev_response_time")
    unanswered = _yes(a, "rev_unanswered")
    personal = _txt(a, "rev_personalized")
    slow = rt in ("Within a week", "Longer", "Don't respond")
    weak_process = (rr is not None and rr < 60) or slow or unanswered is True or personal == "Templated" or _yes(a, "rev_categorize") is False
    if rating >= 4.6 and not weak_process:
        return {"key": "reviews", "label": "Reviews", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": "moderate",
                "current_state": "Google rating %.1f with a consistent response process." % rating,
                "opportunity": "Performing well — protect it", "calc": None, "missing": missing, "module": module}
    if rating >= 4.6:
        lo, lk, hi = 0.001, 0.002, 0.004
        star = "0.02–0.08"
    elif rating >= 4.3:
        lo, lk, hi = 0.0015, 0.003, 0.006
        star = "0.03–0.12"
    else:
        lo, lk, hi = 0.0025, 0.005, 0.01
        star = "0.05–0.2"
    rng = _rng(R, lo, lk, hi)
    calc = {"current_metric": "Google rating %.1f%s%s" % (rating, (", %d reviews" % _num(a, "rev_google_count")) if _num(a, "rev_google_count") else "", (", %.0f%% response rate" % rr) if rr is not None else ""),
            "benchmark": "%s star improvement × 5–9%% revenue per star" % star,
            "benchmark_source": BENCHMARKS["reviews"]["source"],
            "base": "annual revenue $%s" % "{:,.0f}".format(R),
            "formula": "revenue × %.2f%% / %.2f%% / %.1f%%" % (lo * 100, lk * 100, hi * 100),
            "assumptions": [BENCHMARKS["reviews"]["note"], "This is added revenue, not a cost saving — it is counted in the revenue line, never mixed into cost savings — and it is the most speculative category in the audit, so it is labelled low confidence."],
            "method": "reputation lift", "overlap": "Does not overlap any cost category. Owner time spent on reviews is reported as hours, not dollars."}
    state = "Google rating %.1f." % rating
    if rr is not None:
        state += " Response rate about %.0f%%." % rr
    if slow:
        state += " Responses are slow or absent."
    if unanswered:
        state += " Negative reviews sometimes go unanswered."
    out = {"key": "reviews", "label": "Reviews", "status": "ok", "confidence": "low", "current_state": state,
           "opportunity": "Consistent, personal responses and acting on complaint patterns to lift rating and sentiment",
           "calc": calc, "missing": missing, "module": module}
    out.update(rng)
    return out


def calc_marketing(a, fin, cls, owner):
    spend = _num(a, "mkt_spend_month")
    if spend is None:
        parts = [x for x in (_num(a, "mkt_paid_ads"), _num(a, "mkt_social_spend"), _num(a, "mkt_google_ads"),
                             _num(a, "mkt_meta_ads"), _num(a, "mkt_agency_cost"), _num(a, "mkt_software_cost")) if x is not None]
        spend = sum(parts) if parts else _num(a, "fin_marketing_spend")
    agency = _num(a, "mkt_agency_cost") or 0.0
    replace = _txt(a, "mkt_agency_replace")
    module = MODULES["marketing"]
    missing = []
    if spend is None:
        missing.append("Ask %s roughly what marketing costs per month, agency included." % owner)
        return _insufficient("marketing", "Marketing", missing)
    roi = _yes(a, "mkt_roi_tracked")
    knows = _yes(a, "mkt_know_channels")
    decide = _txt(a, "mkt_decide")
    untracked = roi is False or knows is False or decide in ("Gut instinct", "Whatever worked before")
    ad_spend = max(0.0, spend - agency)
    eff = _rng(ad_spend * 12, 0.10, 0.15, 0.25) if (untracked and ad_spend > 0) else {"low": 0, "likely": 0, "high": 0}
    ag = {"low": 0, "likely": 0, "high": 0}
    ag_note = "No agency fee, or the owner would keep the agency — nothing counted."
    if agency and replace == "Yes":
        ag = _rng(agency * 12, 0.5, 0.75, 1.0)
        ag_note = "Owner said they would replace the agency: 50–100%% of the $%s/yr fee counted." % "{:,.0f}".format(agency * 12)
    elif agency and replace == "Maybe":
        ag = _rng(agency * 12, 0.0, 0.25, 0.5)
        ag_note = "Owner might replace the agency: 0–50%% of the $%s/yr fee counted." % "{:,.0f}".format(agency * 12)
    total = {k: eff[k] + ag[k] for k in ("low", "likely", "high")}
    if roi is None and knows is None:
        missing.append("Ask %s whether they track which campaigns actually produce revenue." % owner)
    if total["likely"] == 0 and total["high"] == 0:
        state = "Marketing spend about $%s/mo%s." % ("{:,.0f}".format(spend), " with ROI tracked" if roi else "")
        return {"key": "marketing", "label": "Marketing", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": "moderate",
                "current_state": state, "opportunity": "Performing well — spend is tracked and no agency replacement in play",
                "calc": None, "missing": missing, "module": module}
    calc = {"current_metric": "spend $%s/mo%s%s" % ("{:,.0f}".format(spend), (", agency $%s/mo" % "{:,.0f}".format(agency)) if agency else "", "" if roi else ", ROI not tracked"),
            "benchmark": "10–25% of untracked ad spend redirectable; agency fee per owner's answer",
            "benchmark_source": "Operator assumption. There is no authoritative benchmark for wasted restaurant ad spend; the range is deliberately modest.",
            "base": "ad spend $%s/yr; agency $%s/yr" % ("{:,.0f}".format(ad_spend * 12), "{:,.0f}".format(agency * 12)),
            "formula": "ad spend × 10% / 15% / 25% (only when ROI is not tracked) + agency share",
            "assumptions": ["Redirected spend is treated as savings; in practice it may be re-spent on channels that work.", ag_note],
            "method": "spend efficiency + agency replacement", "overlap": "Agency fees are counted here and excluded from Technology. Marketing software is counted in Technology only if the owner marks it replaceable."}
    state = "Spend about $%s/mo." % "{:,.0f}".format(spend)
    if untracked:
        state += " Channel ROI is not tracked."
    out = {"key": "marketing", "label": "Marketing", "status": "ok", "confidence": "low" if untracked else "moderate", "current_state": state,
           "opportunity": "Data-driven campaigns on slow days and retention outreach instead of blanket promotions",
           "calc": calc, "missing": missing, "module": module}
    out.update(total)
    return out


def calc_waitlist(a, fin, cls, owner):
    walk = _num(a, "wl_walkaways_week")
    ac = fin.get("avg_check", {}).get("value")
    party = _num(a, "wl_party_size")
    module = MODULES["waitlist"]
    missing = []
    if walk is None:
        missing.append("Ask %s how many parties walk on a busy week because the wait is too long." % owner)
    if ac is None:
        missing.append("Ask %s the average check per guest to value lost covers." % owner)
    if walk is None or ac is None:
        return _insufficient("waitlist", "Waitlist & Guest Flow", missing)
    if walk == 0:
        return {"key": "waitlist", "label": "Waitlist & Guest Flow", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": "moderate",
                "current_state": "Owner reports no meaningful walkaways.", "opportunity": "Performing well", "calc": None, "missing": [], "module": module}
    ps = party if party else 2.5
    lost = walk * ps * ac * 52
    rng = _rng(lost, 0.25, 0.40, 0.50)
    calc = {"current_metric": "≈ %.0f walkaway parties/week" % walk,
            "benchmark": "25–50% of walkaways recoverable with accurate quotes and texting",
            "benchmark_source": "Operator assumption; no authoritative industry figure for walkaway recovery.",
            "base": "%.0f parties × %.1f guests × $%.0f check × 52 = $%s lost revenue/yr" % (walk, ps, ac, "{:,.0f}".format(lost)),
            "formula": "lost revenue × 25% / 40% / 50%",
            "assumptions": ["Revenue, not profit — contribution margin on recovered covers is typically 60–70% after food and beverage cost.",
                            "Walkaway count is the owner's estimate." + ("" if party else " Party size assumed at 2.5 — an estimate.")],
            "method": "recovered walkaways", "overlap": "Does not overlap any cost category."}
    if not party:
        missing.append("Ask %s the average party size to firm up the walkaway estimate." % owner)
    out = {"key": "waitlist", "label": "Waitlist & Guest Flow", "status": "ok", "confidence": "low", "current_state": "About %.0f parties a week walk during peaks." % walk,
           "opportunity": "Recover walkaways with accurate quotes, texting and turn-time visibility",
           "calc": calc, "missing": missing, "module": module}
    out.update(rng)
    return out


def calc_operations(a, fin, cls, owner):
    comps = _num(a, "ops_comps_month")
    src = "comps + voids + discounts $%s/mo" % ("{:,.0f}".format(comps) if comps is not None else "?")
    if comps is None:
        parts = [x for x in (_num(a, "food_comps_week"), _num(a, "food_discounts_week"), _num(a, "bar_comps_week")) if x is not None]
        if parts:
            comps = sum(parts) * 52 / 12.0
            src = "food comps + discounts + bar comps ≈ $%s/mo" % "{:,.0f}".format(comps)
    monitored = _txt(a, "ops_comps_monitored")
    hours = _num(a, "ops_hours_data")
    module = MODULES["operations"]
    missing = []
    if comps is None:
        missing.append("Ask %s what comps, voids and discounts run per month — the POS has it." % owner)
        out = _insufficient("operations", "Operations", missing)
        out["hours_week"] = hours
        return out
    if monitored == "Daily":
        return {"key": "operations", "label": "Operations", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": "moderate",
                "current_state": "Comps and voids (%s) are reviewed daily by person." % src, "opportunity": "Performing well — controls in place",
                "calc": None, "missing": [], "module": module, "hours_week": hours}
    rng = _rng(comps * 12, 0.15, 0.25, 0.35)
    calc = {"current_metric": src, "benchmark": "15–35% of unmonitored comps/voids/discounts avoidable",
            "benchmark_source": "Operator assumption. Comp and void leakage varies widely; the range is intentionally modest.",
            "base": "$%s/yr" % "{:,.0f}".format(comps * 12), "formula": "annual comps/voids/discounts × 15% / 25% / 35%",
            "assumptions": ["Counted here only — never inside Food Cost or Bar.", "Assumes visibility by employee and daily review, not a policy of refusing comps."],
            "method": "comp and void control", "overlap": "Waste (Food Cost) and overtime (Labor) are excluded. Owner reporting hours are shown separately as time, not dollars."}
    out = {"key": "operations", "label": "Operations", "status": "ok", "confidence": "moderate" if comps is not None else "low",
           "current_state": "%s, reviewed %s." % (src[0].upper() + src[1:], (monitored or "irregularly").lower()),
           "opportunity": "Daily comp, void and discount visibility by person", "calc": calc, "missing": missing, "module": module, "hours_week": hours}
    out.update(rng)
    return out


NEVER_REPLACED = {"pos", "payroll", "accounting"}


def calc_technology(a, fin, cls, owner):
    tools = (a or {}).get("tech_tools") or []
    module = MODULES["technology"]
    rows = []
    for row in tools if isinstance(tools, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            cost = float(str(row.get("cost") or "").replace(",", "").replace("$", "") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        if cost < 0:
            cost = 0.0
        rows.append({"category": row.get("category") or "", "name": row.get("name") or "", "cost": cost,
                     "replace": (row.get("replace") in ("yes", True)), "duplicate": (row.get("duplicate") in ("yes", True))})
    total_month = sum(r["cost"] for r in rows)
    if not rows or total_month == 0:
        return _insufficient("technology", "Technology", ["Walk %s through the software list with monthly costs — most owners under-count it." % owner])
    replaceable = [r for r in rows if r["replace"] and r["category"].lower() not in NEVER_REPLACED and r["cost"] > 0]
    excluded = [r for r in rows if r["replace"] and r["category"].lower() in NEVER_REPLACED]
    dup_only = [r for r in rows if r["duplicate"] and not r["replace"] and r["cost"] > 0]
    base = sum(r["cost"] for r in replaceable) * 12
    if base == 0:
        return {"key": "technology", "label": "Technology", "status": "none", "low": 0, "likely": 0, "high": 0, "confidence": "high",
                "current_state": "Software stack about $%s/mo (%d tools). Nothing marked replaceable." % ("{:,.0f}".format(total_month), len(rows)),
                "opportunity": "No consolidation savings counted" + (" — %d overlapping tool(s) flagged but kept" % len(dup_only) if dup_only else ""),
                "calc": None, "missing": [], "module": module, "spend_month": total_month, "spend_annual": total_month * 12}
    rng = _rng(base, 0.5, 0.75, 1.0)
    calc = {"current_metric": "software $%s/mo; %s marked replaceable" % ("{:,.0f}".format(total_month), ", ".join(r["name"] or r["category"] for r in replaceable)),
            "benchmark": "only tools the owner marked replaceable, 50–100% of their cost",
            "benchmark_source": "Owner's own answers. No external benchmark used.",
            "base": "$%s/yr across replaceable tools" % "{:,.0f}".format(base), "formula": "replaceable tool cost × 50% / 75% / 100%",
            "assumptions": ["POS, payroll and accounting are never counted — Cavnar AI does not replace them." + (" (%s marked replaceable and excluded.)" % ", ".join(r["category"] for r in excluded) if excluded else ""),
                            "Marketing agency fees are counted in Marketing, not here."],
            "method": "consolidation", "overlap": "Agency fees excluded (Marketing). Only owner-marked tools count."}
    out = {"key": "technology", "label": "Technology", "status": "ok", "confidence": "high",
           "current_state": "Software stack about $%s/mo across %d tools; %d marked replaceable." % ("{:,.0f}".format(total_month), len(rows), len(replaceable)),
           "opportunity": "Consolidate replaceable tools into one platform", "calc": calc, "missing": [], "module": module,
           "spend_month": total_month, "spend_annual": total_month * 12}
    out.update(rng)
    return out


# ── Scores ───────────────────────────────────────────────────────────────────

def _score_label(s):
    if s is None:
        return "Not assessed"
    if s >= 90:
        return "Excellent"
    if s >= 75:
        return "Strong"
    if s >= 60:
        return "Opportunity"
    if s >= 40:
        return "Significant Opportunity"
    return "Critical Opportunity"


def _score(rules):
    """rules: list of (answered: bool, deduction: int, reason). Missing data
    never deducts. Needs at least two answered signals to be assessed."""
    answered = [r for r in rules if r[0]]
    if len(answered) < 2:
        return None, []
    s = 100
    reasons = []
    for _, d, why in answered:
        if d:
            s -= d
            reasons.append("−%d %s" % (d, why))
    return max(0, min(100, s)), reasons


def _gap_ded(gap_pts):
    if gap_pts is None:
        return 0
    if gap_pts <= 0:
        return 0
    if gap_pts <= 2:
        return 12
    if gap_pts <= 4:
        return 25
    if gap_pts <= 7:
        return 38
    return 50


def _var_ded(v):
    if v is None:
        return 0
    return 0 if v <= 3 else (8 if v <= 5 else (16 if v <= 10 else 24))


def _rating_ded(r):
    if r is None:
        return 0
    return 0 if r >= 4.6 else (8 if r >= 4.3 else (18 if r >= 4.0 else 30))


def _rr_ded(rr):
    if rr is None:
        return 0
    return 0 if rr >= 80 else (8 if rr >= 50 else 16)


def _dash_ded(d):
    if d is None:
        return 0
    return 0 if d <= 2 else (6 if d <= 4 else 12)


def score_labor(a, fin, cat):
    gap = (cat.get("calc") or {}).get("gap_pts") if cat.get("status") in ("ok", "none") else None
    ot = _txt(a, "lab_ot_surprise")
    how = _txt(a, "lab_schedule_how")
    fast = _txt(a, "lab_how_fast")
    rules = [
        (gap is not None, _gap_ded(gap), "labor above target"),
        (_yes(a, "lab_know_daily") is not None, 10 if _yes(a, "lab_know_daily") is False else 0, "labor % not known daily"),
        (how is not None, 10 if how in ("Manager intuition", "Copy last week") else 0, "schedules not built from forecast"),
        (ot is not None, {"Never": 0, "Occasionally": 4, "Most pay periods": 10, "Every pay period": 14}.get(ot, 0), "overtime surprises at payroll"),
        (fast is not None, {"Same shift": 0, "Next day": 2, "End of week": 6, "At payroll": 10, "End of month or later": 14}.get(fast, 0), "slow to see labor running high"),
        (_txt(a, "lab_manager_adjust") is not None, 6 if _txt(a, "lab_manager_adjust") in ("Rarely", "Never") else 0, "managers don't adjust in-shift"),
        (_yes(a, "lab_timeclock") is not None, 6 if _yes(a, "lab_timeclock") else 0, "time-clock leakage"),
        (_txt(a, "lab_overstaffed") is not None, {"Rarely": 0, "Some shifts": 4, "Weekly": 8, "Most days": 12}.get(_txt(a, "lab_overstaffed"), 0), "overstaffing frequency"),
    ]
    return _score(rules)


def score_food(a, fin, cat):
    gap = (cat.get("calc") or {}).get("gap_pts") if cat.get("status") in ("ok", "none") else None
    inv = _txt(a, "food_inventory_freq")
    rec = _txt(a, "food_recipes_costed")
    vend = _txt(a, "food_vendor_notice")
    rules = [
        (gap is not None, _gap_ded(gap), "food cost above target"),
        (inv is not None, {"Weekly": 0, "Every two weeks": 4, "Monthly": 8, "Quarterly": 14, "Rarely / never": 18}.get(inv, 0), "infrequent inventory"),
        (_yes(a, "food_theoretical") is not None, 12 if _yes(a, "food_theoretical") is False else 0, "no theoretical vs. actual"),
        (rec is not None, {"All, kept current": 0, "Most": 4, "Some": 10, "None": 14}.get(rec, 0), "recipes not costed"),
        (vend is not None, {"Same invoice": 0, "Within the week": 2, "Month-end": 6, "When margins drop": 10, "Rarely notice": 12}.get(vend, 0), "slow to catch vendor price increases"),
        (_yes(a, "food_item_margins") is not None, 8 if _yes(a, "food_item_margins") is False else 0, "item margins unknown"),
        (_yes(a, "food_waste_tracked") is not None, 6 if _yes(a, "food_waste_tracked") is False else 0, "waste not logged"),
        (_yes(a, "food_alerts") is not None, 6 if _yes(a, "food_alerts") is False else 0, "no food-cost alerts before month-end"),
    ]
    return _score(rules)


def score_bar(a, fin, cat):
    gap = (cat.get("calc") or {}).get("gap_pts") if cat.get("status") in ("ok", "none") else None
    var = _num(a, "bar_variance_pct")
    inv = _txt(a, "bar_inventory_freq")
    pour = _txt(a, "bar_pour_method")
    comp = _txt(a, "bar_comp_policy")
    rules = [
        (gap is not None, _gap_ded(gap), "pour cost above target"),
        (var is not None, _var_ded(var), "inventory variance"),
        (inv is not None, {"Weekly": 0, "Every two weeks": 4, "Monthly": 8, "Quarterly": 14, "Rarely / never": 18}.get(inv, 0), "infrequent bar inventory"),
        (_yes(a, "bar_theoretical") is not None, 12 if _yes(a, "bar_theoretical") is False else 0, "no theoretical vs. actual beverage cost"),
        (pour is not None, {"Free pour": 10, "Mix": 5, "Jiggers required": 0, "Measured pourers": 0}.get(pour, 0), "pour control"),
        (comp is not None, {"Tracked and limited": 0, "Tracked": 3, "Untracked": 10, "No policy": 12}.get(comp, 0), "comp policy"),
        (_yes(a, "bar_drink_margins") is not None, 6 if _yes(a, "bar_drink_margins") is False else 0, "drink margins unknown"),
        (_txt(a, "bar_draft_waste") is not None, {"Tracked and low": 0, "Tracked, a problem": 6, "Not tracked": 8}.get(_txt(a, "bar_draft_waste"), 0), "draft waste"),
    ]
    return _score(rules)


def score_reviews(a, fin, cat):
    rating = _num(a, "rev_google_rating")
    rr = _num(a, "rev_response_rate")
    rt = _txt(a, "rev_response_time")
    rules = [
        (rating is not None, _rating_ded(rating), "rating"),
        (rr is not None, _rr_ded(rr), "response rate"),
        (rt is not None, {"Same day": 0, "1–2 days": 2, "Within a week": 8, "Longer": 12, "Don't respond": 16}.get(rt, 0), "response time"),
        (_yes(a, "rev_categorize") is not None, 8 if _yes(a, "rev_categorize") is False else 0, "complaints not categorized"),
        (_yes(a, "rev_track_sentiment") is not None, 8 if _yes(a, "rev_track_sentiment") is False else 0, "sentiment not tracked"),
        (_txt(a, "rev_personalized") is not None, 8 if _txt(a, "rev_personalized") == "Templated" else 0, "templated responses"),
        (_yes(a, "rev_managers_see") is not None, 5 if _yes(a, "rev_managers_see") is False else 0, "managers don't see trends"),
    ]
    return _score(rules)


def score_marketing(a, fin, cat):
    freq = _txt(a, "mkt_campaign_freq")
    rules = [
        (_yes(a, "mkt_roi_tracked") is not None, 14 if _yes(a, "mkt_roi_tracked") is False else 0, "ROI not tracked"),
        (_yes(a, "mkt_know_channels") is not None, 10 if _yes(a, "mkt_know_channels") is False else 0, "channels' revenue unknown"),
        (_txt(a, "mkt_decide") is not None, 8 if _txt(a, "mkt_decide") in ("Gut instinct", "Whatever worked before") else 0, "promotions by gut"),
        (_yes(a, "mkt_lapsed") is not None, 8 if _yes(a, "mkt_lapsed") is False else 0, "lapsed guests unknown"),
        (_yes(a, "mkt_retention_auto") is not None, 8 if _yes(a, "mkt_retention_auto") is False else 0, "no automated retention"),
        (_yes(a, "mkt_slow_days") is not None, 6 if _yes(a, "mkt_slow_days") is False else 0, "no slow-day marketing"),
        (_yes(a, "mkt_profit_after_discount") is not None, 6 if _yes(a, "mkt_profit_after_discount") is False else 0, "discount profit unmeasured"),
        (freq is not None, {"Weekly+": 0, "Monthly": 2, "Occasional": 6, "Rarely": 8}.get(freq, 0), "campaign cadence"),
    ]
    return _score(rules)


def score_waitlist(a, fin, cat):
    sysm = _txt(a, "wl_waitlist_system")
    rules = [
        (sysm is not None, {"Paper / memory": 12, "Host app": 4, "Texting waitlist": 0, "None needed": 0}.get(sysm, 0), "waitlist system"),
        (_yes(a, "wl_track_walkaways") is not None, 10 if _yes(a, "wl_track_walkaways") is False else 0, "walkaways untracked"),
        (_rating(a, "wl_quote_accuracy") is not None, {1: 14, 2: 10, 3: 6, 4: 2, 5: 0}.get(_rating(a, "wl_quote_accuracy"), 0), "quote accuracy"),
        (_yes(a, "wl_know_turn") is not None, 8 if _yes(a, "wl_know_turn") is False else 0, "turn time unknown"),
        (_yes(a, "wl_bottlenecks") is not None, 8 if _yes(a, "wl_bottlenecks") is False else 0, "bottlenecks unknown"),
        (_txt(a, "wl_complaints") is not None, {"Rare": 0, "Occasional": 5, "Frequent": 12}.get(_txt(a, "wl_complaints"), 0), "wait complaints"),
        (_yes(a, "wl_texting") is not None, 6 if _yes(a, "wl_texting") is False else 0, "no waitlist texting"),
    ]
    return _score(rules)


def score_operations(a, fin, cat):
    lag = _txt(a, "ops_report_lag")
    mon = _txt(a, "ops_comps_monitored")
    ss = _txt(a, "ops_spreadsheets")
    rules = [
        (lag is not None, {"Same day": 0, "Next day": 2, "A few days": 6, "A week or more": 12, "Month-end": 16}.get(lag, 0), "report lag"),
        (mon is not None, {"Daily": 0, "Weekly": 4, "Occasionally": 10, "No": 14}.get(mon, 0), "comps/voids unmonitored"),
        (_yes(a, "ops_daily_reports") is not None, 8 if _yes(a, "ops_daily_reports") is False else 0, "no daily manager reports"),
        (_yes(a, "ops_forecasting") is not None, 8 if _yes(a, "ops_forecasting") is False else 0, "no sales forecasting"),
        (_rating(a, "ops_accountability") is not None, {1: 14, 2: 10, 3: 6, 4: 2, 5: 0}.get(_rating(a, "ops_accountability"), 0), "manager accountability"),
        (_rating(a, "ops_visibility") is not None, {1: 14, 2: 10, 3: 6, 4: 2, 5: 0}.get(_rating(a, "ops_visibility"), 0), "owner visibility"),
        (ss is not None, {"Nothing": 0, "A little": 2, "A lot": 8, "Everything": 12}.get(ss, 0), "spreadsheet dependence"),
        (_rating(a, "ops_cash_controls") is not None, {1: 10, 2: 7, 3: 4, 4: 1, 5: 0}.get(_rating(a, "ops_cash_controls"), 0), "cash controls"),
    ]
    return _score(rules)


def score_technology(a, fin, cat):
    tools = (a or {}).get("tech_tools") or []
    dups = sum(1 for r in tools if isinstance(r, dict) and r.get("duplicate") in ("yes", True))
    lows = sum(1 for r in tools if isinstance(r, dict) and str(r.get("useful") or "") in ("1", "2"))
    dash = _num(a, "tech_dashboards")
    rules = [
        (bool(tools), min(20, dups * 7), "overlapping tools"),
        (bool(tools), min(15, lows * 5), "tools rated barely useful"),
        (_yes(a, "tech_overlap") is not None, 8 if _yes(a, "tech_overlap") else 0, "paying for overlap"),
        (dash is not None, _dash_ded(dash), "dashboards to check"),
        (_txt(a, "tech_no_talk") is not None, 6 if _txt(a, "tech_no_talk") else 0, "systems that don't communicate"),
        (_txt(a, "tech_manual_export") is not None, 6 if _txt(a, "tech_manual_export") else 0, "manual exporting"),
    ]
    return _score(rules)


SCORERS = {"labor": score_labor, "food": score_food, "bar": score_bar, "reviews": score_reviews,
           "marketing": score_marketing, "waitlist": score_waitlist, "operations": score_operations,
           "technology": score_technology}
CALCS = {"labor": calc_labor, "food": calc_food, "bar": calc_bar, "reviews": calc_reviews,
         "marketing": calc_marketing, "waitlist": calc_waitlist, "operations": calc_operations,
         "technology": calc_technology}


def _weights(cls):
    w = {"labor": 20, "food": 18, "bar": 8, "reviews": 12, "marketing": 10, "waitlist": 6, "operations": 12, "technology": 6}
    if cls in ("sports_bar", "bar"):
        w["bar"] = 18
        w["waitlist"] = 8
    return w


# ── Findings, problems, opportunities, wins ─────────────────────────────────

def _money(x):
    return "$" + "{:,.0f}".format(x)


def _range_text(c):
    if c.get("status") != "ok" or not c.get("high"):
        return None
    return "%s – %s / year" % (_money(c["low"]), _money(c["high"]))


def build_wins(a, fin, cats, scores):
    wins = []
    for k, label in CATEGORY_LABELS:
        s = scores.get(k, {}).get("score")
        if s is not None and s >= 75:
            wins.append("%s scores %d — %s." % (label, s, _score_label(s).lower()))
        elif cats[k].get("status") == "none":
            wins.append("%s: %s" % (label, cats[k]["current_state"]))
    checks = [
        ("food_inventory_freq", "Weekly", "Weekly food inventory — real discipline most independents don't have."),
        ("bar_inventory_freq", "Weekly", "Weekly bar inventory."),
        ("food_recipes_costed", "All, kept current", "Every recipe is costed and current."),
        ("rev_response_time", "Same day", "Reviews answered the same day."),
        ("lab_how_fast", "Same shift", "Labor running high is caught during the shift."),
        ("ops_report_lag", "Same day", "Numbers are seen the same day."),
        ("bar_pour_method", "Jiggers required", "Measured pours behind the bar."),
        ("lab_schedule_how", "Based on forecasted sales", "Schedules are built from forecasted sales."),
    ]
    for qid, want, text in checks:
        if _txt(a, qid) == want:
            wins.append(text)
    for qid, text in [("mkt_roi_tracked", "Marketing ROI is tracked."), ("food_theoretical", "Theoretical vs. actual food cost is known."),
                      ("bar_theoretical", "Theoretical vs. actual beverage cost is measured."), ("rev_categorize", "Recurring complaints are categorized."),
                      ("lab_labor_forecast", "Labor need is forecast from sales.")]:
        if _yes(a, qid):
            wins.append(text)
    seen, out = set(), []
    for w in wins:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


def build_findings(cats, scores):
    out = []
    for k, label in CATEGORY_LABELS:
        c = cats[k]
        s = scores.get(k, {}).get("score")
        if c.get("status") == "ok":
            body = c["current_state"] + " " + c["opportunity"] + "."
            out.append({"key": k, "title": "%s Opportunity" % label, "body": body, "range": _range_text(c),
                        "monthly": "%s – %s / month" % (_money(c["low"] / 12.0), _money(c["high"] / 12.0)),
                        "confidence": c.get("confidence"), "score": s, "upcoming": not c["module"]["live"]})
        elif c.get("status") == "none":
            out.append({"key": k, "title": "%s — Performing Well" % label, "body": c["current_state"], "range": None, "monthly": None,
                        "confidence": c.get("confidence"), "score": s, "upcoming": not c["module"]["live"]})
        elif s is not None and s < 60:
            out.append({"key": k, "title": "%s — Visibility Gap" % label,
                        "body": "Not enough numbers to size a dollar opportunity, but the practices point to a gap: " + "; ".join(r[3:] for r in scores[k]["reasons"][:3]) + ".",
                        "range": None, "monthly": None, "confidence": None, "score": s, "upcoming": not c["module"]["live"]})
    return out


def build_problems(a, cats, scores):
    cands = []
    for k, label in CATEGORY_LABELS:
        c = cats[k]
        s = scores.get(k, {}).get("score")
        impact = c.get("likely", 0) if c.get("status") == "ok" else 0
        sev = (100 - s) if s is not None else 0
        # Low-confidence dollars (reviews, walkaways) rank below solid ones.
        rank = impact * {"high": 1.0, "moderate": 0.85, "low": 0.55}.get(c.get("confidence"), 0.5) + sev * 150
        if impact == 0 and (s is None or s >= 60):
            continue
        evidence = []
        if c.get("calc"):
            evidence.append(c["calc"].get("current_metric", ""))
        evidence += [r[3:] for r in scores.get(k, {}).get("reasons", [])[:3]]
        why = {"labor": "Labor is the largest controllable cost line; every point over target is paid every week.",
               "food": "Food cost drift compounds silently — a two-point slide is rarely noticed until month-end.",
               "bar": "Beverage carries the best margins in the building, which makes every ounce of variance expensive.",
               "reviews": "For an independent, rating and sentiment move covers directly.",
               "marketing": "Untracked spend can't be optimized, and blanket promotions discount guests who were coming anyway.",
               "waitlist": "A walkaway is a cover that never hits the P&L — invisible unless counted.",
               "operations": "What isn't seen daily gets managed monthly, and by then the money is gone.",
               "technology": "Disconnected tools mean the owner is the integration layer."}[k]
        cands.append({"key": k, "title": label, "current_state": c.get("current_state"), "why": why,
                      "impact": _range_text(c) or ("Score %d — %s" % (s, _score_label(s).lower()) if s is not None else "Unsized"),
                      "evidence": [e for e in evidence if e][:4], "rank": rank})
    cands.sort(key=lambda x: -x["rank"])
    return cands[:3]


def build_opportunities(cats, scores):
    conf_w = {"high": 1.0, "moderate": 0.8, "low": 0.55, None: 0.4}
    cands = []
    for k, label in CATEGORY_LABELS:
        c = cats[k]
        if c.get("status") != "ok" or not c.get("high"):
            continue
        feas = 1.0 if c["module"]["live"] else 0.6
        s = scores.get(k, {}).get("score")
        importance = 1.0 + ((100 - s) / 200.0 if s is not None else 0)
        rank = c["likely"] * conf_w.get(c.get("confidence")) * feas * importance
        cands.append({"key": k, "title": label, "opportunity": c["opportunity"], "range": _range_text(c),
                      "confidence": c.get("confidence"), "feasibility": "Available today" if c["module"]["live"] else "Upcoming module",
                      "module": c["module"]["label"], "rank": rank})
    cands.sort(key=lambda x: -x["rank"])
    return cands[:3]


# ── Pricing / ROI ────────────────────────────────────────────────────────────

def recommend_modules(cats, scores):
    ranked = []
    for k in ("labor", "food", "reviews", "marketing"):
        c = cats[k]
        s = scores.get(k, {}).get("score")
        val = c.get("likely", 0) if c.get("status") == "ok" else 0
        need = (c.get("status") == "ok" and val > 0) or (s is not None and s < 75)
        if need:
            ranked.append((val + ((100 - s) * 100 if s is not None else 0), k))
    ranked.sort(reverse=True)
    mods = [k for _, k in ranked]
    upcoming = [k for k in ("bar", "waitlist") if cats[k].get("status") == "ok" and cats[k].get("likely", 0) > 0]
    return mods, upcoming


def price_plan(n_modules, override=None):
    """override: None/'auto', 'full', 'starter' (single module) or 'starter:N'."""
    ov = override or "auto"
    if ov == "full" or (ov == "auto" and n_modules >= 3):
        p = PRICING["full"]
        return {"plan": "full", "label": p["label"], "modules": 4, "setup": p["setup"], "annual": p["annual"],
                "first_year": p["setup"] + p["annual"], "monthly_equiv": p["monthly_equiv"], "note": p["note"], "verify": None}
    n = 1
    if ov.startswith("starter:"):
        try:
            n = max(1, min(2, int(ov.split(":")[1])))
        except ValueError:
            n = 1
    elif ov == "auto":
        n = max(1, n_modules)
    # Two modules used to be quoted as 2 × Starter with a "confirm before
    # quoting" caveat, because the price wasn't published. It is published —
    # $649/mo, $6,490/yr — and it is a volume price, not a doubling, so the
    # multiply overcharged by $490 a year. Read the real tier.
    from pricing import plan_for as _plan_for
    tier = _plan_for(n)
    p = PRICING["starter"]
    note = ("%d modules. $%s one-time setup, $%s/yr billed annually ($%s/mo equivalent)."
            % (n, "{:,}".format(tier["setup"]), "{:,}".format(tier["annual"]), "{:,}".format(tier["monthly"]))
            ) if n > 1 else p["note"]
    return {"plan": "starter", "label": p["label"] + (" × %d" % n if n > 1 else ""), "modules": n,
            "setup": tier["setup"], "annual": tier["annual"],
            "first_year": tier["setup"] + tier["annual"], "monthly_equiv": tier["monthly"],
            "note": note, "verify": None}


# ── Entry point ──────────────────────────────────────────────────────────────

_CONF_ORDER = ["low", "moderate", "high"]


def apply_notes(cats, notes_read):
    """Fold what the notes reader concluded into the category results
    before scoring and ranking, so confidence-weighted problems, totals
    confidence and the report all see the same thing.

    Insights only ever move a sized category's confidence one step or add
    context. They never change a dollar figure — a note that carries a
    number becomes a suggested answer instead, and the engine sizes it
    from the typed answer like any other. A read whose fingerprint no
    longer matches the notes is shown but not applied."""
    if not notes_read:
        return None
    insights = [i for i in (notes_read.get("insights") or []) if isinstance(i, dict) and i.get("text")]
    out = {"read_at": notes_read.get("read_at"), "fingerprint": notes_read.get("fingerprint"), "model": notes_read.get("model"),
           "stale": bool(notes_read.get("stale")), "notes_seen": notes_read.get("notes_seen", 0),
           "insights": insights, "suggestions": list(notes_read.get("suggestions") or []),
           "caveats": list(notes_read.get("caveats") or []), "applied": 0}
    if out["stale"]:
        return out
    # Net direction per category, clamped to one step: three notes that all
    # say "less certain" move labor from high to moderate, not to low.
    net = {}
    for ins in insights:
        k = ins.get("category") or ""
        if ins.get("effect") == "raise_confidence":
            net[k] = net.get(k, 0) + 1
        elif ins.get("effect") == "lower_confidence":
            net[k] = net.get(k, 0) - 1
    for k, d in net.items():
        c = cats.get(k)
        if c and c.get("status") == "ok" and d:
            cur = c.get("confidence") if c.get("confidence") in _CONF_ORDER else "moderate"
            i = _CONF_ORDER.index(cur)
            c["confidence"] = _CONF_ORDER[min(2, i + 1)] if d > 0 else _CONF_ORDER[max(0, i - 1)]
    for ins in insights:
        c = cats.get(ins.get("category") or "")
        if not c:
            continue
        # Only owner-safe, audit-note insights may reach the report's
        # "how this was estimated" block. Internal notes stay internal.
        if c.get("calc") is not None and ins.get("report_safe") and ins.get("source") == "audit":
            c["calc"]["assumptions"] = list(c["calc"].get("assumptions") or []) + ["From the conversation: " + ins["text"]]
        c["notes"] = list(c.get("notes") or []) + [ins]
        out["applied"] += 1
    return out


def compute(answers, pricing_override=None, notes_read=None):
    a = answers or {}
    owner = _txt(a, "owner_name")
    owner = owner.split(" ")[0] if owner else "the owner"
    cls = concept_class(a)
    fin = derive_financials(a)
    cats = {k: CALCS[k](a, fin, cls, owner) for k, _ in CATEGORY_LABELS}
    months_open = (fin.get("months_open") or {}).get("value")
    if months_open:
        # Opening-period numbers are not a run rate: labor runs high while
        # the team trains, sales ramp, and vendors are still being settled.
        # Everything sized becomes a watch item at reduced confidence.
        for c in cats.values():
            if c.get("status") == "ok":
                c["confidence"] = "low" if months_open < 6 else ("moderate" if c.get("confidence") == "high" else c.get("confidence"))
                if c.get("calc"):
                    c["calc"]["assumptions"] = list(c["calc"].get("assumptions") or []) + [
                        "%s opened %d months ago. Opening-period labor runs high and sales are still ramping, so this gap is a baseline to watch, not a leak to recover yet." % ((_txt(a, "restaurant_name") or "The restaurant"), months_open)]
    notes_applied = apply_notes(cats, notes_read)
    scores = {}
    for k, label in CATEGORY_LABELS:
        s, reasons = SCORERS[k](a, fin, cats[k])
        scores[k] = {"score": s, "label": _score_label(s), "reasons": reasons}

    w = _weights(cls)
    assessed = [(k, scores[k]["score"]) for k, _ in CATEGORY_LABELS if scores[k]["score"] is not None]
    if assessed:
        tw = sum(w[k] for k, _ in assessed)
        health = int(round(sum(w[k] * s for k, s in assessed) / tw))
    else:
        health = None
    ranked = sorted(assessed, key=lambda x: x[1])
    strongest = [dict(CATEGORY_LABELS)[k] for k, s in ranked[::-1][:3] if s >= 75]
    weakest = [dict(CATEGORY_LABELS)[k] for k, s in ranked[:3] if s < 75]
    immediate = [dict(CATEGORY_LABELS)[k] for k, s in ranked if s < 40]
    health_interp = None
    if health is not None:
        if health >= 90:
            health_interp = "A tightly run operation. The audit's job is to protect what's working and pick off the few remaining leaks."
        elif health >= 75:
            health_interp = "A strong operation with specific, addressable gaps rather than systemic ones."
        elif health >= 60:
            health_interp = "Solid fundamentals with real money on the table in a few areas — mostly visibility, not effort."
        elif health >= 40:
            health_interp = "Several areas run on intuition where numbers should be. The opportunities are significant and mostly fixable with daily visibility."
        else:
            health_interp = "Controls are thin across most of the operation. Prioritise the two or three areas with the biggest dollar impact first."

    R = fin.get("annual_revenue", {}).get("value")
    total = {k: sum(c[k] for c in cats.values() if c.get("status") == "ok") for k in ("low", "likely", "high")}
    cost = {k: sum(cats[c][k] for c in COST_CATEGORIES if cats[c].get("status") == "ok") for k in ("low", "likely", "high")}
    revenue = {k: sum(cats[c][k] for c in REVENUE_CATEGORIES if cats[c].get("status") == "ok") for k in ("low", "likely", "high")}
    monthly = {k: int(round(v / 12.0, -1)) for k, v in total.items()}
    counted = [c for c in cats.values() if c.get("status") == "ok" and c.get("high")]
    conf_pts = {"high": 3, "moderate": 2, "low": 1}
    if counted:
        weighted = sum(conf_pts.get(c.get("confidence"), 1) * c["likely"] for c in counted) / max(1, sum(c["likely"] for c in counted))
        overall_conf = "HIGH" if weighted >= 2.6 else ("MODERATE" if weighted >= 1.7 else "LOW")
    else:
        overall_conf = None

    done, asked, per_section = completion(a)
    missing = []
    for k, _ in CATEGORY_LABELS:
        for m in cats[k].get("missing") or []:
            if m not in missing:
                missing.append(m)

    mods, upcoming = recommend_modules(cats, scores)
    plan = price_plan(len(mods), pricing_override)
    roi = None
    if total["high"] and plan["annual"]:
        roi = {"low_x": round(total["low"] / plan["annual"], 1), "high_x": round(total["high"] / plan["annual"], 1),
               "first_year_low_x": round(total["low"] / plan["first_year"], 1), "first_year_high_x": round(total["high"] / plan["first_year"], 1),
               "cost_low_x": round(cost["low"] / plan["annual"], 1), "cost_high_x": round(cost["high"] / plan["annual"], 1)}
    R = fin.get("annual_revenue", {}).get("value")
    hours = cats["operations"].get("hours_week")
    # Well-run: strong scores and cost savings that are small relative to
    # sales (under 1% of revenue, or under the subscription when revenue is
    # unknown). The report then makes the time-and-early-warning case
    # instead of pretending there is money to recover.
    well_run = bool(health is not None and health >= 75 and cost["likely"] < max(plan["annual"], (R or 0) * 0.01))
    context = {
        "new_restaurant": bool(months_open), "months_open": months_open,
        "well_run": well_run,
        "note": ("Opened %d month%s ago. Annual figures are annualized from the opening period and every gap is treated as a baseline to watch rather than a leak to recover; the audit should be re-run once six months of normal trading exist." % (months_open, "" if months_open == 1 else "s")) if months_open
                else ("A well-run operation: the identified cost savings (%s – %s) are small relative to sales. The case here is time, early warning and consolidation, not recovery." % (_money(cost["low"]), _money(cost["high"])) if well_run else None),
    }
    return {
        "context": context,
        "notes_read": notes_applied,
        "concept_class": cls,
        "financials": fin,
        "categories": cats,
        "scores": scores,
        "health": {"score": health, "label": _score_label(health), "interpretation": health_interp, "strongest": strongest,
                   "weakest": weakest, "immediate": immediate, "assessed": len(assessed), "preliminary": len(assessed) < 3},
        "totals": {"annual": total, "monthly": monthly, "cost": cost, "revenue": revenue, "confidence": overall_conf, "counted": len(counted),
                   "pct_of_revenue": {"low": round(100.0 * total["low"] / R, 1), "high": round(100.0 * total["high"] / R, 1)} if R else None},
        "findings": build_findings(cats, scores),
        "wins": build_wins(a, fin, cats, scores),
        "problems": build_problems(a, cats, scores),
        "opportunities": build_opportunities(cats, scores),
        "missing": missing,
        "recommended_modules": [MODULES[k] for k in mods],
        "upcoming_modules": [MODULES[k] for k in upcoming],
        "plan": plan,
        "roi": roi,
        "owner_hours_week": hours,
        "completion": {"done": done, "total": asked, "pct": int(round(100.0 * done / asked)) if asked else 0, "sections": per_section},
        "benchmarks_used": {"labor": _bench("labor", cls), "food": _bench("food", cls), "bar": BENCHMARKS["bar"]["blended"],
                            "prime": BENCHMARKS["prime"], "reviews": BENCHMARKS["reviews"]},
        "disclaimer": "Estimated opportunities are based on information supplied during the audit, available operating benchmarks, and stated assumptions. Actual results will vary. Cavnar AI is designed to help identify, monitor and act on opportunities like these; it does not guarantee that any portion of an identified opportunity will be captured.",
    }
