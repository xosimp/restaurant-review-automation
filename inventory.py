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
        r.setdefault("unit", "")  # unit label optional (e.g. "lbs", "cases")
    return rows


def analyse_inventory(items: list[dict]) -> dict:
    """Compute waste, overstock, and reorder flags."""
    waste_items   = []
    overstock     = []
    reorder_soon     = []
    critical_low     = []
    order_reduction  = []  # items where suggested qty is meaningfully less than last order

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

        # Suggested order quantity: target 1.5x par, cover 3 days usage, adjusted for waste rate
        # If wasting a lot, pull the order quantity down proportionally
        waste_adj     = min(0.95, max(0.60, 1.0 - (waste_pct / 100) * 0.5))
        raw_qty       = (item["par_level"] * 1.5) - item["current_stock"] + (item["avg_daily_usage"] * 3)
        suggested_qty = max(1, round(raw_qty * waste_adj))
        savings_vs_last = round((item["last_order_qty"] - suggested_qty) * item["unit_cost"], 2)
        item["suggested_order_qty"] = suggested_qty
        item["savings_vs_last"]     = savings_vs_last  # positive = save money, negative = need more

        if waste_pct > 20:
            waste_items.append(item)
        if overstock_units > 0:
            overstock.append(item)
        if days_remaining <= 2 and item["current_stock"] < item["par_level"]:
            critical_low.append(item)
        elif days_remaining <= 4:
            reorder_soon.append(item)
        # Flag items with meaningful savings potential even if stock isn't critically low
        # Threshold: saves $5+ vs last order AND not already in critical/reorder lists
        elif savings_vs_last >= 5.0:
            order_reduction.append(item)

    waste_items      = sorted(waste_items,      key=lambda x: x["waste_cost"],     reverse=True)
    overstock        = sorted(overstock,        key=lambda x: x["overstock_cost"],  reverse=True)
    critical_low     = sorted(critical_low,     key=lambda x: x["days_remaining"])
    order_reduction  = sorted(order_reduction,  key=lambda x: x["savings_vs_last"], reverse=True)

    monthly_waste_projection = total_waste_cost * 4.3
    annual_waste_projection  = monthly_waste_projection * 12
    recoverable = monthly_waste_projection * 0.65
    annual_recoverable = recoverable * 12

    # Industry benchmark: waste cost as % of total purchased this week
    # 4-5% = industry target | 5-8% = above average | 8-15% = concerning | >15% = serious
    total_purchased = sum(i["last_order_qty"] * i["unit_cost"] for i in items)
    waste_rate_pct  = round((total_waste_cost / total_purchased * 100) if total_purchased > 0 else 0, 1)
    # Benchmark rating
    if waste_rate_pct <= 4:
        benchmark_label  = "Excellent"
        benchmark_color  = "#2d6a4f"
        benchmark_detail = "At or below the 4% industry target"
    elif waste_rate_pct <= 6:
        benchmark_label  = "On Track"
        benchmark_color  = "#6fcf97"
        benchmark_detail = "Near the 4-5% industry target"
    elif waste_rate_pct <= 10:
        benchmark_label  = "Above Average"
        benchmark_color  = "#ef9f27"
        benchmark_detail = f"Industry target is 4-5% — you're at {waste_rate_pct}%"
    elif waste_rate_pct <= 15:
        benchmark_label  = "Concerning"
        benchmark_color  = "#e07040"
        benchmark_detail = f"Industry target is 4-5% — you're at {waste_rate_pct}%"
    else:
        benchmark_label  = "Needs Attention"
        benchmark_color  = "#c0392b"
        benchmark_detail = f"Industry target is 4-5% — you're at {waste_rate_pct}%"

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
    # Always use upload date (passed in) or current Chicago time as week end
    # This ensures the week range matches when the client actually uploaded
    try:
        from zoneinfo import ZoneInfo as _ZI_inv
        now_chi = datetime.now(_ZI_inv('America/Chicago')).replace(tzinfo=None)
    except Exception:
        now_chi = datetime.now()
    # Week ending on the upload day, starting 6 days prior
    week_end_dt   = now_chi
    week_start_dt = now_chi - timedelta(days=6)

    def fmt(dt): return dt.strftime("%-m/%-d/%y")

    return {
        "total_waste_cost_week":    round(total_waste_cost, 2),
        "monthly_waste_projection": round(monthly_waste_projection, 2),
        "annual_waste_projection":  round(annual_waste_projection, 2),
        "recoverable_monthly":      round(recoverable, 2),
        "annual_recoverable":       round(annual_recoverable, 2),
        "waste_rate_pct":           waste_rate_pct,
        "benchmark_label":          benchmark_label,
        "benchmark_color":          benchmark_color,
        "benchmark_detail":         benchmark_detail,
        "total_stock_value":     round(total_stock_value, 2),
        "waste_items":    waste_items[:6],
        "overstock":      overstock[:5],
        "critical_low":     critical_low[:4],
        "reorder_soon":     reorder_soon[:6],
        "order_reduction":  order_reduction[:6],
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
            # Get previous week's snapshot (exclude current week so we compare to last week)
            prev = _conn_inv.execute("""
                SELECT waste_json FROM inventory_history
                WHERE restaurant_id=? AND week_end < date('now','-1 day')
                ORDER BY week_end DESC LIMIT 1
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

            # Save current snapshot — one row per week per restaurant (upsert by week_end)
            # Use the inventory data's own week_end date so chart labels match the header
            import json as _json_inv2
            from zoneinfo import ZoneInfo as _ZI_hist
            try:
                from datetime import datetime as _dt_we
                _week_end_str = _dt_we.strptime(analysis.get("week_end", ""), "%m/%d/%y").strftime("%Y-%m-%d")
            except Exception:
                _week_end_str = datetime.now(_ZI_hist('America/Chicago')).strftime('%Y-%m-%d')
            snapshot = {
                "total_waste_cost": analysis['total_waste_cost_week'],
                "top_items": [x["item"] for x in analysis["waste_items"][:4]]
            }
            _conn_inv.execute("""CREATE TABLE IF NOT EXISTS inventory_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                restaurant_id INTEGER NOT NULL,
                waste_json TEXT,
                week_end    TEXT,
                saved_at    TEXT DEFAULT (datetime('now'))
            )""")
            # Add week_end column if missing (migration for existing rows)
            try:
                _conn_inv.execute("ALTER TABLE inventory_history ADD COLUMN week_end TEXT")
                _conn_inv.commit()
            except Exception:
                pass
            # Upsert: update existing row for this week, or insert new one
            existing = _conn_inv.execute(
                "SELECT id FROM inventory_history WHERE restaurant_id=? AND week_end=?",
                (restaurant_id, _week_end_str)
            ).fetchone()
            if existing:
                _conn_inv.execute(
                    "UPDATE inventory_history SET waste_json=?, saved_at=datetime('now') WHERE id=?",
                    (_json_inv2.dumps(snapshot), existing["id"])
                )
            else:
                _conn_inv.execute(
                    "INSERT INTO inventory_history (restaurant_id, waste_json, week_end) VALUES (?,?,?)",
                    (restaurant_id, _json_inv2.dumps(snapshot), _week_end_str)
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

    # Seasonal/event awareness — pull upcoming holidays for ordering recommendations
    holiday_context = ""
    try:
        from marketing import get_upcoming_holidays as _guh
        _upcoming = _guh()
        if _upcoming:
            # Flag any inventory items that are relevant to upcoming holidays
            _all_items = (analysis.get("waste_items", []) +
                         analysis.get("critical_low", []) +
                         analysis.get("reorder_soon", []))
            _item_names = [x["item"].lower() for x in _all_items]
            # Holiday-to-ingredient hints
            _holiday_items = {
                "valentine": ["salmon", "filet", "lobster", "shrimp", "chocolate", "cream", "butter"],
                "mother": ["salmon", "filet", "lobster", "shrimp", "cream", "herbs", "asparagus"],
                "father": ["prime rib", "steak", "beef", "ribs", "lobster"],
                "thanksgiving": ["turkey", "potato", "cream", "butter", "herbs", "onion"],
                "christmas": ["prime rib", "beef", "salmon", "cream", "butter", "herbs"],
                "fourth of july": ["beef", "chicken", "ribs", "corn", "potato"],
                "memorial": ["beef", "chicken", "ribs", "potato"],
                "labor day": ["beef", "chicken", "ribs", "potato"],
                "new year": ["salmon", "lobster", "shrimp", "cream", "butter", "champagne"],
                "st. patrick": ["beef", "potato", "cabbage", "onion"],
            }
            relevant_flags = []
            for holiday_str in _upcoming.split(", "):
                h_lower = holiday_str.lower()
                for keyword, ingredients in _holiday_items.items():
                    if keyword in h_lower:
                        matches = [i for i in ingredients
                                  if any(i in name for name in _item_names)]
                        if matches:
                            relevant_flags.append(
                                f"{holiday_str}: consider stocking up on {', '.join(matches)}"
                            )
            _flag_lines = ("\nInventory flags for upcoming events:\n" + "\n".join("- " + f for f in relevant_flags)) if relevant_flags else ""
            _tail = "\nIf any upcoming holiday is within 2 weeks and relevant to this restaurant's inventory, include a specific ordering heads-up in your recommendations — only if it would genuinely change what they should order this week."
            holiday_context = "\n\nUpcoming holidays/events in the next 30 days: " + _upcoming + _flag_lines + _tail
    except Exception as _he:
        print(f"[inventory holiday context] {_he}")

    prompt = f"""You are a food cost consultant reviewing weekly inventory data for a restaurant.
{rest_line}
{name_line}
Today's date: {today_inv}

Key findings:
- Waste this week: ${analysis['total_waste_cost_week']:,.2f}
- Projected monthly waste cost: ${analysis['monthly_waste_projection']:,.2f}
- Recoverable with better ordering: ${analysis['recoverable_monthly']:,.2f}/month
- Total current inventory value: ${analysis['total_stock_value']:,.2f}
- Waste rate vs industry: {analysis['waste_rate_pct']}% (industry target is 4-5% — label: {analysis['benchmark_label']}){wow_context}{holiday_context}

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
- Focus on quantity reductions or par level adjustments based on the data — NEVER assume or mention ordering frequency (daily, weekly, twice a week etc.) since you don't know their ordering schedule
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
