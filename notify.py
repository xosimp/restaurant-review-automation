"""
notify.py — SMS alert system via Twilio REST API.
No Twilio SDK needed — uses requests (already in requirements).

Environment variables:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER  (E.164 format, e.g. +13125550100)
"""
import os
import requests
from models import get_conn, DB_PATH

TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM  = os.getenv("TWILIO_FROM_NUMBER", "")

HEALTH_KEYWORDS = [
    "food poison", "food poisoning", "foodborne", "sick after", "got sick",
    "felt sick", "vomit", "threw up", "throw up", "diarrhea", "nausea after",
    "ill after", "hospital", "health department", "health inspector",
    "cockroach", "roach", "rat ", "rats ", "rodent", "bug in ", "insect in",
    "foreign object", "glass in", "metal in", "hair in", "mold", "mouldy",
    "raw chicken", "raw meat", "undercooked chicken", "salmonella", "ecoli", "e. coli",
]


def _normalize_phone(phone: str) -> str:
    """Strip formatting and prepend +1 if no country code."""
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
    """Send a single SMS. Returns True on success."""
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


def send_test_sms(restaurant_id: int) -> dict:
    """Send a test SMS to all contacts for a restaurant. Returns {ok, sent, errors}."""
    contacts = get_alert_contacts(restaurant_id)
    if not contacts:
        return {"ok": False, "error": "No alert contacts configured"}
    from models import get_restaurant
    restaurant = get_restaurant(restaurant_id)
    name = restaurant.name if restaurant else f"Restaurant {restaurant_id}"
    msg = f"✓ Test alert from Cavnar AI\n{name} — alert system is active and working."
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


# ── Alert logic ───────────────────────────────────────────────

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
    """Return True if we already fired a neg-spike alert in the last 24h."""
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


def fire_review_alerts(restaurant_id: int, restaurant_name: str, new_reviews: list, db_path: str = DB_PATH):
    """
    Check newly saved reviews against per-restaurant alert toggles.
    Call this after save_reviews() with the list of newly inserted Review objects.
    """
    if not new_reviews:
        return

    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT alert_1star, alert_2star, alert_health,
               alert_neg_spike, alert_negative_trend, alert_no_response
        FROM restaurants WHERE id=?
    """, (restaurant_id,)).fetchone()
    conn.close()

    if not row:
        return

    contacts = get_alert_contacts(restaurant_id, db_path)
    if not contacts:
        return

    def blast(message: str, alert_type: str, review_id: int = None):
        for c in contacts:
            send_sms(c["phone"], message)
        _log_alert(restaurant_id, alert_type, review_id)

    for review in new_reviews:
        rating  = review.rating or 0
        text    = review.text or ""
        author  = (review.author or "").split()[0]
        platform = (review.platform or "Google").title()
        preview = text[:120].strip()
        ellipsis = "…" if len(text) > 120 else ""

        # Health alert — highest priority, fires regardless of rating toggle
        if row["alert_health"] and _is_health_alert(text):
            msg = (
                f"🚨 HEALTH ALERT — {restaurant_name}\n"
                f"{rating}★ {platform}: \"{preview}{ellipsis}\"\n"
                f"Requires immediate response · dashboard.cavnar.ai"
            )
            blast(msg, "health", review.id)
            continue  # health takes priority, skip lower-tier check for same review

        # 1★ alert
        if rating == 1 and row["alert_1star"]:
            who = f"{author}: " if author else ""
            msg = (
                f"🔴 1★ Review — {restaurant_name}\n"
                f"{who}\"{preview}{ellipsis}\"\n"
                f"Respond now · dashboard.cavnar.ai"
            )
            blast(msg, "1star", review.id)

        # 2★ alert
        elif rating == 2 and row["alert_2star"]:
            who = f"{author}: " if author else ""
            msg = (
                f"🟠 2★ Review — {restaurant_name}\n"
                f"{who}\"{preview}{ellipsis}\"\n"
                f"dashboard.cavnar.ai"
            )
            blast(msg, "2star", review.id)

    # Negative spike check — once per batch, not per review
    if row["alert_neg_spike"] and not _already_alerted_spike(restaurant_id, db_path):
        count = _neg_spike_count(restaurant_id, db_path)
        if count >= 3:
            msg = (
                f"⚠️ {restaurant_name}: {count} negative reviews in the last 7 days.\n"
                f"Trending issue — check your dashboard · dashboard.cavnar.ai"
            )
            blast(msg, "neg_spike")


def check_no_response_alerts(db_path: str = DB_PATH):
    """
    Called by the daily scheduler. Fires alerts for restaurants with
    negative reviews sitting unresponded for 48+ hours.
    """
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT r.restaurant_id, rest.name,
               COUNT(*) as overdue_count
        FROM reviews r
        JOIN restaurants rest ON rest.id = r.restaurant_id
        WHERE r.sentiment='negative'
          AND r.response_status = 'pending'
          AND r.fetched_at <= datetime('now', '-48 hours')
          AND rest.alert_no_response = 1
        GROUP BY r.restaurant_id
    """).fetchall()
    conn.close()

    for row in rows:
        rid  = row["restaurant_id"]
        name = row["name"]
        n    = row["overdue_count"]

        # Don't spam — only once per 24h per restaurant
        conn2 = get_conn(db_path)
        already = conn2.execute("""
            SELECT id FROM alert_log
            WHERE restaurant_id=? AND alert_type='no_response'
            AND fired_at >= datetime('now', '-24 hours')
        """, (rid,)).fetchone()
        conn2.close()
        if already:
            continue

        contacts = get_alert_contacts(rid, db_path)
        if not contacts:
            continue

        msg = (
            f"⏰ {name}: {n} negative review{'s' if n > 1 else ''} "
            f"with no response for 48+ hours.\n"
            f"dashboard.cavnar.ai"
        )
        for c in contacts:
            send_sms(c["phone"], msg)
        _log_alert(rid, "no_response")
