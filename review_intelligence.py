"""review_intelligence.py — the step after "food quality is your most-mentioned complaint".

The Reviews module could measure honestly and summarise carefully, and that
is where it stopped. Every substantive claim in its AI output had already been
computed in Python before the model was called; the model's remaining job was
to phrase four pre-computed strings in twenty words each. That is a summariser
with good guardrails, not a consultant.

A consultant's contribution starts at the sentence the module ended on:

    "Food quality is your most-mentioned complaint (11 mentions)."   <- what it did
    "Nine of those eleven name a cold entree, seven landed Friday or
     Saturday dinner, and you cut 14 kitchen hours on exactly those two
     shifts three weeks ago. Most likely an expo/pass problem created by
     the staffing change; the alternative is a hold-time problem at the
     window, which the same reviews cannot separate. Put a manager on
     the pass for two Friday dinners and the two explanations come
     apart."                                                          <- what it does now

This module is the machinery that makes the second paragraph possible, and
every piece of it is built so the model cannot write that paragraph unless the
evidence for it is actually there:

  * `rating_trend`       — a direction with a CONFIDENCE, reusing waste_trend's
                           slope-agreement scorer rather than a bare monotonic
                           all() over three weekly means.
  * `complaint_clusters` — the analyser's new entity fields folded into real
                           clusters: which dish, which role, which daypart,
                           which weekday, with the review ids attached.
  * `operational_context`— what the OTHER modules know about the same period,
                           each line carrying its own staleness.
  * `revenue_at_risk`    — a bounded, sourced range with its inputs shown, or
                           nothing at all when the inputs are missing.
  * `executive_brief`    — deterministic answers to the six questions an owner
                           actually opens the tab with, computed in Python and
                           true before any model runs.
  * `diagnose`           — the root-cause pass. One Sonnet call per cluster,
                           handed the clusters and the operational context,
                           required to cite review ids that exist and to name
                           an alternative explanation and a way to tell them
                           apart. Anything it cites that we did not give it is
                           rejected, the same way `verify_figures` rejects a
                           number we did not give it.

Everything here is read-only against the database except `diagnose`, which
writes its result to `review_diagnoses`.
"""
import json
from datetime import datetime, timezone

from models import DB_PATH, get_conn, REVIEW_TIME_AXIS_BARE

_AXIS = REVIEW_TIME_AXIS_BARE

# How many mentions a cluster needs before it is worth a paragraph. Same idea
# and the same number as notify.MIN_TREND_REVIEWS_PER_WEEK and
# models.MIN_TOPIC_TREND_MENTIONS: below this a "pattern" is a handful of
# guests, and a consultant who calls three reviews a trend is worse than no
# consultant.
MIN_CLUSTER_MENTIONS = 3

# A concentration has to beat chance by a real margin before it is called a
# concentration. Seven of eleven complaints landing on two of seven weekdays
# is a signal; four of eleven is what random scatter looks like.
CONCENTRATION_MIN_SHARE = 0.50

# How far back a diagnosis looks, and how long one stays fresh. Reviews arrive
# a few a day at best, so a cause re-derived hourly would be the same cause
# with a different sentence — which reads as instability, not insight.
DIAGNOSIS_WINDOW_DAYS = 90
DIAGNOSIS_TTL_HOURS = 24

# At most this many clusters get a diagnosis in one pass. Each is a Sonnet
# call; an owner cannot act on six root causes at once anyway, and the ranking
# below puts the ones worth reading first.
MAX_DIAGNOSES_PER_RUN = 3

# ── Revenue at risk ─────────────────────────────────────────────────────────
#
# The published elasticity everyone in this industry cites traces to the
# Harvard Business School working paper on Yelp ratings (Luca), which found a
# one-star increase moved independent-restaurant revenue 5-9%. That is a
# RANGE from ONE study on ONE platform in ONE city, so it is carried here as a
# range, applied only to independents, and every number it produces is labelled
# a forecast and shown with its inputs. A point estimate would be a fabricated
# precision; refusing to estimate at all leaves the owner ranking a complaint
# against a labor decision with no common unit.
REVENUE_ELASTICITY_LOW = 0.05
REVENUE_ELASTICITY_HIGH = 0.09
# Below this the rating move is inside the noise of a normal month and an
# estimate off it would be arithmetic on a coincidence.
MIN_RATING_DELTA_FOR_ESTIMATE = 0.15


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _one(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:
        return None


def _age_days(stamp):
    """How many days ago, or None when the stamp is missing or unparseable."""
    if not stamp:
        return None
    text = str(stamp).replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            when = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return max(0, (datetime.now(timezone.utc) - when).days)
        except ValueError:
            continue
    return None


# ── Rating trend, with a confidence that means something ────────────────────

def rating_trend(restaurant_id: int, weeks: int = 8, db_path: str = DB_PATH) -> dict:
    """The weekly rating series with a direction, a confidence and anomalies.

    The old trend call was `all(ratings[i] <= ratings[i+1] ...)` over at most
    four weekly means — a direction asserted from three data points, stated
    with identical certainty whether it rested on 9 reviews or 900, and with
    no notion of a week that simply sat outside the series' own spread.

    waste_trend already solved exactly this problem for waste dollars: a
    least-squares direction, a confidence scored on series length AND the
    fraction of week-over-week moves that agree with the fitted slope, and an
    anomaly test that needs both a z-score and a real absolute move. Those two
    helpers are reused verbatim here rather than reimplemented, so ratings and
    waste can never disagree about what "medium confidence" means.
    """
    from waste_trend import _confidence, _anomalies, WEEKS_FOR_TREND
    conn = get_conn(db_path)
    rows = _rows(conn, f"""
        SELECT strftime('%Y-W%W', {_AXIS}) AS week,
               COUNT(*) AS cnt,
               AVG(rating) AS avg_r,
               SUM(sentiment='negative') AS neg
        FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL
          AND {_AXIS} >= datetime('now', ?)
        GROUP BY week ORDER BY week
    """, (restaurant_id, f"-{int(weeks) * 7} days"))
    conn.close()

    from notify import MIN_TREND_REVIEWS_PER_WEEK
    series = [{"week": r["week"], "count": r["cnt"] or 0,
               "avg_rating": round(_f(r["avg_r"]), 2),
               "negative": r["neg"] or 0}
              for r in rows]
    # Only weeks with enough reviews behind them can carry a direction. A week
    # holding one 5-star review is a guest, not a data point.
    solid = [w for w in series if w["count"] >= MIN_TREND_REVIEWS_PER_WEEK]
    out = {
        "series": series,
        "weeks_with_data": len(series),
        "weeks_above_floor": len(solid),
        "min_reviews_per_week": MIN_TREND_REVIEWS_PER_WEEK,
        "direction": None, "confidence": None, "slope": None,
        "first": None, "latest": None, "anomalies": [],
        # Why there is no direction, when there isn't one — so the UI and the
        # prompt can both say the honest thing instead of showing a blank.
        "reason": None,
    }
    if len(solid) < WEEKS_FOR_TREND:
        out["reason"] = (f"only {len(solid)} of the last {weeks} weeks have "
                         f"{MIN_TREND_REVIEWS_PER_WEEK}+ reviews — not enough to call a direction")
        return out

    values = [w["avg_rating"] for w in solid]
    n = len(values)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    slope = (sum((i - mean_x) * (values[i] - mean_y) for i in range(n)) / denom) if denom else 0.0
    change = values[-1] - values[0]
    # A tenth of a star across the whole window is not a movement an owner
    # should be told about; it is rounding on a handful of reviews.
    if abs(change) < 0.15:
        direction = "flat"
    else:
        direction = "improving" if slope > 0 else "declining"
    out.update({
        "direction": direction,
        "slope": round(slope, 3),
        "change": round(change, 2),
        "first": values[0],
        "latest": values[-1],
        # _confidence expects "higher is worse" for waste; for ratings higher
        # is better, but it only ever compares the SIGN of each week-over-week
        # move against the sign of the slope, so the semantics carry over
        # unchanged — it is measuring how consistently the series moves one
        # way, not whether that way is good.
        "confidence": _confidence(values, slope),
        "anomalies": [
            {"week": solid[i]["week"], "kind": flag, "avg_rating": values[i],
             "count": solid[i]["count"]}
            for i, flag in enumerate(_anomalies(values)) if flag
        ],
    })
    return out


# ── What the complaints are actually about ──────────────────────────────────

def _parse_entities(raw):
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def complaint_clusters(restaurant_id: int, days: int = DIAGNOSIS_WINDOW_DAYS,
                       db_path: str = DB_PATH) -> list:
    """Negative reviews folded into clusters an owner can act on.

    One cluster per category, carrying the dishes, roles, dayparts and
    weekdays its reviews actually name, the specific complaints in the guests'
    own terms, and the review ids behind it.

    This is the piece that did not exist. Before the analyser stored entities,
    the finest grain available anywhere downstream was one of eight category
    words, so "our Friday dinner expo is failing" and "one guest didn't like
    the risotto" were the same row in the same chart. A cluster is only
    reported when it clears MIN_CLUSTER_MENTIONS, and a concentration
    (weekday, daypart, dish, role) is only reported when it clears both that
    floor and CONCENTRATION_MIN_SHARE — so the structure cannot manufacture a
    pattern out of two reviews.
    """
    conn = get_conn(db_path)
    rows = _rows(conn, f"""
        SELECT id, rating, categories, entities, specific_complaint, severity,
               author, text, {_AXIS} AS occurred_at
        FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1
          AND sentiment='negative'
          AND {_AXIS} >= datetime('now', ?)
        ORDER BY occurred_at DESC
    """, (restaurant_id, f"-{int(days)} days"))
    conn.close()

    buckets = {}
    for r in rows:
        try:
            cats = json.loads(r["categories"] or "[]")
        except Exception:
            cats = []
        ents = _parse_entities(r["entities"] if "entities" in r.keys() else None)
        weekday = None
        occurred = str(r["occurred_at"] or "")[:10]
        if occurred:
            try:
                weekday = _WEEKDAYS[int(datetime.strptime(occurred, "%Y-%m-%d").strftime("%w"))]
            except ValueError:
                weekday = None
        for cat in cats:
            if not cat:
                continue
            b = buckets.setdefault(cat, {
                "category": cat, "review_ids": [], "ratings": [],
                "dishes": {}, "staff_roles": {}, "dayparts": {}, "weekdays": {},
                "complaints": [], "severities": {}, "latest": None, "earliest": None,
            })
            b["review_ids"].append(r["id"])
            b["ratings"].append(r["rating"] or 0)
            for d in (ents.get("dishes") or []):
                b["dishes"][d] = b["dishes"].get(d, 0) + 1
            for role in (ents.get("staff_roles") or []):
                b["staff_roles"][role] = b["staff_roles"].get(role, 0) + 1
            if ents.get("daypart"):
                b["dayparts"][ents["daypart"]] = b["dayparts"].get(ents["daypart"], 0) + 1
            if weekday:
                b["weekdays"][weekday] = b["weekdays"].get(weekday, 0) + 1
            sev = (r["severity"] if "severity" in r.keys() else None) or "service"
            b["severities"][sev] = b["severities"].get(sev, 0) + 1
            sc = (r["specific_complaint"] if "specific_complaint" in r.keys() else None)
            if sc and len(b["complaints"]) < 8:
                b["complaints"].append({"review_id": r["id"], "complaint": sc,
                                        "rating": r["rating"], "when": occurred})
            if occurred:
                if not b["latest"] or occurred > b["latest"]:
                    b["latest"] = occurred
                if not b["earliest"] or occurred < b["earliest"]:
                    b["earliest"] = occurred

    def _concentration(counts, total):
        """The single dominant value in a count map, or None.

        Returns None rather than the top entry whenever the top entry is not
        actually dominant — a "concentration" that holds 2 of 11 mentions is
        the shape of random scatter, and naming it would hand the model a
        pattern to explain that isn't there.
        """
        if not counts or total < MIN_CLUSTER_MENTIONS:
            return None
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if n < MIN_CLUSTER_MENTIONS or (n / total) < CONCENTRATION_MIN_SHARE:
            return None
        return {"value": top, "count": n, "share": round(n / total, 2)}

    out = []
    for cat, b in buckets.items():
        total = len(b["review_ids"])
        if total < MIN_CLUSTER_MENTIONS:
            continue
        # Two weekdays together, not just one — "Friday AND Saturday dinner"
        # is the real shape of a weekend-service problem and a single-day test
        # would miss it entirely.
        pair = None
        if len(b["weekdays"]) >= 2:
            top2 = sorted(b["weekdays"].items(), key=lambda kv: kv[1], reverse=True)[:2]
            pair_n = top2[0][1] + top2[1][1]
            if pair_n >= MIN_CLUSTER_MENTIONS and (pair_n / total) >= CONCENTRATION_MIN_SHARE:
                pair = {"days": [top2[0][0], top2[1][0]], "count": pair_n,
                        "share": round(pair_n / total, 2)}
        worst = min((s for s in b["severities"]),
                    key=lambda s: _SEVERITY_ORDER.get(s, 99), default="service")
        out.append({
            "category": cat,
            "mentions": total,
            "review_ids": b["review_ids"][:25],
            "avg_rating": round(sum(b["ratings"]) / total, 2) if total else None,
            "dish": _concentration(b["dishes"], total),
            "role": _concentration(b["staff_roles"], total),
            "daypart": _concentration(b["dayparts"], total),
            "weekday": _concentration(b["weekdays"], total),
            "weekday_pair": pair,
            "complaints": b["complaints"],
            "worst_severity": worst,
            "severity_counts": b["severities"],
            "first_seen": b["earliest"],
            "last_seen": b["latest"],
            "window_days": int(days),
        })
    # Rank by what an owner should read first: how serious the worst review in
    # the cluster is, then how many guests it represents.
    out.sort(key=lambda c: (_SEVERITY_ORDER.get(c["worst_severity"], 99), -c["mentions"]))
    return out


_SEVERITY_ORDER = {"safety": 0, "legal": 1, "operational": 2, "service": 3, "minor": 4}


def severity_breakdown(restaurant_id: int, days: int = 90, db_path: str = DB_PATH) -> dict:
    """How many open complaints sit in each severity tier.

    The module's only notion of seriousness was binary urgency, which answers
    "wake the owner up?" and cannot answer "where does this sit when they are
    ranking twelve things on a Tuesday morning?".
    """
    from analyser import SEVERITIES, SEVERITY_LABELS
    conn = get_conn(db_path)
    rows = _rows(conn, f"""
        SELECT severity, COUNT(*) AS n,
               SUM(response_status NOT IN ('posted','approved','skipped')) AS open_n
        FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1
          AND severity IS NOT NULL
          AND {_AXIS} >= datetime('now', ?)
        GROUP BY severity
    """, (restaurant_id, f"-{int(days)} days"))
    conn.close()
    by = {r["severity"]: {"total": r["n"] or 0, "open": r["open_n"] or 0} for r in rows}
    return {
        "days": int(days),
        "tiers": [
            {"key": s, "label": SEVERITY_LABELS.get(s, s),
             "total": by.get(s, {}).get("total", 0),
             "open": by.get(s, {}).get("open", 0)}
            for s in SEVERITIES
        ],
        # Reviews analysed before the severity column existed. Reported rather
        # than folded into "minor", so a thin breakdown reads as thin coverage
        # and not as a calm restaurant.
        "unclassified": _unclassified_count(restaurant_id, days, db_path),
    }


def _unclassified_count(restaurant_id, days, db_path):
    conn = get_conn(db_path)
    row = _one(conn, f"""
        SELECT COUNT(*) AS n FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1
          AND severity IS NULL AND {_AXIS} >= datetime('now', ?)
    """, (restaurant_id, f"-{int(days)} days"))
    conn.close()
    return (row["n"] if row else 0) or 0


def daypart_breakdown(restaurant_id: int, days: int = 90, db_path: str = DB_PATH) -> dict:
    """Negative-review rate by weekday and by daypart.

    No review query in the codebase grouped by weekday or daypart, so the most
    natural operational question an owner has — "is this a weekend problem?" —
    had no answer anywhere. Rates, not counts: a Saturday with twice the
    covers will carry twice the complaints at identical quality, and a raw
    count would call that a Saturday problem.
    """
    conn = get_conn(db_path)
    rows = _rows(conn, f"""
        SELECT {_AXIS} AS occurred_at, sentiment, rating, entities
        FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1
          AND {_AXIS} >= datetime('now', ?)
    """, (restaurant_id, f"-{int(days)} days"))
    conn.close()

    wd, dp = {}, {}
    for r in rows:
        occurred = str(r["occurred_at"] or "")[:10]
        neg = 1 if r["sentiment"] == "negative" else 0
        if occurred:
            try:
                label = _WEEKDAYS[int(datetime.strptime(occurred, "%Y-%m-%d").strftime("%w"))]
                e = wd.setdefault(label, {"total": 0, "negative": 0, "rating_sum": 0.0})
                e["total"] += 1
                e["negative"] += neg
                e["rating_sum"] += _f(r["rating"])
            except ValueError:
                pass
        part = _parse_entities(r["entities"] if "entities" in r.keys() else None).get("daypart")
        if part:
            e = dp.setdefault(part, {"total": 0, "negative": 0, "rating_sum": 0.0})
            e["total"] += 1
            e["negative"] += neg
            e["rating_sum"] += _f(r["rating"])

    def _shape(d, key):
        out = []
        for label, e in d.items():
            # A bucket under the floor gets its counts reported and its RATE
            # withheld — a 100% negative rate on one review is a number that
            # will be read as a finding.
            enough = e["total"] >= MIN_CLUSTER_MENTIONS
            out.append({
                key: label, "total": e["total"], "negative": e["negative"],
                "negative_pct": round(e["negative"] / e["total"] * 100) if enough else None,
                "avg_rating": round(e["rating_sum"] / e["total"], 2) if enough else None,
                "below_floor": not enough,
            })
        out.sort(key=lambda x: (x["negative_pct"] is None, -(x["negative_pct"] or 0)))
        return out

    return {"days": int(days), "min_per_bucket": MIN_CLUSTER_MENTIONS,
            "by_weekday": _shape(wd, "weekday"), "by_daypart": _shape(dp, "daypart")}


# ── What the other modules know about the same period ───────────────────────

def operational_context(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Labor, food cost, waste and marketing for the same restaurant, each
    line carrying its own staleness and its own live/sample state.

    The Reviews module read none of this. Every one of these functions is in
    the same process — ask_cavnar.build_context already composes all of them
    into one snapshot — so "slow service complaints are up" and "you cut 14
    labor hours on Saturdays" sat in the same database, one tab apart, and
    were never joined.

    Sample data is refused outright rather than reported, following
    ask_cavnar's precedent: answering from the bundled placeholder pantry as
    if it were this restaurant's numbers is worse than saying nothing.
    """
    ctx = {"labor": None, "food_cost": None, "waste": None, "marketing": None,
           "notes": []}

    try:
        from labor import analyse_shifts_for_restaurant
        a = analyse_shifts_for_restaurant(restaurant_id)
        if a and a.get("is_live"):
            rng = a.get("date_range") or {}
            ctx["labor"] = {
                "labor_pct": round(_f(a.get("overall_labor_pct")), 1),
                "target_pct": round(_f(a.get("labor_target"), 30.0), 1),
                "total_sales": round(_f(a.get("total_sales"))),
                "period_days": a.get("period_days"),
                "overstaffed_days": len(a.get("overstaffed_days") or []),
                "understaffed_days": len(a.get("understaffed_days") or []),
                "covers_from": rng.get("start"), "covers_to": rng.get("end"),
                "age_days": _age_days(rng.get("end")),
            }
        elif a:
            ctx["notes"].append("Labor: sample data only — no shifts uploaded, so labor cannot be used as evidence.")
    except Exception:
        pass

    try:
        from inventory import analysis_for
        inv, inv_live, analysis = analysis_for(restaurant_id)
        if inv and inv_live and analysis:
            waste_items = analysis.get("waste_items") or []
            ctx["food_cost"] = {
                "waste_cost_week": round(_f(analysis.get("total_waste_cost_week")), 2),
                "top_waste_item": (waste_items[0].get("item") if waste_items else None),
                "critical_low": len(analysis.get("critical_low") or []),
            }
        elif inv:
            ctx["notes"].append("Food cost: sample data only — no inventory uploaded, so waste cannot be used as evidence.")
    except Exception:
        pass

    try:
        from waste_trend import build_waste_trend
        wt = build_waste_trend(restaurant_id) or {}
        stats = wt.get("stats") or {}
        if stats.get("direction"):
            ctx["waste"] = {
                "direction": stats.get("direction"),
                "change_pct": stats.get("change_pct"),
                "confidence": stats.get("confidence"),
                "weeks": stats.get("weeks"),
            }
    except Exception:
        pass

    try:
        conn = get_conn(db_path)
        rows = _rows(conn, """
            SELECT topic, reach, impressions, created_at
            FROM marketing_content_log
            WHERE restaurant_id=? AND post_id IS NOT NULL
              AND (reach > 0 OR impressions > 0)
              AND created_at >= datetime('now','-30 days')
            ORDER BY (COALESCE(reach,0) + COALESCE(impressions,0)) DESC LIMIT 3
        """, (restaurant_id,))
        conn.close()
        if rows:
            ctx["marketing"] = {
                "posts_30d": len(rows),
                "best_topic": rows[0]["topic"],
                "best_reach": (rows[0]["reach"] or 0) + (rows[0]["impressions"] or 0),
            }
    except Exception:
        pass

    return ctx


def competitor_benchmark(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """This restaurant's own rating against the competitors Intel already
    tracks, with the intel's age attached.

    Intel holds every competitor's rating and review count and runs a rigorous
    evidence-bounded prompt over their reviews. The Reviews module never put
    the owner's own number beside them, so "is 4.1 good?" — the first question
    anyone asks about a rating — had no answer in the module that owns ratings.
    """
    from models import get_restaurant
    from ai_guard import freshness
    r = get_restaurant(restaurant_id)
    if not r or not getattr(r, "competitor_intel", None):
        return {"available": False, "reason": "no competitor intel on file"}
    try:
        blob = json.loads(r.competitor_intel)
    except Exception:
        return {"available": False, "reason": "competitor intel is not readable"}
    comps = [c for c in (blob.get("competitors") or []) if c.get("rating")]
    if len(comps) < 2:
        return {"available": False, "reason": "fewer than two competitors with a rating"}

    conn = get_conn(db_path)
    row = _one(conn, f"""
        SELECT ROUND(AVG(rating),2) AS r, COUNT(*) AS n FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL
          AND {_AXIS} >= datetime('now','-90 days')
    """, (restaurant_id,))
    conn.close()
    ours_90d = _f(row["r"]) if row and row["r"] else None
    ratings = sorted(_f(c["rating"]) for c in comps)
    median = ratings[len(ratings) // 2] if len(ratings) % 2 else \
        round((ratings[len(ratings) // 2 - 1] + ratings[len(ratings) // 2]) / 2, 2)
    fresh = freshness(getattr(r, "competitor_updated_at", None))
    # Google's own published aggregate is a different number from our
    # review-analysis average and is labelled as such everywhere else in this
    # codebase; both are reported rather than blended.
    return {
        "available": True,
        "our_rating_90d": ours_90d,
        "our_reviews_90d": (row["n"] if row else 0) or 0,
        "our_google_rating": getattr(r, "gbp_rating", None),
        "competitor_median": median,
        "competitor_best": {"name": max(comps, key=lambda c: _f(c["rating"])).get("name"),
                            "rating": max(_f(c["rating"]) for c in comps)},
        "competitor_count": len(comps),
        "gap_vs_median": round(ours_90d - median, 2) if ours_90d else None,
        "as_of": fresh.get("as_of"), "age_days": fresh.get("age_days"),
        "stale": fresh.get("stale"),
    }


def location_comparison(restaurant_id: int, days: int = 90, db_path: str = DB_PATH) -> dict:
    """The same complaint themes across every location in this group.

    Home's multi-location rollup compares siblings on urgent count, awaiting
    count and 30-day rating — which says which location needs attention, not
    what is wrong with it. For a brand, "location B's wait-time complaints run
    three times location A's" is the highest-value review question there is.

    Scoped by location_group AND owner_email, the tenancy boundary the rest of
    the codebase uses.
    """
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not getattr(r, "location_group", None):
        return {"available": False, "reason": "single location"}
    conn = get_conn(db_path)
    sibs = _rows(conn, """
        SELECT id, COALESCE(location_name, name) AS label FROM restaurants
        WHERE location_group=? AND owner_email=? ORDER BY label
    """, (r.location_group, r.owner_email))
    if len(sibs) < 2:
        conn.close()
        return {"available": False, "reason": "single location"}

    out = []
    for s in sibs:
        rows = _rows(conn, f"""
            SELECT categories FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1
              AND sentiment='negative' AND categories IS NOT NULL AND categories != '[]'
              AND {_AXIS} >= datetime('now', ?)
        """, (s["id"], f"-{int(days)} days"))
        tot = _one(conn, f"""
            SELECT COUNT(*) AS n, ROUND(AVG(rating),2) AS r FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL
              AND {_AXIS} >= datetime('now', ?)
        """, (s["id"], f"-{int(days)} days"))
        counts = {}
        for row in rows:
            try:
                for c in json.loads(row["categories"] or "[]"):
                    if c:
                        counts[c] = counts.get(c, 0) + 1
            except Exception:
                pass
        n = (tot["n"] if tot else 0) or 0
        out.append({
            "restaurant_id": s["id"], "label": s["label"],
            "reviews": n, "avg_rating": (_f(tot["r"]) if tot and tot["r"] else None),
            "is_this_one": s["id"] == restaurant_id,
            # Share of that location's own reviews, not a raw count — a
            # location with three times the volume carries three times the
            # complaints at identical quality.
            "complaint_share": {k: round(v / n, 3) for k, v in counts.items()} if n >= MIN_CLUSTER_MENTIONS else {},
            "below_floor": n < MIN_CLUSTER_MENTIONS,
        })
    conn.close()

    # Only call out a theme where this location genuinely stands apart from
    # the group, and only where both sides clear the floor.
    mine = next((o for o in out if o["is_this_one"]), None)
    others = [o for o in out if not o["is_this_one"] and not o["below_floor"]]
    outliers = []
    if mine and not mine["below_floor"] and others:
        for cat, share in mine["complaint_share"].items():
            peer = [o["complaint_share"].get(cat, 0.0) for o in others]
            peer_avg = sum(peer) / len(peer) if peer else 0.0
            if share >= 0.10 and share >= peer_avg * 1.75 and (share - peer_avg) >= 0.05:
                outliers.append({"category": cat, "our_share": round(share, 3),
                                 "peer_share": round(peer_avg, 3)})
        outliers.sort(key=lambda o: o["peer_share"] - o["our_share"])
    return {"available": True, "days": int(days), "locations": out,
            "outlier_themes": outliers[:3]}


# ── What it is worth ────────────────────────────────────────────────────────

def revenue_at_risk(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """A bounded monthly revenue range implied by a rating movement, or
    nothing at all.

    The module had no financial dimension of any kind, so "what should I fix
    first?" was answered by mention count — and mention count is not severity
    and is certainly not cost. An owner ranking a review complaint against a
    labor decision had no common unit to rank them in.

    Everything this returns is a FORECAST built on one published elasticity
    range applied to this restaurant's own trailing sales, and it returns
    nothing rather than guessing when either input is missing:

      * no live labor/sales data  -> no estimate (there is no revenue base)
      * rating move inside noise  -> no estimate (arithmetic on a coincidence)

    The inputs travel with the answer so the owner can see exactly what it
    rests on, and the caller is expected to label it `forecast` via
    ai_guard.CLAIM_KINDS.
    """
    conn = get_conn(db_path)
    cur = _one(conn, f"""
        SELECT ROUND(AVG(rating),2) AS r, COUNT(*) AS n FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL
          AND {_AXIS} >= datetime('now','-30 days')
    """, (restaurant_id,))
    prev = _one(conn, f"""
        SELECT ROUND(AVG(rating),2) AS r, COUNT(*) AS n FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL
          AND {_AXIS} >= datetime('now','-90 days')
          AND {_AXIS} <  datetime('now','-30 days')
    """, (restaurant_id,))
    conn.close()

    from notify import MIN_TREND_REVIEWS_PER_WEEK
    floor = MIN_TREND_REVIEWS_PER_WEEK * 4  # a month's worth at the weekly floor
    if not cur or not prev or (cur["n"] or 0) < floor or (prev["n"] or 0) < floor:
        return {"available": False,
                "reason": f"needs {floor}+ reviews in both the last 30 days and the 60 before it"}
    delta = _f(cur["r"]) - _f(prev["r"])
    if abs(delta) < MIN_RATING_DELTA_FOR_ESTIMATE:
        return {"available": False,
                "reason": f"rating moved {delta:+.2f}★ — inside normal month-to-month noise"}

    monthly_sales = None
    source = None
    try:
        from labor import analyse_shifts_for_restaurant
        a = analyse_shifts_for_restaurant(restaurant_id)
        if a and a.get("is_live") and _f(a.get("total_sales")) > 0 and _f(a.get("period_days")) > 0:
            monthly_sales = _f(a["total_sales"]) / _f(a["period_days"]) * 30.0
            source = f"{int(_f(a['period_days']))} days of synced sales"
    except Exception:
        pass
    if not monthly_sales:
        conn = get_conn(db_path)
        row = _one(conn, """
            SELECT AVG(sales) AS s, COUNT(*) AS n FROM labor_daily_history
            WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0
              AND date >= date('now','-90 days')
        """, (restaurant_id,))
        conn.close()
        if row and _f(row["s"]) > 0 and (row["n"] or 0) >= 14:
            monthly_sales = _f(row["s"]) * 30.0
            source = f"{row['n']} days of recorded sales"
    if not monthly_sales:
        return {"available": False,
                "reason": "no sales data — a revenue estimate needs a revenue base"}

    low = monthly_sales * REVENUE_ELASTICITY_LOW * delta
    high = monthly_sales * REVENUE_ELASTICITY_HIGH * delta
    return {
        "available": True,
        "claim_kind": "forecast",
        "rating_delta": round(delta, 2),
        "direction": "at_risk" if delta < 0 else "upside",
        "monthly_low": round(min(low, high)),
        "monthly_high": round(max(low, high)),
        "monthly_sales_basis": round(monthly_sales),
        "sales_source": source,
        "reviews_recent": cur["n"], "reviews_prior": prev["n"],
        "elasticity_low_pct": REVENUE_ELASTICITY_LOW * 100,
        "elasticity_high_pct": REVENUE_ELASTICITY_HIGH * 100,
        "assumption": ("Applies a published 5-9% revenue-per-star range for independent "
                       "restaurants to this restaurant's own trailing sales. It is an "
                       "order-of-magnitude range, not a measurement of this business."),
    }


# ── The six questions an owner actually opens the tab with ──────────────────

def executive_brief(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Deterministic answers to the six executive questions, computed in
    Python and true before any model runs.

    An owner could not answer "what are my three biggest operational problems"
    or "what should I fix first" from this module: it offered the top three
    CATEGORIES ranked by mention count, which is neither a problem nor a
    priority. This ranks by severity first and volume second, attaches the
    evidence to each line, and says plainly when a question cannot be answered
    rather than filling the slot.
    """
    from models import get_review_stats
    stats = get_review_stats(restaurant_id)
    clusters = complaint_clusters(restaurant_id, db_path=db_path)
    trend = rating_trend(restaurant_id, db_path=db_path)
    money = revenue_at_risk(restaurant_id, db_path=db_path)
    sev = severity_breakdown(restaurant_id, db_path=db_path)

    problems = []
    for c in clusters[:3]:
        where = []
        if c["weekday_pair"]:
            where.append(" and ".join(c["weekday_pair"]["days"]))
        elif c["weekday"]:
            where.append(c["weekday"]["value"])
        if c["daypart"]:
            where.append(c["daypart"]["value"].replace("_", " "))
        detail = c["dish"]["value"] if c["dish"] else (c["role"]["value"] if c["role"] else None)
        problems.append({
            "category": c["category"],
            "mentions": c["mentions"],
            "severity": c["worst_severity"],
            "concentrated_on": " ".join(where) or None,
            "specific": detail,
            "evidence": f"{c['mentions']} negative reviews over {c['window_days']} days",
            "review_ids": c["review_ids"][:5],
        })

    fix_first = None
    if problems:
        top = problems[0]
        fix_first = {
            "what": top["category"],
            "why": ("it carries the most serious complaints in the window"
                    if top["severity"] in ("safety", "legal", "operational")
                    else "it is the most-mentioned complaint theme"),
            "evidence": top["evidence"],
            "specific": top["specific"], "concentrated_on": top["concentrated_on"],
        }

    improved, worsened = [], []
    if trend["direction"] == "improving":
        improved.append({"what": f"Rating up {abs(trend['change']):.2f}★ over {trend['weeks_above_floor']} weeks",
                         "confidence": trend["confidence"]})
    elif trend["direction"] == "declining":
        worsened.append({"what": f"Rating down {abs(trend['change']):.2f}★ over {trend['weeks_above_floor']} weeks",
                         "confidence": trend["confidence"]})
    if stats.get("response_rate", 0) >= 80:
        improved.append({"what": f"{stats['response_rate']:.0f}% of reviews answered", "confidence": "high"})

    return {
        "biggest_problems": problems,
        "fix_first": fix_first,
        "costing_money": money if money.get("available") else
            {"available": False, "reason": money.get("reason")},
        "hurting_reviews_most": (
            {"category": clusters[0]["category"], "mentions": clusters[0]["mentions"],
             "avg_rating": clusters[0]["avg_rating"]} if clusters else None),
        "improved": improved,
        "worsened": worsened,
        "discuss_tomorrow": [p["category"] for p in problems[:2]] or None,
        "severity": sev,
        "trend": {"direction": trend["direction"], "confidence": trend["confidence"],
                  "reason": trend["reason"]},
        "coverage": {"total": stats.get("total", 0),
                     "unanalysed": stats.get("unanalysed", 0),
                     "classified": stats.get("classified", 0)},
    }


# ── The root-cause pass ─────────────────────────────────────────────────────

CONFIDENCES = ("high", "medium", "low")

# The operational chains a restaurant consultant actually reasons along. This
# is given to the model as a VOCABULARY, not as a lookup table: naming the
# kinds of cause that exist stops it inventing a category of explanation, and
# the evidence rules below stop it picking one it cannot support. Without this
# the model reaches for whatever sounds plausible, which is how "consider
# retraining your staff" gets written about a cold-food complaint that is
# really a pass-window problem.
CAUSE_VOCABULARY = """\
- Staffing level on a specific shift (too few on the floor or the line)
- Scheduling shape (the right headcount at the wrong hours, or no overlap at a peak)
- Kitchen throughput (ticket times, station bottleneck, a single overloaded station)
- Expo / pass process (food correct but cold or late leaving the window)
- Recipe execution consistency (the same dish right one night and wrong the next)
- Sourcing or product quality (an ingredient or supplier changed)
- Training or onboarding gap (a specific role not knowing the standard)
- Front-of-house process (seating, greeting, check-back cadence, closing out)
- Reservation or waitlist handling (quoted times, honouring bookings)
- Physical plant (noise, temperature, cleanliness, a broken fixture)
- Demand exceeding what was staffed for (a promotion, an event, a busy period)
- Menu or pricing expectation mismatch (value perception against the price point)"""

DIAGNOSE_PROMPT = """You are an experienced restaurant operations consultant. You have been given one cluster of negative guest reviews from a single restaurant, plus what the restaurant's other systems recorded over the same period.

Your job is the step AFTER counting complaints: say what operational problem most likely produced them, what else it could be, and how the owner could tell the difference. Return ONLY valid JSON — no markdown, no commentary.

{untrusted_note}

RESTAURANT: {restaurant_name}
TODAY: {today}

THE CLUSTER
Theme: {category}
{mentions} negative reviews between {first_seen} and {last_seen} ({window_days}-day window)
Average rating in this cluster: {avg_rating}
Most serious review in it: {worst_severity}
{concentration_block}
What guests specifically said went wrong (review id -> complaint):
{complaint_block}

Guest review excerpts (review id -> text):
{excerpt_block}

WHAT THE OTHER SYSTEMS RECORDED OVER THE SAME PERIOD
{operational_block}

CAUSE VOCABULARY — pick from these kinds of cause:
{cause_vocabulary}

EVIDENCE RULES — these bound what you may claim:
- `evidence_review_ids` MUST be ids listed above. Never write an id that is not on this page. An id you did not see is a fabricated citation.
- State no figure — a dollar amount, a percentage, a count, a rating — that does not appear above.
- Name a person, a dish, a role, a shift or a weekday ONLY if it appears above. If no dish is listed, your cause may not turn on a dish.
- You may connect this cluster to a figure under "WHAT THE OTHER SYSTEMS RECORDED" only by naming that figure in `operational_evidence`. If that section is empty or says data is unavailable, you have NO operational evidence — say so, and let that pull your confidence down.
- Correlation in a 90-day window is not proof. If the reviews and a figure moved together, say they moved together; do not say one caused the other.
- `confidence` is "high" only when the complaints are specific AND concentrated AND a figure from another system points the same way. It is "low" when you are reasoning mostly from the theme name.
- If the evidence genuinely does not identify a cause, say that in `cause` and set confidence "low". A stated uncertainty is worth more than a confident guess, and this text goes to an owner who may act on it.

Return this exact shape:
{{
  "cause": "the single most likely operational cause, 1-2 sentences, specific to what is above",
  "alternative_cause": "the next most likely explanation the same evidence also fits, 1 sentence",
  "what_would_confirm": "one concrete thing the owner could check or observe this week that would tell the two apart, 1 sentence",
  "evidence_review_ids": [ids from above that this cause rests on, 2-6 of them],
  "operational_evidence": [{{"module": "labor|food_cost|waste|marketing", "metric": "what it is", "value": "the figure exactly as given above"}}],
  "confidence": "high" | "medium" | "low",
  "recommended_action": "one thing a manager can start within a week using only the staff, menu and equipment they already have, 1 sentence",
  "expected_outcome": "what the owner should see change if the cause is right, and roughly when, 1 sentence"
}}"""


def _diagnosis_inputs(restaurant_id, cluster, db_path):
    """The prompt's evidence blocks, plus the set of ids the answer may cite."""
    conn = get_conn(db_path)
    ids = cluster["review_ids"][:12]
    placeholders = ",".join("?" for _ in ids) if ids else "NULL"
    rows = _rows(conn, f"""
        SELECT id, rating, text FROM reviews
        WHERE restaurant_id=? AND id IN ({placeholders})
        ORDER BY rating ASC LIMIT 8
    """, tuple([restaurant_id] + ids)) if ids else []
    conn.close()

    from ai_guard import wrap_untrusted
    excerpts = "\n".join(
        f"  {r['id']} ({r['rating']}★): " + wrap_untrusted((r["text"] or "")[:300])
        for r in rows) or "  (none available)"
    complaints = "\n".join(
        f"  {c['review_id']}: {c['complaint']} ({c['when']})"
        for c in cluster["complaints"]) or "  (no specific complaints extracted)"

    conc = []
    if cluster["weekday_pair"]:
        p = cluster["weekday_pair"]
        conc.append(f"Concentrated on {' and '.join(p['days'])}: {p['count']} of {cluster['mentions']} ({int(p['share']*100)}%)")
    elif cluster["weekday"]:
        w = cluster["weekday"]
        conc.append(f"Concentrated on {w['value']}: {w['count']} of {cluster['mentions']} ({int(w['share']*100)}%)")
    for key, label in (("daypart", "daypart"), ("dish", "dish"), ("role", "role")):
        c = cluster.get(key)
        if c:
            conc.append(f"Concentrated on {label} '{c['value']}': {c['count']} of {cluster['mentions']} ({int(c['share']*100)}%)")
    concentration = "\n".join(conc) if conc else \
        "No concentration: these complaints are spread across days, dayparts, dishes and roles."
    return excerpts, complaints, concentration, {r["id"] for r in rows} | set(ids)


def _operational_block(ctx) -> str:
    """The cross-module evidence, or an explicit statement that there is none.

    An empty block would read to the model as "nothing notable happened",
    which is a different claim from "we have no data" — and it is the second
    one that has to pull the confidence down.
    """
    lines = []
    lab = ctx.get("labor")
    if lab:
        age = f", data through {lab['covers_to']}" + (f" ({lab['age_days']} days ago)" if lab.get("age_days") else "") if lab.get("covers_to") else ""
        lines.append(f"- Labor: {lab['labor_pct']}% of sales against a {lab['target_pct']}% target, "
                     f"{lab['understaffed_days']} understaffed and {lab['overstaffed_days']} overstaffed days "
                     f"over {lab['period_days']} days{age}")
    fc = ctx.get("food_cost")
    if fc:
        top = f", top waste item {fc['top_waste_item']}" if fc.get("top_waste_item") else ""
        lines.append(f"- Food cost: ${fc['waste_cost_week']} of waste this week{top}, "
                     f"{fc['critical_low']} items critically low")
    wt = ctx.get("waste")
    if wt:
        lines.append(f"- Waste trend: {wt['direction']} over {wt['weeks']} weeks "
                     f"({wt['change_pct']}% change, {wt['confidence']} confidence)")
    mk = ctx.get("marketing")
    if mk:
        lines.append(f"- Marketing: {mk['posts_30d']} measured posts in 30 days, "
                     f"best was '{mk['best_topic']}' at {mk['best_reach']} reach+impressions")
    for note in ctx.get("notes") or []:
        lines.append(f"- {note}")
    if not lines:
        return ("(No data from any other module. You have NO operational evidence for this "
                "cluster — reason from the reviews alone and keep confidence at most \"medium\".)")
    return "\n".join(lines)


def _validate_diagnosis(raw, allowed_ids, prompt, restaurant_id):
    """Reject a diagnosis that cites what it was not given.

    The same discipline ai_guard applies to figures, applied to citations. A
    root-cause paragraph is only worth more than a summary because the owner
    can click through to the reviews behind it; an id that does not exist
    breaks that in the one place it matters most.
    """
    if not isinstance(raw, dict):
        raise ValueError("diagnosis was not a JSON object")
    cause = " ".join(str(raw.get("cause") or "").split())[:600]
    if not cause:
        raise ValueError("diagnosis had no cause")
    cited = []
    for v in (raw.get("evidence_review_ids") or []):
        try:
            rid = int(v)
        except (TypeError, ValueError):
            continue
        if rid in allowed_ids and rid not in cited:
            cited.append(rid)
    if not cited:
        raise ValueError("diagnosis cited no review we gave it")
    conf = str(raw.get("confidence") or "").strip().lower()
    conf = conf if conf in CONFIDENCES else "low"

    op = []
    for e in (raw.get("operational_evidence") or [])[:4]:
        if isinstance(e, dict) and e.get("module") in ("labor", "food_cost", "waste", "marketing"):
            op.append({"module": e["module"],
                       "metric": str(e.get("metric") or "")[:80],
                       "value": str(e.get("value") or "")[:80]})

    def _line(key, limit=400):
        return " ".join(str(raw.get(key) or "").split())[:limit] or None

    out = {
        "cause": cause,
        "alternative_cause": _line("alternative_cause"),
        "what_would_confirm": _line("what_would_confirm"),
        "evidence_review_ids": cited,
        "operational_evidence": op,
        "confidence": conf,
        "recommended_action": _line("recommended_action"),
        "expected_outcome": _line("expected_outcome"),
    }

    # Every figure it states has to be one it was handed — the same check the
    # weekly digest and the insight already run. A diagnosis is the most
    # quotable thing this module produces; an invented percentage inside a
    # root-cause paragraph is the hardest kind to catch by eye.
    from ai_guard import verify_figures
    joined = " ".join(v for v in (out["cause"], out["alternative_cause"],
                                  out["what_would_confirm"], out["recommended_action"],
                                  out["expected_outcome"]) if v)
    bad = verify_figures(joined, prompt, "review_diagnosis", restaurant_id)
    if bad:
        # Not dropped: an owner reading a cause with one unverified number is
        # better served by seeing it flagged than by seeing a hole. The flag
        # is what stops the UI presenting it as measured.
        out["unsupported_figures"] = bad
        out["confidence"] = "low"
    return out


def diagnose(restaurant_id: int, db_path: str = DB_PATH, force: bool = False,
             max_clusters: int = MAX_DIAGNOSES_PER_RUN) -> list:
    """Produce and store a root-cause diagnosis for this restaurant's top
    complaint clusters.

    One Sonnet call per cluster. Skips a cluster whose stored diagnosis is
    still inside DIAGNOSIS_TTL_HOURS and whose mention count has not moved,
    because a cause re-derived hourly is the same cause in different words,
    which reads as instability rather than insight.
    """
    import os
    import anthropic
    from ai_utils import create_with_retry, extract_text
    from ai_guard import UNTRUSTED_NOTE
    from models import get_restaurant
    from time_utils import restaurant_now

    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return []
    clusters = complaint_clusters(restaurant_id, db_path=db_path)
    if not clusters:
        return []
    ctx = operational_context(restaurant_id, db_path=db_path)
    op_block = _operational_block(ctx)
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    today = restaurant_now(restaurant).strftime("%B %d, %Y")

    existing = {d["category"]: d for d in get_diagnoses(restaurant_id, db_path=db_path,
                                                        include_stale=True)}
    produced = []
    for cluster in clusters[:max_clusters]:
        prior = existing.get(cluster["category"])
        if not force and prior and not prior.get("stale") \
                and prior.get("mention_count") == cluster["mentions"]:
            produced.append(prior)
            continue
        try:
            excerpts, complaints, concentration, allowed = _diagnosis_inputs(
                restaurant_id, cluster, db_path)
            prompt = DIAGNOSE_PROMPT.format(
                untrusted_note=UNTRUSTED_NOTE,
                restaurant_name=restaurant.name,
                today=today,
                category=cluster["category"].replace("_", " "),
                mentions=cluster["mentions"],
                first_seen=cluster["first_seen"] or "unknown",
                last_seen=cluster["last_seen"] or "unknown",
                window_days=cluster["window_days"],
                avg_rating=cluster["avg_rating"],
                worst_severity=cluster["worst_severity"],
                concentration_block=concentration,
                complaint_block=complaints,
                excerpt_block=excerpts,
                operational_block=op_block,
                cause_vocabulary=CAUSE_VOCABULARY,
            )
            msg = create_with_retry(
                client,
                model=os.getenv("CLAUDE_REPORTER_MODEL", "claude-sonnet-5"),
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
                restaurant_id=restaurant_id,
                action="review_diagnosis",
            )
            if getattr(msg, "stop_reason", None) == "max_tokens":
                raise ValueError("diagnosis was truncated")
            raw = extract_text(msg).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            result = _validate_diagnosis(json.loads(raw), allowed, prompt, restaurant_id)
            money = revenue_at_risk(restaurant_id, db_path=db_path)
            _save_diagnosis(restaurant_id, cluster, result, money, db_path)
            result.update({"category": cluster["category"], "mention_count": cluster["mentions"],
                           "window_days": cluster["window_days"], "stale": False})
            produced.append(result)
        except Exception as e:
            try:
                import ops
                ops.capture(e, job="review_diagnosis",
                            context=f"restaurant_id={restaurant_id} category={cluster['category']}")
            except Exception:
                pass
            if prior:
                produced.append(prior)
    return produced


def _save_diagnosis(restaurant_id, cluster, result, money, db_path):
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO review_diagnoses
            (restaurant_id, category, window_days, mention_count, cause, alternative_cause,
             evidence_review_ids, operational_evidence, confidence, what_would_confirm,
             recommended_action, expected_outcome, revenue_at_risk_low, revenue_at_risk_high,
             generated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(restaurant_id, category, window_days) DO UPDATE SET
            mention_count=excluded.mention_count, cause=excluded.cause,
            alternative_cause=excluded.alternative_cause,
            evidence_review_ids=excluded.evidence_review_ids,
            operational_evidence=excluded.operational_evidence,
            confidence=excluded.confidence, what_would_confirm=excluded.what_would_confirm,
            recommended_action=excluded.recommended_action,
            expected_outcome=excluded.expected_outcome,
            revenue_at_risk_low=excluded.revenue_at_risk_low,
            revenue_at_risk_high=excluded.revenue_at_risk_high,
            generated_at=excluded.generated_at
    """, (restaurant_id, cluster["category"], cluster["window_days"], cluster["mentions"],
          result["cause"], result["alternative_cause"],
          json.dumps(result["evidence_review_ids"]),
          json.dumps(result["operational_evidence"]),
          result["confidence"], result["what_would_confirm"],
          result["recommended_action"], result["expected_outcome"],
          money.get("monthly_low") if money.get("available") else None,
          money.get("monthly_high") if money.get("available") else None))
    conn.commit()
    conn.close()


def get_diagnoses(restaurant_id: int, db_path: str = DB_PATH,
                  include_stale: bool = False) -> list:
    """Stored diagnoses, newest first, each carrying its own age.

    `stale` is computed rather than enforced: a diagnosis past its TTL is
    still the best answer available, and hiding it would leave the owner with
    the bare complaint count the module used to give them. The caller decides
    whether to show it with an "as of" or refresh it.
    """
    conn = get_conn(db_path)
    rows = _rows(conn, """
        SELECT * FROM review_diagnoses WHERE restaurant_id=?
        ORDER BY generated_at DESC
    """, (restaurant_id,))
    conn.close()
    out = []
    for r in rows:
        age_h = None
        stamp = r["generated_at"]
        if stamp:
            days = _age_days(stamp)
            try:
                when = datetime.strptime(str(stamp).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
                age_h = (datetime.utcnow() - when).total_seconds() / 3600.0
            except ValueError:
                age_h = (days * 24.0) if days is not None else None
        stale = bool(age_h is None or age_h > DIAGNOSIS_TTL_HOURS)
        if stale and not include_stale:
            continue
        def _j(v, fallback):
            try:
                return json.loads(v) if v else fallback
            except Exception:
                return fallback
        out.append({
            "category": r["category"], "window_days": r["window_days"],
            "mention_count": r["mention_count"], "cause": r["cause"],
            "alternative_cause": r["alternative_cause"],
            "evidence_review_ids": _j(r["evidence_review_ids"], []),
            "operational_evidence": _j(r["operational_evidence"], []),
            "confidence": r["confidence"], "what_would_confirm": r["what_would_confirm"],
            "recommended_action": r["recommended_action"],
            "expected_outcome": r["expected_outcome"],
            "revenue_at_risk_low": r["revenue_at_risk_low"],
            "revenue_at_risk_high": r["revenue_at_risk_high"],
            "generated_at": r["generated_at"],
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "stale": stale,
        })
    return out
