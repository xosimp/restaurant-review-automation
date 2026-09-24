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
year starting Wed 12/31/25 is fiscal 2026.

A 52-52-53 calendar (an accounting system that adds a week every five or six
years) is a fourth, optional fact:

  fiscal_years_json      [{"start": "YYYY-MM-DD", "lengths": [4, 4, 5, ...]}]
                         - the years that differ from the single start +
                         scheme, each with its own lengths (a 53-week year
                         lists one period of 5 more). A day inside a listed
                         year is placed by that year; any other day walks
                         whole default-scheme years from the latest anchor
                         at or before it (a listed year's end, or
                         fiscal_year_start), so the years after a 53-week
                         year start a week later instead of drifting; a day
                         before every anchor walks back from the earliest.

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


MAX_LISTED_YEARS = 40
_YEAR_SHAPE = "12 or 13 periods of 4 or 5 weeks, totalling 52 or 53"


def _lengths_of(v):
    """A listed year's lengths: a list of ints or a scheme string."""
    if isinstance(v, (list, tuple)):
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in v):
            return None
        v = ",".join(str(n) for n in v)
    if not isinstance(v, str):
        return None
    return period_lengths(v)


def parse_years(raw):
    """(rows, error) from a fiscal_years value (a JSON string or a list):
    rows are [(start date, [lengths])] sorted by start; error is the first
    reason a row was refused, in the owner's words, or None. None, "" and
    [] are no listed years."""
    import json
    from time_utils import mdy
    if raw is None or raw == "" or raw == []:
        return [], None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [], "The listed years aren't readable."
    if not isinstance(raw, list):
        return [], "List each year as a start date and its period lengths."
    if len(raw) > MAX_LISTED_YEARS:
        return [], f"List at most {MAX_LISTED_YEARS} years."
    rows = []
    for item in raw:
        start = None
        if isinstance(item, dict) and isinstance(item.get("start"), str) and len(item["start"]) == 10:
            try:
                start = date.fromisoformat(item["start"])
            except ValueError:
                start = None
        if start is None:
            return [], "Each listed year needs a start date (YYYY-MM-DD)."
        lengths = _lengths_of(item.get("lengths"))
        if lengths is None:
            return [], f"The year starting {mdy(start)} needs {_YEAR_SHAPE}."
        rows.append((start, lengths))
    rows.sort(key=lambda r: r[0])
    for (a, la), (b, _lb) in zip(rows, rows[1:]):
        if a + timedelta(weeks=sum(la)) > b:
            return [], f"The years starting {mdy(a)} and {mdy(b)} overlap."
    return rows, None


def listed_years(restaurant):
    """The restaurant's listed years, [(start, lengths)] - [] when none, or
    when the stored value does not parse (the single start then rules)."""
    rows, err = parse_years(getattr(restaurant, "fiscal_years_json", None))
    return [] if err else rows


def _year_of(restaurant, ws):
    """(year_start, lengths) of the fiscal year holding the week that starts
    on `ws`, or None when nothing anchors a calendar."""
    rows = listed_years(restaurant)
    for start, lengths in rows:
        if start <= ws < start + timedelta(weeks=sum(lengths)):
            return start, lengths
    y0 = None
    raw = getattr(restaurant, "fiscal_year_start", None)
    if raw:
        try:
            y0 = _as_date(raw)
        except (TypeError, ValueError):
            y0 = None
    default = _period_lengths(getattr(restaurant, "fiscal_period_scheme", None) or "4x13")
    # Forward from the latest anchor at or before the week: the end of a
    # listed year (the next year starts there) or the configured start.
    forward = [s + timedelta(weeks=sum(n)) for s, n in rows] + ([y0] if y0 else [])
    before = [a for a in forward if a <= ws]
    if before:
        anchor = max(before)
    else:
        backward = [s for s, _n in rows] + ([y0] if y0 else [])
        if not backward:
            return None
        anchor = min(backward)
    weeks_in_year = sum(default)
    # Walk whole fiscal years forward or back from the anchor.
    year_index = ((ws - anchor).days // 7) // weeks_in_year
    return anchor + timedelta(weeks=year_index * weeks_in_year), default


def position(restaurant, day) -> dict:
    """{"week_start", "week_end", "fiscal_year", "period", "week"} for the
    day. fiscal_year/period/week are None when no year start is set and no
    year is listed."""
    d = _as_date(day)
    ws, we = week_bounds(restaurant, d)
    out = {"week_start": ws.isoformat(), "week_end": we.isoformat(),
           "fiscal_year": None, "period": None, "week": None}
    year = _year_of(restaurant, ws)
    if year is None:
        return out
    year_start, lengths = year
    weeks_in_year = sum(lengths)
    period, remaining = 1, (ws - year_start).days // 7      # 0-based week of the year
    for n in lengths:
        if remaining < n:
            break
        remaining -= n
        period += 1
    fiscal_year = (year_start + timedelta(weeks=weeks_in_year // 2)).year
    out.update({"fiscal_year": fiscal_year, "period": period, "week": remaining + 1})
    return out


def period_span(restaurant, day):
    """(first, last) day of the fiscal period holding `day`, or None when
    there are no periods - from the lengths of the day's own year, so a
    listed 53-week year's long period is its real length."""
    ws, _we = week_bounds(restaurant, day)
    year = _year_of(restaurant, ws)
    if year is None:
        return None
    start, lengths = year
    for n in lengths:
        end = start + timedelta(weeks=n)
        if ws < end:
            return start, end - timedelta(days=1)
        start = end
    return None


def label(restaurant, day) -> str:
    """"Period 9 · Week 1" when periods are configured, else the week range
    in M/D/YY."""
    from time_utils import mdy
    p = position(restaurant, day)
    if p["period"]:
        return f"Period {p['period']} · Week {p['week']}"
    return f"Week of {mdy(p['week_start'])}"
