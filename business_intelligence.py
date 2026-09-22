"""
business_intelligence.py — the layer that reads across modules.

Every module in this platform answers its own question well and stops at its
own edge. Reviews knows guests complained about service on Fridays; Labor
knows Friday runs the leanest; Food Cost knows Friday carries most of the
waste. Nothing put those three sentences next to each other, and the whole
value of an owner having all three modules is exactly that sentence.

Audit #15 found why: `review_intelligence` and `food_cost_intelligence` each
carry the same comment — "ask_cavnar.build_context already composes all of
them" — while build_context only ever concatenated per-module AGGREGATES and
never the causal layers. Three components each assumed a fourth did the
joining. This is that fourth component.

Two rules it does not break:

  A LINK NEEDS BOTH SIDES TO CLEAR THEIR OWN MODULE'S FLOOR. Nothing here
  invents a threshold. A complaint cluster is a cluster because
  review_intelligence said so (MIN_CLUSTER_MENTIONS, CONCENTRATION_MIN_SHARE);
  a waste day is concentrated because food_cost_intelligence said so
  (MIN_EVENTS_PER_BUCKET, 1.6x an even week). This module only asks whether
  two already-established findings point at the same day, dish or shift. That
  is deliberately a weak test to fail and a strong one to pass: it cannot
  manufacture a pattern, because it cannot see anything that is not already
  a pattern in its own module.

  CO-OCCURRENCE IS NOT CAUSE. Every link says what would confirm it and what
  else would explain it. labor.py:974 already forbids asserting that a lean
  day cost revenue or slowed service — "this system has no service-time,
  wait-time or cover-count data, so you cannot tell which it was" — and that
  constraint survives being joined to a review. A link between a lean Friday
  and Friday complaints is a QUESTION worth asking, phrased as one.

The money roll-up is the other half. Food Cost knows what its drivers are
worth per month, Labor knows what optimised scheduling is worth per month,
Reviews knows what the rating slide puts at risk per month. Ranked together
they answer "where is the money", which no single module can.
"""
import logging
from datetime import date, timedelta

from models import DB_PATH, get_conn

log = logging.getLogger(__name__)

# A weekday link needs both sides pointing at the same day. Two modules each
# naming a concentrated day is already past their own floors, so no extra
# threshold is applied here — see the module docstring.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")

# How far a weekday's labor percentage must sit below the period average
# before it is described as "the leanest day". Below this the difference is
# noise and naming a day would be inventing a pattern.
LEAN_DAY_MIN_GAP_PTS = 2.0

# A weekday average needs more than one of that weekday behind it. labor.py's
# dow_summary is a mean with no count attached, so two weeks is the shortest
# period in which every weekday has been seen at least twice.
MIN_PERIOD_DAYS_FOR_WEEKDAY = 14

# Mirrors labor.MIN_DAYS_TO_EXTRAPOLATE — the point below which labor.py
# itself withholds a monthly projection, so a zero coming back from it means
# "too short", not "nothing to recover".
_LABOR_MIN_DAYS_TO_PROJECT = 7

# A cross-module money line is only worth an owner's attention above this.
# Under it the figure is real but the action it implies costs more than it
# returns, and listing it crowds out the ones that matter.
MIN_MONTHLY_DOLLARS = 50.0


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _cat(category):
    """A complaint category as prose. The stored values are snake_case
    identifiers, and "takeout_delivery" was reaching the What Connects card
    on the dashboard looking like a variable name."""
    from analyser import category_label
    return category_label(category)


def _norm(text):
    """Loose match key for a dish or role named by two different modules.

    Reviews get a dish name from the analyser's entity extraction ("the
    ribeye"); Food Cost gets it from the POS menu ("Ribeye Steak"). Exact
    string equality would never fire.
    """
    # Punctuation becomes a SPACE, not nothing — "Ribeye-Steak" has to
    # normalise to "ribeye steak" for the word-run test below to see the word
    # "ribeye" in it at all.
    return " ".join("".join(ch if (ch.isalnum() or ch == " ") else " "
                            for ch in (text or "").lower()).split())


# Below this a dish name is too generic to match on: "pie", "dip" and "ale"
# appear inside unrelated words and sentences, and a link built on one would
# be the manufactured pattern this module exists to avoid.
_MIN_DISH_TOKEN = 4


def _same_thing(a, b):
    """True when two modules' names for a dish plainly refer to one dish.

    Reviews get the name from the analyser's entity extraction ("the
    ribeye"); Food Cost gets it from the POS menu ("Ribeye Steak") or from a
    driver's evidence sentence. Exact equality would never fire.

    The test is a shared significant WORD, not a substring: "Ribeye-Steak"
    and "the ribeye" share "ribeye" and are one dish, while a substring test
    would also match "ribeye" inside "ribeyeburgersauce" and a whole-run test
    would miss the pair entirely. Words under _MIN_DISH_TOKEN never count —
    "pie", "dip" and "ale" are too generic to carry a link on their own, and
    neither are the filler words a guest writes around a dish name.
    """
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y:
        return True
    left = {t for t in x.split() if len(t) >= _MIN_DISH_TOKEN and t not in _STOPWORDS}
    right = {t for t in y.split() if len(t) >= _MIN_DISH_TOKEN and t not in _STOPWORDS}
    return bool(left & right)


# Words long enough to pass the length floor but that carry no identity — a
# driver's evidence sentence is full of them, and "over" or "cost" matching
# between a dish name and a sentence about cost would link anything to
# anything.
_STOPWORDS = {
    "the", "and", "with", "from", "over", "under", "this", "that", "than",
    "cost", "costs", "price", "prices", "waste", "wasted", "menu", "item",
    "items", "recipe", "portion", "month", "monthly", "week", "weekly",
    "your", "their", "have", "been", "were", "into", "more", "less", "each",
    "about", "against", "every", "some", "most", "very",
}


# ── gathering ──────────────────────────────────────────────────────────────

def _safe(label, fn, degraded):
    """Run one module's entry point, recording failure rather than hiding it.

    A cross-module read that silently drops a module is worse than one that
    fails: the answer looks complete and is missing the half that mattered.
    Same contract food_cost_intelligence.cost_drivers already uses for its
    five driver sources.
    """
    try:
        return fn()
    except Exception as e:
        log.warning("business_intelligence: %s unavailable: %s", label, e)
        degraded.append(label)
        return None


def gather(restaurant_id: int, restaurant=None, db_path: str = DB_PATH) -> dict:
    """Every module's own executive read, in one pass.

    Module flags are honoured: a restaurant without Food Cost is not asked
    for a food cost brief, and its absence is reported as "not on this plan"
    rather than as a failure or as zero.
    """
    from models import get_restaurant
    restaurant = restaurant or get_restaurant(restaurant_id)
    degraded, off = [], []
    out = {"reviews": None, "food_cost": None, "labor": None,
           "marketing": None, "visibility": None}

    def _on(flag):
        return bool(getattr(restaurant, flag, 0)) if restaurant else False

    if _on("module_reviews"):
        import review_intelligence as ri
        out["reviews"] = _safe("reviews", lambda: {
            "brief": ri.executive_brief(restaurant_id, db_path=db_path),
            "clusters": ri.complaint_clusters(restaurant_id, db_path=db_path),
            "diagnoses": ri.get_diagnoses(restaurant_id, db_path=db_path, include_stale=True),
        }, degraded)
    else:
        off.append("reviews")

    if _on("module_inventory"):
        import food_cost_intelligence as fci
        out["food_cost"] = _safe("food_cost", lambda: {
            "brief": fci.executive_brief(restaurant_id, db_path=db_path),
            "weekday_waste": fci.weekday_waste(restaurant_id, db_path=db_path),
        }, degraded)
    else:
        off.append("food_cost")

    if _on("module_labor"):
        from labor import analyse_shifts_for_restaurant
        out["labor"] = _safe("labor", lambda: analyse_shifts_for_restaurant(restaurant_id), degraded)
        # Sample shift data is not this restaurant's labor. The Labor tab
        # shows it so the module isn't blank before the first upload; reading
        # it here would put invented days into an executive answer.
        if out["labor"] and not out["labor"].get("is_live"):
            out["labor"] = {"is_live": False}
    else:
        off.append("labor")

    if _on("module_marketing"):
        out["marketing"] = _safe("marketing", lambda: _marketing_activity(restaurant_id, db_path), degraded)
    else:
        off.append("marketing")

    out["visibility"] = _safe("visibility", lambda: _visibility(restaurant_id, db_path), degraded)

    out["degraded"] = degraded
    out["modules_off"] = off
    out["complete"] = not degraded
    return out


def _marketing_activity(restaurant_id, db_path=DB_PATH):
    """What marketing actually did in the last 30 days.

    Deliberately NOT joined to review volume or sales as a cause. A campaign
    and a busy week co-occurring is not evidence one produced the other, and
    this product has no covers, no attribution and no control group. It is
    carried so a cross-module answer can say what else was happening in the
    same window, which is genuinely useful and is not a causal claim.
    """
    since = (date.today() - timedelta(days=30)).isoformat()
    conn = get_conn(db_path)
    try:
        posts = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(reach),0) AS reach FROM marketing_content_log "
            "WHERE restaurant_id=? AND post_id IS NOT NULL AND date(created_at) >= ?",
            (restaurant_id, since)).fetchone()
        camps = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sent_count),0) AS sent FROM guest_campaigns "
            "WHERE restaurant_id=? AND date(created_at) >= ?",
            (restaurant_id, since)).fetchone()
    finally:
        conn.close()
    return {"window_days": 30,
            "posts_published": int(posts["n"] or 0) if posts else 0,
            "reach": int(posts["reach"] or 0) if posts else 0,
            "campaigns_sent": int(camps["n"] or 0) if camps else 0,
            "texts_delivered": int(camps["sent"] or 0) if camps else 0}


def _visibility(restaurant_id, db_path=DB_PATH):
    """AI search visibility — an entire module that never reached the
    assistant's context, only a tool it had to think to call.

    STORED RUNS ONLY. client_api._do_ai_visibility looks like the obvious
    call and is the wrong one here: on a cache miss it fires six to eight
    live Perplexity queries through a 1.3s pace gate, so putting it on this
    path would make every single Ask question — "what time do we open?" —
    trigger a paid visibility run, block the answer for ~10 seconds, and eat
    the owner's own aivis rate-limit budget. The scheduled weekly job is what
    produces these rows; this reads the last one it wrote.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT ai_score, gbp_score, created_at FROM ai_visibility_runs "
            "WHERE restaurant_id=? AND ai_score IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    from ai_guard import freshness
    fresh = freshness(row["created_at"], stale_after_days=21)
    return {"ai_score": row["ai_score"], "gbp_score": row["gbp_score"],
            # The age travels with the score. A visibility number from six
            # weeks ago reads exactly like this morning's without it.
            "as_of": fresh["as_of"], "age_days": fresh["age_days"],
            "stale": fresh["stale"]}


# ── the links ──────────────────────────────────────────────────────────────

def _lean_days(labor):
    """Weekdays whose labor percentage runs materially below this
    restaurant's own weekday average, with the gap in points.

    Not "understaffed" — labor.py is explicit that this data cannot tell
    running-lean from running-efficient.

    Two floors, both about not reading a pattern into one shift. The period
    has to span at least MIN_PERIOD_DAYS_FOR_WEEKDAY so each weekday average
    rests on more than a single observation — dow_summary is a mean with no
    count attached, so a restaurant with nine days synced can hand back a
    "Tuesday" built from exactly one Tuesday, and a link on that is a link on
    one shift. And at least three weekdays must carry data, or the average
    the gap is measured against is itself one or two days.
    """
    if _f((labor or {}).get("period_days")) < MIN_PERIOD_DAYS_FOR_WEEKDAY:
        return {}
    dow = (labor or {}).get("dow_summary") or {}
    vals = [v for v in dow.values() if isinstance(v, (int, float)) and v > 0]
    if len(vals) < 3:
        return {}
    avg = sum(vals) / len(vals)
    return {day: round(avg - pct, 1) for day, pct in dow.items()
            if isinstance(pct, (int, float)) and pct > 0 and (avg - pct) >= LEAN_DAY_MIN_GAP_PTS}


def _cluster_days(cluster):
    """The weekday(s) a complaint cluster concentrates on, if any cleared
    review_intelligence's own concentration floor."""
    if cluster.get("weekday_pair"):
        return list(cluster["weekday_pair"]["days"])
    if cluster.get("weekday"):
        return [cluster["weekday"]["value"]]
    return []


def correlations(restaurant_id: int, data: dict = None, restaurant=None,
                 db_path: str = DB_PATH) -> list:
    """Findings that no single module could reach, each with its evidence,
    what would confirm it, and what else would explain it.

    Returns [] when nothing lines up — which is the common and correct
    outcome, and far better than a manufactured connection.
    """
    data = data or gather(restaurant_id, restaurant=restaurant, db_path=db_path)
    links = []

    reviews = data.get("reviews") or {}
    food = data.get("food_cost") or {}
    labor = data.get("labor") or {}
    clusters = reviews.get("clusters") or []

    lean = _lean_days(labor) if labor.get("is_live") else {}
    waste_day = ((food.get("weekday_waste") or {}).get("worst_day") or {}).get("weekday")
    waste_share = ((food.get("weekday_waste") or {}).get("worst_day") or {}).get("share")

    for c in clusters[:5]:
        days = _cluster_days(c)

        # ── complaints and the leanest day fall on the same weekday ──
        for day in days:
            if day in lean:
                links.append({
                    "kind": "reviews_x_labor",
                    "modules": ["reviews", "labor"],
                    "claim_kind": "inferred",
                    # No superlative. _lean_days returns every day past the
                    # gap, so calling this one "the leanest" is a claim the
                    # data may not support when two days qualify — and a
                    # small false claim inside an otherwise sound finding is
                    # the kind an owner never thinks to check.
                    "headline": (f"{c['mentions']} {_cat(c['category'])} complaints concentrate on "
                                 f"{day}, which runs {lean[day]} points leaner on labor "
                                 f"than this restaurant's weekday average"),
                    "evidence": [
                        # A weekday PAIR's share covers both days, so it must
                        # not be reported against the one day this link
                        # happens to match on — "80% on Friday" when the
                        # measured figure was 80% across Friday and Saturday
                        # is a number the owner would act on and could not
                        # reproduce.
                        f"{c['mentions']} negative reviews naming {_cat(c['category'])} over "
                        f"{c['window_days']} days, {_concentration_phrase(c)}",
                        f"{day} averages {lean[day]} points below this restaurant's own "
                        f"weekday average labor percentage",
                    ],
                    "review_ids": (c.get("review_ids") or [])[:5],
                    # labor.py:974 — the data cannot distinguish lean from
                    # efficient, so this is never stated as a cause.
                    "not_a_cause": ("Running lean is not evidence of a service failure. This "
                                    "product has no service-time, wait-time or cover-count "
                                    "data, so the two facts sharing a day is a question, not "
                                    "a finding."),
                    "confirm_by": (f"Check whether {day} covers rose while hours stayed flat, "
                                   f"and read the {day} reviews against that shift's roster."),
                    "alternative": ("The same day may simply be the busiest, which raises both "
                                    "complaint volume and sales-per-labor-hour independently."),
                })
                break

        # ── complaints and waste land on the same weekday ──
        if waste_day and waste_day in days:
            links.append({
                "kind": "reviews_x_food_cost",
                "modules": ["reviews", "food_cost"],
                "claim_kind": "inferred",
                "headline": (f"{waste_day} carries both the {_cat(c['category'])} complaints and "
                             f"{int(_f(waste_share) * 100)}% of the week's waste"),
                "evidence": [
                    # Same rule as the labor link: a pair's share covers both
                    # days and must not be reported against the one day this
                    # link matched on.
                    f"{c['mentions']} negative reviews naming {_cat(c['category'])}, "
                    f"{_concentration_phrase(c)}",
                    f"{waste_day} holds {int(_f(waste_share) * 100)}% of waste dollars against "
                    f"an even week of {int(100 / 7)}%",
                ],
                "review_ids": (c.get("review_ids") or [])[:5],
                "confirm_by": (f"Walk {waste_day} prep: over-prepping and re-firing both show "
                               f"up as waste and as guests waiting."),
                "alternative": ("A high-volume day produces more of everything — more waste, "
                                "more reviews — without the two being connected."),
            })

        # ── a dish guests name is a dish running over recipe ──
        dish = (c.get("dish") or {}).get("value")
        if dish:
            # _as_action's shape: the driver's own words are in "what" (its
            # label) and in the evidence lines under it. A dish named by the
            # analyser can land in either, so both are checked.
            for d in ((food.get("brief") or {}).get("needs_attention_now") or []) + \
                     ((food.get("brief") or {}).get("can_wait") or []):
                haystack = [d.get("what") or ""] + [str(e) for e in (d.get("evidence") or [])]
                if any(_same_thing(dish, h) for h in haystack):
                    links.append({
                        "kind": "reviews_x_menu",
                        "modules": ["reviews", "food_cost"],
                        "claim_kind": "inferred",
                        "headline": (f"Guests name {dish} in {c['mentions']} {_cat(c['category'])} "
                                     f"complaints, and it is also a cost driver"),
                        "evidence": [
                            f"{c['mentions']} negative reviews naming {dish}",
                            f"Food Cost ranks it at ${_f(d.get('dollars_monthly')):,.0f}/month"
                            if d.get("dollars_monthly") else "Food Cost lists it as a driver",
                        ],
                        "review_ids": (c.get("review_ids") or [])[:5],
                        "confirm_by": ("Weigh three plates against the recipe card. Portion "
                                       "drift shows up on both sides of this at once."),
                        "alternative": ("A popular dish appears in more complaints and carries "
                                        "more cost simply because it sells more."),
                    })
                    break

    # ── marketing × reviews: a posting month and a review-volume move ──
    # Marketing and Intel contributed nothing to the cross-module argument:
    # gather() read them and no link kind used them, so removing either
    # module visibly cost the others nothing. Both links below are
    # CO-MOVEMENTS with the same honesty furniture as the rest — a floor on
    # each side, what would confirm it, what else explains it — and neither
    # is a revenue attribution, which marketing has no honest data for.
    mk = data.get("marketing") or {}
    if (mk.get("posts_published") or 0) >= MIN_POSTS_FOR_LINK:
        vol = _review_volume_shift(restaurant_id, db_path)
        if vol and vol["now"] >= MIN_REVIEWS_FOR_LINK and abs(vol["pct"]) >= REVIEW_SHIFT_PCT:
            up = vol["pct"] > 0
            links.append({
                "kind": "marketing_x_reviews",
                "modules": ["marketing", "reviews"],
                "claim_kind": "inferred",
                "headline": (f"{mk['posts_published']} posts went out in the last 30 days and reviews "
                             f"{'rose' if up else 'fell'} {abs(vol['pct']):.0f}% against the 30 days before"),
                "evidence": [
                    f"{mk['posts_published']} posts published in the last 30 days"
                    + (f", reaching about {mk['reach']:,}" if mk.get("reach") else ""),
                    f"{vol['now']} reviews in the last 30 days against {vol['before']} in the 30 before",
                ],
                "not_a_cause": ("Posts and reviews moving in the same month is a co-movement. This product "
                                "has no click or visit data tying a post to a guest who then reviewed."),
                "confirm_by": ("Look at whether the new reviews mention what the posts were about, and "
                               "whether the same weeks last year moved the same way."),
                "alternative": "A seasonal week, a holiday, or a press mention moves review volume on its own.",
            })

    # ── intel × reviews: AI visibility and the rating moving together ──
    vis = data.get("visibility") or {}
    if vis.get("ai_score") is not None and not vis.get("stale"):
        prev = _previous_visibility(restaurant_id, db_path, before=vis.get("as_of"))
        drop = (prev - vis["ai_score"]) if prev is not None else 0
        if drop >= VISIBILITY_DROP_POINTS:
            # The reviews brief carries clusters and diagnoses, not the
            # trend, so read it from its own module — a first draft looked
            # for a key that does not exist and would never have fired.
            try:
                import review_intelligence as _ri
                trend = _ri.rating_trend(restaurant_id, weeks=8, db_path=db_path) or {}
            except Exception:
                trend = {}
            direction = trend.get("direction")
            if direction == "down":
                links.append({
                    "kind": "intel_x_reviews",
                    "modules": ["intel", "reviews"],
                    "claim_kind": "inferred",
                    "headline": (f"Your AI-search visibility fell {drop:.0f} points and your weekly rating "
                                 f"has been slipping over the same stretch"),
                    "evidence": [
                        f"AI visibility {prev} → {vis['ai_score']} between the last two weekly runs",
                        f"Rating trend down over the last 8 weeks"
                        + (f" ({trend['first']:.1f} → {trend['latest']:.1f})"
                           if trend.get("first") and trend.get("latest") else ""),
                    ],
                    "not_a_cause": ("AI assistants weight recent rating and review volume, so a slipping "
                                    "rating can lower visibility — but a competitor's new listing or a "
                                    "profile change lowers it just as well."),
                    "confirm_by": "Re-run the visibility check after the next fortnight of reviews and see whether it recovers with the rating.",
                    "alternative": "A nearby competitor improved their profile, or Google changed what it shows.",
                })

    return links


# Floors for the two co-movement links above. A handful of posts against a
# handful of reviews is two small numbers moving, not a pattern.
MIN_POSTS_FOR_LINK = 4
MIN_REVIEWS_FOR_LINK = 10
REVIEW_SHIFT_PCT = 30.0
VISIBILITY_DROP_POINTS = 15


def _review_volume_shift(restaurant_id, db_path=DB_PATH):
    """Reviews in the last 30 days against the 30 before, on the shared
    review time axis. None when either window is empty."""
    from models import REVIEW_TIME_AXIS_BARE
    conn = get_conn(db_path)
    try:
        now_n = conn.execute(
            f"SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE}) >= date('now','-30 days')", (restaurant_id,)).fetchone()[0]
        before_n = conn.execute(
            f"SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
            f"AND date({REVIEW_TIME_AXIS_BARE}) >= date('now','-60 days') "
            f"AND date({REVIEW_TIME_AXIS_BARE}) < date('now','-30 days')", (restaurant_id,)).fetchone()[0]
    except Exception:
        return None
    finally:
        conn.close()
    if not before_n or not now_n:
        return None
    return {"now": int(now_n), "before": int(before_n),
            "pct": (now_n - before_n) / before_n * 100.0}


def _previous_visibility(restaurant_id, db_path=DB_PATH, before=None):
    """The ai_score from the run before the latest one, or None."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT ai_score FROM ai_visibility_runs WHERE restaurant_id=? AND ai_score IS NOT NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 2", (restaurant_id,)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    return int(rows[1][0]) if len(rows) >= 2 else None


def _concentration_phrase(c):
    """How the cluster's weekday concentration actually reads.

    review_intelligence reports either a single dominant day or a dominant
    PAIR ("Friday and Saturday dinner" being the real shape of a weekend
    service problem). The pair's share is across both days, so it has to be
    stated that way — attributing it to whichever of the two this link
    matched on would overstate a measured figure by roughly double.
    """
    pair = c.get("weekday_pair")
    if pair:
        return (f"{int(_f(pair.get('share')) * 100)}% across "
                f"{' and '.join(pair['days'])} together")
    day = c.get("weekday")
    if day:
        return f"{int(_f(day.get('share')) * 100)}% on {day['value']}"
    return "no single day dominant"


# ── where the money is ─────────────────────────────────────────────────────

def money_at_stake(restaurant_id: int, data: dict = None, restaurant=None,
                   db_path: str = DB_PATH) -> dict:
    """Every module's monthly dollar figure, ranked together.

    Each module already computes what its own problem is worth per month.
    Nothing ranked them against each other, so an owner could read "$740 in
    cost drivers" on one tab and "$1,900 in scheduling" on another with no
    indication which to spend Tuesday on.

    A module with no figure is listed as unavailable with its reason. It is
    never entered as zero — a missing measurement ranked as $0 would push a
    real problem down the list.
    """
    data = data or gather(restaurant_id, restaurant=restaurant, db_path=db_path)
    lines, unavailable = [], []

    fc = ((data.get("food_cost") or {}).get("brief") or {}).get("money_involved") or {}
    if fc.get("monthly_at_stake") is not None:
        lines.append({"module": "food_cost", "label": "Food cost drivers",
                      "monthly": round(_f(fc["monthly_at_stake"]), 2),
                      "claim_kind": "computed",
                      "basis": "ranked cost drivers, dollars per month"})
    else:
        unavailable.append({"module": "food_cost",
                            "reason": fc.get("reason") or "no driver figure available"})

    labor = data.get("labor") or {}
    if not labor.get("is_live"):
        unavailable.append({"module": "labor", "reason": "no real shift data uploaded yet"})
    elif labor.get("potential_savings_monthly"):
        lines.append({"module": "labor", "label": "Scheduling against target",
                      "monthly": round(_f(labor["potential_savings_monthly"]), 2),
                      "claim_kind": "computed",
                      "basis": (f"gap above the {labor.get('labor_target', 30)}% target over "
                                f"{labor.get('period_days', 0)} days synced")})
    elif labor.get("period_too_short_to_project") or \
            _f(labor.get("period_days")) < _LABOR_MIN_DAYS_TO_PROJECT:
        unavailable.append({"module": "labor",
                            "reason": "period too short to project a monthly figure"})
    else:
        # A zero is not a missing measurement here, and reporting it as "too
        # short" was simply the wrong sentence: labor at or under target has
        # nothing above target to recover, which is a result worth saying.
        unavailable.append({"module": "labor",
                            "reason": "labor is at or under target — nothing above it to recover"})

    # revenue_at_risk returns a RANGE and a direction, never a point figure —
    # it is an elasticity forecast, not a measurement. Collapsing it to one
    # number here would publish exactly the false precision that module went
    # out of its way to refuse. The midpoint is carried for ORDERING only and
    # is deliberately not what the snapshot prints.
    money = ((data.get("reviews") or {}).get("brief") or {}).get("costing_money") or {}
    upside = None
    if money.get("available"):
        low, high = _f(money.get("monthly_low")), _f(money.get("monthly_high"))
        lo, hi = min(abs(low), abs(high)), max(abs(low), abs(high))
        entry = {"module": "reviews", "label": "Rating movement",
                 "monthly": round((lo + hi) / 2, 2), "monthly_low": round(lo),
                 "monthly_high": round(hi), "is_range": True,
                 "rating_delta": money.get("rating_delta"),
                 "claim_kind": "forecast",
                 "basis": (f"{_f(money.get('rating_delta')):+.2f}★ against a "
                           f"{money.get('elasticity_low_pct')}-{money.get('elasticity_high_pct')}% "
                           f"revenue-per-star range on "
                           f"{money.get('sales_source') or 'trailing sales'}")}
        # An improving rating is money on the table, not money at stake. Ranking
        # it beside two costs to recover would tell an owner to go and fix
        # something that is already going right.
        if money.get("direction") == "upside":
            upside = entry
        else:
            lines.append(entry)
    else:
        unavailable.append({"module": "reviews",
                            "reason": money.get("reason") or "no rating movement large enough to price"})

    material = [l for l in lines if l["monthly"] >= MIN_MONTHLY_DOLLARS]
    material.sort(key=lambda l: -l["monthly"])
    return {
        "ranked": material,
        "upside": upside,
        "below_floor": [l for l in lines if l["monthly"] < MIN_MONTHLY_DOLLARS],
        "unavailable": unavailable,
        "floor": MIN_MONTHLY_DOLLARS,
        # Deliberately NO total. These are a measured cost, a scheduling gap
        # and an elasticity forecast — three different methods measuring three
        # different things. A sum would be the single most quotable number on
        # the screen and the least defensible one, and audit #14 already
        # caught a model inventing exactly that kind of total.
        "total_note": ("Do not add these together. They come from three different methods — "
                       "measured cost drivers, a scheduling gap against target, and a forecast "
                       "from rating elasticity — and only the first is money already being "
                       "spent. Quote them separately, each with its own basis."),
    }


# ── the one brief ──────────────────────────────────────────────────────────

def executive_brief(restaurant_id: int, restaurant=None, db_path: str = DB_PATH) -> dict:
    """One cross-module read: where the money is, what connects, what to do
    first, and what could not be answered.

    Deterministic — no model runs here. Everything is either measured by a
    module or explicitly reported as unavailable.
    """
    data = gather(restaurant_id, restaurant=restaurant, db_path=db_path)
    links = correlations(restaurant_id, data=data, db_path=db_path)
    money = money_at_stake(restaurant_id, data=data, db_path=db_path)

    reviews_brief = (data.get("reviews") or {}).get("brief") or {}
    food_brief = (data.get("food_cost") or {}).get("brief") or {}

    # What to do first, across modules rather than within one. A link beats a
    # single-module item: two modules agreeing is the strongest evidence this
    # platform can produce, and it is the thing no tab could ever show.
    first = None
    if links:
        top = links[0]
        first = {"what": top["headline"], "why": "two modules point at the same thing",
                 "modules": top["modules"], "evidence": top["evidence"],
                 "confirm_by": top.get("confirm_by"), "claim_kind": "inferred"}
    elif money["ranked"]:
        top = money["ranked"][0]
        first = {"what": top["label"], "why": f"the largest single monthly figure on the books",
                 "modules": [top["module"]], "dollars_monthly": top["monthly"],
                 "evidence": [top["basis"]], "claim_kind": top["claim_kind"]}
    elif food_brief.get("fix_first"):
        first = dict(food_brief["fix_first"], modules=["food_cost"])
    elif reviews_brief.get("fix_first"):
        first = dict(reviews_brief["fix_first"], modules=["reviews"])

    unanswered = []
    for m in data.get("modules_off", []):
        unanswered.append(f"{m} is not on this plan")
    for m in data.get("degraded", []):
        unanswered.append(f"{m} could not be read this time")
    for u in money.get("unavailable", []):
        unanswered.append(f"{u['module']}: {u['reason']}")

    return {
        "fix_first": first,
        "links": links,
        "money": money,
        "reviews": _trim_reviews(reviews_brief),
        "food_cost": _trim_food(food_brief),
        "labor": _trim_labor(data.get("labor") or {}),
        "marketing": data.get("marketing"),
        "visibility": data.get("visibility"),
        "modules_consulted": [k for k in ("reviews", "food_cost", "labor", "marketing", "visibility")
                              if data.get(k)],
        "modules_off": data.get("modules_off", []),
        "degraded": data.get("degraded", []),
        "complete": data.get("complete", True),
        "unanswered": unanswered,
    }


def _trim_reviews(b):
    if not b:
        return None
    return {"biggest_problems": (b.get("biggest_problems") or [])[:3],
            "fix_first": b.get("fix_first"), "trend": b.get("trend"),
            "costing_money": b.get("costing_money"), "coverage": b.get("coverage")}


def _trim_food(b):
    if not b:
        return None
    return {"why": b.get("why"), "fix_first": b.get("fix_first"),
            "needs_attention_now": b.get("needs_attention_now"),
            "food_cost": b.get("food_cost"), "profitability": b.get("profitability"),
            "trust": b.get("trust"), "worsened": b.get("worsened")}


def _trim_labor(a):
    if not a or not a.get("is_live"):
        return {"is_live": False}
    return {"is_live": True, "labor_pct": a.get("overall_labor_pct"),
            "target": a.get("labor_target"),
            "monthly_opportunity": a.get("potential_savings_monthly"),
            "overstaffed_days": len(a.get("overstaffed_days") or []),
            "dow_summary": a.get("dow_summary"),
            "period_days": a.get("period_days")}


# ── the text the assistant reads ───────────────────────────────────────────

def snapshot_block(restaurant_id: int, restaurant=None, db_path: str = DB_PATH) -> str:
    """The cross-module section of Ask Cavnar's context snapshot.

    Short on purpose. The full brief is a tool call away; this exists so the
    model opens every conversation already knowing where the money is and
    what lines up, instead of only finding out when it happens to call the
    right tool.
    """
    try:
        brief = executive_brief(restaurant_id, restaurant=restaurant, db_path=db_path)
    except Exception as e:
        log.warning("business_intelligence snapshot failed: %s", e)
        return ""

    lines = ["ACROSS THE BUSINESS (computed, not written by a model)"]

    money = brief.get("money") or {}
    ranked = money.get("ranked") or []
    if ranked:
        lines.append("- Monthly dollars at stake, ranked:")
        for r in ranked:
            # A range prints as a range. The midpoint exists to order this
            # list and is never shown, because it is not a figure anyone
            # measured.
            amount = (f"${r['monthly_low']:,.0f}-${r['monthly_high']:,.0f}"
                      if r.get("is_range") else f"${r['monthly']:,.0f}")
            lines.append(f"    {amount} — {r['label']} ({r['module']}; {r['basis']})")
        lines.append(f"    {money.get('total_note')}")
    if money.get("upside"):
        u = money["upside"]
        lines.append(f"- Going the right way: {u['label']} is worth "
                     f"${u['monthly_low']:,.0f}-${u['monthly_high']:,.0f}/month of UPSIDE "
                     f"({u['basis']}) — not a problem to fix.")

    if brief.get("links"):
        lines.append("- What lines up across modules:")
        for l in brief["links"][:3]:
            lines.append(f"    {l['headline']}")
            lines.append(f"      confirm by: {l.get('confirm_by')}")
            lines.append(f"      could also be: {l.get('alternative')}")
            if l.get("not_a_cause"):
                lines.append(f"      NOT a cause: {l['not_a_cause']}")
    elif brief.get("modules_consulted"):
        lines.append("- Nothing lines up across modules right now. Say that plainly rather "
                     "than connecting two findings yourself.")

    if brief.get("fix_first"):
        f = brief["fix_first"]
        lines.append(f"- If they only do one thing: {f.get('what')} "
                     f"({', '.join(f.get('modules') or [])}) — {f.get('why')}")

    vis = brief.get("visibility")
    if vis and vis.get("ai_score") is not None:
        age = (f", measured {vis['as_of']}" + (" — out of date" if vis.get("stale") else "")
               if vis.get("as_of") else "")
        lines.append(f"- AI search visibility: {vis['ai_score']}{age}")

    mkt = brief.get("marketing")
    if mkt and (mkt.get("posts_published") or mkt.get("campaigns_sent")):
        lines.append(f"- Marketing in the same 30 days: {mkt['posts_published']} posts published, "
                     f"{mkt['campaigns_sent']} text campaigns. This is what ELSE was happening — "
                     f"it is not evidence any of it caused the numbers above.")

    # Only worth the tokens when it actually said something. A restaurant on
    # one module would otherwise get a header and a list of the modules it
    # does not have, on every question, forever — so "could not be answered"
    # is appended only when there is something above it to qualify, and
    # modules the client simply does not own are not a gap worth naming.
    if len(lines) == 1:
        return ""
    gaps = [u for u in (brief.get("unanswered") or []) if "not on this plan" not in u]
    if gaps:
        lines.append(f"- Could not be answered: {'; '.join(gaps[:4])}")
    return "\n".join(lines) + "\n"
