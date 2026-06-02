"""
labor.py — Labor cost analysis + Claude-powered scheduling recommendations
"""
import os, csv, json
from collections import defaultdict
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
DEFAULT_HOURLY_RATE = 26.0  # fallback if not set per client


def load_shifts(path: str = "sample_shifts.csv",
                csv_string: str = None) -> list[dict]:
    """Load shifts from a CSV string (client data) or file (sample/demo)."""
    if csv_string:
        import io
        return list(csv.DictReader(io.StringIO(csv_string)))
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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
    result = analyse_shifts(shifts, hourly_rate=rate, labor_target=target)
    result['is_live'] = is_live
    return result


def analyse_shifts(shifts: list[dict],
                   hourly_rate: float = DEFAULT_HOURLY_RATE,
                   labor_target: float = 30.0) -> dict:
    """Compute labor metrics from raw shift data."""
    HOURLY_RATE  = hourly_rate
    LABOR_TARGET = labor_target
    OVERSTAFF_THRESHOLD = labor_target  # flag any day over target
    by_day = defaultdict(lambda: {"scheduled": 0, "actual": 0, "sales": 0, "shifts": []})
    by_employee = defaultdict(lambda: {"scheduled": 0, "actual": 0, "shifts": 0})
    by_dayofweek = defaultdict(lambda: {"labor_cost": 0, "sales": 0, "count": 0})
    overtime_flags = []

    for s in shifts:
        day    = s["date"]
        dow    = s["day"]
        emp    = s["employee"]
        sched  = float(s["scheduled_hours"])
        actual = float(s["actual_hours"])
        sales  = float(s["sales_that_day"])

        by_day[day]["scheduled"] += sched
        by_day[day]["actual"]    += actual
        by_day[day]["sales"]     = sales
        by_day[day]["shifts"].append(s)

        by_employee[emp]["scheduled"] += sched
        by_employee[emp]["actual"]    += actual
        by_employee[emp]["shifts"]    += 1

        labor_cost = actual * HOURLY_RATE
        by_dayofweek[dow]["labor_cost"] += labor_cost
        by_dayofweek[dow]["sales"]      += sales
        by_dayofweek[dow]["count"]      += 1

    # Find overstaffed days (labor % > 35% of sales)
    overstaffed = []
    understaffed = []
    for date, d in by_day.items():
        labor_cost = d["actual"] * HOURLY_RATE
        labor_pct  = (labor_cost / d["sales"] * 100) if d["sales"] else 0
        d["labor_cost"] = round(labor_cost, 2)
        d["labor_pct"]  = round(labor_pct, 1)
        if labor_pct > OVERSTAFF_THRESHOLD:
            # Format date as M/D/YY
            try:
                from datetime import datetime as _dt
                fmt_date = _dt.strptime(date, "%Y-%m-%d").strftime("%-m/%-d/%y")
            except Exception:
                fmt_date = date
            overstaffed.append({"date": fmt_date, "day": d["shifts"][0]["day"],
                                 "labor_pct": round(labor_pct, 1),
                                 "labor_cost": round(labor_cost, 2),
                                 "sales": d["sales"]})
        elif labor_pct < 18 and d["sales"] > 2500:
            try:
                from datetime import datetime as _dt
                fmt_date = _dt.strptime(date, "%Y-%m-%d").strftime("%-m/%-d/%y")
            except Exception:
                fmt_date = date
            understaffed.append({"date": fmt_date, "day": d["shifts"][0]["day"],
                                  "labor_pct": round(labor_pct, 1), "sales": d["sales"]})

    # Overtime risk — bucket by week, flag anyone who hit 40h in any single week
    weekly_hours = {}  # {employee: {week_num: hours}}
    for s in shifts:
        emp    = s["employee"]
        actual = float(s["actual_hours"])
        try:
            from datetime import datetime as _dt
            week_num = _dt.strptime(s["date"], "%Y-%m-%d").isocalendar()[1]
        except Exception:
            week_num = 0
        if emp not in weekly_hours:
            weekly_hours[emp] = {}
        weekly_hours[emp][week_num] = weekly_hours[emp].get(week_num, 0) + actual

    for emp, weeks in weekly_hours.items():
        for wk, hrs in weeks.items():
            if hrs > 40:
                overtime_flags.append({
                    "employee": emp,
                    "hours": round(hrs, 1),
                    "week": f"Week {wk}",
                    "status": "overtime"
                })
                break  # only flag once per employee
        else:
            # Check if any week is close (37-40h)
            max_hrs = max(weeks.values())
            if 37 <= max_hrs <= 40:
                overtime_flags.append({
                    "employee": emp,
                    "hours": round(max_hrs, 1),
                    "week": f"Week {max(weeks, key=weeks.get)}",
                    "status": "near"
                })

    # Avg labor % by day of week — average across all occurrences of each day
    dow_summary = {}
    dow_daily = {}  # accumulate per-day labor and sales
    for date, d in by_day.items():
        day_name = d["shifts"][0]["day"] if d.get("shifts") else None
        if not day_name:
            continue
        labor_cost = d["actual"] * HOURLY_RATE
        sales = d["sales"]
        if day_name not in dow_daily:
            dow_daily[day_name] = {"labor": 0, "sales": 0, "count": 0}
        dow_daily[day_name]["labor"] += labor_cost
        dow_daily[day_name]["sales"] += sales
        dow_daily[day_name]["count"] += 1

    for day_name, d in dow_daily.items():
        avg_pct = (d["labor"] / d["sales"] * 100) if d["sales"] else 0
        dow_summary[day_name] = round(avg_pct, 1)

    total_labor  = sum(s["actual"] * HOURLY_RATE for s in
                       [{"actual": float(x["actual_hours"])} for x in shifts])
    total_sales  = sum(float(s["sales_that_day"]) for s in
                       {s["date"]: s for s in shifts}.values())
    overall_pct  = round(total_labor / total_sales * 100, 1) if total_sales else 0
    target_labor_cost = total_sales * (LABOR_TARGET / 100)
    potential_savings = round(max(0, total_labor - target_labor_cost) * 2, 2)  # x2 to project monthly

    # Role-level breakdown
    by_role = defaultdict(lambda: {"hours": 0, "labor_cost": 0, "headcount": set()})
    for s in shifts:
        role = s.get("role", "Unknown")
        actual = float(s["actual_hours"])
        by_role[role]["hours"] += actual
        by_role[role]["labor_cost"] += actual * HOURLY_RATE
        by_role[role]["headcount"].add(s["employee"])
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
            "start": min(by_day.keys()) if by_day else None,
            "end":   max(by_day.keys()) if by_day else None,
            "days":  len(by_day),
        },
    }


def get_claude_insights(analysis: dict, restaurant_name: str = "your restaurant",
                        owner_name: str = None, restaurant_id: int = None) -> str:
    """Ask Claude to narrate labor findings in a warm, direct consultant tone."""
    greeting = f"{owner_name}," if owner_name else "Hi,"
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _now_dt
        today_labor = _now_dt.now(ZoneInfo('America/Chicago')).strftime("%B %d, %Y")
    except Exception:
        from datetime import datetime as _now_dt
        today_labor = _now_dt.now().strftime("%B %d, %Y")

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
        from datetime import datetime as _dt_h
        from zoneinfo import ZoneInfo as _ZI_h
        _upcoming = _get_hols(_dt_h.now(_ZI_h('America/Chicago')).replace(tzinfo=None))
        holiday_context = f"\n- Upcoming holidays (affects scheduling): {_upcoming}" if _upcoming else ""
    except Exception:
        holiday_context = ""

    prompt = f"""You are the Cavnar AI Consultant — a friendly, experienced restaurant labor advisor.
You are writing a weekly labor summary for {owner_name or "the owner"} of {restaurant_name}.
Today's date: {today_labor}{upload_context}{holiday_context}

Data:
- Overall labor cost: ${analysis['total_labor_cost']:,.0f} on ${analysis['total_sales']:,.0f} in sales ({analysis['overall_labor_pct']}% labor ratio)
- Industry target: 28-32% labor ratio
- Overstaffed days: {json.dumps(analysis['overstaffed_days'][:3])}
- Understaffed days: {json.dumps(analysis['understaffed_days'][:2])}
- Overtime risk: {json.dumps(analysis['overtime_risk'])}{role_context}{trend_context}
- Labor % by day of week: {json.dumps(analysis['dow_summary'])}
- Estimated monthly savings with optimized scheduling: ${analysis['potential_savings']:,.0f}

Write a short consultant note structured exactly like this:

Opening paragraph: Start with "{greeting}" then give the honest overall picture with the key number. Call out 1-2 specific problem areas with actual dates and dollars, framed as opportunities.

Recommendations:
1. [First concrete actionable scheduling suggestion for this week — one sentence]
2. [Second concrete actionable scheduling suggestion — one sentence]
3. [Third actionable suggestion. End this recommendation with one short warm closing sentence on the same line, separated by a space. Do not add a 4th item.]

Tone: warm, direct, human. Use the owner name once or twice. Be specific with numbers.
Do NOT use markdown, asterisks, bold, or special characters.
There must be EXACTLY 3 numbered recommendations and nothing after number 3.
The Recommendations section must start with exactly the word "Recommendations:" on its own line."""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    # Strip any markdown that slips through
    import re
    text = msg.content[0].text.strip()
    text = re.sub('\\*\\*(.+?)\\*\\*', lambda m: m.group(1), text)
    text = re.sub('\\*(.+?)\\*',   lambda m: m.group(1), text)
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'^\s*[-•]\s', '', text, flags=re.MULTILINE)
    text = re.sub(r'(?<![\$\d\-\/])(\d{3,}(?:,\d{3})*(?:\.\d+)?)(?![\-\/\d])', r'$\1', text)
    return text


def generate_optimized_schedule(analysis: dict, shifts: list[dict],
                                 restaurant_name: str = "Restaurant",
                                 hourly_rate: float = DEFAULT_HOURLY_RATE,
                                 owner_name: str = None,
                                 staff_notes: list = None,
                                 labor_target: float = 30.0) -> str:
    """Use Claude to generate an optimized weekly schedule as CSV."""
    from datetime import datetime, timedelta

    # Get unique roles and employees with their typical hours
    emp_hours = analysis.get("employee_hours", {})
    employees = list({s["employee"]: s["role"] for s in shifts}.items())
    overstaffed = analysis.get("overstaffed_days", [])[:5]
    understaffed = analysis.get("understaffed_days", [])[:3]
    dow = analysis.get("dow_summary", {})

    # Next Monday as schedule start
    from zoneinfo import ZoneInfo as _ZI_sched
    today = datetime.now(_ZI_sched('America/Chicago')).replace(tzinfo=None)
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    week_dates = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    week_days  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    # Build staff constraints string
    constraints = ""
    if staff_notes:
        constraints = "\nStaff scheduling constraints (MUST be respected):\n"
        for note in staff_notes:
            constraints += f"- {note['employee_name']}: {note['notes']}\n"

    prompt = f"""You are a restaurant scheduling expert for {restaurant_name}. Generate a realistic, optimized schedule for next week.

Current labor data:
- Overall labor ratio: {analysis["overall_labor_pct"]}% (target: {labor_target}%)
- Monthly labor overspend: ${analysis.get("potential_savings",0):,.0f} estimated recoverable
- Overstaffed patterns: {[d["day"] + " avg " + str(d["labor_pct"]) + "%" for d in overstaffed]}
- Understaffed patterns: {[d["day"] for d in understaffed]}
- Labor % by day: {dow}
- Blended hourly rate: ${hourly_rate}/hr
- Active staff: {[e[0] + " (" + e[1] + ")" for e in employees[:15]]}
{constraints}

Schedule dates for next week:
{chr(10).join(f"- {d}: {n}" for d, n in zip(week_dates, week_days))}

Generate a CSV with these EXACT columns (no other text, no markdown, just CSV):
date,day,employee,role,shift_start,shift_end,scheduled_hours,notes

Requirements:
- Use the exact dates listed above
- Use real employee names from the staff list
- Reduce coverage on historically overstaffed days by 10-15%
- Maintain full coverage on high-volume days (Fri/Sat typically)
- No employee over 40 hours for the week (overtime threshold)
- Target labor ratio for each day: {labor_target}%
- Servers: typically 4-6h shifts, bartenders/cooks: 5-7h shifts
- Notes: one brief phrase explaining any change from normal (e.g. "reduced - slow Monday pattern" or "full coverage - high volume Friday")
- Include 6-10 shifts per day
- Infer appropriate shift hours from the existing staff data provided. If the restaurant appears to be breakfast/brunch (early shifts, short hours), use start times between 06:00-10:00 and end times between 12:00-16:00. For lunch/dinner operations use 10:00-14:00 starts and 15:00-23:00 ends. Match the pattern in the actual shift data above.

Output ONLY the CSV rows including header. No explanation."""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=2500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


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
