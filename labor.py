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
    is_live = bool(client_data and client_data.get("labor_csv"))
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
        elif labor_pct < 12 and d["sales"] > 4000:
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

    return {
        "total_labor_cost": round(total_labor, 2),
        "total_sales": round(total_sales, 2),
        "overall_labor_pct": overall_pct,
        "overstaffed_days": sorted(overstaffed, key=lambda x: x["labor_pct"], reverse=True),
        "understaffed_days": understaffed,
        "overtime_risk": overtime_flags,
        "dow_summary": dow_summary,
        "potential_savings": potential_savings,
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
                        owner_name: str = None) -> str:
    """Ask Claude to narrate labor findings in a warm, direct consultant tone."""
    greeting = f"{owner_name}," if owner_name else "Hi,"
    prompt = f"""You are the Cavnar AI Consultant — a friendly, experienced restaurant labor advisor.
You are writing a weekly labor summary for {owner_name or "the owner"} of {restaurant_name}.

Data:
- Overall labor cost: ${analysis['total_labor_cost']:,.0f} on ${analysis['total_sales']:,.0f} in sales ({analysis['overall_labor_pct']}% labor ratio)
- Industry target: 28-32% labor ratio
- Overstaffed days: {json.dumps(analysis['overstaffed_days'][:3])}
- Understaffed days: {json.dumps(analysis['understaffed_days'][:2])}
- Overtime risk: {json.dumps(analysis['overtime_risk'])}
- Labor % by day of week: {json.dumps(analysis['dow_summary'])}
- Estimated monthly savings with optimized scheduling: ${analysis['potential_savings']:,.0f}

Write a short consultant note structured exactly like this:

Opening paragraph: Start with "{greeting}" then give the honest overall picture with the key number. Call out 1-2 specific problem areas with actual dates and dollars, framed as opportunities.

Recommendations:
1. [First concrete actionable scheduling suggestion for this week]
2. [Second concrete actionable scheduling suggestion]
3. [Third concrete actionable suggestion with the savings opportunity]

Tone: warm, direct, and human — like a trusted advisor who knows the restaurant business.
Use the owner's name naturally once or twice. Be specific with numbers.
Do NOT use markdown, asterisks, bold formatting, or special characters.
The Recommendations section must start with exactly the word "Recommendations:" on its own line."""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    # Strip any markdown that slips through
    import re
    text = msg.content[0].text.strip()
    text = re.sub(r'\*\*(.+?)\*\*', r'', text)  # remove bold
    text = re.sub(r'\*(.+?)\*', r'', text)        # remove italic
    text = re.sub(r'#{1,6}\s', '', text)            # remove headers
    text = re.sub(r'^\s*[-•]\s', '', text, flags=re.MULTILINE)  # remove bullets
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
    today = datetime.now()
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
- Servers: typically 4-6h shifts, bartenders: 5-7h shifts
- Notes: one brief phrase explaining any change from normal (e.g. "reduced - slow Monday pattern" or "full coverage - high volume Friday")
- Include 6-10 shifts per day
- Start times between 10:00 and 18:00, end times between 15:00 and 23:00

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
