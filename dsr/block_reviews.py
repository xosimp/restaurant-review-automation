"""
dsr.block_reviews — the reviews that arrived on one night, and what's owed.

Sources (no model call):
  received, rating    reviews whose time (models.REVIEW_TIME_AXIS_BARE: when
                      the guest wrote it, else when Cavnar pulled it — both
                      stored on the restaurant's own clock) falls in the
                      business date's local window (dsr.common.local_bounds)
  themes              the categories the analyser already stored on each
                      review, counted separately for positive and negative
                      ones; a review not analysed yet is counted as such,
                      never guessed into a side
  urgent              reviews in the window the analyser marked urgency=high
  drafts awaiting     models.reply_queue_counts — the same split web and iOS
                      Home show (publishable + held; older drafts are
                      history, not owed); the queue as of the report
  replies posted      replies marked posted whose posted_at (UTC) falls in the
                      business date

Sync recency is restaurants.last_fetched_at, stamped only by a fetch that
reached Google (scheduler.run_daily_fetch). A fetch after the night ended
means the night is complete. Otherwise, two or more missed fetch slots
(admin_ops.fetch_slots_missed, the admin console's own rule — one missed
slot can be the bounded pass's tail) make the block AWAITING, "Review sync
delayed", and the counts that depend on the fetch are None rather than a
number that may be short. A current fetch that simply hasn't run since
close is READY, and detail says what time the reviews run through.
"""
import json
from datetime import timedelta

import dsr
from dsr import common

# admin_ops: "One missed slot can be the bounded pass's tail; two is not."
DELAYED_AFTER_MISSED_SLOTS = 2


def _rows(ctx, sql, args):
    from dsr.store import get_conn
    conn = get_conn(ctx.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _cats(raw):
    try:
        v = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [str(c).strip() for c in v if str(c).strip()] if isinstance(v, list) else []


def _themes(reviews, sentiment):
    counts = {}
    for rv in reviews:
        if rv.get("sentiment") != sentiment:
            continue
        for c in _cats(rv.get("categories")):
            counts[c] = counts.get(c, 0) + 1
    return [{"theme": k, "reviews": n} for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _sync_state(ctx, window_end_local):
    """("complete" | "current" | "delayed" | "never", fetched_local)."""
    import admin_ops
    from zoneinfo import ZoneInfo
    raw = getattr(ctx.restaurant, "last_fetched_at", None)
    fetched_ct = admin_ops.fetched_at_ct(raw)
    if fetched_ct is None:
        return "never", None
    fetched_utc = fetched_ct.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    fetched_local = common.to_local(fetched_utc, ctx.restaurant)
    if fetched_local >= window_end_local:
        return "complete", fetched_local
    now_ct = ctx.now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/Chicago"))
    missed = admin_ops.fetch_slots_missed(raw, now=now_ct) or 0
    return ("delayed" if missed >= DELAYED_AFTER_MISSED_SLOTS else "current"), fetched_local


def collect(ctx):
    from models import REVIEW_TIME_AXIS_BARE as AXIS
    r = ctx.restaurant
    if not getattr(r, "module_reviews", 1):
        return dsr.block(dsr.NOT_CONNECTED, source="google", block_name="reviews",
                         reason="Reviews aren't switched on for this location")
    if not (getattr(r, "reviews_live", 0) or getattr(r, "gmb_refresh_token", None)):
        return dsr.block(dsr.NOT_CONNECTED, source="google", block_name="reviews",
                         reason="Google reviews aren't connected")

    gaps = []
    start, end = common.local_bounds(r, ctx.business_date)
    state, fetched_local = common.guard(ctx, "reviews", "sync", lambda: _sync_state(ctx, end), gaps,
                                        default=("unknown", None))

    # Candidates by date text (the axis is stored as local text, with or
    # without a time), then placed in the window precisely.
    lo, hi = (start.date() - timedelta(days=1)).isoformat(), (end.date() + timedelta(days=1)).isoformat()
    cand = common.guard(ctx, "reviews", "reviews", lambda: _rows(
        ctx, f"SELECT id, platform, author, rating, summary, sentiment, categories, urgency, response_status, "
             f"{AXIS} AS at FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
             f"AND substr({AXIS}, 1, 10) BETWEEN ? AND ?", (ctx.restaurant_id, lo, hi)), gaps)
    if cand is None:
        return dsr.block(dsr.UNAVAILABLE, source="google", block_name="reviews",
                         reason="Review data unavailable")
    day = [rv for rv in cand if common.in_local_day(rv["at"], r, ctx.business_date, (start, end))]
    day.sort(key=lambda rv: str(rv["at"]))

    from models import reply_queue_counts
    queue = common.guard(ctx, "reviews", "drafts",
                         lambda: reply_queue_counts(ctx.restaurant_id, db_path=ctx.db_path), gaps)
    u0, u1 = common.utc_bounds(r, ctx.business_date)
    posted = common.guard(ctx, "reviews", "replies", lambda: _rows(
        ctx, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
             "AND response_status='posted' AND datetime(posted_at) >= ? AND datetime(posted_at) < ?",
        (ctx.restaurant_id, u0, u1))[0]["n"], gaps)

    delayed = state in ("delayed", "never")
    analysed = [rv for rv in day if rv.get("sentiment")]
    ratings = [int(rv["rating"]) for rv in day if rv.get("rating") is not None]
    # Counts that depend on the fetch are unknown while it is behind: a
    # short count is not a count.
    # A night's average rating only on thresholds.RATING_MIN_REVIEWS reviews
    # — the floor metrics.measure("avg_rating") applies everywhere else. One
    # review made "the night's rating" (CA1 D6); below the floor the count
    # is stated and the rating is not.
    from thresholds import RATING_MIN_REVIEWS
    rated_enough = len(ratings) >= RATING_MIN_REVIEWS
    metrics = {
        "received": None if delayed else len(day),
        "avg_rating": (None if (delayed or not rated_enough)
                       else round(sum(ratings) / len(ratings), 2)),
        "positive": None if delayed else sum(1 for rv in analysed if rv["sentiment"] == "positive"),
        "negative": None if delayed else sum(1 for rv in analysed if rv["sentiment"] == "negative"),
        "not_analysed": None if delayed else len(day) - len(analysed),
        "urgent": None if delayed else sum(1 for rv in day if rv.get("urgency") == "high"),
        "drafts_awaiting": (queue["publishable"] + queue["held"]) if queue is not None else None,
        "replies_posted": posted,
    }
    detail = {
        "window": {"start": common.time_label(start), "end": common.time_label(end)},
        "sync": {"state": state,
                 "fetched_through": common.time_label(fetched_local) if fetched_local else None,
                 "note": (None if state == "complete" else
                          "Reviews haven't synced for this location yet." if state == "never" else
                          f"The review fetch is behind — last reached Google {common.time_label(fetched_local)}."
                          if state == "delayed" else
                          f"Reviews run through {common.time_label(fetched_local)}; any written later "
                          "arrive with the next fetch." if fetched_local else None)},
        "reviews": [{"id": rv["id"], "platform": rv["platform"], "author": rv.get("author"),
                     "rating": rv.get("rating"), "sentiment": rv.get("sentiment"),
                     "summary": rv.get("summary"), "urgent": rv.get("urgency") == "high",
                     "status": rv.get("response_status")} for rv in day[:20]],
        "themes": {"positive": _themes(day, "positive"), "negative": _themes(day, "negative"),
                   "basis": "the categories stored when each review was analysed"},
        "urgent": [{"id": rv["id"], "rating": rv.get("rating"), "summary": rv.get("summary"),
                    "status": rv.get("response_status")} for rv in day if rv.get("urgency") == "high"],
        "drafts": ({"publishable": queue["publishable"], "held": queue["held"],
                    "as_of": "the reply queue at the time of this report",
                    "held_basis": "urgent or flagged drafts a person reads one at a time"}
                   if queue is not None else None),
        "unavailable_parts": gaps,
        # Why the night carries no rating when it has reviews: "3 reviews —
        # not rated" rather than a blank a reader takes for a zero.
        "rating_note": (None if delayed or rated_enough or not ratings else
                        f"{len(ratings)} review{'' if len(ratings) == 1 else 's'} — not rated "
                        f"(a rating needs {RATING_MIN_REVIEWS})"),
        "rating_min_reviews": RATING_MIN_REVIEWS,
    }
    if delayed:
        return dsr.block(dsr.AWAITING, source="google", block_name="reviews",
                         reason=("Review sync delayed" if state == "delayed" else "Reviews haven't synced yet"),
                         metrics=metrics, detail=detail)
    return dsr.block(dsr.READY, source="google", metrics=metrics, detail=detail)
