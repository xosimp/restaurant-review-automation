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
    ("similarity", "How alike the group is"),
    ("spread", "How closely the group agrees"),
    ("orgs", "Separate owners"),
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
        "own": ("your own figure for this measure rests on few days or is older than the group's"
                if (dims.get("own") if dims.get("own") is not None else 1) < 1
                else "your own figure for this measure is well measured and current"),
        "similarity": ("every type of restaurant on Cavnar — a behaviour measure, comparable across types"
                       if c.get("kind") == "platform" else
                       ("your type was guessed from your name — confirm it in Account to sharpen this"
                        if c.get("inferred") else
                        (f"a wider group than your profile — rung {int(c.get('level') or 0) + 1} of {c.get('levels')}"
                         if c.get("wider_than_profile") else
                         "restaurants in your confirmed group — a finer split would be more alike"))),
        "spread": "how tightly the group's own figures sit around the middle",
        "orgs": (f"{c.get('orgs')} separate owners in the group" if c.get("orgs") is not None
                 else "separate owners in the group"),
    }
    rows = []
    for key, title in _STRENGTH_ROWS:
        v = dims.get(key)
        if v is None and key in ("spread", "orgs"):
            continue
        rows.append({"key": key, "title": title,
                     "pct": None if v is None else int(round(max(0.0, min(1.0, float(v))) * 100)),
                     "basis": basis[key]})
    caps = list(s.get("caps_applied") or ())
    footer = ("The group's size sets the ceiling; under it the overall figure combines the others — "
              "the weakest pulls it down most.")
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


def outcome_words(standing, better="higher") -> str | None:
    """The engine's quartile standing in outcome words (Benchmarking audit
    #17, R3-23): "bottom quarter" on a lower-is-better metric is the
    HIGHEST labor % in the group, and an owner (or a model) reads "bottom"
    as "lowest". So the screen says which way is better and what the
    quarter means: "in the group's worst quarter — higher than 3 in 4"."""
    if not standing or standing == "unmeasured":
        return None
    lower = better == "lower"
    if standing == "top quarter":
        return "in the group's best quarter — " + ("lower" if lower else "higher") + " than 3 in 4"
    if standing == "bottom quarter":
        return "in the group's worst quarter — " + ("higher" if lower else "lower") + " than 3 in 4"
    if standing == "above the middle":
        return "better than the group's middle"
    if standing == "below the middle":
        return "worse than the group's middle"
    if standing == "about the middle":
        return "about the group's middle"
    return standing


def _week_end_mdy(week) -> str | None:
    """M/D/YY of the Sunday that ends an ISO week ("2026-W38")."""
    try:
        from datetime import timedelta
        from intelligence import benchmarks as _bm
        from time_utils import mdy
        mon = _bm._week_monday(week) if week else None
        return mdy(mon + timedelta(days=6)) if mon else None
    except Exception:
        return None


def _context_row(row, c, unit) -> dict:
    """A published figure measured differently from the restaurant's own
    (the engine's `comparable` False — Benchmarking audit #2, R1-05, R2-2,
    R4-3): shown as CONTEXT, with the engine's own definition note, never a
    standing, a tone, "behind" or an action — for every metric, not only
    labor."""
    lo, hi = c.get("low"), c.get("high")
    middle = (f"{fmt(lo, unit)}–{fmt(hi, unit)}" if lo is not None and hi is not None
              else (fmt(c.get("median"), unit) if c.get("median") is not None else None))
    row.update(kind="industry", context=True, standing=None, tone="neutral", behind=False,
               against=c.get("label") or c.get("source"), middle_text=middle, source=c.get("source"),
               note=(c.get("definition_note") or "The published figure is measured differently from yours, so it "
                     "is context, not a like-for-like comparison."))
    return row


def metric_row(cm) -> dict | None:
    """One metric's row on the card, from its engine comparison: the value,
    which kind the standing is read against, the standing in outcome words,
    its tone (good / neutral / warn — never red: a standing is not an
    emergency), whether the restaurant is behind, and for a metric it is
    behind on one action. A published figure the engine marks not
    comparable is a context row. None when the restaurant has no figure for
    the metric or the engine has no comparison for it."""
    head = (cm.get("headline") or {}).get("kind")
    by = _by_kind(cm)
    label, unit = cm.get("label") or cm.get("metric"), cm.get("unit")
    own = (cm.get("own") or {}).get("value")
    row = {"metric": cm.get("metric"), "label": label, "unit": unit, "module": cm.get("module"),
           "kind": head, "value": own, "value_text": fmt(own, unit), "standing": None, "standing_key": None,
           "tone": "neutral", "behind": False, "context": False, "text": (cm.get("headline") or {}).get("text"),
           "as_of": None, "own_as_of": _week_end_mdy((cm.get("own") or {}).get("week")) if own is not None else None,
           "strength_pct": None, "note": None, "action": None, "group": None, "tag": None}
    ind = by.get("industry") or {}
    if head is None and ind.get("available") and ind.get("comparable") is not True:
        # The engine may keep a not-comparable figure out of the headline;
        # it is still worth showing as context beside the owner's figure.
        head = "industry"
    if head in ("peers", "platform"):
        # platform leads only for a behaviour metric (engine.compare), where
        # the all-types band is a fair peer set; its label says "all types".
        c = by[head]
        st = c.get("standing")
        pct = (c.get("strength") or {}).get("pct")
        n_txt = (c.get("cohort_label") if head == "platform"
                 else f"{c.get('n')} other {_lc_label(c.get('cohort_label'))}")
        row.update(standing=outcome_words(st, cm.get("better")), standing_key=st, as_of=c.get("as_of"),
                   strength_pct=pct, value=c.get("value"), value_text=fmt(c.get("value"), unit),
                   against=n_txt, middle_text=fmt(c.get("p50"), unit), group=(head, c.get("cohort")),
                   tag=((f"vs {c.get('n')} restaurants, all types" if head == "platform"
                         else f"vs {c.get('n')} like yours")
                        + (f" · {pct}% comparison strength" if pct is not None else "")))
        row["tone"] = "good" if st in _AHEAD else ("warn" if st in _BEHIND else "neutral")
        row["behind"] = st in _BEHIND
    elif head == "self":
        c = by["self"]
        v = c.get("verdict")
        row.update(standing=v, standing_key=v, as_of=c.get("as_of"), value=c.get("value"),
                   value_text=fmt(c.get("value"), unit), against=c.get("baseline_label"),
                   middle_text=fmt(c.get("baseline"), unit), group=("self",), tag="vs your own normal")
        row["tone"] = "good" if v and v.startswith("better") else ("warn" if v and v.startswith("worse") else "neutral")
        row["behind"] = bool(v and v.startswith("worse"))
    elif head == "industry":
        c = ind
        if own is None:
            return None
        row.update(group=("industry", c.get("source")), tag="published figure")
        if c.get("comparable") is not True:
            _context_row(row, c, unit)
            row["tag"] = "published figure, as context"
        else:
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
            row.update(standing=st, standing_key=st, tone=tone, behind=tone == "warn", against=c.get("label"),
                       middle_text=f"{fmt(lo, unit)}–{fmt(hi, unit)}", source=c.get("source"),
                       note=c.get("definition_note"))
    else:
        return None
    if own is None and row.get("value") is None:
        return None
    if row["behind"]:
        row["action"] = {"label": "Ask what to change", "ask": _ask(label, f"{row['standing']} "
                                                                  f"({row['value_text']})")}
    return row


def _lc_label(s):
    """"Full-service restaurants on Cavnar" as it reads mid-sentence."""
    s = str(s or "restaurants")
    return s[:1].lower() + s[1:] if s[:2] != s[:2].upper() else s


def withhold_stale(row, state) -> dict:
    """Data Health gate on the card (Benchmarking audit #25, R3-15): when the
    data the restaurant's own figure is read from is out of date, the row
    says when it was last measured and carries no standing, tone or action
    — a July figure is not ranked next to a Labor tab that calls it old."""
    if not state or state.get("state") != "stale":
        return row
    when = row.get("own_as_of") or state.get("as_of")
    row.update(standing=None, standing_key=None, tone="neutral", behind=False, action=None, stale=True,
               note=((f"Last measured {when}. " if when else "")
                     + f"{state.get('label') or 'The data behind it'} is out of date, so no standing is shown "
                       "until it is current."))
    return row


def _group_text(g, rows, cms) -> str | None:
    by_metric = {cm.get("metric"): cm for cm in cms}
    r = next(r for r in rows if r.get("group") == g)
    c = _by_kind(by_metric.get(r["metric"]) or {}).get(r["kind"]) or {}
    if g[0] == "peers":
        return f"Compared to {c.get('n')} other {_lc_label(c.get('cohort_label'))}"
    if g[0] == "self":
        return f"vs {c.get('baseline_label') or 'your own previous 13 weeks'}"
    if g[0] == "industry":
        if r.get("context"):
            return f"Published figure for your type, as context: {c.get('source')}"
        return f"published: {c.get('source')}"
    return None


def _who(rows, cms) -> dict:
    """Who the card compares the restaurant to. One group across every row
    names it ("Compared to 12 other full-service restaurants on Cavnar");
    rows read against different groups get a neutral header and each row
    names its own group and strength (Benchmarking audit #26, R1-13, R2-24,
    R3-14): {kind, text, n, as_of, inferred, groups}."""
    groups = []
    for r in rows:
        if r.get("group") and r["group"] not in groups:
            groups.append(r["group"])
    by_metric = {cm.get("metric"): cm for cm in cms}
    if len(groups) == 1:
        g = groups[0]
        r = next(r for r in rows if r.get("group") == g)
        c = _by_kind(by_metric[r["metric"]]).get(r["kind"]) or {}
        text = _group_text(g, rows, cms)
        if g[0] == "peers":
            return {"kind": "peers", "text": text, "n": c.get("n"), "as_of": c.get("as_of"),
                    "inferred": bool(c.get("inferred")), "groups": 1}
        if g[0] == "self":
            return {"kind": "self", "text": text, "n": c.get("points"), "as_of": c.get("as_of"),
                    "inferred": False, "groups": 1}
        return {"kind": "industry", "text": text, "n": None, "as_of": str(c.get("year") or "") or None,
                "inferred": bool(c.get("inferred")), "groups": 1}
    if not groups:
        return {"kind": None, "text": None, "n": None, "as_of": None, "inferred": False, "groups": 0}
    peers = sum(1 for g in groups if g[0] == "peers")
    parts = []
    if peers:
        parts.append("restaurants like yours" if peers == 1 else f"{peers} groups of restaurants like yours")
    if any(g[0] == "self" for g in groups):
        parts.append("your own normal")
    if any(g[0] == "industry" for g in groups):
        parts.append("published figures")
    return {"kind": "mixed", "text": f"{len(rows)} comparisons — vs " + " and ".join(parts) + "; each row says which",
            "n": None, "as_of": None, "groups": len(groups),
            "inferred": any(r.get("kind") == "peers" and (_by_kind(by_metric[r["metric"]]).get("peers") or {}).get("inferred")
                            for r in rows)}


# Why there is no like-for-like group, by class (Benchmarking audit #29,
# R3-24): the engine's why_not is matched to the reason an owner can act on.
# Checked in this order: "fewer than 5 separate owners" is an owners reason
# before it is a "fewer than" one.
WHY_CLASSES = (
    ("unconfirmed", ("profile isn't confirmed",),
     "Your restaurant profile isn't confirmed yet, so no other restaurant is compared with you"),
    ("concept", ("set the restaurant's concept", "untyped or 'other'"),
     "Your concept isn't set, so there's no like-for-like group for it yet"),
    ("wage", ("pay rate", "default wage", "default rate"),
     "Labor cost is on Cavnar's default wage, so it isn't compared with other restaurants — set your pay rates"),
    ("spread", ("too spread out",),
     "Restaurants like yours vary too much on this for a middle to mean anything yet"),
    ("owners", ("separate owners", "one owner's locations"),
     "Restaurants like yours come from too few separate owners to compare yet"),
    ("own_data", ("more measured day", "more review", "not measured yet", "no current figure", "inventory count",
                  "logged in", "log waste", "a count", "deliveries"),
     "Your own figure isn't measured yet"),
    ("few", ("fewer than", "no current band", "no like-for-like", "withheld"),
     "Not enough restaurants like yours yet"),
)
_WHY_PRIORITY = ("unconfirmed", "concept")


def why_class(why) -> str | None:
    w = str(why or "").lower()
    if not w:
        return None
    for cls, needles, _text in WHY_CLASSES:
        if any(n in w for n in needles):
            return cls
    return "few"


def _why_text(cls) -> str:
    return next((t for c, _n, t in WHY_CLASSES if c == cls), WHY_CLASSES[-1][2])


def _below_minimum(cms, who, rows=(), can_edit=None) -> dict | None:
    """The honest state when no like-for-like group clears its minimum:
    WHY (unconfirmed profile, spread too wide, too few owners, the default
    wage, the restaurant's own figure — never always "not enough
    restaurants"), what the card falls back to, the engine's own reason and,
    for a profile reason, a "Confirm your profile" action carrying the
    engine's suggestion."""
    if who.get("kind") == "peers" or any(r.get("kind") == "peers" for r in rows):
        return None
    shown = {r.get("metric") for r in rows}
    peers_off = [c for cm in cms for c in (cm.get("comparisons") or ())
                 if c.get("kind") == "peers" and not c.get("available") and c.get("why_not")]
    # The reason for the metrics on screen leads; a metric the restaurant
    # has no figure for is counted under "N more not measured yet".
    on_screen = [c for cm in cms if cm.get("metric") in shown for c in (cm.get("comparisons") or ())
                 if c.get("kind") == "peers" and not c.get("available") and c.get("why_not")]
    peers_off = on_screen or peers_off
    classes = [why_class(c.get("why_not")) for c in peers_off]
    cls = next((p for p in _WHY_PRIORITY if p in classes), None)
    if cls is None and classes:
        cls = max(dict.fromkeys(classes), key=classes.count)
    why = next((c.get("why_not") for c in peers_off if why_class(c.get("why_not")) == cls), None)
    reason = _why_text(cls or "few")
    kinds = {r.get("kind") for r in rows}
    if who.get("kind") == "self" or ("self" in kinds and "industry" not in kinds):
        text = f"{reason} — here's how you compare to your own last 13 weeks."
    elif who.get("kind") == "industry" or ("industry" in kinds and "self" not in kinds):
        text = f"{reason} — here's the published figure for your type, as context."
    elif kinds & {"self", "industry"}:
        text = f"{reason} — here's how you compare to your own last 13 weeks, and published figures as context."
    else:
        self_why = next((c.get("why_not") for cm in cms for c in (cm.get("comparisons") or ())
                         if c.get("kind") == "self" and not c.get("available") and c.get("why_not")), None)
        text = (f"{reason}, and not enough of your own history to compare against"
                + (f" — {self_why}" if self_why else "") + ".")
    out = {"text": text, "why_not": why, "reason": cls or "few", "action": None}
    if cls in ("unconfirmed", "concept"):
        sug = next((c.get("suggestion") for c in peers_off if c.get("suggestion")), None)
        out["action"] = {"kind": "profile", "label": "Confirm your profile" if cls == "unconfirmed" else "Set your concept",
                         "suggestion": (sug or {}).get("text") if isinstance(sug, dict) else None,
                         "can_edit": can_edit}
    return out


def build(cms, scope="module", module=None, *, sources=None, can_edit=None) -> dict:
    """The card (or the Home strip) from engine comparisons. `sources`:
    {registry source: data_freshness state} for the Data Health gate (#25)."""
    cms = list(cms or ())
    rows = []
    for cm in cms:
        r = metric_row(cm)
        if not r:
            continue
        src = None
        try:
            from intelligence import metrics_registry as _reg
            src = _reg.meta(cm.get("metric")).get("source")
        except Exception:
            pass
        rows.append(withhold_stale(r, (sources or {}).get(src)))
    if scope == "home":
        # A figure measured differently is context on its module's card,
        # not a comparison worth a line on Home.
        rows = [r for r in rows if not r.get("context")]
    who = _who(rows, cms)
    strength = None
    if who["kind"] == "peers":
        first = next(r for r in rows if r["kind"] == "peers")
        cm = next(c for c in cms if c.get("metric") == first["metric"])
        strength = strength_detail(_by_kind(cm)["peers"])
    unmeasured = sum(1 for cm in cms if not (cm.get("own") or {}).get("measured"))
    if scope == "home":
        rows.sort(key=lambda r: (0 if r["behind"] else (1 if r["tone"] == "good" else 2)))
        rows = rows[:STRIP_MAX]
        for r in rows:
            r["open_module"] = OPEN_MODULE.get(r.get("module"))
    for r in rows:
        r.pop("group", None)
    out = {"ok": True, "scope": scope, "module": module, "who": who, "strength": strength, "rows": rows,
           "below_minimum": _below_minimum(cms, who, rows, can_edit=can_edit), "unmeasured": unmeasured,
           "behind": sum(1 for r in rows if r["behind"])}
    if not rows:
        first = next(iter(cms), None)
        out["empty"] = ((first or {}).get("headline") or {}).get("text") or \
            "Nothing to compare yet — this fills in as your own weeks of data build up."
    return out


def _source_states(restaurant, cms, db_path) -> dict:
    """{registry source: data_freshness.source_state} for the sources the
    rows read, each read once."""
    out = {}
    if restaurant is None:
        return out
    try:
        import data_freshness
        from intelligence import metrics_registry as _reg
    except Exception:
        return out
    for cm in cms or ():
        src = _reg.meta(cm.get("metric")).get("source")
        if not src or src in out or src not in data_freshness.SOURCES:
            continue
        try:
            out[src] = data_freshness.source_state(restaurant, src, db_path=None if db_path == DB_PATH else db_path)
        except Exception:
            out[src] = None
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
    cms = payload.get("comparisons") or []
    restaurant = None
    try:
        import models as _m
        rid = (user or {}).get("restaurant_id")
        restaurant = _m.get_restaurant(rid) if db_path == DB_PATH else _m.get_restaurant(rid, db_path=db_path)
    except Exception:
        restaurant = None
    try:
        from permissions import is_principal
        can_edit = bool(is_principal(user))
    except Exception:
        can_edit = None
    return build(cms, scope=scope, module=mod, sources=_source_states(restaurant, cms, db_path), can_edit=can_edit)


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
                e.update(vs_group="not called — needs 7 weeks of history to tell a gap from noise",
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
