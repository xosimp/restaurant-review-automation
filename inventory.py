"""
inventory.py — Food waste analysis + Claude-powered ordering recommendations
"""
import os, csv, json
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def load_inventory(path: str = "sample_inventory.csv",
                   csv_string: str = None) -> list[dict]:
    """Load inventory from a CSV string (client data) or file (sample/demo)."""
    if csv_string:
        import io
        rows = list(csv.DictReader(io.StringIO(csv_string)))
    else:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
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
        overstock_units = max(0, item["current_stock"] - item["par_level"] * 1.5)
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
        if overstock_units > 3:
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

    return {
        "total_waste_cost_week": round(total_waste_cost, 2),
        "monthly_waste_projection": round(monthly_waste_projection, 2),
        "recoverable_monthly":   round(recoverable, 2),
        "total_stock_value":     round(total_stock_value, 2),
        "waste_items":    waste_items[:6],
        "overstock":      overstock[:5],
        "critical_low":   critical_low[:4],
        "reorder_soon":   reorder_soon[:4],
        "total_items":    len(items),
    }


def get_claude_insights(analysis: dict) -> str:
    """Claude narrates inventory findings like a food cost consultant."""
    prompt = f"""You are a restaurant food cost consultant reviewing inventory data for Maplewood Kitchen in Chicago.

Key findings:
- Waste this week: ${analysis['total_waste_cost_week']:,.2f}
- Projected monthly waste cost: ${analysis['monthly_waste_projection']:,.2f}
- Recoverable with better ordering: ${analysis['recoverable_monthly']:,.2f}/month
- Total current inventory value: ${analysis['total_stock_value']:,.2f}

Top waste offenders:
{json.dumps([{"item": x["item"], "waste_units": x["waste_last_week"], "waste_cost": x["waste_cost"], "waste_pct": x["waste_pct"]} for x in analysis["waste_items"][:4]], indent=2)}

Overstocked items:
{json.dumps([{"item": x["item"], "current": x["current_stock"], "par": x["par_level"], "overstock_cost": x["overstock_cost"]} for x in analysis["overstock"][:3]], indent=2)}

Critical low stock:
{json.dumps([{"item": x["item"], "days_remaining": x["days_remaining"]} for x in analysis["critical_low"]], indent=2)}

Write a sharp 4-5 paragraph consultant report that:
1. Opens with the monthly waste number — make the owner feel it
2. Names the 3 worst offenders specifically (item name, dollars wasted, why it's likely happening)
3. Calls out the overstock problem with dollar amounts tied up
4. Gives 3 specific ordering changes to make immediately
5. Closes with the monthly savings if they fix the top 3 issues

Be direct and specific. Use real numbers. No fluff."""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def load_inventory_for_restaurant(restaurant_id: int) -> list[dict]:
    """Load real client data if available, otherwise use sample data."""
    from models import get_client_data
    data = get_client_data(restaurant_id)
    if data and data.get("inventory_csv"):
        return load_inventory(csv_string=data["inventory_csv"])
    return load_inventory()  # fallback to sample
