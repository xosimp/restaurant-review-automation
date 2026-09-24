"""
dsr.fiscal — the restaurant's own fiscal calendar.

Erik's DSR is labelled "PERIOD 9 · WEEK 1" and runs Wednesday → Tuesday.
A restaurant sets three facts (restaurants columns):

  fiscal_week_start_dow  0=Monday … 6=Sunday (Python weekday); unset = Monday
  fiscal_year_start      ISO date of Period 1, Week 1's first day; unset = no
                         periods, only the week
  fiscal_period_scheme   "4x13" (13 periods of 4 weeks); "445", "454", "544"
                         (quarters of 4- and 5-week periods in that order);
                         or the exact period lengths in weeks, comma-separated
                         ("4,4,5,4,4,5,4,4,5,4,4,5") — for a calendar that has
                         to line up with an accounting system's (Erik: "a
                         mixture of 4 and 5 week periods … must line up with
                         Back Office"); unset = "4x13"

The fiscal year is named for the calendar year that holds most of it, so a
year starting Wed 12/31/25 is fiscal 2026. A 53-week year is expressed by
listing its lengths (one period of 5 more); the year start is the owner's to
move forward each year if their accounting system does.

Nothing here guesses a calendar: with no year start, a report shows its week
(Wed 8/26 – Tue 9/1) and no period number.
"""
from datetime import date, timedelta

SCHEMES = ("4x13", "445", "454", "544")
_QUARTERS = {"445": [4, 4, 5], "454": [4, 5, 4], "544": [5, 4, 4]}


def _as_date(d):
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def week_start_dow(restaurant) -> int:
    v = getattr(restaurant, "fiscal_week_start_dow", None)
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    return v if 0 <= v <= 6 else 0


def week_bounds(restaurant, day):
    """(first, last) date of the restaurant's week containing `day`."""
    d = _as_date(day)
    start = d - timedelta(days=(d.weekday() - week_start_dow(restaurant)) % 7)
    return start, start + timedelta(days=6)


def period_lengths(scheme):
    """The period lengths in weeks for a scheme, or None when it is not one.
    A named scheme, or 12-13 periods of 4 or 5 weeks totalling 52 or 53."""
    s = str(scheme or "").replace(" ", "")
    if s == "4x13":
        return [4] * 13
    if s in _QUARTERS:
        return _QUARTERS[s] * 4
    try:
        lengths = [int(x) for x in s.split(",") if x != ""]
    except ValueError:
        return None
    if 12 <= len(lengths) <= 13 and all(n in (4, 5) for n in lengths) and sum(lengths) in (52, 53):
        return lengths
    return None


def valid_scheme(scheme) -> bool:
    return period_lengths(scheme) is not None


def _period_lengths(scheme):
    return period_lengths(scheme) or [4] * 13


def position(restaurant, day) -> dict:
    """{"week_start", "week_end", "fiscal_year", "period", "week"} for the
    day. fiscal_year/period/week are None when no year start is set."""
    d = _as_date(day)
    ws, we = week_bounds(restaurant, d)
    out = {"week_start": ws.isoformat(), "week_end": we.isoformat(),
           "fiscal_year": None, "period": None, "week": None}
    raw = getattr(restaurant, "fiscal_year_start", None)
    if not raw:
        return out
    try:
        y0 = _as_date(raw)
    except (TypeError, ValueError):
        return out
    lengths = _period_lengths(getattr(restaurant, "fiscal_period_scheme", None) or "4x13")
    weeks_in_year = sum(lengths)
    # Walk whole fiscal years forward or back from the configured start.
    weeks_from_start = (ws - y0).days // 7
    year_index = weeks_from_start // weeks_in_year
    week_of_year = weeks_from_start - year_index * weeks_in_year   # 0-based
    period, remaining = 1, week_of_year
    for n in lengths:
        if remaining < n:
            break
        remaining -= n
        period += 1
    year_start = y0 + timedelta(weeks=year_index * weeks_in_year)
    fiscal_year = (year_start + timedelta(weeks=weeks_in_year // 2)).year
    out.update({"fiscal_year": fiscal_year, "period": period, "week": remaining + 1})
    return out


def label(restaurant, day) -> str:
    """"Period 9 · Week 1" when periods are configured, else the week range
    in M/D/YY."""
    from time_utils import mdy
    p = position(restaurant, day)
    if p["period"]:
        return f"Period {p['period']} · Week {p['week']}"
    return f"Week of {mdy(p['week_start'])}"
