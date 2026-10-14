"""
school_calendar.py — when school is in session, for the minors' hour limits.

A minor's hours depend on whether school is in session: the federal rule for
14-15 year olds (29 CFR 570.35) holds them to 3 hours on a school day and 18
in a school week, and most state tables split the same way. The product had
no school calendar, so "a school day" was a weekday outside June 1 through
Labor Day — and a district that starts in mid-August (most of them, the
Chicago suburbs included) had its first three weeks of term read as summer:
a 14-year-old could be drafted 8 hours on a school day and 40 in a school
week with no hard flag (schedule audit 10/10/26 LEARNING-18).

Now each restaurant can keep its district's calendar
(restaurants.school_calendar_json):

    {"district": "D303", "years": [{"start": "2026-08-19", "end": "2027-06-04",
                                     "breaks": [{"start": "2026-12-21", "end": "2027-01-01",
                                                 "label": "Winter break"}]}],
     "updated_at": "...", "updated_by": "..."}

A weekday inside a year's term and outside its breaks is a school day; a
weekday outside every term on file is not. A date no year on file covers
falls back to the SAFE default: every weekday is a school day except June 1
through July 31 — so August's weekdays are school days until the owner says
otherwise. The latest end (7pm in term, 9pm from June 1 to Labor Day for
14-15s) is date-based in the federal rule and stays date-based
(schedule_rules.is_summer); only the hours limits read this calendar.

Pure apart from `calendar_of`/`save_calendar`, which read and write the
restaurant row.
"""
import json
from datetime import date, datetime, timedelta

# The default's summer: June 1 through July 31. August is term by default.
DEFAULT_SUMMER = ((6, 1), (7, 31))
# The hours school is in session on a school day, when the calendar names
# none: the federal rule keeps 14-15 year olds out of work DURING school
# hours (29 CFR 570.35(a)(1)), and a school day's 7:30am-3:30pm covers the
# usual US bell schedule (sched re-audit 10/11/26 STRESS-5: a 14-year-old on
# 11am-2pm on a school Tuesday raised nothing). A district's own bell times
# go in the calendar ({"hours": {"start": "8:00am", "end": "3:10pm"}}).
DEFAULT_SCHOOL_HOURS = ("7:30am", "3:30pm")
# A year entry may be at most this long (a typo of a year is refused).
MAX_TERM_DAYS = 330
MAX_YEARS = 6
MAX_BREAKS = 24


def _d(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def clean_calendar(data) -> dict:
    """The calendar as stored: years sorted, each {start, end, breaks}, only
    readable dates, a term no longer than MAX_TERM_DAYS and its breaks
    inside it. Raises ValueError with a sentence for the owner."""
    if not isinstance(data, dict):
        raise ValueError("The school calendar must be a list of school years.")
    years = data.get("years") or []
    if not isinstance(years, list):
        raise ValueError("The school calendar must be a list of school years.")
    out = []
    for y in years[:MAX_YEARS]:
        if not isinstance(y, dict):
            continue
        s, e = _d(y.get("start")), _d(y.get("end"))
        if not s or not e:
            raise ValueError("Each school year needs a first and a last day of school.")
        if e <= s:
            raise ValueError("A school year's last day must come after its first day.")
        if (e - s).days > MAX_TERM_DAYS:
            raise ValueError("A school year can't run longer than about eleven months — check the dates.")
        breaks = []
        for b in (y.get("breaks") or [])[:MAX_BREAKS]:
            if not isinstance(b, dict):
                continue
            bs, be = _d(b.get("start")), _d(b.get("end"))
            if not bs or not be or be < bs:
                raise ValueError("Each break needs a first and a last day, the last on or after the first.")
            if bs < s or be > e:
                raise ValueError("A break has to fall inside its school year.")
            breaks.append({"start": bs.isoformat(), "end": be.isoformat(),
                           "label": str(b.get("label") or "").strip()[:60]})
        breaks.sort(key=lambda b: b["start"])
        out.append({"start": s.isoformat(), "end": e.isoformat(), "breaks": breaks})
    out.sort(key=lambda y: y["start"])
    for a, b in zip(out, out[1:]):
        if b["start"] <= a["end"]:
            raise ValueError("Two school years overlap — check their dates.")
    clean = {"district": str(data.get("district") or "").strip()[:80], "years": out}
    hrs = data.get("hours")
    if hrs:
        if not isinstance(hrs, dict):
            raise ValueError("School hours need a start and an end time.")
        import schedule_time as _st
        s_m, e_m = _st.parse_minutes(hrs.get("start")), _st.parse_minutes(hrs.get("end"))
        if s_m is None or e_m is None:
            raise ValueError("School hours need a start and an end time, like 8:00am and 3:10pm.")
        if not (5 * 60 <= s_m < e_m <= 18 * 60):
            raise ValueError("School hours must start after 5am and end after they start, by 6pm.")
        clean["hours"] = {"start": _st.fmt_minutes(s_m), "end": _st.fmt_minutes(e_m)}
    return clean


def school_hours(cal) -> tuple:
    """(start, end) minutes school is in session on a school day: the
    calendar's own bell times where the owner entered them, else
    DEFAULT_SCHOOL_HOURS — the window a 14-15 year old may not work in on a
    school day (29 CFR 570.35(a)(1); sched re-audit 10/11/26 STRESS-5)."""
    import schedule_time as _st
    hrs = (cal or {}).get("hours") or {}
    s_m, e_m = _st.parse_minutes(hrs.get("start")), _st.parse_minutes(hrs.get("end"))
    if s_m is None or e_m is None or e_m <= s_m:
        s_m, e_m = (_st.parse_minutes(x) for x in DEFAULT_SCHOOL_HOURS)
    return s_m, e_m


def calendar_of(restaurant) -> dict:
    """The restaurant's stored calendar ({"years": []} when none or
    unreadable). `restaurant` is a Restaurant or anything with
    school_calendar_json."""
    raw = getattr(restaurant, "school_calendar_json", None) if restaurant is not None else None
    if not raw:
        return {"district": "", "years": []}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return clean_calendar(data)
    except (ValueError, TypeError):
        return {"district": "", "years": []}


def save_calendar(restaurant_id, data, by=None, db_path=None) -> dict:
    """Validate and store a calendar (clean_calendar); returns it."""
    from models import update_restaurant, DB_PATH
    clean = clean_calendar(data)
    clean["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if by:
        clean["updated_by"] = str(by)[:120]
    update_restaurant(restaurant_id, {"school_calendar_json": json.dumps(clean) if (clean["years"] or clean.get("hours")) else None},
                      db_path=db_path or DB_PATH)
    return clean


def _year_for(cal, d):
    for y in (cal or {}).get("years") or []:
        s, e = _d(y.get("start")), _d(y.get("end"))
        if s and e and s <= d <= e:
            return y
    return None


# How far a year on file speaks for the summer around it: the weeks before
# its first day (a term entered as starting 8/19 says 8/12 is still summer)
# and the weeks after its last (early June to the end of July). Past that,
# nothing on file says when the next term starts — the default holds.
KNOWN_BEFORE_START_DAYS = 120
KNOWN_AFTER_END_DAYS = 56


def covered(cal, date_str) -> bool:
    """Whether a calendar year on file says anything about this date: inside
    a term, between two terms on file, in the weeks before a term's first
    day (KNOWN_BEFORE_START_DAYS) or after its last (KNOWN_AFTER_END_DAYS).
    A date further from everything entered is not known."""
    d = _d(date_str)
    years = (cal or {}).get("years") or []
    if not d or not years:
        return False
    firsts = [_d(y["start"]) for y in years]
    lasts = [_d(y["end"]) for y in years]
    return (min(firsts) - timedelta(days=KNOWN_BEFORE_START_DAYS)) <= d <= \
        (max(lasts) + timedelta(days=KNOWN_AFTER_END_DAYS))


def _default_school_day(d) -> bool:
    (sm, sd), (em, ed) = DEFAULT_SUMMER
    in_summer = (sm, sd) <= (d.month, d.day) <= (em, ed)
    return d.weekday() < 5 and not in_summer


def is_school_day(date_str, cal=None) -> bool:
    """Whether minors' school-day hour limits hold on this date: a weekday
    in a term on file and outside its breaks; with no term on file for the
    date, a weekday outside June 1 to July 31 (the safe default — August
    weekdays count as school days until a calendar says otherwise)."""
    d = _d(date_str)
    if not d:
        return False
    if d.weekday() >= 5:
        return False
    if covered(cal, d):
        y = _year_for(cal, d)
        if not y:
            return False                    # between two terms on file: summer
        for b in y.get("breaks") or []:
            if _d(b["start"]) <= d <= _d(b["end"]):
                return False
        return True
    return _default_school_day(d)


def is_school_week(date_str, cal=None) -> bool:
    """Whether the Monday-to-Sunday week holding this date has a school day
    in it (the federal 'school week')."""
    d = _d(date_str)
    if not d:
        return False
    mon = d - timedelta(days=d.weekday())
    return any(is_school_day(mon + timedelta(days=i), cal) for i in range(5))


def basis(cal, date_str) -> str:
    """Where a date's school status came from, in the owner's words."""
    if covered(cal, date_str):
        district = (cal or {}).get("district")
        return f"your school calendar{(' (' + district + ')') if district else ''}"
    return "the default: every weekday is a school day except June 1 to July 31"


def needs_prompt(cal, today, has_minors: bool) -> dict:
    """The yearly ask: {"ask": True, "school_year": "2026-27", "text"} when
    the roster has a minor and no year on file covers the coming (or
    current) school year — from July 1 the next one, else the current one.
    {"ask": False} otherwise."""
    if not has_minors:
        return {"ask": False}
    t = _d(today) or date.today()
    start_year = t.year if t.month >= 7 else t.year - 1
    probe = date(start_year, 9, 15)          # a date every school year includes
    if covered(cal, probe) and _year_for(cal, probe):
        return {"ask": False}
    label = f"{start_year}-{str(start_year + 1)[2:]}"
    return {"ask": True, "school_year": label,
            "text": (f"You have minors on the roster and no school calendar for {label}. Until you add your "
                     f"district's first and last days and breaks, every weekday except June 1 to July 31 is held "
                     f"to the school-day limits.")}


def minors_by_season(restaurant_id, names, cal=None, days=365, db_path=None) -> dict:
    """{name: seasonal_hours} for the minors on the roster, from the last
    year of shifts worked (shift_facts, actual hours where punched) — how
    much each actually works in school weeks and out of them: the revealed
    availability the school calendar explains (LEARNING-18). {} on no data."""
    from models import get_conn, DB_PATH
    keys = {" ".join(str(n).lower().split()): n for n in names or []}
    if not keys:
        return {}
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            f"SELECT employee_key, business_date, COALESCE(actual_hours, scheduled_hours, 0) AS h FROM shift_facts "
            f"WHERE restaurant_id=? AND business_date>=? AND employee_key IN ({','.join('?' * len(keys))})",
            (restaurant_id, since, *keys)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    weeks = {}
    for r in rows:
        d = _d(r["business_date"])
        if not d:
            continue
        wk = (d - timedelta(days=d.weekday())).isoformat()
        per = weeks.setdefault(r["employee_key"], {})
        per[wk] = per.get(wk, 0.0) + float(r["h"] or 0)
    return {keys[k]: seasonal_hours(v, cal) for k, v in weeks.items() if k in keys}


def seasonal_hours(rows_by_week: dict, cal=None) -> dict:
    """{"term": avg hours a week in school weeks, "break": avg in other
    weeks, "term_weeks": n, "break_weeks": n} from {week monday iso: hours}
    — a minor's or student's revealed availability by season, read from the
    weeks they actually worked."""
    term, brk = [], []
    for wk, h in (rows_by_week or {}).items():
        (term if is_school_week(wk, cal) else brk).append(float(h or 0))
    return {"term": round(sum(term) / len(term), 1) if term else None,
            "break": round(sum(brk) / len(brk), 1) if brk else None,
            "term_weeks": len(term), "break_weeks": len(brk)}
