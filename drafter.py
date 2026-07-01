import os, re, anthropic
from models import get_conn, update_draft, get_pending_drafts, get_restaurant
from ai_utils import create_with_retry

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def get_approved_examples(restaurant_id: int, limit: int = 4) -> str:
    """Pull recent approved responses to learn the owner's style."""
    try:
        conn = get_conn()
        rows = conn.execute("""
            SELECT rating, text, draft_response FROM reviews
            WHERE restaurant_id=? AND response_status IN ('approved','posted')
            AND draft_response IS NOT NULL AND draft_response != ''
            ORDER BY id DESC LIMIT ?
        """, (restaurant_id, limit)).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = []
        for i, row in enumerate(rows, 1):
            lines.append(
                f'Example {i} ({row["rating"]}★): '
                f'Review: "{row["text"][:100]}" → '
                f'Response: "{row["draft_response"]}"'
            )
        return "\nApproved response examples — match this owner's exact tone and style:\n" + "\n".join(lines) + "\n"
    except Exception:
        return ""


def get_recurring_themes(restaurant_id: int) -> str:
    """Check if same complaints appear 3+ times recently."""
    try:
        conn = get_conn()
        rows = conn.execute("""
            SELECT text FROM reviews
            WHERE restaurant_id=? AND sentiment='negative'
            AND response_status NOT IN ('skipped')
            ORDER BY fetched_at DESC LIMIT 8
        """, (restaurant_id,)).fetchall()
        conn.close()
        if len(rows) >= 3:
            return f"\nNote: This restaurant has had {len(rows)} negative reviews recently. If this review shares themes with common complaints (service, wait times, food quality), acknowledge the pattern is being actively addressed.\n"
        return ""
    except Exception:
        return ""


def draft_response(review_id: int, rating: int, text: str,
                   sentiment: str, restaurant_name: str,
                   voice_notes: str = "", restaurant_id: int = None,
                   approved_examples: list = None,
                   sign_off: str = None,
                   never_say: str = None,
                   urgency: str = "normal") -> str:

    # Extract reviewer first name if available
    reviewer_name = ""
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT author, platform FROM reviews WHERE id=?", (review_id,)
        ).fetchone()
        conn.close()
        if row:
            platform = row["platform"] or "google"
            name = (row["author"] or "").strip()
            first = name.split()[0] if name else ""
            if len(first) > 1 and first.lower() not in (
                "a","an","the","anonymous","user","google","yelp","local","guide"
            ):
                reviewer_name = first
    except Exception:
        platform = "google"

    # Platform-specific guidance
    if platform == "google":
        platform_note = f"This is a Google review — naturally include '{restaurant_name}' once for SEO. Keep it professional and inviting."
    elif platform == "yelp":
        platform_note = "This is a Yelp review — be conversational and genuine. Do NOT repeat the restaurant name."
    else:
        platform_note = "Keep the response professional and genuine."

    # Length calibration by rating
    if rating >= 4:
        length_note = "25-40 words — brief, warm, genuine. Don't over-explain."
    elif rating == 3:
        length_note = "40-60 words — acknowledge both positives and address any concerns."
    else:
        length_note = "60-80 words — acknowledge SPECIFIC complaints mentioned by name, apologize sincerely, explain what will be done differently."

    # Reviewer address
    reviewer_line = f"Address the reviewer as {reviewer_name} by name naturally in the response." if reviewer_name else "Do not invent a name."

    # Style examples
    if approved_examples:
        ex_lines = "\n".join([
            f'  Example ({e["rating"]}★): "{e["review"][:100]}" → "{e["response"]}"'
            for e in approved_examples
        ])
        style_block = f"\nApproved response examples — study these carefully and extract the owner's style: sentence length, formality level, how they handle complaints vs praise, whether they use first names, how they invite guests back. Replicate that style precisely:\n{ex_lines}\n"
    else:
        style_block = get_approved_examples(restaurant_id) if restaurant_id else ""

    # Recurring negative themes
    theme_note = get_recurring_themes(restaurant_id) if (restaurant_id and sentiment == "negative") else ""

    # Never say
    opener_ban = "\nNever open with 'Thank you for your review', 'Thank you for your feedback', or any variation — start with something specific to what they actually said."
    never_note = opener_ban + (f" Also never use: {never_say}." if never_say else "")

    # Sign off
    sign_off_name = sign_off or restaurant_name

    # Serious-issue escalation — driven by analyser.py's AI urgency classification
    # (food safety, injury, legal threats, staff misconduct, etc.) rather than a
    # separate keyword list here, which used to disagree with the AI's own
    # classification and could false-positive on negated mentions (e.g. "no
    # roach problem at all!" would have tripped the old keyword match).
    is_urgent_issue = urgency == "high"
    if is_urgent_issue:
        length_note = "80-100 words — this is a serious concern, it requires a full and careful response."
    health_note = """\nIMPORTANT: This review was flagged as urgent (health/safety, injury, legal threat, or staff misconduct concern). Take it extremely seriously — no defensiveness, no minimising. Apologise specifically, invite them to contact the owner directly by email or phone.""" if is_urgent_issue else ""

    prompt = f"""Write a public {sentiment} review response for {restaurant_name}.

Platform: {platform_note}
Voice: {voice_notes or "Warm, genuine, never corporate. Always invite guests back."}
Sign off as: {sign_off_name}
{reviewer_line}
Length: {length_note}{never_note}{style_block}{theme_note}{health_note}
LANGUAGE: Detect the language of the review. If the review is NOT in English, write your response in that same language. If it is in English, respond in English.
CRITICAL: If the reviewer mentions specific issues (cold food, slow service, wrong order, noise, parking, staff) — address each one directly by name. Never give a generic apology for a specific complaint.

Review ({rating}/5 stars, {sentiment}):
"{text}"

Write ONLY the response. No preamble, no labels, no quotation marks around the response. Sound like a real person — not a PR firm, not a template."""

    message = create_with_retry(
        client,
        model=os.getenv("DRAFTER_MODEL", "claude-sonnet-5"),
        max_tokens=300,
        temperature=0.7,
        messages=[{"role": "user", "content": prompt}],
    )
    draft = message.content[0].text.strip()

    # Strip markdown if AI slips any in
    draft = re.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1), draft)
    draft = re.sub(r'\*(.+?)\*', lambda m: m.group(1), draft)

    update_draft(review_id, draft)
    return draft


def draft_pending(restaurant_id: int, limit: int = 50):
    restaurant = get_restaurant(restaurant_id)
    reviews = get_pending_drafts(restaurant_id, limit)
    print(f"  Drafting responses for {len(reviews)} reviews...")
    from models import get_approved_examples as _get_ex
    approved_examples = _get_ex(restaurant_id, limit=4)
    for r in reviews:
        try:
            draft = draft_response(
                r.id, r.rating, r.text, r.sentiment,
                restaurant.name,
                voice_notes=restaurant.voice_notes or "",
                restaurant_id=restaurant_id,
                approved_examples=approved_examples,
                sign_off=restaurant.sign_off_name or restaurant.name,
                never_say=restaurant.never_say or "",
                urgency=r.urgency,
            )
            print(f"    [{r.id}] drafted ({len(draft)} chars)")
        except Exception as e:
            print(f"    [{r.id}] ERROR: {e}")
