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


# A forecast row older than this carries stale=True (CA3 F15): the 72-hour
# fallback below used to hand a three-day-old forecast out as if current.
FORECAST_STALE_HOURS = 24


def _cached_at(restaurant):
    """weather_cached_at as an aware UTC datetime, or None. Written UTC with
    an offset now; older rows are server-local naive (time_utils.parse_stamp,
    naive_tz="local")."""
    from time_utils import parse_stamp
    return parse_stamp(getattr(restaurant, "weather_cached_at", None), naive_tz="local")


def _age_hours(cached_at):
    from datetime import timezone as _tz
    if cached_at is None:
        return None
    return max(0.0, (datetime.now(_tz.utc) - cached_at).total_seconds() / 3600.0)


def _stale_periods(restaurant):
    """The cached periods even past _CACHE_HOURS (up to _STALE_OK_HOURS), for
    when a refresh has failed. None when there is nothing usable."""
    try:
        age = _age_hours(_cached_at(restaurant))
        if age is None or age > _STALE_OK_HOURS:
            return None
        periods = json.loads(restaurant.weather_cache_json or "[]")
    except Exception:
        return None
    return periods if isinstance(periods, list) and periods else None


def _cached_periods(restaurant):
    """Returns the cached periods list if fresh, None if missing/stale/bad."""
    if not restaurant.weather_cached_at:
        return None
    age = _age_hours(_cached_at(restaurant))
    if age is None or age > _CACHE_HOURS:
        return None
    try:
        periods = json.loads(restaurant.weather_cache_json or "[]")
    except Exception:
        return None
    # Only ever a list of periods. Anything else is a corrupt or repurposed
    # cache and must read as "no cache", not be handed to the loop below.
    return periods if isinstance(periods, list) else None


def _periods_by_date(restaurant, db_path=DB_PATH):
    """({date: daytime period}, {date: night period}) from the cached NWS
    forecast, refreshed when it is older than _CACHE_HOURS. ({}, {}) on any
    failure (no coordinates, NWS unreachable) — never raises.

    What the cache held before a refresh replaces it is kept too. NWS drops
    a day's daytime period once it has passed, so a refresh at 11pm has
    nothing for today — and the nightly report asks about today. A date the
    new forecast no longer covers is answered from the older forecast (up
    to _STALE_OK_HOURS old, as for a failed refresh); a date both cover
    takes the newer one."""
    periods = _cached_periods(restaurant)
    previous = None
    # Every period is tagged with when ITS copy of the forecast was fetched
    # (`_as_of`, UTC ISO), so each row can say how old it is (CA3 F15).
    old_stamp = _cached_at(restaurant)
    old_iso = old_stamp.isoformat() if old_stamp else None
    periods_as_of = old_iso
    if periods is None:
        previous = _stale_periods(restaurant)
        lat, lon = _geocode(restaurant, db_path=db_path)
        if lat is None or lon is None:
            return {}, {}
        key = ("nws", db_path, restaurant.id, lat, lon)
        failure = "backing_off" if _backing_off(key) else None
        if failure is None:
            periods, failure = _fetch_periods_ex(lat, lon)
        if failure is None:
            from datetime import timezone as _tz
            fetched = datetime.now(_tz.utc).isoformat()
            update_restaurant(restaurant.id, {
                "weather_cache_json": json.dumps(periods),
                # UTC with an offset: it was server-local naive, one of the
                # five stamp formats time_utils.parse_stamp now reconciles.
                "weather_cached_at": fetched,
            }, db_path=db_path)
            periods_as_of = fetched
        else:
            if failure != "backing_off":
                _back_off(key, _NOT_COVERED_BACKOFF_SECS if failure == "not_covered"
                          else _TRANSIENT_BACKOFF_SECS)
            periods = _stale_periods(restaurant) or []

    by_day, by_night = {}, {}
    for src, as_of in ((previous or [], old_iso), (periods or [], periods_as_of)):
        for p in src:
            pdate = (p.get("startTime") or "")[:10]
            if pdate:
                p = dict(p)
                p["_as_of"] = as_of
                (by_day if p.get("isDaytime") else by_night)[pdate] = p
    return by_day, by_night


def _row(d, p):
    """One forecast row. `as_of` (UTC ISO) is when this copy of the forecast
    was fetched, `age_hours` how old it is now — None when unknown, never 0
    — and `stale` is True past FORECAST_STALE_HOURS or when the age is
    unknown: a 72-hour-old fallback must not read as today's forecast."""
    from time_utils import parse_stamp
    precip = (p.get("probabilityOfPrecipitation") or {}).get("value")
    as_of = p.get("_as_of")
    age = _age_hours(parse_stamp(as_of)) if as_of else None
    return {
        "date": d,
        "day_name": p.get("name", ""),
        "high_f": p.get("temperature"),
        "short_forecast": p.get("shortForecast", ""),
        "precip_pct": precip,
        "as_of": as_of,
        "age_hours": round(age, 1) if age is not None else None,
        "stale": age is None or age > FORECAST_STALE_HOURS,
    }


def get_forecast_for_week(restaurant, week_dates, db_path=DB_PATH):
    """One row per date in week_dates that NWS has a forecast for — NWS only
    forecasts about a week out, so later dates in the week may be omitted
    entirely rather than guessed at. Returns [] on any failure (no
    restaurant, no coordinates, NWS unreachable) — never blocks schedule
    generation.

    Each row: {"date", "day_name", "high_f", "short_forecast", "precip_pct"}.
    One row per calendar day: the "...Night" periods are skipped.
    """
    if not restaurant:
        return []
    by_day, _night = _periods_by_date(restaurant, db_path=db_path)
    return [_row(d, by_day[d]) for d in week_dates if d in by_day]


def forecast_for_day(restaurant, day, db_path=DB_PATH):
    """The forecast NWS gave for one date, day and night:
    {"day": row or None, "night": row or None}, where a night row carries
    `low_f` instead of `high_f`. Both None on any failure.

    For the nightly report: after close the daytime period may be gone from
    every copy of the forecast Cavnar holds, and "Tonight" is then the only
    reading of the evening that was served. It is a FORECAST either way —
    nothing here is observed weather."""
    if not restaurant:
        return {"day": None, "night": None}
    d = str(day)[:10]
    by_day, by_night = _periods_by_date(restaurant, db_path=db_path)
    night = None
    if d in by_night:
        night = _row(d, by_night[d])
        night["low_f"] = night.pop("high_f")
    return {"day": _row(d, by_day[d]) if d in by_day else None, "night": night}
