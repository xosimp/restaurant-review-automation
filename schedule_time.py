"""schedule_time.py — the one notion of time a schedule row has (pure, L0).

A schedule row is ``{date, shift_start, shift_end, ...}``. Its ``date`` is the
BUSINESS date the shift belongs to, and its times are wall-clock strings
("5:00pm", "17:00", "noon"). Every question about when a shift is — where it
sits on its night, how long it really is, which daypart it serves, when a day
closes — is answered here, once (schedule audit 10/10/26 ARCH-12, BUGSGEN-8,
BUGSGEN-25). About fifteen private copies of these rules had grown up across
the rules, the scorer, the optimizer, the manager plan, the station check, the
crew grid and the learners, and they disagreed: a 12:30am-4:00am porter was
minutes 1470-1680 to the rules and 30-240 to the optimizer; a 1:00-5:30am
shift sat on the next day while 1:00-6:30am sat on the same one; a 10am-3:15pm
cook covered the DINNER grill station; five different "dinners" were counted.

The business-day timeline
-------------------------
Minutes are counted from midnight at the start of the row's business date.
A start before ``NIGHT_TAIL_BEFORE`` (4:00am) is the small hours of that date's
night — minute 1440 and on (a 12:30am porter dated Friday works Saturday
00:30, after Friday's close). From 4:00am a start is the date's own early prep
(a 4:30am baker is the morning's, owner/lead 10/3/26). The START alone places
a shift — never its end, so an hour's difference in when it ends can never
move it a day (BUGSGEN-8). An end at or before its start runs past midnight.

``time_utils.BUSINESS_DAY_START_HOUR`` (5am) is when "today" turns over for
close-outs and the POS's business day; a shift starting 4:00-4:59am is a
baker's prep for the coming day, so shift placement turns over an hour
earlier on purpose.

Real hours
----------
``real_hours(row, tz)`` is the time actually worked between the two instants
in the restaurant's zone: a 5pm-2am close on the night the clocks go back
(10/31/26 into 11/1/26) is 10 hours, not 9, and a 5pm-3am close on the night
they go forward (3/13/27 into 3/14/27) is 9, not 10. Every
writer of a row's ``scheduled_hours`` writes ``hours_text(real_hours(row,
tz))``: two decimals, never banker's rounding to a tenth (BUGSGEN-6, -7).

Dayparts — the one lunch/dinner rule
------------------------------------
A shift is in every daypart whose core service window (``CORE_WINDOWS``:
lunch 11:00am-2:30pm, dinner 5:30pm-8:30pm) it covers for at least
``PRESENCE_MIN_OVERLAP`` (60) minutes — ``present_dayparts``; its own daypart
(``primary_daypart``) is the one it covers most. A shift covering neither
core window belongs to the daypart its start falls in: before the restaurant's
daypart boundary (``DAYPART_SPLIT``, 3:00pm unless the restaurant set its own —
``daypart_scope``) is the morning, from it the night, and the small hours are
the night they belong to (``start_daypart``). ``touched_dayparts`` — any
minute in a core window — is for blocks (part-day time off, morning-only
availability), where one minute over is already over. Callers pick one of
these by name; nothing else in the app decides "lunch" or "dinner".

Closing times
-------------
``close_on_timeline(close, open)`` reads a day's close relative to its open:
a close at or before the open is after midnight (a 6am-6am diner closes at
6am the NEXT morning, a 4pm-2am bar at 2am); with no open on file a close
before ``SMALL_HOURS_BEFORE`` (6am) is after midnight (BUGSGEN-22).

Pure: no database, no clock read, no network. Module-scope imports: stdlib
and ``time_utils`` (L0) only.
"""
from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from time_utils import BUSINESS_DAY_START_HOUR  # noqa: F401  (re-exported: the business day's turnover)

DAY = 24 * 60
# A shift starting before this is the small hours of its business date's night.
NIGHT_TAIL_BEFORE = 4 * 60
# A close or a "latest" before this, with no opening time to judge it by, is
# the small hours after midnight ("until 1:00am", a 2am close).
SMALL_HOURS_BEFORE = 6 * 60
# The daypart boundary when the restaurant set none: a shift covering neither
# core window that starts before 3pm is the morning's.
DAYPART_SPLIT = 15 * 60
# The earliest and latest boundary a restaurant may set (restaurants.
# daypart_boundary): it must fall between the lunch rush and the dinner one.
DAYPART_SPLIT_MIN = 12 * 60
DAYPART_SPLIT_MAX = 18 * 60
# The core service window of each daypart, and how much of it a shift must
# cover to count as on the floor for it.
CORE_WINDOWS = {"morning": (11 * 60, 14 * 60 + 30), "night": (17 * 60 + 30, 20 * 60 + 30)}
# The ONE pair of words an owner reads for the two dayparts, on every
# surface and in every server sentence (schedule audit 10/10/26 TRUST-31:
# Setup said Daytime/Night, Crew Lunch/Dinner, Intel day/night and a Fix
# line morning/night — four names for one 3pm split). The web's
# SCHED_PART_WORD and iOS read the same pair.
PART_WORDS = {"morning": "Lunch", "night": "Dinner"}


def part_word(part, capital: bool = True) -> str:
    """"Lunch" / "Dinner" for a daypart key ("morning" / "night"; "lunch",
    "dinner", "day", "am", "pm" are read too) — lower case unless
    `capital`. '' for anything else."""
    p = str(part or "").strip().lower()
    key = "morning" if p in ("morning", "lunch", "day", "daytime", "am") else \
        "night" if p in ("night", "dinner", "evening", "pm") else None
    if key is None:
        return ""
    w = PART_WORDS[key]
    return w if capital else w.lower()
PRESENCE_MIN_OVERLAP = 60
DAYPARTS = ("morning", "night")


# ── reading a clock time ────────────────────────────────────────────────────

_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?(am|pm)?$")
_WORDS = {"noon": 12 * 60, "midday": 12 * 60, "12noon": 12 * 60, "midnight": 0, "12midnight": 0}


def parse_minutes(value):
    """A clock time as minutes past midnight (0-1439), or None.

    Reads every way an hours value or a rule time has been stored: "9:30pm",
    "9:30 PM", "9pm", "9:30p", "9:30 p.m.", "12:00 a.m.", "21:30", "21:30:00",
    "noon", "midnight", "24:00" (midnight). A bare hour is read only when it
    cannot be a 12-hour time ("0", "13"-"23"); a bare "9" is ambiguous — 9am
    or 9pm — and reads as None, so the caller says it could not read it
    rather than guess (BUGSGEN-20). Remembered per spelling: the sweep reads
    the same few dozen times on every trial (schedule audit 10/3/26 P-37)."""
    if value is None:
        return None
    raw = str(value).strip().lower().replace(" ", "")
    if not raw:
        return None
    return _parse(raw)


@lru_cache(maxsize=8192)
def _parse(raw: str):
    if raw in _WORDS:
        return _WORDS[raw]
    txt = raw.replace(".", "")
    if txt.endswith(("a", "p")) and not txt.endswith(("am", "pm")):
        txt += "m"
    m = _CLOCK_RE.match(txt)
    if not m:
        return None
    hour, minute, suffix = int(m.group(1)), int(m.group(2) or 0), m.group(4)
    if minute > 59 or (m.group(3) is not None and int(m.group(3)) > 59):
        return None
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    else:
        if m.group(2) is None and 1 <= hour <= 12:
            return None                      # a bare "9": morning or night?
        if hour == 24 and minute == 0:
            hour = 0
        elif hour > 23:
            return None
    return hour * 60 + minute


def fmt_minutes(minutes) -> str:
    """Minutes past midnight (any day: 1500 is 1:00am) as the rows write a
    time: "1:00am", "4:30pm", "12:00pm"."""
    m = int(minutes) % DAY
    h, mm = divmod(m, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def normalise_clock(value):
    """A stored hours value as the one spelling every reader parses
    ("4:00pm"), or None when it cannot be read — for the writers of
    open/close times and closures (BUGSGEN-20: normalised at write)."""
    m = parse_minutes(value)
    return None if m is None else fmt_minutes(m)


# ── placing a shift on its business date ───────────────────────────────────

def night_offset(start_m) -> int:
    """1440 when a start (minutes past midnight) is in the small hours of the
    row's night — before NIGHT_TAIL_BEFORE — else 0. The start alone decides
    (BUGSGEN-8)."""
    return DAY if start_m is not None and 0 <= start_m < NIGHT_TAIL_BEFORE else 0


def place(start_m, end_m):
    """(start, end) minutes on the business date's timeline for clock
    minutes `start_m`-`end_m`: the start placed by night_offset, an end
    before the start past midnight. None when either is None — and when the
    two are the SAME time: "5:00pm-5:00pm" is a typo, not a 24-hour shift
    (25h on the fall-back night); read as one it became a day-long shift
    before the close cap re-timed it (sched re-audit 10/11/26 BUGSGEN-19).
    The parser drops such a row and counts it, like any unreadable line."""
    if start_m is None or end_m is None or start_m % DAY == end_m % DAY:
        return None
    off = night_offset(start_m)
    end = end_m if end_m > start_m else end_m + DAY
    return start_m + off, end + off


def span(row):
    """(start, end) minutes of `row` on its business date's timeline, or
    None when a time cannot be read."""
    return place(parse_minutes((row or {}).get("shift_start")), parse_minutes((row or {}).get("shift_end")))


def start_of(row):
    """A row's start on its business date's timeline (past 1440 in the small
    hours of its night), or None."""
    s = parse_minutes((row or {}).get("shift_start"))
    return None if s is None else s + night_offset(s)


def end_of(row):
    """A row's end on its business date's timeline, or None. With no
    readable start the end is read as on its date's own clock."""
    sp = span(row)
    if sp is not None:
        return sp[1]
    return parse_minutes((row or {}).get("shift_end"))


_ZONES = {}


def zone(tz):
    """The ZoneInfo for an IANA name, remembered; None for no or a bad name."""
    if not tz:
        return None
    key = str(tz)
    if key not in _ZONES:
        try:
            from zoneinfo import ZoneInfo
            _ZONES[key] = ZoneInfo(key)
        except Exception:
            _ZONES[key] = None
    return _ZONES[key]


@lru_cache(maxsize=4096)
def _base_date(iso: str):
    return datetime.strptime(iso, "%Y-%m-%d")


def shift_span(row, tz=None):
    """(start_dt, end_dt) of `row` as datetimes, or (None, None) when its
    date or a time cannot be read. Without `tz` the wall clock (naive, on the
    business date's timeline); with the restaurant's IANA zone the real
    instants, as naive UTC — a close and an open either side of a clock
    change are an hour closer or further apart than the wall clock says."""
    try:
        base = _base_date(str((row or {}).get("date") or "")[:10])
    except (ValueError, TypeError):
        return None, None
    sp = span(row)
    if sp is None:
        return None, None
    start, end = base + timedelta(minutes=sp[0]), base + timedelta(minutes=sp[1])
    z = zone(tz)
    if z is not None:
        start = start.replace(tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None)
        end = end.replace(tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def real_hours(row, tz=None) -> float:
    """The hours `row` is actually worked, to two decimals: with the
    restaurant's zone the real instants (a 5pm-2am close dated 10/31/26 is
    10.0; a 5pm-3am close dated 3/13/27 is 9.0), without it the wall clock.
    0.0 when unreadable."""
    s, e = shift_span(row, tz)
    return round((e - s).total_seconds() / 3600, 2) if s is not None and e is not None else 0.0


def wall_hours(row) -> float:
    """The hours between a row's two wall-clock times (no zone, no date
    needed), 2 decimals; 0.0 when unreadable."""
    sp = span(row)
    return round((sp[1] - sp[0]) / 60, 2) if sp is not None else 0.0


def hours_text(hours) -> str:
    """Hours as the rows carry them: "9", "7.5", "6.25" — two decimals at
    most, never a tenth's banker's rounding (6.25 stays 6.25, BUGSGEN-7)."""
    try:
        h = round(float(hours or 0) + 0.0, 2)
    except (TypeError, ValueError):
        h = 0.0
    return (f"{h:.2f}".rstrip("0").rstrip(".")) or "0"


def row_hours_text(row, tz=None) -> str:
    """hours_text(real_hours(row, tz)) — what every pass that writes a row's
    times writes as its scheduled_hours (BUGSGEN-6); the wall clock when the
    row has no readable date."""
    h = real_hours(row, tz)
    return hours_text(h if h or span(row) is None else wall_hours(row))


def retime(row, start_m=None, end_m=None, tz=None) -> dict:
    """`row` with its start and/or end set to the given minutes (any day's —
    1500 is 1:00am) and its scheduled_hours rewritten from the real span.
    The row is changed in place and returned."""
    if start_m is not None:
        row["shift_start"] = fmt_minutes(start_m)
    if end_m is not None:
        row["shift_end"] = fmt_minutes(end_m)
    row["scheduled_hours"] = row_hours_text(row, tz)
    return row


# ── a day's hours ───────────────────────────────────────────────────────────

def close_on_timeline(close_m, open_m=None):
    """A day's close as minutes past that day's midnight (past 1440 when it
    is after midnight), read relative to the day's open: at or before the
    open is the next morning (a 6am-6am diner, a 4pm-6am bar); with no open
    on file, before SMALL_HOURS_BEFORE is the small hours. None when the
    close is None (BUGSGEN-22)."""
    if close_m is None:
        return None
    if open_m is not None:
        return close_m + DAY if close_m <= open_m else close_m
    return close_m + DAY if close_m < SMALL_HOURS_BEFORE else close_m


def day_hours(open_value, close_value):
    """(open, close) minutes on the day's timeline from the stored values;
    either None when not on file or unreadable."""
    o = parse_minutes(open_value)
    return o, close_on_timeline(parse_minutes(close_value), o)


def business_minute(clock_m) -> int:
    """A clock minute (0-1439) on the business day's timeline: before the
    business day's first hour it is the small hours of the night, past 1440
    — for a sales curve keyed by clock hour, whose 1am sales are the night's
    tail, not the start of the day (BUGSGEN-19)."""
    m = int(clock_m) % DAY
    return m + DAY if m < BUSINESS_DAY_START_HOUR * 60 else m


def business_hour(clock_h) -> int:
    """A clock hour (0-23) on the business day's timeline (0-4 -> 24-28)."""
    return business_minute(int(clock_h) * 60) // 60


# ── dayparts ────────────────────────────────────────────────────────────────

_SPLIT = contextvars.ContextVar("schedule_daypart_split", default=None)


def daypart_split() -> int:
    """The daypart boundary in force: the restaurant's (daypart_scope), else
    3:00pm."""
    v = _SPLIT.get()
    return DAYPART_SPLIT if v is None else v


def split_minutes(value):
    """A restaurant's daypart boundary (restaurants.daypart_boundary, a clock
    time) as minutes, or None when unset, unreadable or outside
    DAYPART_SPLIT_MIN-DAYPART_SPLIT_MAX."""
    m = parse_minutes(value) if not isinstance(value, int) else value
    if m is None or not DAYPART_SPLIT_MIN <= m <= DAYPART_SPLIT_MAX:
        return None
    return m


@contextmanager
def daypart_scope(split=None):
    """Inside this block every daypart question uses `split` (minutes, or a
    clock time as restaurants.daypart_boundary stores it) as the boundary;
    an unset or unreadable one keeps 3:00pm. A context variable: it holds for
    this thread's (and this context's) work only."""
    token = _SPLIT.set(split_minutes(split) if split is not None else None)
    try:
        yield daypart_split()
    finally:
        _SPLIT.reset(token)


def restaurant_split(restaurant):
    """The boundary a restaurant set (its daypart_boundary), or None."""
    return split_minutes(getattr(restaurant, "daypart_boundary", None)) if restaurant is not None else None


# The restaurant whose boundary is in force (restaurant_scope), so a learner
# entered from inside the generation's own scope, or twice over, reads the
# restaurant once (schedule re-audit 10/11/26 ARCH-11).
_SCOPE_RID = contextvars.ContextVar("schedule_daypart_rid", default=None)


def split_for(restaurant_id, db_path=None):
    """restaurants.daypart_boundary of one restaurant as minutes, or None
    (3:00pm). The one read of it the learners and the generation share; the
    lazy models import is this L0 module's single deliberate upward call
    (ARCHITECTURE_MANIFEST §3: function-scope only). Never raises."""
    if not restaurant_id:
        return None
    try:
        import models as _models
        if db_path and db_path != _models.DB_PATH:
            r = _models.get_restaurant(restaurant_id, db_path)
        else:
            r = _models.get_restaurant(restaurant_id)
        return restaurant_split(r)
    except Exception:
        return None


@contextmanager
def restaurant_scope(restaurant_id, db_path=None):
    """daypart_scope for one restaurant's own boundary (schedule re-audit
    10/11/26 ARCH-11): the nightly learners — outcomes, the memory, the
    standing patterns, the edit learners, the attendance and the people
    passes — used to read every daypart on the 3pm default while the
    generation read the restaurant's, so a 2:30pm start was filed under
    "morning" by the learners and asked for as "night" by the generation.
    Entered by every per-restaurant job and by the learners' own entry
    points (`scoped`); a no-op inside the same restaurant's scope."""
    if restaurant_id and _SCOPE_RID.get() == restaurant_id:
        yield daypart_split()
        return
    split = split_for(restaurant_id, db_path=db_path)
    token_rid = _SCOPE_RID.set(restaurant_id or None)
    try:
        with daypart_scope(split) as cut:
            yield cut
    finally:
        _SCOPE_RID.reset(token_rid)


def scoped(fn):
    """Decorator: run `fn(restaurant_id, ..., db_path=...)` inside
    restaurant_scope of its restaurant — the first positional argument, or
    the `restaurant_id` keyword; `db_path` read from the keyword when given.
    A learner called from any job, route or test files its dayparts on the
    restaurant's own boundary (ARCH-11)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        rid = kwargs.get("restaurant_id", args[0] if args else None)
        if not isinstance(rid, int) or isinstance(rid, bool):
            return fn(*args, **kwargs)
        with restaurant_scope(rid, db_path=kwargs.get("db_path")):
            return fn(*args, **kwargs)
    return wrapper


def daypart_at(minute, split=None) -> str:
    """The daypart a moment on the business timeline falls in: the small
    hours (1440 on) are the night's, before the boundary the morning's."""
    if minute is None:
        return "unknown"
    cut = daypart_split() if split is None else split
    return "night" if minute >= DAY or minute >= cut else "morning"


def start_daypart(start, split=None) -> str:
    """The daypart a start time alone falls in — for a time with no shift
    around it (a rule's time, a punch), and the fallback of a shift that
    covers neither core window. A start in the small hours is the night it
    belongs to; from 4am it is the morning (NIGHT_TAIL_BEFORE). "unknown"
    when unreadable."""
    s = parse_minutes(start)
    if s is None:
        return "unknown"
    return daypart_at(s + night_offset(s), split)


def _core_overlaps(sp):
    """[(minutes, daypart)] of a placed span inside each core window — the
    window on the business date and, for a span past midnight, not again."""
    s, e = sp
    out = []
    for part in DAYPARTS:
        lo, hi = CORE_WINDOWS[part]
        out.append((max(0, min(e, hi) - max(s, lo)), part, hi - lo))
    return out


def span_dayparts(sp, split=None) -> list:
    """present_dayparts for a span already placed on the business timeline
    ((start, end) minutes — a person's day merged from several rows, a gap):
    each core window it covers for an hour, the most first; else the daypart
    its start falls in."""
    if sp is None:
        return ["unknown"]
    primary = daypart_at(sp[0], split)
    covered = [(o, part == primary, part) for o, part, width in _core_overlaps(sp)
               if o >= min(PRESENCE_MIN_OVERLAP, width)]
    if not covered:
        return [primary]
    covered.sort(key=lambda t: (-t[0], not t[1]))
    return [p for _o, _pr, p in covered]


def present_dayparts(row, split=None) -> list:
    """THE daypart rule: the dayparts `row` is on the floor for — each whose
    core window it covers for at least PRESENCE_MIN_OVERLAP minutes (or all
    of a window shorter than that), the one it covers most first (its own
    daypart, where its hours and week assignment count). A shift covering
    neither core window (an early prep, a 3-5pm changeover, a porter) is its
    start's daypart. ["unknown"] when its times cannot be read."""
    primary = start_daypart((row or {}).get("shift_start"), split)
    sp = span(row)
    if sp is None or primary == "unknown":
        return [primary]
    return span_dayparts(sp, split)


def primary_daypart(row, split=None) -> str:
    """The row's own daypart: the core window it covers most, else its
    start's (present_dayparts' first)."""
    return present_dayparts(row, split)[0]


def touched_dayparts(row, split=None) -> list:
    """The dayparts `row` reaches into at all — each core window it overlaps
    by any minute, its start's daypart first — for a block on half a day
    (part-day time off, morning-only or night-only availability), where one
    minute into it is already into it. A row touching neither core window is
    its start's daypart."""
    primary = start_daypart((row or {}).get("shift_start"), split)
    sp = span(row)
    if sp is None or primary == "unknown":
        return [primary]
    hit = [part for o, part, _w in _core_overlaps(sp) if o > 0]
    if not hit:
        return [primary]
    return sorted(hit, key=lambda p: p != primary)


def end_daypart(row, split=None) -> str:
    """The daypart the moment a row ENDS falls in — what a close is about (a
    10am-11pm double closed the night): the boundary rule read at the end,
    where a shift ending ON the boundary (a lunch out at 3pm) is still the
    morning's (schedule_memory's closes). "unknown" when unreadable."""
    e = end_of(row)
    if e is None:
        return "unknown"
    cut = daypart_split() if split is None else split
    return "night" if e > cut else "morning"


def _week_monday(today, week_start=None, start_day=0):
    """The first day of the week to schedule. A schedule week is the
    restaurant's own week — `start_day` is restaurants.week_start_day
    (0 = Monday, the default; Simple EJ's runs Wednesday to Tuesday, owner
    10/10/26), the same week its payroll and overtime are counted in: the
    owner's choice when given (any date in the wanted week works), else the
    next one after today. The name is kept from when every week was a
    Monday's (callers across the app import it)."""
    from datetime import datetime as _d, timedelta as _t
    sd = int(start_day or 0) % 7
    if week_start:
        try:
            d = _d.strptime(str(week_start)[:10], "%Y-%m-%d")
            return d - _t(days=(d.weekday() - sd) % 7)
        except ValueError:
            pass
    days_ahead = (sd - today.weekday()) % 7 or 7
    return today + _t(days=days_ahead)


def week_dates_from(first) -> list:
    """The seven ISO dates of the week starting `first` (a date or datetime)."""
    from datetime import timedelta as _t
    return [(first + _t(days=i)).strftime("%Y-%m-%d") for i in range(7)]


def week_day_names(dates) -> list:
    """The weekday name of each ISO date, in the dates' own order — a week
    that starts Wednesday reads Wednesday first (never a fixed Monday list
    beside dates that start elsewhere)."""
    return [_weekday_name(d) for d in dates or []]


def _date_of(iso):
    from datetime import date as _date
    return _date.fromisoformat(str(iso)[:10])


def _weekday_name(iso) -> str:
    try:
        return _date_of(iso).strftime("%A")
    except (TypeError, ValueError):
        return ""


def _week_key(date_str: str, week_start_day: int = 0) -> str:
    """The start date of the payroll week `date_str` falls in."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    offset = (d.weekday() - int(week_start_day or 0)) % 7
    return (d - timedelta(days=offset)).strftime("%Y-%m-%d")


def week_hours_split(row: dict, week_start_day: int = 0, hours: float = None) -> dict:
    """{payroll week start: hours} for one row, by the workweek the hours
    were WORKED in (FLSA): a shift dated the last night of a payroll week
    that runs past midnight puts its after-midnight hours in the next week —
    a Tuesday 10pm-2am row in a Wednesday-start week is 2h Tuesday's week, 2h
    Wednesday's (sched re-audit 10/11/26 BUGSGEN-18); a small-hours row
    (12:30am-4am dated Tuesday) is all the next week's. `hours` defaults to
    the row's scheduled_hours (its real hours), shared in proportion to the
    clock minutes either side of midnight. {} when the date is unreadable."""
    d = str((row or {}).get("date") or "")[:10]
    try:
        b0 = _week_key(d, week_start_day)
    except (TypeError, ValueError):
        return {}
    if hours is None:
        try:
            hours = float((row or {}).get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            hours = 0.0
    h = float(hours or 0.0)
    sp = span(row)
    if not h or sp is None or sp[1] <= DAY:
        return {b0: h}
    nxt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    b1 = _week_key(nxt, week_start_day)
    if b1 == b0:
        return {b0: h}
    after = sp[1] - max(sp[0], DAY)
    h1 = round(h * after / float(sp[1] - sp[0]), 2)
    out = {b0: round(h - h1, 2), b1: h1}
    return {b: v for b, v in out.items() if v}


OVERTIME_THRESHOLD_HOURS = 40.0


OVERTIME_MULTIPLIER = 1.5


def bounded_span(row):
    """(start, end) minutes on the row's business date's timeline (span, the
    one placement), or None when unreadable or longer than a day could be
    (over 16 hours) — the manager plan's and the standing positions' read."""
    sp = span(row)
    return sp if sp is not None and 0 < sp[1] - sp[0] <= 16 * 60 else None


def median(values):
    """The median of the values that are not None (the middle two averaged),
    or None when there are none."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def weekday_of(iso) -> str:
    """The weekday name of a "YYYY-MM-DD" date (strptime's reading), "" when
    it cannot be read."""
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return ""


def overlap_minutes(a, b) -> int:
    """Minutes two (start, end) spans share; 0 when they do not meet."""
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def window_allows(lo, hi, start_m, end_m) -> tuple:
    """(ok, which) for a shift from start_m to end_m (minutes past
    midnight) against a window lo..hi (either may be None). A window whose
    latest is before its earliest, or a bare latest in the small hours,
    runs past midnight; so does a shift whose end is not after its start
    (SCHED-13). `which` is 'early' or 'late' when refused. Pure — the swap
    index in shift_quality applies the same rule."""
    if start_m is None or end_m is None:
        return True, ""
    # A start in the small hours is that night's (E-32): a 12:30am porter is
    # after a "from 6pm" window opens, not eighteen hours before it, and the
    # window's own small-hours bounds are read in the same night.
    night = night_offset(start_m)
    if night:
        lo = None if lo is None else (lo + 24 * 60 if lo < SMALL_HOURS_BEFORE else lo)
        if hi is not None and (hi < SMALL_HOURS_BEFORE or (lo is not None and hi < lo)):
            hi = hi + 24 * 60
    elif hi is not None and ((lo is not None and hi < lo) or (lo is None and hi < SMALL_HOURS_BEFORE)):
        hi = hi + 24 * 60
    end = end_m if end_m > start_m else end_m + 24 * 60
    start_m, end = start_m + night, end + night
    if lo is not None and start_m < lo:
        return False, "early"
    if hi is not None and end > hi:
        return False, "late"
    return True, ""


# A holiday the calendar only approximates (its date moves with a league's
# calendar — demand.APPROXIMATE_HOLIDAYS) never takes a date's name from a
# holiday that falls on its own date every year.
_APPROXIMATE = ("Super Bowl Sunday",)


def holiday_names(year: int) -> dict:
    """{iso date: [names]} — every dining holiday on each date of `year`
    (the ones marketing.get_upcoming_holidays already knows), the date's own
    fixed holiday first, then the floating ones, then the approximate.

    Two can fall on one night: Super Bowl Sunday on 2/14/27 is Valentine's
    Day, and the one-name calendar let whichever was written last overwrite
    the other (schedule audit 10/3/26 E-8)."""
    from datetime import date as _date
    fixed = {(1, 1): "New Year's Day", (2, 14): "Valentine's Day", (3, 17): "St. Patrick's Day",
             (5, 5): "Cinco de Mayo", (7, 4): "Fourth of July", (10, 31): "Halloween",
             (11, 11): "Veterans Day", (12, 24): "Christmas Eve", (12, 25): "Christmas Day",
             (12, 31): "New Year's Eve"}
    out = {}

    def add(d, name):
        names = out.setdefault(d.isoformat() if hasattr(d, "isoformat") else d, [])
        if name not in names:
            names.append(name)
    for (m, d), n in fixed.items():
        add(_date(year, m, d), n)

    def nth_weekday(month, weekday, n):
        first = _date(year, month, 1)
        off = (weekday - first.weekday()) % 7
        return first + timedelta(days=off + 7 * (n - 1))

    def last_weekday(month, weekday):
        nxt = _date(year + (month == 12), (month % 12) + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    add(nth_weekday(5, 6, 2), "Mother's Day")
    add(nth_weekday(6, 6, 3), "Father's Day")
    add(nth_weekday(11, 3, 4), "Thanksgiving")
    add(last_weekday(5, 0), "Memorial Day")
    add(nth_weekday(9, 0, 1), "Labor Day")
    # Easter (Anonymous Gregorian algorithm)
    a, b, c = year % 19, year // 100, year % 100
    d_, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d_ - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    add(_date(year, month, day), "Easter")
    add(nth_weekday(2, 6, 2), "Super Bowl Sunday")
    for names in out.values():
        names.sort(key=lambda n: n in _APPROXIMATE)       # stable: fixed, floating, then approximate
    return out


def _holiday_dates(year: int) -> dict:
    """{iso date: name} — each date's first holiday (holiday_names): the
    fixed-date one wherever two fall on one night."""
    return {d: names[0] for d, names in holiday_names(year).items()}


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y", "%b %d %Y")


def _iso_date(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw[:len(raw)], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw[:19]).strftime("%Y-%m-%d")
    except ValueError:
        return None
