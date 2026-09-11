import os, json, anthropic
from models import get_conn, update_analysis, get_pending_analysis
from ai_utils import create_with_retry, extract_text
from ai_guard import UNTRUSTED_NOTE, wrap_untrusted

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

CATEGORIES = [
    "food_quality", "service", "wait_time", "value",
    "ambiance", "cleanliness", "reservation", "takeout_delivery"
]

ANALYSE_PROMPT = """You are analysing a restaurant review. Return ONLY valid JSON — no markdown, no commentary.

{untrusted_note}

Review:
Rating: {rating}/5
Text:
{text}

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


SENTIMENTS = ("positive", "neutral", "negative")
URGENCIES = ("high", "normal")


# A star rating is a fact the guest chose. Sentiment is the model's reading
# of the text. They can legitimately differ in the middle, but not at the
# ends: a 1-star is not a positive review and a 5-star is not a negative
# one, whatever the prose does. An LLM reading "the staff were lovely but I
# was ill afterwards" as positive dropped that review out of the negative
# spike count and drew it as a positive on the sentiment chart.
def _sentiment_floor(rating: int, sentiment: str) -> str:
    try:
        r = int(rating)
    except (TypeError, ValueError):
        return sentiment
    if r <= 2 and sentiment == "positive":
        return "negative"
    if r >= 5 and sentiment == "negative":
        return "neutral"
    return sentiment


def _validate_analysis(result, rating: int = None):
    """Coerce the model's JSON into what the schema and the UI can hold.

    The reviews table's own CHECK constraints were doing this job, which
    meant an out-of-enum value surfaced as a sqlite IntegrityError swallowed
    by the caller's `except Exception: print(...)` — and the review stayed
    unanalysed, so its urgency alert never fired. `categories` had no
    constraint at all, so an invented category was stored and shown in the
    topic heatmap as if it were one of ours.
    """
    if not isinstance(result, dict):
        raise ValueError("analysis was not a JSON object")
    sentiment = str(result.get("sentiment") or "").strip().lower()
    if sentiment not in SENTIMENTS:
        raise ValueError(f"sentiment {result.get('sentiment')!r} is not one of {SENTIMENTS}")
    urgency = str(result.get("urgency") or "normal").strip().lower()
    if urgency not in URGENCIES:
        urgency = "normal"
    raw_cats = result.get("categories")
    if isinstance(raw_cats, str):
        raw_cats = [raw_cats]
    cats = [c for c in (str(x).strip().lower() for x in (raw_cats or [])) if c in CATEGORIES][:3]
    summary = " ".join(str(result.get("summary") or "").split())[:300]
    if not summary:
        raise ValueError("analysis had no summary")
    if rating is not None:
        sentiment = _sentiment_floor(rating, sentiment)
    return {"sentiment": sentiment, "categories": cats, "summary": summary, "urgency": urgency}


def analyse_review(review_id: int, rating: int, text: str, restaurant_id: int = None) -> dict:
    prompt = ANALYSE_PROMPT.format(
        rating=rating,
        # Fenced and labelled: this is text a stranger wrote, and it used to
        # be interpolated raw with nothing but a quote swap between it and
        # the instructions above it.
        text=wrap_untrusted(text),
        untrusted_note=UNTRUSTED_NOTE,
        categories=", ".join(CATEGORIES),
    )
    message = create_with_retry(
        client,
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="review_analysis",
    )
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ValueError("analysis was truncated")
    raw = extract_text(message).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    result = _validate_analysis(json.loads(raw), rating=rating)
    update_analysis(
        review_id,
        result["sentiment"],
        result["categories"],
        result["summary"],
        result["urgency"],
    )
    return result


def analyse_pending(restaurant_id: int, limit: int = 500):
    """Analyse every review still waiting.

    The default was 50. A review left unanalysed has no urgency, so its
    health/safety alert never fires, and it was invisible to the owner's
    totals entirely until get_review_stats stopped filtering on processed=1.
    A CSV import routinely exceeds 50, and nothing came back for the rest.
    """
    reviews = get_pending_analysis(restaurant_id, limit)
    print(f"  Analysing {len(reviews)} reviews...")
    results = []
    for r in reviews:
        try:
            # restaurant_id was omitted here, so every review analysis went
            # into ai_usage unattributed and the per-restaurant spend cap
            # evaluated with no restaurant to cap.
            res = analyse_review(r.id, r.rating, r.text, restaurant_id=restaurant_id)
            flag = " *** URGENT ***" if res.get("urgency") == "high" else ""
            print(f"    [{r.id}] {res['sentiment']:8s} | {', '.join(res['categories'])}{flag}")
            results.append({"id": r.id, **res})
        except Exception as e:
            # An unanalysed review has no urgency, so its health/safety alert
            # never fires. That is not something to print to stdout and move
            # on from — it reaches the daily failure digest.
            print(f"    [{r.id}] ERROR: {e}")
            try:
                import ops
                ops.capture(e, job="review_analysis",
                            context=f"restaurant_id={restaurant_id} review_id={r.id}")
            except Exception:
                pass
    return results
