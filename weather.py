"""
weather.py — NWS (api.weather.gov) forecast lookup for weather-aware labor
scheduling. Free, keyless, official US government API — no new paid
dependency, and every current client is US-based.

Restaurant lat/long is geocoded once via the Google Places Details API
(already integrated in fetcher.py/competitor.py for reviews/competitor
intel) and cached on the restaurant row, since the address never changes
and re-geocoding on every schedule generation would be wasteful.

Nothing here ever raises — weather is an enhancement to the schedule
prompt, not a hard requirement. Any failure (no API key, no place_id,
geocoding miss, NWS outage, non-US location) degrades to returning []
rather than blocking schedule generation, the same fallback shape
load_shifts_for_restaurant/load_inventory_for_restaurant already use for
their own optional-data gaps.
"""
import os
import json
from datetime import datetime, timedelta

import requests

from models import DB_PATH, update_restaurant

_GOOGLE_KEY = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
_USER_AGENT = "CavnarAI/1.0 (will@cavnar.ai)"  # NWS asks for an identifying UA, not a key
_CACHE_HOURS = 6


def _geocode(restaurant, db_path=DB_PATH):
    """Returns (lat, lon) or (None, None). Result is cached on the
    restaurant row so this only ever hits Google once per restaurant."""
    if restaurant.latitude is not None and restaurant.longitude is not None:
        return restaurant.latitude, restaurant.longitude
    if not restaurant.google_place_id or not _GOOGLE_KEY:
        return None, None
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": restaurant.google_place_id, "fields": "geometry", "key": _GOOGLE_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        loc = resp.json().get("result", {}).get("geometry", {}).get("location", {})
        lat, lon = loc.get("lat"), loc.get("lng")
        if lat is None or lon is None:
            return None, None
        update_restaurant(restaurant.id, {"latitude": lat, "longitude": lon}, db_path=db_path)
        return lat, lon
    except Exception:
        return None, None


def _fetch_periods(lat, lon):
    """Raw NWS forecast periods for a lat/lon, or [] on any failure."""
    try:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
        points_resp = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10)
        points_resp.raise_for_status()
        forecast_url = points_resp.json().get("properties", {}).get("forecast")
        if not forecast_url:
            return []
        fc_resp = requests.get(forecast_url, headers=headers, timeout=10)
        fc_resp.raise_for_status()
        return fc_resp.json().get("properties", {}).get("periods", [])
    except Exception:
        return []


def _cached_periods(restaurant):
    """Returns the cached periods list if fresh, None if missing/stale/bad."""
    if not restaurant.weather_cached_at:
        return None
    try:
        cached_at = datetime.fromisoformat(restaurant.weather_cached_at)
    except Exception:
        return None
    if datetime.now() - cached_at > timedelta(hours=_CACHE_HOURS):
        return None
    try:
        return json.loads(restaurant.weather_cache_json or "[]")
    except Exception:
        return None


def get_forecast_for_week(restaurant, week_dates, db_path=DB_PATH):
    """One row per date in week_dates that NWS has a forecast for — NWS only
    forecasts about a week out, so later dates in the week may be omitted
    entirely rather than guessed at. Returns [] on any failure (no
    restaurant, no coordinates, NWS unreachable) — never blocks schedule
    generation.

    Each row: {"date", "day_name", "high_f", "short_forecast", "precip_pct"}.
    """
    if not restaurant:
        return []

    periods = _cached_periods(restaurant)
    if periods is None:
        lat, lon = _geocode(restaurant, db_path=db_path)
        if lat is None or lon is None:
            return []
        periods = _fetch_periods(lat, lon)
        if periods:
            update_restaurant(restaurant.id, {
                "weather_cache_json": json.dumps(periods),
                "weather_cached_at": datetime.now().isoformat(),
            }, db_path=db_path)

    if not periods:
        return []

    by_date = {}
    for p in periods:
        if not p.get("isDaytime"):
            continue  # one row per calendar day — skip the "...Night" periods
        pdate = (p.get("startTime") or "")[:10]
        if pdate:
            by_date[pdate] = p

    rows = []
    for d in week_dates:
        p = by_date.get(d)
        if not p:
            continue
        precip = (p.get("probabilityOfPrecipitation") or {}).get("value")
        rows.append({
            "date": d,
            "day_name": p.get("name", ""),
            "high_f": p.get("temperature"),
            "short_forecast": p.get("shortForecast", ""),
            "precip_pct": precip,
        })
    return rows
