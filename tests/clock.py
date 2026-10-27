"""Time in tests: the restaurant's clock, never the process's (audit #25,
A01-03 / A02-05).

The code asks `time_utils` for a restaurant's "today" (its own timezone);
a test that builds "today" with `date.today()` or a naive `datetime.now()`
asks the PROCESS's timezone instead. The two agree on a Chicago laptop at
noon and disagree every evening on a UTC runner, which kept CI red for weeks
while hiding real regressions. CI now runs at UTC+14 (TZ=Pacific/Kiritimati),
where they disagree all day, so a test that mixes them fails at once.

Use these instead:

    from tests.clock import restaurant_today, today_for, this_monday, frozen

    today = restaurant_today()            # the fixtures' default zone (Chicago)
    today = today_for(rid)                # what the code under test computes
    monday = this_monday()                # the restaurant's current week
    with frozen("2026-10-25 23:30", tz="America/Chicago"):
        ...                               # a fixed instant (time-machine)

`tests/test_no_wall_clock_in_tests.py` is a ratchet: a new `date.today()` /
`datetime.now(` / `utcnow(` in tests/ fails it.
"""
import datetime as dt
from zoneinfo import ZoneInfo

# Every fixture restaurant is created with models' default timezone.
DEFAULT_TZ = "America/Chicago"


def restaurant_now(tz=DEFAULT_TZ, naive=False):
    """The current instant on the restaurant's wall clock."""
    import time_utils
    return time_utils.restaurant_now(tz, naive=naive)


def restaurant_today(tz=DEFAULT_TZ):
    """Today's date in the restaurant's timezone (the default: Chicago, the
    zone every fixture restaurant is created in)."""
    return restaurant_now(tz).date()


def today_for(restaurant_id):
    """Today for one restaurant, exactly as the code computes it
    (`time_utils.restaurant_now_by_id`). The restaurant is read from the
    default database, so call it where the test has routed that to its own
    (models.get_conn / DB_PATH patched) — otherwise use restaurant_today()."""
    import time_utils
    return time_utils.restaurant_now_by_id(restaurant_id).date()


def monday_of(day):
    """The Monday of `day`'s week."""
    return day - dt.timedelta(days=day.weekday())


def this_monday(tz=DEFAULT_TZ):
    """The restaurant's current week's Monday."""
    return monday_of(restaurant_today(tz))


def next_monday(tz=DEFAULT_TZ):
    """The restaurant's next week's Monday."""
    return this_monday(tz) + dt.timedelta(days=7)


def utc_now():
    """The current instant, aware, in UTC (a stamp the code writes in UTC)."""
    return dt.datetime.now(dt.timezone.utc)


def frozen(when, tz=DEFAULT_TZ, tick=False):
    """Freeze the clock at `when` (an aware datetime, a naive datetime or a
    "YYYY-MM-DD HH:MM" string read in `tz`) for every reader — time.time,
    datetime.now, date.today — through time-machine. Use as a context
    manager or decorator:

        with frozen("2026-11-01 01:30", tz="America/Chicago"):  # DST fall-back
            ...
    """
    import time_machine
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when)
    if isinstance(when, dt.date) and not isinstance(when, dt.datetime):
        when = dt.datetime(when.year, when.month, when.day, 12, 0)
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz))
    return time_machine.travel(when, tick=tick)
