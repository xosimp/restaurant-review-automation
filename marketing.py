"""
marketing.py — AI-powered marketing content generation for restaurants
"""
import json
import os
import anthropic
from ai_utils import create_with_retry, extract_text

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
    # that don't naturally drive restaurant visits or fit most concepts.
    # Veterans Day added: many restaurants run free/discounted meal promos
    # for veterans, which is a real measurable traffic driver, not just a
    # civic observance.
    fixed = [
        (1, 1, "New Year's Day"),
        (2, 14, "Valentine's Day"),
        (3, 17, "St. Patrick's Day"),
        (5, 5, "Cinco de Mayo"),
        (7, 4, "Fourth of July — summer cookout season"),
        (10, 31, "Halloween — great for themed specials"),
        (11, 11, "Veterans Day — many restaurants offer free/discounted meals for veterans"),
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

    # Easter Sunday — one of the single biggest brunch days of the year,
    # arguably outranking several holidays already on the fixed list, but
    # missing entirely since it doesn't fall on a fixed month/day. Anonymous
    # Gregorian algorithm (Meeus/Jones/Butcher) — the standard closed-form
    # computation for the date, not a lookup table.
    def _easter(y: int) -> datetime:
        a = y % 19
        b = y // 100
        c = y % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return datetime(y, month, day)

    calculated.append((_easter(year), "Easter — one of the biggest brunch days of the year"))

    # Super Bowl Sunday — huge for bars/wings/takeout. Not a fixed civic
    # date; approximated as the second Sunday of February, matching the
    # NFL's current schedule pattern under the 17-game season (recent Super
    # Bowls have landed there, not the first Sunday) — the League sets the
    # exact date each year, so this can drift by a week in years it doesn't.
    feb1 = datetime(year, 2, 1)
    first_sunday = feb1 + timedelta(days=(6 - feb1.weekday()) % 7)
    super_bowl = first_sunday + timedelta(weeks=1)
    calculated.append((super_bowl, "Super Bowl Sunday — one of the biggest days of the year for bars/takeout"))

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
        "label": "Instagram/FB post",
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

# Every one of these used to ask for length. instagram_post wanted TWO
# versions (a punchy one AND a 3-4 sentence story), google_promo allowed 100
# words, event_announcement wanted a post plus an email opener — so a tap on
# Generate returned a wall of text an owner had to read, choose from and cut
# down before it was postable. The 2-version format was also the source of
# posts going out with "Option 1 (Short & Punchy):" still in them.
#
# One piece, short, done. Regenerate is right there if the first one misses.
PROMPTS = {
    "instagram_post": """Write ONE Instagram caption for {restaurant} ({neighborhood}).
Vibe: {vibe}. Voice: {voice}.
Topic/occasion: {topic}
Known for: {known_for}

Length: 1-2 short sentences. Punchy. Say one thing well.
Then 4-6 relevant hashtags on their own line.

Reference specific dishes by name if menu items are given below — never invent one.
IMPORTANT: Only reference location details (water views, surroundings, setting) that are provided in the restaurant profile. Never invent or assume geographic details like "river", "ocean", "mountains" — use only what you are told.
No emojis unless one feels completely natural. No preamble, no alternatives, no labels — return the caption itself.
Do not use the phrases "indulge", "culinary journey", "delight", or "experience".""",

    "weekly_email": """Write a short weekly email for {restaurant} regulars.
Voice: {voice}. Neighborhood: {neighborhood}. Known for: {known_for}.
Topic/occasion: {topic}

Format exactly:
SUBJECT LINE: (one option, under 8 words)
BODY: (3-4 short sentences, conversational, like the owner typed it between shifts)

Mention a specific dish by name if menu items are given below.
No "Dear valued customer". No corporate sign-offs. End with a first name sign-off like "— Sarah" or "— the Maplewood team".""",

    "google_promo": """Write a Google Business Profile post for {restaurant} in {neighborhood}.
Topic: {topic}. Known for: {known_for}.

Length: 35-45 words, hard limit. Google truncates after the first line or two,
so the first sentence has to carry it on its own.
Direct, local, specific. One short call to action at the end.
Reference a specific menu item if provided below. No hashtags. No emojis.
Return the post only — no preamble, no alternatives.""",

    "loyalty_nudge": """Write ONE SMS re-engagement message for guests of {restaurant}.
Topic/offer: {topic}
Voice: {voice}

Rules: under 160 characters, hard limit. Personal, not automated. Includes the
restaurant name. Soft incentive if relevant. Return the message only — no
alternatives, no labels, no quotes around it.""",

    "happy_hour": """Write ONE social post promoting happy hour at {restaurant}.
Happy hour details: {topic}
Voice: {voice}.

Length: 1-2 short sentences, then 4-6 hashtags. Make people want to leave work early.
If the topic doesn't specify exact times or deals, write something that feels authentic without inventing specifics.
Return the post only — no alternatives, no labels.""",

    "event_announcement": """Write ONE social post announcing this for {restaurant}.
Event details: {topic}
Voice: {voice}. Neighborhood: {neighborhood}.

Length: 2 short sentences — what it is, and when — then 4-6 hashtags.
Never invent a date, time or price that isn't in the event details above.
Return the post only — no alternatives, no labels.""",
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


def log_content(restaurant_id: int, content_type: str, topic: str,
                post_id: str = None, post_platform: str = None):
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
            post_id TEXT,
            post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        try:
            conn.execute("ALTER TABLE marketing_content_log ADD COLUMN post_id TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE marketing_content_log ADD COLUMN post_platform TEXT")
        except Exception:
            pass
        if post_id:
            # Update the most recent unposted row for this topic instead of inserting a duplicate
            updated = conn.execute(
                """UPDATE marketing_content_log SET post_id=?, post_platform=?
                   WHERE id=(
                     SELECT id FROM marketing_content_log
                     WHERE restaurant_id=? AND topic=? AND post_id IS NULL
                     ORDER BY created_at DESC LIMIT 1
                   )""",
                (post_id, post_platform, restaurant_id, topic)
            ).rowcount
            if not updated:
                conn.execute(
                    "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform) VALUES (?,?,?,?,?)",
                    (restaurant_id, content_type, topic, post_id, post_platform)
                )
        else:
            conn.execute(
                "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform) VALUES (?,?,?,?,?)",
                (restaurant_id, content_type, topic, post_id, post_platform)
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

    # Avoid repeating recent themes — but never the topic the owner just
    # asked for. Pressing Regenerate on the same topic used to hand the model
    # "you have recently generated content about X. Do NOT repeat these
    # themes" while the instruction above said to write about X, and it would
    # answer the contradiction instead of the brief ("Since I already have two
    # prior posts...").
    recent = get_recent_content(restaurant_id, limit=5)
    asked_for = (topic or "").strip().lower()
    recent = [r for r in recent if (r.get("topic") or "").strip().lower() != asked_for]
    recent_context = ""
    if recent:
        recent_topics = ", ".join(
            f"{r['type'].replace('_',' ')} about {r['topic']}" for r in recent
        )
        recent_context = f"\n\nIMPORTANT: You have recently generated content about: {recent_topics}. Do NOT repeat these themes or topics. Be fresh and different."

    # Seasonal awareness with real date — the restaurant's date, not ours
    try:
        from time_utils import restaurant_now_by_id
        now_dt = restaurant_now_by_id(restaurant_id, naive=True)
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

    # What worked, what guests just said, and what the sky is doing. This was
    # a hand-rolled top-performers query that lived only here — the content
    # calendar, which plans a whole week, got none of it, and neither ever saw
    # a review or a forecast. See marketing_signals.generation_context.
    signal_context = ""
    if restaurant_id:
        try:
            from marketing_signals import generation_context
            signal_context = generation_context(restaurant_id)
        except Exception:
            signal_context = ""

    prompt = prompt_template.format(
        restaurant=p["name"],
        neighborhood=p["neighborhood"],
        vibe=p["vibe"],
        voice=p["voice"],
        known_for=p["known_for"],
        topic=topic,
    ) + location_context + recent_context + seasonal_context + never_clause + menu_clause + signal_context

    msg = create_with_retry(
        client,
        model=os.getenv("MARKETING_MODEL", "claude-sonnet-5"),
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="marketing_content",
    )
    result = extract_text(msg).strip()

    # Strip markdown formatting Claude sometimes adds
    import re as _re
    result = _re.sub('[*]{2}(.+?)[*]{2}', lambda m: m.group(1), result)
    result = _re.sub('[*](.+?)[*]', lambda m: m.group(1), result)
    # A markdown heading is "# Heading" — the space is required. Without it
    # this ate the "#" off the first hashtag of every caption whose tags
    # started a new line ("#GiaMia #TruffleSeason" came out "GiaMia
    # #TruffleSeason"), quietly breaking one tag on every Instagram post.
    result = _re.sub(r'^#{1,3}[ \t]+', '', result, flags=_re.MULTILINE)

    # Log this content for future memory
    log_content(restaurant_id, content_type, topic)

    return result


def mark_calendar_idea_used(restaurant_id: int, content_type: str, topic: str):
    """Track which calendar ideas were actually generated — feeds back into future calendar quality."""
    log_content(restaurant_id, f"calendar_{content_type}", topic)


# A forced regeneration inside this window returns what was just built
# instead. Long enough to cover a client giving up and the person tapping
# again; short enough that "generate a new week" a minute later still means it.
RECENT_CALENDAR_SECONDS = 120


def _week_start(restaurant_id):
    """The Sunday that starts this restaurant's current week, in its own
    local time — the cache key, and the same boundary the generator has
    always used for its day/date map."""
    from datetime import timedelta as _td
    from time_utils import restaurant_now_by_id as _rnbi
    now = _rnbi(restaurant_id, naive=True)
    return now - _td(days=(now.weekday() + 1) % 7)


def get_cached_calendar(restaurant_id: int, max_age_seconds: int = None):
    """This week's calendar if one has already been generated, else None.

    `max_age_seconds` narrows that to "generated very recently", which is how
    a retry avoids throwing away work that already finished. Generating takes
    several seconds; if the client gave up waiting and the person pressed the
    button again, the first call had usually completed and cached a perfectly
    good week — regenerating would discard it, pay for a second model call,
    and hand back a different answer to the same question.
    """
    if not restaurant_id:
        return None
    try:
        from models import get_conn
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT ideas_json, generated_at FROM content_calendar_cache "
                "WHERE restaurant_id=? AND week_start=?",
                (restaurant_id, _week_start(restaurant_id).strftime("%Y-%m-%d")),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row["ideas_json"]:
            return None
        if max_age_seconds is not None:
            from datetime import datetime as _dt
            try:
                # generated_at is SQLite's datetime('now') — UTC.
                age = (_dt.utcnow() - _dt.strptime(str(row["generated_at"])[:19],
                                                   "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                return None
            if age > max_age_seconds:
                return None
        return json.loads(row["ideas_json"])
    except Exception:
        return None


def _cache_calendar(restaurant_id: int, ideas: list):
    if not (restaurant_id and ideas):
        return
    try:
        from models import get_conn
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO content_calendar_cache "
                "(restaurant_id, week_start, ideas_json, generated_at) "
                "VALUES (?,?,?,datetime('now'))",
                (restaurant_id, _week_start(restaurant_id).strftime("%Y-%m-%d"), json.dumps(ideas)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log_msg = f"content calendar cache write failed for {restaurant_id}: {e}"
        print(log_msg)


def get_content_calendar_ideas(restaurant_id: int = None, force: bool = False) -> list[dict]:
    """A week of content ideas. Generated once per restaurant per week and
    cached from then on; `force=True` is the owner explicitly asking for a
    different week (the "Generate week" button).

    Before the cache, every single read ran a Sonnet generation — including
    the mobile Marketing tab's own load, which meant the calendar you saw on
    Tuesday was not the calendar you saw on Monday.
    """
    if not force:
        cached = get_cached_calendar(restaurant_id)
        if cached:
            return cached
    else:
        # Even a forced draw yields to one that just finished. See
        # get_cached_calendar — this is the "client timed out, person pressed
        # the button again" path, and regenerating there discards completed
        # work and answers the same question differently.
        just_made = get_cached_calendar(restaurant_id, max_age_seconds=RECENT_CALENDAR_SECONDS)
        if just_made:
            return just_made
    p = get_profile_for_restaurant(restaurant_id)
    from datetime import datetime as _dt, timedelta as _td
    from time_utils import restaurant_now_by_id as _rnbi
    now = _rnbi(restaurant_id, naive=True)
    # Sunday-based week — always show Sun through Sat of the CURRENT week
    # Week never changes until a new Sunday arrives
    days_since_sunday = (now.weekday() + 1) % 7  # Sun=0, Mon=1, ..., Sat=6
    start = now - _td(days=days_since_sunday)  # Most recent Sunday
    days_map = {}
    iso_map = {}
    for i in range(7):
        d = start + _td(days=i)
        dn = d.strftime("%A")
        days_map[dn] = d.strftime("%-m/%-d")
        # The phone needs an unambiguous date to know which day is today
        # and to print the week's range; "9/6" carries no year.
        iso_map[dn] = d.strftime("%Y-%m-%d")
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
    # The calendar plans a whole week and used to know only the profile and a
    # fixed holiday list — not which posts landed, not what guests are saying,
    # and not that Saturday is the first 75° day of the year.
    try:
        from marketing_signals import generation_context
        signal_block = generation_context(restaurant_id) if restaurant_id else ""
    except Exception:
        signal_block = ""
    never_clause = f"Never use these words or phrases: {p['never_say']}." if p.get('never_say') else ""

    prompt = f"""Generate a 7-day social media content calendar for {p['name']}, 
a {p['vibe']} in {p['neighborhood']}.

Known for: {p['known_for']}{menu_context}
Brand voice: {p['voice']}
{never_clause}
TODAY'S DATE: {today_str} (this is the real current date — do not assume any other date)
Upcoming holidays/events in the next 30 days: {upcoming_holidays if upcoming_holidays else "No major holidays"}
Recently generated content (avoid repeating these): {recent_topics}
{signal_block}

Return ONLY valid JSON — no markdown fences. Array of 7 objects with:
{{"day": "Monday", "platform": "Instagram & FB|Email|Google|SMS", "angle": "one short sentence, max 20 words", "type": "instagram_post|weekly_email|google_promo|happy_hour|loyalty_nudge"}}
"day" must be just the weekday name (e.g. "Monday") — never include a date.

Rules:
- Include at least one SMS/loyalty_nudge idea per week to re-engage guests
- Reference real menu items and dishes by name when menu info is provided
- For any upcoming holiday, make the content feel natural and relevant to THIS restaurant — skip it if it doesn't fit
- Vary platforms across the 7 days — don't use Instagram more than 3 times
- Make every idea specific enough that the owner knows exactly what to post
- NEVER invent geographic or setting details — only reference location specifics (waterfront, patio, views) if they are explicitly mentioned in the restaurant profile above"""

    msg = create_with_retry(
        client,
        model=os.getenv("MARKETING_MODEL", "claude-sonnet-5"),
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="content_calendar",
    )
    raw = extract_text(msg).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        ideas = json.loads(raw)
        # Inject real dates into each idea based on day name
        # Strip any date contamination from AI (e.g. "Thursday, June 5" -> "Thursday")
        valid_days = {"Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"}
        for idea in ideas:
            raw_day = idea.get("day", "")
            # Extract just the day name if AI added extra text
            day_name = raw_day.split(",")[0].split(" ")[0].strip()
            if day_name not in valid_days:
                # Try to find a valid day name anywhere in the string
                for vd in valid_days:
                    if vd in raw_day:
                        day_name = vd
                        break
            idea["day"] = day_name
            idea["date"] = days_map.get(day_name, "")
            idea["iso_date"] = iso_map.get(day_name, "")
        # Sort by date so calendar always shows Mon→Sun order
        day_order = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
        ideas.sort(key=lambda x: day_order.index(x.get("day","Sunday")) if x.get("day","") in day_order else 7)
        # Attach week_range to first idea for the UI to read
        if ideas:
            ideas[0]["week_range"] = week_range
        _cache_calendar(restaurant_id, ideas)
        return ideas
    except Exception:
        return []
