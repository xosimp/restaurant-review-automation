import config
import requests, csv
from datetime import datetime, timezone
from models import Review, save_reviews


from ai_utils import meter_places as _meter_places, places_error as _places_error

GOOGLE_API_KEY = config.google_places_key()  # either variable name; used to read only GOOGLE_API_KEY


class PlacesReviews(list):
    """The reviews one Places fetch returned, plus `total`: Google's own
    user_ratings_total for the listing at that moment, or None. Places
    returns at most five reviews; comparing the total's growth with what was
    stored is the only way to see the ones it never returned (MOD-REV-11)."""
    total = None


def fetch_google(place_id: str, restaurant_id: int) -> list[Review]:
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    # author_url rides along inside each review; user_ratings_total is how a
    # window with more than five new reviews is seen (MOD-REV-11).
    params = {"place_id": place_id, "fields": "reviews,user_ratings_total",
              "key": GOOGLE_API_KEY, "reviews_sort": "newest"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        from ai_guard import safe_error as _se
        _meter_places(restaurant_id, "review_fetch", "details", status="error",
                      error=_se(e)[:200])
        raise
    body = resp.json()
    refused = _places_error(body)
    if refused:
        # Metered as an error at no cost, and raised so the caller neither
        # stamps last_fetched_at nor calls it a sync (MOD-REV-1 / AI-6).
        _meter_places(restaurant_id, "review_fetch", "details", status="error", error=str(refused)[:200])
        raise refused
    _meter_places(restaurant_id, "review_fetch", "details")
    raw = (body.get("result") or {}).get("reviews", [])
    tz = _restaurant_tz(restaurant_id)
    out = PlacesReviews()
    try:
        _total = (body.get("result") or {}).get("user_ratings_total")
        out.total = int(_total) if _total is not None else None
    except (TypeError, ValueError):
        out.total = None
    for r in raw:
        # Places always sends a rating; a default of 0 would fail the table's
        # own CHECK(rating BETWEEN 1 AND 5) and be swallowed as an
        # IntegrityError, dropping the review with no trace. Skip loudly.
        try:
            rating = int(r.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        if not 1 <= rating <= 5:
            print(f"[places] skipping review with unusable rating {r.get('rating')!r}")
            continue
        ts = r.get("time")
        if ts is None:
            continue
        # Converted to the RESTAURANT's timezone, not a hardcoded Chicago.
        # A Pacific restaurant's 10pm reviews were being stamped midnight
        # Chicago — the next calendar day — which moved them across the
        # day, week and month boundaries every count in this module uses.
        written = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)
        author = r.get("author_name") or "Anonymous"
        out.append(Review(
            restaurant_id=restaurant_id, platform="google",
            # author_name alone collided for two "Anonymous" reviewers in the
            # same second, and forked one review into two whenever a reviewer
            # changed their display name. Places gives a stable per-author
            # profile URL; fall back to the name only when it is absent.
            external_id=_places_external_id(r),
            author=author,
            rating=rating, text=r.get("text", ""),
            review_date=written.strftime('%Y-%m-%dT%H:%M:%S'),
        ))
    return out


def _places_external_id(r: dict) -> str:
    """Stable identity for a Places review."""
    author_url = (r.get("author_url") or "").strip()
    if author_url:
        return f"google_{r['time']}_{author_url}"
    return f"google_{r['time']}_{(r.get('author_name') or '').strip()}"


def _restaurant_tz(restaurant_id: int):
    """The restaurant's own timezone, falling back to the operator's."""
    from zoneinfo import ZoneInfo
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        if r and getattr(r, "timezone", None):
            return ZoneInfo(r.timezone)
    except Exception:
        pass
    return ZoneInfo("America/Chicago")


def ingest_csv(path: str, restaurant_id: int) -> list[Review]:
    """Reviews from a CSV (main.py's dev path). A row that can't be a review
    is skipped and named, never allowed to take the rest of the file with it
    (MOD A1 R1 #32): utf-8-sig so an Excel byte-order mark doesn't turn the
    'id' header into '\\ufeffid', no blank ids, and a rating must be a whole
    1-5 — the reviews table's CHECK refuses anything else, and a 4.5 is not
    rounded into a rating the guest didn't give."""
    reviews = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            ext_id = (row.get("id") or "").strip()
            raw = (row.get("rating") or "").strip()
            try:
                rating = float(raw)
            except ValueError:
                rating = None
            if not ext_id or rating is None or rating != int(rating) or not 1 <= rating <= 5:
                print(f"[ingest_csv] line {line_no} skipped: id={ext_id!r} rating={raw!r}")
                continue
            reviews.append(Review(
                restaurant_id=restaurant_id, platform=row.get("platform", "csv"),
                external_id=ext_id, author=row.get("author", "Guest"),
                rating=int(rating), text=row.get("text") or "",
                review_date=row.get("date"),
            ))
    return reviews
