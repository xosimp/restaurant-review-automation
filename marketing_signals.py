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
import re
import contextvars
from datetime import datetime, timedelta, timezone

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

ATTRIBUTION_WINDOW_HOURS = 48
BASELINE_WEEKS = 4
# Fewer than this many comparable days and the number is noise wearing a
# percentage sign, so nothing is reported at all.
MIN_BASELINE_DAYS = 2
# The narrowest a post's noise band can be. A lift inside the band is "no
# clear change": the same weekday moves this much on its own. Any lift at
# or above zero used to read as a good result (audit), so +1% on a weekday
# that swings 15% week to week was a win.
MIN_NOISE_BAND_PCT = 5.0


def noise_band_pct(baseline_values) -> float:
    """How much the same weekday moves on its own, as a percentage of its
    mean: the spread (population standard deviation) of the baseline days,
    floored at MIN_NOISE_BAND_PCT. With two baseline days the spread is
    itself a rough figure, which is why the floor exists."""
    vals = [float(v) for v in baseline_values or []]
    if len(vals) < 2:
        return MIN_NOISE_BAND_PCT
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return MIN_NOISE_BAND_PCT
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return round(max(MIN_NOISE_BAND_PCT, (var ** 0.5) / mean * 100), 1)


def lift_verdict(lift_pct, band_pct) -> str:
    """'lifted' | 'dropped' | 'no_clear_change' — never 'good' for a lift
    inside the weekday's own noise."""
    if lift_pct is None:
        return "no_clear_change"
    if lift_pct > band_pct:
        return "lifted"
    if lift_pct < -band_pct:
        return "dropped"
    return "no_clear_change"


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


# One summary scores up to 25 posts; each used to reload and re-parse the
# whole shift history. attribution_summary loads it once and parks it here.
_sales_for_summary = contextvars.ContextVar("_sales_for_summary", default=None)


def _sales(restaurant_id) -> dict:
    held = _sales_for_summary.get()
    if held is not None and held[0] == restaurant_id:
        return held[1]
    return daily_sales(restaurant_id)


def _local_post_time(stamp, tz_name):
    """posted_at / created_at are SQLite datetime('now'): UTC. The sales
    they are compared against are keyed by the restaurant's business date,
    so a 7:30pm Chicago post stored as 00:30 UTC the next day belongs to
    the night it went out, not the day after."""
    from time_utils import restaurant_tz
    naive = datetime.fromisoformat(str(stamp)[:19])
    return naive.replace(tzinfo=timezone.utc).astimezone(restaurant_tz(tz_name)).replace(tzinfo=None)


def _window_dates(posted, days):
    return [(posted + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def attribution_for_post(restaurant_id, content_log_id, db_path: str = DB_PATH) -> dict:
    """Sales in the window after a post, against the same weekday before it.

    Returns {"ok": False, "reason": ...} whenever the comparison would be
    dishonest — no POS data, too few comparable days, no post date. Saying
    nothing is the correct output far more often than a number is.
    """
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, topic, post_platform, COALESCE(posted_at, created_at) AS at, menu_item_id, occasion, post_kind "
            "FROM marketing_content_log WHERE id=? AND restaurant_id=?",
            (content_log_id, restaurant_id),
        ).fetchone()
        tz_row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        others = conn.execute(
            "SELECT id, COALESCE(posted_at, created_at) AS at FROM marketing_content_log "
            "WHERE restaurant_id=? AND id<>? AND post_id IS NOT NULL", (restaurant_id, content_log_id)).fetchall()
    finally:
        conn.close()
    if not row or not row["at"]:
        return {"ok": False, "reason": "no_post"}
    tz_name = tz_row["timezone"] if tz_row else None

    try:
        posted = _local_post_time(row["at"], tz_name)
    except Exception:
        return {"ok": False, "reason": "no_post"}

    sales = _sales(restaurant_id)
    if not sales:
        return {"ok": False, "reason": "no_pos_data"}

    days = max(1, ATTRIBUTION_WINDOW_HOURS // 24)
    window_dates = _window_dates(posted, days)
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
    band = noise_band_pct(baseline_values)
    result = {
        "ok": True, "id": row["id"], "topic": row["topic"], "platform": row["post_platform"],
        "posted_at": row["at"], "window_hours": ATTRIBUTION_WINDOW_HOURS,
        "window_sales": round(window_avg, 2), "baseline_sales": round(baseline_avg, 2),
        "lift_pct": lift, "baseline_days": len(baseline_values),
        # Clients colour and word the result from `verdict`, not from the
        # sign of lift_pct: inside the band is "no clear change".
        "noise_band_pct": band, "verdict": lift_verdict(lift, band),
    }
    # Two posts whose windows share a day are measured against the same
    # sales: one busy Friday cannot be credited in full to both of them.
    mine = set(window_dates)
    overlaps = []
    for o in others:
        try:
            if mine & set(_window_dates(_local_post_time(o["at"], tz_name), days)):
                overlaps.append(o["id"])
        except Exception:
            continue
    result["overlapping"] = bool(overlaps)
    result["overlaps_with"] = overlaps
    result.update(_beyond_sales(restaurant_id, row, posted, window_dates, days, db_path))
    _cache_attribution(restaurant_id, content_log_id, result, db_path=db_path)
    return result


def _beyond_sales(restaurant_id, row, posted, window_dates, days, db_path) -> dict:
    """What else the post's window shows: the promoted dish's own units
    against the same weekdays before (menu_item_sales), reviews in the
    fortnight after that mention the dish or the topic, the guest list's
    move, and the post's own engagement. Each is None when it cannot be
    measured — never 0 standing in for "not tracked"."""
    out = {"menu_item_id": row["menu_item_id"] if "menu_item_id" in row.keys() else None,
           "menu_item_name": None, "occasion": row["occasion"] if "occasion" in row.keys() else None,
           "post_kind": row["post_kind"] if "post_kind" in row.keys() else None,
           "item_lift_pct": None, "item_window_qty": None, "item_baseline_qty": None,
           "reviews_mentioning": None, "guest_list_delta": None, "engagement_rate": None}
    conn = get_conn(db_path)
    try:
        if out["menu_item_id"]:
            mi = conn.execute("SELECT name FROM menu_items WHERE id=? AND restaurant_id=?", (out["menu_item_id"], restaurant_id)).fetchone()
            out["menu_item_name"] = mi["name"] if mi else None
            qty = {r["business_date"]: float(r["qty_sold"]) for r in conn.execute(
                "SELECT business_date, qty_sold FROM menu_item_sales WHERE restaurant_id=? AND menu_item_id=?",
                (restaurant_id, out["menu_item_id"])).fetchall()}
            win = [qty[d] for d in window_dates if d in qty]
            base = []
            for offset in range(1, BASELINE_WEEKS + 1):
                for i in range(days):
                    d = (posted + timedelta(days=i) - timedelta(weeks=offset)).strftime("%Y-%m-%d")
                    if d in qty:
                        base.append(qty[d])
            if win and len(base) >= MIN_BASELINE_DAYS and sum(base) > 0:
                wa, ba = sum(win) / len(win), sum(base) / len(base)
                out["item_window_qty"], out["item_baseline_qty"] = round(wa, 1), round(ba, 1)
                out["item_lift_pct"] = round((wa - ba) / ba * 100, 1)
        # reviews in the 14 days after the post that name the dish or the topic
        needles = set()
        if out["menu_item_name"]:
            needles.add(out["menu_item_name"].lower())
        for w in re.findall(r"[a-z]{5,}", (row["topic"] or "").lower()):
            if w not in _TOPIC_STOPWORDS:
                needles.add(w)
        if needles:
            after = (posted + timedelta(days=14)).strftime("%Y-%m-%d")
            rows = conn.execute("SELECT text FROM reviews WHERE restaurant_id=? AND review_date >= ? AND review_date < ?",
                                (restaurant_id, posted.strftime("%Y-%m-%d"), after)).fetchall()
            out["reviews_mentioning"] = sum(1 for r in rows if any(n in (r["text"] or "").lower() for n in needles))
        # guest list: consents in the 7 days after vs the 7 before
        try:
            a0, a1 = posted.strftime("%Y-%m-%d"), (posted + timedelta(days=7)).strftime("%Y-%m-%d")
            b0 = (posted - timedelta(days=7)).strftime("%Y-%m-%d")
            after_n = conn.execute("SELECT COUNT(*) FROM guest_contacts WHERE restaurant_id=? AND consent=1 AND consent_at >= ? AND consent_at < ?",
                                   (restaurant_id, a0, a1)).fetchone()[0]
            before_n = conn.execute("SELECT COUNT(*) FROM guest_contacts WHERE restaurant_id=? AND consent=1 AND consent_at >= ? AND consent_at < ?",
                                    (restaurant_id, b0, a0)).fetchone()[0]
            out["guest_list_delta"] = int(after_n) - int(before_n)
        except Exception:
            out["guest_list_delta"] = None
        try:
            m = conn.execute("SELECT COALESCE(reach,0)+COALESCE(impressions,0) AS seen, "
                             "COALESCE(likes,0)+COALESCE(comments,0)+COALESCE(shares,0) AS engaged "
                             "FROM marketing_content_log WHERE id=?", (row["id"],)).fetchone()
            if m and m["seen"]:
                out["engagement_rate"] = round(float(m["engaged"]) / float(m["seen"]), 4)
        except Exception:
            pass
    finally:
        conn.close()
    return out


_TOPIC_STOPWORDS = {"about", "their", "there", "these", "those", "which", "would", "could", "should", "tonight",
                    "today", "weekend", "special", "specials", "happy", "hour", "friday", "saturday", "sunday", "monday",
                    "tuesday", "wednesday", "thursday", "every", "night", "great", "amazing", "delicious"}


def _cache_attribution(restaurant_id, content_log_id, result, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO marketing_attribution "
            "(restaurant_id, content_log_id, window_hours, baseline_sales, window_sales, lift_pct, item_lift_pct, "
            " item_window_qty, item_baseline_qty, reviews_mentioning, guest_list_delta, engagement_rate, computed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (restaurant_id, content_log_id, result["window_hours"], result["baseline_sales"],
             result["window_sales"], result["lift_pct"], result.get("item_lift_pct"), result.get("item_window_qty"),
             result.get("item_baseline_qty"), result.get("reviews_mentioning"), result.get("guest_list_delta"),
             result.get("engagement_rate")),
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

    try:
        import marketing_tags
        marketing_tags.backfill(restaurant_id, db_path=db_path)
    except Exception:
        pass
    scored = []
    token = _sales_for_summary.set((restaurant_id, daily_sales(restaurant_id)))
    try:
        for r in rows:
            result = attribution_for_post(restaurant_id, r["id"], db_path=db_path)
            if result.get("ok"):
                scored.append(result)
    finally:
        _sales_for_summary.reset(token)
    if not scored:
        return {"ok": False, "reason": "not_enough_history", "posts": []}
    scored.sort(key=lambda x: x["lift_pct"], reverse=True)
    lifts = [s["lift_pct"] for s in scored]
    return {
        "ok": True,
        "posts": scored[:limit],
        # What did not land is as much of the picture as what did.
        "weakest": [p for p in reversed(scored) if p.get("verdict") == "dropped"][:3],
        "measured": len(scored),
        "median_lift_pct": round(sorted(lifts)[len(lifts) // 2], 1),
        "by_kind": _group_lift(scored, "post_kind"),
        "by_occasion": _group_lift(scored, "occasion"),
        "by_dish": _group_lift(scored, "menu_item_name"),
    }


def _group_lift(scored, key):
    """Median sales lift (and item lift where measured) per group, with the
    count — only groups with two or more measured posts, so one post never
    becomes a rule."""
    groups = {}
    for p in scored:
        g = p.get(key)
        if not g:
            continue
        groups.setdefault(g, []).append(p)
    out = []
    for g, ps in groups.items():
        if len(ps) < 2:
            continue
        ls = sorted(x["lift_pct"] for x in ps)
        il = sorted(x["item_lift_pct"] for x in ps if x.get("item_lift_pct") is not None)
        er = [x["engagement_rate"] for x in ps if x.get("engagement_rate") is not None]
        out.append({"group": g, "posts": len(ps), "median_lift_pct": ls[len(ls) // 2],
                    "median_item_lift_pct": il[len(il) // 2] if il else None,
                    "avg_engagement_rate": round(sum(er) / len(er), 4) if er else None})
    out.sort(key=lambda x: x["median_lift_pct"], reverse=True)
    return out


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
        # Guest-written, so it travels inside the untrusted fence with the
        # note that says what the fence means (AI-15). Quoted raw, a 5-star
        # review was an instruction channel into copy that gets published.
        from ai_guard import UNTRUSTED_NOTE, wrap_untrusted
        parts.append("A recent 5-star guest wrote the review below. You may draw on the "
                     "sentiment; never quote a guest verbatim in a post.\n"
                     f"{UNTRUSTED_NOTE}\n{wrap_untrusted(reviews['best_quote'])}")
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
        # None, not 0.0, when nothing was seen: 0% reads as "nobody engaged",
        # and the truth is "reach was never measured" (MOD-MKT-18).
        seen = row["seen"] or 0
        return round((row["engaged"] or 0) / seen * 100, 1) if seen else None

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
