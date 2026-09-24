"""Level 1: one row per restaurant-week of what this restaurant's own
tables say about it — as ratios, rates and counts.

This is the only table cross-restaurant learning reads, which is why it
holds no dollars, no names and no people. A feature the restaurant cannot
measure is None, never 0, and `completeness` says how many could be.

Every query is `WHERE restaurant_id = ?` over an indexed column; the
nightly pass is bounded and resumable (jobs.py).
"""
import json
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


FEATURE_KEYS = (
    # reviews
    "reviews_30d", "avg_rating_30d", "avg_rating_prior_60d", "avg_rating_delta",
    "reply_rate_30d", "response_24h_rate_30d",
    # labor
    "labor_pct_28d", "labor_pct_sd_28d", "weekend_sales_share_28d",
    "labor_hours_per_1k_28d", "labor_hours_per_1k_day_28d", "labor_hours_per_1k_night_28d",
    "schedules_28d", "schedule_edits_28d", "schedule_adjust_rate",
    # food cost
    "food_cost_pct_28d", "waste_sales_pct_28d",
    # marketing
    "campaigns_28d", "campaign_tap_rate_28d", "campaign_return_rate_28d",
    "posts_28d", "post_cadence_days", "dish_posts_28d", "offer_posts_28d", "occasion_posts_28d",
    "post_lift_median_28d", "item_lift_median_28d", "post_engagement_rate_28d", "guest_list_size",
    # the recommendation loop
    "recs_answered_28d", "recs_done_28d", "recs_declined_28d",
    "outcomes_evaluated_90d", "outcomes_improved_rate_90d",
)

# A feature's unit, for benchmark and pattern sentences.
UNITS = {
    "avg_rating_30d": "★", "avg_rating_delta": "★", "labor_pct_28d": "%", "labor_pct_sd_28d": "pts",
    "food_cost_pct_28d": "%", "waste_sales_pct_28d": "%", "reply_rate_30d": "share",
    "response_24h_rate_30d": "share", "weekend_sales_share_28d": "share", "campaign_tap_rate_28d": "share",
    "campaign_return_rate_28d": "share", "outcomes_improved_rate_90d": "share", "schedule_adjust_rate": "share",
    "post_lift_median_28d": "%", "item_lift_median_28d": "%", "post_engagement_rate_28d": "share",
    "labor_hours_per_1k_28d": "h/$1k", "labor_hours_per_1k_day_28d": "h/$1k", "labor_hours_per_1k_night_28d": "h/$1k",
}

# Features benchmarks are published for (cohort p25/p50/p75). Ratios only.
BENCHMARK_KEYS = ("avg_rating_30d", "response_24h_rate_30d", "reply_rate_30d", "labor_pct_28d",
                  "labor_pct_sd_28d", "labor_hours_per_1k_28d", "labor_hours_per_1k_day_28d",
                  "labor_hours_per_1k_night_28d", "food_cost_pct_28d", "waste_sales_pct_28d", "campaign_tap_rate_28d",
                  "post_lift_median_28d", "post_engagement_rate_28d", "outcomes_improved_rate_90d")


def iso_week(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _d(x):
    return str(x or "")[:10]


def compute(restaurant_id: int, today: date = None, db_path: str = DB_PATH) -> dict:
    """The feature dict for this restaurant as of `today`. Pure read."""
    today = today or date.today()
    d30, d60, d90, d28 = (today - timedelta(days=n) for n in (30, 90, 90, 28))
    f = {k: None for k in FEATURE_KEYS}
    conn = get_conn(db_path)
    try:
        # ── reviews ────────────────────────────────────────────────────────
        rows = conn.execute(
            "SELECT rating, review_date, approved_at, posted_at, response_status FROM reviews "
            "WHERE restaurant_id=? AND COALESCE(review_date, fetched_at) >= ?",
            (restaurant_id, d90.isoformat())).fetchall()
        last30 = [r for r in rows if _d(r["review_date"]) >= d30.isoformat()]
        prior = [r for r in rows if _d(r["review_date"]) < d30.isoformat()]
        # None, not 0, when the restaurant has no review source and nothing
        # on file: "no reviews arrive" is unmeasured, not "0 reviews", and a
        # 0 counted it as measured in completeness (CA3 F12 — group G owns
        # this line and labor_pct_28d below).
        src = conn.execute("SELECT gmb_refresh_token, reviews_live, google_place_id FROM restaurants WHERE id=?",
                           (restaurant_id,)).fetchone()
        has_source = bool(src and (src["gmb_refresh_token"] or (src["reviews_live"] and src["google_place_id"])))
        f["reviews_30d"] = len(last30) if (rows or has_source) else None
        if last30:
            f["avg_rating_30d"] = round(sum(r["rating"] for r in last30) / len(last30), 2)
            replied = [r for r in last30 if r["response_status"] in ("approved", "posted")]
            f["reply_rate_30d"] = round(len(replied) / len(last30), 3)
            within = 0
            timed = 0
            for r in replied:
                stamp = r["posted_at"] or r["approved_at"]
                if not stamp or not r["review_date"]:
                    continue
                try:
                    rd = datetime.fromisoformat(str(r["review_date"])[:19])
                    at = datetime.fromisoformat(str(stamp)[:19])
                except ValueError:
                    continue
                timed += 1
                if (at - rd).total_seconds() <= 24 * 3600:
                    within += 1
            f["response_24h_rate_30d"] = round(within / timed, 3) if timed else None
        if prior:
            f["avg_rating_prior_60d"] = round(sum(r["rating"] for r in prior) / len(prior), 2)
        if f["avg_rating_30d"] is not None and f["avg_rating_prior_60d"] is not None:
            f["avg_rating_delta"] = round(f["avg_rating_30d"] - f["avg_rating_prior_60d"], 2)

        # ── labor ──────────────────────────────────────────────────────────
        lab = conn.execute(
            "SELECT date, labor_pct, sales, total_hours FROM labor_daily_history WHERE restaurant_id=? AND date >= ? ORDER BY date",
            (restaurant_id, d28.isoformat())).fetchall()
        # Days with a sales figure only (a missing figure is NULL — or, on
        # rows from before that fix, 0 — and has no labor %), and SALES-
        # WEIGHTED: the period's labor % is total labor over total sales,
        # the figure labor.py reports. An unweighted mean of daily %s read a
        # slow Monday at 60% as heavily as a $9,000 Saturday (CA3 F12).
        costed = [r for r in lab if r["labor_pct"] is not None and r["sales"] and float(r["sales"]) > 0]
        pcts = [float(r["labor_pct"]) for r in costed]
        if pcts:
            tot_sales = sum(float(r["sales"]) for r in costed)
            f["labor_pct_28d"] = round(sum(float(r["labor_pct"]) * float(r["sales"]) for r in costed)
                                       / tot_sales, 2)
            if len(pcts) >= 5:
                m = sum(pcts) / len(pcts)
                f["labor_pct_sd_28d"] = round((sum((p - m) ** 2 for p in pcts) / (len(pcts) - 1)) ** 0.5, 2)
        sales_days = [(r["date"], float(r["sales"])) for r in lab if r["sales"]]
        if len(sales_days) >= 7:
            total = sum(s for _, s in sales_days)
            wknd = sum(s for dt, s in sales_days if date.fromisoformat(_d(dt)).weekday() >= 4)
            f["weekend_sales_share_28d"] = round(wknd / total, 3) if total else None
        # Hours per $1k of sales — a ratio, so it can be compared across a
        # cohort without a dollar or a name leaving the tenant; by daypart
        # from the schedule's own outcome record when it exists.
        try:
            hrs_rows = [r for r in lab if r["sales"] and r["total_hours"]]
            if len(hrs_rows) >= 7:
                tot_s = sum(float(r["sales"]) for r in hrs_rows)
                tot_h = sum(float(r["total_hours"]) for r in hrs_rows)
                f["labor_hours_per_1k_28d"] = round(tot_h / tot_s * 1000, 2) if tot_s else None
            if _has_col(conn, "schedule_outcomes", "daypart"):
                for part, key in (("morning", "labor_hours_per_1k_day_28d"), ("night", "labor_hours_per_1k_night_28d")):
                    o = conn.execute("SELECT SUM(hours) AS h, SUM(sales) AS s, COUNT(*) AS n FROM schedule_outcomes "
                                     "WHERE restaurant_id=? AND daypart=? AND date >= ? AND sales IS NOT NULL",
                                     (restaurant_id, part, d28.isoformat())).fetchone()
                    if o and (o["n"] or 0) >= 5 and o["s"]:
                        f[key] = round(float(o["h"]) / float(o["s"]) * 1000, 2)
        except Exception:
            pass
        sched = conn.execute(
            "SELECT generated_at, edited_at FROM schedule_history WHERE restaurant_id=? AND generated_at >= ?",
            (restaurant_id, d28.isoformat())).fetchall() if _has_col(conn, "schedule_history", "edited_at") else []
        f["schedules_28d"] = len(sched)
        if sched:
            f["schedule_edits_28d"] = sum(1 for r in sched if r["edited_at"])
            f["schedule_adjust_rate"] = round(f["schedule_edits_28d"] / len(sched), 3)

        # ── food cost ──────────────────────────────────────────────────────
        try:
            import metrics
            fc, _ = metrics.measure(restaurant_id, "food_cost_pct", d28.isoformat(), today.isoformat(), db_path)
            f["food_cost_pct_28d"] = round(float(fc), 2) if fc is not None else None
            waste, _ = metrics.measure(restaurant_id, "weekly_waste", d28.isoformat(), today.isoformat(), db_path)
            sales, _ = metrics.measure(restaurant_id, "sales", d28.isoformat(), today.isoformat(), db_path)
            if waste is not None and sales:
                # weekly waste against weekly sales: sales is per day
                f["waste_sales_pct_28d"] = round(float(waste) / (float(sales) * 7) * 100, 2)
        except Exception:
            pass

        # ── marketing ──────────────────────────────────────────────────────
        try:
            camps = conn.execute(
                "SELECT sent_count, link_token, visits_matched FROM guest_campaigns WHERE restaurant_id=? AND created_at >= ?",
                (restaurant_id, d28.isoformat())).fetchall() if _has_col(conn, "guest_campaigns", "visits_matched") else []
        except Exception:
            camps = []
        f["campaigns_28d"] = len(camps)
        sent = sum(int(c["sent_count"] or 0) for c in camps)
        if sent:
            taps = 0
            for c in camps:
                if c["link_token"]:
                    row = conn.execute("SELECT clicks FROM marketing_links WHERE token=?", (c["link_token"],)).fetchone()
                    taps += int((row["clicks"] if row else 0) or 0)
            f["campaign_tap_rate_28d"] = round(taps / sent, 3)
            measured = [c for c in camps if c["visits_matched"] is not None]
            if measured:
                f["campaign_return_rate_28d"] = round(sum(int(c["visits_matched"]) for c in measured)
                                                      / max(1, sum(int(c["sent_count"] or 0) for c in measured)), 3)
        # Published posts only (post_id set), with what each was about
        # (marketing_tags) and what it did (marketing_attribution).
        tagged = _has_col(conn, "marketing_content_log", "post_kind")
        posts = conn.execute(
            "SELECT c.id, c.created_at, COALESCE(c.posted_at, c.created_at) AS at, "
            + ("c.post_kind, c.occasion, " if tagged else "NULL AS post_kind, NULL AS occasion, ")
            + "COALESCE(c.reach,0)+COALESCE(c.impressions,0) AS seen, "
              "COALESCE(c.likes,0)+COALESCE(c.comments,0)+COALESCE(c.shares,0) AS engaged "
              "FROM marketing_content_log c WHERE c.restaurant_id=? AND c.post_id IS NOT NULL "
              "AND COALESCE(c.posted_at, c.created_at) >= ? ORDER BY at",
            (restaurant_id, d28.isoformat())).fetchall()
        f["posts_28d"] = len(posts)
        if len(posts) >= 2:
            first, last = _d(posts[0]["at"]), _d(posts[-1]["at"])
            span = (date.fromisoformat(last) - date.fromisoformat(first)).days
            f["post_cadence_days"] = round(span / (len(posts) - 1), 1)
        if posts:
            f["dish_posts_28d"] = sum(1 for p in posts if p["post_kind"] == "dish")
            f["offer_posts_28d"] = sum(1 for p in posts if p["post_kind"] == "offer")
            f["occasion_posts_28d"] = sum(1 for p in posts if p["occasion"] in ("game_day", "holiday", "event"))
            seen = [p for p in posts if p["seen"]]
            if seen:
                f["post_engagement_rate_28d"] = round(sum(p["engaged"] for p in seen) / sum(p["seen"] for p in seen), 4)
            try:
                ids = [p["id"] for p in posts]
                marks = ",".join("?" for _ in ids)
                att = conn.execute(f"SELECT lift_pct, item_lift_pct FROM marketing_attribution WHERE content_log_id IN ({marks})",
                                   ids).fetchall()
                from .stats import percentile as _pct
                lifts = [float(a["lift_pct"]) for a in att if a["lift_pct"] is not None]
                ilifts = [float(a["item_lift_pct"]) for a in att if a["item_lift_pct"] is not None]
                if lifts:
                    f["post_lift_median_28d"] = round(_pct(lifts, 50), 1)
                if ilifts:
                    f["item_lift_median_28d"] = round(_pct(ilifts, 50), 1)
            except Exception:
                pass
        try:      # guest_contacts is a lazily created table (guest_marketing.init_guest_marketing)
            f["guest_list_size"] = conn.execute(
                "SELECT COUNT(*) FROM guest_contacts WHERE restaurant_id=? AND consent=1 AND unsubscribed=0",
                (restaurant_id,)).fetchone()[0]
        except Exception:
            f["guest_list_size"] = None

        # ── the recommendation loop ────────────────────────────────────────
        dis = conn.execute(
            "SELECT kind FROM home_dismissals WHERE restaurant_id=? AND dismissed_at >= ?",
            (restaurant_id, d28.isoformat())).fetchall()
        f["recs_answered_28d"] = len(dis)
        f["recs_done_28d"] = sum(1 for r in dis if r["kind"] == "done")
        f["recs_declined_28d"] = sum(1 for r in dis if r["kind"] == "not_for_us")
        ev = conn.execute(
            "SELECT verdict FROM recommendation_outcomes WHERE restaurant_id=? AND status='evaluated' AND evaluate_on >= ?",
            (restaurant_id, d90.isoformat())).fetchall()
        f["outcomes_evaluated_90d"] = len(ev)
        # The share of CLEAR verdicts that improved (re-audit A31): an
        # unknown or a no-clear-change is not a failure, and counting them in
        # the denominator read a restaurant with thin data as one whose
        # changes don't work.
        clear = [r for r in ev if r["verdict"] in ("improved", "worsened")]
        if clear:
            f["outcomes_improved_rate_90d"] = round(sum(1 for r in clear if r["verdict"] == "improved")
                                                    / len(clear), 3)
    finally:
        conn.close()
    # People on the floor per role family and daypart, per $1k of sales
    # (staffing.compute_ratios) — the ratio a new restaurant's starting
    # headcount is borrowed from. Not in FEATURE_KEYS: a family this
    # restaurant does not run is absent, and completeness does not count it.
    try:
        from .staffing import compute_ratios
        f.update(compute_ratios(restaurant_id, today=today, db_path=db_path))
    except Exception as e:
        print(f"[intelligence] staffing ratios unavailable for {restaurant_id}: {e}")
    return f


def completeness(f: dict) -> float:
    measurable = [k for k in FEATURE_KEYS if not k.startswith(("recs_", "outcomes_", "guest_list", "posts_", "dish_posts", "offer_posts",
                                                                 "occasion_posts", "campaigns_", "schedules_"))]
    have = sum(1 for k in measurable if f.get(k) is not None)
    return round(have / len(measurable), 3) if measurable else 0.0


def store(restaurant_id: int, f: dict, week: str = None, today: date = None, db_path: str = DB_PATH) -> str:
    week = week or iso_week(today or date.today())
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,?) "
            "ON CONFLICT(restaurant_id, week) DO UPDATE SET features_json=excluded.features_json, "
            "completeness=excluded.completeness, computed_at=datetime('now')",
            (restaurant_id, week, json.dumps(f), completeness(f)))
        conn.commit()
    finally:
        conn.close()
    return week


def compute_and_store(restaurant_id: int, today: date = None, db_path: str = DB_PATH) -> dict:
    f = compute(restaurant_id, today=today, db_path=db_path)
    store(restaurant_id, f, today=today, db_path=db_path)
    return f


def latest(restaurant_id: int, db_path: str = DB_PATH) -> dict | None:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT week, features_json, completeness, computed_at FROM intel_features "
                           "WHERE restaurant_id=? ORDER BY week DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"week": row["week"], "features": json.loads(row["features_json"]), "completeness": row["completeness"],
            "computed_at": row["computed_at"]}


def series(restaurant_id: int, weeks: int = 12, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT week, features_json, completeness FROM intel_features WHERE restaurant_id=? "
                            "ORDER BY week DESC LIMIT ?", (restaurant_id, int(weeks))).fetchall()
    finally:
        conn.close()
    return [{"week": r["week"], "features": json.loads(r["features_json"]), "completeness": r["completeness"]}
            for r in reversed(rows)]


def latest_by_restaurant(db_path: str = DB_PATH, max_age_weeks: int = 3) -> dict:
    """{restaurant_id: {week, features, completeness}} — each restaurant's
    most recent row, only if it is recent enough to describe it now."""
    floor = iso_week(date.today() - timedelta(weeks=max_age_weeks))
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT f.restaurant_id, f.week, f.features_json, f.completeness FROM intel_features f "
            "JOIN (SELECT restaurant_id, MAX(week) AS week FROM intel_features GROUP BY restaurant_id) m "
            "ON m.restaurant_id=f.restaurant_id AND m.week=f.week WHERE f.week >= ?", (floor,)).fetchall()
    finally:
        conn.close()
    # Cross-restaurant readers (benchmarks, patterns, trends) only ever see
    # real restaurants: a demo account's seeded rows counted toward the
    # privacy floor and the cohort percentiles (CA3 F7).
    from .jobs import seeded_restaurant_ids
    seeded = seeded_restaurant_ids(db_path=db_path)
    return {r["restaurant_id"]: {"week": r["week"], "features": json.loads(r["features_json"]),
                                 "completeness": r["completeness"]} for r in rows if r["restaurant_id"] not in seeded}


def weekly_by_restaurant(weeks: int = 8, db_path: str = DB_PATH) -> dict:
    """{week: {restaurant_id: features}} over the last `weeks` ISO weeks."""
    floor = iso_week(date.today() - timedelta(weeks=weeks))
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, week, features_json FROM intel_features WHERE week >= ? ORDER BY week",
                            (floor,)).fetchall()
    finally:
        conn.close()
    from .jobs import seeded_restaurant_ids
    seeded = seeded_restaurant_ids(db_path=db_path)      # demo accounts excluded (CA3 F7)
    out = {}
    for r in rows:
        if r["restaurant_id"] in seeded:
            continue
        out.setdefault(r["week"], {})[r["restaurant_id"]] = json.loads(r["features_json"])
    return out


def _has_col(conn, table, col):
    try:
        return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception:
        return False
