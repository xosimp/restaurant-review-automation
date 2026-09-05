"""
time_utils.py — per-restaurant timezone resolution.

"America/Chicago" was hardcoded in ~34 places, which silently breaks every
"today", "this week", and holiday calculation the moment a client outside
Central time signs up. Anything computing time *for a specific restaurant*
should go through restaurant_now(); the scheduler's global cadence (2am
backup, 10am job sweep) intentionally stays on operator time.
"""
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


def restaurant_now_by_id(restaurant_id: int, naive: bool = False) -> datetime:
    """Same, for call sites that only have an id in hand."""
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
