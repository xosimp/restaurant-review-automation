"""Restaurant DNA, told (owner, 10/8/26: "make it feel like an AI is revealing
the personality of the restaurant").

intelligence.dna measures the profile — every dimension a figure, a basis
and what it still needs. This module reads that profile (already projected
by the login's module view) and says what it adds up to:

  * traits      — a name a figure EARNED against a threshold stated here
                  ("Weekend Driven": 55% or more of sales Friday to Sunday).
                  A trait is never named from a figure that wasn't measured,
                  and every one carries the figure and the rule behind it.
                  This replaces the 9/24/26 rule "no personality labels"
                  (owner's call, 10/8/26) with the stricter half of it: no
                  label without its measurement.
  * learning    — the traits still waiting on data, with real progress
                  ("11 of 28 nights of hourly sales") where the dimension's
                  own count measures it.
  * axes        — the profile's shape: each axis 0-100, where 50 is the
                  typical restaurant on the stated benchmarks dna.DIMENSIONS
                  carries (its `anchor`), never another restaurant's figure.
                  An axis with nothing measured is None ("learning").
  * connections — chains of MEASURED facts read together (sales rhythm ->
                  how hours follow sales -> how labor % swings). The only
                  arithmetic claim is the measured elasticity's own.
  * headline    — the read in plain words, composed from earned traits and
                  identity the owner set. Deterministic: no model call, so
                  Home never waits on one and nothing is invented.

Ratios, shares and bands only, as the profile itself — never dollars.
"""
from datetime import date

from . import dna as _dna
from .dna import DIMENSIONS, get_conn
from models import DB_PATH


def _pct(v, places=0):
    return f"{float(v) * 100:.{places}f}%"


def _dims(profile):
    out = {}
    for f in (profile or {}).get("families") or []:
        for d in f.get("dimensions") or []:
            out[d["key"]] = d
    return out


def _raw(dims, key):
    d = dims.get(key)
    return d.get("value") if d and d.get("measured") else None


def _z(key, raw):
    """(raw - centre) / scale on the stated anchor, in the dimension's own
    space (log10 for day counts) — the same reading dna.anchor_norms gives."""
    meta = DIMENSIONS[key]
    c, s = meta["anchor"]
    try:
        return (_dna._t(key, raw) - float(c)) / float(s)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _strength(z):
    """A 0-100 meter from an oriented z: 50 is typical, ±2.5 sd the ends."""
    if z is None:
        return None
    return int(round(max(2.0, min(98.0, 50.0 + 20.0 * z))))


# ── traits ───────────────────────────────────────────────────────────────────
# Each rule: the dimension(s) it reads, the threshold (stated, in the copy
# too), the orientation of its meter, an icon key the screens draw, and the
# sentence its figure makes. Order is the order a headline reaches for them.

def _t_weekend(d):
    v = _raw(d, "weekend_share")
    if v is None:
        return None
    if v >= 0.55:
        return {"key": "weekend_driven", "name": "Weekend Driven", "icon": "calendar", "family": "sales",
                "figure": _pct(v), "figure_label": "of sales Friday to Sunday",
                "sentence": f"{_pct(v)} of your sales land Friday to Sunday. A typical restaurant does about 45%.",
                "rule": "55% or more of sales Friday to Sunday", "dims": ["weekend_share"],
                "strength": _strength(_z("weekend_share", v)), "split": round(float(v), 3)}
    if v <= 0.38:
        return {"key": "weekday_business", "name": "Weekday Business", "icon": "calendar", "family": "sales",
                "figure": _pct(1 - v), "figure_label": "of sales Monday to Thursday",
                "sentence": f"{_pct(1 - v)} of your sales come Monday to Thursday — a weekday rhythm most "
                            "restaurants don't have.",
                "rule": "38% or less of sales Friday to Sunday", "dims": ["weekend_share"],
                "strength": _strength(-_z("weekend_share", v)), "split": round(float(v), 3)}
    return None


def _t_daypart(d):
    v = _raw(d, "daypart_mix")
    if v is None:
        return None
    if v >= 0.5:
        return {"key": "daytime_driven", "name": "Daytime Driven", "icon": "sun", "family": "sales",
                "figure": _pct(v), "figure_label": "of sales before 4pm",
                "sentence": f"{_pct(v)} of your sales ring before 4pm.", "rule": "half or more of sales before 4pm",
                "dims": ["daypart_mix"], "strength": _strength(_z("daypart_mix", v))}
    if v <= 0.25:
        return {"key": "evening_driven", "name": "Evening Driven", "icon": "moon", "family": "sales",
                "figure": _pct(1 - v), "figure_label": "of sales after 4pm",
                "sentence": f"{_pct(1 - v)} of your sales ring after 4pm.", "rule": "a quarter or less before 4pm",
                "dims": ["daypart_mix"], "strength": _strength(-_z("daypart_mix", v))}
    return None


def _t_steadiness(d):
    v = _raw(d, "sales_volatility")
    if v is None:
        return None
    if v <= 0.15:
        return {"key": "steady_demand", "name": "Steady Demand", "icon": "wave", "family": "sales",
                "figure": _pct(v), "figure_label": "day-to-day swing",
                "sentence": f"Once the weekday pattern is taken out, your sales swing only {_pct(v)} day to day.",
                "rule": "15% or less day-to-day swing after the weekday pattern", "dims": ["sales_volatility"],
                "strength": _strength(-_z("sales_volatility", v))}
    if v >= 0.30:
        return {"key": "swingy_demand", "name": "Swingy Demand", "icon": "wave", "family": "sales", "tone": "watch",
                "figure": _pct(v), "figure_label": "day-to-day swing",
                "sentence": f"Even after the weekday pattern, your sales swing {_pct(v)} day to day — "
                            "nights are hard to call.",
                "rule": "30% or more day-to-day swing after the weekday pattern", "dims": ["sales_volatility"],
                "strength": _strength(_z("sales_volatility", v))}
    return None


def _t_predictable(d):
    v = _raw(d, "demand_predictability")
    if v is None or v < 0.3:
        return None
    return {"key": "highly_predictable", "name": "Highly Predictable", "icon": "target", "family": "sales",
            "figure": _pct(v), "figure_label": "better than a naive guess",
            "sentence": f"Weekly forecasts here beat a naive guess by {_pct(v)} — your demand follows its patterns.",
            "rule": "forecasts 30% or more better than a naive guess", "dims": ["demand_predictability"],
            "strength": _strength(_z("demand_predictability", v))}


def _t_seasonal(d):
    v = _raw(d, "seasonality")
    if v is None or float(v) < 1.6:
        return None
    return {"key": "highly_seasonal", "name": "Highly Seasonal", "icon": "leaf", "family": "sales",
            "figure": f"{float(v):.1f}×", "figure_label": "busiest month vs quietest",
            "sentence": f"Your busiest month runs {float(v):.1f}× your quietest.",
            "rule": "busiest month 1.6× the quietest or more", "dims": ["seasonality"],
            "strength": _strength(_z("seasonality", v))}


def _t_growth(d):
    v = _raw(d, "growth")
    if v is None:
        return None
    if v >= 1.0:
        return {"key": "growing", "name": "Growing", "icon": "trend_up", "family": "sales",
                "figure": f"+{float(v):.1f}%", "figure_label": "a week, 13-week trend",
                "sentence": f"Weekly sales have been climbing about {float(v):.1f}% a week for 13 weeks.",
                "rule": "13-week trend of +1% a week or more", "dims": ["growth"], "strength": _strength(_z("growth", v))}
    if v <= -1.0:
        return {"key": "softening", "name": "Softening", "icon": "trend_down", "family": "sales", "tone": "watch",
                "figure": f"{float(v):.1f}%", "figure_label": "a week, 13-week trend",
                "sentence": f"Weekly sales have eased about {abs(float(v)):.1f}% a week over 13 weeks.",
                "rule": "13-week trend of -1% a week or less", "dims": ["growth"],
                "strength": _strength(-_z("growth", v))}
    return None


def _t_hours(d):
    v = _raw(d, "open_hours")
    if v is None or float(v) < 85:
        return None
    return {"key": "long_hours", "name": "Open Long Hours", "icon": "clock", "family": "sales",
            "figure": f"{float(v):.0f}h", "figure_label": "open a week",
            "sentence": f"You're open {float(v):.0f} hours a week — most restaurants are open about 70.",
            "rule": "85 or more hours open a week", "dims": ["open_hours"], "strength": _strength(_z("open_hours", v))}


def _t_labor(d):
    v = _raw(d, "labor_pct")
    if v is None or float(v) > 28.0:
        return None
    return {"key": "labor_efficient", "name": "Labor Efficient", "icon": "people", "family": "labor",
            "figure": f"{float(v):.1f}%", "figure_label": "labor %",
            "sentence": f"Labor runs {float(v):.1f}% of sales — under the 30% a typical restaurant carries.",
            "rule": "labor 28% of sales or less", "dims": ["labor_pct"], "strength": _strength(-_z("labor_pct", v))}


def _t_overtime(d):
    v = _raw(d, "overtime_intensity")
    if v is None or float(v) > 0.02:
        return None
    return {"key": "overtime_controlled", "name": "Overtime Under Control", "icon": "shield", "family": "labor",
            "figure": _pct(v), "figure_label": "of hours are overtime",
            "sentence": f"Only {_pct(v)} of hours are overtime — the schedule keeps people under 40.",
            "rule": "2% of hours or less are overtime", "dims": ["overtime_intensity"],
            "strength": _strength(-_z("overtime_intensity", v))}


def _t_flex(d):
    v = _raw(d, "labor_flex")
    if v is None:
        return None
    if v >= 0.7:
        return {"key": "staffs_to_demand", "name": "Staffs to Demand", "icon": "flex", "family": "labor",
                "figure": f"{float(v):.2f}", "figure_label": "hours follow sales",
                "sentence": f"When sales rise 10%, hours rise about {float(v) * 10:.0f}% — staffing tracks the night.",
                "rule": "hours move 0.7 or more with sales", "dims": ["labor_flex"],
                "strength": _strength(_z("labor_flex", v))}
    return None


def _t_labor_swing(d):
    v = _raw(d, "labor_swing")
    if v is None or float(v) < 8.0:
        return None
    return {"key": "uneven_labor_days", "name": "Uneven Labor Days", "icon": "wave", "family": "labor",
            "tone": "watch", "figure": f"{float(v):.1f} pts", "figure_label": "day-to-day labor % swing",
            "sentence": f"Labor % swings {float(v):.1f} points from day to day — slow days carry heavy labor.",
            "rule": "labor % swinging 8 points or more day to day", "dims": ["labor_swing"],
            "strength": _strength(_z("labor_swing", v))}


def _t_rating(d):
    v = _raw(d, "rating_level")
    if v is None or float(v) < 4.4:
        return None
    fav = float(v) >= 4.7
    return {"key": "guest_favorite" if fav else "well_rated", "name": "Guest Favorite" if fav else "Well Rated",
            "icon": "star", "family": "guests", "figure": f"{float(v):.2f}★", "figure_label": "average, 30 days",
            "sentence": f"Guests rate you {float(v):.2f}★ over the last 30 days"
                        + (" — top-tier." if fav else ", above the 4.3 most restaurants hold."),
            "rule": "4.7★ or more" if fav else "4.4★ or more over 30 days", "dims": ["rating_level"],
            "strength": _strength(_z("rating_level", v))}


def _t_replies(d):
    v = _raw(d, "reply_rate")
    if v is None or float(v) < 0.7:
        return None
    return {"key": "answers_reviews", "name": "Answers Its Reviews", "icon": "reply", "family": "guests",
            "figure": _pct(v), "figure_label": "of reviews answered",
            "sentence": f"You answer {_pct(v)} of your reviews — guests see someone is listening.",
            "rule": "70% or more of reviews answered", "dims": ["reply_rate"], "strength": _strength(_z("reply_rate", v))}


def _t_register(d):
    c, v = _raw(d, "comp_rate"), _raw(d, "void_rate")
    if c is None or v is None or float(c) > 1.0 or float(v) > 0.8:
        return None
    zs = [z for z in (_z("comp_rate", c), _z("void_rate", v)) if z is not None]
    return {"key": "tight_register", "name": "Tight Register", "icon": "register", "family": "food",
            "figure": f"{float(c):.1f}% · {float(v):.1f}%", "figure_label": "comps · voids of sales",
            "sentence": f"Comps are {float(c):.1f}% of sales and voids {float(v):.1f}% — little leaks out the register.",
            "rule": "comps 1% of sales or less and voids 0.8% or less", "dims": ["comp_rate", "void_rate"],
            "strength": _strength(-sum(zs) / len(zs)) if zs else None}


def _t_advice(d):
    v = _raw(d, "rec_uptake")
    if v is None or float(v) < 0.6:
        return None
    return {"key": "open_to_advice", "name": "Open to Advice", "icon": "spark", "family": "loop",
            "figure": _pct(v), "figure_label": "of recommendations taken",
            "sentence": f"You've said yes to {_pct(v)} of the recommendations Cavnar AI made.",
            "rule": "60% or more of recommendations taken", "dims": ["rec_uptake"], "strength": _strength(_z("rec_uptake", v))}


TRAIT_RULES = (_t_weekend, _t_daypart, _t_hours, _t_steadiness, _t_predictable, _t_seasonal, _t_growth,
               _t_labor, _t_overtime, _t_flex, _t_labor_swing, _t_rating, _t_replies, _t_register, _t_advice)

# The traits still being learned: what each would read and what measures it.
# `need` is the dimension's own count toward its floor (the same `n` the
# profile carries), so progress is real; None where the count isn't one.
LEARNING = (
    {"key": "daypart", "name": "Daytime or Evening", "icon": "sun", "family": "sales", "dims": ["daypart_mix"],
     "need": 28, "unit": "nights of hourly sales"},
    {"key": "predictability", "name": "Predictability", "icon": "target", "family": "sales",
     "dims": ["demand_predictability"], "need": 6, "unit": "scored weekly forecasts"},
    {"key": "trend", "name": "Growth", "icon": "trend_up", "family": "sales", "dims": ["growth"], "need": 13,
     "unit": "complete weeks of sales"},
    {"key": "seasonality", "name": "Seasonality", "icon": "leaf", "family": "sales", "dims": ["seasonality"],
     "need": 12, "unit": "complete months of sales"},
    {"key": "beverage", "name": "Bar-Led", "icon": "glass", "family": "sales", "dims": ["beverage_share"], "need": None},
    {"key": "team_reliability", "name": "Team Reliability", "icon": "people", "family": "labor",
     "dims": ["staffing_issues"], "need": 8, "unit": "watched nights of published schedules"},
    {"key": "schedule_habit", "name": "Schedule Habit", "icon": "calendar", "family": "labor",
     "dims": ["schedule_publish_rate"], "need": 4, "unit": "weeks since your first Cavnar AI schedule"},
    {"key": "team_stability", "name": "Team Stability", "icon": "people", "family": "labor", "dims": ["retention"],
     "need": None},
    {"key": "service_complaints", "name": "Service Under Pressure", "icon": "reply", "family": "guests",
     "dims": ["wait_service_complaints"], "need": 20, "unit": "analysed reviews in 90 days"},
    {"key": "rating_momentum", "name": "Rating Momentum", "icon": "star", "family": "guests",
     "dims": ["rating_momentum"], "need": None},
    {"key": "food_cost", "name": "Food Cost Control", "icon": "register", "family": "food",
     "dims": ["food_cost_level", "food_cost_stability", "waste_rate"], "need": None},
    {"key": "marketing", "name": "Marketing Rhythm", "icon": "spark", "family": "marketing", "dims": ["post_cadence"],
     "need": None},
    {"key": "results", "name": "Results That Stick", "icon": "trend_up", "family": "loop",
     "dims": ["improvement_rate"], "need": 5, "unit": "measured results in 90 days"},
)

# Guest loyalty has no dimension yet: it needs guests attached to checks.
LOYALTY = {"key": "guest_loyalty", "name": "Guest Loyalty", "icon": "heart", "family": "guests", "need": None,
           "needs": "guests attached to checks in your POS (a phone number or a loyalty account on the ticket)"}


# ── the shape ────────────────────────────────────────────────────────────────
# (key, label, [(dimension, orientation)]): +1 when higher is stronger.
AXES = (
    ("sales_stability", "Sales Stability", [("sales_volatility", -1)]),
    ("predictability", "Predictability", [("demand_predictability", 1)]),
    ("labor_efficiency", "Labor Efficiency", [("labor_pct", -1), ("overtime_intensity", -1)]),
    ("labor_rhythm", "Labor Rhythm", [("labor_flex", 1), ("labor_swing", -1)]),
    ("team_reliability", "Team Reliability", [("staffing_issues", -1), ("retention", -1),
                                              ("schedule_publish_rate", 1)]),
    ("guest_sentiment", "Guest Sentiment", [("rating_level", 1), ("negative_share", -1)]),
    ("review_care", "Review Care", [("reply_rate", 1), ("reply_within_day", 1)]),
    ("register", "Register Discipline", [("comp_rate", -1), ("void_rate", -1)]),
    ("food_cost", "Food Cost Control", [("food_cost_level", -1), ("waste_rate", -1)]),
    ("follow_through", "Follow-through", [("rec_uptake", 1), ("follow_through", 1), ("improvement_rate", 1)]),
)


def _axes(d):
    out = []
    for key, label, members in AXES:
        zs, used, seen = [], [], []
        for dim, sign in members:
            if dim not in d:
                continue                       # a module this login can't view
            seen.append(dim)
            v = _raw(d, dim)
            z = _z(dim, v) if v is not None else None
            if z is not None:
                zs.append(max(-2.5, min(2.5, z * sign)))
                used.append(dim)
        if not seen:
            continue
        score = _strength(sum(zs) / len(zs)) if zs else None
        out.append({"key": key, "label": label, "score": score, "measured": len(used), "of": len(seen),
                    "dims": seen})
    return out


# ── connections ──────────────────────────────────────────────────────────────

def _connections(d):
    out = []
    ws, flex, swing = _raw(d, "weekend_share"), _raw(d, "labor_flex"), _raw(d, "labor_swing")
    if flex is not None and swing is not None:
        nodes = []
        if ws is not None:
            nodes.append({"label": "Sales rhythm", "figure": _pct(ws), "note": "Friday to Sunday", "dim": "weekend_share"})
        nodes.append({"label": "Hours follow sales", "figure": f"{float(flex):.2f}", "note": "elasticity",
                      "dim": "labor_flex"})
        nodes.append({"label": "Labor % swing", "figure": f"{float(swing):.1f} pts", "note": "day to day",
                      "dim": "labor_swing"})
        pct = float(flex) * 10
        if float(flex) < 0.7:
            s = (f"When sales rise 10%, hours rise about {pct:.0f}%. Hours move less than sales, so quiet days "
                 f"carry heavier labor and labor % swings {float(swing):.1f} points day to day.")
        else:
            s = (f"When sales rise 10%, hours rise about {pct:.0f}% — staffing follows the night, and labor % "
                 f"swings {float(swing):.1f} points day to day.")
        out.append({"key": "labor_chain", "title": "How your staffing follows your sales", "nodes": nodes, "sentence": s})
    rr, rd, rl = _raw(d, "reply_rate"), _raw(d, "reply_within_day"), _raw(d, "rating_level")
    if rr is not None and rd is not None:
        nodes = []
        if rl is not None:
            nodes.append({"label": "Rating", "figure": f"{float(rl):.2f}★", "note": "30 days", "dim": "rating_level"})
        nodes += [{"label": "Reviews answered", "figure": _pct(rr), "note": "30 days", "dim": "reply_rate"},
                  {"label": "Within a day", "figure": _pct(rd), "note": "of replies", "dim": "reply_within_day"}]
        s = f"You answer {_pct(rr)} of reviews"
        s += (f", and {_pct(rd)} of those replies go out within a day." if float(rd) >= 0.5 else
              f", but only {_pct(rd)} within a day — the guest has usually moved on by then.")
        out.append({"key": "review_chain", "title": "How you answer your guests", "nodes": nodes, "sentence": s})
    up, ft, im = _raw(d, "rec_uptake"), _raw(d, "follow_through"), _raw(d, "improvement_rate")
    if up is not None and ft is not None:
        nodes = [{"label": "Said yes", "figure": _pct(up), "note": "of recommendations", "dim": "rec_uptake"},
                 {"label": "Changes made", "figure": _pct(ft), "note": "of those accepted", "dim": "follow_through"}]
        if im is not None:
            nodes.append({"label": "Results improved", "figure": _pct(im), "note": "measured", "dim": "improvement_rate"})
        else:
            nodes.append({"label": "Results improved", "figure": None, "note": "measuring", "dim": "improvement_rate"})
        s = f"You said yes to {_pct(up)} of recommendations; {_pct(ft)} of the changes you accepted were made"
        s += (f", and {_pct(im)} of measured results improved." if im is not None else
              ". Whether they worked is measured once five results are in.")
        out.append({"key": "advice_chain", "title": "From advice to results", "nodes": nodes, "sentence": s})
    return out


# ── the read ─────────────────────────────────────────────────────────────────

def _identity(d, restaurant=None):
    out = []
    concept, service = _raw(d, "concept"), _raw(d, "service_type")
    cd = (d.get("concept") or {}).get("display") if concept is not None else None
    sd = (d.get("service_type") or {}).get("display") if service is not None else None
    if cd:
        c = cd[:-1] if cd.endswith("s") and not cd.endswith("ss") else cd
        out.append({"key": "concept", "label": c})
    if sd:
        out.append({"key": "service", "label": sd})
    return out


def _a(word):
    return "an" if word[:1].lower() in "aeiou" else "a"


def _headline(name, identity, traits, d):
    who = name or "This restaurant"
    ids = [i["label"] for i in identity]
    kind = None
    if ids:
        concept = next((i["label"] for i in identity if i["key"] == "concept"), None)
        service = next((i["label"] for i in identity if i["key"] == "service"), None)
        # "full-service sports bar": the service model reads as one adjective
        kind = " ".join(x for x in ((service or "").lower().replace(" ", "-"), (concept or "").lower()) if x)
    got = {t["key"]: t for t in traits}
    first = f"{who} is {_a(kind)} {kind}" if kind else f"{who} is a restaurant"
    hrs = _raw(d, "open_hours")
    if hrs is not None:
        first += f" open {float(hrs):.0f} hours a week"
    if "weekend_driven" in got:
        first += f" that does {got['weekend_driven']['figure']} of its sales Friday to Sunday"
    elif "weekday_business" in got:
        first += f" that does {got['weekday_business']['figure']} of its sales Monday to Thursday"
    first += "."
    second = []
    for k, words in (("labor_efficient", lambda t: f"labor runs {t['figure']}"),
                     ("overtime_controlled", lambda t: f"overtime stays at {t['figure']} of hours"),
                     ("guest_favorite", lambda t: f"guests rate it {t['figure']}"),
                     ("well_rated", lambda t: f"guests rate it {t['figure']}"),
                     ("answers_reviews", lambda t: f"{t['figure']} of reviews get an answer"),
                     ("tight_register", lambda t: "little leaks out through comps and voids")):
        if k in got:
            second.append(words(got[k]))
    out = [first]
    if second:
        s = ", ".join(second[:-1]) + (" and " if len(second) > 1 else "") + second[-1]
        out.append(s[0].upper() + s[1:] + ".")
    watch = next((t for t in traits if t.get("tone") == "watch"), None)
    if watch:
        out.append("Watch: " + watch["sentence"][0].lower() + watch["sentence"][1:])
    return " ".join(out)


def _nights(restaurant_id, db_path):
    """(nights of sales watched, first night) — final days with sales."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT COUNT(DISTINCT substr(date,1,10)) n, MIN(substr(date,1,10)) first "
                         "FROM labor_daily_history WHERE restaurant_id=? AND sales > 0", (restaurant_id,)).fetchone()
        return (int(r["n"] or 0), r["first"]) if r else (0, None)
    except Exception:
        return 0, None
    finally:
        conn.close()


def _weeks_of_profile(restaurant_id, db_path):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT COUNT(*) n, MIN(week) first FROM intel_dna WHERE restaurant_id=?",
                         (restaurant_id,)).fetchone()
        return (int(r["n"] or 0), r["first"]) if r else (0, None)
    except Exception:
        return 0, None
    finally:
        conn.close()


def story(restaurant_id, profile, name=None, db_path=DB_PATH) -> dict:
    """The told DNA for one restaurant's (projected) profile. {} when the
    profile isn't built yet. Never raises on a dimension that's missing."""
    if not (profile or {}).get("available"):
        return {}
    d = _dims(profile)
    traits = []
    for rule in TRAIT_RULES:
        try:
            t = rule(d)
        except (TypeError, ValueError, KeyError):
            t = None
        if t and all(k in d for k in t["dims"]):
            traits.append(t)
    learning = []
    for spec in LEARNING:
        dims = [k for k in spec["dims"] if k in d]
        if not dims or any(d[k].get("measured") for k in dims):
            continue
        first = d[dims[0]]
        item = {k: spec[k] for k in ("key", "name", "icon", "family")}
        item["needs"] = first.get("needs") or DIMENSIONS[dims[0]]["needs"]
        item["dormant"] = bool(first.get("dormant"))
        if spec.get("need") and first.get("n") is not None and not item["dormant"]:
            have = max(0, min(int(first["n"] or 0), int(spec["need"])))
            item["progress"] = {"have": have, "need": spec["need"], "unit": spec["unit"],
                                "pct": int(round(100.0 * have / spec["need"]))}
        learning.append(item)
    learning.sort(key=lambda x: -(x.get("progress") or {}).get("pct", -1))
    if any(f.get("key") == "guests" for f in profile.get("families") or []):
        learning.append(dict(LOYALTY))
    identity = _identity(d)
    nights, first_night = _nights(restaurant_id, db_path)
    weeks, first_week = _weeks_of_profile(restaurant_id, db_path)
    from time_utils import mdy
    observed = {"nights": nights, "since": mdy(first_night) if first_night else None,
                "weeks": weeks, "measured": profile.get("measured"), "of": profile.get("of"),
                "coverage_pct": profile.get("coverage_pct"), "learning": len(learning)}
    lede = (f"Over {nights} nights of sales, Cavnar AI has measured {profile.get('measured')} of "
            f"{profile.get('of')} traits of how this restaurant runs." if nights else
            f"Cavnar AI has measured {profile.get('measured')} of {profile.get('of')} traits so far.")
    # What the last week taught: a dimension measured now that the week
    # before had none.
    fresh = []
    for k, x in d.items():
        h = x.get("history") or []
        if x.get("measured") and len(h) == 1 and weeks > 1:
            fresh.append({"key": k, "label": x["label"], "display": x.get("display")})
    return {"headline": _headline(name, identity, traits, d), "lede": lede, "identity": identity,
            "traits": traits, "learning": learning, "axes": _axes(d), "connections": _connections(d),
            "observed": observed, "recent": fresh[:4],
            "basis": ("Traits are named only from a measured figure against the rule shown with it. "
                      "On the shape, 50 is a typical restaurant on Cavnar AI's stated benchmarks — "
                      "never another restaurant's figures.")}
