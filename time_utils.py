"""
time_utils.py — per-restaurant timezone resolution.

"America/Chicago" was hardcoded in ~34 places, which silently breaks every
"today", "this week", and holiday calculation the moment a client outside
Central time signs up. Anything computing time *for a specific restaurant*
should go through restaurant_now(); the scheduler's global cadence (2am
backup, 10am job sweep) intentionally stays on operator time.
"""
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

OPERATOR_TZ = "America/Chicago"   # Will's ops timezone — scheduler cadence only

# Shown in the admin settings dropdown; any IANA name is accepted via storage.
COMMON_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
]


def restaurant_tz(restaurant_or_tz) -> ZoneInfo:
    """Resolve a ZoneInfo from a Restaurant object, an IANA string, or None.
    Unknown/invalid names fall back to operator time rather than crashing a
    report over a typo in settings."""
    name = None
    if restaurant_or_tz is None:
        name = OPERATOR_TZ
    elif isinstance(restaurant_or_tz, str):
        name = restaurant_or_tz
    else:
        name = getattr(restaurant_or_tz, "timezone", None) or OPERATOR_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(OPERATOR_TZ)


def restaurant_now(restaurant_or_tz=None, naive: bool = False) -> datetime:
    """Current time in the restaurant's local timezone. naive=True strips
    tzinfo for call sites that compare against naive datetimes."""
    now = datetime.now(restaurant_tz(restaurant_or_tz))
    return now.replace(tzinfo=None) if naive else now


_tz_hints = threading.local()


@contextmanager
def known_timezones(mapping):
    """For a sweep that already SELECTed each restaurant's timezone column:
    inside this block restaurant_now_by_id answers from `mapping`
    ({restaurant_id: tz name}) instead of loading the restaurant. The hourly
    alert pass asked "is it 10am there?" of every restaurant with a query
    each, forever, to skip nearly all of them (MOD-NOT-11). Thread-local, so
    a request thread never sees a scheduler sweep's hints."""
    prev = getattr(_tz_hints, "map", None)
    _tz_hints.map = dict(mapping or {})
    try:
        yield
    finally:
        _tz_hints.map = prev


def restaurant_now_by_id(restaurant_id: int, naive: bool = False) -> datetime:
    """Same, for call sites that only have an id in hand."""
    hints = getattr(_tz_hints, "map", None)
    if hints and restaurant_id in hints:
        return restaurant_now(hints[restaurant_id], naive=naive)
    try:
        from models import get_restaurant
        return restaurant_now(get_restaurant(restaurant_id), naive=naive)
    except Exception:
        return restaurant_now(None, naive=naive)


def parse_stored_dt(value, tz=OPERATOR_TZ):
    """Parse a timestamp out of the database into a NAIVE local datetime.

    Stored timestamps are not consistent: SQLite's datetime('now') writes
    "2026-07-06T13:48:36" (naive UTC) while anything built from
    datetime.now(timezone.utc).isoformat() writes
    "2026-05-08T00:04:01+00:00" (offset-aware). Subtracting one from the
    other raises "can't subtract offset-naive and offset-aware datetimes",
    which is exactly what killed the onboarding_emails job — and, more
    quietly, made check_inactive_clients skip those same rows.

    Anything offset-aware is converted into `tz` and stripped, so the result
    is always comparable with restaurant_now(naive=True) / _chi_now().
    Returns None on anything unparseable, so callers can skip a bad row
    instead of taking down a whole sweep.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone(ZoneInfo(tz) if isinstance(tz, str) else tz)
        except Exception:
            pass
        dt = dt.replace(tzinfo=None)
    return dt


def parse_stamp(value, naive_tz="UTC"):
    """Any timestamp this codebase stores, as an AWARE UTC datetime, or None.

    The one parser every freshness reading goes through (confidence audit
    CA3 F15). Five formats are in the tables, and which zone an offset-less
    stamp means depends on who wrote it, so the rule is explicit:

      * an offset or a trailing Z ("…+00:00", "…Z")   → that instant;
      * SQLite's space form ("2026-09-24 13:05:00")   → UTC, always — it is
        what datetime('now') and time_utils.utc_stamp write;
      * a naive 'T' form ("2026-09-24T08:05:00") or a bare date → `naive_tz`,
        which the CALLER names because the column decides it: Chicago for
        restaurants.last_fetched_at (models.update_last_fetched), UTC for
        rpower_last_synced, "local" for the server-local stamps weather.py
        wrote before it stamped UTC.

    `naive_tz` is an IANA name, a tzinfo, or "local". A datetime passes
    through (naive → naive_tz); a date is its midnight in naive_tz.
    Unparseable → None, never an exception: a freshness reading that
    cannot parse its stamp is "unknown", not "fresh"."""
    from datetime import date as _date, timezone as _tz
    if value is None or value == "":
        return None

    def _zone():
        if naive_tz in (None, "local"):
            return None
        if isinstance(naive_tz, str):
            try:
                return ZoneInfo(naive_tz)
            except Exception:
                return _tz.utc
        return naive_tz

    def _localise(dt):
        z = _zone()
        return dt.astimezone() if z is None else dt.replace(tzinfo=z)

    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else _localise(value)
        return dt.astimezone(_tz.utc)
    if isinstance(value, _date):
        return _localise(datetime(value.year, value.month, value.day)).astimezone(_tz.utc)
    text = str(value).strip()
    if not text:
        return None
    sqlite_space = len(text) > 10 and text[10] == " "
    iso = text.replace(" ", "T", 1) if sqlite_space else text
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc) if sqlite_space else _localise(dt)
    return dt.astimezone(_tz.utc)


def age_days(value, naive_tz="UTC", now=None):
    """Days since `value` (parse_stamp rules), never negative; None when the
    stamp is missing or unreadable."""
    from datetime import timezone as _tz
    dt = parse_stamp(value, naive_tz=naive_tz)
    if dt is None:
        return None
    now = now or datetime.now(_tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def utc_stamp(dt=None) -> str:
    """`dt` (or now) as the UTC "YYYY-MM-DD HH:MM:SS" the ledgers store.
    Was defined identically in security, activity and delayed."""
    from datetime import timezone
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def local_iso(value, tz=None) -> str:
    """A stored instant as the restaurant's own day, YYYY-MM-DD. A UTC
    stamp's first ten characters are UTC's date: 8:21pm on 9/26 in St. Louis
    read "Last updated 9/27/26" (owner, 9/26/26). An offset-less space stamp
    is UTC (parse_stamp); anything unparseable keeps its first ten characters."""
    if not value:
        return ""
    try:
        dt = parse_stamp(value, naive_tz="UTC")
        if dt is None:
            return str(value)[:10]
        return dt.astimezone(restaurant_tz(tz)).date().isoformat()
    except Exception:
        return str(value)[:10]


def mdy(value) -> str:
    """M/D/YY with no leading zeros — `9/21/26` — the one date format an
    owner reads (DESIGN_SYSTEM.md → Dates and times). Takes a date, a
    datetime or an ISO string; anything unparseable comes back unchanged
    rather than raising inside a sentence."""
    from datetime import date as _date, datetime as _datetime
    if value is None or value == "":
        return ""
    if isinstance(value, _datetime):
        d = value.date()
    elif isinstance(value, _date):
        d = value
    else:
        s = str(value).strip()
        try:
            d = _date.fromisoformat(s[:10])
        except ValueError:
            return s
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


def mdy_range(start, end) -> str:
    """`9/14/26 – 9/20/26`, or a single date when both ends are the same day."""
    a, b = mdy(start), mdy(end)
    return a if a == b or not b else f"{a} – {b}"


# ── Opening hours ────────────────────────────────────────────────────────────
# One reading of a restaurant's hours for every "is it open / when does it
# close" question. Hours are stored per weekday as the owner typed them
# ("4:00pm", "1:00am", or "01:00" from the web's <input type=time>), and a
# close at or after midnight is the NEXT calendar day's clock: Friday
# "4:00pm"–"1:00am" is one service running into Saturday. Comparing that
# close as a same-day time read the restaurant as closed all day, which
# silently stopped every intraday job for any late-night place (A-1).
#
# A service belongs to the day it OPENED — its business date. The hours past
# midnight are that business date's, not the new calendar day's.

# The clock hour a sales or labor curve keyed by hour treats as the start of
# the business day's timeline (an hour before it is the night's tail):
# hour PLACEMENT only — schedule_time.business_minute, the DSR's hourly
# curves, the iOS staff clock. WHICH business date it is turns over at
# day_start_minutes() below (audit #23 A3-14 / A2-24).
BUSINESS_DAY_START_HOUR = 5

# The ONE business-date boundary (audit #23 A3-14 / A2-24, 10/21/26): the
# date a moment, a punch or a "today" belongs to turns over here — the
# schedule's own rule (schedule_time.NIGHT_TAIL_BEFORE, 4am: a 4:30am baker
# is the morning's) — unless the restaurant trades past it, when its own
# latest close is the boundary (a 4:30am-licence bar's 4:15 tickets and
# punches are the night's). Punches filed on 4am while "today" turned over
# at 5am: at 4:00-4:59 live labor read yesterday's schedule against 4am-4am
# punches (a 4:30 baker unseen) and a night was "complete" an hour after its
# punches had moved on.
DAY_TURNOVER_MINUTES = 4 * 60
# A boundary never runs past this (a close later than 6am is a 24-hour
# store's, whose day turns over at the default).
DAY_TURNOVER_MAX_MINUTES = 6 * 60


def parse_clock(value):
    """'11:00am' / '9:30pm' / '17:30' / '12:00am' -> (hour, minute), or None."""
    raw = str(value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    ampm = None
    for suffix in ("am", "pm"):
        if raw.endswith(suffix):
            ampm, raw = suffix, raw[:-2]
            break
    parts = raw.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def opening_hours(restaurant, weekday_name):
    """((open_h, open_m) or None, (close_h, close_m) or None) as configured
    for this weekday, or None when neither is set. The raw clock readings —
    use service_window() for real datetimes."""
    import json as _json

    def _load(raw):
        try:
            return _json.loads(raw) if raw else {}
        except Exception:
            return {}

    if restaurant is None:
        return None
    opens = parse_clock(_load(getattr(restaurant, "open_times_json", None)).get(weekday_name))
    closes = parse_clock(_load(getattr(restaurant, "close_times_json", None)).get(weekday_name))
    return (opens, closes) if (opens or closes) else None


def service_window(restaurant, day):
    """(opens_at, closes_at) as naive local datetimes for the service whose
    business date is `day`, or None when that weekday has no hours set.

    A close at or before the open (1:00am after a 4:00pm open, or 12:00am)
    is the next morning. With only a close set, the day starts at
    BUSINESS_DAY_START_HOUR, so a bare "1:00am" close is still read as
    tonight's late close rather than a service that ended before breakfast.
    With only an open set, the service runs to midnight."""
    from datetime import datetime as _dt, time as _time, timedelta as _td
    hours = opening_hours(restaurant, day.strftime("%A"))
    if not hours:
        return None
    opens, closes = hours
    start = _dt.combine(day, _time(*opens)) if opens else \
        _dt.combine(day, _time(BUSINESS_DAY_START_HOUR, 0))
    end = _dt.combine(day, _time(*closes)) if closes else _dt.combine(day + _td(days=1), _time(0, 0))
    if end <= start:
        end += _td(days=1)
    return start, end


def open_service_day(restaurant, local):
    """The business date of the service open at `local`, or None when the
    restaurant is closed then (by its configured hours). Yesterday's
    service is checked first: at 12:30am after a 1:00am-close Friday, it is
    still Friday's service."""
    from datetime import timedelta as _td
    for day in (local.date() - _td(days=1), local.date()):
        window = service_window(restaurant, day)
        if window and window[0] <= local < window[1]:
            return day
    return None


def is_open_at(restaurant, local, default_hours=None):
    """True when the restaurant is open at naive local time `local`.

    Handles a close at or after midnight (see service_window). When today's
    weekday has no hours set, `default_hours` ((open_hour, close_hour)) is
    the fallback window; without one, an unconfigured day reads as closed —
    unless last night's late service is still running."""
    if open_service_day(restaurant, local) is not None:
        return True
    if service_window(restaurant, local.date()) is not None:
        return False
    if default_hours:
        return default_hours[0] <= local.hour < default_hours[1]
    return False


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def day_start_minutes(restaurant=None) -> int:
    """Minutes past local midnight at which this restaurant's business date
    turns over — the one boundary (DAY_TURNOVER_MINUTES, 4am), moved later
    only when its own hours close past it on some weekday (a 4:30am close
    turns the day over at 4:30), never past DAY_TURNOVER_MAX_MINUTES. A
    close past the boundary that is the same day's (a 7am-3pm cafe) or an
    unreadable hours row leaves the default."""
    base = DAY_TURNOVER_MINUTES
    if restaurant is None:
        return base
    latest = base
    for wd in _WEEKDAYS:
        try:
            hours = opening_hours(restaurant, wd)
        except Exception:
            hours = None
        if not hours or not hours[1]:
            continue
        opens, closes = hours
        cm = closes[0] * 60 + closes[1]
        om = (opens[0] * 60 + opens[1]) if opens else None
        # A small-hours close: past the boundary, before the cap, and before
        # the day's own open (so it is last night's close, not today's).
        if base < cm <= DAY_TURNOVER_MAX_MINUTES and (om is None or om > cm):
            latest = max(latest, cm)
    return min(latest, DAY_TURNOVER_MAX_MINUTES)


def business_date(restaurant, local):
    """The service date `local` belongs to: the service open right now if
    there is one, else the calendar date — except before the restaurant's
    day_start_minutes() (4am unless it trades later), when it is still last
    night. The same boundary its punches are filed on
    (rpower.punch_business_date), so "today" and the punches never disagree
    for an hour (audit #23 A3-14)."""
    from datetime import timedelta as _td
    day = open_service_day(restaurant, local)
    if day is not None:
        return day
    before = local.hour * 60 + local.minute < day_start_minutes(restaurant)
    return (local.date() - _td(days=1)) if before else local.date()


def business_date_by_id(restaurant_id, local=None):
    """business_date for a restaurant id at `local` (its own wall clock when
    None) — for a reader holding only the id. Its calendar date when the row
    cannot be read."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
    except Exception:
        r = None
    local = local or restaurant_now(r, naive=True)
    return business_date(r, local) if r is not None else local.date()
