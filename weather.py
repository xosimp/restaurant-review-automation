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
import config
import json
import threading
import time
from datetime import datetime, timedelta

import requests

from models import DB_PATH, update_restaurant


from ai_utils import meter_places as _meter_places

_GOOGLE_KEY = config.google_places_key()
_USER_AGENT = "CavnarAI/1.0 (will@cavnar.ai)"  # NWS asks for an identifying UA, not a key
_CACHE_HOURS = 6


# How long to wait before retrying a place_id that produced no geometry.
#
# Audit #17 measured 572 billed geocode requests in 30 days, of which 570 were
# ONE restaurant: its place_id returns a result with no geometry, the old code
# returned early WITHOUT recording anything, and so every caller re-geocoded
# and re-billed — 71 times a day, forever. That was 61% of all AI and vendor
# spend on the account.
#
# A place_id that resolves but carries no geometry is a permanent condition
# until somebody fixes the place_id, not a transient one. Weekly is often
# enough to pick up a correction and rare enough to cost nothing.
_GEOCODE_RETRY_DAYS = 7


def _geocode_recently_failed(restaurant):
    """True when this restaurant's last geocode attempt failed inside the
    retry window.

    Its own column rather than a key inside weather_cache_json: that column
    holds a LIST of NWS periods and _cached_periods json.loads it as one, so
    putting a dict in there would be two shapes in one field waiting to
    collide.
    """
    failed_at = getattr(restaurant, "geocode_failed_at", None)
    if not failed_at:
        return False
    try:
        return datetime.fromisoformat(failed_at) > datetime.now() - timedelta(days=_GEOCODE_RETRY_DAYS)
    except Exception:
        return False


def _note_geocode_failure(restaurant, db_path=DB_PATH):
    """Record that this place_id produced no usable location."""
    try:
        update_restaurant(restaurant.id,
                          {"geocode_failed_at": datetime.now().isoformat()},
                          db_path=db_path)
    except Exception:
        pass


def _geocode(restaurant, db_path=DB_PATH):
    """Returns (lat, lon) or (None, None).

    A success is cached on the restaurant row, so this hits Google once per
    restaurant. A FAILURE is cached too — see _GEOCODE_RETRY_DAYS for the
    570-call bill that taught us to.
    """
    if restaurant.latitude is not None and restaurant.longitude is not None:
        return restaurant.latitude, restaurant.longitude
    if not restaurant.google_place_id or not _GOOGLE_KEY:
        return None, None
    if _geocode_recently_failed(restaurant):
        return None, None
    if _backing_off(("geocode", db_path, getattr(restaurant, "id", None), restaurant.google_place_id)):
        return None, None
    try:
        resp = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": restaurant.google_place_id, "fields": "geometry", "key": _GOOGLE_KEY},
            timeout=10,
        )
    except Exception:
        # A timeout or a dropped connection says nothing about the place_id.
        # Stamping geocode_failed_at for it blacked weather out for a week
        # over one blip (MOD-INT-7); back off briefly in this process instead.
        _back_off(("geocode", db_path, getattr(restaurant, "id", None), restaurant.google_place_id), _TRANSIENT_BACKOFF_SECS)
        return None, None
    try:
        if getattr(resp, "status_code", 200) >= 500:
            _back_off(("geocode", db_path, getattr(restaurant, "id", None), restaurant.google_place_id), _TRANSIENT_BACKOFF_SECS)
            return None, None
        resp.raise_for_status()
        # Geocoding a restaurant once is cheap, but it is still a billed
        # Places request and belongs in the same ledger as the rest.
        _meter_places(getattr(restaurant, "id", None), "weather_geocode", "details")
        loc = resp.json().get("result", {}).get("geometry", {}).get("location", {})
        lat, lon = loc.get("lat"), loc.get("lng")
        if lat is None or lon is None:
            # Resolved, but carries no location. Permanent until the place_id
            # itself is corrected — do not pay for this answer again today.
            _note_geocode_failure(restaurant, db_path=db_path)
            return None, None
        update_restaurant(restaurant.id,
                          {"latitude": lat, "longitude": lon, "geocode_failed_at": None},
                          db_path=db_path)
        return lat, lon
    except Exception:
        # A 4xx or an unreadable answer from Google is about this place_id,
        # not the network — not worth retrying on every call in a loop over
        # every restaurant.
        _note_geocode_failure(restaurant, db_path=db_path)
        return None, None


# ── negative cache for failed lookups ───────────────────────────────────────
#
# An NWS failure wrote nothing, so every Home, Labor and schedule request for
# the restaurant re-paid the blocking /points + /forecast calls — two 10 s
# timeouts per request during an outage, and a fresh 404 on every read for a
# restaurant outside the US, which NWS will never cover (AI-28, MOD-INT-7).
# Process-local on purpose: it bounds how often THIS process waits on a
# vendor, needs no schema, and a redeploy costing one retry is fine. Keyed by
# restaurant, so it is bounded by the number of restaurants.
_TRANSIENT_BACKOFF_SECS = 15 * 60
_NOT_COVERED_BACKOFF_SECS = 24 * 3600
# A refresh that fails falls back to a cached forecast up to this old: the
# forecast for tomorrow written yesterday beats none, and dates outside what
# it covers simply produce no row.
_STALE_OK_HOURS = 72

_backoff = {}
_backoff_lock = threading.Lock()


def _back_off(key, seconds):
    with _backoff_lock:
        _backoff[key] = time.time() + seconds


def _backing_off(key):
    with _backoff_lock:
        until = _backoff.get(key)
        if until and until > time.time():
            return True
        _backoff.pop(key, None)
        return False


def _fetch_periods_ex(lat, lon):
    """(periods, failure) — failure is None, "transient" or "not_covered"."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
    try:
        points_resp = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=headers, timeout=10)
        if getattr(points_resp, "status_code", 200) == 404:
            return [], "not_covered"
        points_resp.raise_for_status()
        forecast_url = points_resp.json().get("properties", {}).get("forecast")
        if not forecast_url:
            return [], "not_covered"
        fc_resp = requests.get(forecast_url, headers=headers, timeout=10)
        fc_resp.raise_for_status()
        periods = fc_resp.json().get("properties", {}).get("periods", [])
        return (periods, None) if periods else ([], "transient")
    except Exception:
        return [], "transient"


def _fetch_periods(lat, lon):
    """Raw NWS forecast periods for a lat/lon, or [] on any failure."""
    return _fetch_periods_ex(lat, lon)[0]


def _stale_periods(restaurant):
    """The cached periods even past _CACHE_HOURS (up to _STALE_OK_HOURS), for
    when a refresh has failed. None when there is nothing usable."""
    try:
        cached_at = datetime.fromisoformat(restaurant.weather_cached_at)
        if datetime.now() - cached_at > timedelta(hours=_STALE_OK_HOURS):
            return None
        periods = json.loads(restaurant.weather_cache_json or "[]")
    except Exception:
        return None
    return periods if isinstance(periods, list) and periods else None


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
        periods = json.loads(restaurant.weather_cache_json or "[]")
    except Exception:
        return None
    # Only ever a list of periods. Anything else is a corrupt or repurposed
    # cache and must read as "no cache", not be handed to the loop below.
    return periods if isinstance(periods, list) else None


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
        key = ("nws", db_path, restaurant.id, lat, lon)
        failure = "backing_off" if _backing_off(key) else None
        if failure is None:
            periods, failure = _fetch_periods_ex(lat, lon)
        if failure is None:
            update_restaurant(restaurant.id, {
                "weather_cache_json": json.dumps(periods),
                "weather_cached_at": datetime.now().isoformat(),
            }, db_path=db_path)
        else:
            if failure != "backing_off":
                _back_off(key, _NOT_COVERED_BACKOFF_SECS if failure == "not_covered"
                          else _TRANSIENT_BACKOFF_SECS)
            periods = _stale_periods(restaurant) or []

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
