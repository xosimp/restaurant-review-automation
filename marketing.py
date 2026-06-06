"""
marketing.py — AI-powered marketing content generation for restaurants
"""
import os
import anthropic

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Default profile used for demo/sample mode
DEFAULT_PROFILE = {
    "name": "Maplewood Kitchen",
    "neighborhood": "Lincoln Park, Chicago",
    "vibe": "warm neighborhood bistro, serious about food without being precious about it",
    "known_for": "short rib pasta, brunch, house-baked bread, craft cocktails",
    "voice": "genuine and warm, a little witty, never corporate, speaks like a person not a brand",
    "never_say": "",
    "sign_off_name": "the Maplewood team",
}

# Keep for backward compat
RESTAURANT_PROFILE = DEFAULT_PROFILE


def get_upcoming_holidays(from_date=None) -> str:
    """Return a comma-separated string of holidays/events in the next 30 days."""
    from datetime import datetime, timedelta
    if from_date is None:
        try:
            from zoneinfo import ZoneInfo
            from_date = datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None)
        except Exception:
            from_date = datetime.now()

    # Dining-relevant holidays only — skip civic/cultural holidays
    # that don't naturally drive restaurant visits or fit most concepts
    fixed = [
        (1, 1, "New Year's Day"),
        (2, 14, "Valentine's Day"),
        (3, 17, "St. Patrick's Day"),
        (5, 5, "Cinco de Mayo"),
        (7, 4, "Fourth of July — summer cookout season"),
        (10, 31, "Halloween — great for themed specials"),
        (12, 24, "Christmas Eve — holiday dining"),
        (12, 25, "Christmas Day"),
        (12, 31, "New Year's Eve — celebration dining"),
    ]

    # Calculated holidays
    year = from_date.year
    calculated = []

    # Mother's Day — 2nd Sunday of May
    may1 = datetime(year, 5, 1)
    mothers_day = may1 + timedelta(days=(6 - may1.weekday()) % 7 + 7)
    calculated.append((mothers_day, "Mother's Day"))

    # Father's Day — 3rd Sunday of June
    jun1 = datetime(year, 6, 1)
    fathers_day = jun1 + timedelta(days=(6 - jun1.weekday()) % 7 + 14)
    calculated.append((fathers_day, "Father's Day"))

    # Thanksgiving — 4th Thursday of November
    nov1 = datetime(year, 11, 1)
    first_thu = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    thanksgiving = first_thu + timedelta(weeks=3)
    calculated.append((thanksgiving, "Thanksgiving"))

    # Memorial Day — last Monday of May
    may31 = datetime(year, 5, 31)
    memorial = may31 - timedelta(days=(may31.weekday()) % 7)
    calculated.append((memorial, "Memorial Day"))

    # Labor Day — first Monday of September
    sep1 = datetime(year, 9, 1)
    labor_day = sep1 + timedelta(days=(7 - sep1.weekday()) % 7)
    calculated.append((labor_day, "Labor Day"))

    # Check next year too for year-end queries
    year2 = year + 1
    jan1_next = datetime(year2, 1, 1)
    calculated.append((jan1_next, "New Year's Day"))

    # Find holidays in next 30 days
    end_date = from_date + timedelta(days=30)
    upcoming = []

    for month, day, name in fixed:
        for y in [year, year2]:
            try:
                d = datetime(y, month, day)
                if from_date <= d <= end_date:
                    upcoming.append(f"{name} ({d.strftime('%b %d')})")
            except ValueError:
                pass

    for d, name in calculated:
        if from_date <= d <= end_date:
            upcoming.append(f"{name} ({d.strftime('%b %d')})")

    upcoming.sort()
    return ", ".join(upcoming) if upcoming else ""


def get_profile_for_restaurant(restaurant_id: int = None) -> dict:
    """Get restaurant profile from DB, fall back to default."""
    if not restaurant_id:
        return DEFAULT_PROFILE
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        if not r:
            return DEFAULT_PROFILE
        return {
            "name":        r.name,
            "neighborhood": r.neighborhood or "Chicago, IL",
            "vibe":        r.vibe or "independent restaurant",
            "known_for":   r.known_for or "great food and hospitality",
            "voice":       r.voice_notes or "warm, genuine, never corporate",
            "never_say":   r.never_say or "",
            "sign_off_name": r.sign_off_name or r.name,
            "menu_notes":   r.menu_notes or "",
            "skip_holidays": r.skip_holidays or "",
        }
    except Exception:
        return DEFAULT_PROFILE

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
1st option (short, punchy — 1-2 sentences + hashtags)
2nd option (storytelling — 3-4 sentences + hashtags)

If menu items are provided below, reference specific dishes by name — never make up dishes.
IMPORTANT: Only reference location details (water views, surroundings, setting) that are provided in the restaurant profile. Never invent or assume geographic details like "river", "ocean", "mountains" — use only what you are told.
Use 5-8 relevant hashtags per version. No emojis unless they feel totally natural.
Do not use the phrases "indulge", "culinary journey", "delight", or "experience".""",

    "weekly_email": """Write a short weekly email for {restaurant} regulars.
Voice: {voice}. Neighborhood: {neighborhood}. Known for: {known_for}.
Topic/occasion: {topic}

Format:
SUBJECT LINE: (2 options)
BODY: (4-6 sentences, conversational, like the owner wrote it personally)

If menu items are provided below, mention specific dishes by name to make it feel personal and specific.
No "Dear valued customer". No corporate sign-offs. End with a first name sign-off like "— Sarah" or "— the Maplewood team".""",

    "google_promo": """Write a Google Business Profile promotional post for {restaurant} in {neighborhood}.
Topic: {topic}. Known for: {known_for}.
Keep it under 100 words. Direct, local, specific. Include a soft call to action.
Reference specific menu items if provided below. No hashtags. No emojis.""",

    "loyalty_nudge": """Write an SMS re-engagement message for guests of {restaurant}.
Topic/offer: {topic}
Voice: {voice}

Rules: Under 160 characters. Feels personal not automated. Includes restaurant name. Soft incentive if relevant.
Write 2 options.""",

    "happy_hour": """Write a social media post promoting happy hour at {restaurant}.
Happy hour details: {topic}
Voice: {voice}.

Write for Instagram/Facebook. 2-4 sentences + hashtags. Make people actually want to leave work early.
If the topic doesn't specify exact times or deals, write something that feels authentic without inventing specifics.""",

    "event_announcement": """Write a social media announcement for {restaurant}.
Event details: {topic}
Voice: {voice}. Neighborhood: {neighborhood}.

Write 2 versions — one for Instagram (casual, visual), one for email subject line + first paragraph.""",
}


def get_recent_content(restaurant_id: int, limit: int = 5) -> list:
    """Get recently generated content topics to avoid repetition."""
    if not restaurant_id:
        return []
    try:
        from models import get_conn
        conn = get_conn()
        # Ensure table exists
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            content_type TEXT,
            topic TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        rows = conn.execute(
            """SELECT content_type, topic FROM marketing_content_log
               WHERE restaurant_id=? ORDER BY created_at DESC LIMIT ?""",
            (restaurant_id, limit)
        ).fetchall()
        conn.commit()
        conn.close()
        return [{"type": r["content_type"], "topic": r["topic"]} for r in rows]
    except Exception:
        return []


def log_content(restaurant_id: int, content_type: str, topic: str):
    """Log generated content for memory."""
    if not restaurant_id:
        return
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            content_type TEXT,
            topic TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute(
            "INSERT INTO marketing_content_log (restaurant_id, content_type, topic) VALUES (?,?,?)",
            (restaurant_id, content_type, topic)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def generate_content(content_type: str, topic: str,
                     restaurant_id: int = None) -> str:
    """Generate marketing content for a given type and topic."""
    from datetime import datetime
    prompt_template = PROMPTS.get(content_type, PROMPTS["instagram_post"])
    p = get_profile_for_restaurant(restaurant_id)

    # Build recent content context to avoid repetition
    recent = get_recent_content(restaurant_id, limit=5)
    recent_context = ""
    if recent:
        recent_topics = ", ".join(
            f"{r['type'].replace('_',' ')} about {r['topic']}" for r in recent
        )
        recent_context = f"\n\nIMPORTANT: You have recently generated content about: {recent_topics}. Do NOT repeat these themes or topics. Be fresh and different."

    # Seasonal awareness with real date
    try:
        from zoneinfo import ZoneInfo
        now_dt = datetime.now(ZoneInfo('America/Chicago')).replace(tzinfo=None)
    except Exception:
        now_dt = datetime.now()
    month = now_dt.strftime("%B")
    today_date = now_dt.strftime("%B %d, %Y")
    upcoming = get_upcoming_holidays(now_dt)
    # Filter out client-skipped holidays
    skip_h = [h.strip().lower() for h in (p.get('skip_holidays') or '').split(',') if h.strip()]
    if skip_h and upcoming:
        filtered_h = [h for h in upcoming.split(', ')
                      if not any(s in h.lower() for s in skip_h)]
        upcoming = ', '.join(filtered_h) if filtered_h else None
    seasonal_context = f"\nToday's date: {today_date}. Upcoming holidays in next 30 days: {upcoming if upcoming else 'none'}. Only reference holidays that are actually coming up soon."

    never_clause = f"\nNever use these words or phrases: {p['never_say']}." if p.get('never_say') else ""
    menu_clause = f"\nMenu & current specials for {p['name']}: {p['menu_notes']}\nUse this to make content specific and accurate — reference real dishes, specials, and offerings when relevant." if p.get('menu_notes') else ""

    # Build explicit location context so AI doesn't invent geography
    location_context = f"\nLocation context: {p['neighborhood']}. Setting/vibe: {p['vibe']}. Only use these details when describing the restaurant's physical setting — do not add any geographic details not mentioned here."

    prompt = prompt_template.format(
        restaurant=p["name"],
        neighborhood=p["neighborhood"],
        vibe=p["vibe"],
        voice=p["voice"],
        known_for=p["known_for"],
        topic=topic,
    ) + location_context + recent_context + seasonal_context + never_clause + menu_clause

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    result = msg.content[0].text.strip()

    # Strip markdown formatting Claude sometimes adds
    import re as _re
    result = _re.sub('[*]{2}(.+?)[*]{2}', lambda m: m.group(1), result)
    result = _re.sub('[*](.+?)[*]', lambda m: m.group(1), result)
    result = _re.sub(r'^#{1,3}\s*', '', result, flags=_re.MULTILINE)

    # Log this content for future memory
    log_content(restaurant_id, content_type, topic)

    return result


def mark_calendar_idea_used(restaurant_id: int, content_type: str, topic: str):
    """Track which calendar ideas were actually generated — feeds back into future calendar quality."""
    log_content(restaurant_id, f"calendar_{content_type}", topic)


def get_content_calendar_ideas(restaurant_id: int = None) -> list[dict]:
    """Generate a week of content ideas using Claude."""
    p = get_profile_for_restaurant(restaurant_id)
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    now = _dt.now(_ZI('America/Chicago')).replace(tzinfo=None)
    # Always show the NEXT full Mon-Sun week from today
    # Find next Monday (if today is Mon-Wed show this week, Thu-Sun show next)
    days_since_monday = now.weekday()  # Mon=0 ... Sun=6
    if days_since_monday <= 2:  # Mon/Tue/Wed — show current week
        start = now - _td(days=days_since_monday)
    else:  # Thu/Fri/Sat/Sun — show next week's Monday
        days_to_next_monday = 7 - days_since_monday
        start = now + _td(days=days_to_next_monday)
    days_map = {}
    for i in range(7):
        d = start + _td(days=i)
        dn = d.strftime("%A")
        if dn not in days_map:
            days_map[dn] = d.strftime("%-m/%-d")
    week_range = f"{start.strftime('%-m/%-d')} – {(start + _td(days=6)).strftime('%-m/%-d/%y')}"
    current_month = now.strftime("%B")
    today_str = now.strftime("%B %d, %Y")
    recent = get_recent_content(restaurant_id, limit=5)
    recent_topics = ", ".join(r['topic'] for r in recent) if recent else "none"

    # Build upcoming holidays in the next 30 days
    upcoming_holidays = get_upcoming_holidays(now)
    # Filter out holidays the client wants to skip
    skip = [h.strip().lower() for h in (p.get('skip_holidays') or '').split(',') if h.strip()]
    if skip and upcoming_holidays:
        filtered = [h for h in upcoming_holidays.split(', ')
                    if not any(s in h.lower() for s in skip)]
        upcoming_holidays = ', '.join(filtered) if filtered else None

    menu_context = f"\nMenu & current specials: {p['menu_notes']}\nReference specific dishes and specials in content ideas when relevant." if p.get('menu_notes') else ""
    never_clause = f"Never use these words or phrases: {p['never_say']}." if p.get('never_say') else ""

    prompt = f"""Generate a 7-day social media content calendar for {p['name']}, 
a {p['vibe']} in {p['neighborhood']}.

Known for: {p['known_for']}{menu_context}
Brand voice: {p['voice']}
{never_clause}
TODAY'S DATE: {today_str} (this is the real current date — do not assume any other date)
Upcoming holidays/events in the next 30 days: {upcoming_holidays if upcoming_holidays else "No major holidays"}
Recently generated content (avoid repeating these): {recent_topics}

Return ONLY valid JSON — no markdown fences. Array of 7 objects with:
{{"day": "Monday", "platform": "Instagram & FB|Email|Google|SMS", "angle": "one sentence topic idea", "type": "instagram_post|weekly_email|google_promo|happy_hour|loyalty_nudge"}}

Rules:
- Include at least one SMS/loyalty_nudge idea per week to re-engage guests
- Reference real menu items and dishes by name when menu info is provided
- For any upcoming holiday, make the content feel natural and relevant to THIS restaurant — skip it if it doesn't fit
- Vary platforms across the 7 days — don't use Instagram more than 3 times
- Make every idea specific enough that the owner knows exactly what to post
- NEVER invent geographic or setting details — only reference location specifics (waterfront, patio, views) if they are explicitly mentioned in the restaurant profile above"""

    msg = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        ideas = json.loads(raw)
        # Inject real dates into each idea based on day name
        for idea in ideas:
            day_name = idea.get("day", "")
            idea["date"] = days_map.get(day_name, "")
        # Sort by date so calendar always shows Mon→Sun order
        day_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        ideas.sort(key=lambda x: day_order.index(x.get("day","Monday")) if x.get("day","") in day_order else 7)
        # Attach week_range to first idea for the UI to read
        if ideas:
            ideas[0]["week_range"] = week_range
        return ideas
    except Exception:
        return []


import json
