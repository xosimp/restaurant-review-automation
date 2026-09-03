"""
ask_cavnar.py — the in-dashboard AI copilot: answers a plain-English
question by gathering a snapshot of the restaurant's current stats across
whichever modules are active, then asking Claude to answer it.

Two modes, chosen by Claude per-question rather than hard-coded here:
questions about the restaurant's OWN numbers are answered strictly from
the data snapshot (never invent a figure that isn't there — say so and
suggest what to check instead), while general restaurant-consultant
questions (marketing ideas, staffing strategy, menu pricing, industry
benchmarks, or just conversation) draw on Claude's own expertise the same
way any other AI assistant would, optionally grounded in the real
snapshot data when it's relevant. The snapshot is still the model's only
source of truth for this restaurant's actual figures — that half of the
rule never loosens — but it's no longer the model's only allowed source
of information overall.
"""
import os
import anthropic
from ai_utils import create_with_retry, extract_text

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _fmt(v, default="n/a"):
    return default if v is None else v


def _cap(text, limit=280):
    """Defensively bounds any freeform admin-entered text field before it
    goes into the prompt. Found live: Gia Mia's hours_notes turned out to
    be a 2,854-character labor-scheduling rulebook (staff arrival times,
    closer rules, floor layout, minimum staffing floors...) repurposed
    from what the field name suggests — real, useful content for the
    LABOR schedule generator it was written for, but far more than a
    general question deserves to pay for in every single Ask Cavnar call
    regardless of relevance. Nothing stops an admin from putting something
    similarly long in vibe/known_for/menu_notes/daypart_split either, so
    this applies uniformly rather than only patching the one field that
    happened to get caught."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _identity_context(restaurant):
    """Today's real date (in the restaurant's own timezone, not the
    server's) and upcoming dining-relevant holidays — the one section
    that's always included regardless of which modules are active.
    Without this, a question like "what upcoming holidays should I focus
    on" had no anchor date at all: the model has no live clock of its own,
    so it would (correctly, from its own perspective) say it can't check
    dates. Reused get_upcoming_holidays() from marketing.py rather than
    re-deriving a second holiday calendar — it already computes real
    calendar dates (including movable ones like Mother's Day/Thanksgiving)
    and already respects this restaurant's own skip_holidays preference,
    the same list marketing content generation and labor's holiday-aware
    forecasting both already rely on."""
    from time_utils import restaurant_now
    from marketing import get_upcoming_holidays

    now = restaurant_now(restaurant, naive=True)
    lines = ["TODAY", f"- Today's date: {now.strftime('%A, %B %d, %Y')}"]

    try:
        upcoming = get_upcoming_holidays(now)
        skip = [h.strip().lower() for h in (restaurant.skip_holidays or "").split(",") if h.strip()]
        if skip and upcoming:
            upcoming = ", ".join(
                h for h in upcoming.split(", ") if not any(s in h.lower() for s in skip)
            )
        lines.append(f"- Upcoming holidays/events (next 30 days): {upcoming if upcoming else 'none in the next 30 days'}")
    except Exception:
        pass

    return "\n".join(lines) + "\n"


def _profile_context(restaurant):
    """Everything else admin has on file about this restaurant that an
    owner's own question might reasonably need — identity/vibe, hours,
    menu, Google's own published rating (distinct from the review-analysis
    numbers in REVIEWS below), revenue target, delivery mix, which plan is
    active, which platforms are connected, and how long they've been a
    client. Every line is conditional on the field actually being set — an
    unset field is omitted, not described as empty, same principle the
    module sections below already follow.

    Deliberately excludes anything that's a credential, access token,
    internal admin/ops note, or account-security setting (2FA, login
    notify, Stripe/DocuSign ids, temp passwords, etc.) — those aren't
    "restaurant data" an owner would ask their own copilot about, and
    several are outright secrets that must never reach a model prompt.
    Connection status is surfaced the same way the Account/Settings screen
    does it (see mobile_api.py's connections summary): a plain boolean
    derived from whether a token/id is present, never the token itself."""
    lines = ["RESTAURANT PROFILE"]

    profile_bits = []
    if restaurant.neighborhood:
        profile_bits.append(_cap(restaurant.neighborhood, 80))
    if restaurant.vibe:
        profile_bits.append(_cap(restaurant.vibe))
    if restaurant.known_for:
        profile_bits.append(f"known for {_cap(restaurant.known_for)}")
    if profile_bits:
        lines.append(f"- About this restaurant: {'; '.join(profile_bits)}")

    if restaurant.hours_notes:
        lines.append(f"- Hours: {_cap(restaurant.hours_notes)}")

    menu_bits = [b for b in (_cap(restaurant.menu_notes), restaurant.menu_url) if b]
    if menu_bits:
        lines.append(f"- Menu: {' — '.join(menu_bits)}")

    if restaurant.gbp_rating is not None:
        count = f" from {restaurant.gbp_review_count} reviews" if restaurant.gbp_review_count else ""
        lines.append(
            f"- Google's published rating: {restaurant.gbp_rating}★{count} "
            "(Google's own aggregate — separate from the review-response tracking in REVIEWS)"
        )

    if restaurant.monthly_revenue_target:
        lines.append(f"- Monthly revenue target: ${restaurant.monthly_revenue_target:,.0f}")

    if restaurant.delivery_pct is not None:
        lines.append(f"- Delivery/takeout share of revenue: {restaurant.delivery_pct}%")

    if restaurant.daypart_split:
        lines.append(f"- Daypart split: {_cap(restaurant.daypart_split, 120)}")

    try:
        from models import TIER_LABELS
        tier_label = TIER_LABELS.get(restaurant.service_tier, restaurant.service_tier)
    except Exception:
        tier_label = restaurant.service_tier
    lines.append(f"- Plan: {tier_label}")

    connected = []
    if getattr(restaurant, "gmb_refresh_token", None):
        connected.append("Google Business Profile")
    if getattr(restaurant, "ig_token", None):
        connected.append("Instagram")
    if getattr(restaurant, "toast_restaurant_guid", None):
        connected.append("Toast POS")
    if getattr(restaurant, "square_location_id", None):
        connected.append("Square POS")
    if getattr(restaurant, "clover_merchant_id", None):
        connected.append("Clover POS")
    lines.append(f"- Connected integrations: {', '.join(connected) if connected else 'none yet'}")

    if restaurant.created_at:
        lines.append(f"- Client since: {restaurant.created_at[:10]}")

    return "\n".join(lines) + "\n"


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
    from marketing import get_upcoming_holidays
    from models import get_restaurant as _gr_ac
    items, is_live = load_inventory_for_restaurant(restaurant_id)
    # Same sample-data-fallback concern as labor above.
    if not items or not is_live:
        return "FOOD COST\n- No real inventory data uploaded yet — the owner needs to upload an inventory CSV. (The Food Cost tab currently shows sample placeholder data, not this restaurant's real numbers.)\n"
    _rest_ac = _gr_ac(restaurant_id)
    a = analyse_inventory(
        items,
        delivery_days=_rest_ac.delivery_days if _rest_ac else None,
        upcoming_holidays=get_upcoming_holidays(),
    )
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
    """Plain-text snapshot: two always-present sections (TODAY — date and
    upcoming holidays, see _identity_context; RESTAURANT PROFILE — hours,
    menu, Google rating, revenue target, connections, plan, etc., see
    _profile_context) followed by whichever modules `restaurant` has
    active. A module the client doesn't have is simply omitted, not
    described as empty — that keeps the model from being asked to reason
    about data that was never going to exist for this client."""
    parts = [_identity_context(restaurant), _profile_context(restaurant)]
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
    # parts always has at least the TODAY section now, so this never falls
    # back to a bare placeholder the way it used to for a restaurant with
    # zero active modules — date/holiday/identity info isn't module-gated.
    return "\n".join(parts)


# System prompt (persona/rules/data snapshot) is sent once per call via the
# `system` parameter, refreshed with current data every time — separate
# from `messages`, which now carries the actual back-and-forth so the model
# can see what it's replying to. Previously the whole thing (rules + data +
# question) was one giant "user" message with no history at all, so a
# follow-up like "yes" arrived as a fresh, context-free question every
# time — real bug, reported live: the model had no way to know what "yes"
# was even responding to.
ASK_CAVNAR_SYSTEM_PROMPT = """You are Cavnar AI, an AI-powered restaurant intelligence consultant embedded in {restaurant_name}'s dashboard, having an ongoing conversation with the owner. You have two modes, and most questions call for a blend of both:

1. QUESTIONS ABOUT THIS RESTAURANT'S OWN NUMBERS (reviews, labor, food cost, marketing, competitors): answer strictly from the DATA SNAPSHOT below. Never invent a figure that isn't there. If what's needed isn't in the snapshot, say so plainly and suggest what to check instead (e.g. "upload your shifts CSV" if labor data is missing) rather than guessing.

2. EVERYTHING ELSE — restaurant industry advice, marketing ideas, menu strategy, staffing/scheduling best practices, general business questions, or just conversation: answer using your own knowledge and expertise as an experienced restaurant consultant, same as you would in any other context. Weave in this restaurant's real data from the snapshot when it's genuinely relevant, but don't limit yourself to only what's in the snapshot for these — you're free to think and advise.

Use judgment about which mode (or blend) a question calls for — "how do I get my labor cost down" wants both this restaurant's real labor % AND general scheduling advice, for example.

The DATA SNAPSHOT below always opens with a TODAY section — this restaurant's real current date (in its own local timezone) and its real upcoming holidays for the next 30 days. Always use that section directly for any date, day-of-week, "how many days until," or "what's coming up" question — you have real, live information here, not a training cutoff. Never say you don't have access to a calendar or can't check dates; you can, right there in TODAY.

Right after that is a RESTAURANT PROFILE section — hours, menu, Google's own published rating, revenue target, delivery mix, which plan they're on, which platforms are connected, and how long they've been a client, whenever admin has that on file. Use it the same way: it's real information about this specific restaurant, not something to say you don't have access to.

Restaurant: {restaurant_name}

CURRENT DATA SNAPSHOT:
{context}

This is a real, ongoing conversation — the message history below is genuine back-and-forth with this same owner, not a series of disconnected one-off questions. Read it the way a person would: if the latest message is a short reply like "yes," "the second one," or "how about labor instead," resolve it against what YOU just said or asked in your own previous message, and answer accordingly. Never ask the owner to repeat context that's already sitting right there in the conversation.

Answer in 2-3 sentences by default — this renders as a narrow mobile chat bubble, not a report, so keep sentences themselves short too, not just the count. Only go to 4 sentences when the question is genuinely multi-part and actually needs it. If there's more worth saying than that, say the most useful part now and offer to go deeper, rather than saying everything at once. Warm and direct, like a trusted advisor sitting across the table — not a corporate assistant. Always use $ signs before dollar amounts when citing this restaurant's real numbers. No markdown, no bullet points, no headers — plain conversational text only."""

# Bounds how much prior conversation gets sent (and paid for) on every
# single call — 12 messages is 6 full exchanges, plenty for a short-term
# "what were we just talking about" memory without letting an old, long
# session balloon every subsequent request's cost indefinitely.
_MAX_HISTORY_MESSAGES = 12
_MAX_HISTORY_TURN_LENGTH = 800


def _sanitize_history(history):
    """Defensively rebuilds the conversation history rather than trusting
    the client's payload outright: drops anything without a valid
    user/assistant role or non-empty content, caps each turn's length,
    collapses any accidental same-role repeats (the Messages API requires
    strict alternation starting with "user"), and keeps only the most
    recent _MAX_HISTORY_MESSAGES entries."""
    cleaned = []
    for turn in (history or []):
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = (turn.get("content") or "").strip()[:_MAX_HISTORY_TURN_LENGTH]
        if role not in ("user", "assistant") or not content:
            continue
        if cleaned and cleaned[-1]["role"] == role:
            cleaned[-1] = {"role": role, "content": content}  # keep the newer one
        else:
            cleaned.append({"role": role, "content": content})
    # Cap FIRST, then re-check the "starts with user" rule against the
    # capped result — checking before the cap would only guarantee the
    # FULL list started with "user," not the truncated tail actually sent,
    # which could land on "assistant" first if the cap boundary fell there.
    cleaned = cleaned[-_MAX_HISTORY_MESSAGES:]
    if cleaned and cleaned[0]["role"] != "user":
        cleaned = cleaned[1:]
    return cleaned


def ask(restaurant, question, history=None):
    """Ask Cavnar a question about `restaurant`'s own data. `history` is the
    prior back-and-forth in THIS chat session as
    [{"role": "user"|"assistant", "content": str}, ...], oldest first, NOT
    including `question` itself — the caller's own message list up to (but
    not including) the new question.

    Returns (answer_text, was_truncated). `was_truncated` is True when the
    model hit max_tokens and the answer therefore stops mid-thought — the
    client used to render that identically to a complete answer, so a
    half-finished recommendation about labor or a supplier read as final
    advice. labor.py already checks stop_reason for the same reason.
    Callers are responsible for rate-limiting (see ai_utils.ai_rate_limited)
    before calling this — it always makes a real Claude call."""
    context = build_context(restaurant)
    system_prompt = ASK_CAVNAR_SYSTEM_PROMPT.format(restaurant_name=restaurant.name, context=context)
    messages = _sanitize_history(history) + [{"role": "user", "content": question.strip()[:500]}]
    message = create_with_retry(
        _client,
        model=os.getenv("ASK_CAVNAR_MODEL", "claude-sonnet-5"),
        # Brought back down from 450 now that the prompt targets 2-3
        # sentences (occasionally 4) instead of 2-5 — this is a safety
        # ceiling against a rare run-on answer, not the actual length
        # target, so it stays a bit above what 3-4 tight sentences with a
        # couple of dollar figures actually needs rather than risking a
        # mid-sentence cutoff.
        max_tokens=320,
        # claude-sonnet-5 rejects `temperature` outright ("deprecated for
        # this model") — confirmed live via direct API call. Omitted rather
        # than set, since this model doesn't accept it at all.
        system=system_prompt,
        messages=messages,
        restaurant_id=restaurant.id,
        action="ask_cavnar",
    )
    truncated = getattr(message, "stop_reason", None) == "max_tokens"
    return extract_text(message).strip(), truncated
