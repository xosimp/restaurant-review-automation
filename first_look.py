"""
first_look.py — what Cavnar AI can say about a restaurant before it has any
data of its own.

The delight audit found that day one of a subscription is a password and an
empty dashboard. `emails.send_welcome_email` carried a URL, a username, a
temporary password and a list of module names — nothing about the business.
The first email with any content in it is the day-2 onboarding tip, and the
first real number waits on a review fetch that itself waited on a checkbox.

Everything below is fetched from Google Places using the Place ID that was
just typed into the new-client form, so it is available the instant the
account exists. It is the same data `competitor.py` has always read; it had
simply never been read at the one moment it is most useful.

TWO RULES.

  IT SAYS ONLY WHAT IT FETCHED. Every field is independently optional and
  the caller renders only what came back. A missing rating is absent, never
  zero, and no sentence is generated from a value that is None — the same
  stance metrics.py takes and for the same reason: the very first number an
  owner ever sees from this product sets what they think the rest are worth.

  IT NEVER PREDICTS. This is an observation ("you are at 4.3 across 218
  reviews; the five nearest comparable restaurants average 4.1"), not a
  forecast and not a promise of what Cavnar AI will do about it. The dollar
  case belongs to sales_audit_engine, which builds it from real figures at
  the table with ranges attached.
"""
import config
import logging
import threading
import time

log = logging.getLogger(__name__)

# Below this many nearby restaurants a "neighbourhood average" is one or two
# places, which is not an average of anything.
MIN_COMPETITORS = 3

# A restaurant's Google rating and its neighbourhood do not move in an hour,
# and this runs on a REQUEST PATH. Railway serves the whole platform on
# `gunicorn --workers 1 --threads 4`, so one uncached call holds a quarter
# of it for as long as Google takes to answer.
#
# Two bounds, both deliberate:
#
#   * `timeout=(3, 5)` rather than the 8 the rest of competitor.py uses. A
#     first look is a nicety; waiting eight seconds for one on the screen an
#     owner is staring at is worse than not having it.
#   * `deep=False` by default, which skips the nearby-restaurants lookup
#     (two more Places calls inside get_nearby_competitors, each with its
#     own 8s timeout — up to 24 seconds on one thread in the worst case).
#     The neighbourhood comparison is only fetched where latency does not
#     reach a user: the welcome email, which is sent from a webhook or an
#     admin action, never from a page load.
_CACHE = {}
_CACHE_TTL = 6 * 3600
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 500

PLACES_TIMEOUT = (3, 5)


def _cached(key):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    return None


def _remember(key, value):
    with _CACHE_LOCK:
        # Bounded: one entry per place id, and the platform has far fewer
        # restaurants than this — but an unbounded process-lifetime dict is
        # how a small cache becomes a memory leak.
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (time.time(), value)


def build(google_place_id, restaurant_id=None, deep=False):
    """{"rating", "review_count", "name", "neighbourhood": {...}} or {}.

    `deep=True` adds the neighbourhood comparison, at the cost of two more
    Places calls. Only pass it off the request path.

    Best-effort by construction: any failure returns whatever was gathered
    so far. A Places outage must cost a nicer welcome, never the account.
    """
    out = {}
    if not google_place_id:
        return out
    cache_key = f"{google_place_id}:{'deep' if deep else 'shallow'}"
    hit = _cached(cache_key)
    if hit is not None:
        return hit
    try:
        import requests
        key = config.google_places_key()
        if not key:
            return out
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": google_place_id,
                    "fields": "name,rating,user_ratings_total",
                    "key": key},
            timeout=PLACES_TIMEOUT)
        data = r.json()
        if data.get("status") == "OK":
            res = data.get("result", {}) or {}
            if res.get("name"):
                out["name"] = res["name"]
            # Rule 1: absent, never zero. Google returns no `rating` key at
            # all for a listing with too few reviews, and rendering that as
            # "0.0 stars" would be the worst possible first impression.
            if res.get("rating"):
                out["rating"] = round(float(res["rating"]), 1)
            if res.get("user_ratings_total"):
                out["review_count"] = int(res["user_ratings_total"])
    except Exception as e:
        log.warning("first_look details failed: %s", e)

    if deep:
        try:
            from competitor import get_nearby_competitors
            rivals = [c for c in (get_nearby_competitors(google_place_id, max_results=5) or [])
                      if c.get("rating")]
            # The market average every surface uses (competitor_intel_format.
            # market_rating: review-weighted, provisional ratings left out) —
            # this took a flat mean, so the welcome email could put an owner
            # "ahead of your own neighbourhood" that the Intel tab put behind.
            from competitor_intel_format import market_rating
            mk = market_rating(rivals)
            # The standing's one floor (competitor_intel_format.
            # MARKET_MIN_MATCHED, Benchmarking #38): rivals matched on
            # cuisine and price, the widened-radius fallback left out.
            if mk["market_matched_n"] >= MIN_COMPETITORS:
                out["neighbourhood"] = {
                    "count": mk["market_rating_n"],
                    "matched": mk["market_matched_n"],
                    "avg_rating": mk["market_rating"],
                    "reviews": mk["market_rating_reviews"],
                    "effective_reviews": mk["market_effective_reviews"],
                    "radius_km": mk["market_radius_km"],
                    "basis": "weighted by each restaurant's review count",
                    "best": max(rivals, key=lambda c: float(c["rating"]))["name"],
                }
        except Exception as e:
            log.warning("first_look competitors failed: %s", e)
    _remember(cache_key, out)
    return out


def lines(look):
    """The first look as plain sentences, in the order an owner reads them.

    Returns [] when there is nothing honest to say, and the welcome email
    then simply omits the whole block rather than printing a heading over
    an empty space.
    """
    if not look:
        return []
    out = []
    rating = look.get("rating")
    count = look.get("review_count")
    if rating and count:
        out.append(f"Google has you at {rating} stars across {count:,} reviews.")
    elif rating:
        out.append(f"Google has you at {rating} stars.")

    hood = look.get("neighbourhood") or {}
    if rating and hood.get("avg_rating") and hood.get("count"):
        avg = hood["avg_rating"]
        n = hood["count"]
        # The same standing rule as the Intel tab (competitor_intel_format.
        # market_standing): matched rivals only, and "ahead" or "behind"
        # only beyond the gap's standard error (Benchmarking #38). The
        # neighbourhood was built on the matched floor, so an older cached
        # one without `matched` counts its rivals as matched.
        from competitor_intel_format import market_standing
        st = market_standing({"own_rating": rating, "own_rating_basis": "google_all_time",
                              "own_rating_count": count},
                             {"market_rating": avg, "market_matched_n": hood.get("matched", n),
                              "market_effective_reviews": hood.get("effective_reviews", hood.get("reviews")),
                              "market_radius_km": hood.get("radius_km")})["standing"]
        radius = hood.get("radius_km")
        # Owners here are American: miles, not the kilometres Places measures in.
        where = f"within {round(radius * 0.621371, 1):g} miles of you" if radius else "nearest you"
        if st == "ahead":
            out.append(f"The {n} comparable restaurants {where} average {avg} — "
                       f"you are ahead of your own neighborhood.")
        elif st == "behind":
            out.append(f"The {n} comparable restaurants {where} average {avg}. "
                       f"That gap is the first thing I will go after.")
        elif st == "level":
            out.append(f"The {n} comparable restaurants {where} average {avg} — "
                       f"you are level with them.")
    return out
