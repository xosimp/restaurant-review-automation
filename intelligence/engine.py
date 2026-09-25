"""Level 3: the Benchmark Engine — every comparison a restaurant is shown,
through one function, one vocabulary and one set of facts (Benchmarking
audit 9/24/26, BM4 §4).

Six kinds of comparison, each either available or carrying `why_not`:

  self      compared to this restaurant's own normal — its trailing 13
            weeks, moved by how the same weeks moved last year once a year
            of history exists ("adjusted for this time last year"), judged
            against its own swing read from non-overlapping 28-day windows;
            a verdict needs the gap to clear a t-quantile of that swing plus
            the baseline's own uncertainty (≤10% false calls on an unchanged
            restaurant). Meaningful at any platform size, so it is the
            headline until peers clear their floors.
  peers     compared to restaurants like it: the published band of the
            finest group on its ladder that clears every floor — its
            owner-CONFIRMED partition (service model; × bar-led for labor
            and food cost; × menu family for food cost and waste —
            categories.partition_key) narrowed by the ticket band, volume
            band and market it measures, then the partition itself, then
            the service model alone, said in the label (`level` of
            `levels`); the viewer's whole organisation left out, at least
            MIN_QUARTILE_N others from privacy.MIN_ORGS organisations,
            coarse, frozen weekly, at most 8 weeks old. The small-group
            blend toward a published median (_blend) is DORMANT: it needs a
            published figure measured the same way as Cavnar's, and none is
            registered (the NRA labor and food medians are defined
            differently). A guessed type compares with no one; the
            restaurant's own figure below its floor reads "about N more
            measured days to a comparison".
  platform  compared to every restaurant on Cavnar — ONLY for behaviour
            metrics (metrics_registry.platform_allowed); an all-types band
            for labor %, food cost % or hours per $1k is never shown.
  industry  a published figure for the restaurant's type (benchmark_registry),
            quoted with its source and year.
  location  compared to the owner's other locations (privacy.org_key, named
            by location_name) — own data, so no privacy floor; like for like
            (same confirmed partition for a format or economics metric, each
            figure own_eligible); only for logins that may switch locations,
            and closed when no viewer is given.
  market    nearby competitors, rating only: Intel's matched-rival,
            review-weighted standing with its tie band
            (competitor_intel_format.market_comparison) — the one market
            comparison the Reviews read and Intel both quote.

The headline is peers when available, else (behaviour metrics only) the
all-types platform band, else self, else industry; `why_not`
is always filled for a kind that cannot be shown — that is how the engine
knows when NOT to benchmark. Every kind that ranks the restaurant's own
figure asks own_eligible() first: a labor cost on the $26/hr default (or
role rates covering under 80% of the hours) and an irregularly logged waste
% are never ranked. A comparison-strength percentage (never words) says how
well supported a band comparison is: capped by the group's size (ranking
level near 20 others, never 100), then band age, this metric's own figure,
how finely the group is split, how tightly it agrees and how many separate
owners it holds. A gap inside the band's and the restaurant's own
uncertainty reads "about the middle", never a quartile word.

facts() turns every figure into a response_validation Fact (kind
"benchmark" for bands and published figures, "computed" for the
restaurant's own baseline and own value), each source carrying metric,
better, own_value, standing, comparable, definition_note, inferred and
strength_pct, so a peer claim binds to a figure the engine emitted — or is
not said. prompt_lines() is the one wording for prompts: the own figure and
which way is better first, the standing in better/worse words.

payload_for() serves the owner screens: permission-filtered first, every
kind but location from the nightly cache (comparison_cache) when it is
fresh and was computed from the current settings (inputs_key), the
location kind live once per request.

Level 3 of intelligence/: never imports the app's request layer.
"""
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
from . import benchmarks as _bm
from . import categories
from . import features as _features
from . import metrics_registry as reg
from .stats import percentile

KINDS = ("self", "peers", "platform", "industry", "location", "market")
# 2: the re-audit round (9/24/26) — a cached payload from before it is
# recomputed, never served.
ENGINE_VERSION = 2

# Comparison strength (a % like every confidence in the product).
# Group size is a CAP, not one dimension among several (re-audit #15,
# R1-07/R3-6): 40% at the minimum group of 8, +3 points per restaurant, so
# ranking words (75%) need about 20 others; never 100.
STRENGTH_FULL_N = 20            # others at which the size cap reaches ranking level
STRENGTH_SIZE_BASE = 40         # the cap at MIN_QUARTILE_N others
STRENGTH_SIZE_STEP = 3          # the cap rises this much per extra restaurant
STRENGTH_MAX = 95               # no comparison of other restaurants is certain
STRENGTH_FULL_ORGS = 10         # separate owners at which the organisation dimension is full
STRENGTH_FRESH_WEEKS = 2        # a band this young is fully fresh; 0 at MAX_BAND_AGE_WEEKS
# A type Cavnar guessed compares with no one (_peers refuses an unconfirmed
# profile before any band is read), so a guessed type holds strength at 0 —
# the old 74% cap described a comparison the engine never makes (R3-21).
INFERRED_TYPE_CAP = 0
PLATFORM_CAP = 60               # an all-types band, even for a behaviour metric
# The restaurant's own normal (re-audit #1, R2-1/R3-11/R4-9). A feature row
# is a 28-day window stored weekly, so neighbouring rows share three of their
# four weeks: the week-to-week spread of those rows understates the real
# swing and a ±1-MAD band called about half of an unchanged restaurant's
# weeks "better" or "worse". The swing is now read from rows a whole window
# apart (non-overlapping), and a verdict needs the gap to clear a t-quantile
# of that swing plus the uncertainty of the baseline itself — a steady
# restaurant is called better/worse in ≤10% of weeks (tested by simulation).
SELF_BASELINE_WEEKS = 13        # weeks of history the normal is read from
SELF_GAP_WEEKS = 4              # the latest weeks' rows overlap the current window
SELF_WINDOW_WEEKS = 4           # one feature row spans this many weeks
SELF_MIN_POINTS = 7
SELF_MIN_DF = 3                 # degrees of freedom the swing is estimated on
# With a year of history the normal is seasonal (re-audit #39, R2-15): the
# recent baseline moved by how the same weeks moved last year.
SEASONAL_BACK_WEEKS = (52, 51, 53)
SERIES_WEEKS = 72               # enough for last year's week and its own baseline
# Two-sided 95% t-quantiles; the large-sample value is 2.
_T975 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
         11: 2.20, 12: 2.18, 13: 2.16, 14: 2.14, 15: 2.13}
# "Compared to your other locations" needs this many measured.
LOCATION_MIN = 2
# A restaurant's labor cost is its own when at least this share of the hours
# worked is priced at a rate the owner set (re-audit #11, R2-5): one priced
# role with every other role on the $26/hr default is not a labor cost.
LABOR_SOURCED_MIN = 0.8
# A small peer group is blended toward the published median with weight
# n / (n + BLEND_KAPPA) on the group (Benchmarking audit #20, BM2 §6): a
# 9-restaurant group is not treated as gospel, and the blend fades as n grows.
# Dormant (fix round R2-9/R1-16): _blend_entry finds no published figure
# measured the same way, so no band is blended today. Candidate for future
# cleanup after additional verification — or re-sourcing, if a like-for-like
# published figure is ever registered.
BLEND_KAPPA = 8


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _num(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt(x, unit=""):
    if x is None:
        return "—"
    if unit == "share":
        return f"{round(x * 100)}%"
    if unit == "%":
        return f"{x:g}%"
    if unit == "★":
        return f"{x:.1f}★"
    return f"{x:g}"


def _lc(s):
    """A label mid-sentence: "Counter-service restaurants on Cavnar" →
    "counter-service restaurants on Cavnar"."""
    s = str(s or "")
    return s[:1].lower() + s[1:]


def _mdy(d):
    from time_utils import mdy
    return mdy(d) if d else None


# ── standing ───────────────────────────────────────────────────────────────

def standing(value, band, metric, n, own_noise=None):
    """(standing, margin) of `value` in a published band.

    The margin is the uncertainty of the gap: the band median's own
    (≈ 1.25·IQR/1.35/√n) and this restaurant's week-to-week swing
    (`own_noise`, one 28-day window's standard deviation — _own_sigma)
    combined, never under the metric's published step (re-audit #16,
    R1-08/R3-10). Inside it: "about the middle". A quartile word needs the
    figure to sit past the quartile by the whole margin; with `own_noise`
    unknown (too little of its own history) it cannot be shown to, so the
    word stops at "above/below the middle"."""
    v = _num(value)
    p25, p50, p75 = _num(band.get("p25")), _num(band.get("p50")), _num(band.get("p75"))
    if v is None or p50 is None or p25 is None or p75 is None:
        return "unmeasured", None
    step = reg.meta(metric).get("step") or 0
    iqr = max(0.0, p75 - p25)
    se = 1.25 * iqr / 1.35 / math.sqrt(max(1, int(n or 1)))
    own = _num(own_noise)
    margin = max(step, math.sqrt(se ** 2 + (own or 0.0) ** 2))
    if abs(v - p50) <= margin:
        return "about the middle", round(margin, 3)
    higher = reg.meta(metric).get("better", "higher") == "higher"
    good = v > p50 if higher else v < p50
    quartile_ok = own is not None
    far = quartile_ok and ((v - margin >= p75) if higher else (v + margin <= p25))
    far_bad = quartile_ok and ((v + margin <= p25) if higher else (v - margin >= p75))
    if good:
        return ("top quarter" if far else "above the middle"), round(margin, 3)
    return ("bottom quarter" if far_bad else "below the middle"), round(margin, 3)


# The standing as an outcome, in better/worse words (re-audit #17, R3-7/
# R3-23): "bottom quarter" on a lower-is-better metric is the HIGHEST labor
# %, and a model (and an owner) read it as the lowest. Every prompt carries
# these words; the screens may too (benchmark_views).
_OUTCOME = {"top quarter": "better than 3 in 4 of the group",
            "above the middle": "better than the middle of the group",
            "about the middle": "about the middle of the group — the gap is within the uncertainty",
            "below the middle": "worse than the middle of the group",
            "bottom quarter": "worse than 3 in 4 of the group"}


def outcome_words(standing_word, metric) -> str | None:
    """"worse than 3 in 4 of the group (higher labor % than 3 in 4)" — the
    standing said as better or worse, with the direction spelled out for a
    quartile."""
    base = _OUTCOME.get(standing_word)
    if not base:
        return None
    m = reg.meta(metric)
    label = (m.get("label") or "figure").lower()
    higher = m.get("better", "higher") == "higher"
    if standing_word == "top quarter":
        return f"{base} ({'higher' if higher else 'lower'} {label} than 3 in 4)"
    if standing_word == "bottom quarter":
        return f"{base} ({'lower' if higher else 'higher'} {label} than 3 in 4)"
    return base


def _size_cap(n) -> float:
    """The most a group of `n` others can support, in %."""
    n = int(n or 0)
    if n <= 0:
        return 0.0
    return float(min(STRENGTH_MAX, STRENGTH_SIZE_BASE + STRENGTH_SIZE_STEP * (n - _bm.MIN_QUARTILE_N)))


def granularity(cohort, metric=None, level=None) -> float:
    """How alike the group is by construction, 0–1 (R3-21, R4-11): every
    type on Cavnar 0.6; the service model alone 0.75; × bar-led (labor, food
    and staffing families) 0.85; × menu family or sales band 0.95. A coarser
    rung of the peer ladder (`level` > 0) is marked down 5 points a rung."""
    if not cohort or cohort == "platform":
        return 0.6
    if categories.is_partition(cohort):
        parts = str(cohort)[3:].split("|")[1:]
        fam = reg.partition_family(metric) if metric else "format"
        g = 0.75
        if fam in ("labor", "food", "staff"):
            g += 0.1
        if any(p != "bar" for p in parts):
            g += 0.1
    else:
        g = 0.8          # a single legacy type
    try:
        g *= max(0.5, 1.0 - 0.05 * int(level or 0))
    except (TypeError, ValueError):
        pass
    return round(min(0.95, g), 3)


def strength(n, band_week=None, type_source=None, platform=False, own_stale=False, completeness=None,
             today=None, *, orgs=None, spread=None, own_quality=None, similarity=None, level=None,
             levels=None) -> dict:
    """The comparison-strength object for a band comparison, in the K1
    shape: {pct, label, dimensions, caps_applied, reason, meaning}.

    Re-audit #15: the group's size is a CAP (40% at 8 others, +3 a
    restaurant — ranking level near 20; never over STRENGTH_MAX); under it,
    the geometric mean of freshness, the restaurant's own figure for THIS
    metric (`own_quality`, else the older whole-row `completeness`), how
    alike the group is by construction (`similarity`, granularity()), how
    tightly the group agrees (`spread`: the IQR as a share of the quality
    gate's ceiling, metrics_registry.spread_ratio) and how many separate
    owners it holds (`orgs`). Then the caps: a guessed type
    INFERRED_TYPE_CAP, an all-types band PLATFORM_CAP. `level`/`levels`
    (the peer ladder's rung) lower the similarity for a coarser rung."""
    dims = {}
    size_cap = _size_cap(n)
    dims["size"] = size_cap / 100.0
    mon = _bm._week_monday(band_week) if band_week else None
    if mon:
        age = max(0.0, ((today or date.today()) - mon).days / 7.0)
        span = max(1.0, _bm.MAX_BAND_AGE_WEEKS - STRENGTH_FRESH_WEEKS)
        dims["freshness"] = 1.0 if age <= STRENGTH_FRESH_WEEKS else max(0.0, 1.0 - (age - STRENGTH_FRESH_WEEKS) / span)
    if own_quality is not None:
        own = max(0.0, min(1.0, float(own_quality)))
    else:
        own = 0.5 if own_stale else 1.0
        if completeness is not None:
            own *= max(0.0, min(1.0, float(completeness)))
    dims["own"] = own
    if similarity is None:
        # No group named: all types for a platform band, else unmeasured
        # (1.0) less the ladder's rung.
        similarity = 0.6 if platform else max(0.5, 1.0 - 0.05 * int(level or 0))
    dims["similarity"] = max(0.0, min(1.0, float(similarity)))
    if spread is not None:
        dims["spread"] = max(0.1, min(1.0, 1.0 - 0.6 * float(spread)))
    if orgs is not None:
        dims["orgs"] = max(0.0, min(1.0, float(orgs) / STRENGTH_FULL_ORGS))
    others = [v for k, v in dims.items() if k != "size" and v is not None]
    if size_cap <= 0 or not others or any(v <= 0 for v in others):
        pct = 0.0
    else:
        pct = min(size_cap, 100.0 * math.exp(sum(math.log(v) for v in others) / len(others)))
    caps = []
    if pct >= size_cap and size_cap < STRENGTH_MAX:
        caps.append("group_size")
    pct = min(pct, float(STRENGTH_MAX))
    if type_source == "inferred" and pct > INFERRED_TYPE_CAP:
        pct, caps = float(INFERRED_TYPE_CAP), caps + ["inferred_type"]
    if platform and pct > PLATFORM_CAP:
        pct, caps = float(PLATFORM_CAP), caps + ["all_types"]
    pct = int(round(pct))
    weakest = min(dims, key=lambda k: dims[k])
    reason = {"size": f"{n} other restaurants — about {STRENGTH_FULL_N} makes a strong comparison",
              "freshness": "the band is several weeks old",
              "own": "this restaurant's own figure for this measure is thin or old",
              "similarity": ("an all-types band" if platform else
                             ("the type was guessed from the name" if type_source == "inferred" else
                              "the group is split only coarsely, so its restaurants differ")),
              "spread": "the group's figures are widely spread",
              "orgs": "the group comes from few separate owners"}[weakest]
    out = {"pct": pct, "label": f"{pct}% comparison strength",
           "dimensions": {k: round(v, 3) for k, v in dims.items()},
           "caps_applied": caps, "reason": reason,
           "meaning": "How well supported this comparison is — not how well you are doing"}
    if level is not None:
        out["level"], out["levels"] = level, levels
    return out


# ── the kinds ──────────────────────────────────────────────────────────────

def _series(restaurant_id, db_path, weeks=SERIES_WEEKS):
    try:
        return _features.series(restaurant_id, weeks=weeks, db_path=db_path)
    except Exception:
        return []


# ── own-figure eligibility (re-audit #11, #12) ─────────────────────────────
# A viewer's figure must pass the same eligibility a band member's does, or
# it is not ranked: a labor cost on Cavnar's $26/hr default is not a labor
# cost (benchmarks.compute drops such members), and a waste % from a kitchen
# that logs waste irregularly reads near 0% ("doesn't log" passing as "low
# waste" — features.cross_restaurant_view drops it for members).

def _g(obj, k):
    return obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)


def _owner_fallback_rate(restaurant) -> bool:
    """True when a shift whose role has no rate of its own is priced at a
    rate the owner set (their flat rate, or a `_default` in the role
    rates) rather than Cavnar's $26/hr."""
    import thresholds as _thr
    dflt = _thr.LABOR_DEFAULT_HOURLY_RATE
    raw = _g(restaurant, "role_rates_json")
    try:
        import json as _json
        rates = _json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        d = float((rates or {}).get("_default") or 0)
        if d > 0 and abs(d - dflt) > 1e-9:
            return True
    except Exception:
        pass
    try:
        rate = float(_g(restaurant, "hourly_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return rate > 0 and abs(rate - dflt) > 1e-9


def labor_rate_coverage(restaurant) -> float | None:
    """The share of the last 28 days' hours whose role has a rate the owner
    set (matched the way labor._shift_rate matches), or None when the shifts
    cannot be read. Only needed when the fallback rate is the default."""
    try:
        import json as _json
        import labor as _labor
        raw = _g(restaurant, "role_rates_json")
        rates = _json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
        named = {str(k).strip().lower() for k, v in (rates or {}).items()
                 if k != "_default" and float(v or 0) > 0}
        shifts = _labor.current_window(_labor.load_shifts_for_restaurant(_g(restaurant, "id")),
                                       _labor.CURRENT_WINDOW_DAYS)
        total = priced = 0.0
        for s in shifts or ():
            h = float(_labor._shift_hours(s) or 0)
            if h <= 0:
                continue
            total += h
            if str(s.get("role") or "").strip().lower() in named:
                priced += h
        return (priced / total) if total > 0 else None
    except Exception:
        return None


def labor_cost_sourced(restaurant, _memo=None) -> tuple:
    """(ok, why_not): whether this restaurant's labor cost rests on rates the
    owner set — its own blended rate, or per-role rates covering at least
    LABOR_SOURCED_MIN of the hours worked (R2-5: one priced role put a
    mostly-$26 labor % in front of everyone)."""
    if _memo is not None and "labor_sourced" in _memo:
        return _memo["labor_sourced"]
    import thresholds as _thr
    basis = _thr.labor_cost_basis(restaurant)
    why = "set your pay rates to compare labor cost — it is on Cavnar's assumed $26/hr, not your payroll"
    if basis == "default":
        out = (False, why)
    elif basis == "owner_blended" or _owner_fallback_rate(restaurant):
        out = (True, None)
    else:
        cov = labor_rate_coverage(restaurant)
        if cov is not None and cov >= LABOR_SOURCED_MIN:
            out = (True, None)
        else:
            out = (False, ("set a pay rate for every role (or a flat rate) to compare labor cost — "
                           + (f"your role rates price {int(round(cov * 100))}% of the hours worked, the rest is "
                              "Cavnar's assumed $26/hr" if cov is not None else
                              "the roles without a rate are on Cavnar's assumed $26/hr")))
    if _memo is not None:
        _memo["labor_sourced"] = out
    return out


def own_eligible(restaurant, metric, features_row=None, _memo=None) -> tuple:
    """(ok, why_not): whether this restaurant's own figure for `metric` may
    be ranked against anyone — the same eligibility a band member passes.
    `features_row` is a feature row ({week, features, …}) or the features
    dict; None when no own figure is involved (a published figure looked up
    alone), which only the labor-cost basis can refuse."""
    if metric in reg.LABOR_COST_METRICS:
        ok, why = labor_cost_sourced(restaurant, _memo)
        if not ok:
            return False, why
    if metric == "waste_sales_pct_28d" and features_row is not None:
        f = features_row.get("features") if isinstance(features_row, dict) and "features" in features_row \
            else features_row
        f = f or {}
        if f.get(metric) is not None and not _features.waste_logging_regular(f):
            r = _num(f.get(_features.WASTE_REGULARITY_KEY))
            have = (f"{int(round(r * _features.WASTE_REGULARITY_WEEKS))} of {_features.WASTE_REGULARITY_WEEKS} weeks"
                    if r is not None else "not measured yet")
            return False, f"waste isn't logged regularly enough to compare ({have})"
    return True, None


# ── self: the restaurant's own normal ──────────────────────────────────────

def _t975(df):
    return _T975.get(int(df), 2.0 if df > 30 else 2.1)


def _by_back(rows, metric, cur_mon):
    """{weeks back: value} for every earlier row that measured `metric`
    (waste only from weeks it was logged regularly)."""
    out = {}
    for r in rows:
        mon = _bm._week_monday(r["week"])
        f = r.get("features") or {}
        v = _num(f.get(metric))
        if mon is None or v is None or cur_mon is None:
            continue
        if metric == "waste_sales_pct_28d" and not _features.waste_logging_regular(f):
            continue
        back = (cur_mon - mon).days // 7
        if back > 0:
            out.setdefault(back, v)
    return out


def _baseline(by_back, at=0):
    """[(weeks back from `at`, value)] of the baseline window before week
    `at`."""
    return [(b - at, by_back[b]) for b in range(at + SELF_GAP_WEEKS, at + SELF_GAP_WEEKS + SELF_BASELINE_WEEKS)
            if b in by_back]


def window_sigma(pairs):
    """(σ, df): one window's standard deviation, read only from rows a whole
    window apart. Rows are grouped by (weeks back mod SELF_WINDOW_WEEKS);
    inside a group no two share a day, so the pooled within-group variance
    is not shrunk by the overlap that made the old MAD band too narrow."""
    phases = {}
    for back, v in pairs:
        phases.setdefault(int(back) % SELF_WINDOW_WEEKS, []).append(float(v))
    ss, df = 0.0, 0
    for vs in phases.values():
        if len(vs) > 1:
            mu = sum(vs) / len(vs)
            ss += sum((v - mu) ** 2 for v in vs)
            df += len(vs) - 1
    if df < 1:
        return None, 0
    return math.sqrt(ss / df), df


def _threshold(sigma, df, backs, step):
    """The gap a verdict must clear: a two-sided 95% t-quantile of the swing
    plus the baseline median's own uncertainty over the independent windows
    the baseline spans."""
    n_eff = max(1.0, (max(backs) - min(backs) + SELF_WINDOW_WEEKS) / float(SELF_WINDOW_WEEKS))
    return max(step or 0.0, _t975(df) * sigma + 1.25 * sigma / math.sqrt(n_eff))


def _own_sigma(metric, rows, today=None):
    """This restaurant's own week-to-week swing on `metric` (one window's σ),
    or None with too little history — what standing() adds to a band's
    uncertainty."""
    cur = rows[-1] if rows else None
    if not cur or cur["week"] < _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS):
        return None
    cur_mon = _bm._week_monday(cur["week"])
    base = _baseline(_by_back(rows[:-1], metric, cur_mon))
    if len(base) < SELF_MIN_POINTS:
        return None
    sigma, df = window_sigma(base)
    return sigma if sigma is not None and df >= SELF_MIN_DF else None


def _self(restaurant_id, metric, rows, today=None, restaurant=None, _memo=None) -> dict:
    """This restaurant against its own normal: the median of its weekly
    figures SELF_GAP_WEEKS..SELF_GAP_WEEKS+SELF_BASELINE_WEEKS weeks back
    (with a year of history, moved by how the same weeks moved last year),
    and a verdict only when the gap clears _threshold() — otherwise "about
    your normal"."""
    m = reg.meta(metric)
    floor = _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS)
    cur = rows[-1] if rows else None
    if not cur or cur["week"] < floor:
        return {"kind": "self", "available": False, "why_not": "no current figure for this restaurant yet"}
    value = _num((cur.get("features") or {}).get(metric))
    if value is None:
        return {"kind": "self", "available": False, "why_not": "not measured yet for this restaurant"}
    if metric == "waste_sales_pct_28d":
        ok, why = own_eligible(restaurant, metric, cur, _memo)
        if not ok:
            return {"kind": "self", "available": False, "why_not": why}
    cur_mon = _bm._week_monday(cur["week"])
    by_back = _by_back(rows[:-1], metric, cur_mon)
    base = _baseline(by_back)
    if len(base) < SELF_MIN_POINTS:
        return {"kind": "self", "available": False, "value": value,
                "why_not": f"needs {SELF_MIN_POINTS} weeks of this restaurant's own history (has {len(base)})"}
    sigma, df = window_sigma(base)
    if sigma is None or df < SELF_MIN_DF:
        return {"kind": "self", "available": False, "value": value,
                "why_not": (f"needs {SELF_MIN_POINTS} weeks of this restaurant's own history, with fewer gaps, "
                            "to tell a change from its normal swing")}
    vals = [v for _b, v in base]
    med = percentile(vals, 50)
    noise = _threshold(sigma, df, [b for b, _v in base], m.get("step"))
    expected, basis, label = med, "recent", f"your own previous {SELF_BASELINE_WEEKS} weeks"
    ly_back = next((b for b in SEASONAL_BACK_WEEKS if b in by_back), None)
    last_year = by_back.get(ly_back) if ly_back is not None else None
    if ly_back is not None:
        ly_base = _baseline(by_back, at=ly_back)
        if len(ly_base) >= SELF_MIN_POINTS:
            # vs this time last year: the recent normal moved by how the
            # same weeks moved from their own normal last year. The gap is
            # a difference of two such readings, so its swing is √2 wider.
            expected = med + (last_year - percentile([v for _b, v in ly_base], 50))
            noise *= math.sqrt(2.0)
            basis = "seasonal"
            label = f"your own previous {SELF_BASELINE_WEEKS} weeks, adjusted for this time last year"
    delta = value - expected
    higher = m.get("better", "higher") == "higher"
    if abs(delta) <= noise:
        verdict = "about your normal"
    else:
        verdict = "better than your normal" if (delta > 0) == higher else "worse than your normal"
    out = {"kind": "self", "available": True, "value": round(value, 3), "baseline": round(expected, 3),
           "baseline_label": label, "basis": basis, "points": len(base),
           "delta": round(delta, 3), "noise_band": round(noise, 3), "sigma": round(sigma, 3), "df": df,
           "verdict": verdict, "week": cur["week"], "as_of": _mdy(cur_mon + timedelta(days=6)) if cur_mon else None}
    if basis == "seasonal":
        out["recent_baseline"] = round(med, 3)
    if last_year is not None:
        out["last_year"] = round(last_year, 3)
    if metric in reg.LABOR_COST_METRICS and restaurant is not None:
        ok, why = own_eligible(restaurant, metric, cur, _memo)
        if not ok:
            # Its own history on the same assumed wage is still a fair
            # "vs your normal"; it says what the figure rests on (R4-8).
            out["basis_note"] = "on Cavnar's assumed $26/hr, not your payroll"
    return out


def own_quality(metric, rows, today=None) -> float | None:
    """This metric's own figure, 0–1, for comparison strength (R3-9): how
    current the row is, times how much data stands behind THIS metric — the
    reviews behind a review measure against four times its floor, else how
    many of the previous four weekly rows measured it."""
    cur = rows[-1] if rows else None
    if not cur:
        return None
    mon = _bm._week_monday(cur["week"])
    age = max(0.0, ((today or date.today()) - mon).days / 7.0 - 1.0) if mon else 0.0
    fresh = max(0.5, 1.0 - 0.5 * age / max(1.0, _bm.MAX_OWN_AGE_WEEKS - 1.0))
    f = cur.get("features") or {}
    if reg.meta(metric).get("source") == "reviews" and _num(f.get("reviews_30d")) is not None:
        amount = math.sqrt(min(1.0, _num(f.get("reviews_30d")) / (4.0 * _features.MIN_REVIEWS_FOR_RATIO)))
    else:
        prev = rows[-5:-1]
        have = sum(1 for r in prev if _num((r.get("features") or {}).get(metric)) is not None)
        amount = 0.5 + 0.5 * (have / 4.0)
    return round(max(0.0, min(1.0, fresh * amount)), 3)


def _own_at(rows, metric, today=None, band_week=None):
    """(value now, value at the band's week, own week, stale) from the rows
    already read — benchmarks._own's rule without a second read of the
    series (R4-14)."""
    floor = _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS)
    cur = rows[-1] if rows else None
    value, own_week, stale = None, None, False
    if cur:
        own_week = cur["week"]
        if cur["week"] >= floor:
            value = (cur.get("features") or {}).get(metric)
        else:
            stale = True
    at_band = None
    if band_week:
        mon = _bm._week_monday(band_week)
        lo = _features.iso_week(mon - timedelta(weeks=3)) if mon else None
        for r in reversed(rows or ()):
            if r["week"] <= band_week and (lo is None or r["week"] >= lo):
                at_band = (r.get("features") or {}).get(metric)
                break
    return value, at_band, own_week, stale


def _own_progress(restaurant_id, metric, db_path, today=None) -> str:
    """How far this restaurant's OWN figure is from its measured floor, in
    words an owner can act on: "about 9 more measured days to a comparison"
    (Benchmarking audit #20, BM2-10)."""
    today = today or date.today()
    src = reg.meta(metric).get("source")
    try:
        conn = get_conn(db_path)
        try:
            if src in ("labor", "inventory", "waste"):
                since = (today - timedelta(days=28)).isoformat()
                if metric in ("labor_pct_28d", "labor_pct_sd_28d"):
                    sql = ("SELECT COUNT(DISTINCT date) FROM labor_daily_history WHERE restaurant_id=? AND date >= ? "
                           "AND labor_pct IS NOT NULL AND sales > 0")
                else:
                    sql = "SELECT COUNT(DISTINCT date) FROM labor_daily_history WHERE restaurant_id=? AND date >= ? AND sales > 0"
                have = int(conn.execute(sql, (restaurant_id, since)).fetchone()[0] or 0)
                if have < _features.MIN_MEASURED_DAYS or src == "labor":
                    need = max(1, _features.MIN_MEASURED_DAYS - have)
                    return f"about {need} more measured day{'s' if need != 1 else ''} to a comparison"
                # Enough sales days: what is missing is the metric's own
                # input, not more days (Benchmarking #29, R2-22, R4-10) — a
                # restaurant with 60 sales days and no count read "about 1
                # more measured day" forever.
                if src == "waste":
                    share, hit = _features.waste_log_regularity(conn, restaurant_id, today)
                    weeks = _features.WASTE_REGULARITY_WEEKS
                    floor = int(math.ceil(_features.WASTE_REGULARITY_MIN * weeks))
                    if share is None:
                        return (f"waste logged in {floor} of {weeks} weeks of inventory history to a comparison "
                                "(the inventory is newer than that)")
                    if hit < floor:
                        more = floor - hit
                        return (f"waste logged in {hit} of the last {weeks} weeks — {more} more week"
                                f"{'s' if more != 1 else ''} with waste logged to a comparison")
                    return "waste and deliveries logged over the last 4 weeks to a comparison"
                return ("two inventory counts about 4 weeks apart, with the deliveries between them logged, "
                        "to a comparison")
            if src == "reviews":
                since = (today - timedelta(days=30)).isoformat()
                have = int(conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND "
                                        "COALESCE(review_date, fetched_at) >= ?", (restaurant_id, since)).fetchone()[0] or 0)
                need = max(1, _features.MIN_REVIEWS_FOR_RATIO - have)
                return f"about {need} more review{'s' if need != 1 else ''} this month to a comparison"
        finally:
            conn.close()
    except Exception:
        pass
    return "not measured yet for this restaurant"


def _blend(metric, band, n, entry):
    """A small group's band shifted toward the published median by
    (1 - n/(n+BLEND_KAPPA)), stated. `entry` is a registry entry with a
    median measured the SAME way (definitions match), else no blend."""
    if not entry or entry.get("median") is None or band.get("p50") is None:
        return band, None
    w = n / float(n + BLEND_KAPPA)
    shift = (1.0 - w) * (float(entry["median"]) - float(band["p50"]))
    out = dict(band)
    for q in ("p25", "p50", "p75"):
        out[q] = _bm.coarse(metric, float(band[q]) + shift)
    import benchmark_registry
    return out, {"group_weight_pct": int(round(w * 100)), "toward": benchmark_registry.cite(entry),
                 "median": entry["median"],
                 "text": (f"blended {100 - int(round(w * 100))}% toward the published median "
                          f"({benchmark_registry.cite(entry)}) because the group is small")}


def _viewer_org(restaurant_id, db_path, memo=None):
    """benchmarks.viewer_org, read once per request (R4-14)."""
    if memo is not None and "viewer_org" in memo:
        return memo["viewer_org"]
    org = _bm.viewer_org(restaurant_id, db_path=db_path)
    if memo is not None:
        memo["viewer_org"] = org
    return org

def _band_kind(kind, restaurant_id, metric, cohort, type_source, db_path, today, rows, entry=None,
               exclude=None, *, level=None, levels=None, _memo=None):
    """A published band (peers: a rung of the restaurant's confirmed
    ladder; platform: every restaurant), the viewer's whole organisation
    taken out (`exclude`: benchmarks.viewer_org, read once by the caller),
    with its standing and strength."""
    row = _bm._row(cohort, metric, db_path=db_path, today=today)
    if not row:
        return {"kind": kind, "available": False,
                "why_not": (f"no current band of at least {_bm.MIN_QUARTILE_N} other "
                            f"{'restaurants on Cavnar' if kind == 'platform' else _bm.cohort_label(cohort).replace(' on Cavnar', '').lower()} "
                            "with this measured yet")}
    v_now, v_at, own_week, own_stale = _own_at(rows, metric, today=today, band_week=row.get("week"))
    if exclude is None:
        exclude = _viewer_org(restaurant_id, db_path, _memo)
    p = _bm.published(cohort, metric, exclude_org=exclude, db_path=db_path, today=today)
    if not p or p.get("withheld"):
        return {"kind": kind, "available": False, "value": v_now,
                "why_not": (p or {}).get("reason") or "the band is withheld"}
    blend = None
    if kind == "peers":
        p, blend = _blend(metric, p, p["n"], entry)
    own_sigma = _own_sigma(metric, rows, today)
    st, margin = standing(v_now, p, metric, p["n"], own_noise=own_sigma)
    platform = kind == "platform"
    out = {"kind": kind, "available": True, "value": v_now, "cohort": cohort,
           "cohort_label": (f"{p['n']} other restaurants on Cavnar, all types" if platform
                            else p["cohort_label"]),
           "type_source": ("platform" if platform else type_source),
           "inferred": kind == "peers" and type_source == "inferred",
           "n": p["n"], "orgs": p.get("orgs"), "min_n": _bm.MIN_QUARTILE_N, "p25": p["p25"], "p50": p["p50"],
           "p75": p["p75"], "week": p["week"], "as_of": p["as_of"], "own_week": own_week, "own_stale": own_stale,
           "standing": st, "outcome": outcome_words(st, metric), "margin": margin,
           "own_noise": None if own_sigma is None else round(own_sigma, 3), "comparable": True, "blend": blend,
           "strength": strength(p["n"], p["week"], type_source, platform=platform, own_stale=own_stale,
                                today=today, orgs=p.get("orgs"),
                                spread=reg.spread_ratio(metric, p["p25"], p["p50"], p["p75"]),
                                own_quality=own_quality(metric, rows, today),
                                similarity=granularity("platform" if platform else cohort, metric, level),
                                level=level, levels=levels)}
    if level is not None:
        out["level"], out["levels"] = level, levels
    # "Measured at k of m" (BM3-8): how many of the group measured this
    # metric, of the most that measured any benchmarked metric that week —
    # both AFTER the viewer's organisation is taken out, so they agree with
    # n (re-audit #44, R1-15). Counts only, never a member.
    try:
        out["measured"] = int(p.get("measured") or p["n"])
        out["members"] = max(out["measured"], _bm.group_size(cohort, row.get("week"), exclude_org=exclude,
                                                             db_path=db_path))
        if p.get("capped"):
            out["capped"] = int(p["capped"])
    except Exception:
        pass
    return out


def _peers(restaurant_id, metric, restaurant, db_path, today, rows, industry_entry=None, _memo=None):
    """The peers kind, down the ladder (fix round #34/#36): the finest group
    on the restaurant's ladder (categories.partition_ladder — its confirmed
    partition narrowed by the ticket band, volume band and market it
    measures, then the partition itself, then the service model alone) whose
    band clears every floor; `level` is the rung used (0 = finest) of
    `levels`, and a group WIDER than the confirmed partition says so in its
    label. Economics metrics stop at the service model; the all-types band
    is the platform kind's, for behaviour metrics only. Or why there is
    none."""
    prof = categories.profile_for(restaurant) if restaurant is not None else None
    if not prof or not prof.get("confirmed"):
        sug = categories.suggestion(restaurant) if restaurant is not None else None
        why = ("this restaurant's profile isn't confirmed, so there is no like-for-like group — confirm it in "
               "Account → Restaurant profile")
        out = {"kind": "peers", "available": False, "why_not": why}
        if sug:
            out["suggestion"] = sug
        return out
    feats = (rows[-1].get("features") or {}) if rows else {}
    ladder, key, why = _bm.peer_ladder(restaurant, metric, feats)
    if not key:
        return {"kind": "peers", "available": False, "why_not": why}
    own_now = None
    if rows and rows[-1]["week"] >= _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS):
        own_now = _num((rows[-1].get("features") or {}).get(metric))
    if own_now is None:
        return {"kind": "peers", "available": False, "cohort": key,
                "cohort_label": _bm.cohort_label(key),
                "why_not": _own_progress(restaurant_id, metric, db_path, today)}
    exclude = _viewer_org(restaurant_id, db_path, _memo)
    base_i = categories.ladder_base_index(ladder, key)
    tried = []
    for level, rung in enumerate(ladder):
        c = _band_kind("peers", restaurant_id, metric, rung, "set", db_path, today, rows, entry=industry_entry,
                       exclude=exclude, level=level, levels=len(ladder), _memo=_memo)
        c["level"], c["levels"] = level, len(ladder)
        if c.get("available"):
            note = categories.rung_note(rung, key)
            if note:
                c["cohort_label"] = f"{c['cohort_label']} ({note})"
                c["wider_than_profile"] = True
            return c
        tried.append(c)
    # Nothing on the ladder clears the floors: the confirmed partition's own
    # reason (it is the group the owner chose).
    out = dict(tried[min(base_i, len(tried) - 1)]) if tried else {"kind": "peers", "available": False}
    out.setdefault("cohort", key)
    return out


def _members(cohort, week, db_path, exclude_org=None):
    """The most restaurants in `cohort` that measured any benchmarked
    metric in `week`, the viewer's organisation (`exclude_org`, an org hash)
    left out — the group's size as the viewer's bands know it. Server-side
    member lists are counted, never returned."""
    import json as _json
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT n, members_json FROM intel_benchmarks WHERE cohort=? AND week=?",
                            (cohort, week)).fetchall()
    finally:
        conn.close()
    best = 0
    for r in rows:
        try:
            members = _json.loads(r["members_json"]) if r["members_json"] else None
        except (TypeError, ValueError):
            members = None
        if members is None:
            continue          # a pre-privacy band: its count cannot be taken without the viewer
        best = max(best, sum(1 for _v, o in members if exclude_org is None or o != exclude_org))
    return best


def _industry(metric, restaurant):
    m = reg.meta(metric)
    key = m.get("industry")
    if not key:
        return {"kind": "industry", "available": False, "why_not": "no published figure for this metric"}
    import benchmark_registry
    try:
        _cat, _src = categories.category_for(restaurant) if restaurant is not None else (None, None)
    except Exception:
        _cat, _src = None, None
    if _src == "inferred":
        # A published figure is quoted for a type the owner confirmed, never
        # one Cavnar guessed (Benchmarking audit #8, BM2 §6 R1).
        return {"kind": "industry", "available": False,
                "why_not": "the restaurant's type was guessed from its name — confirm it to see the published figure"}
    e = benchmark_registry.for_restaurant(key, restaurant)
    if not e:
        return {"kind": "industry", "available": False,
                "why_not": "no published figure for this restaurant's type"}
    # A figure that measures something else is quoted as CONTEXT with its
    # definition, never compared (Benchmarking audit #14, BM3-15): the NRA
    # labor median includes benefits; Cavnar's labor % is wages from shifts.
    # `comparable` False → no standing, no blend, no dollar gap from it.
    differs = benchmark_registry.definitions_differ(e, reg.definition(metric))
    out = {"kind": "industry", "available": True, "source": benchmark_registry.cite(e),
           "source_kind": e.get("source_kind"), "year": e.get("year"), "label": e.get("label"),
           "low": e.get("low"), "high": e.get("high"), "median": e.get("median"),
           "median_basis": e.get("median_basis"), "inferred": bool(e.get("inferred")),
           "type_source": e.get("category_source"), "line": benchmark_registry.line(e, m.get("label")),
           "comparable": not differs, "_entry": e}
    if differs:
        # One wording with the registry's facts; a rule of thumb that does
        # not say what it counts is context too (re-audit #32, R4-5).
        out["definition_note"] = benchmark_registry.definition_note(e, m["label"], reg.definition(metric))
    return out


_LOCATION_DENIED = "shown only to logins that manage locations"


def _location_allowed(viewer) -> bool:
    """The location kind is viewer-scoped and fails CLOSED (re-audit #27,
    R1-22/R4-27): no viewer → not shown; "system" is an explicit server-side
    call; a login needs LOCATION_SWITCH (an admin always may)."""
    if viewer is None:
        return False
    if viewer == "system":
        return True
    if not isinstance(viewer, dict):
        return False
    if viewer.get("is_admin"):
        return True
    try:
        from permissions import has_permission, LOCATION_SWITCH
        return bool(has_permission(viewer, LOCATION_SWITCH))
    except Exception:
        return False


def _location_context(restaurant_id, db_path, today=None) -> dict:
    """The owner's locations, read once per request (R4-14): the siblings
    are every restaurant with the same privacy.org_key as this one (the one
    organisation identity bands use — R4-25), each with its Restaurant row,
    its display name (location_name, else name — R3-17) and its own latest
    feature row read directly (own data: not the cross-restaurant view)."""
    from . import privacy
    conn = get_conn(db_path)
    try:
        me = conn.execute("SELECT id, organization_id, location_group, owner_email FROM restaurants WHERE id=?",
                          (restaurant_id,)).fetchone()
        if not me:
            return {"sibs": []}
        me = dict(me)
        cands = conn.execute(
            "SELECT id, name, location_name, organization_id, location_group, owner_email FROM restaurants "
            "WHERE id=? OR (organization_id IS NOT NULL AND organization_id=?) "
            "OR (COALESCE(location_group,'') != '' AND LOWER(location_group)=LOWER(?)) "
            "OR (COALESCE(owner_email,'') != '' AND LOWER(owner_email)=LOWER(?)) ORDER BY id",
            (restaurant_id, me.get("organization_id"), me.get("location_group") or "",
             me.get("owner_email") or "")).fetchall()
        mine = privacy.org_key(me)
        sibs = [dict(c) for c in cands if privacy.org_key(dict(c)) == mine]
        rows = {}
        if len(sibs) >= LOCATION_MIN:
            floor = _features.iso_week((today or date.today()) - timedelta(weeks=3))
            ids = [s["id"] for s in sibs]
            q = ",".join("?" * len(ids))
            for r in conn.execute(f"SELECT restaurant_id, week, features_json FROM intel_features WHERE "
                                  f"restaurant_id IN ({q}) AND week >= ? ORDER BY week DESC", (*ids, floor)).fetchall():
                if r["restaurant_id"] not in rows:
                    import json as _json
                    rows[r["restaurant_id"]] = {"week": r["week"], "features": _json.loads(r["features_json"] or "{}")}
    finally:
        conn.close()
    out = []
    for s in sibs:
        try:
            rest = _models_mod.get_restaurant(s["id"], db_path=db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(s["id"])
        except Exception:
            rest = None
        out.append({"id": s["id"], "name": s.get("location_name") or s.get("name"), "restaurant": rest,
                    "row": rows.get(s["id"]), "memo": {}})
    return {"sibs": out}


def _location(restaurant_id, metric, restaurant, db_path, viewer=None, ctx=None, today=None):
    """This location against the owner's other locations — the owner's own
    data, so no cross-restaurant floor, but like for like (re-audit #27,
    R2-16): a format or economics metric compares only locations in the same
    confirmed peer partition, and every location's figure must pass
    own_eligible (no $26/hr labor cost, no irregular waste log)."""
    if not _location_allowed(viewer):
        return {"kind": "location", "available": False, "why_not": _LOCATION_DENIED}
    ctx = ctx if ctx is not None else _location_context(restaurant_id, db_path, today)
    sibs = ctx.get("sibs") or []
    if len(sibs) < LOCATION_MIN:
        return {"kind": "location", "available": False, "why_not": "a single location"}
    me = next((s for s in sibs if s["id"] == restaurant_id), None)
    if me is None:
        return {"kind": "location", "available": False, "why_not": "a single location"}
    ok, why = own_eligible(me["restaurant"] if me["restaurant"] is not None else restaurant, metric, me["row"],
                           me["memo"])
    if not ok:
        return {"kind": "location", "available": False, "why_not": why}
    like_for_like = not reg.platform_allowed(metric)
    fam = reg.partition_family(metric)
    fam = "labor" if fam == "staff" else fam

    def key_of(rest):
        try:
            return categories.partition_key(categories.profile_for(rest), fam) if rest is not None else None
        except Exception:
            return None
    my_key = key_of(me["restaurant"] if me["restaurant"] is not None else restaurant) if like_for_like else None
    if like_for_like and not my_key:
        return {"kind": "location", "available": False,
                "why_not": ("confirm each location's profile in Account → Restaurant profile to compare your "
                            "locations like for like")}
    locs, differ = [], 0
    for s in sibs:
        v = _num(((s["row"] or {}).get("features") or {}).get(metric))
        if v is None:
            continue
        if s is not me:
            if not own_eligible(s["restaurant"], metric, s["row"], s["memo"])[0]:
                continue
            if like_for_like and key_of(s["restaurant"]) != my_key:
                differ += 1
                continue
        locs.append({"restaurant_id": s["id"], "name": s["name"], "value": round(v, 3),
                     "this": s is me})
    if len(locs) < LOCATION_MIN or not any(l["this"] for l in locs):
        why = ("your locations serve differently (or a profile isn't confirmed), so they aren't compared on this"
               if differ else f"fewer than {LOCATION_MIN} of your locations have this measured")
        return {"kind": "location", "available": False, "why_not": why}
    higher = reg.meta(metric).get("better", "higher") == "higher"
    locs.sort(key=lambda l: l["value"], reverse=higher)
    rank = next(i for i, l in enumerate(locs) if l["this"]) + 1
    out = {"kind": "location", "available": True, "locations": locs, "rank": rank, "of": len(locs),
           "median": round(percentile([l["value"] for l in locs], 50), 3)}
    if like_for_like:
        out["cohort"], out["cohort_label"] = my_key, categories.partition_label(my_key)
    return out


# ── compare ────────────────────────────────────────────────────────────────

# What a cached comparison depends on besides the feature rows and bands the
# nightly pass reads: the profile (peer partition), the pay rates (cost
# basis), the targets and the organisation (whose figures are taken out). A
# change to any of them misses the cache (re-audit #28, R2-20: a confirmed
# profile read "isn't confirmed" for 36 hours).
_CACHE_INPUTS = ("profile_source", "service_model", "concept", "category", "bar_led", "menu_family",
                 "profile_confirmed_at", "role_rates_json", "hourly_rate", "labor_target_pct",
                 "labor_target_source", "food_cost_target", "food_cost_target_source", "organization_id",
                 "location_group", "owner_email")


def inputs_key(restaurant) -> str | None:
    """A short hash of the restaurant settings a comparison depends on."""
    if restaurant is None:
        return None
    import hashlib
    raw = "|".join(f"{k}={_g(restaurant, k)!r}" for k in _CACHE_INPUTS)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


_OWN_RANKED = ("peers", "platform", "location")


def compare(restaurant_id, metric, *, kinds=None, viewer=None, restaurant=None, db_path=DB_PATH,
            today=None, rows=None, use_cache=False, _memo=None) -> dict:
    """Every comparison for one metric: {version, metric, label, unit,
    better, comparability, own, headline, comparisons[], facts[]}. Never
    raises: an unreadable kind is unavailable with its reason.

    use_cache: read the nightly materialised row (comparison_cache) when it
    is fresh, was computed from the restaurant's current settings
    (inputs_key) and covers `kinds` — never for the viewer-dependent
    `location` kind, which is always computed live.

    Every kind that ranks the restaurant's own figure (peers, platform,
    location) first asks own_eligible(); an ineligible figure is not ranked
    and says why. The published (industry) figure stays as context, marked
    `own_eligible` False."""
    m = reg.meta(metric)
    if not m:
        return {"version": ENGINE_VERSION, "metric": metric, "available": False, "why_not": "unknown metric"}
    want = tuple(kinds or KINDS)
    memo = _memo if _memo is not None else {}
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    ikey = inputs_key(restaurant)
    if use_cache and today is None:
        from . import comparison_cache
        hit = comparison_cache.read(restaurant_id, metric, kinds=want, db_path=db_path, inputs_key=ikey)
        if hit is not None and hit.get("version") == ENGINE_VERSION:
            return hit
    if rows is None:
        rows = _series(restaurant_id, db_path)
    own_row = rows[-1] if rows else None
    elig_ok, elig_why = own_eligible(restaurant, metric, own_row, memo) if restaurant is not None else (True, None)
    comps = []
    for kind in want:
        try:
            if kind in ("platform", "location") and not elig_ok:
                comps.append({"kind": kind, "available": False, "why_not": elig_why, "own_ineligible": True})
            elif kind == "self":
                comps.append(_self(restaurant_id, metric, rows, today, restaurant=restaurant, _memo=memo))
            elif kind == "peers":
                c = _peers(restaurant_id, metric, restaurant, db_path, today, rows,
                           industry_entry=_blend_entry(metric, restaurant), _memo=memo)
                if not elig_ok and (c.get("available") or c.get("cohort")):
                    # The partition is known; the restaurant's own figure is
                    # what cannot be ranked in it.
                    c = {"kind": "peers", "available": False, "why_not": elig_why, "own_ineligible": True,
                         "cohort": c.get("cohort"), "cohort_label": c.get("cohort_label")}
                comps.append(c)
            elif kind == "platform":
                if not reg.platform_allowed(metric):
                    comps.append({"kind": "platform", "available": False,
                                  "why_not": f"{m['label']} depends on the type of restaurant, so an all-types "
                                             "comparison would mislead"})
                else:
                    comps.append(_band_kind("platform", restaurant_id, metric, "platform", None, db_path, today, rows,
                                            _memo=memo))
            elif kind == "industry":
                c = _industry(metric, restaurant)
                if c.get("available") and not elig_ok:
                    c["own_eligible"], c["own_why_not"] = False, elig_why
                comps.append(c)
            elif kind == "location":
                if "location_ctx" not in memo and _location_allowed(viewer):
                    memo["location_ctx"] = _location_context(restaurant_id, db_path, today)
                comps.append(_location(restaurant_id, metric, restaurant, db_path, viewer,
                                       ctx=memo.get("location_ctx"), today=today))
            elif kind == "market":
                # Intel's matched-rival standing, the one market comparison
                # (Benchmarking re-audit #30, R3-19, R4-13).
                import competitor_intel_format as _cif
                comps.append(_cif.market_comparison(restaurant, metric))
        except Exception as e:
            print(f"[benchmark_engine] {kind} for {restaurant_id}/{metric} failed: {e}")
            comps.append({"kind": kind, "available": False, "why_not": "could not be computed right now"})
    by = {c["kind"]: c for c in comps}
    # For a behaviour metric the all-types band is a fair peer set, so it
    # leads before the restaurant's own normal (fix round #34, R3-27: the
    # card said "about your normal" while Ask said "top quarter of all
    # restaurants on Cavnar").
    order = ("peers", "platform", "self", "industry") if reg.platform_allowed(metric) else ("peers", "self", "industry")
    head = next((by[k] for k in order if by.get(k, {}).get("available")), None)
    own_val = None
    if own_row and own_row["week"] >= _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS):
        own_val = _num((own_row.get("features") or {}).get(metric))
    out = {"version": ENGINE_VERSION, "metric": metric, "label": m["label"], "unit": m.get("unit"),
           "better": m.get("better"), "comparability": m.get("comparability"), "module": m.get("module"),
           "own": {"value": own_val, "week": own_row["week"] if own_row else None, "measured": own_val is not None,
                   "eligible": bool(elig_ok), "why_not": elig_why},
           "headline": {"kind": head["kind"], "text": headline_text(m, head)} if head else
                       {"kind": None, "text": no_comparison_text(m, by)},
           "comparisons": [{k: v for k, v in c.items() if not k.startswith("_")} for c in comps],
           "inputs_key": ikey}
    out["facts"] = facts([out], _entries={metric: by.get("industry", {}).get("_entry")})
    return out


def _blend_entry(metric, restaurant):
    """The published entry a small peer group is blended toward: the
    confirmed type's, measured the same way as Cavnar's figure."""
    key = reg.meta(metric).get("industry")
    if not key or restaurant is None:
        return None
    try:
        import benchmark_registry
        if categories.category_for(restaurant)[1] != "set":
            return None
        return benchmark_registry.for_restaurant(key, restaurant, published_only=True,
                                                 definition=reg.definition(metric))
    except Exception:
        return None


def compare_all(restaurant_id, module=None, *, kinds=None, viewer=None, restaurant=None, db_path=DB_PATH,
                today=None, metrics=None) -> list:
    """compare() for every metric of a module (or every registered metric,
    or exactly `metrics`), reading the restaurant's feature history, its
    organisation and its locations once."""
    rows = _series(restaurant_id, db_path)
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    memo = {}
    return [compare(restaurant_id, mt, kinds=kinds, viewer=viewer, restaurant=restaurant, db_path=db_path,
                    today=today, rows=rows, _memo=memo)
            for mt in (metrics if metrics is not None else reg.metrics_for(module))]


# ── words and facts ────────────────────────────────────────────────────────

def headline_text(m, c) -> str:
    label, unit = m["label"], m.get("unit")
    if c["kind"] == "peers":
        s = (f"Compared to {c['n']} other {_lc(c['cohort_label'])} (as of {c['as_of']}), your {label.lower()} is "
             f"{c['standing']}")
        if c.get("blend"):
            s += f" — {c['blend']['text']}"
        if c.get("inferred"):
            s += " — your type was guessed from your name; confirm it to sharpen this"
        return s + "."
    if c["kind"] == "platform":
        return (f"Compared to {c['cohort_label']} (as of {c['as_of']}), your {label.lower()} is "
                f"{c['standing']}.")
    if c["kind"] == "self":
        note = f", {c['basis_note']}" if c.get("basis_note") else ""
        return (f"Your {label.lower()} is {_fmt(c['value'], unit)}{note} — {c['verdict']} "
                f"({_fmt(c['baseline'], unit)} over {c['baseline_label']}).")
    if c["kind"] == "industry":
        return c.get("line") or ""
    return ""


def no_comparison_text(m, by) -> str:
    why = (by.get("peers") or {}).get("why_not") or (by.get("self") or {}).get("why_not") or "not enough data yet"
    return f"No fair comparison for {m['label'].lower()} yet — {why}."


def _own_clause(cm) -> str | None:
    """"this restaurant 33.4% (lower is better)" — the figure every
    standing in the line is about, and which way is better (R3-7)."""
    own = cm.get("own") or {}
    metric = cm.get("metric")
    better = cm.get("better") or reg.meta(metric).get("better")
    direction = f" ({better} is better)" if better in ("higher", "lower") else ""
    if own.get("value") is None:
        return None
    s = f"this restaurant {_fmt(_num(own.get('value')), cm.get('unit'))}{direction}"
    if own.get("eligible") is False and own.get("why_not"):
        s += f" — not ranked against anyone: {own['why_not']}"
    return s


def prompt_lines(comparisons) -> list:
    """The one wording every prompt carries for engine comparisons: the
    restaurant's own figure and which way is better, then who the peers
    are, how many, as of when, how strong, and the standing as better/worse
    words (never a bare "bottom quarter", which on a lower-is-better metric
    means the highest figure — re-audit #17) — or why there is none."""
    out = []
    for cm in comparisons or ():
        if not isinstance(cm, dict) or not cm.get("label"):
            continue
        label, unit = cm["label"], cm.get("unit")
        parts = []
        own_clause = _own_clause(cm)
        for c in cm.get("comparisons") or ():
            if not c.get("available"):
                continue
            k = c["kind"]
            if k in ("peers", "platform"):
                # Who the group is and how it was chosen, how many of it
                # measured this, and how strong the comparison is (BM3-8).
                if k == "platform":
                    who = f"{c['n']} other restaurants on Cavnar, all types"
                    group = ("peer group: every restaurant on Cavnar, all types — a behaviour metric, comparable "
                             "across types; never call it restaurants like yours")
                else:
                    who = f"{c['n']} other {_lc(c['cohort_label'])}"
                    if categories.is_partition(c.get("cohort")):
                        # The owner-confirmed partition (Benchmarking #20).
                        group = (f"peer group: {_lc(c['cohort_label']).replace(' on Cavnar', '')} — split by "
                                 "how they serve (and bar-led, and menu family for food cost), from the profile "
                                 f"the owner confirmed; {c.get('orgs') or 'several'} separate owners")
                    else:
                        tl = categories.label(c.get("cohort")) if c.get("cohort") else c["cohort_label"]
                        group = (f"peer group: restaurants of the same type ({tl}), the type inferred from the "
                                 "restaurant's name, not set by the owner" if c.get("inferred") else
                                 f"peer group: restaurants of the same type ({tl}), the type set by the owner")
                measured = (f"measured at {c['measured']} of {c['members']} in the group; "
                            if c.get("measured") and c.get("members") else "")
                words = outcome_words(c.get("standing"), cm.get("metric")) or c.get("standing")
                s = (f"compared to {who}: {words} (middle {_fmt(c['p50'], unit)}, band "
                     f"{_fmt(c['p25'], unit)}–{_fmt(c['p75'], unit)}; as of {c['as_of']}; {measured}"
                     f"{c['strength']['pct']}% comparison strength; {group})")
                if c.get("blend"):
                    s += f" — {c['blend']['text']}"
                parts.append(s)
            elif k == "self":
                note = f", {c['basis_note']}" if c.get("basis_note") else ""
                parts.append(f"vs {c.get('baseline_label') or f'its own previous {SELF_BASELINE_WEEKS} weeks'}: "
                             f"{_fmt(c['value'], unit)}{note} against {_fmt(c['baseline'], unit)} — {c['verdict']} "
                             f"(normal swing ±{_fmt(c['noise_band'], unit if unit != 'share' else '')})")
            elif k == "industry":
                s = f"published: {c.get('line')}"
                if c.get("comparable") is False:
                    s += (" — context only, measured differently; never rank against it"
                          + (f" ({c['definition_note']})" if c.get("definition_note") else ""))
                elif c.get("own_eligible") is False:
                    s += " — context only; this restaurant's own figure is not ranked against it"
                parts.append(s)
            elif k == "location":
                parts.append(f"ranks {c['rank']} of {c['of']} of the owner's locations")
        if parts:
            if own_clause:
                parts.insert(0, own_clause)
            out.append(f"{label}: " + "; ".join(parts) + ".")
        else:
            out.append(f"{label}: no fair comparison — say so rather than comparing "
                       f"({(cm.get('headline') or {}).get('text') or 'not enough data'}).")
    return out


def facts(comparisons, _entries=None) -> list:
    """response_validation Fact dicts for every figure the comparisons
    carry. Bands and published figures are kind "benchmark" with source
    {source_kind: peers|platform|industry kind, cohort_label, n, min_n,
    as_of, comparable, restaurant_category, strength_pct, inferred}; the
    restaurant's own baseline is kind "computed". A peer claim binds to
    these or is not said."""
    import benchmark_registry
    out = []
    for cm in comparisons or ():
        metric = cm.get("metric")
        unit = {"share": "", "★": "★", "%": "%"}.get(cm.get("unit"), "")
        better = cm.get("better") or reg.meta(metric).get("better")
        own = cm.get("own") or {}
        # The restaurant's own figure — only when it may be ranked (#11, #12).
        own_value = _num(own.get("value")) if own.get("eligible", True) is not False else None
        # The shared contract every benchmark fact carries (engine.facts,
        # benchmark_registry.facts; response_validation reads it).
        common = {"metric": metric, "better": better, "own_value": own_value}
        for c in cm.get("comparisons") or ():
            if not c.get("available"):
                continue
            k = c["kind"]
            if k in ("peers", "platform"):
                src = {"source": "Cavnar anonymous cohort", "source_kind": ("cohort" if k == "peers" else "platform"),
                       "engine_kind": k, "cohort_label": c.get("cohort_label"), "n": c.get("n"),
                       "min_n": c.get("min_n"), "as_of": c.get("as_of"), "comparable": bool(c.get("comparable")),
                       "definition_note": None,
                       "restaurant_category": ("platform" if k == "platform" else c.get("cohort")),
                       "strength_pct": (c.get("strength") or {}).get("pct"), "inferred": bool(c.get("inferred")),
                       "standing": c.get("standing"), "outcome": c.get("outcome"), **common}
                for part in ("p25", "p50", "p75"):
                    if c.get(part) is not None:
                        out.append({"key": f"bench.{metric}.{k}.{part}", "value": float(c[part]), "unit": unit,
                                    "kind": "benchmark", "entity": "cohort", "period": None, "as_of": c.get("as_of"),
                                    "source": dict(src)})
            elif k == "industry":
                e = (_entries or {}).get(metric)
                if not e and c.get("source") and c.get("year"):
                    # A comparison read back from a payload (the entry is
                    # never sent): the same figures, source and year.
                    e = {"metric": reg.meta(metric).get("industry") or metric, "category": "type",
                         "label": c.get("label"), "low": c.get("low"), "high": c.get("high"),
                         "median": c.get("median"), "unit": "%", "short": c.get("source"), "year": c.get("year"),
                         "source_kind": c.get("source_kind"), "inferred": bool(c.get("inferred"))}
                if e:
                    for f in benchmark_registry.facts(e, key_prefix=f"bench.{metric}.industry"):
                        s = f["source"] if isinstance(f.get("source"), dict) else {"source": f.get("source")}
                        s["engine_kind"] = "industry"
                        # The engine's own judgement of the figure wins: not
                        # comparable → context only, never a standing.
                        s["comparable"] = c.get("comparable") is not False
                        s["definition_note"] = c.get("definition_note")
                        s["inferred"] = bool(s.get("inferred") or c.get("inferred"))
                        s.setdefault("standing", None)
                        s.setdefault("strength_pct", None)
                        s.update(common)
                        if not s["comparable"] or c.get("own_eligible") is False:
                            s["own_value"] = None
                        f["source"] = s
                        out.append(f)
            elif k == "self":
                src = {"source_kind": "self", "engine_kind": "self", "comparable": True, "definition_note": None,
                       "inferred": False, "standing": c.get("verdict"), "strength_pct": None, **common}
                src["own_value"] = _num(c.get("value"))
                out.append({"key": f"bench.{metric}.self.baseline", "value": float(c["baseline"]), "unit": unit,
                            "kind": "computed", "entity": "own", "period": None, "as_of": c.get("as_of"),
                            "source": src})
        # The restaurant's own figure as a fact, so a sentence quoting it
        # binds (R3-7).
        if own_value is not None and any(c.get("available") for c in cm.get("comparisons") or ()):
            out.append({"key": f"bench.{metric}.own", "value": float(own_value), "unit": unit, "kind": "computed",
                        "entity": "own", "period": None, "as_of": None,
                        "source": {"source_kind": "own", "engine_kind": "own", "comparable": True,
                                   "definition_note": None, "inferred": False, "standing": None,
                                   "strength_pct": None, **common}})
    return out


# ── the route body ─────────────────────────────────────────────────────────

def payload_for(user, metric=None, module=None, db_path=DB_PATH) -> dict:
    """The body /api/benchmarks and /mobile/api/benchmarks return: the
    login's restaurant, projected by its module view permissions."""
    rid = (user or {}).get("restaurant_id")
    if not rid:
        return {"ok": False, "error": "No restaurant on this login."}
    if metric and not reg.meta(metric):
        return {"ok": False, "error": "Unknown metric."}
    # A staffing ratio (people per $1k) never leaves the server — the
    # borrowed headcount is what a screen sees (staffing.payload; R1-14).
    if metric and str(metric).startswith("staff_per_1k."):
        return {"ok": False, "error": "Unknown metric."}
    # A module alias (food, food_cost) is checked as the permission key it
    # names (inventory): the raw alias is no MODULE_VIEW_PERMISSIONS key, so
    # every non-admin login was refused its own food cost comparisons.
    mods = [reg.meta(metric).get("module")] if metric else ([reg.module_key(module)] if module else None)
    allowed = _visible_modules(user)
    if mods and any(mo and allowed is not None and mo not in allowed for mo in mods if mo):
        return {"ok": False, "error": "This login can't see that module."}
    # Serving (re-audit #28, R2-20/R3-22/R4-14): the modules this login may
    # not see are filtered out BEFORE anything is computed; every other
    # kind is read from the nightly cache when it is fresh and was computed
    # from the restaurant's current settings; the viewer-dependent location
    # kind is computed live, once per request for all metrics.
    if metric:
        wanted = [metric]
    else:
        wanted = [mt for mt in reg.metrics_for(module)
                  if allowed is None or not reg.meta(mt).get("module") or reg.meta(mt).get("module") in allowed]
    from . import comparison_cache
    try:
        restaurant = _models_mod.get_restaurant(rid, db_path=db_path) if db_path != DB_PATH \
            else _models_mod.get_restaurant(rid)
    except Exception:
        restaurant = None
    rows = None
    memo = {}
    comps = []
    for mt in wanted:
        cm = compare(rid, mt, kinds=comparison_cache.CACHED_KINDS, viewer=user, restaurant=restaurant,
                     db_path=db_path, use_cache=True, rows=rows, _memo=memo)
        if cm.get("cached_at") is None and rows is None:
            rows = _series(rid, db_path)       # a miss: read the history once for the rest
        cm = dict(cm, comparisons=list(cm.get("comparisons") or ()))
        try:
            # _location applies own_eligible to this location and each
            # sibling itself, from the rows its context read.
            if "location_ctx" not in memo and _location_allowed(user):
                memo["location_ctx"] = _location_context(rid, db_path)
            loc = _location(rid, mt, restaurant, db_path, user, ctx=memo.get("location_ctx"))
        except Exception as e:
            print(f"[benchmark_engine] location for {rid}/{mt} failed: {e}")
            loc = {"kind": "location", "available": False, "why_not": "could not be computed right now"}
        at = next((i for i, c in enumerate(cm["comparisons"]) if c.get("kind") == "market"), len(cm["comparisons"]))
        cm["comparisons"].insert(at, loc)
        cm.pop("facts", None)
        cm.pop("inputs_key", None)
        comps.append(cm)
    return {"ok": True, "comparisons": comps}


def _visible_modules(user):
    """The permission-module keys this login may view, or None for no
    restriction (admin, or no user)."""
    if user is None or user.get("is_admin"):
        return None
    try:
        from permissions import MODULE_VIEW_PERMISSIONS, has_permission
        return {k for k, perm in MODULE_VIEW_PERMISSIONS.items() if has_permission(user, perm)}
    except Exception:
        return set()
