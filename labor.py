"""
labor.py — Labor cost analysis + Claude-powered scheduling recommendations
"""
import os, csv, json
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import anthropic

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
        day    = s.get("date") or ""
        dow    = s.get("day") or ""
        emp    = s.get("employee") or "Unknown"
        sched  = float(s.get("scheduled_hours") or 0)
        actual = float(s.get("actual_hours") or 0)
        sales  = float(s.get("sales_that_day") or s.get("sales") or 0)

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
    total_sales  = sum(float(s.get("sales_that_day") or s.get("sales") or 0) for s in
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
    today_labor = datetime.now(ZoneInfo('America/Chicago')).strftime("%B %d, %Y")

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
        _upcoming = _get_hols(datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None))
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

    prompt = f"""You are the Cavnar AI Consultant — a friendly, experienced restaurant labor advisor.
You are writing a weekly labor summary for {owner_name or "the owner"} of {restaurant_name}.
Today's date: {today_labor}{upload_context}{holiday_context}

Data:
- Overall labor cost: ${analysis['total_labor_cost']:,.0f} on ${analysis['total_sales']:,.0f} in sales ({analysis['overall_labor_pct']}% labor ratio)
- Industry target: 28-32% labor ratio
- Overstaffed days: {json.dumps(analysis['overstaffed_days'][:3])}
- Understaffed days (IMPORTANT — these are NOT good days despite low labor %): {json.dumps(analysis['understaffed_days'][:2])} — these days had strong sales but lean staffing, meaning the restaurant likely left revenue on the table through slower service, longer waits, or missed covers. Flag these explicitly as missed revenue opportunities and recommend adding 1-2 staff on these days going forward.
- Overtime risk: {json.dumps(analysis['overtime_risk'])}{role_context}{trend_context}
- Labor % by day of week: {json.dumps(analysis['dow_summary'])}
- Estimated monthly savings with optimized scheduling: ${analysis['potential_savings']:,.0f}{constraints_context}

Write a short consultant note structured exactly like this:

Opening paragraph: Start with "{greeting}" then give the honest overall picture with the key number. Call out 1-2 specific problem areas with actual dates and dollars, framed as opportunities.

Recommendations:
1. [First concrete actionable scheduling suggestion for this week — one sentence]
2. [Second concrete actionable scheduling suggestion — one sentence]
3. [Third actionable suggestion. End this recommendation with one short warm closing sentence on the same line, separated by a space. Do not add a 4th item.]

Tone: warm, direct, human. Use the owner name once or twice. Be specific with numbers.
Always use $ signs before dollar amounts (e.g. $2,400 not 2400 or 2,400).
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
    return text


def generate_optimized_schedule(analysis: dict, shifts: list[dict],
                                 restaurant_name: str = "Restaurant",
                                 hourly_rate: float = DEFAULT_HOURLY_RATE,
                                 owner_name: str = None,
                                 staff_notes: list = None,
                                 labor_target: float = 30.0,
                                 yoy_context: list = None,
                                 upcoming_events: list = None) -> dict:
    """
    Use Claude to generate an optimized weekly schedule.
    Returns dict: {schedule_csv: str, summary: list[str], week_dates: list, week_days: list}
    """
    employees = list({s["employee"]: s["role"] for s in shifts}.items())
    overstaffed = analysis.get("overstaffed_days", [])[:5]
    understaffed = analysis.get("understaffed_days", [])[:3]
    dow = analysis.get("dow_summary", {})

    # Next Monday as schedule start
    today = datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None)
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_ahead)
    week_dates = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    week_days  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

    # Build staff constraints block
    constraints = ""
    if staff_notes:
        constraints = "\nStaff scheduling constraints (MUST be respected):\n"
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

    # Compute PAR hours budget from YoY projected revenue (or fall back to recent)
    projected_revenue = 0.0
    if yoy_context:
        yoy_sales = [r["yoy_sales"] for r in yoy_context if r.get("yoy_sales")]
        if yoy_sales:
            projected_revenue = sum(yoy_sales)
    if not projected_revenue:
        projected_revenue = analysis.get("total_sales", 0) * (7 / max(len(set(s.get("date","") for s in shifts if s.get("date"))), 1))
    hours_budget = round((projected_revenue * (labor_target / 100)) / hourly_rate, 1) if hourly_rate else 0
    labor_budget_dollars = round(projected_revenue * (labor_target / 100), 0)

    par_block = (f"\n\nPAR HOURS TARGET (non-negotiable):\n"
                 f"  Projected revenue this week: ${projected_revenue:,.0f}\n"
                 f"  Labor target: {labor_target}% = ${labor_budget_dollars:,.0f} labor budget\n"
                 f"  At ${hourly_rate}/hr blended rate → schedule EXACTLY {hours_budget}h total across all shifts\n"
                 f"  Build the schedule to land within ±5h of this number. Show total hours in summary.")

    prompt = f"""You are a restaurant scheduling expert for {restaurant_name}. Generate an optimized schedule for next week AND a brief plain-English summary of your decisions.

CONTEXT:
- Current overall labor: {analysis["overall_labor_pct"]}% (target: {labor_target}%)
- Blended hourly rate: ${hourly_rate}/hr
- Recent overstaffed days: {[d["day"] + " (" + str(d["labor_pct"]) + "%)" for d in overstaffed]}
- Recent understaffed days: {[d["day"] for d in understaffed]}
- Recent labor % by day of week: {dow}
- Active staff: {[e[0] + " (" + e[1] + ")" for e in employees[:15]]}{yoy_block}{events_block}{par_block}{constraints}

Next week dates:
{chr(10).join(f"- {d}: {n}" for d, n in zip(week_dates, week_days))}

OUTPUT FORMAT — two sections separated by exactly "---SUMMARY---":

Section 1: CSV schedule
date,day,employee,role,shift_start,shift_end,scheduled_hours,notes

Section 2: After "---SUMMARY---", write exactly 3 bullet points (start each with "- ") explaining the key scheduling decisions you made. Be specific: name the days, total hours scheduled vs PAR target, and why (reference the YoY data or event if relevant). No markdown headers, no asterisks.

SCHEDULING RULES:
- Use exact dates listed above and real employee names from the staff list
- Base each day's staffing on the YoY same-day data when available — that is your primary projection
- For holiday weeks, match staffing to last year's holiday labor hours, not recent averages
- No employee over 40h for the week
- Total weekly hours MUST be within ±5h of the PAR target ({hours_budget}h)
- Servers: 4-6h shifts; bartenders/cooks: 5-8h shifts
- 6-10 shifts per day
- Notes column: one brief phrase per shift explaining any change (e.g. "YoY match - high Father's Day volume" or "reduced - YoY shows slow Monday")
- Match shift times to the operation type visible in the staff data (lunch/dinner vs breakfast/brunch)"""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()

    # Split on the summary delimiter
    if "---SUMMARY---" in raw:
        csv_part, summary_part = raw.split("---SUMMARY---", 1)
    else:
        csv_part = raw
        summary_part = ""

    # Clean CSV — find the header row first, then keep only data rows after it
    import re as _re_sched
    EXPECTED_HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
    all_lines = [l for l in csv_part.split("\n")
                 if l.strip() and not l.startswith("#") and not l.startswith("```")]
    # Find header (case-insensitive match on known columns)
    header_idx = None
    for idx, line in enumerate(all_lines):
        low = line.lower().replace(" ", "")
        if "date" in low and "employee" in low and "shift" in low:
            header_idx = idx
            # Normalize header to our canonical form
            all_lines[idx] = EXPECTED_HEADER
            break
    if header_idx is not None:
        csv_lines = [all_lines[header_idx]] + [
            l for l in all_lines[header_idx + 1:]
            if "," in l and not l.lower().startswith("date,")
        ]
    else:
        csv_lines = all_lines
    csv_clean = "\n".join(csv_lines)

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
