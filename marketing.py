"""
marketing.py — AI-powered marketing content generation for restaurants
"""
import json
from ai_utils import create_with_retry, extract_text, get_client, model_for


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
                post_id: str = None, post_platform: str = None, body: str = None, tags: dict = None):
    """Log generated content for memory — and tag it (marketing_tags.py)
    with the dish, occasion and kind it is about, so its result can be read
    against them. A post that went live also starts the month's observed
    sales tracker (outcomes.observe)."""
    if not restaurant_id:
        return
    row_id = _log_content_row(restaurant_id, content_type, topic, post_id, post_platform)
    if row_id:
        try:
            import marketing_tags
            marketing_tags.tag_row(row_id, restaurant_id, topic, body, overrides=tags)
        except Exception as e:
            try:
                import ops
                ops.capture(e, job="marketing_tags", context=f"restaurant_id={restaurant_id}")
            except Exception:
                pass
    if post_id:
        try:
            import outcomes
            outcomes.observe(restaurant_id, "post_published", detail=(topic or "")[:60])
        except Exception:
            pass
        post_went_live(restaurant_id, post_id)
    return row_id


# The Home cards a published post carries out (home_brief "Get a post out
# this week", "Generate your first post").
POST_REC_KEYS = ("post_this_week", "first_post")


def post_went_live(restaurant_id, post_id, db_path=None):
    """A post is live: the posting recommendations were implemented, not only
    answered (rec_ledger, ROI #27) — recorded only for one that was shown.
    Never raises."""
    try:
        import rec_ledger
        kw = {"db_path": db_path} if db_path else {}
        rec_ledger.implemented(restaurant_id, list(POST_REC_KEYS), "marketing", source_ref=f"post:{post_id}",
                               meta={"module": "marketing"}, **kw)
    except Exception as e:
        print(f"[marketing] implementation not recorded for {restaurant_id}: {e}")


def _content_origin(content_type):
    """Who a content-log row stands for: 'marker' (a calendar idea marked
    used — not a piece of content), 'job' (drafted by a scheduled job, no
    person asked), or 'owner'."""
    if str(content_type or "").startswith("calendar_"):
        return "marker"
    try:
        from flask import has_request_context
        return "owner" if has_request_context() else "job"
    except Exception:
        return "owner"


# A real marketing piece: anything published, or content a person asked for
# that is not a calendar marker. Scheduled-job drafts (origin 'job') and
# calendar markers are log rows, not work anyone did. One definition, read
# by every count of "pieces" and by value_delivered's cost avoidance (M-15,
# M-27) — they each used to count raw rows.
REAL_PIECE_SQL = ("((post_id IS NOT NULL AND TRIM(post_id) != '') "
                  "OR (COALESCE(origin, 'owner')='owner' AND content_type NOT LIKE 'calendar\\_%' ESCAPE '\\'))")
# One piece, however many times it was regenerated: a published row is its
# own piece; unposted drafts of the same type and topic on the same day are
# one piece (every Regenerate writes a row).
PIECE_ID_SQL = ("(CASE WHEN post_id IS NOT NULL AND TRIM(post_id) != '' THEN 'p' || id "
                "ELSE content_type || '|' || LOWER(TRIM(COALESCE(topic, ''))) || '|' || substr(created_at, 1, 10) END)")


def count_pieces(restaurant_id, since_modifier, db_path=None) -> int:
    """Distinct real pieces since `since_modifier` (an SQLite date modifier
    such as 'start of month' or '-7 days')."""
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        return conn.execute(
            f"SELECT COUNT(DISTINCT {PIECE_ID_SQL}) FROM marketing_content_log WHERE restaurant_id=? "
            f"AND created_at >= datetime('now', ?) AND {REAL_PIECE_SQL}",
            (restaurant_id, since_modifier)).fetchone()[0] or 0
    finally:
        conn.close()


def pieces_this_month(restaurant_id, db_path=None) -> int:
    """Real marketing pieces this calendar month: content a person generated,
    plus anything published. "Pieces this month" counted every log row, so
    a calendar idea marked used and the weekly job's own draft each added
    one — two rows for one post the owner never saw (audit)."""
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        try:
            return conn.execute(
                f"SELECT COUNT(DISTINCT {PIECE_ID_SQL}) FROM marketing_content_log WHERE restaurant_id=? "
                "AND created_at >= date('now','start of month') "
                f"AND {REAL_PIECE_SQL}",
                (restaurant_id,)).fetchone()[0] or 0
        except Exception:
            return conn.execute(
                "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? "
                "AND created_at >= date('now','start of month') AND content_type NOT LIKE 'calendar\\_%' ESCAPE '\\'",
                (restaurant_id,)).fetchone()[0] or 0
    finally:
        conn.close()


def _log_content_row(restaurant_id, content_type, topic, post_id, post_platform):
    """The row write, returning the row's id (the updated or inserted one).
    Each new row records its origin (_content_origin) so a count of pieces
    can leave out calendar markers and job drafts."""
    row_id = None
    origin = _content_origin(content_type)
    try:
        from models import get_conn
        conn = get_conn()
        if post_id:
            # Update the most recent unposted row for this topic instead of inserting a duplicate
            target = conn.execute(
                "SELECT id FROM marketing_content_log WHERE restaurant_id=? AND topic=? AND post_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1", (restaurant_id, topic)).fetchone()
            if target:
                conn.execute("UPDATE marketing_content_log SET post_id=?, post_platform=? WHERE id=?",
                             (post_id, post_platform, target["id"]))
                row_id = target["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, origin) "
                    "VALUES (?,?,?,?,?,?)",
                    (restaurant_id, content_type, topic, post_id, post_platform, origin)
                )
                row_id = cur.lastrowid
        else:
            cur = conn.execute(
                "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, origin) "
                "VALUES (?,?,?,?,?,?)",
                (restaurant_id, content_type, topic, post_id, post_platform, origin)
            )
            row_id = cur.lastrowid
        conn.commit()
        conn.close()
    except Exception as e:
        # The marketing log is what "what did we post" is answered from —
        # losing a row silently makes it quietly wrong.
        try:
            import ops
            ops.capture(e, job="log_content", context=f"restaurant_id={restaurant_id} {content_type}")
        except Exception:
            pass
    return row_id


# ── the public-text check (Response Validation Layer) ─────────────────────

def _owner_source(p, topic=""):
    """What the owner wrote about the restaurant, as one string: the
    profile fields a marketing prompt is built from, plus the topic the owner
    asked for. An offer, an award or a sourcing claim whose words are here is
    theirs to publish (response_validation P1, offer_source)."""
    p = p or {}
    parts = [p.get("known_for"), p.get("vibe"), p.get("neighborhood"), p.get("voice"), p.get("menu_notes"), topic]
    return " ".join(str(x) for x in parts if x)


def validate_marketing_text(text, restaurant_id, surface, profile=None, topic="", untrusted=(),
                            action="marketing_content"):
    """`text` through response_validation on a public marketing surface
    (social_post, calendar_idea): an rv.Validated str ("" when refused, in
    enforce mode) carrying `.verdict`. No marker: public text never had it."""
    import response_validation as rv
    ctx = marketing_context(restaurant_id, surface, profile, topic=topic, untrusted=untrusted, action=action)
    return rv.enforce(text or "", ctx, marker=False)


def marketing_context(restaurant_id, surface, profile=None, topic="", untrusted=(), action="marketing_content"):
    """The ValidationContext for owner-profile marketing text: offer_source
    is what the owner wrote (_owner_source), never_say the restaurant's list,
    names the restaurant's own, tenant_names_denied every other tenant."""
    import response_validation as rv
    p = profile if profile is not None else get_profile_for_restaurant(restaurant_id)
    tenants = set()
    if restaurant_id:
        try:
            import models as _m
            tenants = _m.other_tenant_names(restaurant_id)
        except Exception:
            tenants = set()
    names = {n for n in ((p or {}).get("name"), (p or {}).get("sign_off_name")) if n}
    return rv.ValidationContext(
        restaurant_id=restaurant_id, surface=surface, names_allowed=names, tenant_names_denied=tenants,
        never_say=(p or {}).get("never_say") or "", offer_source=_owner_source(p, topic),
        untrusted=[u for u in (untrusted or ()) if u], policy={"action": action})


def refusal_detail(verdict) -> str:
    """The first refusing finding, worded for the owner ("the copy contains
    'x'", "award or ranking claim ('famous')")."""
    why = next((f for f in (verdict.findings if verdict else []) if f.get("severity") == "refuse"), None)
    if not why:
        return "it makes a claim the restaurant never made"
    detail, span = why.get("detail") or "", why.get("span") or ""
    if span and span.lower() not in detail.lower():
        return f"{detail} ('{span}')"
    return detail


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

    import data_health
    msg = create_with_retry(
        get_client(),
        model=model_for("marketing"),
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="marketing_content",
        # Rests on no data source: a social post drafted from the owner's topic.
        readiness=data_health.NOT_APPLICABLE,
    )
    result = extract_text(msg).strip()
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("marketing copy was truncated")

    # Strip markdown formatting Claude sometimes adds
    import re as _re
    result = _re.sub('[*]{2}(.+?)[*]{2}', lambda m: m.group(1), result)
    result = _re.sub('[*](.+?)[*]', lambda m: m.group(1), result)
    # A markdown heading is "# Heading" — the space is required. Without it
    # this ate the "#" off the first hashtag of every caption whose tags
    # started a new line ("#GiaMia #TruffleSeason" came out "GiaMia
    # #TruffleSeason"), quietly breaking one tag on every Instagram post.
    result = _re.sub(r'^#{1,3}[ \t]+', '', result, flags=_re.MULTILINE)

    # This copy is published to Instagram, Facebook and Google Business
    # Profile. Hashtags and links are fine here — a claim the restaurant
    # cannot make about itself is not. The Response Validation Layer on
    # social_post (workstream A) replaces the bare check_marketing_copy: the
    # same closure / health-department / never-say check, plus an offer, an
    # award ("famous", "voted", "#1"), a sourcing or allergen claim the owner
    # never wrote, fault and inspection claims, another tenant's name.
    # What the owner wrote is the offer source: the profile the prompt was
    # built from (known for, vibe, voice, menu notes) and the topic they
    # asked for. What guests said (the signal block) is never a source.
    result = validate_marketing_text(result, restaurant_id, "social_post", p, topic=topic,
                                     untrusted=[signal_context] if signal_context else (),
                                     action="marketing_content")
    if result.verdict is not None and result.verdict.verdict == "refuse":
        raise ValueError(f"marketing copy rejected: {refusal_detail(result.verdict)}")

    # Log this content for future memory
    log_content(restaurant_id, content_type, topic)

    return result


def calendar_idea_key(angle) -> str:
    """A content-calendar idea's rec_ledger key: its angle, normalised
    (insight_store.line_key) — the same idea in another week's draw is the
    same recommendation, and an answer to it holds."""
    import insight_store
    return insight_store.line_key("content_idea", angle or "")


def mark_calendar_idea_used(restaurant_id: int, content_type: str, topic: str):
    """Track which calendar ideas were actually generated — feeds back into
    future calendar quality — and record the idea as taken on the
    recommendation trail: writing from an idea is the owner's answer to it
    (#41)."""
    log_content(restaurant_id, f"calendar_{content_type}", topic)
    if restaurant_id and topic:
        try:
            import rec_ledger
            from datetime import date as _date
            key = calendar_idea_key(topic)
            rec_ledger.record(restaurant_id, key, "accepted", surface="marketing",
                              meta={"module": "marketing", "via": "wrote_from_calendar"},
                              source_ref=f"calendar:{key}:{_date.today().isoformat()}")
        except Exception as e:
            print(f"[marketing] calendar acceptance not recorded rid={restaurant_id}: {e}")


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
        return _revalidated(restaurant_id, json.loads(row["ideas_json"]))
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


def _with_margin_idea(restaurant_id, ideas, days_map, iso_map):
    """Food Cost joins the calendar: the priced dish with the best margin
    becomes one of the week's ideas, with the figure that earned it. Only
    when margins are real (three or more priced recipes) — a calendar
    must never suggest featuring a dish on a margin it cannot compute."""
    if not restaurant_id:
        return ideas
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        if not r or not getattr(r, "module_inventory", 0):
            return ideas
        import inventory_ledger
        priced = [p for p in (inventory_ledger.menu_profitability(restaurant_id).get("priced") or [])
                  if p.get("food_cost_pct") is not None and p.get("sell_price")]
        if len(priced) < 3:
            return ideas
        best = min(priced, key=lambda p: p["food_cost_pct"])
        day = "Thursday" if "Thursday" in days_map else next(iter(days_map), "")
        idea = {"day": day, "date": days_map.get(day, ""), "iso_date": iso_map.get(day, ""),
                "platform": "Instagram & FB", "type": "instagram_post",
                "angle": f"Feature {best['name']} — your best-margin plate ({best['food_cost_pct']:.0f}% food cost)",
                "source": "menu_margins"}
        return [i for i in ideas if i.get("source") != "menu_margins"] + [idea]
    except Exception:
        return ideas


# The fields of a calendar idea that are structure, not words an owner reads
# as the idea (day, dates, platform and type are enums the code fills in).
_IDEA_STRUCTURAL = frozenset({"day", "date", "iso_date", "week_range", "platform", "type", "source", "validation",
                              "rec_key", "written", "shown"})


def _validated_ideas(restaurant_id, ideas, profile=None, untrusted=()):
    """Each model-written idea's text fields through response_validation
    (calendar_idea). A refused idea is dropped — alone: one bad angle does not
    cost the owner the week. A kept idea carries `validation` (the verdict
    payload, whose version lets a cached week be re-checked when the engine
    changes). Deterministic ideas (source menu_margins) are not model text
    and pass through untouched."""
    import response_validation as rv
    ctx = marketing_context(restaurant_id, "calendar_idea", profile, untrusted=untrusted, action="content_calendar")
    kept, dropped = [], 0
    for idea in ideas or []:
        if not isinstance(idea, dict):
            continue
        if idea.get("source") == "menu_margins":
            kept.append(idea)
            continue
        new, refused, verdicts = dict(idea), False, []
        for k, v in idea.items():
            if k in _IDEA_STRUCTURAL or not isinstance(v, str) or not v.strip():
                continue
            out = rv.enforce(v, ctx, marker=False)
            if out.verdict is not None and out.verdict.verdict == "refuse":
                refused = True
                break
            new[k] = str(out)
            verdicts.append(out.validation)
        if refused:
            dropped += 1
            continue
        worst = max(verdicts, key=lambda x: rv.VERDICTS.index(x["verdict"]) if x else -1, default=None)
        new["validation"] = worst or {"verdict": "pass", "caveats": [], "controls": True, "codes": [],
                                      "version": rv.VERSION}
        kept.append(new)
    if dropped and not kept:
        try:
            import ops
            ops.capture(RuntimeError(f"content calendar: all {dropped} ideas refused by response validation"),
                        job="content_calendar", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
    return kept


def _revalidated(restaurant_id, ideas):
    """A cached week, with any idea not checked by this engine version
    checked now (no model call) — a week cached before the engine, or under
    an older version, never reaches the owner unchecked."""
    import response_validation as rv
    stale = [i for i in ideas or [] if isinstance(i, dict) and i.get("source") != "menu_margins"
             and ((i.get("validation") or {}).get("version") != rv.VERSION)]
    if not stale:
        return ideas
    week_range = next((i.get("week_range") for i in ideas if isinstance(i, dict) and i.get("week_range")), None)
    out = _validated_ideas(restaurant_id, ideas)
    if out and week_range and not out[0].get("week_range"):
        out[0]["week_range"] = week_range
    return out


def _calendar_ideas(text):
    """The week's ideas from the model's reply: a JSON array of objects, or
    the same array wrapped in an object ({"ideas": [...]}), with any preamble
    or code fence around it. The wrapped shape used to be read as free text,
    fail, and reach the owner as an empty week with no reason (AI-26)."""
    from ai_utils import parse_json_reply

    def _week(v):
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
        if isinstance(v, dict):
            for inner in v.values():
                if isinstance(inner, list) and inner and all(isinstance(x, dict) for x in inner):
                    return inner
        return None

    return _week(parse_json_reply(text, accept=lambda v: _week(v) is not None))


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

    import data_health
    msg = create_with_retry(
        get_client(),
        model=model_for("marketing"),
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id,
        action="content_calendar",
        # Rests on no data source: calendar ideas from the profile and holidays.
        readiness=data_health.NOT_APPLICABLE,
    )
    # A week cut off at max_tokens is not a parse error to swallow into an
    # empty list — it is a failure the caller has to be able to name (AI-26).
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("the content calendar was cut off before the week was finished")
    try:
        ideas = _calendar_ideas(extract_text(msg))
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
        # Each idea's owner-visible text through the Response Validation
        # Layer (calendar_idea): this had no guard at all (NS6 A2). A
        # refused idea is dropped on its own; the rest of the week stands.
        ideas = _validated_ideas(restaurant_id, ideas, p, untrusted=[signal_block] if signal_block else ())
        # Sort by date so calendar always shows Mon→Sun order
        day_order = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
        ideas.sort(key=lambda x: day_order.index(x.get("day","Sunday")) if x.get("day","") in day_order else 7)
        # Attach week_range to first idea for the UI to read
        ideas = _with_margin_idea(restaurant_id, ideas, days_map, iso_map)
        if ideas:
            ideas[0]["week_range"] = week_range
        _cache_calendar(restaurant_id, ideas)
        return ideas
    except Exception as e:
        # An empty calendar and a failed generation looked identical to every
        # caller and to us. The list stays empty (the UI handles that), but
        # the failure reaches the daily digest instead of vanishing.
        try:
            import ops
            ops.capture(e, job="content_calendar", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return []
