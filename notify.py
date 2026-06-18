"""
notify.py — alert system via Twilio (SMS) and Resend (email).
Both channels use the same 6 alert toggles; delivery is controlled
by urgent_via_sms and urgent_via_email per restaurant.
"""
import os
import requests
from models import get_conn, DB_PATH

TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM    = os.getenv("TWILIO_FROM_NUMBER", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "will@cavnar.ai")

HEALTH_KEYWORDS = [
    "food poison", "food poisoning", "foodborne", "sick after", "got sick",
    "felt sick", "vomit", "threw up", "throw up", "diarrhea", "nausea after",
    "ill after", "hospital", "health department", "health inspector",
    "cockroach", "roach", "rat ", "rats ", "rodent", "bug in ", "insect in",
    "foreign object", "glass in", "metal in", "hair in", "mold", "mouldy",
    "raw chicken", "raw meat", "undercooked chicken", "salmonella", "ecoli", "e. coli",
]


def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    if digits.startswith("+"):
        return digits
    digits_only = "".join(c for c in phone if c.isdigit())
    if len(digits_only) == 10:
        return "+1" + digits_only
    if len(digits_only) == 11 and digits_only.startswith("1"):
        return "+" + digits_only
    return "+" + digits_only


def send_sms(to_phone: str, message: str) -> bool:
    """Send a single SMS via Twilio. Returns True on success."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        print(f"[notify] Twilio not configured — would send to {to_phone}: {message[:80]}")
        return False
    phone = _normalize_phone(to_phone)
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"From": TWILIO_FROM, "To": phone, "Body": message},
            timeout=10,
        )
        if r.status_code == 201:
            return True
        print(f"[notify] Twilio error {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[notify] SMS send failed: {e}")
        return False


def _send_alert_email(owner_email: str, subject: str, html: str) -> bool:
    """Send an alert email via Resend. Returns True on success."""
    if not RESEND_API_KEY or not owner_email:
        print(f"[notify] Resend not configured — would email {owner_email}: {subject}")
        return False
    try:
        import resend as _r
        _r.api_key = RESEND_API_KEY
        _r.Emails.send({
            "from": f"Cavnar AI Alerts <{FROM_EMAIL}>",
            "to": [owner_email],
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        print(f"[notify] Email send failed: {e}")
        return False


def _alert_email_html(restaurant_name: str, headline: str, body_lines: list, cta_label: str = "View on dashboard") -> str:
    body_html = "".join(f'<p style="font-size:14px;color:#3a3530;line-height:1.6;margin:0 0 10px">{l}</p>' for l in body_lines)
    return f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
    <h2 style="font-family:Georgia,serif;font-size:20px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">Alert &mdash; {restaurant_name}</p>
  </div>
  <h3 style="font-size:16px;font-weight:600;margin:0 0 12px;color:#1a1714">{headline}</h3>
  {body_html}
  <div style="margin-top:20px">
    <a href="https://dashboard.cavnar.ai"
       style="display:inline-block;background:#c84b2f;color:white;padding:11px 22px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">
      {cta_label} &#8594;
    </a>
  </div>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Cavnar AI &middot;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &middot; Reply to this email or log in to manage alert settings.
  </p>
</div>"""


def send_test_sms(restaurant_id: int) -> dict:
    """Send a test SMS to all contacts for a restaurant."""
    contacts = get_alert_contacts(restaurant_id)
    if not contacts:
        return {"ok": False, "error": "No alert contacts configured"}
    from models import get_restaurant
    restaurant = get_restaurant(restaurant_id)
    name = restaurant.name if restaurant else f"Restaurant {restaurant_id}"
    msg = f"✓ Test alert from Cavnar AI\n{name} — SMS alert system is active and working."
    sent, errors = 0, []
    for c in contacts:
        ok = send_sms(c["phone"], msg)
        if ok:
            sent += 1
        else:
            errors.append(c["phone"])
    return {"ok": sent > 0, "sent": sent, "errors": errors}


# ── Contact CRUD ───────────────────────────────────────────────

def get_alert_contacts(restaurant_id: int, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT id, name, phone FROM alert_contacts WHERE restaurant_id=? ORDER BY id",
        (restaurant_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"] or "", "phone": r["phone"]} for r in rows]


def add_alert_contact(restaurant_id: int, name: str, phone: str, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO alert_contacts (restaurant_id, name, phone) VALUES (?,?,?)",
        (restaurant_id, name.strip(), phone.strip()),
    )
    conn.commit()
    contact_id = cur.lastrowid
    conn.close()
    return contact_id


def delete_alert_contact(contact_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM alert_contacts WHERE id=?", (contact_id,))
    conn.commit()
    conn.close()


# ── Alert helpers ─────────────────────────────────────────────

def _is_health_alert(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in HEALTH_KEYWORDS)


def _neg_spike_count(restaurant_id: int, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    count = conn.execute("""
        SELECT COUNT(*) FROM reviews
        WHERE restaurant_id=? AND sentiment='negative'
        AND fetched_at >= datetime('now', '-7 days')
    """, (restaurant_id,)).fetchone()[0]
    conn.close()
    return count


def _already_alerted_spike(restaurant_id: int, db_path: str = DB_PATH) -> bool:
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT id FROM alert_log
        WHERE restaurant_id=? AND alert_type='neg_spike'
        AND fired_at >= datetime('now', '-24 hours')
    """, (restaurant_id,)).fetchone()
    conn.close()
    return row is not None


def _log_alert(restaurant_id: int, alert_type: str, review_id: int = None, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO alert_log (restaurant_id, alert_type, review_id) VALUES (?,?,?)",
        (restaurant_id, alert_type, review_id),
    )
    conn.commit()
    conn.close()


# ── Main alert dispatch ───────────────────────────────────────

def fire_review_alerts(restaurant_id: int, restaurant_name: str, new_reviews: list, db_path: str = DB_PATH):
    """
    Check newly saved reviews against per-restaurant alert toggles.
    Fires SMS (if urgent_via_sms) and/or email (if urgent_via_email).
    Call this after save_reviews() with the list of newly inserted Review objects.
    """
    if not new_reviews:
        return

    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT alert_1star, alert_2star, alert_health,
               alert_neg_spike, alert_negative_trend, alert_no_response,
               urgent_via_sms, urgent_via_email, owner_email
        FROM restaurants WHERE id=?
    """, (restaurant_id,)).fetchone()
    conn.close()

    if not row:
        return

    via_sms   = bool(row["urgent_via_sms"])
    via_email = bool(row["urgent_via_email"])
    if not via_sms and not via_email:
        return

    contacts    = get_alert_contacts(restaurant_id, db_path) if via_sms else []
    owner_email = row["owner_email"] or ""

    def blast(sms_text: str, subject: str, html: str, alert_type: str, review_id: int = None):
        if via_sms and contacts:
            for c in contacts:
                send_sms(c["phone"], sms_text)
        if via_email and owner_email:
            _send_alert_email(owner_email, subject, html)
        _log_alert(restaurant_id, alert_type, review_id)

    for review in new_reviews:
        rating   = review.rating or 0
        text     = review.text or ""
        author   = (review.author or "").split()[0]
        platform = (review.platform or "Google").title()
        preview  = text[:120].strip()
        ellipsis = "…" if len(text) > 120 else ""

        # Health alert — highest priority
        if row["alert_health"] and _is_health_alert(text):
            sms = (
                f"🚨 HEALTH ALERT — {restaurant_name}\n"
                f"{rating}★ {platform}: \"{preview}{ellipsis}\"\n"
                f"Requires immediate response · dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🚨 Health/safety mention in a new review",
                [
                    f"A <strong>{rating}★ review</strong> on {platform} contains a health or safety mention.",
                    f'<em>"{preview}{ellipsis}"</em>',
                    "This requires an immediate response.",
                ],
                cta_label="Respond now",
            )
            blast(sms, f"🚨 Health alert — {restaurant_name}", html, "health", review.id)
            continue

        # 1★ alert
        if rating == 1 and row["alert_1star"]:
            who  = f"{author}: " if author else ""
            sms  = (
                f"🔴 1★ Review — {restaurant_name}\n"
                f"{who}\"{preview}{ellipsis}\"\n"
                f"Respond now · dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🔴 1★ review received on {platform}",
                [
                    f'<strong>{author}</strong> left a 1-star review on {platform}:' if author else f"A 1-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                ],
                cta_label="Respond now",
            )
            blast(sms, f"🔴 1★ review — {restaurant_name}", html, "1star", review.id)

        # 2★ alert
        elif rating == 2 and row["alert_2star"]:
            who  = f"{author}: " if author else ""
            sms  = (
                f"🟠 2★ Review — {restaurant_name}\n"
                f"{who}\"{preview}{ellipsis}\"\n"
                f"dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🟠 2★ review received on {platform}",
                [
                    f'<strong>{author}</strong> left a 2-star review on {platform}:' if author else f"A 2-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                ],
            )
            blast(sms, f"🟠 2★ review — {restaurant_name}", html, "2star", review.id)

    # Negative spike — once per batch, 24h dedup
    if row["alert_neg_spike"] and not _already_alerted_spike(restaurant_id, db_path):
        count = _neg_spike_count(restaurant_id, db_path)
        if count >= 3:
            sms  = (
                f"⚠️ {restaurant_name}: {count} negative reviews in the last 7 days.\n"
                f"Trending issue — check your dashboard · dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"⚠️ Negative review spike detected",
                [
                    f"<strong>{count} negative reviews</strong> have been received in the last 7 days.",
                    "This may indicate a recurring issue worth investigating.",
                ],
            )
            blast(sms, f"⚠️ Negative spike — {restaurant_name}", html, "neg_spike")


def check_no_response_alerts(db_path: str = DB_PATH):
    """
    Called daily by the scheduler. Fires alerts for restaurants with negative
    reviews unresponded for 48+ hours. Fires both email and SMS per restaurant flags.
    """
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT r.restaurant_id, rest.name, rest.owner_email,
               rest.urgent_via_sms, rest.urgent_via_email,
               COUNT(*) as overdue_count
        FROM reviews r
        JOIN restaurants rest ON rest.id = r.restaurant_id
        WHERE r.sentiment='negative'
          AND r.response_status = 'pending'
          AND r.fetched_at <= datetime('now', '-48 hours')
          AND rest.alert_no_response = 1
          AND (rest.urgent_via_sms = 1 OR rest.urgent_via_email = 1)
        GROUP BY r.restaurant_id
    """).fetchall()
    conn.close()

    for row in rows:
        rid         = row["restaurant_id"]
        name        = row["name"]
        n           = row["overdue_count"]
        via_sms     = bool(row["urgent_via_sms"])
        via_email   = bool(row["urgent_via_email"])
        owner_email = row["owner_email"] or ""

        # 24h dedup
        conn2 = get_conn(db_path)
        already = conn2.execute("""
            SELECT id FROM alert_log
            WHERE restaurant_id=? AND alert_type='no_response'
            AND fired_at >= datetime('now', '-24 hours')
        """, (rid,)).fetchone()
        conn2.close()
        if already:
            continue

        review_word = "reviews" if n > 1 else "review"
        sms = (
            f"⏰ {name}: {n} negative {review_word} with no response for 48+ hours.\n"
            f"dashboard.cavnar.ai"
        )
        html = _alert_email_html(
            name,
            f"⏰ {n} negative {review_word} still unresponded",
            [
                f"<strong>{n} negative {review_word}</strong> have been waiting for a response for over 48 hours.",
                "Responding promptly helps protect your rating.",
            ],
            cta_label="View & respond",
        )

        if via_sms:
            contacts = get_alert_contacts(rid, db_path)
            for c in contacts:
                send_sms(c["phone"], sms)

        if via_email and owner_email:
            _send_alert_email(owner_email, f"⏰ Unresponded reviews — {name}", html)

        _log_alert(rid, "no_response")
