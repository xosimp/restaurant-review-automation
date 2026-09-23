"""
schedule_rules.py — the rules a schedule has to obey, held as facts the code
can check rather than sentences the model is asked to remember.

Two halves. The owner's settings: rest between shifts, shift length, minors,
days off, an hours ceiling, and per-role per-daypart staffing floors
(compliance_json and role_floors_json on the restaurant). And the
Constraints object built once per generation from every source the pipeline
used to consult separately — approved and pending time off, weekday and
daypart availability, per-person hours limits, hours already published in
the same payroll week (here and at sibling sites), who is still on the
roster — with one legality check (`can_work`) and one violation sweep the
backstops, the swap search, the review panel and the publish gate all share.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from models import get_conn, DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

DEFAULTS = {
    "min_rest_hours": 10.0,          # between one shift's end and the next start (a "clopen" is under this)
    "max_shift_hours": 12.0,
    "daily_ot_hours": None,          # e.g. 8 where daily overtime applies; None = not tracked
    "meal_break_after_hours": None,  # e.g. 5 or 6; None = not tracked
    "minor_latest_end": "10:00pm",
    "minor_max_daily_hours": 8.0,
    "min_consecutive_days_off": 2,
    "part_time_days_off": 3,
    "weekly_hours_ceiling": 40.0,
    "max_consecutive_days": 6,       # a longer run is a hard breach (the tail of the published week counts)
    "notice_days": None,             # predictive-scheduling notice; informational
}
_BOUNDS = {"min_rest_hours": (0, 24), "max_shift_hours": (4, 24), "daily_ot_hours": (4, 24),
           "meal_break_after_hours": (2, 12), "minor_max_daily_hours": (1, 12),
           "min_consecutive_days_off": (0, 4), "part_time_days_off": (0, 5),
           "weekly_hours_ceiling": (10, 80), "max_consecutive_days": (2, 14), "notice_days": (0, 30)}

# Reasons that mean the person is not really on that shift, so the row must
# not count as coverage. Everything else in HARD is a cost or a rule breach
# with a real person still on the floor.
NO_SHOW = frozenset({"off_roster", "inactive", "outside_week", "double_booked", "overlap",
                     "approved_time_off", "unavailable_day", "unavailable_daypart", "elsewhere"})
NO_SHOW = NO_SHOW | frozenset({"outside_window", "missing_cert"})
HARD = NO_SHOW | frozenset({"over_max_hours", "shift_too_long", "rest_gap", "minor_late", "minor_hours", "long_run",
                            "no_manager_on_duty"})
SOFT = frozenset({"days_off", "pending_time_off", "daily_ot", "meal_break", "under_min_hours", "over_section_cap",
                  "before_arrival", "ends_before_role_close", "manager_rule_unusable"})

LABELS = {
    "off_roster": "not on the staff list", "inactive": "no longer on the roster",
    "outside_week": "date is outside next week", "double_booked": "double-booked at the same start time",
    "overlap": "two shifts overlap", "approved_time_off": "on approved time off",
    "unavailable_day": "marked unavailable that day", "unavailable_daypart": "not available for that daypart",
    "elsewhere": "already scheduled at another location", "over_max_hours": "over their hours ceiling for the payroll week",
    "shift_too_long": "shift longer than the maximum", "rest_gap": "not enough rest since their previous shift",
    "minor_late": "a minor working past the latest allowed end", "minor_hours": "a minor over the daily hours limit",
    "days_off": "fewer consecutive days off than the rule", "pending_time_off": "has a time-off request waiting on you",
    "daily_ot": "daily overtime", "meal_break": "long enough to need a meal break",
    "under_min_hours": "under their minimum hours", "over_section_cap": "more servers than sections",
    "long_run": "too many days in a row",
    "outside_window": "outside the hours they can work that day",
    "missing_cert": "missing a certification the role needs",
    "no_manager_on_duty": "no manager or keyholder on the shift",
    "before_arrival": "starts before that role's arrival time",
    "ends_before_role_close": "ends before that role is meant to stay until",
    "manager_rule_unusable": "manager on duty is on, but nobody is a closer or keyholder",
}


# ── settings ───────────────────────────────────────────────────────────────

def _num(v, lo, hi):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return max(lo, min(hi, n))


def compliance(restaurant) -> dict:
    """The rules in force for one restaurant: DEFAULTS with the owner's
    overrides. `restaurant` is a Restaurant or an id."""
    if not hasattr(restaurant, "compliance_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    out = dict(DEFAULTS)
    out["manager_on_duty"] = False
    raw = getattr(restaurant, "compliance_json", None) if restaurant else None
    if not raw:
        return _with_pack(out, restaurant, {})
    try:
        data = json.loads(raw) or {}
    except Exception:
        return _with_pack(out, restaurant, {})
    for k, (lo, hi) in _BOUNDS.items():
        if k in data:
            out[k] = _num(data[k], lo, hi) if data[k] not in (None, "", False) else None
    if isinstance(data.get("minor_latest_end"), str) and data["minor_latest_end"].strip():
        out["minor_latest_end"] = data["minor_latest_end"].strip()[:10]
    for k in ("min_consecutive_days_off", "part_time_days_off", "max_consecutive_days"):
        if out.get(k) is not None:
            out[k] = int(out[k])
    out["manager_on_duty"] = bool(data.get("manager_on_duty"))
    return _with_pack(out, restaurant, data)


def _with_pack(out: dict, restaurant, owner_set: dict) -> dict:
    """A jurisdiction pack sits UNDER the owner's own values."""
    code = (getattr(restaurant, "jurisdiction", None) or "").strip() if restaurant else ""
    if not code:
        return out
    try:
        import compliance_packs
        return compliance_packs.apply(out, code, owner_set or {})
    except Exception:
        return out


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_CLOSURE_KEYS = ("closed_weekdays", "closed_dates")


def closures(restaurant) -> dict:
    """{"closed_weekdays": ["Monday"], "closed_dates": ["2026-12-25"]} — the
    days the owner says the restaurant does not trade. Stored beside the
    compliance rules; a restaurant closed Mondays could never generate a
    week before this existed, because every date had to carry shifts."""
    if not hasattr(restaurant, "compliance_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    try:
        data = json.loads(getattr(restaurant, "compliance_json", None) or "{}") or {}
    except Exception:
        data = {}
    days = [d for d in (data.get("closed_weekdays") or []) if d in WEEKDAYS]
    dates = sorted({str(d)[:10] for d in (data.get("closed_dates") or []) if _iso(str(d)[:10])})
    return {"closed_weekdays": days, "closed_dates": dates}


def _iso(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def save_closures(restaurant_id, closed_weekdays=None, closed_dates=None, db_path=DB_PATH) -> dict:
    from models import get_restaurant, update_restaurant
    r = get_restaurant(restaurant_id, db_path)
    try:
        data = json.loads(getattr(r, "compliance_json", None) or "{}") or {}
    except Exception:
        data = {}
    if closed_weekdays is not None:
        wanted = {str(d).strip().capitalize() for d in closed_weekdays or []}
        data["closed_weekdays"] = [d for d in WEEKDAYS if d in wanted]
        if len(data["closed_weekdays"]) == 7:
            raise ValueError("A restaurant closed every day of the week has nothing to schedule.")
    if closed_dates is not None:
        data["closed_dates"] = sorted({str(d).strip()[:10] for d in closed_dates or [] if _iso(str(d).strip()[:10])})[-120:]
    update_restaurant(restaurant_id, {"compliance_json": json.dumps(data) if data else None}, db_path=db_path)
    return closures(get_restaurant(restaurant_id, db_path))


def closed_in(restaurant, week_dates) -> set:
    cl = closures(restaurant)
    out = set()
    for d in week_dates or []:
        try:
            wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (TypeError, ValueError):
            continue
        if wd in cl["closed_weekdays"] or d in cl["closed_dates"]:
            out.add(d)
    return out


def save_compliance(restaurant_id, data: dict, db_path=DB_PATH) -> dict:
    from models import update_restaurant, get_restaurant as _gr
    clean = {}
    # The closures live in the same JSON; saving the rules must keep them.
    try:
        _prev = json.loads(getattr(_gr(restaurant_id, db_path), "compliance_json", None) or "{}") or {}
    except Exception:
        _prev = {}
    for k in _CLOSURE_KEYS:
        if _prev.get(k):
            clean[k] = _prev[k]
    for k, (lo, hi) in _BOUNDS.items():
        if k in (data or {}):
            v = data[k]
            clean[k] = None if v in (None, "", False) else _num(v, lo, hi)
    if isinstance((data or {}).get("minor_latest_end"), str):
        clean["minor_latest_end"] = data["minor_latest_end"].strip()[:10] or DEFAULTS["minor_latest_end"]
    if "manager_on_duty" in (data or {}):
        clean["manager_on_duty"] = bool(data["manager_on_duty"])
    update_restaurant(restaurant_id, {"compliance_json": json.dumps(clean) if clean else None}, db_path=db_path)
    from models import get_restaurant
    return compliance(get_restaurant(restaurant_id, db_path))


def role_floors(restaurant) -> dict:
    """{role: {"morning": n, "night": n, "days": {day: {"morning": n, "night": n}}}}
    as the owner set it, with role names kept as typed."""
    if not hasattr(restaurant, "role_floors_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    raw = getattr(restaurant, "role_floors_json", None) if restaurant else None
    if not raw:
        return {}
    try:
        data = json.loads(raw) or {}
    except Exception:
        return {}
    out = {}
    for role, spec in data.items():
        if not isinstance(spec, dict) or not str(role).strip():
            continue
        entry = {"morning": int(_num(spec.get("morning"), 0, 50) or 0), "night": int(_num(spec.get("night"), 0, 50) or 0), "days": {}}
        for day, dspec in (spec.get("days") or {}).items():
            day = str(day).strip().capitalize()
            if day in DAYS and isinstance(dspec, dict):
                entry["days"][day] = {p: int(_num(dspec.get(p), 0, 50) or 0) for p in ("morning", "night") if dspec.get(p) is not None}
        if entry["morning"] or entry["night"] or entry["days"]:
            out[str(role).strip()] = entry
    return out


def save_role_floors(restaurant_id, data: dict, db_path=DB_PATH) -> dict:
    from models import update_restaurant, get_restaurant
    update_restaurant(restaurant_id, {"role_floors_json": json.dumps(data) if data else None}, db_path=db_path)
    return role_floors(get_restaurant(restaurant_id, db_path))


def floor_for(floors: dict, role: str, day: str, daypart: str) -> int:
    for r, spec in (floors or {}).items():
        if r.strip().lower() == (role or "").strip().lower():
            dspec = (spec.get("days") or {}).get(day) or {}
            if daypart in dspec:
                return int(dspec[daypart])
            return int(spec.get(daypart) or 0)
    return 0


# ── time helpers ───────────────────────────────────────────────────────────

def parse_minutes(t: str):
    """'9:30pm' / '21:30' → minutes past midnight, or None."""
    raw = (t or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            d = datetime.strptime(raw, fmt)
            return d.hour * 60 + d.minute
        except ValueError:
            continue
    return None


def shift_span(row, tz=None) -> tuple:
    """(start_dt, end_dt) as datetimes on the row's date; an end before the
    start crosses midnight. (None, None) when unreadable.

    With `tz` (the restaurant's IANA zone) both ends are the real instants,
    as naive UTC: a close and an open either side of a clock change are an
    hour closer or further apart than the wall clock says, and the rest rule
    is about hours actually off (SCHED-32). Without it, wall-clock time."""
    try:
        base = datetime.strptime(row.get("date", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None, None
    s, e = parse_minutes(row.get("shift_start", "")), parse_minutes(row.get("shift_end", ""))
    if s is None or e is None:
        return None, None
    start = base + timedelta(minutes=s)
    end = base + timedelta(minutes=e if e > s else e + 24 * 60)
    zone = _zone(tz)
    if zone is not None:
        start = start.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
        end = end.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


_ZONES = {}


def _zone(tz):
    if not tz:
        return None
    if tz not in _ZONES:
        try:
            from zoneinfo import ZoneInfo
            _ZONES[tz] = ZoneInfo(str(tz))
        except Exception:
            _ZONES[tz] = None
    return _ZONES[tz]


# A "latest" this early with no "earliest" after it is the small hours of
# the next morning ("until 1:00am"), not a one-in-the-morning curfew.
_OVERNIGHT_LATEST_BEFORE = 6 * 60


def window_allows(lo, hi, start_m, end_m) -> tuple:
    """(ok, which) for a shift from start_m to end_m (minutes past
    midnight) against a window lo..hi (either may be None). A window whose
    latest is before its earliest, or a bare latest in the small hours,
    runs past midnight; so does a shift whose end is not after its start
    (SCHED-13). `which` is 'early' or 'late' when refused. Pure — the swap
    index in shift_quality applies the same rule."""
    if start_m is None or end_m is None:
        return True, ""
    if hi is not None and ((lo is not None and hi < lo) or (lo is None and hi < _OVERNIGHT_LATEST_BEFORE)):
        hi = hi + 24 * 60
    end = end_m if end_m > start_m else end_m + 24 * 60
    if lo is not None and start_m < lo:
        return False, "early"
    if hi is not None and end > hi:
        return False, "late"
    return True, ""


def row_hours(row) -> float:
    try:
        return float(row.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        s, e = shift_span(row)
        return round((e - s).total_seconds() / 3600, 1) if s and e else 0.0


def _fmt_minutes(m: int) -> str:
    h, mm = divmod(int(m), 60)
    suffix = "am" if h < 12 or h == 24 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d}{suffix}"


def daypart_of(shift_start: str) -> str:
    m = parse_minutes(shift_start)
    if m is None:
        return "unknown"
    return "night" if m >= 15 * 60 else "morning"


# ── the constraint set ─────────────────────────────────────────────────────

@dataclass
class Constraints:
    restaurant_id: int
    week_dates: list
    week_days: list
    compliance: dict = field(default_factory=lambda: dict(DEFAULTS))
    week_start_day: int = 0
    roster_names: list = field(default_factory=list)       # display names, active only
    active: set = field(default_factory=set)               # lowercase
    inactive: set = field(default_factory=set)             # lowercase, deactivated
    blocked_dates: dict = field(default_factory=dict)      # {lower: {date: reason}}
    pending_off: dict = field(default_factory=dict)        # {lower: set(dates)}
    unavailable_days: dict = field(default_factory=dict)   # {lower: set(day)}
    daypart_avail: dict = field(default_factory=dict)      # {lower: {day: any|morning|night|off}}
    hours_limits: dict = field(default_factory=dict)       # {lower: (min, max)}
    employment: dict = field(default_factory=dict)         # {lower: 'full'|'part'}
    minors: set = field(default_factory=set)
    base_hours: dict = field(default_factory=dict)         # {lower: {bucket: hours already published}}
    base_rows: dict = field(default_factory=dict)          # {lower: [published rows outside this week]}
    notes: dict = field(default_factory=dict)              # {lower: free-text note}
    time_windows: dict = field(default_factory=dict)       # {lower: {day: (earliest_min, latest_min)}}
    certifications: dict = field(default_factory=dict)     # {lower: set(cert)}
    role_requirements: dict = field(default_factory=dict)  # {role lower: set(cert)}
    keyholders: set = field(default_factory=set)           # lower: can close, or holds a manager/keyholder cert
    preferred: dict = field(default_factory=dict)          # {name: {"preferred_dayparts": [...], "desired_hours": n}}
    foh_roles: set = field(default_factory=lambda: {"server"})
    patio_roles: set = field(default_factory=set)
    arrivals: dict = field(default_factory=dict)           # {role lower: minutes relative to open}
    close_mins: dict = field(default_factory=dict)         # {role lower: minutes after close the role stays until}
    section_cap: int = 0
    role_floors: dict = field(default_factory=dict)
    open_times: dict = field(default_factory=dict)
    close_times: dict = field(default_factory=dict)
    role_buffers: dict = field(default_factory=dict)
    closed_dates: set = field(default_factory=set)         # iso dates the restaurant does not trade this week
    tz: str = ""                                           # IANA zone: rest is measured in real hours (SCHED-32)

    # ── lookups ────────────────────────────────────────────────────────
    def bucket(self, date_str: str) -> str:
        from labor import _week_key
        try:
            return _week_key(date_str, self.week_start_day)
        except (TypeError, ValueError):
            return ""          # a garbled date is flagged elsewhere; it counts toward no payroll week

    def max_hours(self, name: str) -> float:
        lim = self.hours_limits.get((name or "").strip().lower())
        ceiling = float(self.compliance.get("weekly_hours_ceiling") or DEFAULTS["weekly_hours_ceiling"])
        if lim and lim[1]:
            return min(float(lim[1]), ceiling) if ceiling else float(lim[1])
        return ceiling

    def min_hours(self, name: str):
        lim = self.hours_limits.get((name or "").strip().lower())
        return float(lim[0]) if lim and lim[0] else None

    def can_work(self, name: str, date_str: str, daypart: str = None) -> tuple:
        """(ok, reason) — the one legality question every pass asks."""
        key = (name or "").strip().lower()
        if not key:
            return False, "no name"
        if key in self.inactive:
            return False, LABELS["inactive"]
        if self.active and key not in self.active:
            return False, LABELS["off_roster"]
        if self.week_dates and date_str not in self.week_dates:
            return False, LABELS["outside_week"]
        blocked = self.blocked_dates.get(key) or {}
        if date_str in blocked:
            return False, blocked[date_str]
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = ""
        if day and day in (self.unavailable_days.get(key) or set()):
            return False, LABELS["unavailable_day"]
        choice = (self.daypart_avail.get(key) or {}).get(day)
        if choice == "off":
            return False, LABELS["unavailable_day"]
        if daypart and choice in ("morning", "night") and daypart not in ("unknown", choice):
            return False, LABELS["unavailable_daypart"]
        return True, ""

    def window_ok(self, name: str, date_str: str, start: str, end: str) -> tuple:
        """Whether a shift's times sit inside the person's window that day."""
        key = (name or "").strip().lower()
        win = self.time_windows.get(key)
        if not win:
            return True, ""
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            return True, ""
        w = win.get(day)
        if not w:
            return True, ""
        lo, hi = w
        ok, which = window_allows(lo, hi, parse_minutes(start), parse_minutes(end))
        if ok:
            return True, ""
        if which == "early":
            return False, f"{LABELS['outside_window']} (not before {_fmt_minutes(lo)})"
        return False, f"{LABELS['outside_window']} (not after {_fmt_minutes(hi)})"

    def cert_ok(self, name: str, role: str) -> tuple:
        need = self.role_requirements.get((role or "").strip().lower()) or set()
        if not need:
            return True, ""
        have = self.certifications.get((name or "").strip().lower()) or set()
        missing = sorted(need - have)
        if missing:
            return False, f"{LABELS['missing_cert']} ({', '.join(missing)})"
        return True, ""

    def rest_ok(self, name: str, candidate: dict, other_rows: list) -> tuple:
        """Whether `candidate` leaves the configured rest before and after
        this person's other shifts (this week's and the published tail)."""
        need = float(self.compliance.get("min_rest_hours") or 0)
        if need <= 0:
            return True, ""
        s, e = shift_span(candidate, self.tz)
        if not s:
            return True, ""
        key = (name or "").strip().lower()
        for r in list(other_rows) + list(self.base_rows.get(key) or []):
            if (r.get("employee") or "").strip().lower() != key:
                continue
            if r is candidate or (r.get("date") == candidate.get("date") and r.get("shift_start") == candidate.get("shift_start")):
                continue
            rs, re_ = shift_span(r, self.tz)
            if not rs:
                continue
            if rs >= e:
                gap = (rs - e).total_seconds() / 3600
            elif re_ <= s:
                gap = (s - re_).total_seconds() / 3600
            else:
                return False, LABELS["overlap"]
            # The rest rule is the overnight turnaround. Two legs on the same
            # date (lunch then dinner) are a double, which the prompt asks
            # for; only an overlap is wrong there.
            if r.get("date") == candidate.get("date"):
                continue
            if gap < need:
                return False, f"{LABELS['rest_gap']} ({gap:.1f}h, needs {need:g}h)"
        return True, ""


def _load_json(raw, default):
    try:
        return json.loads(raw or "") or default
    except Exception:
        return default


def build_constraints(restaurant_id, week_dates, week_days, restaurant=None, db_path=DB_PATH) -> Constraints:
    """Everything the pipeline needs to know about who can work when,
    gathered once. Every source is optional; a missing one costs a rule,
    never the generation."""
    from models import get_restaurant, get_staff_availability, get_close_times, get_role_close_buffers, get_staff_notes
    restaurant = restaurant or get_restaurant(restaurant_id, db_path)
    c = Constraints(restaurant_id=restaurant_id, week_dates=list(week_dates or []), week_days=list(week_days or []))
    c.compliance = compliance(restaurant)
    c.week_start_day = int(getattr(restaurant, "week_start_day", 0) or 0)
    c.tz = (getattr(restaurant, "timezone", None) or "").strip()
    c.section_cap = int(getattr(restaurant, "section_count", 0) or 0)
    c.role_floors = role_floors(restaurant)
    c.open_times = _load_json(getattr(restaurant, "open_times_json", None), {})
    try:
        c.closed_dates = closed_in(restaurant, c.week_dates)
    except Exception:
        c.closed_dates = set()
    try:
        c.close_times = get_close_times(restaurant_id, db_path) or {}
        c.role_buffers = get_role_close_buffers(restaurant_id, db_path) or {}
    except Exception:
        pass

    # roster + per-person settings
    try:
        import staff_settings as _ss
        for e in _ss.roster(restaurant_id, db_path=db_path, include_inactive=True):
            key = e["name"].strip().lower()
            if e["active"]:
                c.roster_names.append(e["name"])
                c.active.add(key)
            else:
                c.inactive.add(key)
            st = e.get("settings") or {}
            if st.get("min_hours") is not None or st.get("max_hours") is not None:
                c.hours_limits[key] = (st.get("min_hours"), st.get("max_hours"))
            if st.get("employment_type"):
                c.employment[key] = st["employment_type"]
            if st.get("daypart_availability"):
                c.daypart_avail[key] = dict(st["daypart_availability"])
            if st.get("is_minor"):
                c.minors.add(key)
            if st.get("time_windows"):
                win = {}
                for day, w in st["time_windows"].items():
                    lo, hi = parse_minutes((w or {}).get("earliest") or ""), parse_minutes((w or {}).get("latest") or "")
                    if lo is not None or hi is not None:
                        win[day] = (lo, hi)
                if win:
                    c.time_windows[key] = win
            if st.get("certifications"):
                c.certifications[key] = {str(x).strip().lower() for x in st["certifications"] if str(x).strip()}
                if c.certifications[key] & {"manager", "keyholder"}:
                    c.keyholders.add(key)
            if st.get("preferred_dayparts") or st.get("desired_hours"):
                c.preferred[e["name"]] = {"preferred_dayparts": list(st.get("preferred_dayparts") or []),
                                          "desired_hours": st.get("desired_hours")}
    except Exception:
        pass
    try:
        from models import get_leader_flags
        c.keyholders |= {n.strip().lower() for n, v in (get_leader_flags(restaurant_id, db_path) or {}).items() if v}
    except Exception:
        pass
    c.role_requirements = {str(k).strip().lower(): {str(x).strip().lower() for x in (v or []) if str(x).strip()}
                           for k, v in (_load_json(getattr(restaurant, "role_requirements_json", None), {}) or {}).items() if k}
    foh = _load_json(getattr(restaurant, "foh_roles_json", None), [])
    c.foh_roles = {str(x).strip().lower() for x in foh if str(x).strip()} or {"server"}
    c.patio_roles = {str(x).strip().lower() for x in (_load_json(getattr(restaurant, "patio_roles_json", None), []) or []) if str(x).strip()}
    c.arrivals = {}
    for k, v in (_load_json(getattr(restaurant, "role_arrival_json", None), {}) or {}).items():
        try:
            c.arrivals[str(k).strip().lower()] = int(v)
        except (TypeError, ValueError):
            continue
    c.close_mins = {}
    for k, v in (_load_json(getattr(restaurant, "role_close_min_json", None), {}) or {}).items():
        try:
            c.close_mins[str(k).strip().lower()] = int(v)
        except (TypeError, ValueError):
            continue

    # weekday availability + free-text notes
    try:
        for a in get_staff_availability(restaurant_id, db_path) or []:
            key = (a.get("employee_name") or "").strip().lower()
            if not key:
                continue
            c.unavailable_days[key] = set(_load_json(a.get("unavailable_days"), []))
            avail = _load_json(a.get("available_days"), [])
            if avail:
                c.unavailable_days[key] |= {d for d in DAYS if d not in avail}
            if (a.get("notes") or "").strip():
                c.notes[key] = a["notes"].strip()
    except Exception:
        pass
    try:
        for n in get_staff_notes(restaurant_id, db_path) or []:
            key = (n.get("employee_name") or "").strip().lower()
            if key and n.get("notes"):
                c.notes[key] = (c.notes.get(key, "") + " " + n["notes"]).strip()
    except Exception:
        pass

    # time off: approved blocks, pending is a warning
    if c.week_dates:
        try:
            import time_off as _to
            for name, days in (_to.approved_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1], db_path=db_path) or {}).items():
                c.blocked_dates.setdefault(name.strip().lower(), {}).update({d: LABELS["approved_time_off"] for d in days})
            for name, days in (_pending_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1], db_path) or {}).items():
                c.pending_off.setdefault(name.strip().lower(), set()).update(days)
        except Exception:
            pass

    # hours already published in the same payroll week(s), here and at siblings
    try:
        _published_tail(c, restaurant_id, db_path)
    except Exception:
        pass
    return c


def _pending_in_window(restaurant_id, start, end, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT employee_name, start_date, end_date FROM staff_time_off WHERE restaurant_id=? "
            "AND status='pending' AND NOT (end_date < ? OR start_date > ?)", (restaurant_id, start, end)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        a = max(datetime.strptime(r["start_date"][:10], "%Y-%m-%d"), datetime.strptime(start, "%Y-%m-%d"))
        b = min(datetime.strptime(r["end_date"][:10], "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d"))
        days = [(a + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((b - a).days + 1)]
        out.setdefault(r["employee_name"], []).extend(days)
    return {k: sorted(set(v)) for k, v in out.items()}


def _published_tail(c: Constraints, restaurant_id, db_path):
    """Rows from the last PUBLISHED schedule here (and at sibling sites)
    that fall in a payroll bucket this week touches or in the seven days
    before it — the hours count toward the ceiling, the rows toward rest,
    and a sibling's date blocks the person here."""
    from schedule_versions import rows_from_csv
    if not c.week_dates:
        return
    buckets = {c.bucket(d) for d in c.week_dates}
    first = datetime.strptime(c.week_dates[0], "%Y-%m-%d")
    window_start = (first - timedelta(days=7)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    try:
        me = conn.execute("SELECT location_group, owner_email FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        own = conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
                           "AND week_end >= ? ORDER BY id DESC LIMIT 2", (restaurant_id, window_start)).fetchall()
        sibs = []
        group = ((me["location_group"] if me else "") or "").strip()
        if group:
            for s in conn.execute("SELECT id, COALESCE(location_name, name) AS label FROM restaurants "
                                  "WHERE location_group=? AND owner_email=? AND id<>?", (group, me["owner_email"], restaurant_id)).fetchall():
                row = conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
                                   "ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
                if row:
                    sibs.append((s["label"], row["schedule_csv"]))
    finally:
        conn.close()
    seen = set()
    for row in own:
        for r in rows_from_csv(row["schedule_csv"]):
            key = r["employee"].strip().lower()
            sig = (key, r["date"], r["shift_start"])
            if sig in seen or r["date"] in c.week_dates:
                continue
            seen.add(sig)
            if r["date"] >= window_start:
                c.base_rows.setdefault(key, []).append(r)
            b = c.bucket(r["date"])
            if b in buckets:
                c.base_hours.setdefault(key, {})[b] = c.base_hours.get(key, {}).get(b, 0.0) + row_hours(r)
    for label, csv_text in sibs:
        for r in rows_from_csv(csv_text):
            key = r["employee"].strip().lower()
            if r["date"] in c.week_dates:
                c.blocked_dates.setdefault(key, {}).setdefault(r["date"], f"already scheduled at {label}")
            elif r["date"] >= window_start:
                # A close at the other site the night before is still a close
                # for the rest rule here.
                c.base_rows.setdefault(key, []).append(r)
            b = c.bucket(r["date"])
            if b in buckets:
                c.base_hours.setdefault(key, {})[b] = c.base_hours.get(key, {}).get(b, 0.0) + row_hours(r)


# ── the violation sweep ────────────────────────────────────────────────────

def violations(rows: list, c: Constraints) -> list:
    """Every rule the finished week breaks, one entry per breach, each
    naming the person, the date, the rule and whether it is hard (the row
    cannot stand) or soft (worth a look)."""
    out = []
    by_person = {}
    seen_slots = set()
    from shift_quality import present_dayparts as _present
    for i, r in enumerate(rows or []):
        name = (r.get("employee") or "").strip()
        key = name.lower()
        if not key:
            continue
        by_person.setdefault(key, []).append((i, r))
        # Availability is judged on every daypart the shift covers, not its
        # start: a "mornings only" person on 2:30-11:30pm was never flagged.
        ok, why = True, ""
        for part in _present(r):
            ok, why = c.can_work(name, r.get("date", ""), part)
            if not ok:
                break
        if not ok:
            kind = next((k for k, lab in LABELS.items() if lab == why), None)
            if kind is None:
                kind = "elsewhere" if "another location" in why or "scheduled at" in why else "approved_time_off"
            out.append(_v(kind, i, r, why))
        slot = (key, r.get("date"), r.get("shift_start"))
        if slot in seen_slots:
            out.append(_v("double_booked", i, r, LABELS["double_booked"]))
        seen_slots.add(slot)
        hrs = row_hours(r)
        mx = c.compliance.get("max_shift_hours")
        if mx and hrs > float(mx) + 0.01:
            out.append(_v("shift_too_long", i, r, f"{hrs:g}h shift, maximum {float(mx):g}h"))
        if key in c.minors:
            latest = parse_minutes(c.compliance.get("minor_latest_end") or "")
            end_m = parse_minutes(r.get("shift_end", ""))
            start_m = parse_minutes(r.get("shift_start", ""))
            if latest is not None and end_m is not None and start_m is not None and (end_m > latest or end_m < start_m):
                out.append(_v("minor_late", i, r, f"ends {r.get('shift_end')}, minors stop at {c.compliance.get('minor_latest_end')}"))
            mm = c.compliance.get("minor_max_daily_hours")
            if mm and hrs > float(mm) + 0.01:
                out.append(_v("minor_hours", i, r, f"{hrs:g}h, minors stop at {float(mm):g}h a day"))
        dot = c.compliance.get("daily_ot_hours")
        if dot and hrs > float(dot) + 0.01:
            out.append(_v("daily_ot", i, r, f"{hrs:g}h in one day, daily overtime starts at {float(dot):g}h"))
        mb = c.compliance.get("meal_break_after_hours")
        if mb and hrs > float(mb) + 0.01:
            out.append(_v("meal_break", i, r, f"{hrs:g}h shift — schedule a meal break"))
        if c.pending_off.get(key) and r.get("date") in c.pending_off[key]:
            out.append(_v("pending_time_off", i, r, LABELS["pending_time_off"]))
        ok, why = c.window_ok(name, r.get("date", ""), r.get("shift_start", ""), r.get("shift_end", ""))
        if not ok:
            out.append(_v("outside_window", i, r, why))
        ok, why = c.cert_ok(name, r.get("role", ""))
        if not ok:
            out.append(_v("missing_cert", i, r, why))
        arr = c.arrivals.get((r.get("role") or "").strip().lower())
        if arr is not None and c.open_times:
            try:
                day = datetime.strptime(r.get("date", ""), "%Y-%m-%d").strftime("%A")
            except (ValueError, TypeError):
                day = r.get("day") or ""
            open_m = parse_minutes((c.open_times or {}).get(day, ""))
            start_m = parse_minutes(r.get("shift_start", ""))
            if open_m is not None and start_m is not None and start_m < open_m + arr - 15:
                out.append(_v("before_arrival", i, r, f"starts {r.get('shift_start')}, {r.get('role')} arrives at {_fmt_minutes(open_m + arr)}"))

    # a role that stays until N minutes after close: the last of that role
    # each night must end no earlier ("bartenders stay an hour after close")
    if c.close_mins and c.close_times:
        by_date_role = {}
        for i, r in enumerate(rows or []):
            role = (r.get("role") or "").strip().lower()
            if role in c.close_mins and r.get("date"):
                by_date_role.setdefault((r["date"], role), []).append((i, r))
        for (d, role), items in by_date_role.items():
            try:
                day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
            except (ValueError, TypeError):
                continue
            close_m = parse_minutes((c.close_times or {}).get(day, ""))
            if close_m is None:
                continue
            need = close_m + int(c.close_mins[role])
            ends = [(parse_minutes(r.get("shift_end", "")), i, r) for i, r in items]
            ends = [(e, i, r) for e, i, r in ends if e is not None]
            if not ends:
                continue
            e_last, i_last, r_last = max(ends, key=lambda t: t[0])
            if e_last < need - 15:
                out.append(_v("ends_before_role_close", i_last, r_last,
                              f"last {r_last.get('role')} ends {r_last.get('shift_end')}, the rule is until {_fmt_minutes(need)}"))

    # every open daypart needs a manager or keyholder, when the owner says so
    if c.compliance.get("manager_on_duty") and not c.keyholders and rows:
        out.append(_v("manager_rule_unusable", 0, rows[0], LABELS["manager_rule_unusable"]))
    if c.compliance.get("manager_on_duty") and c.keyholders:
        slots = {}
        for i, r in enumerate(rows or []):
            if r.get("date") and (r.get("employee") or "").strip():
                slots.setdefault((r["date"], daypart_of(r.get("shift_start", ""))), []).append((i, r))
        for (d, part), items in sorted(slots.items()):
            if part == "unknown":
                continue
            if not any((r.get("employee") or "").strip().lower() in c.keyholders for _, r in items):
                i0, r0 = items[0]
                out.append(_v("no_manager_on_duty", i0, r0, f"{LABELS['no_manager_on_duty']} ({part})"))

    # more front-of-house on the floor at once than there are sections
    if c.section_cap:
        by_date = {}
        for i, r in enumerate(rows or []):
            if (r.get("role") or "").strip().lower() in c.foh_roles and r.get("date"):
                by_date.setdefault(r["date"], []).append((i, r))
        for d, items in by_date.items():
            events = []
            for i, r in items:
                s_, e_ = parse_minutes(r.get("shift_start", "")), parse_minutes(r.get("shift_end", ""))
                if s_ is not None and e_ is not None and e_ > s_:
                    events.append((s_, 1, i)); events.append((e_, -1, i))
            events.sort(key=lambda ev: (ev[0], ev[1]))
            running, peak, active, worst = 0, 0, [], []
            for t, delta, i in events:
                if delta > 0:
                    active.append(i)
                else:
                    active = [x for x in active if x != i]
                running += delta
                if running > peak:
                    peak, worst = running, list(active)
            if peak > int(c.section_cap) and worst:
                latest = max(worst, key=lambda i: parse_minutes(rows[i].get("shift_start", "")) or 0)
                out.append(_v("over_section_cap", latest, rows[latest], f"{peak} on the floor at once, {int(c.section_cap)} sections"))

    # per-person rules: overlap, rest, hours, days off
    for key, items in by_person.items():
        items.sort(key=lambda ir: (ir[1].get("date", ""), parse_minutes(ir[1].get("shift_start", "")) or 0))
        # hours by payroll bucket, including what is already published
        per_bucket = dict(c.base_hours.get(key) or {})
        for i, r in items:
            b = c.bucket(r.get("date", "")) if r.get("date") else ""
            per_bucket[b] = per_bucket.get(b, 0.0) + row_hours(r)
        name = items[0][1].get("employee")
        mx = c.max_hours(name)
        for b, total in per_bucket.items():
            if mx and total > mx + 0.05 and b:
                for i, r in items:
                    if c.bucket(r.get("date", "")) == b:
                        v = _v("over_max_hours", i, r, f"{total:g}h in the payroll week — over {mx:g}h")
                        # Which payroll week, and by how much, so a fix moves
                        # that week's excess and nothing more.
                        v["bucket"], v["over_by"] = b, round(total - mx, 2)
                        out.append(v)
        mn = c.min_hours(name)
        if mn:
            this_week = sum(row_hours(r) for _, r in items)
            if this_week + 0.05 < mn:
                out.append(_v("under_min_hours", items[0][0], items[0][1], f"{this_week:g}h this week, wants at least {mn:g}h"))
        # rest and overlap, against this week's other shifts and the published tail
        spans = [(i, r, *shift_span(r, c.tz)) for i, r in items]
        spans = [(i, r, s, e) for i, r, s, e in spans if s]
        tail = [(None, r, *shift_span(r, c.tz)) for r in (c.base_rows.get(key) or [])]
        tail = [(i, r, s, e) for i, r, s, e in tail if s]
        need = float(c.compliance.get("min_rest_hours") or 0)
        allspans = sorted(spans + tail, key=lambda t: t[2])
        for a in range(1, len(allspans)):
            i_prev, r_prev, s_prev, e_prev = allspans[a - 1]
            i_cur, r_cur, s_cur, e_cur = allspans[a]
            if i_cur is None:
                continue
            if s_cur < e_prev:
                if r_cur.get("shift_start") != r_prev.get("shift_start") or r_cur.get("date") != r_prev.get("date"):
                    out.append(_v("overlap", i_cur, r_cur, f"overlaps their {r_prev.get('shift_start')}–{r_prev.get('shift_end')} on {r_prev.get('date')}"))
                continue
            gap = (s_cur - e_prev).total_seconds() / 3600
            # Same date = a double shift, not a rest breach (see rest_ok).
            if need and gap < need - 0.01 and r_cur.get("date") != r_prev.get("date"):
                out.append(_v("rest_gap", i_cur, r_cur, f"{gap:.1f}h since their previous shift, the rule is {need:g}h"))
        # too many days in a row — the published tail counts, so a Saturday
        # and Sunday already sent plus Monday to Friday here reads as seven
        max_run = c.compliance.get("max_consecutive_days")
        if max_run:
            worked_dates = {r.get("date") for _, r in items if r.get("date")}
            worked_dates |= {r.get("date") for r in (c.base_rows.get(key) or []) if r.get("date")}
            try:
                ordered = sorted(datetime.strptime(d, "%Y-%m-%d") for d in worked_dates)
            except (TypeError, ValueError):
                ordered = []
            run, run_dates = [], []
            best_run = []
            for d in ordered:
                if run and (d - run[-1]).days == 1:
                    run.append(d)
                else:
                    run = [d]
                if len(run) > len(best_run):
                    best_run = list(run)
            if len(best_run) > int(max_run):
                run_iso = {d.strftime("%Y-%m-%d") for d in best_run}
                week_rows = [(i, r) for i, r in items if r.get("date") in run_iso]
                if week_rows:
                    i_last, r_last = week_rows[-1]
                    out.append(_v("long_run", i_last, r_last,
                                  f"{len(best_run)} days in a row — the rule is at most {int(max_run)}"))
        # consecutive days off inside the generated week
        req = c.compliance.get("part_time_days_off") if c.employment.get(key) == "part" else c.compliance.get("min_consecutive_days_off")
        if req and c.week_dates:
            worked = {r.get("date") for _, r in items}
            off = [d for d in c.week_dates if d not in worked]
            # Anyone working at all is checked: Monday/Wednesday/Friday has
            # four days off and no two of them together — and seven shifts
            # have none at all, which a run limit above 6 used to let
            # through unflagged (SCHED-31).
            if len(worked) >= 2:
                best = run = 0
                prev = None
                for d in c.week_dates:
                    if d in worked:
                        run = 0
                    else:
                        run += 1
                        best = max(best, run)
                if best < int(req):
                    out.append(_v("days_off", items[0][0], items[0][1],
                                  f"{len(off)} day{'s' if len(off) != 1 else ''} off, longest run {best} — the rule is {int(req)} together"))
    return out


def _v(kind, index, row, detail):
    return {"kind": kind, "index": index, "employee": row.get("employee"), "date": row.get("date"),
            "day": row.get("day"), "shift_start": row.get("shift_start"), "role": row.get("role"),
            "detail": detail, "hard": kind in HARD, "no_show": kind in NO_SHOW, "label": LABELS.get(kind, kind)}


def summarize(viols: list) -> dict:
    hard = [v for v in viols if v["hard"]]
    soft = [v for v in viols if not v["hard"]]
    by_kind = {}
    for v in viols:
        by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
    lines = []
    for v in sorted(viols, key=lambda x: (not x["hard"], x["date"] or "", x["employee"] or ""))[:12]:
        where = f"{v.get('day') or v.get('date')} {v.get('shift_start') or ''}".strip()
        lines.append(f"{'⚠ ' if v['hard'] else ''}{v['employee']} — {where}: {v['detail']}")
    return {"hard": len(hard), "soft": len(soft), "by_kind": by_kind, "lines": lines,
            "hard_rows": sorted({v["index"] for v in hard})}


def prompt_block(c: Constraints) -> str:
    """The rules, as the model should read them — the same facts the code
    will check afterwards, so the draft has every chance to be right."""
    comp = c.compliance
    lines = []
    if c.closed_dates:
        _pretty = ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%A %-m/%-d") for d in sorted(c.closed_dates))
        lines.append(f"- The restaurant is CLOSED on {_pretty}. Write no shifts at all on those dates.")
    if comp.get("min_rest_hours"):
        lines.append(f"- At least {float(comp['min_rest_hours']):g} hours between one shift's end and the same person's next start. No closing then opening.")
    if comp.get("max_shift_hours"):
        lines.append(f"- No shift longer than {float(comp['max_shift_hours']):g} hours.")
    lines.append(f"- Nobody over {float(comp.get('weekly_hours_ceiling') or 40):g} hours in the payroll week"
                 + (" (the week starts on " + DAYS[c.week_start_day] + ")" if c.week_start_day else "") + ".")
    if comp.get("daily_ot_hours"):
        lines.append(f"- Daily overtime starts at {float(comp['daily_ot_hours']):g} hours — avoid it.")
    if comp.get("meal_break_after_hours"):
        lines.append(f"- A shift over {float(comp['meal_break_after_hours']):g} hours includes a meal break; leave room for it.")
    if comp.get("max_consecutive_days"):
        lines.append(f"- Nobody works more than {int(comp['max_consecutive_days'])} days in a row, counting days already published last week.")
    if comp.get("min_consecutive_days_off"):
        lines.append(f"- Everyone gets at least {int(comp['min_consecutive_days_off'])} consecutive days off"
                     + (f"; part-time staff {int(comp['part_time_days_off'])}." if comp.get("part_time_days_off") else "."))
    if comp.get("manager_on_duty"):
        lines.append("- Every open daypart has somebody authorised to close or holding a manager/keyholder certification on it.")
    pack = comp.get("_pack") or {}
    if pack.get("applied"):
        lines.append(f"- {pack.get('label')} rules apply: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in pack["applied"].items()) + ".")
    if c.role_requirements:
        lines.append("- Certifications by role: " + "; ".join(f"{r} needs {', '.join(sorted(v))}" for r, v in sorted(c.role_requirements.items())) + " — only schedule people who hold them.")
    if c.close_mins and c.close_times:
        lines.append("- Stays after close: " + "; ".join(f"the last {role} until {m} min after close" for role, m in sorted(c.close_mins.items())) + ".")
    if c.arrivals and c.open_times:
        bits = []
        for role, off in sorted(c.arrivals.items()):
            bits.append(f"{role} {abs(off)} min {'before' if off < 0 else 'after'} open" if off else f"{role} at open")
        lines.append("- Arrival times by role: " + "; ".join(bits) + " — nobody starts earlier than their role's arrival.")
    if c.minors:
        names = ", ".join(sorted({n for n in c.roster_names if n.lower() in c.minors}))
        lines.append(f"- MINORS ({names}): no later than {comp.get('minor_latest_end')} and at most {float(comp.get('minor_max_daily_hours') or 8):g} hours a day.")
    per_person = []
    for n in c.roster_names:
        key = n.lower()
        bits = []
        lim = c.hours_limits.get(key)
        if lim:
            if lim[0]:
                bits.append(f"at least {float(lim[0]):g}h")
            if lim[1]:
                bits.append(f"at most {float(lim[1]):g}h")
        if c.employment.get(key):
            bits.append(f"{c.employment[key]}-time")
        dp = c.daypart_avail.get(key) or {}
        offs = [d[:3] for d in DAYS if dp.get(d) == "off"]
        mornings = [d[:3] for d in DAYS if dp.get(d) == "morning"]
        nights = [d[:3] for d in DAYS if dp.get(d) == "night"]
        if offs:
            bits.append("off " + "/".join(offs))
        if mornings:
            bits.append("lunch/day only " + "/".join(mornings))
        if nights:
            bits.append("dinner/night only " + "/".join(nights))
        win = c.time_windows.get(key) or {}
        for d in DAYS:
            w = win.get(d)
            if w:
                lo, hi = w
                span = (f"from {_fmt_minutes(lo)}" if lo is not None else "") + (f" until {_fmt_minutes(hi)}" if hi is not None else "")
                bits.append(f"{d[:3]} only {span.strip()}")
        certs = c.certifications.get(key)
        if certs:
            bits.append("holds " + ", ".join(sorted(certs)))
        base = c.base_hours.get(key) or {}
        carried = sum(h for b, h in base.items() if b in {c.bucket(d) for d in c.week_dates})
        if carried:
            bits.append(f"{carried:g}h already published in this payroll week")
        pend = c.pending_off.get(key)
        if pend:
            bits.append("has asked for " + ", ".join(sorted(pend)) + " off (not yet decided — avoid if the day can be covered)")
        if bits:
            per_person.append(f"  {n}: " + "; ".join(bits))
    block = "\n\nRULES THE SCHEDULE IS CHECKED AGAINST (every one of these is verified in code after you write the week; a breach is flagged to the owner):\n" + "\n".join(lines)
    if per_person:
        block += "\n\nPER-PERSON LIMITS AND WINDOWS:\n" + "\n".join(per_person)
    floors = []
    for role, spec in (c.role_floors or {}).items():
        base = []
        if spec.get("morning"):
            base.append(f"{spec['morning']} lunch/day")
        if spec.get("night"):
            base.append(f"{spec['night']} dinner/night")
        days = "; ".join(f"{d}: " + ", ".join(f"{n} {p}" for p, n in ds.items() if n) for d, ds in (spec.get("days") or {}).items())
        if base or days:
            floors.append(f"  {role}: at least " + ", ".join(base) + (f" — {days}" if days else ""))
    if floors:
        block += "\n\nSTAFFING FLOORS BY ROLE AND DAYPART (never below these, whatever the hours ceiling says):\n" + "\n".join(floors)
    return block
