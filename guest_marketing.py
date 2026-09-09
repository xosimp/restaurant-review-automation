"""
guest_marketing.py — SMS lifecycle marketing to guests, not staff/owner.

notify.py already has everything needed to send an SMS (Twilio) and the
exact consent-gating pattern this needs (alert_contacts.sms_consent /
sms_consent_at) — this reuses both rather than re-inventing them, extended
to a new guest_contacts table since alert_contacts is specifically for
staff/owner alert routing, a different table with a different lifecycle.

Consent model mirrors alert_contacts exactly and for the same reason: an
owner manually adding a guest's number (from a receipt, a comment card)
is NOT the guest consenting to marketing texts — only add_guest_contact_
public_optin() (the guest submitting the public join page themselves) can
ever set consent=True. TCPA marketing consent has to come from the
recipient, not be asserted on their behalf.
"""
import os
from datetime import datetime, timedelta
import anthropic
from models import get_conn, DB_PATH
from notify import send_sms, _normalize_phone
from ai_utils import create_with_retry, extract_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guest_contacts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id           INTEGER NOT NULL REFERENCES restaurants(id),
    name                    TEXT,
    phone                   TEXT NOT NULL,
    consent                 INTEGER NOT NULL DEFAULT 0,
    consent_at              TEXT,
    unsubscribed            INTEGER NOT NULL DEFAULT 0,
    last_visit              TEXT,
    last_review_requested_at TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, phone)
);
CREATE TABLE IF NOT EXISTS guest_campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    message         TEXT NOT NULL,
    sent_count      INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sms_optin_invites (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
    phone          TEXT    NOT NULL,
    source         TEXT    NOT NULL DEFAULT 'toast_order',
    external_ref   TEXT,
    sent_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    responded_at   TEXT,
    response       TEXT,
    UNIQUE(restaurant_id, external_ref)
);
CREATE INDEX IF NOT EXISTS idx_optin_invites_phone ON sms_optin_invites(phone, sent_at);
"""


def init_guest_marketing(db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    # Migration for guest_contacts rows created before last_visit/
    # last_review_requested_at existed (same pattern as webhooks.init_webhooks).
    for col_sql in (
        "ALTER TABLE guest_contacts ADD COLUMN last_visit TEXT",
        "ALTER TABLE guest_contacts ADD COLUMN last_review_requested_at TEXT",
        # Segmentation: campaigns went to everyone consented, which is how a
        # win-back text reaches someone who ate here last night.
        "ALTER TABLE guest_contacts ADD COLUMN visit_count INTEGER DEFAULT 0",
        "ALTER TABLE guest_contacts ADD COLUMN last_campaign_at TEXT",
        # Campaign history was written and never read back; segment records
        # who a campaign actually went to.
        "ALTER TABLE guest_campaigns ADD COLUMN segment TEXT",
        "ALTER TABLE guest_campaigns ADD COLUMN segment_label TEXT",
        "ALTER TABLE guest_campaigns ADD COLUMN link_token TEXT",
        # The email channel. `weekly_email` has generated newsletters — two
        # subject-line options and all — since this module existed, and there
        # was no list to send one to and no way to send it, so the output was
        # something you copied into your own mail client by hand.
        "ALTER TABLE guest_contacts ADD COLUMN email TEXT",
        "ALTER TABLE guest_contacts ADD COLUMN email_consent INTEGER DEFAULT 0",
        "ALTER TABLE guest_contacts ADD COLUMN email_consent_at TEXT",
        "ALTER TABLE guest_contacts ADD COLUMN email_unsubscribed INTEGER DEFAULT 0",
        "ALTER TABLE guest_contacts ADD COLUMN email_token TEXT",
        "CREATE INDEX IF NOT EXISTS idx_guest_email_token ON guest_contacts(email_token)",
    ):
        try:
            conn.execute(col_sql)
        except Exception:
            pass
    conn.commit()
    conn.close()


# ── Quiet hours for guest texts ────────────────────────────────────────────
# TCPA restricts marketing calls and texts to 8am–9pm in the RECIPIENT's local
# time. Nothing in this module enforced that: send_campaign fired the moment
# the owner pressed the button, and run_review_request_followups is an hourly
# job that texts a guest three hours after their visit — so a 9pm dinner got a
# review request at midnight, automatically, every night, without anyone
# touching the app.
#
# models.is_in_quiet_hours is deliberately NOT reused here. That one is the
# owner's own alert preference: it is opt-in (no window set means no quiet
# hours at all) and it is hard-coded to America/Chicago. This is a legal floor
# that applies whether or not anyone configured anything, and it has to follow
# the restaurant's own timezone — which is the closest proxy available for the
# guest's, since a restaurant's guests are overwhelmingly local to it.
GUEST_SMS_EARLIEST_HOUR = 8    # 8:00 AM local
GUEST_SMS_LATEST_HOUR = 21     # 9:00 PM local — last send starts at 8:59 PM


def _sms_local_now(restaurant_id):
    """The restaurant's own wall clock. Its own function so tests can pin it
    to a specific hour without also freezing the visit-age arithmetic in
    run_review_request_followups, which reads the same clock for a different
    purpose."""
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(restaurant_id, naive=True)


def guest_sms_allowed_now(restaurant_id) -> bool:
    """True when a marketing text may legally be sent to this restaurant's
    guests right now. Fails CLOSED: if the restaurant's local time can't be
    resolved, no marketing text goes out."""
    try:
        return GUEST_SMS_EARLIEST_HOUR <= _sms_local_now(restaurant_id).hour < GUEST_SMS_LATEST_HOUR
    except Exception:
        return False


def guest_sms_window_label() -> str:
    """"8:00 AM and 9:00 PM" — for the message an owner sees when a campaign
    is held back, so the refusal reads as a rule and not a failure."""
    def _fmt(h):
        suffix = "AM" if h < 12 else "PM"
        return f"{h % 12 or 12}:00 {suffix}"
    return f"{_fmt(GUEST_SMS_EARLIEST_HOUR)} and {_fmt(GUEST_SMS_LATEST_HOUR)}"


def get_guest_contacts(restaurant_id, consent_only=False, db_path=DB_PATH):
    """consent_only=True is the enforcement point for actually sending SMS —
    same shape as notify.get_alert_contacts. Management UI wants
    consent_only=False so the owner can see (and remove) every contact,
    consented or not."""
    conn = get_conn(db_path)
    query = ("SELECT id, name, phone, consent, consent_at, unsubscribed, "
             "last_visit, last_review_requested_at, visit_count, last_campaign_at "
             "FROM guest_contacts WHERE restaurant_id=?")
    if consent_only:
        query += " AND consent=1 AND unsubscribed=0"
    rows = conn.execute(query + " ORDER BY id DESC", (restaurant_id,)).fetchall()
    conn.close()
    return [
        {"id": r["id"], "name": r["name"] or "", "phone": r["phone"],
         "consent": bool(r["consent"]), "consent_at": r["consent_at"],
         "unsubscribed": bool(r["unsubscribed"]), "last_visit": r["last_visit"],
         "last_review_requested_at": r["last_review_requested_at"],
         "visit_count": int(r["visit_count"] or 0),
         "last_campaign_at": r["last_campaign_at"]}
        for r in rows
    ]


def add_guest_contact_manual(restaurant_id, phone, name=None, db_path=DB_PATH):
    """Owner adding a number for their own reference/tracking — never
    consented, can never receive a campaign until the guest opts in
    themselves via the public join page."""
    return _upsert_contact(restaurant_id, phone, name=name, consent=False, db_path=db_path)


def add_guest_contact_public_optin(restaurant_id, phone, name=None, db_path=DB_PATH):
    """The guest submitting the public opt-in page themselves."""
    return _upsert_contact(restaurant_id, phone, name=name, consent=True, db_path=db_path)


def add_guest_contact_sms_optin(restaurant_id, phone, name=None, db_path=DB_PATH):
    """The guest texting YES back to an opt-in invite.

    Consent-equivalent to the public opt-in page: in both cases the guest
    themselves takes an affirmative action. A number Toast happened to
    capture at checkout is NOT this — that only ever reaches
    add_guest_contact_manual (consent=False), because handing a phone
    number to a POS for a receipt is not agreeing to marketing texts.
    """
    return _upsert_contact(restaurant_id, phone, name=name, consent=True, db_path=db_path)


def _upsert_contact(restaurant_id, phone, name, consent, db_path):
    phone = _normalize_phone(phone)
    conn = get_conn(db_path)
    existing = conn.execute(
        "SELECT id, consent FROM guest_contacts WHERE restaurant_id=? AND phone=?",
        (restaurant_id, phone)
    ).fetchone()
    now_iso = None
    if consent:
        from time_utils import restaurant_now_by_id
        now_iso = restaurant_now_by_id(restaurant_id, naive=True).isoformat()
    if existing:
        # Re-submitting the opt-in page (or re-adding the same number) only
        # ever upgrades consent, never revokes it silently — revoking is
        # unsubscribe()'s job specifically so it's an explicit action.
        if consent:
            # A guest scanning the table QR code and submitting is itself a
            # fresh visit signal — set last_visit=now every submission (not
            # just the first), so a repeat guest's automated review-request
            # follow-up re-fires for *this* visit. consent_at only gets set
            # once though (COALESCE) — consent doesn't need re-timestamping.
            conn.execute(
                "UPDATE guest_contacts SET consent=1, consent_at=COALESCE(consent_at,?), "
                "unsubscribed=0, name=COALESCE(?,name), last_visit=?, "
                "visit_count=COALESCE(visit_count,0)+1 WHERE id=?",
                (now_iso, name, now_iso, existing["id"])
            )
        elif name:
            conn.execute("UPDATE guest_contacts SET name=? WHERE id=?", (name, existing["id"]))
        conn.commit()
        contact_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO guest_contacts (restaurant_id, name, phone, consent, consent_at, "
            "last_visit, visit_count) VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, (name or "").strip() or None, phone, int(consent), now_iso,
             now_iso, 1 if consent else 0)
        )
        conn.commit()
        contact_id = cur.lastrowid
    conn.close()
    return contact_id


def delete_guest_contact(contact_id, restaurant_id, db_path=DB_PATH):
    """Scoped to restaurant_id — a client must never be able to delete
    another restaurant's contact by guessing an id."""
    conn = get_conn(db_path)
    conn.execute("DELETE FROM guest_contacts WHERE id=? AND restaurant_id=?", (contact_id, restaurant_id))
    conn.commit()
    conn.close()


def mark_guest_visit(contact_id, restaurant_id, db_path=DB_PATH):
    """Manual visit signal for contacts that don't have a natural opt-in-scan
    moment (e.g. added from a comment card, not the table QR code) — owner
    taps this right after serving them. Scoped to restaurant_id, same IDOR
    guard as delete_guest_contact."""
    from time_utils import restaurant_now_by_id
    now_iso = restaurant_now_by_id(restaurant_id, naive=True).isoformat()
    conn = get_conn(db_path)
    # visit_count as well as last_visit — segmentation asks "how many times",
    # which a single timestamp can't answer, so regulars and first-timers were
    # indistinguishable.
    conn.execute(
        "UPDATE guest_contacts SET last_visit=?, visit_count=COALESCE(visit_count,0)+1 "
        "WHERE id=? AND restaurant_id=?",
        (now_iso, contact_id, restaurant_id)
    )
    conn.commit()
    conn.close()


def unsubscribe_guest(restaurant_id, phone, db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE guest_contacts SET unsubscribed=1 WHERE restaurant_id=? AND phone=?",
        (restaurant_id, _normalize_phone(phone))
    )
    conn.commit()
    conn.close()


# ── Inbound SMS ─────────────────────────────────────────────────────────────
# Every outbound message this system sends promises "Reply STOP to
# unsubscribe". Until these handlers existed that promise was not actually
# kept by anything — a guest could text STOP and keep receiving messages.

STOP_KEYWORDS  = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke"}
START_KEYWORDS = {"start", "unstop", "yes", "y"}
HELP_KEYWORDS  = {"help", "info"}


def resubscribe_guest(restaurant_id, phone, db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE guest_contacts SET unsubscribed=0 WHERE restaurant_id=? AND phone=?",
        (restaurant_id, _normalize_phone(phone))
    )
    conn.commit()
    conn.close()


def record_optin_invite(restaurant_id, phone, source="toast_order", external_ref=None, db_path=DB_PATH):
    """Log that an opt-in invite went out. external_ref (e.g. a Toast order
    GUID) makes re-running the job idempotent — the UNIQUE constraint means
    the same order can never invite the same guest twice."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO sms_optin_invites (restaurant_id, phone, source, external_ref) VALUES (?,?,?,?)",
            (restaurant_id, _normalize_phone(phone), source, external_ref)
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        return None          # already invited for this external_ref
    finally:
        conn.close()


# How far back an unanswered invite still counts as "what they're replying
# to". Past this, a YES is not attributable to it.
_INVITE_REPLY_WINDOW_DAYS = 14


def _inbound_candidates(phone, db_path=DB_PATH):
    """Every restaurant this phone could plausibly be replying to, newest
    invite first, as [(restaurant_id, name)].

    Every restaurant shares one platform Twilio number, so the inbound `To`
    cannot identify the restaurant. This used to take the single most recent
    invite and treat it as the answer — which silently attributed a guest's
    YES to whichever restaurant happened to text last. A diner on two
    restaurants' lists could consent to one and be enrolled in the other's
    marketing, with that owner then seeing their phone number.
    """
    phone = _normalize_phone(phone)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.restaurant_id, r.name, MAX(i.sent_at) AS last_sent "
            "FROM sms_optin_invites i JOIN restaurants r ON r.id = i.restaurant_id "
            "WHERE i.phone=? AND i.responded_at IS NULL "
            "  AND i.sent_at >= datetime('now', ?) "
            "GROUP BY i.restaurant_id ORDER BY last_sent DESC",
            (phone, f"-{_INVITE_REPLY_WINDOW_DAYS} days")
        ).fetchall()
        if rows:
            return [(r["restaurant_id"], r["name"] or "") for r in rows]
        rows = conn.execute(
            "SELECT DISTINCT g.restaurant_id, r.name FROM guest_contacts g "
            "JOIN restaurants r ON r.id = g.restaurant_id WHERE g.phone=?",
            (phone,)
        ).fetchall()
        return [(r["restaurant_id"], r["name"] or "") for r in rows]
    finally:
        conn.close()


def _match_named_restaurant(body, candidates):
    """Did the guest name one of them in the reply itself?"""
    text = " ".join((body or "").lower().split())
    for rid, name in candidates:
        n = " ".join((name or "").lower().split())
        if n and n in text:
            return rid
    return None


def _restaurant_for_inbound(phone, db_path=DB_PATH):
    """The single restaurant this reply belongs to, or None when it is
    ambiguous. Never guesses — see _inbound_candidates."""
    candidates = _inbound_candidates(phone, db_path=db_path)
    return candidates[0][0] if len(candidates) == 1 else None


def _mark_invite_response(phone, response, db_path=DB_PATH, restaurant_id=None):
    """Close the invite this reply answers.

    `restaurant_id` pins which one. Without it (a STOP, which is global by
    design) the most recent unanswered invite is closed, but a YES must
    always pass the restaurant it was actually resolved to — closing the
    wrong restaurant's invite leaves the right one open forever and the
    guest never gets their review link.
    """
    from time_utils import restaurant_now_by_id
    conn = get_conn(db_path)
    try:
        if restaurant_id is not None:
            row = conn.execute(
                "SELECT id, restaurant_id FROM sms_optin_invites WHERE phone=? AND restaurant_id=? "
                "AND responded_at IS NULL ORDER BY sent_at DESC, id DESC LIMIT 1",
                (_normalize_phone(phone), restaurant_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, restaurant_id FROM sms_optin_invites WHERE phone=? AND responded_at IS NULL "
                "ORDER BY sent_at DESC, id DESC LIMIT 1", (_normalize_phone(phone),)
            ).fetchone()
        if not row:
            return
        now_iso = restaurant_now_by_id(row["restaurant_id"], naive=True).isoformat()
        conn.execute("UPDATE sms_optin_invites SET responded_at=?, response=? WHERE id=?",
                     (now_iso, response, row["id"]))
        conn.commit()
    finally:
        conn.close()


def handle_inbound_sms(from_phone, body, db_path=DB_PATH):
    """Process one inbound guest text. Returns a reply string to send back
    (or None to stay silent).

    A STOP unsubscribes the guest from EVERY restaurant that has their
    number, not just the one we think they were replying to — when someone
    says stop, the safe reading is stop, not "stop from this one tenant".
    """
    phone = _normalize_phone(from_phone)
    word = (body or "").strip().lower().split()[0] if (body or "").strip() else ""

    if word in STOP_KEYWORDS:
        conn = get_conn(db_path)
        conn.execute("UPDATE guest_contacts SET unsubscribed=1 WHERE phone=?", (phone,))
        conn.commit()
        conn.close()
        _mark_invite_response(phone, "stop", db_path=db_path)
        return "You're unsubscribed and won't get any more texts from us. Reply START to opt back in."

    candidates = _inbound_candidates(phone, db_path=db_path)
    if not candidates:
        return None          # nothing of ours — stay silent rather than guess

    if word in HELP_KEYWORDS:
        return "This is a guest text line for restaurant updates. Reply STOP to unsubscribe."

    if word in START_KEYWORDS or _match_named_restaurant(body, candidates):
        # One candidate is unambiguous. Several means two restaurants texted
        # this number and only the guest knows which they meant — consent
        # recorded against a guess is consent for a business they never
        # agreed to hear from, so ask instead of picking.
        restaurant_id = (candidates[0][0] if len(candidates) == 1
                         else _match_named_restaurant(body, candidates))
        if restaurant_id is None:
            names = " or ".join(n for _, n in candidates[:3] if n)
            return (f"Thanks! Which restaurant did you mean — {names}? "
                    "Reply with the name. Reply STOP to opt out of all of them.")
        conn = get_conn(db_path)
        row = conn.execute("SELECT name FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        conn.close()
        add_guest_contact_sms_optin(restaurant_id, phone, db_path=db_path)
        resubscribe_guest(restaurant_id, phone, db_path=db_path)
        _mark_invite_response(phone, "yes", db_path=db_path, restaurant_id=restaurant_id)
        name = row["name"] if row else "us"
        return f"Thanks! You're in — we'll text you a review link after your next visit to {name}. Reply STOP anytime."

    return None


CAMPAIGN_PROMPTS = {
    "win_back": "a friendly win-back text to a guest who hasn't visited in a while, inviting them back",
    "event": "a text announcing an upcoming event, special, or promotion",
    "loyalty": "a short thank-you/loyalty text rewarding a regular guest",
    "general": "a short promotional text on the topic given",
}


# ── Audience segments ──────────────────────────────────────────────────────
# A campaign went to every consented contact, full stop. That is how a
# "we miss you" text reaches someone who ate here last night, and it is the
# difference between a text club and a blast list: `win_back` was a TONE the
# copy was written in, never an AUDIENCE it was sent to.
#
# Every segment is a filter on rows this module already keeps — last_visit,
# visit_count — on top of the consent gate, which is never optional.

SEGMENTS = {
    "all": {
        "label": "Everyone consented",
        "help": "Every guest who opted in and hasn't unsubscribed.",
    },
    "lapsed_30": {
        "label": "Haven't been in 30+ days",
        "help": "Opted-in guests whose last recorded visit was over a month ago.",
    },
    "lapsed_60": {
        "label": "Haven't been in 60+ days",
        "help": "The ones drifting away rather than just busy.",
    },
    "regulars": {
        "label": "Regulars (3+ visits)",
        "help": "Guests you've recorded at least three visits for.",
    },
    "new": {
        "label": "First-timers",
        "help": "One recorded visit — the ones worth turning into regulars.",
    },
}

# A default pairing, so picking a tone suggests the audience it was written
# for instead of leaving the two unrelated.
CAMPAIGN_DEFAULT_SEGMENT = {
    "win_back": "lapsed_30",
    "loyalty": "regulars",
    "event": "all",
    "general": "all",
}


def segment_contacts(restaurant_id, segment="all", db_path=DB_PATH):
    """Consented, non-unsubscribed contacts matching `segment`.

    Consent is applied first and unconditionally — a segment can only ever
    narrow the eligible set, never widen it.
    """
    contacts = get_guest_contacts(restaurant_id, consent_only=True, db_path=db_path)
    segment = (segment or "all").strip().lower()
    if segment not in SEGMENTS:
        segment = "all"
    if segment == "all":
        return contacts

    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)

    def days_since_visit(c):
        raw = c.get("last_visit")
        if not raw:
            return None
        try:
            return (now - datetime.fromisoformat(str(raw)[:19])).days
        except Exception:
            return None

    out = []
    for c in contacts:
        visits = int(c.get("visit_count") or 0)
        gap = days_since_visit(c)
        if segment == "lapsed_30":
            # No recorded visit is not the same as a lapsed one — a guest who
            # joined at the table and never got marked doesn't belong in a
            # "we miss you" text.
            if gap is not None and gap >= 30:
                out.append(c)
        elif segment == "lapsed_60":
            if gap is not None and gap >= 60:
                out.append(c)
        elif segment == "regulars":
            if visits >= 3:
                out.append(c)
        elif segment == "new":
            if visits == 1:
                out.append(c)
    return out


def segment_counts(restaurant_id, db_path=DB_PATH) -> dict:
    """How many guests each segment would reach right now, so the owner picks
    an audience seeing its size rather than after sending to it."""
    return {key: len(segment_contacts(restaurant_id, key, db_path=db_path)) for key in SEGMENTS}


# ── Send frequency ─────────────────────────────────────────────────────────
# Nothing capped how often a guest could be texted. Quiet hours stop a message
# at midnight; this stops four messages on a Tuesday, which is the other half
# of not being the restaurant people mute.
GUEST_SMS_MIN_DAYS_BETWEEN = 3


def _too_soon(contact, now):
    last = contact.get("last_campaign_at")
    if not last:
        return False
    try:
        return (now - datetime.fromisoformat(str(last)[:19])).days < GUEST_SMS_MIN_DAYS_BETWEEN
    except Exception:
        return False


def draft_campaign_message(restaurant, campaign_type="general", topic=""):
    """AI-drafts a short SMS (under ~300 chars — a real SMS/MMS segment
    budget, not email) in the restaurant's own voice. Reuses marketing.py's
    profile lookup for brand voice instead of re-deriving it."""
    from marketing import get_profile_for_restaurant

    p = get_profile_for_restaurant(restaurant.id)
    intent = CAMPAIGN_PROMPTS.get(campaign_type, CAMPAIGN_PROMPTS["general"])
    never_clause = f" Never use these words or phrases: {p['never_say']}." if p.get("never_say") else ""
    # Same profile dict marketing.py's own generator uses menu_notes from —
    # this generator was silently dropping it, so a guest text campaign
    # could never reference an actual dish or special the way a social
    # post or review reply already can.
    menu_clause = f" Menu & current specials: {p['menu_notes']}. Reference something specific when it fits naturally." if p.get("menu_notes") else ""
    topic_clause = f" Topic/specifics to include: {topic}." if topic else ""

    prompt = (
        f"Write {intent} for {p['name']}, a {p['vibe']} in {p['neighborhood']}. "
        f"Brand voice: {p['voice']}.{never_clause}{menu_clause}{topic_clause}\n\n"
        "Rules: under 300 characters total (this is a real text message, not an email). "
        "No markdown, no emoji spam (at most one emoji). No links or phone numbers. "
        "End naturally — no 'reply STOP to unsubscribe' (that's added automatically). "
        "Return ONLY the message text, nothing else."
    )
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = create_with_retry(
        client,
        model=os.getenv("GUEST_MARKETING_MODEL", "claude-sonnet-5"),
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant.id,
        action="guest_campaign_draft",
    )
    return extract_text(message).strip()


def send_campaign(restaurant_id, message, db_path=DB_PATH, segment="all", link_token=None):
    """Send `message` to one SEGMENT of consented, non-unsubscribed guests.

    Returns {"ok": True, "sent", "failed", "total", "skipped_recent", ...}, or
    {"ok": False, "error"} when the quiet-hours window is closed.
    Never raises — one bad number must not stop the rest of the list.

    Three gates, in order, and all of them server-side so every caller (web,
    mobile, Ask Cavnar, a future scheduled campaign) inherits them:
      1. quiet hours   — nothing goes out between 9pm and 8am local
      2. consent       — only guests who opted in themselves
      3. frequency     — nobody gets two campaigns inside three days
    """
    if not guest_sms_allowed_now(restaurant_id):
        return {"ok": False, "blocked": "quiet_hours", "sent": 0, "failed": 0, "total": 0,
                "error": ("Guest texts only go out between "
                          f"{guest_sms_window_label()} in your local time. "
                          "Your message is ready — send it in the morning.")}

    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)

    audience = segment_contacts(restaurant_id, segment, db_path=db_path)
    eligible = [c for c in audience if not _too_soon(c, now)]
    skipped_recent = len(audience) - len(eligible)

    body = message.strip()
    if link_token:
        body = f"{body}\n{_short_link(link_token)}"
    full_message = body + "\n\nReply STOP to unsubscribe."

    sent, failed = 0, 0
    reached_ids = []
    for c in eligible:
        try:
            if send_sms(c["phone"], full_message):
                sent += 1
                reached_ids.append(c["id"])
            else:
                failed += 1
        except Exception:
            failed += 1

    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO guest_campaigns "
            "(restaurant_id, message, sent_count, failed_count, segment, segment_label, link_token) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, message.strip(), sent, failed, segment,
             SEGMENTS.get(segment, SEGMENTS["all"])["label"], link_token),
        )
        # Stamps the frequency cap. Only guests actually reached are stamped,
        # so a failed send doesn't lock someone out of the next campaign.
        for cid in reached_ids:
            conn.execute("UPDATE guest_contacts SET last_campaign_at=? WHERE id=?",
                         (now.strftime("%Y-%m-%dT%H:%M:%S"), cid))
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "sent": sent, "failed": failed, "total": len(eligible),
            "skipped_recent": skipped_recent, "segment": segment,
            "segment_label": SEGMENTS.get(segment, SEGMENTS["all"])["label"]}


def _short_link(token, base_url=None):
    base = (base_url or os.getenv("PUBLIC_BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")
    return f"{base}/g/{token}"


def campaign_history(restaurant_id, limit=20, db_path=DB_PATH) -> list:
    """What has been sent, to whom, and what it did.

    guest_campaigns has recorded every send since the table existed and
    nothing ever displayed it — an owner could not answer "did we already
    text about the wine dinner?" without asking Ask Cavnar.
    """
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT c.id, c.message, c.sent_count, c.failed_count, c.segment, "
            "       c.segment_label, c.link_token, c.created_at, "
            "       COALESCE(l.clicks, 0) AS clicks "
            "FROM guest_campaigns c "
            "LEFT JOIN marketing_links l ON l.token = c.link_token "
            "WHERE c.restaurant_id=? ORDER BY c.id DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def consent_ledger(restaurant_id, db_path=DB_PATH) -> dict:
    """The compliance picture, in one call.

    Consent has always been recorded — consent_at has been on every row since
    the table existed — and never shown anywhere. If someone ever asks how a
    number got on this list, this is the answer.
    """
    contacts = get_guest_contacts(restaurant_id, db_path=db_path)
    conn = get_conn(db_path)
    try:
        this_month = conn.execute(
            "SELECT COALESCE(SUM(sent_count),0) FROM guest_campaigns "
            "WHERE restaurant_id=? AND created_at >= date('now','start of month')",
            (restaurant_id,),
        ).fetchone()[0] or 0
        campaigns = conn.execute(
            "SELECT COUNT(*) FROM guest_campaigns WHERE restaurant_id=? "
            "AND created_at >= date('now','start of month')", (restaurant_id,),
        ).fetchone()[0] or 0
    finally:
        conn.close()
    return {
        "total": len(contacts),
        "textable": sum(1 for c in contacts if c["consent"] and not c["unsubscribed"]),
        "unsubscribed": sum(1 for c in contacts if c["unsubscribed"]),
        "no_consent": sum(1 for c in contacts if not c["consent"] and not c["unsubscribed"]),
        "texts_this_month": int(this_month),
        "campaigns_this_month": int(campaigns),
        "window": guest_sms_window_label(),
        "min_days_between": GUEST_SMS_MIN_DAYS_BETWEEN,
    }


# ── Automated post-visit review request ─────────────────────────────────────

DEFAULT_REVIEW_REQUEST_DELAY_HOURS = 3


def _google_review_link(place_id):
    return (f"https://search.google.com/local/writereview?placeid={place_id}"
            if place_id else "")


def run_toast_optin_invites(business_date=None, db_path=DB_PATH):
    """Daily job — invites guests Toast identified to opt in for themselves.

    The invite is a single transactional message tied to a visit that
    actually happened; it is deliberately NOT the review request and NOT a
    campaign. The guest's number is stored unconsented (same footing as an
    owner's manual add), and only a YES reply — handled in
    handle_inbound_sms — ever sets consent. That keeps the one rule this
    module is built around intact: consent comes from the guest.

    A guest already consented, already unsubscribed, or already invited for
    this order is skipped, so re-running is safe.
    """
    from datetime import date as _date
    import toast as _toast

    if business_date is None:
        business_date = _date.today()

    conn = get_conn(db_path)
    restaurants = conn.execute(
        "SELECT id, name FROM restaurants "
        "WHERE module_marketing=1 AND toast_client_id IS NOT NULL AND toast_restaurant_guid IS NOT NULL"
    ).fetchall()
    conn.close()

    invited, skipped, failed, deferred = 0, 0, 0, 0
    for r in restaurants:
        rid = r["id"]
        # This job is pinned to 11am on the SERVER's clock while restaurants
        # keep their own timezones, so "11am" is not 11am everywhere. An
        # opt-in invite is asking for marketing consent, which makes it a
        # marketing text — same window as everything else here.
        if not guest_sms_allowed_now(rid):
            deferred += 1
            continue
        try:
            customers = _toast.fetch_order_customers(rid, business_date)
        except Exception:
            failed += 1
            continue

        for cust in customers:
            phone = _normalize_phone(cust["phone"])
            conn = get_conn(db_path)
            existing = conn.execute(
                "SELECT consent, unsubscribed FROM guest_contacts WHERE restaurant_id=? AND phone=?",
                (rid, phone)
            ).fetchone()
            already_invited = conn.execute(
                "SELECT 1 FROM sms_optin_invites WHERE restaurant_id=? AND phone=?", (rid, phone)
            ).fetchone()
            conn.close()

            if existing and (existing["consent"] or existing["unsubscribed"]):
                skipped += 1
                continue
            if already_invited:
                skipped += 1
                continue

            # Stored unconsented — visible to the owner, textable only if
            # the guest replies YES.
            add_guest_contact_manual(rid, phone, name=cust.get("name") or None, db_path=db_path)

            if record_optin_invite(rid, phone, source="toast_order",
                                   external_ref=cust.get("order_guid"), db_path=db_path) is None:
                skipped += 1
                continue

            first = (cust.get("name") or "").split()[0] if cust.get("name") else "there"
            message = (
                f"Hi {first}, thanks for visiting {r['name']}! "
                "Reply YES if we can text you a quick link to leave a review. "
                "Reply STOP to opt out."
            )
            try:
                if send_sms(phone, message):
                    invited += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

    return {"invited": invited, "skipped": skipped, "failed": failed,
            "deferred_quiet_hours": deferred}


def run_review_request_followups(delay_hours=None, db_path=DB_PATH):
    """Hourly job (called from scheduler.py) — texts a Google-review link to
    any consented, non-unsubscribed guest whose last visit crossed the delay
    threshold, as long as they haven't already been asked about *this* visit.

    Eligibility is "last_review_requested_at is unset or older than
    last_visit" rather than an exact hour-window match — that makes it
    idempotent under scheduler downtime/late ticks (it just catches up next
    run instead of missing the window) and naturally re-arms on a genuinely
    new visit (see _upsert_contact, which bumps last_visit on every opt-in
    submission).

    last_visit/last_review_requested_at are stored in each restaurant's own
    local time (time_utils.py — restaurants can have different timezones),
    so the delay check is done in Python per-restaurant rather than in SQL
    against SQLite's UTC datetime('now'), which would be off by that
    restaurant's UTC offset.
    """
    from time_utils import restaurant_now_by_id

    if delay_hours is None:
        delay_hours = int(os.getenv("REVIEW_REQUEST_DELAY_HOURS", DEFAULT_REVIEW_REQUEST_DELAY_HOURS))

    conn = get_conn(db_path)
    rows = conn.execute(
        """
        SELECT gc.id AS contact_id, gc.restaurant_id, gc.name, gc.phone, gc.last_visit,
               r.name AS restaurant_name, r.google_place_id
        FROM guest_contacts gc
        JOIN restaurants r ON r.id = gc.restaurant_id
        WHERE gc.consent=1 AND gc.unsubscribed=0
          AND r.module_marketing=1
          AND gc.last_visit IS NOT NULL
          AND (gc.last_review_requested_at IS NULL OR gc.last_review_requested_at < gc.last_visit)
        """
    ).fetchall()

    sent, failed, skipped, deferred = 0, 0, 0, 0
    for row in rows:
        try:
            visited_at = datetime.fromisoformat(row["last_visit"])
        except Exception:
            continue
        now_local = restaurant_now_by_id(row["restaurant_id"], naive=True)
        if now_local - visited_at < timedelta(hours=delay_hours):
            continue  # not due yet
        # A 9pm dinner came due at midnight and this job, which runs hourly,
        # texted them. Eligibility here is "older than", never an exact
        # window, so holding a guest until 8am costs nothing — the next tick
        # inside the window picks them up and last_review_requested_at is
        # only written on an actual send.
        if not guest_sms_allowed_now(row["restaurant_id"]):
            deferred += 1
            continue

        review_url = _google_review_link(row["google_place_id"])
        if not review_url:
            skipped += 1
            continue

        first_name = (row["name"] or "").split()[0] if row["name"] else "there"
        message = (
            f"Hi {first_name}, thanks for visiting {row['restaurant_name']}! "
            f"We'd love your feedback — leave us a quick Google review: {review_url}"
            "\n\nReply STOP to unsubscribe."
        )
        try:
            ok = send_sms(row["phone"], message)
        except Exception:
            ok = False
        if ok:
            sent += 1
        else:
            failed += 1

        conn.execute(
            "UPDATE guest_contacts SET last_review_requested_at=? WHERE id=?",
            (now_local.isoformat(), row["contact_id"])
        )
        conn.execute(
            "INSERT INTO review_requests (restaurant_id, customer_name, customer_email, customer_phone, method, status) "
            "VALUES (?,?,?,?,?,?)",
            (row["restaurant_id"], row["name"] or "", "", row["phone"], "sms_auto", "sent" if ok else "failed")
        )
    conn.commit()
    conn.close()
    return {"sent": sent, "failed": failed, "skipped_no_place_id": skipped,
            "deferred_quiet_hours": deferred}
