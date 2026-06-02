"""
inventory.py — Food waste analysis + Claude-powered ordering recommendations
"""
import os, csv, json
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def load_inventory(path: str = "sample_inventory.csv",
                   csv_string: str = None) -> list[dict]:
    """Load inventory from a CSV string (client data) or bundled sample."""
    import io
    if csv_string:
        rows = list(csv.DictReader(io.StringIO(csv_string)))
    else:
        _SAMPLE = """item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week
Romaine Lettuce,Produce,20,28,2.5,3.5,25,8.0
Chicken Breast,Protein,30,22,5.8,6.0,30,3.5
Salmon Fillet,Protein,15,18,12.5,2.5,15,2.0
Ground Beef 80/20,Protein,25,32,4.2,4.0,25,4.5
Heavy Cream,Dairy,12,9,3.8,1.8,12,1.5
Butter Unsalted,Dairy,10,14,4.5,1.5,10,0.5
Parmesan Cheese,Dairy,8,5,8.2,1.2,8,0.8
Roma Tomatoes,Produce,15,22,1.8,2.8,20,6.5
Fresh Garlic,Produce,5,7,3.2,0.8,5,0.3
Yellow Onions,Produce,10,13,1.2,1.5,10,1.2
Olive Oil Extra Virgin,Pantry,6,8,14.5,0.9,6,0.2
Pasta Rigatoni,Pantry,15,19,2.8,2.2,15,1.8
Bread Rolls,Bakery,60,45,0.45,12.0,60,15.0
Russet Potatoes,Produce,20,16,0.8,3.5,20,4.0
Baby Spinach,Produce,8,11,4.2,1.4,8,3.5
White Wine Chardonnay,Beverage,12,15,8.5,1.8,12,0.0
Lemons,Produce,10,7,0.6,1.5,10,0.8
Fresh Herbs Mix,Produce,4,6,5.5,0.7,4,2.0
Beef Stock,Pantry,8,10,4.8,1.2,8,0.5
Shrimp 16/20,Protein,10,8,14.2,1.6,10,1.2"""
        try:
            rows = list(csv.DictReader(io.StringIO(_SAMPLE)))
        except Exception:
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                return []
    for r in rows:
        r["par_level"]      = float(r["par_level"])
        r["current_stock"]  = float(r["current_stock"])
        r["unit_cost"]      = float(r["unit_cost"])
        r["avg_daily_usage"]= float(r["avg_daily_usage"])
        r["last_order_qty"] = float(r["last_order_qty"])
        r["waste_last_week"]= float(r["waste_last_week"])
    return rows


def analyse_inventory(items: list[dict]) -> dict:
    """Compute waste, overstock, and reorder flags."""
    waste_items   = []
    overstock     = []
    reorder_soon  = []
    critical_low  = []

    total_waste_cost  = 0.0
    total_stock_value = 0.0

    for item in items:
        days_remaining  = (item["current_stock"] / item["avg_daily_usage"]
                           if item["avg_daily_usage"] > 0 else 99)
        waste_cost      = item["waste_last_week"] * item["unit_cost"]
        # Category-specific overstock thresholds (industry standard)
        # Proteins/dairy: flag at 110% of par (perishable, high cost)
        # Produce: flag at 120% of par
        # All others (dry, beverage): flag at 130% of par
        category = (item.get("category") or "").lower()
        if category in ("protein", "dairy"):
            overstock_multiplier = 1.10
        elif category == "produce":
            overstock_multiplier = 1.20
        else:
            overstock_multiplier = 1.30
        overstock_units = max(0, item["current_stock"] - item["par_level"] * overstock_multiplier)
        overstock_cost  = overstock_units * item["unit_cost"]
        stock_value     = item["current_stock"] * item["unit_cost"]
        waste_pct       = (item["waste_last_week"] / item["last_order_qty"] * 100
                           if item["last_order_qty"] > 0 else 0)

        total_waste_cost  += waste_cost
        total_stock_value += stock_value

        item["days_remaining"] = round(days_remaining, 1)
        item["waste_cost"]     = round(waste_cost, 2)
        item["overstock_cost"] = round(overstock_cost, 2)
        item["waste_pct"]      = round(waste_pct, 1)

        if waste_pct > 20:
            waste_items.append(item)
        if overstock_units > 0:
            overstock.append(item)
        if days_remaining <= 2 and item["current_stock"] < item["par_level"]:
            critical_low.append(item)
        elif days_remaining <= 4:
            reorder_soon.append(item)

    waste_items  = sorted(waste_items,  key=lambda x: x["waste_cost"],     reverse=True)
    overstock    = sorted(overstock,    key=lambda x: x["overstock_cost"],  reverse=True)
    critical_low = sorted(critical_low, key=lambda x: x["days_remaining"])

    monthly_waste_projection = total_waste_cost * 4.3
    recoverable = monthly_waste_projection * 0.65

    from datetime import datetime, timedelta
    # Derive week range from last_ordered dates in items, or use current week
    ordered_dates = []
    for item in items:
        lo = item.get("last_ordered","")
        if lo:
            try:
                ordered_dates.append(datetime.strptime(lo[:10], "%Y-%m-%d"))
            except Exception:
                pass
    if ordered_dates:
        latest = max(ordered_dates)
        week_end_dt   = latest
        week_start_dt = latest - timedelta(days=6)
    else:
        week_end_dt   = datetime.now()
        week_start_dt = week_end_dt - timedelta(days=6)

    def fmt(dt): return dt.strftime("%-m/%-d/%y")

    return {
        "total_waste_cost_week": round(total_waste_cost, 2),
        "monthly_waste_projection": round(monthly_waste_projection, 2),
        "recoverable_monthly":   round(recoverable, 2),
        "total_stock_value":     round(total_stock_value, 2),
        "waste_items":    waste_items[:6],
        "overstock":      overstock[:5],
        "critical_low":   critical_low[:4],
        "reorder_soon":   reorder_soon[:6],
        "total_items":    len(items),
        "week_start":     fmt(week_start_dt),
        "week_end":       fmt(week_end_dt),
        "last_updated":   fmt(datetime.now()),
    }


def get_claude_insights(analysis: dict, owner_name: str = None, restaurant_name: str = None, restaurant_id: int = None) -> str:
    """Claude narrates inventory findings like a food cost consultant."""
    name_line = f"Owner name: {owner_name}" if owner_name else ""
    rest_line  = f"Restaurant: {restaurant_name}" if restaurant_name else ""
    # Pull inventory history for week-over-week comparison
    wow_context = ""
    menu_context = ""
    if restaurant_id:
        try:
            from models import get_conn as _gc_inv
            _conn_inv = _gc_inv()
            # Get previous waste snapshot
            prev = _conn_inv.execute("""
                SELECT waste_json FROM inventory_history
                WHERE restaurant_id=? ORDER BY saved_at DESC LIMIT 1
            """, (restaurant_id,)).fetchone()
            if prev and prev["waste_json"]:
                import json as _json_inv
                prev_waste = _json_inv.loads(prev["waste_json"])
                prev_total = prev_waste.get("total_waste_cost", 0)
                curr_total = analysis['total_waste_cost_week']
                if prev_total > 0:
                    diff = curr_total - prev_total
                    pct_change = round((diff / prev_total) * 100, 1)
                    direction = "UP" if diff > 0 else "DOWN"
                    wow_context = f"\n- vs last week: waste is {direction} ${abs(diff):,.2f} ({abs(pct_change)}%) — mention this trend"
                # Check if same items repeating
                prev_items = set(prev_waste.get("top_items", []))
                curr_items = set(x["item"] for x in analysis["waste_items"][:4])
                repeat = prev_items & curr_items
                if repeat:
                    wow_context += f"\n- REPEAT waste offenders (2+ weeks): {', '.join(repeat)} — these need stronger action, not just reordering"

            # Save current snapshot
            import json as _json_inv2
            snapshot = {
                "total_waste_cost": analysis['total_waste_cost_week'],
                "top_items": [x["item"] for x in analysis["waste_items"][:4]]
            }
            _conn_inv.execute("""CREATE TABLE IF NOT EXISTS inventory_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL,
                waste_json TEXT,
                saved_at TEXT DEFAULT (datetime('now'))
            )""")
            _conn_inv.execute(
                "INSERT INTO inventory_history (restaurant_id, waste_json) VALUES (?,?)",
                (restaurant_id, _json_inv2.dumps(snapshot))
            )
            _conn_inv.commit()
            _conn_inv.close()
        except Exception as ie:
            print(f"[inventory history] {ie}")

        # Menu connection — if restaurant has menu_notes, suggest menu decisions for repeat waste
        try:
            from models import get_restaurant as _gr_inv
            rest = _gr_inv(restaurant_id)
            if rest and rest.menu_notes and analysis["waste_items"]:
                top_waste_item = analysis["waste_items"][0]["item"]
                menu_context = f"\n- Menu context: {rest.menu_notes[:300]}. If {top_waste_item} appears in multiple dishes, consider whether portion sizes or menu placement should change."
        except Exception:
            pass

    from datetime import datetime as _dt_inv
    from zoneinfo import ZoneInfo
    today_inv = _dt_inv.now(ZoneInfo('America/Chicago')).strftime("%B %d, %Y")

    prompt = f"""You are a food cost consultant reviewing weekly inventory data for a restaurant.
{rest_line}
{name_line}
Today's date: {today_inv}

Key findings:
- Waste this week: ${analysis['total_waste_cost_week']:,.2f}
- Projected monthly waste cost: ${analysis['monthly_waste_projection']:,.2f}
- Recoverable with better ordering: ${analysis['recoverable_monthly']:,.2f}/month
- Total current inventory value: ${analysis['total_stock_value']:,.2f}{wow_context}

Top waste offenders:
{json.dumps([{"item": x["item"], "waste_units": x["waste_last_week"], "waste_cost": x["waste_cost"], "waste_pct": x["waste_pct"]} for x in analysis["waste_items"][:4]], indent=2)}

Overstocked items:
{json.dumps([{"item": x["item"], "current": x["current_stock"], "par": x["par_level"], "overstock_cost": x["overstock_cost"]} for x in analysis["overstock"][:3]], indent=2)}

Critical low stock:
{json.dumps([{"item": x["item"], "days_remaining": x["days_remaining"]} for x in analysis["critical_low"]], indent=2)}{menu_context}

Write a food cost analysis. Rules that apply to everything:
- No markdown, no bullet points, no bold text, no asterisks whatsoever
- Do NOT label sections or write "Part 1", "Part 2", "Recommendations", or any headers
- Plain flowing prose throughout — no line that starts with a dash or number
- Friendly and direct — like a trusted advisor, not a formal report
- Always use $ signs before dollar amounts (e.g. $2,400 not 2400 or 2,400)

First, write one paragraph of 3-4 sentences:
- Open with the monthly waste projection dollar amount, make it feel real and personal
- Name the 2 worst waste offenders by item name with their dollar amounts and a brief reason why it is likely happening
- Call out the biggest overstock issue with the dollar amount tied up, if any

Then, on new lines after the paragraph, write 1-3 recommendations:
- Only include recommendations where there is a genuine, specific opportunity — do not pad to three if the data does not support it
- Maximum of three, minimum of one, ranked by dollar impact (highest first)
- Number each one: start with "1. ", "2. ", "3. " 
- Each must directly save money this week or next week with an estimated dollar amount
- Specific to the actual items in the data — never generic advice
- Never suggest anything that hurts guest experience, reduces quality, or cuts portions
- Focus on ordering frequency, quantity reductions, or par level adjustments
- Do not use the owner name anywhere in the recommendations

Finally, on a new line with NO number, write one short warm closing sentence:
- Tied loosely to how the week looks — good week gets a small celebration, rough week gets encouragement
- Never generic filler, no more than one sentence
- Do NOT start it with a number"""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=900,
        messages=[{"role": "user", "content": prompt}],
    )
    result = msg.content[0].text.strip()
    # Strip any markdown that slips through
    import re as _re_inv
    result = _re_inv.sub('[*]{2}(.+?)[*]{2}', lambda m: m.group(1), result)
    result = _re_inv.sub('[*](.+?)[*]', lambda m: m.group(1), result)
    result = _re_inv.sub(r'#{1,6}\s', '', result)
    return result


def load_inventory_for_restaurant(restaurant_id: int):
    """Load real client data if available, otherwise use sample data. Returns (items, is_live)."""
    from models import get_client_data
    data = get_client_data(restaurant_id)
    if data and data.get("inventory_csv"):
        return load_inventory(csv_string=data["inventory_csv"]), True
    return load_inventory(), False  # fallback to sample
