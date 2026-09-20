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
import logging

log = logging.getLogger(__name__)

# Below this many nearby restaurants a "neighbourhood average" is one or two
# places, which is not an average of anything.
MIN_COMPETITORS = 3


def build(google_place_id, restaurant_id=None, timeout_ok=True):
    """{"rating", "review_count", "name", "neighbourhood": {...}} or {}.

    Best-effort by construction: any failure returns whatever was gathered
    so far. This runs on the new-client path, and a Places outage must cost
    a nicer welcome email, never the account.
    """
    out = {}
    if not google_place_id:
        return out
    try:
        import os
        import requests
        key = os.getenv("GOOGLE_PLACES_API_KEY", "")
        if not key:
            return out
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": google_place_id,
                    "fields": "name,rating,user_ratings_total",
                    "key": key},
            timeout=8)
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

    try:
        from competitor import get_nearby_competitors
        rivals = [c for c in (get_nearby_competitors(google_place_id, max_results=5) or [])
                  if c.get("rating")]
        if len(rivals) >= MIN_COMPETITORS:
            avg = sum(float(c["rating"]) for c in rivals) / len(rivals)
            out["neighbourhood"] = {
                "count": len(rivals),
                "avg_rating": round(avg, 1),
                "best": max(rivals, key=lambda c: float(c["rating"]))["name"],
            }
    except Exception as e:
        log.warning("first_look competitors failed: %s", e)
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
        if rating > avg:
            out.append(f"The {n} comparable restaurants nearest you average {avg} — "
                       f"you are ahead of your own neighbourhood.")
        elif rating < avg:
            out.append(f"The {n} comparable restaurants nearest you average {avg}. "
                       f"That gap is the first thing I will go after.")
        else:
            out.append(f"The {n} comparable restaurants nearest you average the same {avg}.")
    return out
