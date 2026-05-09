"""
labor.py — Labor cost analysis + Claude-powered scheduling recommendations
"""
import os, csv, json
from collections import defaultdict
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
HOURLY_RATE = 26.0  # avg blended rate, configurable


def load_shifts(path: str = "sample_shifts.csv") -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def analyse_shifts(shifts: list[dict]) -> dict:
    """Compute labor metrics from raw shift data."""
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
        if labor_pct > 20:
            overstaffed.append({"date": date, "day": d["shifts"][0]["day"],
                                 "labor_pct": round(labor_pct, 1),
                                 "labor_cost": round(labor_cost, 2),
                                 "sales": d["sales"]})
        elif labor_pct < 12 and d["sales"] > 4000:
            understaffed.append({"date": date, "day": d["shifts"][0]["day"],
                                  "labor_pct": round(labor_pct, 1), "sales": d["sales"]})

    # Overtime risk (>40h/week employee)
    for emp, d in by_employee.items():
        if d["actual"] > 38:
            overtime_flags.append({"employee": emp, "hours": round(d["actual"], 1)})

    # Weekly avg labor % by day of week
    dow_summary = {}
    for dow, d in by_dayofweek.items():
        weeks = d["count"] / 10  # rough shift-to-day ratio
        avg_labor_pct = (d["labor_cost"] / d["sales"] * 100) if d["sales"] else 0
        dow_summary[dow] = round(avg_labor_pct, 1)

    total_labor  = sum(s["actual"] * HOURLY_RATE for s in
                       [{"actual": float(x["actual_hours"])} for x in shifts])
    total_sales  = sum(float(s["sales_that_day"]) for s in
                       {s["date"]: s for s in shifts}.values())
    overall_pct  = round(total_labor / total_sales * 100, 1) if total_sales else 0
    potential_savings = round(total_labor * 0.12, 2)  # industry benchmark: 12% reducible

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
    }


def get_claude_insights(analysis: dict) -> str:
    """Ask Claude to narrate the findings like a restaurant consultant."""
    prompt = f"""You are an expert restaurant labor consultant reviewing two weeks of shift data for Maplewood Kitchen, a busy Lincoln Park restaurant.

Here is the analysis:
- Overall labor cost: ${analysis['total_labor_cost']:,.0f} on ${analysis['total_sales']:,.0f} in sales ({analysis['overall_labor_pct']}% labor ratio)
- Industry target: 28-32% labor ratio
- Overstaffed days (labor > 35%): {json.dumps(analysis['overstaffed_days'][:3])}
- Understaffed days: {json.dumps(analysis['understaffed_days'][:2])}
- Overtime risk: {json.dumps(analysis['overtime_risk'])}
- Labor % by day of week: {json.dumps(analysis['dow_summary'])}
- Estimated savings with optimized scheduling: ${analysis['potential_savings']:,.0f}/month

Write a concise, direct consultant report (4-6 short paragraphs) that:
1. Leads with the most important number (the overall labor % vs target)
2. Calls out the 2-3 most specific problems with dates and dollars
3. Gives 3 concrete scheduling fixes the owner can implement this week
4. Ends with the monthly savings opportunity

Write like a sharp consultant who respects the owner's time. No fluff, no bullet points — short punchy paragraphs. Use specific numbers throughout."""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
