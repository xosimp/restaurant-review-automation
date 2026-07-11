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
"""


def init_guest_marketing(db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    # Migration for guest_contacts rows created before last_visit/
    # last_review_requested_at existed (same pattern as webhooks.init_webhooks).
    for col_sql in (
        "ALTER TABLE guest_contacts ADD COLUMN last_visit TEXT",
        "ALTER TABLE guest_contacts ADD COLUMN last_review_requested_at TEXT",
    ):
        try:
            conn.execute(col_sql)
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_guest_contacts(restaurant_id, consent_only=False, db_path=DB_PATH):
    """consent_only=True is the enforcement point for actually sending SMS —
    same shape as notify.get_alert_contacts. Management UI wants
    consent_only=False so the owner can see (and remove) every contact,
    consented or not."""
    conn = get_conn(db_path)
    query = ("SELECT id, name, phone, consent, consent_at, unsubscribed, "
             "last_visit, last_review_requested_at FROM guest_contacts WHERE restaurant_id=?")
    if consent_only:
        query += " AND consent=1 AND unsubscribed=0"
    rows = conn.execute(query + " ORDER BY id DESC", (restaurant_id,)).fetchall()
    conn.close()
    return [
        {"id": r["id"], "name": r["name"] or "", "phone": r["phone"],
         "consent": bool(r["consent"]), "consent_at": r["consent_at"],
         "unsubscribed": bool(r["unsubscribed"]), "last_visit": r["last_visit"],
         "last_review_requested_at": r["last_review_requested_at"]}
        for r in rows
    ]


def add_guest_contact_manual(restaurant_id, phone, name=None, db_path=DB_PATH):
    """Owner adding a number for their own reference/tracking — never
    consented, can never receive a campaign until the guest opts in
    themselves via the public join page."""
    return _upsert_contact(restaurant_id, phone, name=name, consent=False, db_path=db_path)


def add_guest_contact_public_optin(restaurant_id, phone, name=None, db_path=DB_PATH):
    """The one and only path that can ever set consent=True — the guest
    submitting the public opt-in page themselves."""
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
                "unsubscribed=0, name=COALESCE(?,name), last_visit=? WHERE id=?",
                (now_iso, name, now_iso, existing["id"])
            )
        elif name:
            conn.execute("UPDATE guest_contacts SET name=? WHERE id=?", (name, existing["id"]))
        conn.commit()
        contact_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO guest_contacts (restaurant_id, name, phone, consent, consent_at, last_visit) VALUES (?,?,?,?,?,?)",
            (restaurant_id, (name or "").strip() or None, phone, int(consent), now_iso, now_iso)
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
    conn.execute(
        "UPDATE guest_contacts SET last_visit=? WHERE id=? AND restaurant_id=?",
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


CAMPAIGN_PROMPTS = {
    "win_back": "a friendly win-back text to a guest who hasn't visited in a while, inviting them back",
    "event": "a text announcing an upcoming event, special, or promotion",
    "loyalty": "a short thank-you/loyalty text rewarding a regular guest",
    "general": "a short promotional text on the topic given",
}


def draft_campaign_message(restaurant, campaign_type="general", topic=""):
    """AI-drafts a short SMS (under ~300 chars — a real SMS/MMS segment
    budget, not email) in the restaurant's own voice. Reuses marketing.py's
    profile lookup for brand voice instead of re-deriving it."""
    from marketing import get_profile_for_restaurant

    p = get_profile_for_restaurant(restaurant.id)
    intent = CAMPAIGN_PROMPTS.get(campaign_type, CAMPAIGN_PROMPTS["general"])
    never_clause = f" Never use these words or phrases: {p['never_say']}." if p.get("never_say") else ""
    topic_clause = f" Topic/specifics to include: {topic}." if topic else ""

    prompt = (
        f"Write {intent} for {p['name']}, a {p['vibe']} in {p['neighborhood']}. "
        f"Brand voice: {p['voice']}.{never_clause}{topic_clause}\n\n"
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


def send_campaign(restaurant_id, message, db_path=DB_PATH):
    """Send `message` to every consented, non-unsubscribed guest contact.
    Returns {"sent": n, "failed": n, "total": n}. Never raises — a bad
    number failing to send shouldn't stop the rest of the list."""
    contacts = get_guest_contacts(restaurant_id, consent_only=True, db_path=db_path)
    full_message = message.strip() + "\n\nReply STOP to unsubscribe."
    sent, failed = 0, 0
    for c in contacts:
        try:
            if send_sms(c["phone"], full_message):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count) VALUES (?,?,?,?)",
        (restaurant_id, message.strip(), sent, failed)
    )
    conn.commit()
    conn.close()
    return {"sent": sent, "failed": failed, "total": len(contacts)}


# ── Automated post-visit review request ─────────────────────────────────────

DEFAULT_REVIEW_REQUEST_DELAY_HOURS = 3


def _google_review_link(place_id):
    return (f"https://search.google.com/local/writereview?placeid={place_id}"
            if place_id else "")


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

    sent, failed, skipped = 0, 0, 0
    for row in rows:
        try:
            visited_at = datetime.fromisoformat(row["last_visit"])
        except Exception:
            continue
        now_local = restaurant_now_by_id(row["restaurant_id"], naive=True)
        if now_local - visited_at < timedelta(hours=delay_hours):
            continue  # not due yet

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
    return {"sent": sent, "failed": failed, "skipped_no_place_id": skipped}
