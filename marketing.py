"""
marketing.py — AI-powered marketing content generation for restaurants
"""
import json
import re
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
    # Names and dates only, never an offer: "Veterans Day — many restaurants
    # offer free/discounted meals" and "Halloween — great for themed
    # specials" read to the model as a promotion to write, and the
    # restaurant had agreed to none (Marketing audit AI-3 / #16).
    fixed = [
        (1, 1, "New Year's Day"),
        (2, 14, "Valentine's Day"),
        (3, 17, "St. Patrick's Day"),
        (5, 5, "Cinco de Mayo"),
        (7, 4, "Fourth of July — summer cookout season"),
        (10, 31, "Halloween"),
        (11, 11, "Veterans Day"),
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
            # The restaurant's own site (Settings → menu link): the one link
            # public copy may carry without the owner typing it (AUX-1).
            "website":      getattr(r, "menu_url", None) or "",
        }
    except Exception:
        return DEFAULT_PROFILE

CONTENT_TYPES = [
    # `channel` is where the piece goes. The Content tab publishes to social
    # accounts only: a text or an email type is written and sent from the
    # Campaign Studio (text / email channel), never offered Instagram,
    # Facebook or a schedule (Marketing audit AUX-4 / UX-3 — a win-back SMS
    # and a "SUBJECT LINE: … BODY:" email could be posted to Facebook).
    {
        "id": "instagram_post",
        "label": "Instagram/FB post",
        "icon": "camera",
        "channel": "social",
        "description": "Caption + hashtags for a food or ambiance photo",
    },
    {
        "id": "weekly_email",
        "label": "Weekly email",
        "icon": "mail",
        "channel": "email",
        "description": "An email to your list, written and sent from Campaigns",
    },
    {
        "id": "google_promo",
        "label": "Google post",
        "icon": "search",
        "channel": "social",
        "description": "Short promotional post for Google Business Profile",
    },
    {
        "id": "loyalty_nudge",
        "label": "Re-engagement text",
        "icon": "message",
        "channel": "text",
        "description": "A text to your guests, written and sent from Campaigns",
    },
    {
        "id": "happy_hour",
        "label": "Happy hour promo",
        "icon": "glass",
        "channel": "social",
        "description": "Social post for your happy hour, in your own details",
    },
    {
        "id": "event_announcement",
        "label": "Event announcement",
        "icon": "calendar",
        "channel": "social",
        "description": "Post announcing a special dinner, wine night, or seasonal menu",
    },
]

# Where each kind of piece goes: "social" (a post), "text" or "email" (the
# Campaign Studio). guest_sms is the quiet-night job's old guest-text draft.
CONTENT_CHANNELS = {ct["id"]: ct["channel"] for ct in CONTENT_TYPES}
CONTENT_CHANNELS["guest_sms"] = "text"


def content_channel(content_type) -> str:
    """"social", "text" or "email" for a content type; an unknown or empty
    type is a social post (the Content tab's default)."""
    return CONTENT_CHANNELS.get(str(content_type or "").strip(), "social")


def is_social_type(content_type) -> bool:
    """Whether a piece of this type may be published or scheduled to a
    social account (Instagram, Facebook, Google)."""
    return content_channel(content_type) == "social"


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
No "Dear valued customer". No corporate sign-offs. {sign_off_rule}""",

    "google_promo": """Write a Google Business Profile post for {restaurant} in {neighborhood}.
Topic: {topic}. Known for: {known_for}.

Length: 35-45 words, hard limit. Google truncates after the first line or two,
so the first sentence has to carry it on its own.
Direct, local, specific. One short call to action at the end.
Reference a specific menu item if provided below. No hashtags. No emojis.
Return the post only — no preamble, no alternatives.""",

    "loyalty_nudge": """Write ONE SMS re-engagement message for guests of {restaurant}.
Topic: {topic}
Voice: {voice}

Rules: under 160 characters, hard limit. Personal, not automated. Includes the
restaurant name. Return the message only — no alternatives, no labels, no
quotes around it.""",

    "happy_hour": """Write ONE social post promoting happy hour at {restaurant}.
Happy hour details: {topic}
Voice: {voice}.

Length: 1-2 short sentences, then 4-6 hashtags. Make people want to leave work early.
If the details don't give exact days, times, prices or deals, write it without any: never invent them.
Return the post only — no alternatives, no labels.""",

    "event_announcement": """Write ONE social post announcing this for {restaurant}.
Event details: {topic}
Voice: {voice}. Neighborhood: {neighborhood}.

Length: 2 short sentences — what it is, and when — then 4-6 hashtags.
Never invent a date, time or price that isn't in the event details above.
Return the post only — no alternatives, no labels.""",
}

# The hard rules every public piece is written under, the same four the
# newsletter and the guest text carry (Marketing audit AUX-1 / #17) — and
# response_validation holds the draft to them (invented_offers,
# invented_specifics), so a draft that breaks one is refused, not published.
PUBLIC_COPY_RULES = (
    "\n\nHard rules — a draft that breaks one is thrown away:\n"
    "1. No offer: no discount, percentage or dollars off, free item, half price, 2-for-1, BOGO, deals, "
    "giveaway or anything on the house, unless the owner's words above say it, in those words.\n"
    "2. No price, date, time or event that isn't written above.\n"
    "3. Nothing is new, back, better or changed unless the owner's words above say so.\n"
    "4. No links, email addresses or phone numbers unless they are written above."
)
# Said when the topic is a suggestion (a calendar idea, the quiet-night job),
# not something the owner typed: it is never where an offer comes from (AI-2).
SUGGESTED_TOPIC_RULE = ("\nThe topic above is a suggestion, not the owner's words: it is never a source for an offer, "
                        "a price, a date, a time or an event.")

# What a calendar idea may carry for the owner and a public topic never may
# (AUX-15 / #88): "Feature Short Rib — your best-margin plate (22% food
# cost)" is the owner's card; the post is about "Feature Short Rib".
_INTERNAL_TOPIC_RE = re.compile(
    r"\s*[—–-]+\s*your\s+best[\s-]margin\s+plate\b[^\n]*$"
    r"|\s*\(?\s*\d+(?:\.\d+)?\s?%\s*(?:food|plate|drink|pour|beverage)\s+cost\s*\)?"
    r"|\s*\(?\s*(?:food|plate)\s+cost\s*(?:of|at|:)?\s*\d+(?:\.\d+)?\s?%\s*\)?", re.I)


def public_topic(topic) -> str:
    """The topic as the public may see it: an internal cost figure and the
    margin label taken out. Every generator reads its topic through this."""
    t = _INTERNAL_TOPIC_RE.sub("", str(topic or ""))
    return " ".join(t.split()).strip(" —–-")


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
                post_id: str = None, post_platform: str = None, body: str = None, tags: dict = None,
                content_log_id: int = None, user: dict = None):
    """Log generated content for memory — and tag it (marketing_tags.py)
    with the dish, occasion and kind it is about, so its result can be read
    against them. A post that went live also starts the month's observed
    sales tracker (outcomes.observe). `content_log_id` is the generated row a
    publish completes (MB-8). Returns the row's id.

    A publish a person made (`user`, the route's login) is a piece that went
    out: its caption is measured against the model's draft
    (marketing_voice.record_final; memory audit 9/29/26, mkt_edits). A
    scheduled publish has no person — its draft's approval was recorded."""
    if not restaurant_id:
        return
    row_id = _log_content_row(restaurant_id, content_type, topic, post_id, post_platform,
                              content_log_id=content_log_id)
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
        if user and body:
            try:
                import marketing_voice
                marketing_voice.record_final(restaurant_id, content_channel(content_type), body, "post_publish",
                                             ref_id=row_id, user=user, content_log_id=content_log_id)
            except Exception:
                pass
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


# A new row's metrics are NULL — unmeasured — not the columns' DEFAULT 0
# (MB-7): marketing_signals.MEASURED_SQL.
_INSERT_ROW_SQL = ("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, "
                   "origin, posted_at, reach, impressions, engaged, likes, comments, shares) "
                   "VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL)")


def _log_content_row(restaurant_id, content_type, topic, post_id, post_platform, content_log_id=None):
    """The row write, returning the row's id (the updated or inserted one).
    Each new row records its origin (_content_origin) so a count of pieces
    can leave out calendar markers and job drafts.

    A publish (post_id) stamps posted_at — the time it went out, which the
    attribution window and "last post" read (MB-8) — and completes the
    generated row it names by `content_log_id` (this restaurant's, not yet
    posted). Without one it is its own row: "the most recent unposted row
    for this topic" attached a Friday post to Monday's generation, or a
    stale topic to the wrong row."""
    row_id = None
    origin = _content_origin(content_type)
    try:
        from models import get_conn
        conn = get_conn()
        target = None
        if post_id and content_log_id:
            target = conn.execute(
                "SELECT id FROM marketing_content_log WHERE id=? AND restaurant_id=? "
                "AND (post_id IS NULL OR TRIM(post_id) = '')", (int(content_log_id), restaurant_id)).fetchone()
        if target:
            conn.execute("UPDATE marketing_content_log SET post_id=?, post_platform=?, "
                         "posted_at=COALESCE(posted_at, datetime('now')) WHERE id=?",
                         (post_id, post_platform, target["id"]))
            row_id = target["id"]
        else:
            import datetime as _dt
            posted_at = (_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                         if post_id else None)
            cur = conn.execute(_INSERT_ROW_SQL, (restaurant_id, content_type, topic, post_id, post_platform,
                                                 origin, posted_at))
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
    parts = [p.get("known_for"), p.get("vibe"), p.get("neighborhood"), p.get("voice"), p.get("menu_notes"),
             p.get("website"), topic]
    return " ".join(str(x) for x in parts if x)


def validate_marketing_text(text, restaurant_id, surface, profile=None, topic="", untrusted=(),
                            action="marketing_content", given="", topic_is_owner=True):
    """`text` through response_validation on a public marketing surface
    (social_post, calendar_idea): an rv.Validated str ("" when refused, in
    enforce mode) carrying `.verdict`. No marker: public text never had it."""
    import response_validation as rv
    ctx = marketing_context(restaurant_id, surface, profile, topic=topic, untrusted=untrusted, action=action,
                            given=given, topic_is_owner=topic_is_owner)
    return rv.enforce(text or "", ctx, marker=False)


def marketing_context(restaurant_id, surface, profile=None, topic="", untrusted=(), action="marketing_content",
                      given="", topic_is_owner=True):
    """The ValidationContext for owner-profile marketing text: offer_source
    is what the owner wrote (_owner_source — the topic only when the owner
    typed it: a calendar angle a model wrote is never the owner saying so,
    AI-2), never_say the restaurant's list, names the restaurant's own,
    tenant_names_denied every other tenant. The figures in the owner's words
    are typed facts and the owner's words the context, so a figure a draft
    states is held to them (F1); `given` is what the system handed the model
    as fact — today's date, the holiday dates — which may back a date and
    never an offer (response_validation.invented_specifics)."""
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
    source = _owner_source(p, public_topic(topic) if topic_is_owner else "")
    return rv.ValidationContext(
        restaurant_id=restaurant_id, surface=surface, names_allowed=names, tenant_names_denied=tenants,
        never_say=(p or {}).get("never_say") or "", offer_source=source,
        facts=rv.owner_facts(source), context_text=" ".join(x for x in (source, given) if x),
        untrusted=[u for u in (untrusted or ()) if u],
        policy={"action": action, "given_text": given or "", "context_facts": True})


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
                     restaurant_id: int = None, topic_is_owner: bool = True, user_id: int = None) -> str:
    """Generate marketing content for a given type and topic.

    `topic_is_owner` is whether the owner typed the topic. A calendar idea's
    angle ("Write this") and a scheduled job's topic were written by a model
    or by us: the draft is written from them, but nothing in them — an
    offer, a price, a date, an event — counts as the owner saying so (AI-2).
    Every draft is held to one public-copy guard set (response_validation:
    the shared offer vocabulary, invented prices / times / dates / events /
    new-back-better-changed claims / links, the sign-off name), with the
    owner's figures as typed facts (AUX-1).

    Memory audit 9/29/26 (mkt_edits): the prompt carries the owner's voice on
    this channel (marketing_voice.voice_block — their edits, three pieces
    they sent in their own words, what their regenerated drafts had in
    common), and the draft is kept (marketing_voice.record_draft, `user_id`
    the person who asked) so what goes out can be measured against it; its
    id rides on the text as `draft_ref`."""
    from datetime import datetime
    prompt_template = PROMPTS.get(content_type, PROMPTS["instagram_post"])
    p = get_profile_for_restaurant(restaurant_id)
    # A cost figure a calendar card carries for the owner is never part of
    # a public topic (AUX-15 / #88). The log keeps the words as asked, so a
    # later publish of the same topic completes this row.
    asked_topic = topic
    topic = public_topic(topic)
    # The owner picking "Happy hour promo" is the owner saying there is one.
    owner_topic = topic if topic_is_owner else ""
    if topic_is_owner and content_type == "happy_hour":
        owner_topic = f"happy hour {topic}".strip()

    # Avoid repeating recent themes — but never the topic the owner just
    # asked for. Pressing Regenerate on the same topic used to hand the model
    # "you have recently generated content about X. Do NOT repeat these
    # themes" while the instruction above said to write about X, and it would
    # answer the contradiction instead of the brief ("Since I already have two
    # prior posts...").
    recent = get_recent_content(restaurant_id, limit=5)
    asked_for = {(topic or "").strip().lower(), (asked_topic or "").strip().lower()}
    recent = [r for r in recent if (r.get("topic") or "").strip().lower() not in asked_for]
    recent_context = ""
    if recent:
        recent_topics = ", ".join(
            f"{r['type'].replace('_',' ')} about {public_topic(r['topic'])}" for r in recent
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

    # The owner's own voice on this channel, learned from what they changed
    # and what they threw away (memory audit 9/29/26, mkt_edits), and what
    # Cavnar AI remembers about the restaurant (memory_context, surface
    # 'marketing'): the owner's constraints, goals, answers, what worked.
    voice_context = ""
    memory_block = ""
    if restaurant_id:
        try:
            import marketing_voice
            voice_context = marketing_voice.voice_block(restaurant_id, content_channel(content_type))
        except Exception:
            voice_context = ""
        memory_block = marketing_memory_block(restaurant_id)

    # The weekly email signs off as the restaurant actually does (its
    # sign-off name, else its own name) — never an invented "— Sarah".
    sign_off = (p.get("sign_off_name") or p.get("name") or "").strip()
    sign_off_rule = (f'End with this sign-off on its own line: "— {sign_off}". No other name.' if sign_off
                     else "No sign-off name.")
    prompt = prompt_template.format(
        restaurant=p["name"],
        neighborhood=p["neighborhood"],
        vibe=p["vibe"],
        voice=p["voice"],
        known_for=p["known_for"],
        topic=topic,
        sign_off_rule=sign_off_rule,
    ) + (location_context + recent_context + seasonal_context + never_clause + menu_clause + signal_context
         + voice_context + memory_block)
    # A topic can be the owner's aim ("Fill Tuesday dinner", the Opportunity
    # Feed's slow night); said to the public it announces a slow night.
    prompt += ("\nThe topic may be the owner's own aim. Never say or hint that a night is slow, quiet or empty, "
               "or that the restaurant wants to fill tables.")
    prompt += PUBLIC_COPY_RULES + ("" if topic_is_owner else SUGGESTED_TOPIC_RULE)

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
    # built from (known for, vibe, voice, menu notes, their website) and the
    # topic they typed — not a calendar angle or a job's topic (AI-2). What
    # guests said (the signal block) is never a source. Today's date and the
    # holiday dates the prompt carried may back a date, never an offer.
    given = f"Today's date: {today_date}. Upcoming holidays: {upcoming or 'none'}."
    result = validate_marketing_text(result, restaurant_id, "social_post", p, topic=owner_topic,
                                     untrusted=[signal_context] if signal_context else (),
                                     action="marketing_content", given=given)
    if result.verdict is not None and result.verdict.verdict == "refuse":
        raise ValueError(f"marketing copy rejected: {refusal_detail(result.verdict)}")

    # Log this content for future memory — the owner's own topic, not the
    # public one (AUX-15). Its row id travels with the text, so a publish
    # completes THIS row (MB-8), not a guess by topic.
    row_id = log_content(restaurant_id, content_type, asked_topic)
    try:
        result.content_log_id = row_id
    except AttributeError:
        pass
    # The model's text, kept so the piece that goes out is measured against
    # it (marketing_voice; mkt_edits). A scheduled job's draft has no person.
    try:
        import marketing_voice
        draft_ref = marketing_voice.record_draft(restaurant_id, content_channel(content_type), str(result),
                                                 "post" if user_id else "job", user_id=user_id,
                                                 content_log_id=row_id)
        result.draft_ref = draft_ref
    except Exception:
        pass

    return result


def marketing_memory_block(restaurant_id, subjects=()) -> str:
    """memory_context for a marketing generator (surface 'marketing'), as a
    fenced, M/D/YY-dated prompt block, or "" — never raises into a draft.
    Context for what to write, never a source for an offer or a claim (the
    public-copy guard's offer source is still only the owner's profile)."""
    try:
        import memory_context
        text = memory_context.memory_context(restaurant_id, "marketing", subjects=subjects).text
    except Exception:
        return ""
    if not text:
        return ""
    return ("\n\nWHAT CAVNAR AI REMEMBERS ABOUT THIS RESTAURANT — context for what to write, never a source "
            "for an offer, a price, a date or a claim:\n" + text + "\n")


def calendar_idea_key(angle) -> str:
    """A content-calendar idea's rec_ledger key: its angle, normalised
    (insight_store.line_key) — the same idea in another week's draw is the
    same recommendation, and an answer to it holds."""
    import insight_store
    return insight_store.line_key("content_idea", angle or "")


def mark_calendar_idea_used(restaurant_id: int, content_type: str, topic: str):
    """Track which calendar ideas were actually generated — the next week's
    calendar reads them as the kinds of angle the owner picks
    (chosen_calendar_angles; memory audit 9/29/26 — they used to come back
    only as "avoid repeating these") — and record the idea as taken on the
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


def chosen_calendar_angles(restaurant_id: int, limit: int = 5) -> list:
    """The calendar angles the owner chose to write from, newest first (the
    calendar_* markers mark_calendar_idea_used logs). An angle only an admin
    took through view-as (its calendar answer on the trail carries
    authority 'admin' — memory audit 9/29/26, "view_as") is support at work,
    not the owner's pick. Never raises."""
    if not restaurant_id:
        return []
    try:
        from models import get_conn
        conn = get_conn()
        try:
            rows = conn.execute("SELECT topic FROM marketing_content_log WHERE restaurant_id=? "
                                "AND content_type LIKE 'calendar\\_%' ESCAPE '\\' AND TRIM(COALESCE(topic, '')) != '' "
                                "ORDER BY created_at DESC, id DESC LIMIT ?", (restaurant_id, int(limit) * 3)).fetchall()
            try:
                admin_only = {r["key"] for r in conn.execute(
                    "SELECT key FROM rec_events WHERE restaurant_id=? AND event='accepted' "
                    "AND key LIKE 'content_idea:%' GROUP BY key "
                    "HAVING MIN(CASE WHEN COALESCE(authority, '') = 'admin' THEN 1 ELSE 0 END) = 1",
                    (restaurant_id,)).fetchall()}
            except Exception:        # a database from before the ledger's authority column
                admin_only = set()
        finally:
            conn.close()
        out = []
        for r in rows:
            angle = public_topic(r["topic"])
            if angle and calendar_idea_key(r["topic"]) not in admin_only:
                out.append(angle)
            if len(out) >= int(limit):
                break
        return out
    except Exception:
        return []


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


PAST_CALENDAR_WEEKS = 4


def past_calendar_angles(restaurant_id: int, weeks: int = PAST_CALENDAR_WEEKS, limit: int = 20) -> list:
    """The ideas of the last `weeks` weeks' calendars, newest first — the
    rows content_calendar_cache kept and nothing read (memory audit 9/29/26,
    "dead_memory"): the calendar prompt now sees them as "don't repeat", so
    a slow Tuesday is not the same happy-hour post four weeks running."""
    if not restaurant_id:
        return []
    try:
        from models import get_conn
        this_week = _week_start(restaurant_id).strftime("%Y-%m-%d")
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT ideas_json FROM content_calendar_cache WHERE restaurant_id=? AND week_start < ? "
                "ORDER BY week_start DESC LIMIT ?", (restaurant_id, this_week, int(weeks))).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            ideas = json.loads(r["ideas_json"] or "[]") or []
        except (TypeError, ValueError):
            continue
        for i in ideas:
            angle = str((i or {}).get("angle") or "").strip() if isinstance(i, dict) else ""
            if angle and angle not in out:
                out.append(angle[:160])
    return out[:limit]


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
        # The card is the owner's, and keeps the figure that earned it; the
        # post written from it is about "Feature <dish>" only - generate_content
        # reads every topic through public_topic, so neither the food-cost
        # figure nor the margin label reaches a public prompt or post (#88),
        # whichever client sends the angle as the topic.
        idea = {"day": day, "date": days_map.get(day, ""), "iso_date": iso_map.get(day, ""),
                "platform": "Instagram & FB", "type": "instagram_post",
                "angle": f"Feature {best['name']} — your best-margin plate ({best['food_cost_pct']:.0f}% food cost)",
                "source": "menu_margins"}
        # It takes its day (one idea a day): appended, it made two Thursdays.
        return [i for i in ideas if i.get("source") != "menu_margins" and i.get("day") != day] + [idea]
    except Exception:
        return ideas


CALENDAR_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _day_name(raw) -> str:
    """The weekday a model wrote, without a date it may have added
    ("Thursday, June 5" -> "Thursday"); "" when there is none."""
    raw = str(raw or "")
    name = raw.split(",")[0].split(" ")[0].strip()
    if name in CALENDAR_DAYS:
        return name
    return next((d for d in CALENDAR_DAYS if d in raw), "")


def _one_per_day(ideas):
    """One idea a day, Sunday to Saturday; a deterministic idea (menu
    margins) keeps its day over a model's."""
    seen, out = set(), []
    for idea in sorted(ideas, key=lambda x: 0 if x.get("source") == "menu_margins" else 1):
        day = idea.get("day")
        if day in seen:
            continue
        seen.add(day)
        out.append(idea)
    out.sort(key=lambda x: CALENDAR_DAYS.index(x["day"]) if x.get("day") in CALENDAR_DAYS else 7)
    return out


def _fill_missing_days(restaurant_id, prompt, ideas, days_map, iso_map, profile, untrusted=()):
    """Ask once more for the days the week is missing (owner, 9/28/26:
    Monday had no card). A day goes missing when the model skips it or the
    validation refuses its idea - dropped alone, and nothing refilled it. One
    short call for just those days, through the same validation; a day that
    is still empty stays empty (the page says so) rather than be invented."""
    missing = [d for d in CALENDAR_DAYS if d not in {i.get("day") for i in ideas}]
    if not missing:
        return ideas
    import data_health
    try:
        msg = create_with_retry(
            get_client(),
            model=model_for("marketing"),
            max_tokens=600,
            messages=[{"role": "user", "content": prompt + "\n\nThe rest of the week is planned. Only these days are "
                       f"still open: {', '.join(missing)}. Return ONLY a JSON array with one object for each of "
                       "them, in the same shape."}],
            restaurant_id=restaurant_id,
            action="content_calendar",
            readiness=data_health.NOT_APPLICABLE,
        )
        if getattr(msg, "stop_reason", None) == "max_tokens":
            return ideas
        extra = _calendar_ideas(extract_text(msg), message=msg) or []
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="content_calendar_fill", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return ideas
    fill, taken = [], set()
    for idea in extra:
        if not isinstance(idea, dict):
            continue
        day = _day_name(idea.get("day"))
        if day in missing and day not in taken:
            taken.add(day)
            idea["day"], idea["date"], idea["iso_date"] = day, days_map.get(day, ""), iso_map.get(day, "")
            fill.append(idea)
    return ideas + _validated_ideas(restaurant_id, fill, profile, untrusted=untrusted)


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
        new["validation"] = dict(worst or {"verdict": "pass", "caveats": [], "controls": True, "codes": [],
                                           "version": rv.VERSION})
        # Which public-copy vocabulary checked it: a week cached under an
        # older one is checked again on read (_revalidated).
        new["validation"]["public"] = rv.PUBLIC_COPY_VERSION
        kept.append(new)
    if dropped and not kept:
        # An AI-quality finding (fix round G #58), not a failing job.
        import ai_utils as _ai_q
        _ai_q.record_quality_event("content_calendar", "validation_refused", restaurant_id=restaurant_id,
                                   action="content_calendar", n=dropped,
                                   detail=f"content calendar: all {dropped} ideas refused by response validation")
    return kept


def _revalidated(restaurant_id, ideas):
    """A cached week, with any idea not checked by this engine version
    checked now (no model call) — a week cached before the engine, or under
    an older version, never reaches the owner unchecked."""
    import response_validation as rv
    stale = [i for i in ideas or [] if isinstance(i, dict) and i.get("source") != "menu_margins"
             and ((i.get("validation") or {}).get("version") != rv.VERSION
                  or (i.get("validation") or {}).get("public") != rv.PUBLIC_COPY_VERSION)]
    if not stale:
        return ideas
    week_range = next((i.get("week_range") for i in ideas if isinstance(i, dict) and i.get("week_range")), None)
    out = _validated_ideas(restaurant_id, ideas)
    if out and week_range and not out[0].get("week_range"):
        out[0]["week_range"] = week_range
    return out


def _calendar_ideas(text, message=None):
    """The week's ideas from the model's reply: a JSON array of objects, or
    the same array wrapped in an object ({"ideas": [...]}), with any preamble
    or code fence around it. The wrapped shape used to be read as free text,
    fail, and reach the owner as an empty week with no reason (AI-26). With
    `message` (what create_with_retry returned), a reply with no week in it
    is re-filed 'unparseable' in the ledger (fix round G, #52)."""
    from ai_utils import parse_json_reply

    def _week(v):
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
        if isinstance(v, dict):
            for inner in v.values():
                if isinstance(inner, list) and inner and all(isinstance(x, dict) for x in inner):
                    return inner
        return None

    return _week(parse_json_reply(text, accept=lambda v: _week(v) is not None, message=message))


def _calendar_week_context(restaurant_id, iso_map, now):
    """(prompt block, rules) for the content calendar: which date each of
    the calendar's weekdays is (M/D/YY — the model put Halloween on the
    wrong day with only "(Oct 31)" to go on), where an idea can go (the
    accounts connected, the text and email lists' sizes — it was required
    to include SMS without knowing whether anyone could be texted), and the
    Opportunity Feed's measured cards, so the week plans around what the
    feed found instead of competing with it (re-audit AUX-3 / AUX-9). Every
    part is optional; never raises."""
    from time_utils import mdy
    lines = []
    try:
        today_iso = now.date().isoformat()
        days = []
        for dn in CALENDAR_DAYS:
            iso = iso_map.get(dn)
            if iso:
                days.append(f"{dn} {mdy(iso)}" + (" (today)" if iso == today_iso else
                                                  (" (already past)" if iso < today_iso else "")))
        if days:
            lines.append("THIS WEEK'S DATES: " + ", ".join(days) + ".")
    except Exception:
        pass
    try:
        from marketing_publish import channels_for
        ch = channels_for(restaurant_id)
        on = [label for key, label in (("instagram", "Instagram"), ("facebook", "Facebook"), ("google", "Google"))
              if ch.get(key)]
        lines.append("Accounts connected to post to: " + (", ".join(on) if on else
                                                          "none yet (a social idea is written for the owner to post)")
                     + ".")
    except Exception:
        pass
    texts = emails = None
    try:
        import guest_marketing
        texts = len(guest_marketing.segment_contacts(restaurant_id, "all"))
    except Exception:
        pass
    try:
        import guest_email
        emails = guest_email.subscriber_count(restaurant_id)
    except Exception:
        pass
    if texts is not None or emails is not None:
        lines.append(f"Guests who can be texted: {texts if texts is not None else 'not known'}. "
                     f"Guests on the email list: {emails if emails is not None else 'not known'}.")
    try:
        import marketing_opportunities
        feed = marketing_opportunities.context_lines(restaurant_id)
    except Exception:
        feed = []
    if feed:
        lines.append("What Cavnar AI measured for this week (the owner sees these as cards; plan ideas around "
                     "them where they fit and never contradict them):\n" + "\n".join(f"- {c['line']}" for c in feed))
    rules = []
    if texts:
        rules.append("Include at least one SMS/loyalty_nudge idea this week to re-engage the guests who can be texted")
    elif texts == 0:
        rules.append("Nobody can be texted yet: no SMS or loyalty_nudge ideas")
    if emails == 0:
        rules.append("Nobody is on the email list yet: no Email or weekly_email ideas")
    if not rules:
        rules.append("Suggest an SMS or email idea only for a list the owner has")
    return (("\n" + "\n".join(lines)) if lines else ""), "\n- ".join(rules)


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
    # The last four weeks' calendar ideas (Cavnar AI's own earlier output,
    # fenced as text that is not an instruction): not to be repeated.
    past_angles = past_calendar_angles(restaurant_id)
    if past_angles:
        from ai_guard import wrap_untrusted as _wu_cal
        past_block = ("\nIdeas already given in the last four weeks' calendars (do not repeat them; "
                      "a new angle on the same dish is fine): " + _wu_cal("; ".join(past_angles)))
    else:
        past_block = ""

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
    # What the week holds and where ideas can go (re-audit AUX-3 / AUX-9):
    # the calendar never saw the Opportunity Feed, the accounts connected,
    # the list sizes or which date each weekday is — so Halloween landed on
    # the wrong day and an SMS idea was required of a restaurant with no
    # text list.
    week_block, sms_rule = _calendar_week_context(restaurant_id, iso_map, now)
    # What the owner picks and how they write (memory audit 9/29/26,
    # mkt_edits): the angles they chose to write from are the kinds they
    # like — offered again as kinds, never as the same topic — and their
    # voice on social. Plus what Cavnar AI remembers (memory_context).
    chosen = chosen_calendar_angles(restaurant_id) if restaurant_id else []
    chosen_block = (("\nAngles the owner chose to write from lately (the KINDS of idea they pick — offer fresh ones "
                     "of these kinds, never the same topic): " + "; ".join(chosen)) if chosen else "")
    try:
        import marketing_voice
        voice_block = marketing_voice.voice_block(restaurant_id, "social") if restaurant_id else ""
    except Exception:
        voice_block = ""
    memory_block = marketing_memory_block(restaurant_id) if restaurant_id else ""

    prompt = f"""Generate a 7-day social media content calendar for {p['name']},
a {p['vibe']} in {p['neighborhood']}.

Known for: {p['known_for']}{menu_context}
Brand voice: {p['voice']}
{never_clause}
TODAY'S DATE: {today_str} (this is the real current date — do not assume any other date)
Upcoming holidays/events in the next 30 days: {upcoming_holidays if upcoming_holidays else "No major holidays"}
Recently generated content (avoid repeating these): {recent_topics}{past_block}{chosen_block}
{signal_block}{week_block}{voice_block}{memory_block}

Return ONLY valid JSON — no markdown fences. Array of 7 objects with:
{{"day": "Monday", "platform": "Instagram & FB|Email|Google|SMS", "angle": "one short sentence, max 20 words", "type": "instagram_post|weekly_email|google_promo|happy_hour|loyalty_nudge"}}
"day" must be just the weekday name (e.g. "Monday") — never include a date.

Rules:
- {sms_rule}
- Put a holiday idea only on the weekday THIS WEEK'S DATES gives its date; a holiday whose date isn't one of them belongs to another week
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
        ideas = _calendar_ideas(extract_text(msg), message=msg)
        # Inject real dates into each idea based on day name, without any
        # date the model added to it ("Thursday, June 5" -> "Thursday").
        for idea in ideas:
            day_name = _day_name(idea.get("day", ""))
            idea["day"] = day_name
            idea["date"] = days_map.get(day_name, "")
            idea["iso_date"] = iso_map.get(day_name, "")
        # Each idea's owner-visible text through the Response Validation
        # Layer (calendar_idea): this had no guard at all (NS6 A2). A
        # refused idea is dropped on its own; the rest of the week stands.
        untrusted = [signal_block] if signal_block else ()
        ideas = _validated_ideas(restaurant_id, ideas, p, untrusted=untrusted)
        ideas = _with_margin_idea(restaurant_id, ideas, days_map, iso_map)
        # Every day of the week has an idea: the missing ones are asked for
        # once, then one a day, Sunday to Saturday.
        ideas = _one_per_day(_fill_missing_days(restaurant_id, prompt, _one_per_day(ideas), days_map, iso_map, p,
                                                untrusted=untrusted))
        # Attach week_range to first idea for the UI to read
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


# ── memory_context provider (memory audit 9/29/26, workstream M6) ───────────

def memory_lines(req):
    """memory_context provider 'marketing': what measurably worked in this
    restaurant's marketing, and the owner's marketing voice, as memory lines.

    For the surfaces that do not build those blocks themselves — Ask above
    all. The marketing generators carry richer blocks of their own
    (marketing_signals.generation_context, guest_marketing.returns_block,
    marketing_voice.voice_block) and the reply drafter has its own voice
    learning, so on 'marketing' and 'reply_drafter' this says nothing rather
    than say it twice inside the budget. A login that may not read Marketing
    gets nothing; nor does a restaurant that may not teach a learner
    (models.learning_eligible). A line that names a dish is fenced (the
    name is the owner's); the rest are system sentences. Never raises."""
    if req.surface in ("marketing", "reply_drafter"):
        return []
    rid = req.restaurant_id
    try:
        if req.viewer:
            from permissions import MARKETING_VIEW, has_permission
            if not has_permission(req.viewer, MARKETING_VIEW):
                return []
        import models as _m
        if not _m.learning_eligible(rid):
            return []
    except Exception:
        return []
    kw = {"db_path": req.db_path} if req.db_path else {}
    out = []
    try:
        import marketing_signals
        for line in marketing_signals.measured_lines(rid, **kw)[:4]:
            out.append({"text": f"Posts here — {line} (before and after, not proof).", "source": "system",
                        "subject": "marketing", "module": "marketing", "weight": 3.0,
                        "trusted": 'dish "' not in line})
    except Exception as e:
        print(f"[marketing] memory: post results unavailable for {rid}: {e}")
    try:
        import guest_marketing
        for seg, r in sorted(guest_marketing.segment_returns(rid, **kw).items(),
                             key=lambda kv: -kv[1]["back_per_100"])[:3]:
            out.append({"text": (f"Guest texts to \"{r['label']}\": {r['back_per_100']:g} came back per 100 texted "
                                 f"over {r['campaigns']} campaigns (matched against check-ins; before and after, "
                                 "not proof)."),
                        "source": "system", "subject": "marketing", "module": "marketing", "weight": 2.0,
                        "trusted": True})
    except Exception as e:
        print(f"[marketing] memory: text returns unavailable for {rid}: {e}")
    try:
        import marketing_voice
        for line in marketing_voice.summary_lines(rid, **kw):
            out.append({"text": line, "source": "system", "subject": "marketing", "module": "marketing",
                        "weight": 1.0, "trusted": True})
    except Exception as e:
        print(f"[marketing] memory: voice unavailable for {rid}: {e}")
    try:
        import review_signals
        conv = review_signals.request_conversion(rid, **kw)
        if conv.get("pct") is not None:
            out.append({"text": (f"Review requests: {conv['reviewed']} of {conv['asked']} guests asked left a review "
                                 f"within {conv['window_days']} days ({conv['pct']:g}%; matched by the guest's name)."),
                        "source": "system", "subject": "reviews", "module": "reviews", "weight": 1.5,
                        "trusted": True})
    except Exception as e:
        print(f"[marketing] memory: review requests unavailable for {rid}: {e}")
    return out
