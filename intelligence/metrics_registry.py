"""Level 3: the Benchmark Engine's metric registry — one row per metric a
restaurant can be compared on, and the rules that decide WHETHER a
comparison is fair before any number is shown (Benchmarking audit 9/24/26:
BM1-4, BM2-3, BM3-7, BM4-13).

`comparability` is the heart of "know when not to benchmark":

  behaviour  how a restaurant RUNS (reply rate, 24-hour response,
             recommendations that improved): comparable across every type,
             so the all-restaurants band on Cavnar is a fair peer set.
  format     shaped by the service model (rating, campaign and post
             engagement): a peer band needs at least the same type.
  economics  what a restaurant IS decides the number (labor %, hours and
             staff per $1k of sales, food cost %, waste %): a coffee shop,
             a sports bar and a steakhouse differ by 3-5x for identical
             efficiency, so these are NEVER compared against an all-types
             band — no like-for-like peers means no peer comparison.

Everything else a comparison needs rides here too: the label, the unit,
which direction is better, the published rounding step, the module whose
view permission gates it, the freshness source it rests on, the published
industry entry it maps to (benchmark_registry) and the metric the
restaurant's own noise band is read from.
"""

BEHAVIOUR = "behaviour"
FORMAT = "format"
ECONOMICS = "economics"

# key: label, unit, better, comparability, module (permissions.MODULE_VIEW_PERMISSIONS
# key), source (data_freshness key), industry (benchmark_registry metric), step.
METRICS = {
    "avg_rating_30d": {"label": "Average rating (30 days)", "unit": "★", "better": "higher",
                       "comparability": FORMAT, "module": "reviews", "source": "reviews", "step": 0.25},
    "response_24h_rate_30d": {"label": "Reviews answered within a day", "unit": "share", "better": "higher",
                              "comparability": BEHAVIOUR, "module": "reviews", "source": "reviews", "step": 0.05},
    "reply_rate_30d": {"label": "Reviews answered", "unit": "share", "better": "higher",
                       "comparability": BEHAVIOUR, "module": "reviews", "source": "reviews", "step": 0.05},
    "labor_pct_28d": {"label": "Labor %", "unit": "%", "better": "lower", "comparability": ECONOMICS,
                      "module": "labor", "source": "labor", "industry": "labor_pct", "step": 0.5},
    "labor_pct_sd_28d": {"label": "Day-to-day labor swing", "unit": "pts", "better": "lower",
                         "comparability": ECONOMICS, "module": "labor", "source": "labor", "step": 0.5},
    "labor_hours_per_1k_28d": {"label": "Labor hours per $1k of sales", "unit": "h", "better": "lower",
                               "comparability": ECONOMICS, "module": "labor", "source": "labor", "step": 0.1},
    "labor_hours_per_1k_day_28d": {"label": "Labor hours per $1k, lunch/day", "unit": "h", "better": "lower",
                                   "comparability": ECONOMICS, "module": "labor", "source": "labor", "step": 0.1},
    "labor_hours_per_1k_night_28d": {"label": "Labor hours per $1k, dinner/night", "unit": "h", "better": "lower",
                                     "comparability": ECONOMICS, "module": "labor", "source": "labor", "step": 0.1},
    "food_cost_pct_28d": {"label": "Food cost %", "unit": "%", "better": "lower", "comparability": ECONOMICS,
                          "module": "inventory", "source": "inventory", "industry": "food_cost_pct", "step": 0.5},
    "waste_sales_pct_28d": {"label": "Waste as % of sales", "unit": "%", "better": "lower",
                            "comparability": ECONOMICS, "module": "inventory", "source": "waste", "step": 0.5},
    "campaign_tap_rate_28d": {"label": "Text campaign tap rate", "unit": "share", "better": "higher",
                              "comparability": FORMAT, "module": "marketing", "source": "campaigns", "step": 0.01},
    "post_lift_median_28d": {"label": "Sales lift after a post (median)", "unit": "%", "better": "higher",
                             "comparability": FORMAT, "module": "marketing", "source": "marketing", "step": 0.5},
    "post_engagement_rate_28d": {"label": "Post engagement rate", "unit": "share", "better": "higher",
                                 "comparability": FORMAT, "module": "marketing", "source": "marketing",
                                 "step": 0.01},
    "outcomes_improved_rate_90d": {"label": "Recommendations that measurably improved", "unit": "share",
                                   "better": "higher", "comparability": BEHAVIOUR, "module": None,
                                   "source": None, "step": 0.05},
}

# What each figure MEASURES, so a published figure that measures something
# else is refused rather than compared (Benchmarking audit #14, BM3-15).
# Cavnar's labor % is shift wages over net sales — the NRA median includes
# benefits; Cavnar's food cost is inventory COGS over total sales — the NRA
# figure is food and non-alcohol beverage cost.
DEFINITIONS = {"labor_pct_28d": "wages_from_shifts", "food_cost_pct_28d": "cogs_pct_sales"}

# The hard split each metric's peer partition needs (#20, BM2 §4):
# format → service model; labor → × bar-led; food → × bar-led × menu family.
_PARTITION = {"labor_pct_28d": "labor", "labor_pct_sd_28d": "labor", "labor_hours_per_1k_28d": "labor",
              "labor_hours_per_1k_day_28d": "labor", "labor_hours_per_1k_night_28d": "labor",
              "food_cost_pct_28d": "food", "waste_sales_pct_28d": "food"}

# Labor-COST metrics: a member whose labor cost rests on the $26/hr default
# is not in their bands (#14, thresholds.labor_cost_basis).
LABOR_COST_METRICS = ("labor_pct_28d", "labor_pct_sd_28d")

# The quality gate (#39, BM1-14): a band whose interquartile range is more
# than this share of its median says "these restaurants are not alike" —
# withheld, not published. Shares and ratings get an absolute ceiling on the
# IQR instead (a median of 0.1 would make any spread look huge).
MAX_REL_IQR = {"labor_pct_28d": 0.35, "labor_pct_sd_28d": 1.0, "food_cost_pct_28d": 0.35,
               "waste_sales_pct_28d": 1.0, "labor_hours_per_1k_28d": 0.6, "labor_hours_per_1k_day_28d": 0.6,
               "labor_hours_per_1k_night_28d": 0.6, "post_lift_median_28d": 2.0}
MAX_ABS_IQR = {"avg_rating_30d": 0.8, "response_24h_rate_30d": 0.6, "reply_rate_30d": 0.6,
               "campaign_tap_rate_28d": 0.3, "post_engagement_rate_28d": 0.3, "outcomes_improved_rate_90d": 0.6}

# staff_per_1k.<role family> (intelligence.staffing) — economics by nature.
_STAFF_PREFIX = "staff_per_1k."

MODULE_METRICS = {
    "labor": [k for k, v in METRICS.items() if v["module"] == "labor"],
    "inventory": [k for k, v in METRICS.items() if v["module"] == "inventory"],
    "food": [k for k, v in METRICS.items() if v["module"] == "inventory"],
    "food_cost": [k for k, v in METRICS.items() if v["module"] == "inventory"],
    "reviews": [k for k, v in METRICS.items() if v["module"] == "reviews"],
    "marketing": [k for k, v in METRICS.items() if v["module"] == "marketing"],
}


def meta(metric) -> dict:
    """The registry row for `metric` (a staff_per_1k.* ratio gets an
    economics row), or {} for an unknown metric — never raises."""
    m = METRICS.get(metric)
    if m:
        return dict(m, key=metric)
    if str(metric or "").startswith(_STAFF_PREFIX):
        fam = str(metric)[len(_STAFF_PREFIX):].replace("_", " ")
        return {"key": metric, "label": f"People per $1k of sales, {fam}", "unit": "people", "better": "lower",
                "comparability": ECONOMICS, "module": "labor", "source": "labor", "step": 0.05}
    return {}


def comparability(metric) -> str | None:
    return meta(metric).get("comparability")


def definition(metric) -> str | None:
    return DEFINITIONS.get(metric)


def partition_family(metric) -> str:
    """'format' | 'labor' | 'food' | 'staff' — which hard split a peer band
    for `metric` is read from (categories.partition_key)."""
    if str(metric or "").startswith(_STAFF_PREFIX):
        return "staff"
    return _PARTITION.get(metric, "format")


def spread_ok(metric, p25, p50, p75) -> bool:
    """The quality gate on a band's spread (#39): True when the members are
    alike enough for a standing to mean something."""
    try:
        iqr = float(p75) - float(p25)
        med = abs(float(p50))
    except (TypeError, ValueError):
        return False
    if metric in MAX_ABS_IQR:
        return iqr <= MAX_ABS_IQR[metric]
    ceiling = MAX_REL_IQR.get(metric, 1.0)
    if med <= 0:
        return iqr <= 0
    return iqr / med <= ceiling


def platform_allowed(metric) -> bool:
    """Whether the all-restaurants band on Cavnar is a fair comparison for
    `metric` — behaviour metrics only (BM2-3)."""
    return comparability(metric) == BEHAVIOUR


# Module aliases a caller may pass → the permissions.MODULE_VIEW_PERMISSIONS
# key the metrics' `module` field uses.
MODULE_ALIASES = {"food": "inventory", "food_cost": "inventory", "inventory": "inventory"}


def module_key(module) -> str | None:
    """The permission-module key for a module name or alias."""
    if module is None:
        return None
    m = str(module).lower()
    return MODULE_ALIASES.get(m, m)


def metrics_for(module=None) -> list:
    """The metrics of one module (labor, inventory/food/food_cost, reviews,
    marketing), or every registered metric."""
    if module is None:
        return list(METRICS)
    return list(MODULE_METRICS.get(str(module).lower(), []))
