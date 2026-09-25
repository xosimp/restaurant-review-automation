"""benchmark_registry.py — every outside benchmark the product may state.

One table, keyed by metric and restaurant type. Each entry carries its band,
its source, the year, which restaurant types it applies to and a note. The
rules (Never-Say audit NS4 H3, C2, M5; workstream B):

* No entry, no benchmark. A metric with no entry for a restaurant's type
  gets no "industry" figure on any surface — no tile, no prompt line, no
  email sentence. A full-service band is never applied to a taco stand.
* An explicit "general" entry exists only where the source is about all
  restaurants (prime cost). Everything else is by type.
* The type's `inferred` flag travels with the entry. A benchmark picked
  from a type Cavnar guessed from the restaurant's name says so.
* A rule of thumb is labelled a rule of thumb (`source_kind`), and a dollar
  figure (labor "saving vs industry") is computed only against a published
  source (`published_only=True`).
* Deliberately absent (see ABSENT): the 4–5% waste target and the $22–28/hr
  blended wage had no source anywhere in the codebase. They are not here,
  so nothing may call them an industry figure.

Seeded from sales_audit_engine.BENCHMARKS (the sourced, type-specific bands
the in-person audit already used). Layer 0: pure, no database; the one
clock read is an entry's age limit (`is_stale`, which takes `today`).
`for_restaurant` reaches intelligence.categories inside the function (the
package's __init__ imports models).

Cohort benchmarks (Cavnar's own anonymous cohorts, intelligence/benchmarks)
are not in this table — they are measured weekly — but `cohort_facts` turns
one into the same fact shape, so response_validation sees one kind of
benchmark object whichever source it came from.
"""

# The restaurant types a registry entry can name. Keys of
# intelligence.categories.TAXONOMY; "*" means every restaurant, typed or not.
ALL = "*"

# Types whose service format the full-service figures describe. Kept narrow:
# a pizza place, a taqueria or a sushi bar can be counter service or full
# service, and a band for the wrong format is worse than no band (NS4 H3).
_FULL_SERVICE = ("steakhouse", "italian", "seafood", "family")
_BAR_TYPES = ("bar", "wine_bar", "brewery")

# Which confirmed service models (intelligence.categories.SERVICE_MODELS) a
# figure describes (Benchmarking re-audit #4, R2-17/R4-2): a counter-service
# Italian never gets the full-service NRA median. An entry without
# `service_models` describes every format.
_BAR_SERVICE = ("bar_led", "full_service")

# How long a figure may be quoted, in years after its data year (else its
# publication year) — the re-audit's #32 (R1-16): an annual abstract is
# superseded by the next one; a rule of thumb is re-checked every few years.
_ANNUAL_ABSTRACT_MAX_AGE = 3
_RULE_OF_THUMB_MAX_AGE = 5

# What the NRA food figure (and every band derived from it) measures.
_NRA_FOOD_DEFINITION = "food_nonalc_bev_cost_pct_sales"

_NRA_2025 = ("National Restaurant Association, 2025 Restaurant Operations Data Abstract")
_NRA_SHORT = "NRA 2025 Restaurant Operations Data Abstract"

ENTRIES = (
    # ── labor % of sales ────────────────────────────────────────────────
    {"metric": "labor_pct", "category": "full_service", "label": "full-service restaurants",
     "low": 30.0, "high": 34.0, "median": 34.2, "unit": "%",
     "median_basis": "profitable full-service operators' median labor %, including benefits",
     # NRA counts benefits; Cavnar's labor % is wages from shifts. A
     # comparison asked for with Cavnar's definition is refused (#14).
     "definition": "labor_incl_benefits",
     "service_models": ("full_service",), "max_age_years": _ANNUAL_ABSTRACT_MAX_AGE,
     "source": _NRA_2025 + ": full-service median labor (incl. benefits) 36.5% of sales in 2024; "
               "profitable full-service operators median 34.2%.",
     "short": _NRA_SHORT, "source_kind": "published", "year": 2025, "data_year": 2024,
     "applies_to": _FULL_SERVICE,
     "note": "Band top set at the profitable-operator median. Wage markets vary; the owner's own target wins."},
    {"metric": "labor_pct", "category": "fine_dining", "label": "fine-dining restaurants",
     "low": 33.0, "high": 38.0, "unit": "%",
     "definition": None, "service_models": ("full_service",), "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "median_basis": "operator rule of thumb for fine-dining labor %, which does not say what it counts",
     "source": "Operator rule of thumb; service-heavy formats carry more labor.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fine_dining",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "labor_pct", "category": "fast_casual", "label": "counter-service restaurants",
     "low": 25.0, "high": 30.0, "unit": "%",
     "definition": None, "service_models": ("counter",), "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "median_basis": "operator rule of thumb for counter-service labor %, which does not say what it counts",
     "source": "Operator rule of thumb for counter-service formats.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fast_casual",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "labor_pct", "category": "bar", "label": "bar-led concepts",
     "low": 25.0, "high": 30.0, "unit": "%",
     "definition": None, "service_models": _BAR_SERVICE, "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "median_basis": "operator rule of thumb for bar-led labor %, which does not say what it counts",
     "source": "Operator rule of thumb for bar-dominant concepts.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": _BAR_TYPES, "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "labor_pct", "category": "sports_bar", "label": "sports bars",
     "low": 27.0, "high": 32.0, "unit": "%",
     "definition": None, "service_models": _BAR_SERVICE, "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "median_basis": "operator rule of thumb for sports-bar labor %, which does not say what it counts",
     "source": "Operator rule of thumb. Bar-forward concepts run lower labor % because beverage sales carry little labor.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("sports_bar",), "note": "Less authoritative than the NRA figure. Prefer the owner's own target."},

    # ── food cost % of sales ────────────────────────────────────────────
    {"metric": "food_cost_pct", "category": "full_service", "label": "full-service restaurants",
     "low": 28.0, "high": 32.0, "median": 32.0, "unit": "%",
     "median_basis": "median food and non-alcohol beverage cost of full-service operators",
     "definition": _NRA_FOOD_DEFINITION,
     "service_models": ("full_service",), "max_age_years": _ANNUAL_ABSTRACT_MAX_AGE,
     "source": _NRA_2025 + ": full-service food and non-alcohol beverage cost median 32.0% of sales in 2024.",
     "short": _NRA_SHORT, "source_kind": "published", "year": 2025, "data_year": 2024,
     # Not steakhouses: steak-heavy menus run higher (the source's own note).
     "applies_to": ("italian", "seafood", "family"),
     "note": "Menu type moves this a lot — steak-heavy menus run higher, pizza far lower."},
    {"metric": "food_cost_pct", "category": "fine_dining", "label": "fine-dining restaurants",
     "low": 30.0, "high": 35.0, "unit": "%", "source": "Operator rule of thumb.",
     "definition": None, "service_models": ("full_service",), "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "median_basis": "operator rule of thumb for fine-dining food cost %, which does not say what it counts",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fine_dining",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "food_cost_pct", "category": "fast_casual", "label": "counter-service restaurants",
     "low": 26.0, "high": 31.0, "unit": "%", "source": "Operator rule of thumb.",
     "definition": None, "service_models": ("counter",), "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "median_basis": "operator rule of thumb for counter-service food cost %, which does not say what it counts",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fast_casual",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "food_cost_pct", "category": "sports_bar", "label": "sports bars",
     "low": 28.0, "high": 33.0, "unit": "%",
     "source": "NRA full-service median, widened one point for shareables/wings-heavy menus.",
     # Derived from the NRA figure, so it measures what that figure measures.
     "definition": _NRA_FOOD_DEFINITION, "service_models": _BAR_SERVICE, "max_age_years": _ANNUAL_ABSTRACT_MAX_AGE,
     "median_basis": "NRA full-service food and non-alcohol beverage cost median, widened a point",
     "short": "NRA full-service median, widened a point", "source_kind": "rule_of_thumb", "year": 2025,
     "data_year": 2024, "applies_to": ("sports_bar",), "note": "Prefer the owner's own target."},
    {"metric": "food_cost_pct", "category": "bar", "label": "bar-led concepts",
     "low": 28.0, "high": 33.0, "unit": "%",
     "source": "NRA full-service median, widened one point (as sports bars).",
     "definition": _NRA_FOOD_DEFINITION, "service_models": _BAR_SERVICE, "max_age_years": _ANNUAL_ABSTRACT_MAX_AGE,
     "median_basis": "NRA full-service food and non-alcohol beverage cost median, widened a point",
     "short": "NRA full-service median, widened a point", "source_kind": "rule_of_thumb", "year": 2025,
     "data_year": 2024, "applies_to": _BAR_TYPES, "note": "Prefer the owner's own target."},

    # ── prime cost (labor + COGS) — the one all-restaurant figure ───────
    {"metric": "prime_cost_pct", "category": "general", "label": "restaurants in general",
     "low": 60.0, "high": 65.0, "unit": "%",
     "definition": "prime_cost_labor_plus_cogs_pct_sales", "max_age_years": _RULE_OF_THUMB_MAX_AGE,
     "source": "Widely used operator target: prime cost (labor + COGS) under 60–65% of sales.",
     "short": "widely used operator target", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": (ALL,), "note": "A target operators use, not a measured average."},

    # ── bar pour cost — about the bar program, whatever the restaurant ──
    {"metric": "pour_cost_pct", "category": "bar_program", "label": "bar programs",
     "low": 18.0, "high": 24.0, "unit": "%",
     "definition": "pour_cost_pct_beverage_sales", "max_age_years": _ANNUAL_ABSTRACT_MAX_AGE,
     "source": "Bar-industry consensus (Backbar, Sculpture Hospitality, DoorDash for Merchants guides): "
               "blended pour cost 18–24%.",
     "short": "bar-industry vendor guides", "source_kind": "vendor", "year": 2025, "data_year": None,
     "applies_to": (ALL,), "note": "Mix matters — a wine-heavy list legitimately runs higher."},

    # ── revenue per rating star — independents only ────────────────────
    {"metric": "revenue_per_star_pct", "category": "independent", "label": "independent restaurants",
     "low": 5.0, "high": 9.0, "unit": "%",
     # A peer-reviewed study, not an annual abstract: cited for longer, and
     # due for re-review by 2031.
     "definition": "revenue_change_per_yelp_star_independents", "max_age_years": 15,
     "source": "Luca, M. (Harvard Business School, 2016) 'Reviews, Reputation, and Revenue: The Case of "
               "Yelp.com': a one-star increase in Yelp rating was associated with a 5–9% revenue increase "
               "for independent restaurants (Seattle).",
     "short": "Luca, Harvard Business School, 2016 (Yelp, Seattle)", "source_kind": "published",
     "year": 2016, "data_year": None, "applies_to": (ALL,),
     "requires": "independent",
     "note": "One study, one platform, one city; chain restaurants showed no effect. An association, "
             "not a promise."},
)

# Metrics the product used to benchmark with no source anywhere. Not in the
# table on purpose; `why_absent` says so to anyone who asks.
ABSENT = {
    "waste_pct_of_purchases": ("No published source was found for the 4–5% waste target the product "
                               "used; it is Cavnar's starting target, not an industry figure."),
    "blended_hourly_rate": ("No sourced, regional figure for a blended hourly rate; the $22–28/hr "
                            "'industry average' had none."),
}

# Owner-facing words for where a type came from.
INFERRED_NOTE = "type inferred from the restaurant's name, not set by the owner"


def _copy(e, category=None):
    out = dict(e)
    out["applies_to"] = tuple(e.get("applies_to") or ())
    lo, hi = out.get("low"), out.get("high")
    out["mid"] = round((lo + hi) / 2.0, 2) if lo is not None and hi is not None else None
    out["restaurant_category"] = category
    return out


def entries(metric=None) -> list:
    """Every entry (or every entry for one metric), as copies."""
    return [_copy(e) for e in ENTRIES if metric is None or e["metric"] == metric]


def definitions_differ(e, definition) -> bool:
    """True when `definition` is asked for and the entry does not measure
    exactly that (Benchmarking audit #14, BM3-15; re-audit #32, R4-5): the
    NRA labor median includes benefits; Cavnar's labor % is wages from
    shifts. An entry that does not state what it measures (`definition`
    None — every operator rule of thumb) is NOT known to match, so it is
    treated as differing: unknown means not comparable."""
    if not definition or not e:
        return False
    return e.get("definition") != definition


def is_stale(e, today=None) -> bool:
    """True when the entry is older than its `max_age_years`, counted from
    its data year (else its publication year) — re-audit #32 (R1-16): a
    figure with no age limit was quoted forever."""
    if not e or not e.get("max_age_years"):
        return False
    base = e.get("data_year") or e.get("year")
    if not base:
        return False
    if today is None:
        from datetime import date as _date
        today = _date.today()
    return int(today.year) > int(base) + int(e["max_age_years"])


def service_model_fits(e, service_model) -> bool:
    """Whether the entry describes this confirmed service model
    (intelligence.categories.SERVICE_MODELS). An entry that names no
    service model describes every format; an unknown service model is not a
    disagreement (re-audit #4, R2-17/R4-2)."""
    sms = (e or {}).get("service_models")
    if not sms or not service_model:
        return True
    return service_model in sms


def lookup(metric, category, published_only=False, definition=None, service_model=None, today=None):
    """The entry for `metric` that applies to restaurant type `category`,
    or None — the ONE lookup the engine, target seeding, the blend, the peer
    ledger and the sales audit share. A type-specific entry wins over an
    all-restaurant one. With `published_only`, a rule of thumb or vendor
    figure is no entry. With `definition`, an entry that measures something
    else — or does not say what it measures — is no entry. With
    `service_model`, an entry for another format is no entry (a
    counter-service Italian never gets the full-service NRA median). An
    entry past its age limit is never an entry."""
    cat = (category or "").strip().lower() or None
    best = None
    for e in ENTRIES:
        if e["metric"] != metric:
            continue
        if published_only and e.get("source_kind") != "published":
            continue
        if definitions_differ(e, definition):
            continue
        if not service_model_fits(e, service_model):
            continue
        if is_stale(e, today):
            continue
        applies = e.get("applies_to") or ()
        if cat and cat in applies:
            return _copy(e, cat)
        if ALL in applies and best is None:
            best = e
    return _copy(best, cat) if best else None


def for_category(metric, category, source=None, published_only=False, definition=None, service_model=None):
    """lookup() plus the type's provenance: `category_source` is 'set' or
    'inferred', and `inferred` is True when Cavnar guessed the type."""
    e = lookup(metric, category, published_only=published_only, definition=definition,
               service_model=service_model)
    if e is None:
        return None
    e["category_source"] = source
    e["inferred"] = source == "inferred"
    e["service_model"] = service_model
    return e


def confirmed_service_model(restaurant):
    """The owner-confirmed service model, or None (unconfirmed profile or
    no restaurant). Never raises."""
    if restaurant is None:
        return None
    try:
        from intelligence import categories
        prof = categories.profile_for(restaurant)
    except Exception:
        return None
    return prof.get("service_model") if prof.get("confirmed") else None


def for_restaurant(metric, restaurant, published_only=False, definition=None):
    """The entry for this restaurant's type (its confirmed concept, its own
    `category`, else the name-inferred one) AND its confirmed service
    model, or None. Never raises."""
    try:
        from intelligence import categories
        cat, src = categories.category_for(restaurant) if restaurant is not None else (None, None)
    except Exception:
        cat, src = None, None
    return for_category(metric, cat, src, published_only=published_only, definition=definition,
                        service_model=confirmed_service_model(restaurant))


def definition_note(e, what, definition) -> str | None:
    """Why an entry is context rather than a comparison for a metric
    labelled `what` (e.g. "Labor %") measured as `definition`, or None when
    the entry measures exactly that. Names what the figure counts when the
    source says, and says plainly when it does not."""
    if not e or not definitions_differ(e, definition):
        return None
    what = (what or "figure").lower()
    if e.get("definition"):
        return (f"The published figure is the {e.get('median_basis') or 'published figure'}; this restaurant's "
                f"{what} is measured differently, so it is context, not a like-for-like comparison.")
    return (f"The {cite(e) or 'figure'} does not say what it counts, so it is context for this restaurant's "
            f"{what}, not a like-for-like comparison.")


def why_absent(metric) -> str | None:
    return ABSENT.get(metric)


def _num(x):
    return f"{x:g}" if isinstance(x, (int, float)) else str(x)


def cite(e) -> str:
    """Where the figure comes from, in a few words an owner can check:
    "NRA 2025 Restaurant Operations Data Abstract (2024 data)" or "an
    operator rule of thumb, not a published study"."""
    if not e:
        return ""
    kind = e.get("source_kind")
    if kind == "rule_of_thumb":
        return f"{e.get('short') or 'an operator rule of thumb'} — not a published study"
    if kind == "vendor":
        return f"{e.get('short')} — vendor guidance, directional"
    yr = f" ({e['data_year']} data)" if e.get("data_year") else ""
    return f"{e.get('short') or e.get('source')}{yr}"


def band_text(e) -> str:
    return f"{_num(e['low'])}–{_num(e['high'])}{e.get('unit') or ''}"


def line(e, what=None) -> str:
    """One benchmark sentence with its source, year and applicability, the
    shape every surface uses. A published figure is quoted as the source
    states it (the median), and a band Cavnar set from it is said to be
    Cavnar's — the NRA never published "30–34%":
      "Labor % for full-service restaurants: median labor % (incl. benefits)
       of profitable full-service operators 34.2% (NRA 2025 Restaurant
       Operations Data Abstract (2024 data)); Cavnar's target band 30–34% is
       set from it; type inferred from the restaurant's name, not set by the
       owner." """
    if not e:
        return ""
    what = what or e["metric"].replace("_pct", " %").replace("_", " ")
    unit = e.get("unit") or ""
    if e.get("median") is not None:
        s = (f"{what} for {e['label']}: {e.get('median_basis') or 'published median'} "
             f"{_num(e['median'])}{unit} ({cite(e)}); Cavnar's target band {band_text(e)} is set from it")
    else:
        s = f"{what} for {e['label']}: {band_text(e)} ({cite(e)})"
    if e.get("inferred"):
        s += f"; {INFERRED_NOTE}"
    return s + "."


def engine_metric(registry_metric) -> str | None:
    """The metrics_registry key whose published figure is `registry_metric`
    (labor_pct -> labor_pct_28d), or None for a metric the engine does not
    compare (prime cost, pour cost, revenue per star)."""
    try:
        from intelligence import metrics_registry as _mr
    except Exception:
        return None
    return next((k for k, v in _mr.METRICS.items() if v.get("industry") == registry_metric), None)


def facts(e, key_prefix="benchmark", own_value=None) -> list:
    """The entry as response_validation Facts (contract: kind 'benchmark',
    source {source, year, cohort_label, n}) — one per figure it carries, so
    a sentence quoting the band or the median binds to a registered value.

    The source also carries the shared fact contract (re-audit fix round):
    `metric` (the metrics_registry key), `better`, `comparable` (False when
    the figure measures something else or does not say what it measures —
    context only, so no directional claim may bind to it),
    `definition_note`, `inferred`, `own_value` (the restaurant's own figure
    when the caller has it), and `standing` / `strength_pct` (None: a
    published figure is never a ranked standing)."""
    if not e:
        return []
    metric = engine_metric(e.get("metric"))
    better, own_def, label = None, None, None
    if metric:
        try:
            from intelligence import metrics_registry as _mr
            m = _mr.meta(metric)
            better, own_def, label = m.get("better"), _mr.definition(metric), m.get("label")
        except Exception:
            pass
    # With no Cavnar definition to hold it against, the figure is not known
    # to be like for like: not comparable.
    comparable = bool(own_def) and not definitions_differ(e, own_def)
    try:
        own = float(own_value) if own_value is not None else None
    except (TypeError, ValueError):
        own = None
    src = {"source": e.get("short") or e.get("source"), "year": e.get("year"), "data_year": e.get("data_year"),
           "cohort_label": e.get("label"), "n": None, "source_kind": e.get("source_kind"),
           "inferred": bool(e.get("inferred")), "restaurant_category": e.get("restaurant_category"),
           "metric": metric, "better": better, "comparable": comparable,
           "definition_note": (None if comparable else
                               (definition_note(e, label, own_def) if own_def else
                                f"{cite(e) or 'This figure'} is context, not a like-for-like comparison.")),
           "own_value": own, "standing": None, "strength_pct": None}
    base = f"{key_prefix}.{e['metric']}.{e['category']}"
    out = []
    for part in ("low", "high", "median"):
        if e.get(part) is None:
            continue
        out.append({"key": f"{base}.{part}", "value": float(e[part]), "unit": e.get("unit") or "%",
                    "kind": "benchmark", "entity": "industry", "period": None,
                    "as_of": str(e.get("data_year") or e.get("year") or ""), "source": dict(src)})
    return out


def cohort_facts(b, key_prefix="cohort") -> list:
    """A Cavnar cohort band (intelligence.benchmarks.benchmark()) in the
    same fact shape: cohort_label is the cohort ACTUALLY used, n its size
    (the viewer excluded), as_of the M/D/YY the band was computed."""
    if not b or not b.get("available"):
        return []
    src = {"source": "Cavnar anonymous cohort", "year": None, "cohort_label": b.get("cohort_label"),
           "n": b.get("n"), "source_kind": "cohort", "inferred": bool(b.get("inferred")),
           "restaurant_category": b.get("cohort")}
    out = []
    for part in ("p25", "p50", "p75"):
        if b.get(part) is None:
            continue
        out.append({"key": f"{key_prefix}.{b.get('metric')}.{part}", "value": float(b[part]), "unit": "",
                    "kind": "benchmark", "entity": "cohort", "period": None, "as_of": b.get("as_of"),
                    "source": dict(src)})
    return out
