"""
guest_marketing.py — SMS lifecycle marketing to guests, not staff/owner.

notify.py already has everything needed to send an SMS (Twilio) and the
exact consent-gating pattern this needs (alert_contacts.sms_consent /
sms_consent_at) — this reuses both rather than re-inventing them, extended
to a new guest_contacts table since alert_contacts is specifically for
staff/owner alert routing, a different table with a different lifecycle.

Consent model mirrors alert_contacts exactly and for the same reason: an
owner manually adding a guest's number (from a receipt, a comment card)
is NOT the guest consenting to marketing texts. TCPA marketing consent has
to come from the recipient, not be asserted on their behalf.

Two scopes (marketing audit MB-2 / SMS-1, 9/28/26):

  consent=1         MARKETING texts — campaigns, win-back, every count of
                    "who can be texted". Set only by a confirmed opt-in: the
                    guest replying Y to the join form's confirmation text
                    (double opt-in, request_public_optin), or
                    add_guest_contact_public_optin for another explicit
                    marketing opt-in. marketing_text_sql() is the one
                    definition every send and count reads.
  review_consent=1  REVIEW LINKS only — the guest said YES to "can we text
                    you a quick link to leave a review". That YES used to set
                    consent=1 and put the guest on every promo blast.

A join is not a visit and neither is a YES: neither touches last_visit or
visit_count. Every consent change is written to guest_consent_events, an
append-only ledger that outlives the contact row.
"""
import hashlib
import json
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
-- Phone-only lookups (a STOP, an inbound reply, the invite pass) scanned the
-- whole table: the only index was UNIQUE(restaurant_id, phone) (MB-21, #94).
CREATE INDEX IF NOT EXISTS idx_guest_contacts_phone ON guest_contacts(phone);
CREATE INDEX IF NOT EXISTS idx_guest_campaigns_rid_created ON guest_campaigns(restaurant_id, created_at);
-- Consent evidence (MB-4 / SMS-4 / ARC-13, 9/28/26): one row per consent
-- change, never rewritten (the trigger below) and never deleted with the
-- contact. consent + consent_at on the contact row were the only record,
-- and deleting the guest deleted them. restaurant_id 0 is the shared number
-- (a STOP to all of them). created_at is UTC.
CREATE TABLE IF NOT EXISTS guest_consent_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id      INTEGER NOT NULL DEFAULT 0,
    channel            TEXT    NOT NULL DEFAULT 'sms',
    phone              TEXT,
    email              TEXT,
    event              TEXT    NOT NULL,
    source             TEXT    NOT NULL,
    ip                 TEXT,
    user_agent         TEXT,
    disclosure_version TEXT,
    disclosure_hash    TEXT,
    message_sid        TEXT,
    detail             TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_gce_phone ON guest_consent_events(phone, restaurant_id);
CREATE INDEX IF NOT EXISTS idx_gce_rid ON guest_consent_events(restaurant_id, created_at);
CREATE TRIGGER IF NOT EXISTS guest_consent_events_append_only
BEFORE UPDATE ON guest_consent_events
BEGIN
    SELECT RAISE(ABORT, 'guest_consent_events is append-only');
END;
-- The public join form's pending opt-ins (double opt-in, MB-4 / #3). A row
-- is a request, not consent: consent is set only when that phone replies
-- Y within JOIN_CONFIRM_HOURS. The rows are also the durable per-phone and
-- per-IP submission limit — the old limit lived in one process's memory.
-- status: pending | confirmed | already | blocked_stop | send_failed | stopped
CREATE TABLE IF NOT EXISTS guest_optin_requests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id      INTEGER NOT NULL,
    phone              TEXT    NOT NULL,
    name               TEXT,
    ip                 TEXT,
    user_agent         TEXT,
    disclosure_version TEXT,
    disclosure_hash    TEXT,
    status             TEXT    NOT NULL DEFAULT 'pending',
    requested_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    confirmed_at       TEXT,
    confirm_sid        TEXT
);
CREATE INDEX IF NOT EXISTS idx_gor_phone ON guest_optin_requests(phone, requested_at);
CREATE INDEX IF NOT EXISTS idx_gor_ip ON guest_optin_requests(ip, requested_at);
-- "Which restaurant did you mean?" — asked when one YES could answer more
-- than one restaurant. A bare restaurant name counts only as the answer to
-- an open question for that phone (MB-1 / #6); it never opts anyone in on
-- its own. candidates is JSON [restaurant_id, ...].
CREATE TABLE IF NOT EXISTS sms_pending_questions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    phone        TEXT    NOT NULL,
    candidates   TEXT    NOT NULL,
    asked_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    answered_at  TEXT,
    answer_rid   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_spq_phone ON sms_pending_questions(phone, asked_at);
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
        # Campaign Studio (9/28/26): an email carries its look (headline,
        # photo, button) and its audience like a text does, and each
        # recipient keeps the send's message id so Resend's open and click
        # events (email_log) can be read back per newsletter.
        "ALTER TABLE guest_newsletters ADD COLUMN design TEXT",
        "ALTER TABLE guest_newsletters ADD COLUMN segment TEXT",
        "ALTER TABLE guest_newsletters ADD COLUMN segment_label TEXT",
        "ALTER TABLE guest_newsletter_recipients ADD COLUMN message_id TEXT",
        # Guest email consent belongs to the ADDRESS, per restaurant (marketing
        # fix round B, 9/28/26: MB-12 / CS-2). An unsubscribe was one contact
        # row's flag, so the same address on a second row (a couple, two
        # phones) kept getting mail and the public form could clear it. This
        # outlives the row, like guest_sms_optouts. guest_email owns it.
        """CREATE TABLE IF NOT EXISTS guest_email_optouts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            email         TEXT    NOT NULL,
            source        TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, email)
        )""",
        "INSERT OR IGNORE INTO guest_email_optouts (restaurant_id, email, source) "
        "SELECT restaurant_id, LOWER(TRIM(email)), 'backfill' FROM guest_contacts "
        "WHERE email_unsubscribed=1 AND email IS NOT NULL AND TRIM(email) != ''",
        # The unsubscribe token each email carried, so an old link reaches
        # the address that email went to after the contact's address changed;
        # and whether a failed send is worth retrying (not a rejected address).
        "ALTER TABLE guest_newsletter_recipients ADD COLUMN email_token TEXT",
        "CREATE INDEX IF NOT EXISTS idx_gnr_token ON guest_newsletter_recipients(email_token)",
        "UPDATE guest_newsletter_recipients SET email_token = (SELECT g.email_token FROM guest_contacts g "
        "WHERE g.id = guest_newsletter_recipients.contact_id "
        "AND LOWER(TRIM(g.email)) = LOWER(TRIM(guest_newsletter_recipients.email))) "
        "WHERE email_token IS NULL AND status='sent'",
        "ALTER TABLE guest_newsletter_recipients ADD COLUMN retryable INTEGER",
        "UPDATE guest_newsletter_recipients SET status='skipped' "
        "WHERE status='failed' AND error LIKE 'recipient suppressed%'",
        "UPDATE guest_newsletter_recipients SET retryable = CASE "
        "WHEN error LIKE 'RESEND_API_KEY%' OR error LIKE 'flood guard%' THEN 1 "
        "WHEN error = 'interrupted while sending' THEN 0 "
        "WHEN error LIKE '%\"statusCode\":4%' AND error NOT LIKE '%\"statusCode\":408%' "
        "     AND error NOT LIKE '%\"statusCode\":429%' THEN 0 ELSE 1 END "
        "WHERE status='failed' AND retryable IS NULL",
        """CREATE TABLE IF NOT EXISTS optin_invite_runs (
            restaurant_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            done_at       TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date)
        )""",
        # Consent scopes (MB-2, 9/28/26): `consent` is marketing consent;
        # a YES to the review-link invite is review_consent and nothing more.
        "ALTER TABLE guest_contacts ADD COLUMN review_consent INTEGER DEFAULT 0",
        "ALTER TABLE guest_contacts ADD COLUMN review_consent_at TEXT",
        # How the row was first created: manual | join | toast_invite. NULL
        # on rows from before this existed. The retention purge
        # (purge_unanswered_contacts) only ever considers toast_invite and
        # join rows, so a legacy or hand-added guest is never purged.
        "ALTER TABLE guest_contacts ADD COLUMN source TEXT",
        # The durable text-campaign queue (audit #10, MB-10). A campaign was
        # one daemon thread texting a list held in memory: a deploy killed it
        # silently, a 9pm cutoff dropped the rest, and nothing recorded how
        # many were meant to go. Now every recipient is a row, claimed before
        # its text; the campaign carries its status and counts; the scheduler
        # resumes whatever is left (run_campaign_sends). Rows written before
        # this existed are finished campaigns: status defaults to 'done'.
        "ALTER TABLE guest_campaigns ADD COLUMN status TEXT DEFAULT 'done'",
        "ALTER TABLE guest_campaigns ADD COLUMN total INTEGER",
        "ALTER TABLE guest_campaigns ADD COLUMN deferred_count INTEGER DEFAULT 0",
        "ALTER TABLE guest_campaigns ADD COLUMN skipped_count INTEGER DEFAULT 0",
        "ALTER TABLE guest_campaigns ADD COLUMN body TEXT",
        "ALTER TABLE guest_campaigns ADD COLUMN content_hash TEXT",
        "ALTER TABLE guest_campaigns ADD COLUMN target_day TEXT",
        "ALTER TABLE guest_campaigns ADD COLUMN created_by INTEGER",
        "ALTER TABLE guest_campaigns ADD COLUMN winback_draft_id INTEGER",
        "ALTER TABLE guest_campaigns ADD COLUMN completed_at TEXT",
        # The Opportunity Feed card a campaign began on (re-audit OPP-10): the
        # card is marked acted on when the campaign finishes having sent,
        # from whichever drain finishes it (_on_campaign_done).
        "ALTER TABLE guest_campaigns ADD COLUMN rec_key TEXT",
        """CREATE TABLE IF NOT EXISTS guest_campaign_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id   INTEGER NOT NULL,
            restaurant_id INTEGER NOT NULL,
            contact_id    INTEGER NOT NULL,
            phone         TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            claimed_at    TEXT,
            sent_at       TEXT,
            error         TEXT,
            UNIQUE(campaign_id, contact_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gcq_campaign ON guest_campaign_queue(campaign_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_gcq_restaurant ON guest_campaign_queue(restaurant_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_guest_campaigns_status ON guest_campaigns(status, restaurant_id)",
    ):
        try:
            conn.execute(col_sql)
        except Exception:
            pass
    conn.commit()
    try:
        _backfill_consent_scopes(conn)
    except Exception as e:
        print(f"[guest_marketing] consent-scope backfill skipped: {e}")
    conn.close()


def _backfill_consent_scopes(conn):
    """Boot-time, idempotent. Two corrections to rows written before the
    consent scopes existed (owner decision 9/28/26):

    1. A guest whose only consent came from a YES to the review-link invite
       becomes REVIEW-ONLY: consent=0, review_consent=1, a 'review_only'
       evidence row. "Only" means the marketing consent was not already on
       the row before that YES (consent_at more than a minute before the
       reply) and no confirmed marketing opt-in has been recorded since —
       so a guest who later confirms through the join form is never
       demoted on a later boot.
    2. Rows the invite job created (the contact and the invite written in
       the same second) are marked source='toast_invite', so the retention
       purge can find the ones that never answered.
    """
    rows = conn.execute(
        "SELECT g.id, g.restaurant_id, g.phone, g.consent_at, MIN(i.responded_at) AS yes_at "
        "FROM guest_contacts g JOIN sms_optin_invites i "
        "  ON i.restaurant_id = g.restaurant_id AND i.phone = g.phone AND i.response = 'yes' "
        "WHERE g.consent = 1 AND NOT EXISTS (SELECT 1 FROM guest_consent_events e "
        "  WHERE e.restaurant_id = g.restaurant_id AND e.phone = g.phone "
        "  AND e.event IN ('confirmed', 'review_only')) "
        "GROUP BY g.id").fetchall()
    demoted = 0
    for r in rows:
        consent_at = _parse_local(r["consent_at"])
        yes_at = _parse_local(r["yes_at"])
        if consent_at and yes_at and consent_at < yes_at - timedelta(seconds=60):
            continue          # marketing consent predates the YES: it came from elsewhere
        conn.execute("UPDATE guest_contacts SET consent=0, review_consent=1, "
                     "review_consent_at=COALESCE(review_consent_at, consent_at) WHERE id=?", (r["id"],))
        _insert_consent_event(conn, r["restaurant_id"], "review_only", "backfill", phone=r["phone"],
                              detail="consent came only from a YES to the review-link invite")
        demoted += 1
    conn.execute(
        "UPDATE guest_contacts SET source='toast_invite' WHERE source IS NULL AND EXISTS ("
        "  SELECT 1 FROM sms_optin_invites i WHERE i.restaurant_id = guest_contacts.restaurant_id "
        "  AND i.phone = guest_contacts.phone "
        "  AND ABS(julianday(i.sent_at) - julianday(guest_contacts.created_at)) < 120.0 / 86400)")
    conn.commit()
    if demoted:
        print(f"[guest_marketing] {demoted} review-link YES contact(s) moved to review-only consent")
    return demoted


def _parse_local(value):
    """A stored naive-local ISO stamp, or None."""
    try:
        return datetime.fromisoformat(str(value or "").strip().replace(" ", "T")[:26]) if value else None
    except ValueError:
        return None


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
    return f"{_hour_label(GUEST_SMS_EARLIEST_HOUR)} and {_hour_label(GUEST_SMS_LATEST_HOUR)}"


def _hour_label(h) -> str:
    return f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'}"


# The window was checked when a text was SUBMITTED, and Twilio queues what it
# cannot send at once for up to its default ValidityPeriod (hours): a big list
# pressed at 8:40pm could reach phones after 9 (MB-13, audit #60). Every
# campaign text now tells Twilio to drop it rather than deliver it after the
# window closes, never longer than four hours.
GUEST_SMS_MAX_VALIDITY_SECONDS = 4 * 3600


def guest_sms_validity_seconds(restaurant_id) -> int:
    """Seconds until 9:00 PM on the restaurant's clock, capped at
    GUEST_SMS_MAX_VALIDITY_SECONDS: the Twilio ValidityPeriod a guest text is
    sent with. 0 outside the window (or when the clock can't be read), which
    means: don't send."""
    try:
        now = _sms_local_now(restaurant_id)
    except Exception:
        return 0
    if not (GUEST_SMS_EARLIEST_HOUR <= now.hour < GUEST_SMS_LATEST_HOUR):
        return 0
    close = now.replace(hour=GUEST_SMS_LATEST_HOUR, minute=0, second=0, microsecond=0)
    left = int((close - now).total_seconds())
    return max(1, min(left, GUEST_SMS_MAX_VALIDITY_SECONDS)) if left > 0 else 0


# ── Who may be texted: one definition (MB-2 / OPP-6, 9/28/26) ─────────────
# The feed counted "opted-in guests" without the review-link YESes, the
# Studio counted them, and the send texted them. Every marketing send and
# every count of who can be texted now reads this one fragment; a guest is
# in it only with MARKETING consent, not unsubscribed, and no STOP on file
# for this restaurant or the shared number.

def marketing_text_sql(alias="") -> str:
    """WHERE fragment (no leading AND) for "may receive a marketing text".
    `alias` is the guest_contacts table alias in the caller's query (none:
    the table's own name). Always qualified: inside the STOP subquery a bare
    `phone` would be the opt-out table's own column, and any one STOP on file
    would empty every audience."""
    p = f"{alias or 'guest_contacts'}."
    return (f"{p}consent=1 AND COALESCE({p}unsubscribed,0)=0 AND NOT EXISTS ("
            f"SELECT 1 FROM guest_sms_optouts o_ WHERE o_.phone={p}phone "
            f"AND o_.restaurant_id IN (0, {p}restaurant_id))")


def review_text_sql(alias="") -> str:
    """WHERE fragment for "may receive a review-link text": marketing
    consent, or the narrower review-link consent a YES to the invite gives."""
    p = f"{alias}." if alias else ""
    return f"({p}consent=1 OR COALESCE({p}review_consent,0)=1) AND COALESCE({p}unsubscribed,0)=0"


_CONTACT_COLS = ("id, name, phone, consent, consent_at, unsubscribed, last_visit, last_review_requested_at, "
                 "visit_count, last_campaign_at, review_consent, source")


def _contact_dict(r):
    return {"id": r["id"], "name": r["name"] or "", "phone": r["phone"],
            "consent": bool(r["consent"]), "consent_at": r["consent_at"],
            "unsubscribed": bool(r["unsubscribed"]), "last_visit": r["last_visit"],
            "last_review_requested_at": r["last_review_requested_at"],
            "visit_count": int(r["visit_count"] or 0),
            "last_campaign_at": r["last_campaign_at"],
            # Review-link consent only: can get a review link, never a campaign.
            "review_only": bool(r["review_consent"]) and not bool(r["consent"]),
            "source": r["source"]}


def get_guest_contacts(restaurant_id, consent_only=False, db_path=DB_PATH):
    """consent_only=True is the enforcement point for actually sending a
    MARKETING text (marketing_text_sql) — same shape as
    notify.get_alert_contacts. Management UI wants consent_only=False so
    the owner can see (and remove) every contact, consented or not.
    `consent` in each row is marketing consent; `review_only` marks a guest
    who agreed to review links and nothing else."""
    conn = get_conn(db_path)
    query = f"SELECT {_CONTACT_COLS} FROM guest_contacts WHERE restaurant_id=?"
    if consent_only:
        query += " AND " + marketing_text_sql()
    rows = conn.execute(query + " ORDER BY id DESC", (restaurant_id,)).fetchall()
    conn.close()
    return [_contact_dict(r) for r in rows]


# ── Consent evidence ────────────────────────────────────────────────────────

CONSENT_EVENTS = ("requested", "confirmed", "opted_out", "resubscribed", "review_only")


def _insert_consent_event(conn, restaurant_id, event, source, phone=None, email=None, channel="sms",
                          ip=None, user_agent=None, disclosure_version=None, disclosure_hash=None,
                          message_sid=None, detail=None):
    conn.execute(
        "INSERT INTO guest_consent_events (restaurant_id, channel, phone, email, event, source, ip, "
        "user_agent, disclosure_version, disclosure_hash, message_sid, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(restaurant_id or 0), channel, phone, email, event, source, (ip or None),
         (user_agent or "")[:300] or None, disclosure_version, disclosure_hash, message_sid,
         (detail or "")[:300] or None))


def record_consent_event(restaurant_id, event, source, phone=None, email=None, channel="sms", ip=None,
                         user_agent=None, disclosure_version=None, disclosure_hash=None, message_sid=None,
                         detail=None, db_path=DB_PATH):
    """Append one consent change to guest_consent_events. Never raises: a
    STOP or an opt-in is not lost because its evidence row failed, but the
    failure is captured for the operator. Also the entry point for the email
    channel (channel='email')."""
    try:
        conn = get_conn(db_path)
        try:
            _insert_consent_event(conn, restaurant_id, event, source,
                                  phone=_normalize_phone(phone) if phone else None, email=email,
                                  channel=channel, ip=ip, user_agent=user_agent,
                                  disclosure_version=disclosure_version, disclosure_hash=disclosure_hash,
                                  message_sid=message_sid, detail=detail)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="guest_consent_evidence", context=f"restaurant_id={restaurant_id} event={event}")
        except Exception:
            pass
        return False


def consent_events(restaurant_id=None, phone=None, db_path=DB_PATH) -> list:
    """The evidence for a restaurant or a number, newest first — "how did
    this number get on the list" answered from the record, not the row."""
    conn = get_conn(db_path)
    try:
        where, args = [], []
        if restaurant_id is not None:
            where.append("restaurant_id IN (0, ?)")
            args.append(int(restaurant_id))
        if phone:
            where.append("phone=?")
            args.append(_normalize_phone(phone))
        sql = "SELECT * FROM guest_consent_events" + (" WHERE " + " AND ".join(where) if where else "")
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT 500", args).fetchall()]
    finally:
        conn.close()


def add_guest_contact_manual(restaurant_id, phone, name=None, db_path=DB_PATH, source="manual"):
    """Owner adding a number for their own reference/tracking — never
    consented, can never receive a campaign until the guest opts in
    themselves via the public join page."""
    return _upsert_contact(restaurant_id, phone, name=name, consent=False, db_path=db_path, source=source)


def add_guest_contact_public_optin(restaurant_id, phone, name=None, db_path=DB_PATH, source="explicit_optin",
                                   message_sid=None, ip=None, user_agent=None, disclosure_version=None,
                                   disclosure_hash=None, detail=None):
    """A CONFIRMED marketing opt-in by the guest themselves: their Y to the
    join form's confirmation text (confirm path in handle_inbound_sms), or
    another guest-initiated marketing opt-in. The public form itself no
    longer calls this — it records a pending request (request_public_optin)
    and consent waits for the phone's own Y (double opt-in, MB-4). Writes a
    'confirmed' evidence row. Never a visit, never un-STOPs."""
    cid = _upsert_contact(restaurant_id, phone, name=name, consent=True, db_path=db_path)
    record_consent_event(restaurant_id, "confirmed", source, phone=phone, message_sid=message_sid, ip=ip,
                         user_agent=user_agent, disclosure_version=disclosure_version,
                         disclosure_hash=disclosure_hash, detail=detail, db_path=db_path)
    return cid


def add_guest_contact_sms_optin(restaurant_id, phone, name=None, db_path=DB_PATH, message_sid=None, detail=None):
    """The guest texting YES back to the review-link invite ("Reply YES if
    we can text you a quick link to leave a review").

    That YES agreed to a review link and nothing else, so it sets
    review_consent — never marketing consent (MB-2 / SMS-1). It used to set
    consent=1 and the guest was in every promo blast from then on. A number
    Toast happened to capture at checkout is not even this; that only ever
    reaches add_guest_contact_manual."""
    cid = _upsert_contact(restaurant_id, phone, name=name, consent=False, review=True, db_path=db_path)
    record_consent_event(restaurant_id, "review_only", "sms_reply", phone=phone, message_sid=message_sid,
                         detail=detail, db_path=db_path)
    return cid


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


def _upsert_contact(restaurant_id, phone, name, consent, db_path, review=False, source=None):
    """Create or update one contact. `consent` grants MARKETING consent,
    `review` the review-link scope; neither is ever revoked here (that is
    unsubscribe's job, an explicit action).

    Neither is a visit (MB-5 / #3 / #6): last_visit and visit_count are left
    alone. The join form used to set last_visit=now and visit_count+1 on
    every submission, so three submits made a "Regular (3+ visits)" and a
    window-QR join got a "thanks for visiting" review request three hours
    later. `source` is recorded on a new row (see purge_unanswered_contacts);
    an existing row the invite job created takes the new source, so a guest
    the owner or the guest has touched is never purged as an unanswered
    invite."""
    phone = _normalize_phone(phone)
    conn = get_conn(db_path)
    existing = conn.execute(
        "SELECT id, consent, source FROM guest_contacts WHERE restaurant_id=? AND phone=?",
        (restaurant_id, phone)
    ).fetchone()
    now_iso = None
    if consent or review:
        from time_utils import restaurant_now_by_id
        now_iso = restaurant_now_by_id(restaurant_id, naive=True).isoformat()
    clean_name = (name or "").strip() or None
    if existing:
        # Never un-STOPs: a STOP is undone only by an exact START/UNSTOP/YES
        # texted from that phone (handle_inbound_sms). Nor does a submission
        # rename an existing guest (COALESCE).
        if consent:
            conn.execute(
                "UPDATE guest_contacts SET consent=1, consent_at=COALESCE(consent_at,?), "
                "name=COALESCE(name,?) WHERE id=?",
                (now_iso, clean_name, existing["id"])
            )
        if review:
            conn.execute(
                "UPDATE guest_contacts SET review_consent=1, review_consent_at=COALESCE(review_consent_at,?), "
                "name=COALESCE(name,?) WHERE id=?",
                (now_iso, clean_name, existing["id"])
            )
        if not consent and not review and clean_name:
            conn.execute("UPDATE guest_contacts SET name=? WHERE id=?", (clean_name, existing["id"]))
        if existing["source"] == "toast_invite" and source and source != "toast_invite":
            conn.execute("UPDATE guest_contacts SET source=? WHERE id=?", (source, existing["id"]))
        conn.commit()
        contact_id = existing["id"]
    else:
        # A number that texted STOP (here, or to the shared number) comes
        # back unsubscribed, however it comes back: a POS import, an owner
        # re-typing it, the public form. Only their own START undoes it.
        stopped = _opted_out_here(conn, restaurant_id, phone)
        cur = conn.execute(
            "INSERT INTO guest_contacts (restaurant_id, name, phone, consent, consent_at, review_consent, "
            "review_consent_at, visit_count, unsubscribed, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, clean_name, phone, 1 if consent else 0, now_iso if consent else None,
             1 if review else 0, now_iso if review else None, 0, 1 if stopped else 0, source)
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
    again later is added unsubscribed (CLIENT-34). The consent evidence
    (guest_consent_events) is not touched: it outlives the row."""
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


def unsubscribe_guest(restaurant_id, phone, db_path=DB_PATH, source="owner"):
    phone = _normalize_phone(phone)
    conn = get_conn(db_path)
    n = conn.execute(
        "UPDATE guest_contacts SET unsubscribed=1 WHERE restaurant_id=? AND phone=? AND unsubscribed=0",
        (restaurant_id, phone)
    ).rowcount
    conn.commit()
    conn.close()
    if n:
        record_consent_event(restaurant_id, "opted_out", source, phone=phone, db_path=db_path)


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
#
# Keywords are EXACT (MB-1 / #6, 9/28/26). A reply that merely contained a
# restaurant's name used to opt the sender in and delete their STOP: "Why is
# Kimball Diner still texting me? I said stop" came back "Thanks! You're in",
# and "what time does kimball diner close tonight" from a hand-added number
# made it consented. Now:
#   * a STOP in any reasonable form stops (unchanged: NFKC, zero-width, short
#     phrases — _is_stop);
#   * only a message that IS exactly START / UNSTOP / YES / Y (after
#     normalisation) undoes a STOP, and it grants no consent a row did not
#     already have;
#   * only an exact YES / Y answers a pending question — the join form's
#     confirmation (marketing consent) or the review-link invite
#     (review-only consent);
#   * a restaurant's name counts only as the answer to a "which restaurant
#     did you mean" question open for that phone, or after an explicit YES
#     ("yes kimball diner") naming one of the restaurants it could answer.

STOP_KEYWORDS  = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke"}
YES_WORDS      = {"yes", "y"}
RESUME_WORDS   = {"start", "unstop"}
START_KEYWORDS = YES_WORDS | RESUME_WORDS      # exact-message opt-in keywords (Twilio's defaults)
HELP_KEYWORDS  = {"help", "info"}

# ── The public join form: double opt-in (MB-4 / #3 / #9, owner 9/28/26) ────
JOIN_CONFIRM_HOURS = 48        # a Y later than this confirms nothing
JOIN_PHONE_DAILY_LIMIT = 3     # form submissions for one number per 24h, all restaurants
JOIN_IP_HOURLY_LIMIT = 20      # durable, beside the in-process per-IP limit on the route
JOIN_RESEND_MINUTES = 10       # a double tap does not send a second confirmation text
QUESTION_HOURS = 24            # how long "which restaurant did you mean" stays open

JOIN_DISCLOSURE_VERSION = "2026-09-28"
JOIN_DISCLOSURE = ("I agree to receive recurring marketing text messages from {restaurant} at the number "
                   "provided. Consent is not a condition of purchase. Msg frequency varies. Msg & data rates "
                   "may apply. Reply STOP to cancel, HELP for help. We'll text you once to confirm.")


def join_disclosure(restaurant_name) -> str:
    """The consent sentence the join page shows, word for word — the one
    whose version and hash each request records."""
    return JOIN_DISCLOSURE.format(restaurant=(restaurant_name or "this restaurant").strip())


def join_disclosure_hash() -> str:
    return hashlib.sha256(f"{JOIN_DISCLOSURE_VERSION}\n{JOIN_DISCLOSURE}".encode("utf-8")).hexdigest()[:16]


_GSM_SWAPS = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
              "…": "...", " ": " "}
_CONFIRM_TAIL = (": Reply Y to confirm texts from us. Msg frequency varies. Msg & data rates may apply. "
                 "Reply STOP to cancel, HELP for help.")


def confirmation_text(restaurant_name) -> str:
    """The one confirmation text a join sends — a single GSM-7 segment
    (160 characters), the restaurant's name trimmed to fit. A curly
    apostrophe would make it UCS-2 and three segments."""
    name = "".join(_GSM_SWAPS.get(ch, ch) for ch in (restaurant_name or "")).strip() or "Text club"
    room = 160 - len(_CONFIRM_TAIL)
    return name[:room].rstrip() + _CONFIRM_TAIL


def request_public_optin(restaurant_id, phone, name=None, ip=None, user_agent=None, db_path=DB_PATH) -> dict:
    """The public join form's submission: a PENDING opt-in and one
    confirmation text. Consent is set only when that phone replies Y within
    JOIN_CONFIRM_HOURS (handle_inbound_sms). Anyone with the link could
    enrol any number, turning a POS-captured number into a textable one and
    arming a "thanks for visiting" review text three hours later (MB-4,
    ARC-1).

    Never touches consent, last_visit or visit_count, and never re-subscribes
    a STOP. A number not yet on the list is added unconsented (so an email
    given on the same form has a row to attach to). A number that already
    has marketing consent, or that texted STOP, gets no text — and the form's
    answer is the same either way, so the page does not tell a stranger who
    is on the list.

    Returns {"ok": True, "pending": True, "contact_id"} or
    {"ok": False, "status": 429|502, "error"}."""
    phone = _normalize_phone(phone)
    ip = (ip or "").strip() or None
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT name FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        restaurant_name = (r["name"] if r else "") or ""
        by_phone = conn.execute("SELECT COUNT(*) FROM guest_optin_requests WHERE phone=? "
                                "AND requested_at >= datetime('now','-1 day')", (phone,)).fetchone()[0]
        by_ip = (conn.execute("SELECT COUNT(*) FROM guest_optin_requests WHERE ip=? "
                              "AND requested_at >= datetime('now','-1 hour')", (ip,)).fetchone()[0] if ip else 0)
        recent = conn.execute("SELECT 1 FROM guest_optin_requests WHERE phone=? AND restaurant_id=? "
                              "AND status='pending' AND requested_at >= datetime('now', ?) LIMIT 1",
                              (phone, restaurant_id, f"-{JOIN_RESEND_MINUTES} minutes")).fetchone()
        on_list = conn.execute("SELECT id FROM guest_contacts WHERE restaurant_id=? AND phone=?",
                               (restaurant_id, phone)).fetchone()
        existing = conn.execute("SELECT id FROM guest_contacts WHERE restaurant_id=? AND phone=? AND "
                                + marketing_text_sql(), (restaurant_id, phone)).fetchone()
    finally:
        conn.close()
    if by_phone >= JOIN_PHONE_DAILY_LIMIT:
        return {"ok": False, "status": 429,
                "error": "We've already sent that number a text to confirm. Reply Y to it to finish joining."}
    if by_ip >= JOIN_IP_HOURLY_LIMIT:
        return {"ok": False, "status": 429, "error": "Too many attempts. Please try again later."}

    # A new number is added unconsented with the name typed; an existing
    # guest is never renamed by a stranger's submission (the name waits on
    # the request and is applied, if the row has none, on the Y).
    contact_id = _upsert_contact(restaurant_id, phone, name=None if on_list else name, consent=False,
                                 db_path=db_path, source="join")
    if recent:
        return {"ok": True, "pending": True, "contact_id": contact_id}
    stopped = phone_opted_out(phone, db_path=db_path)
    status = "already" if existing else ("blocked_stop" if stopped else "pending")
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO guest_optin_requests (restaurant_id, phone, name, ip, user_agent, disclosure_version, "
            "disclosure_hash, status) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, phone, (name or "").strip()[:80] or None, ip, (user_agent or "")[:300] or None,
             JOIN_DISCLOSURE_VERSION, join_disclosure_hash(), status))
        request_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    if status != "pending":
        return {"ok": True, "pending": True, "contact_id": contact_id}
    record_consent_event(restaurant_id, "requested", "join_form", phone=phone, ip=ip, user_agent=user_agent,
                         disclosure_version=JOIN_DISCLOSURE_VERSION, disclosure_hash=join_disclosure_hash(),
                         detail=f"join request {request_id}", db_path=db_path)
    try:
        sent = bool(send_sms(phone, confirmation_text(restaurant_name), use_case="guest"))
    except Exception:
        sent = False
    if not sent:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE guest_optin_requests SET status='send_failed' WHERE id=?", (request_id,))
            conn.commit()
        finally:
            conn.close()
        return {"ok": False, "status": 502,
                "error": "We couldn't send the confirmation text just now. Please try again in a few minutes."}
    return {"ok": True, "pending": True, "contact_id": contact_id}


# The replies, apart from the keyword logic that picks them (MB-15, audit
# #65). A STOP is recorded against the number for every restaurant on this
# shared number, and the owner-alert sender honours it too
# (notify.get_alert_contacts) — so the reply says exactly that, and no more:
# it used to promise "no more texts from us" while alerts kept coming.
STOP_REPLY = ("You're unsubscribed from every restaurant that texts you from this number. "
              "Reply START to opt back in.")
START_REPLY = "You're opted back in to texts from this number. Reply STOP anytime."


def _clear_platform_stop(phone, db_path=DB_PATH) -> bool:
    """A START from a number that is on no restaurant's guest list: lift its
    platform-level STOP (restaurant 0), which is all that holds back its
    owner-alert texts (notify.sms_stopped_phones). True when there was one."""
    conn = get_conn(db_path)
    try:
        n = conn.execute("DELETE FROM guest_sms_optouts WHERE restaurant_id=0 AND phone=?",
                         (_normalize_phone(phone),)).rowcount
        conn.commit()
        return bool(n)
    finally:
        conn.close()


def _support_contact() -> str:
    """Where a guest who replies HELP can reach a person: the address the
    platform already signs its texts with (config), never an invented one."""
    try:
        import config
        return os.getenv("SUPPORT_EMAIL") or config.will_email()
    except Exception:
        return os.getenv("SUPPORT_EMAIL") or ""


def help_reply(names) -> str:
    """The carrier's HELP answer: whose program this is, who sends it, how
    often, what it costs, a contact and how to stop (CTIA)."""
    names = " and ".join(n for n in (names or []) if n) or "a restaurant you joined"
    contact = _support_contact()
    return (f"{names}: guest texts sent by Cavnar AI. Msg frequency varies. Msg & data rates may apply. "
            + (f"Help: {contact}. " if contact else "") + "Reply STOP to unsubscribe.")


def resubscribe_guest(restaurant_id, phone, db_path=DB_PATH):
    """Undo a STOP for one restaurant (and the shared number's record of
    it). The inbound handler's START uses _undo_stop, which follows the STOP
    everywhere it went."""
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


def _undo_stop(phone, message_sid=None, db_path=DB_PATH) -> list:
    """The guest's own exact START / UNSTOP / YES: their STOP went to every
    restaurant on the shared number, so this undoes it everywhere — except a
    row the OWNER unsubscribed (unsubscribe_guest), which the guest's
    keyword to the shared number does not override. Grants no consent: a
    row that had none still has none. Returns the restaurant ids resumed."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, restaurant_id FROM guest_contacts WHERE phone=? AND unsubscribed=1",
                            (phone,)).fetchall()
        held, resumed = set(), []
        for r in rows:
            # The owner's unsubscribe stands unless the guest opted back in
            # (resubscribed / confirmed) after it; the guest's own STOP to
            # the shared number, recorded after it, does not undo it.
            last = conn.execute("SELECT event, source FROM guest_consent_events WHERE restaurant_id=? AND phone=? "
                                "AND ((event='opted_out' AND source='owner') OR event IN ('resubscribed','confirmed')) "
                                "ORDER BY id DESC LIMIT 1", (r["restaurant_id"], phone)).fetchone()
            if last and last["event"] == "opted_out" and last["source"] == "owner":
                held.add(r["id"])
                continue
            conn.execute("UPDATE guest_contacts SET unsubscribed=0 WHERE id=?", (r["id"],))
            resumed.append(r["restaurant_id"])
        conn.execute("DELETE FROM guest_sms_optouts WHERE phone=?", (phone,))
        for rid in [0] + resumed:
            _insert_consent_event(conn, rid, "resubscribed", "sms_reply", phone=phone, message_sid=message_sid)
        conn.commit()
        return resumed
    finally:
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


def _pending_answers(phone, db_path=DB_PATH) -> list:
    """What a YES from this phone could be answering, newest first:
    [{"rid", "name", "kind"}] with kind "confirm" (a join request waiting
    for its Y, JOIN_CONFIRM_HOURS) or "invite" (a review-link invite,
    _INVITE_REPLY_WINDOW_DAYS). One entry per restaurant; a join request
    wins over an invite at the same restaurant.

    Every restaurant shares one platform Twilio number, so the inbound `To`
    cannot identify the restaurant. The old fallback — every restaurant
    with a contact row for this phone — is gone: a row is not a question,
    and treating it as one is how a hand-added number was opted in (MB-1)."""
    conn = get_conn(db_path)
    try:
        confirms = conn.execute(
            "SELECT q.restaurant_id AS rid, r.name, MAX(q.requested_at) AS at FROM guest_optin_requests q "
            "JOIN restaurants r ON r.id = q.restaurant_id WHERE q.phone=? AND q.status='pending' "
            "AND q.requested_at >= datetime('now', ?) GROUP BY q.restaurant_id",
            (phone, f"-{JOIN_CONFIRM_HOURS} hours")).fetchall()
        invites = conn.execute(
            "SELECT i.restaurant_id AS rid, r.name, MAX(i.sent_at) AS at FROM sms_optin_invites i "
            "JOIN restaurants r ON r.id = i.restaurant_id WHERE i.phone=? AND i.responded_at IS NULL "
            "AND i.sent_at >= datetime('now', ?) GROUP BY i.restaurant_id",
            (phone, f"-{_INVITE_REPLY_WINDOW_DAYS} days")).fetchall()
    finally:
        conn.close()
    out = {}
    for r in invites:
        out[r["rid"]] = {"rid": r["rid"], "name": r["name"] or "", "kind": "invite", "at": r["at"] or ""}
    for r in confirms:
        out[r["rid"]] = {"rid": r["rid"], "name": r["name"] or "", "kind": "confirm", "at": r["at"] or ""}
    return sorted(out.values(), key=lambda p: p["at"], reverse=True)


def _mark_invite_response(phone, response, db_path=DB_PATH, restaurant_id=None):
    """Close the invite this reply answers.

    `restaurant_id` pins which one. Without it the most recent unanswered
    invite is closed, but a YES must always pass the restaurant it was
    actually resolved to — closing the wrong restaurant's invite leaves the
    right one open forever and the guest never gets their review link.
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
            return None
        now_iso = restaurant_now_by_id(row["restaurant_id"], naive=True).isoformat()
        conn.execute("UPDATE sms_optin_invites SET responded_at=?, response=? WHERE id=?",
                     (now_iso, response, row["id"]))
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def _apply_yes(phone, pending, message_sid=None, db_path=DB_PATH):
    """A YES resolved to one restaurant's pending question. The reply."""
    rid, kind = pending["rid"], pending["kind"]
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT name, google_place_id FROM restaurants WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    name = ((r["name"] if r else "") or "us").strip()
    if kind == "confirm":
        conn = get_conn(db_path)
        try:
            req = conn.execute(
                "SELECT * FROM guest_optin_requests WHERE phone=? AND restaurant_id=? AND status='pending' "
                "AND requested_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                (phone, rid, f"-{JOIN_CONFIRM_HOURS} hours")).fetchone()
            if req is None:
                return None
            conn.execute("UPDATE guest_optin_requests SET status='confirmed', confirmed_at=datetime('now'), "
                         "confirm_sid=? WHERE phone=? AND restaurant_id=? AND status='pending'",
                         (message_sid, phone, rid))
            conn.commit()
        finally:
            conn.close()
        add_guest_contact_public_optin(rid, phone, name=req["name"], db_path=db_path, source="sms_reply",
                                       message_sid=message_sid, ip=req["ip"], user_agent=req["user_agent"],
                                       disclosure_version=req["disclosure_version"],
                                       disclosure_hash=req["disclosure_hash"],
                                       detail=f"confirmed join request {req['id']}")
        return (f"{name}: You're in! Msg frequency varies. Msg & data rates may apply. "
                "Reply STOP to cancel, HELP for help.")

    # A YES to the review-link invite: review-link consent, and the link
    # they asked for, now. It is never a visit (#6) and never marketing
    # consent (MB-2).
    invite_id = _mark_invite_response(phone, "yes", db_path=db_path, restaurant_id=rid)
    add_guest_contact_sms_optin(rid, phone, db_path=db_path, message_sid=message_sid,
                                detail=f"invite {invite_id}" if invite_id else None)
    link = _google_review_link(r["google_place_id"] if r else None)
    if not link:
        return f"Thanks! {name} can text you a review link now. Reply STOP to opt out."
    from time_utils import restaurant_now_by_id
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE guest_contacts SET last_review_requested_at=? WHERE restaurant_id=? AND phone=?",
                     (restaurant_now_by_id(rid, naive=True).isoformat(), rid, phone))
        row = conn.execute("SELECT name FROM guest_contacts WHERE restaurant_id=? AND phone=?",
                           (rid, phone)).fetchone()
        conn.execute("INSERT INTO review_requests (restaurant_id, customer_name, customer_email, customer_phone, "
                     "method, status) VALUES (?,?,?,?,?,?)",
                     (rid, (row["name"] if row else "") or "", "", phone, "sms_reply", "sent"))
        conn.commit()
    except Exception as e:
        import ops
        ops.capture(e, job="guest_review_reply", context=f"restaurant_id={rid}")
    finally:
        conn.close()
    return f"Thanks! Here's the link to review {name}: {link} Reply STOP to opt out."


def _open_question(phone, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        return conn.execute(
            "SELECT id, candidates FROM sms_pending_questions WHERE phone=? AND answered_at IS NULL "
            "AND asked_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (phone, f"-{QUESTION_HOURS} hours")).fetchone()
    finally:
        conn.close()


def _close_questions(phone, answer_rid=None, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sms_pending_questions SET answered_at=datetime('now'), answer_rid=? "
                     "WHERE phone=? AND answered_at IS NULL", (answer_rid, phone))
        conn.commit()
    finally:
        conn.close()


def _ask_which(phone, pending, db_path=DB_PATH):
    """Several restaurants are waiting on this phone's answer and only the
    guest knows which they meant — consent recorded against a guess is
    consent for a business they never agreed to hear from."""
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO sms_pending_questions (phone, candidates) VALUES (?,?)",
                     (phone, json.dumps([p["rid"] for p in pending])))
        conn.commit()
    finally:
        conn.close()
    names = " or ".join(p["name"] for p in pending[:3] if p["name"])
    return (f"Thanks! Which restaurant did you mean: {names}? "
            "Reply with the name. Reply STOP to opt out of all of them.")


def _named_answer(phone, norm, pending, db_path=DB_PATH):
    """The pending restaurant a name picks out, or None. "yes <name>" is an
    explicit YES naming one of the restaurants it could answer; a bare name
    counts only while a "which restaurant" question is open for this phone.
    The whole message must be the name — "why is kimball diner still
    texting me" names a restaurant and answers nothing."""
    words = norm.split()
    after_yes = " ".join(words[1:]) if len(words) > 1 and words[0] in YES_WORDS else None
    by_name = {}
    for p in pending:
        key = _normalise_sms(p["name"])
        if key:
            by_name.setdefault(key, []).append(p)
    if after_yes and len(by_name.get(after_yes, [])) == 1:
        _close_questions(phone, by_name[after_yes][0]["rid"], db_path=db_path)
        return by_name[after_yes][0]
    q = _open_question(phone, db_path=db_path)
    if not q:
        return None
    try:
        asked = set(int(x) for x in json.loads(q["candidates"] or "[]"))
    except (ValueError, TypeError):
        asked = set()
    for text in (norm, after_yes):
        hits = [p for p in by_name.get(text or "", []) if p["rid"] in asked]
        if len(hits) == 1:
            _close_questions(phone, hits[0]["rid"], db_path=db_path)
            return hits[0]
    return None


_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿"), None)
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


def _stop_everywhere(phone, message_sid=None, db_path=DB_PATH):
    """A STOP unsubscribes the guest from EVERY restaurant that has their
    number, closes every question waiting on them, and is recorded apart
    from the contact rows (which an owner can delete)."""
    from time_utils import restaurant_now_by_id
    conn = get_conn(db_path)
    try:
        rids = [r[0] for r in conn.execute("SELECT DISTINCT restaurant_id FROM guest_contacts WHERE phone=?",
                                           (phone,)).fetchall()]
        conn.execute("UPDATE guest_contacts SET unsubscribed=1 WHERE phone=?", (phone,))
        conn.execute("INSERT OR IGNORE INTO guest_sms_optouts (restaurant_id, phone) VALUES (0, ?)", (phone,))
        for inv in conn.execute("SELECT id, restaurant_id FROM sms_optin_invites WHERE phone=? "
                                "AND responded_at IS NULL", (phone,)).fetchall():
            conn.execute("UPDATE sms_optin_invites SET responded_at=?, response='stop' WHERE id=?",
                         (restaurant_now_by_id(inv["restaurant_id"], naive=True).isoformat(), inv["id"]))
        conn.execute("UPDATE guest_optin_requests SET status='stopped' WHERE phone=? AND status='pending'", (phone,))
        conn.execute("UPDATE sms_pending_questions SET answered_at=datetime('now') WHERE phone=? "
                     "AND answered_at IS NULL", (phone,))
        conn.commit()
    finally:
        conn.close()
    for rid in [0] + rids:
        record_consent_event(rid, "opted_out", "sms_reply", phone=phone, message_sid=message_sid, db_path=db_path)


def handle_inbound_sms(from_phone, body, db_path=DB_PATH, message_sid=None):
    """Process one inbound guest text. Returns a reply string to send back
    (or None to stay silent). `message_sid` is Twilio's MessageSid for the
    inbound text, kept on the consent evidence it produces.

    A STOP unsubscribes the guest from EVERY restaurant that has their
    number, not just the one we think they were replying to — when someone
    says stop, the safe reading is stop, not "stop from this one tenant".
    """
    phone = _normalize_phone(from_phone)
    norm = _normalise_sms(body)
    word = norm.split()[0] if norm else ""

    if _is_stop(body):
        _stop_everywhere(phone, message_sid=message_sid, db_path=db_path)
        return STOP_REPLY

    pending = _pending_answers(phone, db_path=db_path)

    if word in HELP_KEYWORDS:
        # The carrier requirement for HELP: name the program, say how to stop.
        conn = get_conn(db_path)
        try:
            known = [r[0] or "" for r in conn.execute(
                "SELECT DISTINCT r.name FROM guest_contacts g JOIN restaurants r ON r.id = g.restaurant_id "
                "WHERE g.phone=?", (phone,)).fetchall()]
        finally:
            conn.close()
        names = []
        for n in [p["name"] for p in pending] + known:
            if n and n not in names:
                names.append(n)
        if not names:
            return None          # nothing of ours — stay silent rather than guess
        # The carrier's HELP answer, with a contact (MB-15, help_reply).
        return help_reply(names[:3])

    if norm in START_KEYWORDS:
        resumed = None
        if phone_opted_out(phone, db_path=db_path):
            resumed = _undo_stop(phone, message_sid=message_sid, db_path=db_path)
        if norm in YES_WORDS and pending:
            if len(pending) == 1:
                _close_questions(phone, pending[0]["rid"], db_path=db_path)
                return _apply_yes(phone, pending[0], message_sid=message_sid, db_path=db_path)
            return _ask_which(phone, pending, db_path=db_path)
        if resumed is not None:
            conn = get_conn(db_path)
            try:
                names = [r[0] for r in conn.execute(
                    "SELECT name FROM restaurants WHERE id IN (%s)" % ",".join("?" * len(resumed)),
                    resumed).fetchall()] if resumed else []
            finally:
                conn.close()
            who = f" from {' and '.join(n for n in names[:3] if n)}" if any(names) else ""
            # No restaurant to name: a platform-level STOP only (an owner-alert
            # contact) - _undo_stop lifted it, which is what unblocks alerts.
            return f"You're resubscribed to texts{who}. Reply STOP to opt out." if who else START_REPLY
        return None

    picked = _named_answer(phone, norm, pending, db_path=db_path)
    if picked:
        return _apply_yes(phone, picked, message_sid=message_sid, db_path=db_path)
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


def _norm_segment(segment):
    segment = (segment or "all").strip().lower()
    return segment if segment in SEGMENTS else "all"


_ISO_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*"


def _iso_col(col):
    """A stored stamp in comparable form: first 19 characters, 'T' between
    date and time — the same truncation filter_segment parses."""
    return f"replace(substr({col},1,19),' ','T')"


def _segment_where(segment, now, alias=""):
    """SQL twin of filter_segment: (fragment, params). `now` is the
    restaurant's naive local clock. lapsed_N is "last visit N or more whole
    days ago" — last_visit <= now - N days — and a missing or unreadable
    visit is never lapsed."""
    p = f"{alias}." if alias else ""
    segment = (segment or "all").strip().lower()
    if segment not in SEGMENTS:
        # An unknown audience reaches nobody, never everyone (CS-17) - the
        # same rule filter_segment keeps; the send routes refuse it first.
        return "0=1", []
    if segment in ("lapsed_30", "lapsed_60"):
        cutoff = (now - timedelta(days=30 if segment == "lapsed_30" else 60)).strftime("%Y-%m-%dT%H:%M:%S")
        return (f"({p}last_visit GLOB '{_ISO_GLOB}' AND {_iso_col(p + 'last_visit')} <= ?)", [cutoff])
    if segment == "regulars":
        return f"COALESCE({p}visit_count,0) >= 3", []
    if segment == "new":
        return f"COALESCE({p}visit_count,0) = 1", []
    return "1=1", []


def _not_recent_where(now, alias=""):
    """SQL twin of `not _too_soon`: no campaign text inside
    GUEST_SMS_MIN_DAYS_BETWEEN whole days (an unreadable stamp does not hold
    a guest back, as in _too_soon)."""
    p = f"{alias}." if alias else ""
    cutoff = (now - timedelta(days=GUEST_SMS_MIN_DAYS_BETWEEN)).strftime("%Y-%m-%dT%H:%M:%S")
    return (f"({p}last_campaign_at IS NULL OR {p}last_campaign_at NOT GLOB '{_ISO_GLOB}' "
            f"OR {_iso_col(p + 'last_campaign_at')} <= ?)", [cutoff])


def marketing_audience(restaurant_id, segment="all", exclude_recent=False, db_path=DB_PATH) -> list:
    """THE audience of a marketing text (MB-2 / OPP-6): guests with
    marketing consent (marketing_text_sql), narrowed to `segment`, and with
    exclude_recent also without a campaign text inside the frequency window.
    Everything that sends or counts a text audience — the send, the Studio's
    counts, the win-back floor, the Opportunity Feed — reads this or its
    SQL (marketing_text_sql + _segment_where), so a card, a count and a send
    can never disagree about who is in it. Filtered in SQL (CS-19 / #94):
    the whole list was loaded and filtered in Python once per segment."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    where, args = _segment_where(segment, now)
    sql = (f"SELECT {_CONTACT_COLS} FROM guest_contacts WHERE restaurant_id=? AND "
           f"{marketing_text_sql()} AND {where}")
    params = [restaurant_id] + args
    if exclude_recent:
        w2, a2 = _not_recent_where(now)
        sql += f" AND {w2}"
        params += a2
    conn = get_conn(db_path)
    try:
        rows = conn.execute(sql + " ORDER BY id DESC", params).fetchall()
    finally:
        conn.close()
    return [_contact_dict(r) for r in rows]


def marketing_audience_count(restaurant_id, segment="all", exclude_recent=False, db_path=DB_PATH) -> int:
    """len(marketing_audience(...)), counted in SQL."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    where, args = _segment_where(segment, now)
    sql = f"SELECT COUNT(*) FROM guest_contacts WHERE restaurant_id=? AND {marketing_text_sql()} AND {where}"
    params = [restaurant_id] + args
    if exclude_recent:
        w2, a2 = _not_recent_where(now)
        sql += f" AND {w2}"
        params += a2
    conn = get_conn(db_path)
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    finally:
        conn.close()


def segment_contacts(restaurant_id, segment="all", db_path=DB_PATH):
    """Consented, non-unsubscribed contacts matching `segment` — the
    marketing audience (marketing_audience).

    Consent is applied first and unconditionally — a segment can only ever
    narrow the eligible set, never widen it.
    """
    return marketing_audience(restaurant_id, segment, db_path=db_path)


def filter_segment(restaurant_id, contacts, segment="all"):
    """`contacts` narrowed to `segment`. Consent is the caller's: texts pass
    SMS-consented guests, the newsletter its email subscribers, and this
    only ever narrows what it was given. _segment_where is its SQL twin."""
    contacts = list(contacts)
    segment = (segment or "all").strip().lower()
    if segment not in SEGMENTS:
        # A misspelt or retired audience reached EVERYONE consented (CS-17):
        # an unknown segment narrows to nobody. The send routes refuse it
        # outright (known_segment) before anything is queued.
        return []
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


def known_segment(segment):
    """The SEGMENTS key `segment` names (blank is "all"), or None when it
    names none — which a send refuses rather than widening to everyone."""
    key = (segment or "all").strip().lower() if isinstance(segment, str) or segment is None else ""
    return key if key in SEGMENTS else None


def segment_counts(restaurant_id, db_path=DB_PATH, exclude_recent=False) -> dict:
    """How many guests each segment would reach right now, so the owner picks
    an audience seeing its size rather than after sending to it. One
    aggregate query over the marketing audience (was: the whole list loaded
    once per segment, CS-19 / ARC-6)."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    cols, params = [], []
    for key in SEGMENTS:
        where, args = _segment_where(key, now)
        cols.append(f"COALESCE(SUM(CASE WHEN {where} THEN 1 ELSE 0 END),0)")
        params += args
    sql = f"SELECT {', '.join(cols)} FROM guest_contacts WHERE restaurant_id=? AND {marketing_text_sql()}"
    params.append(restaurant_id)
    if exclude_recent:
        w2, a2 = _not_recent_where(now)
        sql += f" AND {w2}"
        params += a2
    conn = get_conn(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return {key: int(row[i] or 0) for i, key in enumerate(SEGMENTS)}


def eligible_counts(restaurant_id, db_path=DB_PATH) -> dict:
    """How many guests each segment's TEXT would actually go to now: the
    segment, less anyone texted inside GUEST_SMS_MIN_DAYS_BETWEEN and anyone
    already waiting on a campaign that is still sending. The Studio's
    "Text N" button read segment_counts, which ignored the spacing, so it
    promised guests the send then skipped (CS-5, MB-11)."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    queued = _queued_contact_ids(restaurant_id, db_path=db_path)
    textable = [c for c in segment_contacts(restaurant_id, "all", db_path=db_path)
                if not _too_soon(c, now) and c["id"] not in queued]
    return {key: len(filter_segment(restaurant_id, textable, key)) for key in SEGMENTS}


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


def _validate_sms(text, restaurant, offer_source, never_say):
    """The guest text after the engine (surface guest_sms), or ValueError
    "campaign copy rejected: …" when it refuses."""
    import response_validation as rv
    rid = getattr(restaurant, "id", None)
    tenants = set()
    if rid:
        try:
            import models as _m
            tenants = _m.other_tenant_names(rid)
        except Exception:
            tenants = set()
    names = {n for n in (getattr(restaurant, "name", None), getattr(restaurant, "sign_off_name", None)) if n}
    out = rv.enforce(text, rv.ValidationContext(
        restaurant_id=rid, surface="guest_sms", names_allowed=names, tenant_names_denied=tenants,
        never_say=never_say, offer_source=offer_source, policy={"action": "guest_campaign_draft"}), marker=False)
    v = out.verdict
    if v is not None and v.verdict == "refuse":
        why = next((f for f in v.findings if f.get("severity") == "refuse"), None)
        detail = (why or {}).get("detail") or "it makes a claim a guest text must not"
        span = (why or {}).get("span") or ""
        if detail.startswith("the draft "):
            detail = "the copy " + detail[len("the draft "):]
        elif span and span.lower() not in detail.lower():
            detail = f"{detail} ('{span}')"
        raise ValueError(f"campaign copy rejected: {detail}")
    return out


def draft_campaign_message(restaurant, campaign_type="general", topic="", goal=""):
    """AI-drafts a short SMS (under ~300 chars — a real SMS/MMS segment
    budget, not email) in the restaurant's own voice. Reuses marketing.py's
    profile lookup for brand voice instead of re-deriving it. `goal` is the
    owner's own words from the Campaigns page ("fill Thursday dinner"): what
    the text is for, not copy to include, and a source an offer may cite."""
    from marketing import get_profile_for_restaurant

    p = get_profile_for_restaurant(restaurant.id)
    intent = CAMPAIGN_PROMPTS.get(campaign_type, CAMPAIGN_PROMPTS["general"])
    # The owner's words share CAMPAIGN_MAX_CHARS with the "{Name}: " the send
    # puts in front and a tracked link the owner may add (CS-12, MB-10): a
    # draft of the whole 300 was refused at the send it was drafted for.
    budget = message_budget(getattr(restaurant, "name", None), with_link=True)
    never_clause = f" Never use these words or phrases: {p['never_say']}." if p.get("never_say") else ""
    # Same profile dict marketing.py's own generator uses menu_notes from —
    # this generator was silently dropping it, so a guest text campaign
    # could never reference an actual dish or special the way a social
    # post or review reply already can.
    menu_clause = f" Menu & current specials: {p['menu_notes']}. Reference something specific when it fits naturally." if p.get("menu_notes") else ""
    topic_clause = f" Topic/specifics to include: {topic}." if topic else ""
    goal_clause = f" What the owner wants this text to do, in their words: {goal}." if goal else ""

    prompt = (
        f"Write {intent} for {p['name']}, a {p['vibe']} in {p['neighborhood']}. "
        f"Brand voice: {p['voice']}.{never_clause}{menu_clause}{topic_clause}{goal_clause}\n\n"
        f"Rules: under {budget} characters total (this is a real text message, not an email). "
        # A guest text is refused on any stated cause ("because of you",
        # "thanks to our new chef"): the guard can't tell warmth from a claim.
        "Give no reason or cause for anything: no 'because', 'due to', 'thanks to' or 'since'. "
        # One em dash or curly quote sends the whole text as Unicode, 70
        # characters a part instead of 160: two or three texts per guest.
        "Plain keyboard punctuation only: no em dashes, curly quotes or ellipsis characters. "
        # The owner's "45 days" is an audience, not a fact about each guest.
        "It goes to many guests at once: never say how long it has been since a guest's visit. "
        # "Fill Tuesday dinner" is the owner's aim (the Opportunity Feed's
        # slow night), and drafts told guests "Tuesday nights are looking a
        # little quiet": a slow night said to the public, under their name.
        "The owner's goal is theirs, not something to tell guests: never say or hint that a night is slow, "
        "quiet or empty, or that the restaurant wants to fill tables. "
        "Nothing is new, back, better or changed unless the owner's words say so. "
        "No markdown, no emoji spam (at most one emoji). No links or phone numbers. "
        "End naturally — no 'reply STOP to unsubscribe' (that's added automatically). "
        "Never invent an offer: no discount, percentage or dollars off, free item, half price, "
        "buy-one-get-one or anything on the house, unless it is written in the topic or menu above, "
        "in those words. The restaurant has not agreed to one.\n"
        "Return ONLY the message text, nothing else."
    )
    client = get_client()
    import data_health
    message = create_with_retry(
        client,
        model=model_for("guest_marketing"),
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant.id,
        action="guest_campaign_draft",
        # Rests on no data source: guest SMS copy from the owner's offer.
        readiness=data_health.NOT_APPLICABLE,
    )
    text = extract_text(message).strip()
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ValueError("campaign copy was truncated")
    # This goes out as an SMS to real guests. A link, a phone number or an
    # offer the restaurant never agreed to is not something to send unread.
    # The comment above promised an offer the restaurant never agreed to is
    # not sent unread, and only the link/phone check ran: "enjoy a free
    # dessert with any entree" and "20% off all week" both passed (M-24).
    # invented_offers stays: it names half price, two-for-one, "$5 off" and
    # "discount", which the engine's comp rule does not.
    offer_source = (topic or "") + " " + (goal or "") + " " + (p.get("menu_notes") or "")
    offers = invented_offers(text, offer_source)
    if offers:
        raise ValueError("campaign copy rejected: it offers " + ", ".join(offers[:3])
                         + ", which nobody told Cavnar AI the restaurant is running")
    # The Response Validation Layer on guest_sms (workstream A) replaces the
    # bare check_public_reply: the same residue / link / phone check, now
    # WITH the never-say list the prompt carried and the check did not (NS6
    # A3 #6), plus the public claims — an award, a comp, a sourcing or
    # allergen claim the owner never wrote, a fault or an inspection claim,
    # another tenant's name, injection residue. Refused on the verdict, in
    # shadow mode too: this text reaches guests.
    text = _validate_sms(text, restaurant, offer_source, p.get("never_say") or "")
    # check_public_reply allows a 1,200-character review reply; a text
    # message has its own, much smaller budget.
    if len(text) > budget:
        raise ValueError(f"campaign copy rejected: {len(text)} characters, over the "
                         f"{budget} a text message leaves after your name and a link")
    return text


MAX_CAMPAIGN_CHARS = 1600       # Twilio's hard ceiling; CAMPAIGN_MAX_CHARS is the one that binds


# ── What a guest reads (CS-12, MB-15, audit #7) ────────────────────────────
# Every restaurant shares one platform number, so a guest's phone shows a
# number, never the restaurant — and the text itself did not say whose it
# was. Each campaign and win-back text now opens with the restaurant's name,
# and that name and a tracked link count against CAMPAIGN_MAX_CHARS like the
# owner's own words: the 300 used to be checked on the words alone, then the
# name, link and STOP line were added after.
STOP_LINE = "\n\nReply STOP to unsubscribe."
SMS_NAME_MAX = 40
# marketing_links.create_link mints secrets.token_urlsafe(7): 10 characters.
_LINK_TOKEN_CHARS = 10
_PLAIN = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def sms_prefix(restaurant_name) -> str:
    """"Simple EJ's: " — the restaurant's name as the text's first words, in
    plain keyboard punctuation (a curly apostrophe in the name sent every
    text as Unicode, 70 characters a part)."""
    name = " ".join(str(restaurant_name or "").split()).translate(_PLAIN)[:SMS_NAME_MAX].strip()
    return f"{name}: " if name else ""


def link_chars() -> int:
    """What a tracked link adds: a newline and the short /g/ address."""
    return 1 + len(_short_link("x" * _LINK_TOKEN_CHARS))


def campaign_text(restaurant_name, message, link_token=None) -> str:
    """The text above the STOP line: "{Name}: " + the owner's words + the
    tracked link. A message that already opens with the name keeps it once."""
    msg = (message or "").strip()
    prefix = sms_prefix(restaurant_name)
    if prefix and msg.translate(_PLAIN).lower().startswith(prefix[:-2].lower()):
        prefix = ""
    body = prefix + msg
    if link_token:
        body += "\n" + _short_link(link_token)
    return body


def message_budget(restaurant_name, with_link=False) -> int:
    """Characters left for the owner's own words."""
    return CAMPAIGN_MAX_CHARS - len(sms_prefix(restaurant_name)) - (link_chars() if with_link else 0)


def _restaurant_name(restaurant_id, db_path=DB_PATH) -> str:
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db_path)
        return (getattr(r, "name", "") or "") if r else ""
    except Exception:
        return ""


def check_campaign_text(restaurant_id, message, with_link=False, restaurant_name=None, db_path=DB_PATH):
    """None when the text fits CAMPAIGN_MAX_CHARS with the name and link
    counted; otherwise the refusal. Every send path asks this BEFORE anything
    is queued or a link is minted: start_campaign only checked 1,600 and
    answered "queued", then the send thread refused the same text at 300 and
    nobody was told (MB-10, audit #11)."""
    msg = (message or "").strip()
    if not msg:
        return {"ok": False, "blocked": "empty", "sent": 0, "failed": 0, "total": 0,
                "error": "The message is empty."}
    name = restaurant_name if restaurant_name is not None else _restaurant_name(restaurant_id, db_path)
    text = campaign_text(name, msg, ("x" * _LINK_TOKEN_CHARS) if with_link else None)
    if len(text) <= CAMPAIGN_MAX_CHARS:
        return None
    what = "your restaurant's name" + (" and the link" if with_link else "")
    return {"ok": False, "blocked": "too_long", "sent": 0, "failed": 0, "total": 0,
            "error": (f"That text is {len(text)} characters with {what}; a guest text can carry "
                      f"{CAMPAIGN_MAX_CHARS}. Shorten it by {len(text) - CAMPAIGN_MAX_CHARS} and send again.")}


def audience_size(restaurant_id, segment="all", db_path=DB_PATH) -> int:
    """How many guests a campaign to this segment would text right now: the
    marketing audience less anyone inside the frequency window, counted in
    SQL (marketing_audience_count), and nobody already waiting on a sending
    campaign (the durable queue)."""
    queued = _queued_contact_ids(restaurant_id, db_path=db_path)
    if not queued:
        return marketing_audience_count(restaurant_id, segment, exclude_recent=True, db_path=db_path)
    return sum(1 for c in marketing_audience(restaurant_id, segment, exclude_recent=True, db_path=db_path)
               if c["id"] not in queued)


# ── The campaign queue (MB-10, audit #10) ──────────────────────────────────
# A campaign is a guest_campaigns row with a status and one guest_campaign_
# queue row per recipient, written before anything is texted. Each recipient
# is claimed (pending -> sending) and then claimed on the contact too (the
# three-day stamp, conditional, with consent re-read) before their text goes
# out, so two drains of one campaign — the owner's thread and the scheduler,
# a double press, a retry after a deploy — never text anyone twice. The
# guest_newsletter_recipients pattern, for texts.
#
#   status  sending   texts are going out
#           waiting   the 8am-9pm window is closed: pending texts wait for
#                     8:00 AM and then go (run_campaign_sends)
#           done      nothing pending; the outcome tracker runs once, and
#                     only if something was sent (MB-11)
#           cancelled the owner stopped it; pending rows never go
#
# A pending text older than GUEST_CAMPAIGN_MAX_WAIT_HOURS expires rather
# than reaching a guest a day late. No row at all is written when nobody is
# eligible: a zero-text campaign used to be recorded, and credited.
GUEST_CAMPAIGN_TICK_SECONDS = 120
GUEST_CAMPAIGN_THREAD_SECONDS = 15 * 60
GUEST_CAMPAIGN_MAX_WAIT_HOURS = 24
# The same words to the same audience pressed again while the first is still
# going resumes it (a retry after a lost answer, a double press).
GUEST_CAMPAIGN_RESUME_HOURS = 24
# Submissions per second to Twilio while a campaign drains. A 5,000-text list
# handed over at once sat in Twilio's queue behind — and ahead of — every
# other restaurant's review requests on the shared number (MB-13).
GUEST_SMS_PER_SECOND_DEFAULT = 2.0
_OPEN = ("sending", "waiting")
_WEEKDAY_TITLES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _queued_contact_ids(restaurant_id, db_path=DB_PATH) -> set:
    """Contacts waiting on (or being texted by) a campaign still sending."""
    conn = get_conn(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT contact_id FROM guest_campaign_queue WHERE restaurant_id=? AND status IN ('pending','sending')",
            (restaurant_id,)).fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def _submit_interval() -> float:
    """Seconds between two submissions to Twilio: GUEST_SMS_PER_SECOND (env)
    or the default. Nothing to pace when no text reaches a carrier (Twilio
    unconfigured: local runs and tests)."""
    import notify as _n
    if not (_n.TWILIO_SID and _n.TWILIO_TOKEN):
        return 0.0
    try:
        rate = float(os.getenv("GUEST_SMS_PER_SECOND") or GUEST_SMS_PER_SECOND_DEFAULT)
    except ValueError:
        rate = GUEST_SMS_PER_SECOND_DEFAULT
    return 1.0 / rate if rate > 0 else 0.0


def _content_hash(segment, message) -> str:
    import hashlib
    return hashlib.sha256(f"{segment}\n{(message or '').strip()}".encode("utf-8")).hexdigest()


def _enqueue(restaurant_id, message, segment, link_token, *, target_day=None, user_id=None, draft_id=None, rec_key=None,
             db_path=DB_PATH):
    """-> (campaign_id or None, info). One writer at a time from the read of
    who is already queued to the commit, so two presses find one campaign."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    msg = message.strip()
    body = campaign_text(_restaurant_name(restaurant_id, db_path), msg, link_token) + STOP_LINE
    digest = _content_hash(segment, msg)
    day = str(target_day or "").strip().capitalize()
    day = day if day in _WEEKDAY_TITLES else None
    audience = segment_contacts(restaurant_id, segment, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        same = conn.execute(
            "SELECT id FROM guest_campaigns WHERE restaurant_id=? AND content_hash=? AND status IN ('sending','waiting') "
            "AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (restaurant_id, digest, f"-{GUEST_CAMPAIGN_RESUME_HOURS} hours")).fetchone()
        if same:
            conn.commit()
            return same["id"], {"resumed": True}
        queued = {r[0] for r in conn.execute(
            "SELECT contact_id FROM guest_campaign_queue WHERE restaurant_id=? AND status IN ('pending','sending')",
            (restaurant_id,)).fetchall()}
        recent = [c for c in audience if _too_soon(c, now)]
        eligible = [c for c in audience if not _too_soon(c, now) and c["id"] not in queued]
        info = {"resumed": False, "audience": len(audience), "skipped_recent": len(recent),
                "already_queued": len(audience) - len(recent) - len(eligible)}
        if not eligible:
            conn.rollback()
            return None, info
        status = "sending" if guest_sms_allowed_now(restaurant_id) else "waiting"
        cid = conn.execute(
            "INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count, segment, segment_label, "
            "link_token, status, total, deferred_count, skipped_count, body, content_hash, target_day, created_by, "
            "winback_draft_id, rec_key) VALUES (?,?,0,0,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, msg, segment, SEGMENTS[segment]["label"], link_token, status, len(eligible),
             len(eligible) if status == "waiting" else 0, len(audience) - len(eligible), body, digest, day,
             user_id, draft_id, (str(rec_key).strip()[:120] or None) if rec_key else None)).lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO guest_campaign_queue (campaign_id, restaurant_id, contact_id, phone) VALUES (?,?,?,?)",
            [(cid, restaurant_id, c["id"], _normalize_phone(c.get("phone") or "")) for c in eligible])
        conn.commit()
        return cid, info
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _campaign_row(campaign_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT * FROM guest_campaigns WHERE id=?", (campaign_id,)).fetchone()
    finally:
        conn.close()


def _queue_counts(conn, campaign_id) -> dict:
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM guest_campaign_queue WHERE campaign_id=? GROUP BY status",
        (campaign_id,)).fetchall()}


def _set_waiting(campaign_id, db_path=DB_PATH):
    """The window closed: what is pending waits for 8:00 AM."""
    conn = get_conn(db_path)
    try:
        pending = _queue_counts(conn, campaign_id).get("pending", 0)
        conn.execute("UPDATE guest_campaigns SET status='waiting', deferred_count=? WHERE id=? AND status IN "
                     "('sending','waiting')", (pending, campaign_id))
        conn.commit()
    finally:
        conn.close()


def _finish_row(queue_id, campaign_id, status, error=None, db_path=DB_PATH, contact=None, restaurant_id=None):
    """One recipient's outcome, and the campaign's count with it."""
    col = {"sent": "sent_count", "failed": "failed_count", "skipped": "skipped_count"}.get(status)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE guest_campaign_queue SET status=?, error=?, "
                     "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END WHERE id=?",
                     (status, error, status, queue_id))
        if col:
            conn.execute(f"UPDATE guest_campaigns SET {col}=COALESCE({col},0)+1 WHERE id=?", (campaign_id,))
        if status == "sent" and contact:
            try:
                # Who the campaign reached, for Toast attribution (moat #21).
                conn.execute("INSERT INTO guest_campaign_recipients (campaign_id, restaurant_id, contact_id, phone) "
                             "VALUES (?,?,?,?)", (campaign_id, restaurant_id, contact[0], contact[1]))
            except Exception as e:
                import ops
                ops.capture(e, job="campaign_recipients", context=f"restaurant_id={restaurant_id}")
        conn.commit()
    finally:
        conn.close()


def _claim_guest(restaurant_id, contact_id, db_path=DB_PATH):
    """The per-guest claim: stamp last_campaign_at only if the guest is still
    textable (consent re-read, not unsubscribed, no STOP on record — a STOP
    that arrives mid-campaign stops that campaign, SMS-19) and was not texted
    inside the frequency window. -> (claimed, prior stamp, phone, reason)."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    cutoff = (now - timedelta(days=GUEST_SMS_MIN_DAYS_BETWEEN)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT phone, consent, unsubscribed, last_campaign_at FROM guest_contacts "
                           "WHERE id=? AND restaurant_id=?", (contact_id, restaurant_id)).fetchone()
        if not row:
            return False, None, None, "no longer on the list"
        if not row["consent"] or row["unsubscribed"] or _opted_out_here(conn, restaurant_id, row["phone"]):
            return False, None, row["phone"], "unsubscribed"
        got = conn.execute(
            "UPDATE guest_contacts SET last_campaign_at=? WHERE id=? AND restaurant_id=? AND consent=1 "
            "AND unsubscribed=0 AND (last_campaign_at IS NULL OR last_campaign_at < ?)",
            (stamp, contact_id, restaurant_id, cutoff)).rowcount
        conn.commit()
        if not got:
            return False, None, row["phone"], "texted in the last few days"
        return True, (row["last_campaign_at"], stamp), row["phone"], None
    finally:
        conn.close()


def _release_guest(contact_id, prior, db_path=DB_PATH):
    before, stamp = prior
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE guest_contacts SET last_campaign_at=? WHERE id=? AND last_campaign_at=?",
                     (before, contact_id, stamp))
        conn.commit()
    finally:
        conn.close()


def _drain(campaign_id, max_seconds=None, limit=None, db_path=DB_PATH) -> dict:
    """Send what is pending on one campaign until it is done, the window
    closes, the owner cancels, or the bound is reached. Never raises for one
    bad number; a kill mid-text gives that guest back and propagates."""
    import time as _time
    camp = _campaign_row(campaign_id, db_path)
    if not camp or camp["status"] not in _OPEN:
        return {"sent": 0, "failed": 0}
    rid, body = camp["restaurant_id"], camp["body"] or (camp["message"] + STOP_LINE)
    started, last_submit = _time.monotonic(), 0.0
    interval = _submit_interval()
    sent = failed = done = 0
    while limit is None or done < limit:
        if max_seconds is not None and _time.monotonic() - started > max_seconds:
            break
        # The 8am-9pm window is checked before EVERY text, and what is left
        # when it closes now waits for 8:00 AM instead of being dropped.
        if not guest_sms_allowed_now(rid):
            _set_waiting(campaign_id, db_path)
            break
        # Twilio drops the text rather than deliver it after 9:00 PM. A
        # minute when the two clock reads straddle the close.
        validity = guest_sms_validity_seconds(rid) or 60
        conn = get_conn(db_path)
        try:
            st = conn.execute("SELECT status FROM guest_campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not st or st["status"] not in _OPEN:
                break                                   # cancelled, or finished by another drain
            row = conn.execute("SELECT id, contact_id FROM guest_campaign_queue WHERE campaign_id=? "
                               "AND status='pending' ORDER BY id LIMIT 1", (campaign_id,)).fetchone()
            if row is None:
                break
            claimed = conn.execute("UPDATE guest_campaign_queue SET status='sending', claimed_at=datetime('now') "
                                   "WHERE id=? AND status='pending'", (row["id"],)).rowcount == 1
            if claimed and st["status"] == "waiting":
                conn.execute("UPDATE guest_campaigns SET status='sending', deferred_count=0 "
                             "WHERE id=? AND status='waiting'", (campaign_id,))
            conn.commit()
        finally:
            conn.close()
        if not claimed:
            continue
        done += 1
        ok_claim, prior, phone, reason = _claim_guest(rid, row["contact_id"], db_path)
        if not ok_claim:
            _finish_row(row["id"], campaign_id, "skipped", reason, db_path)
            continue
        if interval:
            wait = interval - (_time.monotonic() - last_submit)
            if wait > 0:
                _time.sleep(wait)
        last_submit = _time.monotonic()
        err = None
        try:
            ok = bool(send_sms(phone, body, use_case="guest", validity_seconds=validity))
        except Exception as e:
            ok, err = False, str(e)[:300]
        except BaseException:
            # Killed before this text is known to have gone: the guest goes
            # back to pending, so a resume reaches them; the kill propagates.
            _release_guest(row["contact_id"], prior, db_path)
            conn = get_conn(db_path)
            try:
                conn.execute("UPDATE guest_campaign_queue SET status='pending', claimed_at=NULL "
                             "WHERE id=? AND status='sending'", (row["id"],))
                conn.commit()
            finally:
                conn.close()
            raise
        if ok:
            sent += 1
            _finish_row(row["id"], campaign_id, "sent", None, db_path, contact=(row["contact_id"], _normalize_phone(phone)),
                        restaurant_id=rid)
        else:
            # A failure never locks the guest out of the next campaign.
            failed += 1
            _release_guest(row["contact_id"], prior, db_path)
            _finish_row(row["id"], campaign_id, "failed", err or "not accepted by the carrier", db_path)
    _maybe_finish(campaign_id, db_path)
    return {"sent": sent, "failed": failed}


def _maybe_finish(campaign_id, db_path=DB_PATH) -> bool:
    """Close a campaign with nothing left pending or in flight, once; the
    close runs its follow-ups (_on_campaign_done)."""
    conn = get_conn(db_path)
    try:
        counts = _queue_counts(conn, campaign_id)
        if counts.get("pending", 0) or counts.get("sending", 0):
            return False
        closed = conn.execute("UPDATE guest_campaigns SET status='done', completed_at=datetime('now'), "
                              "deferred_count=0 WHERE id=? AND status IN ('sending','waiting')",
                              (campaign_id,)).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if closed:
        _on_campaign_done(campaign_id, db_path)
    return closed


def _on_campaign_done(campaign_id, db_path=DB_PATH):
    """What a finished campaign starts — only when a text actually went
    (MB-11, audit #61): the slow-day tracker for a fill-a-night campaign,
    and a win-back recommendation marked implemented. Durable: it runs from
    whichever drain finishes the campaign, the owner's thread or the
    scheduler after a deploy. Never raises."""
    camp = _campaign_row(campaign_id, db_path)
    if not camp or not int(camp["sent_count"] or 0):
        return
    rid, sent = camp["restaurant_id"], int(camp["sent_count"])
    rec_key = camp["rec_key"] if "rec_key" in camp.keys() else None
    # The feed card it began on (OPP-10) and a fill-a-night tracker, both
    # only now that texts actually went.
    track_campaign_outcome(rid, camp["target_day"], {"ok": True, "sent": sent}, camp["created_by"],
                           rec_key=rec_key)
    if camp["winback_draft_id"]:
        try:
            conn = get_conn(db_path)
            try:
                d = conn.execute("SELECT rec_key FROM guest_campaign_drafts WHERE id=? AND restaurant_id=?",
                                 (camp["winback_draft_id"], rid)).fetchone()
            finally:
                conn.close()
            if d and d["rec_key"]:
                import rec_ledger
                # The texts went out: the win-back was implemented (ROI #27).
                rec_ledger.implemented(rid, d["rec_key"], "marketing", user_id=camp["created_by"],
                                       source_ref=f"winback:{camp['winback_draft_id']}",
                                       meta={"module": "marketing", "sent": sent}, db_path=db_path)
        except Exception as e:
            print(f"[winback] implementation not recorded for {rid}: {e}")


def track_campaign_outcome(restaurant_id, target_day, result, user_id=None, rec_key=None):
    """A campaign aimed at a slow weekday starts an outcome tracker on that
    weekday's sales — only once texts actually went out: `result` must be ok
    AND carry sent > 0. Recording on the confirmation instead would track a
    campaign that quiet hours, an empty segment or a failed list refused;
    ok with sent 0 used to start one, crediting a lift to texts nobody got
    (MB-11). Best-effort: tracking never fails the send. The one body behind
    client_api._track_campaign_outcome."""
    day = str(target_day or "").strip().capitalize()
    res = result or {}
    if not res.get("ok") or not int(res.get("sent") or 0):
        return None
    if rec_key:
        # Began on an Opportunity Feed card: that card was acted on, whatever
        # it was about (re-audit OPP-10) — recorded once texts actually went.
        try:
            import marketing_opportunities
            marketing_opportunities.implemented_by_send(restaurant_id, rec_key, "text", user_id=user_id,
                                                        sent=int(res.get("sent")))
        except Exception as e:
            import ops
            ops.capture(e, job="campaign_card_implemented", context=f"restaurant_id={restaurant_id}")
    if day not in _WEEKDAY_TITLES:
        return None
    # The texts went out: "text your list before a slow <day>" was
    # implemented (ROI #27) — recorded only if it was ever shown, and once:
    # not again when the feed card that named it was just recorded (OPP-10).
    try:
        import rec_ledger
        from datetime import date as _d2
        key = rec_ledger.rec_key("slow_day", day)
        if (rec_key or "") != key:
            rec_ledger.implemented(restaurant_id, key, "marketing", user_id=user_id,
                                   source_ref=f"campaign:{day}:{_d2.today().isoformat()}",
                                   meta={"module": "marketing", "sent": res.get("sent")})
    except Exception as e:
        import ops
        ops.capture(e, job="campaign_implemented", context=f"restaurant_id={restaurant_id}")
    try:
        import outcomes
        # Credited to Marketing (the campaign is marketing's recommendation,
        # rec-ROI #5), and refused while that weekday is already measured
        # (#3) — a second campaign on the same Tuesdays would read the same
        # lift twice. A refusal is an answer, not a failure. The
        # restaurant's own date in the key, not the server's (re-audit A8).
        return outcomes.start(restaurant_id, "slow_day_campaign",
                              f"campaign:{day}:{outcomes.local_today(restaurant_id).isoformat()}",
                              f"Guest text to lift {day}s", f"weekday_sales:{day}", user_id=user_id,
                              module="marketing", gate="metric")
    except Exception as e:
        import ops
        ops.capture(e, job="campaign_outcome", context=f"restaurant_id={restaurant_id}")
        return None


def _refusal(blocked, error, **extra):
    return dict({"ok": False, "blocked": blocked, "sent": 0, "failed": 0, "total": 0, "error": error}, **extra)


def _quiet_refusal():
    return _refusal("quiet_hours", "Guest texts only go out between "
                                   f"{guest_sms_window_label()} in your local time. "
                                   "Your message is ready — send it in the morning.")


def _nobody(segment, info):
    """No eligible guest: no campaign row, nothing tracked (MB-11)."""
    recent, queued = info.get("skipped_recent", 0), info.get("already_queued", 0)
    if not info.get("audience"):
        why = "Nobody in that audience has joined by text yet."
    else:
        bits = []
        if recent:
            bits.append(f"{recent} texted in the last {GUEST_SMS_MIN_DAYS_BETWEEN} days")
        if queued:
            bits.append(f"{queued} already waiting on a campaign that is still sending")
        why = "Nobody in that audience can be texted right now" + (f" ({' and '.join(bits)})." if bits else ".")
    return _refusal("no_audience", why, skipped_recent=recent + queued, segment=segment,
                    segment_label=SEGMENTS[segment]["label"])


def prepare_campaign(restaurant_id, message, segment, with_link=False, db_path=DB_PATH):
    """The checks every send path runs before anything is queued: a known
    audience (CS-17: an unknown one is refused, never "all") and the length
    with the name and link counted (MB-10)."""
    seg = known_segment(segment)
    if seg is None:
        return None, _refusal("unknown_segment", "That audience isn't one Cavnar AI knows. Pick one of the audiences shown.")
    too = check_campaign_text(restaurant_id, message, with_link=with_link, db_path=db_path)
    return seg, too


def campaign_status(campaign_id, db_path=DB_PATH) -> dict:
    """A campaign's own counts, from its rows."""
    camp = _campaign_row(campaign_id, db_path)
    if not camp:
        return {}
    conn = get_conn(db_path)
    try:
        q = _queue_counts(conn, campaign_id)
    finally:
        conn.close()
    pending = q.get("pending", 0) + q.get("sending", 0)
    return {"campaign_id": campaign_id, "status": camp["status"], "total": int(camp["total"] or 0),
            "sent": int(camp["sent_count"] or 0), "failed": int(camp["failed_count"] or 0),
            "skipped_recent": int(camp["skipped_count"] or 0), "pending": pending,
            "deferred_quiet_hours": pending if camp["status"] == "waiting" else 0,
            "segment": camp["segment"], "segment_label": camp["segment_label"]}


def start_campaign(restaurant_id, message, segment="all", link_token=None, *, target_day=None, user_id=None,
                   draft_id=None, hold=False, rec_key=None, db_path=DB_PATH) -> dict:
    """Validate, queue every recipient, answer at once; the texts go from a
    background drain and — whatever is left after a deploy, or waiting on
    the 8am window — from the scheduler (run_campaign_sends). The fan-out
    used to run inside the HTTP request, then on a daemon thread holding the
    list in memory (MOD-MKT-7, MB-10).

    Outside 8am-9pm the send is refused, unless `hold` — the owner saw
    "Texts wait until 8:00 AM" and sent anyway: then it is queued as
    `waiting` and genuinely goes at 8:00 AM (CS-6). `target_day` (a
    fill-a-night goal) and `draft_id` (a win-back draft) are what the
    finished campaign starts, once texts went (_on_campaign_done)."""
    seg, refused = prepare_campaign(restaurant_id, message, segment, bool(link_token), db_path)
    if refused:
        return refused
    in_window = guest_sms_allowed_now(restaurant_id)
    if not in_window and not hold:
        return _quiet_refusal()
    cid, info = _enqueue(restaurant_id, message, seg, link_token, target_day=target_day, user_id=user_id,
                         draft_id=draft_id, rec_key=rec_key, db_path=db_path)
    if cid is None:
        return _nobody(seg, info)
    st = campaign_status(cid, db_path)
    if in_window:
        import threading

        def _run():
            try:
                _drain(cid, max_seconds=GUEST_CAMPAIGN_THREAD_SECONDS, db_path=db_path)
            except Exception as e:
                import ops
                ops.capture(e, job="guest_campaign_send", context=f"restaurant_id={restaurant_id}")
        threading.Thread(target=_run, name=f"guest-campaign-{cid}", daemon=True).start()
    waiting = st.get("status") == "waiting"
    return {"ok": True, "queued": True, "campaign_id": cid, "total": st.get("total", 0),
            "resumed": bool(info.get("resumed")), "skipped_recent": st.get("skipped_recent", 0),
            "waiting": waiting, "waiting_until": _hour_label(GUEST_SMS_EARLIEST_HOUR) if waiting else None,
            "segment": seg, "segment_label": SEGMENTS[seg]["label"]}


def send_campaign(restaurant_id, message, db_path=DB_PATH, segment="all", link_token=None):
    """Queue and send `message` to one SEGMENT now, on this thread — the
    synchronous form of start_campaign, for jobs and tests. Refused outside
    8am-9pm; texts left when the window closes mid-send wait for 8:00 AM.

    Returns {"ok": True, "sent", "failed", "total", "skipped_recent",
    "deferred_quiet_hours", "campaign_id", ...}, or {"ok": False, "blocked",
    "error"}: too long, an unknown audience, quiet hours, or nobody eligible
    (no campaign row is written then). Never raises for one bad number.

    Every gate is server-side so every caller inherits it:
      1. length        — the name, words and link within CAMPAIGN_MAX_CHARS
      2. quiet hours   — nothing goes out between 9pm and 8am local
      3. consent       — only guests who opted in themselves, re-read per text
      4. frequency     — nobody gets two campaigns inside three days
    """
    seg, refused = prepare_campaign(restaurant_id, message, segment, bool(link_token), db_path)
    if refused:
        return refused
    if not guest_sms_allowed_now(restaurant_id):
        return _quiet_refusal()
    cid, info = _enqueue(restaurant_id, message, seg, link_token, db_path=db_path)
    if cid is None:
        return _nobody(seg, info)
    _drain(cid, db_path=db_path)
    return dict(campaign_status(cid, db_path), ok=True, resumed=bool(info.get("resumed")))


def cancel_campaign(restaurant_id, campaign_id, db_path=DB_PATH) -> dict:
    """Stop a campaign that is still sending or waiting for 8:00 AM: its
    pending texts never go. A text already handed to Twilio is not recalled."""
    conn = get_conn(db_path)
    try:
        camp = conn.execute("SELECT status FROM guest_campaigns WHERE id=? AND restaurant_id=?",
                            (campaign_id, restaurant_id)).fetchone()
        if not camp:
            return {"ok": False, "error": "That campaign is gone."}
        if camp["status"] not in _OPEN:
            return {"ok": False, "error": "That campaign already finished."}
        n = conn.execute("UPDATE guest_campaign_queue SET status='cancelled' WHERE campaign_id=? AND status='pending'",
                         (campaign_id,)).rowcount
        conn.execute("UPDATE guest_campaigns SET status='cancelled', completed_at=datetime('now'), deferred_count=0 "
                     "WHERE id=? AND restaurant_id=?", (campaign_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()
    return dict(campaign_status(campaign_id, db_path), ok=True, cancelled=n)


def run_campaign_sends(db_path=DB_PATH, max_seconds=None) -> dict:
    """Scheduler tick: resume every campaign still sending or waiting for
    8:00 AM, bounded in wall-clock time; the queue rows are the cursor.

    A recipient left 'sending' for 30 minutes belonged to a process that died
    mid-text: it may have gone, so it is marked failed rather than texted
    twice. A text pending longer than GUEST_CAMPAIGN_MAX_WAIT_HOURS expires.
    A restaurant that is no longer in service texts nobody."""
    import time as _time
    from models import in_service_sql
    if max_seconds is None:
        max_seconds = GUEST_CAMPAIGN_TICK_SECONDS
    conn = get_conn(db_path)
    try:
        stale = conn.execute("SELECT campaign_id, COUNT(*) AS n FROM guest_campaign_queue WHERE status='sending' "
                             "AND claimed_at < datetime('now','-30 minutes') GROUP BY campaign_id").fetchall()
        conn.execute("UPDATE guest_campaign_queue SET status='failed', error='interrupted while sending' "
                     "WHERE status='sending' AND claimed_at < datetime('now','-30 minutes')")
        for r in stale:
            conn.execute("UPDATE guest_campaigns SET failed_count=COALESCE(failed_count,0)+? WHERE id=?",
                         (r["n"], r["campaign_id"]))
        conn.execute("UPDATE guest_campaign_queue SET status='expired', error='waited too long to send' "
                     "WHERE status='pending' AND campaign_id IN (SELECT id FROM guest_campaigns WHERE status IN "
                     "('sending','waiting') AND created_at < datetime('now', ?))",
                     (f"-{GUEST_CAMPAIGN_MAX_WAIT_HOURS} hours",))
        conn.commit()
        open_rows = conn.execute(
            "SELECT c.id, c.restaurant_id, (CASE WHEN " + in_service_sql("r.billing_status")
            + " THEN 1 ELSE 0 END) AS live FROM guest_campaigns c JOIN restaurants r ON r.id = c.restaurant_id "
            "WHERE c.status IN ('sending','waiting') ORDER BY c.id").fetchall()
    finally:
        conn.close()
    started = _time.monotonic()
    totals = {"campaigns": 0, "sent": 0, "failed": 0}
    for row in open_rows:
        # Closed first: everything sent, failed, skipped or expired. An
        # account no longer in service texts nobody; its pending rows wait
        # out GUEST_CAMPAIGN_MAX_WAIT_HOURS and expire.
        if _maybe_finish(row["id"], db_path) or not row["live"]:
            continue
        left = max_seconds - (_time.monotonic() - started)
        if left <= 0:
            break
        if not guest_sms_allowed_now(row["restaurant_id"]):
            _set_waiting(row["id"], db_path)
            continue
        out = _drain(row["id"], max_seconds=left, db_path=db_path)
        totals["campaigns"] += 1
        totals["sent"] += out["sent"]
        totals["failed"] += out["failed"]
    return totals


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
            "       COALESCE(l.clicks, 0) AS clicks, COALESCE(c.status, 'done') AS status, c.total, "
            "       COALESCE(c.skipped_count, 0) AS skipped_count, c.completed_at, c.target_day, "
            "       (SELECT COUNT(*) FROM guest_campaign_queue q WHERE q.campaign_id = c.id "
            "          AND q.status IN ('pending','sending')) AS pending "
            "FROM guest_campaigns c "
            "LEFT JOIN marketing_links l ON l.token = c.link_token "
            "WHERE c.restaurant_id=? ORDER BY c.id DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        item = dict(r)
        # Whether "came back within 14 days" can be read off this campaign
        # yet (CS-8): only once attribution has read its whole window.
        item["window_closed"] = _window_closed(item)
        item["waiting_until"] = _hour_label(GUEST_SMS_EARLIEST_HOUR) if item["status"] == "waiting" else None
        out.append(item)
    return out


def _window_closed(c) -> bool:
    """True once Toast attribution has read a campaign's whole 14-day window
    (attribution_through on or past the send day + ATTRIBUTION_WINDOW_DAYS,
    the send day as run_campaign_attribution reads it). visits_matched is
    written after the FIRST day is read, so a rate over open windows counted
    two days of a campaign against fourteen of another and was labelled
    "within 14 days" (CS-8, audit #63)."""
    if c.get("visits_matched") is None or not c.get("attribution_through"):
        return False
    try:
        from datetime import date as _date
        sent_on = _date.fromisoformat(str(c.get("created_at") or "")[:10])
        return _date.fromisoformat(str(c["attribution_through"])[:10]) >= sent_on + timedelta(days=ATTRIBUTION_WINDOW_DAYS)
    except (TypeError, ValueError):
        return False


def diagnose(restaurant_id, db_path=DB_PATH) -> dict:
    """The campaigns' read, in the shared diagnosis shape. Deterministic:
    the best and worst measured campaign by taps per hundred sent and, once
    Toast attribution has run, by guests who came back. A campaign with no
    measurement is never scored against one that has."""
    hist = campaign_history(restaurant_id, limit=20, db_path=db_path)
    sent = [c for c in hist if (c.get("sent_count") or 0) >= 10]
    if len(sent) < 2:
        return {"available": False, "reason": "fewer than two campaigns of ten or more texts — nothing to compare yet"}
    # "Came back" only off campaigns whose whole window has been read (CS-8).
    measured = [c for c in sent if _window_closed(c)]
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
        taps_only = "partial" in (ev_in.get("flags") or ())
        srcs = () if taps_only else data_freshness.sources_for(["campaigns"])
        conf = rec_trust.assess(restaurant_id, "diag_campaign", evidence=ev_in, sources=srcs, db_path=db_path,
                                rests_on="link taps only" if taps_only else None)
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
    no discount: nobody has agreed to one. Plain punctuation: an em dash
    sent it as Unicode, three texts a guest where this is one."""
    # It opens with the name the send would put in front anyway (CS-12), so
    # the owner reads exactly what guests get and the name is there once.
    prefix = sms_prefix(restaurant_name) or "Hi! "
    msg = (f"{prefix}It's been a little while and we'd love to have you back. "
           f"Come see us this week. Your table's waiting.")
    return msg[:CAMPAIGN_MAX_CHARS]


def winback_return(restaurant_id, db_path=DB_PATH) -> dict:
    """What past win-back texts did, measured — or that nothing has been.
    A campaign to a lapsed segment with attribution run counts; its return
    is guests who came back within ATTRIBUTION_WINDOW_DAYS per 100 texted."""
    from time_utils import mdy as _mdy
    past = [c for c in campaign_history(restaurant_id, limit=50, db_path=db_path)
            if (c.get("segment") or "") in WINBACK_SEGMENTS and (c.get("sent_count") or 0) > 0]
    # Only campaigns whose 14-day window has closed: an open one's count is
    # the first few days of fourteen (CS-8).
    measured = [c for c in past if _window_closed(c)]
    if not measured:
        return {"measured": False, "campaigns": len(past),
                "text": ("No past win-back text has a measured return yet."
                         if not past else f"{len(past)} past win-back text{'s' if len(past) != 1 else ''}, "
                                          "none measured yet — a return counts once its 14 days have passed, matched against "
                                          "Toast check-ins.")}
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
    # The draft opens with the name, so the whole budget is the message's;
    # an edit that drops the name gets it back in front at the send, counted
    # (check_campaign_text refuses what then runs over).
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


def send_winback(restaurant_id, draft_id, message=None, user_id=None, hold=False, db_path=DB_PATH) -> dict:
    """The owner's send of a win-back draft — through start_campaign, so
    consent, quiet hours (`hold`: queue it for 8:00 AM), the frequency cap
    and the length with the restaurant's name all apply. The draft is
    answered only once the campaign is accepted; the recommendation is
    marked implemented only once texts went (_on_campaign_done)."""
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
    too_long = check_campaign_text(restaurant_id, text, db_path=db_path)
    if too_long:
        return too_long
    # The owner's own words (or the fixed win-back copy): the residue / link
    # / phone check, now WITH the restaurant's never-say list (NS6 A3 #6).
    # Not the engine's claim rules — an offer, an award or a "because" the
    # owner typed is theirs to send; a model rewrite of this text was
    # already validated where it was drafted (draft_campaign_message).
    from ai_guard import check_public_reply
    never_say = ""
    try:
        from models import get_restaurant as _gr
        _r = _gr(restaurant_id, db_path=db_path)
        never_say = (getattr(_r, "never_say", "") or "") if _r else ""
    except Exception:
        never_say = ""
    refusal = check_public_reply(text, never_say=never_say)
    if refusal:
        return {"ok": False, "error": f"Not sent: {refusal}."}
    # The texts going out marks the win-back implemented (ROI #27): the
    # campaign carries the draft, and its close does it (_on_campaign_done),
    # whichever drain finishes it — a thread callback died with a deploy.
    result = start_campaign(restaurant_id, text, segment=row["segment"], draft_id=draft_id, user_id=user_id,
                            hold=hold, db_path=db_path)
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
ATTRIBUTION_PASS_SECONDS = int(os.getenv("CAMPAIGN_ATTRIBUTION_SECONDS", str(10 * 60)))
ATTRIBUTION_CURSOR_KEY = "campaign_attribution_cursor"


def run_campaign_attribution(db_path=DB_PATH, today=None, max_seconds=None):
    """Daily: for every campaign sent in the last ATTRIBUTION_WINDOW_DAYS
    at a Toast-connected restaurant, count recipients Toast identified on
    a check on a later business day. Written to guest_campaigns as
    visits_matched, with attribution_through saying how far the window
    has been read. A visit is counted once per recipient per campaign.

    Partial by nature — Toast only has a customer on a check when one was
    captured — and said so wherever the number is shown. Each business
    date is fetched once per restaurant per run, whatever the number of
    campaigns, and only dates not yet read for that campaign.

    On the restaurant's own calendar (MB-19 / #57): a campaign's send day is
    the LOCAL date of its UTC created_at (a 7pm Central text is stored as
    the next UTC day, so the window started a day late and skipped the first
    evening), and the window reads through the restaurant's last CLOSED
    business day, never today's open one. `today` pins the local date (the
    last day read is the day before it).

    Bounded and resumable (MB-18 / #62): a resumable_sweep over restaurants
    with a cursor, and each campaign's attribution_through is its own
    cursor, so a pass cut off by the bound loses nothing."""
    from datetime import date as _date, timedelta as _td
    from models import get_restaurant
    from time_utils import local_iso, restaurant_now
    import pos as _pos
    import scheduler
    if max_seconds is None:
        max_seconds = ATTRIBUTION_PASS_SECONDS
    # A coarse UTC floor for the query; each campaign's window is then
    # decided on its own restaurant's calendar below.
    base = today or datetime.utcnow().date()
    floor = (base - _td(days=ATTRIBUTION_WINDOW_DAYS + 3)).isoformat()
    conn = get_conn(db_path)
    try:
        camps = conn.execute(
            "SELECT id, restaurant_id, created_at, attribution_through "
            "FROM guest_campaigns WHERE created_at >= ? AND sent_count > 0 "
            "ORDER BY restaurant_id, id", (floor,)).fetchall()
    finally:
        conn.close()
    by_rid = {}
    for c in camps:
        by_rid.setdefault(c["restaurant_id"], []).append(c)
    totals = {"checked": 0, "matched": 0}

    def _one(rid):
        r = get_restaurant(rid, db_path=db_path)
        # Any POS that shares guest records — Toast today; RPOWER once its
        # customer scope is granted — not a Toast field check.
        if not r or not _pos.supports(rid, "fetch_order_customers"):
            return
        if (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled", "paused"):
            return          # no POS calls on behalf of an account that asked for quiet
        tz = getattr(r, "timezone", None)
        local_today = today or restaurant_now(r, naive=True).date()
        last_closed = (today - _td(days=1)) if today else _last_closed_business_date(r)
        phones_by_day = {}          # iso date -> set of normalized phones
        for c in by_rid[rid]:
            try:
                sent_on = _date.fromisoformat(local_iso(c["created_at"], tz))
            except ValueError:
                continue
            if sent_on < local_today - _td(days=ATTRIBUTION_WINDOW_DAYS + 1):
                continue
            # From the day AFTER the send (M-22): the send day's orders include
            # the lunch before a 3pm text, which is not a guest coming back.
            start = (_date.fromisoformat(c["attribution_through"]) + _td(days=1) if c["attribution_through"]
                     else sent_on + _td(days=1))
            end = min(last_closed, sent_on + _td(days=ATTRIBUTION_WINDOW_DAYS))
            if start > end:
                continue
            conn = get_conn(db_path)
            try:
                recips = conn.execute("SELECT id, phone FROM guest_campaign_recipients WHERE campaign_id=? "
                                      "AND visited_on IS NULL", (c["id"],)).fetchall()
            finally:
                conn.close()
            if not recips:
                continue
            by_phone = {rr["phone"]: rr["id"] for rr in recips}
            newly = []
            day = start
            while day <= end:
                key = day.isoformat()
                if key not in phones_by_day:
                    try:
                        custs, _prov = _pos.fetch_order_customers(rid, day)
                        phones_by_day[key] = {_normalize_phone(x.get("phone")) for x in custs if x.get("phone")}
                    except Exception as e:
                        import ops
                        ops.capture(e, job="campaign_attribution", context=f"restaurant_id={rid} date={day}")
                        break
                for ph in phones_by_day[key] & set(by_phone):
                    if by_phone[ph] not in [n[0] for n in newly]:
                        newly.append((by_phone[ph], key))
                day += _td(days=1)
            through = day - _td(days=1)
            conn = get_conn(db_path)
            try:
                for rec_id, on in newly:
                    conn.execute("UPDATE guest_campaign_recipients SET visited_on=? WHERE id=? AND visited_on IS NULL",
                                 (on, rec_id))
                total = conn.execute("SELECT COUNT(*) FROM guest_campaign_recipients WHERE campaign_id=? "
                                     "AND visited_on IS NOT NULL", (c["id"],)).fetchone()[0]
                if through >= start:
                    conn.execute("UPDATE guest_campaigns SET visits_matched=?, attribution_through=? WHERE id=?",
                                 (int(total), through.isoformat(), c["id"]))
                conn.commit()
            finally:
                conn.close()
            totals["checked"] += 1
            totals["matched"] += len(newly)

    if by_rid:
        _done, hit_bound = scheduler.resumable_sweep(ATTRIBUTION_CURSOR_KEY, sorted(by_rid), _one, max_seconds,
                                                     job="campaign_attribution")
        if hit_bound:
            import ops
            ops.capture(RuntimeError(f"Campaign attribution stopped at its {max_seconds}s bound after {_done} "
                                     "restaurant(s); the next pass resumes from there"),
                        job="campaign_attribution", context="time_bound")
    return {"campaigns_checked": totals["checked"], "visits_matched": totals["matched"]}


def _last_closed_business_date(restaurant, now_local=None):
    """The restaurant's last CLOSED business day, on its own clock: the
    service date running now (time_utils.business_date — last night's until
    the business day turns over, a late close included) less one day. Never
    today's open service, whose orders are still coming in."""
    from time_utils import restaurant_now, business_date
    local = now_local or restaurant_now(restaurant, naive=True)
    return business_date(restaurant, local) - timedelta(days=1)


def consent_ledger(restaurant_id, db_path=DB_PATH) -> dict:
    """The compliance picture, in one call, counted in SQL.

      textable      marketing consent, not unsubscribed (marketing_text_sql)
      review_only   agreed to review links only (a YES to the invite) — never
                    texted a campaign
      pending       join-form requests still waiting for their Y
      unsubscribed  said STOP (or the owner unsubscribed them)
      no_consent    on the list with no consent of any kind

    "This month" is the restaurant's own calendar month. The evidence behind
    each number is in guest_consent_events (consent_events)."""
    from datetime import timezone as _tz
    from time_utils import restaurant_now_by_id
    local_now = restaurant_now_by_id(restaurant_id)
    try:
        month_start = (local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                       .astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        month_start = local_now.strftime("%Y-%m-01 00:00:00")
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            f"COALESCE(SUM(CASE WHEN {marketing_text_sql()} THEN 1 ELSE 0 END),0) AS textable, "
            "COALESCE(SUM(CASE WHEN COALESCE(unsubscribed,0)=1 THEN 1 ELSE 0 END),0) AS unsubscribed, "
            "COALESCE(SUM(CASE WHEN COALESCE(unsubscribed,0)=0 AND consent=0 AND COALESCE(review_consent,0)=1 "
            "  THEN 1 ELSE 0 END),0) AS review_only, "
            "COALESCE(SUM(CASE WHEN COALESCE(unsubscribed,0)=0 AND consent=0 AND COALESCE(review_consent,0)=0 "
            "  THEN 1 ELSE 0 END),0) AS no_consent "
            "FROM guest_contacts WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        pending = conn.execute(
            "SELECT COUNT(DISTINCT phone) FROM guest_optin_requests WHERE restaurant_id=? AND status='pending' "
            "AND requested_at >= datetime('now', ?)", (restaurant_id, f"-{JOIN_CONFIRM_HOURS} hours")).fetchone()[0]
        this_month = conn.execute(
            "SELECT COALESCE(SUM(sent_count),0) FROM guest_campaigns WHERE restaurant_id=? AND created_at >= ?",
            (restaurant_id, month_start)).fetchone()[0] or 0
        campaigns = conn.execute(
            "SELECT COUNT(*) FROM guest_campaigns WHERE restaurant_id=? AND created_at >= ?",
            (restaurant_id, month_start)).fetchone()[0] or 0
    finally:
        conn.close()
    return {
        "total": int(row["total"] or 0),
        "textable": int(row["textable"] or 0),
        "review_only": int(row["review_only"] or 0),
        "pending": int(pending or 0),
        "unsubscribed": int(row["unsubscribed"] or 0),
        "no_consent": int(row["no_consent"] or 0),
        "texts_this_month": int(this_month),
        "campaigns_this_month": int(campaigns),
        "window": guest_sms_window_label(),
        "min_days_between": GUEST_SMS_MIN_DAYS_BETWEEN,
    }


# ── Campaigns (owner, 9/28/26) ─────────────────────────────────────────────
# The page starts from what the owner wants to happen ("Bring back guests
# who haven't been in for 30 days"), not from a type, an audience and a
# topic field. plan_campaign reads that goal deterministically - the draft
# that follows is the one model call - and campaign_overview is everything
# the page shows around it, all of it measured.

_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WINBACK_WORDS = ("win back", "win-back", "winback", "bring back", "come back", "haven't been", "havent been",
                  "haven't visited", "havent visited", "have not been", "missed", "miss you", "lapsed", "drifted",
                  "been a while", "lost guests", "inactive")
_LOYALTY_WORDS = ("regular", "thank", "thank you", "loyal", "vip", "best guests", "appreciate", "appreciation")
_NEW_WORDS = ("first-time", "first time", "first-timer", "new guest", "newcomer", "first visit", "one visit")
_HOLIDAY_WORDS = ("holiday", "thanksgiving", "christmas", "new year", "new year's", "new years", "valentine",
                  "valentine's", "mother's day", "mothers day", "father's day", "fathers day", "easter",
                  "halloween", "fourth of july", "july 4th", "st patrick's", "super bowl", "game day")
_EVENT_WORDS = ("event", "special", "tonight", "this weekend", "trivia", "live music", "happy hour", "promo",
                "promote", "launch", "new menu", "fill", "slow", "quiet", "busier", "sales", "deal", "brunch",
                "tasting", "party") + _HOLIDAY_WORDS
_FILL_WORDS = ("fill", "slow", "quiet", "busier", "pack")


def _term_re(words):
    """Whole words or phrases, an optional plural "s", never part of a longer
    word: "thank" is not "Thanksgiving" and "slow" is not "slow-roasted"
    (CS-9, audit #52) — the old rules were substrings."""
    alts = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(words, key=len, reverse=True))
    return re.compile(r"(?<![\w-])(?:" + alts + r")s?(?![\w-])")


_WINBACK_RE, _LOYALTY_RE, _NEW_RE = _term_re(_WINBACK_WORDS), _term_re(_LOYALTY_WORDS), _term_re(_NEW_WORDS)
_EVENT_RE, _FILL_RE, _WEEKDAY_RE = _term_re(_EVENT_WORDS), _term_re(_FILL_WORDS), _term_re(_WEEKDAY_NAMES)
_SIXTY_RE = _term_re(("60", "sixty", "two months", "2 months", "couple of months", "couple months"))


def plan_campaign(prompt) -> dict:
    """What a typed goal asks for: the tone the draft is written in (a
    CAMPAIGN_PROMPTS key), the audience it goes to (a SEGMENTS key), the goal
    in the owner's words for the page, and the weekday it is meant to fill
    (target_day, which starts the slow-day tracker once texts go -
    track_campaign_outcome). Keyword rules on whole words, no model call; the
    owner can change the audience before anything goes out.

    target_day is set only for a fill-a-night goal — a fill word AND a
    weekday — and the weekday is the first one WRITTEN, not the first in
    calendar order: "Fill Saturday, plus Tuesday trivia" fills Saturday
    (CS-9). "Slow-roasted brisket Friday" names no slow night."""
    p = " " + re.sub(r"\s+", " ", str(prompt or "").translate(_PLAIN).lower()) + " "
    m = _WEEKDAY_RE.search(p)
    day = m.group(0)[:-1] if m and m.group(0).endswith("days") else (m.group(0) if m else None)
    if _WINBACK_RE.search(p):
        return {"type": "win_back", "segment": "lapsed_60" if _SIXTY_RE.search(p) else "lapsed_30",
                "goal": "Win back guests who drifted away", "target_day": None}
    if _NEW_RE.search(p):
        return {"type": "loyalty", "segment": "new", "goal": "Turn first-timers into regulars", "target_day": None}
    if _LOYALTY_RE.search(p):
        return {"type": "loyalty", "segment": "regulars", "goal": "Thank your regulars", "target_day": None}
    if day and _FILL_RE.search(p):
        return {"type": "event", "segment": "all", "goal": f"Fill {day.capitalize()}", "target_day": day.capitalize()}
    if _EVENT_RE.search(p) or day:
        return {"type": "event", "segment": "all", "goal": "Promote an event or special", "target_day": None}
    return {"type": "general", "segment": "all", "goal": "Send your guests a text", "target_day": None}


CAMPAIGN_RATE_MIN = 2           # campaigns a rate rests on before the page shows it
CAMPAIGN_RATE_MIN_SENT = 10     # texts a campaign needs to count toward a rate


def campaign_overview(restaurant_id, db_path=DB_PATH) -> dict:
    """The Campaigns page's figures, every one measured:

      subscribers   guests who can be texted (consented, not unsubscribed)
      today / last_30  opt-ins by consent_at, on the restaurant's calendar
      weekly        opt-ins per week, oldest first, the last 12 weeks
      last_campaign the newest send: its local date, sent, audience
      tap_rate      taps on the tracked link per text, over campaigns that
                    carried one (at least CAMPAIGN_RATE_MIN of them, each
                    CAMPAIGN_RATE_MIN_SENT or more) - else None
      back_rate     guests who came back within the attribution window per
                    text, over campaigns whose whole window Toast attribution
                    has read (CS-8) - else None
      tap_by_segment / back_by_segment  the same two rates per audience, so
                    a forecast for one audience uses that audience's record
                    or none (CS-8: the all-audience rate was applied to any)
      accepted      sent / (sent + failed) this month: texts the carrier
                    ACCEPTED (Twilio's 201). There is no delivery receipt yet,
                    so it is not called "delivered" (CS-13, audit #35)
      sms           what the Studio's counter and preview need: the name the
                    send puts in front, a tracked link's length, the STOP
                    line's, the limit, and the number guests see it from

    SMS has no opens: a phone reports none, so there is no open rate here.
    Nothing is summed across these, and none is money."""
    from time_utils import restaurant_now_by_id
    now = restaurant_now_by_id(restaurant_id, naive=True)
    today = now.date()
    contacts = get_guest_contacts(restaurant_id, db_path=db_path)
    textable = [c for c in contacts if c["consent"] and not c["unsubscribed"]]

    def consent_day(c):
        try:
            return datetime.fromisoformat(str(c.get("consent_at") or "")[:19]).date()
        except ValueError:
            return None

    days = [d for d in (consent_day(c) for c in textable) if d]
    week0 = today - timedelta(days=today.weekday())          # this week's Monday
    weekly = []
    for i in range(11, -1, -1):
        start = week0 - timedelta(weeks=i)
        weekly.append({"week_start": start.isoformat(),
                       "joined": sum(1 for d in days if start <= d < start + timedelta(days=7))})

    hist = campaign_history(restaurant_id, limit=50, db_path=db_path)
    sized = [c for c in hist if (c.get("sent_count") or 0) >= CAMPAIGN_RATE_MIN_SENT]
    linked = [c for c in sized if c.get("link_token")]
    attributed = [c for c in sized if c.get("window_closed")]

    def rate(rows, key):
        if len(rows) < CAMPAIGN_RATE_MIN:
            return None
        sent = sum(int(c.get("sent_count") or 0) for c in rows)
        return {"pct": round(sum(int(c.get(key) or 0) for c in rows) / sent * 100, 1), "campaigns": len(rows)} if sent else None

    month = f"{today.year:04d}-{today.month:02d}"
    this_month = [c for c in hist if str(c.get("created_at") or "")[:7] == month]
    m_sent = sum(int(c.get("sent_count") or 0) for c in this_month)
    m_failed = sum(int(c.get("failed_count") or 0) for c in this_month)
    last = hist[0] if hist else None
    channel = "text"
    # The newest campaign on either channel (Campaign Studio, 9/28/26).
    try:
        import guest_email
        mail = guest_email.newsletter_history(restaurant_id, limit=1, db_path=db_path)
    except Exception:
        mail = []
    if mail and (not last or str(mail[0].get("created_at") or "") > str(last.get("created_at") or "")):
        # Sent, never the total: an email that failed to everyone read
        # "Last campaign: 40 emailed" (CS-3).
        last = {"created_at": mail[0]["created_at"], "sent_count": mail[0]["sent"],
                "segment_label": mail[0].get("segment_label")}
        channel = "email"
    from time_utils import local_iso
    tz, r = None, None
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db_path)
        tz = getattr(r, "timezone", None) if r else None
    except Exception:
        tz = None
    return {
        "subscribers": len(textable),
        "today": sum(1 for d in days if d == today),
        "last_30": sum(1 for d in days if (today - d).days < 30),
        "weekly": weekly,
        "last_campaign": ({"date": local_iso(last.get("created_at"), tz), "sent": int(last.get("sent_count") or 0),
                           "segment_label": last.get("segment_label") or SEGMENTS["all"]["label"],
                           "channel": channel} if last else None),
        "tap_rate": rate(linked, "clicks"),
        "back_rate": rate(attributed, "visits_matched"),
        "tap_by_segment": _by_segment(linked, "clicks", rate),
        "back_by_segment": _by_segment(attributed, "visits_matched", rate),
        "accepted": (round(m_sent / (m_sent + m_failed) * 100, 1) if (m_sent + m_failed) else None),
        "texts_this_month": m_sent,
        "texts_failed_this_month": m_failed,
        "sms": {"prefix": sms_prefix(getattr(r, "name", "") if r else ""), "link_chars": link_chars(),
                "link_example": _short_link("…"), "stop_chars": len(STOP_LINE), "max": CAMPAIGN_MAX_CHARS,
                "sender": _sms_sender()},
        "sending_now": guest_sms_allowed_now(restaurant_id),
        "window": guest_sms_window_label(),
        "min_days_between": GUEST_SMS_MIN_DAYS_BETWEEN,
        "rate_min": CAMPAIGN_RATE_MIN,
        # The email channel (Campaign Studio, 9/28/26): who can be emailed,
        # and whether the law's mailing address is on file to email them.
        "email_subscribers": _email_subscribers(restaurant_id, db_path),
        "mailing_address_set": bool((getattr(r, "mailing_address", None) or "").strip()),
        "insights": campaign_insights(restaurant_id, hist=hist, db_path=db_path),
    }


def _by_segment(rows, key, rate) -> dict:
    """{segment: rate} over each audience's own campaigns; an audience below
    the rate's minimum is absent, not zero."""
    out = {}
    for seg in SEGMENTS:
        v = rate([c for c in rows if (c.get("segment") or "all") == seg], key)
        if v:
            out[seg] = v
    return out


def _sms_sender() -> str:
    """The number guests see a campaign come from, for the Studio's phone
    preview ("" when it isn't known here)."""
    try:
        import notify
        return notify.guest_sender_display()
    except Exception:
        return ""


def _email_subscribers(restaurant_id, db_path=DB_PATH) -> int:
    try:
        import guest_email
        return guest_email.subscriber_count(restaurant_id, db_path=db_path)
    except Exception:
        return 0


def campaign_insights(restaurant_id, hist=None, db_path=DB_PATH) -> list:
    """At most three short things that worked, each measured and carrying
    what it rests on - never an estimate, never money:

      back    how many texted guests came back within 14 days, over every
              campaign whose window has closed (CAMPAIGN_RATE_MIN of them, each
              CAMPAIGN_RATE_MIN_SENT or more texts). One figure across them:
              this used to crown the audience with the highest raw rate, which
              is regulars by construction - they come back whether or not they
              are texted - with no holdout to say otherwise (CS-8, audit #63,
              M-22). "Came back within", never "because of".
      opened  the last email's opens recorded (Apple Mail auto-opens
              included, so never "at least"), once open tracking reports
              and it went to 10 or more

    An insight below its minimum is left out, not shown as zero."""
    out = []
    hist = hist if hist is not None else campaign_history(restaurant_id, limit=50, db_path=db_path)
    rows = [c for c in hist if (c.get("sent_count") or 0) >= CAMPAIGN_RATE_MIN_SENT and _window_closed(c)]
    sent = sum(int(c.get("sent_count") or 0) for c in rows)
    if len(rows) >= CAMPAIGN_RATE_MIN and sent:
        pct = round(sum(int(c.get("visits_matched") or 0) for c in rows) / sent * 100, 1)
        out.append({"kind": "back", "figure": f"{pct:g}%", "tone": "",
                    "text": f"of texted guests came back within {ATTRIBUTION_WINDOW_DAYS} days",
                    "basis": f"{len(rows)} campaigns · a visit matched in your POS, not proof the text brought them"})
    try:
        import guest_email
        last = next((n for n in guest_email.newsletter_history(restaurant_id, limit=5, db_path=db_path)
                     if n["sent"] >= CAMPAIGN_RATE_MIN_SENT), None)
    except Exception:
        last = None
    if last and last.get("opened") is not None:
        # Recorded, not "at least" (CS-7): Apple Mail's auto-opens push it up.
        out.append({"kind": "opened", "figure": f"{round(last['opened'] / last['sent'] * 100):g}%", "tone": "",
                    "text": "opens recorded on your last email",
                    "basis": "includes Apple Mail auto-opens"})
    return out[:3]


# ── Automated post-visit review request ─────────────────────────────────────

DEFAULT_REVIEW_REQUEST_DELAY_HOURS = 3


def _google_review_link(place_id):
    return (f"https://search.google.com/local/writereview?placeid={place_id}"
            if place_id else "")


OPTIN_INVITE_PASS_SECONDS = int(os.getenv("OPTIN_INVITE_SECONDS", "240"))
OPTIN_INVITE_CURSOR_KEY = "optin_invite_cursor"
# Retention (MB-3): a contact the invite job (or an unconfirmed join) put on
# the list, who never answered and holds no consent, goes after this long.
CONTACT_RETENTION_DAYS = 30
PURGE_BATCH = 500

# The owner's switch (Campaigns -> Settings) shows this sentence beside it;
# turning the switch on is the acknowledgement, stored with who and when
# (restaurants.optin_invites_ack_at / _ack_by) and the version here.
OPTIN_INVITES_DISCLOSURE_VERSION = "2026-09-28"
OPTIN_INVITES_DISCLOSURE = (
    "Each afternoon, text guests your POS identified at your last service one message asking if they'd "
    "like a review link; by turning this on you confirm those guests were told at checkout you may "
    "text them about their visit.")


def optin_invites_state(restaurant) -> dict:
    """The switch as the Settings row reads it."""
    return {"enabled": bool(getattr(restaurant, "optin_invites_enabled", 0)),
            "acknowledged_at": getattr(restaurant, "optin_invites_ack_at", None),
            "acknowledged_by": getattr(restaurant, "optin_invites_ack_by", None),
            "disclosure": OPTIN_INVITES_DISCLOSURE,
            "disclosure_version": OPTIN_INVITES_DISCLOSURE_VERSION}


def set_optin_invites(restaurant_id, enabled, user_id=None, acknowledged=False, db_path=DB_PATH) -> dict:
    """Turn the Toast opt-in invite texts on or off. On needs the owner's
    acknowledgement of OPTIN_INVITES_DISCLOSURE; who and when is stored.
    Off keeps the last acknowledgement on record."""
    from models import update_restaurant, get_restaurant
    if enabled and not acknowledged:
        return {"ok": False, "error": "Turning invites on needs your acknowledgement of the sentence beside it."}
    fields = {"optin_invites_enabled": 1 if enabled else 0}
    if enabled:
        from time_utils import utc_stamp
        fields["optin_invites_ack_at"] = utc_stamp()
        fields["optin_invites_ack_by"] = int(user_id) if user_id else None
    update_restaurant(restaurant_id, fields, db_path=db_path)
    r = get_restaurant(restaurant_id, db_path=db_path)
    return dict(optin_invites_state(r), ok=True)


def purge_unanswered_contacts(db_path=DB_PATH, limit=PURGE_BATCH, days=CONTACT_RETENTION_DAYS) -> int:
    """Retention (MB-3): every identified POS guest used to be stored as a
    contact forever. A contact the invite job created (source toast_invite),
    or a join-form number that never confirmed (source join), is deleted
    once it is `days` old when nobody answered in that time and nothing
    else holds it: no consent of any kind, no email, no visit, no campaign,
    no review ask, no invite answered or sent inside `days`, no join
    confirmed or requested inside `days`. At most `limit` rows a pass.

    Never purged: a STOP (guest_sms_optouts is not touched, and an
    unsubscribed row's STOP is copied there before the row goes), the
    invite record (so the number is not invited again), and the consent
    evidence. Rows from before `source` existed are never considered."""
    window = f"-{int(days)} days"
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT g.id, g.restaurant_id, g.phone, g.unsubscribed FROM guest_contacts g "
            "WHERE g.source IN ('toast_invite', 'join') AND COALESCE(g.consent,0)=0 "
            "AND COALESCE(g.review_consent,0)=0 AND COALESCE(TRIM(g.email),'')='' "
            "AND g.last_visit IS NULL AND COALESCE(g.visit_count,0)=0 AND g.last_campaign_at IS NULL "
            "AND g.last_review_requested_at IS NULL AND g.created_at < datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM sms_optin_invites i WHERE i.restaurant_id = g.restaurant_id "
            "  AND i.phone = g.phone AND (i.responded_at IS NOT NULL OR i.sent_at >= datetime('now', ?))) "
            "AND NOT EXISTS (SELECT 1 FROM guest_optin_requests q WHERE q.restaurant_id = g.restaurant_id "
            "  AND q.phone = g.phone AND (q.status = 'confirmed' OR q.requested_at >= datetime('now', ?))) "
            "ORDER BY g.id LIMIT ?", (window, window, window, int(limit))).fetchall()
        for r in rows:
            if r["unsubscribed"]:
                conn.execute("INSERT OR IGNORE INTO guest_sms_optouts (restaurant_id, phone) VALUES (?,?)",
                             (int(r["restaurant_id"]), r["phone"]))
            conn.execute("DELETE FROM guest_contacts WHERE id=?", (r["id"],))
        # The request rows are the durable submission limit (a day) and
        # carry the IP; the evidence ledger keeps what they proved.
        conn.execute("DELETE FROM guest_optin_requests WHERE id IN (SELECT id FROM guest_optin_requests "
                     "WHERE status != 'confirmed' AND requested_at < datetime('now','-90 days') LIMIT ?)",
                     (int(limit),))
        conn.execute("DELETE FROM sms_pending_questions WHERE id IN (SELECT id FROM sms_pending_questions "
                     "WHERE asked_at < datetime('now','-30 days') LIMIT ?)", (int(limit),))
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def run_toast_optin_invites(business_date=None, db_path=DB_PATH, max_seconds=None):
    """Hourly through the afternoon — invites guests the POS identified at
    each restaurant's last closed service to ask for a review link.

    OFF until the owner turns it on (MB-3 / #2, owner decision 9/28/26): a
    restaurant is invited for only with optin_invites_enabled=1 AND an
    acknowledgement on record (Campaigns -> Settings), never a demo row. It
    used to run for every restaurant with Marketing on — the default — so
    every identified Toast guest got a cold text nobody at the restaurant
    had chosen to send.

    The invite is a single transactional message tied to a visit that
    actually happened; it is deliberately NOT the review request and NOT a
    campaign. The guest's number is stored unconsented (source
    toast_invite), a YES reply sets REVIEW-LINK consent only (never
    marketing, MB-2), and purge_unanswered_contacts removes the ones who
    never answered after CONTACT_RETENTION_DAYS.

    A guest already consented (either scope), already unsubscribed, already
    invited, or who has texted STOP to any restaurant on the shared number
    is skipped, so re-running is safe. The business date is each
    restaurant's own last CLOSED business day (MB-19 / #58) — the server's
    "yesterday" was an open day for a restaurant behind UTC — unless
    `business_date` pins one. A restaurant outside its own 8am-9pm window
    is deferred to a later pass, and one finished for its date
    (optin_invite_runs) is not fetched again (MOD-MKT-12).

    Bounded and resumable (MB-18 / #62): a resumable_sweep with a cursor,
    and a restaurant cut off mid-list is not marked done — its invites so
    far are recorded, so the next pass carries on from them."""
    import time as _time
    from models import in_service_sql, get_restaurant
    import pos as _pos
    import scheduler
    if max_seconds is None:
        max_seconds = OPTIN_INVITE_PASS_SECONDS
    started = _time.monotonic()
    counts = {"invited": 0, "skipped": 0, "failed": 0, "deferred": 0, "purged": 0}
    try:
        counts["purged"] = purge_unanswered_contacts(db_path=db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="toast_optin_invites", context="retention purge")

    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name FROM restaurants WHERE module_marketing=1 "
            "AND COALESCE(optin_invites_enabled,0)=1 AND optin_invites_ack_at IS NOT NULL "
            "AND COALESCE(is_demo,0)=0 AND " + in_service_sql("billing_status")
        ).fetchall()
    finally:
        conn.close()
    # Whichever POS shares guest records (pos.supports), not a Toast column.
    restaurants = {r["id"]: r for r in rows if _pos.supports(r["id"], "fetch_order_customers")}

    def _one(rid):
        r = restaurants[rid]
        if business_date is not None:
            bdate = business_date
        else:
            robj = get_restaurant(rid, db_path=db_path)
            if not robj:
                return
            bdate = _last_closed_business_date(robj)
        bdate_s = str(bdate)
        conn = get_conn(db_path)
        try:
            done = conn.execute("SELECT 1 FROM optin_invite_runs WHERE restaurant_id=? AND business_date=?",
                                (rid, bdate_s)).fetchone()
        finally:
            conn.close()
        if done:
            return
        # An opt-in invite asks for consent, which makes it a marketing text:
        # the same 8am-9pm window as everything else here. A deferred
        # restaurant is not marked done, so the next hourly pass retries it.
        if not guest_sms_allowed_now(rid):
            counts["deferred"] += 1
            return
        try:
            customers, _prov = _pos.fetch_order_customers(rid, bdate)
        except Exception:
            counts["failed"] += 1
            return       # not marked done: the next pass fetches again

        finished = True
        for cust in customers:
            if _time.monotonic() - started > max_seconds:
                finished = False
                break
            phone = _normalize_phone(cust["phone"])
            conn = get_conn(db_path)
            try:
                existing = conn.execute(
                    "SELECT consent, review_consent, unsubscribed FROM guest_contacts "
                    "WHERE restaurant_id=? AND phone=?", (rid, phone)
                ).fetchone()
                already_invited = conn.execute(
                    "SELECT 1 FROM sms_optin_invites WHERE restaurant_id=? AND phone=?", (rid, phone)
                ).fetchone()
            finally:
                conn.close()

            if existing and (existing["consent"] or existing["review_consent"] or existing["unsubscribed"]):
                counts["skipped"] += 1
                continue
            if already_invited or phone_opted_out(phone, db_path=db_path):
                counts["skipped"] += 1
                continue

            # Stored unconsented — visible to the owner, textable only if
            # the guest replies YES, purged if they never do.
            add_guest_contact_manual(rid, phone, name=cust.get("name") or None, db_path=db_path,
                                     source="toast_invite")

            if record_optin_invite(rid, phone, source="toast_order",
                                   external_ref=cust.get("order_guid"), db_path=db_path) is None:
                counts["skipped"] += 1
                continue

            first = (cust.get("name") or "").split()[0] if cust.get("name") else "there"
            message = (
                f"Hi {first}, thanks for visiting {r['name']}! "
                "Reply YES if we can text you a quick link to leave a review. "
                "Reply STOP to opt out."
            )
            try:
                if send_sms(phone, message, use_case="guest"):
                    counts["invited"] += 1
                else:
                    counts["failed"] += 1
            except Exception:
                counts["failed"] += 1

        if finished:
            conn = get_conn(db_path)
            try:
                conn.execute("INSERT OR IGNORE INTO optin_invite_runs (restaurant_id, business_date) "
                             "VALUES (?,?)", (rid, bdate_s))
                conn.commit()
            finally:
                conn.close()

    hit_bound = False
    if restaurants:
        _done, hit_bound = scheduler.resumable_sweep(OPTIN_INVITE_CURSOR_KEY, sorted(restaurants), _one,
                                                     max_seconds, job="toast_optin_invites")

    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM optin_invite_runs WHERE done_at < datetime('now','-45 days')")
        conn.commit()
    finally:
        conn.close()
    return {"invited": counts["invited"], "skipped": counts["skipped"], "failed": counts["failed"],
            "deferred_quiet_hours": counts["deferred"], "purged": counts["purged"],
            "hit_bound": bool(hit_bound)}


# One pass stops after this long; guests it did not reach stay eligible (the
# claim below is their only marker), so the next hourly pass continues.
REVIEW_REQUEST_PASS_SECONDS = 240
# A review request asks about a visit this recent or not at all (MB-20).
REVIEW_REQUEST_MAX_AGE_HOURS = 48


def run_review_request_followups(delay_hours=None, db_path=DB_PATH, max_seconds=None):
    """Hourly job (called from scheduler.py) — texts a Google-review link to
    any consented, non-unsubscribed guest whose last visit crossed the delay
    threshold, as long as they haven't already been asked about *this* visit.

    Eligibility is "last_review_requested_at is unset or older than
    last_visit" rather than an exact hour-window match — that makes it
    idempotent under scheduler downtime/late ticks (it just catches up next
    run instead of missing the window) and naturally re-arms on a genuinely
    new visit (a visit the owner marks; a join or a YES is never a visit).

    Consent is the review scope (review_text_sql): marketing consent, or the
    review-link consent a YES to the invite gives. And only a visit inside
    REVIEW_REQUEST_MAX_AGE_HOURS is asked about (MB-20): with no upper
    bound, a place_id added later or a resumed account texted "thanks for
    visiting" to guests whose visit was months old.

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
    # A coarse bound in SQL (a local stamp two days old is at least this
    # UTC date anywhere); the exact 48 hours is checked per restaurant below.
    visit_floor = (datetime.utcnow() - timedelta(hours=REVIEW_REQUEST_MAX_AGE_HOURS + 30)).strftime("%Y-%m-%d")

    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT gc.id AS contact_id, gc.restaurant_id, gc.name, gc.phone, gc.last_visit,
                   gc.last_review_requested_at,
                   r.name AS restaurant_name, r.google_place_id
            FROM guest_contacts gc
            JOIN restaurants r ON r.id = gc.restaurant_id
            WHERE """ + review_text_sql("gc") + """
              AND r.module_marketing=1
              AND """ + in_service_sql("r.billing_status") + """
              AND gc.last_visit IS NOT NULL AND gc.last_visit >= ?
              AND (gc.last_review_requested_at IS NULL OR gc.last_review_requested_at < gc.last_visit)
            ORDER BY gc.id
            """, (visit_floor,)
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
        if now_local - visited_at > timedelta(hours=REVIEW_REQUEST_MAX_AGE_HOURS):
            continue  # too long ago to ask "thanks for visiting" (MB-20)
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
                "UPDATE guest_contacts SET last_review_requested_at=? WHERE id=? AND " + review_text_sql() +
                " AND (last_review_requested_at IS NULL OR last_review_requested_at < last_visit)",
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
