import os, requests, csv
from datetime import datetime, timezone
from models import Review, save_reviews


def _meter_places(restaurant_id, action, kind="details", status="ok", error=None):
    """Google Places is billed per request. Audit #7 found it outside the
    ledger and the budget entirely, so a Places-only restaurant's four daily
    review fetches and the weekly competitor run were real money that no
    ceiling could see. Best-effort: metering must never break a fetch."""
    try:
        from ai_utils import log_api_call
        log_api_call(restaurant_id, action, f"google-places-{kind}",
                     calls=1, status=status, error=error)
    except Exception:
        pass


GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


def fetch_google(place_id: str, restaurant_id: int) -> list[Review]:
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "reviews",  # author_url rides along inside each review
              "key": GOOGLE_API_KEY, "reviews_sort": "newest"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        from ai_guard import safe_error as _se
        _meter_places(restaurant_id, "review_fetch", "details", status="error",
                      error=_se(e)[:200])
        raise
    _meter_places(restaurant_id, "review_fetch", "details")
    raw = resp.json().get("result", {}).get("reviews", [])
    tz = _restaurant_tz(restaurant_id)
    out = []
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
    reviews = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            reviews.append(Review(
                restaurant_id=restaurant_id, platform=row.get("platform", "csv"),
                external_id=row["id"], author=row.get("author", "Guest"),
                rating=int(row["rating"]), text=row["text"],
                review_date=row.get("date"),
            ))
    return reviews
