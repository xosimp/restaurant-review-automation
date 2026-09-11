"""
labor.py — Labor cost analysis + Claude-powered scheduling recommendations
"""
import os, csv, json, math
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import anthropic
from ai_utils import create_with_retry, extract_text

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
DEFAULT_HOURLY_RATE = 26.0  # fallback if not set per client


def load_shifts(path: str = "sample_shifts.csv",
                csv_string: str = None) -> list[dict]:
    """Load shifts from a CSV string (client data) or bundled sample."""
    import io
    if csv_string:
        return list(csv.DictReader(io.StringIO(csv_string)))
    # Bundled sample data — week of June 1-7 2026 with verified correct day names
    _SAMPLE = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Marcus T.,Server,11:00,17:00,6,6.1,4200,
2026-06-01,Monday,Jamie L.,Server,11:00,17:00,6,5.5,4200,
2026-06-01,Monday,Priya K.,Server,11:00,17:00,6,5.8,4200,
2026-06-01,Monday,Derek M.,Bartender,16:00,24:00,8,7.7,4200,
2026-06-01,Monday,Sofia R.,Bartender,16:00,24:00,8,8.2,4200,
2026-06-01,Monday,Carlos B.,Cook,10:00,18:00,8,8.2,4200,
2026-06-01,Monday,Amy C.,Cook,10:00,18:00,8,8.4,4200,
2026-06-01,Monday,James H.,Host,17:00,22:00,5,4.6,4200,
2026-06-02,Tuesday,Marcus T.,Server,11:00,17:00,6,5.9,4800,
2026-06-02,Tuesday,Jamie L.,Server,11:00,17:00,6,5.5,4800,
2026-06-02,Tuesday,Priya K.,Server,11:00,17:00,6,5.7,4800,
2026-06-02,Tuesday,Derek M.,Bartender,16:00,24:00,8,8.0,4800,
2026-06-02,Tuesday,Sofia R.,Bartender,16:00,24:00,8,7.5,4800,
2026-06-02,Tuesday,Carlos B.,Cook,10:00,18:00,8,7.7,4800,
2026-06-02,Tuesday,Amy C.,Cook,10:00,18:00,8,8.1,4800,
2026-06-02,Tuesday,James H.,Host,17:00,22:00,5,5.0,4800,
2026-06-03,Wednesday,Marcus T.,Server,11:00,17:00,6,6.1,5100,
2026-06-03,Wednesday,Marcus T.,Server,17:00,23:00,6,6.3,5100,
2026-06-03,Wednesday,Jamie L.,Server,11:00,17:00,6,6.3,5100,
2026-06-03,Wednesday,Jamie L.,Server,17:00,23:00,6,6.2,5100,
2026-06-03,Wednesday,Priya K.,Server,11:00,17:00,6,5.7,5100,
2026-06-03,Wednesday,Derek M.,Bartender,16:00,24:00,8,7.8,5100,
2026-06-03,Wednesday,Sofia R.,Bartender,16:00,24:00,8,7.6,5100,
2026-06-03,Wednesday,Carlos B.,Cook,10:00,18:00,8,8.1,5100,
2026-06-03,Wednesday,Amy C.,Cook,10:00,18:00,8,8.2,5100,
2026-06-03,Wednesday,James H.,Host,17:00,22:00,5,5.5,5100,
2026-06-04,Thursday,Marcus T.,Server,11:00,17:00,6,6.1,5600,
2026-06-04,Thursday,Jamie L.,Server,11:00,17:00,6,6.1,5600,
2026-06-04,Thursday,Priya K.,Server,11:00,17:00,6,6.1,5600,
2026-06-04,Thursday,Derek M.,Bartender,16:00,24:00,8,7.5,5600,
2026-06-04,Thursday,Sofia R.,Bartender,16:00,24:00,8,7.8,5600,
2026-06-04,Thursday,Carlos B.,Cook,10:00,18:00,8,7.7,5600,
2026-06-04,Thursday,Carlos B.,Cook,16:00,24:00,8,7.6,5600,
2026-06-04,Thursday,Amy C.,Cook,10:00,18:00,8,8.1,5600,
2026-06-04,Thursday,Amy C.,Cook,16:00,24:00,8,7.9,5600,
2026-06-04,Thursday,James H.,Host,17:00,22:00,5,4.7,5600,
2026-06-05,Friday,Marcus T.,Server,11:00,17:00,6,5.8,7800,
2026-06-05,Friday,Marcus T.,Server,17:00,23:00,6,6.4,7800,
2026-06-05,Friday,Jamie L.,Server,11:00,17:00,6,6.1,7800,
2026-06-05,Friday,Jamie L.,Server,17:00,23:00,6,6.1,7800,
2026-06-05,Friday,Priya K.,Server,11:00,17:00,6,5.7,7800,
2026-06-05,Friday,Priya K.,Server,17:00,23:00,6,6.2,7800,
2026-06-05,Friday,Derek M.,Bartender,16:00,24:00,8,7.7,7800,
2026-06-05,Friday,Sofia R.,Bartender,16:00,24:00,8,7.9,7800,
2026-06-05,Friday,Carlos B.,Cook,10:00,18:00,8,8.5,7800,
2026-06-05,Friday,Carlos B.,Cook,16:00,24:00,8,8.1,7800,
2026-06-05,Friday,Amy C.,Cook,10:00,18:00,8,8.1,7800,
2026-06-05,Friday,Amy C.,Cook,16:00,24:00,8,8.2,7800,
2026-06-05,Friday,James H.,Host,17:00,22:00,5,5.3,7800,
2026-06-06,Saturday,Marcus T.,Server,11:00,17:00,6,6.3,9200,
2026-06-06,Saturday,Marcus T.,Server,17:00,23:00,6,5.7,9200,
2026-06-06,Saturday,Jamie L.,Server,11:00,17:00,6,5.5,9200,
2026-06-06,Saturday,Jamie L.,Server,17:00,23:00,6,5.8,9200,
2026-06-06,Saturday,Priya K.,Server,11:00,17:00,6,5.8,9200,
2026-06-06,Saturday,Priya K.,Server,17:00,23:00,6,5.7,9200,
2026-06-06,Saturday,Derek M.,Bartender,16:00,24:00,8,8.4,9200,
2026-06-06,Saturday,Sofia R.,Bartender,16:00,24:00,8,8.4,9200,
2026-06-06,Saturday,Carlos B.,Cook,10:00,18:00,8,7.8,9200,
2026-06-06,Saturday,Carlos B.,Cook,16:00,24:00,8,8.2,9200,
2026-06-06,Saturday,Amy C.,Cook,10:00,18:00,8,7.9,9200,
2026-06-06,Saturday,Amy C.,Cook,16:00,24:00,8,8.4,9200,
2026-06-06,Saturday,James H.,Host,17:00,22:00,5,5.0,9200,
2026-06-07,Sunday,Marcus T.,Server,11:00,17:00,6,5.8,6400,
2026-06-07,Sunday,Marcus T.,Server,17:00,23:00,6,5.7,6400,
2026-06-07,Sunday,Jamie L.,Server,11:00,17:00,6,6.1,6400,
2026-06-07,Sunday,Jamie L.,Server,17:00,23:00,6,5.8,6400,
2026-06-07,Sunday,Priya K.,Server,11:00,17:00,6,6.1,6400,
2026-06-07,Sunday,Priya K.,Server,17:00,23:00,6,6.4,6400,
2026-06-07,Sunday,Derek M.,Bartender,16:00,24:00,8,7.9,6400,
2026-06-07,Sunday,Sofia R.,Bartender,16:00,24:00,8,7.7,6400,
2026-06-07,Sunday,Carlos B.,Cook,10:00,18:00,8,8.5,6400,
2026-06-07,Sunday,Carlos B.,Cook,16:00,24:00,8,8.0,6400,
2026-06-07,Sunday,Amy C.,Cook,10:00,18:00,8,7.6,6400,
2026-06-07,Sunday,Amy C.,Cook,16:00,24:00,8,7.5,6400,
2026-06-07,Sunday,James H.,Host,17:00,22:00,5,4.6,6400,"""
    try:
        return list(csv.DictReader(io.StringIO(_SAMPLE)))
    except Exception:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))
        except Exception:
            return []


def load_shifts_for_restaurant(restaurant_id: int) -> list[dict]:
    """Load real client data if available, otherwise use sample data."""
    from models import get_client_data
    data = get_client_data(restaurant_id)
    if data and data.get("shifts_csv"):
        return load_shifts(csv_string=data["shifts_csv"])
    return load_shifts()  # fallback to sample


def get_hourly_rate(restaurant_id: int) -> float:
    """Get per-client hourly rate from DB."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return r.hourly_rate if r and r.hourly_rate else DEFAULT_HOURLY_RATE
    except Exception:
        return DEFAULT_HOURLY_RATE


def get_labor_target(restaurant_id: int) -> float:
    """Get per-client labor target % from DB."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return r.labor_target_pct if r and r.labor_target_pct else 30.0
    except Exception:
        return 30.0


def get_week_start_day(restaurant_id: int) -> int:
    """Payroll workweek start, 0=Monday .. 6=Sunday.

    FLSA overtime is computed on the employer's own designated 7-day
    workweek. This module used to hardcode Monday for both the overtime
    bucket and the generated schedule's start, which silently mis-stated
    overtime for every restaurant that runs a Sunday- or Wednesday-start
    payroll week.
    """
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        return int(getattr(r, "week_start_day", 0) or 0) if r else 0
    except Exception:
        return 0


def analyse_shifts_for_restaurant(restaurant_id: int) -> dict:
    """Load shifts and analyse with client-specific hourly rate and target."""
    from models import get_client_data
    client_data = get_client_data(restaurant_id)
    is_live = bool(client_data and client_data.get("shifts_csv"))
    shifts = load_shifts_for_restaurant(restaurant_id)
    rate   = get_hourly_rate(restaurant_id)
    target = get_labor_target(restaurant_id)
    from models import get_role_rates, compute_blended_rate
    role_rates = get_role_rates(restaurant_id)
    blended = compute_blended_rate(shifts, role_rates, fallback=rate)
    result = analyse_shifts(shifts, hourly_rate=blended, labor_target=target,
                            role_rates=role_rates,
                            week_start_day=get_week_start_day(restaurant_id))
    result['is_live'] = is_live
    result['blended_rate'] = blended
    result['role_rates'] = {k: v for k, v in role_rates.items() if k != "_default"}
    return result


def _shift_rate(shift: dict, role_rates: dict, fallback: float) -> float:
    """Return the hourly rate for a single shift based on role.

    Matched case- and whitespace-insensitively. The role names an owner
    types into settings ("Server") and the ones their CSV carries
    ("server", from the paste-box template the product itself documents)
    are the same role, and an exact match meant every per-role wage they
    had configured was silently ignored in favour of the flat default.
    """
    default = role_rates.get("_default", fallback)
    raw = shift.get("role", "") or ""
    if raw in role_rates:
        return role_rates[raw]
    key = raw.strip().lower()
    for name, rate in role_rates.items():
        if name != "_default" and (name or "").strip().lower() == key:
            return rate
    return default


def _shift_hours(shift: dict) -> float:
    """Hours actually worked for a shift, falling back to scheduled.

    The cost pass used to read actual_hours with no fallback, so a CSV
    carrying only scheduled hours — a published schedule rather than a
    timesheet, which is a shape clients really do upload — produced $0 of
    labor cost, 0% labor, and an "on track" badge. Every other pass in
    this file already read the column this tolerant way; the one that
    priced it did not. hours_are_estimated below reports which happened.
    """
    actual = shift.get("actual_hours")
    if actual not in (None, ""):
        try:
            return float(actual)
        except (TypeError, ValueError):
            pass
    try:
        return float(shift.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_actual_hours(shift: dict) -> bool:
    """True when this row carries a real actual_hours reading."""
    v = shift.get("actual_hours")
    if v in (None, ""):
        return False
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _week_key(date_str: str, week_start_day: int = 0) -> str:
    """The start date of the payroll week `date_str` falls in."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    offset = (d.weekday() - int(week_start_day or 0)) % 7
    return (d - timedelta(days=offset)).strftime("%Y-%m-%d")


OVERTIME_THRESHOLD_HOURS = 40.0
OVERTIME_MULTIPLIER = 1.5

# A period shorter than this is reported as-is but never projected forward.
# One Saturday over target used to become "$24,267/month in savings".
MIN_DAYS_TO_EXTRAPOLATE = 7


def analyse_shifts(shifts: list[dict],
                   hourly_rate: float = DEFAULT_HOURLY_RATE,
                   labor_target: float = 30.0,
                   role_rates: dict = None,
                   week_start_day: int = 0) -> dict:
    """Compute labor metrics from raw shift data."""
    if role_rates is None:
        role_rates = {"_default": hourly_rate}
    LABOR_TARGET = labor_target
    OVERSTAFF_THRESHOLD = labor_target
    by_day = defaultdict(lambda: {"scheduled": 0, "actual": 0, "sales": 0, "shifts": [], "labor_cost": 0})
    by_employee = defaultdict(lambda: {"scheduled": 0, "actual": 0, "shifts": 0})
    overtime_flags = []

    # Identical rows are a re-upload of an overlapping period, not two
    # people working the same shift. Counting them twice inflates hours,
    # cost and the labor percentage with nothing anywhere saying so.
    # The signature is the WHOLE row, deliberately. A subset of columns
    # collapses real shifts: the CSV template this product documents to
    # clients carries no shift_start/shift_end at all — it has a `shift`
    # column reading "lunch" or "dinner" — so a server working both on one
    # day, same role, same hours, is two rows identical in every field the
    # subset looked at. Halving somebody's hours is a worse error than
    # counting a re-upload twice, so only a row identical in every column
    # counts as a duplicate.
    _seen_rows = set()
    duplicate_rows = 0
    _deduped = []
    for s in shifts:
        sig = tuple(sorted((str(k), str(v)) for k, v in s.items()))
        if sig in _seen_rows and s.get("date") and s.get("employee"):
            duplicate_rows += 1
            continue
        _seen_rows.add(sig)
        _deduped.append(s)
    shifts = _deduped

    # Did this upload carry real clock-in readings, or only the planned
    # hours? Every downstream figure changes meaning between the two, so
    # the answer travels with the result instead of being inferred from a
    # cost of zero.
    _rows_with_actual = sum(1 for s in shifts if _has_actual_hours(s))
    hours_are_estimated = bool(shifts) and _rows_with_actual == 0

    # Sales is a per-day figure repeated on every row for that day. It used
    # to be ASSIGNED, so the last row won — including a blank one, which
    # zeroed a day that had $8,000 in it three rows earlier. That made the
    # day-of-week table read double the true rate while the headline read
    # correctly, because the headline was computed from a different pass.
    # One resolution, used by everything.
    day_sales: dict = {}
    sales_conflicts: set = set()
    for s in shifts:
        d_ = s.get("date") or ""
        if not d_:
            continue
        try:
            v_ = float(s.get("sales_that_day") or s.get("sales") or 0)
        except (TypeError, ValueError):
            continue
        if v_ <= 0:
            continue
        prior = day_sales.get(d_)
        if prior is not None and abs(prior - v_) > 0.01:
            sales_conflicts.add(d_)
        else:
            day_sales[d_] = v_

    # A day whose rows disagree about its own sales has no figure we can
    # stand behind. Taking the larger of the two would have been the
    # optimistic choice — more sales means a lower labor percentage — which
    # is exactly the direction this module must never guess in. Dropped from
    # costing and named, the same discipline already applied to a day with
    # no sales at all, so the percentage covers only days with one
    # unambiguous figure.
    for d_ in sales_conflicts:
        day_sales.pop(d_, None)

    for s in shifts:
        day    = s.get("date") or ""
        emp    = s.get("employee") or "Unknown"
        try:
            sched = float(s.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            sched = 0.0
        actual = _shift_hours(s)
        rate   = _shift_rate(s, role_rates, hourly_rate)

        by_day[day]["scheduled"] += sched
        by_day[day]["actual"]    += actual
        by_day[day]["sales"]     = day_sales.get(day, 0.0)
        by_day[day]["shifts"].append(s)
        by_day[day]["labor_cost"] += actual * rate

        by_employee[emp]["scheduled"] += sched
        by_employee[emp]["actual"]    += actual
        by_employee[emp]["shifts"]    += 1

    # ── Overtime premium ──────────────────────────────────────────────
    # Hours past 40 in the payroll week cost 1.5x, not 1x. Costing them
    # straight understated the labor percentage precisely when a
    # restaurant was overstaffed — the case the whole module exists to
    # catch — and the module already flagged those same employees as
    # overtime in the same result. The premium (the extra 0.5x) is
    # attributed back to the days that employee worked that week, in
    # proportion to their hours, so per-day percentages stay coherent
    # with the total.
    _emp_week_rows: dict = defaultdict(list)
    for s in shifts:
        emp = s.get("employee") or "Unknown"
        d_ = s.get("date") or ""
        if not d_:
            continue
        try:
            wk = _week_key(d_, week_start_day)
        except (ValueError, TypeError):
            continue
        _emp_week_rows[(emp, wk)].append(s)

    overtime_premium = 0.0
    overtime_hours_total = 0.0
    for (emp, wk), rows in _emp_week_rows.items():
        wk_hours = sum(_shift_hours(r) for r in rows)
        if wk_hours <= OVERTIME_THRESHOLD_HOURS:
            continue
        ot_hours = wk_hours - OVERTIME_THRESHOLD_HOURS
        overtime_hours_total += ot_hours
        blended = (sum(_shift_hours(r) * _shift_rate(r, role_rates, hourly_rate) for r in rows)
                   / wk_hours) if wk_hours else hourly_rate
        premium = ot_hours * blended * (OVERTIME_MULTIPLIER - 1.0)
        overtime_premium += premium
        for r in rows:
            h = _shift_hours(r)
            if h <= 0:
                continue
            by_day[r.get("date") or ""]["labor_cost"] += premium * (h / wk_hours)

    # Find overstaffed days
    overstaffed = []
    understaffed = []
    for date, d in by_day.items():
        labor_cost = d["labor_cost"]  # already summed with per-role rates
        labor_pct  = (labor_cost / d["sales"] * 100) if d["sales"] else 0
        d["labor_cost"] = round(labor_cost, 2)
        d["labor_pct"]  = round(labor_pct, 1)
        if labor_pct > OVERSTAFF_THRESHOLD:
            # Format date as M/D/YY
            try:
                fmt_date = datetime.strptime(date, "%Y-%m-%d").strftime("%-m/%-d/%y")
            except Exception:
                fmt_date = date
            real_day = datetime.strptime(date, "%Y-%m-%d").strftime("%A") if date else d["shifts"][0]["day"]
            overstaffed.append({"date": fmt_date, "day": real_day,
                                 "labor_pct": round(labor_pct, 1),
                                 "labor_cost": round(labor_cost, 2),
                                 "sales": d["sales"]})
        elif labor_pct < (LABOR_TARGET - 3) and d["sales"] > 2500:
            try:
                fmt_date = datetime.strptime(date, "%Y-%m-%d").strftime("%-m/%-d/%y")
            except Exception:
                fmt_date = date
            real_day_u = datetime.strptime(date, "%Y-%m-%d").strftime("%A") if date else d["shifts"][0]["day"]
            understaffed.append({"date": fmt_date, "day": real_day_u,
                                  "labor_pct": round(labor_pct, 1), "sales": d["sales"]})

    # Overtime risk — bucketed by the restaurant's OWN payroll week (see
    # get_week_start_day), not a hardcoded Monday.
    weekly_hours = {}  # {employee: {week_start: hours}}
    for s in shifts:
        emp    = s.get("employee") or "Unknown"
        actual = _shift_hours(s)
        try:
            week_key = _week_key(s["date"], week_start_day)
        except Exception:
            week_key = s.get("date", "unknown")
        if emp not in weekly_hours:
            weekly_hours[emp] = {}
        weekly_hours[emp][week_key] = weekly_hours[emp].get(week_key, 0) + actual

    def _wk_label(wk):
        try:
            return datetime.strptime(wk, "%Y-%m-%d").strftime("%b %-d")
        except Exception:
            return str(wk)

    for emp, weeks in weekly_hours.items():
        # Every overtime week, not just the first. Breaking after one meant
        # an employee with three 48-hour weeks read as a single incident,
        # so the owner saw a third of their real exposure.
        ot_weeks = sorted((wk for wk, hrs in weeks.items() if hrs > OVERTIME_THRESHOLD_HOURS),
                          key=lambda w: weeks[w], reverse=True)
        if ot_weeks:
            for wk in ot_weeks:
                overtime_flags.append({
                    "employee": emp,
                    "hours": round(weeks[wk], 1),
                    "week": _wk_label(wk),
                    "week_start": wk,
                    "status": "overtime",
                    "hours_estimated": hours_are_estimated,
                })
            continue
        max_hrs = max(weeks.values())
        if 37 <= max_hrs <= OVERTIME_THRESHOLD_HOURS:
            _best_wk = max(weeks, key=weeks.get)
            overtime_flags.append({
                "employee": emp,
                "hours": round(max_hrs, 1),
                "week": _wk_label(_best_wk),
                "week_start": _best_wk,
                "status": "near",
                "hours_estimated": hours_are_estimated,
            })

    # Avg labor % by day of week — average across all occurrences of each day
    dow_summary = {}
    dow_daily = {}  # accumulate per-day labor and sales
    for date, d in by_day.items():
        # Derive day name from actual date, not CSV field (CSV may have wrong day)
        try:
            day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        except Exception:
            day_name = d["shifts"][0]["day"] if d.get("shifts") else None
        if not day_name:
            continue
        # A day with no sales figure contributes labor but no revenue, which
        # inflates that weekday's percentage against the days that do have
        # both. Same population as the headline: costed days only.
        if date not in day_sales:
            continue
        labor_cost = d["labor_cost"]  # already accumulated per-role in the main loop
        sales = d["sales"]
        if day_name not in dow_daily:
            dow_daily[day_name] = {"labor": 0, "sales": 0, "count": 0}
        dow_daily[day_name]["labor"] += labor_cost
        dow_daily[day_name]["sales"] += sales
        dow_daily[day_name]["count"] += 1

    for day_name, d in dow_daily.items():
        avg_pct = (d["labor"] / d["sales"] * 100) if d["sales"] else 0
        dow_summary[day_name] = round(avg_pct, 1)

    total_labor  = sum(d["labor_cost"] for d in by_day.values())

    # Labor percentage and the gap to target are ratios against sales, so they
    # are only meaningful for days we actually have sales for. A Toast sync
    # that brings shifts across before (or instead of) sales is a real and
    # common shape, and treating a missing sales figure as $0 of sales made
    # target_labor_cost 0 — so potential_savings became the ENTIRE payroll.
    # Two 8-hour shifts with no sales reported "$6,309/month in savings" next
    # to "0% labor", which is the kind of number that ends a sales meeting.
    #
    # Only days carrying sales are costed, on both sides of the ratio, so a
    # partial sync understates the period rather than inventing savings from
    # it. With sales on every day — the normal case — this is identical to
    # summing everything.
    total_sales = sum(day_sales.values())
    costed_labor = sum(d["labor_cost"] for k, d in by_day.items() if k in day_sales)
    days_missing_sales = sorted(k for k in by_day.keys() if k and k not in day_sales)

    if total_sales > 0:
        overall_pct = round(costed_labor / total_sales * 100, 1)
        target_labor_cost = total_sales * (LABOR_TARGET / 100)
        potential_savings = round(max(0, costed_labor - target_labor_cost), 2)
    else:
        # No sales at all: there is no labor percentage and no gap to a
        # percentage target. Stays numerically 0 rather than None — a dozen
        # callers do arithmetic on this and iOS decodes it as a non-optional
        # Double, so a null would break the Labor tab outright. The
        # sales_data_missing flag below is how a caller tells "0% because
        # they spent nothing" from "0% because we have no sales to divide
        # by"; what matters here is that savings is 0 and not the payroll.
        overall_pct = 0
        potential_savings = 0.0
    # potential_savings is the gap over the WHOLE synced period. Callers
    # used to multiply it by 4.33 as if every sync were one week, which
    # doubled the monthly figure for a two-week period. Normalize by the
    # calendar days the data covers (closed days are part of the week too)
    # and express it per week and per month (52/12 weeks) explicitly.
    _dates = sorted(k for k in by_day.keys() if k)
    period_days = 0
    if _dates:
        try:
            from datetime import datetime as _dt
            period_days = (_dt.strptime(_dates[-1][:10], "%Y-%m-%d") - _dt.strptime(_dates[0][:10], "%Y-%m-%d")).days + 1
        except (ValueError, TypeError):
            period_days = len(_dates)
        period_days = max(period_days, len(_dates))
    # Under a full week there is no weekly rate to state. One Saturday over
    # target used to be divided by one day and multiplied by seven, then by
    # 4.33 — $800 of real overage presented as "$24,267/month in savings".
    # Below the floor the period figure still stands on its own; only the
    # projection is withheld, and period_too_short_to_project says why.
    period_too_short_to_project = bool(period_days) and period_days < MIN_DAYS_TO_EXTRAPOLATE
    if period_days >= MIN_DAYS_TO_EXTRAPOLATE:
        potential_savings_weekly = round(potential_savings / period_days * 7, 2)
        potential_savings_monthly = round(potential_savings_weekly * 52.0 / 12.0, 2)
    else:
        potential_savings_weekly = 0.0
        potential_savings_monthly = 0.0

    # Role-level breakdown
    by_role = defaultdict(lambda: {"hours": 0, "labor_cost": 0, "headcount": set()})
    for s in shifts:
        role = s.get("role", "Unknown")
        actual = _shift_hours(s)
        rate   = _shift_rate(s, role_rates, hourly_rate)
        by_role[role]["hours"] += actual
        by_role[role]["labor_cost"] += actual * rate
        by_role[role]["headcount"].add(s.get("employee", "Unknown"))
    role_summary = {
        role: {
            "hours": round(d["hours"], 1),
            "labor_cost": round(d["labor_cost"], 2),
            "headcount": len(d["headcount"]),
            "labor_pct": round(d["labor_cost"] / total_sales * 100, 1) if total_sales else 0
        }
        for role, d in by_role.items()
    }

    return {
        "total_labor_cost": round(total_labor, 2),
        "total_sales": round(total_sales, 2),
        "overall_labor_pct": overall_pct,
        "overstaffed_days": sorted(overstaffed, key=lambda x: x["labor_pct"], reverse=True),
        "understaffed_days": understaffed,
        "overtime_risk": overtime_flags,
        "dow_summary": dow_summary,
        "potential_savings": potential_savings,
        "potential_savings_weekly": potential_savings_weekly,
        "potential_savings_monthly": potential_savings_monthly,
        "period_days": period_days,
        "role_summary": role_summary,
        "by_day": {k: {kk: vv for kk, vv in v.items() if kk != "shifts"}
                   for k, v in by_day.items()},
        "employee_hours": {k: dict(v) for k, v in by_employee.items()},
        "labor_target": LABOR_TARGET,
        # Days with shifts but no sales figure. Non-empty means the labor
        # percentage and the savings gap cover only part of the period —
        # the UI should say so rather than present a partial number as whole.
        "days_missing_sales": days_missing_sales,
        "sales_data_missing": not bool(total_sales),
        # True when the upload carried no clock-in readings at all, so every
        # hour above is the planned figure rather than the worked one. The
        # cost pass used to silently price these at zero and report 0% labor
        # with an "on track" badge.
        "hours_are_estimated": hours_are_estimated,
        # Identical rows dropped as a re-upload of an overlapping period.
        "duplicate_rows_ignored": duplicate_rows,
        # Days where two rows disagreed about that day's sales; the larger
        # figure is used and the day is named here rather than resolved
        # silently by whichever row happened to be last.
        "days_with_conflicting_sales": sorted(sales_conflicts),
        "period_too_short_to_project": period_too_short_to_project,
        "min_days_to_project": MIN_DAYS_TO_EXTRAPOLATE,
        "overtime_hours": round(overtime_hours_total, 1),
        "overtime_premium": round(overtime_premium, 2),
        "week_start_day": int(week_start_day or 0),
        "date_range": {
            "start": min((k for k in by_day.keys() if k), default=None),
            "end":   max((k for k in by_day.keys() if k), default=None),
            "days":  len(by_day),
        },
    }


def _period_length_days(snapshot: dict) -> int:
    """Calendar days a stored labor_history snapshot covers."""
    try:
        a = datetime.strptime(str(snapshot.get("period_start"))[:10], "%Y-%m-%d")
        b = datetime.strptime(str(snapshot.get("period_end"))[:10], "%Y-%m-%d")
        return abs((b - a).days) + 1
    except (ValueError, TypeError):
        return 0


# ── Shift strength ────────────────────────────────────────────────────────
#
# An owner asked what happens when the scheduler puts his two weakest
# bartenders on a Saturday night. Availability was satisfied; the schedule
# was still wrong. Strength is the missing signal.
#
# Combined score per role per daypart. Two 5-rated bartenders make 10.
#
# Additive on purpose — it is what the owner described and what he can
# reason about — but additive alone lets four 3s satisfy a threshold of 10
# that was written to mean "two good bartenders". So a floor on the weakest
# member travels alongside it, and the verification below reports both.

def _daypart_of(shift_start: str) -> str:
    """Same 3pm split the schedule generator uses."""
    raw = (shift_start or "").strip().lower().replace(" ", "")
    if not raw:
        return "unknown"
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            return "night" if datetime.strptime(raw, fmt).hour >= 15 else "morning"
        except ValueError:
            continue
    return "unknown"


def shift_strength(rows: list, scores: dict) -> dict:
    """Combined Operational Score per (date, daypart, role).

    An employee with no rating contributes NOTHING and is named. Scoring an
    unrated person as a middle 3 would invent a fact about them, and the
    owner would never learn the rating was missing.
    """
    out = {}
    for r in rows or []:
        name = (r.get("employee") or "").strip()
        role = (r.get("role") or "").strip()
        date = r.get("date") or ""
        if not (name and role and date):
            continue
        key = (date, _daypart_of(r.get("shift_start", "")), role)
        b = out.setdefault(key, {"date": date, "daypart": key[1], "role": role,
                                 "members": [], "unrated": [], "strength": 0,
                                 "weakest": None, "day": r.get("day")})
        if name in [m["name"] for m in b["members"]] or name in b["unrated"]:
            continue  # a double shift is one person, counted once
        sc = scores.get(name)
        if sc is None:
            b["unrated"].append(name)
        else:
            b["members"].append({"name": name, "score": sc})
            b["strength"] += sc
            b["weakest"] = sc if b["weakest"] is None else min(b["weakest"], sc)
    return out


def _same_role(a: str, b: str) -> bool:
    """Whether two role strings name the same role.

    The role an owner types into the targets editor ("Bartender") and the
    one the generated CSV carries ("bartender") are the same job. An exact
    match meant every target and every leader rule silently checked
    nothing — the same failure per-role wages had before _shift_rate
    started matching this way.
    """
    return (a or "").strip().lower() == (b or "").strip().lower()


def _role_lookup(mapping: dict, role: str):
    """mapping[role], tolerant of case and stray whitespace."""
    if role in (mapping or {}):
        return mapping[role]
    for name, value in (mapping or {}).items():
        if _same_role(name, role):
            return value
    return None


def verify_shift_strength(rows: list, scores: dict, thresholds: dict,
                          leader_rules: list = None, close_times: dict = None) -> dict:
    """Check a generated schedule against the strength targets.

    Returns every shortfall with a reason, never a pass/fail. The schedule
    still ships — an owner who cannot staff a Saturday to target needs the
    best available schedule AND to be told, not an error.

    This is a deterministic pass over the finished CSV rather than a rule in
    the prompt alone, because a prompt-only rule plateaus below full
    compliance — the same reason close times and the server cap have
    backstops in code.
    """
    buckets = shift_strength(rows, scores)
    shortfalls, leader_misses, met = [], [], []

    for key, b in sorted(buckets.items()):
        target = _role_lookup(thresholds, b["role"])
        if not target:
            continue
        entry = {
            "date": b["date"], "day": b["day"], "daypart": b["daypart"], "role": b["role"],
            "strength": b["strength"], "target": target,
            "members": sorted(b["members"], key=lambda m: -m["score"]),
            "unrated": b["unrated"],
        }
        if b["strength"] >= target:
            met.append(entry)
            continue
        entry["short_by"] = round(target - b["strength"], 1)
        entry["reason"] = _shortfall_reason(b, target)
        shortfalls.append(entry)

    for rule in (leader_rules or []):
        leader_misses.extend(_check_leader_rule(rule, buckets, scores, close_times))

    return {
        "checked": bool(thresholds) or bool(leader_rules),
        "met": met,
        "shortfalls": shortfalls,
        "leader_misses": leader_misses,
        "buckets": [dict(v, key=None) for v in buckets.values()],
    }


def _shortfall_reason(b: dict, target) -> str:
    """Why this shift came in under, in the owner's terms."""
    names = ", ".join(f"{m['name']} ({m['score']})" for m in
                      sorted(b["members"], key=lambda m: -m["score"])) or "nobody"
    if b["unrated"]:
        return (f"{names} came to {b['strength']} against a target of {target:g}. "
                f"{', '.join(b['unrated'])} " +
                ("has" if len(b["unrated"]) == 1 else "have") +
                " no Operational Score yet, so nothing was counted for "
                + ("them" if len(b["unrated"]) > 1 else "them") + ".")
    if not b["members"]:
        return f"Nobody rated was scheduled, against a target of {target:g}."
    if len(b["members"]) == 1:
        return (f"Only {names} was available, against a target of {target:g}.")
    return (f"{names} came to {b['strength']} against a target of {target:g} — "
            f"the strongest people available were already scheduled elsewhere "
            f"or unavailable.")


def _leader_reason(date, part, role, need, min_score, qualified, bucket) -> str:
    """One sentence an owner can act on, not a rule id."""
    who = ", ".join(f"{m['name']} ({m['score']})" for m in
                    sorted(bucket["members"], key=lambda m: -m["score"]))
    plural = "" if need == 1 else "s"
    head = (f"{bucket.get('day') or date} {part}: needs {need} {role.lower()}{plural} "
            f"scoring {float(min_score):g} or above, found {len(qualified)}.")
    if who:
        return head + f" Scheduled: {who}."
    if bucket["unrated"]:
        return head + (f" {', '.join(bucket['unrated'])} scheduled, with no "
                       f"Operational Score on file.")
    return head + " Nobody was scheduled for that role."


def _check_leader_rule(rule: dict, buckets: dict, scores: dict, close_times: dict = None) -> list:
    """Shift leader requirements, on top of the same capability data.

    "Saturday dinner must include at least one bartender scoring 5."
    "Every closing shift needs somebody authorised to close."
    """
    role = (rule.get("role") or "").strip()
    if not role:
        return []
    want_days = {d.strip().lower() for d in (rule.get("days") or []) if d}
    want_part = (rule.get("daypart") or "").strip().lower() or None
    min_score = rule.get("min_score")
    need = int(rule.get("count") or 1)
    misses = []

    for (date, part, r_role), b in sorted(buckets.items()):
        if not _same_role(r_role, role):
            continue
        day = (b.get("day") or "").strip().lower()
        if want_days and day not in want_days:
            continue
        if want_part and part != want_part:
            continue
        if min_score is not None:
            qualified = [m for m in b["members"] if m["score"] >= float(min_score)]
            if len(qualified) < need:
                misses.append({
                    "date": date, "day": b.get("day"), "daypart": part, "role": role,
                    "rule": f"at least {need} {role.lower()}"
                            f"{'' if need == 1 else 's'} scoring {min_score:g} or above",
                    "found": len(qualified),
                    "reason": _leader_reason(date, part, role, need, min_score,
                                             qualified, b),
                })
    return misses


def get_claude_insights(analysis: dict, restaurant_name: str = "your restaurant",
                        owner_name: str = None, restaurant_id: int = None,
                        staff_notes: list = None) -> str:
    """Ask Claude to narrate labor findings in a warm, direct consultant tone."""
    greeting = f"{owner_name}," if owner_name else "Hi,"
    from time_utils import restaurant_now_by_id
    _local_now = restaurant_now_by_id(restaurant_id) if restaurant_id else datetime.now(ZoneInfo('America/Chicago'))
    today_labor = _local_now.strftime("%B %d, %Y")

    # Guard: sample data is not this restaurant's data. load_shifts_for_restaurant
    # substitutes a bundled fictional week when nothing has been uploaded, and
    # narrating that week as the owner's own — with real-looking dollar amounts
    # and specific dates — is the single most misleading thing this module can
    # do. is_live was already computed and honoured by Home, Total Value
    # Delivered and the web dashboard; the two AI paths ignored it.
    if analysis and analysis.get("is_live") is False:
        return (f"{greeting} There's no shift data on file yet, so there's nothing to analyse. "
                "Upload your shifts CSV under Account and this will fill in with your own numbers. "
                "Reply to will@cavnar.ai if you'd like help getting the export out of your POS.")

    # Guard: if no sales data, return a helpful message instead of nonsense
    total_sales = analysis.get("total_sales", 0)
    total_labor = analysis.get("total_labor_cost", 0)
    if total_sales == 0:
        return (f"{greeting} Your shift data has been uploaded and analyzed, but no sales figures were found. "
                "To see your labor cost percentage and get accurate recommendations, please make sure your CSV includes a sales or revenue column. "
                "Reply to will@cavnar.ai and I can help you format it correctly.")
    if total_labor == 0:
        return (f"{greeting} No labor cost data was found in your upload. "
                "Please make sure your CSV includes employee hours and hourly rates so we can calculate your true labor cost percentage.")
    # Feedback loop: check how many times this client has uploaded shift data
    upload_context = ""
    if restaurant_id:
        try:
            from models import get_conn as _gc_l
            _c = _gc_l()
            row = _c.execute(
                "SELECT COUNT(*) as cnt FROM client_data WHERE restaurant_id=? AND data_type='shifts'",
                (restaurant_id,)
            ).fetchone()
            _c.close()
            if row and row["cnt"] > 1:
                upload_context = f"\nThis client has uploaded shift data {row['cnt']} times — they are actively engaged. Acknowledge their consistency and note if numbers are trending better or need more attention."
        except Exception:
            pass

    # Pull labor history for trend awareness
    trend_context = ""
    has_trend = False
    if restaurant_id:
        try:
            from models import get_labor_history, save_labor_snapshot
            history = get_labor_history(restaurant_id, limit=3)
            if history:
                trend_lines = []
                for h in history:
                    trend_lines.append(f"{h['period_start']} to {h['period_end']}: {h['labor_pct']}% labor")
                trend_context = f"\n- Previous uploads (for trend comparison): {'; '.join(trend_lines)}"
                # Only call it a trend when the two periods are actually
                # comparable. Snapshots cover whatever window each upload
                # happened to carry, so a three-week upload against a
                # one-day upload used to produce a confident "labor is UP
                # 8.2 points" that was mostly a difference in window.
                if len(history) >= 2:
                    _cur_days = int((analysis.get("date_range") or {}).get("days") or 0)
                    _prev_days = _period_length_days(history[0])
                    _comparable = (
                        _cur_days >= 5 and _prev_days >= 5
                        and min(_cur_days, _prev_days) / max(_cur_days, _prev_days) >= 0.6
                    )
                    if _comparable:
                        has_trend = True
                        diff = analysis['overall_labor_pct'] - history[0]['labor_pct']
                        if abs(diff) >= 1:
                            direction = "UP" if diff > 0 else "DOWN"
                            trend_context += f"\n- TREND: Labor % is {direction} {abs(diff):.1f} points from last upload — mention this trend explicitly"
                    else:
                        trend_context += (
                            f"\n- The previous upload covers {_prev_days} days and this one covers {_cur_days}. "
                            "Those windows are too different to compare — do NOT state a trend, a direction, "
                            "or a point change between them, and do not write a forecast.")
            # Save this upload as a new snapshot
            dr = analysis.get('date_range', {})
            if dr.get('start') and dr.get('end'):
                save_labor_snapshot(
                    restaurant_id, dr['start'], dr['end'],
                    analysis['overall_labor_pct'],
                    analysis['total_labor_cost'],
                    analysis['total_sales']
                )
        except Exception as le:
            print(f"[labor trend] {le}")

    # Role breakdown context
    role_context = ""
    role_summary = analysis.get('role_summary', {})
    if role_summary:
        role_lines = [f"{role}: {d['labor_pct']}% labor ({d['headcount']} staff, {d['hours']}h)"
                      for role, d in sorted(role_summary.items(), key=lambda x: x[1]['labor_cost'], reverse=True)]
        role_context = f"\n- Labor by role/department: {'; '.join(role_lines)}"

    # Add upcoming holidays for scheduling context
    try:
        from marketing import get_upcoming_holidays as _get_hols
        _upcoming = _get_hols(_local_now.replace(tzinfo=None))
        holiday_context = f"\n- Upcoming holidays (affects scheduling): {_upcoming}" if _upcoming else ""
    except Exception:
        holiday_context = ""

    # Staff constraints context
    constraints_context = ""
    if staff_notes:
        constraints_context = "\n- Staff scheduling constraints (MUST be respected and referenced when relevant):\n"
        for note in staff_notes:
            constraints_context += f"  * {note['employee_name']}: {note['notes']}\n"
        constraints_context += "  IMPORTANT: If an employee appears in overtime risk but has a constraint allowing overtime or extra hours, explicitly acknowledge this and do NOT flag it as a problem."

    # Everything the analysis knows is incomplete about its own input goes
    # into the prompt as a constraint, rather than being computed and then
    # dropped on the floor the way sales_data_missing was.
    _caveats = []
    if analysis.get("hours_are_estimated"):
        _caveats.append("This upload carried no clock-in times — every hour figure above is the SCHEDULED "
                        "hour, not the worked hour. Say so once, plainly, and do not present the labor "
                        "percentage as a measured actual.")
    _missing = analysis.get("days_missing_sales") or []
    if _missing:
        _caveats.append(f"{len(_missing)} day(s) in this period have shifts but no sales figure "
                        f"({', '.join(_missing[:5])}). The labor percentage and the savings gap cover only "
                        "the days that have both. Mention that the period is partial.")
    if analysis.get("days_with_conflicting_sales"):
        _caveats.append("Some days had two different sales figures on different rows; the larger was used. "
                        "Do not present those days' percentages as exact.")
    if analysis.get("duplicate_rows_ignored"):
        _caveats.append(f"{analysis['duplicate_rows_ignored']} duplicate row(s) were dropped from this upload.")
    data_caveats = ("\n- DATA LIMITS you must respect: " + " ".join(_caveats)) if _caveats else ""

    if analysis.get("period_too_short_to_project"):
        savings_line = (f"not available — this upload covers {analysis.get('period_days', 0)} day(s), which is too "
                        f"short to state a weekly or monthly rate. Do NOT state a monthly or annual savings "
                        f"figure. You may cite the ${analysis.get('potential_savings', 0):,.0f} gap above target "
                        f"for the period itself.")
    else:
        savings_line = (f"${analysis.get('potential_savings_monthly', 0):,.0f} (the gap above target over the "
                        f"{analysis.get('period_days', 0)} days synced, per month)")

    forecast_instruction = (
        '\nAfter the 3 recommendations, add one final line starting with exactly "FORECAST:" '
        "— one sentence, 25 words max, predicting where labor % is headed next week and what "
        "happens if the current trajectory continues. Only write this if the trend direction is "
        "genuinely supported by the data given."
    ) if has_trend else ""

    prompt = f"""You are the Cavnar AI Consultant — a friendly, experienced restaurant labor advisor.
You are writing a weekly labor summary for {owner_name or "the owner"} of {restaurant_name}.
Today's date: {today_labor}{upload_context}{holiday_context}

Data:
- Overall labor cost: ${analysis['total_labor_cost']:,.0f} on ${analysis['total_sales']:,.0f} in sales ({analysis['overall_labor_pct']}% labor ratio)
- This restaurant's labor target: {analysis.get('labor_target', 30)}% (industry full-service range: 33–36%, National Restaurant Association 2024)
- Overstaffed days: {json.dumps(analysis['overstaffed_days'][:3])}
- Days that ran BELOW target on a strong sales day: {json.dumps(analysis['understaffed_days'][:2])} — these are days where labor ran under the target while sales were strong. That is usually a good outcome. It is SOMETIMES a sign of running short, but this system has no service-time, wait-time or cover-count data, so you cannot tell which it was. If you mention one of these days, describe only what the numbers show and ask whether service held up. Never assert that revenue was lost, that service was slow, or that covers were missed, and never recommend adding staff to a day on this evidence alone.
- Overtime risk: {json.dumps(analysis['overtime_risk'])}{role_context}{trend_context}
- Labor % by day of week: {json.dumps(analysis['dow_summary'])}{data_caveats}
- Estimated monthly savings with optimized scheduling: {savings_line}{constraints_context}

This is read on a phone screen — brevity is the whole point. Every sentence you don't need is a sentence a client scrolls past. Cut ruthlessly.

Write a short consultant note structured exactly like this:

Opening: Start with "{greeting}" then ONE sentence with the key number and the single biggest opportunity (a specific date and dollar amount). Maximum 2 sentences total — never 3+.

Recommendations:
1. [One concrete, actionable scheduling suggestion. Hard cap: 20 words. Lead with the action, not the reasoning — "Trim Wednesday staffing by 1" beats "Because Wednesday has historically run high on labor percentage, consider trimming..."]
2. [Second suggestion, same 20-word cap.]
3. [Third suggestion, same 20-word cap. You may add up to 8 words of warm closing on this line — nothing more.]

Tone: warm, direct, human — but terse, like a text message from a sharp consultant, not a report. Use the owner name once, not twice. Every number must be real and specific; never pad a sentence just to sound thorough.
Always use $ signs before dollar amounts (e.g. $2,400 not 2400 or 2,400).
Do NOT use markdown, asterisks, bold, or special characters.
There must be EXACTLY 3 numbered recommendations and nothing after number 3.
The Recommendations section must start with exactly the word "Recommendations:" on its own line.{forecast_instruction}"""

    msg = create_with_retry(
        client,
        model=os.getenv("LABOR_INSIGHT_MODEL", "claude-sonnet-5"),
        max_tokens=650,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="labor_insight",
    )
    # Strip any markdown that slips through
    import re
    text = extract_text(msg).strip()
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("labor insight was truncated")
    from ai_guard import verify_figures
    # The return value is the list of figures the model stated that are not
    # in its input. It used to be thrown away here, which meant an insight
    # with invented numbers was captured for the operator and shown to the
    # owner as fact. verify_figures' own docstring describes the flag this
    # is for; labor was the one path not wiring it up.
    unsupported = verify_figures(text, prompt, "labor_insight", restaurant_id)
    text = re.sub('\\*\\*(.+?)\\*\\*', lambda m: m.group(1), text)
    text = re.sub('\\*(.+?)\\*',   lambda m: m.group(1), text)
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'^\s*[-•]\s', '', text, flags=re.MULTILINE)
    if unsupported:
        text = text.rstrip() + "\n\nUNVERIFIED: " + ", ".join(str(u) for u in unsupported[:5])
    return text


def historical_patterns(shifts: list) -> dict:
    """What this restaurant's own history says about how it staffs.

    Two signals the Shift Quality Engine needs and that only the shift data
    can answer: how many of each role typically work a given weekday and
    daypart, and who has actually worked more than one role.

    Extracted rather than left inline in the prompt builder because the
    live-rescore path a manager hits after moving a shift needs the same
    numbers. Two hand-rolled versions of this is how the score on screen
    starts disagreeing with the score in the schedule.
    """
    from collections import defaultdict as _dd
    by_role_date = _dd(lambda: _dd(lambda: _dd(lambda: _dd(set))))
    dates_by_day = _dd(set)
    roles_by_employee = _dd(set)

    for s in shifts or []:
        date = (s.get("date") or "").strip()
        role = (s.get("role") or "").strip()
        name = (s.get("employee") or "").strip()
        if name and role:
            roles_by_employee[name].add(role)
        if not date:
            continue
        try:
            day = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = (s.get("day") or "").strip()
        if not day:
            continue
        dates_by_day[day].add(date)
        if name and role:
            by_role_date[day][_daypart_of(s.get("shift_start", ""))][role][date].add(name)

    typical = {}
    for day, parts in by_role_date.items():
        dates = dates_by_day[day]
        for part in ("morning", "night"):
            counts = {}
            for role, per_date in parts.get(part, {}).items():
                # Averaged over every date this weekday ran, not only the
                # dates this role appeared — otherwise an occasional role
                # reads as a permanent one.
                n = int(math.floor(
                    sum(len(per_date.get(d, ())) for d in dates) / max(len(dates), 1) + 0.5))
                if n:
                    counts[role] = n
            if counts:
                typical[(day, part)] = counts

    return {
        "typical_headcount": typical,
        "cross_trained": {n: sorted(r) for n, r in roles_by_employee.items() if len(r) > 1},
    }


def _quality_rules_block() -> str:
    """How the model should USE the Operational Score data.

    Kept as its own function because every line here exists to counteract a
    specific failure mode, and they are easier to argue with in one place
    than buried in an f-string:

      Benching the weaker half permanently is the obvious way to maximise a
      rating and the fastest way to lose a team.
      Stacking the strong together leaves a weak shift somewhere else.
      Strength is about WHO works, never about adding people — otherwise it
      becomes a licence to blow the hours ceiling.
    """
    return (
        "\nHOW TO USE THIS:\n"
        "  - Never put your two weakest people on together on a high-volume shift. "
        "That is the specific failure this exists to prevent.\n"
        "  - Pair a weaker person with a stronger one rather than stacking the weak "
        "together or the strong together. A quieter shift is where somebody learns.\n"
        "  - Do NOT simply schedule the highest scores everywhere. Benching the weaker "
        "half every week is how a team stops improving and how people quit.\n"
        "  - Spread the busiest shifts around. The same three people carrying every "
        "Friday and Saturday is how you lose them, and it is scored against you.\n"
        "  - Strength is about WHO works, never about adding people. It can never push "
        "you over the hours ceiling or below the minimum staffing floors.\n"
        "  - If you cannot clear a target with who is available, write the best schedule "
        "you can and say so plainly in your summary — which shift, which target, and who "
        "was missing. Never silently miss one.\n"
    )


def format_profile_block(profiles: list = None) -> str:
    """The shift profiles, as the model needs to read them.

    Returns "" when there is nothing worth saying — a restaurant running
    entirely on defaults gets the generic rules above and no invented claim
    that its Friday is busy.
    """
    if not profiles:
        return ""
    try:
        from shift_quality import DEMAND_RANK
    except Exception:
        return ""
    lines = []
    for p in sorted(profiles, key=lambda x: (-x.priority, x.key)):
        when = ", ".join(p.days) if p.days else "Any day"
        part = {"morning": "lunch/day", "night": "dinner/night"}.get(p.daypart, "any daypart")
        bits = [f"quality target {p.min_quality}/100"]
        if p.min_strength:
            bits.append("strength " + ", ".join(
                f"{r} {float(v):g}+" for r, v in sorted(p.min_strength.items())))
        if p.critical_positions:
            bits.append("must staff " + ", ".join(
                f"{int(c)} {r}" for r, c in sorted(p.critical_positions.items())))
        if p.requires_leader:
            bits.append(f"needs somebody who can run it ({p.leader_min_score:g}+ or "
                        f"authorised to close)")
        if p.experience_mix:
            bits.append(f"about {int(round(p.experience_mix * 100))}% experienced hands")
        if p.training_allowed:
            bits.append("training shift — a weaker, mentored team is acceptable here")
        lines.append(f"  {p.label} ({when}, {part}, {p.demand} demand): " + "; ".join(bits))

    return ("\n\nSHIFT PROFILES — not every shift is judged the same way. Each shift you write "
            "is scored 0-100 on coverage, operational strength, leadership, experience, "
            "training balance, demand match, labor efficiency, fatigue and fairness, and "
            "these are the bars each one is scored against. Optimise for the OVERALL quality "
            "of each shift, not for whichever single rule is easiest to satisfy. A profile "
            "marked as a training shift is where a developing employee should be working "
            "alongside a mentor; a peak-demand profile is never that place:\n"
            + "\n".join(lines) + "\n"
            "  Where a shift matches no profile above, use the standard bar.\n")


def generate_optimized_schedule(analysis: dict, shifts: list[dict],
                                 restaurant_name: str = "Restaurant",
                                 hourly_rate: float = DEFAULT_HOURLY_RATE,
                                 owner_name: str = None,
                                 staff_notes: list = None,
                                 labor_target: float = 30.0,
                                 yoy_context: list = None,
                                 upcoming_events: list = None,
                                 monthly_revenue_target: float = 0.0,
                                 hours_notes: str = None,
                                 role_rates: dict = None,
                                 section_count: int = None,
                                 daypart_split: str = None,
                                 delivery_pct: int = None,
                                 role_minimums_json: str = None,
                                 sched_notes: str = None,
                                 staff_availability: list = None,
                                 tz_name: str = None,
                                 restaurant_id: int = None,
                                 weather_forecast: list = None,
                                 operational_scores: dict = None,
                                 strength_thresholds: dict = None,
                                 leader_rules: list = None,
                                 prior_schedule_summary: dict = None,
                                 shift_profiles: list = None) -> dict:
    """
    Use Claude to generate an optimized weekly schedule.
    Returns dict: {schedule_csv: str, summary: list[str], week_dates: list, week_days: list}
    """
    # Was capped at 15 in the prompt below — silently invisible to any
    # restaurant with a bigger real roster (found via Gia Mia's actual
    # 66-person staff list): SCHEDULING RULES tells the model to use real
    # names "from the staff list," but names past #15 were never in it, so
    # the model could only ever assign shifts to the first 15 it happened to
    # see. Raised generously; TYPICAL HEADCOUNT/PAR reconciliation already
    # bound how many actually get scheduled per day.
    employees = list({s.get("employee"): s.get("role") for s in shifts if s.get("employee")}.items())
    overstaffed = analysis.get("overstaffed_days", [])[:5]
    understaffed = analysis.get("understaffed_days", [])[:3]
    dow = analysis.get("dow_summary", {})

    # Compute no-show risk per DOW from shifts where actual_hours is 0 (employee didn't work).
    # Only rows that actually carry a clock-in reading can testify to this.
    # A CSV with no actual_hours column at all used to read as a 100%
    # no-show rate on every single day, which told the scheduler to add a
    # standby flex staffer seven days a week off the back of a missing
    # column. The overtime pass two functions up already read the column
    # tolerantly; this one asserted from its absence.
    _noshows = {}
    _dow_shift_counts = {}
    for s in shifts:
        if not _has_actual_hours(s):
            continue
        _actual = float(s.get("actual_hours") or 0)
        _sched  = float(s.get("scheduled_hours") or s.get("hours") or 0)
        _date = s.get("date","")
        _dn = ""
        try:
            from datetime import datetime as _dt3
            _dn = _dt3.strptime(_date, "%Y-%m-%d").strftime("%A")
        except Exception:
            _dn = s.get("day","")
        if _dn and _sched > 0:
            _dow_shift_counts[_dn] = _dow_shift_counts.get(_dn, 0) + 1
            if _actual == 0:
                _noshows[_dn] = _noshows.get(_dn, 0) + 1
    _noshows_block = ""
    _high_risk_days = []
    for _dn, _cnt in _noshows.items():
        _total = _dow_shift_counts.get(_dn, 1)
        _rate = round(_cnt / _total * 100)
        if _rate >= 10:
            _high_risk_days.append(f"{_dn} ({_rate}% historical no-show rate)")
    if _high_risk_days:
        _noshows_block = (f"\n\nNO-SHOW RISK (from historical data): {', '.join(_high_risk_days)}. "
                          f"On these days, consider scheduling one extra flex staff member or note "
                          f"in the summary that a standby should be on-call.")

    # Detect cross-trained employees from shift history (appear with 2+ distinct roles)
    _emp_roles = {}
    for s in shifts:
        e, r = s.get("employee",""), s.get("role","")
        if e and r:
            _emp_roles.setdefault(e, set()).add(r)
    _cross_trained = {e: sorted(roles) for e, roles in _emp_roles.items() if len(roles) > 1}
    _cross_block = ""
    if _cross_trained:
        _lines = [f"  {e}: {' / '.join(roles)}" for e, roles in sorted(_cross_trained.items())]
        _cross_block = ("\n\nCROSS-TRAINED STAFF — these employees can flex between roles. "
                        "Use this flexibility to fill gaps before adding headcount:\n" + "\n".join(_lines))

    # Compute typical headcount per role per day-of-week, split by daypart,
    # from actual shift history. This prevents the AI from over/under-
    # staffing vs what the restaurant actually runs — and critically, from
    # collapsing a night-specific headcount into a same-day morning+night
    # split (a real bug: "6 servers Friday" was being read as 6 total for
    # the day and cut to 3+3, when the real pattern is 6 AT NIGHT with a
    # separate, smaller morning crew).
    from collections import defaultdict as _dd
    from datetime import datetime as _dt2

    def _daypart(shift_start: str) -> str:
        """3pm cutoff, matching the mobile app's own morning/night split.

        Parsed 12-hour only ("4:00pm"), which is the format this module's
        own generated schedules use — but every CSV a client uploads is
        24-hour ("16:00"), both in the bundled sample and in the paste-box
        template the product documents. Those all fell through to the
        except and were classified "night", so the morning crew was
        invisible to the scheduler for every real restaurant. Both forms
        parse now, and an unreadable value says so instead of guessing.
        """
        raw = (shift_start or "").strip().lower().replace(" ", "")
        if not raw:
            return "unknown"
        for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
            try:
                return "night" if _dt2.strptime(raw, fmt).hour >= 15 else "morning"
            except ValueError:
                continue
        return "unknown"

    # {dow -> {daypart -> {role -> {date -> set of employees}}}}
    #
    # Was {dow -> daypart -> role -> set of employees} accumulated across
    # every week, then divided by the number of weeks. That set is
    # deduplicated, so a restaurant running the SAME six servers every
    # Friday for three weeks held six employees, divided by three, and
    # reported "Server: 2 night" — a third of the truth. A restaurant with
    # a rotating roster of eighteen different people got the right answer.
    # The metric rewarded churn and punished a stable roster, and it is the
    # scheduler's stated starting point for how many people to schedule.
    #
    # Keyed by date now, so each date's real headcount is counted and then
    # averaged across dates.
    _dow_daypart_role_staff = _dd(lambda: _dd(lambda: _dd(lambda: _dd(set))))
    _dow_date_sets = _dd(set)
    for s in shifts:
        _date = s.get("date", "")
        _role = s.get("role", "Unknown")
        _emp  = s.get("employee", "")
        _dn   = ""
        try:
            _dn = _dt2.strptime(_date, "%Y-%m-%d").strftime("%A")
        except Exception:
            _dn = s.get("day", "")
        if _dn and _date:
            _dow_date_sets[_dn].add(_date)
        if _dn and _emp and _date:
            _dow_daypart_role_staff[_dn][_daypart(s.get("shift_start", ""))][_role][_date].add(_emp)

    def _avg_headcount(by_date: dict, dates: set) -> int:
        """Mean staff on shift across the dates this weekday actually ran.

        Averaged over the dates that had ANY shift for this weekday, so a
        role that only appears on two of three Fridays averages over three,
        not two — otherwise an occasional role reads as a permanent one.
        """
        if not dates:
            return 0
        # Half-up, not Python's bank rounding: 4.5 people on a Friday is
        # 5, not 4. Understaffing is the direction that hurts service,
        # and the hours ceiling already stops the schedule overspending.
        import math
        return int(math.floor(sum(len(by_date.get(d, ())) for d in dates) / len(dates) + 0.5))
    # Build headcount block: "Friday: Server 3 morning / 6 night, Cook 2 morning / 3 night"
    _dow_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    _hc_lines = []
    for _dn in _dow_order:
        if _dn not in _dow_daypart_role_staff:
            continue
        _dates_for_dow = _dow_date_sets[_dn]
        _roles_seen = sorted({r for _dp in _dow_daypart_role_staff[_dn].values() for r in _dp})
        _parts = []
        for _role in _roles_seen:
            _m_avg = _avg_headcount(_dow_daypart_role_staff[_dn]["morning"].get(_role, {}), _dates_for_dow)
            _n_avg = _avg_headcount(_dow_daypart_role_staff[_dn]["night"].get(_role, {}), _dates_for_dow)
            # A shift whose start time could not be read belongs to neither
            # daypart. Reported separately rather than folded into one of
            # them, so the model isn't handed a night figure that quietly
            # includes morning people.
            _u_avg = _avg_headcount(_dow_daypart_role_staff[_dn]["unknown"].get(_role, {}), _dates_for_dow)
            _seg = []
            if _m_avg:
                _seg.append(f"{_m_avg} morning")
            if _n_avg:
                _seg.append(f"{_n_avg} night")
            if _u_avg:
                _seg.append(f"{_u_avg} unspecified start time")
            if _seg:
                _parts.append(f"{_role}: {' / '.join(_seg)}")
        if _parts:
            _hc_lines.append(f"  {_dn}: {', '.join(_parts)}")
    _headcount_block = ""
    if _hc_lines:
        _headcount_block = ("\n\nTYPICAL HEADCOUNT PER DAY — your starting point for who/how many per role per "
                            "day, split by daypart. IMPORTANT: morning and night are SEPARATE headcounts, not "
                            "a combined daily total to divide between them — \"6 night\" means 6 people ON AT "
                            "NIGHT, on top of (not instead of) whatever the morning figure says. Never read a "
                            "day's total as one pool to split across dayparts.\n"
                            "Use these as the baseline. The only reasons to go over are a flagged event or a "
                            "genuine year-over-year volume spike on that specific day. The PAR HOURS CEILING "
                            "below is NOT a reason to go over — it only ever removes hours, never adds them. "
                            "If you do scale up for an event or a spike, do it proportionally across roles "
                            "(not by piling extra hours onto one role) and name the event or the spike in your "
                            "summary. Don't invent a reason that isn't true; staying within these numbers is "
                            "the normal, correct outcome:\n"
                            + "\n".join(_hc_lines))

    # Next Monday as schedule start — in the restaurant's local week, not ours
    from time_utils import restaurant_now
    today = restaurant_now(tz_name, naive=True)
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    week_dates = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    week_days  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    # Build staff constraints block — placed LAST in prompt so it overrides all rules
    constraints = ""
    if staff_notes:
        constraints = ("\n\nSTAFF CONSTRAINTS — HIGHEST PRIORITY. These override ALL scheduling rules above, "
                       "including server stagger, PAR hours target, typical headcount, and shift length guidelines. "
                       "If a constraint conflicts with any rule, the constraint wins, always:\n")
        for note in staff_notes:
            constraints += f"- {note['employee_name']}: {note['notes']}\n"

    # Build year-over-year context block (the key intelligence)
    yoy_block = ""
    if yoy_context:
        yoy_lines = []
        for row in yoy_context:
            dow_name = row.get("next_week_dow", "")
            nw_date  = row.get("next_week_date", "")
            if row.get("yoy_sales"):
                line = (f"  {dow_name} {nw_date}: last year same day → "
                        f"${row['yoy_sales']:,.0f} sales, "
                        f"{row['yoy_labor_pct']}% labor, "
                        f"{row['yoy_hours']}h total hours")
                # Flag if this day is a holiday match
                if row.get("is_holiday"):
                    line += f" ← USE THIS (matched to {row['holiday_name']} last year)"
                yoy_lines.append(line)
            else:
                yoy_lines.append(f"  {dow_name} {nw_date}: no historical data for this day last year")
        if yoy_lines:
            yoy_block = ("\n\nYear-over-year same-day data (PRIMARY scheduling basis — "
                         "prefer this over recent averages; it controls for holidays and seasonality):\n"
                         + "\n".join(yoy_lines))

    # Build upcoming events block
    events_block = ""
    if upcoming_events:
        event_lines = []
        for ev in upcoming_events:
            day_label = f"{ev['days_away']} days away" if ev['days_away'] > 0 else "THIS WEEK"
            event_lines.append(f"  {ev['name']} ({ev['date_str']}) — {day_label}: staff UP vs typical, expect 20-40% higher covers")
        events_block = "\n\nUpcoming events this week (adjust staffing accordingly):\n" + "\n".join(event_lines)

    # Build weather forecast block — NWS only forecasts ~7 days out, so this
    # may cover fewer than all 7 days; that's expected, not an error.
    _weather_block = ""
    if weather_forecast:
        _w_lines = []
        for w in weather_forecast:
            precip = f", {w['precip_pct']}% chance of rain" if w.get("precip_pct") else ""
            _w_lines.append(f"  {w['date']} ({w['day_name']}): {w['high_f']}°F, {w['short_forecast']}{precip}")
        _weather_block = ("\n\nWeather forecast for next week — a MODEST nudge on top of TYPICAL "
                          "HEADCOUNT and the per-day targets above, never a replacement for them. Heavy "
                          "rain/snow/extreme heat typically means fewer walk-ins and unusable patio "
                          "seating; mild/clear days, especially on weekends, typically mean higher patio "
                          "traffic. But a day can still turn out busy despite a bad forecast (or slow "
                          "despite a good one) — actual demand routinely doesn't match the forecast, so "
                          "weather alone should shift staffing by at most a person or two on any given "
                          "day, never restructure it. This especially applies to a day that's ALREADY "
                          "historically slow (e.g. a typical quiet Tuesday): its historical pattern "
                          "already reflects ordinary weather variance for that day, so bad weather on top "
                          "of it is not a reason to cut further below the historical baseline or the "
                          "minimum staffing floors below — those floors hold regardless of forecast.\n"
                          + "\n".join(_w_lines))

    # Which days actually take the money — this restaurant's own median
    # sales per weekday (see build_demand_forecast). Silently omitted when
    # there isn't enough history to say anything honest.
    _demand_block = ""
    if restaurant_id:
        try:
            _demand_block = format_demand_block(build_demand_forecast(restaurant_id))
        except Exception:
            _demand_block = ""

    # The actual previous generation's per-day/per-role staffing (not just
    # historical shift patterns, which TYPICAL HEADCOUNT above already
    # covers) — gives the model something concrete to genuinely compare
    # against for the summary, instead of writing about "changes" with
    # nothing specific to have changed from.
    _prior_schedule_block = ""
    if prior_schedule_summary:
        _prior_lines = []
        for _day in week_days:
            _roles = prior_schedule_summary.get(_day)
            if not _roles:
                continue
            _role_parts = [f"{role} {d['count']} ({d['hours']:.0f}h)" for role, d in _roles.items()]
            _prior_lines.append(f"  {_day}: " + ", ".join(_role_parts))
        if _prior_lines:
            _prior_schedule_block = (
                "\n\nPREVIOUS GENERATED SCHEDULE (last time this was run, per day — headcount and hours "
                "by role):\n" + "\n".join(_prior_lines) +
                "\n  Your summary bullets must describe what's ACTUALLY DIFFERENT this time vs. this "
                "specific prior schedule, not just restate today's staffing in isolation or describe "
                "changes relative to historical patterns instead. If a day's staffing is essentially "
                "unchanged from last time, say so plainly rather than inventing a change that didn't "
                "happen — an accurate 'no change' is more useful to the owner than a fabricated one."
            )

    # Compute PAR hours budget — monthly_revenue_target takes priority, then YoY sum, then recent
    projected_revenue = 0.0
    if monthly_revenue_target and monthly_revenue_target > 0:
        projected_revenue = round(monthly_revenue_target / 4.33, 0)  # monthly → weekly
    elif yoy_context:
        yoy_sales = [r["yoy_sales"] for r in yoy_context if r.get("yoy_sales")]
        if yoy_sales:
            projected_revenue = sum(yoy_sales)
    if not projected_revenue:
        # Scale the synced period up to a week by CALENDAR days covered, not
        # by the count of days that happen to have shifts. A restaurant
        # closed on Mondays has 18 shift-days in 21 calendar days, and
        # dividing by 18 overstated the weekly figure by ~17%. A period
        # under a full week is not scaled up at all — one Saturday times
        # seven is not a week of revenue, and it fed straight into the
        # hours budget below.
        _period = int(analysis.get("period_days") or 0)
        _sales = analysis.get("total_sales", 0)
        if _period >= MIN_DAYS_TO_EXTRAPOLATE:
            projected_revenue = _sales * (7 / _period)
        elif _period:
            projected_revenue = 0.0
    hours_budget = round((projected_revenue * (labor_target / 100)) / hourly_rate, 1) if hourly_rate else 0
    labor_budget_dollars = round(projected_revenue * (labor_target / 100), 0)

    # Build role rates block
    role_rates_block = ""
    if role_rates:
        rate_lines = [f"  {role}: ${rate:.2f}/hr" for role, rate in sorted(role_rates.items(), key=lambda x: x[0] or "") if role and role != "_default"]
        if rate_lines:
            role_rates_block = (f"\n\nPer-role hourly rates (use for cost-aware scheduling decisions):\n"
                                + "\n".join(rate_lines)
                                + f"\n  Blended rate: ${hourly_rate:.2f}/hr (weighted average)")

    # Build hours/operations block
    hours_block = ""
    if hours_notes:
        hours_block = f"\n\nRESTAURANT HOURS & SHIFT RULES (follow exactly — these override any patterns in the historical data):\n{hours_notes}"
    else:
        hours_block = ("\n\nShift timing: base start/end times on the patterns visible in the historical shift data. "
                       "Ensure prep staff (cooks) start before open and closers stay until service ends.")

    # Compute per-day hour targets scaled from YoY totals to hit PAR.
    # _daily_target_map (date -> target hours) is the structured form of
    # the same numbers, returned below for the deterministic top-up pass
    # in client_api.py — the AI only ever sees the text block, but the
    # top-up needs real per-day numbers to know which days to add to.
    _daily_targets = ""
    _daily_target_map: dict = {}
    # A scale factor of budget/covered-hours hands the WHOLE week's budget
    # to whichever days happen to carry history. With two of seven days
    # covered, those two days were each told to absorb roughly triple their
    # own hours. Scaling only happens when most of the week is represented;
    # otherwise the covered days keep their own historical hours as targets
    # and the block says the week is only partly covered.
    _MIN_DAYS_COVERED_TO_SCALE = 5
    if yoy_context:
        _yoy_days = [r for r in yoy_context if float(r.get("yoy_hours") or 0) > 0]
        _yoy_total = sum(float(r.get("yoy_hours") or 0) for r in _yoy_days)
        if _yoy_total > 0:
            _covered = len(_yoy_days)
            _scale = (hours_budget / _yoy_total) if _covered >= _MIN_DAYS_COVERED_TO_SCALE else 1.0
            _day_lines = []
            for _r in _yoy_days:
                _target_h = round(float(_r["yoy_hours"]) * _scale, 1)
                _day_lines.append(f"    {_r['next_week_dow']} {_r['next_week_date']}: {_target_h}h")
                _daily_target_map[_r['next_week_date']] = _target_h
            if _day_lines:
                _hdr = ("\n  Per-day targets (YoY scaled to PAR):\n" if _covered >= _MIN_DAYS_COVERED_TO_SCALE else
                        f"\n  Per-day targets — last year's own hours, NOT scaled to the weekly budget. Only "
                        f"{_covered} of 7 days have prior-year data, so spreading the whole week's budget across "
                        f"them would over-staff those days badly. Staff the uncovered days from TYPICAL HEADCOUNT "
                        f"and do not try to hit the weekly hours total from these days alone:\n")
                _daily_targets = _hdr + "\n".join(_day_lines)

    # Fallback for a restaurant with no real YoY history yet (same-day-
    # last-year data needs a full year on the platform — Gia Mia's 2-week
    # seed history never has it, and this fallback was consistently
    # missing every time this was tested live this session). Without it, a
    # large PAR gap got a single abstract "hit 1314h somehow" instruction
    # with no per-day breakdown at all — far easier to under-shoot than 7
    # concrete numbers. Scales actual historical hours-by-weekday (same
    # technique as the YoY branch above, just sourced from by_day instead
    # of a prior year) up to the PAR total.
    if not _daily_targets:
        # Averaged per weekday occurrence, not summed. A period that
        # happens to contain four Mondays and three Fridays weighted Monday
        # a third heavier than it should have been purely because of where
        # the period boundaries fell.
        _hist_sum: dict = {}
        _hist_n: dict = {}
        for _date, _d in (analysis.get("by_day") or {}).items():
            try:
                _dow = datetime.strptime(_date, "%Y-%m-%d").strftime("%A")
            except (ValueError, TypeError):
                continue
            _hist_sum[_dow] = _hist_sum.get(_dow, 0.0) + float(_d.get("actual") or 0)
            _hist_n[_dow] = _hist_n.get(_dow, 0) + 1
        _hist_by_dow = {k: (_hist_sum[k] / _hist_n[k]) for k in _hist_sum if _hist_n.get(k)}
        _hist_total = sum(_hist_by_dow.values())
        _covered2 = sum(1 for v in _hist_by_dow.values() if v > 0)
        if _hist_total > 0:
            _scale2 = (hours_budget / _hist_total) if _covered2 >= _MIN_DAYS_COVERED_TO_SCALE else 1.0
            _day_lines2 = []
            for _wd, _wdate in zip(week_days, week_dates):
                _h = _hist_by_dow.get(_wd, 0.0)
                if _h:
                    _target_h2 = round(_h * _scale2, 1)
                    _day_lines2.append(f"    {_wd} {_wdate}: {_target_h2}h")
                    _daily_target_map[_wdate] = _target_h2
            if _day_lines2:
                _hdr2 = ("\n  Per-day targets (this restaurant's own average hours for each weekday, scaled to "
                         "the weekly budget — no YoY data available):\n"
                         if _covered2 >= _MIN_DAYS_COVERED_TO_SCALE else
                         f"\n  Per-day targets — this restaurant's own average hours per weekday, NOT scaled to "
                         f"the weekly budget. Only {_covered2} of 7 weekdays appear in the synced history, so "
                         f"scaling would pile the whole week onto them:\n")
                _daily_targets = _hdr2 + "\n".join(_day_lines2)

    # Section count — caps how many servers can work simultaneously
    _section_block = ""
    if section_count:
        _section_block = (f"\n\nDINING SECTIONS: {section_count} sections/tables. "
                          f"Maximum {section_count} servers can work simultaneously (one per section). "
                          f"Never schedule more servers than sections — extra servers have nothing to serve.")

    # Daypart split — tells AI how to weight lunch vs dinner staffing
    _daypart_block = ""
    if daypart_split:
        _daypart_block = (f"\n\nDAYPART REVENUE SPLIT: {daypart_split}. "
                          f"Weight staffing toward the higher-revenue daypart. "
                          f"If dinner is 70%+, prioritize closers and dinner openers over lunch staffing.")

    # Delivery/takeout split — shifts labor toward kitchen, away from FOH
    _delivery_block = ""
    if delivery_pct and delivery_pct > 0:
        foh_note = "fewer servers needed" if delivery_pct >= 20 else "minor FOH impact"
        _delivery_block = (f"\n\nDELIVERY/TAKEOUT: {delivery_pct}% of revenue is off-premise. "
                           f"This means more kitchen labor is needed for packaging/output, "
                           f"but {foh_note} — do not over-schedule servers to cover revenue that isn't dine-in.")

    # Role minimums from settings (overrides the defaults in the prompt if provided)
    _role_minimums_extra = ""
    if role_minimums_json:
        import json as _jrm
        try:
            _rm = _jrm.loads(role_minimums_json)
            _rm_lines = [f"  {role}: minimum {count} on any service day" for role, count in _rm.items()]
            _role_minimums_extra = ("\n  Restaurant-specific overrides:\n" + "\n".join(_rm_lines))
        except Exception:
            pass

    # Employee availability block
    import json as _jav
    _avail_block = ""
    if staff_availability:
        _av_lines = []
        for av in staff_availability:
            _name = av.get("employee_name","")
            _avail = _jav.loads(av.get("available_days") or "[]")
            _unavail = _jav.loads(av.get("unavailable_days") or "[]")
            _anote = av.get("notes","")
            parts = []
            if _avail:
                parts.append(f"available: {', '.join(_avail)}")
            if _unavail:
                parts.append(f"NOT available: {', '.join(_unavail)}")
            if _anote:
                parts.append(_anote)
            if parts:
                _av_lines.append(f"  {_name}: {' | '.join(parts)}")
        if _av_lines:
            _avail_block = ("\n\nEMPLOYEE AVAILABILITY — do not schedule anyone on days they are unavailable. "
                            "This is a hard constraint, same priority as STAFF CONSTRAINTS:\n"
                            + "\n".join(_av_lines))

    # ── Operational Score ─────────────────────────────────────────────────
    #
    # The signal that was missing: availability said two bartenders could
    # work Saturday, and nothing said they were the two weakest.
    #
    # Deliberately NOT "put the best people on everything". A schedule that
    # maximises rating benches the weaker half permanently, which is how a
    # team stops improving and how people leave. The instruction below is to
    # clear a bar on the shifts that matter and to pair rather than stack.
    _strength_block = ""
    _scores = {k: v for k, v in (operational_scores or {}).items() if v}
    if _scores:
        _rated_lines = []
        for _e, _r in employees:
            _sc = _scores.get(_e)
            if _sc:
                _rated_lines.append(f"  {_e} ({_r}): {_sc}")
        _unrated = [e for e, _r in employees if e and e not in _scores]
        _thr_lines = [f"  {role}: combined {float(v):g} or better on a shift"
                      for role, v in sorted((strength_thresholds or {}).items())]
        _rule_lines = []
        for _rule in (leader_rules or []):
            _days = ", ".join(_rule.get("days") or []) or "every day"
            _part = _rule.get("daypart") or "any daypart"
            if _rule.get("min_score") is not None:
                _rule_lines.append(
                    f"  {_days} ({_part}): at least {int(_rule.get('count') or 1)} "
                    f"{_rule['role']} scoring {float(_rule['min_score']):g} or above")

        _strength_block = (
            "\n\nOPERATIONAL SCORE — how strong each person is, 1 weakest to 5 strongest, "
            "set by the owner:\n" + "\n".join(_rated_lines)
            + (f"\n  Not yet rated: {', '.join(_unrated)} — treat as unknown, "
               f"neither strong nor weak, and do not avoid them for it.\n" if _unrated else "\n")
        )
        if _thr_lines:
            _strength_block += (
                "\nSHIFT STRENGTH TARGETS — the scores of everyone in that role on that "
                "shift, added up:\n" + "\n".join(_thr_lines) + "\n"
                "  Two people scoring 5 make 10. So do a 5, a 3 and a 2 — but that is a "
                "weaker team, so prefer fewer stronger people over more weaker ones when "
                "both clear the bar.\n"
                "  Hit these on the busiest shifts first. Which ones those are is in the "
                "demand and year-over-year figures above, not in the day's name.\n"
                "  An unrated person contributes nothing to the total. That is not a reason "
                "to leave them off — it is why the owner will be told to rate them.\n"
            )
        if _rule_lines:
            _strength_block += ("\nSHIFT LEADER REQUIREMENTS — each of these must be "
                                "satisfied, not merely aimed at:\n" + "\n".join(_rule_lines) + "\n")
        _strength_block += _quality_rules_block()

    # ── What each shift is actually judged on ─────────────────────────────
    #
    # The scheduler used to be handed a pile of independent rules and no
    # statement of what a GOOD shift looks like, so it optimised whichever
    # rule was stated most forcefully. This block names the profile each
    # shift is scored against, so "Saturday dinner" and "Monday lunch" stop
    # being the same problem with different dates.
    _profile_block = format_profile_block(shift_profiles)

    # Extra scheduling notes from admin
    _sched_notes_block = ""
    if sched_notes:
        _sched_notes_block = f"\n\nADDITIONAL SCHEDULING NOTES (from management):\n{sched_notes}"

    # A labor target is a CEILING, not a quota. This block used to tell the
    # model that landing under budget meant "the historical staffing data
    # doesn't reflect what this restaurant can now afford" and to close the
    # gap by adding headcount across every day — so a restaurant running an
    # efficient 24% against a 30% target had staff added until it reached
    # 30%. That raised payroll inside the one module whose headline metric
    # is savings. Under budget is a good outcome and is now stated as one.
    _hours_rule = (
        f"- Weekly hours must not EXCEED {hours_budget}h. Landing under it is fine and expected — "
        f"never add people or hours to reach it (see PAR HOURS CEILING above). If you are over it, trim back."
        if hours_budget else
        "- There is no weekly hours ceiling for this schedule (not enough history to set one honestly). "
        "Staff from TYPICAL HEADCOUNT and the minimum floors; do not invent a total to aim at."
    )

    if not hours_budget:
        # No defensible revenue projection — too little history, and no
        # revenue target on file. Stating the ceiling anyway printed
        # "0.0h is the MAXIMUM for the week", which reads as an instruction
        # to schedule nobody. Say there is no ceiling instead.
        par_block = ("\n\nPAR HOURS CEILING — none available. There isn't enough sales history "
                     "(and no monthly revenue target on file) to put an honest weekly hours "
                     "budget on this schedule. Staff it from TYPICAL HEADCOUNT, the minimum "
                     "floors and the constraints below, and do not invent an hours figure to "
                     "aim at." + _daily_targets)
    else:
        par_block = (f"\n\nPAR HOURS CEILING — schedule is verified against actual column totals:\n"
                     f"  Projected revenue: ${projected_revenue:,.0f} | Labor target: {labor_target}% = ${labor_budget_dollars:,.0f}\n"
                     f"  Blended rate: ${hourly_rate}/hr → {hours_budget}h is the MAXIMUM for the week\n"
                     f"  This is a ceiling, not a quota. Coming in under it is a good outcome and needs no "
                     f"correction, no explanation and no compensating headcount. NEVER add people, extend shifts "
                     f"or invent coverage in order to reach it. If TYPICAL HEADCOUNT and the per-day targets land "
                     f"you well under {hours_budget}h, that is the right schedule — write it and move on.\n"
                     f"  If they would put you OVER {hours_budget}h, that is the case to act on: trim back toward "
                     f"the ceiling, taking hours from the days furthest above their own per-day target first, and "
                     f"never below the MINIMUM STAFFING FLOORS above. Say in the summary which days you trimmed.\n"
                     f"  Staffing is governed by TYPICAL HEADCOUNT, the per-day targets, the minimum floors and the "
                     f"constraints below — in that order. The hours ceiling only ever removes hours; it never adds "
                     f"them.{_daily_targets}")

    prompt = f"""You are a restaurant scheduling expert for {restaurant_name}. Generate an optimized schedule for next week AND a brief plain-English summary of your decisions.

CONTEXT:
- Current overall labor: {analysis["overall_labor_pct"]}% (target: {labor_target}%)
- Blended hourly rate: ${hourly_rate}/hr
- Recent overstaffed days: {[d["day"] + " (" + str(d["labor_pct"]) + "%)" for d in overstaffed]}
- Recent understaffed days: {[d["day"] for d in understaffed]}
- Recent labor % by day of week: {dow}
- Active staff: {[e[0] + " (" + e[1] + ")" for e in employees[:100]]}{yoy_block}{events_block}{_demand_block}{_weather_block}{_prior_schedule_block}{role_rates_block}{hours_block}{par_block}{_headcount_block}{_cross_block}{_section_block}{_daypart_block}{_delivery_block}{_noshows_block}{_strength_block}{_profile_block}{_avail_block}{_sched_notes_block}

Next week dates:
{chr(10).join(f"- {d}: {n}" for d, n in zip(week_dates, week_days))}

OUTPUT — your entire response must follow this structure with no text before the CSV:

date,day,employee,role,shift_start,shift_end,scheduled_hours,notes
2026-MM-DD,Day,Employee Name,Role,start,end,hours,note
(continue for every shift)
---SUMMARY---
- bullet 1
- bullet 2
- bullet 3

Each summary bullet: one short clause, 10 words or fewer, plain language — the concrete change and its one-line reason, nothing more. A restaurant owner should be able to read all 3 in under 5 seconds. No full sentences, no restating these rules back, no generic scheduling advice.

No emoji anywhere in the CSV notes or summary bullets — plain professional text only.

DO NOT write any explanation, reasoning, preamble, or step-by-step deliberation anywhere in your response — not before the CSV, not in a "<think>" block, not between rows, not woven into the notes column. Do the arithmetic and constraint-solving silently and output only the final answer: the CSV rows, then "---SUMMARY---", then the bullets. Start your response with "date,day,employee..." immediately and do not deviate from that format at any point.

Rows for a non-routine addition — a food runner, a second/extra staff member added for volume, a role or arrival time called out by a special rule above — are exactly where column order most often gets scrambled, because they don't follow the same repeating pattern as the rest of the week. Before writing one of these rows, slow down internally (without narrating it) and confirm you are about to write, in order: date, day, employee, role, shift_start, shift_end, scheduled_hours, notes — a real weekday word in the day column and a real person's name in the employee column, same as every other row. Never let a special role name or rule override push into the day or employee position.

SCHEDULING RULES:
- Use exact dates listed above and real employee names from the staff list
- Base each day's staffing on the YoY same-day data when available — that is your primary projection
- For holiday weeks, match staffing to last year's holiday labor hours, not recent averages
- No employee over 40h for the week
{_hours_rule}

ROLE STAGGER RULE (universal — applies to every restaurant):
- Never schedule two employees in the same role at the exact same start time. The first person opens; additional staff stagger in based on volume. Add headcount only when YoY data or a flagged event justifies it — never to consume an hours budget.

SERVER CLOSING STAGGER RULE (universal — applies to every restaurant, including busy nights like Mondays and weekends, unless RESTAURANT HOURS & SHIFT RULES below explicitly says otherwise):
- Never schedule every server on a shift to close at the same time. Dinner rush tapers off well before actual closing — real restaurants don't pay a full server lineup to stand around a dead dining room for the last hour. Keep only 1-2 servers on through close to handle stragglers and closing side-work; end the rest of that shift's servers' shifts once volume visibly drops (commonly ~8:30-9pm, adjust to this restaurant's own patterns). A busier night justifies scheduling MORE servers earlier in the shift, not keeping more of them until close.

CONSECUTIVE DAYS OFF:
- Every employee must receive at least 2 consecutive days off per week. Never give isolated single days off. Part-time staff should have 3+ consecutive days off.

CROSS-TRAINING:
- When a gap exists in a role, check CROSS-TRAINED STAFF first before adding a new person. Flexing a cross-trained employee costs nothing extra and keeps headcount lean.

NO-SHOW BUFFER:
- On the highest-volume days of the week (typically Fri/Sat for most restaurants), note in the summary that a standby should be on-call if headcount is already at ceiling.

ARRIVAL TIMES, ROLE MINIMUMS, SHIFT LENGTHS, AND ROLE-SPECIFIC RULES:
- Follow the RESTAURANT HOURS & SHIFT RULES block above exactly. Those are the definitive rules for this restaurant.
- If a rule is not specified there, infer reasonable defaults from the historical shift data patterns.{_role_minimums_extra}
- A day's stated close time is a hard ceiling for every shift_end that day — no exceptions beyond an explicit "stay N after close" rule stated for a specific role. Close time commonly varies by day of week (e.g. an earlier weekday close vs. a later weekend close); always use the close time for the EXACT day you are scheduling, never a different day's. A day being staffed heavier because it's unusually busy (e.g. "treat this day's volume like a busy Friday") is about HEADCOUNT, never about closing time — a busy Monday that closes at 9pm still closes at 9pm, not whatever time a busier weekend day closes. Before finalizing, check every closer's shift_end against that specific day's actual close time.

- Shifts per day: use the TYPICAL HEADCOUNT block as your starting point (scale beyond it only for a high-volume YoY day or a flagged event, never to reach an hours figure). Use CROSS-TRAINED STAFF to fill role gaps before adding new headcount.
- Server shift length: split most servers into a lunch/day shift OR a dinner/night shift, not a single shift spanning the whole day — that's how real restaurants staff and it's what lets a manager read morning vs. night coverage at a glance. At most 1-2 servers per day may work a "straight through" (opening to close); everyone else gets a clear daypart split. This is about shift LENGTH, not headcount — do not use it as a reason to cut the number of people working nights. Each daypart gets its own full headcount per the TYPICAL HEADCOUNT block above (e.g. 6 people at night stays 6 people at night; splitting shift length doesn't mean splitting the 6 into 3 morning + 3 night) — but that total is a headcount of everyone PRESENT during that daypart, not a count of night-only shifts specifically. A straight-through and any shift that extends into the night daypart (e.g. an 11:30am-7pm server) is ALREADY one of the night total's people — it counts toward the 6, it does not add to it. Before finalizing each day, count every person actually on the floor during dinner service (straight-throughs and extended day-into-night shifts included) and confirm that total — not just the count of night-only rows — matches the TYPICAL HEADCOUNT night number.
- Notes column: one brief phrase per shift (e.g. "YoY match - high volume", "staggered opener", "cross-trained flex")
- IMPORTANT: All times in shift_start and shift_end MUST be in 12-hour US format with am/pm — e.g. "11:00am", "4:00pm", "9:30pm". Never use 24-hour/military time.{constraints}"""

    EXPECTED_HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"

    msg = create_with_retry(
        client,
        model=os.getenv("SCHEDULE_MODEL", "claude-sonnet-5"),
        # Was 8000 — ai_usage logs showed real generations for this
        # restaurant landing on exactly 8000 output tokens, which is
        # truncation (stop_reason: max_tokens), not natural completion.
        # Raising the ceiling doesn't cost anything extra by itself —
        # output tokens (and their cost/time) are billed for what the
        # model actually generates, not the ceiling.
        max_tokens=16000,
        # A captured generation once opened with a literal "<think>...</think>"
        # block of plain-text step-by-step reasoning — not the API's own
        # (disabled) structured thinking feature, just prose the model chose
        # to write — that alone consumed the entire max_tokens budget and
        # left zero room for actual CSV rows (stop_reason: max_tokens,
        # hours_scheduled: 0). An assistant-message prefill would have
        # blocked this structurally, but this model rejects prefill outright
        # ("This model does not support assistant message prefill" — a hard
        # model constraint). The fix is prompt-only: the explicit
        # no-preamble/no-"<think>" instruction in SCHEDULING RULES below.
        # Verified live (2026-08-14): stop_reason=end_turn, ~3.7-4k output
        # tokens (well under the ceiling), real non-empty CSV output.
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="labor_schedule",
    )
    raw = extract_text(msg).strip()
    print(f"[schedule] raw length={len(raw)} stop_reason={msg.stop_reason}")
    import re as _re_sched

    if "---SUMMARY---" in raw:
        _csv_raw, summary_part = raw.split("---SUMMARY---", 1)
    else:
        _csv_raw = raw
        summary_part = ""

    # Build cleaned CSV: header + data rows that have commas and aren't a repeat header
    _data_rows = []
    for _l in _csv_raw.split("\n"):
        _l = _l.strip().strip('"')
        if not _l or "," not in _l:
            continue
        _low = _l.lower().replace(" ", "")
        if "date" in _low and "employee" in _low and "shift" in _low:
            continue  # skip any accidental header repetition
        _data_rows.append(_l)
    csv_clean = EXPECTED_HEADER + "\n" + "\n".join(_data_rows)
    print(f"[schedule] data_rows={len(_data_rows)} first={_data_rows[0] if _data_rows else None}")

    def _count_csv_hours(csv_text):
        import io
        total = 0.0
        try:
            for row in csv.DictReader(io.StringIO(csv_text)):
                try:
                    total += float(row.get("scheduled_hours") or 0)
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass
        return round(total, 1)

    actual_hours = _count_csv_hours(csv_clean)
    print(f"[schedule] hours_budget={hours_budget} actual={actual_hours} diff={round(actual_hours - hours_budget, 1):+.1f}")

    # Parse summary bullets
    summary_bullets = []
    for line in summary_part.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            line = line[2:].strip()
        line = _re_sched.sub(r'\*+', '', line).strip()
        if line:
            summary_bullets.append(line)

    return {
        "schedule_csv": csv_clean,
        "summary": summary_bullets[:3],
        "week_dates": week_dates,
        "week_days": week_days,
        "projected_revenue": projected_revenue,
        "hours_budget": hours_budget,
        "labor_budget_dollars": labor_budget_dollars,
        "labor_target": labor_target,
        "daily_target_hours": _daily_target_map,
        # The staff list the prompt was actually built from, so the caller
        # can check the model's rows against it rather than trusting that
        # "use real employee names from the staff list" was obeyed.
        "roster": sorted({e for e, _r in employees if e}),
        # Carried back so the deterministic verification pass can check the
        # finished CSV against the same numbers the model was given.
        "operational_scores": _scores,
        "strength_thresholds": dict(strength_thresholds or {}),
        "leader_rules": list(leader_rules or []),
        # The profiles the prompt was built from, so the deterministic
        # quality pass judges the result against the same bars the model
        # was given rather than a set that has drifted since.
        "shift_profiles": list(shift_profiles or []),
        # {(weekday, daypart): {role: typical people}} and who can flex
        # between roles — from the one shared implementation, so the
        # live-rescore path scores against identical numbers.
        **historical_patterns(shifts),
    }


def calculate_monthly_gap(analysis: dict) -> dict:
    """Calculate the dollar gap between current and target labor %."""
    current_pct = analysis["overall_labor_pct"]
    total_sales  = analysis["total_sales"]
    total_labor  = analysis["total_labor_cost"]
    target_pct   = analysis.get("labor_target", 30.0)

    # Was `* 2`, with the comment "data covers ~2 weeks". Uploads are
    # whatever window the client exported: measured error against the real
    # monthly rate was -53% on a one-week upload and +87% on a four-week
    # one. Normalize by the calendar days the period actually covers, and
    # refuse to project at all below a full week — the same floor
    # analyse_shifts uses for its own weekly and monthly figures.
    period_days = int(analysis.get("period_days") or 0)
    if not period_days or period_days < MIN_DAYS_TO_EXTRAPOLATE:
        return {
            "current_pct":   current_pct,
            "target_pct":    target_pct,
            "monthly_labor": 0,
            "monthly_sales": 0,
            "target_labor":  0,
            "monthly_gap":   0,
            "over_target":   current_pct > target_pct,
            "period_days":   period_days,
            "projectable":   False,
            "reason":        (f"{period_days} day(s) of data — a monthly figure needs at least "
                              f"{MIN_DAYS_TO_EXTRAPOLATE}") if period_days else "no shift data",
        }

    scale         = 30.0 / period_days
    monthly_sales = total_sales * scale
    monthly_labor = total_labor * scale
    target_labor  = monthly_sales * (target_pct / 100)
    gap           = max(0, monthly_labor - target_labor)

    return {
        "current_pct":   current_pct,
        "target_pct":    target_pct,
        "monthly_labor": round(monthly_labor, 0),
        "monthly_sales": round(monthly_sales, 0),
        "target_labor":  round(target_labor, 0),
        "monthly_gap":   round(gap, 0),
        "over_target":   current_pct > target_pct,
        "period_days":   period_days,
        "projectable":   True,
    }


# ── Sales-based demand forecast ────────────────────────────────────────────────

def build_demand_forecast(restaurant_id: int, weeks: int = 8, db_path: str = None) -> dict:
    """Per-weekday sales expectation from this restaurant's own recent history.

    The scheduler already knew what a typical WEEK looks like in headcount
    terms (TYPICAL HEADCOUNT) and what the weather is doing, but nothing
    told it which days actually take the money. labor_daily_history has had
    per-day sales in it all along — from the Toast sync and from CSV
    uploads — so this reads the trailing `weeks` weeks, groups by weekday,
    and reports each weekday's median sales alongside how it compares to an
    average day.

    Median, not mean: one catered private event or one storm-closed
    Saturday shouldn't redefine what a normal Saturday looks like.

    Returns {"ok": False, ...} when there isn't enough history to say
    anything honest — the caller then simply omits the block rather than
    presenting a number built on two data points.
    """
    from models import get_conn as _gc
    try:
        conn = _gc(db_path) if db_path else _gc()
    except Exception:
        return {"ok": False, "reason": "no database"}

    try:
        rows = conn.execute("""
            SELECT day_of_week, sales FROM labor_daily_history
            WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0
              AND date >= date('now', ?)
            ORDER BY date DESC
        """, (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
    except Exception:
        return {"ok": False, "reason": "no history table"}
    finally:
        conn.close()

    by_day = {}
    for r in rows:
        day = (r["day_of_week"] or "").strip().capitalize()
        if day:
            by_day.setdefault(day, []).append(float(r["sales"] or 0))

    # At least three weekdays with two readings each — below that the
    # "typical" is really just "last week", which the model already sees.
    usable = {d: v for d, v in by_day.items() if len(v) >= 2}
    if len(usable) < 3:
        return {"ok": False, "reason": "not enough sales history yet",
                "days_with_data": len(usable)}

    def _median(values):
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    medians = {d: round(_median(v), 2) for d, v in usable.items()}
    overall = _median(list(medians.values()))
    if overall <= 0:
        return {"ok": False, "reason": "no usable sales figures"}

    days = []
    for day, med in medians.items():
        pct = int(round((med / overall - 1) * 100))
        days.append({
            "day": day,
            "median_sales": med,
            "samples": len(usable[day]),
            "vs_average_pct": pct,
        })
    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days.sort(key=lambda d: order.index(d["day"]) if d["day"] in order else 99)

    ranked = sorted(days, key=lambda d: d["median_sales"], reverse=True)
    return {
        "ok": True,
        "weeks": int(weeks),
        "overall_median": round(overall, 2),
        "days": days,
        "busiest": ranked[0]["day"],
        "quietest": ranked[-1]["day"],
    }


def format_demand_block(forecast: dict) -> str:
    """The prompt block for build_demand_forecast's output. Framed the same
    way the weather block is: a real signal, but one that adjusts staffing
    around the historical headcount and the minimum floors rather than
    replacing either."""
    if not forecast or not forecast.get("ok"):
        return ""
    lines = []
    for d in forecast["days"]:
        pct = d["vs_average_pct"]
        if pct > 4:
            rel = f"{pct}% above an average day"
        elif pct < -4:
            rel = f"{abs(pct)}% below an average day"
        else:
            rel = "about an average day"
        lines.append(f"  {d['day']}: ${int(d['median_sales']):,} typical sales — {rel} "
                     f"({d['samples']} recent {'week' if d['samples'] == 1 else 'weeks'})")
    return ("\n\nEXPECTED DEMAND BY DAY — this restaurant's own median sales per weekday over the "
            f"last {forecast['weeks']} weeks, so the schedule can put people where the money "
            f"actually is. {forecast['busiest']} is the busiest day and {forecast['quietest']} the "
            "quietest. Weight staffing toward the higher-demand days and trim the quiet ones, but "
            "treat this the same way as the weather block: it adjusts the TYPICAL HEADCOUNT "
            "starting point by a person or two per day, it does not replace it, and it never "
            "overrides the minimum staffing floors or a day's own coverage requirements. Median, "
            "not average, so a one-off private event or a storm-closed day hasn't skewed it.\n"
            + "\n".join(lines))


# ── Publishing a schedule to staff ─────────────────────────────────────────────

def employee_shifts_from_csv(schedule_csv: str, employee_name: str) -> list:
    """One employee's own shifts, pulled out of the generated schedule CSV.

    The CSV is the schedule's source of truth (see generate_optimized_schedule's
    header: date,day,employee,role,shift_start,shift_end,scheduled_hours,notes),
    so a staff-facing view reads from it rather than from a second copy that
    could drift. Matching is case- and whitespace-insensitive because names
    arrive from POS exports with inconsistent spacing.
    """
    import csv as _csv
    import io as _io

    target = (employee_name or "").strip().lower()
    if not schedule_csv or not target:
        return []
    shifts = []
    try:
        reader = _csv.DictReader(_io.StringIO(schedule_csv))
        for row in reader:
            name = (row.get("employee") or "").strip()
            if name.lower() != target:
                continue
            try:
                hours = float(row.get("scheduled_hours") or 0)
            except (TypeError, ValueError):
                hours = 0.0
            shifts.append({
                "date": (row.get("date") or "").strip(),
                "day": (row.get("day") or "").strip(),
                "role": (row.get("role") or "").strip(),
                "start": (row.get("shift_start") or "").strip(),
                "end": (row.get("shift_end") or "").strip(),
                "hours": round(hours, 2),
                "notes": (row.get("notes") or "").strip(),
            })
    except Exception:
        return []
    shifts.sort(key=lambda s: (s["date"], s["start"]))
    return shifts


def employees_in_schedule(schedule_csv: str) -> list:
    """Every distinct employee named in a generated schedule, in the order
    a person would read them (alphabetical)."""
    import csv as _csv
    import io as _io
    if not schedule_csv:
        return []
    seen = {}
    try:
        for row in _csv.DictReader(_io.StringIO(schedule_csv)):
            name = (row.get("employee") or "").strip()
            if name and name.lower() not in seen:
                seen[name.lower()] = name
    except Exception:
        return []
    return sorted(seen.values(), key=lambda n: n.lower())
