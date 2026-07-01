import os, json, anthropic
from models import get_conn, update_analysis, get_pending_analysis
from ai_utils import create_with_retry

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

CATEGORIES = [
    "food_quality", "service", "wait_time", "value",
    "ambiance", "cleanliness", "reservation", "takeout_delivery"
]

ANALYSE_PROMPT = """You are analysing a restaurant review. Return ONLY valid JSON — no markdown, no commentary.

Review:
Rating: {rating}/5
Text: "{text}"

Return this exact shape:
{{
  "sentiment": "positive" | "neutral" | "negative",
  "categories": [list of 1-3 from: {categories}],
  "summary": "one sentence, max 20 words, owner perspective",
  "urgency": "high" | "normal"
}}

urgency is "high" if ANY of these are present:
- Food safety, illness, food poisoning, allergic reaction, foreign object in food
- Physical injury on premises
- Legal threats, lawsuit, attorney
- Explicit threat to contact health department or BBB
- Threatening, abusive, or discriminatory language directed at staff
- Direct staff misconduct complaint (harassment, theft, dishonesty)"""


def analyse_review(review_id: int, rating: int, text: str) -> dict:
    prompt = ANALYSE_PROMPT.format(
        rating=rating,
        text=text.replace('"', "'"),
        categories=", ".join(CATEGORIES),
    )
    message = create_with_retry(
        client,
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    result = json.loads(raw)
    update_analysis(
        review_id,
        result["sentiment"],
        result["categories"],
        result["summary"],
        result.get("urgency", "normal"),
    )
    return result


def analyse_pending(restaurant_id: int, limit: int = 50):
    reviews = get_pending_analysis(restaurant_id, limit)
    print(f"  Analysing {len(reviews)} reviews...")
    results = []
    for r in reviews:
        try:
            res = analyse_review(r.id, r.rating, r.text)
            flag = " *** URGENT ***" if res.get("urgency") == "high" else ""
            print(f"    [{r.id}] {res['sentiment']:8s} | {', '.join(res['categories'])}{flag}")
            results.append({"id": r.id, **res})
        except Exception as e:
            print(f"    [{r.id}] ERROR: {e}")
    return results
