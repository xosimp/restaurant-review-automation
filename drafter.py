import os, anthropic
from models import get_conn, update_draft, get_pending_drafts, get_restaurant

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DRAFT_PROMPT = """You are writing a public response on behalf of {restaurant_name}.
{voice_notes}
{style_examples}
Review:
- Rating: {rating}/5
- Sentiment: {sentiment}
- Text: "{text}"

Write a response that:
- Is warm and genuine — not corporate
- Addresses specifics from the review
- For negative: acknowledges the issue without being defensive, invites them back
- For positive: thanks them and references something specific they mentioned
- 3-5 sentences maximum
- Does NOT start with "Dear" or "Hello"
- Does NOT use "We strive to" or "It is our goal"
- Match the tone and style of the approved examples above if provided

Return only the response text."""


def get_approved_examples(restaurant_id: int, limit: int = 3) -> str:
    """Pull recent approved responses to learn the owner's style."""
    try:
        conn = get_conn()
        rows = conn.execute("""
            SELECT text, draft_response FROM reviews
            WHERE restaurant_id=? AND response_status IN ('approved','posted')
            AND draft_response IS NOT NULL AND draft_response != ''
            ORDER BY id DESC LIMIT ?
        """, (restaurant_id, limit)).fetchall()
        conn.close()
        if not rows:
            return ""
        examples = "\nExamples of approved responses from this restaurant (use these to match their voice):\n"
        for i, row in enumerate(rows, 1):
            examples += f'''
Example {i}:
Review: "{row["text"][:120]}..."
Response: "{row["draft_response"]}"
'''
        return examples + "\n"
    except Exception:
        return ""

def draft_response(review_id: int, rating: int, text: str,
                   sentiment: str, restaurant_name: str,
                   voice_notes: str = "", restaurant_id: int = None) -> str:
    voice_section = f"\nOwner voice guidance: {voice_notes}\n" if voice_notes else ""
    style_examples = get_approved_examples(restaurant_id) if restaurant_id else ""
    prompt = DRAFT_PROMPT.format(
        restaurant_name=restaurant_name,
        voice_notes=voice_section,
        style_examples=style_examples,
        rating=rating,
        sentiment=sentiment,
        text=text.replace('"', "'"),
    )
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    draft = message.content[0].text.strip()
    update_draft(review_id, draft)
    return draft


def draft_pending(restaurant_id: int, limit: int = 50):
    restaurant = get_restaurant(restaurant_id)
    reviews = get_pending_drafts(restaurant_id, limit)
    print(f"  Drafting responses for {len(reviews)} reviews...")
    for r in reviews:
        try:
            draft = draft_response(
                r.id, r.rating, r.text, r.sentiment,
                restaurant.name, restaurant.voice_notes or "",
                restaurant_id=restaurant_id,
            )
            print(f"    [{r.id}] drafted ({len(draft)} chars)")
        except Exception as e:
            print(f"    [{r.id}] ERROR: {e}")
