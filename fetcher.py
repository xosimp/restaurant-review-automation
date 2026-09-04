import os, requests, csv
from datetime import datetime, timezone
from models import Review, save_reviews

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


def fetch_google(place_id: str, restaurant_id: int) -> list[Review]:
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "reviews",
              "key": GOOGLE_API_KEY, "reviews_sort": "newest"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
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
