"""event_intel.reviews — service complaints against game nights (phase 4).

"Service strain shows up in reviews after busy games": the reviews posted on
the day of one of this restaurant's games or the REVIEW_WINDOW_DAYS after it,
against the reviews posted on every other day of the same period — the share
of each that complains about service or the wait (the analyser's `service`
and `wait_time` categories, an owner's re-tag included).

A review's date is when it was posted, not when the guest came: the window
catches most of a game night's reviews and some that are not, so the result
is a lean, said as one, never proof. Only headline games count (a frequent
series is in only once it measured here, engine.headline): every Blackhawks
night would otherwise be "a game night". Nothing is said below
REVIEW_MIN_EACH reviews on each side, or under REVIEW_GAP_POINTS apart.

Pure reads, no model call; never raises into its caller.
"""
import json
import logging
from datetime import date, timedelta

from event_intel import engine, store

log = logging.getLogger(__name__)

REVIEW_WINDOW_DAYS = 2          # the game day and the two days after it
REVIEW_LOOKBACK_DAYS = 365
REVIEW_MIN_EACH = 8             # reviews on each side before a share is said
REVIEW_GAP_POINTS = 10          # percentage points apart before it is a lean
SERVICE_CATEGORIES = ("service", "wait_time")


def _cats(raw):
    try:
        v = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return set()
    return {str(c).strip().lower() for c in v if str(c).strip()} if isinstance(v, list) else set()


def game_night_reviews(restaurant_id, today=None, days=REVIEW_LOOKBACK_DAYS, db_path=store.DB_PATH):
    """{"games", "game_reviews", "game_service", "game_pct", "other_reviews",
    "other_service", "other_pct", "lean" ("more" | "fewer" | None), "text",
    "basis"} over the last `days`, or None without a followed game or reviews
    in the period. `game_pct` / `other_pct` are None below REVIEW_MIN_EACH;
    `text` only when both clear it and they sit REVIEW_GAP_POINTS apart."""
    try:
        today = today or engine._today(_restaurant(restaurant_id, db_path))
        today = today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
        start = today - timedelta(days=int(days))
        followed = store.follows(restaurant_id, db_path=db_path)
        games = [e for e in store.events_for([f["series_id"] for f in followed], start, today - timedelta(days=1),
                                             db_path=db_path)
                 if e.get("category") == "sports" and e.get("status") not in ("cancelled", "postponed")
                 and engine.headline(restaurant_id, e, db_path=db_path)]
        if not games:
            return None
        window = set()
        for e in games:
            d = date.fromisoformat(e["event_date"])
            window |= {(d + timedelta(days=k)).isoformat() for k in range(REVIEW_WINDOW_DAYS + 1)}
        from models import REVIEW_TIME_AXIS_BARE as AXIS
        conn = store.get_conn(db_path)
        try:
            rows = conn.execute(
                f"SELECT substr({AXIS}, 1, 10) AS day, categories FROM reviews WHERE restaurant_id=? "
                f"AND deleted_at IS NULL AND processed=1 AND substr({AXIS}, 1, 10) >= ? AND substr({AXIS}, 1, 10) < ?",
                (restaurant_id, start.isoformat(), today.isoformat())).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        g_n = g_s = o_n = o_s = 0
        for r in rows:
            svc = bool(_cats(r["categories"]) & set(SERVICE_CATEGORIES))
            if r["day"] in window:
                g_n += 1
                g_s += svc
            else:
                o_n += 1
                o_s += svc
        pct = lambda s, n: round(s / n * 100) if n >= REVIEW_MIN_EACH else None
        g_pct, o_pct = pct(g_s, g_n), pct(o_s, o_n)
        lean, text = None, None
        if g_pct is not None and o_pct is not None and abs(g_pct - o_pct) >= REVIEW_GAP_POINTS:
            lean = "more" if g_pct > o_pct else "fewer"
            text = (f"{g_pct}% of the reviews posted on a game day or the {REVIEW_WINDOW_DAYS} days after it "
                    f"mention service or the wait, against {o_pct}% of the reviews posted on other days "
                    f"({g_n} and {o_n} reviews, the last {int(days)} days) — a lean, not proof: a review's "
                    f"date is when it was posted, not when the guest came")
        return {"games": len(games), "game_reviews": g_n, "game_service": g_s, "game_pct": g_pct,
                "other_reviews": o_n, "other_service": o_s, "other_pct": o_pct, "lean": lean, "text": text,
                "window_days": REVIEW_WINDOW_DAYS, "days": int(days),
                "basis": (f"reviews tagged service or wait time, posted on the day of one of the {len(games)} "
                          f"followed games or the {REVIEW_WINDOW_DAYS} days after, against the rest of the period")}
    except Exception as ex:
        log.warning("event_intel.reviews failed rid=%s: %s", restaurant_id, ex)
        return None


def _restaurant(restaurant_id, db_path):
    import models
    return models.get_restaurant(restaurant_id, db_path=db_path) if db_path != store.DB_PATH \
        else models.get_restaurant(restaurant_id)
