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
from . import privacy, categories, features, memory, feedback, scoring, patterns, benchmarks, trends, confidence, jobs, dashboard, staffing, metrics_registry, engine  # noqa: F401


def restaurant_memory(restaurant_id, db_path=DB_PATH):
    return memory.restaurant_memory(restaurant_id, db_path=db_path)


def platform_intelligence(db_path=DB_PATH):
    return {"patterns": patterns.active(db_path=db_path), "trends": trends.emerging(db_path=db_path)}


def industry_intelligence(cohort, db_path=DB_PATH):
    """A cohort's patterns and its bands as they may be published: current,
    over MIN_QUARTILE_N, coarse-rounded (benchmarks.published); a withheld
    band is None."""
    def _pub(m):
        p = benchmarks.published(cohort, m, db_path=db_path)
        return None if (not p or p.get("withheld")) else p
    return {"cohort": cohort, "label": categories.label(cohort),
            "patterns": patterns.active(cohort, db_path=db_path, include_platform=False),
            "benchmarks": [_pub(m) for m in features.BENCHMARK_KEYS]}


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


def benchmark(restaurant_id, metric, cohort=None, db_path=DB_PATH, cohort_source=None):
    """benchmarks.benchmark with the restaurant's own type and where it came
    from ('set' | 'inferred'), so an inferred cohort says so (NS4 M5)."""
    if cohort is None:
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db_path=db_path)
        cohort, cohort_source = categories.category_for(r) if r else (None, None)
    return benchmarks.benchmark(restaurant_id, metric, cohort=cohort, db_path=db_path, cohort_source=cohort_source)


def confidence_for(restaurant_id, rec_kind, metric=None, restaurant=None, db_path=DB_PATH):
    return confidence.score(restaurant_id, rec_kind, metric=metric, restaurant=restaurant, db_path=db_path)


def cohort_for(restaurant):
    return categories.category_for(restaurant)


# Which module's view permission a feature or metric key belongs to (the
# registry's module, else its name) — so a login denied Labor or Food Cost
# is never handed those figures through the learning layer (BM1-17).
_MODULE_PREFIXES = (
    ("labor", ("labor_", "staff_per_1k", "schedule")),
    ("inventory", ("food_cost", "waste_", "count_")),
    ("marketing", ("campaign", "post_", "posts_", "dish_posts", "offer_posts", "occasion_posts", "item_lift",
                   "guest_list")),
    ("reviews", ("reviews_", "avg_rating", "reply_", "response_")),
)


def metric_module(key) -> str | None:
    """The permission-module key (permissions.MODULE_VIEW_PERMISSIONS) a
    metric or feature belongs to, or None for one no module owns."""
    m = metrics_registry.meta(key).get("module")
    if m:
        return m
    k = str(key or "")
    return next((mod for mod, prefixes in _MODULE_PREFIXES if k.startswith(prefixes)), None)


def visible(key, denied_modules=None) -> bool:
    """Whether a login denied `denied_modules` may see metric `key`."""
    mod = metric_module(key)
    return not (mod and denied_modules and mod in denied_modules)


def _pattern_visible(p, denied_modules=None) -> bool:
    ev = p.get("evidence") or {}
    beh = ev.get("behaviour") or []
    return visible(ev.get("outcome"), denied_modules) and visible(beh[0] if beh else None, denied_modules)


def context_bundle(restaurant_id, restaurant=None, db_path=DB_PATH, denied_modules=None) -> tuple:
    """(lines, facts): context_lines and the response_validation benchmark
    facts behind every comparison a line states (engine.facts), so a peer
    claim the model makes from them binds (BM3-3)."""
    return context_lines(restaurant_id, restaurant=restaurant, db_path=db_path, denied_modules=denied_modules,
                         with_facts=True)


def context_lines(restaurant_id, restaurant=None, db_path=DB_PATH, denied_modules=None, with_facts=False):
    """The short prompt section: own memory lines, then peer comparisons
    only where a fair one exists (the Benchmark Engine), then patterns.
    With `with_facts`, (lines, facts) — see context_bundle.
    `denied_modules` (the permission-module keys this login may not view)
    projects lines and facts alike: no labor or food figure for a login
    denied that module (BM1-17)."""
    denied = frozenset(denied_modules or ())
    mem = memory.restaurant_memory(restaurant_id, db_path=db_path)
    if denied:
        mem = dict(mem, slopes={k: v for k, v in (mem.get("slopes") or {}).items() if visible(k, denied)})
    lines = memory.lines(mem)
    if restaurant is None:
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id, db_path=db_path)
    cohort, src = categories.category_for(restaurant) if restaurant else (None, None)
    # The Benchmark Engine's comparisons (Benchmarking audit 9/24/26): a
    # peer band of the restaurant's own type, the all-types band ONLY for a
    # behaviour metric (an all-types labor or food cost band is never
    # stated), each line naming how the group was chosen, set or guessed,
    # how many measured it and the comparison strength (engine.prompt_lines).
    comps, facts = [], []
    try:
        for cm in engine.compare_all(restaurant_id, kinds=("peers", "platform"), restaurant=restaurant,
                                     db_path=db_path):
            if not visible(cm.get("metric"), denied):
                continue
            if any(c.get("available") for c in cm.get("comparisons") or ()):
                comps.append(cm)
                facts += cm.get("facts") or []
    except Exception as e:
        print(f"[intelligence] benchmark context for {restaurant_id} unavailable: {e}")
    lines += engine.prompt_lines(comps)
    for p in patterns.active(cohort, db_path=db_path, limit=6):
        if not _pattern_visible(p, denied) or patterns.pooled_on_economics(p):
            continue
        if sum(1 for ln in lines if ln.startswith("Pattern")) >= 3:
            break
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
        if p.get("as_of"):
            bits.append(f"as of {p['as_of']}")
        lines.append(f"Pattern ({', '.join(bits) or 'measured across the cohort'} — an association, not a cause "
                     f"and not a probability): {p['sentence']}")
    return (lines, facts) if with_facts else lines


# ── the Benchmark Engine (Benchmarking audit 9/24/26) ──────────────────────
# Every comparison a restaurant is shown goes through these: one vocabulary
# (self, peers, platform, industry, location, market), one comparison-
# strength %, one wording for prompts and one set of validation facts.

def compare(restaurant_id, metric, **kw):
    return engine.compare(restaurant_id, metric, **kw)


def compare_all(restaurant_id, module=None, **kw):
    return engine.compare_all(restaurant_id, module=module, **kw)


def benchmark_facts(comparisons):
    return engine.facts(comparisons)


def benchmark_prompt_lines(comparisons):
    return engine.prompt_lines(comparisons)
