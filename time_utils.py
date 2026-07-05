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
