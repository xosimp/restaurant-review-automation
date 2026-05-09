"""
marketing.py — AI-powered marketing content generation for restaurants
"""
import os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

RESTAURANT_PROFILE = {
    "name": "Maplewood Kitchen",
    "neighborhood": "Lincoln Park, Chicago",
    "vibe": "warm neighborhood bistro, serious about food without being precious about it",
    "known_for": "short rib pasta, brunch, house-baked bread, craft cocktails",
    "voice": "genuine and warm, a little witty, never corporate, speaks like a person not a brand",
}

CONTENT_TYPES = [
    {
        "id": "instagram_post",
        "label": "Instagram post",
        "icon": "camera",
        "description": "Caption + hashtags for a food or ambiance photo",
    },
    {
        "id": "weekly_email",
        "label": "Weekly email",
        "icon": "mail",
        "description": "Short newsletter to regulars — specials, events, updates",
    },
    {
        "id": "google_promo",
        "label": "Google post",
        "icon": "search",
        "description": "Short promotional post for Google Business Profile",
    },
    {
        "id": "loyalty_nudge",
        "label": "Re-engagement text",
        "icon": "message",
        "description": "SMS to guests who haven't visited in 3+ weeks",
    },
    {
        "id": "happy_hour",
        "label": "Happy hour promo",
        "icon": "glass",
        "description": "Social post driving traffic to Mon-Thu 4-6pm deals",
    },
    {
        "id": "event_announcement",
        "label": "Event announcement",
        "icon": "calendar",
        "description": "Post announcing a special dinner, wine night, or seasonal menu",
    },
]

PROMPTS = {
    "instagram_post": """Write an Instagram caption for {restaurant} ({neighborhood}).
Vibe: {vibe}. Voice: {voice}.
Topic/occasion: {topic}
Known for: {known_for}

Write 2 versions:
VERSION A (short, punchy — 1-2 sentences + hashtags)
VERSION B (storytelling — 3-4 sentences + hashtags)

Use 5-8 relevant hashtags per version. No emojis unless they feel totally natural.
Do not use the phrases "indulge", "culinary journey", "delight", or "experience".""",

    "weekly_email": """Write a short weekly email for {restaurant} regulars.
Voice: {voice}. Neighborhood: {neighborhood}.
Topic/occasion: {topic}

Format:
SUBJECT LINE: (2 options)
BODY: (4-6 sentences, conversational, like the owner wrote it personally)

No "Dear valued customer". No corporate sign-offs. End with a first name sign-off like "— Sarah" or "— the Maplewood team".""",

    "google_promo": """Write a Google Business Profile promotional post for {restaurant}.
Topic: {topic}
Keep it under 100 words. Direct, local, specific. Include a soft call to action.
No hashtags. No emojis.""",

    "loyalty_nudge": """Write an SMS re-engagement message for guests of {restaurant} who haven't visited in 3+ weeks.
Topic/offer: {topic}
Voice: {voice}

Rules: Under 160 characters. Feels personal not automated. Includes restaurant name. Soft incentive if relevant.
Write 2 options.""",

    "happy_hour": """Write a social media post promoting happy hour at {restaurant}.
Details: Mon-Thu 4-6pm, half-price small plates, $8 cocktails.
Voice: {voice}. Topic angle: {topic}

Write for Instagram/Facebook. 2-4 sentences + hashtags. Make people actually want to leave work early.""",

    "event_announcement": """Write a social media announcement for {restaurant}.
Event details: {topic}
Voice: {voice}. Neighborhood: {neighborhood}.

Write 2 versions — one for Instagram (casual, visual), one for email subject line + first paragraph.""",
}


def generate_content(content_type: str, topic: str) -> str:
    """Generate marketing content for a given type and topic."""
    prompt_template = PROMPTS.get(content_type, PROMPTS["instagram_post"])
    p = RESTAURANT_PROFILE
    prompt = prompt_template.format(
        restaurant=p["name"],
        neighborhood=p["neighborhood"],
        vibe=p["vibe"],
        voice=p["voice"],
        known_for=p["known_for"],
        topic=topic,
    )
    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def get_content_calendar_ideas() -> list[dict]:
    """Generate a week of content ideas using Claude."""
    prompt = f"""Generate a 7-day social media content calendar for {RESTAURANT_PROFILE['name']}, 
a {RESTAURANT_PROFILE['vibe']} in {RESTAURANT_PROFILE['neighborhood']}.

Known for: {RESTAURANT_PROFILE['known_for']}
Current month: May

Return ONLY valid JSON — no markdown fences. Array of 7 objects with:
{{"day": "Monday", "platform": "Instagram|Email|Google", "angle": "one sentence topic idea", "type": "instagram_post|weekly_email|google_promo|happy_hour"}}

Make ideas specific and seasonal. Vary platforms."""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except Exception:
        return []


import json
