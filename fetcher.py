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
    params = {"place_id": place_id, "fields": "reviews",
              "key": GOOGLE_API_KEY, "reviews_sort": "newest"}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        _meter_places(restaurant_id, "review_fetch", "details", status="error", error=str(e)[:200])
        raise
    _meter_places(restaurant_id, "review_fetch", "details")
    raw = resp.json().get("result", {}).get("reviews", [])
    return [Review(
        restaurant_id=restaurant_id, platform="google",
        external_id=f"google_{r['time']}_{r.get('author_name','')}",
        author=r.get("author_name", "Anonymous"),
        rating=r.get("rating", 0), text=r.get("text", ""),
        review_date=datetime.fromtimestamp(r["time"]).astimezone(__import__('zoneinfo').ZoneInfo('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'),
    ) for r in raw]


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
