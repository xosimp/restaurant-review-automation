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

# Measured floors for every ratio feature (confidence re-audit B3 #11): one
# costed day at 61% labor published labor_pct_28d = 61.0, a benchmark key,
# at completeness 0.045. A ratio over a 28-day window needs MIN_MEASURED_DAYS
# days that carry its data (half the window); a daypart ratio as many
# schedule-outcome days; a review ratio MIN_REVIEWS_FOR_RATIO reviews (and
# timed replies); a campaign rate MIN_SENT_FOR_RATE messages sent; a post
# rate MIN_POSTS_FOR_RATE posts. Below a floor the feature is None — not
# measured — and neither enters benchmarks nor counts in completeness.
MIN_MEASURED_DAYS = 14
MIN_REVIEWS_FOR_RATIO = 5
MIN_SENT_FOR_RATE = 20
MIN_POSTS_FOR_RATE = 3

# Features benchmarks are published for (cohort p25/p50/p75). Ratios only.
BENCHMARK_KEYS = ("avg_rating_30d", "response_24h_rate_30d", "reply_rate_30d", "labor_pct_28d",
                  "labor_pct_sd_28d", "labor_hours_per_1k_28d", "labor_hours_per_1k_day_28d",
                  "labor_hours_per_1k_night_28d", "food_cost_pct_28d", "waste_sales_pct_28d", "campaign_tap_rate_28d",
                  "post_lift_median_28d", "post_engagement_rate_28d", "outcomes_improved_rate_90d")


# ── the structural block (Benchmarking audit #43, BM2-8) ─────────────────
# What a restaurant IS, measured: the coordinates peers are matched on (and
# Restaurant DNA's structural layer), never what it is compared ON. Ratios,
# shares, hours and BAND INDICES only — never a dollar (privacy.
# FORBIDDEN_KEY_STEMS holds `revenue` and `sales_total`, so sales volume and
# ticket are stored as the index of a fixed band, 0 = smallest). Not in
# FEATURE_KEYS: a structural fact is not a measure, and completeness does
# not count it. None when not measured, never 0.
STRUCTURAL_KEYS = ("ticket_band", "volume_band", "alcohol_share", "delivery_share", "weekly_open_hours",
                   "daypart_mix", "urbanity_band")
# Sales volume band and daypart mix are Restaurant DNA's (dna.VOLUME_BAND_EDGES,
# dna._daypart_mix): one definition, read here.
# Average ticket (sales ÷ covers): <$12, 12–20, 20–35, 35–60, $60+.
TICKET_BAND_EDGES = (12.0, 20.0, 35.0, 60.0)
# Median distance to the matched competitors: under 0.8km urban (2), under
# 3km suburban (1), else rural (0) — a proxy, from Intel's own search.
URBANITY_EDGES_M = (800.0, 3000.0)
_ALCOHOL_CATS = ("Liquor", "Beer", "Wine")


def _band(value, edges):
    if value is None:
        return None
    return sum(1 for e in edges if value >= e)


def _clock_hours(raw):
    """"11:00am" / "9:30 pm" / "21:00" → hours after midnight, or None."""
    import re as _re
    t = str(raw or "").strip().lower().replace(" ", "")
    m = _re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", t)
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 24 or mins > 59:
        return None
    return h + mins / 60.0


def weekly_open_hours(open_json, close_json):
    """Hours open across the week from the owner's per-day open and close
    times (a close at or before the open runs past midnight), or None."""
    try:
        opens = json.loads(open_json) if isinstance(open_json, str) else (open_json or {})
        closes = json.loads(close_json) if isinstance(close_json, str) else (close_json or {})
    except Exception:
        return None
    total, days = 0.0, 0
    for day, o in (opens or {}).items():
        a, b = _clock_hours(o), _clock_hours((closes or {}).get(day))
        if a is None or b is None:
            continue
        span = b - a if b > a else b + 24 - a
        if 0 < span <= 24:
            total += span
            days += 1
    return round(total, 1) if days else None


def structural(conn, restaurant_id: int, today: date) -> dict:
    """The structural block for one restaurant. Pure read."""
    out = {k: None for k in STRUCTURAL_KEYS}
    d28 = (today - timedelta(days=28)).isoformat()
    try:
        row = conn.execute("SELECT open_times_json, close_times_json, delivery_pct, competitor_intel "
                           "FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    except Exception:
        row = None
    if row:
        out["weekly_open_hours"] = weekly_open_hours(row["open_times_json"], row["close_times_json"])
        try:
            dp = float(row["delivery_pct"]) if row["delivery_pct"] not in (None, "") else None
            out["delivery_share"] = round(dp / 100.0, 3) if dp is not None and 0 <= dp <= 100 else None
        except (TypeError, ValueError):
            pass
        try:
            blob = json.loads(row["competitor_intel"] or "{}") if row["competitor_intel"] else {}
            dists = sorted(float(c["distance_m"]) for c in (blob.get("competitors") or [])
                           if isinstance(c, dict) and c.get("distance_m")
                           and "widened" not in str(c.get("match_basis") or c.get("basis") or ""))
            if len(dists) >= 3:
                med = dists[len(dists) // 2]
                out["urbanity_band"] = 2 - _band(med, URBANITY_EDGES_M)
        except Exception:
            pass
    try:
        sales = {str(r["date"])[:10]: float(r["sales"]) for r in conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date >= ? AND sales > 0",
            (restaurant_id, d28)).fetchall()}
    except Exception:
        sales = {}
    # Sales volume band and daypart mix: ONE definition with Restaurant DNA
    # (intelligence.dna S1/S3 — 8 weeks of final sales days, and the POS's
    # hourly RUNNING total before 4pm against the day's final sales), so a
    # peer coordinate and a DNA dimension can never disagree.
    try:
        from . import dna as _dna
        days = _dna._sales_days(conn, restaurant_id, today - timedelta(days=60))
        out["volume_band"] = _dna._volume_band(days, today).get("raw")
        out["daypart_mix"] = _dna._daypart_mix(conn, restaurant_id, days, today).get("raw")
    except Exception:
        pass
    if len(sales) >= MIN_MEASURED_DAYS:
        try:
            cov = {str(r["date"])[:10]: int(r["covers"]) for r in conn.execute(
                "SELECT date, covers FROM covers_daily WHERE restaurant_id=? AND date >= ? AND covers > 0",
                (restaurant_id, d28)).fetchall()}
            both = [d for d in cov if d in sales]
            if len(both) >= MIN_MEASURED_DAYS:
                out["ticket_band"] = _band(sum(sales[d] for d in both) / sum(cov[d] for d in both), TICKET_BAND_EDGES)
        except Exception:
            pass
    try:
        cats = conn.execute("SELECT metric, SUM(value) AS v, COUNT(DISTINCT business_date) AS n FROM dsr_metrics "
                            "WHERE restaurant_id=? AND business_date >= ? AND metric LIKE 'sales.cat:%' "
                            "AND value IS NOT NULL GROUP BY metric", (restaurant_id, d28)).fetchall()
        by = {r["metric"][len("sales.cat:"):]: float(r["v"] or 0) for r in cats}
        days = max((int(r["n"] or 0) for r in cats), default=0)
        total = sum(v for v in by.values() if v > 0)
        unmapped = sum(v for k, v in by.items() if k not in ("Food", "NA Beverage", "Retail") + _ALCOHOL_CATS)
        # Only where the categories are mapped: an unmapped share over 10%
        # leaves the alcohol share unknown, not low.
        if days >= MIN_MEASURED_DAYS and total > 0 and unmapped / total <= 0.10:
            out["alcohol_share"] = round(sum(by.get(c, 0.0) for c in _ALCOHOL_CATS) / total, 3)
    except Exception:
        pass
    return out
# Waste-logging regularity (DNA dimension F3; Benchmarking audit BM4-16,
# Top-50 #27): the share of the last WASTE_REGULARITY_WEEKS seven-day
# windows with at least one logged waste event. `waste_sales_pct_28d` from a
# restaurant that logs two small events a month reads near 0% — "doesn't
# log waste" passing as "low waste" — so the figure enters a cross-
# restaurant band, a pattern or the DNA only at WASTE_REGULARITY_MIN or
# above (cross_restaurant_view). The restaurant's own screens still see it.
# Not in FEATURE_KEYS: a data-quality signal, not counted in completeness.
WASTE_REGULARITY_KEY = "waste_log_regularity_8w"
WASTE_REGULARITY_WEEKS = 8
WASTE_REGULARITY_MIN = 0.75


def waste_logging_regular(f: dict) -> bool:
    """True when a feature row's waste log is regular enough for its waste
    % to be compared with anyone else's (F3 >= WASTE_REGULARITY_MIN). A row
    written before the regularity was measured is not regular."""
    r = (f or {}).get(WASTE_REGULARITY_KEY)
    try:
        return r is not None and float(r) >= WASTE_REGULARITY_MIN
    except (TypeError, ValueError):
        return False


def cross_restaurant_view(f: dict) -> dict:
    """A feature row as cross-restaurant learning may read it: the waste %
    withdrawn (None — unmeasured, never 0) unless waste is logged regularly.
    Every cross-restaurant reader (latest_by_restaurant, weekly_by_restaurant)
    goes through this."""
    f = dict(f or {})
    if f.get("waste_sales_pct_28d") is not None and not waste_logging_regular(f):
        f["waste_sales_pct_28d"] = None
    return f


def waste_log_regularity(conn, restaurant_id, today: date):
    """(share, windows_with_waste) over the last WASTE_REGULARITY_WEEKS
    seven-day windows ending today, or (None, 0) when the restaurant has not
    kept an inventory that long — no stock history is unmeasured, not
    irregular."""
    start = today - timedelta(days=7 * WASTE_REGULARITY_WEEKS - 1)
    try:
        first = conn.execute(
            "SELECT MIN(d) FROM (SELECT MIN(substr(event_date,1,10)) AS d FROM ingredient_stock_events "
            "WHERE restaurant_id=? UNION ALL SELECT MIN(substr(created_at,1,10)) FROM ingredients "
            "WHERE restaurant_id=?)", (restaurant_id, restaurant_id)).fetchone()[0]
    except Exception:
        return None, 0
    if not first or str(first)[:10] > start.isoformat():
        return None, 0
    rows = conn.execute("SELECT DISTINCT substr(event_date,1,10) AS d FROM ingredient_stock_events "
                        "WHERE restaurant_id=? AND event_type='waste' AND substr(event_date,1,10) >= ? "
                        "AND substr(event_date,1,10) <= ?",
                        (restaurant_id, start.isoformat(), today.isoformat())).fetchall()
    windows = set()
    for r in rows:
        try:
            windows.add((today - date.fromisoformat(r["d"])).days // 7)
        except (TypeError, ValueError):
            continue
    hit = sum(1 for k in windows if 0 <= k < WASTE_REGULARITY_WEEKS)
    return round(hit / float(WASTE_REGULARITY_WEEKS), 3), hit


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
        if len(last30) >= MIN_REVIEWS_FOR_RATIO:
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
            f["response_24h_rate_30d"] = round(within / timed, 3) if timed >= MIN_REVIEWS_FOR_RATIO else None
        if len(prior) >= MIN_REVIEWS_FOR_RATIO:
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
        # MIN_MEASURED_DAYS costed days of the 28 (re-audit B3 #11).
        if len({_d(r["date"]) for r in costed}) >= MIN_MEASURED_DAYS:
            tot_sales = sum(float(r["sales"]) for r in costed)
            f["labor_pct_28d"] = round(sum(float(r["labor_pct"]) * float(r["sales"]) for r in costed)
                                       / tot_sales, 2)
            m = sum(pcts) / len(pcts)
            f["labor_pct_sd_28d"] = round((sum((p - m) ** 2 for p in pcts) / (len(pcts) - 1)) ** 0.5, 2)
        sales_days = [(r["date"], float(r["sales"])) for r in lab if r["sales"]]
        n_sales_days = len({_d(dt) for dt, _s in sales_days})
        if n_sales_days >= MIN_MEASURED_DAYS:
            total = sum(s for _, s in sales_days)
            wknd = sum(s for dt, s in sales_days if date.fromisoformat(_d(dt)).weekday() >= 4)
            f["weekend_sales_share_28d"] = round(wknd / total, 3) if total else None
        # Hours per $1k of sales — a ratio, so it can be compared across a
        # cohort without a dollar or a name leaving the tenant; by daypart
        # from the schedule's own outcome record when it exists.
        try:
            hrs_rows = [r for r in lab if r["sales"] and r["total_hours"]]
            if len({_d(r["date"]) for r in hrs_rows}) >= MIN_MEASURED_DAYS:
                tot_s = sum(float(r["sales"]) for r in hrs_rows)
                tot_h = sum(float(r["total_hours"]) for r in hrs_rows)
                f["labor_hours_per_1k_28d"] = round(tot_h / tot_s * 1000, 2) if tot_s else None
            if _has_col(conn, "schedule_outcomes", "daypart"):
                for part, key in (("morning", "labor_hours_per_1k_day_28d"), ("night", "labor_hours_per_1k_night_28d")):
                    o = conn.execute("SELECT SUM(hours) AS h, SUM(sales) AS s, COUNT(DISTINCT date) AS n "
                                     "FROM schedule_outcomes WHERE restaurant_id=? AND daypart=? AND date >= ? "
                                     "AND sales IS NOT NULL", (restaurant_id, part, d28.isoformat())).fetchone()
                    if o and (o["n"] or 0) >= MIN_MEASURED_DAYS and o["s"]:
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
            # Both are shares of sales: the same MIN_MEASURED_DAYS sales days.
            enough_sales = n_sales_days >= MIN_MEASURED_DAYS
            fc, _ = metrics.measure(restaurant_id, "food_cost_pct", d28.isoformat(), today.isoformat(), db_path)
            f["food_cost_pct_28d"] = round(float(fc), 2) if (fc is not None and enough_sales) else None
            waste, _ = metrics.measure(restaurant_id, "weekly_waste", d28.isoformat(), today.isoformat(), db_path)
            sales, _ = metrics.measure(restaurant_id, "sales", d28.isoformat(), today.isoformat(), db_path)
            if waste is not None and sales and enough_sales:
                # weekly waste against weekly sales: sales is per day
                f["waste_sales_pct_28d"] = round(float(waste) / (float(sales) * 7) * 100, 2)
        except Exception:
            pass
        try:
            f[WASTE_REGULARITY_KEY] = waste_log_regularity(conn, restaurant_id, today)[0]
        except Exception as e:
            print(f"[intelligence] waste regularity unavailable for {restaurant_id}: {e}")

        # ── marketing ──────────────────────────────────────────────────────
        try:
            camps = conn.execute(
                "SELECT sent_count, link_token, visits_matched FROM guest_campaigns WHERE restaurant_id=? AND created_at >= ?",
                (restaurant_id, d28.isoformat())).fetchall() if _has_col(conn, "guest_campaigns", "visits_matched") else []
        except Exception:
            camps = []
        f["campaigns_28d"] = len(camps)
        sent = sum(int(c["sent_count"] or 0) for c in camps)
        if sent >= MIN_SENT_FOR_RATE:
            taps = 0
            for c in camps:
                if c["link_token"]:
                    row = conn.execute("SELECT clicks FROM marketing_links WHERE token=?", (c["link_token"],)).fetchone()
                    taps += int((row["clicks"] if row else 0) or 0)
            f["campaign_tap_rate_28d"] = round(taps / sent, 3)
            measured = [c for c in camps if c["visits_matched"] is not None]
            if sum(int(c["sent_count"] or 0) for c in measured) >= MIN_SENT_FOR_RATE:
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
            if len(seen) >= MIN_POSTS_FOR_RATE:
                f["post_engagement_rate_28d"] = round(sum(p["engaged"] for p in seen) / sum(p["seen"] for p in seen), 4)
            try:
                ids = [p["id"] for p in posts]
                marks = ",".join("?" for _ in ids)
                att = conn.execute(f"SELECT lift_pct, item_lift_pct FROM marketing_attribution WHERE content_log_id IN ({marks})",
                                   ids).fetchall()
                from .stats import percentile as _pct
                lifts = [float(a["lift_pct"]) for a in att if a["lift_pct"] is not None]
                ilifts = [float(a["item_lift_pct"]) for a in att if a["item_lift_pct"] is not None]
                if len(lifts) >= MIN_POSTS_FOR_RATE:
                    f["post_lift_median_28d"] = round(_pct(lifts, 50), 1)
                if len(ilifts) >= MIN_POSTS_FOR_RATE:
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
        f.update(_rec_loop(conn, restaurant_id, d28, d90))
    finally:
        conn.close()
    # The structural block (#43): what the restaurant IS, never a dollar.
    try:
        conn = get_conn(db_path)
        try:
            f.update(structural(conn, restaurant_id, today))
        finally:
            conn.close()
    except Exception as e:
        print(f"[intelligence] structural block unavailable for {restaurant_id}: {e}")
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


def _rec_loop(conn, restaurant_id, d28, d90) -> dict:
    """The recommendation-loop features, by the ONE success definition
    (CA2 #4, CA1 red flag 13):

      recs_*_28d                 from the ledger (rec_instances / rec_events),
                                 every surface — not the legacy Home-only
                                 home_dismissals table: episodes some surface
                                 SHOWED, answered in the last 28 days; done =
                                 taken (accepted, completed, implemented),
                                 declined = dismissed.
      outcomes_evaluated_90d     results evaluated in the last 90 days.
      outcomes_improved_rate_90d improved ÷ measured, each result read
                                 through rec_learning.learned_verdict (a
                                 disowned, conditions-changed, informational,
                                 trigger-window, faded or reversed result is
                                 never a win), the CLEAR_VERDICTS denominator
                                 (no clear change is measured, not a win), one
                                 result per number per overlapping window —
                                 and None below MIN_MEASURED_FOR_RATE: one
                                 result the owner disowned used to publish a
                                 1.0 benchmark (CA2 probe B)."""
    import rec_learning
    import rec_ledger
    out = {"recs_answered_28d": None, "recs_done_28d": None, "recs_declined_28d": None,
           "outcomes_evaluated_90d": None, "outcomes_improved_rate_90d": None}
    try:
        rows = conn.execute(
            "SELECT i.rec_id, i.key, e.event FROM rec_events e JOIN rec_instances i ON i.rec_id = e.rec_id "
            "WHERE i.restaurant_id=? AND e.at >= ? AND e.event IN ('accepted','completed','implemented','dismissed') "
            "AND EXISTS (SELECT 1 FROM rec_events s WHERE s.rec_id = i.rec_id AND s.event = 'shown')",
            (restaurant_id, d28.isoformat())).fetchall()
        answered, done, declined = set(), set(), set()
        for r in rows:
            if not rec_ledger.counts_in_acceptance(r["key"]):
                continue
            answered.add(r["rec_id"])
            (declined if r["event"] == "dismissed" else done).add(r["rec_id"])
        out["recs_answered_28d"] = len(answered)
        out["recs_done_28d"] = len(done)
        out["recs_declined_28d"] = len(declined - done)
    except Exception as e:
        print(f"[intelligence] ledger answers unreadable for {restaurant_id}: {e}")
    try:
        ev = [dict(r) for r in conn.execute(
            "SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND status='evaluated' AND evaluate_on >= ?",
            (restaurant_id, d90.isoformat())).fetchall()]
    except Exception as e:
        print(f"[intelligence] results unreadable for {restaurant_id}: {e}")
        return out
    out["outcomes_evaluated_90d"] = len(ev)
    eps = [{"rec_id": r["id"], "verdict": rec_learning.learned_verdict(r.get("verdict"), r), "tracker": r}
           for r in ev]
    clear = [e for e in rec_learning._one_per_window(eps) if e["verdict"] in rec_learning.CLEAR_VERDICTS]
    if len(clear) >= rec_learning.MIN_MEASURED_FOR_RATE:
        out["outcomes_improved_rate_90d"] = round(sum(1 for e in clear if e["verdict"] == "improved")
                                                  / len(clear), 3)
    return out


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
    return {r["restaurant_id"]: {"week": r["week"], "features": cross_restaurant_view(json.loads(r["features_json"])),
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
        out.setdefault(r["week"], {})[r["restaurant_id"]] = cross_restaurant_view(json.loads(r["features_json"]))
    return out


def _has_col(conn, table, col):
    try:
        return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception:
        return False
