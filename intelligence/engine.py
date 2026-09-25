"""Level 3: the Benchmark Engine — every comparison a restaurant is shown,
through one function, one vocabulary and one set of facts (Benchmarking
audit 9/24/26, BM4 §4).

Six kinds of comparison, each either available or carrying `why_not`:

  self      compared to this restaurant's own normal — its trailing 13
            weeks (and the same week last year where one exists), judged
            against its own week-to-week swing. Meaningful at any platform
            size, so it is the headline until peers clear their floors.
  peers     compared to restaurants like it: the published band of its
            owner-CONFIRMED peer partition (service model; × bar-led for
            labor and food cost; × menu family for food cost and waste —
            categories.partition_key), the viewer's whole organisation left
            out, at least MIN_QUARTILE_N others from privacy.MIN_ORGS
            organisations, coarse, frozen weekly, at most 8 weeks old. A
            small group is blended toward the published median by
            n/(n+BLEND_KAPPA), and says so. A guessed type compares with no
            one; the restaurant's own figure below its floor reads "about N
            more measured days to a comparison".
  platform  compared to every restaurant on Cavnar — ONLY for behaviour
            metrics (metrics_registry.platform_allowed); an all-types band
            for labor %, food cost % or hours per $1k is never shown.
  industry  a published figure for the restaurant's type (benchmark_registry),
            quoted with its source and year.
  location  compared to the owner's other locations (organization_id) —
            own data, so no privacy floor, and only for logins that may
            switch locations.
  market    nearby competitors, rating only: Intel's matched-rival,
            review-weighted standing with its tie band
            (competitor_intel_format.market_comparison) — the one market
            comparison the Reviews read and Intel both quote.

The headline is peers when available, else self, else industry; `why_not`
is always filled for a kind that cannot be shown — that is how the engine
knows when NOT to benchmark. A comparison-strength percentage (never
words) says how well supported a band comparison is: peer count, band age,
whether the restaurant's type was set or guessed, and the quality of the
restaurant's own figure. A gap inside the band's uncertainty reads "about
the middle", never a quartile word.

facts() turns every figure into a response_validation Fact (kind
"benchmark" for bands and published figures, "computed" for the
restaurant's own baseline), so a peer claim binds to a figure the engine
emitted — or is not said. prompt_lines() is the one wording for prompts.

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
ENGINE_VERSION = 1

# Comparison strength (a % like every confidence in the product).
STRENGTH_FULL_N = 20            # others at which peer count stops raising strength
STRENGTH_FRESH_WEEKS = 2        # a band this young is fully fresh; 0 at MAX_BAND_AGE_WEEKS
INFERRED_TYPE_CAP = 74          # a type Cavnar guessed from the name
PLATFORM_CAP = 60               # an all-types band, even for a behaviour metric
# The restaurant's own normal.
SELF_BASELINE_WEEKS = 13        # weeks of history the normal is read from
SELF_GAP_WEEKS = 4              # the latest weeks' rows overlap the current window
SELF_MIN_POINTS = 6
# "Compared to your other locations" needs this many measured.
LOCATION_MIN = 2
# A small peer group is blended toward the published median with weight
# n / (n + BLEND_KAPPA) on the group (Benchmarking audit #20, BM2 §6): a
# 9-restaurant group is not treated as gospel, and the blend fades as n grows.
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

def standing(value, band, metric, n):
    """(standing, margin) of `value` in a published band: "about the middle"
    whenever the gap to the median sits inside the median's own uncertainty
    (≈ 1.25·IQR/1.35/√n, never under the metric's published step);
    otherwise a quartile word from the better-direction."""
    v = _num(value)
    p25, p50, p75 = _num(band.get("p25")), _num(band.get("p50")), _num(band.get("p75"))
    if v is None or p50 is None or p25 is None or p75 is None:
        return "unmeasured", None
    step = reg.meta(metric).get("step") or 0
    iqr = max(0.0, p75 - p25)
    se = 1.25 * iqr / 1.35 / math.sqrt(max(1, int(n or 1)))
    margin = max(step, se)
    if abs(v - p50) <= margin:
        return "about the middle", round(margin, 3)
    higher = reg.meta(metric).get("better", "higher") == "higher"
    good = v > p50 if higher else v < p50
    far = (v >= p75) if higher else (v <= p25)
    far_bad = (v <= p25) if higher else (v >= p75)
    if good:
        return ("top quarter" if far else "above the middle"), round(margin, 3)
    return ("bottom quarter" if far_bad else "below the middle"), round(margin, 3)


def strength(n, band_week=None, type_source=None, platform=False, own_stale=False, completeness=None,
             today=None) -> dict:
    """The comparison-strength object for a band comparison, in the K1
    shape: {pct, label, dimensions, caps_applied, reason}. The geometric
    mean of the measured dimensions (peer count, band freshness, own-figure
    quality, type similarity), then the caps: a guessed type ≤
    INFERRED_TYPE_CAP, an all-types band ≤ PLATFORM_CAP."""
    dims = {}
    dims["size"] = min(1.0, max(0.0, (n or 0) / float(STRENGTH_FULL_N)))
    age = None
    mon = _bm._week_monday(band_week) if band_week else None
    if mon:
        age = max(0.0, ((today or date.today()) - mon).days / 7.0)
        span = max(1.0, _bm.MAX_BAND_AGE_WEEKS - STRENGTH_FRESH_WEEKS)
        dims["freshness"] = 1.0 if age <= STRENGTH_FRESH_WEEKS else max(0.0, 1.0 - (age - STRENGTH_FRESH_WEEKS) / span)
    own = 0.5 if own_stale else 1.0
    if completeness is not None:
        own *= max(0.0, min(1.0, float(completeness)))
    dims["own"] = own
    dims["similarity"] = 0.6 if platform else (0.74 if type_source == "inferred" else 1.0)
    vals = [v for v in dims.values() if v is not None]
    if not vals or any(v <= 0 for v in vals):
        pct = 0
    else:
        pct = 100.0 * math.exp(sum(math.log(v) for v in vals) / len(vals))
    caps = []
    if type_source == "inferred" and pct > INFERRED_TYPE_CAP:
        pct, caps = float(INFERRED_TYPE_CAP), caps + ["inferred_type"]
    if platform and pct > PLATFORM_CAP:
        pct, caps = float(PLATFORM_CAP), caps + ["all_types"]
    pct = int(round(pct))
    weakest = min(dims, key=lambda k: dims[k])
    reason = {"size": f"{n} other restaurants — {STRENGTH_FULL_N} makes a full comparison",
              "freshness": "the band is several weeks old",
              "own": "this restaurant's own figure is incomplete or old",
              "similarity": ("an all-types band" if platform else "the type was guessed from the name")}[weakest]
    return {"pct": pct, "label": f"{pct}% comparison strength", "dimensions": {k: round(v, 3) for k, v in dims.items()},
            "caps_applied": caps, "reason": reason,
            "meaning": "How well supported this comparison is — not how well you are doing"}


# ── the kinds ──────────────────────────────────────────────────────────────

def _series(restaurant_id, db_path, weeks=60):
    try:
        return _features.series(restaurant_id, weeks=weeks, db_path=db_path)
    except Exception:
        return []


def _self(restaurant_id, metric, rows, today=None) -> dict:
    """This restaurant against its own normal: the median of its weekly
    figures SELF_GAP_WEEKS..SELF_GAP_WEEKS+SELF_BASELINE_WEEKS weeks back,
    with a normal swing of 1.4826 × the median absolute deviation (never
    under the metric's step). "about your normal" inside that swing."""
    m = reg.meta(metric)
    floor = _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS)
    cur = rows[-1] if rows else None
    if not cur or cur["week"] < floor:
        return {"kind": "self", "available": False, "why_not": "no current figure for this restaurant yet"}
    value = _num((cur.get("features") or {}).get(metric))
    if value is None:
        return {"kind": "self", "available": False, "why_not": "not measured yet for this restaurant"}
    cur_mon = _bm._week_monday(cur["week"])
    base = []
    last_year = None
    for r in rows[:-1]:
        mon = _bm._week_monday(r["week"])
        v = _num((r.get("features") or {}).get(metric))
        if mon is None or v is None or cur_mon is None:
            continue
        back = (cur_mon - mon).days // 7
        if SELF_GAP_WEEKS <= back < SELF_GAP_WEEKS + SELF_BASELINE_WEEKS:
            base.append(v)
        if 51 <= back <= 53 and last_year is None:
            last_year = v
    if len(base) < SELF_MIN_POINTS:
        return {"kind": "self", "available": False, "value": value,
                "why_not": f"needs {SELF_MIN_POINTS} weeks of this restaurant's own history (has {len(base)})"}
    med = percentile(base, 50)
    mad = percentile([abs(x - med) for x in base], 50)
    noise = max(m.get("step") or 0, 1.4826 * mad)
    delta = value - med
    higher = m.get("better", "higher") == "higher"
    if abs(delta) <= noise:
        verdict = "about your normal"
    else:
        verdict = "better than your normal" if (delta > 0) == higher else "worse than your normal"
    out = {"kind": "self", "available": True, "value": round(value, 3), "baseline": round(med, 3),
           "baseline_label": f"your own previous {SELF_BASELINE_WEEKS} weeks", "points": len(base),
           "delta": round(delta, 3), "noise_band": round(noise, 3), "verdict": verdict,
           "week": cur["week"], "as_of": _mdy(cur_mon + timedelta(days=6)) if cur_mon else None}
    if last_year is not None:
        out["last_year"] = round(last_year, 3)
    return out


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
                need = max(1, _features.MIN_MEASURED_DAYS - have)
                return f"about {need} more measured day{'s' if need != 1 else ''} to a comparison"
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


def _band_kind(kind, restaurant_id, metric, cohort, type_source, db_path, today, rows, entry=None):
    """A published band (peers: the restaurant's confirmed partition;
    platform: every restaurant), the viewer's whole organisation taken out,
    with its standing and strength."""
    row = _bm._row(cohort, metric, db_path=db_path, today=today)
    if not row:
        return {"kind": kind, "available": False,
                "why_not": (f"no current band of at least {_bm.MIN_QUARTILE_N} other "
                            f"{'restaurants on Cavnar' if kind == 'platform' else _bm.cohort_label(cohort).replace(' on Cavnar', '').lower()} "
                            "with this measured yet")}
    v_now, v_at, own_week, own_stale = _bm._own(restaurant_id, metric, db_path=db_path, today=today,
                                               band_week=row.get("week"))
    p = _bm.published(cohort, metric, exclude_org=_bm.viewer_org(restaurant_id, db_path=db_path),
                      db_path=db_path, today=today)
    if not p or p.get("withheld"):
        return {"kind": kind, "available": False, "value": v_now,
                "why_not": (p or {}).get("reason") or "the band is withheld"}
    blend = None
    if kind == "peers":
        p, blend = _blend(metric, p, p["n"], entry)
    st, margin = standing(v_now, p, metric, p["n"])
    completeness = (rows[-1].get("completeness") if rows else None)
    out = {"kind": kind, "available": True, "value": v_now, "cohort": cohort,
           "cohort_label": (f"{p['n']} other restaurants on Cavnar, all types" if kind == "platform"
                            else p["cohort_label"]),
           "type_source": ("platform" if kind == "platform" else type_source),
           "inferred": kind == "peers" and type_source == "inferred",
           "n": p["n"], "orgs": p.get("orgs"), "min_n": _bm.MIN_QUARTILE_N, "p25": p["p25"], "p50": p["p50"],
           "p75": p["p75"], "week": p["week"], "as_of": p["as_of"], "own_week": own_week, "own_stale": own_stale,
           "standing": st, "margin": margin, "comparable": True, "blend": blend,
           "strength": strength(p["n"], p["week"], type_source, platform=(kind == "platform"),
                                own_stale=own_stale, completeness=completeness, today=today)}
    # "Measured at k of m" (BM3-8, workstream V): how many of the group
    # measured this metric that week, of the most that measured any
    # benchmarked metric — counts only, never a member.
    try:
        out["measured"] = int(row.get("n") or 0)
        out["members"] = max(out["measured"], _members(cohort, row.get("week"), db_path))
    except Exception:
        pass
    return out


def _peers(restaurant_id, metric, restaurant, db_path, today, rows, industry_entry=None):
    """The peers kind: the owner-confirmed partition or why there is none."""
    prof = categories.profile_for(restaurant) if restaurant is not None else None
    if not prof or not prof.get("confirmed"):
        sug = categories.suggestion(restaurant) if restaurant is not None else None
        why = ("this restaurant's profile isn't confirmed, so there is no like-for-like group — confirm it in "
               "Account → Restaurant profile")
        out = {"kind": "peers", "available": False, "why_not": why}
        if sug:
            out["suggestion"] = sug
        return out
    fam = reg.partition_family(metric)
    key = categories.partition_key(prof, "labor" if fam == "staff" else fam)
    if not key:
        return {"kind": "peers", "available": False,
                "why_not": "set the restaurant's concept in Account → Restaurant profile to compare food cost"}
    own_now = None
    if rows and rows[-1]["week"] >= _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS):
        own_now = _num((rows[-1].get("features") or {}).get(metric))
    if own_now is None:
        return {"kind": "peers", "available": False, "cohort": key,
                "cohort_label": _bm.cohort_label(key),
                "why_not": _own_progress(restaurant_id, metric, db_path, today)}
    return _band_kind("peers", restaurant_id, metric, key, "set", db_path, today, rows, entry=industry_entry)


def _members(cohort, week, db_path):
    """The most restaurants in `cohort` that measured any benchmarked
    metric in `week` — the group's size as the stored bands know it."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT MAX(n) AS m FROM intel_benchmarks WHERE cohort=? AND week=?",
                         (cohort, week)).fetchone()
    finally:
        conn.close()
    return int((r["m"] if r else 0) or 0)


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
        out["definition_note"] = (f"The published figure is the {e.get('median_basis') or 'published figure'}; "
                                  f"this restaurant's {m['label'].lower()} is measured differently, so it is "
                                  "context, not a like-for-like comparison.")
    return out


def _location(restaurant_id, metric, restaurant, db_path, viewer=None):
    """This location against the owner's other locations (organization_id)
    — the owner's own data, so no cross-restaurant floor."""
    if viewer is not None and not viewer.get("is_admin"):
        try:
            from permissions import has_permission, LOCATION_SWITCH
            if not has_permission(viewer, LOCATION_SWITCH):
                return {"kind": "location", "available": False, "why_not": "shown only to logins that manage locations"}
        except Exception:
            return {"kind": "location", "available": False, "why_not": "shown only to logins that manage locations"}
    # organization_id is a column the Restaurant dataclass does not carry.
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT organization_id FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        org = row["organization_id"] if row else None
        sibs = (conn.execute("SELECT id, name FROM restaurants WHERE organization_id=? ORDER BY id", (org,)).fetchall()
                if org else [])
    finally:
        conn.close()
    if not org:
        return {"kind": "location", "available": False, "why_not": "a single location"}
    if len(sibs) < LOCATION_MIN:
        return {"kind": "location", "available": False, "why_not": "a single location"}
    latest = _features.latest_by_restaurant(db_path=db_path)
    locs = []
    for s in sibs:
        v = _num(((latest.get(s["id"]) or {}).get("features") or {}).get(metric))
        if v is not None:
            locs.append({"restaurant_id": s["id"], "name": s["name"], "value": round(v, 3),
                         "this": s["id"] == restaurant_id})
    if len(locs) < LOCATION_MIN or not any(l["this"] for l in locs):
        return {"kind": "location", "available": False,
                "why_not": f"fewer than {LOCATION_MIN} of your locations have this measured"}
    higher = reg.meta(metric).get("better", "higher") == "higher"
    locs.sort(key=lambda l: l["value"], reverse=higher)
    rank = next(i for i, l in enumerate(locs) if l["this"]) + 1
    return {"kind": "location", "available": True, "locations": locs, "rank": rank, "of": len(locs),
            "median": round(percentile([l["value"] for l in locs], 50), 3)}


# ── compare ────────────────────────────────────────────────────────────────

def compare(restaurant_id, metric, *, kinds=None, viewer=None, restaurant=None, db_path=DB_PATH,
            today=None, rows=None, use_cache=False) -> dict:
    """Every comparison for one metric: {version, metric, label, unit,
    better, comparability, own, headline, comparisons[], facts[]}. Never
    raises: an unreadable kind is unavailable with its reason.

    use_cache: read the nightly materialised row (comparison_cache) when it
    is fresh and covers `kinds` — never for the viewer-dependent `location`
    kind, which is always computed live."""
    m = reg.meta(metric)
    if not m:
        return {"version": ENGINE_VERSION, "metric": metric, "available": False, "why_not": "unknown metric"}
    want = tuple(kinds or KINDS)
    if use_cache and today is None:
        from . import comparison_cache
        hit = comparison_cache.read(restaurant_id, metric, kinds=want, db_path=db_path)
        if hit is not None and hit.get("version") == ENGINE_VERSION:
            return hit
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    if rows is None:
        rows = _series(restaurant_id, db_path)
    comps = []
    for kind in want:
        try:
            if kind == "self":
                comps.append(_self(restaurant_id, metric, rows, today))
            elif kind == "peers":
                comps.append(_peers(restaurant_id, metric, restaurant, db_path, today, rows,
                                    industry_entry=_blend_entry(metric, restaurant)))
            elif kind == "platform":
                if not reg.platform_allowed(metric):
                    comps.append({"kind": "platform", "available": False,
                                  "why_not": f"{m['label']} depends on the type of restaurant, so an all-types "
                                             "comparison would mislead"})
                else:
                    comps.append(_band_kind("platform", restaurant_id, metric, "platform", None, db_path, today, rows))
            elif kind == "industry":
                comps.append(_industry(metric, restaurant))
            elif kind == "location":
                comps.append(_location(restaurant_id, metric, restaurant, db_path, viewer))
            elif kind == "market":
                # Intel's matched-rival standing, the one market comparison
                # (Benchmarking re-audit #30, R3-19, R4-13).
                import competitor_intel_format as _cif
                comps.append(_cif.market_comparison(restaurant, metric))
        except Exception as e:
            print(f"[benchmark_engine] {kind} for {restaurant_id}/{metric} failed: {e}")
            comps.append({"kind": kind, "available": False, "why_not": "could not be computed right now"})
    by = {c["kind"]: c for c in comps}
    head = next((by[k] for k in ("peers", "self", "industry") if by.get(k, {}).get("available")), None)
    own_row = rows[-1] if rows else None
    own_val = None
    if own_row and own_row["week"] >= _bm._week_floor(today, _bm.MAX_OWN_AGE_WEEKS):
        own_val = _num((own_row.get("features") or {}).get(metric))
    out = {"version": ENGINE_VERSION, "metric": metric, "label": m["label"], "unit": m.get("unit"),
           "better": m.get("better"), "comparability": m.get("comparability"), "module": m.get("module"),
           "own": {"value": own_val, "week": own_row["week"] if own_row else None, "measured": own_val is not None},
           "headline": {"kind": head["kind"], "text": headline_text(m, head)} if head else
                       {"kind": None, "text": no_comparison_text(m, by)},
           "comparisons": [{k: v for k, v in c.items() if not k.startswith("_")} for c in comps]}
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
                today=None) -> list:
    """compare() for every metric of a module (or every registered metric),
    reading the restaurant's feature history once."""
    rows = _series(restaurant_id, db_path)
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    return [compare(restaurant_id, mt, kinds=kinds, viewer=viewer, restaurant=restaurant, db_path=db_path,
                    today=today, rows=rows) for mt in reg.metrics_for(module)]


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
    if c["kind"] == "self":
        return (f"Your {label.lower()} is {_fmt(c['value'], unit)} — {c['verdict']} "
                f"({_fmt(c['baseline'], unit)} over {c['baseline_label']}).")
    if c["kind"] == "industry":
        return c.get("line") or ""
    return ""


def no_comparison_text(m, by) -> str:
    why = (by.get("peers") or {}).get("why_not") or (by.get("self") or {}).get("why_not") or "not enough data yet"
    return f"No fair comparison for {m['label'].lower()} yet — {why}."


def prompt_lines(comparisons) -> list:
    """The one wording every prompt carries for engine comparisons: who the
    peers are, how many, as of when, how strong — or why there is none."""
    out = []
    for cm in comparisons or ():
        if not isinstance(cm, dict) or not cm.get("label"):
            continue
        label, unit = cm["label"], cm.get("unit")
        parts = []
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
                s = (f"compared to {who}: {c['standing']} (middle {_fmt(c['p50'], unit)}, band "
                     f"{_fmt(c['p25'], unit)}–{_fmt(c['p75'], unit)}; as of {c['as_of']}; {measured}"
                     f"{c['strength']['pct']}% comparison strength; {group})")
                if c.get("blend"):
                    s += f" — {c['blend']['text']}"
                parts.append(s)
            elif k == "self":
                parts.append(f"vs its own previous {SELF_BASELINE_WEEKS} weeks: {_fmt(c['value'], unit)} against "
                             f"{_fmt(c['baseline'], unit)} — {c['verdict']} (normal swing ±{_fmt(c['noise_band'], unit if unit != 'share' else '')})")
            elif k == "industry":
                parts.append(f"published: {c.get('line')}")
            elif k == "location":
                parts.append(f"ranks {c['rank']} of {c['of']} of the owner's locations")
        if parts:
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
        for c in cm.get("comparisons") or ():
            if not c.get("available"):
                continue
            k = c["kind"]
            if k in ("peers", "platform"):
                src = {"source": "Cavnar anonymous cohort", "source_kind": ("cohort" if k == "peers" else "platform"),
                       "engine_kind": k, "cohort_label": c.get("cohort_label"), "n": c.get("n"),
                       "min_n": c.get("min_n"), "as_of": c.get("as_of"), "comparable": bool(c.get("comparable")),
                       "restaurant_category": ("platform" if k == "platform" else c.get("cohort")),
                       "strength_pct": (c.get("strength") or {}).get("pct"), "inferred": bool(c.get("inferred")),
                       "standing": c.get("standing")}
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
                        f["source"]["engine_kind"] = "industry"
                        out.append(f)
            elif k == "self":
                out.append({"key": f"bench.{metric}.self.baseline", "value": float(c["baseline"]), "unit": unit,
                            "kind": "computed", "entity": "own", "period": None, "as_of": c.get("as_of"),
                            "source": {"source_kind": "self", "engine_kind": "self"}})
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
    # A module alias (food, food_cost) is checked as the permission key it
    # names (inventory): the raw alias is no MODULE_VIEW_PERMISSIONS key, so
    # every non-admin login was refused its own food cost comparisons.
    mods = [reg.meta(metric).get("module")] if metric else ([reg.module_key(module)] if module else None)
    allowed = _visible_modules(user)
    if mods and any(mo and allowed is not None and mo not in allowed for mo in mods if mo):
        return {"ok": False, "error": "This login can't see that module."}
    if metric:
        comps = [compare(rid, metric, viewer=user, db_path=db_path)]
    else:
        comps = [c for c in compare_all(rid, module=module, viewer=user, db_path=db_path)
                 if allowed is None or not c.get("module") or c.get("module") in allowed]
    for c in comps:
        c.pop("facts", None)
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
