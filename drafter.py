import os, anthropic
from models import get_conn, update_draft, get_pending_drafts, get_restaurant

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DRAFT_PROMPT = """You are writing a public response on behalf of {restaurant_name}.
{voice_notes}

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

Return only the response text."""


def draft_response(review_id: int, rating: int, text: str,
                   sentiment: str, restaurant_name: str,
                   voice_notes: str = "") -> str:
    voice_section = f"\nOwner voice guidance: {voice_notes}\n" if voice_notes else ""
    prompt = DRAFT_PROMPT.format(
        restaurant_name=restaurant_name,
        voice_notes=voice_section,
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
            )
            print(f"    [{r.id}] drafted ({len(draft)} chars)")
        except Exception as e:
            print(f"    [{r.id}] ERROR: {e}")
