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
import re
from datetime import datetime, timedelta
from models import get_conn, DB_PATH
from notify import send_sms, _normalize_phone
from ai_utils import create_with_retry, extract_text, get_client, model_for

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
-- A STOP that outlives the contact row it was recorded on (CLIENT-34).
-- Deleting a guest used to hard-delete their STOP with them, so the same
-- number re-imported from the POS or re-typed by the owner came back
-- textable. restaurant_id 0 is a STOP to the shared platform number.
CREATE TABLE IF NOT EXISTS guest_sms_optouts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL DEFAULT 0,
    phone          TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_guest_sms_optouts_phone ON guest_sms_optouts(phone);
-- A newsletter and who it went to (guest_email.send_newsletter). There was
-- no record, so a second press — or a retry after a send that died halfway —
-- mailed everyone again (MOD-EML-3). One row per recipient is the claim.
CREATE TABLE IF NOT EXISTS guest_newsletters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL,
    subject        TEXT    NOT NULL,
    body           TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    total          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_guest_newsletters_open ON guest_newsletters(completed_at, restaurant_id);
CREATE TABLE IF NOT EXISTS guest_newsletter_recipients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    newsletter_id  INTEGER NOT NULL,
    contact_id     INTEGER NOT NULL,
    email          TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    claimed_at     TEXT,
    sent_at        TEXT,
    error          TEXT,
    UNIQUE(newsletter_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_gnr_status ON guest_newsletter_recipients(newsletter_id, status);
-- A campaign Cavnar drafted for the owner to approve (audit #47: win-back).
-- Nothing in this table is ever sent without the owner's send; `message`
-- is what was drafted, `sent_message` what the owner actually sent.
CREATE TABLE IF NOT EXISTS guest_campaign_drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL,
    kind           TEXT    NOT NULL DEFAULT 'winback',
    segment        TEXT    NOT NULL,
    segment_size   INTEGER NOT NULL DEFAULT 0,
    message        TEXT    NOT NULL,
    rec_key        TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending',
    sent_message   TEXT,
    campaign_total INTEGER,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    answered_at    TEXT,
    answered_by    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_guest_campaign_drafts_rid ON guest_campaign_drafts(restaurant_id, status);
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
        # Who each campaign reached, so a later Toast check-in on the same
        # phone can be counted as a visit (moat audit #21). Taps measured
        # interest; this measures the only thing that pays.
        """CREATE TABLE IF NOT EXISTS guest_campaign_recipients (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id   INTEGER NOT NULL,
            restaurant_id INTEGER NOT NULL,
            contact_id    INTEGER,
            phone         TEXT NOT NULL,
            visited_on    TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gcr_campaign ON guest_campaign_recipients(campaign_id)",
        "ALTER TABLE guest_campaigns ADD COLUMN visits_matched INTEGER",
        "ALTER TABLE guest_campaigns ADD COLUMN attribution_through TEXT",
        # Which restaurants the daily opt-in invite pass has finished for a
        # business date. The job re-runs hourly through the afternoon so a
        # restaurant held back by its own 8am-9pm window is reached later
        # the same day; this keeps the others from being re-fetched from
        # the POS every hour (MOD-MKT-12).
        """CREATE TABLE IF NOT EXISTS optin_invite_runs (
            restaurant_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            done_at       TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date)
        )""",
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
# Known limitation (NS5 L16, MODULE_OVERVIEW.md → Marketing): a guest in
# another zone, and a state with a narrower window for marketing texts, are
# not modelled — this is one fixed floor, not a per-state rule table.
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


def _record_optout(phone, restaurant_id=0, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO guest_sms_optouts (restaurant_id, phone) VALUES (?,?)",
                     (int(restaurant_id or 0), _normalize_phone(phone)))
        conn.commit()
    finally:
        conn.close()


def _opted_out_here(conn, restaurant_id, phone) -> bool:
    return conn.execute(
        "SELECT 1 FROM guest_sms_optouts WHERE phone=? AND restaurant_id IN (0, ?) LIMIT 1",
        (phone, int(restaurant_id or 0))).fetchone() is not None


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
            # Never un-STOPs: the public form is open to anyone with the
            # link, and a STOP is undone only by a START texted from that
            # phone (resubscribe_guest, handle_inbound_sms). The form used to
            # re-subscribe whoever's number was typed in (MOD-MKT-9). Nor
            # does a stranger's submission rename an existing guest.
            conn.execute(
                "UPDATE guest_contacts SET consent=1, consent_at=COALESCE(consent_at,?), "
                "name=COALESCE(name,?), last_visit=?, "
                "visit_count=COALESCE(visit_count,0)+1 WHERE id=?",
                (now_iso, name, now_iso, existing["id"])
            )
        elif name:
            conn.execute("UPDATE guest_contacts SET name=? WHERE id=?", (name, existing["id"]))
        conn.commit()
        contact_id = existing["id"]
    else:
        # A number that texted STOP (here, or to the shared number) comes
        # back unsubscribed, however it comes back: a POS import, an owner
        # re-typing it, the public form. Only their own START undoes it.
        stopped = _opted_out_here(conn, restaurant_id, phone)
        cur = conn.execute(
            "INSERT INTO guest_contacts (restaurant_id, name, phone, consent, consent_at, "
            "last_visit, visit_count, unsubscribed) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, (name or "").strip() or None, phone, int(consent), now_iso,
             now_iso, 1 if consent else 0, 1 if stopped else 0)
        )
        conn.commit()
        contact_id = cur.lastrowid
    conn.close()
    return contact_id


def delete_guest_contact(contact_id, restaurant_id, db_path=DB_PATH):
    """Scoped to restaurant_id — a client must never be able to delete
    another restaurant's contact by guessing an id.

    The row goes (the owner's list no longer shows them), but a STOP it
    carried is kept in guest_sms_optouts first, so the same number added
    again later is added unsubscribed (CLIENT-34)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT phone, unsubscribed FROM guest_contacts WHERE id=? AND restaurant_id=?",
                           (contact_id, restaurant_id)).fetchone()
        if row and row["unsubscribed"]:
            conn.execute("INSERT OR IGNORE INTO guest_sms_optouts (restaurant_id, phone) VALUES (?,?)",
                         (int(restaurant_id), row["phone"]))
        conn.execute("DELETE FROM guest_contacts WHERE id=? AND restaurant_id=?", (contact_id, restaurant_id))
        conn.commit()
    finally:
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


def phone_opted_out(phone, db_path=DB_PATH) -> bool:
    """True when this number has texted STOP to ANY restaurant here.

    Every restaurant shares one platform number, and the STOP reply promises
    "no more texts from us" — "us" being that number. So anything that texts
    a guest who has not consented to THIS restaurant (an opt-in invite, a
    review request an owner typed in) honours a STOP sent to any of them
    (MOD-MKT-11, MOD-MKT-12)."""
    phone = _normalize_phone(phone)
    conn = get_conn(db_path)
    try:
        return (conn.execute(
            "SELECT 1 FROM guest_contacts WHERE phone=? AND unsubscribed=1 LIMIT 1", (phone,)
        ).fetchone() is not None or conn.execute(
            "SELECT 1 FROM guest_sms_optouts WHERE phone=? LIMIT 1", (phone,)
        ).fetchone() is not None)
    finally:
        conn.close()


# ── Inbound SMS ─────────────────────────────────────────────────────────────
# Every outbound message this system sends promises "Reply STOP to
# unsubscribe". Until these handlers existed that promise was not actually
# kept by anything — a guest could text STOP and keep receiving messages.

STOP_KEYWORDS  = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke"}
START_KEYWORDS = {"start", "unstop", "yes", "y"}
HELP_KEYWORDS  = {"help", "info"}


def resubscribe_guest(restaurant_id, phone, db_path=DB_PATH):
    """The guest's own START (handle_inbound_sms) — the one thing that undoes
    a STOP, including one kept after their contact row was deleted."""
    phone = _normalize_phone(phone)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE guest_contacts SET unsubscribed=0 WHERE restaurant_id=? AND phone=?",
        (restaurant_id, phone)
    )
    conn.execute("DELETE FROM guest_sms_optouts WHERE phone=? AND restaurant_id IN (0, ?)",
                 (phone, int(restaurant_id or 0)))
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


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None)
_STOP_PHRASES = ("stop", "unsubscribe", "opt out", "optout", "remove me", "stop texting", "cancel", "end", "quit")


def _normalise_sms(body: str) -> str:
    """NFKC (full-width letters), zero-width characters out, lowercase,
    punctuation to spaces, whitespace collapsed."""
    import unicodedata
    text = unicodedata.normalize("NFKC", body or "").translate(_ZERO_WIDTH).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _is_stop(body: str) -> bool:
    """A revocation in any reasonable form. The FCC's rule is "any reasonable
    means"; only a bare first word counted, so "Stop.", "Please stop", "opt
    out" and a zero-width or full-width STOP left the guest subscribed
    (MOD-MKT-8). A short message carrying a stop phrase is a stop; a long
    one ("don't stop the specials…") is left to its first word."""
    text = _normalise_sms(body)
    if not text:
        return False
    words = text.split()
    if words[0] in STOP_KEYWORDS:
        return True
    if len(words) <= 5:
        padded = f" {text} "
        return any(f" {p} " in padded for p in _STOP_PHRASES) or text.replace(" ", "") in ("optout", "unsubscribe")
    return False


def handle_inbound_sms(from_phone, body, db_path=DB_PATH):
    """Process one inbound guest text. Returns a reply string to send back
    (or None to stay silent).

    A STOP unsubscribes the guest from EVERY restaurant that has their
    number, not just the one we think they were replying to — when someone
    says stop, the safe reading is stop, not "stop from this one tenant".
    """
    phone = _normalize_phone(from_phone)
    _norm = _normalise_sms(body)
    word = _norm.split()[0] if _norm else ""

    if _is_stop(body):
        conn = get_conn(db_path)
        conn.execute("UPDATE guest_contacts SET unsubscribed=1 WHERE phone=?", (phone,))
        # Kept apart from the contact rows, which an owner can delete.
        conn.execute("INSERT OR IGNORE INTO guest_sms_optouts (restaurant_id, phone) VALUES (0, ?)",
                     (phone,))
        conn.commit()
        conn.close()
        _mark_invite_response(phone, "stop", db_path=db_path)
        return "You're unsubscribed and won't get any more texts from us. Reply START to opt back in."

    candidates = _inbound_candidates(phone, db_path=db_path)
    if not candidates:
        return None          # nothing of ours — stay silent rather than guess

    if word in HELP_KEYWORDS:
        # The carrier requirement for HELP: name the program, say how to stop.
        names = " and ".join(n for _, n in candidates[:3] if n) or "a restaurant you joined"
        return (f"Guest texts from {names}, sent by Cavnar AI. Msg & data rates may apply. "
                "Reply STOP to unsubscribe.")

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


# The SMS budget for a campaign's own words (the STOP line and any link are
# added after). The draft prompt asks for it, the web composer's textarea
# enforces it — and nothing on the server did, so a 700-character message
# (from the API, the phone, Ask, or a draft the model over-wrote) went to the
# whole list as a five-segment text, billed per segment per guest (AI-32).
CAMPAIGN_MAX_CHARS = 300


# Offer shapes ai_guard.unsupported_commitments (written for review replies)
# does not name, because a reply never runs a promotion and a text does.
_EXTRA_OFFER_RE = re.compile(
    r"\b(half[- ]?price|half[- ]?off|b\.?o\.?g\.?o\b|buy one,? get one|two[- ]for[- ]one|2[- ]for[- ]1|"
    r"\$\s?\d+(?:\.\d\d)?\s+off|discount(?:ed)?|on the house)\b", re.I)


def invented_offers(text, allowed_source=""):
    """Offers in guest-text copy that the owner never wrote down (M-24): a
    freebie, a percentage or dollar off, half price, BOGO, a discount. An
    offer whose words appear in the owner's own topic or menu notes is
    theirs and is allowed. Returns the offending phrases."""
    from ai_guard import unsupported_commitments
    found = list(unsupported_commitments(text or ""))
    for m in _EXTRA_OFFER_RE.finditer(text or ""):
        ph = m.group(0).strip()
        if ph and ph not in found:
            found.append(ph)
    src = re.sub(r"\s+", " ", (allowed_source or "").lower())
    return [ph for ph in found if re.sub(r"\s+", " ", ph.lower()) not in src]


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
        "Never invent an offer: no discount, percentage or dollars off, free item, half price, "
        "buy-one-get-one or anything on the house, unless it is written in the topic or menu above, "
        "in those words. The restaurant has not agreed to one.\n"
        "Return ONLY the message text, nothing else."
    )
    client = get_client()
    message = create_with_retry(
        client,
        model=model_for("guest_marketing"),
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant.id,
        action="guest_campaign_draft",
    )
    text = extract_text(message).strip()
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ValueError("campaign copy was truncated")
    # This goes out as an SMS to real guests. A link, a phone number or an
    # offer the restaurant never agreed to is not something to send unread.
    from ai_guard import check_public_reply
    refusal = check_public_reply(text)
    if refusal:
        raise ValueError(f"campaign copy rejected: {refusal}")
    # The comment above promised an offer the restaurant never agreed to is
    # not sent unread, and only the link/phone check ran: "enjoy a free
    # dessert with any entree" and "20% off all week" both passed (M-24).
    offers = invented_offers(text, (topic or "") + " " + (p.get("menu_notes") or ""))
    if offers:
        raise ValueError("campaign copy rejected: it offers " + ", ".join(offers[:3])
                         + ", which nobody told Cavnar the restaurant is running")
    # check_public_reply allows a 1,200-character review reply; a text
    # message has its own, much smaller budget.
    if len(text) > CAMPAIGN_MAX_CHARS:
        raise ValueError(f"campaign copy rejected: {len(text)} characters, over the "
                         f"{CAMPAIGN_MAX_CHARS} a text message can carry")
    return text


MAX_CAMPAIGN_CHARS = 1600


def audience_size(restaurant_id, segment="all", db_path=DB_PATH) -> int:
    """How many guests a campaign to this segment would text right now."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    return sum(1 for c in segment_contacts(restaurant_id, segment, db_path=db_path) if not _too_soon(c, now))


def start_campaign(restaurant_id, message, segment="all", link_token=None, on_done=None, db_path=DB_PATH) -> dict:
    """Validate now, text in the background. The fan-out used to run inside
    the HTTP request: one synchronous Twilio call per guest on one of the
    four request threads, so a 5,000-guest list held a quarter of the
    platform for an hour and the phone gave up long before (MOD-MKT-7).
    Returns what the owner is told immediately; campaign history shows the
    sends as they land."""
    if not guest_sms_allowed_now(restaurant_id):
        return {"ok": False, "blocked": "quiet_hours", "sent": 0, "failed": 0, "total": 0,
                "error": ("Guest texts only go out between "
                          f"{guest_sms_window_label()} in your local time. "
                          "Your message is ready — send it in the morning.")}
    full = message.strip() + ("\n" + _short_link(link_token) if link_token else "") + "\n\nReply STOP to unsubscribe."
    if len(full) > MAX_CAMPAIGN_CHARS:
        return {"ok": False, "sent": 0, "failed": 0, "total": 0,
                "error": f"That text is {len(full):,} characters with the opt-out line; "
                         f"the most one text can carry is {MAX_CAMPAIGN_CHARS:,}. Shorten it and send again."}
    total = audience_size(restaurant_id, segment, db_path=db_path)
    import threading

    def _run():
        try:
            result = send_campaign(restaurant_id, message, db_path=db_path, segment=segment, link_token=link_token)
            if on_done:
                on_done(result)
        except Exception as e:
            import ops
            ops.capture(e, job="guest_campaign_send", context=f"restaurant_id={restaurant_id}")
    threading.Thread(target=_run, name=f"guest-campaign-{restaurant_id}", daemon=True).start()
    return {"ok": True, "queued": True, "total": total, "segment": segment,
            "segment_label": SEGMENTS.get(segment, SEGMENTS["all"])["label"]}


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
    if len((message or "").strip()) > CAMPAIGN_MAX_CHARS:
        n = len((message or "").strip())
        return {"ok": False, "blocked": "too_long", "sent": 0, "failed": 0, "total": 0,
                "error": (f"That message is {n} characters. A guest text can carry "
                          f"{CAMPAIGN_MAX_CHARS} — shorten it and send again.")}
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
    if len(full_message) > MAX_CAMPAIGN_CHARS:
        # Twilio's hard ceiling; every 160 characters is a billed segment.
        # Refused before anyone is texted, not discovered per guest.
        return {"ok": False, "sent": 0, "failed": 0, "total": 0,
                "error": f"That text is {len(full_message):,} characters with the opt-out line; "
                         f"the most one text can carry is {MAX_CAMPAIGN_CHARS:,}. Shorten it and send again."}

    # Exactly once per guest, even with two sends in flight (a double tap, a
    # phone that gave up at 20 s and was pressed again) or a send killed
    # halfway by a deploy. The old loop texted the whole list and only then
    # wrote the campaign row, the recipients and the frequency stamps in one
    # commit, so an overlapping send saw nobody stamped and texted everyone
    # again, and a crash left no record of who had been texted.
    #
    # Now: the campaign row goes in first; each guest is CLAIMED by stamping
    # last_campaign_at conditionally (only if nobody stamped them inside the
    # frequency window) and committed before their text goes out; each
    # delivered text is recorded as it happens. A claim whose send fails is
    # released, so a failure never locks a guest out of the next campaign.
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff = (now - timedelta(days=GUEST_SMS_MIN_DAYS_BETWEEN)).strftime("%Y-%m-%dT%H:%M:%S")
    label = SEGMENTS.get(segment, SEGMENTS["all"])["label"]
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO guest_campaigns "
            "(restaurant_id, message, sent_count, failed_count, segment, segment_label, link_token) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, message.strip(), 0, 0, segment, label, link_token))
        campaign_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    def _claim(contact_id, prior):
        c2 = get_conn(db_path)
        try:
            got = c2.execute(
                "UPDATE guest_contacts SET last_campaign_at=? WHERE id=? AND restaurant_id=? "
                "AND (last_campaign_at IS NULL OR last_campaign_at < ? OR last_campaign_at IS ?)",
                (stamp, contact_id, restaurant_id, cutoff, prior)).rowcount
            c2.commit()
            return bool(got)
        finally:
            c2.close()

    def _release(contact_id, prior):
        c2 = get_conn(db_path)
        try:
            c2.execute("UPDATE guest_contacts SET last_campaign_at=? WHERE id=? AND last_campaign_at=?",
                       (prior, contact_id, stamp))
            c2.commit()
        finally:
            c2.close()

    def _record(contact, ok):
        c2 = get_conn(db_path)
        try:
            if ok:
                c2.execute("UPDATE guest_campaigns SET sent_count=sent_count+1 WHERE id=?", (campaign_id,))
                try:
                    c2.execute("INSERT INTO guest_campaign_recipients (campaign_id, restaurant_id, contact_id, phone) "
                               "VALUES (?,?,?,?)", (campaign_id, restaurant_id, contact["id"],
                                                    _normalize_phone(contact.get("phone") or "")))
                except Exception as e:
                    # The text already went; the count stands even if the
                    # recipients table is missing. Recorded, never swallowed.
                    import ops
                    ops.capture(e, job="campaign_recipients", context=f"restaurant_id={restaurant_id}")
            else:
                c2.execute("UPDATE guest_campaigns SET failed_count=failed_count+1 WHERE id=?", (campaign_id,))
            c2.commit()
        finally:
            c2.close()

    sent, failed, raced, deferred = 0, 0, 0, 0
    for i, c in enumerate(eligible):
        # The 8am-9pm window is checked before EVERY text: a send that
        # starts at 8:57pm used to keep texting past 9 (MOD-MKT-7). The rest
        # wait for the next campaign rather than going out at night.
        if not guest_sms_allowed_now(restaurant_id):
            deferred = len(eligible) - i
            break
        prior = c.get("last_campaign_at")
        if not _claim(c["id"], prior):
            raced += 1          # another send in flight already has this guest
            continue
        try:
            ok = bool(send_sms(c["phone"], full_message, use_case="guest"))
        except Exception:
            ok = False
        except BaseException:
            # Killed before this text is known to have gone: give the guest
            # back so a retry reaches them, then let the kill propagate.
            _release(c["id"], prior)
            raise
        if not ok:
            _release(c["id"], prior)
        _record(c, ok)
        if ok:
            sent += 1
        else:
            failed += 1
    skipped_recent += raced

    return {"ok": True, "sent": sent, "failed": failed, "total": len(eligible) - raced,
            "campaign_id": campaign_id, "deferred_quiet_hours": deferred,
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
            "       c.segment_label, c.link_token, c.created_at, c.visits_matched, c.attribution_through, "
            "       COALESCE(l.clicks, 0) AS clicks "
            "FROM guest_campaigns c "
            "LEFT JOIN marketing_links l ON l.token = c.link_token "
            "WHERE c.restaurant_id=? ORDER BY c.id DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def diagnose(restaurant_id, db_path=DB_PATH) -> dict:
    """The campaigns' read, in the shared diagnosis shape. Deterministic:
    the best and worst measured campaign by taps per hundred sent and, once
    Toast attribution has run, by guests who came back. A campaign with no
    measurement is never scored against one that has."""
    hist = campaign_history(restaurant_id, limit=20, db_path=db_path)
    sent = [c for c in hist if (c.get("sent_count") or 0) >= 10]
    if len(sent) < 2:
        return {"available": False, "reason": "fewer than two campaigns of ten or more texts — nothing to compare yet"}
    measured = [c for c in sent if c.get("visits_matched") is not None]
    basis = "came back" if len(measured) >= 2 else "taps"
    # Taps are only measurable on a campaign that carried a link: one with
    # no link scored 0 and was named "weakest" for a number it could never
    # have had (M-22).
    pool = measured if basis == "came back" else [c for c in sent if c.get("link_token")]
    if len(pool) < 2:
        return {"available": False,
                "reason": "fewer than two campaigns with a link or a matched visit — nothing to compare yet"}

    def rate(c):
        n = (c.get("visits_matched") if basis == "came back" else c.get("clicks")) or 0
        return n / float(c["sent_count"]) * 100

    ranked = sorted(pool, key=rate, reverse=True)
    best, worst = ranked[0], ranked[-1]
    seg_diff = (best.get("segment") or "all") != (worst.get("segment") or "all")
    ev_in = _campaign_evidence(pool, basis, seg_diff)
    if rate(best) == rate(worst):
        return _with_confidence(restaurant_id, {
            "available": True, "cause": None,
            "summary": f"{len(pool)} campaigns measured by {basis}; none stood apart.",
            "alternative_cause": None, "what_would_confirm": None,
            "operational_evidence": [{"module": "marketing", "metric": f"{basis} per 100 sent", "value": f"{rate(best):.1f}"}]},
            ev_in, db_path)
    # Owner-facing dates read M/D/YY (audit #16); these were created_at[:10].
    from time_utils import mdy as _mdy
    evidence = [{"module": "marketing", "metric": f"best campaign — {basis} per 100 sent",
                 "value": f"{rate(best):.1f} ({best.get('segment_label') or 'everyone'}, {_mdy(best.get('created_at'))})"},
                {"module": "marketing", "metric": f"weakest campaign — {basis} per 100 sent",
                 "value": f"{rate(worst):.1f} ({worst.get('segment_label') or 'everyone'}, {_mdy(worst.get('created_at'))})"}]
    cause = (f"The {best.get('segment_label') or 'everyone'} segment answered at {rate(best):.1f} {basis} per 100 texts "
             f"against {rate(worst):.1f} for {worst.get('segment_label') or 'everyone'}." if seg_diff else
             f"The message sent {_mdy(best.get('created_at'))} drew {rate(best):.1f} {basis} per 100 texts; "
             f"the one on {_mdy(worst.get('created_at'))} drew {rate(worst):.1f} to the same audience.")
    alt = ("The day and hour it went out, not the audience — a Thursday-afternoon text and a Monday-morning one reach "
           "the same people in different moods." if seg_diff else
           "The audience had simply been texted more recently the second time; the frequency cap holds three days, not three weeks.")
    # Two segments are not compared on raw rates (M-22): regulars come back
    # whether or not they were texted, so the segment with more of them
    # "wins" with no baseline for what it would have done anyway. No
    # "send to that segment only" — only the test that would tell.
    confirm = ("Send the same message to both segments on the same day, and hold a few guests in each back; "
               "only against those held back does a gap say the text worked." if seg_diff else
               "Send the stronger message's shape again on the weaker one's weekday; if it holds, it was the message.")
    return _with_confidence(restaurant_id, {
        "available": True, "cause": cause, "alternative_cause": alt, "what_would_confirm": confirm,
        "operational_evidence": evidence,
        "summary": f"{len(pool)} campaigns measured by {basis}."
                   + ("" if basis == "came back" else " Toast check-ins have not been matched yet, so taps stand in.")},
        ev_in, db_path)


# No campaign here holds guests back, so no comparison between campaigns can
# read as the text having caused the visits: evidence is capped at this
# (below high) until a holdout exists (confidence audit E15, CA1 M7). Two
# SEGMENTS compared on raw rates have no baseline at all (M-22): low.
CAMPAIGN_NO_HOLDOUT_CAP = 65
CAMPAIGN_SEGMENT_CAP = 35


def _campaign_evidence(pool, basis, seg_diff) -> dict:
    """The campaign read's Evidence Strength input: how many measured
    campaigns (N_FULL "campaigns"), taps standing in for visits flagged as
    partial, and the holdout / baseline caps."""
    ev = {"n": len(pool), "kind": "campaigns",
          "flags": () if basis == "came back" else ("partial",),
          "basis": f"{len(pool)} campaigns measured by {'guests who came back' if basis == 'came back' else 'link taps'}"}
    if seg_diff:
        ev["cap"], ev["cap_reason"] = CAMPAIGN_SEGMENT_CAP, "two segments compared with no baseline for either"
    else:
        ev["cap"], ev["cap_reason"] = CAMPAIGN_NO_HOLDOUT_CAP, "no guests were held back, so cause isn't measured"
    return ev


def _with_confidence(restaurant_id, out, ev_in, db_path=DB_PATH):
    """`confidence_detail` (K1, measured) and `confidence` (its band, for the
    clients that decode a string) on a campaign diagnosis. Never raises."""
    out["evidence_input"] = ev_in
    try:
        import rec_trust
        import data_freshness
        # Data Freshness from what the read rests on (re-audit B3#13, B4
        # M12 — it passed no sources, so it was never measured): guests who
        # came back are matched through the POS's orders; a read on link
        # taps (flagged partial) rests on no dated POS data.
        srcs = () if "partial" in (ev_in.get("flags") or ()) else data_freshness.sources_for(["campaigns"])
        conf = rec_trust.assess(restaurant_id, "diag_campaign", evidence=ev_in, sources=srcs, db_path=db_path)
    except Exception as e:
        print(f"[guest_marketing] campaign confidence unavailable: {e}")
        import confidence_engine
        conf = confidence_engine.unknown()
    out["confidence_detail"] = conf
    out["confidence"] = conf.get("band") or "low"
    return out


# ── Win-back (audit #47) ────────────────────────────────────────────────────
#
# lapsed_30 / lapsed_60 existed as audiences and nothing ever suggested using
# them: the guests drifting away were countable and nobody was told. Now,
# when a lapsed segment is big enough to be worth a text, Marketing carries
# a DRAFTED win-back campaign — the segment, its size, the measured return
# of past win-back texts if there is one — waiting for the owner. It is
# never sent automatically: the owner's send goes through start_campaign,
# which applies consent, quiet hours, the three-day cap and the length
# limits like any other campaign.

WINBACK_MIN_GUESTS = 5          # below this a text is a personal call, not a campaign
WINBACK_SEGMENTS = ("lapsed_60", "lapsed_30")   # the more lapsed audience first


def winback_key(segment):
    import rec_ledger
    return rec_ledger.rec_key("winback", segment)


def _winback_message(restaurant_name):
    """Deterministic copy — no model on a page load. The owner edits it, or
    asks for an AI rewrite with the composer's own win-back draft. No offer,
    no discount: nobody has agreed to one."""
    name = (restaurant_name or "us").strip()
    msg = (f"Hi from {name}! It's been a little while and we'd love to have you back. "
           f"Come see us this week — your table's waiting.")
    return msg[:CAMPAIGN_MAX_CHARS]


def winback_return(restaurant_id, db_path=DB_PATH) -> dict:
    """What past win-back texts did, measured — or that nothing has been.
    A campaign to a lapsed segment with attribution run counts; its return
    is guests who came back within ATTRIBUTION_WINDOW_DAYS per 100 texted."""
    from time_utils import mdy as _mdy
    past = [c for c in campaign_history(restaurant_id, limit=50, db_path=db_path)
            if (c.get("segment") or "") in WINBACK_SEGMENTS and (c.get("sent_count") or 0) > 0]
    measured = [c for c in past if c.get("visits_matched") is not None]
    if not measured:
        return {"measured": False, "campaigns": len(past),
                "text": ("No past win-back text has a measured return yet."
                         if not past else f"{len(past)} past win-back text{'s' if len(past) != 1 else ''}, "
                                          "none measured yet — returns are matched against Toast check-ins.")}
    sent = sum(int(c["sent_count"]) for c in measured)
    back = sum(int(c.get("visits_matched") or 0) for c in measured)
    last = measured[0]
    return {"measured": True, "campaigns": len(measured), "sent": sent, "came_back": back,
            "per_100": round(back / sent * 100, 1) if sent else None,
            "text": (f"Past win-back texts: {back} of {sent} guests came back within "
                     f"{ATTRIBUTION_WINDOW_DAYS} days ({round(back / sent * 100, 1) if sent else 0} per 100); "
                     f"the last went out {_mdy(last.get('created_at'))}.")}


def winback_suggestion(restaurant_id, restaurant_name=None, surface="marketing", user_id=None,
                       db_path=DB_PATH) -> dict:
    """The pending win-back draft for Marketing, creating one when a lapsed
    segment clears WINBACK_MIN_GUESTS and the owner has not answered that
    segment's recommendation. {"available", "draft"?, "reason"?}."""
    import insight_store
    silenced = set()
    try:
        import rec_ledger
        silenced = rec_ledger.silenced_keys(restaurant_id, db_path=db_path)
    except Exception:
        pass
    conn = get_conn(db_path)
    try:
        pending = conn.execute("SELECT * FROM guest_campaign_drafts WHERE restaurant_id=? AND kind='winback' "
                               "AND status='pending' ORDER BY id DESC", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    draft = None
    for row in pending:
        if row["rec_key"] in silenced:
            _answer_winback(restaurant_id, row["id"], "dismissed", user_id, db_path=db_path)
            continue
        draft = dict(row)
        break
    if draft is None:
        seg, size = None, 0
        for s_ in WINBACK_SEGMENTS:
            n = audience_size(restaurant_id, s_, db_path=db_path)
            if n >= WINBACK_MIN_GUESTS and winback_key(s_) not in silenced:
                seg, size = s_, n
                break
        if not seg:
            return {"available": False,
                    "reason": f"no lapsed segment has {WINBACK_MIN_GUESTS}+ opted-in guests who can be texted"}
        if not restaurant_name:
            try:
                from models import get_restaurant
                _r = get_restaurant(restaurant_id)
                restaurant_name = _r.name if _r else None
            except Exception:
                restaurant_name = None
        conn = get_conn(db_path)
        try:
            cur = conn.execute("INSERT INTO guest_campaign_drafts (restaurant_id, kind, segment, segment_size, "
                               "message, rec_key) VALUES (?,?,?,?,?,?)",
                               (restaurant_id, "winback", seg, size, _winback_message(restaurant_name),
                                winback_key(seg)))
            conn.commit()
            draft = dict(conn.execute("SELECT * FROM guest_campaign_drafts WHERE id=?", (cur.lastrowid,)).fetchone())
        finally:
            conn.close()
    # The size moves as guests visit or join; show today's.
    draft["segment_size"] = audience_size(restaurant_id, draft["segment"], db_path=db_path)
    draft["segment_label"] = SEGMENTS.get(draft["segment"], {}).get("label")
    draft["return"] = winback_return(restaurant_id, db_path=db_path)
    draft["max_chars"] = CAMPAIGN_MAX_CHARS
    draft["sms_window"] = guest_sms_window_label()
    insight_store.present_recs(restaurant_id, "marketing", surface,
                               [{"key": draft["rec_key"], "text": f"Win back {draft['segment_size']} guests "
                                                                 f"({draft['segment_label']})",
                                 "model_written": False, "evidence_sources": ["marketing", "guests"],
                                 "expected_metric": None}], user_id=user_id, db_path=db_path)
    return {"available": True, "draft": draft}


def _answer_winback(restaurant_id, draft_id, status, user_id=None, sent_message=None, total=None, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE guest_campaign_drafts SET status=?, answered_at=datetime('now'), answered_by=?, "
                         "sent_message=COALESCE(?, sent_message), campaign_total=COALESCE(?, campaign_total) "
                         "WHERE id=? AND restaurant_id=? AND status='pending'",
                         (status, user_id, sent_message, total, draft_id, restaurant_id)).rowcount
        conn.commit()
        return bool(n)
    finally:
        conn.close()


def send_winback(restaurant_id, draft_id, message=None, user_id=None, db_path=DB_PATH) -> dict:
    """The owner's send of a win-back draft — through start_campaign, so
    consent, quiet hours, the frequency cap and MAX_CAMPAIGN_CHARS all
    apply. The draft is answered only once the campaign is accepted."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM guest_campaign_drafts WHERE id=? AND restaurant_id=?",
                           (draft_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That draft is gone."}
    if row["status"] != "pending":
        return {"ok": False, "error": "That draft was already answered."}
    text = (message or row["message"] or "").strip()
    if not text:
        return {"ok": False, "error": "The message is empty."}
    if len(text) > CAMPAIGN_MAX_CHARS:
        return {"ok": False, "error": f"That message is {len(text)} characters. A guest text can carry "
                                      f"{CAMPAIGN_MAX_CHARS} — shorten it and send again."}
    from ai_guard import check_public_reply
    refusal = check_public_reply(text)
    if refusal:
        return {"ok": False, "error": f"Not sent: {refusal}."}
    rec_key = row["rec_key"]

    def _sent(res):
        # The texts went out: the win-back was implemented, not only
        # accepted (ROI #27). Runs on the send thread once it finishes.
        if (res or {}).get("sent"):
            try:
                import rec_ledger
                rec_ledger.implemented(restaurant_id, rec_key, "marketing", user_id=user_id,
                                       source_ref=f"winback:{draft_id}",
                                       meta={"module": "marketing", "sent": res.get("sent")}, db_path=db_path)
            except Exception as e:
                print(f"[winback] implementation not recorded for {restaurant_id}: {e}")
    result = start_campaign(restaurant_id, text, segment=row["segment"], on_done=_sent, db_path=db_path)
    if not result.get("ok"):
        return result
    _answer_winback(restaurant_id, draft_id, "sent", user_id, sent_message=text, total=result.get("total"),
                    db_path=db_path)
    try:
        import rec_ledger
        rec_ledger.record(restaurant_id, row["rec_key"], "accepted", surface="marketing", user_id=user_id,
                          meta={"module": "marketing", "segment": row["segment"],
                                "edited": text != row["message"], "total": result.get("total")},
                          db_path=db_path)
    except Exception:
        pass
    return dict(result, draft_id=draft_id)


def dismiss_winback(restaurant_id, draft_id, user_id=None, kind="not_for_us", db_path=DB_PATH, viewer=None,
                    reason_code=None, reason=None) -> dict:
    """"Not for us" on a win-back draft. With `viewer` (the routes) the
    draft's recommendation must be one this login was shown and may see
    (K2) — otherwise it reads as gone. `reason_code` (rec_ledger.
    REASON_CODES; the route refuses any other) and a free `reason` ride on
    the ledger answer like every other "Not for us" (K3)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT rec_key FROM guest_campaign_drafts WHERE id=? AND restaurant_id=?",
                           (draft_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That draft is gone."}
    if viewer is not None:
        import rec_learning
        if rec_learning.answerable_episode(viewer, restaurant_id, row["rec_key"], db_path=db_path) is None:
            return {"ok": False, "error": "That draft is gone."}
    _answer_winback(restaurant_id, draft_id, "dismissed", user_id, db_path=db_path)
    try:
        import rec_ledger
        meta = {"kind": kind if kind in ("hide", "not_for_us") else "not_for_us", "module": "marketing"}
        if reason_code in rec_ledger.REASON_CODES:
            meta["reason_code"] = reason_code
        if isinstance(reason, str) and reason.strip():
            meta["reason"] = reason.strip()[:200]
        rec_ledger.record(restaurant_id, row["rec_key"], "dismissed", surface="marketing", user_id=user_id,
                          role=(viewer or {}).get("role") if isinstance(viewer, dict) else None,
                          meta=meta, db_path=db_path, require_existing=viewer is not None)
    except Exception:
        pass
    return {"ok": True}


ATTRIBUTION_WINDOW_DAYS = 14


def run_campaign_attribution(db_path=DB_PATH, today=None):
    """Daily: for every campaign sent in the last ATTRIBUTION_WINDOW_DAYS
    at a Toast-connected restaurant, count recipients Toast identified on
    a check on a later business day. Written to guest_campaigns as
    visits_matched, with attribution_through saying how far the window
    has been read. A visit is counted once per recipient per campaign.

    Partial by nature — Toast only has a customer on a check when one was
    captured — and said so wherever the number is shown. Each business
    date is fetched once per restaurant per run, whatever the number of
    campaigns, and only dates not yet read for that campaign."""
    from datetime import date as _date, timedelta as _td
    from models import get_restaurant
    today = today or _date.today()
    yesterday = today - _td(days=1)
    floor = (today - _td(days=ATTRIBUTION_WINDOW_DAYS + 1)).isoformat()
    conn = get_conn(db_path)
    try:
        camps = conn.execute(
            "SELECT id, restaurant_id, substr(created_at,1,10) AS sent_on, attribution_through "
            "FROM guest_campaigns WHERE substr(created_at,1,10) >= ? AND sent_count > 0 "
            "ORDER BY restaurant_id, id", (floor,)).fetchall()
    finally:
        conn.close()
    checked = matched = 0
    phones_by_day = {}          # (rid, iso date) -> set of normalized phones
    import pos as _pos
    for c in camps:
        rid = c["restaurant_id"]
        r = get_restaurant(rid)
        # Any POS that shares guest records — Toast today; RPOWER once its
        # customer scope is granted — not a Toast field check.
        if not r or not _pos.supports(rid, "fetch_order_customers"):
            continue
        if (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled", "paused"):
            continue          # no POS calls on behalf of an account that asked for quiet
        sent_on = _date.fromisoformat(c["sent_on"])
        # From the day AFTER the send (M-22): the send day's orders include
        # the lunch before a 3pm text, which is not a guest coming back.
        start = (_date.fromisoformat(c["attribution_through"]) + _td(days=1) if c["attribution_through"]
                 else sent_on + _td(days=1))
        end = min(yesterday, sent_on + _td(days=ATTRIBUTION_WINDOW_DAYS))
        if start > end:
            continue
        conn = get_conn(db_path)
        try:
            recips = conn.execute("SELECT id, phone FROM guest_campaign_recipients WHERE campaign_id=? AND visited_on IS NULL",
                                  (c["id"],)).fetchall()
        finally:
            conn.close()
        if not recips:
            continue
        by_phone = {rr["phone"]: rr["id"] for rr in recips}
        newly = []
        day = start
        ok = True
        while day <= end:
            key = (rid, day.isoformat())
            if key not in phones_by_day:
                try:
                    custs, _prov = _pos.fetch_order_customers(rid, day)
                    phones_by_day[key] = {_normalize_phone(x.get("phone")) for x in custs if x.get("phone")}
                except Exception as e:
                    import ops
                    ops.capture(e, job="campaign_attribution", context=f"restaurant_id={rid} date={day}")
                    ok = False
                    break
            for ph in phones_by_day[key] & set(by_phone):
                if by_phone[ph] not in [n[0] for n in newly]:
                    newly.append((by_phone[ph], day.isoformat()))
            day += _td(days=1)
        through = (day - _td(days=1)) if ok else (day - _td(days=1))
        conn = get_conn(db_path)
        try:
            for rec_id, on in newly:
                conn.execute("UPDATE guest_campaign_recipients SET visited_on=? WHERE id=? AND visited_on IS NULL", (on, rec_id))
            total = conn.execute("SELECT COUNT(*) FROM guest_campaign_recipients WHERE campaign_id=? AND visited_on IS NOT NULL",
                                 (c["id"],)).fetchone()[0]
            if through >= start:
                conn.execute("UPDATE guest_campaigns SET visits_matched=?, attribution_through=? WHERE id=?",
                             (int(total), through.isoformat(), c["id"]))
            conn.commit()
        finally:
            conn.close()
        checked += 1
        matched += len(newly)
    return {"campaigns_checked": checked, "visits_matched": matched}


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

    A guest already consented, already unsubscribed, already invited for
    this order, or who has texted STOP to any restaurant on the shared
    number is skipped, so re-running is safe. The scheduler runs this hourly
    through the afternoon: a restaurant outside its own 8am-9pm window is
    deferred and picked up by a later pass, and one already finished for
    `business_date` (optin_invite_runs) is not fetched again (MOD-MKT-12).
    A cancelled restaurant sends nothing.
    """
    from datetime import date as _date
    from models import in_service_sql

    if business_date is None:
        business_date = _date.today()
    bdate = str(business_date)

    conn = get_conn(db_path)
    try:
        restaurants = conn.execute(
            "SELECT id, name FROM restaurants WHERE module_marketing=1 AND "
            + in_service_sql("billing_status") +
            " AND id NOT IN (SELECT restaurant_id FROM optin_invite_runs WHERE business_date=?)",
            (bdate,)
        ).fetchall()
    finally:
        conn.close()
    # Whichever POS shares guest records (pos.supports), not a Toast column.
    import pos as _pos
    restaurants = [r for r in restaurants if _pos.supports(r["id"], "fetch_order_customers")]

    invited, skipped, failed, deferred = 0, 0, 0, 0
    for r in restaurants:
        rid = r["id"]
        # This job is scheduled on the SERVER's clock while restaurants
        # keep their own timezones, so "11am" is not 11am everywhere. An
        # opt-in invite is asking for marketing consent, which makes it a
        # marketing text — same window as everything else here. A deferred
        # restaurant is not marked done, so the next hourly pass retries it.
        if not guest_sms_allowed_now(rid):
            deferred += 1
            continue
        try:
            customers, _prov = _pos.fetch_order_customers(rid, business_date)
        except Exception:
            failed += 1
            continue       # not marked done: the next pass fetches again

        for cust in customers:
            phone = _normalize_phone(cust["phone"])
            conn = get_conn(db_path)
            try:
                existing = conn.execute(
                    "SELECT consent, unsubscribed FROM guest_contacts WHERE restaurant_id=? AND phone=?",
                    (rid, phone)
                ).fetchone()
                already_invited = conn.execute(
                    "SELECT 1 FROM sms_optin_invites WHERE restaurant_id=? AND phone=?", (rid, phone)
                ).fetchone()
            finally:
                conn.close()

            if existing and (existing["consent"] or existing["unsubscribed"]):
                skipped += 1
                continue
            if already_invited or phone_opted_out(phone, db_path=db_path):
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
                if send_sms(phone, message, use_case="guest"):
                    invited += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        conn = get_conn(db_path)
        try:
            conn.execute("INSERT OR IGNORE INTO optin_invite_runs (restaurant_id, business_date) "
                         "VALUES (?,?)", (rid, bdate))
            conn.commit()
        finally:
            conn.close()

    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM optin_invite_runs WHERE done_at < datetime('now','-45 days')")
        conn.commit()
    finally:
        conn.close()
    return {"invited": invited, "skipped": skipped, "failed": failed,
            "deferred_quiet_hours": deferred}


# One pass stops after this long; guests it did not reach stay eligible (the
# claim below is their only marker), so the next hourly pass continues.
REVIEW_REQUEST_PASS_SECONDS = 240


def run_review_request_followups(delay_hours=None, db_path=DB_PATH, max_seconds=None):
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

    Each guest is CLAIMED — last_review_requested_at stamped by a
    conditional UPDATE and committed — before the text goes out, and the
    review_requests row is committed right after it. This used to hold one
    write transaction across the whole Twilio loop, so every other writer in
    the app got "database is locked" on the hour, and a crash rolled back
    every stamp so the next run texted everyone again (MOD-MKT-10). A send
    that raises gives its claim back, so that guest is tried again; one
    Twilio answered with a failure keeps it (a bad number is not re-texted
    every hour) and is recorded as failed. The pass is bounded by
    `max_seconds`; the claims are the cursor, so nothing is starved.
    """
    import time as _time
    from time_utils import restaurant_now_by_id
    # A cancelled restaurant's guests are not texted on its behalf (MOD-REV-2).
    from models import in_service_sql

    if delay_hours is None:
        delay_hours = int(os.getenv("REVIEW_REQUEST_DELAY_HOURS", DEFAULT_REVIEW_REQUEST_DELAY_HOURS))
    if max_seconds is None:
        max_seconds = REVIEW_REQUEST_PASS_SECONDS
    started = _time.monotonic()

    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT gc.id AS contact_id, gc.restaurant_id, gc.name, gc.phone, gc.last_visit,
                   gc.last_review_requested_at,
                   r.name AS restaurant_name, r.google_place_id
            FROM guest_contacts gc
            JOIN restaurants r ON r.id = gc.restaurant_id
            WHERE gc.consent=1 AND gc.unsubscribed=0
              AND r.module_marketing=1
              AND """ + in_service_sql("r.billing_status") + """
              AND gc.last_visit IS NOT NULL
              AND (gc.last_review_requested_at IS NULL OR gc.last_review_requested_at < gc.last_visit)
            ORDER BY gc.id
            """
        ).fetchall()
    finally:
        conn.close()

    sent, failed, skipped, deferred = 0, 0, 0, 0
    for row in rows:
        if _time.monotonic() - started > max_seconds:
            break
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

        stamp = now_local.isoformat()
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "UPDATE guest_contacts SET last_review_requested_at=? WHERE id=? AND consent=1 "
                "AND unsubscribed=0 AND (last_review_requested_at IS NULL "
                "OR last_review_requested_at < last_visit)",
                (stamp, row["contact_id"])
            )
            conn.commit()
            claimed = cur.rowcount == 1
        finally:
            conn.close()
        if not claimed:
            continue          # another pass got here first, or they said STOP

        first_name = (row["name"] or "").split()[0] if row["name"] else "there"
        message = (
            f"Hi {first_name}, thanks for visiting {row['restaurant_name']}! "
            f"We'd love your feedback — leave us a quick Google review: {review_url}"
            "\n\nReply STOP to unsubscribe."
        )
        try:
            ok = bool(send_sms(row["phone"], message, use_case="guest"))
        except BaseException:
            # Nothing was confirmed sent: give the claim back so the next
            # pass tries this guest again, then let the failure go on.
            conn = get_conn(db_path)
            try:
                conn.execute(
                    "UPDATE guest_contacts SET last_review_requested_at=? "
                    "WHERE id=? AND last_review_requested_at=?",
                    (row["last_review_requested_at"], row["contact_id"], stamp)
                )
                conn.commit()
            finally:
                conn.close()
            raise
        if ok:
            sent += 1
        else:
            failed += 1

        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO review_requests (restaurant_id, customer_name, customer_email, customer_phone, method, status) "
                "VALUES (?,?,?,?,?,?)",
                (row["restaurant_id"], row["name"] or "", "", row["phone"], "sms_auto", "sent" if ok else "failed")
            )
            conn.commit()
        finally:
            conn.close()
    return {"sent": sent, "failed": failed, "skipped_no_place_id": skipped,
            "deferred_quiet_hours": deferred}
