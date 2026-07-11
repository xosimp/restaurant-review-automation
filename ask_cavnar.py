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
    # "Awaiting approval" and "needs a response drafted" are two different
    # queues, easy to conflate — a real bug here: a question like "how many
    # reviews need approval" used to only ever see awaiting_approval (drafts
    # already written, pending the owner's final approve click), completely
    # missing reviews that don't have a draft yet at all (need "Generate
    # response" clicked first). Both are surfaced explicitly now.
    return (
        "REVIEWS\n"
        f"- Total reviews analyzed: {s['total']}\n"
        f"- Average rating: {s['avg_rating']} / 5\n"
        f"- Positive: {s['positive']} ({s['positive_pct']}%), Negative: {s['negative']}, Neutral: {s['neutral']}\n"
        f"- Response rate: {s['response_rate']}%\n"
        f"- Urgent/unresolved reviews: {s['urgent']}\n"
        f"- Need a response drafted (no AI draft written yet — owner must click 'Generate response'): {s['needs_response']}\n"
        f"- Have a draft already written, awaiting the owner's final approval to post: {s['awaiting_approval']}\n"
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
    target = a.get("labor_target", 30.0)
    over_under = "over" if a["overall_labor_pct"] > target else ("under" if a["overall_labor_pct"] < target else "at")
    return (
        "LABOR\n"
        f"- Overall labor cost: {a['overall_labor_pct']}% of sales ({over_under} this restaurant's {target}% target)\n"
        f"- Total labor cost this period: ${a['total_labor_cost']:,.0f} on ${a['total_sales']:,.0f} in sales\n"
        f"- Estimated monthly savings available from optimized scheduling: ${a.get('potential_savings', 0):,.0f}\n"
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
    critical = a.get("critical_low") or []
    reorder = a.get("reorder_soon") or []
    critical_names = ", ".join(f"{x['item']} ({x['days_remaining']}d left)" for x in critical) or "none"
    reorder_names = ", ".join(x["item"] for x in reorder) or "none"
    return (
        "FOOD COST\n"
        f"- Weekly waste cost: ${a['total_waste_cost_week']:,.0f}\n"
        f"- Projected monthly waste: ${a['monthly_waste_projection']:,.0f}\n"
        f"- Critical low items ({len(critical)}): {critical_names}\n"
        f"- Items to reorder soon ({len(reorder)}): {reorder_names}\n"
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
    lines = ["MARKETING"]
    if not row or not row["posted"]:
        lines.append("- No posts published yet through Cavnar AI.")
    else:
        lines.append(f"- Posts published: {row['posted']}")
        lines.append(f"- Total reach: {row['reach']}")
        lines.append(f"- Total likes: {row['likes']}, comments: {row['comments']}")
    lines.append(_guest_text_club_summary(restaurant_id))
    return "\n".join(lines) + "\n"


def _guest_text_club_summary(restaurant_id):
    """Guest text club moved under the Marketing tab/module — its numbers
    belong in the marketing snapshot too, not just posts/reach."""
    try:
        from guest_marketing import get_guest_contacts
        from models import get_conn
        contacts = get_guest_contacts(restaurant_id)
        eligible = [c for c in contacts if c["consent"] and not c["unsubscribed"]]
        conn = get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(sent_count),0) AS sent FROM guest_campaigns WHERE restaurant_id=?",
            (restaurant_id,)
        ).fetchone()
        conn.close()
        return (
            f"- Guest text club: {len(eligible)} text-eligible contact{'s' if len(eligible) != 1 else ''} "
            f"({len(contacts)} total added), {row['n']} campaign{'s' if row['n'] != 1 else ''} sent "
            f"({row['sent']} texts delivered total)"
        )
    except Exception:
        return "- Guest text club: no data available."


def _intel_context(restaurant_id):
    from models import get_restaurant
    from competitor_intel_format import parse_competitor_intel
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.competitor_intel:
        return "COMPETITOR INTEL\n- No competitor analysis run yet.\n"
    try:
        parsed = parse_competitor_intel(restaurant.competitor_intel)
    except Exception:
        return "COMPETITOR INTEL\n- No competitor analysis run yet.\n"
    recs = parsed.get("recommendations") or []
    updated = restaurant.competitor_updated_at or "unknown date"
    lines = [f"COMPETITOR INTEL (last updated {updated})"]
    if recs:
        lines.append("- Top recommendations from the last analysis:")
        for r in recs[:5]:
            lines.append(f"  - {r}")
    else:
        lines.append("- Analysis on file, but no specific recommendations were parsed from it.")
    return "\n".join(lines) + "\n"


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
    # Intel isn't gated by a single module flag — it's gated the same way
    # the Intel tab itself is (all 4 modules on, plus a Google Place ID).
    try:
        from models import is_full_tier
        if getattr(restaurant, "google_place_id", None) and is_full_tier(restaurant):
            parts.append(_intel_context(restaurant.id))
    except Exception:
        pass
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
