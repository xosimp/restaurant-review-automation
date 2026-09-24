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
the in-person audit already used). Layer 0: pure, no database, no clock.
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

_NRA_2025 = ("National Restaurant Association, 2025 Restaurant Operations Data Abstract")
_NRA_SHORT = "NRA 2025 Restaurant Operations Data Abstract"

ENTRIES = (
    # ── labor % of sales ────────────────────────────────────────────────
    {"metric": "labor_pct", "category": "full_service", "label": "full-service restaurants",
     "low": 30.0, "high": 34.0, "median": 34.2, "unit": "%",
     "median_basis": "median labor % (incl. benefits) of profitable full-service operators",
     "source": _NRA_2025 + ": full-service median labor (incl. benefits) 36.5% of sales in 2024; "
               "profitable full-service operators median 34.2%.",
     "short": _NRA_SHORT, "source_kind": "published", "year": 2025, "data_year": 2024,
     "applies_to": _FULL_SERVICE,
     "note": "Band top set at the profitable-operator median. Wage markets vary; the owner's own target wins."},
    {"metric": "labor_pct", "category": "fine_dining", "label": "fine-dining restaurants",
     "low": 33.0, "high": 38.0, "unit": "%",
     "source": "Operator rule of thumb; service-heavy formats carry more labor.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fine_dining",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "labor_pct", "category": "fast_casual", "label": "counter-service restaurants",
     "low": 25.0, "high": 30.0, "unit": "%",
     "source": "Operator rule of thumb for counter-service formats.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fast_casual",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "labor_pct", "category": "bar", "label": "bar-led concepts",
     "low": 25.0, "high": 30.0, "unit": "%",
     "source": "Operator rule of thumb for bar-dominant concepts.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": _BAR_TYPES, "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "labor_pct", "category": "sports_bar", "label": "sports bars",
     "low": 27.0, "high": 32.0, "unit": "%",
     "source": "Operator rule of thumb. Bar-forward concepts run lower labor % because beverage sales carry little labor.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("sports_bar",), "note": "Less authoritative than the NRA figure. Prefer the owner's own target."},

    # ── food cost % of sales ────────────────────────────────────────────
    {"metric": "food_cost_pct", "category": "full_service", "label": "full-service restaurants",
     "low": 28.0, "high": 32.0, "median": 32.0, "unit": "%",
     "median_basis": "median food and non-alcohol beverage cost of full-service operators",
     "source": _NRA_2025 + ": full-service food and non-alcohol beverage cost median 32.0% of sales in 2024.",
     "short": _NRA_SHORT, "source_kind": "published", "year": 2025, "data_year": 2024,
     # Not steakhouses: steak-heavy menus run higher (the source's own note).
     "applies_to": ("italian", "seafood", "family"),
     "note": "Menu type moves this a lot — steak-heavy menus run higher, pizza far lower."},
    {"metric": "food_cost_pct", "category": "fine_dining", "label": "fine-dining restaurants",
     "low": 30.0, "high": 35.0, "unit": "%", "source": "Operator rule of thumb.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fine_dining",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "food_cost_pct", "category": "fast_casual", "label": "counter-service restaurants",
     "low": 26.0, "high": 31.0, "unit": "%", "source": "Operator rule of thumb.",
     "short": "operator rule of thumb", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": ("fast_casual",), "note": "Not a published study. Prefer the owner's own target."},
    {"metric": "food_cost_pct", "category": "sports_bar", "label": "sports bars",
     "low": 28.0, "high": 33.0, "unit": "%",
     "source": "NRA full-service median, widened one point for shareables/wings-heavy menus.",
     "short": "NRA full-service median, widened a point", "source_kind": "rule_of_thumb", "year": 2025,
     "data_year": 2024, "applies_to": ("sports_bar",), "note": "Prefer the owner's own target."},
    {"metric": "food_cost_pct", "category": "bar", "label": "bar-led concepts",
     "low": 28.0, "high": 33.0, "unit": "%",
     "source": "NRA full-service median, widened one point (as sports bars).",
     "short": "NRA full-service median, widened a point", "source_kind": "rule_of_thumb", "year": 2025,
     "data_year": 2024, "applies_to": _BAR_TYPES, "note": "Prefer the owner's own target."},

    # ── prime cost (labor + COGS) — the one all-restaurant figure ───────
    {"metric": "prime_cost_pct", "category": "general", "label": "restaurants in general",
     "low": 60.0, "high": 65.0, "unit": "%",
     "source": "Widely used operator target: prime cost (labor + COGS) under 60–65% of sales.",
     "short": "widely used operator target", "source_kind": "rule_of_thumb", "year": 2025, "data_year": None,
     "applies_to": (ALL,), "note": "A target operators use, not a measured average."},

    # ── bar pour cost — about the bar program, whatever the restaurant ──
    {"metric": "pour_cost_pct", "category": "bar_program", "label": "bar programs",
     "low": 18.0, "high": 24.0, "unit": "%",
     "source": "Bar-industry consensus (Backbar, Sculpture Hospitality, DoorDash for Merchants guides): "
               "blended pour cost 18–24%.",
     "short": "bar-industry vendor guides", "source_kind": "vendor", "year": 2025, "data_year": None,
     "applies_to": (ALL,), "note": "Mix matters — a wine-heavy list legitimately runs higher."},

    # ── revenue per rating star — independents only ────────────────────
    {"metric": "revenue_per_star_pct", "category": "independent", "label": "independent restaurants",
     "low": 5.0, "high": 9.0, "unit": "%",
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


def lookup(metric, category, published_only=False):
    """The entry for `metric` that applies to restaurant type `category`,
    or None. A type-specific entry wins over an all-restaurant one. With
    `published_only`, a rule of thumb or vendor figure is no entry."""
    cat = (category or "").strip().lower() or None
    best = None
    for e in ENTRIES:
        if e["metric"] != metric:
            continue
        if published_only and e.get("source_kind") != "published":
            continue
        applies = e.get("applies_to") or ()
        if cat and cat in applies:
            return _copy(e, cat)
        if ALL in applies and best is None:
            best = e
    return _copy(best, cat) if best else None


def for_category(metric, category, source=None, published_only=False):
    """lookup() plus the type's provenance: `category_source` is 'set' or
    'inferred', and `inferred` is True when Cavnar guessed the type."""
    e = lookup(metric, category, published_only=published_only)
    if e is None:
        return None
    e["category_source"] = source
    e["inferred"] = source == "inferred"
    return e


def for_restaurant(metric, restaurant, published_only=False):
    """The entry for this restaurant's type (its own `category`, else the
    name-inferred one), or None. Never raises."""
    try:
        from intelligence import categories
        cat, src = categories.category_for(restaurant) if restaurant is not None else (None, None)
    except Exception:
        cat, src = None, None
    return for_category(metric, cat, src, published_only=published_only)


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


def facts(e, key_prefix="benchmark") -> list:
    """The entry as response_validation Facts (contract: kind 'benchmark',
    source {source, year, cohort_label, n}) — one per figure it carries, so
    a sentence quoting the band or the median binds to a registered value."""
    if not e:
        return []
    src = {"source": e.get("short") or e.get("source"), "year": e.get("year"), "data_year": e.get("data_year"),
           "cohort_label": e.get("label"), "n": None, "source_kind": e.get("source_kind"),
           "inferred": bool(e.get("inferred")), "restaurant_category": e.get("restaurant_category")}
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
