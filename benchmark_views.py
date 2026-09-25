"""benchmark_views.py — the owner's screens onto the Benchmark Engine.

The engine (intelligence.engine) decides every comparison: which kinds are
fair, the headline (peers, else the restaurant's own normal, else the
published figure), the standing, the comparison strength and why a kind
cannot be shown. This module only shapes what it returns for the screens
(Benchmarking audit 9/24/26, Top-50 #23, #18, #19; BM3-14, BM1-23, BM4-10,
BM4-11):

  card(user, module)       the "How you compare" card on Labor, Food Cost,
                           Reviews and Marketing — who the restaurant is
                           compared to, how many, as of when, the
                           comparison strength % with its Why? rows, the
                           standing per metric, one action for each metric
                           it is behind on, and an honest below-the-minimum
                           state that falls back to the restaurant's own
                           previous 13 weeks (#18).
  strip(user)              the compact Home line: the same rows across the
                           modules the login can see, behind-first.
  location_compare(user)   the group Home's location-to-location table (the
                           engine's `location` kind): each location's figure
                           against its own normal first, then against the
                           owner's other locations, a gap called only when it
                           is wider than both locations' own week-to-week
                           swing (#19).
  rank_by_rating(entries)  the group "strongest / weakest" read (home_brief's
                           group brief, reporter's group digest): the
                           platform's rating floor, each location against its
                           own baseline first, and a name only for a gap
                           beyond noise.

Nothing here computes a comparison of its own. Comparison strength and
confidence are percentages, never words; dates are M/D/YY (the engine's
as_of). Level 2: reads the engine through the intelligence facade.
"""
from models import DB_PATH

# Screen module → the engine's module key (the permission key, which the
# engine's payload projects by: "inventory" is Food Cost).
MODULES = {"labor": "labor", "food_cost": "inventory", "inventory": "inventory", "food": "inventory",
           "reviews": "reviews", "marketing": "marketing"}
# Where a Home row's "Open" goes (the web's data-open-module / iOS tab).
OPEN_MODULE = {"labor": "labor", "inventory": "inventory", "reviews": "reviews", "marketing": "marketing"}
# The Home strip shows at most this many rows.
STRIP_MAX = 6
# The group location table's metrics, in the order an owner reads them.
LOCATION_METRICS = ("avg_rating_30d", "reply_rate_30d", "labor_pct_28d", "labor_hours_per_1k_28d",
                    "food_cost_pct_28d")
# "Weakest" is named only when two ratings differ by this many standard
# errors of their difference — the ordinary bar for "more than noise"
# (models._RATING_SIGNIFICANT_Z).
RANK_Z = 2.0

_BEHIND = ("below the middle", "bottom quarter")
_AHEAD = ("top quarter", "above the middle")


def fmt(value, unit) -> str:
    """One figure in the metric's unit: 4.3★, 31.2%, 82% (a share), 2.1 h."""
    if value is None:
        return "—"
    v = float(value)
    if unit == "share":
        return f"{round(v * 100)}%"
    if unit == "%":
        return f"{round(v, 1):g}%"
    if unit == "★":
        return f"{v:.1f}★"
    if unit == "h":
        return f"{round(v, 1):g} h"
    if unit == "pts":
        return f"{round(v, 1):g} pts"
    return f"{round(v, 2):g}"


def _by_kind(cm) -> dict:
    return {c.get("kind"): c for c in (cm.get("comparisons") or ())}


# ── the comparison strength, as the Why? panel's rows ──────────────────────

_STRENGTH_ROWS = (
    ("size", "Peer count"),
    ("freshness", "Band freshness"),
    ("own", "Your own figure"),
    ("similarity", "Type match"),
)


def strength_detail(c) -> dict | None:
    """The engine's comparison strength for a band comparison (peers or
    platform) in the Why? panel's shape — the K1 rows the confidence drawer
    draws: {pct, label, reason, meaning, caps_applied, rows: [{key, title,
    pct, basis}], footer}. None for a kind with no band (self, industry)."""
    s = (c or {}).get("strength")
    if not s or s.get("pct") is None:
        return None
    dims = s.get("dimensions") or {}
    n = c.get("n")
    try:
        from intelligence import engine as _eng
        full_n = _eng.STRENGTH_FULL_N
        inferred_cap, platform_cap = _eng.INFERRED_TYPE_CAP, _eng.PLATFORM_CAP
    except Exception:
        full_n, inferred_cap, platform_cap = 20, 74, 60
    basis = {
        "size": f"{n} other restaurants measured this — {full_n} makes a full comparison",
        "freshness": f"the group's figures as of {c.get('as_of') or '—'}",
        "own": ("your own figure is incomplete or older than the group's" if (dims.get("own") or 1) < 1
                else "your own figure is complete and current"),
        "similarity": ("every type of restaurant on Cavnar — a behaviour measure, comparable across types"
                       if c.get("kind") == "platform" else
                       ("your type was guessed from your name — confirm it in Account to sharpen this"
                        if c.get("inferred") else "your type, as you set it")),
    }
    rows = []
    for key, title in _STRENGTH_ROWS:
        v = dims.get(key)
        rows.append({"key": key, "title": title,
                     "pct": None if v is None else int(round(max(0.0, min(1.0, float(v))) * 100)),
                     "basis": basis[key]})
    caps = list(s.get("caps_applied") or ())
    footer = "The overall figure combines all four — the weakest pulls it down most."
    if "inferred_type" in caps or c.get("inferred"):
        footer += f" A type guessed from the name holds it at {inferred_cap}% or below."
    if "all_types" in caps or c.get("kind") == "platform":
        footer += f" A comparison across every type holds it at {platform_cap}% or below."
    return {"pct": int(s["pct"]), "label": s.get("label") or f"{int(s['pct'])}% comparison strength",
            "reason": s.get("reason"), "meaning": s.get("meaning"), "caps_applied": caps,
            "rows": rows, "footer": footer}


# ── one metric's row ───────────────────────────────────────────────────────

def _ask(label, words) -> str:
    return f"My {label.lower()} is {words}. What should I change first?"


def metric_row(cm) -> dict | None:
    """One metric's row on the card, from its engine comparison: the value,
    which kind the standing is read against, the standing in words, its tone
    (good / neutral / warn — never red: a standing is not an emergency),
    whether the restaurant is behind, and for a metric it is behind on one
    action. None when the restaurant has no figure for the metric or the
    engine has no comparison for it."""
    head = (cm.get("headline") or {}).get("kind")
    by = _by_kind(cm)
    label, unit = cm.get("label") or cm.get("metric"), cm.get("unit")
    own = (cm.get("own") or {}).get("value")
    row = {"metric": cm.get("metric"), "label": label, "unit": unit, "module": cm.get("module"),
           "kind": head, "value": own, "value_text": fmt(own, unit), "standing": None, "tone": "neutral",
           "behind": False, "text": (cm.get("headline") or {}).get("text"), "as_of": None,
           "strength_pct": None, "note": None, "action": None}
    if head == "peers":
        c = by["peers"]
        st = c.get("standing")
        row.update(standing=st, as_of=c.get("as_of"), strength_pct=(c.get("strength") or {}).get("pct"),
                   value=c.get("value"), value_text=fmt(c.get("value"), unit),
                   against=f"{c.get('n')} other {c.get('cohort_label')}",
                   middle_text=fmt(c.get("p50"), unit))
        row["tone"] = "good" if st in _AHEAD else ("warn" if st in _BEHIND else "neutral")
        row["behind"] = st in _BEHIND
    elif head == "self":
        c = by["self"]
        v = c.get("verdict")
        row.update(standing=v, as_of=c.get("as_of"), value=c.get("value"), value_text=fmt(c.get("value"), unit),
                   against=c.get("baseline_label"), middle_text=fmt(c.get("baseline"), unit))
        row["tone"] = "good" if v and v.startswith("better") else ("warn" if v and v.startswith("worse") else "neutral")
        row["behind"] = bool(v and v.startswith("worse"))
    elif head == "industry":
        c = by["industry"]
        if own is None:
            return None
        lo, hi = c.get("low"), c.get("high")
        higher = cm.get("better") == "higher"
        try:
            import cogs
            name = cogs.band_name(c) or "published band"
        except Exception:
            name = "published band"
        if lo is None or hi is None:
            return None
        if (own < lo and not higher) or (own > hi and higher):
            st, tone = f"better than the {name}", "good"
        elif lo <= own <= hi:
            st, tone = f"within the {name}", "neutral"
        else:
            st, tone = f"outside the {name}, on the wrong side", "warn"
        row.update(standing=st, tone=tone, behind=tone == "warn", against=c.get("label"),
                   middle_text=f"{fmt(lo, unit)}–{fmt(hi, unit)}", source=c.get("source"))
        if cm.get("metric") == "labor_pct_28d":
            # BM3-15: not like for like — say so wherever it is shown.
            row["note"] = ("The published figure includes benefits; yours is wages from your shifts, "
                           "so the two are not the same measure.")
    else:
        return None
    if own is None and row.get("value") is None:
        return None
    if row["behind"]:
        row["action"] = {"label": "Ask what to change", "ask": _ask(label, f"{row['standing']} "
                                                                  f"({row['value_text']})")}
    return row


def _who(rows, cms) -> dict:
    """Who the card compares the restaurant to — the headline kind the most
    rows share, peers first: {kind, text, n, as_of}."""
    kinds = [r["kind"] for r in rows]
    by_metric = {cm.get("metric"): cm for cm in cms}
    if "peers" in kinds:
        r = next(r for r in rows if r["kind"] == "peers")
        c = _by_kind(by_metric[r["metric"]])["peers"]
        return {"kind": "peers", "text": f"Compared to {c.get('n')} other {c.get('cohort_label')}",
                "n": c.get("n"), "as_of": c.get("as_of"), "inferred": bool(c.get("inferred"))}
    if "self" in kinds:
        r = next(r for r in rows if r["kind"] == "self")
        c = _by_kind(by_metric[r["metric"]])["self"]
        return {"kind": "self", "text": f"vs {c.get('baseline_label') or 'your own previous 13 weeks'}",
                "n": c.get("points"), "as_of": c.get("as_of"), "inferred": False}
    if "industry" in kinds:
        r = next(r for r in rows if r["kind"] == "industry")
        c = _by_kind(by_metric[r["metric"]])["industry"]
        return {"kind": "industry", "text": f"published: {c.get('source')}", "n": None,
                "as_of": str(c.get("year") or "") or None, "inferred": bool(c.get("inferred"))}
    return {"kind": None, "text": None, "n": None, "as_of": None, "inferred": False}


def _below_minimum(cms, who) -> dict | None:
    """The honest state when no like-for-like group clears its minimum:
    what the card falls back to, and the engine's own reason."""
    if who.get("kind") == "peers":
        return None
    why = next((c.get("why_not") for cm in cms for c in (cm.get("comparisons") or ())
                if c.get("kind") == "peers" and not c.get("available") and c.get("why_not")), None)
    if who.get("kind") == "self":
        text = "Not enough restaurants like yours yet — here's how you compare to your own last 13 weeks."
    elif who.get("kind") == "industry":
        text = "Not enough restaurants like yours yet — here's the published figure for your type."
    else:
        self_why = next((c.get("why_not") for cm in cms for c in (cm.get("comparisons") or ())
                         if c.get("kind") == "self" and not c.get("available") and c.get("why_not")), None)
        text = ("Not enough restaurants like yours yet, and not enough of your own history to compare "
                "against" + (f" — {self_why}" if self_why else "") + ".")
    return {"text": text, "why_not": why}


def build(cms, scope="module", module=None) -> dict:
    """The card (or the Home strip) from engine comparisons."""
    rows = [r for r in (metric_row(cm) for cm in cms or ()) if r]
    who = _who(rows, cms or ())
    strength = None
    if who["kind"] == "peers":
        first = next(r for r in rows if r["kind"] == "peers")
        cm = next(c for c in cms if c.get("metric") == first["metric"])
        strength = strength_detail(_by_kind(cm)["peers"])
    unmeasured = sum(1 for cm in cms or () if not (cm.get("own") or {}).get("measured"))
    if scope == "home":
        rows.sort(key=lambda r: (0 if r["behind"] else (1 if r["tone"] == "good" else 2)))
        rows = rows[:STRIP_MAX]
        for r in rows:
            r["open_module"] = OPEN_MODULE.get(r.get("module"))
    out = {"ok": True, "scope": scope, "module": module, "who": who, "strength": strength, "rows": rows,
           "below_minimum": _below_minimum(cms or (), who), "unmeasured": unmeasured,
           "behind": sum(1 for r in rows if r["behind"])}
    if not rows:
        first = next(iter(cms or ()), None)
        out["empty"] = ((first or {}).get("headline") or {}).get("text") or \
            "Nothing to compare yet — this fills in as your own weeks of data build up."
    return out


def card(user, module=None, db_path=DB_PATH) -> dict:
    """GET /api/benchmarks/card?module= (and ?scope=home) body. Projected by
    the login's module view permissions, as the engine's own payload is."""
    from intelligence import engine
    if module in (None, "", "home"):
        payload = engine.payload_for(user, db_path=db_path)
        scope, mod = "home", None
    else:
        mod = MODULES.get(str(module).lower())
        if not mod:
            return {"ok": False, "error": "Unknown module."}
        payload = engine.payload_for(user, module=mod, db_path=db_path)
        scope = "module"
    if not payload.get("ok"):
        return payload
    return build(payload.get("comparisons") or [], scope=scope, module=mod)


def strip(user, db_path=DB_PATH) -> dict:
    return card(user, None, db_path=db_path)


# ── location to location (#19) ─────────────────────────────────────────────

def _noise_of(cm):
    s = _by_kind(cm).get("self") or {}
    return (float(s["noise_band"]) if s.get("available") and s.get("noise_band") is not None else None), s


def location_compare(user, db_path=DB_PATH) -> dict:
    """The owner's locations side by side on the metrics that travel
    between them (the engine's `location` kind — organization_id, own data,
    no privacy floor, only for logins that may switch locations). Each
    location is first read against its own normal (the engine's `self`),
    then against the median of the owner's OTHER locations; a gap is called
    only when it is wider than the two sides' combined week-to-week swing
    (the location's own noise band and the others' median one) — else
    "in line". {ok, metrics: [{metric, label, unit, locations: [{id, name,
    value, value_text, vs_own, vs_group, tone, called}]}], why_not}."""
    from intelligence import engine
    from intelligence import features as _features
    rid = (user or {}).get("restaurant_id")
    if not rid:
        return {"ok": False, "error": "No restaurant on this login."}
    allowed = engine._visible_modules(user)
    series = {}
    out = []
    why_not = None
    for metric in LOCATION_METRICS:
        # rows=[]: the location kind reads each location's latest row itself.
        cm = engine.compare(rid, metric, kinds=("location",), viewer=user, db_path=db_path, rows=[])
        mod = cm.get("module")
        if allowed is not None and mod and mod not in allowed:
            continue
        loc = _by_kind(cm).get("location") or {}
        if not loc.get("available"):
            why_not = why_not or loc.get("why_not")
            continue
        entries = []
        for l in loc.get("locations") or ():
            lid = l["restaurant_id"]
            if lid not in series:
                try:
                    series[lid] = _features.series(lid, weeks=60, db_path=db_path)
                except Exception:
                    series[lid] = []
            own_cm = engine.compare(lid, metric, kinds=("self",), db_path=db_path, rows=series[lid])
            noise, s = _noise_of(own_cm)
            entries.append({"id": lid, "name": l.get("name"), "value": l.get("value"),
                            "value_text": fmt(l.get("value"), cm.get("unit")), "this": bool(l.get("this")),
                            "noise": noise, "vs_own": s.get("verdict") if s.get("available") else None})
        higher = cm.get("better") == "higher"
        for e in entries:
            others = [o for o in entries if o["id"] != e["id"]]
            if not others:
                continue
            from intelligence.stats import percentile
            med = percentile([o["value"] for o in others], 50)
            o_noise = [o["noise"] for o in others if o["noise"] is not None]
            e["group_median_text"] = fmt(med, cm.get("unit"))
            if e["noise"] is None or not o_noise:
                e.update(vs_group="not called — needs 6 weeks of history to tell a gap from noise",
                         tone="neutral", called=False)
                continue
            band = (e["noise"] ** 2 + percentile(o_noise, 50) ** 2) ** 0.5
            gap = e["value"] - med
            if abs(gap) <= band:
                e.update(vs_group="in line with your other locations", tone="neutral", called=False)
            elif (gap > 0) == higher:
                e.update(vs_group="ahead of your other locations", tone="good", called=True)
            else:
                e.update(vs_group="behind your other locations", tone="warn", called=True)
        for e in entries:
            e.pop("noise", None)
        out.append({"metric": metric, "label": cm.get("label"), "unit": cm.get("unit"), "better": cm.get("better"),
                    "locations": entries})
    return {"ok": True, "metrics": out, "why_not": None if out else (why_not or "a single location")}


# ── group strongest / weakest (#19) ────────────────────────────────────────

def rank_by_rating(entries, db_path=DB_PATH) -> dict:
    """entries: [{id, name, rating, n}] — each location's 30-day (or the
    week's) rating and the reviews behind it. Each location is first read
    against its own normal (the engine's `self` on avg_rating_30d; None
    without its history), then the strongest and weakest are named only
    among locations with thresholds.GROUP_RANK_MIN_REVIEWS reviews, and only
    when the two differ by more than RANK_Z standard errors (thresholds.
    RATING_SIGMA over both counts). {strongest, weakest, level, basis,
    vs_own: {id: verdict}}."""
    from thresholds import GROUP_RANK_MIN_REVIEWS, rating_gap_beyond_noise
    vs_own = {}
    for e in entries or ():
        try:
            from intelligence import engine
            s = _by_kind(engine.compare(e["id"], "avg_rating_30d", kinds=("self",), db_path=db_path)).get("self") or {}
            vs_own[e["id"]] = s.get("verdict") if s.get("available") else None
        except Exception:
            vs_own[e["id"]] = None
    rated = [e for e in entries or () if e.get("rating") and int(e.get("n") or 0) >= GROUP_RANK_MIN_REVIEWS]
    out = {"strongest": None, "weakest": None, "level": False, "basis": None, "vs_own": vs_own}
    if len(rated) < 2:
        out["basis"] = (f"a rating needs {GROUP_RANK_MIN_REVIEWS} reviews behind it; "
                        f"{len(rated)} location{'' if len(rated) == 1 else 's'} {'has' if len(rated) == 1 else 'have'} that")
        return out
    best = max(rated, key=lambda e: (float(e["rating"]), int(e.get("n") or 0)))
    worst = min(rated, key=lambda e: (float(e["rating"]), -int(e.get("n") or 0)))
    if best is worst or not rating_gap_beyond_noise(best["rating"], best.get("n"), worst["rating"], worst.get("n"),
                                                    z=RANK_Z):
        out.update(level=True, basis="the locations' ratings are within noise of each other")
        return out

    def named(e):
        return {"location": e.get("name"), "id": e["id"], "rating": e["rating"], "reviews": int(e.get("n") or 0),
                "vs_own": vs_own.get(e["id"])}
    out.update(strongest=named(best), weakest=named(worst),
               basis=f"ratings on at least {GROUP_RANK_MIN_REVIEWS} reviews, a gap beyond noise")
    return out
