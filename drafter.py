import os, re, anthropic
from models import get_conn, update_draft, get_pending_drafts, get_restaurant
from ai_utils import create_with_retry, extract_text
from ai_guard import UNTRUSTED_NOTE, wrap_untrusted

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


RECURRING_WINDOW_DAYS = 90
RECURRING_MIN_MENTIONS = 3


def get_recurring_themes(restaurant_id: int) -> str:
    """Named complaint categories a guest has raised at least three times in
    the last 90 days, or "" when there is no such pattern.

    Was a count of the last 8 negative reviews with NO date filter, phrased
    as "{n} negative reviews recently" — so eight negatives spread over
    three years read as a current pattern, and the number was capped by the
    LIMIT rather than being a real count. It then told the model to
    "acknowledge the pattern is being actively addressed" without showing it
    a single theme, inviting it to assert a shared complaint it had never
    seen, in a reply published on a public listing.

    Now it names the actual categories the analyser assigned, over a real
    window, and says nothing about what is being done about them — because
    this system does not know that.
    """
    try:
        from collections import Counter
        import json as _json
        conn = get_conn()
        rows = conn.execute("""
            SELECT categories FROM reviews
            WHERE restaurant_id=? AND sentiment='negative'
              AND response_status NOT IN ('skipped')
              AND deleted_at IS NULL
              AND categories IS NOT NULL AND categories != '[]'
              AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', ?)
        """, (restaurant_id, f"-{RECURRING_WINDOW_DAYS} days")).fetchall()
        conn.close()
        counts = Counter()
        for row in rows:
            try:
                for c in _json.loads(row["categories"] or "[]"):
                    if c:
                        counts[c] += 1
            except Exception:
                continue
        themes = [c for c, n in counts.most_common() if n >= RECURRING_MIN_MENTIONS]
        if not themes:
            return ""
        pretty = ", ".join(t.replace("_", " ") for t in themes[:3])
        return (f"\nContext: over the last {RECURRING_WINDOW_DAYS} days, guests have raised "
                f"{pretty} in at least {RECURRING_MIN_MENTIONS} separate negative reviews. "
                f"If THIS review raises one of those, you may acknowledge it is something the "
                f"restaurant is aware of. Do NOT claim any specific fix, change, retraining or "
                f"process has happened — you have no way to know that.\n")
    except Exception:
        return ""


# Account -> Profile -> "How the AI writes for you". A preset is a short
# steer layered onto the owner's own voice notes, not a replacement.
TONE_PRESETS = {
    "warm": " Tone: warm and personal, like the owner wrote it themselves.",
    "professional": " Tone: polished and professional; courteous, no slang, no exclamation marks.",
    "playful": " Tone: light and playful, with a little personality — never sarcastic.",
    "concise": " Tone: brief and direct — two or three sentences at most.",
}
LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "it": "Italian", "pt": "Portuguese", "de": "German"}


def draft_response(review_id: int, rating: int, text: str,
                   sentiment: str, restaurant_name: str,
                   voice_notes: str = "", restaurant_id: int = None,
                   approved_examples: list = None,
                   sign_off: str = None,
                   never_say: str = None,
                   urgency: str = "normal",
                   language: str = None,
                   tone: str = None) -> str:

    # Extract reviewer first name if available
    reviewer_name = ""
    # Bound up front: the try below only assigned it when a row came back,
    # so a review_id with no row left it undefined and the next line raised
    # NameError rather than falling back.
    platform = "google"
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
        pass

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

{UNTRUSTED_NOTE}

Platform: {platform_note}
Voice: {voice_notes or "Warm, genuine, never corporate. Always invite guests back."}{TONE_PRESETS.get(tone or "", "")}
Sign off as: {sign_off_name}
{reviewer_line}
Length: {length_note}{never_note}{style_block}{theme_note}{health_note}
LANGUAGE: {("Always write the response in " + LANGUAGE_NAMES.get(language, language) + ", regardless of the language of the review.") if language else "Detect the language of the review. If the review is NOT in English, write your response in that same language. If it is in English, respond in English."}
CRITICAL: If the reviewer mentions specific issues (cold food, slow service, wrong order, noise, parking, staff) — address each one directly by name. Never give a generic apology for a specific complaint.

Review ({rating}/5 stars, {sentiment}):
{wrap_untrusted(text)}

Write ONLY the response. No preamble, no labels, no quotation marks around the response. Sound like a real person — not a PR firm, not a template."""

    message = create_with_retry(
        client,
        model=os.getenv("DRAFTER_MODEL", "claude-sonnet-5"),
        max_tokens=300,
        # claude-sonnet-5 rejects `temperature` outright ("deprecated for
        # this model") — confirmed live via direct API call. This means
        # every draft_response() call has been failing in production with a
        # 400 whenever DRAFTER_MODEL isn't overridden away from the sonnet-5
        # default, until this fix.
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="draft_response",
    )
    draft = extract_text(message).strip()
    if getattr(message, "stop_reason", None) == "max_tokens":
        # A reply cut off mid-sentence is worse published than absent, and
        # this one can be published without a human reading it.
        raise ValueError("draft response was truncated")

    # Strip markdown if AI slips any in
    draft = re.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1), draft)
    draft = re.sub(r'\*(.+?)\*', lambda m: m.group(1), draft)

    # A reply is published on a public listing under the owner's name, and
    # the 1-star prompt above literally asks the model to "explain what will
    # be done differently". Nothing checked what it wrote there, so an
    # invented remediation — staff retrained, supplier changed, policy
    # updated — went out as a statement of fact the restaurant never made.
    from ai_guard import unsupported_commitments
    claims = unsupported_commitments(draft)
    if claims:
        update_draft(review_id, draft, needs_review=True,
                     review_reason="states a specific action the restaurant may not have taken: "
                                   + ", ".join(claims[:3]))
    else:
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
            # A failed draft leaves the review pending with nobody told.
            # analyse_pending already reports its failures to the daily
            # digest; this one printed to stdout and moved on.
            print(f"    [{r.id}] ERROR: {e}")
            try:
                import ops
                ops.capture(e, job="review_draft",
                            context=f"restaurant_id={restaurant_id} review_id={r.id}")
            except Exception:
                pass
