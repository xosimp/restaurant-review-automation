"""
dsr.common — what the block collectors share.

A business date is not a calendar date. A service that closes at 1am belongs
to the night before (time_utils.business_date), and the rows a collector
reads are stamped two different ways:

  local  review_date / fetched_at — the restaurant's own clock
  UTC    every SQLite datetime('now'): posted_at, created_at, captured_at

`local_bounds` and `utc_bounds` give the same stretch of time both ways, so
a review written at 12:40am after a late close, and a reply posted at 05:10
UTC, land on the night they belong to.

`guard` runs one source of a block. A failure becomes a named gap in the
block (and a row in the failure digest via ops.capture), never an exception
out of the collector: a collector does not raise for missing data, and a
source that failed is said, not quietly dropped.
"""
from datetime import datetime, time, timedelta, timezone


def local_bounds(restaurant, day):
    """(start, end) naive local datetimes of the business date `day`:
    BUSINESS_DAY_START_HOUR on the day to the same hour the next morning,
    stretched to the end of that night's service when it runs later, and
    started after the previous night's service when that ran past the
    start hour — the same rule time_utils.business_date applies to one
    moment."""
    from time_utils import BUSINESS_DAY_START_HOUR, service_window
    start = datetime.combine(day, time(BUSINESS_DAY_START_HOUR))
    end = start + timedelta(days=1)
    prev = service_window(restaurant, day - timedelta(days=1))
    if prev and prev[1] > start:
        start = prev[1]
    cur = service_window(restaurant, day)
    if cur and cur[1] > end:
        end = cur[1]
    return start, end


def to_utc(local_naive, restaurant):
    """A naive local datetime as naive UTC."""
    from time_utils import restaurant_tz
    aware = local_naive.replace(tzinfo=restaurant_tz(restaurant))
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def to_local(utc_naive, restaurant):
    """A naive UTC datetime as naive local time."""
    from time_utils import restaurant_tz
    aware = utc_naive.replace(tzinfo=timezone.utc)
    return aware.astimezone(restaurant_tz(restaurant)).replace(tzinfo=None)


def utc_bounds(restaurant, day):
    """The business date's bounds as the "YYYY-MM-DD HH:MM:SS" UTC text
    SQLite's datetime() returns — compare with `datetime(col) >= ? AND
    datetime(col) < ?`, which reads both the 'T' and the space forms."""
    s, e = local_bounds(restaurant, day)
    fmt = "%Y-%m-%d %H:%M:%S"
    return to_utc(s, restaurant).strftime(fmt), to_utc(e, restaurant).strftime(fmt)


def parse_utc(stamp):
    """A stored UTC stamp ("2026-09-22 23:10:00", "2026-09-22T23:10:00",
    with or without an offset) as naive UTC, or None."""
    s = str(stamp or "").strip().replace(" ", "T")
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def local_stamp(stamp, restaurant):
    """A locally-stored stamp as naive local time; one carrying an offset is
    converted to the restaurant's zone first. None when unreadable."""
    from time_utils import parse_stored_dt, restaurant_tz
    return parse_stored_dt(stamp, restaurant_tz(restaurant))


def in_local_day(stamp, restaurant, day, bounds=None):
    """Whether a locally-stamped value falls in the business date. A bare
    date ("2026-09-22", a CSV import) belongs to that calendar date."""
    s = str(stamp or "").strip()
    if not s:
        return False
    if len(s) == 10:
        return s == day.isoformat()
    start, end = bounds or local_bounds(restaurant, day)
    dt = local_stamp(s, restaurant)
    return dt is not None and start <= dt < end


def time_label(local_dt) -> str:
    """`9/22/26 · 11:48pm` (DESIGN_SYSTEM.md → Dates and times)."""
    from time_utils import mdy
    if local_dt is None:
        return ""
    hour = local_dt.hour % 12 or 12
    return f"{mdy(local_dt)} · {hour}:{local_dt.minute:02d}{'am' if local_dt.hour < 12 else 'pm'}"


def guard(ctx, block_name, part, fn, gaps, default=None):
    """Run one source of a block. On failure: record it (ops.capture), add
    `part` to `gaps` and return `default`."""
    try:
        return fn()
    except Exception as e:
        import ops
        ops.capture(e, job=f"dsr.{block_name}", context=f"restaurant_id={ctx.restaurant_id} "
                    f"date={ctx.day} part={part}")
        gaps.append(part)
        return default
