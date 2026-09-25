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
                       "comparability": FORMAT, "module": "reviews", "source": "reviews", "step": 0.1},
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


def platform_allowed(metric) -> bool:
    """Whether the all-restaurants band on Cavnar is a fair comparison for
    `metric` — behaviour metrics only (BM2-3)."""
    return comparability(metric) == BEHAVIOUR


def metrics_for(module=None) -> list:
    """The metrics of one module (labor, inventory/food/food_cost, reviews,
    marketing), or every registered metric."""
    if module is None:
        return list(METRICS)
    return list(MODULE_METRICS.get(str(module).lower(), []))
