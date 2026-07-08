"""
ask_cavnar.py — the in-dashboard AI copilot: answers a plain-English
question about a restaurant's own data by gathering a snapshot of its
current stats across whichever modules are active, then asking Claude to
answer strictly from that snapshot.

Not a general-purpose chatbot — a narrow "explain what my own numbers
mean" tool. The context assembled here is the only "memory" the model
gets, so it can't invent a number for a module the client doesn't have,
and it's told explicitly to say so rather than guess when the data isn't
in the snapshot.
"""
import os
import anthropic
from ai_utils import create_with_retry, extract_text

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _fmt(v, default="n/a"):
    return default if v is None else v


def _reviews_context(restaurant_id):
    from models import get_review_stats
    s = get_review_stats(restaurant_id)
    if not s["total"]:
        return "REVIEWS\n- No reviews recorded yet.\n"
    return (
        "REVIEWS\n"
        f"- Total reviews analyzed: {s['total']}\n"
        f"- Average rating: {s['avg_rating']} / 5\n"
        f"- Positive: {s['positive']} ({s['positive_pct']}%), Negative: {s['negative']}, Neutral: {s['neutral']}\n"
        f"- Response rate: {s['response_rate']}%\n"
        f"- Urgent/unresolved reviews: {s['urgent']}\n"
        f"- Awaiting owner approval: {s['awaiting_approval']}\n"
        f"- Received this month: {s['received_this_month']}\n"
        f"- Average response time: {_fmt(s['avg_response_hours'])} hours\n"
    )


def _labor_context(restaurant_id):
    from labor import analyse_shifts_for_restaurant
    a = analyse_shifts_for_restaurant(restaurant_id)
    # load_shifts_for_restaurant() falls back to bundled SAMPLE shift data
    # (by design, so the Labor tab isn't blank before a client's first
    # upload) when no real CSV has been saved — analyse_shifts_for_restaurant
    # threads that through as is_live=False. Answering from the sample data
    # as if it were this restaurant's real numbers would be actively
    # misleading, not just unhelpful.
    if not a or not a.get("is_live"):
        return "LABOR\n- No real shift data uploaded yet — the owner needs to upload a shifts CSV. (The Labor tab currently shows sample placeholder data, not this restaurant's real numbers.)\n"
    return (
        "LABOR\n"
        f"- Overall labor cost: {a['overall_labor_pct']}% of sales\n"
        f"- Total labor cost this period: ${a['total_labor_cost']:,.0f} on ${a['total_sales']:,.0f} in sales\n"
        f"- Overstaffed days this period: {len(a.get('overstaffed_days') or [])}\n"
        f"- Understaffed days this period: {len(a.get('understaffed_days') or [])}\n"
    )


def _inventory_context(restaurant_id):
    from inventory import load_inventory_for_restaurant, analyse_inventory
    items, is_live = load_inventory_for_restaurant(restaurant_id)
    # Same sample-data-fallback concern as labor above.
    if not items or not is_live:
        return "FOOD COST\n- No real inventory data uploaded yet — the owner needs to upload an inventory CSV. (The Food Cost tab currently shows sample placeholder data, not this restaurant's real numbers.)\n"
    a = analyse_inventory(items)
    return (
        "FOOD COST\n"
        f"- Weekly waste cost: ${a['total_waste_cost_week']:,.0f}\n"
        f"- Projected monthly waste: ${a['monthly_waste_projection']:,.0f}\n"
        f"- Critical low items: {len(a.get('critical_low') or [])}\n"
        f"- Items to reorder soon: {len(a.get('reorder_soon') or [])}\n"
        f"- Total inventory value: ${a['total_stock_value']:,.0f}\n"
    )


def _marketing_context(restaurant_id):
    from models import get_conn
    conn = get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    row = conn.execute("""
        SELECT COUNT(*) as posted,
               COALESCE(SUM(reach),0) as reach,
               COALESCE(SUM(likes),0) as likes,
               COALESCE(SUM(comments),0) as comments
        FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL
    """, (restaurant_id,)).fetchone()
    conn.close()
    if not row or not row["posted"]:
        return "MARKETING\n- No posts published yet through Cavnar AI.\n"
    return (
        "MARKETING\n"
        f"- Posts published: {row['posted']}\n"
        f"- Total reach: {row['reach']}\n"
        f"- Total likes: {row['likes']}, comments: {row['comments']}\n"
    )


_CONTEXT_BUILDERS = (
    ("module_reviews", _reviews_context),
    ("module_labor", _labor_context),
    ("module_inventory", _inventory_context),
    ("module_marketing", _marketing_context),
)


def build_context(restaurant):
    """Plain-text snapshot of whichever modules `restaurant` has active.
    A module the client doesn't have is simply omitted, not described as
    empty — that keeps the model from being asked to reason about data
    that was never going to exist for this client."""
    parts = []
    for attr, builder in _CONTEXT_BUILDERS:
        if not getattr(restaurant, attr, 0):
            continue
        try:
            parts.append(builder(restaurant.id))
        except Exception:
            continue
    return "\n".join(parts) if parts else "No data available yet for this restaurant."


ASK_CAVNAR_PROMPT = """You are Cavnar AI, a restaurant intelligence consultant. Answer the owner's question using ONLY the data below — never invent a number that isn't here. If the data needed to answer isn't in the snapshot, say so plainly and suggest what to check instead (e.g. "upload your shifts CSV" if labor data is missing), rather than guessing.

Restaurant: {restaurant_name}

CURRENT DATA SNAPSHOT:
{context}

Owner's question: "{question}"

Answer in 2-4 sentences, warm and direct, like a trusted advisor. Always use $ signs before dollar amounts. No markdown, no bullet points, no headers — plain conversational text only."""


def ask(restaurant, question):
    """Ask Cavnar a question about `restaurant`'s own data. Returns the
    plain-text answer. Callers are responsible for rate-limiting (see
    ai_utils.ai_rate_limited) before calling this — it always makes a real
    Claude call."""
    context = build_context(restaurant)
    prompt = ASK_CAVNAR_PROMPT.format(
        restaurant_name=restaurant.name,
        context=context,
        question=question.strip()[:500],
    )
    message = create_with_retry(
        _client,
        model=os.getenv("ASK_CAVNAR_MODEL", "claude-sonnet-5"),
        max_tokens=350,
        # claude-sonnet-5 rejects `temperature` outright ("deprecated for
        # this model") — confirmed live via direct API call. Omitted rather
        # than set, since this model doesn't accept it at all.
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant.id,
        action="ask_cavnar",
    )
    return extract_text(message).strip()
