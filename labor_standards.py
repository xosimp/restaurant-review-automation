"""
labor_standards.py — how much work one person of a role carries an hour
here, measured from the punches and the ticket archive, with the owner's own
figure over it (schedule audit 10/3/26 D-25).

The only bridge from demand to people was past headcount (D-23): a night
forecast busier asked for a typical night's crew, and nothing said what a
server or a cook can carry. A labor standard is that bridge in the owner's
own terms:

  measure()         per role family and daypart, over the last STANDARD_WEEKS
                    weeks: the work the archive counts for the family (guests
                    seated for the floor, the bar's own tickets for the bar,
                    every ticket for the line) in the hours that family was on
                    the floor ÷ those hours — "11 guests per server-hour at
                    dinner". A family whose work the archive cannot count
                    (dish, prep, managers) has none; under STANDARD_MIN_DAYS
                    measured days, none.
  standards()       the measured figure with the owner's own over it
                    (restaurants.labor_standards_json: {family: {"all" |
                    "morning" | "night": per hour}}), each saying which it is.
  needs_for_week()  for the OWNER's standards only: each shift's people from
                    its usual work, moved by the date's demand, ÷ (standard ×
                    the hours one of them usually works on that shift) — the
                    requirements table replaces that role's usual crew with
                    it and says why. A measured standard is reported, never
                    applied: it is the same history the usual crew already is.

Reads only; nothing calls a model. Family names are intelligence.staffing's
(the ones that cross restaurants); each restaurant keeps its own role names.
"""
import json
import math
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

STANDARD_WEEKS = 8
STANDARD_MIN_DAYS = 6            # measured shifts of a family and daypart before a standard is stated
DAYPART_SPLIT_HOUR = 15          # shift_quality.DAYPART_CUTOVER: a ticket opened before 3pm is lunch's

# What each family's work is, counted from pos_tickets: (unit, column, bar
# tickets only?). A family not here has no standard the archive can measure.
WORK = {
    "server": ("guests", "guest_count", False),
    "runner": ("guests", "guest_count", False),
    "busser": ("guests", "guest_count", False),
    "host": ("guests", "guest_count", False),
    "bartender": ("bar tickets", None, True),
    "barback": ("bar tickets", None, True),
    "line_cook": ("tickets", None, False),
    "cashier": ("tickets", None, False),
    "barista": ("tickets", None, False),
}
UNIT_WORDS = {"server": "server", "runner": "runner", "busser": "busser", "host": "host", "bartender": "bartender",
              "barback": "barback", "line_cook": "cook", "cashier": "cashier", "barista": "barista"}
OVERRIDE_BOUNDS = (0.5, 500.0)   # an owner's per-hour figure outside these is a typo, not a standard


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _family(role):
    from intelligence.staffing import family_of
    return family_of(role)


def _today(restaurant_id, today=None):
    if today is not None:
        return today
    try:
        import demand
        return demand.local_today(restaurant_id)
    except Exception:
        return date.today()


def _part_of_ticket(opened_at, business_date) -> str:
    try:
        opened = datetime.strptime(str(opened_at).replace(" ", "T")[:16], "%Y-%m-%dT%H:%M")
        hour = opened.hour + 24 * max(0, (opened.date() - date.fromisoformat(str(business_date)[:10])).days)
    except (TypeError, ValueError):
        return ""
    return "morning" if hour < DAYPART_SPLIT_HOUR else "night"


def _work_by_date(restaurant_id, start, end, db_path=DB_PATH) -> dict:
    """{(business date, daypart): {"guests", "tickets", "bar tickets"}} from
    the ticket archive, cancelled tickets out: `guests` are the dining
    room's (a bar ticket's guests are the bar's work, counted as its
    tickets), `tickets` every ticket the line fires."""
    conn = get_conn(db_path)
    try:
        # Summed by the hour in SQL, not read ticket by ticket.
        rows = conn.execute(
            "SELECT business_date, substr(replace(opened_at, ' ', 'T'), 1, 13) || ':00' AS hk, "
            "SUM(CASE WHEN COALESCE(is_bar,0)=0 THEN guest_count ELSE 0 END) AS guests, COUNT(*) AS tickets, "
            "SUM(CASE WHEN COALESCE(is_bar,0)=1 THEN 1 ELSE 0 END) AS bar FROM pos_tickets WHERE restaurant_id=? "
            "AND business_date>=? AND business_date<? AND COALESCE(cancelled,0)=0 AND opened_at IS NOT NULL "
            "GROUP BY business_date, hk", (restaurant_id, start, end)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        part = _part_of_ticket(r["hk"], r["business_date"])
        if not part:
            continue
        e = out.setdefault((str(r["business_date"])[:10], part), {"guests": 0, "tickets": 0, "bar tickets": 0})
        e["tickets"] += int(r["tickets"] or 0)
        e["bar tickets"] += int(r["bar"] or 0)          # the bar's own guests are the bartenders' work
        e["guests"] += int(r["guests"] or 0)
    return out


def _hours_by_date(shifts, start, end) -> dict:
    """{(date, daypart, family): [hours per person]} from the punches, each
    row under the daypart it is mostly on the floor for."""
    from shift_quality import present_dayparts
    out = {}
    for s in shifts or []:
        d = str(s.get("date") or "")[:10]
        if not d or d < start or d >= end:
            continue
        fam = _family(s.get("role"))
        if not fam:
            continue
        part = present_dayparts(s)[0]
        if part not in ("morning", "night"):
            continue
        try:
            h = float(s.get("actual_hours") or s.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            continue
        if h > 0:
            out.setdefault((d, part, fam), []).append(h)
    return out


def _median(vals):
    s = sorted(vals)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def measure(restaurant_id, shifts: list = None, weeks: int = STANDARD_WEEKS, db_path=DB_PATH, today=None) -> dict:
    """{family: {daypart: {"per_hour", "unit", "days", "hours"}}} — the
    measured standards (see the module note), plus `"_slots"`: {(weekday,
    daypart): {"work": {unit: median a night}, "hours_per_person": {family:
    median}}} for needs_for_week. Salaried punches are left out, as the
    baseline leaves them. {} without a ticket archive or punches."""
    today = _today(restaurant_id, today)
    start = (today - timedelta(weeks=int(weeks))).isoformat()
    end = today.isoformat()
    work = _work_by_date(restaurant_id, start, end, db_path=db_path)
    if not work:
        return {}
    if shifts is None:
        import labor
        shifts = labor.load_shifts_for_restaurant(restaurant_id) or []
    try:
        import labor
        shifts, _h = labor._without_salaried(restaurant_id, shifts)
    except Exception:
        pass
    hours = _hours_by_date(shifts, start, end)
    acc = {}
    slots = {}
    for (d, part, fam), hs in hours.items():
        spec = WORK.get(fam)
        w = work.get((d, part))
        wd = date.fromisoformat(d).strftime("%A")
        slot = slots.setdefault((wd, part), {"work": {}, "hours_per_person": {}})
        slot["hours_per_person"].setdefault(fam, []).extend(hs)
        if not spec or not w:
            continue
        units = w[spec[0]]
        if units <= 0:
            continue
        e = acc.setdefault(fam, {}).setdefault(part, {"work": 0.0, "hours": 0.0, "days": 0})
        e["work"] += units
        e["hours"] += sum(hs)
        e["days"] += 1
    for (d, part), w in work.items():
        wd = date.fromisoformat(d).strftime("%A")
        slot = slots.setdefault((wd, part), {"work": {}, "hours_per_person": {}})
        for unit, v in w.items():
            slot["work"].setdefault(unit, []).append(v)
    out = {}
    for fam, parts in acc.items():
        for part, e in parts.items():
            if e["days"] < STANDARD_MIN_DAYS or e["hours"] <= 0:
                continue
            out.setdefault(fam, {})[part] = {"per_hour": round(e["work"] / e["hours"], 1), "unit": WORK[fam][0],
                                             "days": e["days"], "hours": round(e["hours"], 1)}
    out["_slots"] = {k: {"work": {u: _median(v) for u, v in s["work"].items()},
                         "hours_per_person": {f: round(_median(v), 2) for f, v in s["hours_per_person"].items() if v}}
                     for k, s in slots.items()}
    return out


def overrides(restaurant) -> dict:
    """{family: {"all" | "morning" | "night": per hour}} — the owner's own
    standards (restaurants.labor_standards_json). A family this module
    cannot measure work for, or a figure outside OVERRIDE_BOUNDS, is
    skipped."""
    raw = getattr(restaurant, "labor_standards_json", None) if restaurant is not None else None
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    out = {}
    for fam, spec in (data.items() if isinstance(data, dict) else ()):
        fam = str(fam).strip().lower()
        if fam not in WORK or not isinstance(spec, dict):
            continue
        for part, v in spec.items():
            part = str(part).strip().lower()
            if part not in ("all", "morning", "night"):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if OVERRIDE_BOUNDS[0] <= v <= OVERRIDE_BOUNDS[1]:
                out.setdefault(fam, {})[part] = round(v, 1)
    return out


def clean_overrides(data) -> dict:
    """The owner's standards as stored: what overrides() would read back,
    for a settings save. Raises ValueError naming the first bad entry."""
    if not isinstance(data, dict):
        raise ValueError("Send standards as {role family: {lunch|dinner|all: per hour}}.")
    out = {}
    for fam, spec in data.items():
        key = str(fam).strip().lower()
        if key not in WORK:
            raise ValueError(f"{fam} is not a role whose work the ticket archive counts.")
        if not isinstance(spec, dict):
            raise ValueError(f"{fam}: send a per-hour figure for lunch, dinner or all.")
        for part, v in spec.items():
            p = {"lunch": "morning", "dinner": "night"}.get(str(part).strip().lower(), str(part).strip().lower())
            if p not in ("all", "morning", "night"):
                raise ValueError(f"{fam}: {part} is not lunch, dinner or all.")
            if v in (None, ""):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{fam} {part}: {v} is not a number.")
            if not OVERRIDE_BOUNDS[0] <= v <= OVERRIDE_BOUNDS[1]:
                raise ValueError(f"{fam} {part}: {v:g} an hour is outside {OVERRIDE_BOUNDS[0]:g}-{OVERRIDE_BOUNDS[1]:g}.")
            out.setdefault(key, {})[p] = round(v, 1)
    return out


def standards(restaurant_id, restaurant=None, shifts: list = None, db_path=DB_PATH, today=None) -> dict:
    """{"families": {family: {daypart: {"per_hour", "unit", "source": "yours"
    | "measured", "measured", "days"}}}, "slots": measure's `_slots`} — the
    owner's figure over the measured one, each saying which it is. The
    words for a screen are in `text`."""
    if restaurant is None:
        restaurant = _models_mod.get_restaurant(restaurant_id) if db_path in (None, DB_PATH) else \
            _models_mod.get_restaurant(restaurant_id, db_path)
    try:
        measured = measure(restaurant_id, shifts=shifts, db_path=db_path, today=today)
    except Exception as e:
        print(f"[labor_standards] measurement failed for {restaurant_id}: {e}")
        measured = {}
    slots = measured.pop("_slots", {}) if measured else {}
    own = overrides(restaurant)
    fams = {}
    for fam in sorted(set(measured) | set(own)):
        for part in ("morning", "night"):
            m = (measured.get(fam) or {}).get(part)
            o = (own.get(fam) or {}).get(part, (own.get(fam) or {}).get("all"))
            if o is None and not m:
                continue
            unit = WORK[fam][0]
            e = {"per_hour": o if o is not None else m["per_hour"], "unit": unit,
                 "source": "yours" if o is not None else "measured",
                 "measured": m["per_hour"] if m else None, "days": m["days"] if m else 0}
            who = UNIT_WORDS.get(fam, fam)
            e["text"] = (f"{e['per_hour']:g} {unit} per {who}-hour at {'lunch' if part == 'morning' else 'dinner'}"
                         + (" (your standard" + (f"; measured here {e['measured']:g}" if e["measured"] else "") + ")"
                            if e["source"] == "yours" else f" (measured over {e['days']} shifts here)"))
            fams.setdefault(fam, {})[part] = e
    return {"families": fams, "slots": slots}


def split_people(need: int, roles: dict) -> dict:
    """{role: people} — `need` people of one family shared across its job
    codes as they usually split (`roles`: {role: usual people}), by largest
    remainder: each code its whole share, then the people left over to the
    codes with the largest fractions (the busier code, then the name, on a
    tie). The split always sums to exactly `need`: rounding each share and
    holding the lead code to at least 1 asked 3 servers across three codes
    when the owner's standard said 2 (schedule re-audit 10/4/26 SQ-12)."""
    need = max(0, int(need))
    usual = sum(max(0, int(n or 0)) for n in roles.values())
    if not roles:
        return {}
    if usual <= 0:
        lead = max(roles, key=lambda r: (int(roles[r] or 0), r))
        return {r: (need if r == lead else 0) for r in roles}
    exact = {r: need * max(0, int(n or 0)) / usual for r, n in roles.items()}
    split = {r: int(math.floor(v + 1e-9)) for r, v in exact.items()}
    left = need - sum(split.values())
    for r in sorted(roles, key=lambda r: (-(exact[r] - split[r]), -int(roles[r] or 0), r))[:max(0, left)]:
        split[r] += 1
    return split


def needs_for_week(week_dates, typical_headcount: dict, std: dict, date_demand: dict = None) -> dict:
    """{(date, daypart): {role lower: {"people", "reason"}}} — for each role
    whose family carries the OWNER's standard on that daypart: the usual
    work of that weekday's shift, moved by the date's demand, ÷ (standard ×
    the hours one of the family usually works on it), rounded up; the
    family's people go to its roles in the shift as they usually split, the
    largest taking the rest. Pure over its inputs."""
    from schedule_requirements import demand_factor
    fams = (std or {}).get("families") or {}
    slots = (std or {}).get("slots") or {}
    out = {}
    for d in week_dates or []:
        try:
            wd = date.fromisoformat(str(d)[:10]).strftime("%A")
        except ValueError:
            continue
        ratio = ((date_demand or {}).get(d) or {}).get("ratio") or 1.0
        f = demand_factor(ratio) if ratio else 1.0
        for part in ("morning", "night"):
            typical = (typical_headcount or {}).get((wd, part)) or {}
            slot = slots.get((wd, part)) or {}
            by_fam = {}
            for role, n in typical.items():
                fam = _family(role)
                if fam:
                    by_fam.setdefault(fam, {})[role] = int(n or 0)
            for fam, roles in by_fam.items():
                st = (fams.get(fam) or {}).get(part)
                if not st or st.get("source") != "yours":
                    continue
                work = (slot.get("work") or {}).get(st["unit"])
                per_person = (slot.get("hours_per_person") or {}).get(fam)
                if not work or not per_person:
                    continue
                need = max(1, int(math.ceil(work * f / (float(st["per_hour"]) * float(per_person)) - 1e-9)))
                split = split_people(need, roles)
                why = (f"your standard of {st['per_hour']:g} {st['unit']} per {UNIT_WORDS.get(fam, fam)}-hour: "
                       f"about {work * f:.0f} {st['unit']} over {per_person:g}h shifts needs {need}")
                for r, v in split.items():
                    out.setdefault((d, part), {})[r.strip().lower()] = {"people": v, "reason": why}
    return out


def for_requirements(restaurant_id, week_dates, typical_headcount, date_demand=None, restaurant=None,
                     shifts=None, db_path=DB_PATH, needs_only: bool = False) -> dict:
    """{"standards": standards()["families"], "needs": needs_for_week(...)}
    for the generation and the live rescore; {} on any failure — the
    requirements then stand on the usual crew alone. `needs_only` (a live
    rescore): with no standard of the owner's there is nothing to apply, so
    the archive is not read at all."""
    try:
        if restaurant is None:
            restaurant = _models_mod.get_restaurant(restaurant_id) if db_path in (None, DB_PATH) else \
                _models_mod.get_restaurant(restaurant_id, db_path)
        if needs_only and not overrides(restaurant):
            return {"standards": {}, "needs": {}}
        std = standards(restaurant_id, restaurant=restaurant, shifts=shifts, db_path=db_path)
        return {"standards": std["families"], "needs": needs_for_week(week_dates, typical_headcount, std, date_demand)}
    except Exception as e:
        print(f"[labor_standards] unavailable for {restaurant_id}: {e}")
        return {}
