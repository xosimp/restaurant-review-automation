"""marketing_signals.py — what the rest of the product knows, fed back into marketing.

Everything here already existed somewhere in this codebase and was never
connected to the thing that writes the posts.

  · Post metrics were stored per post and never read back, so the generator
    could avoid REPEATING a topic but had no idea which topic had worked.
  · Review Intelligence knows the most-praised dish this week. `grep review
    marketing.py` returned nothing — the best post idea available was invisible
    to the thing whose job is post ideas.
  · weather.py has been feeding the labor scheduler for months. The first 70°
    day is the highest-return post a restaurant makes all year and the content
    calendar was working from a static holiday list.
  · POS sales sit next to post timestamps and nobody could answer whether a
    post sold anything.

`attribution_for_post` is the one to read carefully. It is a correlation, not
a causal claim, and it is labelled that way everywhere it surfaces: a Friday
post is compared against the last four Fridays, because comparing it to
Tuesday would just measure the weekend.
"""
import json
import logging
from datetime import datetime, timedelta

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

ATTRIBUTION_WINDOW_HOURS = 48
BASELINE_WEEKS = 4
# Fewer than this many comparable days and the number is noise wearing a
# percentage sign, so nothing is reported at all.
MIN_BASELINE_DAYS = 2


# ── POS sales ──────────────────────────────────────────────────────────────

def daily_sales(restaurant_id) -> dict:
    """{"YYYY-MM-DD": net_sales} from whatever this restaurant's labor data
    already holds — Toast sync writes it, a CSV upload writes it, and the
    labor module has been reading it all along."""
    try:
        from labor import load_shifts_for_restaurant
        shifts = load_shifts_for_restaurant(restaurant_id)
    except Exception:
        return {}
    out = {}
    for s in shifts or []:
        date = (s.get("date") or "").strip()[:10]
        if not date:
            continue
        sales = s.get("sales_that_day", s.get("sales"))
        try:
            sales = float(sales or 0)
        except (TypeError, ValueError):
            continue
        # One sales figure per date, not a sum over that date's shifts.
        if sales and date not in out:
            out[date] = sales
    return out


def attribution_for_post(restaurant_id, content_log_id, db_path: str = DB_PATH) -> dict:
    """Sales in the window after a post, against the same weekday before it.

    Returns {"ok": False, "reason": ...} whenever the comparison would be
    dishonest — no POS data, too few comparable days, no post date. Saying
    nothing is the correct output far more often than a number is.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, topic, post_platform, COALESCE(posted_at, created_at) AS at "
            "FROM marketing_content_log WHERE id=? AND restaurant_id=?",
            (content_log_id, restaurant_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["at"]:
        return {"ok": False, "reason": "no_post"}

    try:
        posted = datetime.fromisoformat(str(row["at"])[:19])
    except Exception:
        return {"ok": False, "reason": "no_post"}

    sales = daily_sales(restaurant_id)
    if not sales:
        return {"ok": False, "reason": "no_pos_data"}

    days = max(1, ATTRIBUTION_WINDOW_HOURS // 24)
    window_dates = [(posted + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    window_values = [sales[d] for d in window_dates if d in sales]
    if not window_values:
        return {"ok": False, "reason": "no_sales_yet"}

    # Same weekday, previous weeks — a Friday post measured against Fridays.
    baseline_values = []
    for offset in range(1, BASELINE_WEEKS + 1):
        for i in range(days):
            d = (posted + timedelta(days=i) - timedelta(weeks=offset)).strftime("%Y-%m-%d")
            if d in sales:
                baseline_values.append(sales[d])
    if len(baseline_values) < MIN_BASELINE_DAYS:
        return {"ok": False, "reason": "not_enough_history"}

    window_avg = sum(window_values) / len(window_values)
    baseline_avg = sum(baseline_values) / len(baseline_values)
    if baseline_avg <= 0:
        return {"ok": False, "reason": "not_enough_history"}

    lift = round((window_avg - baseline_avg) / baseline_avg * 100, 1)
    result = {
        "ok": True, "topic": row["topic"], "platform": row["post_platform"],
        "posted_at": row["at"], "window_hours": ATTRIBUTION_WINDOW_HOURS,
        "window_sales": round(window_avg, 2), "baseline_sales": round(baseline_avg, 2),
        "lift_pct": lift, "baseline_days": len(baseline_values),
    }
    _cache_attribution(restaurant_id, content_log_id, result, db_path=db_path)
    return result


def _cache_attribution(restaurant_id, content_log_id, result, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO marketing_attribution "
            "(restaurant_id, content_log_id, window_hours, baseline_sales, window_sales, lift_pct, computed_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            (restaurant_id, content_log_id, result["window_hours"], result["baseline_sales"],
             result["window_sales"], result["lift_pct"]),
        )
        conn.commit()
    except Exception as e:
        log.warning("attribution cache write failed: %s", e)
    finally:
        conn.close()


def attribution_summary(restaurant_id, limit=5, db_path: str = DB_PATH) -> dict:
    """The best-evidenced posts, for the Analytics tab. Correlational — the
    copy that renders this says so."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL "
            "ORDER BY COALESCE(posted_at, created_at) DESC LIMIT 25",
            (restaurant_id,),
        ).fetchall()
    finally:
        conn.close()

    scored = []
    for r in rows:
        result = attribution_for_post(restaurant_id, r["id"], db_path=db_path)
        if result.get("ok"):
            scored.append(result)
    if not scored:
        return {"ok": False, "reason": "not_enough_history", "posts": []}
    scored.sort(key=lambda x: x["lift_pct"], reverse=True)
    lifts = [s["lift_pct"] for s in scored]
    return {
        "ok": True,
        "posts": scored[:limit],
        "measured": len(scored),
        "median_lift_pct": round(sorted(lifts)[len(lifts) // 2], 1),
    }


# ── What worked, fed back in ───────────────────────────────────────────────

def top_performing(restaurant_id, limit=3, db_path: str = DB_PATH) -> list:
    """The posts that actually landed. Read by the generator and the calendar
    so the system chases what worked instead of only avoiding what it just
    said."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT topic, post_platform, "
            "       COALESCE(reach,0) + COALESCE(impressions,0) AS seen, "
            "       COALESCE(likes,0) + COALESCE(comments,0) + COALESCE(shares,0) AS engaged "
            "FROM marketing_content_log "
            "WHERE restaurant_id=? AND post_id IS NOT NULL AND topic IS NOT NULL "
            "  AND (reach > 0 OR impressions > 0 OR likes > 0) "
            "ORDER BY engaged DESC, seen DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def review_signal(restaurant_id, days=14, db_path: str = DB_PATH) -> dict:
    """What guests are actually praising and complaining about right now.

    Review Intelligence has categorised every review since this product
    existed. marketing.py never read one.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT rating, text, categories, sentiment FROM reviews "
            "WHERE restaurant_id=? AND deleted_at IS NULL AND COALESCE(review_date, fetched_at) >= ? "
            "ORDER BY rating DESC LIMIT 60",
            (restaurant_id, since),
        ).fetchall()
    finally:
        conn.close()

    praised, criticised = {}, {}
    best_quote = None
    for r in rows:
        try:
            cats = json.loads(r["categories"]) if r["categories"] else []
        except Exception:
            cats = []
        bucket = praised if (r["rating"] or 0) >= 4 else criticised
        for c in cats:
            label = str(c).replace("_", " ").strip().lower()
            if label:
                bucket[label] = bucket.get(label, 0) + 1
        if (r["rating"] or 0) == 5 and not best_quote and (r["text"] or "").strip():
            best_quote = (r["text"] or "").strip()[:160]

    def top(d):
        return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:3]]

    return {"praised": top(praised), "criticised": top(criticised),
            "best_quote": best_quote, "reviews_read": len(rows)}


def weather_signal(restaurant_id) -> dict:
    """Anything in the week's forecast worth building a post around.

    weather.py has fed the labor scheduler for months. A patio push on the
    first genuinely warm day is the highest-return post a restaurant makes all
    year, and the content calendar was working from a fixed holiday list.
    """
    try:
        from models import get_restaurant
        from weather import get_forecast_for_week
        restaurant = get_restaurant(restaurant_id)
        if not restaurant:
            return {}
        today = datetime.now().date()
        week = [(today + timedelta(days=i)).isoformat() for i in range(7)]
        forecast = get_forecast_for_week(restaurant, week) or []
    except Exception:
        return {}

    notes = []
    for day in forecast:
        try:
            high = day.get("high_f") if isinstance(day, dict) else None
            desc = (day.get("summary") or day.get("forecast") or "") if isinstance(day, dict) else ""
            date = day.get("date") if isinstance(day, dict) else None
        except Exception:
            continue
        if high is None or not date:
            continue
        try:
            label = datetime.fromisoformat(str(date)[:10]).strftime("%A")
        except Exception:
            label = str(date)
        if high >= 72:
            notes.append(f"{label}: {int(high)}°F — patio weather")
        elif high <= 38:
            notes.append(f"{label}: {int(high)}°F — a night for comfort food")
        elif desc and any(w in str(desc).lower() for w in ("rain", "storm", "snow")):
            notes.append(f"{label}: {desc} — delivery and takeout push")
    return {"notes": notes[:3]}


def generation_context(restaurant_id, db_path: str = DB_PATH) -> str:
    """The three signals above, as prompt text.

    Appended to both the single-piece generator and the calendar so a post is
    written knowing what worked, what guests just said, and what the sky is
    doing — instead of only the restaurant's static profile.
    """
    parts = []

    winners = top_performing(restaurant_id, db_path=db_path)
    if winners:
        best = ", ".join(f"\"{w['topic']}\" ({w['engaged']} interactions)" for w in winners if w.get("topic"))
        if best:
            parts.append(f"Posts that performed best for this restaurant: {best}. "
                         "Lean toward what these have in common; don't copy them.")

    reviews = review_signal(restaurant_id, db_path=db_path)
    if reviews.get("praised"):
        parts.append("Guests are praising: " + ", ".join(reviews["praised"]) + ".")
    if reviews.get("best_quote"):
        parts.append(f"A recent 5-star guest wrote: \"{reviews['best_quote']}\". "
                     "You may draw on the sentiment; never quote a guest verbatim in a post.")
    if reviews.get("criticised"):
        parts.append("Guests have complained about: " + ", ".join(reviews["criticised"]) +
                     ". Do not raise these in marketing copy.")

    weather = weather_signal(restaurant_id)
    if weather.get("notes"):
        parts.append("This week's weather: " + "; ".join(weather["notes"]) + ".")

    return ("\n" + "\n".join(parts)) if parts else ""


# ── Windowed performance ───────────────────────────────────────────────────

def performance_window(restaurant_id, days=30, db_path: str = DB_PATH) -> dict:
    """Reach, engagement and rate for a period, against the period before it.

    The old summary was all-time totals with no denominator and no trend:
    "Total reach 4,231" is a number, not a metric. Engagement RATE is the one
    a marketing director reads first, because it survives a restaurant's
    follower count changing.
    """
    conn = get_conn(db_path)
    try:
        def bucket(start_days, end_days=None):
            """[start_days ago, end_days ago). end_days=None means "up to now"
            — the current window has to include TODAY, and comparing against
            date('now','-0 days') excluded it, so a post published this
            morning was missing from the headline while still showing up in
            the per-platform breakdown below."""
            sql = ("SELECT COUNT(*) AS posts, "
                   "       COALESCE(SUM(reach),0) + COALESCE(SUM(impressions),0) AS seen, "
                   "       COALESCE(SUM(likes),0) + COALESCE(SUM(comments),0) + COALESCE(SUM(shares),0) AS engaged "
                   "FROM marketing_content_log "
                   "WHERE restaurant_id=? AND post_id IS NOT NULL "
                   "  AND COALESCE(posted_at, created_at) >= date('now', ?)")
            args = [restaurant_id, f"-{start_days} days"]
            if end_days is not None:
                sql += " AND COALESCE(posted_at, created_at) < date('now', ?)"
                args.append(f"-{end_days} days")
            return conn.execute(sql, args).fetchone()

        current = bucket(days)
        previous = bucket(days * 2, days)

        by_platform = conn.execute(
            "SELECT post_platform AS platform, COUNT(*) AS posts, "
            "       COALESCE(SUM(reach),0) + COALESCE(SUM(impressions),0) AS seen, "
            "       COALESCE(SUM(likes),0) + COALESCE(SUM(comments),0) + COALESCE(SUM(shares),0) AS engaged "
            "FROM marketing_content_log "
            "WHERE restaurant_id=? AND post_id IS NOT NULL "
            "  AND COALESCE(posted_at, created_at) >= date('now', ?) "
            "GROUP BY post_platform ORDER BY seen DESC",
            (restaurant_id, f"-{days} days"),
        ).fetchall()
    finally:
        conn.close()

    def rate(row):
        seen = row["seen"] or 0
        return round((row["engaged"] or 0) / seen * 100, 1) if seen else 0.0

    def change(now_v, then_v):
        if not then_v:
            return None
        return round((now_v - then_v) / then_v * 100, 1)

    return {
        "days": days,
        "posts": current["posts"] or 0,
        "reach": current["seen"] or 0,
        "engagement": current["engaged"] or 0,
        "engagement_rate": rate(current),
        "previous": {
            "posts": previous["posts"] or 0,
            "reach": previous["seen"] or 0,
            "engagement": previous["engaged"] or 0,
            "engagement_rate": rate(previous),
        },
        "change": {
            "posts": change(current["posts"] or 0, previous["posts"] or 0),
            "reach": change(current["seen"] or 0, previous["seen"] or 0),
            "engagement": change(current["engaged"] or 0, previous["engaged"] or 0),
        },
        "by_platform": [
            {"platform": r["platform"] or "unknown", "posts": r["posts"],
             "reach": r["seen"], "engagement": r["engaged"], "engagement_rate": rate(r)}
            for r in by_platform
        ],
    }
