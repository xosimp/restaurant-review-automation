"""Restaurant Intelligence Engine — the one import other modules need.

See INTELLIGENCE_ENGINE.md. Level 1 is a restaurant's own history and
serves only that restaurant; Levels 2 and 3 are aggregates over cohorts of
at least `privacy.MIN_COHORT` restaurants and never carry a name.

    restaurant_memory(rid)            what this restaurant's history says
    platform_intelligence()           active patterns across the platform
    industry_intelligence(cohort)     patterns and benchmarks for a type
    recommendation_history(rid)       this restaurant's recommendation events
    recommendation_success(kind, …)   acceptance and success rates by kind
    pattern_discovery()               run discovery now (jobs do this nightly)
    benchmark(rid, metric)            this restaurant against its cohort
    confidence(rid, kind, metric)     the confidence model for one recommendation
    cohort_for(restaurant)            (category, source) for a restaurant
"""
from models import DB_PATH
from . import privacy, categories, features, memory, feedback, scoring, patterns, benchmarks, trends, confidence, jobs, dashboard, staffing  # noqa: F401


def restaurant_memory(restaurant_id, db_path=DB_PATH):
    return memory.restaurant_memory(restaurant_id, db_path=db_path)


def platform_intelligence(db_path=DB_PATH):
    return {"patterns": patterns.active(db_path=db_path), "trends": trends.emerging(db_path=db_path)}


def industry_intelligence(cohort, db_path=DB_PATH):
    return {"cohort": cohort, "label": categories.label(cohort),
            "patterns": patterns.active(cohort, db_path=db_path, include_platform=False),
            "benchmarks": [benchmarks.band(cohort, m, db_path=db_path) for m in features.BENCHMARK_KEYS]}


def recommendation_history(restaurant_id, limit=100, db_path=DB_PATH):
    return feedback.history(restaurant_id, limit=limit, db_path=db_path)


def recommendation_success(rec_kind, cohort=None, restaurant_id=None, db_path=DB_PATH, exclude_restaurant_id=None):
    """scoring.kind_stats. A cohort figure used as one restaurant's prior
    passes exclude_restaurant_id so its own rows are not in it."""
    return scoring.kind_stats(rec_kind, cohort=cohort, restaurant_id=restaurant_id, db_path=db_path,
                              exclude_restaurant_id=exclude_restaurant_id)


def pattern_discovery(db_path=DB_PATH):
    rs = jobs.active_restaurants(db_path)
    return patterns.discover(db_path=db_path, cohorts=jobs.cohorts_for(rs))


def benchmark(restaurant_id, metric, cohort=None, db_path=DB_PATH):
    if cohort is None:
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db_path=db_path)
        cohort = categories.category_for(r)[0] if r else None
    return benchmarks.benchmark(restaurant_id, metric, cohort=cohort, db_path=db_path)


def confidence_for(restaurant_id, rec_kind, metric=None, restaurant=None, db_path=DB_PATH):
    return confidence.score(restaurant_id, rec_kind, metric=metric, restaurant=restaurant, db_path=db_path)


def cohort_for(restaurant):
    return categories.category_for(restaurant)


def context_lines(restaurant_id, restaurant=None, db_path=DB_PATH) -> list:
    """The short prompt section: own memory lines, then cohort facts only
    when the cohort clears the floor."""
    lines = memory.lines(memory.restaurant_memory(restaurant_id, db_path=db_path))
    if restaurant is None:
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id, db_path=db_path)
    cohort, src = categories.category_for(restaurant) if restaurant else (None, None)
    for b in benchmarks.all_for(restaurant_id, cohort=cohort, db_path=db_path):
        if b.get("available") and b.get("standing") not in (None, "unmeasured"):
            lines.append(f"{b['label']}: this restaurant is in the {b['standing']} of {b['n']} {b['cohort_label'].lower()} "
                         f"(band {b['p25']}–{b['p75']}, middle {b['p50']}).")
    for p in patterns.active(cohort, db_path=db_path, limit=3):
        # The pattern's MEASURED figures, never a composite percentage (R9,
        # B1: the strength % mixes weights that were never fitted, and a
        # model handed "strength 62%" can restate it as how sure it is).
        # Never worded as the chance the pattern is right (CA1 red flag 9).
        ev = p.get("evidence") or {}
        bits = []
        if ev.get("n"):
            bits.append(f"{ev['n']} restaurants")
        if p.get("cohen_d") is not None:
            bits.append(f"effect size d={float(p['cohen_d']):.2f}")
        if p.get("p_value") is not None:
            bits.append(f"p={float(p['p_value']):.3f}")
        lines.append(f"Pattern ({', '.join(bits) or 'measured across the cohort'} — an association, not a cause "
                     f"and not a probability): {p['sentence']}")
    return lines
