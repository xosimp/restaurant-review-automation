"""
labor.py — Labor cost analysis + Claude-powered scheduling recommendations
"""
import os, csv, json
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
    result = analyse_shifts(shifts, hourly_rate=blended, labor_target=target, role_rates=role_rates)
    result['is_live'] = is_live
    result['blended_rate'] = blended
    result['role_rates'] = {k: v for k, v in role_rates.items() if k != "_default"}
    return result


def _shift_rate(shift: dict, role_rates: dict, fallback: float) -> float:
    """Return the hourly rate for a single shift based on role."""
    default = role_rates.get("_default", fallback)
    return role_rates.get(shift.get("role", ""), default)


def analyse_shifts(shifts: list[dict],
                   hourly_rate: float = DEFAULT_HOURLY_RATE,
                   labor_target: float = 30.0,
                   role_rates: dict = None) -> dict:
    """Compute labor metrics from raw shift data."""
    if role_rates is None:
        role_rates = {"_default": hourly_rate}
    LABOR_TARGET = labor_target
    OVERSTAFF_THRESHOLD = labor_target
    by_day = defaultdict(lambda: {"scheduled": 0, "actual": 0, "sales": 0, "shifts": [], "labor_cost": 0})
    by_employee = defaultdict(lambda: {"scheduled": 0, "actual": 0, "shifts": 0})
    overtime_flags = []

    for s in shifts:
        day    = s.get("date") or ""
        emp    = s.get("employee") or "Unknown"
        sched  = float(s.get("scheduled_hours") or 0)
        actual = float(s.get("actual_hours") or 0)
        sales  = float(s.get("sales_that_day") or s.get("sales") or 0)
        rate   = _shift_rate(s, role_rates, hourly_rate)

        by_day[day]["scheduled"] += sched
        by_day[day]["actual"]    += actual
        by_day[day]["sales"]     = sales
        by_day[day]["shifts"].append(s)
        by_day[day]["labor_cost"] += actual * rate

        by_employee[emp]["scheduled"] += sched
        by_employee[emp]["actual"]    += actual
        by_employee[emp]["shifts"]    += 1

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

    # Overtime risk — bucket by week, flag anyone who hit 40h in any single week
    weekly_hours = {}  # {employee: {week_num: hours}}
    for s in shifts:
        emp    = s["employee"]
        actual = float(s["actual_hours"])
        try:
            _d = datetime.strptime(s["date"], "%Y-%m-%d")
            # Store Monday of the week as key so we can show "Week of Jun 8"
            week_key = (_d - timedelta(days=_d.weekday())).strftime("%Y-%m-%d")
        except Exception:
            week_key = s.get("date", "unknown")
        if emp not in weekly_hours:
            weekly_hours[emp] = {}
        weekly_hours[emp][week_key] = weekly_hours[emp].get(week_key, 0) + actual

    for emp, weeks in weekly_hours.items():
        for wk, hrs in weeks.items():
            if hrs > 40:
                try:
                    _wk_label = datetime.strptime(wk, "%Y-%m-%d").strftime("%b %-d")
                except Exception:
                    _wk_label = str(wk)
                overtime_flags.append({
                    "employee": emp,
                    "hours": round(hrs, 1),
                    "week": _wk_label,
                    "status": "overtime"
                })
                break  # only flag once per employee
        else:
            # Check if any week is close (37-40h)
            max_hrs = max(weeks.values())
            if 37 <= max_hrs <= 40:
                _best_wk = max(weeks, key=weeks.get)
                try:
                    _wk_label2 = datetime.strptime(_best_wk, "%Y-%m-%d").strftime("%b %-d")
                except Exception:
                    _wk_label2 = str(_best_wk)
                overtime_flags.append({
                    "employee": emp,
                    "hours": round(max_hrs, 1),
                    "week": _wk_label2,
                    "status": "near"
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
    total_sales  = sum(float(s.get("sales_that_day") or s.get("sales") or 0) for s in
                       {s["date"]: s for s in shifts}.values())
    overall_pct  = round(total_labor / total_sales * 100, 1) if total_sales else 0
    target_labor_cost = total_sales * (LABOR_TARGET / 100)
    potential_savings = round(max(0, total_labor - target_labor_cost), 2)

    # Role-level breakdown
    by_role = defaultdict(lambda: {"hours": 0, "labor_cost": 0, "headcount": set()})
    for s in shifts:
        role = s.get("role", "Unknown")
        actual = float(s.get("actual_hours") or 0)
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
        "role_summary": role_summary,
        "by_day": {k: {kk: vv for kk, vv in v.items() if kk != "shifts"}
                   for k, v in by_day.items()},
        "employee_hours": {k: dict(v) for k, v in by_employee.items()},
        "labor_target": LABOR_TARGET,
        "date_range": {
            "start": min((k for k in by_day.keys() if k), default=None),
            "end":   max((k for k in by_day.keys() if k), default=None),
            "days":  len(by_day),
        },
    }


def get_claude_insights(analysis: dict, restaurant_name: str = "your restaurant",
                        owner_name: str = None, restaurant_id: int = None,
                        staff_notes: list = None) -> str:
    """Ask Claude to narrate labor findings in a warm, direct consultant tone."""
    greeting = f"{owner_name}," if owner_name else "Hi,"
    from time_utils import restaurant_now_by_id
    _local_now = restaurant_now_by_id(restaurant_id) if restaurant_id else datetime.now(ZoneInfo('America/Chicago'))
    today_labor = _local_now.strftime("%B %d, %Y")

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
                # Check if trending up or down
                if len(history) >= 2:
                    has_trend = True
                    diff = analysis['overall_labor_pct'] - history[0]['labor_pct']
                    if abs(diff) >= 1:
                        direction = "UP" if diff > 0 else "DOWN"
                        trend_context += f"\n- TREND: Labor % is {direction} {abs(diff):.1f} points from last upload — mention this trend explicitly"
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
- Understaffed days (IMPORTANT — these are NOT good days despite low labor %): {json.dumps(analysis['understaffed_days'][:2])} — these days had strong sales but lean staffing, meaning the restaurant likely left revenue on the table through slower service, longer waits, or missed covers. Flag these explicitly as missed revenue opportunities and recommend adding 1-2 staff on these days going forward.
- Overtime risk: {json.dumps(analysis['overtime_risk'])}{role_context}{trend_context}
- Labor % by day of week: {json.dumps(analysis['dow_summary'])}
- Estimated monthly savings with optimized scheduling: ${analysis['potential_savings']:,.0f}{constraints_context}

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
    text = re.sub('\\*\\*(.+?)\\*\\*', lambda m: m.group(1), text)
    text = re.sub('\\*(.+?)\\*',   lambda m: m.group(1), text)
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'^\s*[-•]\s', '', text, flags=re.MULTILINE)
    return text


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
                                 prior_schedule_summary: dict = None) -> dict:
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
    employees = list({s["employee"]: s["role"] for s in shifts}.items())
    overstaffed = analysis.get("overstaffed_days", [])[:5]
    understaffed = analysis.get("understaffed_days", [])[:3]
    dow = analysis.get("dow_summary", {})

    # Compute no-show risk per DOW from shifts where actual_hours is 0 (employee didn't work)
    _noshows = {}
    _dow_shift_counts = {}
    for s in shifts:
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
        """3pm cutoff, matching the mobile app's own morning/night split."""
        try:
            _t = _dt2.strptime((shift_start or "").strip().lower(), "%I:%M%p")
            return "night" if _t.hour >= 15 else "morning"
        except Exception:
            return "night"

    # {dow -> {daypart -> {role -> set of employees}}}
    _dow_daypart_role_staff = _dd(lambda: _dd(lambda: _dd(set)))
    for s in shifts:
        _date = s.get("date", "")
        _role = s.get("role", "Unknown")
        _emp  = s.get("employee", "")
        _dn   = ""
        try:
            _dn = _dt2.strptime(_date, "%Y-%m-%d").strftime("%A")
        except Exception:
            _dn = s.get("day", "")
        if _dn and _emp:
            _dow_daypart_role_staff[_dn][_daypart(s.get("shift_start", ""))][_role].add(_emp)
    # Count unique dates per DOW (divisor for "avg per week")
    _dow_date_sets = _dd(set)
    for s in shifts:
        _date = s.get("date", "")
        try:
            _dn = _dt2.strptime(_date, "%Y-%m-%d").strftime("%A")
        except Exception:
            _dn = s.get("day", "")
        if _dn and _date:
            _dow_date_sets[_dn].add(_date)
    # Build headcount block: "Friday: Server 3 morning / 6 night, Cook 2 morning / 3 night"
    _dow_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    _hc_lines = []
    for _dn in _dow_order:
        if _dn not in _dow_daypart_role_staff:
            continue
        _n_weeks = max(len(_dow_date_sets[_dn]), 1)
        _roles_seen = sorted({r for _dp in _dow_daypart_role_staff[_dn].values() for r in _dp})
        _parts = []
        for _role in _roles_seen:
            _morning_emps = _dow_daypart_role_staff[_dn]["morning"].get(_role, set())
            _night_emps = _dow_daypart_role_staff[_dn]["night"].get(_role, set())
            _m_avg = round(len(_morning_emps) / _n_weeks)
            _n_avg = round(len(_night_emps) / _n_weeks)
            if _m_avg and _n_avg:
                _parts.append(f"{_role}: {_m_avg} morning / {_n_avg} night")
            elif _n_avg:
                _parts.append(f"{_role}: {_n_avg} night")
            elif _m_avg:
                _parts.append(f"{_role}: {_m_avg} morning")
        if _parts:
            _hc_lines.append(f"  {_dn}: {', '.join(_parts)}")
    _headcount_block = ""
    if _hc_lines:
        _headcount_block = ("\n\nTYPICAL HEADCOUNT PER DAY — your starting point for who/how many per role per "
                            "day, split by daypart. IMPORTANT: morning and night are SEPARATE headcounts, not "
                            "a combined daily total to divide between them — \"6 night\" means 6 people ON AT "
                            "NIGHT, on top of (not instead of) whatever the morning figure says. Never read a "
                            "day's total as one pool to split across dayparts.\n"
                            "Use these as the baseline. A reason to go over is an event, a YoY spike, OR "
                            "the PAR HOURS TARGET below being significantly out of reach at this headcount — see "
                            "that section's reconciliation rule. If you do scale up for that reason, do it "
                            "proportionally across roles (not by piling extra hours onto one role) and say so "
                            "plainly in your summary, e.g. \"Added ~1 server/day vs. the on-file staffing "
                            "pattern — that pattern looked short for this week's revenue target.\" Don't invent "
                            "a reason that isn't true; if the historical numbers already support the target, "
                            "stay within them:\n"
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
        projected_revenue = analysis.get("total_sales", 0) * (7 / max(len(set(s.get("date","") for s in shifts if s.get("date"))), 1))
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
    if yoy_context:
        _yoy_total = sum(float(r.get("yoy_hours") or 0) for r in yoy_context)
        if _yoy_total > 0:
            _scale = hours_budget / _yoy_total
            _day_lines = []
            for _r in yoy_context:
                _yh = float(_r.get("yoy_hours") or 0)
                if _yh:
                    _target_h = round(_yh * _scale, 1)
                    _day_lines.append(f"    {_r['next_week_dow']} {_r['next_week_date']}: {_target_h}h")
                    _daily_target_map[_r['next_week_date']] = _target_h
            if _day_lines:
                _daily_targets = "\n  Per-day targets (YoY scaled to PAR):\n" + "\n".join(_day_lines)

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
        _hist_by_dow: dict = {}
        for _date, _d in (analysis.get("by_day") or {}).items():
            try:
                _dow = datetime.strptime(_date, "%Y-%m-%d").strftime("%A")
            except (ValueError, TypeError):
                continue
            _hist_by_dow[_dow] = _hist_by_dow.get(_dow, 0.0) + float(_d.get("actual") or 0)
        _hist_total = sum(_hist_by_dow.values())
        if _hist_total > 0:
            _scale2 = hours_budget / _hist_total
            _day_lines2 = []
            for _wd, _wdate in zip(week_days, week_dates):
                _h = _hist_by_dow.get(_wd, 0.0)
                if _h:
                    _target_h2 = round(_h * _scale2, 1)
                    _day_lines2.append(f"    {_wd} {_wdate}: {_target_h2}h")
                    _daily_target_map[_wdate] = _target_h2
            if _day_lines2:
                _daily_targets = ("\n  Per-day targets (historical hours-by-weekday scaled to PAR — no YoY "
                                   "data available, so this is this restaurant's own recent day-of-week pattern "
                                   "scaled up to the budget instead):\n" + "\n".join(_day_lines2))

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

    # Extra scheduling notes from admin
    _sched_notes_block = ""
    if sched_notes:
        _sched_notes_block = f"\n\nADDITIONAL SCHEDULING NOTES (from management):\n{sched_notes}"

    par_block = (f"\n\nPAR HOURS TARGET — schedule is verified against actual column totals:\n"
                 f"  Projected revenue: ${projected_revenue:,.0f} | Labor target: {labor_target}% = ${labor_budget_dollars:,.0f}\n"
                 f"  Blended rate: ${hourly_rate}/hr → target {hours_budget}h total (±10h acceptable)\n"
                 f"  RECONCILIATION — this target is a real revenue-driven budget, not a suggestion. If TYPICAL "
                 f"HEADCOUNT and the per-day YoY targets together land you within about 15% of {hours_budget}h, "
                 f"use them as-is. If they would leave you MORE than 15% under budget, that gap means the "
                 f"historical staffing data doesn't reflect what this restaurant can now afford — close most of "
                 f"the gap by scaling headcount up proportionally across roles (shift-length extensions alone "
                 f"have an obvious ceiling and usually can't close a gap that size).\n"
                 f"  Spread the added headcount across EVERY day of the week, not just the days that were "
                 f"already busiest. Closing this gap by only extending Thursday/Friday/Saturday and Pizza "
                 f"Monday leaves the ordinary weekdays (Tuesday, Wednesday) exactly as thin as before — that is "
                 f"concentrating the gap, not closing it. Before finalizing, check every day against the "
                 f"MINIMUM STAFFING FLOORS above individually — if a normal Tuesday or Wednesday is still sitting "
                 f"at or near those floors while Thursday-Saturday are well above them, that is a sign the added "
                 f"hours went to the wrong days.\n"
                 f"  State the gap and your adjustment explicitly in the summary. Never land silently far under "
                 f"budget with no explanation — if you genuinely cannot close it, say why in the summary instead "
                 f"of leaving it unaddressed. The hours target is never a lower priority than TYPICAL HEADCOUNT "
                 f"or the per-day YoY targets — when they conflict, the hours target is what "
                 f"governs.{_daily_targets}")

    prompt = f"""You are a restaurant scheduling expert for {restaurant_name}. Generate an optimized schedule for next week AND a brief plain-English summary of your decisions.

CONTEXT:
- Current overall labor: {analysis["overall_labor_pct"]}% (target: {labor_target}%)
- Blended hourly rate: ${hourly_rate}/hr
- Recent overstaffed days: {[d["day"] + " (" + str(d["labor_pct"]) + "%)" for d in overstaffed]}
- Recent understaffed days: {[d["day"] for d in understaffed]}
- Recent labor % by day of week: {dow}
- Active staff: {[e[0] + " (" + e[1] + ")" for e in employees[:100]]}{yoy_block}{events_block}{_demand_block}{_weather_block}{_prior_schedule_block}{role_rates_block}{hours_block}{par_block}{_headcount_block}{_cross_block}{_section_block}{_daypart_block}{_delivery_block}{_noshows_block}{_avail_block}{_sched_notes_block}

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
- Weekly hours target is {hours_budget}h (±10h). Prefer shift length and later start times to close small gaps. If historical headcount plus reasonable shift lengths would still leave you more than ~15% under this target, add headcount proportionally across roles instead of leaving the gap unaddressed (see PAR HOURS TARGET reconciliation above) — but never add headcount for a gap that shift-length adjustments could already close.

ROLE STAGGER RULE (universal — applies to every restaurant):
- Never schedule two employees in the same role at the exact same start time. The first person opens; additional staff stagger in based on volume. Add headcount when YoY data or a flagged event justifies it, or when closing a >15% PAR hours gap requires it (see PAR HOURS TARGET reconciliation above).

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

- Shifts per day: use the TYPICAL HEADCOUNT block as your starting point (see that block for when to scale beyond it — high-volume YoY days, flagged events, or closing a >15% PAR hours gap). Use CROSS-TRAINED STAFF to fill role gaps before adding new headcount.
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
    }


def calculate_monthly_gap(analysis: dict) -> dict:
    """Calculate the dollar gap between current and target labor %."""
    current_pct = analysis["overall_labor_pct"]
    total_sales  = analysis["total_sales"]
    total_labor  = analysis["total_labor_cost"]
    target_pct   = analysis.get("labor_target", 30.0)

    # Extrapolate to monthly (data covers ~2 weeks)
    monthly_sales = total_sales * 2
    monthly_labor = total_labor * 2
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
